"""Copy the gallery pages and judge images into a folder a web server already serves, so you can open the
gallery on your phone.

Anything in that folder is usually readable by anyone who can reach the server, so the copy goes under a
random, unguessable folder name (kept in data/, out of git) and holds only the pages and the judge-sized
images. Full-size photos stay behind their Google Photos links. Don't publish to a server reachable from
the internet unless you're comfortable with that.
"""

import secrets
import shutil

from .config import Config


def _token(cfg: Config) -> str:
    path = cfg.path("db").parent / "publish-token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(16))
        path.chmod(0o600)
    return path.read_text().strip()


def _sync(src, dst) -> int:
    """Copy new or changed files from src into dst (same relative layout); return files copied."""
    if not src.exists():
        return 0
    copied = 0
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        target = dst / f.relative_to(src)
        st = f.stat()
        if target.exists() and target.stat().st_size == st.st_size and target.stat().st_mtime >= st.st_mtime:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        copied += 1
    return copied


def run(cfg: Config, quiet: bool = False) -> None:
    pc = cfg["publish"]
    if not pc["www_dir"]:
        raise SystemExit("Set [publish] www_dir (and base_url) in config_overrides.toml first, or just open "
                         f"{cfg.path('out') / 'index.html'}.")
    folder = f"photo-curator-{_token(cfg)}"
    root = cfg.base / pc["www_dir"] / folder
    # Mirror the layout the pages expect: out/*.html link to ../data/judge/ and ../data/crops/.
    n = _sync(cfg.path("out"), root / "out")
    for name in ("judge", "crops"):
        n += _sync(cfg.path(name), root / cfg["paths"][name])
    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    url = f"{pc['base_url'].rstrip('/')}/{folder}/out/index.html" if pc["base_url"] else str(root / "out" / "index.html")
    if quiet:
        print(f"Gallery updated: {url}")
        return
    print(f"Published {n} new/changed files ({size / 1e6:.0f} MB total) to {root}")
    print(f"Open: {url}")
    print("Anyone who can reach that server and knows the folder name can see these photos.")
