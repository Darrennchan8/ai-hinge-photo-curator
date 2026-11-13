"""The gallery's browser-side ranking, run in Node when it's available."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

GALLERY = Path(__file__).parent.parent / "curator" / "gallery.js"
YEAR = 365.25 * 86400
NOW = 2_000_000_000
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def photo(pid, jev, years_old=0.0, claude=None, obscured=False):
    return {"id": pid, "t": NOW - years_old * YEAR, "jev": jev, "claude": claude, "obscured": obscured}


def shaped(p):
    """The page's format: the ranker's score in rank, the finalist judges' in scores."""
    p = dict(p)
    scores = {k: p.pop(k) for k in ("claude", "gemini", "openai") if k in p}
    return {**p, "rank": p.pop("jev"), "scores": {k: v for k, v in scores.items() if v is not None}}


JUDGES = {"ranker": "jev", "finalJudge": "claude", "opinions": ["gemini", "openai"], "labels": {"jev": "Jev"}}


def view(data, mode, penalty, variety=False):
    data = {"now": NOW, "profileSize": 6, "sessionGap": 7200, "maxObscured": 1, "maxSameLook": 2,
            "varietyDepth": 30, **JUDGES, **data}
    data["buckets"] = [{**b, "photos": [shaped(p) for p in b["photos"]]} for b in data["buckets"]]
    mode = "ranker" if mode == "jev" else mode
    code = (f"const {{computeView}} = require({json.dumps(str(GALLERY))});"
            f"const v = computeView({json.dumps(data)}, {json.dumps(mode)}, {penalty}, {json.dumps(variety)});"
            "console.log(JSON.stringify({b: Object.fromEntries(Object.entries(v.buckets).map("
            "([k, rows]) => [k, rows.map((r) => r.photo.id)])), picks: v.picks.map((r) => r.photo.id)}));")
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


def category(*photos, name="headshot"):
    return {"profileOrder": [name], "buckets": [{"name": name, "photos": list(photos)}]}


def test_judge_toggle_only_reorders_finalists():
    data = category(photo("a", 3.0, claude=-1.0), photo("b", 2.0, claude=1.0), photo("c", 2.5))
    assert view(data, "jev", 0)["b"]["headshot"] == ["a", "c", "b"]
    assert view(data, "claude", 0)["b"]["headshot"] == ["b", "a", "c"]  # finalists stay above non-finalists


def test_blend_uses_both_judges():
    # Jev prefers a by 2, Claude prefers b by 1: the even blend goes with a.
    data = category(photo("a", 3.0, claude=0.0), photo("b", 1.0, claude=1.0))
    assert view(data, "claude", 0)["b"]["headshot"] == ["b", "a"]
    assert view(data, "blend", 0)["b"]["headshot"] == ["a", "b"]


def test_penalty_slider_demotes_old_photos():
    data = category(photo("old", 2.0, years_old=8), photo("new", 1.0))
    assert view(data, "jev", 0)["b"]["headshot"] == ["old", "new"]
    assert view(data, "jev", 0.5)["b"]["headshot"] == ["new", "old"]


def test_picker_skips_same_outing_and_obscured_first_photo():
    data = {"profileOrder": ["headshot", "full_body"], "buckets": [
        {"name": "headshot", "photos": [photo("sunnies", 9.0, obscured=True), photo("clear", 1.0)]},
        {"name": "full_body", "photos": [photo("same_outing", 9.0), photo("other_day", 1.0, years_old=1)]},
    ]}
    assert view(data, "jev", 0)["picks"] == ["clear", "other_day"]


def test_gemini_toggle_and_blend_of_three():
    data = category({**photo("a", 3.0, claude=1.0), "gemini": -1.0}, {**photo("b", 1.0, claude=-1.0), "gemini": 1.0},
                    photo("c", 2.0))
    assert view(data, "gemini", 0)["b"]["headshot"] == ["b", "a", "c"]
    assert view(data, "claude", 0)["b"]["headshot"] == ["a", "b", "c"]
    assert view(data, "blend", 0)["b"]["headshot"] == ["a", "b", "c"]  # Jev breaks the Claude/Gemini tie


def test_blend_includes_every_judge_that_played():
    # OpenAI's opinion alone flips the order that Claude, Gemini and Jev leave tied.
    a = {**photo("a", 2.0, claude=1.0), "gemini": -1.0, "openai": -3.0}
    b = {**photo("b", 2.0, claude=-1.0), "gemini": 1.0, "openai": 3.0}
    assert view(category(a, b), "openai", 0)["b"]["headshot"] == ["b", "a"]
    assert view(category(a, b), "blend", 0)["b"]["headshot"] == ["b", "a"]


def test_even_blend_stops_a_wide_spread_judge_from_outvoting_the_rest():
    # OpenAI alone prefers b, by a lot; the other three prefer a, by a little.
    a = {**photo("a", 2.1, claude=0.1), "gemini": 0.1, "openai": -3.0}
    b = {**photo("b", 2.0, claude=-0.1), "gemini": -0.1, "openai": 3.0}
    assert view(category(a, b), "blend", 0)["b"]["headshot"] == ["b", "a"]
    assert view(category(a, b), "even", 0)["b"]["headshot"] == ["a", "b"]


def test_even_blend_keeps_non_finalists_below_and_survives_a_tied_judge():
    a = {**photo("a", 2.0, claude=1.0), "gemini": 0.5, "openai": 0.5}
    b = {**photo("b", 2.0, claude=-1.0), "gemini": -0.5, "openai": -0.5}  # Jev ties them: no spread
    assert view(category(a, b, photo("c", 9.0)), "even", 0)["b"]["headshot"] == ["a", "b", "c"]


def test_variety_limits_repeated_looks_in_the_profile_only():
    def shot(pid, jev, look, days):
        return {**photo(pid, jev, years_old=days / 365.25), "look": look}
    data = {"profileOrder": ["headshot", "full_body", "travel_scenic"], "buckets": [
        {"name": "headshot", "photos": [shot("h", 9.0, "smile camera", 0)]},
        {"name": "full_body", "photos": [shot("f", 9.0, "smile camera", 1)]},
        {"name": "travel_scenic", "photos": [shot("t1", 9.0, "smile camera", 2), shot("t2", 1.0, "soft away", 3),
                                             {**photo("t3", 0.5, years_old=4 / 365.25), "look": None}]},
    ]}
    assert view(data, "jev", 0)["picks"] == ["h", "f", "t1"]
    varied = view(data, "jev", 0, variety=True)
    assert varied["picks"] == ["h", "f", "t2"]
    assert varied["b"]["travel_scenic"] == ["t1", "t2", "t3"]  # the rankings don't change


def test_variety_keeps_the_best_photo_rather_than_digging_too_deep():
    def shot(pid, jev, look, days):
        return {**photo(pid, jev, years_old=days / 365.25), "look": look}
    data = {"profileOrder": ["headshot", "full_body", "travel_scenic"], "varietyDepth": 2, "buckets": [
        {"name": "headshot", "photos": [shot("h", 9.0, "smile camera", 0)]},
        {"name": "full_body", "photos": [shot("f", 9.0, "smile camera", 1)]},
        {"name": "travel_scenic", "photos": [shot("t1", 9.0, "smile camera", 2), shot("t2", 8.0, "smile camera", 3),
                                             shot("t3", 1.0, "soft away", 4)]},
    ]}
    assert view(data, "jev", 0, variety=True)["picks"] == ["h", "f", "t1"]  # t3 is outside the top 2
    data["varietyDepth"] = 3
    assert view(data, "jev", 0, variety=True)["picks"] == ["h", "f", "t3"]


def test_variety_ignores_untagged_photos():
    data = category(*[{**photo(f"p{k}", 9.0 - k, years_old=k), "look": None} for k in range(3)])
    data["profileOrder"] = ["headshot"] * 3
    assert view(data, "jev", 0, variety=True)["picks"] == ["p0", "p1", "p2"]


def run_js(expr):
    code = f"const g = require({json.dumps(str(GALLERY))}); console.log(JSON.stringify({expr}));"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


def test_score_colors_run_red_to_blue_and_stay_in_range():
    hues = run_js("[-1, 0, 1/3, 2/3, 1, 2].map(g.scoreHue)")
    assert hues == pytest.approx([0, 0, 50, 130, 210, 210])


def test_score_ranges_use_each_judges_own_scores():
    photos = [shaped({"jev": 3, "claude": 1, "gemini": None}), shaped({"jev": -1, "claude": None})]
    assert run_js(f"g.scoreRanges({json.dumps(JUDGES)}, {json.dumps(photos)})") == {"ranker": [-1, 3], "claude": [1, 1]}


def test_without_a_final_judge_only_the_ranker_counts():
    data = {**category(photo("a", 1.0, claude=5.0), photo("b", 2.0, claude=-5.0)), "finalJudge": None}
    assert view(data, "blend", 0)["b"]["headshot"] == ["b", "a"]  # leftover final scores are ignored
    assert run_js(f"g.modes({json.dumps({**JUDGES, 'finalJudge': None})})") == [["ranker", "Jev"]]
    assert [m for m, _ in run_js(f"g.modes({json.dumps(JUDGES)})")] == [
        "ranker", "claude", "gemini", "openai", "blend", "even"]
