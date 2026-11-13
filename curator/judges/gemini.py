"""Gemini vision judge, for a second opinion on the finalists. Like Claude, it gives a hard pick plus a
self-reported confidence rather than probabilities. Requests aren't stored on Google's side."""

import base64
import json
import time

import httpx

from ..config import Config
from ..images import judge_file
from .base import REFERENCE_TEXT, Photo, Verdict, compare_instructions, reference_path

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "At most 20 words."},
        "better": {"type": "string", "enum": ["A", "B"]},
        "confidence": {"type": "number", "description": "Probability your pick is right, from 0.5 (a toss-up) to 1."},
    },
    "required": ["reason", "better", "confidence"],
}


class GeminiJudge:
    name = "gemini"

    def __init__(self, cfg: Config):
        key = cfg.secret("GEMINI_API_KEY")
        if not key:
            raise SystemExit("Add GEMINI_API_KEY=... to .env (see .env.example) to use Gemini.")
        self.cfg = cfg
        self.model = cfg["gemini"]["model"]
        self.side = cfg["judge"]["image_side"]
        self.ref = reference_path(cfg)
        self.http = httpx.Client(
            base_url=cfg["gemini"]["base_url"],
            timeout=httpx.Timeout(120.0, connect=15.0),
            headers={"x-goog-api-key": key},
        )

    def _image(self, path, crop=None) -> dict:
        data = base64.b64encode(judge_file(self.cfg.path("judge"), path, self.side, crop).read_bytes()).decode()
        return {"type": "image", "mime_type": "image/jpeg", "data": data}

    def _reference(self, *photos: Photo) -> list:
        """Reference image blocks when any photo is a group shot or the owner wasn't located."""
        if self.ref is None or not any(p.needs_ref for p in photos):
            return []
        return [{"type": "text", "text": REFERENCE_TEXT}, self._image(self.ref)]

    def _call(self, body: dict) -> dict:
        for attempt in range(6):
            try:
                resp = self.http.post("/v1beta/interactions", json=body)
            except httpx.TransportError:
                if attempt == 5:
                    raise
            else:
                if resp.status_code not in RETRY_STATUS:
                    if resp.is_error:
                        raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:500]}")
                    return resp.json()
                if attempt == 5:
                    resp.raise_for_status()
            time.sleep(min(60, 2 ** attempt))
        raise AssertionError("unreachable")

    def compare(self, first: Photo, second: Photo) -> Verdict:
        body = {
            "model": self.model,
            "store": False,
            "input": self._reference(first, second) + [
                {"type": "text", "text": "Two photos of the same person, the profile owner, being "
                                         "considered for their Hinge dating profile.\n\nPhoto A:"},
                self._image(first.path, first.crop),
                {"type": "text", "text": f"In photo A, {first.hint}.\n\nPhoto B:"},
                self._image(second.path, second.crop),
                {"type": "text", "text": f"In photo B, {second.hint}.\n\n{compare_instructions(self.cfg)}"},
            ],
            "response_format": {"type": "text", "mime_type": "application/json", "schema": SCHEMA},
        }
        out = self._call(body)
        text = "".join(part.get("text", "") for step in out.get("steps", []) if step.get("type") == "model_output"
                       for part in step.get("content", []))
        if not text:  # e.g. a safety block
            raise RuntimeError(f"Gemini gave no answer (status {out.get('status')})")
        answer = json.loads(text)
        conf = min(1.0, max(0.5, float(answer["confidence"])))
        p_a = conf if answer["better"] == "A" else 1 - conf
        usage = out.get("usage", {})
        # Thinking is billed as output.
        return Verdict(p_a, conf, usage.get("total_input_tokens", 0),
                       usage.get("total_output_tokens", 0) + usage.get("total_thought_tokens", 0), answer.get("reason"))
