"""OpenAI vision judge, for another opinion on the finalists. Like Claude and Gemini, it gives a hard pick
plus a self-reported confidence rather than probabilities. Requests aren't stored on OpenAI's side."""

import base64
import json
import time

import httpx

from ..config import Config
from ..images import judge_file
from .base import REFERENCE_TEXT, Photo, Verdict, compare_instructions, explain_instructions, reference_path

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "At most 20 words."},
        "better": {"type": "string", "enum": ["A", "B"]},
        "confidence": {"type": "number", "description": "Probability your pick is right, from 0.5 (a toss-up) to 1."},
    },
    "required": ["reason", "better", "confidence"],
    "additionalProperties": False,
}


_FRACTION = {"type": "number", "description": "A fraction of the photo's width or height, from 0 to 1."}
EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "crop": {"anyOf": [
            {"type": "object",
             "properties": {"left": _FRACTION, "top": _FRACTION, "right": _FRACTION, "bottom": _FRACTION},
             "required": ["left", "top", "right", "bottom"], "additionalProperties": False},
            {"type": "null"},
        ]},
    },
    "required": ["reason", "crop"],
    "additionalProperties": False,
}


class OpenAIJudge:
    name = "openai"

    def __init__(self, cfg: Config):
        key = cfg.secret("OPENAI_API_KEY")
        if not key:
            raise SystemExit("Add OPENAI_API_KEY=... to .env (see .env.example) to use OpenAI.")
        self.cfg = cfg
        self.model = cfg["openai"]["model"]
        self.side = cfg["judge"]["image_side"]
        self.ref = reference_path(cfg)
        self.http = httpx.Client(
            base_url=cfg["openai"]["base_url"],
            timeout=httpx.Timeout(120.0, connect=15.0),
            headers={"Authorization": f"Bearer {key}"},
        )

    def _image(self, path, crop=None) -> dict:
        data = base64.b64encode(judge_file(self.cfg.path("judge"), path, self.side, crop).read_bytes()).decode()
        return {"type": "input_image", "image_url": f"data:image/jpeg;base64,{data}", "detail": "high"}

    def _reference(self, *photos: Photo) -> list:
        """Reference image blocks when any photo is a group shot or the owner wasn't located."""
        if self.ref is None or not any(p.needs_ref for p in photos):
            return []
        return [{"type": "input_text", "text": REFERENCE_TEXT}, self._image(self.ref)]

    def _call(self, body: dict) -> dict:
        for attempt in range(6):
            try:
                resp = self.http.post("/v1/responses", json=body)
            except httpx.TransportError:
                if attempt == 5:
                    raise
            else:
                if resp.status_code not in RETRY_STATUS:
                    if resp.is_error:
                        raise RuntimeError(f"OpenAI {resp.status_code}: {resp.text[:500]}")
                    return resp.json()
                if attempt == 5:
                    resp.raise_for_status()
            time.sleep(min(60, 2 ** attempt))
        raise AssertionError("unreachable")

    def _answer(self, content: list, name: str, schema: dict) -> tuple[dict, dict]:
        out = self._call({
            "model": self.model,
            "store": False,
            "input": [{"role": "user", "content": content}],
            "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
        })
        text = "".join(part.get("text", "") for item in out.get("output", []) if item.get("type") == "message"
                       for part in item.get("content", []) if part.get("type") == "output_text")
        if not text:  # e.g. a refusal
            raise RuntimeError(f"OpenAI gave no answer (status {out.get('status')})")
        return json.loads(text), out.get("usage", {})  # output_tokens already includes reasoning

    def compare(self, first: Photo, second: Photo) -> Verdict:
        answer, usage = self._answer(self._reference(first, second) + [
            {"type": "input_text", "text": "Two photos of the same person, the profile owner, being "
                                           "considered for their Hinge dating profile.\n\nPhoto A:"},
            self._image(first.path, first.crop),
            {"type": "input_text", "text": f"In photo A, {first.hint}.\n\nPhoto B:"},
            self._image(second.path, second.crop),
            {"type": "input_text", "text": f"In photo B, {second.hint}.\n\n{compare_instructions(self.cfg)}"},
        ], "verdict", SCHEMA)
        conf = min(1.0, max(0.5, float(answer["confidence"])))
        p_a = conf if answer["better"] == "A" else 1 - conf
        return Verdict(p_a, conf, usage.get("input_tokens", 0), usage.get("output_tokens", 0), answer.get("reason"))

    def explain(self, photo: Photo, bucket: str) -> tuple[str, dict | None, tuple[int, int]]:
        before, after = explain_instructions(self.cfg, bucket, photo.hint)
        answer, usage = self._answer(self._reference(photo) + [
            {"type": "input_text", "text": before},
            self._image(photo.path, photo.crop),
            {"type": "input_text", "text": after},
        ], "explain", EXPLAIN_SCHEMA)
        return answer["reason"], answer.get("crop"), (usage.get("input_tokens", 0), usage.get("output_tokens", 0))
