"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    """Alert-path tests must not pop real macOS notifications (or shell out
    at all) - stub the osascript subprocess while keeping the code path
    exercised. Tests that care about the call re-patch and inspect."""
    monkeypatch.setattr(
        "copilot.notify.alerts.subprocess.run", lambda *a, **k: None
    )


@pytest.fixture(autouse=True)
def _isolate_real_data_files(tmp_path, monkeypatch):
    """Several modules default to writing the real project's data/.env files
    when no explicit path is passed (api_usage.py's USAGE_PATH, envfile.py's
    ENV_PATH) - by design, since that's what the running app should do. But
    a test that mocks the Anthropic/JSearch client without also mocking the
    LLM call's response.usage (e.g. a SimpleNamespace with no .usage
    attribute) still exercises the real llm.py -> api_usage.py write path,
    silently polluting the developer's real data/api_usage.json on every
    test run (found live: test_title_suggestions.py wrote 8 phantom
    zero-token Anthropic call records into the real file this way).
    Redirect both defaults to a per-test tmp path so no test - existing or
    future - can touch real project files just by exercising a code path
    that happens to hit one of these writers without explicitly opting in.

    sponsorship.py's DATA_DIR is read-only here (current_cached_year() just
    globs for cached USCIS CSVs) but gets the same treatment for the same
    reason: a test shouldn't see - or have its outcome depend on - whatever
    happens to be cached in the developer's real data/ directory."""
    monkeypatch.setattr("copilot.api_usage.USAGE_PATH", tmp_path / "api_usage.json")
    monkeypatch.setattr("copilot.envfile.ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr("copilot.scoring.sponsorship.DATA_DIR", tmp_path)
