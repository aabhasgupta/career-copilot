"""Orchestrates discovery: run both sources for every title/location pair in
the profile, normalize, apply hard dealbreaker filters, dedupe, and upsert
into the DB. Salary/location are never filtered out here - only surfaced -
so unknowns stay queryable rather than silently dropped (docs/DECISIONS.md D10).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot.config import Profile
from copilot.db.models import Company, Job
from copilot.discovery.adzuna import search_adzuna
from copilot.discovery.dedupe import dedupe_hash
from copilot.discovery.jsearch import search_jsearch
from copilot.discovery.models import DiscoveredJob


@dataclass
class DiscoverySummary:
    found: int = 0
    added: int = 0
    duplicates: int = 0
    dealbreakers_dropped: int = 0
    errors: list[str] = field(default_factory=list)


def _hits_dealbreaker(discovered: DiscoveredJob, dealbreakers: list[str]) -> bool:
    haystack = f"{discovered.title}\n{discovered.jd_text or ''}".lower()
    return any(d.strip().lower() in haystack for d in dealbreakers if d.strip())


def _get_or_create_company(session: Session, discovered: DiscoveredJob) -> Company:
    company = session.scalar(select(Company).where(Company.name == discovered.company_name))
    if company is None:
        company = Company(name=discovered.company_name)
        session.add(company)
        session.flush()

    if discovered.ats_type.value != "unknown" and not company.watchlisted:
        company.ats_type = discovered.ats_type
        company.ats_slug = discovered.ats_slug
        company.watchlisted = True

    return company


def _upsert_job(session: Session, discovered: DiscoveredJob, company: Company) -> bool:
    """Returns True if a new job row was inserted, False if it was a duplicate."""
    h = dedupe_hash(discovered.company_name, discovered.title, discovered.location)
    existing = session.scalar(select(Job).where(Job.dedupe_hash == h))
    if existing is not None:
        return False

    session.add(
        Job(
            company_id=company.id,
            title=discovered.title,
            location=discovered.location,
            remote=discovered.remote,
            employment_type=discovered.employment_type,
            salary_min=discovered.salary_min,
            salary_max=discovered.salary_max,
            salary_currency=discovered.salary_currency,
            salary_source=discovered.salary_source,
            source=discovered.source,
            jd_text=discovered.jd_text,
            apply_url=discovered.apply_url,
            posted_at=discovered.posted_at,
            dedupe_hash=h,
        )
    )
    return True


def run_discovery(
    profile: Profile,
    session: Session,
    *,
    adzuna_app_id: str | None,
    adzuna_app_key: str | None,
    jsearch_api_key: str | None,
) -> DiscoverySummary:
    summary = DiscoverySummary()

    for title in profile.search.titles:
        for location in profile.search.locations:
            discovered_jobs: list[DiscoveredJob] = []

            if adzuna_app_id and adzuna_app_key:
                try:
                    discovered_jobs += search_adzuna(title, location, adzuna_app_id, adzuna_app_key)
                except Exception as exc:  # noqa: BLE001 - one source failing shouldn't kill the run
                    summary.errors.append(f"adzuna[{title} / {location}]: {exc}")

            if jsearch_api_key:
                try:
                    discovered_jobs += search_jsearch(title, location, jsearch_api_key)
                except Exception as exc:  # noqa: BLE001
                    summary.errors.append(f"jsearch[{title} / {location}]: {exc}")

            for discovered in discovered_jobs:
                summary.found += 1
                if _hits_dealbreaker(discovered, profile.search.dealbreakers):
                    summary.dealbreakers_dropped += 1
                    continue

                company = _get_or_create_company(session, discovered)
                if _upsert_job(session, discovered, company):
                    summary.added += 1
                else:
                    summary.duplicates += 1

    session.commit()
    return summary
