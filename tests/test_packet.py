import json
from pathlib import Path

from copilot.config import Profile
from copilot.packet import build_base_packet, write_base_packet
from copilot.resume import ResumeProfile, WorkExperience, _cache_key


def _profile(tmp_path: Path, **visa_overrides) -> Profile:
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("resume content")
    visa = {"needs_sponsorship": True, "status": "h1b_transfer", **visa_overrides}
    return Profile.model_validate(
        {
            "identity": {
                "full_name": "Jane Doe",
                "email": "jane@example.com",
                "phone": "555-1234",
                "links": {"linkedin": "linkedin.com/in/janedoe"},
            },
            "resume_path": str(resume_path),
            "search": {"titles": ["Engineer"]},
            "visa": visa,
            "email_integration": {"provider": "outlook", "address": "jane@hotmail.com"},
        }
    )


def _seed_resume_cache(profile: Profile, cache_path: Path) -> None:
    resume = ResumeProfile(
        full_name="Jane Doe",
        summary="Experienced engineer.",
        seniority="senior",
        years_of_experience=8.0,
        skills=["Python", "SQL"],
        domains=["fintech"],
        work_experience=[WorkExperience(title="Engineer", company="Acme")],
        education=[],
    )
    key = _cache_key(profile.resume_path, profile.llm.model)
    cache_path.write_text(json.dumps({"cache_key": key, "profile": resume.model_dump()}))


def test_build_base_packet_includes_contact_and_resume_summary(tmp_path: Path):
    profile = _profile(tmp_path)
    cache_path = tmp_path / "resume_cache.json"
    _seed_resume_cache(profile, cache_path)

    content = build_base_packet(profile, cache_path=cache_path)
    assert "Jane Doe" in content
    assert "jane@example.com" in content
    assert "linkedin.com/in/janedoe" in content
    assert "Experienced engineer." in content
    assert "Python, SQL" in content
    assert "fintech" in content


def test_h1b_transfer_work_authorization_wording(tmp_path: Path):
    profile = _profile(tmp_path, status="h1b_transfer", needs_sponsorship=True)
    cache_path = tmp_path / "resume_cache.json"
    _seed_resume_cache(profile, cache_path)
    content = build_base_packet(profile, cache_path=cache_path)
    assert "H-1B transfer petition" in content


def test_no_sponsorship_needed_work_authorization_wording(tmp_path: Path):
    profile = _profile(tmp_path, status="other", needs_sponsorship=False)
    cache_path = tmp_path / "resume_cache.json"
    _seed_resume_cache(profile, cache_path)
    content = build_base_packet(profile, cache_path=cache_path)
    assert "without needing employer sponsorship" in content


def test_write_base_packet_writes_file(tmp_path: Path):
    profile = _profile(tmp_path)
    cache_path = tmp_path / "resume_cache.json"
    _seed_resume_cache(profile, cache_path)
    out_path = tmp_path / "packet_base.md"

    write_base_packet(profile, path=out_path, cache_path=cache_path)
    assert out_path.exists()
    assert "Jane Doe" in out_path.read_text()
