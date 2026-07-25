"""Immediate alert emails for time-sensitive inbox events. Policy (user
decision 2026-07-25, docs/DECISIONS.md D26): only interview invites and
online assessments interrupt - they demand action on a clock. Rejections,
confirmations, and recruiter screens wait for the daily digest.
"""

from __future__ import annotations

import html

from copilot.config import Profile
from copilot.db.models import EmailEventType
from copilot.email_agent.provider import EmailProvider, InboundMessage

ALERT_TYPES = {EmailEventType.interview_invite, EmailEventType.online_assessment}

_SUBJECT_LABEL = {
    EmailEventType.interview_invite: "Interview invite",
    EmailEventType.online_assessment: "Online assessment",
}


def send_event_alert(
    provider: EmailProvider,
    profile: Profile,
    event_type: EmailEventType,
    message: InboundMessage,
    company_name: str,
    job_title: str | None,
) -> None:
    """Send one alert about `message`. Callers gate on ALERT_TYPES."""
    label = _SUBJECT_LABEL[event_type]
    role = f" - {job_title}" if job_title else ""
    subject = f"Career Copilot: {label}: {company_name}{role}"
    body_html = (
        f"<p><b>{html.escape(label)}</b> from {html.escape(company_name)}{html.escape(role)}.</p>"
        f"<p>Original email: <i>{html.escape(message.subject)}</i> "
        f"(from {html.escape(message.sender)})</p>"
        f"<blockquote>{html.escape(message.body_text[:1500])}</blockquote>"
        "<p>Details and state are in the dashboard (<code>copilot dashboard</code>).</p>"
    )
    provider.send_mail(to=profile.email_integration.address, subject=subject, body_html=body_html)
