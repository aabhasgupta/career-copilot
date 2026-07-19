from pathlib import Path

from ruamel.yaml import YAML

from copilot.profile_fill import fill_identity
from copilot.resume import ContactInfo, ResumeProfile

EXAMPLE_PROFILE = """\
# a helpful comment that must survive
identity:
  full_name: Jane Doe
  email: jane@example.com
  phone: "+1 555 000 0000"
  location: San Francisco, CA
  links:
    linkedin: https://linkedin.com/in/janedoe
    github: null
    portfolio: null

search:
  titles: [Software Engineer]
"""


def write_profile(tmp_path: Path) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(EXAMPLE_PROFILE)
    return path


def make_resume(**contact_overrides) -> ResumeProfile:
    return ResumeProfile(
        full_name="Real Person",
        contact=ContactInfo(
            email="real@person.com",
            phone="+1 999 999 9999",
            location="Austin, TX",
            linkedin_url=None,
            github_url="https://github.com/realperson",
            portfolio_url=None,
            **contact_overrides,
        ),
        summary="s",
        seniority="senior",
        years_of_experience=5,
        skills=[],
        domains=[],
        work_experience=[],
        education=[],
    )


def test_fills_only_stated_fields(tmp_path: Path):
    path = write_profile(tmp_path)
    changed = fill_identity(path, make_resume())

    assert set(changed) == {"full_name", "email", "phone", "location", "github"}

    yaml = YAML()
    result = yaml.load(path.read_text())
    assert result["identity"]["full_name"] == "Real Person"
    assert result["identity"]["email"] == "real@person.com"
    # linkedin already had a value and the resume didn't state one - untouched
    assert result["identity"]["links"]["linkedin"] == "https://linkedin.com/in/janedoe"
    assert result["identity"]["links"]["github"] == "https://github.com/realperson"


def test_preserves_comments_and_untouched_sections(tmp_path: Path):
    path = write_profile(tmp_path)
    fill_identity(path, make_resume())

    content = path.read_text()
    assert "# a helpful comment that must survive" in content
    assert "titles: [Software Engineer]" in content or "Software Engineer" in content


def test_idempotent_on_second_run(tmp_path: Path):
    path = write_profile(tmp_path)
    fill_identity(path, make_resume())
    second_changed = fill_identity(path, make_resume())
    assert second_changed == []


def test_no_change_when_resume_states_nothing_new(tmp_path: Path):
    path = write_profile(tmp_path)
    resume = ResumeProfile(
        full_name="Jane Doe",
        contact=ContactInfo(),
        summary="s",
        seniority="senior",
        years_of_experience=5,
        skills=[],
        domains=[],
        work_experience=[],
        education=[],
    )
    changed = fill_identity(path, resume)
    assert changed == []
