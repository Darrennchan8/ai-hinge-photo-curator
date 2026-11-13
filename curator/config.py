"""Settings: config_base.toml, with your own changes from config_overrides.toml on top. API keys come
from the environment or from a .env file next to them."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "config_base.toml"
OVERRIDES = ROOT / "config_overrides.toml"


def merge(base: dict, over: dict) -> dict:
    """Tables merge key by key; anything else (numbers, strings, lists) in `over` replaces the base value."""
    out = dict(base)
    for key, value in over.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines; blank lines, # comments and a leading `export` are ignored."""
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


@dataclass(frozen=True)
class Config:
    raw: dict
    base: Path = ROOT
    env: dict = field(default_factory=dict, compare=False)
    overrides: Path | None = None  # the overrides file that was applied, if any

    def __getitem__(self, key):
        return self.raw[key]

    def path(self, name: str) -> Path:
        return self.base / Path(self.raw["paths"][name]).expanduser()

    def secret(self, name: str) -> str | None:
        """An API key from the environment, else from .env."""
        return os.environ.get(name) or self.env.get(name) or None

    @property
    def buckets(self) -> dict[str, str]:
        return {b["name"]: b["description"] for b in self.raw["buckets"]}

    @property
    def flags(self) -> dict[str, str]:
        return {**self.raw["flags"]["hard"], **self.raw["flags"]["soft"]}

    @property
    def hard_flags(self) -> set[str]:
        return set(self.raw["flags"]["hard"])


def load(overrides: Path | str | None = OVERRIDES) -> Config:
    """The base settings plus `overrides` (None for the base alone, as the tests use)."""
    with open(BASE, "rb") as f:
        raw = tomllib.load(f)
    applied = None
    if overrides is not None:
        path = Path(overrides)
        if path.is_file():
            with open(path, "rb") as f:
                raw = merge(raw, tomllib.load(f))
            applied = path
        elif path != OVERRIDES:
            raise SystemExit(f"No config file at {path}.")
    return Config(raw, ROOT, read_env_file(ROOT / ".env"), applied)
