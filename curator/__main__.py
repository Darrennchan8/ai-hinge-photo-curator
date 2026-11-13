"""Pick Hinge photos from a Google Photos export with pairwise AI judgments."""

import argparse

from . import config
from .db import DB


def main() -> None:
    ap = argparse.ArgumentParser(prog="curator", description=__doc__)
    ap.add_argument("--config", help="overrides file to use instead of config_overrides.toml")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")
    sub.add_parser("doctor", help="check the setup: packages, keys, config and input, without paid calls")
    p = sub.add_parser("run", help="every step from ingest to report; finished work is skipped")
    p.add_argument("paths", nargs="*", help="extra archives, folders or rclone remotes to ingest (as for ingest)")
    p = sub.add_parser("ingest", help="unpack Takeout archives/photos in input/ into working copies")
    p.add_argument("paths", nargs="*",
                   help="extra archives/folders to read in place: a local path, an rclone remote such as "
                        "'gdrive:Takeout', or - for a .tgz on stdin")
    p.add_argument("--sample", type=int, help="only ingest this many new photos (for a trial run)")
    sub.add_parser("prefilter", help="find your face, drop blurry/tiny/duplicate photos (local, free)")
    p = sub.add_parser("classify", help="assign categories and flags")
    p.add_argument("--judge")
    p.add_argument("--limit", type=int)
    sub.add_parser("playoff", help="keep the best shot of each category from each burst of photos")
    p = sub.add_parser("rank", help="pairwise-rank each category")
    p.add_argument("--judge")
    p.add_argument("--bucket")
    sub.add_parser("fullbody", help="move photos without a visible full body out of the full-body category")
    sub.add_parser("headshot", help="move photos that aren't close-ups out of the headshot category")
    p = sub.add_parser("refine", help="extra comparisons for each category's top contenders")
    p.add_argument("--judge")
    p.add_argument("--bucket")
    p.add_argument("--again", action="store_true", help="refine categories that were already refined")
    sub.add_parser("refit", help="refit each category's ranking from saved comparisons, with no calls")
    sub.add_parser("final", help="optional: the final judge re-orders each category's top photos")
    sub.add_parser("opinion", help="optional: other judges play the same finalists, for the gallery's toggles")
    p = sub.add_parser("looks", help="tag each ranked photo's expression, gaze and pose, for the gallery's variety toggle")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("explain", help="optional: rationale + crop for the shortlist")
    p.add_argument("--redo", action="store_true", help="rewrite existing rationales too")
    sub.add_parser("report", help="write out/index.html")
    sub.add_parser("publish", help="copy the gallery to a web server's folder ([publish])")
    p = sub.add_parser("pilot", help="compare two judges on the same random pairs")
    p.add_argument("--pairs", type=int, default=150)
    p.add_argument("--photos", type=int, default=40, help="photos to classify with each judge")
    p.add_argument("--judges", default="jev,claude")
    sub.add_parser("status", help="show progress and spend so far")
    args = ap.parse_args()

    cfg = config.load(args.config) if args.config else config.load()
    if args.cmd == "doctor":
        from . import doctor
        raise SystemExit(doctor.run(cfg))
    db = DB(cfg.path("db"))
    if args.cmd == "run":
        run_all(cfg, db, args.paths)
    elif args.cmd == "ingest":
        from . import ingest
        ingest.run(cfg, db, args.sample, args.paths)
    elif args.cmd == "prefilter":
        from . import prefilter
        prefilter.run(cfg, db)
    elif args.cmd == "playoff":
        from . import playoff
        playoff.run(cfg, db)
    elif args.cmd == "pilot":
        from . import pilot
        pilot.run(cfg, db, args.pairs, args.judges.split(","), args.photos)
    elif args.cmd == "classify":
        from . import classify
        classify.run(cfg, db, args.judge, args.limit)
    elif args.cmd == "rank":
        from . import rank
        rank.run(cfg, db, args.judge, args.bucket)
    elif args.cmd == "refit":
        from . import rank
        rank.refit(cfg, db)
    elif args.cmd == "refine":
        from . import rank
        rank.refine(cfg, db, args.judge, args.bucket, args.again)
    elif args.cmd in ("fullbody", "headshot"):
        from . import recheck
        recheck.run(cfg, db, "full_body" if args.cmd == "fullbody" else "headshot")
    elif args.cmd == "final":
        from . import final
        final.run(cfg, db)
    elif args.cmd == "opinion":
        from . import final
        final.opinion(cfg, db)
    elif args.cmd == "looks":
        from . import looks
        looks.run(cfg, db, args.limit)
    elif args.cmd == "explain":
        from . import explain
        explain.run(cfg, db, args.redo)
    elif args.cmd == "report":
        from . import report
        print(report.build(cfg, db))
    elif args.cmd == "publish":
        from . import publish
        publish.run(cfg)
    elif args.cmd == "status":
        status(cfg, db)

    if args.cmd in ("run", "prefilter", "pilot", "report", "explain") and cfg["publish"]["auto"]:
        from . import publish
        publish.run(cfg, quiet=True)


def run_all(cfg, db, paths: list[str]) -> None:
    """The whole pipeline in order. Every step skips what it has already done, so after a failure or a
    budget stop, running this again picks up where it left off."""
    from . import classify, explain, final, ingest, looks, playoff, prefilter, rank, recheck, report

    steps = [
        ("ingest", lambda: ingest.run(cfg, db, None, paths)),
        ("prefilter", lambda: prefilter.run(cfg, db)),
        ("classify", lambda: classify.run(cfg, db)),
        ("playoff", lambda: playoff.run(cfg, db)),
        ("rank", lambda: rank.run(cfg, db)),
        ("fullbody", lambda: recheck.run(cfg, db, "full_body")),
        ("headshot", lambda: recheck.run(cfg, db, "headshot")),
        ("refine", lambda: rank.refine(cfg, db)),
        ("final", lambda: final.run(cfg, db)),
        ("opinion", lambda: final.opinion(cfg, db)),
        ("looks", lambda: looks.run(cfg, db)),
        ("explain", lambda: explain.run(cfg, db)),
        ("report", lambda: print(report.build(cfg, db))),
    ]
    for n, (name, step) in enumerate(steps, 1):
        print(f"\n== {n}/{len(steps)} {name}")
        step()


def status(cfg, db) -> None:
    def count(sql):
        return db.one(sql)[0]

    photos = count("SELECT COUNT(*) FROM photos")
    analyzed = count("SELECT COUNT(*) FROM faces")
    kept = count("SELECT COUNT(*) FROM prefilter WHERE status = 'keep'")
    print(f"photos {photos}, analyzed {analyzed}, kept {kept}")
    for r in db.all("SELECT judge, COUNT(*) n, SUM(tokens) t FROM classify GROUP BY judge"):
        print(f"classify[{r['judge']}]: {r['n']} photos, {r['t'] or 0:,} input tokens")
    from .judges.base import prompt_version
    current = prompt_version(cfg)
    for r in db.all("SELECT judge, prompt_version v, COUNT(*) n, SUM(tokens_in) ti, SUM(tokens_out) tout "
                    "FROM comparisons GROUP BY judge, prompt_version ORDER BY judge, MIN(created)"):
        mark = " (current)" if r["v"] == current else ""
        print(f"compare[{r['judge']}, prompt {r['v']}{mark}]: {r['n']} calls, "
              f"{r['ti'] or 0:,} in / {r['tout'] or 0:,} out tokens")
    from .judging import spent
    for judge in ("jev", "claude", "gemini", "openai"):
        if used := spent(cfg, db, judge):
            print(f"{judge} spend: ~${used:.2f} of ${cfg[judge]['budget_usd']:.2f} budget")


if __name__ == "__main__":
    main()
