from pathlib import Path

from fastapi.testclient import TestClient

from copilot.dashboard import create_app

PROFILE = """\
identity:
  full_name: Test User
  email: test@example.com
resume_path: data/resume.pdf
search:
  # Keep this comment
  titles:
  - Software Engineer
  min_salary: 150000
  industry_preference:
  - fintech
visa:
  needs_sponsorship: true
  status: h1b_transfer
email_integration:
  provider: outlook
  address: test@hotmail.com
"""


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    path = tmp_path / "profile.yaml"
    path.write_text(PROFILE)
    return TestClient(create_app(path)), path


def test_index_renders_current_values(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Software Engineer" in resp.text
    assert 'value="150000"' in resp.text
    assert "fintech" in resp.text


def test_save_updates_yaml_and_preserves_comments(tmp_path: Path):
    client, path = _client(tmp_path)
    resp = client.post(
        "/save",
        data={
            "titles": "LLM Engineer\nAI Engineer",
            "locations": "United States",
            "min_salary": "160000",
            "dealbreakers": "clearance required",
            "location_preference": "remote\nChicago, IL",
            "industry_preference": "banking\nfintech",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    text = path.read_text()
    assert "# Keep this comment" in text
    assert "LLM Engineer" in text
    assert "160000" in text

    from copilot.config import load_profile

    profile = load_profile(path)
    assert profile.search.titles == ["LLM Engineer", "AI Engineer"]
    assert profile.search.min_salary == 160000
    assert profile.search.industry_preference == ["banking", "fintech"]


def test_save_rejects_invalid_and_writes_nothing(tmp_path: Path):
    client, path = _client(tmp_path)
    before = path.read_text()
    resp = client.post(
        "/save",
        data={
            "titles": "",  # empty titles violates min_length=1
            "locations": "United States",
            "min_salary": "",
            "dealbreakers": "",
            "location_preference": "",
            "industry_preference": "",
        },
    )
    assert "Not saved" in resp.text
    assert path.read_text() == before
