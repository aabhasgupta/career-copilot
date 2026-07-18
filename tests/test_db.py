from datetime import datetime, timezone
from pathlib import Path

from copilot.db import (
    Application,
    Company,
    EmailEvent,
    Job,
    get_engine,
    get_session,
    init_db,
)
from copilot.db.models import ApplicationState, EmailEventType, SponsorshipStatus, VisaSignal


def make_engine(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    return engine


def test_full_pipeline_row_lifecycle(tmp_path: Path):
    engine = make_engine(tmp_path)
    with get_session(engine) as session:
        company = Company(name="Acme", sponsorship_status=SponsorshipStatus.transfers_h1b)
        job = Job(
            company=company,
            title="Backend Engineer",
            source="adzuna",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
            dedupe_hash="abc123",
            visa_signal=VisaSignal.unknown,
        )
        application = Application(job=job, state=ApplicationState.applied)
        event = EmailEvent(
            application=application,
            provider_thread_id="thread-1",
            classified_type=EmailEventType.interview_invite,
            received_at=datetime.now(timezone.utc),
        )
        session.add_all([company, job, application, event])
        session.commit()

        loaded = session.query(Job).one()
        assert loaded.company.name == "Acme"
        assert loaded.application.state == ApplicationState.applied
        assert loaded.application.email_events[0].classified_type == (
            EmailEventType.interview_invite
        )


def test_unknowns_are_defaults(tmp_path: Path):
    engine = make_engine(tmp_path)
    with get_session(engine) as session:
        company = Company(name="Mystery Co")
        job = Job(
            company=company,
            title="Engineer",
            source="adzuna",
            apply_url="https://example.com/apply",
            dedupe_hash="def456",
        )
        session.add(job)
        session.commit()

        loaded = session.query(Job).one()
        assert loaded.visa_signal == VisaSignal.unknown
        assert loaded.salary_source.value == "unknown"
        assert loaded.company.sponsorship_status == SponsorshipStatus.unknown
