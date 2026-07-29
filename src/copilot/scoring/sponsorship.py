"""Company-level H1B sponsorship evidence from the public USCIS H-1B Employer
Data Hub. Deterministic, no LLM - see the module's role in the design in
docs/DECISIONS.md D18.

Matching is intentionally conservative. The filing data lists legal entity
names ("AMAZON COM SERVICES LLC") that often differ from the brand name a job
posting uses ("Amazon"), so beyond an exact match (after normalizing away
legal suffixes), a prefix match is also tried - but only when the company's
own name is distinctive enough (2+ words, or a single word of 6+ characters)
that a false match is unlikely. Same lesson already learned matching ATS
slugs (D3): loose matching on short/common words produces real false
positives (e.g. "Capital One" must not match on the bare word "Capital").

This module intentionally never feeds sponsorship data back into fit_score
(D18): the data is a year-plus-old, company-wide aggregate that says nothing
about a specific role or team's current policy, so it is surfaced as
separate context (`copilot jobs show`) rather than blended into a
job-specific number that would imply more certainty than the data has.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot.config import DATA_DIR
from copilot.db.models import Company, SponsorshipStatus

# USCIS publishes the Data Hub export roughly annually and drops older years'
# URLs; FY2023 is the latest confirmed reachable (FY2024/2025 both 404 as of
# 2026-07-19 - see docs/APIS.md). This is only the fallback used when nothing
# is cached yet - once a file is downloaded, current_cached_year() reads the
# real year back from the cache filename, and refresh_h1b_data() probes for a
# newer one on demand rather than trusting this constant to stay current.
FISCAL_YEAR = 2023
H1B_DATA_URL_TEMPLATE = (
    "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{year}.csv"
)
H1B_DATA_URL = H1B_DATA_URL_TEMPLATE.format(year=FISCAL_YEAR)
_CACHE_NAME_RE = re.compile(r"h1b_data_hub_fy(\d{4})\.csv")


def h1b_cache_path(year: int = FISCAL_YEAR) -> Path:
    return DATA_DIR / f"h1b_data_hub_fy{year}.csv"


H1B_CACHE_PATH = h1b_cache_path(FISCAL_YEAR)

# USCIS blocks non-browser user agents (confirmed live 2026-07-19) even though
# the file itself is public and unauthenticated.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "llp",
    "lp",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "pc",
    "plc",
    "na",
    "national",
    "association",
    "holdings",
}
_FILLER_WORDS = {"and", "the", "of"}


def normalize_company_name(name: str) -> str:
    """Strip filler words and trailing legal-entity suffixes so different
    legal names for the same company (or different sources' spelling of the
    same brand) converge on one key."""
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    words = [w for w in words if w not in _FILLER_WORDS]
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words) or name.lower().strip()


@dataclass
class FilingStats:
    employer_names: set[str] = field(default_factory=set)
    initial_approval: int = 0
    initial_denial: int = 0
    continuing_approval: int = 0
    continuing_denial: int = 0

    @property
    def total_approved(self) -> int:
        return self.initial_approval + self.continuing_approval

    def merge(self, other: FilingStats) -> None:
        self.employer_names |= other.employer_names
        self.initial_approval += other.initial_approval
        self.initial_denial += other.initial_denial
        self.continuing_approval += other.continuing_approval
        self.continuing_denial += other.continuing_denial


def download_data(year: int = FISCAL_YEAR, force: bool = False) -> Path:
    """Download and cache the USCIS H-1B Employer Data Hub CSV for `year`.
    USCIS updates this roughly annually, so re-downloading daily would be
    pointless; skipped once cached unless force=True."""
    cache_path = h1b_cache_path(year)
    if cache_path.exists() and not force:
        return cache_path
    resp = httpx.get(
        H1B_DATA_URL_TEMPLATE.format(year=year),
        headers={"User-Agent": _USER_AGENT}, timeout=60, follow_redirects=True,
    )
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(resp.content)
    return cache_path


def current_cached_year(default: int = FISCAL_YEAR) -> int:
    """The newest fiscal year already cached on disk, or `default` if nothing
    has been downloaded yet."""
    years = [
        int(m.group(1))
        for p in DATA_DIR.glob("h1b_data_hub_fy*.csv")
        if (m := _CACHE_NAME_RE.match(p.name))
    ]
    return max(years) if years else default


def _year_is_published(year: int) -> bool:
    """Check whether USCIS has a file at `year` without downloading the full
    CSV (it can be tens of MB) - stream just far enough to read the status."""
    try:
        with httpx.stream(
            "GET", H1B_DATA_URL_TEMPLATE.format(year=year),
            headers={"User-Agent": _USER_AGENT}, timeout=15, follow_redirects=True,
        ) as resp:
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


def probe_newer_year(after_year: int, max_ahead: int = 3) -> int | None:
    """Check for a fiscal year newer than `after_year`, newest first. Returns
    the newest one that's actually reachable, or None if nothing newer has
    been published yet."""
    ceiling = min(after_year + max_ahead, date.today().year + 1)
    for year in range(ceiling, after_year, -1):
        if _year_is_published(year):
            return year
    return None


def refresh_h1b_data() -> tuple[int, bool]:
    """Probe for a newer fiscal year than what's cached; download whichever
    year ends up current. Returns (year_now_cached, found_newer)."""
    existing_year = current_cached_year()
    newer_year = probe_newer_year(existing_year)
    year = newer_year or existing_year
    download_data(year=year, force=True)
    return year, newer_year is not None


def load_filings(cache_path: Path = H1B_CACHE_PATH) -> dict[str, FilingStats]:
    """Parse the cached CSV into {normalized_employer_name: FilingStats},
    aggregating the multiple worksite/Tax-ID rows USCIS lists per employer."""
    by_name: dict[str, FilingStats] = {}
    with open(cache_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            employer = (row.get("Employer") or "").strip()
            if not employer:
                continue
            key = normalize_company_name(employer)
            stats = by_name.setdefault(key, FilingStats())
            stats.employer_names.add(employer)
            stats.initial_approval += int(row.get("Initial Approval") or 0)
            stats.initial_denial += int(row.get("Initial Denial") or 0)
            stats.continuing_approval += int(row.get("Continuing Approval") or 0)
            stats.continuing_denial += int(row.get("Continuing Denial") or 0)
    return by_name


def match_company(company_name: str, filings: dict[str, FilingStats]) -> FilingStats | None:
    """Exact match after normalization, or a prefix match restricted to
    distinctive names (2+ words, or a single word of 6+ characters) - see
    module docstring for why. Multiple matching legal entities (e.g. Amazon's
    subsidiary filers) are combined into one aggregate."""
    key = normalize_company_name(company_name)
    if key in filings:
        return filings[key]

    words = key.split()
    distinctive = len(words) >= 2 or (len(words) == 1 and len(words[0]) >= 6)
    if not distinctive:
        return None

    matches = [stats for k, stats in filings.items() if k.startswith(key + " ")]
    if not matches:
        return None
    combined = FilingStats()
    for m in matches:
        combined.merge(m)
    return combined


@dataclass
class SponsorshipSyncSummary:
    companies_checked: int = 0
    matched: int = 0


def sync_sponsorship_data(
    session: Session, cache_path: Path = H1B_CACHE_PATH, year: int | None = None
) -> SponsorshipSyncSummary:
    """Match every company in the DB against the cached filing data and
    update h1b_filing_count / sponsorship_evidence / sponsorship_status.
    Deterministic and free - safe to re-run any time (e.g. after new
    companies are discovered). `year` labels the evidence text; if omitted
    it's parsed from `cache_path`'s filename, falling back to FISCAL_YEAR."""
    if year is None:
        m = _CACHE_NAME_RE.match(Path(cache_path).name)
        year = int(m.group(1)) if m else FISCAL_YEAR
    filings = load_filings(cache_path)
    summary = SponsorshipSyncSummary()
    for company in session.scalars(select(Company)).all():
        summary.companies_checked += 1
        stats = match_company(company.name, filings)
        if stats is None:
            continue
        summary.matched += 1
        company.h1b_filing_count = stats.total_approved
        company.sponsorship_evidence = (
            f"{stats.total_approved} H1B petitions approved (FY{year}) per USCIS "
            f"H-1B Employer Data Hub ({stats.initial_approval} initial, "
            f"{stats.continuing_approval} continuing; {stats.initial_denial + stats.continuing_denial} "
            "denied)"
        )
        if stats.total_approved > 0:
            company.sponsorship_status = SponsorshipStatus.sponsors
    session.commit()
    return summary
