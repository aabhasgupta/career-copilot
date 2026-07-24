"""Preference-based ordering of job listings.

Preferences reorder, filters drop - that's the line (docs/DECISIONS.md D12).
`search.location_preference` in profile.yaml is an ordered list, most
preferred first; each job gets the index of the first entry it matches
(unmatched jobs sort last, but are never hidden).

Entry forms:
- "remote"                        - matches remote jobs
- "within 30 miles of Place, ST"  - haversine distance against job coordinates
- anything else                   - case-insensitive substring of job location
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from copilot.config import Profile
from copilot.db.models import Job
from copilot.formatting import posting_age_days
from copilot.geocode import Geocoder

_RADIUS_RE = re.compile(r"^within\s+(\d+)\s+miles?\s+of\s+(.+)$", re.IGNORECASE)
_EARTH_RADIUS_MILES = 3958.8

# Coarse recency bucket used as a dominant-ish ranking signal (not just a
# last-resort tiebreak) - "important factor" per the user, not blended into
# fit_score. Separate from search.max_posting_age_days, which drops jobs
# outright once they're stale; this just groups the freshest ones first
# among what's still shown. Unknown posted_at is never assumed fresh.
FRESH_WINDOW_DAYS = 14


def freshness_tier(job: Job) -> int:
    age = posting_age_days(job)
    return 0 if age is not None and age <= FRESH_WINDOW_DAYS else 1


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


@dataclass
class _Rule:
    label: str

    def matches(self, job: Job) -> bool:
        raise NotImplementedError


class _RemoteRule(_Rule):
    def matches(self, job: Job) -> bool:
        return bool(job.remote) or "remote" in (job.location or "").lower()


@dataclass
class _RadiusRule(_Rule):
    center: tuple[float, float] | None
    miles: float

    def matches(self, job: Job) -> bool:
        if self.center is None or job.latitude is None or job.longitude is None:
            return False
        distance = haversine_miles(
            self.center[0], self.center[1], job.latitude, job.longitude
        )
        return distance <= self.miles


@dataclass
class _TextRule(_Rule):
    needle: str

    def matches(self, job: Job) -> bool:
        # Sources format the same place differently ("Chicago, IL" vs Adzuna's
        # "Chicago, Cook County"), so fall back to the city part alone. For
        # precision over ambiguous city names, use a radius rule instead.
        haystack = (job.location or "").lower()
        if self.needle in haystack:
            return True
        city = self.needle.split(",")[0].strip()
        return bool(city) and city in haystack


def build_rules(location_preference: list[str], geocoder: Geocoder) -> list[_Rule]:
    rules: list[_Rule] = []
    for entry in location_preference:
        entry = entry.strip()
        if entry.lower() == "remote":
            rules.append(_RemoteRule(label="remote"))
            continue
        radius = _RADIUS_RE.match(entry)
        if radius:
            miles, place = float(radius.group(1)), radius.group(2).strip()
            rules.append(
                _RadiusRule(label=f"{miles:g}mi of {place}", center=geocoder.lookup(place), miles=miles)
            )
            continue
        rules.append(_TextRule(label=entry, needle=entry.lower()))
    return rules


def preference_tier(job: Job, rules: list[_Rule]) -> int:
    """Index of the first matching preference; len(rules) if none match."""
    for i, rule in enumerate(rules):
        if rule.matches(job):
            return i
    return len(rules)


def tier_label(tier: int, rules: list[_Rule]) -> str:
    return rules[tier].label if tier < len(rules) else "-"


def industry_tier(
    job: Job, industry_preference: list[str], deprioritize_staffing: bool = True
) -> int:
    """Index of the job's company industry in the preference list; len(list)
    if unknown or unpreferred. Staffing agencies sort one tier below even
    that (direct employer postings are preferred as a general rule) unless
    "staffing" is explicitly listed as a preference."""
    industry = (job.company.industry or "").lower() if job.company else ""
    for i, entry in enumerate(industry_preference):
        if entry.strip().lower() == industry:
            return i
    if deprioritize_staffing and industry == "staffing":
        return len(industry_preference) + 1
    return len(industry_preference)


def _company_words(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def company_tier(job: Job, company_preference: list[str]) -> int:
    """Index of the first preference entry naming the job's company;
    len(list) if none. Whole-word matching so "Stripe" doesn't catch
    "Stripes Group"."""
    name = _company_words(job.company.name) if job.company else ""
    for i, entry in enumerate(company_preference):
        pattern = r"\b" + re.escape(_company_words(entry)) + r"\b"
        if re.search(pattern, name):
            return i
    return len(company_preference)


def industry_label(tier: int, industry_preference: list[str]) -> str:
    if tier < len(industry_preference):
        return industry_preference[tier]
    if tier == len(industry_preference):
        return "-"
    return "staffing↓"


def rank_jobs(jobs: list[Job], profile: Profile, geocoder: Geocoder) -> list[Job]:
    """The single ranking used everywhere jobs are listed (CLI and dashboard),
    so the two never drift apart. Scored jobs rank first, by fit_score
    descending - the model's holistic judgment is the strongest signal once it
    exists. Unscored jobs (fit_score is None) all tie on those first two keys
    and fall back to the pre-Phase-2 ranking: staffing-agency jobs sort after
    direct employers (dominant rule, not blended), then a coarse freshness
    tier (postings within FRESH_WINDOW_DAYS group ahead of older ones - a
    real ranking factor, not just a tiebreak), then the location/industry/
    company preference blend, then salary, then fine-grained recency - by the
    posting's own posted_at (when the source says the job went live), not our
    created_at (when we happened to discover it); unknown posted_at sorts
    after known, never assumed to be the newest."""
    rules = build_rules(profile.search.location_preference, geocoder)
    industries = profile.search.industry_preference
    companies = profile.search.company_preference
    downrank_staffing = profile.search.deprioritize_staffing

    def ind_tier(j: Job) -> int:
        return industry_tier(j, industries, downrank_staffing)

    return sorted(
        jobs,
        key=lambda j: (
            j.fit_score is None,
            -(j.fit_score or 0),
            ind_tier(j) > len(industries),
            freshness_tier(j),
            preference_tier(j, rules) + min(ind_tier(j), len(industries)) + company_tier(j, companies),
            preference_tier(j, rules),
            -(j.salary_max or j.salary_min or 0),
            j.posted_at is None,
            -(j.posted_at.timestamp() if j.posted_at else 0),
        ),
    )
