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
    app = create_app(
        path, db_path=tmp_path / "test.db", packet_base_path=tmp_path / "packet_base.md"
    )
    return TestClient(app), path


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
    return TestClient(create_app(profile_path, db_path=db_path, packet_base_path=tmp_path / "packet_base.md"))


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


def _client_with_ages(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    from copilot.db import get_engine, get_session, init_db
    from copilot.db.models import Company, Job

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE)  # search.max_posting_age_days unset -> config default of 30
    db_path = tmp_path / "test.db"
    engine = get_engine(db_path)
    init_db(engine)
    now = datetime.now(timezone.utc)
    with get_session(engine) as session:
        co = Company(name="Acme")
        session.add(co)
        session.flush()
        session.add_all(
            [
                Job(
                    company_id=co.id,
                    title="Fresh Job",
                    source="adzuna",
                    apply_url="https://example.com/fresh",
                    dedupe_hash="fresh",
                    posted_at=now - timedelta(days=5),
                ),
                Job(
                    company_id=co.id,
                    title="Stale Job",
                    source="adzuna",
                    apply_url="https://example.com/stale",
                    dedupe_hash="stale",
                    posted_at=now - timedelta(days=60),
                ),
                Job(
                    company_id=co.id,
                    title="Unknown Age Job",
                    source="adzuna",
                    apply_url="https://example.com/unknown",
                    dedupe_hash="unknown",
                ),
            ]
        )
        session.commit()
    return TestClient(create_app(profile_path, db_path=db_path, packet_base_path=tmp_path / "packet_base.md"))


def test_jobs_list_applies_profile_age_floor_by_default(tmp_path: Path):
    client = _client_with_ages(tmp_path)
    resp = client.get("/jobs")
    assert "Fresh Job" in resp.text
    assert "Unknown Age Job" in resp.text  # unknown posted_at is never dropped
    assert "Stale Job" not in resp.text  # older than the 30-day default
    assert 'value="30"' in resp.text  # input reflects the applied floor


def test_jobs_list_explicit_max_age_overrides_default(tmp_path: Path):
    client = _client_with_ages(tmp_path)
    resp = client.get("/jobs", params={"max_age": "90"})
    assert "Stale Job" in resp.text


def test_jobs_list_blank_max_age_disables_floor(tmp_path: Path):
    client = _client_with_ages(tmp_path)
    resp = client.get("/jobs", params={"max_age": ""})
    assert "Stale Job" in resp.text


def test_packet_section_shows_not_generated_when_missing(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.get("/")
    assert "Not generated yet." in resp.text


def test_job_queue_and_mark_applied_actions(tmp_path: Path):
    client = _client_with_db(tmp_path)
    list_resp = client.get("/jobs")
    start = list_resp.text.index("/jobs/") + len("/jobs/")
    job_id = list_resp.text[start : list_resp.text.index('"', start)]

    detail = client.get(f"/jobs/{job_id}")
    assert 'class="state">found' in detail.text

    queue_resp = client.post(f"/jobs/{job_id}/queue", follow_redirects=False)
    assert queue_resp.status_code == 303
    detail = client.get(f"/jobs/{job_id}")
    assert 'class="state">queued' in detail.text

    applied_resp = client.post(f"/jobs/{job_id}/mark-applied", follow_redirects=False)
    assert applied_resp.status_code == 303
    detail = client.get(f"/jobs/{job_id}")
    assert 'class="state">applied' in detail.text


def test_job_queue_404_for_missing_job(tmp_path: Path):
    client = _client_with_db(tmp_path)
    resp = client.post("/jobs/99999/queue")
    assert resp.status_code == 404


def test_suggestions_page_empty_state(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.get("/suggestions")
    assert resp.status_code == 200
    assert "Nothing pending" in resp.text


def test_suggestions_confirm_and_decline_flow(tmp_path: Path, monkeypatch):
    # No provider in tests: filing the email is skipped, DB writes still apply.
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    from datetime import datetime, timezone

    from copilot.db import get_engine, get_session, init_db
    from copilot.db.models import (
        ApplicationState,
        EmailEventType,
        Suggestion,
        SuggestionKind,
    )

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE)
    db_path = tmp_path / "test.db"
    engine = get_engine(db_path)
    init_db(engine)
    with get_session(engine) as session:
        for i in (1, 2):
            session.add(
                Suggestion(
                    kind=SuggestionKind.untracked_application,
                    provider_message_id=f"m{i}",
                    sender="careers@acme.com",
                    subject=f"Thanks for applying to Acme {i}",
                    body_text="We received your application.",
                    received_at=datetime.now(timezone.utc),
                    company_name="Acme",
                    job_title=f"Engineer {i}",
                    proposed_state=ApplicationState.applied,
                    classified_type=EmailEventType.confirmation,
                )
            )
        session.commit()
    client = TestClient(
        create_app(profile_path, db_path=db_path, packet_base_path=tmp_path / "packet.md")
    )

    listing = client.get("/suggestions")
    assert "Untracked application" in listing.text
    assert '<span class="badge">2</span>' in listing.text  # nav badge

    assert client.post("/suggestions/1/confirm", follow_redirects=False).status_code == 303
    assert client.post("/suggestions/2/decline", follow_redirects=False).status_code == 303
    assert "Nothing pending" in client.get("/suggestions").text

    with get_session(engine) as session:
        from copilot.db.models import Application, Company

        assert session.query(Company).filter_by(name="Acme").count() == 1
        assert session.query(Application).count() == 1  # only the confirmed one


def test_suggestions_404_for_missing_or_resolved(tmp_path: Path):
    client, _ = _client(tmp_path)
    assert client.post("/suggestions/99/confirm").status_code == 404
    assert client.post("/suggestions/99/decline").status_code == 404


def test_prefs_page_shows_inbox_sync_card(tmp_path: Path, monkeypatch):
    from copilot.schedule.launchd import ScheduleStatus

    monkeypatch.setattr(
        "copilot.schedule.launchd.status",
        lambda: ScheduleStatus(installed=False, loaded=False, detail="not installed"),
    )
    client, _ = _client(tmp_path)
    resp = client.get("/")
    assert "Inbox sync" in resp.text
    assert "not installed" in resp.text
    assert "copilot schedule install" in resp.text  # hint instead of buttons


def test_schedule_pause_and_resume_buttons_reflect_state(tmp_path: Path, monkeypatch):
    from copilot.schedule.launchd import ScheduleStatus

    client, _ = _client(tmp_path)

    monkeypatch.setattr(
        "copilot.schedule.launchd.status",
        lambda: ScheduleStatus(installed=True, loaded=True, detail="running hourly"),
    )
    assert 'action="/schedule/pause"' in client.get("/").text

    monkeypatch.setattr(
        "copilot.schedule.launchd.status",
        lambda: ScheduleStatus(installed=True, loaded=False, detail="installed but paused"),
    )
    assert 'action="/schedule/resume"' in client.get("/").text


def test_schedule_pause_route_calls_launchd_and_redirects(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr("copilot.schedule.launchd.pause", lambda: calls.append("pause"))
    monkeypatch.setattr("copilot.schedule.launchd.resume", lambda: calls.append("resume"))
    client, _ = _client(tmp_path)

    assert client.post("/schedule/pause", follow_redirects=False).status_code == 303
    assert client.post("/schedule/resume", follow_redirects=False).status_code == 303
    assert calls == ["pause", "resume"]


def test_schedule_resume_failure_shows_error_not_500(tmp_path: Path, monkeypatch):
    from copilot.schedule.launchd import ScheduleStatus

    def boom():
        raise RuntimeError("Not installed - run 'copilot schedule install' first.")

    monkeypatch.setattr("copilot.schedule.launchd.resume", boom)
    monkeypatch.setattr(
        "copilot.schedule.launchd.status",
        lambda: ScheduleStatus(installed=False, loaded=False, detail="not installed"),
    )
    client, _ = _client(tmp_path)
    resp = client.post("/schedule/resume")
    assert resp.status_code == 200
    assert "Resume failed" in resp.text


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
