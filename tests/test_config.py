from pathlib import Path

import pytest
from pydantic import ValidationError

from copilot.config import Profile, VisaStatus, load_profile

VALID_PROFILE = """
identity:
  full_name: Test User
  email: test@example.com
resume_path: data/resume.pdf
search:
  titles: [Software Engineer]
visa:
  needs_sponsorship: true
  status: h1b_transfer
email_integration:
  provider: outlook
  address: test@hotmail.com
"""


def test_load_valid_profile(tmp_path: Path):
    path = tmp_path / "profile.yaml"
    path.write_text(VALID_PROFILE)
    profile = load_profile(path)
    assert profile.identity.full_name == "Test User"
    assert profile.visa.status == VisaStatus.h1b_transfer
    assert profile.search.deprioritize_staffing is True
    assert profile.llm.model == "claude-sonnet-5"


def test_missing_profile_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="copilot init"):
        load_profile(tmp_path / "nope.yaml")


def test_invalid_visa_status_rejected():
    with pytest.raises(ValidationError):
        Profile.model_validate(
            {
                "identity": {"full_name": "X", "email": "x@example.com"},
                "resume_path": "data/resume.pdf",
                "search": {"titles": ["Engineer"]},
                "visa": {"needs_sponsorship": True, "status": "green-card-maybe"},
                "email_integration": {"provider": "outlook", "address": "x@hotmail.com"},
            }
        )


def test_empty_titles_rejected():
    with pytest.raises(ValidationError):
        Profile.model_validate(
            {
                "identity": {"full_name": "X", "email": "x@example.com"},
                "resume_path": "data/resume.pdf",
                "search": {"titles": []},
                "visa": {"needs_sponsorship": False, "status": "none"},
                "email_integration": {"provider": "gmail", "address": "x@gmail.com"},
            }
        )
