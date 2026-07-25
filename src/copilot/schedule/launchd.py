"""launchd scheduling for the hourly inbox sync + digest run.

launchd over cron (docs/DECISIONS.md D8): a StartInterval job whose fire time
passed while the laptop slept runs on wake, so a closed lid means catching up
later, not silently missing runs. The plist lives in ~/Library/LaunchAgents
(per-user, no sudo) and shells out to `uv run copilot ...` with the project
directory as cwd, same as running the commands by hand.

Pause/resume use launchctl bootout/bootstrap rather than deleting the plist,
so "paused" and "never installed" stay distinguishable.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

LABEL = "com.careercopilot.sync"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
INTERVAL_SECONDS = 3600  # hourly (user decision 2026-07-25, D26)
LOG_DIR_NAME = "logs"


def _plist_content(project_dir: Path, uv_path: str) -> bytes:
    log_dir = project_dir / "data" / LOG_DIR_NAME
    return plistlib.dumps(
        {
            "Label": LABEL,
            # `uv run` resolves the project env from WorkingDirectory; sync
            # runs first, then the digest reports whatever it changed.
            # --if-due keeps the digest daily even though the job is hourly.
            "ProgramArguments": [
                "/bin/sh",
                "-c",
                f'"{uv_path}" run copilot inbox sync && "{uv_path}" run copilot digest --if-due',
            ],
            "WorkingDirectory": str(project_dir),
            "StartInterval": INTERVAL_SECONDS,
            "RunAtLoad": False,
            "StandardOutPath": str(log_dir / "sync.log"),
            "StandardErrorPath": str(log_dir / "sync.err.log"),
        }
    )


def _gui_domain() -> str:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True, check=True).stdout.strip()
    return f"gui/{uid}"


@dataclass
class ScheduleStatus:
    installed: bool  # plist file exists
    loaded: bool  # known to launchd right now
    detail: str


def install(project_dir: Path) -> Path:
    uv_path = shutil.which("uv")
    if uv_path is None:
        raise RuntimeError("uv not found on PATH - can't build the launchd job.")
    (project_dir / "data" / LOG_DIR_NAME).mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_bytes(_plist_content(project_dir, uv_path))
    # Re-bootstrap cleanly if an older version is already loaded.
    subprocess.run(
        ["launchctl", "bootout", _gui_domain(), str(PLIST_PATH)], capture_output=True
    )
    subprocess.run(
        ["launchctl", "bootstrap", _gui_domain(), str(PLIST_PATH)],
        capture_output=True,
        text=True,
        check=True,
    )
    return PLIST_PATH


def uninstall() -> bool:
    """Returns True if there was anything to remove."""
    existed = PLIST_PATH.exists()
    subprocess.run(
        ["launchctl", "bootout", _gui_domain(), str(PLIST_PATH)], capture_output=True
    )
    if existed:
        PLIST_PATH.unlink()
    return existed


def pause() -> None:
    subprocess.run(
        ["launchctl", "bootout", _gui_domain(), str(PLIST_PATH)],
        capture_output=True,
        text=True,
        check=True,
    )


def resume() -> None:
    if not PLIST_PATH.exists():
        raise RuntimeError("Not installed - run 'copilot schedule install' first.")
    subprocess.run(
        ["launchctl", "bootstrap", _gui_domain(), str(PLIST_PATH)],
        capture_output=True,
        text=True,
        check=True,
    )


def status() -> ScheduleStatus:
    installed = PLIST_PATH.exists()
    result = subprocess.run(
        ["launchctl", "print", f"{_gui_domain()}/{LABEL}"], capture_output=True, text=True
    )
    loaded = result.returncode == 0
    if not installed:
        detail = "not installed"
    elif not loaded:
        detail = "installed but paused (launchctl job not loaded)"
    else:
        # Pull the last exit status line out of launchctl's verbose dump.
        exit_line = next(
            (line.strip() for line in result.stdout.splitlines() if "last exit" in line),
            "",
        )
        detail = f"running hourly; {exit_line or 'no runs yet'}"
    return ScheduleStatus(installed=installed, loaded=loaded, detail=detail)
