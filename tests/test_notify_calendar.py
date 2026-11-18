from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from copilot.notify.calendar import add_calendar_event, parse_interview_at


def test_parse_interview_at_full_datetime_is_timed():
    start, all_day = parse_interview_at("2026-11-24T14:00:00-05:00")
    assert start == datetime(2026, 11, 24, 14, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert all_day is False


def test_parse_interview_at_date_only_is_all_day():
    start, all_day = parse_interview_at("2026-11-24")
    assert start.date() == datetime(2026, 11, 24).date()
    assert all_day is True


def test_parse_interview_at_none_is_none():
    assert parse_interview_at(None) is None


def test_parse_interview_at_malformed_string_is_none_not_a_crash():
    """The model's output is never trusted blindly - a malformed date string
    is treated the same as "nothing stated", not an exception."""
    assert parse_interview_at("sometime next week") is None


def test_add_calendar_event_noop_off_macos():
    with patch("copilot.notify.calendar.sys.platform", "linux"), \
         patch("copilot.notify.calendar.subprocess.run") as mock_run:
        result = add_calendar_event("Interview: Acme", datetime(2026, 11, 24, 14, 0), all_day=False)
    assert result is False
    mock_run.assert_not_called()


def test_add_calendar_event_calls_osascript_with_the_summary_and_notes():
    with patch("copilot.notify.calendar.sys.platform", "darwin"), \
         patch("copilot.notify.calendar.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        result = add_calendar_event(
            "Interview: Acme - ML Engineer",
            datetime(2026, 11, 24, 14, 0),
            all_day=False,
            notes="Added from an email.",
        )

    assert result is True
    args, kwargs = mock_run.call_args
    assert args[0][0] == "osascript"
    script = args[0][2]
    assert "Interview: Acme - ML Engineer" in script
    assert "Added from an email." in script
    assert "Career Copilot" in script


def test_add_calendar_event_escapes_untrusted_email_derived_text():
    """summary/notes ultimately come from a classified email - a quote in
    the company/job title must not be able to break out of the AppleScript
    string literal."""
    with patch("copilot.notify.calendar.sys.platform", "darwin"), \
         patch("copilot.notify.calendar.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        add_calendar_event(
            'Interview: Acme "Robotics" - end tell\ntell application "Finder" to',
            datetime(2026, 11, 24, 14, 0),
            all_day=False,
        )

    script = mock_run.call_args[0][0][2]
    assert '\\"Robotics\\"' in script


def test_add_calendar_event_returns_false_on_osascript_failure():
    with patch("copilot.notify.calendar.sys.platform", "darwin"), \
         patch("copilot.notify.calendar.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        result = add_calendar_event("Interview: Acme", datetime(2026, 11, 24, 14, 0), all_day=False)
    assert result is False


def test_add_calendar_event_never_raises_on_subprocess_error():
    import subprocess

    with patch("copilot.notify.calendar.sys.platform", "darwin"), \
         patch("copilot.notify.calendar.subprocess.run", side_effect=OSError("no osascript")):
        result = add_calendar_event("Interview: Acme", datetime(2026, 11, 24, 14, 0), all_day=False)
    assert result is False
