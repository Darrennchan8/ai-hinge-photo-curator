"""A second opinion where it counts: the final judge plays every pair among each category's top
photos, and its order decides those spots."""

from itertools import combinations

from .config import Config
from .db import DB
from .judges import make
from .judges.base import prompt_version
from .judging import compare_both_orders, photos
from .rank import fit_bt
from .report import PROFILE_ORDER, ranked


def _play(cfg: Config, db: DB, judge, finalists: dict[str, list[str]]) -> None:
    pairs = [pair for ids in finalists.values() for pair in combinations(ids, 2)]
    print(f"Final: {sum(map(len, finalists.values()))} photos, {len(pairs)} pairs x 2 orders with {judge.name}...")
    lookup = photos(cfg, db, {pid for pair in pairs for pid in pair})
    failed = compare_both_orders(cfg, db, judge, [(lookup[a], lookup[b]) for a, b in pairs])
    if failed:
        print(f"  {failed} calls failed; re-run to retry them.")


def _scores(cfg: Config, db: DB, judge, ids: list[str]):
    index = {pid: k for k, pid in enumerate(ids)}
    marks = ",".join("?" * len(ids))
    rows = db.all(f"SELECT first, second, p_first FROM comparisons WHERE judge = ? AND prompt_version = ? "
                  f"AND first IN ({marks}) AND second IN ({marks})", (judge.name, prompt_version(cfg), *ids, *ids))
    return fit_bt(len(ids), [(index[r["first"]], index[r["second"]], r["p_first"]) for r in rows])


def run(cfg: Config, db: DB) -> None:
    if not cfg["final"]["judge"]:
        print("No final judge set ([final] judge); the ranker's order stands.")
        return
    judge = make(cfg, cfg["final"]["judge"])
    per = cfg["final"]["per_bucket"]
    by = ranked(cfg, db, use_finals=False)
    finalists = {b: [r["photo_id"] for r in by.get(b, [])[:per]] for b in dict.fromkeys(PROFILE_ORDER)}
    _play(cfg, db, judge, finalists)

    db.execute("DELETE FROM finals")
    for bucket, ids in finalists.items():
        scores, stderr = _scores(cfg, db, judge, ids)
        db.executemany("INSERT INTO finals VALUES (?,?,?,?,?)",
                       [(pid, judge.name, bucket, float(scores[k]), float(stderr[k])) for k, pid in enumerate(ids)])
        order = sorted(range(len(ids)), key=lambda k: -scores[k])
        print(f"  {bucket}: ranker's #{order[0] + 1} wins; order " + ", ".join(f"#{k + 1}" for k in order))


def opinion(cfg: Config, db: DB) -> None:
    """The same finalists through other judges, kept apart so they can't change the ranking."""
    finalists: dict[str, list[str]] = {}
    for r in db.all("SELECT photo_id, bucket FROM finals ORDER BY bucket, score DESC"):
        finalists.setdefault(r["bucket"], []).append(r["photo_id"])
    if not finalists or not cfg["final"]["opinions"]:
        print("Opinions need a final judge and [final] opinions; skipping.")
        return
    for name in cfg["final"]["opinions"]:
        judge = make(cfg, name)
        _play(cfg, db, judge, finalists)
        db.execute("DELETE FROM opinions WHERE judge = ?", (judge.name,))
        for bucket, ids in finalists.items():
            scores, stderr = _scores(cfg, db, judge, ids)
            db.executemany("INSERT INTO opinions VALUES (?,?,?,?,?)",
                           [(pid, judge.name, bucket, float(scores[k]), float(stderr[k])) for k, pid in enumerate(ids)])
            order = sorted(range(len(ids)), key=lambda k: -scores[k])
            print(f"  {bucket}: final judge's #{order[0] + 1} wins; order " + ", ".join(f"#{k + 1}" for k in order))
