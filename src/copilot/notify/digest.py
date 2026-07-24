"""Daily digest email content: jobs discovered since the last digest, with
high fit_score jobs (>= HIGH_FIT_THRESHOLD) called out in a table (apply
link, salary, location); everything else summarized as a count only, since a
mailbox isn't the place to browse the full list - `copilot jobs list` /
the dashboard's Jobs tab are (docs/DECISIONS.md D19).

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
from copilot.db.models import Job
from copilot.formatting import format_salary

DIGEST_STATE_PATH = DATA_DIR / "digest_state.json"
HIGH_FIT_THRESHOLD = 70


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


def build_digest(session: Session, state_path: Path = DIGEST_STATE_PATH) -> Digest | None:
    """Returns None when there's nothing new to report - callers should skip
    sending rather than send an empty digest."""
    since = _last_sent_at(state_path)
    new_jobs = session.scalars(select(Job).where(Job.created_at >= since)).all()
    if not new_jobs:
        return None

    high_fit = sorted(
        (j for j in new_jobs if (j.fit_score or 0) >= HIGH_FIT_THRESHOLD),
        key=lambda j: -(j.fit_score or 0),
    )

    subject = f"Career Copilot: {len(new_jobs)} new jobs, {len(high_fit)} scored {HIGH_FIT_THRESHOLD}+"

    if high_fit:
        rows = "".join(_job_row_html(j) for j in high_fit)
        highlights = (
            f"<p>{len(high_fit)} scored {HIGH_FIT_THRESHOLD}+ - the rest are in "
            "<code>copilot jobs list</code> or the dashboard's Jobs tab.</p>"
            "<table border='1' cellpadding='6' cellspacing='0'>"
            "<tr><th>Fit</th><th>Title</th><th>Company</th><th>Location</th><th>Salary</th></tr>"
            f"{rows}</table>"
        )
    else:
        highlights = f"<p>None scored {HIGH_FIT_THRESHOLD}+ yet - check the full list for anything promising.</p>"

    body_html = f"<p>{len(new_jobs)} new job{'s' if len(new_jobs) != 1 else ''} since the last digest.</p>{highlights}"

    return Digest(new_count=len(new_jobs), high_fit_jobs=high_fit, subject=subject, body_html=body_html)
