import os
from pathlib import Path

import pytest

from copilot.envfile import set_env


def test_set_env_writes_file_and_applies_live(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.delenv("JSEARCH_API_KEY", raising=False)
    set_env("JSEARCH_API_KEY", "abc123", path=env)
    assert "JSEARCH_API_KEY" in env.read_text()
    assert "abc123" in env.read_text()
    # Applied to the running process so status flips without a restart.
    assert os.environ["JSEARCH_API_KEY"] == "abc123"


def test_set_env_preserves_other_lines(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# my keys\nANTHROPIC_API_KEY=keep-me\n")
    monkeypatch.delenv("JSEARCH_API_KEY", raising=False)
    set_env("JSEARCH_API_KEY", "new", path=env)
    text = env.read_text()
    assert "keep-me" in text  # existing key untouched
    assert "# my keys" in text  # comment preserved
    assert "JSEARCH_API_KEY=new" in text.replace("'", "").replace('"', "")


def test_set_env_rejects_unknown_key(tmp_path: Path):
    with pytest.raises(ValueError):
        set_env("SECRET_BACKDOOR", "x", path=tmp_path / ".env")


def test_set_env_ignores_blank(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    set_env("ADZUNA_APP_ID", "   ", path=env)
    assert not env.exists()  # nothing written for a blank value
    assert "ADZUNA_APP_ID" not in os.environ
