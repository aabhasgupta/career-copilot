"""Application state-machine writes, shared by the CLI and dashboard so
"queue" and "mark applied" behave identically everywhere - same reasoning as
ranking.rank_jobs being factored out for the CLI/dashboard jobs list (D19).

Every transition here is an explicit user action (a CLI command or a
dashboard button click) - nothing here ever runs automatically. Phase 4's
email agent will eventually drive these same transitions from confirmed
inbox activity, but that's a distinct, not-yet-built path (D20).

Each transition is also logged to ApplicationStateEvent tagged source="manual",
so once Phase 4 becomes a second writer to Application.state (tagging its own
entries "email:<event_id>"), a genuine conflict between a human-set and an
email-inferred state can actually be told apart from plain forward progress
(docs/DECISIONS.md D24).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot.db.models import (
    Application,
    ApplicationState,
    ApplicationStateEvent,
    Job,
    Suggestion,
    SuggestionStatus,
    utcnow,
)

MANUAL = "manual"

# The forward ladder. rejected/ghosted are terminal branches reachable from
# any active state, not rungs above offer - see state_relation() for how
# comparisons treat them.
_LADDER = [
    ApplicationState.found,
    ApplicationState.queued,
    ApplicationState.applied,
    ApplicationState.replied,
    ApplicationState.interviewing,
    ApplicationState.offer,
]
_TERMINAL = {ApplicationState.rejected, ApplicationState.ghosted}


def state_relation(current: ApplicationState, implied: ApplicationState) -> str:
    """How an email-implied state relates to the stored one (D24):
    - "forward": implied is further along - safe to auto-advance
    - "stale": implied is the same or earlier news - record, change nothing
    - "conflict": implied contradicts the stored state - flag, never write
    """
    if implied == current:
        return "stale"
    if current in _TERMINAL:
        # Any signal of active progress after rejected/ghosted contradicts
        # the terminal state, whoever set it - flagging is the safe default.
        return "conflict"
    if implied in _TERMINAL:
        # A rejection landing on an active application is forward progress.
        return "forward"
    return "forward" if _LADDER.index(implied) > _LADDER.index(current) else "stale"


def get_or_create_application(session: Session, job_id: int) -> Application:
    application = session.scalar(select(Application).where(Application.job_id == job_id))
    if application is None:
        application = Application(job_id=job_id)
        session.add(application)
        session.flush()
    return application


def _record_transition(application: Application, to_state: ApplicationState, source: str) -> None:
    """No-op if the state isn't actually changing, so repeat calls (e.g.
    mark_applied twice) don't pile up identical history rows."""
    if application.state == to_state:
        return
    application.state_history.append(
        ApplicationStateEvent(from_state=application.state, to_state=to_state, source=source)
    )


def queue(session: Session, job_id: int) -> Application:
    """Declares intent to apply. Never downgrades a state already past queued."""
    application = get_or_create_application(session, job_id)
    if application.state == ApplicationState.found:
        _record_transition(application, ApplicationState.queued, source=MANUAL)
        application.state = ApplicationState.queued
    return application


def mark_applied(session: Session, job_id: int) -> Application:
    application = get_or_create_application(session, job_id)
    _record_transition(application, ApplicationState.applied, source=MANUAL)
    application.state = ApplicationState.applied
    application.applied_at = utcnow()
    return application


def set_state(session: Session, job_id: int, to_state: ApplicationState) -> Application:
    """Manual override to any state, in any direction - the escape hatch for
    reality Copilot missed (an email that never arrived, a phone call, an
    accidental click being undone). Unlike queue/mark_applied it can move
    backward: it records what the human says is true, and the manual-tagged
    history row keeps D24's conflict rules working afterward."""
    application = get_or_create_application(session, job_id)
    _record_transition(application, to_state, source=MANUAL)
    application.state = to_state
    if to_state == ApplicationState.applied and application.applied_at is None:
        application.applied_at = utcnow()
    return application


def untrack(session: Session, job_id: int) -> str:
    """Remove all tracking for a job - the undo for a wrong confirm or a
    mistaken queue/mark-applied (D28). Deletes the application with its state
    history and email events; a job that only exists because an email was
    confirmed (source="email") is deleted too, along with its company if
    nothing else references it. Discovery-found jobs stay in the jobs list.

    No resurrection: suggestion rows are kept (their message ids keep sync's
    idempotency intact), just detached and closed out. Returns a short
    human-readable summary of what was removed."""
    job = session.get(Job, job_id)
    if job is None:
        raise LookupError(f"No job with id {job_id}")
    application = session.scalar(select(Application).where(Application.job_id == job_id))
    if application is None:
        raise LookupError(f"Job {job_id} isn't tracked - nothing to untrack.")

    for suggestion in session.scalars(
        select(Suggestion).where(Suggestion.application_id == application.id)
    ):
        suggestion.application_id = None
        if suggestion.status == SuggestionStatus.pending:
            # A pending conflict about an application that no longer exists
            # has nothing left to decide.
            suggestion.status = SuggestionStatus.declined
            suggestion.resolved_at = utcnow()

    for event in list(application.email_events):
        session.delete(event)
    for history_row in list(application.state_history):
        session.delete(history_row)
    session.delete(application)

    removed = f"application for job {job_id} ({job.title})"
    if job.source == "email":
        company = job.company
        session.delete(job)
        session.flush()
        # Re-query rather than trusting company.jobs: the loaded collection
        # can still hold the just-deleted row.
        other_jobs = (
            None
            if company is None
            else session.scalar(select(Job.id).where(Job.company_id == company.id))
        )
        if company is not None and other_jobs is None:
            session.delete(company)
            removed += f", the email-created job and company ({company.name})"
        else:
            removed += ", the email-created job"
    return removed


def advance_from_email(
    application: Application, to_state: ApplicationState, email_event_id: int
) -> None:
    """Email-driven state advance (Phase 4's second writer). Callers must
    have already established this is plain forward progress via
    state_relation() - conflicts become Suggestions instead, never writes."""
    _record_transition(application, to_state, source=f"email:{email_event_id}")
    application.state = to_state
    if to_state == ApplicationState.applied and application.applied_at is None:
        application.applied_at = utcnow()
