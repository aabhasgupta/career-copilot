"""Confirm/decline logic for inbox suggestions, shared by the CLI and the
dashboard so both surfaces behave identically (same factoring reason as
applications.py and ranking.rank_jobs - D19/D25).

Confirming is the moment Copilot is finally allowed to act on what the email
implied (D20/D24): create the stub records for an untracked application, or
apply the conflicting state the human just sided with. Declining marks the
suggestion resolved and touches nothing else - permanently, since sync's
idempotency check skips any message that already has a suggestion row.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot.applications import advance_from_email, get_or_create_application, state_relation
from copilot.db.models import (
    Application,
    ApplicationState,
    Company,
    EmailEvent,
    EmailEventType,
    Job,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
    utcnow,
)
from copilot.discovery.dedupe import dedupe_hash
from copilot.email_agent.provider import EmailProvider


def list_pending(session: Session) -> list[Suggestion]:
    return list(
        session.scalars(
            select(Suggestion)
            .where(Suggestion.status == SuggestionStatus.pending)
            .order_by(Suggestion.created_at)
        )
    )


def pending_count(session: Session) -> int:
    return len(list_pending(session))


def confirm(
    session: Session,
    suggestion_id: int,
    provider: EmailProvider | None = None,
    *,
    job_id: int | None = None,
) -> Suggestion:
    """Apply a suggestion. `provider` files the email afterward when given;
    None (e.g. tests, offline) skips filing - the DB writes are the truth,
    the mailbox is a mirror (D5), so filing later or never is safe.

    `job_id` links an untracked-application suggestion to a job you already
    track instead of creating stub records - the human knows which
    application an email belongs to even when the correlator couldn't tell
    (D28). Only meaningful for untracked suggestions; conflicts already know
    their application."""
    suggestion = _pending(session, suggestion_id)
    if job_id is not None and suggestion.kind != SuggestionKind.untracked_application:
        raise ValueError("Only untracked-application suggestions can be linked to a job.")

    if suggestion.kind == SuggestionKind.untracked_application:
        if job_id is not None:
            application = _link_to_existing_job(session, suggestion, job_id)
        else:
            application = _create_tracked_application(session, suggestion)
    else:  # state_conflict: the human sided with the email's implied state
        application = session.get(Application, suggestion.application_id)
        event = _record_email_event(session, suggestion, application.id)
        if suggestion.proposed_state is not None:
            advance_from_email(application, suggestion.proposed_state, event.id)

    suggestion.status = SuggestionStatus.confirmed
    suggestion.resolved_at = utcnow()

    if provider is not None:
        from copilot.email_agent.sync import CATEGORY_BY_TYPE

        category = CATEGORY_BY_TYPE[suggestion.classified_type or EmailEventType.other]
        provider.file_message(suggestion.provider_message_id, category=category)
    return suggestion


def decline(session: Session, suggestion_id: int) -> Suggestion:
    suggestion = _pending(session, suggestion_id)
    suggestion.status = SuggestionStatus.declined
    suggestion.resolved_at = utcnow()
    return suggestion


def _pending(session: Session, suggestion_id: int) -> Suggestion:
    suggestion = session.get(Suggestion, suggestion_id)
    if suggestion is None:
        raise LookupError(f"No suggestion with id {suggestion_id}")
    if suggestion.status != SuggestionStatus.pending:
        raise ValueError(f"Suggestion {suggestion_id} is already {suggestion.status.value}")
    return suggestion


def _create_tracked_application(session: Session, suggestion: Suggestion) -> Application:
    from copilot.email_agent.correlator import learn_domain
    from copilot.email_agent.sync import ATS_SENDER_DOMAINS

    name = suggestion.company_name or "Unknown (from email)"
    company = session.scalar(select(Company).where(Company.name == name))
    if company is None:
        company = Company(name=name)
        session.add(company)
        session.flush()
    learn_domain(company, suggestion.sender or "", ATS_SENDER_DOMAINS)

    title = suggestion.job_title or "Unknown role (from email)"
    job_hash = dedupe_hash(name, title, None)
    job = session.scalar(select(Job).where(Job.dedupe_hash == job_hash))
    if job is None:
        job = Job(
            company_id=company.id,
            title=title,
            source="email",
            apply_url="",  # applied outside Copilot; no posting URL known
            dedupe_hash=job_hash,
        )
        session.add(job)
        session.flush()

    application = get_or_create_application(session, job.id)
    event = _record_email_event(session, suggestion, application.id)
    state = suggestion.proposed_state or ApplicationState.applied
    advance_from_email(application, state, event.id)
    return application


def _link_to_existing_job(session: Session, suggestion: Suggestion, job_id: int) -> Application:
    """The human says "this email belongs to job N": record the email on that
    application and advance it only if the email is plain forward progress -
    anything else just becomes recorded history, never a silent overwrite
    (same D24 rules the automatic path follows)."""
    from copilot.email_agent.correlator import learn_domain
    from copilot.email_agent.sync import ATS_SENDER_DOMAINS

    job = session.get(Job, job_id)
    if job is None:
        raise LookupError(f"No job with id {job_id}")

    application = get_or_create_application(session, job.id)
    event = _record_email_event(session, suggestion, application.id)
    proposed = suggestion.proposed_state
    if proposed is not None and state_relation(application.state, proposed) == "forward":
        advance_from_email(application, proposed, event.id)
    if job.company is not None:
        learn_domain(job.company, suggestion.sender or "", ATS_SENDER_DOMAINS)
    return application


def _record_email_event(
    session: Session, suggestion: Suggestion, application_id: int
) -> EmailEvent:
    event = EmailEvent(
        application_id=application_id,
        provider_message_id=suggestion.provider_message_id,
        internet_message_id=suggestion.internet_message_id,
        provider_thread_id=suggestion.provider_thread_id or suggestion.provider_message_id,
        sender=suggestion.sender,
        subject=suggestion.subject,
        body_text=suggestion.body_text,
        classified_type=suggestion.classified_type or EmailEventType.other,
        implied_state=suggestion.proposed_state,
        received_at=suggestion.received_at or utcnow(),
    )
    session.add(event)
    session.flush()
    return event
