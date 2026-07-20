"""Adzuna job search client. Free aggregator, no card required.

See docs/APIS.md for auth, endpoint, and limit details.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from copilot.db.models import SalarySource
from copilot.discovery.ats import detect_ats
from copilot.discovery.models import DiscoveredJob

BASE_URL = "https://api.adzuna.com/v1/api/jobs"
_COUNTRYWIDE_LOCATIONS = {"united states", "usa", "us"}


def _is_countrywide(location: str) -> bool:
    return location.strip().lower() in _COUNTRYWIDE_LOCATIONS


def _looks_remote(*texts: str | None) -> bool | None:
    for text in texts:
        if text and "remote" in text.lower():
            return True
    return None


def _normalize(result: dict, source_query_location: str) -> DiscoveredJob:
    title = result.get("title", "").strip()
    company_name = (result.get("company") or {}).get("display_name", "Unknown Company")
    location_display = (result.get("location") or {}).get("display_name")
    apply_url = result.get("redirect_url", "")
    ats_type, ats_slug = detect_ats(apply_url)

    salary_min = result.get("salary_min")
    salary_max = result.get("salary_max")
    if salary_min or salary_max:
        salary_source = (
            SalarySource.aggregator_estimate
            if result.get("salary_is_predicted") == "1"
            else SalarySource.posted
        )
    else:
        salary_source = SalarySource.unknown

    posted_at = None
    if result.get("created"):
        try:
            posted_at = datetime.fromisoformat(result["created"].replace("Z", "+00:00"))
        except ValueError:
            posted_at = None

    contract_time = result.get("contract_time")  # full_time / part_time
    contract_type = result.get("contract_type")  # permanent / contract
    employment_type = contract_time or contract_type

    return DiscoveredJob(
        title=title,
        company_name=company_name,
        location=location_display or source_query_location,
        latitude=result.get("latitude"),
        longitude=result.get("longitude"),
        remote=_looks_remote(title, location_display),
        employment_type=employment_type,
        salary_min=int(salary_min) if salary_min else None,
        salary_max=int(salary_max) if salary_max else None,
        salary_currency="USD" if (salary_min or salary_max) else None,
        salary_source=salary_source,
        source="adzuna",
        jd_text=result.get("description"),
        apply_url=apply_url,
        posted_at=posted_at,
        ats_type=ats_type,
        ats_slug=ats_slug,
    )


def search_adzuna(
    title: str,
    location: str,
    app_id: str,
    app_key: str,
    *,
    country: str = "us",
    results_per_page: int = 20,
    max_pages: int = 1,
) -> list[DiscoveredJob]:
    """Search Adzuna for a title/location. `location` is passed as Adzuna's
    `where` filter unless it's a countrywide value like "United States", in
    which case the search is left nationwide.
    """
    jobs: list[DiscoveredJob] = []
    with httpx.Client(timeout=20) as client:
        for page in range(1, max_pages + 1):
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "what": title,
                "results_per_page": results_per_page,
                "content-type": "application/json",
            }
            if not _is_countrywide(location):
                params["where"] = location

            resp = client.get(f"{BASE_URL}/{country}/search/{page}", params=params)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            jobs.extend(_normalize(r, location) for r in results)
            if len(results) < results_per_page:
                break
    return jobs
