from datetime import datetime, timezone
from pathlib import Path

import httpx

from copilot.api_usage import get_jsearch_quota, record_jsearch_quota


def test_record_and_get_roundtrip(tmp_path: Path):
    path = tmp_path / "usage.json"
    headers = httpx.Headers({
        "x-ratelimit-requests-limit": "200",
        "x-ratelimit-requests-remaining": "142",
        "x-ratelimit-requests-reset": "86400",  # 1 day, RapidAPI convention
    })
    record_jsearch_quota(headers, path=path)

    quota = get_jsearch_quota(path=path)
    assert quota is not None
    assert quota.limit == 200
    assert quota.remaining == 142
    assert quota.reset_at is not None
    delta = quota.reset_at - datetime.now(timezone.utc)
    assert 23 * 3600 < delta.total_seconds() < 25 * 3600  # ~1 day out


def test_get_quota_missing_file_returns_none(tmp_path: Path):
    assert get_jsearch_quota(path=tmp_path / "nope.json") is None


def test_record_ignores_response_with_no_ratelimit_headers(tmp_path: Path):
    """A non-rate-limited response (or a provider outage) must not clobber
    the last known good value with nothing."""
    path = tmp_path / "usage.json"
    record_jsearch_quota(httpx.Headers({"x-ratelimit-requests-limit": "200",
                                         "x-ratelimit-requests-remaining": "150"}), path=path)
    record_jsearch_quota(httpx.Headers({}), path=path)
    quota = get_jsearch_quota(path=path)
    assert quota is not None
    assert quota.remaining == 150


def test_record_handles_malformed_reset_header_gracefully(tmp_path: Path):
    path = tmp_path / "usage.json"
    headers = httpx.Headers({
        "x-ratelimit-requests-limit": "200",
        "x-ratelimit-requests-remaining": "10",
        "x-ratelimit-requests-reset": "not-a-number",
    })
    record_jsearch_quota(headers, path=path)  # must not raise
    quota = get_jsearch_quota(path=path)
    assert quota is not None
    assert quota.remaining == 10
    assert quota.reset_at is None


def test_record_updates_existing_file_in_place(tmp_path: Path):
    path = tmp_path / "usage.json"
    record_jsearch_quota(httpx.Headers({"x-ratelimit-requests-limit": "200",
                                         "x-ratelimit-requests-remaining": "150"}), path=path)
    record_jsearch_quota(httpx.Headers({"x-ratelimit-requests-limit": "200",
                                         "x-ratelimit-requests-remaining": "149"}), path=path)
    assert get_jsearch_quota(path=path).remaining == 149
