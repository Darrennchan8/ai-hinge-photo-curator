"""Gemini Decision 1: like Jev, typed decisions with a probability for every option, and output is free.
It reads images natively and takes both photos of a pair in one request. Unlike the Gemini judge, the
Decisions API has no option to ask Google not to store requests."""

import base64
import time

import httpx

from ..config import Config
from ..images import judge_file
from .base import (REFERENCE_TEXT, Classification, Photo, Verdict, classify_instructions,
                   compare_instructions, reference_path)

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class GeminiDecisionJudge:
    name = "gemini_decision"

    def __init__(self, cfg: Config):
        key = cfg.secret("GEMINI_API_KEY")
        if not key:
            raise SystemExit("Add GEMINI_API_KEY=... to .env (see .env.example) to use Gemini Decision.")
        self.cfg = cfg
        self.model = cfg["gemini_decision"]["model"]
        self.side = cfg["judge"]["image_side"]
        self.ref = reference_path(cfg)
        self.http = httpx.Client(
            base_url=cfg["gemini_decision"]["base_url"],
            timeout=httpx.Timeout(90.0, connect=15.0),
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

    def _one(self, photo: Photo, intro: str) -> list:
        return self._reference(photo) + [{"type": "text", "text": intro}, self._image(photo.path, photo.crop),
                                         {"type": "text", "text": f"In this photo, {photo.hint}."}]

    def _call(self, input_: list, questions: dict) -> dict:
        body = {"model": self.model, "input": input_, "questions": questions}
        for attempt in range(6):
            try:
                resp = self.http.post("/v1beta/decisions", json=body)
            except httpx.TransportError:
                if attempt == 5:
                    raise
            else:
                if resp.status_code not in RETRY_STATUS:
                    if resp.is_error:
                        raise RuntimeError(f"Gemini Decision {resp.status_code}: {resp.text[:500]}")
                    return resp.json()
                if attempt == 5:
                    resp.raise_for_status()
            time.sleep(min(60, 2 ** attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _tokens(out: dict) -> int:
        return out.get("usage", {}).get("total_input_tokens", 0)

    def compare(self, first: Photo, second: Photo) -> Verdict:
        input_ = self._reference(first, second) + [
            {"type": "text", "text": "Two photos of the same person, the profile owner, being considered for "
                                     "their Hinge dating profile.\n\nPhoto A:"},
            self._image(first.path, first.crop),
            {"type": "text", "text": f"In photo A, {first.hint}.\n\nPhoto B:"},
            self._image(second.path, second.crop),
            {"type": "text", "text": f"In photo B, {second.hint}."},
        ]
        out = self._call(input_, {"better": {
            "type": "choice",
            "instructions": compare_instructions(self.cfg),
            "criteria": {
                "A": "Photo A would make the stronger Hinge profile photo",
                "B": "Photo B would make the stronger Hinge profile photo",
            },
        }})
        ans = out["answers"]["better"]
        probs = ans["probabilities"]
        return Verdict(probs["A"] / (probs["A"] + probs["B"]), ans.get("confidence"), self._tokens(out))

    def yes_no(self, photo: Photo, statement: str) -> tuple[float, int]:
        out = self._call(self._one(photo, "A photo of the profile owner:"),
                         {"answer": {"type": "noul", "instructions": statement}})
        return float(out["answers"]["answer"]["noul"]), self._tokens(out)

    def choose(self, photo: Photo, questions: dict[str, tuple[str, dict[str, str]]]) -> tuple[dict, int]:
        out = self._call(self._one(photo, "A photo of the profile owner:"),
                         {name: {"type": "choice", "instructions": text, "criteria": options}
                          for name, (text, options) in questions.items()})
        answers = {name: {"choice": out["answers"][name]["choice"],
                          "probabilities": out["answers"][name]["probabilities"]} for name in questions}
        return answers, self._tokens(out)

    def classify(self, photo: Photo) -> Classification:
        questions = {"bucket": {"type": "choice", "instructions": classify_instructions(),
                                "criteria": self.cfg.buckets}}
        for flag, text in self.cfg.flags.items():
            questions[f"flag_{flag}"] = {"type": "noul", "instructions": text}
        out = self._call(self._one(photo, "A candidate photo for the profile owner's Hinge dating profile:"),
                         questions)
        ans = out["answers"]
        return Classification(
            bucket=ans["bucket"]["choice"],
            bucket_probs=ans["bucket"]["probabilities"],
            flags={f: float(ans[f"flag_{f}"]["noul"]) for f in self.cfg.flags},
            tokens=self._tokens(out),
        )
