"""Title matching shared by the pipeline and discovery sources."""

from __future__ import annotations

import re


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", title.lower()).strip()


def titles_equal(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    return bool(na) and na == nb


def titles_match(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    return bool(na and nb) and (na == nb or na in nb or nb in na)


def matches_search_titles(posting_title: str, search_titles: list[str]) -> bool:
    return any(titles_match(posting_title, t) for t in search_titles)
