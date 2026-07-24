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
