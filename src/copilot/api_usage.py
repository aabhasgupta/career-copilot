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


@dataclass
class AnthropicSpend:
    month: str  # "YYYY-MM"
    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float | None  # None if any recorded call used a model with no known price


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


def record_anthropic_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    cost_usd: float | None,
    path: Path | None = None,
) -> None:
    """Accumulate one real Claude call's usage into the current calendar
    month's running total (docs/DECISIONS.md D34). This is a self-tracked
    estimate of what this tool's own calls cost - not the account's real
    balance, which no API exposes for a standard (non-Admin) key."""
    path = path if path is not None else USAGE_PATH
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    data = _load(path)
    months = data.setdefault("anthropic", {})
    bucket = months.setdefault(
        month,
        {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.0,
            "cost_unknown": False,
        },
    )
    bucket["calls"] += 1
    bucket["input_tokens"] += input_tokens
    bucket["output_tokens"] += output_tokens
    bucket["cache_creation_tokens"] += cache_creation_tokens
    bucket["cache_read_tokens"] += cache_read_tokens
    if cost_usd is None:
        bucket["cost_unknown"] = True  # at least one call used an unpriced model
    else:
        bucket["cost_usd"] += cost_usd
    _save(path, data)


def get_anthropic_spend(month: str | None = None, path: Path | None = None) -> AnthropicSpend | None:
    """Current (or given) calendar month's tracked spend, or None if no
    calls have been recorded yet this month."""
    path = path if path is not None else USAGE_PATH
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    bucket = _load(path).get("anthropic", {}).get(month)
    if not bucket:
        return None
    return AnthropicSpend(
        month=month,
        calls=bucket["calls"],
        input_tokens=bucket["input_tokens"],
        output_tokens=bucket["output_tokens"],
        cache_creation_tokens=bucket["cache_creation_tokens"],
        cache_read_tokens=bucket["cache_read_tokens"],
        cost_usd=None if bucket.get("cost_unknown") else bucket["cost_usd"],
    )
