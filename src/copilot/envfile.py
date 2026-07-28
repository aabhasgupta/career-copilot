"""Read/write the project `.env` for the dashboard control panel.

Localhost, single-user (D4): API keys are edited in the Integrations page and
applied to the live process immediately, so no dashboard restart is needed. The
durable write goes to `.env` (python-dotenv preserves any other lines and
comments), which the CLI and scheduled runs load fresh on their next start.

Only a fixed allow-list of keys can be written - the endpoint never lets an
arbitrary environment variable be set from a form.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import set_key

ENV_PATH = Path(".env")

# The only environment variables the control panel may write.
ALLOWED_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ADZUNA_APP_ID",
        "ADZUNA_APP_KEY",
        "JSEARCH_API_KEY",
        "AZURE_CLIENT_ID",
    }
)


def set_env(key: str, value: str, path: Path | None = None) -> None:
    """Persist one allow-listed key to `.env` and apply it to os.environ live.

    Empty/whitespace values are ignored (a blank field means "leave unchanged",
    so a form can offer several keys and only update the ones filled in)."""
    if key not in ALLOWED_ENV_KEYS:
        raise ValueError(f"{key} is not a configurable key")
    value = value.strip()
    if not value:
        return
    path = path if path is not None else ENV_PATH
    path.touch(exist_ok=True)
    set_key(str(path), key, value)
    os.environ[key] = value  # live-apply so the status row flips without a restart
