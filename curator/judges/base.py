import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import Config


@dataclass(frozen=True)
class Photo:
    id: str
    path: Path
    hint: str  # where the profile owner is in the frame
    needs_ref: bool = False  # group or unlocated photo: show the judge a reference of the owner
    crop: tuple[int, int, int, int] | None = None  # (x, y, w, h) zoom on the owner when their face is small


@dataclass
class Verdict:
    p_first: float  # P(first photo is the better profile photo)
    confidence: float | None
    tokens_in: int
    tokens_out: int = 0
    note: str | None = None


@dataclass
class Classification:
    bucket: str
    bucket_probs: dict[str, float]
    flags: dict[str, float]  # flag -> probability it applies
    tokens: int
    tokens_out: int = 0


class Judge(Protocol):
    name: str

    def compare(self, first: Photo, second: Photo) -> Verdict: ...

    def classify(self, photo: Photo) -> Classification: ...

    def yes_no(self, photo: Photo, statement: str) -> tuple[float, int]:
        """P(statement is true of the photo), and input tokens used."""
        ...

    def choose(self, photo: Photo, questions: dict[str, tuple[str, dict[str, str]]]) -> tuple[dict, int]:
        """Answer {name: (instructions, {option: description})} with {name: {choice, probabilities}},
        and input tokens used."""
        ...


def reference_path(cfg: Config) -> Path | None:
    chosen = cfg["judge"].get("reference_image")
    if chosen:
        return cfg.base / chosen
    from ..images import PHOTO_EXTS
    ref_dir = cfg.path("reference")
    refs = sorted(p for p in ref_dir.glob("*") if p.suffix.lower() in PHOTO_EXTS) if ref_dir.exists() else []
    if refs:
        return refs[0]
    auto = cfg.path("auto_reference")  # face crop written by prefilter when no references were given
    return auto if auto.exists() else None


REFERENCE_TEXT = "For reference only (not a candidate), this is what the profile owner looks like:"


def prompt_version(cfg: Config) -> str:
    """Cache key for comparisons. It follows the comparison wording, persona included, so changing either
    starts fresh answers instead of reusing ones given to a different question."""
    return "p-" + hashlib.sha256(compare_instructions(cfg).encode()).hexdigest()[:10]


def ranking_versions(cfg: Config) -> list[str]:
    """Prompt versions whose answers the ranking may use, oldest first: [rank] reuse_prompt_versions, then the
    current one. A pair answered under several keeps its newest answer."""
    current = prompt_version(cfg)
    return [v for v in cfg["rank"].get("reuse_prompt_versions", []) if v != current] + [current]


def compare_instructions(cfg: Config) -> str:
    return (
        f"Imagine you are {cfg['persona']['audience']}, scrolling Hinge, and this person's profile "
        "comes up. Which of the two photos would make a better impression and make you more likely "
        "to like the profile? Judge the photo, not the activity in it. What matters: the profile "
        "owner's face is clearly visible and flattering; the expression is genuine and relaxed; "
        "lighting, focus and image quality are good; the angle and composition flatter them; the "
        "photo shows some personality or lifestyle; and the profile owner is clearly the focus."
    )


def explain_instructions(cfg: Config, bucket: str, hint: str) -> tuple[str, str]:
    """The text before and after the photo when asking for a rationale."""
    return (f"This photo ranked near the top of the '{bucket}' photos for the profile owner's Hinge dating profile:",
            f"In this photo, {hint}. In one sentence, say what makes it work for {cfg['persona']['audience']} and "
            "anything that would make it stronger. If cropping would help (e.g. to make the profile owner the clear "
            "focus), give the crop.")


def classify_instructions() -> str:
    return "Which kind of dating-profile photo is this, judged by how the profile owner appears in it?"
