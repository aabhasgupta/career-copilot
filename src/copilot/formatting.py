"""Small display-formatting helpers shared by the CLI, dashboard, and digest
email, so a job's salary/posting-date is never rendered two subtly different
ways."""

from __future__ import annotations

from datetime import datetime, timezone

from copilot.db.models import Job


def format_salary(job: Job) -> str:
    if not (job.salary_min or job.salary_max):
        return "unknown"
    lo = f"{job.salary_min:,}" if job.salary_min else "?"
    hi = f"{job.salary_max:,}" if job.salary_max else "?"
    return f"${lo}-${hi}"


def posting_age_days(job: Job) -> int | None:
    """Days since the posting went live per the source's own posted_at, not
    since we discovered it - None when the source didn't report one (never
    assumed to be either fresh or stale)."""
    if job.posted_at is None:
        return None
    # SQLite doesn't persist tz-awareness even for DateTime(timezone=True)
    # columns, so values come back naive - treat them as UTC like they were
    # stored (see utcnow() in db/models.py).
    posted_at = job.posted_at.replace(tzinfo=timezone.utc) if job.posted_at.tzinfo is None else job.posted_at
    return (datetime.now(timezone.utc) - posted_at).days


def format_posted(job: Job) -> str:
    days = posting_age_days(job)
    if days is None:
        return "unknown"
    if days <= 0:
        return "today"
    if days == 1:
        return "1d ago"
    return f"{days}d ago"
