"""JSearch (RapidAPI) job search client. Closes the LinkedIn/Indeed coverage
gap Adzuna alone doesn't have, since it indexes Google for Jobs.

See docs/APIS.md for auth, the `/search-v2` endpoint shape, and rate limits.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

import httpx

from copilot.db.models import SalarySource
from copilot.discovery.ats import detect_ats
from copilot.discovery.models import DiscoveredJob

BASE_URL = "https://jsearch.p.rapidapi.com/search-v2"
HOST = "jsearch.p.rapidapi.com"

# job_apply_is_direct is sometimes absent even when the domain itself is a
# known metasearch/republisher that chains through further redirects rather
# than landing on an application - confirmed live (docs/DECISIONS.md D51):
# a bebee.com result had no is_direct flag at all and got no "indirect"
# badge as a result. Deliberately narrow - legitimate single-hop boards
# (LinkedIn, Indeed, Dice, ZipRecruiter, Talent, Monster, CareerBuilder)
# are excluded, since the complaint this targets is specifically "clicked
# through to a third party, which itself redirects to another third
# party," not "didn't land on the employer's own domain."
_KNOWN_INDIRECT_DOMAINS = {
    "jobleads.com", "trabajo.org", "learn4good.com", "bebee.com",
    "careerboard.com", "jobmesh.io", "jobrapido.com", "computerjobs.com",
    "iitjobs.com",
}


def _looks_indirect_by_domain(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return any(netloc == d or netloc.endswith(f".{d}") for d in _KNOWN_INDIRECT_DOMAINS)


def _normalize(job: dict) -> DiscoveredJob:
    apply_url = job.get("job_apply_link") or ""
    ats_type, ats_slug = detect_ats(apply_url)
    apply_is_direct = job.get("job_apply_is_direct")
    if apply_is_direct is None and apply_url and _looks_indirect_by_domain(apply_url):
        apply_is_direct = False

    salary_min = job.get("job_min_salary")
    salary_max = job.get("job_max_salary")
    salary_source = SalarySource.posted if (salary_min or salary_max) else SalarySource.unknown

    posted_at = None
    if job.get("job_posted_at_datetime_utc"):
        try:
            posted_at = datetime.fromisoformat(
                job["job_posted_at_datetime_utc"].replace("Z", "+00:00")
            )
        except ValueError:
            posted_at = None

    return DiscoveredJob(
        title=(job.get("job_title") or "").strip(),
        company_name=job.get("employer_name") or "Unknown Company",
        location=job.get("job_location"),
        remote=job.get("job_is_remote"),
        employment_type=job.get("job_employment_type"),
        salary_min=int(salary_min) if salary_min else None,
        salary_max=int(salary_max) if salary_max else None,
        salary_currency=job.get("job_salary_currency") or ("USD" if salary_min or salary_max else None),
        salary_source=salary_source,
        source="jsearch",
        jd_text=job.get("job_description"),
        apply_url=apply_url,
        apply_is_direct=apply_is_direct,
        posted_at=posted_at,
        ats_type=ats_type,
        ats_slug=ats_slug,
    )


def _record_quota(headers) -> None:
    """Capture RapidAPI's rate-limit headers opportunistically (docs/APIS.md,
    D33) - best-effort, must never break discovery over a usage-file hiccup."""
    try:
        from copilot.api_usage import record_jsearch_quota

        record_jsearch_quota(headers)
    except Exception:  # noqa: BLE001
        pass


def search_jsearch(
    title: str,
    location: str,
    api_key: str,
    *,
    country: str = "us",
    num_pages: int = 1,
    date_posted: str = "week",
) -> list[DiscoveredJob]:
    """Search JSearch for a title/location using natural-language query text,
    matching Google for Jobs' own search style.
    """
    query = f"{title} in {location}" if location else title
    with httpx.Client(timeout=20) as client:
        resp = client.get(
            BASE_URL,
            params={
                "query": query,
                "num_pages": num_pages,
                "country": country,
                "date_posted": date_posted,
            },
            headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": HOST},
        )
        # Read headers before raise_for_status so a 429 quota-exhausted
        # response still updates the captured "remaining" count.
        _record_quota(resp.headers)
        resp.raise_for_status()
        data = resp.json()

    jobs = data.get("data", {}).get("jobs", [])
    return [_normalize(j) for j in jobs]
