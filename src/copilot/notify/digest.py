"""Daily digest email content: jobs discovered since the last digest, with
high fit_score jobs (>= HIGH_FIT_THRESHOLD) called out in a table (apply
link, salary, location); everything else summarized as a count only, since a
mailbox isn't the place to browse the full list - `copilot jobs list` /
the dashboard's Jobs tab are (docs/DECISIONS.md D19).

Phase 4 additions: application state changes since the last digest (from the
state-history log, so email-driven and manual changes both show, tagged with
who made them) and a pending-suggestions count. Non-urgent inbox events
(rejections, confirmations, recruiter screens) surface here rather than as
immediate alerts - only interview invites and OAs interrupt (D26).

"Since the last digest" is tracked in data/digest_state.json rather than a
fixed 24h window, so a missed/late scheduled run (D8 - launchd fires on wake,
not a fixed clock) never silently drops jobs found in between. No prior
state (first run) falls back to a 24h lookback.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot.config import DATA_DIR
from copilot.db.models import Application, ApplicationStateEvent, Job
from copilot.formatting import format_salary
from copilot.suggestions import list_pending

DIGEST_STATE_PATH = DATA_DIR / "digest_state.json"
HIGH_FIT_THRESHOLD = 70

# The scheduled job runs hourly (sync needs to be timely) but the digest is a
# daily summary - without a gate it would degrade into hourly change
# notifications, exactly what D26 reserved for interview invites and OAs.
# 20h rather than 24h so a run that lands slightly earlier than yesterday's
# doesn't skip a whole day.
MIN_INTERVAL_HOURS = 20


def digest_due(state_path: Path = DIGEST_STATE_PATH) -> bool:
    """True when enough time has passed since the last send for a scheduled
    digest to fire. Manual `copilot digest` ignores this - asking is due."""
    if not state_path.exists():
        return True
    last = datetime.fromisoformat(json.loads(state_path.read_text())["last_sent_at"])
    return datetime.now(timezone.utc) - last >= timedelta(hours=MIN_INTERVAL_HOURS)


def _last_sent_at(state_path: Path) -> datetime:
    if state_path.exists():
        data = json.loads(state_path.read_text())
        return datetime.fromisoformat(data["last_sent_at"])
    return datetime.now(timezone.utc) - timedelta(hours=24)


def mark_digest_sent(state_path: Path = DIGEST_STATE_PATH) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_sent_at": datetime.now(timezone.utc).isoformat()}))


@dataclass
class Digest:
    new_count: int
    high_fit_jobs: list[Job]
    subject: str
    body_html: str


def _job_row_html(job: Job) -> str:
    title = html.escape(job.title)
    company = html.escape(job.company.name if job.company else "?")
    location = html.escape(job.location or "unknown")
    apply_url = html.escape(job.apply_url)
    return (
        f"<tr><td>{job.fit_score:.0f}</td>"
        f'<td><a href="{apply_url}">{title}</a></td>'
        f"<td>{company}</td><td>{location}</td><td>{format_salary(job)}</td></tr>"
    )


def _state_changes_html(session: Session, since: datetime) -> str:
    changes = session.scalars(
        select(ApplicationStateEvent)
        .where(ApplicationStateEvent.created_at >= since)
        .order_by(ApplicationStateEvent.created_at)
    ).all()
    if not changes:
        return ""
    lines = []
    for c in changes:
        application = session.get(Application, c.application_id)
        job = application.job if application else None
        label = f"{job.company.name} - {job.title}" if job else f"application {c.application_id}"
        who = "you" if c.source == "manual" else "email"
        frm = c.from_state.value if c.from_state else "?"
        lines.append(f"<li>{html.escape(label)}: {frm} &rarr; {c.to_state.value} ({who})</li>")
    return f"<h3>State changes</h3><ul>{''.join(lines)}</ul>"


def _suggestions_html(session: Session) -> str:
    count = len(list_pending(session))
    if not count:
        return ""
    return (
        f"<p><b>{count} suggestion{'s' if count != 1 else ''} awaiting your review</b> - "
        "run <code>copilot inbox review</code> or open the dashboard's Suggestions tab.</p>"
    )


def build_digest(session: Session, state_path: Path = DIGEST_STATE_PATH) -> Digest | None:
    """Returns None when there's nothing new to report - callers should skip
    sending rather than send an empty digest."""
    since = _last_sent_at(state_path)
    # "New jobs" means jobs discovered by discovery - not jobs created as a
    # side effect of confirming an inbox suggestion (source="email"), which
    # are applications the user already made and surface in "State changes"
    # instead. Counting those as discoveries double-reports and inflates the
    # subject line (found live 2026-07-26).
    new_jobs = session.scalars(
        select(Job).where(Job.created_at >= since, Job.source != "email")
    ).all()
    state_changes = _state_changes_html(session, since)
    suggestions = _suggestions_html(session)
    if not new_jobs and not state_changes and not suggestions:
        return None

    high_fit = sorted(
        (j for j in new_jobs if (j.fit_score or 0) >= HIGH_FIT_THRESHOLD),
        key=lambda j: -(j.fit_score or 0),
    )

    if new_jobs:
        subject = (
            f"Career Copilot: {len(new_jobs)} new jobs, {len(high_fit)} scored "
            f"{HIGH_FIT_THRESHOLD}+"
        )
    else:
        subject = "Career Copilot: application updates"

    if high_fit:
        rows = "".join(_job_row_html(j) for j in high_fit)
        highlights = (
            f"<p>{len(high_fit)} scored {HIGH_FIT_THRESHOLD}+ - the rest are in "
            "<code>copilot jobs list</code> or the dashboard's Jobs tab.</p>"
            "<table border='1' cellpadding='6' cellspacing='0'>"
            "<tr><th>Fit</th><th>Title</th><th>Company</th><th>Location</th><th>Salary</th></tr>"
            f"{rows}</table>"
        )
    elif new_jobs:
        highlights = f"<p>None scored {HIGH_FIT_THRESHOLD}+ yet - check the full list for anything promising.</p>"
    else:
        highlights = ""

    jobs_line = (
        f"<p>{len(new_jobs)} new job{'s' if len(new_jobs) != 1 else ''} since the last digest.</p>"
        if new_jobs
        else ""
    )
    body_html = f"{jobs_line}{highlights}{state_changes}{suggestions}"

    return Digest(new_count=len(new_jobs), high_fit_jobs=high_fit, subject=subject, body_html=body_html)
