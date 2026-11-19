from ..config import Config
from .base import Judge


def make(cfg: Config, name: str) -> Judge:
    if name == "jev":
        from .jev import JevJudge
        return JevJudge(cfg)
    if name == "claude":
        from .claude import ClaudeJudge
        return ClaudeJudge(cfg)
    if name == "gemini":
        from .gemini import GeminiJudge
        return GeminiJudge(cfg)
    if name == "gemini_decision":
        from .gemini_decision import GeminiDecisionJudge
        return GeminiDecisionJudge(cfg)
    if name == "openai":
        from .openai import OpenAIJudge
        return OpenAIJudge(cfg)
    if name == "fake":
        from .fake import FakeJudge
        return FakeJudge(cfg)
    raise SystemExit(f"Unknown judge {name!r}; use jev, gemini_decision, claude, gemini, openai, or fake.")
