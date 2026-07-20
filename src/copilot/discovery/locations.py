"""US-eligibility check for sources that aren't country-scoped.

Adzuna and JSearch queries are already US-only, but company ATS boards are
global - Snowflake's Greenhouse board happily returns Israel and UK postings.
This uses a non-US blocklist rather than a US allowlist because board
locations are usually bare city/state strings ("Chicago, IL") that carry no
positive US marker.
"""

from __future__ import annotations

import re

# Country names plus a few unambiguous region tags. Deliberately omits
# ambiguous words: "georgia" (US state), bare city names like "london"
# (London, KY exists). "new mexico" is stripped before matching so the
# country "mexico" doesn't false-positive on the US state.
_NON_US_MARKERS = [
    "united kingdom", "uk", "england", "scotland", "wales", "ireland",
    "canada", "ontario", "quebec", "british columbia", "alberta",
    "india", "israel", "germany", "poland", "spain", "portugal",
    "netherlands", "france", "italy", "romania", "bulgaria", "ukraine",
    "hungary", "czech", "austria", "switzerland", "sweden", "norway",
    "denmark", "finland", "greece", "serbia", "croatia",
    "brazil", "argentina", "mexico", "colombia", "peru", "chile",
    "costa rica", "uruguay",
    "australia", "new zealand", "singapore", "japan", "china",
    "hong kong", "taiwan", "south korea", "philippines", "pakistan",
    "bangladesh", "vietnam", "indonesia", "malaysia", "thailand",
    "egypt", "nigeria", "kenya", "south africa", "morocco",
    "uae", "dubai", "saudi arabia", "turkey", "qatar",
    "europe", "emea", "apac", "latam",
]

_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in _NON_US_MARKERS) + r")\b"
)

# "Peru, IL" is an Illinois town, not the country: a ", XX" US-state suffix
# wins over any country-name match.
_US_STATE_SUFFIX = re.compile(
    r",\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi"
    r"|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt"
    r"|va|wa|wv|wi|wy|dc)\b"
)


_US_ELIGIBLE_HINTS = ("usa", "united states", "americas", "north america", "worldwide", "anywhere")


def is_non_us(location: str | None) -> bool:
    """True when the location explicitly names a non-US country or region.
    Unknown/empty/bare-US-city locations return False (kept), as do
    multi-region strings that include a US-eligible region ("Americas,
    Europe, Israel" - the user qualifies via Americas)."""
    if not location:
        return False
    loc = location.lower().replace("new mexico", "")
    if _US_STATE_SUFFIX.search(loc):
        return False
    if any(hint in loc for hint in _US_ELIGIBLE_HINTS):
        return False
    return bool(_PATTERN.search(loc))
