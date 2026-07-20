"""Orchestrates discovery: run both sources for every title/location pair in
the profile, normalize, apply hard dealbreaker filters, dedupe, and upsert
into the DB. Salary/location are never filtered out here - only surfaced -
so unknowns stay queryable rather than silently dropped (docs/DECISIONS.md D10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot.config import Profile
from copilot.db.models import ATSType, Company, Job
from copilot.discovery.adzuna import search_adzuna
from copilot.discovery.ats_boards import BoardPosting, fetch_board_postings
from copilot.discovery.ats_resolver import resolve_company_ats
from copilot.discovery.dedupe import dedupe_hash
from copilot.discovery.jsearch import search_jsearch
from copilot.discovery.models import DiscoveredJob

_REAL_ATS_TYPES = (ATSType.greenhouse, ATSType.lever, ATSType.ashby)


@dataclass
class DiscoverySummary:
    found: int = 0
    added: int = 0
    duplicates: int = 0
    dealbreakers_dropped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ATSSummary:
    companies_probed: int = 0
    boards_found: int = 0
    boards_polled: int = 0
    links_upgraded: int = 0
    board_jobs_added: int = 0
    errors: list[str] = field(default_factory=list)


def _hits_dealbreaker(discovered: DiscoveredJob, rules: list) -> bool:
    from copilot.rules import job_matches_rules

    return job_matches_rules(
        rules,
        title=discovered.title,
        jd_text=discovered.jd_text,
        location=discovered.location,
        company=discovered.company_name,
    )


def _below_salary_floor(
    salary_min: int | None, salary_max: int | None, floor: int | None
) -> bool:
    """True only when the salary is known and its best case is under the
    floor. Unknown salary never trips this (D10)."""
    if floor is None:
        return False
    best_known = salary_max or salary_min
    return best_known is not None and best_known < floor


def _get_or_create_company(session: Session, discovered: DiscoveredJob) -> Company:
    company = session.scalar(select(Company).where(Company.name == discovered.company_name))
    if company is None:
        company = Company(name=discovered.company_name)
        session.add(company)
        session.flush()

    if discovered.ats_type in _REAL_ATS_TYPES and not company.watchlisted:
        company.ats_type = discovered.ats_type
        company.ats_slug = discovered.ats_slug
        company.watchlisted = True

    return company


def _upsert_job(session: Session, discovered: DiscoveredJob, company: Company) -> bool:
    """Returns True if a new job row was inserted, False if it was a duplicate."""
    h = dedupe_hash(discovered.company_name, discovered.title, discovered.location)
    existing = session.scalar(select(Job).where(Job.dedupe_hash == h))
    if existing is not None:
        # The other source may know things this row is missing (e.g. Adzuna
        # has coordinates and salary estimates JSearch doesn't).
        if existing.latitude is None and discovered.latitude is not None:
            existing.latitude = discovered.latitude
            existing.longitude = discovered.longitude
        if existing.salary_min is None and discovered.salary_min is not None:
            existing.salary_min = discovered.salary_min
            existing.salary_max = discovered.salary_max
            existing.salary_currency = discovered.salary_currency
            existing.salary_source = discovered.salary_source
        if existing.posted_at is None and discovered.posted_at is not None:
            existing.posted_at = discovered.posted_at
        return False

    session.add(
        Job(
            company_id=company.id,
            title=discovered.title,
            location=discovered.location,
            latitude=discovered.latitude,
            longitude=discovered.longitude,
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
    from copilot.rules import load_dealbreaker_rules

    summary = DiscoverySummary()
    rules, rule_errors = load_dealbreaker_rules(profile)
    summary.errors.extend(rule_errors)

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
                if _hits_dealbreaker(discovered, rules) or _below_salary_floor(
                    discovered.salary_min, discovered.salary_max, profile.search.min_salary
                ):
                    summary.dealbreakers_dropped += 1
                    continue

                company = _get_or_create_company(session, discovered)
                if _upsert_job(session, discovered, company):
                    summary.added += 1
                else:
                    summary.duplicates += 1

    session.commit()
    return summary


def classify_new_companies(profile: Profile, session: Session) -> int:
    """Classify the industry of companies that don't have one yet - a single
    batched LLM call per run, and none at all when there's nothing new.
    Returns the number of companies classified."""
    from copilot.industry import classify_companies

    unclassified = session.scalars(
        select(Company).where(Company.industry.is_(None))
    ).all()
    if not unclassified:
        return 0

    with_context = []
    for company in unclassified:
        sample = session.scalar(select(Job.title).where(Job.company_id == company.id))
        with_context.append((company.name, sample))

    labels = classify_companies(profile, with_context)
    classified = 0
    for company in unclassified:
        label = labels.get(company.name)
        if label:
            company.industry = label
            classified += 1
    session.commit()
    return classified


@dataclass
class PruneSummary:
    dealbreakers: int = 0
    below_salary_floor: int = 0
    errors: list[str] = field(default_factory=list)


def prune_jobs(profile: Profile, session: Session) -> PruneSummary:
    """Re-apply the profile's hard filters (dealbreakers, salary floor) to
    jobs already stored, deleting violators. This is how rule changes take
    effect retroactively without re-calling any discovery API - everything
    ever discovered is already in the DB, so the DB itself is the cache to
    re-derive from. Jobs with an application on file are never pruned."""
    from copilot.rules import job_matches_rules, load_dealbreaker_rules

    summary = PruneSummary()
    rules, rule_errors = load_dealbreaker_rules(profile)
    summary.errors.extend(rule_errors)
    for job in session.scalars(select(Job)).all():
        if job.application is not None:
            continue
        if job_matches_rules(
            rules,
            title=job.title,
            jd_text=job.jd_text,
            location=job.location,
            company=job.company.name if job.company else None,
        ):
            summary.dealbreakers += 1
            session.delete(job)
        elif _below_salary_floor(job.salary_min, job.salary_max, profile.search.min_salary):
            summary.below_salary_floor += 1
            session.delete(job)
    session.commit()
    return summary


def geocode_missing_coordinates(session: Session) -> int:
    """Fill in coordinates for jobs that have a location string but no
    lat/long (JSearch never provides them; Adzuna does). Cached lookups make
    this nearly free after the first run - only never-before-seen places hit
    the geocoder. Returns the number of jobs updated."""
    from copilot.geocode import Geocoder

    geocoder = Geocoder()
    updated = 0
    jobs = session.scalars(
        select(Job).where(Job.latitude.is_(None), Job.location.is_not(None))
    ).all()
    for job in jobs:
        coords = geocoder.lookup(job.location)
        if coords:
            job.latitude, job.longitude = coords
            updated += 1
    session.commit()
    return updated


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", title.lower()).strip()


def _titles_equal(a: str, b: str) -> bool:
    na, nb = _normalize_title(a), _normalize_title(b)
    return bool(na) and na == nb


def _titles_match(a: str, b: str) -> bool:
    na, nb = _normalize_title(a), _normalize_title(b)
    return bool(na and nb) and (na == nb or na in nb or nb in na)


def _matches_search_titles(posting_title: str, search_titles: list[str]) -> bool:
    return any(_titles_match(posting_title, t) for t in search_titles)


def _pick_posting_for_job(
    candidates: list[BoardPosting], job_location: str | None
) -> BoardPosting | None:
    """A board can list the same title in several locations. Only upgrade a
    job when we can name its posting unambiguously - a single candidate, or
    one whose location matches the job's. Guessing here would point the user
    at the wrong location's application form."""
    if len(candidates) == 1:
        return candidates[0]
    if job_location:
        city = job_location.split(",")[0].strip().lower()
        located = [c for c in candidates if c.location and city in c.location.lower()]
        if len(located) == 1:
            return located[0]
    return None


def _poll_board(
    session: Session,
    company: Company,
    postings: list[BoardPosting],
    profile: Profile,
    summary: ATSSummary,
    rules: list,
) -> None:
    # Exact title match only: containment would let one generic aggregator
    # title ("AI Engineer") soak up direct links from many board postings.
    by_title: dict[str, list[BoardPosting]] = {}
    for posting in postings:
        by_title.setdefault(_normalize_title(posting.title), []).append(posting)

    matched: set[int] = set()
    jobs = session.scalars(select(Job).where(Job.company_id == company.id)).all()
    for job in jobs:
        candidates = by_title.get(_normalize_title(job.title), [])
        posting = _pick_posting_for_job(candidates, job.location)
        if posting is None:
            continue
        matched.add(id(posting))
        changed = False
        if posting.apply_url and job.apply_url != posting.apply_url:
            job.apply_url = posting.apply_url
            changed = True
        if posting.jd_text and len(posting.jd_text) > len(job.jd_text or ""):
            job.jd_text = posting.jd_text
            changed = True
        if posting.posted_at and job.posted_at is None:
            job.posted_at = posting.posted_at
            changed = True
        if changed:
            summary.links_upgraded += 1

    for posting in postings:
        if id(posting) in matched:
            continue
        if not _matches_search_titles(posting.title, profile.search.titles):
            continue
        discovered = DiscoveredJob(
            title=posting.title,
            company_name=company.name,
            location=posting.location,
            remote=posting.remote,
            source=company.ats_type.value,
            jd_text=posting.jd_text,
            apply_url=posting.apply_url,
            posted_at=posting.posted_at,
            ats_type=company.ats_type,
            ats_slug=company.ats_slug,
        )
        if _hits_dealbreaker(discovered, rules) or _below_salary_floor(
            discovered.salary_min, discovered.salary_max, profile.search.min_salary
        ):
            continue
        if _upsert_job(session, discovered, company):
            summary.board_jobs_added += 1


def resolve_and_poll_ats(profile: Profile, session: Session) -> ATSSummary:
    """Two passes over the companies table:

    1. Probe: for every company not yet checked (ats_type == unknown), ask the
       three public ATS APIs whether it hosts a board there. Found -> record
       type+slug and watchlist; not found -> mark `none` so it isn't re-probed
       every run.
    2. Poll: fetch every watchlisted board's postings. Postings matching a job
       we already have upgrade it in place (direct apply URL, full JD text);
       postings matching the profile's search titles are added as new jobs.
    """
    from copilot.rules import load_dealbreaker_rules

    summary = ATSSummary()
    rules, rule_errors = load_dealbreaker_rules(profile)
    summary.errors.extend(rule_errors)

    with httpx.Client(timeout=15) as client:
        for company in session.scalars(
            select(Company).where(Company.ats_type == ATSType.unknown)
        ).all():
            summary.companies_probed += 1
            try:
                result = resolve_company_ats(company.name, client)
            except Exception as exc:  # noqa: BLE001 - one company failing shouldn't kill the run
                summary.errors.append(f"probe[{company.name}]: {exc}")
                continue
            if result:
                company.ats_type, company.ats_slug = result
                company.watchlisted = True
                summary.boards_found += 1
            else:
                company.ats_type = ATSType.none
        session.flush()

        for company in session.scalars(
            select(Company).where(Company.watchlisted, Company.ats_type.in_(_REAL_ATS_TYPES))
        ).all():
            if not company.ats_slug:
                continue
            summary.boards_polled += 1
            try:
                postings = fetch_board_postings(company.ats_type, company.ats_slug, client)
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"board[{company.name}]: {exc}")
                continue
            _poll_board(session, company, postings, profile, summary, rules)

    session.commit()
    return summary
