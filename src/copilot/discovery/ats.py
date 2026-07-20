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


def detect_ats(apply_url: str) -> tuple[ATSType, str | None]:
    """Return (ats_type, slug) for a known ATS apply URL, else (unknown, None)."""
    for ats_type, pattern in _PATTERNS:
        match = pattern.search(apply_url)
        if match:
            return ats_type, match.group(1)
    return ATSType.unknown, None
