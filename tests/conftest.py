"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    """Alert-path tests must not pop real macOS notifications (or shell out
    at all) - stub the osascript subprocess while keeping the code path
    exercised. Tests that care about the call re-patch and inspect."""
    monkeypatch.setattr(
        "copilot.notify.alerts.subprocess.run", lambda *a, **k: None
    )
