"""Cache behavior of resume extraction, with the API call stubbed out."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from copilot.config import Profile
from copilot.resume import ResumeProfile, extract_resume_profile

FAKE_PROFILE = ResumeProfile(
    full_name="Test User",
    summary="A test engineer.",
    seniority="senior",
    years_of_experience=6,
    skills=["python"],
    domains=["testing"],
    work_experience=[],
    education=[],
)


def make_profile(tmp_path: Path) -> Profile:
    resume = tmp_path / "resume.txt"
    resume.write_text("Test User. Senior engineer, 6 years of Python.")
    return Profile.model_validate(
        {
            "identity": {"full_name": "Test User", "email": "t@example.com"},
            "resume_path": str(resume),
            "search": {"titles": ["Engineer"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "t@hotmail.com"},
        }
    )


def test_cache_hit_skips_api(tmp_path: Path):
    profile = make_profile(tmp_path)
    cache = tmp_path / "cache.json"

    with patch("copilot.resume.Anthropic") as mock_client:
        mock_client.return_value.messages.parse.return_value.parsed_output = FAKE_PROFILE
        first = extract_resume_profile(profile, cache_path=cache)
        second = extract_resume_profile(profile, cache_path=cache)

    assert first == second == FAKE_PROFILE
    assert mock_client.return_value.messages.parse.call_count == 1
    assert json.loads(cache.read_text())["profile"]["full_name"] == "Test User"


def test_resume_change_invalidates_cache(tmp_path: Path):
    profile = make_profile(tmp_path)
    cache = tmp_path / "cache.json"

    with patch("copilot.resume.Anthropic") as mock_client:
        mock_client.return_value.messages.parse.return_value.parsed_output = FAKE_PROFILE
        extract_resume_profile(profile, cache_path=cache)
        profile.resume_path.write_text("Updated resume content.")
        extract_resume_profile(profile, cache_path=cache)

    assert mock_client.return_value.messages.parse.call_count == 2


def test_missing_resume_raises(tmp_path: Path):
    profile = make_profile(tmp_path)
    profile.resume_path.unlink()
    with pytest.raises(FileNotFoundError, match="resume_path"):
        extract_resume_profile(profile, cache_path=tmp_path / "cache.json")
