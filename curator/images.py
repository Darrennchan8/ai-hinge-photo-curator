import io
import os
import threading
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}

# Jev's image bounds; Claude accepts anything inside them too.
MIN_SIDE, MAX_SIDE = 280, 1568
MIN_PIXELS, MAX_PIXELS = 78_400, 2_458_624


def open_upright(src) -> Image.Image:
    """Open an image with EXIF rotation applied to the pixels (Jev does not rotate)."""
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def resize_long(img: Image.Image, side: int) -> Image.Image:
    scale = side / max(img.size)
    if scale >= 1:
        return img.copy()
    return img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)


def fit_for_judge(img: Image.Image, long_side: int) -> Image.Image:
    """Resize to `long_side`, then enlarge or letterbox so every Jev size bound holds."""
    img = resize_long(img, long_side)
    if min(img.size) < MIN_SIDE:
        scale = min(MIN_SIDE / min(img.size), MAX_SIDE / max(img.size))
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    if min(img.size) < MIN_SIDE:  # extreme panorama: pad the short side
        w, h = max(img.width, MIN_SIDE), max(img.height, MIN_SIDE)
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
        img = canvas
    assert MIN_PIXELS <= img.width * img.height <= MAX_PIXELS, img.size
    return img


def jpeg_bytes(img: Image.Image, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def judge_jpeg(path: Path, long_side: int) -> bytes:
    return jpeg_bytes(fit_for_judge(open_upright(path), long_side))


def judge_name(src: Path, crop: tuple[int, int, int, int] | None = None) -> str:
    return f"{src.stem}.z{'_'.join(map(str, crop))}.jpg" if crop else f"{src.stem}.jpg"


def judge_file(judge_dir: Path, src: Path, long_side: int, crop: tuple[int, int, int, int] | None = None) -> Path:
    """The exact JPEG a judge is sent for `src` (optionally cropped), made once and cached, so the
    gallery can show precisely what Jev and Claude saw."""
    out = judge_dir / str(long_side) / judge_name(src, crop)
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        img = open_upright(src)
        if crop:
            x, y, w, h = crop
            img = img.crop((x, y, x + w, y + h))
        tmp = out.with_name(f".{out.name}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_bytes(jpeg_bytes(fit_for_judge(img, long_side)))
        tmp.replace(out)
    return out


def ensure_judge_images(cfg, db) -> int:
    """Make the judge-sized image for every photo (and the reference) that doesn't have one yet, so the
    pages can show exactly what the judges see. Returns how many were made."""
    from concurrent.futures import ProcessPoolExecutor

    from .judges.base import reference_path
    from .judging import photos
    side, judge_dir = cfg["judge"]["image_side"], cfg.path("judge")
    views = [(p.path, p.crop) for p in photos(cfg, db, [r["id"] for r in db.all("SELECT id FROM photos")]).values()]
    if ref := reference_path(cfg):
        views.append((ref, None))
    todo = [(src, crop) for src, crop in views if not (judge_dir / str(side) / judge_name(src, crop)).exists()]
    if todo:
        print(f"Making {len(todo)} judge-sized images...")
        with ProcessPoolExecutor(cfg["prefilter"]["workers"]) as pool:
            list(pool.map(judge_file, [judge_dir] * len(todo), [s for s, _ in todo], [side] * len(todo),
                          [c for _, c in todo], chunksize=16))
    return len(todo)
