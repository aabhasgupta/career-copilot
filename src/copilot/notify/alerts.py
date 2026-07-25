"""Immediate alerts for time-sensitive inbox events. Policy (user decision
2026-07-25, docs/DECISIONS.md D26): only interview invites and online
assessments interrupt - they demand action on a clock. Rejections,
confirmations, and recruiter screens wait for the daily digest.

Two channels per alert: a macOS desktop notification (the sync runs locally
via launchd, so this is the one channel that isn't the inbox the triggering
email is already sitting in) and an alert email as the durable fallback for
when the user is away from this machine.
"""

from __future__ import annotations

import html
import subprocess
import sys

from copilot.config import Profile
from copilot.db.models import EmailEventType
from copilot.email_agent.provider import EmailProvider, InboundMessage

ALERT_TYPES = {EmailEventType.interview_invite, EmailEventType.online_assessment}

_SUBJECT_LABEL = {
    EmailEventType.interview_invite: "Interview invite",
    EmailEventType.online_assessment: "Online assessment",
}


def send_desktop_notification(title: str, body: str) -> None:
    """Best-effort native notification; no-op off macOS, never raises - a
    broken notifier must not fail the sync that triggered it."""
    if sys.platform != "darwin":
        return
    # AppleScript string literals escape like JSON strings for plain text.
    script = f"display notification {_as_script_string(body)} with title {_as_script_string(title)}"
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _as_script_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
    send_desktop_notification(
        title=f"{label}: {company_name}", body=job_title or message.subject
    )
    body_html = (
        f"<p><b>{html.escape(label)}</b> from {html.escape(company_name)}{html.escape(role)}.</p>"
        f"<p>Original email: <i>{html.escape(message.subject)}</i> "
        f"(from {html.escape(message.sender)})</p>"
        f"<blockquote>{html.escape(message.body_text[:1500])}</blockquote>"
        "<p>Details and state are in the dashboard (<code>copilot dashboard</code>).</p>"
    )
    provider.send_mail(to=profile.email_integration.address, subject=subject, body_html=body_html)
