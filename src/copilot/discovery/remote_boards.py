"""Remote-first job boards: Remotive and RemoteOK.

Both are free, keyless APIs covering remote tech jobs - the user's top
location preference - and often carry postings that lag on the big
aggregators. Neither is location-scoped, so they run per title (Remotive)
or once per run (RemoteOK), not per title x location.

Courtesy notes (see docs/APIS.md): Remotive asks for light usage, so calls
are spaced out; RemoteOK requires a descriptive User-Agent.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import httpx

from copilot.db.models import SalarySource
from copilot.discovery.matching import matches_search_titles
from copilot.discovery.models import DiscoveredJob

_USER_AGENT = "career-copilot/0.1 (https://github.com/aabhasgupta/career-copilot)"

# The user is US-based: skip postings explicitly restricted to elsewhere.
_ELIGIBLE_HINTS = (
    "usa",
    "united states",
    ", us",
    "us only",
    "worldwide",
    "anywhere",
    "americas",
    "north america",
)


def _us_eligible(location: str | None) -> bool:
    if not location or not location.strip():
        return True  # unrestricted remote
    loc = location.lower()
    return any(hint in loc for hint in _ELIGIBLE_HINTS)


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip() or None


def _parse_salary_text(salary: str | None) -> tuple[int | None, int | None]:
    """Remotive salaries are free text: "$36k", "$120k-$150k", "$120,000"."""
    if not salary:
        return None, None
    amounts = []
    for match in re.finditer(r"\$?([\d,]+)\s*(k?)", salary.lower()):
        digits = match.group(1).replace(",", "")
        if not digits.isdigit():
            continue
        value = int(digits) * (1000 if match.group(2) == "k" else 1)
        if value >= 10000:  # ignore stray small numbers ("401k" is caught below)
            amounts.append(value)
    if "401k" in salary.lower().replace(" ", ""):
        return None, None
    if not amounts:
        return None, None
    return min(amounts), max(amounts)


def _normalize_remotive(job: dict) -> DiscoveredJob:
    salary_min, salary_max = _parse_salary_text(job.get("salary"))
    posted_at = None
    if job.get("publication_date"):
        try:
            posted_at = datetime.fromisoformat(job["publication_date"])
        except ValueError:
            posted_at = None
    return DiscoveredJob(
        title=(job.get("title") or "").strip(),
        company_name=job.get("company_name") or "Unknown Company",
        location=job.get("candidate_required_location") or "Remote",
        remote=True,
        employment_type=job.get("job_type"),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency="USD" if salary_min else None,
        salary_source=SalarySource.posted if salary_min else SalarySource.unknown,
        source="remotive",
        jd_text=_strip_html(job.get("description")),
        apply_url=job.get("url") or "",
        posted_at=posted_at,
    )


def _normalize_remoteok(job: dict) -> DiscoveredJob:
    salary_min = job.get("salary_min") or None  # 0 means unknown
    salary_max = job.get("salary_max") or None
    posted_at = None
    if job.get("epoch"):
        posted_at = datetime.fromtimestamp(job["epoch"], tz=timezone.utc)
    return DiscoveredJob(
        title=(job.get("position") or "").strip(),
        company_name=job.get("company") or "Unknown Company",
        location=job.get("location") or "Remote",
        remote=True,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency="USD" if salary_min or salary_max else None,
        salary_source=SalarySource.posted if salary_min or salary_max else SalarySource.unknown,
        source="remoteok",
        jd_text=_strip_html(job.get("description")),
        apply_url=job.get("apply_url") or job.get("url") or "",
        posted_at=posted_at,
    )


def search_remotive(titles: list[str]) -> list[DiscoveredJob]:
    """One search call per title, spaced out per Remotive's light-usage ask.

    Remotive's `search` matches descriptions too (live testing returned
    "Freelance Writer" for GenAI queries), so results are re-filtered locally
    by title - same standard RemoteOK gets."""
    jobs: list[DiscoveredJob] = []
    with httpx.Client(timeout=20, headers={"User-Agent": _USER_AGENT}) as client:
        for i, title in enumerate(titles):
            if i:
                time.sleep(0.6)
            resp = client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": title, "limit": 20},
            )
            resp.raise_for_status()
            for job in resp.json().get("jobs", []):
                if not matches_search_titles(job.get("title", ""), titles):
                    continue
                if _us_eligible(job.get("candidate_required_location")):
                    jobs.append(_normalize_remotive(job))
    return jobs


def fetch_remoteok(search_titles: list[str]) -> list[DiscoveredJob]:
    """One call returns RemoteOK's whole active board (~100 jobs, first item
    is a legal notice); filter locally against the profile's titles."""
    with httpx.Client(timeout=25, headers={"User-Agent": _USER_AGENT}) as client:
        resp = client.get("https://remoteok.com/api")
        resp.raise_for_status()
        items = resp.json()

    jobs = []
    for job in items:
        if not isinstance(job, dict) or "position" not in job:
            continue  # legal-notice header entry
        if not matches_search_titles(job.get("position", ""), search_titles):
            continue
        if not _us_eligible(job.get("location")):
            continue
        jobs.append(_normalize_remoteok(job))
    return jobs
