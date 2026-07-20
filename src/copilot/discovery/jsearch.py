"""JSearch (RapidAPI) job search client. Closes the LinkedIn/Indeed coverage
gap Adzuna alone doesn't have, since it indexes Google for Jobs.

See docs/APIS.md for auth, the `/search-v2` endpoint shape, and rate limits.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from copilot.db.models import SalarySource
from copilot.discovery.ats import detect_ats
from copilot.discovery.models import DiscoveredJob

BASE_URL = "https://jsearch.p.rapidapi.com/search-v2"
HOST = "jsearch.p.rapidapi.com"


def _normalize(job: dict) -> DiscoveredJob:
    apply_url = job.get("job_apply_link") or ""
    ats_type, ats_slug = detect_ats(apply_url)

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
        posted_at=posted_at,
        ats_type=ats_type,
        ats_slug=ats_slug,
    )


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
        resp.raise_for_status()
        data = resp.json()

    jobs = data.get("data", {}).get("jobs", [])
    return [_normalize(j) for j in jobs]
