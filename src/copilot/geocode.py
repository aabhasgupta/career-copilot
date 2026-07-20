"""Geocoding via OpenStreetMap's Nominatim, with a local file cache.

Used for the "within N miles of X" location preference: preference anchors
and job locations without coordinates get looked up once and cached forever
in data/geocode_cache.json. Nominatim is free with a courtesy limit of about
one request per second, so uncached lookups are deliberately throttled - the
cache means that cost is only ever paid the first time a place is seen.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from copilot.config import DATA_DIR

GEOCODE_CACHE_PATH = DATA_DIR / "geocode_cache.json"
_USER_AGENT = "career-copilot/0.1 (https://github.com/aabhasgupta/career-copilot)"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

_NON_PLACES = ("remote", "anywhere", "unknown", "united states", "usa")


def _clean_location(location: str) -> str | None:
    """Strip aggregator noise like "Columbus, OH (+2 others)"; reject values
    that aren't a geocodable place."""
    cleaned = re.sub(r"\s*\(\+\d+ others?\)", "", location).strip()
    if not cleaned or cleaned.lower() in _NON_PLACES:
        return None
    return cleaned


class Geocoder:
    def __init__(self, cache_path: Path = GEOCODE_CACHE_PATH):
        self._cache_path = cache_path
        self._cache: dict[str, list[float] | None] = {}
        if cache_path.exists():
            self._cache = json.loads(cache_path.read_text())
        self._last_request_at = 0.0

    def _save(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self._cache, indent=1))

    def lookup(self, location: str) -> tuple[float, float] | None:
        """Return (latitude, longitude) for a place name, or None if it can't
        be resolved. Failed lookups are cached too, so they aren't retried on
        every run."""
        place = _clean_location(location)
        if place is None:
            return None

        key = place.lower()
        if key in self._cache:
            cached = self._cache[key]
            return (cached[0], cached[1]) if cached else None

        elapsed = time.monotonic() - self._last_request_at
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)

        try:
            resp = httpx.get(
                _NOMINATIM_URL,
                params={"q": place, "format": "json", "limit": 1, "countrycodes": "us"},
                headers={"User-Agent": _USER_AGENT},
                timeout=15,
            )
            self._last_request_at = time.monotonic()
            resp.raise_for_status()
            results = resp.json()
        except httpx.HTTPError:
            return None  # transient failure: don't cache, retry next run

        if results:
            coords = [float(results[0]["lat"]), float(results[0]["lon"])]
            self._cache[key] = coords
            self._save()
            return coords[0], coords[1]

        self._cache[key] = None
        self._save()
        return None
