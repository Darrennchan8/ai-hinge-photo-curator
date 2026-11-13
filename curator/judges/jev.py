"""TypeSafe Jev: typed decisions with a probability for every option. Image input is experimental."""

import base64
import time
import uuid

import httpx

from ..config import Config
from ..images import judge_file
from .base import (REFERENCE_TEXT, Classification, Photo, Verdict, classify_instructions,
                   compare_instructions, reference_path)

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}


class JevJudge:
    name = "jev"

    def __init__(self, cfg: Config):
        key = cfg.secret("TYPESAFE_API_KEY")
        if not key:
            raise SystemExit("Add TYPESAFE_API_KEY=... to .env (see .env.example) to use Jev.")
        self.cfg = cfg
        self.model = cfg["jev"]["model"]
        self.side = cfg["judge"]["image_side"]
        self.ref = reference_path(cfg)
        self.http = httpx.Client(
            base_url=cfg["jev"]["base_url"],
            timeout=httpx.Timeout(90.0, connect=15.0),
            headers={"Authorization": f"Bearer {key}", "TypeSafe-Danger-Zone-Images": "true"},
        )

    def _asset(self, path, crop=None) -> tuple[str, dict]:
        # A fresh key per image per request, as TypeSafe recommends; it is matched as plain text.
        key = f"img_{uuid.uuid4().hex}"
        data = base64.b64encode(judge_file(self.cfg.path("judge"), path, self.side, crop).read_bytes()).decode()
        return key, {"type": "image", "format": "jpeg", "data_base64": data}

    def _reference(self, *photos: Photo) -> tuple[str, dict]:
        """Reference text and asset when any photo is a group shot or the owner wasn't located."""
        if self.ref is None or not any(p.needs_ref for p in photos):
            return "", {}
        key, asset = self._asset(self.ref)
        return f"{REFERENCE_TEXT} `{key}`\n\n", {key: asset}

    def _call(self, body: dict) -> dict:
        for attempt in range(6):
            try:
                resp = self.http.post("/v1/systemone", json=body)
            except httpx.TransportError:
                if attempt == 5:
                    raise
            else:
                if resp.status_code not in RETRY_STATUS:
                    if resp.is_error:
                        raise RuntimeError(f"Jev {resp.status_code}: {resp.text[:500]}")
                    return resp.json()
                if attempt == 5:
                    resp.raise_for_status()
            time.sleep(min(60, 2 ** attempt))
        raise AssertionError("unreachable")

    def compare(self, first: Photo, second: Photo) -> Verdict:
        ka, asset_a = self._asset(first.path, first.crop)
        kb, asset_b = self._asset(second.path, second.crop)
        ref_text, ref_asset = self._reference(first, second)
        state = ref_text + (
            "Two photos of the same person, the profile owner, being considered for their Hinge "
            f"dating profile.\n\nPhoto A: `{ka}`\nIn photo A, {first.hint}.\n\n"
            f"Photo B: `{kb}`\nIn photo B, {second.hint}."
        )
        body = {
            "model": self.model,
            "state": state,
            "assets": {ka: asset_a, kb: asset_b, **ref_asset},
            "questions": {"better": {
                "type": "choice",
                "instructions": compare_instructions(self.cfg),
                "criteria": {
                    "A": "Photo A would make the stronger Hinge profile photo",
                    "B": "Photo B would make the stronger Hinge profile photo",
                },
            }},
        }
        out = self._call(body)
        ans = out["answers"]["better"]
        probs = ans["probabilities"]
        p_a = probs["A"] / (probs["A"] + probs["B"])
        usage = out.get("usage", {})
        return Verdict(p_a, ans.get("confidence"), usage.get("input_tokens", 0), usage.get("output_tokens", 0))

    def yes_no(self, photo: Photo, statement: str) -> tuple[float, int]:
        key, asset = self._asset(photo.path, photo.crop)
        ref_text, ref_asset = self._reference(photo)
        state = ref_text + f"A photo of the profile owner: `{key}`\nIn this photo, {photo.hint}."
        out = self._call({"model": self.model, "state": state, "assets": {key: asset, **ref_asset},
                          "questions": {"answer": {"type": "noul", "instructions": statement}}})
        return float(out["answers"]["answer"]["noul"]), out.get("usage", {}).get("input_tokens", 0)

    def choose(self, photo: Photo, questions: dict[str, tuple[str, dict[str, str]]]) -> tuple[dict, int]:
        key, asset = self._asset(photo.path, photo.crop)
        ref_text, ref_asset = self._reference(photo)
        state = ref_text + f"A photo of the profile owner: `{key}`\nIn this photo, {photo.hint}."
        out = self._call({"model": self.model, "state": state, "assets": {key: asset, **ref_asset},
                          "questions": {name: {"type": "choice", "instructions": text, "criteria": options}
                                        for name, (text, options) in questions.items()}})
        answers = {name: {"choice": out["answers"][name]["choice"],
                          "probabilities": out["answers"][name]["probabilities"]} for name in questions}
        return answers, out.get("usage", {}).get("input_tokens", 0)

    def classify(self, photo: Photo) -> Classification:
        key, asset = self._asset(photo.path, photo.crop)
        ref_text, ref_asset = self._reference(photo)
        state = ref_text + f"A candidate photo for the profile owner's Hinge dating profile: `{key}`\nIn this photo, {photo.hint}."
        questions = {"bucket": {"type": "choice", "instructions": classify_instructions(),
                                "criteria": self.cfg.buckets}}
        for flag, text in self.cfg.flags.items():
            questions[f"flag_{flag}"] = {"type": "noul", "instructions": text}
        out = self._call({"model": self.model, "state": state, "assets": {key: asset, **ref_asset},
                           "questions": questions})
        ans = out["answers"]
        return Classification(
            bucket=ans["bucket"]["choice"],
            bucket_probs=ans["bucket"]["probabilities"],
            flags={f: float(ans[f"flag_{f}"]["noul"]) for f in self.cfg.flags},
            tokens=out.get("usage", {}).get("input_tokens", 0),
        )
