"""Dedupe key for discovered jobs: company + title + location, normalized.

Deliberately ignores source - the same real posting found via both Adzuna and
JSearch should collapse to one row rather than two.
"""

from __future__ import annotations

import hashlib


def dedupe_hash(company_name: str, title: str, location: str | None) -> str:
    parts = [company_name.strip().lower(), title.strip().lower(), (location or "").strip().lower()]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
