"""Local, free filtering: find the user's face, drop blurry/tiny photos, collapse near-duplicates."""

import json
import time
from concurrent.futures import ProcessPoolExecutor

import cv2
import imagehash
import numpy as np
from PIL import Image

from .config import Config
from .db import DB, dumps
from .images import PHOTO_EXTS, open_upright, resize_long

_det = _rec = None
SCAN_VERSION = 2  # 2: also detect on a 640 px copy, which catches faces too large for full resolution


def _init(models: str, detect_score: float) -> None:
    global _det, _rec
    _det = cv2.FaceDetectorYN.create(f"{models}/face_detection_yunet_2023mar.onnx", "", (320, 320), detect_score)
    _rec = cv2.FaceRecognizerSF.create(f"{models}/face_recognition_sface_2021dec.onnx", "")


def _detect(bgr: np.ndarray) -> np.ndarray:
    _det.setInputSize((bgr.shape[1], bgr.shape[0]))
    _, faces = _det.detect(bgr)
    return faces if faces is not None else np.zeros((0, 15), np.float32)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / (a[2] * a[3] + b[2] * b[3] - inter + 1e-9)


def _faces(bgr: np.ndarray, small_side: int = 640) -> np.ndarray:
    """Detect at full resolution and on a downscaled copy, merged. YuNet misses faces that fill much
    of a large frame (close-ups and selfies), and those are found at the smaller scale."""
    faces = _detect(bgr)
    scale = small_side / max(bgr.shape[:2])
    if scale >= 1:
        return faces
    small = cv2.resize(bgr, (round(bgr.shape[1] * scale), round(bgr.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    extra = []
    for f in _detect(small):
        f = f.copy()
        f[:14] /= scale  # box and landmarks back to full-resolution pixels
        if all(_iou(f[:4], g[:4]) < 0.3 for g in faces):
            extra.append(f)
    return np.vstack([faces, *extra]) if extra else faces


def _embed(bgr: np.ndarray, face: np.ndarray) -> np.ndarray:
    feat = _rec.feature(_rec.alignCrop(bgr, face)).flatten()
    return (feat / np.linalg.norm(feat)).astype(np.float32)


def _sharpness(bgr: np.ndarray, box) -> float:
    x, y, w, h = (int(v) for v in box)
    crop = bgr[max(y, 0):y + h, max(x, 0):x + w]
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (max(1, round(gray.shape[1] * 256 / gray.shape[0])), 256))
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _analyze(pid: str, work_path: str, thumb_path: str) -> dict:
    bgr = cv2.imread(work_path)
    faces = _faces(bgr)
    return {
        "photo_id": pid,
        "phash": str(imagehash.phash(Image.open(thumb_path))),
        "faces": [{"box": [float(v) for v in f[:4]], "frac": float(f[3] / bgr.shape[0]),
                   "sharpness": _sharpness(bgr, f[:4]), "emb": _embed(bgr, f).tobytes()} for f in faces],
    }


MODELS = {
    "face_detection_yunet_2023mar.onnx": "face_detection_yunet",
    "face_recognition_sface_2021dec.onnx": "face_recognition_sface",
}


def ensure_models(cfg: Config) -> None:
    """Download the OpenCV Zoo face models on first use (they're not kept in git)."""
    import urllib.request
    models = cfg.path("models")
    models.mkdir(parents=True, exist_ok=True)
    for name, folder in MODELS.items():
        if not (models / name).exists():
            print(f"Downloading {name}...")
            url = f"https://github.com/opencv/opencv_zoo/raw/main/models/{folder}/{name}"
            tmp = models / f"{name}.part"
            urllib.request.urlretrieve(url, tmp)
            tmp.rename(models / name)


def _file_references(cfg: Config) -> np.ndarray | None:
    ref_dir = cfg.path("reference")
    paths = sorted(p for p in ref_dir.glob("*") if p.suffix.lower() in PHOTO_EXTS) if ref_dir.exists() else []
    if not paths:
        return None
    _init(str(cfg.path("models")), cfg["prefilter"]["detect_score"])
    embs = []
    for p in paths:
        bgr = cv2.cvtColor(np.asarray(resize_long(open_upright(p), 1568)), cv2.COLOR_RGB2BGR)
        faces = _faces(bgr)
        if not len(faces):
            print(f"  warning: no face found in reference {p.name}")
            continue
        embs.append(_embed(bgr, faces[np.argmax(faces[:, 2] * faces[:, 3])]))
    if not embs:
        raise SystemExit(f"No faces found in any photo in {ref_dir}.")
    print(f"Using {len(embs)} reference face(s) from {ref_dir}.")
    return np.stack(embs)


def _auto_references(cfg: Config, db: DB) -> np.ndarray:
    """With no reference photos, the face that recurs most across solo photos is taken to be the user,
    as it is in an export filtered to their face. Its most typical instances become the references, and
    the best of them is saved as the reference image shown to judges."""
    pf = cfg["prefilter"]
    rows = db.all(
        "SELECT e.photo_id, e.box, e.emb FROM face_embeddings e JOIN faces f ON f.photo_id = e.photo_id "
        "WHERE f.n_faces = 1 AND e.frac >= ? AND e.sharpness >= ?", (0.08, pf["auto_ref_min_sharpness"]))
    if len(rows) < 5:
        raise SystemExit(f"Only {len(rows)} clear solo photos; add 3-5 photos of just you to "
                         f"{cfg.path('reference')} instead.")
    embs = np.stack([np.frombuffer(r["emb"], np.float32) for r in rows])
    support = ((embs @ embs.T) >= pf["match_threshold"]).sum(axis=1)
    order = np.argsort(-support)
    best = rows[int(order[0])]
    share = support[order[0]] / len(rows)
    print(f"Auto reference: the most common face matches {support[order[0]]} of {len(rows)} clear solo photos "
          f"({share:.0%}).")
    if share < 0.3:
        print("  warning: no clearly dominant face; consider adding reference photos.")
    x, y, w, h = json.loads(best["box"])
    img = Image.open(cfg.path("work") / f"{best['photo_id']}.jpg")
    pad = 0.6 * max(w, h)
    img.crop((max(0, x - pad), max(0, y - pad), min(img.width, x + w + pad),
              min(img.height, y + h + pad))).save(cfg.path("auto_reference"), quality=92)
    return embs[order[: pf["auto_references"]]]


def find_moments(rows: list[dict], gap_s: int, max_hamming: int, max_gap_s: int = 3 * 3600) -> list[list[str]]:
    """Group photos into moments: taken within `gap_s` of the previous one (chained), or visually
    near-identical (phash) within a few hours or with an unknown date, which catches edited copies.
    Returns every moment, singletons included, each sorted by time."""
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: (r["taken_at"] is None, r["taken_at"] or 0))
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    times = np.array([r["taken_at"] if r["taken_at"] is not None else -1 for r in rows], dtype=np.int64)
    for i in range(1, len(rows)):
        if times[i] >= 0 and times[i - 1] >= 0 and times[i] - times[i - 1] <= gap_s:
            parent[find(i)] = find(i - 1)
    bits = np.array([[int(c, 16) for c in r["phash"]] for r in rows], dtype=np.uint8)
    bits = np.unpackbits(bits[:, :, None], axis=2)[:, :, 4:].reshape(len(rows), -1)  # 64 bits
    for i in range(len(rows)):
        dist = np.count_nonzero(bits[i + 1:] != bits[i], axis=1)
        gap = np.abs(times[i + 1:] - times[i])
        unknown = (times[i + 1:] < 0) | (times[i] < 0)
        for j in np.nonzero((dist <= max_hamming) & (unknown | (gap <= max_gap_s)))[0] + i + 1:
            parent[find(int(j))] = find(i)

    moments: dict[int, list[str]] = {}
    for i in range(len(rows)):
        moments.setdefault(find(i), []).append(rows[i]["photo_id"])
    return list(moments.values())



def analyze(cfg: Config, db: DB) -> None:
    pf = cfg["prefilter"]
    todo = [r["id"] for r in db.all(
        "SELECT id FROM photos WHERE id NOT IN (SELECT photo_id FROM faces WHERE COALESCE(scan, 1) >= ?)",
        (SCAN_VERSION,))]
    if not todo:
        return
    ensure_models(cfg)
    work, thumbs = cfg.path("work"), cfg.path("thumbs")
    print(f"Analyzing faces in {len(todo)} photos...")
    with ProcessPoolExecutor(pf["workers"], initializer=_init,
                             initargs=(str(cfg.path("models")), pf["detect_score"])) as pool:
        futs = [pool.submit(_analyze, pid, str(work / f"{pid}.jpg"), str(thumbs / f"{pid}.jpg")) for pid in todo]
        for n, fut in enumerate(futs, 1):
            r = fut.result()
            pid = r["photo_id"]
            db.execute("DELETE FROM face_embeddings WHERE photo_id = ?", (pid,))
            db.executemany("INSERT OR REPLACE INTO face_embeddings VALUES (?,?,?,?,?,?)",
                           [(pid, k, dumps(f["box"]), f["frac"], f["sharpness"], f["emb"])
                            for k, f in enumerate(r["faces"])])
            db.execute("INSERT OR REPLACE INTO faces (photo_id, n_faces, matched, scan) VALUES (?, ?, 0, ?)",
                       (pid, len(r["faces"]), SCAN_VERSION))
            db.execute("INSERT OR REPLACE INTO prefilter (photo_id, status, phash) VALUES (?, 'pending', ?)",
                       (pid, r["phash"]))
            if n % 100 == 0:
                print(f"  {n}/{len(todo)}")


def _match_faces(cfg: Config, db: DB) -> None:
    """Find the user's face in every photo against the references and summarize it in `faces`."""
    refs = _file_references(cfg)
    if refs is None:
        refs = _auto_references(cfg, db)
    by_photo: dict[str, list] = {}
    for r in db.all("SELECT photo_id, idx, box, frac, sharpness, emb FROM face_embeddings ORDER BY photo_id, idx"):
        by_photo.setdefault(r["photo_id"], []).append(r)
    updates = []
    for pid, faces in by_photo.items():
        sims = [float(np.max(refs @ np.frombuffer(f["emb"], np.float32))) for f in faces]
        best = int(np.argmax(sims))
        areas = [json.loads(f["box"])[2] * json.loads(f["box"])[3] for f in faces]
        f = faces[best]
        updates.append((sims[best], f["frac"], int(best == int(np.argmax(areas))), f["sharpness"], f["box"], pid))
    db.executemany("UPDATE faces SET similarity = ?, face_frac = ?, is_largest = ?, sharpness = ?, box = ? "
                   "WHERE photo_id = ?", updates)


def decide(cfg: Config, db: DB) -> list[list[str]]:
    """Apply thresholds to stored measurements (cheap, re-runnable) and group survivors into moments.

    In an export already filtered to the user's face, a photo where we can't find that face is kept as
    'unlocated' rather than rejected ([prefilter] keep_unlocated); size and blur checks only apply once
    we've found it.
    Near-duplicates are not dropped here: every photo records its moment, and after classification
    the playoff keeps the best photo of each category within each moment. Returns all moments.
    """
    pf = cfg["prefilter"]
    _match_faces(cfg, db)
    rows = [dict(r) for r in db.all(
        "SELECT f.*, p.phash, ph.taken_at FROM faces f JOIN prefilter p ON p.photo_id = f.photo_id "
        "JOIN photos ph ON ph.id = f.photo_id")]
    decisions: dict[str, tuple] = {}
    candidates = []
    oldest = time.time() - pf["max_age_years"] * 365.25 * 86400 if pf.get("max_age_years") else None
    for r in rows:
        if oldest is not None and r["taken_at"] is not None and r["taken_at"] < oldest:
            decisions[r["photo_id"]] = ("reject", "too_old")
            continue
        bar = pf["solo_match_threshold"] if r["n_faces"] == 1 else pf["match_threshold"]
        matched = r["similarity"] is not None and r["similarity"] >= bar
        db.execute("UPDATE faces SET matched = ? WHERE photo_id = ?", (int(matched), r["photo_id"]))
        if not matched and not pf.get("keep_unlocated", True):
            decisions[r["photo_id"]] = ("reject", "not_you")
            continue
        if not matched:
            decisions[r["photo_id"]] = ("keep", "unlocated")
        elif r["face_frac"] < pf["min_face_frac"]:
            decisions[r["photo_id"]] = ("reject", "face_too_small")
            continue
        elif r["sharpness"] < pf["min_sharpness"]:
            decisions[r["photo_id"]] = ("reject", "blurry")
            continue
        candidates.append(r)

    moments = find_moments(candidates, pf["moment_gap_s"], pf["dup_hamming"])
    moment_of = {pid: m[0] for m in moments for pid in m}  # a moment is named by its earliest photo
    db.executemany("UPDATE prefilter SET status = ?, reason = ?, dup_of = NULL, moment = ? WHERE photo_id = ?",
                   [(*decisions.get(r["photo_id"], ("keep", None)), moment_of.get(r["photo_id"]), r["photo_id"])
                    for r in rows])
    return moments


def run(cfg: Config, db: DB) -> None:
    analyze(cfg, db)
    moments = decide(cfg, db)
    counts = db.all("SELECT COALESCE(reason, 'kept') k, COUNT(*) n FROM prefilter GROUP BY 1 ORDER BY 2 DESC")
    print("Prefilter: " + ", ".join(f"{r['k']} {r['n']}" for r in counts))
    multi = [m for m in moments if len(m) > 1]
    print(f"{len(moments)} moments ({len(multi)} with several photos, largest {max(map(len, moments), default=0)}). "
          "Next: `classify`, then `playoff` keeps the best of each category in each moment.")
    from . import report
    print(f"Contact sheet: {report.prefilter_sheet(cfg, db)}")
