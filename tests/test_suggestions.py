from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from copilot.db import get_engine, get_session, init_db
from copilot.db.models import (
    Application,
    ApplicationState,
    ApplicationStateEvent,
    Company,
    EmailEvent,
    EmailEventType,
    Job,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
)
from copilot.suggestions import confirm, decline, list_pending


def _session(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    return get_session(engine)


def _untracked_suggestion(**overrides) -> Suggestion:
    defaults = dict(
        kind=SuggestionKind.untracked_application,
        provider_message_id="m1",
        provider_thread_id="t1",
        sender="careers@acme.com",
        subject="Thanks for applying to Acme",
        body_text="We received your application.",
        received_at=datetime.now(timezone.utc),
        company_name="Acme",
        job_title="Platform Engineer",
        proposed_state=ApplicationState.applied,
        classified_type=EmailEventType.confirmation,
    )
    defaults.update(overrides)
    return Suggestion(**defaults)


def test_confirm_untracked_creates_company_job_application(tmp_path: Path):
    with _session(tmp_path) as session:
        session.add(_untracked_suggestion())
        session.commit()

        suggestion = confirm(session, 1)
        session.commit()

        assert suggestion.status == SuggestionStatus.confirmed
        company = session.query(Company).one()
        assert company.name == "Acme"
        assert company.domain == "acme.com"  # learned from the sender
        job = session.query(Job).one()
        assert job.source == "email"
        application = session.query(Application).one()
        assert application.state == ApplicationState.applied
        event = session.query(EmailEvent).one()
        history = session.query(ApplicationStateEvent).one()
        assert history.source == f"email:{event.id}"


def test_confirm_untracked_never_learns_ats_domain(tmp_path: Path):
    with _session(tmp_path) as session:
        session.add(_untracked_suggestion(sender="no-reply@greenhouse.io"))
        session.commit()
        confirm(session, 1)
        assert session.query(Company).one().domain is None


def test_confirm_untracked_never_learns_freemail_domain(tmp_path: Path):
    """gmail.com identifies a person, not a company - learning it would make
    every gmail sender a correlation candidate (found live, 2026-07-26)."""
    with _session(tmp_path) as session:
        session.add(_untracked_suggestion(sender="recruiter.jane@gmail.com"))
        session.commit()
        confirm(session, 1)
        assert session.query(Company).one().domain is None


def test_confirm_untracked_reuses_existing_company(tmp_path: Path):
    with _session(tmp_path) as session:
        session.add(Company(name="Acme"))
        session.add(_untracked_suggestion())
        session.commit()
        confirm(session, 1)
        assert session.query(Company).count() == 1


def test_confirm_conflict_adopts_email_state(tmp_path: Path):
    with _session(tmp_path) as session:
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
        application = Application(job_id=job.id, state=ApplicationState.rejected)
        session.add(application)
        session.flush()
        session.add(
            _untracked_suggestion(
                kind=SuggestionKind.state_conflict,
                application_id=application.id,
                company_name="Stripe",
                proposed_state=ApplicationState.interviewing,
                classified_type=EmailEventType.interview_invite,
            )
        )
        session.commit()

        confirm(session, 1)
        session.commit()
        assert application.state == ApplicationState.interviewing


def test_decline_changes_nothing_and_is_permanent(tmp_path: Path):
    with _session(tmp_path) as session:
        session.add(_untracked_suggestion())
        session.commit()

        decline(session, 1)
        session.commit()

        assert session.query(Company).count() == 0
        assert session.query(Application).count() == 0
        assert list_pending(session) == []
        with pytest.raises(ValueError):
            confirm(session, 1)  # already resolved


def test_confirm_missing_suggestion_raises(tmp_path: Path):
    with _session(tmp_path) as session:
        with pytest.raises(LookupError):
            confirm(session, 99)


def _tracked_job(session) -> Job:
    company = Company(name="Stripe")
    session.add(company)
    session.flush()
    job = Job(
        company_id=company.id,
        title="ML Engineer",
        source="greenhouse",
        apply_url="https://stripe.com/jobs/1",
        dedupe_hash="stripe-ml",
    )
    session.add(job)
    session.flush()
    return job


def test_confirm_with_job_id_links_instead_of_creating(tmp_path: Path):
    """D28: the human knows which application an email belongs to even when
    the correlator couldn't tell - linking must not mint duplicate records."""
    with _session(tmp_path) as session:
        job = _tracked_job(session)
        session.add(
            _untracked_suggestion(
                sender="recruiter@stripe.com", company_name=None, job_title=None
            )
        )
        session.commit()

        confirm(session, 1, job_id=job.id)
        session.commit()

        assert session.query(Company).count() == 1  # nothing new created
        assert session.query(Job).count() == 1
        application = session.query(Application).one()
        assert application.job_id == job.id
        assert application.state == ApplicationState.applied  # forward from found
        assert session.query(EmailEvent).one().application_id == application.id
        assert job.company.domain == "stripe.com"  # learned from the sender


def test_confirm_link_never_moves_state_backward(tmp_path: Path):
    with _session(tmp_path) as session:
        job = _tracked_job(session)
        session.add(Application(job_id=job.id, state=ApplicationState.interviewing))
        session.add(_untracked_suggestion(sender="recruiter@stripe.com"))
        session.commit()

        confirm(session, 1, job_id=job.id)  # proposes applied - stale news
        session.commit()

        application = session.query(Application).one()
        assert application.state == ApplicationState.interviewing  # untouched
        assert session.query(EmailEvent).count() == 1  # but still recorded


def test_confirm_conflict_as_new_creates_separate_application(tmp_path: Path):
    """D31: a conflicting email can be about a DIFFERENT application at the
    same company - confirming with as_new creates a separate one and leaves
    the flagged (rejected) application untouched."""
    with _session(tmp_path) as session:
        job = _tracked_job(session)  # Stripe / ML Engineer
        rejected = Application(job_id=job.id, state=ApplicationState.rejected)
        session.add(rejected)
        session.flush()
        session.add(
            _untracked_suggestion(
                kind=SuggestionKind.state_conflict,
                application_id=rejected.id,
                company_name="Stripe",
                job_title="Staff Platform Engineer",  # a different role
                proposed_state=ApplicationState.interviewing,
            )
        )
        session.commit()

        confirm(session, 1, as_new=True)
        session.commit()

        assert session.query(Application).count() == 2
        assert session.get(Application, rejected.id).state == ApplicationState.rejected  # untouched
        new_app = session.query(Application).filter(Application.id != rejected.id).one()
        assert new_app.state == ApplicationState.interviewing
        assert session.query(Company).count() == 1  # reused, not duplicated


def test_confirm_conflict_link_to_other_job_leaves_flagged_untouched(tmp_path: Path):
    """A conflict can also be resolved by linking the email to a *different*
    already-tracked application - the flagged one still isn't overwritten."""
    with _session(tmp_path) as session:
        flagged_job = _tracked_job(session)
        flagged = Application(job_id=flagged_job.id, state=ApplicationState.rejected)
        session.add(flagged)
        # A second tracked application the email really belongs to.
        other_company = Company(name="Stripe")  # reuse company is fine
        session.flush()
        other_job = Job(
            company_id=flagged_job.company_id,
            title="Staff Platform Engineer",
            source="adzuna",
            apply_url="https://stripe.com/jobs/2",
            dedupe_hash="stripe-staff",
        )
        session.add(other_job)
        session.flush()
        session.add(Application(job_id=other_job.id, state=ApplicationState.applied))
        session.add(
            _untracked_suggestion(
                kind=SuggestionKind.state_conflict,
                application_id=flagged.id,
                proposed_state=ApplicationState.interviewing,
            )
        )
        session.commit()

        confirm(session, 1, job_id=other_job.id)
        session.commit()

        assert session.get(Application, flagged.id).state == ApplicationState.rejected  # untouched
        assert other_job.application.state == ApplicationState.interviewing  # advanced


def test_confirm_link_missing_job_raises(tmp_path: Path):
    with _session(tmp_path) as session:
        session.add(_untracked_suggestion())
        session.commit()
        with pytest.raises(LookupError):
            confirm(session, 1, job_id=999)
