"""Check the setup before spending anything: packages, config, API keys, input photos and disk space."""

import shutil
import sys

from .config import Config

KEYS = {"jev": "TYPESAFE_API_KEY", "claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
        "gemini_decision": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}
CAN = {  # what each role needs a judge to do, and who can
    "ranker": {"jev", "gemini_decision", "claude", "gemini", "openai", "fake"},
    "classifier": {"jev", "gemini_decision", "fake"},
    "explainer": {"claude", "openai", "fake"},
    "final judge": {"claude", "gemini", "openai", "jev", "gemini_decision", "fake"},
    "opinion": {"claude", "gemini", "openai", "jev", "gemini_decision", "fake"},
}


def run(cfg: Config) -> int:
    """Print one line per check; return 1 if any check failed."""
    failed = 0

    def report(ok: bool | None, text: str) -> None:
        nonlocal failed
        failed += ok is False
        print(f"  {'ok  ' if ok else 'FAIL' if ok is False else 'note'}  {text}")

    print("Packages")
    report(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]} (3.11 or newer)")
    try:
        import cv2
        report(hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF"),
               f"OpenCV {cv2.__version__} with face detection and recognition")
    except ImportError as e:
        report(False, f"OpenCV: {e}")
    for name in ("numpy", "PIL", "pillow_heif", "imagehash", "httpx"):
        try:
            __import__(name)
            report(True, name)
        except ImportError as e:
            report(False, f"{name}: {e}")
    if failed:
        print("\nInstall the packages first: uv sync (see README).")
        return 1

    print("Config")
    report(None, f"settings: config_base.toml + {cfg.overrides}" if cfg.overrides else
           "settings: config_base.toml alone (copy config_overrides.example.toml to config_overrides.toml to change them)")
    if cfg["persona"]["audience"] == _base_audience():
        report(None, "[persona] audience is the generic default; describe who you want to appeal to")
    j, f = cfg["judge"], cfg["final"]
    roles = [("ranker", j["ranker"]), ("classifier", j["classifier"]), ("explainer", j["explainer"]),
             ("final judge", f["judge"]), *(("opinion", name) for name in f["opinions"])]
    used = set()
    for role, name in roles:
        if not name:
            continue
        report(name in CAN[role], f"{role}: {name}" + ("" if name in CAN[role] else
                                                       f" (must be one of {', '.join(sorted(CAN[role]))})"))
        used.add(name)
    if f["opinions"] and not f["judge"]:
        report(False, "[final] opinions need a [final] judge")

    print("API keys (from the environment or .env)")
    for key in sorted({KEYS[name] for name in used & set(KEYS)}):
        report(bool(cfg.secret(key)), f"{key} for {', '.join(sorted(n for n in used if KEYS.get(n) == key))}")

    print("Photos")
    from .images import PHOTO_EXTS
    from .ingest import _is_archive
    input_dir, ref_dir = cfg.path("input"), cfg.path("reference")
    files = [p for p in input_dir.rglob("*") if p.is_file() and ref_dir not in p.parents] if input_dir.exists() else []
    archives = sum(map(_is_archive, files))
    loose = sum(p.suffix.lower() in PHOTO_EXTS for p in files)
    report(bool(archives or loose) or None,
           f"{input_dir}: {archives} archives, {loose} loose photos" if files else
           f"nothing in {input_dir} yet (or pass archives, folders or rclone remotes to `run`/`ingest`)")
    refs = [p for p in ref_dir.glob("*") if p.suffix.lower() in PHOTO_EXTS] if ref_dir.exists() else []
    report(True if refs else None, f"{len(refs)} reference photos in {ref_dir}" if refs else
           f"no reference photos in {ref_dir}; your face will be detected automatically (best when every "
           "photo is of you, as in an export filtered to your face)")
    from .prefilter import MODELS
    have = all((cfg.path("models") / name).exists() for name in MODELS)
    report(True if have else None, "face models present" if have else "face models download on the first prefilter")
    work = cfg.path("db").parent
    free = shutil.disk_usage(next(p for p in [work, *work.parents] if p.exists())).free / 1024 ** 3
    report(free >= cfg["ingest"]["min_free_gb"] + 1, f"{free:.1f} GB free (working copies take ~0.4 MB per photo)")

    if cfg["publish"]["www_dir"]:
        print("Publish")
        www = cfg.base / cfg["publish"]["www_dir"]
        report(www.is_dir(), f"www_dir {www} exists")
        report(bool(cfg["publish"]["base_url"]) or None, "base_url is set" if cfg["publish"]["base_url"] else
               "no base_url; publish will print a file path instead of a link")

    print("\nAll good." if not failed else f"\n{failed} problem(s) to fix first.")
    return 1 if failed else 0


def _base_audience() -> str:
    from .config import load
    return load(None)["persona"]["audience"]
