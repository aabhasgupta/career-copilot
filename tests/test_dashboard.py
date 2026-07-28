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
    resp = client.get("/preferences")
    assert resp.status_code == 200
    assert "Software Engineer" in resp.text
    assert 'value="150000"' in resp.text
    assert "fintech" in resp.text


def _complete_status():
    from copilot.setup_status import Check, SetupStatus

    return SetupStatus(checks=[
        Check("profile", "Profile & preferences", True, True),
        Check("resume", "Resume", True, True),
        Check("anthropic", "Anthropic API key", True, True),
        Check("discovery", "Job discovery source", True, True),
        Check("email", "Email (inbox sync)", False, False),
    ])


def _incomplete_status():
    from copilot.setup_status import Check, SetupStatus

    return SetupStatus(checks=[
        Check("profile", "Profile & preferences", False, True, "profile.yaml not found"),
        Check("resume", "Resume", False, True),
        Check("anthropic", "Anthropic API key", False, True),
        Check("discovery", "Job discovery source", False, True),
        Check("email", "Email (inbox sync)", False, False),
    ])


def test_home_landing_when_not_set_up(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path)
    monkeypatch.setattr(
        "copilot.setup_status.get_setup_status", lambda *a, **k: _incomplete_status()
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert "What Career Copilot does" in resp.text  # explainer/landing
    assert "Finish setup" in resp.text
    assert "required" in resp.text  # a required item is flagged


def test_home_getting_started_when_set_up_but_empty(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path)
    monkeypatch.setattr(
        "copilot.setup_status.get_setup_status", lambda *a, **k: _complete_status()
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert "copilot discover" in resp.text  # guidance for the empty-but-ready state
    assert "What Career Copilot does" in resp.text  # explainer still available


def test_home_surfaces_active_application_and_pipeline(tmp_path: Path, monkeypatch):
    client = _client_with_db(tmp_path)
    monkeypatch.setattr(
        "copilot.setup_status.get_setup_status", lambda *a, **k: _complete_status()
    )
    list_resp = client.get("/jobs")
    start = list_resp.text.index("/jobs/") + len("/jobs/")
    job_id = list_resp.text[start : list_resp.text.index('"', start)]
    client.post(f"/jobs/{job_id}/set-state", data={"state": "interviewing"}, follow_redirects=False)

    home = client.get("/").text
    assert "Needs attention" in home
    assert "Stripe" in home  # the interviewing application is surfaced
    assert 'class="pill"' in home  # pipeline snapshot rendered
    assert "all caught up" not in home  # something needs attention now


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
    assert "No sponsor (JD)" in resp.text  # explicit_no visa signal
    assert "/jobs/" in resp.text  # links to detail pages


def test_jobs_hide_tracked_filters_out_applications(tmp_path: Path):
    client = _client_with_db(tmp_path)
    list_resp = client.get("/jobs")
    start = list_resp.text.index("/jobs/") + len("/jobs/")
    job_id = list_resp.text[start : list_resp.text.index('"', start)]
    client.post(f"/jobs/{job_id}/set-state", data={"state": "applied"}, follow_redirects=False)

    # Default view still shows the tracked job...
    assert "ML Engineer" in client.get("/jobs").text
    # ...but hide_tracked drops it while keeping untracked listings.
    hidden = client.get("/jobs", params={"hide_tracked": "true"}).text
    assert "ML Engineer" not in hidden
    assert "Backend Engineer" in hidden


def test_jobs_sort_newest_orders_by_posting_date(tmp_path: Path):
    client = _client_with_ages(tmp_path)
    page = client.get("/jobs", params={"sort": "new", "max_age": ""}).text
    assert page.index("Fresh Job") < page.index("Stale Job") < page.index("Unknown Age Job")


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


def test_job_detail_renders_cleaned_jd_and_chips(tmp_path: Path):
    from copilot.db import get_engine, get_session, init_db
    from copilot.db.models import Company, Job

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE)
    db_path = tmp_path / "test.db"
    engine = get_engine(db_path)
    init_db(engine)
    with get_session(engine) as session:
        co = Company(name="Acme", industry="fintech")
        session.add(co)
        session.flush()
        job = Job(
            company_id=co.id,
            title="Data Engineer",
            source="greenhouse",
            apply_url="https://acme.com/1",
            dedupe_hash="jd1",
            remote=True,
            seniority_level="senior",
            employment_type="full-time",
            jd_text="<p>Build <b>pipelines</b>.</p><ul><li>Python</li></ul>",
        )
        session.add(job)
        session.flush()
        job_id = job.id
        session.commit()
    client = TestClient(
        create_app(profile_path, db_path=db_path, packet_base_path=tmp_path / "p.md")
    )
    page = client.get(f"/jobs/{job_id}").text
    assert "Job description" in page
    assert "Build pipelines" in page  # tags stripped
    assert "<b>pipelines</b>" not in page  # raw HTML not leaked
    assert "Remote" in page  # meta chip
    assert "senior" in page  # seniority chip


def test_job_detail_jd_fallback_when_absent(tmp_path: Path):
    client = _client_with_db(tmp_path)  # jobs here have no jd_text
    list_resp = client.get("/jobs")
    start = list_resp.text.index("/jobs/") + len("/jobs/")
    job_id = list_resp.text[start : list_resp.text.index('"', start)]
    assert "No description stored" in client.get(f"/jobs/{job_id}").text


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
    resp = client.get("/preferences")
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


def test_job_set_state_any_direction(tmp_path: Path):
    client = _client_with_db(tmp_path)
    list_resp = client.get("/jobs")
    start = list_resp.text.index("/jobs/") + len("/jobs/")
    job_id = list_resp.text[start : list_resp.text.index('"', start)]

    resp = client.post(
        f"/jobs/{job_id}/set-state", data={"state": "interviewing"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert 'class="state">interviewing' in client.get(f"/jobs/{job_id}").text

    # Backward on purpose: the manual override can undo a wrong state.
    client.post(f"/jobs/{job_id}/set-state", data={"state": "applied"}, follow_redirects=False)
    assert 'class="state">applied' in client.get(f"/jobs/{job_id}").text

    assert client.post(
        f"/jobs/{job_id}/set-state", data={"state": "bogus"}
    ).status_code == 400
    assert client.post(
        "/jobs/99999/set-state", data={"state": "applied"}
    ).status_code == 404


def test_job_untrack_button_and_route(tmp_path: Path):
    client = _client_with_db(tmp_path)
    list_resp = client.get("/jobs")
    start = list_resp.text.index("/jobs/") + len("/jobs/")
    job_id = list_resp.text[start : list_resp.text.index('"', start)]

    untrack_action = f'action="/jobs/{job_id}/untrack"'
    # Untracked jobs offer no Untrack button (nothing to remove).
    assert untrack_action not in client.get(f"/jobs/{job_id}").text

    client.post(f"/jobs/{job_id}/queue", follow_redirects=False)
    assert untrack_action in client.get(f"/jobs/{job_id}").text

    resp = client.post(f"/jobs/{job_id}/untrack", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/jobs"
    detail = client.get(f"/jobs/{job_id}")
    assert 'class="state">found' in detail.text  # back to an untracked listing
    assert untrack_action not in detail.text

    assert client.post(f"/jobs/{job_id}/untrack").status_code == 404  # nothing left


def test_applications_tab_lists_tracked_by_activity(tmp_path: Path):
    from sqlalchemy import select as sa_select

    from copilot.db import get_engine, get_session
    from copilot.db.models import Job

    client = _client_with_db(tmp_path)
    empty = client.get("/applications")
    assert empty.status_code == 200
    assert "Nothing tracked yet" in empty.text

    with get_session(get_engine(tmp_path / "test.db")) as session:
        ids = [j for (j,) in session.execute(sa_select(Job.id).order_by(Job.id)).all()]

    client.post(f"/jobs/{ids[0]}/set-state", data={"state": "applied"}, follow_redirects=False)
    client.post(
        f"/jobs/{ids[1]}/set-state", data={"state": "interviewing"}, follow_redirects=False
    )

    page = client.get("/applications").text
    assert "Applications" in page
    # interviewing (state_order 0) must appear before applied (state_order 3)
    assert page.index("interviewing") < page.index("applied")


def test_jobs_tab_shows_state_badge_not_apply_for_tracked(tmp_path: Path):
    client = _client_with_db(tmp_path)
    list_resp = client.get("/jobs")
    start = list_resp.text.index("/jobs/") + len("/jobs/")
    job_id = list_resp.text[start : list_resp.text.index('"', start)]

    client.post(f"/jobs/{job_id}/set-state", data={"state": "interviewing"}, follow_redirects=False)
    page = client.get("/jobs").text
    # The tracked row shows its state as a badge instead of an Apply link.
    assert '<span class="state' in page
    assert "interviewing" in page


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


def test_suggestion_confirm_with_job_link(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    from datetime import datetime, timezone

    from copilot.db import get_engine, get_session, init_db
    from copilot.db.models import (
        Application,
        ApplicationState,
        Company,
        EmailEventType,
        Job,
        Suggestion,
        SuggestionKind,
    )

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE)
    db_path = tmp_path / "test.db"
    engine = get_engine(db_path)
    init_db(engine)
    with get_session(engine) as session:
        company = Company(name="Stripe")
        session.add(company)
        session.flush()
        job = Job(
            company_id=company.id,
            title="ML Engineer",
            source="greenhouse",
            apply_url="https://stripe.com/jobs/1",
            dedupe_hash="h1",
        )
        session.add(job)
        session.flush()
        job_id = job.id
        session.add(
            Suggestion(
                kind=SuggestionKind.untracked_application,
                provider_message_id="m1",
                sender="recruiter@stripe.com",
                subject="Your interview",
                body_text="Let's schedule.",
                received_at=datetime.now(timezone.utc),
                proposed_state=ApplicationState.interviewing,
                classified_type=EmailEventType.interview_invite,
            )
        )
        session.commit()
    client = TestClient(
        create_app(profile_path, db_path=db_path, packet_base_path=tmp_path / "packet.md")
    )

    assert 'name="job_id"' in client.get("/suggestions").text  # link field offered
    resp = client.post(
        "/suggestions/1/confirm", data={"job_id": str(job_id)}, follow_redirects=False
    )
    assert resp.status_code == 303

    with get_session(engine) as session:
        assert session.query(Company).count() == 1  # linked, not duplicated
        application = session.query(Application).one()
        assert application.job_id == job_id
        assert application.state == ApplicationState.interviewing


def test_repeat_confirm_is_harmless_no_op(tmp_path: Path, monkeypatch):
    """A double-click or browser re-POST of Confirm must redirect quietly and
    create nothing twice (live failure 2026-07-26: the repeat request raced
    the first and 500'd on the jobs dedupe unique constraint)."""
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    from datetime import datetime, timezone

    from copilot.db import get_engine, get_session, init_db
    from copilot.db.models import (
        Application,
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
        session.add(
            Suggestion(
                kind=SuggestionKind.untracked_application,
                provider_message_id="m1",
                sender="careers@acme.com",
                subject="Thanks for applying to Acme",
                body_text="...",
                received_at=datetime.now(timezone.utc),
                company_name="Acme",
                job_title="Engineer",
                proposed_state=ApplicationState.applied,
                classified_type=EmailEventType.confirmation,
            )
        )
        session.commit()
    client = TestClient(
        create_app(profile_path, db_path=db_path, packet_base_path=tmp_path / "packet.md")
    )

    first = client.post("/suggestions/1/confirm", follow_redirects=False)
    repeat = client.post("/suggestions/1/confirm", follow_redirects=False)
    repeat_decline = client.post("/suggestions/1/decline", follow_redirects=False)

    assert first.status_code == 303
    assert repeat.status_code == 303  # no-op redirect, not an error page
    assert repeat_decline.status_code == 303
    with get_session(engine) as session:
        assert session.query(Application).count() == 1  # created exactly once


def test_link_dropdown_ordered_by_likelihood_with_new_first(tmp_path: Path):
    """A suggestion whose evidence names Syndigo must list Syndigo above
    Stripe (alphabetical would put Stripe first - so this proves scoring),
    and 'Track as a new application' stays the pinned default."""
    from datetime import datetime, timezone

    from copilot.db import get_engine, get_session, init_db
    from copilot.db.models import (
        Application,
        ApplicationState,
        Company,
        EmailEventType,
        Job,
        Suggestion,
        SuggestionKind,
    )

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE)
    db_path = tmp_path / "test.db"
    engine = get_engine(db_path)
    init_db(engine)
    with get_session(engine) as session:
        for i, name in enumerate(["Stripe", "Syndigo"], start=1):
            company = Company(name=name)
            session.add(company)
            session.flush()
            job = Job(
                company_id=company.id,
                title="Engineer",
                source="adzuna",
                apply_url=f"https://example.com/{i}",
                dedupe_hash=f"h{i}",
            )
            session.add(job)
            session.flush()
            session.add(Application(job_id=job.id, state=ApplicationState.applied))
        session.add(
            Suggestion(
                kind=SuggestionKind.untracked_application,
                provider_message_id="m1",
                sender="no-reply@talenthub.example",
                subject="Your Syndigo candidacy update",
                body_text="...",
                received_at=datetime.now(timezone.utc),
                classified_type=EmailEventType.other,
            )
        )
        session.commit()
    client = TestClient(
        create_app(profile_path, db_path=db_path, packet_base_path=tmp_path / "packet.md")
    )

    page = client.get("/suggestions").text
    new_option = page.index("Track as a new application")
    syndigo = page.index("Syndigo - Engineer")
    stripe = page.index("Stripe - Engineer")
    assert new_option < syndigo < stripe  # pinned default, then by likelihood


def test_suggestions_404_for_missing_or_resolved(tmp_path: Path):
    client, _ = _client(tmp_path)
    assert client.post("/suggestions/99/confirm").status_code == 404
    assert client.post("/suggestions/99/decline").status_code == 404


def test_integrations_page_lists_setup_checklist(tmp_path: Path, monkeypatch):
    from copilot.schedule.launchd import ScheduleStatus

    monkeypatch.setattr(
        "copilot.schedule.launchd.status",
        lambda: ScheduleStatus(installed=False, loaded=False, detail="not installed"),
    )
    client, _ = _client(tmp_path)
    resp = client.get("/integrations")
    assert resp.status_code == 200
    # Every setup check surfaces as a compact row, grouped and labeled.
    for label in (
        "Profile", "Resume", "Anthropic API key",
        "Job discovery source", "Email (inbox sync)", "Hourly background sync",
    ):
        assert label in resp.text
    # Compact-row status dots render, and a health summary counts what's configured.
    assert 'class="rdot' in resp.text
    assert "configured" in resp.text


def test_integrations_schedule_controls_reflect_state(tmp_path: Path, monkeypatch):
    from copilot.schedule.launchd import ScheduleStatus

    client, _ = _client(tmp_path)

    monkeypatch.setattr(
        "copilot.schedule.launchd.status",
        lambda: ScheduleStatus(installed=False, loaded=False, detail="not installed"),
    )
    assert 'action="/schedule/install"' in client.get("/integrations").text

    monkeypatch.setattr(
        "copilot.schedule.launchd.status",
        lambda: ScheduleStatus(installed=True, loaded=True, detail="running hourly"),
    )
    assert 'action="/schedule/pause"' in client.get("/integrations").text

    monkeypatch.setattr(
        "copilot.schedule.launchd.status",
        lambda: ScheduleStatus(installed=True, loaded=False, detail="installed but paused"),
    )
    assert 'action="/schedule/resume"' in client.get("/integrations").text


def test_preferences_no_longer_hosts_schedule_controls(tmp_path: Path):
    """Schedule/inbox-sync moved to Integrations; Preferences just links there."""
    client, _ = _client(tmp_path)
    resp = client.get("/preferences")
    assert 'action="/schedule/' not in resp.text
    assert 'href="/integrations"' in resp.text


def test_schedule_routes_call_launchd_and_redirect_to_integrations(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr("copilot.schedule.launchd.install", lambda project_dir: calls.append("install"))
    monkeypatch.setattr("copilot.schedule.launchd.pause", lambda: calls.append("pause"))
    monkeypatch.setattr("copilot.schedule.launchd.resume", lambda: calls.append("resume"))
    client, _ = _client(tmp_path)

    for route in ("install", "pause", "resume"):
        resp = client.post(f"/schedule/{route}", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/integrations"
    assert calls == ["install", "pause", "resume"]


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


def test_integrations_rows_are_editable_controls(tmp_path: Path):
    """The config rows expose inline forms - the page is a control panel, not
    just a status board."""
    client, _ = _client(tmp_path)
    text = client.get("/integrations").text
    assert 'action="/config/resume"' in text and 'type="file"' in text
    assert 'action="/config/env"' in text
    for field in ("ANTHROPIC_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY",
                  "JSEARCH_API_KEY", "AZURE_CLIENT_ID"):
        assert f'name="{field}"' in text


def test_config_env_writes_submitted_keys_and_redirects(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr("copilot.envfile.set_env", lambda k, v: calls.append((k, v)))
    client, _ = _client(tmp_path)
    resp = client.post(
        "/config/env",
        data={"ANTHROPIC_API_KEY": "sk-ant-xyz", "JSEARCH_API_KEY": "js-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/integrations"
    assert calls == [("ANTHROPIC_API_KEY", "sk-ant-xyz"), ("JSEARCH_API_KEY", "js-1")]


def test_config_env_nothing_submitted_is_not_an_error(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr("copilot.envfile.set_env", lambda k, v: calls.append(k))
    client, _ = _client(tmp_path)
    resp = client.post("/config/env", data={})
    assert resp.status_code == 200
    assert "Nothing entered" in resp.text
    assert calls == []


def _client_with_resume_path(tmp_path: Path, resume: Path):
    path = tmp_path / "profile.yaml"
    profile = PROFILE.replace("resume_path: data/resume.pdf", f"resume_path: {resume}")
    path.write_text(profile)
    app = create_app(path, db_path=tmp_path / "test.db", packet_base_path=tmp_path / "packet_base.md")
    return TestClient(app)


def test_config_resume_replaces_file_when_extension_matches(tmp_path: Path):
    resume = tmp_path / "resume.pdf"
    client = _client_with_resume_path(tmp_path, resume)
    resp = client.post(
        "/config/resume",
        files={"file": ("updated.pdf", b"%PDF-1.4 new bytes", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resume.read_bytes() == b"%PDF-1.4 new bytes"


def test_config_resume_rejects_extension_mismatch(tmp_path: Path):
    resume = tmp_path / "resume.pdf"
    client = _client_with_resume_path(tmp_path, resume)
    resp = client.post(
        "/config/resume",
        files={"file": ("resume.docx", b"docx bytes", "application/octet-stream")},
    )
    assert resp.status_code == 200
    assert "expects" in resp.text  # friendly guidance, not a crash
    assert not resume.exists()  # nothing written on mismatch


def test_usage_page_lists_metered_and_free_apis(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.get("/usage")
    assert resp.status_code == 200
    for label in (
        "Anthropic API", "JSearch (RapidAPI)", "Adzuna", "Microsoft Graph",
        "Greenhouse", "Remotive", "RemoteOK", "Nominatim", "USCIS",
    ):
        assert label in resp.text
    assert 'class="rdot' in resp.text


def test_usage_page_shows_real_jsearch_quota_when_captured(tmp_path: Path, monkeypatch):
    from copilot.api_usage import Quota

    monkeypatch.setenv("JSEARCH_API_KEY", "test-key")
    monkeypatch.setattr(
        "copilot.api_usage.get_jsearch_quota",
        lambda: Quota(limit=200, remaining=58, reset_at=None, captured_at=None),
    )
    client, _ = _client(tmp_path)
    resp = client.get("/usage")
    assert "58 of 200 left" in resp.text


def test_usage_page_shows_not_connected_without_keys(tmp_path: Path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "JSEARCH_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        monkeypatch.delenv(var, raising=False)
    client, _ = _client(tmp_path)
    resp = client.get("/usage")
    assert "not connected" in resp.text


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
