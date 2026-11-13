"""Compare judges on the same random pairs before trusting one with the full ranking."""

import json
import random

import numpy as np

from .config import Config
from .db import DB
from . import classify
from .judges import make
from .judges.base import prompt_version
from .judging import compare_both_orders, photos
from .rank import fit_bt


def _sample_pairs(ids: list[str], n: int, seed: int) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    pairs, seen = [], set()
    for _ in range(n * 20):
        if len(pairs) >= n or len(ids) < 2:
            break
        a, b = rng.sample(ids, 2)
        if frozenset((a, b)) not in seen:
            seen.add(frozenset((a, b)))
            pairs.append((a, b))
    return pairs


def _results(cfg: Config, db: DB, judge: str, pairs: list[tuple[str, str]]) -> dict | None:
    rows = {(r["first"], r["second"]): r for r in db.all(
        "SELECT first, second, p_first, tokens_in, tokens_out, note, ms FROM comparisons "
        "WHERE judge = ? AND prompt_version = ?", (judge, prompt_version(cfg)))}
    got = [(a, b) for a, b in pairs if (a, b) in rows and (b, a) in rows]
    if not got:
        return None
    p1 = np.array([rows[(a, b)]["p_first"] for a, b in got])
    p2 = np.array([rows[(b, a)]["p_first"] for a, b in got])
    index = {pid: k for k, pid in enumerate({x for pair in got for x in pair})}
    obs = [(index[a], index[b], rows[(a, b)]["p_first"]) for a, b in got] + \
          [(index[b], index[a], rows[(b, a)]["p_first"]) for a, b in got]
    _, _, bias = fit_bt(len(index), obs, return_bias=True)
    calls = [rows[k] for pair in got for k in (pair, pair[::-1])]
    return {
        "pairs": got,
        "p_a": dict(zip(got, (p1 + 1 - p2) / 2)),  # order-averaged P(a better)
        "order_consistent": float(np.mean((p1 > 0.5) == (p2 < 0.5))),
        "order_gap": float(np.mean(np.abs(p1 - (1 - p2)))),
        "first_bias_logit": bias,
        "decisive": float(np.mean(np.abs((p1 + 1 - p2) / 2 - 0.5) > 0.2)),
        "tokens_in": float(np.mean([c["tokens_in"] or 0 for c in calls])),
        "tokens_out": float(np.mean([c["tokens_out"] or 0 for c in calls])),
        "ms": float(np.median([c["ms"] for c in calls if c["ms"]] or [0])),
        "notes": {(a, b): rows[(a, b)]["note"] for a, b in got},
    }


def _calls(cfg: Config, db: DB) -> int:
    """Projected judge calls for the full run: classify every kept photo, playoff within each moment
    (an upper bound: as if a moment were all one category), then rank about one winner per moment.
    Pair counts double because every pair is asked in both orders."""
    import math
    sizes = [r["n"] for r in db.all("SELECT COUNT(*) n FROM prefilter WHERE status = 'keep' GROUP BY moment")]
    rr, pp = cfg["playoff"]["round_robin_max"], cfg["playoff"]["comparisons_per_photo"]
    playoff_pairs = sum(math.comb(n, 2) if n <= rr else n * pp // 2 for n in sizes)
    rank_pairs = len(sizes) * cfg["rank"]["comparisons_per_photo"] // 2
    return sum(sizes) + 2 * (playoff_pairs + rank_pairs)


def _hours(cfg: Config, r: dict, calls: int) -> float:
    return calls * r["ms"] / 1000 / cfg["judge"]["concurrency"] / 3600


def _cost(cfg: Config, judge: str, r: dict, calls: int) -> float:
    tokens_in = calls * r["tokens_in"]
    if judge == "jev":
        return tokens_in * cfg["jev"]["usd_per_mtok_input"] / 1e6
    if judge == "claude":
        return (tokens_in * cfg["claude"]["usd_per_mtok_input"]
                + calls * r["tokens_out"] * cfg["claude"]["usd_per_mtok_output"]) / 1e6
    return 0.0


def _classifications(cfg: Config, db: DB, judges: list[str], ids: list[str]) -> dict | None:
    """Bucket and flag agreement between the first two judges on the same photos."""
    if len(judges) < 2:
        return None
    n1, n2 = judges[:2]
    rows = {}
    for name in (n1, n2):
        marks = ",".join("?" * len(ids))
        rows[name] = {r["photo_id"]: r for r in db.all(
            f"SELECT photo_id, bucket, flags, ms FROM classify WHERE judge = ? AND photo_id IN ({marks})",
            (name, *ids))}
    common = [pid for pid in ids if pid in rows[n1] and pid in rows[n2]]
    if not common:
        return None
    flags1 = {pid: json.loads(rows[n1][pid]["flags"]) for pid in common}
    flags2 = {pid: json.loads(rows[n2][pid]["flags"]) for pid in common}
    per_flag = {}
    for f in cfg.flags:
        on1 = [flags1[p].get(f, 0) >= 0.5 for p in common]
        on2 = [flags2[p].get(f, 0) >= 0.5 for p in common]
        per_flag[f] = (sum(on1), sum(on2), float(np.mean([a == b for a, b in zip(on1, on2)])))
    disagree = []
    for pid in common:
        b1, b2 = rows[n1][pid]["bucket"], rows[n2][pid]["bucket"]
        f1 = {f for f, v in flags1[pid].items() if v >= 0.5}
        f2 = {f for f, v in flags2[pid].items() if v >= 0.5}
        if b1 != b2 or f1 != f2:
            disagree.append((pid, b1, sorted(f1), b2, sorted(f2)))
    return {"judges": (n1, n2), "n": len(common),
            "bucket_agree": float(np.mean([rows[n1][p]["bucket"] == rows[n2][p]["bucket"] for p in common])),
            "flags": per_flag, "disagree": disagree}


def run(cfg: Config, db: DB, n_pairs: int, judges: list[str], n_photos: int = 40, seed: int = 0) -> None:
    ids = [r["photo_id"] for r in db.all("SELECT photo_id FROM prefilter WHERE status = 'keep' ORDER BY photo_id")]
    pairs = _sample_pairs(ids, n_pairs, seed)
    sample = random.Random(seed).sample(ids, min(n_photos, len(ids)))
    lookup = photos(cfg, db, {x for p in pairs for x in p})
    results, ran = {}, []
    for name in judges:
        try:
            judge = make(cfg, name)
        except SystemExit as e:
            print(f"Skipping {name}: {e}")
            continue
        ran.append(name)
        print(f"Pilot: {len(pairs)} pairs x 2 orders and {len(sample)} classifications with {name}...")
        compare_both_orders(cfg, db, judge, [(lookup[a], lookup[b]) for a, b in pairs])
        classify.run(cfg, db, name, ids=sample, quiet=True)
        results[name] = _results(cfg, db, name, pairs)

    results = {k: v for k, v in results.items() if v}
    print()
    for name, r in results.items():
        print(f"{name}: order-consistent {r['order_consistent']:.0%}, mean order gap {r['order_gap']:.2f}, "
              f"first-position bias {r['first_bias_logit']:+.2f} logits, decisive {r['decisive']:.0%}, "
              f"~{r['tokens_in']:.0f} input tokens/call, median {r['ms']:.0f} ms/call")
        calls = _calls(cfg, db)
        print(f"  full run (~{calls:,} calls for {len(ids)} photos): "
              f"~${_cost(cfg, name, r, calls):.2f}, ~{_hours(cfg, r, calls):.1f} h at concurrency "
              f"{cfg['judge']['concurrency']}")

    agreement = None
    if len(results) >= 2:
        (n1, r1), (n2, r2) = list(results.items())[:2]
        common = [p for p in r1["pairs"] if p in r2["p_a"]]
        bands = [(0.0, 0.1), (0.1, 0.3), (0.3, 0.51)]
        agreement = {"judges": (n1, n2), "overall": None, "bands": []}
        if common:
            same = [(r1["p_a"][p] > 0.5) == (r2["p_a"][p] > 0.5) for p in common]
            agreement["overall"] = float(np.mean(same))
            print(f"\n{n1} vs {n2} agree on {agreement['overall']:.0%} of {len(common)} pairs")
            for lo, hi in bands:
                sel = [s for p, s in zip(common, same) if lo <= abs(r1["p_a"][p] - 0.5) < hi]
                if sel:
                    agreement["bands"].append((lo, hi, len(sel), float(np.mean(sel))))
                    print(f"  when {n1}'s margin is {lo:.1f}-{min(hi, 0.5):.1f}: {np.mean(sel):.0%} of {len(sel)}")

    labels = _classifications(cfg, db, ran, sample)
    if labels:
        n1, n2 = labels["judges"]
        print(f"\nClassification on {labels['n']} photos: same category {labels['bucket_agree']:.0%}")
        for f, (c1, c2, agree) in labels["flags"].items():
            print(f"  {f}: {n1} flagged {c1}, {n2} flagged {c2}, agree {agree:.0%}")

    from . import report
    print(f"\nPilot page: {report.pilot_page(cfg, db, results, agreement, labels)}")
