"""Best photo of each category within each moment, chosen by the judge.

Photos taken seconds apart are variations of one shot, and a profile only wants one of them. But a
moment can hold different kinds of shot (a close-up selfie and a posed full-body), so the playoff
runs per (moment, category) after classification, and only its winners go on to ranking.
"""

from itertools import combinations

import numpy as np

from .config import Config
from .db import DB
from .judges import make
from .judges.base import prompt_version
from .judging import compare_both_orders, photos
from .rank import adaptive, classified, fit_bt


def groups(cfg: Config, db: DB) -> list[tuple[str, str, list[str]]]:
    """(moment, bucket, photo ids) for every classified, unflagged photo."""
    by: dict[tuple, list[str]] = {}
    for pid, (bucket, moment) in classified(cfg, db).items():
        by.setdefault((moment or pid, bucket), []).append(pid)
    return [(m, b, sorted(ids)) for (m, b), ids in sorted(by.items())]


def _round_robin_scores(cfg: Config, db: DB, judge: str, ids: list[str]) -> np.ndarray | None:
    index = {pid: k for k, pid in enumerate(ids)}
    marks = ",".join("?" * len(ids))
    rows = db.all(f"SELECT first, second, p_first FROM comparisons WHERE judge = ? AND prompt_version = ? "
                  f"AND first IN ({marks}) AND second IN ({marks})", (judge, prompt_version(cfg), *ids, *ids))
    if len(rows) < len(ids) * (len(ids) - 1):
        return None
    scores, _ = fit_bt(len(ids), [(index[r["first"]], index[r["second"]], r["p_first"]) for r in rows])
    return scores


def run(cfg: Config, db: DB, only: set[str] | None = None) -> None:
    """Run the playoff, for just the `only` categories if given (the others keep their winners)."""
    judge = make(cfg, cfg["judge"]["ranker"])
    pc = cfg["playoff"]
    all_groups = [g for g in groups(cfg, db) if only is None or g[1] in only]
    if not all_groups:
        raise SystemExit("Nothing classified yet; run `classify` first.")
    small = [g for g in all_groups if 1 < len(g[2]) <= pc["round_robin_max"]]
    large = [g for g in all_groups if len(g[2]) > pc["round_robin_max"]]
    pairs = [pair for _, _, ids in small for pair in combinations(ids, 2)]
    print(f"Playoff: {len(all_groups)} (moment, category) groups; {sum(len(g[2]) == 1 for g in all_groups)} "
          f"single photos win outright, {len(small)} small groups play {len(pairs)} pairs, "
          f"{len(large)} large groups use adaptive matching.")
    lookup = photos(cfg, db, {pid for pair in pairs for pid in pair})
    compare_both_orders(cfg, db, judge, [(lookup[a], lookup[b]) for a, b in pairs])

    results = []
    for moment, bucket, ids in all_groups:
        if len(ids) == 1:
            scores = np.zeros(1)
        elif len(ids) <= pc["round_robin_max"]:
            scores = _round_robin_scores(cfg, db, judge.name, ids)
            if scores is None:
                print(f"  [{bucket} @ {moment}] incomplete (failed calls); re-run playoff to finish.")
                continue
        else:
            scores, *_ = adaptive(cfg, db, judge, ids, 1, pc["comparisons_per_photo"], 2,
                                  f"{bucket} @ {moment}", quiet=True)
        best = int(np.argmax(scores))
        results += [(pid, judge.name, moment, bucket, int(k == best), float(scores[k])) for k, pid in enumerate(ids)]

    if only is None:
        db.execute("DELETE FROM playoff WHERE judge = ?", (judge.name,))
    else:
        db.executemany("DELETE FROM playoff WHERE judge = ? AND bucket = ?", [(judge.name, b) for b in only])
    db.executemany("INSERT INTO playoff VALUES (?,?,?,?,?,?)", results)
    winners = sum(r[4] for r in results)
    print(f"Playoff done: {winners} winners go on to ranking, {len(results) - winners} runners-up set aside.")
