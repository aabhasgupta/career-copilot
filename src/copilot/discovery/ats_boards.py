"""Fetch postings from a company's public ATS job board (Greenhouse, Lever,
or Ashby). These are the direct employer boards: apply URLs go straight to
the company's own application form, and JD text is complete rather than the
truncated snippets aggregators return.

Field names verified live against real boards on 2026-07-19; see docs/APIS.md.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel

from copilot.db.models import ATSType


class BoardPosting(BaseModel):
    title: str
    location: str | None = None
    remote: bool | None = None
    apply_url: str
    jd_text: str | None = None
    posted_at: datetime | None = None


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip() or None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _looks_remote(location: str | None) -> bool | None:
    if location and "remote" in location.lower():
        return True
    return None


def _fetch_greenhouse(slug: str, client: httpx.Client) -> list[BoardPosting]:
    resp = client.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        params={"content": "true"},
    )
    resp.raise_for_status()
    postings = []
    for job in resp.json().get("jobs", []):
        location = (job.get("location") or {}).get("name")
        postings.append(
            BoardPosting(
                title=job.get("title", "").strip(),
                location=location,
                remote=_looks_remote(location),
                apply_url=job.get("absolute_url", ""),
                jd_text=_strip_html(job.get("content")),
                posted_at=_parse_iso(job.get("first_published") or job.get("updated_at")),
            )
        )
    return postings


def _fetch_lever(slug: str, client: httpx.Client) -> list[BoardPosting]:
    resp = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    resp.raise_for_status()
    postings = []
    for job in resp.json():
        location = (job.get("categories") or {}).get("location")
        created_ms = job.get("createdAt")
        posted_at = (
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) if created_ms else None
        )
        postings.append(
            BoardPosting(
                title=job.get("text", "").strip(),
                location=location,
                remote=_looks_remote(location) or (job.get("workplaceType") == "remote" or None),
                apply_url=job.get("hostedUrl") or job.get("applyUrl", ""),
                jd_text=job.get("descriptionPlain") or _strip_html(job.get("description")),
                posted_at=posted_at,
            )
        )
    return postings


def _fetch_ashby(slug: str, client: httpx.Client) -> list[BoardPosting]:
    resp = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    resp.raise_for_status()
    postings = []
    for job in resp.json().get("jobs", []):
        if not job.get("isListed", True):
            continue
        location = job.get("location")
        postings.append(
            BoardPosting(
                title=job.get("title", "").strip(),
                location=location,
                remote=job.get("isRemote") or _looks_remote(location),
                apply_url=job.get("jobUrl") or job.get("applyUrl", ""),
                jd_text=job.get("descriptionPlain") or _strip_html(job.get("descriptionHtml")),
                posted_at=_parse_iso(job.get("publishedAt")),
            )
        )
    return postings


_FETCHERS = {
    ATSType.greenhouse: _fetch_greenhouse,
    ATSType.lever: _fetch_lever,
    ATSType.ashby: _fetch_ashby,
}


def fetch_board_postings(
    ats_type: ATSType, slug: str, client: httpx.Client
) -> list[BoardPosting]:
    fetcher = _FETCHERS.get(ats_type)
    if fetcher is None:
        raise ValueError(f"No board fetcher for ATS type {ats_type!r}")
    return fetcher(slug, client)
