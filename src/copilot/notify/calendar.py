"""Best-effort Mac Calendar event for an interview_invite/online_assessment
with a stated date (docs/DECISIONS.md D49). Same AppleScript approach as
notify/alerts.py's desktop notifications - no-op off macOS, never raises;
a broken Calendar.app/osascript must not fail the sync that triggered it.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta

from copilot.notify.alerts import as_applescript_string

CALENDAR_NAME = "Career Copilot"
_DEFAULT_DURATION_MINUTES = 60


def parse_interview_at(value: str | None) -> tuple[datetime, bool] | None:
    """Parse the classifier's interview_at string. Returns (start, all_day)
    or None if there's nothing to add - a malformed string from the model
    is treated the same as "nothing stated", never a crash."""
    if not value:
        return None
    try:
        all_day = len(value) <= 10  # "YYYY-MM-DD" only, no time component
        return datetime.fromisoformat(value), all_day
    except ValueError:
        return None


def _date_literal(var: str, dt: datetime) -> str:
    """AppleScript field-setting on a `current date` is immune to the
    locale/format quirks that plague string-to-date parsing. day is set to 1
    before year/month so an intermediate value can never roll over into the
    wrong month (e.g. today is the 31st, target month is February)."""
    return (
        f"set {var} to current date\n"
        f"set day of {var} to 1\n"
        f"set year of {var} to {dt.year}\n"
        f"set month of {var} to {dt.month}\n"
        f"set day of {var} to {dt.day}\n"
        f"set hours of {var} to {dt.hour}\n"
        f"set minutes of {var} to {dt.minute}\n"
        f"set seconds of {var} to 0\n"
    )


def add_calendar_event(summary: str, start: datetime, *, all_day: bool, notes: str = "") -> bool:
    """Create an event in the local "Career Copilot" calendar (created on
    first use if missing). Returns True on success. A timezone-aware `start`
    is converted to the Mac's local time, since AppleScript's date fields
    are local-time by nature."""
    if sys.platform != "darwin":
        return False

    local_start = start.astimezone() if start.tzinfo else start
    local_end = local_start + (
        timedelta(days=1) if all_day else timedelta(minutes=_DEFAULT_DURATION_MINUTES)
    )

    script = (
        # Found live: an hourly background sync almost never has Calendar.app
        # already open, and `tell application "Calendar"` does not reliably
        # auto-launch it in that context (observed: "-600 Application isn't
        # running" even though a normal interactive AppleScript usually
        # launches the target app on demand) - launch explicitly and give it
        # a moment before addressing it.
        'tell application "Calendar" to launch\n'
        "delay 1\n"
        + _date_literal("startDate", local_start)
        + _date_literal("endDate", local_end)
        + f'tell application "Calendar"\n'
        f'  if not (exists calendar "{CALENDAR_NAME}") then\n'
        f'    make new calendar with properties {{name:"{CALENDAR_NAME}"}}\n'
        f"  end if\n"
        f'  tell calendar "{CALENDAR_NAME}"\n'
        f"    make new event with properties {{summary:{as_applescript_string(summary)}, "
        f"start date:startDate, end date:endDate, description:{as_applescript_string(notes)}, "
        f'allday event:{"true" if all_day else "false"}}}\n'
        f"  end tell\n"
        f"end tell\n"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=15, text=True
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
