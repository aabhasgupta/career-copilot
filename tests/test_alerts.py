"""Desktop-notification channel: composed correctly, and never able to break
the sync that triggered it."""

from __future__ import annotations

import subprocess

from copilot.notify import alerts


def test_desktop_notification_invokes_osascript_with_escaped_strings(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(alerts.sys, "platform", "darwin")
    monkeypatch.setattr(
        alerts.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd)
    )

    alerts.send_desktop_notification('Interview invite: "Stripe"', "ML Engineer")

    assert len(calls) == 1
    assert calls[0][:2] == ["osascript", "-e"]
    script = calls[0][2]
    assert script == (
        'display notification "ML Engineer" '
        'with title "Interview invite: \\"Stripe\\""'
    )


def test_desktop_notification_noop_off_macos(monkeypatch):
    monkeypatch.setattr(alerts.sys, "platform", "linux")
    monkeypatch.setattr(
        alerts.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not shell out")),
    )
    alerts.send_desktop_notification("title", "body")


def test_desktop_notification_swallows_subprocess_failures(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=10)

    monkeypatch.setattr(alerts.sys, "platform", "darwin")
    monkeypatch.setattr(alerts.subprocess, "run", boom)
    alerts.send_desktop_notification("title", "body")  # must not raise
