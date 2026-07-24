"""Application state-machine writes, shared by the CLI and dashboard so
"queue" and "mark applied" behave identically everywhere - same reasoning as
ranking.rank_jobs being factored out for the CLI/dashboard jobs list (D19).

Every transition here is an explicit user action (a CLI command or a
dashboard button click) - nothing here ever runs automatically. Phase 4's
email agent will eventually drive these same transitions from confirmed
inbox activity, but that's a distinct, not-yet-built path (D20).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot.db.models import Application, ApplicationState, utcnow


def get_or_create_application(session: Session, job_id: int) -> Application:
    application = session.scalar(select(Application).where(Application.job_id == job_id))
    if application is None:
        application = Application(job_id=job_id)
        session.add(application)
        session.flush()
    return application


def queue(session: Session, job_id: int) -> Application:
    """Declares intent to apply. Never downgrades a state already past queued."""
    application = get_or_create_application(session, job_id)
    if application.state == ApplicationState.found:
        application.state = ApplicationState.queued
    return application


def mark_applied(session: Session, job_id: int) -> Application:
    application = get_or_create_application(session, job_id)
    application.state = ApplicationState.applied
    application.applied_at = utcnow()
    return application
