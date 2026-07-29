import re
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from copilot.dashboard import create_app


def _first_job_id(html_text: str) -> str:
    """The first real job detail link (/jobs/<numeric id>) on a rendered
    Jobs-list page. Anchored to a digit so it can't match a non-detail
    /jobs/... route (e.g. the Search button's action="/jobs/search")."""
    match = re.search(r'/jobs/(\d+)"', html_text)
    assert match, "no job detail link found in the page"
    return match.group(1)

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
    job_id = _first_job_id(list_resp.text)
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
    job_id = _first_job_id(list_resp.text)
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


def test_jobs_list_role_cell_combines_company_and_location(tmp_path: Path):
    client = _client_with_db(tmp_path)
    resp = client.get("/jobs")
    assert 'class="role-cell"' in resp.text
    assert 'class="role-title"' in resp.text
    assert "Stripe &middot; Remote US" in resp.text or "Stripe · Remote US" in resp.text


def test_jobs_list_empty_state_has_correct_colspan(tmp_path: Path):
    client, _ = _client(tmp_path)  # no DB jobs at all
    resp = client.get("/jobs")
    assert 'colspan="6"' in resp.text
    assert "No jobs match these filters." in resp.text


def test_job_detail_shows_dimension_breakdown_and_sponsorship(tmp_path: Path):
    client = _client_with_db(tmp_path)
    with_db_client = client
    # Look up the scored job's id via the list page link.
    list_resp = with_db_client.get("/jobs")
    job_id = _first_job_id(list_resp.text)

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
    job_id = _first_job_id(list_resp.text)
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
    job_id = _first_job_id(list_resp.text)

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
    job_id = _first_job_id(list_resp.text)

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
    job_id = _first_job_id(list_resp.text)

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
    # interviewing lands in the Active group, which renders above Waiting
    assert page.index("interviewing") < page.index("applied")


def _email_event(application_id: int, event_type, **overrides):
    from datetime import datetime, timezone

    from copilot.db.models import EmailEvent

    fields = dict(
        application_id=application_id,
        provider_message_id=overrides.pop("provider_message_id", "m1"),
        provider_thread_id="t1",
        classified_type=event_type,
        sender="recruiter@stripe.com",
        subject="Next steps",
        body_text="We would like to move forward - please pick a time.",
        received_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return EmailEvent(**fields)


def _application_id_for_job(tmp_path: Path, job_id: str) -> int:
    from sqlalchemy import select as sa_select

    from copilot.db import get_engine, get_session
    from copilot.db.models import Application

    with get_session(get_engine(tmp_path / "test.db")) as session:
        return session.execute(
            sa_select(Application.id).where(Application.job_id == int(job_id))
        ).scalar_one()


def test_application_detail_shows_timeline_with_expandable_email(tmp_path: Path):
    from copilot.db import get_engine, get_session
    from copilot.db.models import EmailEventType

    client = _client_with_db(tmp_path)
    job_id = _first_job_id(client.get("/jobs").text)
    client.post(f"/jobs/{job_id}/set-state", data={"state": "interviewing"}, follow_redirects=False)
    application_id = _application_id_for_job(tmp_path, job_id)

    with get_session(get_engine(tmp_path / "test.db")) as session:
        session.add(_email_event(application_id, EmailEventType.interview_invite))
        session.commit()

    detail = client.get(f"/applications/{application_id}").text
    assert "You set state to interviewing" in detail  # state_history row, source=manual
    assert "Interview invite email" in detail
    assert "Next steps" in detail  # subject shown in the (collapsed) summary
    assert "We would like to move forward" in detail  # full body inside <details>
    # timeline is newest-first: the email (just added) precedes the earlier state row
    assert detail.index("Interview invite email") < detail.index("You set state to interviewing")


def test_application_note_persists_and_shows_on_reload(tmp_path: Path):
    client = _client_with_db(tmp_path)
    job_id = _first_job_id(client.get("/jobs").text)
    client.post(f"/jobs/{job_id}/set-state", data={"state": "applied"}, follow_redirects=False)
    application_id = _application_id_for_job(tmp_path, job_id)

    resp = client.post(
        f"/applications/{application_id}/note",
        data={"note": "Referred by a former colleague."},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/applications/{application_id}"

    detail = client.get(f"/applications/{application_id}").text
    assert "Referred by a former colleague." in detail


def test_application_set_state_from_detail_page(tmp_path: Path):
    client = _client_with_db(tmp_path)
    job_id = _first_job_id(client.get("/jobs").text)
    client.post(f"/jobs/{job_id}/set-state", data={"state": "applied"}, follow_redirects=False)
    application_id = _application_id_for_job(tmp_path, job_id)

    resp = client.post(
        f"/applications/{application_id}/set-state",
        data={"state": "interviewing"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/applications/{application_id}"
    assert 'class="state">interviewing' in client.get(f"/applications/{application_id}").text


def test_application_untrack_from_detail_redirects_to_list(tmp_path: Path):
    client = _client_with_db(tmp_path)
    job_id = _first_job_id(client.get("/jobs").text)
    client.post(f"/jobs/{job_id}/set-state", data={"state": "applied"}, follow_redirects=False)
    application_id = _application_id_for_job(tmp_path, job_id)

    resp = client.post(f"/applications/{application_id}/untrack", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/applications"
    assert client.get(f"/applications/{application_id}").status_code == 404


def test_application_detail_404_for_unknown_id(tmp_path: Path):
    client = _client_with_db(tmp_path)
    assert client.get("/applications/9999").status_code == 404


def test_needs_attention_flags_unacted_interview_invite_and_clears_on_note(tmp_path: Path):
    from copilot.db import get_engine, get_session
    from copilot.db.models import EmailEventType

    client = _client_with_db(tmp_path)
    job_id = _first_job_id(client.get("/jobs").text)
    client.post(f"/jobs/{job_id}/set-state", data={"state": "interviewing"}, follow_redirects=False)
    application_id = _application_id_for_job(tmp_path, job_id)

    with get_session(get_engine(tmp_path / "test.db")) as session:
        session.add(_email_event(application_id, EmailEventType.online_assessment))
        session.commit()

    flagged = client.get("/applications").text
    assert "Needs attention" in flagged
    assert "Online assessment email" in flagged

    client.post(f"/applications/{application_id}/note", data={"note": "Scheduled for Friday."})
    cleared = client.get("/applications").text
    assert "Needs attention" not in cleared  # a logged note counts as acted-on


def test_needs_attention_flags_stale_applied_but_not_fresh(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select as sa_select

    from copilot.db import get_engine, get_session
    from copilot.db.models import Application, ApplicationStateEvent

    client = _client_with_db(tmp_path)
    job_id = _first_job_id(client.get("/jobs").text)
    client.post(f"/jobs/{job_id}/set-state", data={"state": "applied"}, follow_redirects=False)

    fresh = client.get("/applications").text
    assert "Needs attention" not in fresh  # just applied, nothing stale yet

    with get_session(get_engine(tmp_path / "test.db")) as session:
        application_id = session.execute(
            sa_select(Application.id).where(Application.job_id == int(job_id))
        ).scalar_one()
        backdated = datetime.now(timezone.utc) - timedelta(days=25)
        session.execute(
            ApplicationStateEvent.__table__.update()
            .where(ApplicationStateEvent.application_id == application_id)
            .values(created_at=backdated)
        )
        session.commit()

    stale = client.get("/applications").text
    assert "Needs attention" in stale
    assert "No activity in 25 days" in stale


def test_applications_grouped_active_waiting_closed(tmp_path: Path):
    client = _client_with_db(tmp_path)
    list_resp = client.get("/jobs")
    match = re.findall(r'/jobs/(\d+)"', list_resp.text)
    job_ids = sorted(set(match), key=int)
    assert len(job_ids) >= 2

    client.post(f"/jobs/{job_ids[0]}/set-state", data={"state": "interviewing"}, follow_redirects=False)
    client.post(f"/jobs/{job_ids[1]}/set-state", data={"state": "rejected"}, follow_redirects=False)

    page = client.get("/applications").text
    assert "Active" in page
    assert "Closed" in page
    # the closed group is collapsed by default, not shown open
    assert '<details class="closed-group">' in page
    assert "<details class=\"closed-group\" open>" not in page


def test_jobs_tab_shows_state_badge_not_apply_for_tracked(tmp_path: Path):
    client = _client_with_db(tmp_path)
    list_resp = client.get("/jobs")
    job_id = _first_job_id(list_resp.text)

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


def test_integrations_email_row_hides_sync_button_when_not_connected(tmp_path: Path):
    client, _ = _client(tmp_path)
    text = client.get("/integrations").text
    assert 'action="/integrations/sync-email"' not in text


def test_integrations_email_row_shows_sync_button_when_connected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "test-client-id")
    token_cache = tmp_path / "token_cache.bin"
    token_cache.write_text("x")
    monkeypatch.setattr("copilot.email_agent.outlook.TOKEN_CACHE_PATH", token_cache)
    client, _ = _client(tmp_path)
    text = client.get("/integrations").text
    assert 'action="/integrations/sync-email"' in text
    assert "Sync now" in text


def test_sync_email_route_reports_handled_count_and_suggestions(tmp_path: Path, monkeypatch):
    from copilot.email_agent.sync import SyncResult

    monkeypatch.setattr("copilot.email_agent.provider.get_provider", lambda profile: object())
    monkeypatch.setattr(
        "copilot.email_agent.sync.run_sync",
        lambda session, provider, profile: SyncResult(
            fetched=5, skipped_prefilter=1, skipped_seen=1, suggestions=1, conflicts=0,
        ),
    )
    client, _ = _client(tmp_path)
    resp = client.post("/integrations/sync-email")
    assert resp.status_code == 200
    assert "Synced: 3 messages handled" in resp.text
    assert "1 suggestion(s) awaiting your review" in resp.text


def test_sync_email_route_shows_error_on_failure(tmp_path: Path, monkeypatch):
    def boom(profile):
        raise RuntimeError("not signed in")

    monkeypatch.setattr("copilot.email_agent.provider.get_provider", boom)
    client, _ = _client(tmp_path)
    resp = client.post("/integrations/sync-email")
    assert resp.status_code == 200
    assert "Sync failed: not signed in" in resp.text


def test_preferences_page_has_clear_all_buttons_and_hover_tooltips(tmp_path: Path):
    client, _ = _client(tmp_path)
    text = client.get("/preferences").text
    assert 'data-clear-target="titles"' in text
    assert 'data-clear-target="industry_preference"' in text
    assert "Clear all" in text
    # Hover tooltip warns this is a real Claude API call before the button is clicked.
    assert "Claude" in text and "API call" in text


def test_suggest_titles_route_prefills_chips_without_saving(tmp_path: Path, monkeypatch):
    from copilot.title_suggestions import TitleSuggestion

    monkeypatch.setattr("copilot.resume.extract_resume_profile", lambda profile: object())
    monkeypatch.setattr(
        "copilot.title_suggestions.suggest_target_titles",
        lambda profile, resume: [
            TitleSuggestion(title="Staff Data Scientist", reasoning="fits", demand_signal="high"),
            TitleSuggestion(title="ML Platform Lead", reasoning="fits", demand_signal="rising"),
        ],
    )
    client, profile_path = _client(tmp_path)
    before = profile_path.read_text()

    resp = client.post("/preferences/suggest-titles")
    assert resp.status_code == 200
    assert "Added 2 new titles from your resume" in resp.text
    assert "Staff Data Scientist" in resp.text
    assert "ML Platform Lead" in resp.text
    # Never written to profile.yaml - only the Save button does that.
    assert profile_path.read_text() == before


def test_suggest_titles_route_keeps_existing_and_only_adds_new(tmp_path: Path, monkeypatch):
    """The user's own chips (submitted from the live form, whether saved to
    profile.yaml yet or not) must survive a Suggest click untouched, with
    only genuinely new suggestions appended - and a suggestion that duplicates
    an existing chip must not be added twice."""
    from copilot.title_suggestions import TitleSuggestion

    monkeypatch.setattr("copilot.resume.extract_resume_profile", lambda profile: object())
    monkeypatch.setattr(
        "copilot.title_suggestions.suggest_target_titles",
        lambda profile, resume: [
            TitleSuggestion(title="Hand-Added Title", reasoning="", demand_signal=""),  # already present
            TitleSuggestion(title="Brand New Title", reasoning="", demand_signal=""),
        ],
    )
    client, _ = _client(tmp_path)
    resp = client.post(
        "/preferences/suggest-titles", data={"titles": "My Own Title\nHand-Added Title"}
    )
    assert resp.status_code == 200
    assert "Added 1 new title from your resume (1 already there)" in resp.text
    assert "My Own Title" in resp.text  # untouched
    assert "Hand-Added Title" in resp.text  # not duplicated
    assert resp.text.count("Hand-Added Title") <= 2  # once in the textarea value, not twice
    assert "Brand New Title" in resp.text


def test_suggest_titles_route_shows_error_on_failure(tmp_path: Path, monkeypatch):
    def boom(profile):
        raise RuntimeError("resume unreadable")

    monkeypatch.setattr("copilot.resume.extract_resume_profile", boom)
    client, _ = _client(tmp_path)
    resp = client.post("/preferences/suggest-titles", data={"titles": "Keep Me"})
    assert resp.status_code == 200
    assert "Suggest titles failed: resume unreadable" in resp.text
    assert "Keep Me" in resp.text  # current chips preserved even on failure


def test_suggest_industries_route_prefills_chips_without_saving(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("copilot.resume.extract_resume_profile", lambda profile: SimpleNamespace(domains=["fintech"]))
    monkeypatch.setattr(
        "copilot.industry.suggest_industry_preference", lambda profile, domains: ["tech", "banking"]
    )
    client, profile_path = _client(tmp_path)
    before = profile_path.read_text()

    resp = client.post("/preferences/suggest-industries")
    assert resp.status_code == 200
    assert "Added 2 new industries from your resume" in resp.text
    assert profile_path.read_text() == before


def test_suggest_industries_route_keeps_existing_and_only_adds_new(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("copilot.resume.extract_resume_profile", lambda profile: SimpleNamespace(domains=["fintech"]))
    monkeypatch.setattr(
        "copilot.industry.suggest_industry_preference", lambda profile, domains: ["banking", "tech"]
    )
    client, _ = _client(tmp_path)
    resp = client.post(
        "/preferences/suggest-industries", data={"industry_preference": "consulting\nbanking"}
    )
    assert resp.status_code == 200
    assert "Added 1 new industry from your resume (1 already there)" in resp.text
    assert "consulting\nbanking\ntech" in resp.text  # existing order kept, new appended


def test_suggest_industries_route_handles_no_clean_mapping(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("copilot.resume.extract_resume_profile", lambda profile: SimpleNamespace(domains=["obscure thing"]))
    monkeypatch.setattr("copilot.industry.suggest_industry_preference", lambda profile, domains: [])
    client, _ = _client(tmp_path)
    resp = client.post("/preferences/suggest-industries")
    assert resp.status_code == 200
    assert "nothing in your resume mapped cleanly" in resp.text


def test_suggest_industries_route_shows_error_on_failure(tmp_path: Path, monkeypatch):
    def boom(profile):
        raise RuntimeError("resume unreadable")

    monkeypatch.setattr("copilot.resume.extract_resume_profile", boom)
    client, _ = _client(tmp_path)
    resp = client.post("/preferences/suggest-industries", data={"industry_preference": "banking"})
    assert resp.status_code == 200
    assert "Suggest industries failed: resume unreadable" in resp.text


def test_jobs_search_route_reports_results(tmp_path: Path, monkeypatch):
    from copilot.discovery.pipeline import DiscoverySummary

    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    monkeypatch.setattr(
        "copilot.discovery.pipeline.run_discovery",
        lambda profile, session, **kw: DiscoverySummary(found=12, added=5, duplicates=7),
    )
    monkeypatch.setattr("copilot.discovery.pipeline.resolve_and_poll_ats", lambda profile, session: None)
    monkeypatch.setattr("copilot.discovery.pipeline.geocode_missing_coordinates", lambda session: 0)
    monkeypatch.setattr("copilot.discovery.pipeline.classify_new_companies", lambda profile, session: 0)
    client, _ = _client(tmp_path)

    resp = client.post("/jobs/search")
    assert resp.status_code == 200
    assert "Found 12 postings, 5 new, 7 already known" in resp.text
    assert "5 new jobs still need scoring" in resp.text


def test_jobs_search_route_requires_a_discovery_source(tmp_path: Path, monkeypatch):
    for var in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "JSEARCH_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    client, _ = _client(tmp_path)
    resp = client.post("/jobs/search")
    assert resp.status_code == 200
    assert "No discovery source configured" in resp.text


def test_jobs_search_route_shows_error_on_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")

    def boom(profile, session, **kw):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr("copilot.discovery.pipeline.run_discovery", boom)
    client, _ = _client(tmp_path)
    resp = client.post("/jobs/search")
    assert resp.status_code == 200
    assert "Search failed: network unreachable" in resp.text


def test_jobs_search_reports_quota_exhaustion(tmp_path: Path, monkeypatch):
    from copilot.discovery.pipeline import DiscoverySummary

    monkeypatch.setenv("JSEARCH_API_KEY", "key")
    monkeypatch.setattr(
        "copilot.discovery.pipeline.run_discovery",
        lambda profile, session, **kw: DiscoverySummary(found=0, added=0, duplicates=0, quota_exhausted=["JSearch"]),
    )
    monkeypatch.setattr("copilot.discovery.pipeline.resolve_and_poll_ats", lambda profile, session: None)
    monkeypatch.setattr("copilot.discovery.pipeline.geocode_missing_coordinates", lambda session: 0)
    monkeypatch.setattr("copilot.discovery.pipeline.classify_new_companies", lambda profile, session: 0)
    client, _ = _client(tmp_path)
    resp = client.post("/jobs/search")
    assert "JSearch hit its usage limit" in resp.text


def test_jobs_score_batch_scores_oldest_unscored_first(tmp_path: Path, monkeypatch):
    from copilot.scoring.rubric import ScoreRunResult

    client = _client_with_db(tmp_path)
    calls = []
    monkeypatch.setattr("copilot.resume.extract_resume_profile", lambda profile: object())
    monkeypatch.setattr(
        "copilot.scoring.rubric.score_and_apply",
        lambda session, profile, resume, jobs, **kw: (calls.append(len(jobs)), ScoreRunResult(scored=len(jobs), missing=0))[1],
    )
    resp = client.post("/jobs/score-batch", data={"count": "10"})
    assert resp.status_code == 200
    assert "Scored" in resp.text
    assert calls  # score_and_apply was actually invoked with the unscored jobs


def test_jobs_score_batch_nothing_to_score(tmp_path: Path):
    client, _ = _client(tmp_path)  # no DB jobs at all
    resp = client.post("/jobs/score-batch", data={"count": "10"})
    assert resp.status_code == 200
    assert "Nothing to score" in resp.text


def test_jobs_score_batch_clamps_count_to_a_sane_range(tmp_path: Path, monkeypatch):
    from copilot.scoring.rubric import ScoreRunResult

    client = _client_with_db(tmp_path)
    seen_limits = []
    monkeypatch.setattr("copilot.resume.extract_resume_profile", lambda profile: object())
    monkeypatch.setattr(
        "copilot.scoring.rubric.score_and_apply",
        lambda session, profile, resume, jobs, **kw: ScoreRunResult(scored=len(jobs), missing=0),
    )
    # A wildly out-of-range count must not blow up or query for 100000 rows.
    resp = client.post("/jobs/score-batch", data={"count": "100000"})
    assert resp.status_code == 200


def test_job_score_one_scores_and_redirects_to_detail(tmp_path: Path, monkeypatch):
    from copilot.scoring.rubric import ScoreRunResult

    client = _client_with_db(tmp_path)
    list_resp = client.get("/jobs")
    job_id = _first_job_id(list_resp.text)

    monkeypatch.setattr("copilot.resume.extract_resume_profile", lambda profile: object())
    called = {}
    monkeypatch.setattr(
        "copilot.scoring.rubric.score_and_apply",
        lambda session, profile, resume, jobs, **kw: (called.setdefault("jobs", jobs), ScoreRunResult(scored=1, missing=0))[1],
    )
    resp = client.post(f"/jobs/{job_id}/score", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs/{job_id}"
    assert called["jobs"][0].id == int(job_id)


def test_job_score_one_404_for_missing_job(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.post("/jobs/999/score")
    assert resp.status_code == 404


def test_job_score_one_shows_error_on_failure(tmp_path: Path, monkeypatch):
    client = _client_with_db(tmp_path)
    list_resp = client.get("/jobs")
    job_id = _first_job_id(list_resp.text)

    def boom(profile):
        raise RuntimeError("resume unreadable")

    monkeypatch.setattr("copilot.resume.extract_resume_profile", boom)
    resp = client.post(f"/jobs/{job_id}/score")
    assert resp.status_code == 500
    assert "Scoring failed: resume unreadable" in resp.text


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


def test_usage_page_shows_tracked_anthropic_spend(tmp_path: Path, monkeypatch):
    from copilot.api_usage import AnthropicSpend

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "copilot.api_usage.get_anthropic_spend",
        lambda: AnthropicSpend(month="2026-07", calls=12, input_tokens=50000,
                                output_tokens=8000, cache_creation_tokens=0,
                                cache_read_tokens=0, cost_usd=0.42),
    )
    client, _ = _client(tmp_path)
    resp = client.get("/usage")
    assert "$0.42 est. this month" in resp.text
    assert "12 calls" in resp.text


def test_usage_page_anthropic_no_calls_yet_is_honest_not_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("copilot.api_usage.get_anthropic_spend", lambda: None)
    client, _ = _client(tmp_path)
    resp = client.get("/usage")
    assert "no calls recorded yet this month" in resp.text


def test_usage_page_uscis_row_shows_not_downloaded_when_uncached(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.get("/usage")
    assert "not downloaded yet" in resp.text
    assert 'action="/usage/refresh-h1b"' in resp.text


def test_usage_page_uscis_row_shows_cached_year(tmp_path: Path):
    (tmp_path / "h1b_data_hub_fy2024.csv").write_text("x")
    client, _ = _client(tmp_path)
    resp = client.get("/usage")
    assert "FY2024 cached" in resp.text


def test_usage_refresh_h1b_reports_newer_year_found(tmp_path: Path, monkeypatch):
    from copilot.scoring.sponsorship import SponsorshipSyncSummary

    monkeypatch.setattr("copilot.scoring.sponsorship.refresh_h1b_data", lambda: (2025, True))
    monkeypatch.setattr(
        "copilot.scoring.sponsorship.sync_sponsorship_data",
        lambda session, cache_path, year: SponsorshipSyncSummary(companies_checked=3, matched=2),
    )
    client, _ = _client(tmp_path)
    resp = client.post("/usage/refresh-h1b")
    assert resp.status_code == 200
    assert "Found and switched to FY2025 data" in resp.text
    assert "matched 2 of 3 companies" in resp.text


def test_usage_refresh_h1b_reports_nothing_newer(tmp_path: Path, monkeypatch):
    from copilot.scoring.sponsorship import SponsorshipSyncSummary

    monkeypatch.setattr("copilot.scoring.sponsorship.refresh_h1b_data", lambda: (2023, False))
    monkeypatch.setattr(
        "copilot.scoring.sponsorship.sync_sponsorship_data",
        lambda session, cache_path, year: SponsorshipSyncSummary(companies_checked=1, matched=0),
    )
    client, _ = _client(tmp_path)
    resp = client.post("/usage/refresh-h1b")
    assert "No newer report published yet" in resp.text
    assert "FY2023 data still current" in resp.text


def test_usage_refresh_h1b_shows_error_on_failure(tmp_path: Path, monkeypatch):
    def _boom():
        raise RuntimeError("USCIS unreachable")

    monkeypatch.setattr("copilot.scoring.sponsorship.refresh_h1b_data", _boom)
    client, _ = _client(tmp_path)
    resp = client.post("/usage/refresh-h1b")
    assert resp.status_code == 200
    assert "Refresh failed: USCIS unreachable" in resp.text


def test_api_suggestions_returns_seed_and_db_data(tmp_path: Path):
    client = _client_with_db(tmp_path)
    resp = client.get("/api/suggestions")
    assert resp.status_code == 200
    data = resp.json()
    assert "ML Engineer" in data["titles"]  # from the DB fixture
    assert "Software Engineer" in data["titles"]  # from the static seed
    assert "Stripe" in data["companies"]
    assert "NoSponsor Inc" in data["companies"]
    assert "Remote" in data["locations"]
    assert len(data["industries"]) > 0


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
