"""Persisted, opportunistically-captured API quota data (docs/PLAN.md "Later /
optional", docs/DECISIONS.md D33).

Design constraint: never spend a real API call just to check quota. Instead,
capture whatever a provider's response headers report on every real call
already being made for actual work (e.g. `search_jsearch`), and persist the
latest known values here. The dashboard's API usage page just reads what was
last captured - possibly stale, always honest about it (shown as "as of
<time>"), never a live probe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

USAGE_PATH = Path("data/api_usage.json")


@dataclass
class Quota:
    limit: int | None
    remaining: int | None
    reset_at: datetime | None
    captured_at: datetime


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def record_jsearch_quota(headers, path: Path | None = None) -> None:
    """Capture RapidAPI's rate-limit headers from a real `search_jsearch`
    response - success or quota-exhausted 429, either way the headers are
    read before `raise_for_status` so exhaustion still updates `remaining`.
    A response with neither header leaves the last known value untouched."""
    limit = headers.get("x-ratelimit-requests-limit")
    remaining = headers.get("x-ratelimit-requests-remaining")
    reset = headers.get("x-ratelimit-requests-reset")
    if limit is None and remaining is None:
        return

    now = datetime.now(timezone.utc)
    reset_at = None
    if reset is not None:
        try:
            reset_at = (now + timedelta(seconds=int(reset))).isoformat()
        except ValueError:
            reset_at = None  # unexpected header shape - skip rather than guess

    path = path if path is not None else USAGE_PATH
    data = _load(path)
    data["jsearch"] = {
        "limit": int(limit) if limit is not None else None,
        "remaining": int(remaining) if remaining is not None else None,
        "reset_at": reset_at,
        "captured_at": now.isoformat(),
    }
    _save(path, data)


def get_jsearch_quota(path: Path | None = None) -> Quota | None:
    path = path if path is not None else USAGE_PATH
    data = _load(path).get("jsearch")
    if not data:
        return None
    return Quota(
        limit=data.get("limit"),
        remaining=data.get("remaining"),
        reset_at=datetime.fromisoformat(data["reset_at"]) if data.get("reset_at") else None,
        captured_at=datetime.fromisoformat(data["captured_at"]),
    )
