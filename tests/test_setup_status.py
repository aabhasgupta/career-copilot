from pathlib import Path

from copilot.setup_status import get_setup_status

PROFILE = """\
identity:
  full_name: Test User
  email: test@example.com
resume_path: {resume}
search:
  titles:
  - Software Engineer
visa:
  needs_sponsorship: true
  status: h1b_transfer
email_integration:
  provider: outlook
  address: test@hotmail.com
"""


def _write_profile(tmp_path: Path, resume: Path) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(PROFILE.format(resume=resume))
    return path


def test_missing_profile_is_incomplete(tmp_path: Path):
    status = get_setup_status(tmp_path / "nope.yaml")
    assert not status.required_complete
    profile = status.get("profile")
    assert profile is not None and not profile.ok and profile.required


def test_required_complete_when_profile_resume_and_keys_present(tmp_path: Path, monkeypatch):
    resume = tmp_path / "resume.pdf"
    resume.write_text("pdf")
    profile_path = _write_profile(tmp_path, resume)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")

    status = get_setup_status(profile_path)
    assert status.required_complete
    assert status.get("discovery").ok
    # Email and schedule are optional: whatever their state, they must not be
    # marked required (so they can never block required_complete).
    assert not status.get("email").required
    assert not status.get("schedule").required


def test_missing_resume_blocks(tmp_path: Path, monkeypatch):
    profile_path = _write_profile(tmp_path, tmp_path / "absent.pdf")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("JSEARCH_API_KEY", "j")

    status = get_setup_status(profile_path)
    assert not status.required_complete
    assert not status.get("resume").ok
    # JSearch alone satisfies the discovery requirement.
    assert status.get("discovery").ok


def test_missing_discovery_source_blocks(tmp_path: Path, monkeypatch):
    resume = tmp_path / "resume.pdf"
    resume.write_text("pdf")
    profile_path = _write_profile(tmp_path, resume)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    monkeypatch.delenv("JSEARCH_API_KEY", raising=False)

    status = get_setup_status(profile_path)
    assert not status.required_complete
    assert not status.get("discovery").ok
