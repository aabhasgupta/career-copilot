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

from copilot.db.models import Application, ApplicationState, ApplicationStateEvent, utcnow

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
