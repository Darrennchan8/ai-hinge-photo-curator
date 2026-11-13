"""Adaptive pairwise ranking within each bucket, fit with a soft-label Bradley-Terry model."""

import json
import random
import time

import numpy as np

from .config import Config
from .db import DB
from .judges import make
from .judges.base import prompt_version, ranking_versions
from .judging import compare_both_orders, photos


def fit_bt(n: int, obs: list[tuple[int, int, float]], prior: float = 0.1,
           return_bias: bool = False):
    """Fit scores s so that P(first beats second) = sigmoid(s_first - s_second + bias).

    obs holds ordered (first, second, p) with p the judged probability that the photo shown first
    is better; soft labels are fine. `bias` absorbs a judge's preference for whichever photo it saw
    first, so asking both orders recovers the true score gaps instead of compressed ones. A
    N(0, 1/prior) prior keeps unconnected or undefeated photos finite. Returns (scores, stderrs)
    with Laplace-approximation stderrs, plus the bias if requested.
    """
    theta = np.zeros(n + 1)  # scores, then position bias
    if not obs:
        out = np.zeros(n), np.full(n, prior ** -0.5)
        return (*out, 0.0) if return_bias else out
    i, j, p = (np.array(x) for x in zip(*obs))
    i, j = i.astype(int), j.astype(int)
    b = np.full(len(i), n)
    for _ in range(100):
        q = 1 / (1 + np.exp(-(theta[i] - theta[j] + theta[n])))
        r, w = p - q, q * (1 - q)
        grad = -prior * theta
        np.add.at(grad, i, r)
        np.add.at(grad, j, -r)
        np.add.at(grad, b, r)
        hess = prior * np.eye(n + 1)  # negative Hessian of the log-posterior
        for x, y, sign in ((i, i, 1), (j, j, 1), (b, b, 1), (i, j, -1), (j, i, -1),
                           (i, b, 1), (b, i, 1), (j, b, -1), (b, j, -1)):
            np.add.at(hess, (x, y), sign * w)
        step = np.linalg.solve(hess, grad)
        theta += step
        if np.max(np.abs(step)) < 1e-7:
            break
    stderr = np.sqrt(np.diag(np.linalg.inv(hess)))[:n]
    return (theta[:n], stderr, float(theta[n])) if return_bias else (theta[:n], stderr)


def select_pairs(scores: np.ndarray, stderr: np.ndarray, compared: set[frozenset], top_k: int,
                 rng: random.Random, offset: np.ndarray | None = None,
                 only: list[int] | None = None) -> list[tuple[int, int]]:
    """Pick one opponent for each photo still in contention for the top K, or for each of `only`.

    A photo is in contention if its optimistic score (s + 2se) reaches the K-th best score. Its
    opponent is the not-yet-compared photo that maximizes expected information: close in score
    (outcome uncertain) and with high combined uncertainty. Each photo plays at most once per round.
    """
    n = len(scores)
    final = scores + (offset if offset is not None else 0)  # what the top K is judged on
    kth = np.sort(final)[::-1][min(top_k, n) - 1]
    contenders = list(only) if only is not None else [i for i in range(n) if final[i] + 2 * stderr[i] >= kth]
    rng.shuffle(contenders)
    busy, pairs = set(), []
    for i in contenders:
        if i in busy:
            continue
        best, best_val = None, 0.0
        for j in range(n):
            if j == i or j in busy or frozenset((i, j)) in compared:
                continue
            q = 1 / (1 + np.exp(-(scores[i] - scores[j])))
            val = q * (1 - q) * np.hypot(stderr[i], stderr[j]) * (1 + 1e-6 * rng.random())
            if val > best_val:
                best, best_val = j, val
        if best is not None:
            pairs.append((i, best))
            busy.update((i, best))
    return pairs


def _observations(cfg: Config, db: DB, judge: str, index: dict[str, int]) -> tuple[list, set]:
    latest = {}
    versions = ranking_versions(cfg)
    marks = ",".join("?" * len(versions))
    rows = db.all(f"SELECT first, second, p_first, prompt_version FROM comparisons WHERE judge = ? "
                  f"AND prompt_version IN ({marks})", (judge, *versions))
    for r in sorted(rows, key=lambda r: versions.index(r["prompt_version"])):
        if r["first"] in index and r["second"] in index:
            latest[(index[r["first"]], index[r["second"]])] = r["p_first"]  # later versions replace earlier
    return [(a, b, p) for (a, b), p in latest.items()], {frozenset(pair) for pair in latest}


def age_offsets(cfg: Config, db: DB, ids: list[str]) -> np.ndarray:
    """Recency bias added to judged scores: -age_penalty_per_year per year since the photo was taken."""
    per_year = cfg["rank"].get("age_penalty_per_year", 0.0)
    when = {r["id"]: r["taken_at"] for r in db.all("SELECT id, taken_at FROM photos")}
    now = time.time()
    return np.array([-per_year * (now - when[i]) / (365.25 * 86400) if when.get(i) else 0.0 for i in ids])


def classified(cfg: Config, db: DB) -> dict[str, tuple[str, str | None]]:
    """Kept photo id -> (bucket, moment) for photos without a hard flag, per the configured classifier."""
    hard, thr = cfg.hard_flags, cfg["flags"]["hard_threshold"]
    out = {}
    for r in db.all("SELECT c.photo_id, c.bucket, c.flags, p.moment FROM classify c JOIN prefilter p "
                    "ON p.photo_id = c.photo_id WHERE p.status = 'keep' AND c.judge = ? ORDER BY c.photo_id",
                    (cfg["judge"]["classifier"],)):
        flags = json.loads(r["flags"])
        if not any(flags.get(f, 0) >= thr for f in hard):
            out[r["photo_id"]] = (r["bucket"], r["moment"])
    return out


def eligible(cfg: Config, db: DB) -> dict[str, list[str]]:
    """Bucket -> photos to rank: each moment's playoff winner in that bucket."""
    winners = {r["photo_id"] for r in db.all("SELECT photo_id FROM playoff WHERE judge = ? AND winner = 1",
                                             (cfg["judge"]["ranker"],))}
    cl = classified(cfg, db)
    if cl and not winners:
        raise SystemExit("Run `playoff` first: it keeps the best photo of each category in each moment.")
    buckets: dict[str, list[str]] = {}
    for pid, (bucket, _) in cl.items():
        if pid in winners:
            buckets.setdefault(bucket, []).append(pid)
    return buckets


def adaptive(cfg: Config, db: DB, judge, ids: list[str], top_k: int, per_photo: int, min_rounds: int,
             label: str, quiet: bool = False, offset: np.ndarray | None = None
             ) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """Rounds of informative pairs among `ids` until the top K (by score + offset) is stable or the
    budget runs out. Returns (judged scores, stderrs, comparisons per photo, why it stopped)."""
    index = {pid: k for k, pid in enumerate(ids)}
    lookup = photos(cfg, db, ids)
    budget = per_photo * len(ids) // 2  # pairs; each pair counts for two photos
    rng = random.Random(label)
    history: list[frozenset] = []
    rounds = 0
    while True:
        obs, compared = _observations(cfg, db, judge.name, index)
        scores, stderr = fit_bt(len(ids), obs)
        history.append(frozenset(np.argsort(-(scores + (offset if offset is not None else 0)))[:top_k].tolist()))
        settled = rounds >= min_rounds and len(history) >= 3 and history[-1] == history[-2] == history[-3]
        if settled or len(compared) >= budget:
            break
        pairs = select_pairs(scores, stderr, compared, top_k, rng, offset)[: budget - len(compared)]
        if not pairs:
            break
        rounds += 1
        if not quiet:
            print(f"  [{label}] round {rounds}: {len(pairs)} pairs ({len(compared)}/{budget} used)")
        compare_both_orders(cfg, db, judge, [(lookup[ids[a]], lookup[ids[b]]) for a, b in pairs])

    return scores, stderr, _counts(compared, len(ids)), ("top settled" if settled else "budget/pairs exhausted")


def _save(db: DB, judge, bucket: str, ids: list[str], scores, stderr, counts) -> None:
    db.executemany("INSERT OR REPLACE INTO ratings VALUES (?,?,?,?,?,?)",
                   [(pid, judge.name, bucket, float(scores[k]), float(stderr[k]), int(counts[k]))
                    for k, pid in enumerate(ids)])


def _counts(compared: set[frozenset], n: int) -> np.ndarray:
    counts = np.zeros(n, int)
    for pair in compared:
        for k in pair:
            counts[k] += 1
    return counts


def rank_bucket(cfg: Config, db: DB, judge, bucket: str, ids: list[str]) -> None:
    rc = cfg["rank"]
    scores, stderr, counts, why = adaptive(cfg, db, judge, ids, rc["top_k"], rc["comparisons_per_photo"],
                                           rc["min_rounds"], bucket, offset=age_offsets(cfg, db, ids))
    _save(db, judge, bucket, ids, scores, stderr, counts)
    print(f"  [{bucket}] done ({why}); {int(counts.sum()) // 2} pairs judged.")


def refine_bucket(cfg: Config, db: DB, judge, bucket: str, ids: list[str]) -> None:
    """Extra comparisons for the top contenders only, against opponents each hasn't met. `rank` stops
    as soon as its top K looks settled, which leaves close calls right where the finalists are cut."""
    extra = cfg["refine"]["comparisons_per_photo"]
    n = min(cfg["refine"]["contenders"], len(ids))
    budget = n * extra // 2  # pairs; each pair counts for two photos
    index = {pid: k for k, pid in enumerate(ids)}
    lookup = photos(cfg, db, ids)
    offset = age_offsets(cfg, db, ids)
    rng = random.Random(f"refine {bucket}")
    obs, compared = _observations(cfg, db, judge.name, index)
    start = _counts(compared, len(ids))
    used = 0
    while True:
        scores, stderr = fit_bt(len(ids), obs)
        added = _counts(compared, len(ids)) - start
        waiting = [int(k) for k in np.argsort(-(scores + offset))[:n] if added[k] < extra]
        pairs = select_pairs(scores, stderr, compared, n, rng, offset, only=waiting)[: budget - used]
        if not pairs:
            break
        compare_both_orders(cfg, db, judge, [(lookup[ids[a]], lookup[ids[b]]) for a, b in pairs])
        used += len(pairs)
        obs, compared = _observations(cfg, db, judge.name, index)
    _save(db, judge, bucket, ids, scores, stderr, _counts(compared, len(ids)))
    print(f"  [{bucket}] {used} more pairs for the top {n} of {len(ids)}.")


def refit(cfg: Config, db: DB) -> None:
    """Refit every category from the comparisons already saved, making no calls."""
    judge = make(cfg, cfg["judge"]["ranker"])
    for bucket, ids in sorted(eligible(cfg, db).items()):
        if len(ids) >= 2:
            obs, compared = _observations(cfg, db, judge.name, {pid: k for k, pid in enumerate(ids)})
            scores, stderr = fit_bt(len(ids), obs)
            _save(db, judge, bucket, ids, scores, stderr, _counts(compared, len(ids)))
            print(f"  [{bucket}] refit {len(ids)} photos from {len(compared)} pairs.")


def refine(cfg: Config, db: DB, judge_name: str | None = None, only: str | None = None, again: bool = False) -> None:
    """Refine each category once; `again` adds another round to categories already refined."""
    judge = make(cfg, judge_name or cfg["judge"]["ranker"])
    version = prompt_version(cfg)
    done = {r["bucket"] for r in db.all("SELECT bucket FROM refined WHERE judge = ? AND prompt_version = ?",
                                        (judge.name, version))}
    for bucket, ids in sorted(eligible(cfg, db).items()):
        if len(ids) < 2 or (only and bucket != only):
            continue
        if bucket in done and not again:
            print(f"  [{bucket}] already refined; use --again for another round.")
            continue
        refine_bucket(cfg, db, judge, bucket, ids)
        db.execute("INSERT OR IGNORE INTO refined VALUES (?,?,?)", (bucket, judge.name, version))


def run(cfg: Config, db: DB, judge_name: str | None = None, only: str | None = None) -> None:
    judge = make(cfg, judge_name or cfg["judge"]["ranker"])
    # Drop ratings for photos that have since changed bucket, lost a playoff or been flagged.
    if only:
        db.execute("DELETE FROM ratings WHERE judge = ? AND bucket = ?", (judge.name, only))
    else:
        db.execute("DELETE FROM ratings WHERE judge = ?", (judge.name,))
    for bucket, ids in sorted(eligible(cfg, db).items()):
        if only and bucket != only:
            continue
        if len(ids) < 2:
            if ids:
                db.execute("INSERT OR REPLACE INTO ratings VALUES (?,?,?,?,?,?)", (ids[0], judge.name, bucket, 0, 0, 0))
            continue
        print(f"Ranking {len(ids)} {bucket} photos with {judge.name}...")
        rank_bucket(cfg, db, judge, bucket, ids)
