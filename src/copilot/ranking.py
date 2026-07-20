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

from copilot.db.models import Job
from copilot.geocode import Geocoder

_RADIUS_RE = re.compile(r"^within\s+(\d+)\s+miles?\s+of\s+(.+)$", re.IGNORECASE)
_EARTH_RADIUS_MILES = 3958.8


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
