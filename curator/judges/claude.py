"""Claude vision judge. It gives a hard pick plus a self-reported confidence rather than probabilities."""

import base64

import anthropic

from ..config import Config
from ..images import judge_file
from .base import (REFERENCE_TEXT, Classification, Photo, Verdict, classify_instructions,
                   compare_instructions, explain_instructions, reference_path)


class ClaudeJudge:
    name = "claude"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = cfg["claude"]["model"]
        self.side = cfg["judge"]["image_side"]
        self.ref = reference_path(cfg)
        key = cfg.secret("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("Add ANTHROPIC_API_KEY=... to .env (see .env.example) to use Claude.")
        self.client = anthropic.Anthropic(api_key=key, max_retries=6)

    def _image(self, photo: Photo) -> dict:
        return self._image_path(photo.path, photo.crop)

    def _image_path(self, path, crop=None) -> dict:
        data = base64.b64encode(judge_file(self.cfg.path("judge"), path, self.side, crop).read_bytes()).decode()
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}

    def _reference(self, *photos: Photo) -> list:
        """Reference image blocks when any photo is a group shot or the owner wasn't located."""
        if self.ref is None or not any(p.needs_ref for p in photos):
            return []
        return [{"type": "text", "text": REFERENCE_TEXT}, self._image_path(self.ref)]

    def _tool_call(self, content: list, tool: dict, max_tokens: int = 400):
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": content}],
        )
        block = next(b for b in resp.content if b.type == "tool_use")
        return block.input, resp.usage

    def compare(self, first: Photo, second: Photo) -> Verdict:
        content = self._reference(first, second) + [
            {"type": "text", "text": "Two photos of the same person, the profile owner, being "
                                     "considered for their Hinge dating profile.\n\nPhoto A:"},
            self._image(first),
            {"type": "text", "text": f"In photo A, {first.hint}.\n\nPhoto B:"},
            self._image(second),
            {"type": "text", "text": f"In photo B, {second.hint}.\n\n{compare_instructions(self.cfg)}"},
        ]
        tool = {
            "name": "verdict",
            "description": "Record which photo makes the stronger profile photo.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "At most 20 words."},
                    "better": {"type": "string", "enum": ["A", "B"]},
                    "confidence": {"type": "number", "minimum": 0.5, "maximum": 1.0,
                                   "description": "Probability your pick is right; 0.5 means a toss-up."},
                },
                "required": ["reason", "better", "confidence"],
            },
        }
        out, usage = self._tool_call(content, tool)
        conf = min(1.0, max(0.5, float(out["confidence"])))
        p_a = conf if out["better"] == "A" else 1 - conf
        return Verdict(p_a, conf, usage.input_tokens, usage.output_tokens, out.get("reason"))

    def classify(self, photo: Photo) -> Classification:
        flags = self.cfg.flags
        content = self._reference(photo) + [
            {"type": "text", "text": "A candidate photo for the profile owner's Hinge dating profile:"},
            self._image(photo),
            {"type": "text", "text": f"In this photo, {photo.hint}.\n\n{classify_instructions()}\n\n"
                                     "Also give, for each flag, the probability that it applies."},
        ]
        tool = {
            "name": "classify",
            "description": "Record the photo's category and flags.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bucket": {"type": "string", "enum": list(self.cfg.buckets),
                               "description": "; ".join(f"{k}: {v}" for k, v in self.cfg.buckets.items())},
                    "flags": {"type": "object",
                              "properties": {f: {"type": "number", "minimum": 0, "maximum": 1, "description": t}
                                             for f, t in flags.items()},
                              "required": list(flags)},
                },
                "required": ["bucket", "flags"],
            },
        }
        out, usage = self._tool_call(content, tool)
        return Classification(
            bucket=out["bucket"],
            bucket_probs={out["bucket"]: 1.0},
            flags={f: float(out["flags"].get(f, 0.0)) for f in flags},
            tokens=usage.input_tokens,
            tokens_out=usage.output_tokens,
        )

    def explain(self, photo: Photo, bucket: str) -> tuple[str, dict | None, tuple[int, int]]:
        before, after = explain_instructions(self.cfg, bucket, photo.hint)
        content = self._reference(photo) + [
            {"type": "text", "text": before},
            self._image(photo),
            {"type": "text", "text": after},
        ]
        frac = {"type": "number", "minimum": 0, "maximum": 1}
        tool = {
            "name": "explain",
            "description": "Record the rationale and an optional crop, as fractions of width/height.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "crop": {"type": ["object", "null"],
                             "properties": {"left": frac, "top": frac, "right": frac, "bottom": frac}},
                },
                "required": ["reason"],
            },
        }
        out, usage = self._tool_call(content, tool)
        return out["reason"], out.get("crop"), (usage.input_tokens, usage.output_tokens)
