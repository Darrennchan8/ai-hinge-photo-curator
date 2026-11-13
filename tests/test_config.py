"""Overrides merge onto the base settings, and keys come from the environment or .env."""

from curator import config


def test_overrides_merge_table_by_table(tmp_path):
    over = tmp_path / "over.toml"
    over.write_text('[final]\njudge = "claude"\n[persona]\naudience = "someone"\n')
    base, cfg = config.load(None), config.load(over)
    assert cfg["final"]["judge"] == "claude" and cfg["persona"]["audience"] == "someone"
    assert cfg["final"]["per_bucket"] == base["final"]["per_bucket"]  # untouched keys keep the base value
    assert cfg["buckets"] == base["buckets"]


def test_merge_replaces_lists_rather_than_appending():
    assert config.merge({"a": {"x": [1, 2], "y": 1}}, {"a": {"x": [3]}}) == {"a": {"x": [3], "y": 1}}


def test_env_file_and_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# keys\nexport TYPESAFE_API_KEY='from-file'\nOPENAI_API_KEY=\n\nBROKEN LINE\n")
    cfg = config.Config({}, tmp_path, config.read_env_file(env))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert cfg.secret("TYPESAFE_API_KEY") == "from-file"
    assert cfg.secret("OPENAI_API_KEY") is None  # empty means unset
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-env")
    assert cfg.secret("TYPESAFE_API_KEY") == "from-env"
