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


def _client_with_db(tmp_path: Path):
    from copilot.db import get_engine, get_session, init_db
    from copilot.db.models import Company, Job, SponsorshipStatus, VisaSignal

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE)
    db_path = tmp_path / "test.db"
    engine = get_engine(db_path)
    init_db(engine)
    with get_session(engine) as session:
        stripe = Company(
            name="Stripe",
            sponsorship_status=SponsorshipStatus.sponsors,
            h1b_filing_count=64,
            sponsorship_evidence="64 H1B petitions approved (FY2023)",
        )
        blocked_co = Company(name="NoSponsor Inc")
        session.add_all([stripe, blocked_co])
        session.flush()
        session.add(
            Job(
                company_id=stripe.id,
                title="ML Engineer",
                location="Remote US",
                source="greenhouse",
                apply_url="https://stripe.com/jobs/1",
                dedupe_hash="h1",
                fit_score=78,
                fit_reasoning="Strong skill match on PyTorch and fraud modeling.",
                skill_match_score=80,
                experience_level_score=85,
                domain_fit_score=70,
                location_fit_score=90,
                visa_feasibility_score=65,
                visa_signal=VisaSignal.unknown,
            )
        )
        session.add(
            Job(
                company_id=blocked_co.id,
                title="Backend Engineer",
                location="Austin, TX",
                source="jsearch",
                apply_url="https://example.com/jobs/2",
                dedupe_hash="h2",
                visa_signal=VisaSignal.explicit_no,
            )
        )
        session.commit()
    return TestClient(create_app(profile_path, db_path=db_path))


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


def test_jobs_list_shows_scored_first_and_visa_signals(tmp_path: Path):
    client = _client_with_db(tmp_path)
    resp = client.get("/jobs")
    assert resp.status_code == 200
    # Scored job appears before the unscored one in the rendered order.
    scored_pos = resp.text.index("ML Engineer")
    unscored_pos = resp.text.index("Backend Engineer")
    assert scored_pos < unscored_pos
    assert "78" in resp.text  # fit badge
    assert "No sponsorship (JD)" in resp.text  # explicit_no visa signal
    assert "/jobs/" in resp.text  # links to detail pages


def test_jobs_list_min_fit_filter_keeps_unscored(tmp_path: Path):
    client = _client_with_db(tmp_path)
    resp = client.get("/jobs", params={"min_fit": "50"})
    assert "ML Engineer" in resp.text  # fit 78, passes floor
    assert "Backend Engineer" in resp.text  # unscored, always kept


def test_job_detail_shows_dimension_breakdown_and_sponsorship(tmp_path: Path):
    client = _client_with_db(tmp_path)
    with_db_client = client
    # Look up the scored job's id via the list page link.
    list_resp = with_db_client.get("/jobs")
    start = list_resp.text.index('/jobs/') + len("/jobs/")
    job_id = list_resp.text[start : list_resp.text.index('"', start)]

    resp = with_db_client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert "Fit: 78/100" in resp.text
    assert "Strong skill match on PyTorch" in resp.text
    assert "90/100" in resp.text  # location_fit_score
    assert "sponsors" in resp.text
    assert "64 H1B petitions approved" in resp.text


def test_job_detail_404_for_missing_job(tmp_path: Path):
    client = _client_with_db(tmp_path)
    resp = client.get("/jobs/99999")
    assert resp.status_code == 404


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
