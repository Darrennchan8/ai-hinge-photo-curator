import random

import numpy as np

from curator.config import Config
from curator.db import DB
from curator.judges.base import Verdict
from curator.rank import fit_bt, refine_bucket, select_pairs


def _sigmoid(x):
    return 1 / (1 + np.exp(-x))


def test_fit_recovers_known_order_from_soft_labels():
    rng = np.random.default_rng(0)
    true = np.linspace(-2, 2, 12)
    obs = []
    for _ in range(400):
        i, j = rng.choice(12, 2, replace=False)
        obs.append((i, j, float(_sigmoid(true[i] - true[j]))))
    scores, stderr = fit_bt(12, obs, prior=0.01)
    assert list(np.argsort(scores)) == list(range(12))
    assert np.corrcoef(scores, true)[0, 1] > 0.99
    assert np.all(stderr > 0)


def test_position_bias_cancels_when_both_orders_asked():
    true = np.array([0.0, 1.0, 2.0])
    bias = 0.8  # judge favors whichever photo is shown first
    obs = []
    for i in range(3):
        for j in range(3):
            if i != j:
                obs.append((i, j, float(_sigmoid(true[i] - true[j] + bias))))
    scores, _, est_bias = fit_bt(3, obs, prior=1e-4, return_bias=True)
    assert np.allclose(scores - scores.mean(), true - true.mean(), atol=0.05)
    assert abs(est_bias - bias) < 0.05


def test_unconnected_photos_stay_finite():
    scores, stderr = fit_bt(4, [(0, 1, 1.0)])
    assert np.all(np.isfinite(scores)) and scores[0] > scores[1]
    assert stderr[2] > stderr[0]


def test_select_pairs_skips_compared_and_reuses_no_photo():
    scores, stderr = np.zeros(6), np.ones(6)
    compared = {frozenset((0, 1))}
    pairs = select_pairs(scores, stderr, compared, top_k=6, rng=random.Random(0))
    used = [p for pair in pairs for p in pair]
    assert len(used) == len(set(used))
    assert frozenset((0, 1)) not in {frozenset(p) for p in pairs}


def test_adaptive_loop_finds_true_top_k():
    rng = random.Random(1)
    n, k = 60, 5
    true = np.array([rng.gauss(0, 1.5) for _ in range(n)])
    obs, compared = [], set()
    for _ in range(40):
        scores, stderr = fit_bt(n, obs)
        pairs = select_pairs(scores, stderr, compared, k, rng)
        if not pairs or len(compared) > 5 * n:
            break
        for i, j in pairs:
            noise = rng.gauss(0, 0.3)
            obs.append((i, j, float(_sigmoid(true[i] - true[j] + noise))))
            obs.append((j, i, float(_sigmoid(true[j] - true[i] + noise))))
            compared.add(frozenset((i, j)))
    scores, _ = fit_bt(n, obs)
    found = set(np.argsort(-scores)[:k])
    best = set(np.argsort(-true)[:k])
    assert len(found & best) >= k - 1


def test_offset_steers_contention_toward_recent_photos():
    # Equal judged scores; a big recency offset should make photo 0 the only top-1 contender.
    scores, stderr = np.zeros(4), np.full(4, 0.1)
    offset = np.array([0.0, -2.0, -2.0, -2.0])
    pairs = select_pairs(scores, stderr, set(), top_k=1, rng=random.Random(0), offset=offset)
    assert [p[0] for p in pairs] == [0]


def test_select_pairs_only_plays_the_named_photos():
    scores, stderr = np.zeros(6), np.ones(6)
    pairs = select_pairs(scores, stderr, set(), 6, random.Random(0), only=[0, 1])
    assert pairs and all(0 in pair or 1 in pair for pair in pairs)


def test_refine_spends_its_budget_on_the_top_contenders(tmp_path):
    ids = [f"p{k}" for k in range(12)]
    strength = {pid: -k * 0.3 for k, pid in enumerate(ids)}  # p0 is best

    class Judge:
        name = "stub"

        def compare(self, first, second):
            return Verdict(float(_sigmoid(strength[first.id] - strength[second.id])), None, 0)

    db = DB(tmp_path / "t.db")
    db.executemany("INSERT INTO photos (id, src, taken_at, width, height) VALUES (?,?,?,?,?)",
                   [(pid, pid, None, 100, 100) for pid in ids])
    cfg = Config({"paths": {"work": "work"}, "judge": {"concurrency": 2}, "rank": {}, "persona": {"audience": "x"},
                  "refine": {"contenders": 4, "comparisons_per_photo": 3}}, tmp_path)
    refine_bucket(cfg, db, Judge(), "headshot", ids)

    rated = {r["photo_id"]: r for r in db.all("SELECT * FROM ratings WHERE judge = 'stub'")}
    assert set(rated) == set(ids)
    pairs = db.one("SELECT COUNT(*) FROM comparisons")[0] / 2
    assert 0 < pairs <= 4 * 3 // 2  # stays within the budget, both orders asked


def test_ranking_prefers_the_current_wording_for_a_pair(tmp_path):
    from curator.judges.base import prompt_version
    from curator.rank import _observations
    cfg = Config({"persona": {"audience": "x"}, "rank": {"reuse_prompt_versions": ["old"]}}, tmp_path)
    now = prompt_version(cfg)
    db = DB(tmp_path / "t.db")
    row = "INSERT INTO comparisons (judge, prompt_version, first, second, p_first) VALUES ('j', ?, ?, ?, ?)"
    for args in (("old", "a", "b", 0.9), (now, "a", "b", 0.2), ("old", "b", "c", 0.7), ("older", "a", "c", 0.5)):
        db.execute(row, args)
    obs, compared = _observations(cfg, db, "j", {"a": 0, "b": 1, "c": 2})
    assert sorted(obs) == [(0, 1, 0.2), (1, 2, 0.7)]  # the current answer wins; "older" isn't reused
    assert compared == {frozenset((0, 1)), frozenset((1, 2))}


def test_prompt_version_follows_the_persona(tmp_path):
    from curator.judges.base import prompt_version
    a, b = (Config({"persona": {"audience": who}}, tmp_path) for who in ("a man", "a woman"))
    assert prompt_version(a) != prompt_version(b)
    assert prompt_version(a) == prompt_version(Config({"persona": {"audience": "a man"}}, tmp_path))
