"""launchd plist content - the parts that encode decisions, not launchctl
itself (that needs a live machine and is covered by manual verification)."""

from __future__ import annotations

import plistlib
from pathlib import Path

from copilot.schedule.launchd import INTERVAL_SECONDS, _plist_content


def test_plist_runs_hourly_sync_with_daily_gated_digest(tmp_path: Path):
    plist = plistlib.loads(_plist_content(tmp_path, "/opt/bin/uv"))

    assert plist["StartInterval"] == INTERVAL_SECONDS == 3600
    command = plist["ProgramArguments"][2]
    assert "copilot inbox sync" in command
    # The job fires hourly; the digest must self-gate to daily (D27).
    assert "copilot digest --if-due" in command
    assert plist["WorkingDirectory"] == str(tmp_path)
