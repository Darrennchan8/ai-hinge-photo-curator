"""Assign each kept photo a bucket and flag probabilities."""

import time

from .config import Config
from .db import DB, dumps
from .judges import make
from .judging import check_budget, parallel, photos


def run(cfg: Config, db: DB, judge_name: str | None = None, limit: int | None = None,
        ids: list[str] | None = None, quiet: bool = False) -> None:
    judge = make(cfg, judge_name or cfg["judge"]["classifier"])
    done = {r["photo_id"] for r in db.all("SELECT photo_id FROM classify WHERE judge = ?", (judge.name,))}
    if ids is None:
        ids = [r["photo_id"] for r in db.all("SELECT photo_id FROM prefilter WHERE status = 'keep' ORDER BY photo_id")]
    ids = [pid for pid in ids if pid not in done][:limit]
    print(f"Classifying {len(ids)} photos with {judge.name}...")
    lookup = photos(cfg, db, ids)

    def one(pid):
        check_budget(cfg, db, judge.name)
        start = time.perf_counter()
        c = judge.classify(lookup[pid])
        ms = round((time.perf_counter() - start) * 1000)
        db.execute("INSERT OR REPLACE INTO classify (photo_id, judge, bucket, bucket_probs, flags, tokens, ms) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (pid, judge.name, c.bucket, dumps(c.bucket_probs), dumps(c.flags), c.tokens, ms))
        if c.tokens_out:
            db.execute("INSERT INTO spend (judge, tokens_in, tokens_out) VALUES (?, 0, ?)", (judge.name, c.tokens_out))

    parallel(cfg, ids, one, label="classify")
    if quiet:
        return
    for r in db.all("SELECT bucket, COUNT(*) n FROM classify WHERE judge = ? GROUP BY 1 ORDER BY 2 DESC",
                    (judge.name,)):
        print(f"  {r['bucket']}: {r['n']}")
