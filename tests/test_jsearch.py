from pathlib import Path

import httpx
import pytest

from copilot.discovery import jsearch


_RealClient = httpx.Client  # captured before any test monkeypatches httpx.Client


def _mock_client(status_code: int, ratelimit_headers: dict, body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body, headers=ratelimit_headers)

    return lambda *a, **kw: _RealClient(transport=httpx.MockTransport(handler), **kw)


def test_search_jsearch_records_quota_from_response_headers(tmp_path: Path, monkeypatch):
    captured_path = tmp_path / "usage.json"
    monkeypatch.setattr("copilot.api_usage.USAGE_PATH", captured_path)
    monkeypatch.setattr(
        httpx, "Client",
        _mock_client(200, {"x-ratelimit-requests-limit": "200",
                            "x-ratelimit-requests-remaining": "199"},
                     {"data": {"jobs": []}}),
    )

    jsearch.search_jsearch("Software Engineer", "Remote", "fake-key")

    from copilot.api_usage import get_jsearch_quota
    quota = get_jsearch_quota(path=captured_path)
    assert quota is not None
    assert quota.limit == 200
    assert quota.remaining == 199


def test_search_jsearch_records_quota_even_on_429(tmp_path: Path, monkeypatch):
    """Exhaustion (402/429) still updates the captured remaining count -
    that's the moment the number matters most."""
    captured_path = tmp_path / "usage.json"
    monkeypatch.setattr("copilot.api_usage.USAGE_PATH", captured_path)
    monkeypatch.setattr(
        httpx, "Client",
        _mock_client(429, {"x-ratelimit-requests-limit": "200",
                            "x-ratelimit-requests-remaining": "0"},
                     {"message": "quota exceeded"}),
    )

    with pytest.raises(httpx.HTTPStatusError):
        jsearch.search_jsearch("Software Engineer", "Remote", "fake-key")

    from copilot.api_usage import get_jsearch_quota
    quota = get_jsearch_quota(path=captured_path)
    assert quota is not None
    assert quota.remaining == 0


def test_search_jsearch_survives_usage_file_write_failure(monkeypatch):
    """A broken usage-file write must never break discovery itself."""
    monkeypatch.setattr(
        "copilot.api_usage.record_jsearch_quota",
        lambda headers, path=None: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        httpx, "Client",
        _mock_client(200, {"x-ratelimit-requests-limit": "200",
                            "x-ratelimit-requests-remaining": "199"},
                     {"data": {"jobs": []}}),
    )
    jobs = jsearch.search_jsearch("Software Engineer", "Remote", "fake-key")
    assert jobs == []
