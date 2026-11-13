"""Unpack a Google Takeout export into resized, upright working copies.

Reads .zip, .tgz/.tar.gz and loose files. Archives are streamed member by member, so an export can
be read straight off a USB drive or network share without copying it to local disk first. The path
"-" reads a .tgz from stdin. An rclone path like "gdrive:Photos of me" reads from cloud storage:
archives are streamed (zips are fetched one at a time), loose files are fetched in small batches,
and videos and already-ingested files are never downloaded.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath

from PIL import Image

from .config import Config
from .db import DB
from .images import PHOTO_EXTS, open_upright, resize_long

TAR_SUFFIXES = (".tgz", ".tar.gz", ".tar")


def _wanted(name: str) -> bool:
    return name.endswith(".json") or PurePosixPath(name).suffix.lower() in PHOTO_EXTS


def _is_archive(path: Path) -> bool:
    return path.suffix.lower() == ".zip" or path.name.lower().endswith(TAR_SUFFIXES)


def _tar_stream(fileobj) -> Iterator[tuple[str, Callable[[], bytes]]]:
    # Stream mode ("r|*"): one sequential pass, no seeking, works for any compression.
    with tarfile.open(fileobj=fileobj, mode="r|*") as tf:
        for m in tf:
            if m.isfile() and _wanted(m.name):
                yield m.name, lambda m=m: tf.extractfile(m).read()  # must be called before advancing


def _archive(path: Path) -> Iterator[tuple[str, Callable[[], bytes]]]:
    print(f"Reading {'stdin' if str(path) == '-' else path}...")
    if str(path) == "-":
        yield from _tar_stream(sys.stdin.buffer)
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if _wanted(name):
                    yield name, lambda name=name: zf.read(name)
    else:
        with open(path, "rb") as f:
            yield from _tar_stream(f)


def is_remote(spec: str) -> bool:
    """'gdrive:Photos of me' is an rclone remote; '-' and existing local paths are not."""
    return spec != "-" and bool(re.match(r"^[\w.-]+:", spec)) and not Path(spec).exists()


def _join(remote: str, rel: str) -> str:
    return remote + ("" if remote.endswith((":", "/")) else "/") + rel


class _Rclone:
    def __init__(self, cfg: Config):
        rc = cfg["rclone"]
        self.exe = shutil.which(os.path.expanduser(rc["binary"])) or shutil.which("rclone")
        if not self.exe:
            raise SystemExit(f"rclone not found at {rc['binary']}; set [rclone] binary in config_overrides.toml.")
        self.batch, self.transfers = rc["batch"], rc["transfers"]
        self.tmp = cfg.path("tmp")
        self.min_free = cfg["ingest"]["min_free_gb"] * 1024 ** 3

    def run(self, *args: str) -> str:
        r = subprocess.run([self.exe, *args], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"rclone {args[0]} failed: {r.stderr.strip()[-800:]}")
        return r.stdout

    def stream_tar(self, spec: str) -> Iterator[tuple[str, Callable[[], bytes]]]:
        proc = subprocess.Popen([self.exe, "cat", spec], stdout=subprocess.PIPE)
        try:
            yield from _tar_stream(proc.stdout)
        finally:
            proc.stdout.close()
            proc.kill() if proc.poll() is None else None
            proc.wait()

    def fetch_zip(self, spec: str, size: int) -> Iterator[tuple[str, Callable[[], bytes]]]:
        """Zips need random access, so download one, read it, delete it."""
        self.tmp.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.tmp).free - size < self.min_free:
            raise SystemExit(f"Not enough disk to fetch {spec} ({size / 1e9:.1f} GB). Use smaller Takeout "
                             "parts or the .tgz format, which streams without touching disk.")
        local = self.tmp / "remote.zip"
        try:
            self.run("copyto", spec, str(local))
            yield from _archive(local)
        finally:
            local.unlink(missing_ok=True)

    def fetch_files(self, remote: str, rels: list[str]) -> Iterator[tuple[str, Callable[[], bytes]]]:
        """Download loose files in batches into a scratch folder, yield them, then clear it."""
        for start in range(0, len(rels), self.batch):
            batch = rels[start:start + self.batch]
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp.mkdir(parents=True)
            listing = self.tmp.parent / "rclone-files.txt"
            listing.write_text("\n".join(batch) + "\n")
            self.run("copy", remote, str(self.tmp), "--files-from-raw", str(listing),
                     "--transfers", str(self.transfers), "--checkers", str(self.transfers))
            for rel in batch:
                path = self.tmp / rel
                if path.exists():
                    yield rel, path.read_bytes
            print(f"  fetched {min(start + self.batch, len(rels))}/{len(rels)} files")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sources(self, spec: str, skip: set[str]) -> Iterator[tuple[str, Callable[[], bytes]]]:
        print(f"Reading {spec}...")
        lower = spec.lower()
        if lower.endswith(".zip"):
            size = json.loads(self.run("lsjson", "--no-mimetype", "--no-modtime", spec))[0]["Size"]
            yield from self.fetch_zip(spec, size)
            return
        if lower.endswith(TAR_SUFFIXES):
            yield from self.stream_tar(spec)
            return
        items = json.loads(self.run("lsjson", "-R", "--files-only", "--no-mimetype", "--no-modtime", spec))
        for it in sorted(items, key=lambda it: it["Path"]):
            if it["Path"].lower().endswith((".zip", *TAR_SUFFIXES)):
                yield from self.sources(_join(spec, it["Path"]), skip)
        loose = [it["Path"] for it in items if _wanted(it["Path"]) and it["Path"] not in skip]
        skipped = sum(1 for it in items if not _wanted(it["Path"]) and not it["Path"].lower().endswith(
            (".zip", *TAR_SUFFIXES)))
        print(f"  {len(loose)} new photos/sidecars to fetch; {skipped} videos/other files ignored")
        yield from self.fetch_files(spec, loose)


def _sources(cfg: Config, extra: list[str], skip: set[str]) -> Iterator[tuple[str, Callable[[], bytes]]]:
    """Yield (name, loader) for photos and JSON sidecars in input/ plus any extra paths or remotes."""
    input_dir, reference_dir = cfg.path("input"), cfg.path("reference")
    for spec in [str(input_dir), *extra]:
        if is_remote(spec):
            yield from _Rclone(cfg).sources(spec, skip)
            continue
        root = Path(spec)
        if spec == "-" or root.is_file():
            yield from _archive(root)
            continue
        files = sorted(p for p in root.rglob("*") if p.is_file() and reference_dir not in p.parents)
        for p in files:
            if _is_archive(p):
                yield from _archive(p)
        for p in files:
            name = p.relative_to(root).as_posix()
            if not _is_archive(p) and _wanted(name):
                yield name, p.read_bytes


def _sidecar(name: str, data: bytes) -> tuple[str, int | None, str | None] | None:
    """('dir/IMG_1.JPG', photoTakenTime, Google Photos URL) from a Takeout JSON sidecar.

    Takeout names sidecars 'IMG_1.JPG.json' or 'IMG_1.JPG.supplemental-metadata.json', sometimes
    truncated, so key them by the JSON's own 'title' field when present.
    """
    try:
        meta = json.loads(data)
    except ValueError:
        return None
    if not isinstance(meta, dict):
        return None
    try:
        ts = int(meta["photoTakenTime"]["timestamp"])
    except (KeyError, ValueError, TypeError):
        ts = None
    url = meta.get("url") if str(meta.get("url", "")).startswith("https://photos.google.com/") else None
    if ts is None and url is None:
        return None
    p = PurePosixPath(name)
    return f"{p.parent}/{sidecar_photo_name(p.name, meta.get('title'))}", ts, url


def sidecar_photo_name(sidecar: str, title: str | None) -> str:
    """The photo filename a Takeout sidecar describes.

    The JSON 'title' holds the full original name (the sidecar's own filename may be truncated),
    but when two photos share a name Takeout saves the second as 'IMG_1(1).HEIC' with sidecar
    'IMG_1.HEIC.supplemental-metadata(1).json' (or older 'IMG_1.HEIC(1).json') and the same
    title 'IMG_1.HEIC'. So the '(n)' comes from the sidecar's filename.
    """
    m = re.search(r"(\(\d+\))\.json$", sidecar)
    title = title or re.sub(r"(\.supplemental-metadata)?(\(\d+\))?\.json$", "", sidecar)
    if not m or title.endswith(m.group(1)):
        return title
    stem, dot, ext = title.rpartition(".")
    return f"{stem}{m.group(1)}.{ext}" if dot else f"{title}{m.group(1)}"


def _sidecar_for(src: str, sidecars: dict):
    p = PurePosixPath(src)
    key = f"{p.parent}/{p.name}"
    if key in sidecars:
        return sidecars[key]
    # Edited copies share the original's sidecar: IMG_1-edited.jpg -> IMG_1.jpg
    if "-edited" in p.stem:
        return sidecars.get(f"{p.parent}/{p.stem.replace('-edited', '')}{p.suffix}")
    return None


def _apply_sidecars(db: DB) -> int:
    """Sidecar dates beat EXIF. Done after reading because a split export may put a photo's
    sidecar in a different archive part, or after the photo in a tar stream."""
    sidecars = {r["key"]: r for r in db.all("SELECT key, taken_at, url FROM sidecars")}
    updates = [(sc["taken_at"], sc["url"], r["id"]) for r in db.all("SELECT id, src FROM photos")
               if (sc := _sidecar_for(r["src"], sidecars)) is not None]
    db.executemany("UPDATE photos SET taken_at = COALESCE(?, taken_at), url = COALESCE(?, url) WHERE id = ?",
                   updates)
    return len(updates)


def _exif_time(img) -> int | None:
    try:
        raw = img.getexif().get_ifd(0x8769).get(36867)  # DateTimeOriginal
        return int(datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def _process(data: bytes, work: str, thumbs: str, max_side: int,
             quality: int, thumb_side: int) -> tuple[str, int | None, int, int]:
    pid = hashlib.sha1(data).hexdigest()[:16]
    taken_at = _exif_time(Image.open(BytesIO(data)))
    img = open_upright(BytesIO(data))
    work_img = resize_long(img, max_side)
    work_img.save(Path(work) / f"{pid}.jpg", "JPEG", quality=quality)
    resize_long(work_img, thumb_side).save(Path(thumbs) / f"{pid}.jpg", "JPEG", quality=82)
    return pid, taken_at, work_img.width, work_img.height


def run(cfg: Config, db: DB, sample: int | None = None, extra: list[str] | None = None) -> None:
    work, thumbs = cfg.path("work"), cfg.path("thumbs")
    work.mkdir(parents=True, exist_ok=True)
    thumbs.mkdir(parents=True, exist_ok=True)
    seen_src = {r["src"] for r in db.all("SELECT src FROM photos")}
    ing = cfg["ingest"]
    args = (str(work), str(thumbs), ing["max_side"], ing["jpeg_quality"], ing["thumb_side"])
    min_free = ing["min_free_gb"] * 1024 ** 3

    done = failed = 0
    pending = {}
    with ProcessPoolExecutor(cfg["prefilter"]["workers"]) as pool:
        def drain(block_until_below: int):
            nonlocal done, failed
            while len(pending) > block_until_below:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in finished:
                    name = pending.pop(fut)
                    try:
                        pid, taken_at, w, h = fut.result()
                    except Exception as e:  # corrupt or unsupported file
                        failed += 1
                        print(f"  skip {name}: {e}")
                        continue
                    db.execute("INSERT OR IGNORE INTO photos (id, src, taken_at, width, height) VALUES (?,?,?,?,?)",
                               (pid, name, taken_at, w, h))
                    done += 1
                    if done % 100 == 0:
                        print(f"  ingested {done}")

        seen_json = {r["src"] for r in db.all("SELECT src FROM sidecars WHERE src IS NOT NULL")}
        for name, load in _sources(cfg, extra or [], seen_src | seen_json):
            if name.endswith(".json"):
                if name not in seen_json and (sc := _sidecar(name, load())):
                    db.execute("INSERT OR REPLACE INTO sidecars (key, taken_at, url, src) VALUES (?, ?, ?, ?)",
                               (*sc, name))
                continue
            if name in seen_src:
                continue
            if sample is not None and done + len(pending) >= sample:
                break
            if shutil.disk_usage(work).free < min_free:
                print(f"Stopping: less than {ing['min_free_gb']} GB free on disk. Free space and re-run; "
                      "finished photos are kept and skipped next time.")
                break
            pending[pool.submit(_process, load(), *args)] = name
            seen_src.add(name)  # the same file can appear in more than one archive or part
            drain(block_until_below=cfg["prefilter"]["workers"] * 2)
        drain(block_until_below=0)

    dated = _apply_sidecars(db)
    total = db.one("SELECT COUNT(*) n FROM photos")["n"]
    linked = db.one("SELECT COUNT(*) n FROM photos WHERE url IS NOT NULL")["n"]
    print(f"Ingested {done} new photos ({failed} failed); {total} photos in library, {dated} matched to Takeout "
          f"metadata, {linked} with Google Photos links.")
