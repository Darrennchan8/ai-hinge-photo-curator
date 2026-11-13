"""Keep two categories honest with a direct yes/no per photo, because classification put waist-up
shots in both. Failures move to upper_body; then that category pair's playoff and rank are redone."""

import json

from . import playoff, rank
from .config import Config
from .db import DB, dumps
from .judges import make
from .judging import check_budget, parallel, photos

# bucket -> (flag holding the answer, statement, lowest probability that stays in the bucket). Jev leans
# towards yes on the close-up question: nothing scored under 0.53, and by eye the waist-up and chest-up
# shots end around 0.80.
CHECKS = {
    "full_body": ("shows_full_body",
                  "The profile owner's whole body is visible in the photo, from their head to at least their knees",
                  0.5),
    "headshot": ("shows_close_up",
                 "This is a close-up of the profile owner: their head and shoulders fill much of the frame, "
                 "and little or nothing of them is visible below the chest",
                 0.8),
}


def run(cfg: Config, db: DB, bucket: str) -> None:
    flag, statement, threshold = CHECKS[bucket]
    judge = make(cfg, cfg["judge"]["classifier"])
    rows = db.all("SELECT c.photo_id, c.flags FROM classify c JOIN prefilter p ON p.photo_id = c.photo_id "
                  "WHERE c.judge = ? AND c.bucket = ? AND p.status = 'keep'", (judge.name, bucket))
    todo = {r["photo_id"]: json.loads(r["flags"]) for r in rows if flag not in json.loads(r["flags"])}
    print(f"Checking {len(todo)} {bucket} photos with {judge.name}...")
    lookup = photos(cfg, db, todo)

    def one(pid):
        check_budget(cfg, db, judge.name)
        p, tokens = judge.yes_no(lookup[pid], statement)
        db.execute("UPDATE classify SET flags = ? WHERE photo_id = ? AND judge = ?",
                   (dumps({**todo[pid], flag: p}), pid, judge.name))
        db.execute("INSERT INTO spend (judge, tokens_in, tokens_out) VALUES (?, ?, 0)", (judge.name, tokens))

    parallel(cfg, list(todo), one, label=f"{bucket} check")
    # Apply the threshold to every stored answer, so changing it needs no new calls.
    failed = [r["photo_id"] for r in db.all("SELECT photo_id, flags FROM classify WHERE judge = ? AND bucket = ?",
                                            (judge.name, bucket))
              if json.loads(r["flags"]).get(flag, 1) < threshold]
    db.executemany("UPDATE classify SET bucket = 'upper_body' WHERE photo_id = ? AND judge = ?",
                   [(pid, judge.name) for pid in failed])
    print(f"  moved {len(failed)} to upper_body")
    affected = {bucket, "upper_body"}
    for r in db.all("SELECT bucket, COUNT(*) n FROM classify WHERE judge = ? AND bucket IN (?, ?) GROUP BY 1",
                    (judge.name, *sorted(affected))):
        print(f"  {r['bucket']}: {r['n']}")
    playoff.run(cfg, db, only=affected)
    for name in sorted(affected):
        rank.run(cfg, db, only=name)
