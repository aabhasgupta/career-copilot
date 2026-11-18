"""Resolve a company name to a public ATS job board by probing the free
Greenhouse/Lever/Ashby APIs with slug candidates guessed from the name.

This inverts the original D3 mechanism (sniff apply URLs for ATS domains),
which turned out to have no signal in practice - aggregator apply links never
expose the employer's own URL (see docs/DECISIONS.md D3). Probing costs
nothing: all three APIs are public and unauthenticated.
"""

from __future__ import annotations

import re

import httpx

from copilot.db.models import ATSType

_LEGAL_SUFFIXES = {
    "inc",
    "llc",
    "ltd",
    "corp",
    "corporation",
    "co",
    "company",
    "group",
    "holdings",
}


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]


def _dedupe_valid(candidates: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for c in candidates:
        if len(c) >= 2 and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def slug_candidates(name: str) -> list[str]:
    """Slug guesses derived from the company's full name. Safe to trust even
    without independent confirmation, since a collision would require another
    company with essentially the same name."""
    words = _words(name)
    trimmed = [w for w in words if w not in _LEGAL_SUFFIXES] or words
    candidates = ["".join(trimmed)]
    if len(trimmed) > 1:
        candidates.append("-".join(trimmed))
    return _dedupe_valid(candidates)


def loose_slug_candidates(name: str) -> list[str]:
    """First-word-only guesses (e.g. "Inabia Solutions and Consulting, Inc."
    -> "inabia"). High collision risk - "Capital One" would match any board
    called "capital" - so these may only be used where the ATS response lets
    us verify the company name (Greenhouse does, Lever/Ashby don't)."""
    words = _words(name)
    trimmed = [w for w in words if w not in _LEGAL_SUFFIXES] or words
    if len(trimmed) <= 1:
        return []
    return _dedupe_valid([trimmed[0]])


def _names_match(company_name: str, board_name: str) -> bool:
    a, b = _letters(company_name), _letters(board_name)
    return bool(a and b) and (a in b or b in a)


def _probe_greenhouse(slug: str, company_name: str, client: httpx.Client) -> bool:
    resp = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    if resp.status_code != 200:
        return False
    # Greenhouse tells us the board's display name, so a wrong-company slug
    # collision (e.g. a different "Blend") can be rejected instead of trusted.
    board_name = resp.json().get("name", "")
    return _names_match(company_name, board_name)


def _probe_lever(slug: str, client: httpx.Client) -> bool:
    resp = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json", "limit": 1})
    return resp.status_code == 200 and isinstance(resp.json(), list)


def _probe_ashby(slug: str, client: httpx.Client) -> bool:
    resp = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return resp.status_code == 200 and resp.json().get("jobs") is not None


def _probe_smartrecruiters(slug: str, company_name: str, client: httpx.Client) -> bool:
    """SmartRecruiters returns HTTP 200 with an empty result for *any* slug
    (no 404 on a miss), so status code alone is no signal - totalFound must be
    positive. Each posting also echoes the real company name, so a slug
    collision (two unrelated companies normalizing to the same name) can be
    rejected the same way Greenhouse's board-name check works."""
    resp = client.get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 1}
    )
    if resp.status_code != 200:
        return False
    data = resp.json()
    if not data.get("content"):
        return False
    posted_name = (data["content"][0].get("company") or {}).get("name", "")
    return _names_match(company_name, posted_name)


def resolve_company_ats(
    name: str, client: httpx.Client
) -> tuple[ATSType, str] | None:
    """Return (ats_type, slug) if the company has a public board on one of the
    ATSes probed here, else None. Workday is deliberately not probed here -
    its tenant slugs aren't derivable from a company name, so it's only ever
    learned from a real apply_url (see discovery/ats.py, D45).

    Full-name slugs are tried against all probed ATSes. First-word fallback
    slugs are tried against Greenhouse only, because Greenhouse echoes the
    board's display name back so a wrong-company collision can be rejected -
    Lever and Ashby return no company name, and trusting a loose slug there
    produced real false positives (Capital One matching a board named
    "capital") in live testing."""
    for slug in slug_candidates(name):
        if _probe_greenhouse(slug, name, client):
            return ATSType.greenhouse, slug
        if _probe_lever(slug, client):
            return ATSType.lever, slug
        if _probe_ashby(slug, client):
            return ATSType.ashby, slug
        if _probe_smartrecruiters(slug, name, client):
            return ATSType.smartrecruiters, slug
    for slug in loose_slug_candidates(name):
        if _probe_greenhouse(slug, name, client):
            return ATSType.greenhouse, slug
    return None
