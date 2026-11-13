"""One-line rationale and optional crop for the shortlist."""

from PIL import Image

from .config import Config
from .db import DB, dumps
from .judges import make
from .judging import check_budget, parallel, photos
from .report import ranked, suggested_profile


def run(cfg: Config, db: DB, redo: bool = False) -> None:
    """Write rationales for the shortlist. `redo` rewrites them, along with any older ones still on file."""
    if not cfg["judge"]["explainer"]:
        print("No explainer set ([judge] explainer); skipping rationales.")
        return
    judge = make(cfg, cfg["judge"]["explainer"])
    if not hasattr(judge, "explain"):
        raise SystemExit(f"The {judge.name} judge can't write explanations; set [judge] explainer to claude or openai.")
    per = cfg["judge"]["explain_per_bucket"]
    shortlist = {r["photo_id"]: r["bucket"] for r in suggested_profile(cfg, db)}
    for bucket, rows in ranked(cfg, db).items():
        shortlist.update({r["photo_id"]: bucket for r in rows[:per]})
    done = {r["photo_id"] for r in db.all("SELECT photo_id FROM explanations")}
    if redo:
        buckets = {r["photo_id"]: r["bucket"] for r in db.all("SELECT photo_id, bucket FROM classify WHERE judge = ?",
                                                              (cfg["judge"]["classifier"],))}
        shortlist.update({pid: buckets[pid] for pid in done - set(shortlist) if pid in buckets})
        done = set()
    todo = [pid for pid in shortlist if pid not in done]
    print(f"Explaining {len(todo)} shortlisted photos with {judge.name}...")
    lookup = photos(cfg, db, todo)
    crops = cfg.path("crops")
    crops.mkdir(parents=True, exist_ok=True)

    def one(pid):
        check_budget(cfg, db, judge.name)
        reason, crop, usage = judge.explain(lookup[pid], shortlist[pid])
        db.execute("INSERT INTO spend (judge, tokens_in, tokens_out) VALUES (?,?,?)", (judge.name, *usage))
        if crop and all(k in crop for k in ("left", "top", "right", "bottom")) \
                and crop["right"] > crop["left"] and crop["bottom"] > crop["top"]:
            img = Image.open(lookup[pid].path)
            w, h = img.size
            img.crop((round(crop["left"] * w), round(crop["top"] * h),
                      round(crop["right"] * w), round(crop["bottom"] * h))).save(crops / f"{pid}.jpg", quality=92)
        else:
            crop = None
        if not crop:
            (crops / f"{pid}.jpg").unlink(missing_ok=True)
        db.execute("INSERT OR REPLACE INTO explanations VALUES (?,?,?)", (pid, reason, dumps(crop) if crop else None))

    parallel(cfg, todo, one, label="explain")
