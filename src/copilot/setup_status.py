"""One source of truth for "what is configured" - inspects profile.yaml, the
resume file, the .env API keys, the email token, and the launchd schedule, and
returns a structured checklist.

Shared (like applications.py / suggestions.py, D19): the dashboard's Home
command center, the Integrations page, and the first-run onboarding gate all read
from here, and a future `copilot status --setup` can too. Reading only - it never
writes or makes network calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from copilot.config import PROFILE_FILENAME, Profile


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    required: bool
    detail: str = ""


@dataclass
class SetupStatus:
    checks: list[Check] = field(default_factory=list)

    @property
    def required_complete(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    @property
    def complete(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def missing_required(self) -> list[Check]:
        return [c for c in self.checks if c.required and not c.ok]

    def get(self, key: str) -> Check | None:
        return next((c for c in self.checks if c.key == key), None)


def get_setup_status(profile_path: Path | None = None) -> SetupStatus:
    """Build the checklist. Required items gate the app (Home landing /
    onboarding); optional items (email, schedule) enrich it but never block."""
    profile_path = profile_path or Path(PROFILE_FILENAME)
    checks: list[Check] = []

    # --- Profile ---
    profile: Profile | None = None
    if not profile_path.exists():
        checks.append(Check("profile", "Profile & preferences", False, True,
                            f"{profile_path} not found"))
    else:
        try:
            profile = Profile.model_validate(yaml.safe_load(profile_path.read_text()))
            checks.append(Check("profile", "Profile & preferences", True, True,
                                "identity, search titles, and visa set"))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            checks.append(Check("profile", "Profile & preferences", False, True,
                                f"{profile_path} is invalid: {exc}"))

    # --- Resume ---
    if profile is not None:
        resume = Path(profile.resume_path)
        ok = resume.exists()
        checks.append(Check("resume", "Resume", ok, True,
                            str(resume) if ok else f"{resume} is missing"))
    else:
        checks.append(Check("resume", "Resume", False, True, "add your profile first"))

    # --- Anthropic key (scoring, tailoring, inbox classification) ---
    checks.append(Check(
        "anthropic", "Anthropic API key", bool(os.environ.get("ANTHROPIC_API_KEY")), True,
        "powers fit scoring, tailoring, and inbox classification",
    ))

    # --- Discovery source: at least one aggregator ---
    adzuna = bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))
    jsearch = bool(os.environ.get("JSEARCH_API_KEY"))
    sources = [name for name, on in (("Adzuna", adzuna), ("JSearch", jsearch)) if on]
    checks.append(Check(
        "discovery", "Job discovery source", bool(sources), True,
        (", ".join(sources) + " configured") if sources else "add Adzuna or JSearch keys",
    ))

    # --- Email inbox sync (optional) ---
    from copilot.email_agent.outlook import TOKEN_CACHE_PATH

    azure = bool(os.environ.get("AZURE_CLIENT_ID"))
    logged_in = azure and TOKEN_CACHE_PATH.exists()
    email_detail = (
        "logged in - inbox sync ready" if logged_in
        else "client id set, run login" if azure
        else "optional - reply tracking and digests"
    )
    checks.append(Check("email", "Email (inbox sync)", logged_in, False, email_detail))

    # --- Hourly schedule (optional) ---
    try:
        from copilot.schedule.launchd import status as schedule_status

        s = schedule_status()
        checks.append(Check("schedule", "Hourly background sync", s.loaded, False, s.detail))
    except Exception:  # noqa: BLE001 - a broken launchctl must not break the page
        checks.append(Check("schedule", "Hourly background sync", False, False, "not installed"))

    return SetupStatus(checks)
