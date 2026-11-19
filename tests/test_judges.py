"""Check the request shapes sent to the judges without calling any API."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from curator import config
from curator.judges.base import Photo


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "ref").mkdir()
    Image.new("RGB", (600, 600), "white").save(tmp_path / "ref" / "me.jpg")
    for name, size in (("a", (1568, 1176)), ("b", (1176, 1568)), ("pano", (1568, 200))):
        Image.new("RGB", size, "gray").save(tmp_path / f"{name}.jpg")
    base = config.load(None).raw
    raw = base | {"paths": {**base["paths"], "reference": "ref"}}
    return config.Config(raw, tmp_path)


def photo(cfg, name, needs_ref=False):
    return Photo(name, cfg.base / f"{name}.jpg", "the profile owner's face is about 50% from the left", needs_ref)


def jev(cfg, monkeypatch, answers):
    from curator.judges import jev as jev_mod
    sent = []

    def handler(request):
        sent.append((request, json.loads(request.content)))
        return httpx.Response(200, json={"model": "jev-1.14.0", "answers": answers,
                                         "usage": {"input_tokens": 1234, "output_tokens": 5}})

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    judge = jev_mod.JevJudge(cfg)
    judge.http = httpx.Client(base_url="https://api.typesafe.ai", transport=httpx.MockTransport(handler),
                              headers=judge.http.headers)
    return judge, sent


def test_jev_compare_request(cfg, monkeypatch):
    judge, sent = jev(cfg, monkeypatch, {"better": {"type": "choice", "choice": "B", "confidence": 0.4,
                                                    "probabilities": {"A": 0.3, "B": 0.7}}})
    v = judge.compare(photo(cfg, "a"), photo(cfg, "pano"))
    req, body = sent[0]
    assert req.url.path == "/v1/systemone"
    assert req.headers["TypeSafe-Danger-Zone-Images"] == "true"
    assert req.headers["Authorization"] == "Bearer test-key"
    assert set(body) == {"model", "state", "assets", "questions"}
    assert len(body["assets"]) == 2  # no reference: neither photo needs one
    for key, asset in body["assets"].items():
        assert f"`{key}`" in body["state"]
        assert asset["type"] == "image" and asset["format"] == "jpeg"
        img = Image.open(__import__("io").BytesIO(base64.b64decode(asset["data_base64"])))
        assert 280 <= min(img.size) and max(img.size) <= 1568  # panorama got enlarged/padded
    q = body["questions"]["better"]
    assert q["type"] == "choice" and set(q["criteria"]) == {"A", "B"}
    assert v.p_first == pytest.approx(0.3) and v.tokens_in == 1234


def test_jev_sends_reference_for_group_photos(cfg, monkeypatch):
    judge, sent = jev(cfg, monkeypatch, {"better": {"type": "choice", "choice": "A", "confidence": 1,
                                                    "probabilities": {"A": 1.0, "B": 0.0}}})
    judge.compare(photo(cfg, "a", needs_ref=True), photo(cfg, "b"))
    _, body = sent[0]
    assert len(body["assets"]) == 3
    assert body["state"].startswith("For reference only")
    assert all(f"`{k}`" in body["state"] for k in body["assets"])


def test_jev_classify_request(cfg, monkeypatch):
    answers = {"bucket": {"type": "choice", "choice": "headshot", "confidence": 0.9,
                          "probabilities": {b: (0.9 if b == "headshot" else 0.02) for b in cfg.buckets}}}
    answers |= {f"flag_{f}": {"type": "noul", "noul": 0.1} for f in cfg.flags}
    judge, sent = jev(cfg, monkeypatch, answers)
    c = judge.classify(photo(cfg, "b"))
    _, body = sent[0]
    assert body["questions"]["bucket"]["criteria"] == cfg.buckets
    assert all(body["questions"][f"flag_{f}"]["type"] == "noul" for f in cfg.flags)
    assert c.bucket == "headshot" and c.flags["eyes_closed"] == 0.1


def test_jev_choose_request(cfg, monkeypatch):
    from curator.looks import QUESTIONS
    answers = {name: {"type": "choice", "choice": next(iter(options)), "confidence": 0.9,
                      "probabilities": {o: 1 / len(options) for o in options}}
               for name, (_, options) in QUESTIONS.items()}
    judge, sent = jev(cfg, monkeypatch, answers)
    out, tokens = judge.choose(photo(cfg, "a"), QUESTIONS)
    _, body = sent[0]
    assert len(body["assets"]) == 1
    assert all(q["type"] == "choice" for q in body["questions"].values())
    assert set(body["questions"]["gaze"]["criteria"]) == {"at_camera", "looking_away"}
    assert out["expression"]["choice"] == "big_smile" and set(out["expression"]) == {"choice", "probabilities"}
    assert tokens == 1234


def test_claude_compare_request(cfg, monkeypatch):
    from curator.judges import claude as claude_mod
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    judge = claude_mod.ClaudeJudge(cfg)
    calls = []

    def create(**kw):
        calls.append(kw)
        block = SimpleNamespace(type="tool_use", input={"reason": "clearer face", "better": "B", "confidence": 0.8})
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=2000, output_tokens=40))

    judge.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    v = judge.compare(photo(cfg, "a"), photo(cfg, "b", needs_ref=True))
    kw = calls[0]
    assert kw["model"] == cfg["claude"]["model"]
    assert kw["tool_choice"] == {"type": "tool", "name": "verdict"}
    images = [c for c in kw["messages"][0]["content"] if c["type"] == "image"]
    assert len(images) == 3  # reference + two candidates
    assert v.p_first == pytest.approx(0.2) and v.note == "clearer face"


def test_gemini_compare_request_and_answer(cfg, monkeypatch):
    from curator.judges.gemini import GeminiJudge
    sent = []

    def handler(request):
        sent.append((request, json.loads(request.content)))
        return httpx.Response(200, json={
            "status": "completed",
            "usage": {"total_input_tokens": 100, "total_output_tokens": 10, "total_thought_tokens": 5},
            "steps": [{"type": "thought"}, {"type": "model_output", "content": [
                {"type": "text", "text": '{"reason": "warmer smile", "better": "B", "confidence": 0.8}'}]}]})

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    judge = GeminiJudge(cfg)
    judge.http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler),
                              headers=judge.http.headers)
    v = judge.compare(photo(cfg, "a"), photo(cfg, "b"))
    req, body = sent[0]
    assert req.url.path == "/v1beta/interactions"
    assert req.headers["x-goog-api-key"] == "test-key" and "test-key" not in str(req.url)
    assert body["store"] is False
    assert sum(part["type"] == "image" for part in body["input"]) == 2
    assert abs(v.p_first - 0.2) < 1e-9
    assert (v.tokens_in, v.tokens_out, v.note) == (100, 15, "warmer smile")  # thinking is billed as output


def test_openai_compare_request_and_answer(cfg, monkeypatch):
    from curator.judges.openai import OpenAIJudge
    sent = []

    def handler(request):
        sent.append((request, json.loads(request.content)))
        return httpx.Response(200, json={
            "status": "completed", "usage": {"input_tokens": 100, "output_tokens": 15},
            "output": [{"type": "reasoning"}, {"type": "message", "content": [
                {"type": "output_text", "text": '{"reason": "warmer smile", "better": "B", "confidence": 0.8}'}]}]})

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    judge = OpenAIJudge(cfg)
    judge.http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler),
                              headers=judge.http.headers)
    v = judge.compare(photo(cfg, "a"), photo(cfg, "b"))
    req, body = sent[0]
    assert req.url.path == "/v1/responses" and req.headers["Authorization"] == "Bearer test-key"
    assert body["store"] is False
    assert sum(part["type"] == "input_image" for part in body["input"][0]["content"]) == 2
    assert abs(v.p_first - 0.2) < 1e-9
    assert (v.tokens_in, v.tokens_out, v.note) == (100, 15, "warmer smile")


def test_openai_refusal_is_a_failed_call(cfg, monkeypatch):
    from curator.judges.openai import OpenAIJudge
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    judge = OpenAIJudge(cfg)
    judge.http = httpx.Client(base_url="https://example.invalid", headers=judge.http.headers, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]})))
    with pytest.raises(RuntimeError, match="no answer"):
        judge.compare(photo(cfg, "a"), photo(cfg, "b"))


def test_openai_explain_request_and_answer(cfg, monkeypatch):
    from curator.judges.openai import OpenAIJudge
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "usage": {"input_tokens": 50, "output_tokens": 20},
                                         "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(
                                             {"reason": "Clear, warm smile.", "crop": None})}]}]})

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    judge = OpenAIJudge(cfg)
    judge.http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler),
                              headers=judge.http.headers)
    reason, crop, usage = judge.explain(photo(cfg, "a"), "headshot")
    body = sent[0]
    assert body["store"] is False and body["text"]["format"]["strict"] is True
    assert sum(part["type"] == "input_image" for part in body["input"][0]["content"]) == 1
    assert cfg["persona"]["audience"] in body["input"][0]["content"][-1]["text"]
    assert (reason, crop, usage) == ("Clear, warm smile.", None, (50, 20))


def decision(cfg, monkeypatch, answers):
    from curator.judges import gemini_decision
    sent = []

    def handler(request):
        sent.append((request, json.loads(request.content)))
        return httpx.Response(200, json={"answers": answers, "usage": {"total_input_tokens": 2278,
                                                                       "total_output_tokens": 0}})

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    judge = gemini_decision.GeminiDecisionJudge(cfg)
    judge.http = httpx.Client(base_url="https://generativelanguage.googleapis.com",
                              transport=httpx.MockTransport(handler), headers=judge.http.headers)
    return judge, sent


def test_gemini_decision_compare_request(cfg, monkeypatch):
    judge, sent = decision(cfg, monkeypatch, {"better": {"type": "choice", "choice": "B", "confidence": 0.7,
                                                         "probabilities": {"A": 0.3, "B": 0.7}}})
    v = judge.compare(photo(cfg, "a"), photo(cfg, "b", needs_ref=True))
    req, body = sent[0]
    assert req.url.path == "/v1beta/decisions"
    assert req.headers["x-goog-api-key"] == "test-key"
    assert body["model"] == "gemini-decision-1" and set(body) == {"model", "input", "questions"}
    kinds = [block["type"] for block in body["input"]]
    assert kinds == ["text", "image", "text", "image", "text", "image", "text"]  # reference, A, B
    assert body["input"][0]["text"].startswith("For reference only")
    assert all(b["mime_type"] == "image/jpeg" for b in body["input"] if b["type"] == "image")
    q = body["questions"]["better"]
    assert q["type"] == "choice" and set(q["criteria"]) == {"A", "B"}
    assert v.p_first == pytest.approx(0.3) and v.tokens_in == 2278 and v.tokens_out == 0


def test_gemini_decision_classify_request(cfg, monkeypatch):
    answers = {"bucket": {"type": "choice", "choice": "full_body", "confidence": 0.8,
                          "probabilities": {b: (0.8 if b == "full_body" else 0.03) for b in cfg.buckets}}}
    answers |= {f"flag_{f}": {"type": "noul", "noul": 0.2, "confidence": 0.8} for f in cfg.flags}
    judge, sent = decision(cfg, monkeypatch, answers)
    c = judge.classify(photo(cfg, "a"))
    _, body = sent[0]
    assert [b["type"] for b in body["input"]] == ["text", "image", "text"]
    assert body["questions"]["bucket"]["criteria"] == cfg.buckets
    assert all(body["questions"][f"flag_{f}"]["type"] == "noul" for f in cfg.flags)
    assert c.bucket == "full_body" and c.flags["low_quality"] == pytest.approx(0.2) and c.tokens == 2278
