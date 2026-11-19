"""Static HTML pages: prefilter contact sheet, pilot results, and the final ranked gallery."""

import html
import json
import os
import time
from pathlib import Path

from .config import Config
from .db import DB
from .images import judge_name

CSS = """
:root { --bg:#fafaf9; --fg:#1c1917; --muted:#78716c; --card:#fff; --line:#e7e5e4; --accent:#be185d;
        --warn:#b45309; --bad:#b91c1c; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#1c1917; --fg:#f5f5f4; --muted:#a8a29e; --card:#292524; --line:#44403c; --accent:#f472b6;
  --warn:#fbbf24; --bad:#f87171; } }
* { box-sizing: border-box; }
[hidden] { display:none !important; }
body { margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui, -apple-system, sans-serif; }
main { max-width:1200px; margin:0 auto; }
h1 { font-size:28px; margin:0 0 4px; } h2 { margin:40px 0 4px; font-size:20px; }
.sub { color:var(--muted); margin:0 0 16px; }
a { color:var(--accent); }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(180px, 1fr)); gap:12px; }
.grid.big { grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); }
.scores { margin:4px 0; }
.scores > div { margin-top:2px; }
.score { display:inline-block; padding:0 5px; border-radius:5px; color:#1c1917; white-space:nowrap;
         font-variant-numeric:tabular-nums; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
.card img { display:block; width:100%; aspect-ratio:4/5; object-fit:contain; background:var(--line); }
.meta { padding:8px 10px; font-size:13px; }
.meta .rank { font-weight:700; } .muted { color:var(--muted); }
.badge { display:inline-block; font-size:11px; padding:1px 6px; border-radius:99px; margin:2px 2px 0 0;
         border:1px solid var(--warn); color:var(--warn); }
.badge.bad { border-color:var(--bad); color:var(--bad); }
.pair { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:12px 0; }
.pair .card.win { outline:3px solid var(--accent); }
table { border-collapse:collapse; margin:8px 0; } td, th { padding:4px 12px 4px 0; text-align:left; }
th { color:var(--muted); font-weight:500; }
details summary { cursor:pointer; color:var(--accent); margin:12px 0; }
#controls { position:sticky; top:0; z-index:5; display:flex; flex-wrap:wrap; gap:12px 24px; align-items:center;
            margin:16px 0; padding:12px 16px; background:var(--card); border:1px solid var(--line); border-radius:10px; }
#controls fieldset { border:0; margin:0; padding:0; display:flex; flex-wrap:wrap; gap:4px 12px; align-items:center; }
#controls legend { float:left; margin-right:12px; padding:0; color:var(--muted); }
#controls label { min-height:32px; display:inline-flex; gap:6px; align-items:center; }
#controls .slider { flex:1 1 240px; display:flex; flex-direction:column; }
#controls input[type=range] { width:100%; accent-color:var(--accent); }
#controls input[type=radio], #controls input[type=checkbox] { accent-color:var(--accent); }
#controls button { min-height:36px; padding:0 14px; font:inherit; color:var(--fg); background:transparent;
                   border:1px solid var(--line); border-radius:8px; cursor:pointer; }
"""


def _rel(cfg: Config, name: str) -> str:
    return os.path.relpath(cfg.path(name), cfg.path("out")).replace(os.sep, "/")


def _page(cfg: Config, filename: str, title: str, body: str) -> Path:
    out = cfg.path("out")
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename
    path.write_text(f"<!doctype html><html lang=en><head><meta charset=utf-8>"
                    f"<meta name=viewport content='width=device-width,initial-scale=1'>"
                    f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
                    f"<body><main>{body}</main></body></html>")
    return path


class _Gallery:
    """Per-page lookups: the exact judge image for each photo and its Google Photos link."""

    def __init__(self, cfg: Config, db: DB):
        from .images import ensure_judge_images
        from .judging import photos
        ensure_judge_images(cfg, db)
        self.urls = {r["id"]: r["url"] for r in db.all("SELECT id, url FROM photos WHERE url IS NOT NULL")}
        self.photos = photos(cfg, db, [r["id"] for r in db.all("SELECT id FROM photos")])
        self.base = f"{_rel(cfg, 'judge')}/{cfg['judge']['image_side']}"

    def img(self, pid: str) -> str:
        p = self.photos[pid]
        return f"{self.base}/{judge_name(p.path, p.crop)}"


def _links(cfg: Config, db: DB) -> _Gallery:
    return _Gallery(cfg, db)


def _card(cfg: Config, links: _Gallery, pid: str, meta: str = "", cls: str = "") -> str:
    """The exact image the judges see; tapping opens the photo in Google Photos."""
    judged = links.img(pid)
    if links.photos[pid].crop:
        meta = "<span class=badge>zoomed, as you'd crop in Hinge</span> " + meta
    href = html.escape(links.urls.get(pid) or judged, quote=True)
    return (f"<div class='card {cls}'><a href='{href}' target=_blank rel=noopener>"
            f"<img loading=lazy src='{judged}' alt=''></a><div class=meta>{meta}</div></div>")


# ---------------------------------------------------------------- prefilter

REASONS = {
    "kept": "Kept",
    "too_old": "Taken more than the [prefilter] max_age_years ago",
    "blurry": "Your face is blurry",
    "face_too_small": "Your face is too small in the frame",
    "unlocated": "Kept, but your face wasn't located (the export is trusted to be of you; judges get a reference photo)",
    "not_you": "Your face wasn't found ([prefilter] keep_unlocated is off)",
}


def prefilter_sheet(cfg: Config, db: DB) -> Path:
    links = _links(cfg, db)
    rows = db.all("SELECT p.photo_id, COALESCE(p.reason, 'kept') reason, p.dup_of, f.similarity, f.face_frac, "
                  "f.sharpness, f.n_faces FROM prefilter p JOIN faces f ON f.photo_id = p.photo_id "
                  "ORDER BY f.similarity DESC")
    groups: dict[str, list] = {k: [] for k in REASONS}
    for r in rows:
        groups.setdefault(r["reason"], []).append(r)
    body = ["<h1>Prefilter contact sheet</h1><p class=sub>Check the cuts look right. Thresholds are under "
            "[prefilter] in the config; re-running <code>prefilter</code> re-applies them without "
            "re-analyzing. Every image is exactly what the judges are sent; tap one to open the original "
            "in Google Photos when the export says where it is.</p>"]
    from .judges.base import reference_path
    if ref := reference_path(cfg):
        src = f"{_rel(cfg, 'judge')}/{cfg['judge']['image_side']}/{ref.stem}.jpg"
        body.append(f"<h2>Reference face</h2><p class=sub>Shown to the judges as \"the profile owner\" "
                    f"for group shots and photos where your face wasn't located.</p>"
                    f"<div class=grid><div class=card><img src='{src}' alt=''></div></div>")
    for reason, items in groups.items():
        if not items:
            continue
        cards = []
        for r in items:
            sim = f"{r['similarity']:.2f}" if r["similarity"] is not None else "–"
            frac = f"{r['face_frac']:.0%}" if r["face_frac"] is not None else "–"
            sharp = f"{r['sharpness']:.0f}" if r["sharpness"] is not None else "–"
            meta = f"<span class=muted>match {sim} · face {frac} · sharp {sharp}</span>"
            cards.append(_card(cfg, links, r["photo_id"], meta))
        label = REASONS.get(reason, reason)
        open_attr = " open" if reason != "kept" else ""
        body.append(f"<details{open_attr}><summary><b>{html.escape(label)}</b> ({len(items)})</summary>"
                    f"<div class=grid>{''.join(cards)}</div></details>")

    moments: dict[str, list] = {}
    for r in db.all("SELECT p.photo_id, p.moment, ph.taken_at FROM prefilter p JOIN photos ph ON ph.id = p.photo_id "
                    "WHERE p.status = 'keep' AND p.moment IS NOT NULL ORDER BY ph.taken_at"):
        moments.setdefault(r["moment"], []).append(r)
    multi = sorted((m for m in moments.values() if len(m) > 1), key=len, reverse=True)
    body.append(f"<h2>Moments</h2><p class=sub>{len(moments)} moments; {len(multi)} have several photos taken "
                f"within {cfg['prefilter']['moment_gap_s']} s of each other. After classification the playoff "
                "keeps the best photo of each category from each moment.</p>")
    body.append(f"<details><summary><b>Moments with several photos</b> ({len(multi)}, largest first)</summary>")
    for m in multi:
        cards = "".join(_card(cfg, links, r["photo_id"]) for r in m)
        body.append(f"<p class=muted>{len(m)} photos</p><div class=grid>{cards}</div>")
    body.append("</details>")
    return _page(cfg, "prefilter.html", "Prefilter", "".join(body))


# ---------------------------------------------------------------- pilot

def pilot_page(cfg: Config, db: DB, results: dict, agreement: dict | None, labels: dict | None = None) -> Path:
    links = _links(cfg, db)
    body = ["<h1>Judge pilot</h1><p class=sub>Each pair was asked in both orders. A judge that is "
            "really looking at the photos should give the same answer either way.</p>"]
    body.append("<table><tr><th>Judge</th><th>Same pick both orders</th><th>Mean order gap</th>"
                "<th>Prefers first photo by</th><th>Decisive pairs</th><th>Input tokens/call</th>"
                "<th>Median call</th></tr>")
    for name, r in results.items():
        body.append(f"<tr><td>{name}</td><td>{r['order_consistent']:.0%}</td><td>{r['order_gap']:.2f}</td>"
                    f"<td>{r['first_bias_logit']:+.2f} logits</td><td>{r['decisive']:.0%}</td>"
                    f"<td>{r['tokens_in']:.0f}</td><td>{r['ms']:.0f} ms</td></tr>")
    body.append("</table>")

    if agreement and agreement["overall"] is not None:
        n1, n2 = agreement["judges"]
        body.append(f"<h2>{n1} vs {n2}</h2><p>They pick the same photo in "
                    f"<b>{agreement['overall']:.0%}</b> of pairs.</p><table><tr><th>{n1}'s margin</th>"
                    f"<th>Pairs</th><th>Agreement</th></tr>")
        for lo, hi, n, a in agreement["bands"]:
            body.append(f"<tr><td>{lo:.1f}–{min(hi, 0.5):.1f}</td><td>{n}</td><td>{a:.0%}</td></tr>")
        body.append("</table><p class=sub>If the first judge is well calibrated, agreement should rise "
                    "with its margin.</p>")

        r1, r2 = results[n1], results[n2]
        disagree = [p for p in r1["pairs"] if p in r2["p_a"] and (r1["p_a"][p] > 0.5) != (r2["p_a"][p] > 0.5)]
        disagree.sort(key=lambda p: -abs(r1["p_a"][p] - 0.5) - abs(r2["p_a"][p] - 0.5))
        body.append(f"<h2>Where they disagree ({len(disagree)})</h2><p class=sub>Most confident "
                    "disagreements first. Which pick do you (or a friend) side with?</p>")
        for a, b in disagree[:30]:
            pa1, pa2 = r1["p_a"][(a, b)], r2["p_a"][(a, b)]
            note = r2["notes"].get((a, b)) or r1["notes"].get((a, b)) or ""
            meta_a = f"{n1} {pa1:.0%} · {n2} {pa2:.0%}"
            meta_b = f"{n1} {1 - pa1:.0%} · {n2} {1 - pa2:.0%}"
            body.append(f"<div class=pair>{_card(cfg, links, a, meta_a)}{_card(cfg, links, b, meta_b)}</div>")
            if note:
                body.append(f"<p class=muted>{html.escape(note)}</p>")

    if labels:
        n1, n2 = labels["judges"]
        body.append(f"<h2>Categories and flags</h2><p>On {labels['n']} photos they chose the same category "
                    f"<b>{labels['bucket_agree']:.0%}</b> of the time. Hard flags drop a photo from ranking, "
                    f"so they need to be right.</p><table><tr><th>Flag</th><th>{n1} flagged</th>"
                    f"<th>{n2} flagged</th><th>Agree</th></tr>")
        for f, (c1, c2, agree) in labels["flags"].items():
            kind = "hard" if f in cfg.hard_flags else "soft"
            body.append(f"<tr><td>{f.replace('_', ' ')} <span class=muted>({kind})</span></td>"
                        f"<td>{c1}</td><td>{c2}</td><td>{agree:.0%}</td></tr>")
        body.append(f"</table><h3>Disagreements ({len(labels['disagree'])})</h3><div class=grid>")
        for pid, b1, f1, b2, f2 in labels["disagree"]:
            meta = (f"<b>{n1}</b>: {b1.replace('_', ' ')} {html.escape(', '.join(f1))}<br>"
                    f"<b>{n2}</b>: {b2.replace('_', ' ')} {html.escape(', '.join(f2))}")
            body.append(_card(cfg, links, pid, meta))
        body.append("</div>")
    return _page(cfg, "pilot.html", "Judge pilot", "".join(body))


# ---------------------------------------------------------------- final report

# Full body comes right after the headshot, so same-outing rules can't crowd it out of the profile.
PROFILE_ORDER = ["headshot", "full_body", "activity_hobby", "travel_scenic", "social_group",
                 "headshot", "activity_hobby", "full_body", "travel_scenic"]


def ranked(cfg: Config, db: DB, use_finals: bool = True) -> dict[str, list[dict]]:
    """Bucket -> rows best first. Finalists come first, ordered by the final judge plus the recency
    bias; everyone else follows, ordered by the ranker's score plus the recency bias ('final')."""
    from .rank import age_offsets
    rows = [dict(r) for r in db.all(
        "SELECT r.*, c.flags, ph.taken_at FROM ratings r JOIN classify c ON c.photo_id = r.photo_id AND c.judge = ? "
        "JOIN photos ph ON ph.id = r.photo_id WHERE r.judge = ?", (cfg["judge"]["classifier"], cfg["judge"]["ranker"]))]
    final_judge = cfg["final"]["judge"]
    finals = ({r["photo_id"]: r for r in db.all("SELECT * FROM finals WHERE judge = ?", (final_judge,))}
              if use_finals and final_judge else {})
    for r, off in zip(rows, age_offsets(cfg, db, [r["photo_id"] for r in rows])):
        r["age_offset"], r["final"] = float(off), r["score"] + float(off)
        f = finals.get(r["photo_id"])
        r["final_judge"] = f["judge"] if f else None
        r["final_score"] = f["score"] + float(off) if f else None
    out: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (r["bucket"], r["final_score"] is None,
                                         -(r["final"] if r["final_score"] is None else r["final_score"]))):
        out.setdefault(r["bucket"], []).append(r)
    return out


def suggested_profile(cfg: Config, db: DB, size: int = 6) -> list:
    """Best photo from each main bucket in Hinge-friendly order (headshot first), then seconds,
    never taking two photos from the same outing (taken within [rank] session_gap_s), and keeping
    face-obscured photos out of the first slot and to at most [rank] max_obscured_picks."""
    by_bucket = ranked(cfg, db)
    when = {r["id"]: r["taken_at"] for r in db.all("SELECT id, taken_at FROM photos")}
    gap = cfg["rank"]["session_gap_s"]
    used: dict[str, int] = {}
    picks: list = []

    def same_outing(pid: str) -> bool:
        t = when.get(pid)
        return t is not None and any(when.get(p["photo_id"]) is not None and abs(when[p["photo_id"]] - t) < gap
                                     for p in picks)

    def obscured(row: dict) -> bool:
        return json.loads(row["flags"]).get("face_obscured", 0) >= 0.5

    def blocked(row: dict) -> bool:
        if same_outing(row["photo_id"]):
            return True
        if not obscured(row):
            return False
        return not picks or sum(map(obscured, picks)) >= cfg["rank"]["max_obscured_picks"]

    for bucket in PROFILE_ORDER:
        if len(picks) >= size:
            break
        rows = by_bucket.get(bucket, [])
        k = used.get(bucket, 0)
        while k < len(rows) and blocked(rows[k]):
            k += 1
        if k < len(rows):
            picks.append(rows[k])
            k += 1
        used[bucket] = k
    return picks


def _soft_badges(cfg: Config, flags_json: str) -> str:
    flags = json.loads(flags_json)
    return "".join(f"<span class=badge>{f.replace('_', ' ')}</span>"
                   for f in cfg["flags"]["soft"] if flags.get(f, 0) >= cfg["flags"]["badge_threshold"])


LABELS = {"jev": "Jev", "gemini_decision": "Gemini Decision", "claude": "Claude", "gemini": "Gemini", "openai": "OpenAI", "fake": "Fake"}


def gallery_data(cfg: Config, db: DB, links: _Gallery) -> dict:
    """Everything the page needs to re-rank and re-pick in the browser (see gallery.js)."""
    explained = {r["photo_id"]: r for r in db.all("SELECT * FROM explanations")}
    opinions = {(r["photo_id"], r["judge"]): r["score"] for r in db.all("SELECT * FROM opinions")}
    by_bucket = ranked(cfg, db)
    final_judge = next((r["final_judge"] for rows in by_bucket.values() for r in rows if r["final_judge"]), None)
    played = {judge for _, judge in opinions}
    opinion_judges = [j for j in cfg["final"]["opinions"] if j in played and j != final_judge] if final_judge else []
    looks = {r["photo_id"]: {name: a["choice"] for name, a in json.loads(r["answers"]).items()}
             for r in db.all("SELECT * FROM looks WHERE judge = ?", (cfg["judge"]["classifier"],))}
    crops_rel = _rel(cfg, "crops")
    buckets = []
    for bucket, rows in by_bucket.items():
        photos = []
        for r in rows:
            pid, flags, e = r["photo_id"], json.loads(r["flags"]), explained.get(r["photo_id"])
            look = looks.get(pid)
            photos.append({
                "id": pid, "img": links.img(pid), "href": links.urls.get(pid) or links.img(pid), "t": r["taken_at"],
                "rank": r["score"], "err": r["stderr"], "n": r["n_comparisons"],
                "scores": {judge: score for judge, score in [
                    (final_judge, None if r["final_score"] is None else r["final_score"] - r["age_offset"]),
                    *((j, opinions.get((pid, j))) for j in opinion_judges)] if score is not None},
                "obscured": flags.get("face_obscured", 0) >= 0.5, "zoomed": bool(links.photos[pid].crop),
                "badges": [f.replace("_", " ") for f in cfg["flags"]["soft"]
                           if flags.get(f, 0) >= cfg["flags"]["badge_threshold"]],
                "look": look["expression"] if look else None,
                "lookLabel": " · ".join(v.replace("_", " ") for v in look.values()) if look else None,
                "reason": e["reason"] if e else None,
                "crop": f"{crops_rel}/{pid}.jpg" if e and e["crop"] else None,
            })
        buckets.append({"name": bucket, "label": bucket.replace("_", " ").capitalize(),
                        "description": cfg.buckets.get(bucket, ""), "photos": photos})
    return {"now": time.time(), "defaultPenalty": cfg["rank"].get("age_penalty_per_year", 0.0),
            "ranker": cfg["judge"]["ranker"], "finalJudge": final_judge, "opinions": opinion_judges, "labels": LABELS,
            "profileOrder": PROFILE_ORDER, "profileSize": 6, "sessionGap": cfg["rank"]["session_gap_s"],
            "maxObscured": cfg["rank"]["max_obscured_picks"], "maxSameLook": cfg["rank"]["max_same_look"],
            "varietyDepth": cfg["refine"]["contenders"], "buckets": buckets}


CONTROLS = """
<form id=controls onsubmit="return false" aria-label="Ranking controls">
  <fieldset id=modes><legend>Judge</legend></fieldset>
  <div class=slider><label for=penalty>Old-photo penalty: <b id=penalty-value></b> per year</label>
    <input id=penalty type=range min=0 max=0.5 step=0.01></div>
  <label><input id=variety type=checkbox> Vary expressions</label>
  <button id=reset type=button>Reset</button>
</form>
"""


def build(cfg: Config, db: DB) -> Path:
    links = _links(cfg, db)
    counts = {r["k"]: r["n"] for r in db.all(
        "SELECT 'photos' k, COUNT(*) n FROM photos UNION ALL "
        "SELECT 'kept', COUNT(*) FROM prefilter WHERE status = 'keep' UNION ALL "
        "SELECT 'ranked', COUNT(*) FROM ratings WHERE judge = ?", (cfg["judge"]["ranker"],))}
    data = gallery_data(cfg, db, links)
    name = lambda judge: LABELS.get(judge, judge)
    ranker, top = name(data["ranker"]), cfg["final"]["per_bucket"]
    intro = (f"{counts['photos']} photos → {counts['kept']} after local filtering → {counts['ranked']} ranked by "
             f"{ranker}. Scores are relative, and only within a category; ± is roughly a 95% range. ")
    if data["finalJudge"]:
        others = [name(j) for j in [data["finalJudge"], *data["opinions"]]]
        who = others[0] if len(others) == 1 else ", ".join(others[:-1]) + " and " + others[-1]
        intro += (f"{who} re-judged each category's top {top}, so choosing one of them or a blend reorders only "
                  f"those spots; everything below stays in {ranker}'s order. Blend averages every judge's scores, "
                  "so a judge with widely spread scores counts for more; Even blend first puts them on the same "
                  "spread. ")
    intro += ("Colors run red, yellow, green, blue, from lowest to highest in the category: a judge's colors compare "
              "only its own scores, and the score's color follows the photo's current place. The old-photo penalty "
              f"was {cfg['rank'].get('age_penalty_per_year', 0.0)} during ranking, so a very different setting relies "
              "on fewer comparisons. <a href=prefilter.html>Prefilter sheet</a>")
    if (cfg.path("out") / "pilot.html").exists():
        intro += " · <a href=pilot.html>Judge pilot</a>"
    body = [f"<h1>Hinge photo picks</h1><p class=sub>{intro}</p>",
            CONTROLS,
            "<h2>Suggested profile</h2><p class=sub>Best of each category, headshot first, with no two photos "
            f"from the same outing. Vary expressions aims for at most {cfg['rank']['max_same_look']} with the same "
            f"expression, wherever you're looking, searching each category's top {cfg['refine']['contenders']} and keeping its best when "
            "none of those fit. It changes this profile, not the rankings below, and each photo's tags are "
            f"{name(cfg['judge']['classifier'])}'s guesses. Upload these with Hinge's Smart Photos on and let real likes reorder them.</p>"
            "<div id=profile class='grid big'></div><div id=categories></div>"]

    thr, hard = cfg["flags"]["hard_threshold"], cfg.hard_flags
    flagged = []
    for r in db.all("SELECT c.photo_id, c.flags FROM classify c JOIN prefilter p ON p.photo_id = c.photo_id "
                    "WHERE p.status = 'keep' AND c.judge = ?", (cfg["judge"]["classifier"],)):
        hits = [f for f in hard if json.loads(r["flags"]).get(f, 0) >= thr]
        if hits:
            flagged.append((r["photo_id"], hits))
    if flagged:
        body.append(f"<details><summary>Left out of ranking: hard flags ({len(flagged)})</summary><div class=grid>")
        body += [_card(cfg, links, pid, "".join(f"<span class='badge bad'>{h.replace('_', ' ')}</span>" for h in hits))
                 for pid, hits in flagged]
        body.append("</div></details>")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    script = (Path(__file__).parent / "gallery.js").read_text()
    body.append(f"<script type=application/json id=gallery-data>{payload}</script><script>{script}</script>")
    return _page(cfg, "index.html", "Hinge photo picks", "".join(body))
