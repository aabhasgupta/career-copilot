"""Immediate alerts for time-sensitive inbox events. Policy (user decisions
2026-07-25, docs/DECISIONS.md D26 + D30): only interview invites and online
assessments interrupt - they demand action on a clock - and the channel is a
macOS desktop notification only. Alert *emails* were built first and dropped
after live testing (D30): an email about an email, delivered to the same
inbox that already holds the original, added noise and no information. The
digest remains the durable record for time-shifted reading.
"""

from __future__ import annotations

import subprocess
import sys

from copilot.db.models import EmailEventType
from copilot.email_agent.provider import InboundMessage

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
    script = f"display notification {as_applescript_string(body)} with title {as_applescript_string(title)}"
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def as_applescript_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def send_event_alert(
    event_type: EmailEventType,
    message: InboundMessage,
    company_name: str,
    job_title: str | None,
) -> None:
    """Alert about `message` via desktop notification. Callers gate on
    ALERT_TYPES."""
    label = _SUBJECT_LABEL[event_type]
    send_desktop_notification(
        title=f"{label}: {company_name}", body=job_title or message.subject
    )
