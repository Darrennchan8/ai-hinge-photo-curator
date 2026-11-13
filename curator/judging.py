"""Shared plumbing between pipeline steps and judges: photo hints, caching, parallel calls."""

import json
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Config
from .db import DB
from .judges.base import Judge, Photo, Verdict, prompt_version


def photos(cfg: Config, db: DB, ids: Iterable[str]) -> dict[str, Photo]:
    ids = list(ids)
    rows = {r["id"]: r for r in db.all(
        f"SELECT p.id, p.width, p.height, f.box, f.n_faces, f.matched, f.face_frac FROM photos p "
        f"LEFT JOIN faces f ON f.photo_id = p.id "
        f"WHERE p.id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    work = cfg.path("work")
    out = {}
    for pid in ids:
        r = rows[pid]
        crop = zoom_crop(cfg, r)
        out[pid] = Photo(pid, work / f"{pid}.jpg", _hint(r, crop),
                         needs_ref=not r["matched"] or (r["n_faces"] or 0) > 1, crop=crop)
    return out


def zoom_crop(cfg: Config, row) -> tuple[int, int, int, int] | None:
    """A portrait crop around the owner when their face is small, like zooming in when uploading to
    Hinge: the face becomes `zoom_to` of the crop height, a quarter of the way down."""
    j = cfg["judge"]
    if not (row["matched"] and row["box"] and row["face_frac"]) or row["face_frac"] >= j.get("zoom_below", 0):
        return None
    x, y, w, h = json.loads(row["box"])
    W, H = row["width"], row["height"]
    ch = min(H, h / j["zoom_to"])
    cw = ch * j["zoom_aspect"]
    if cw > W:
        cw, ch = W, W / j["zoom_aspect"]
    cx = min(max(x + w / 2 - cw / 2, 0), W - cw)
    cy = min(max(y + h / 2 - ch / 4, 0), H - ch)  # face a quarter down so a standing body fits
    return round(cx), round(cy), round(cw), round(ch)


def _hint(row, crop=None) -> str:
    box = json.loads(row["box"]) if row["box"] and row["matched"] else None
    if not box:
        return "the profile owner appears somewhere in the photo; match them to the reference photo"
    x, y, w, h = box
    ox, oy, W, H = crop or (0, 0, row["width"], row["height"])
    cx, cy = (x + w / 2 - ox) / W, (y + h / 2 - oy) / H
    hint = f"the profile owner's face is about {cx:.0%} from the left edge and {cy:.0%} from the top"
    if row["n_faces"] and row["n_faces"] > 1:
        hint += f" ({row['n_faces']} faces are visible; the profile owner is the one at that position)"
    return hint


class BudgetExceeded(Exception):
    pass


def spent(cfg: Config, db: DB, judge: str) -> float:
    """Dollars recorded so far for a paid judge, from token counts and config prices."""
    if judge not in ("jev", "claude", "gemini", "openai"):
        return 0.0
    tin, tout = db.one(
        "SELECT (SELECT COALESCE(SUM(tokens_in), 0) FROM comparisons WHERE judge = :j)"
        " + (SELECT COALESCE(SUM(tokens), 0) FROM classify WHERE judge = :j)"
        " + (SELECT COALESCE(SUM(tokens_in), 0) FROM spend WHERE judge = :j),"
        " (SELECT COALESCE(SUM(tokens_out), 0) FROM comparisons WHERE judge = :j)"
        " + (SELECT COALESCE(SUM(tokens_out), 0) FROM spend WHERE judge = :j)", {"j": judge})
    price = cfg[judge]
    return (tin * price["usd_per_mtok_input"] + tout * price.get("usd_per_mtok_output", 0)) / 1e6


def check_budget(cfg: Config, db: DB, judge: str) -> None:
    budget = cfg.raw.get(judge, {}).get("budget_usd")
    if budget is not None and (used := spent(cfg, db, judge)) >= budget:
        raise BudgetExceeded(f"{judge} has used ${used:.2f} of its ${budget:.2f} budget ([{judge}] budget_usd).")


def cached_compare(cfg: Config, db: DB, judge: Judge, a: Photo, b: Photo) -> float:
    """P(a beats b) as judge sees it with a shown first; cached forever per prompt version."""
    version = prompt_version(cfg)
    row = db.one("SELECT p_first FROM comparisons WHERE judge=? AND prompt_version=? AND first=? AND second=?",
                 (judge.name, version, a.id, b.id))
    if row:
        return row["p_first"]
    check_budget(cfg, db, judge.name)
    start = time.perf_counter()
    v: Verdict = judge.compare(a, b)
    ms = round((time.perf_counter() - start) * 1000)
    db.execute("INSERT OR REPLACE INTO comparisons (judge, prompt_version, first, second, p_first, confidence, "
               "tokens_in, tokens_out, note, ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (judge.name, version, a.id, b.id, v.p_first, v.confidence, v.tokens_in, v.tokens_out,
                v.note, ms))
    return v.p_first


def compare_both_orders(cfg: Config, db: DB, judge: Judge, pairs: list[tuple[Photo, Photo]]) -> int:
    """Ask every pair as (a, b) and (b, a). Returns the number of failed calls."""
    jobs = [(a, b) for a, b in pairs] + [(b, a) for a, b in pairs]
    return parallel(cfg, jobs, lambda job: cached_compare(cfg, db, judge, *job), label="comparisons")


def parallel(cfg: Config, items: list, fn: Callable, label: str) -> int:
    failed = 0
    with ThreadPoolExecutor(cfg["judge"]["concurrency"]) as pool:
        futs = {pool.submit(fn, it): it for it in items}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                fut.result()
            except BudgetExceeded as e:
                pool.shutdown(cancel_futures=True)
                raise SystemExit(f"Stopped: {e} Finished results are saved.") from e
            except Exception as e:
                failed += 1
                print(f"  {label} call failed: {e}")
                if failed >= 10 and failed > n // 4:
                    pool.shutdown(cancel_futures=True)
                    raise SystemExit(f"Too many failed {label} calls; stopping.") from e
            if n % 50 == 0:
                print(f"  {n}/{len(items)} {label}")
    return failed
