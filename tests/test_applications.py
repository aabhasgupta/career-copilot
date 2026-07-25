from pathlib import Path

from copilot.applications import get_or_create_application, mark_applied, queue
from copilot.db import get_engine, get_session, init_db
from copilot.db.models import ApplicationState, Company, Job


def _session(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    return get_session(engine)


def _job(session) -> int:
    company = Company(name="Acme")
    session.add(company)
    session.flush()
    job = Job(
        company_id=company.id,
        title="Engineer",
        source="adzuna",
        apply_url="https://example.com",
        dedupe_hash="h",
    )
    session.add(job)
    session.commit()
    return job.id


def test_queue_creates_application_in_queued_state(tmp_path: Path):
    with _session(tmp_path) as session:
        job_id = _job(session)
        application = queue(session, job_id)
        assert application.state == ApplicationState.queued


def test_queue_never_downgrades_further_along_state(tmp_path: Path):
    with _session(tmp_path) as session:
        job_id = _job(session)
        mark_applied(session, job_id)
        application = queue(session, job_id)
        assert application.state == ApplicationState.applied


def test_mark_applied_sets_state_and_timestamp(tmp_path: Path):
    with _session(tmp_path) as session:
        job_id = _job(session)
        application = mark_applied(session, job_id)
        assert application.state == ApplicationState.applied
        assert application.applied_at is not None


def test_get_or_create_application_reuses_existing_row(tmp_path: Path):
    with _session(tmp_path) as session:
        job_id = _job(session)
        first = get_or_create_application(session, job_id)
        second = get_or_create_application(session, job_id)
        assert first.id == second.id


def test_queue_and_mark_applied_record_manual_state_history(tmp_path: Path):
    with _session(tmp_path) as session:
        job_id = _job(session)
        application = queue(session, job_id)
        mark_applied(session, job_id)
        session.refresh(application)
        transitions = [(e.from_state, e.to_state, e.source) for e in application.state_history]
        assert transitions == [
            (ApplicationState.found, ApplicationState.queued, "manual"),
            (ApplicationState.queued, ApplicationState.applied, "manual"),
        ]


def test_mark_applied_twice_does_not_duplicate_history(tmp_path: Path):
    with _session(tmp_path) as session:
        job_id = _job(session)
        application = mark_applied(session, job_id)
        mark_applied(session, job_id)
        session.refresh(application)
        assert len(application.state_history) == 1


def test_set_state_moves_any_direction_with_manual_history(tmp_path: Path):
    from copilot.applications import set_state
    from copilot.db.models import ApplicationStateEvent

    with _session(tmp_path) as session:
        job_id = _job(session)
        set_state(session, job_id, ApplicationState.interviewing)
        # Backward on purpose - undoing a wrong state is exactly what the
        # escape hatch is for.
        application = set_state(session, job_id, ApplicationState.applied)

        assert application.state == ApplicationState.applied
        assert application.applied_at is not None
        history = session.query(ApplicationStateEvent).order_by(ApplicationStateEvent.id).all()
        assert [(h.to_state, h.source) for h in history] == [
            (ApplicationState.interviewing, "manual"),
            (ApplicationState.applied, "manual"),
        ]


def test_set_state_same_state_writes_no_history(tmp_path: Path):
    from copilot.applications import set_state
    from copilot.db.models import ApplicationStateEvent

    with _session(tmp_path) as session:
        job_id = _job(session)
        set_state(session, job_id, ApplicationState.replied)
        set_state(session, job_id, ApplicationState.replied)
        assert session.query(ApplicationStateEvent).count() == 1


def _email_event(application_id: int):
    from datetime import datetime, timezone

    from copilot.db.models import EmailEvent, EmailEventType

    return EmailEvent(
        application_id=application_id,
        provider_message_id="m1",
        provider_thread_id="t1",
        classified_type=EmailEventType.confirmation,
        received_at=datetime.now(timezone.utc),
    )


def test_untrack_removes_tracking_but_keeps_discovered_job(tmp_path: Path):
    from copilot.applications import untrack
    from copilot.db.models import Application, ApplicationStateEvent, EmailEvent

    with _session(tmp_path) as session:
        job_id = _job(session)  # source="adzuna" - a discovery find
        application = mark_applied(session, job_id)
        session.add(_email_event(application.id))
        session.commit()

        summary = untrack(session, job_id)
        session.commit()

        assert "Engineer" in summary
        assert session.query(Application).count() == 0
        assert session.query(ApplicationStateEvent).count() == 0
        assert session.query(EmailEvent).count() == 0
        assert session.query(Job).count() == 1  # the listing itself survives


def test_untrack_email_created_job_removes_job_and_stub_company(tmp_path: Path):
    from copilot.applications import untrack
    from copilot.db.models import Application

    with _session(tmp_path) as session:
        company = Company(name="Acme Robotics")
        session.add(company)
        session.flush()
        job = Job(
            company_id=company.id,
            title="Data Engineer",
            source="email",  # exists only because a suggestion was confirmed
            apply_url="",
            dedupe_hash="e1",
        )
        session.add(job)
        session.commit()
        mark_applied(session, job.id)
        session.commit()

        summary = untrack(session, job.id)
        session.commit()

        assert "Acme Robotics" in summary
        assert session.query(Application).count() == 0
        assert session.query(Job).count() == 0
        assert session.query(Company).count() == 0  # stub vanishes too


def test_untrack_keeps_company_that_has_other_jobs(tmp_path: Path):
    from copilot.applications import untrack

    with _session(tmp_path) as session:
        job_id = _job(session)  # Acme, adzuna job
        company_id = session.get(Job, job_id).company_id
        email_job = Job(
            company_id=company_id,
            title="Other Role",
            source="email",
            apply_url="",
            dedupe_hash="e2",
        )
        session.add(email_job)
        session.commit()
        mark_applied(session, email_job.id)
        session.commit()

        untrack(session, email_job.id)
        session.commit()

        assert session.query(Company).count() == 1  # still owns the adzuna job
        assert session.query(Job).count() == 1


def test_untrack_detaches_suggestions_and_closes_pending_conflicts(tmp_path: Path):
    from datetime import datetime, timezone

    from copilot.applications import untrack
    from copilot.db.models import Suggestion, SuggestionKind, SuggestionStatus

    with _session(tmp_path) as session:
        job_id = _job(session)
        application = mark_applied(session, job_id)
        session.add(
            Suggestion(
                kind=SuggestionKind.state_conflict,
                provider_message_id="m1",
                application_id=application.id,
                received_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

        untrack(session, job_id)
        session.commit()

        suggestion = session.query(Suggestion).one()  # kept: idempotency record
        assert suggestion.application_id is None
        assert suggestion.status == SuggestionStatus.declined  # nothing left to decide


def test_untrack_raises_for_missing_or_untracked(tmp_path: Path):
    import pytest

    from copilot.applications import untrack

    with _session(tmp_path) as session:
        job_id = _job(session)  # exists but never tracked
        with pytest.raises(LookupError):
            untrack(session, job_id)
        with pytest.raises(LookupError):
            untrack(session, 999)
