"""Sniff a posting's apply URL for a known ATS, so the company can be
auto-watchlisted for direct board polling later (see docs/DECISIONS.md D3).
Detection only - this module never calls the ATS APIs themselves.
"""

from __future__ import annotations

import re

from copilot.db.models import ATSType

_PATTERNS: list[tuple[ATSType, re.Pattern[str]]] = [
    (ATSType.greenhouse, re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)")),
    (ATSType.lever, re.compile(r"jobs\.lever\.co/([^/?#]+)")),
    (ATSType.ashby, re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)")),
]

# Workday tenant slugs aren't derivable from a company name (unlike the three
# above, where the slug is normally just the name) - the only reliable signal
# is an apply_url we've actually seen on a real discovered posting, e.g.
# "https://ntrs.wd1.myworkdayjobs.com/northerntrust/job/...". Captured as
# "tenant:site" (docs/DECISIONS.md D45).
_WORKDAY_PATTERN = re.compile(r"https?://([\w-]+)\.wd\d+\.myworkdayjobs\.com/([\w-]+)/")


def detect_ats(apply_url: str) -> tuple[ATSType, str | None]:
    """Return (ats_type, slug) for a known ATS apply URL, else (unknown, None)."""
    for ats_type, pattern in _PATTERNS:
        match = pattern.search(apply_url)
        if match:
            return ats_type, match.group(1)
    match = _WORKDAY_PATTERN.search(apply_url)
    if match:
        return ATSType.workday, f"{match.group(1)}:{match.group(2)}"
    return ATSType.unknown, None
