"""Offline stand-in judge for testing the pipeline: prefers sharper, brighter, bigger-face photos."""

import random

import cv2

from ..config import Config
from .base import Classification, Photo, Verdict


class FakeJudge:
    name = "fake"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._quality: dict[str, float] = {}

    def _q(self, photo: Photo) -> float:
        if photo.id not in self._quality:
            gray = cv2.imread(str(photo.path), cv2.IMREAD_GRAYSCALE)
            self._quality[photo.id] = cv2.Laplacian(gray, cv2.CV_64F).var() ** 0.5 / 10 + gray.mean() / 64
        return self._quality[photo.id]

    def compare(self, first: Photo, second: Photo) -> Verdict:
        import math
        diff = self._q(first) - self._q(second) + 0.15 + random.gauss(0, 0.3)  # built-in position bias
        return Verdict(1 / (1 + math.exp(-diff)), None, 0)

    def yes_no(self, photo: Photo, statement: str) -> tuple[float, int]:
        return random.Random(photo.id + statement).random(), 0

    def choose(self, photo: Photo, questions: dict[str, tuple[str, dict[str, str]]]) -> tuple[dict, int]:
        rng = random.Random(photo.id)
        return {name: {"choice": rng.choice(list(options)), "probabilities": {}}
                for name, (_, options) in questions.items()}, 0

    def classify(self, photo: Photo) -> Classification:
        rng = random.Random(photo.id)
        bucket = rng.choice(list(self.cfg.buckets))
        return Classification(bucket, {bucket: 1.0}, {f: rng.random() * 0.8 for f in self.cfg.flags}, 0)

    def explain(self, photo: Photo, bucket: str) -> tuple[str, dict | None, tuple[int, int]]:
        return f"(fake) solid {bucket} photo", None, (0, 0)
