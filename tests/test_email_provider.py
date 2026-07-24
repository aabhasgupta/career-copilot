from pathlib import Path

import pytest

from copilot.config import Profile
from copilot.email_agent.provider import get_provider


def _profile(provider: str) -> Profile:
    return Profile.model_validate(
        {
            "identity": {"full_name": "X", "email": "x@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {"titles": ["Engineer"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": provider, "address": "x@hotmail.com"},
        }
    )


def test_gmail_not_yet_implemented():
    with pytest.raises(NotImplementedError, match="gmail"):
        get_provider(_profile("gmail"))


def test_outlook_requires_client_id(monkeypatch):
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_CLIENT_ID"):
        get_provider(_profile("outlook"))


def test_outlook_provider_constructed_with_client_id(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "fake-client-id")
    provider = get_provider(_profile("outlook"))
    from copilot.email_agent.outlook import OutlookProvider

    assert isinstance(provider, OutlookProvider)
    assert provider.client_id == "fake-client-id"


def test_token_cache_round_trip(tmp_path: Path):
    from copilot.email_agent.outlook import _load_cache, _save_cache

    cache_path = tmp_path / "token.json"
    cache = _load_cache(cache_path)
    assert cache.has_state_changed is False

    # Simulate MSAL writing new state to the cache.
    cache.add({"response": {"token_type": "Bearer"}, "scope": ["Mail.Send"], "client_id": "x"})
    _save_cache(cache, cache_path)
    assert cache_path.exists()

    reloaded = _load_cache(cache_path)
    assert reloaded.serialize() == cache.serialize()
