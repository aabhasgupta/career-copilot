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
from copilot.discovery.matching import matches_search_titles

# Workday's Cloudflare-fronted CXS endpoint 429s without a browser-shaped
# User-Agent (confirmed live 2026-11-17) - it's still unauthenticated and
# read-only, the same data the public careers page itself fetches, just
# gated behind basic bot-detection rather than Greenhouse/Lever/Ashby/
# SmartRecruiters' fully open APIs (docs/DECISIONS.md D45).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


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


def _fetch_smartrecruiters(
    slug: str, client: httpx.Client, title_filter: list[str] | None
) -> list[BoardPosting]:
    """Unlike Greenhouse/Lever/Ashby, SmartRecruiters' list endpoint doesn't
    include full JD text - a second call per posting is needed for that, so
    a company with thousands of postings (AbbVie: 2,043) would be far too
    expensive to fully detail-fetch every run. title_filter (the profile's
    search titles plus this company's already-tracked job titles) bounds the
    list to postings worth the extra call; None means no bound (used by
    tests / callers that already know they want everything)."""
    postings: list[BoardPosting] = []
    offset = 0
    while True:
        resp = client.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            params={"limit": 100, "offset": offset},
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        if not content:
            break
        for item in content:
            title = item.get("name", "")
            if title_filter is not None and not matches_search_titles(title, title_filter):
                continue
            detail = client.get(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{item['id']}"
            )
            if detail.status_code != 200:
                continue
            d = detail.json()
            location = d.get("location") or {}
            loc_str = location.get("fullLocation") or location.get("city")
            sections = (d.get("jobAd") or {}).get("sections") or {}
            jd_text = " ".join(
                _strip_html(s.get("text")) or "" for s in sections.values() if isinstance(s, dict)
            ).strip() or None
            postings.append(
                BoardPosting(
                    title=title,
                    location=loc_str,
                    remote=location.get("remote"),
                    apply_url=d.get("postingUrl") or d.get("applyUrl", ""),
                    jd_text=jd_text,
                    posted_at=_parse_iso(d.get("releasedDate")),
                )
            )
        offset += 100
        if offset >= data.get("totalFound", 0) or offset >= 500:
            break
    return postings


def _fetch_workday(
    slug: str, client: httpx.Client, title_filter: list[str] | None
) -> list[BoardPosting]:
    """slug is "tenant:site" (see discovery/ats.py). Same two-call shape as
    SmartRecruiters: the list endpoint gives title/location/a relative
    "Posted Today"-style string only, full JD + an actual date need a second
    call per posting - title_filter bounds that."""
    tenant, _, site = slug.partition(":")
    postings: list[BoardPosting] = []
    offset = 0
    while True:
        resp = client.post(
            f"https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
            headers=_BROWSER_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        jobs = data.get("jobPostings", [])
        if not jobs:
            break
        for item in jobs:
            title = item.get("title", "")
            if title_filter is not None and not matches_search_titles(title, title_filter):
                continue
            detail = client.get(
                f"https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
                f"{item['externalPath']}",
                headers=_BROWSER_HEADERS,
            )
            if detail.status_code != 200:
                continue
            jpi = detail.json().get("jobPostingInfo", {})
            postings.append(
                BoardPosting(
                    title=jpi.get("title", title),
                    location=jpi.get("location") or item.get("locationsText"),
                    remote=(jpi.get("remoteType") or "").lower() in ("remote", "fully remote"),
                    apply_url=jpi.get("externalUrl", ""),
                    jd_text=_strip_html(jpi.get("jobDescription")),
                    posted_at=_parse_iso(jpi.get("startDate")),
                )
            )
        offset += 20
        if offset >= data.get("total", 0) or offset >= 500:
            break
    return postings


_FETCHERS = {
    ATSType.greenhouse: lambda slug, client, _title_filter: _fetch_greenhouse(slug, client),
    ATSType.lever: lambda slug, client, _title_filter: _fetch_lever(slug, client),
    ATSType.ashby: lambda slug, client, _title_filter: _fetch_ashby(slug, client),
    ATSType.smartrecruiters: _fetch_smartrecruiters,
    ATSType.workday: _fetch_workday,
}


def fetch_board_postings(
    ats_type: ATSType, slug: str, client: httpx.Client, title_filter: list[str] | None = None
) -> list[BoardPosting]:
    fetcher = _FETCHERS.get(ats_type)
    if fetcher is None:
        raise ValueError(f"No board fetcher for ATS type {ats_type!r}")
    return fetcher(slug, client, title_filter)
