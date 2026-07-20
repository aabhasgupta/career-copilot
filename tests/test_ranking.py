from pathlib import Path

from copilot.db.models import Job
from copilot.geocode import Geocoder, _clean_location
from copilot.ranking import build_rules, haversine_miles, preference_tier, tier_label

CHICAGO = (41.8781, -87.6298)
HOFFMAN_ESTATES = (42.0629, -88.1227)
NEW_YORK = (40.7128, -74.0060)


def _job(**kwargs) -> Job:
    defaults = dict(title="Engineer", source="adzuna", apply_url="x", dedupe_hash="h")
    defaults.update(kwargs)
    return Job(**defaults)


def _offline_geocoder(tmp_path: Path) -> Geocoder:
    # Empty cache and no seeded places: lookups of unknown places would hit
    # the network, so tests only use rules that don't need the geocoder or
    # pre-seed the cache below.
    return Geocoder(cache_path=tmp_path / "cache.json")


def _seeded_geocoder(tmp_path: Path) -> Geocoder:
    g = Geocoder(cache_path=tmp_path / "cache.json")
    g._cache["hoffman estates, il"] = list(HOFFMAN_ESTATES)
    return g


def test_haversine_chicago_to_hoffman_estates_about_28_miles():
    d = haversine_miles(*CHICAGO, *HOFFMAN_ESTATES)
    assert 25 < d < 32


def test_remote_rule_matches_flag_or_location_text(tmp_path: Path):
    rules = build_rules(["remote"], _offline_geocoder(tmp_path))
    assert preference_tier(_job(remote=True), rules) == 0
    assert preference_tier(_job(location="Remote US"), rules) == 0
    assert preference_tier(_job(location="Chicago, IL"), rules) == 1


def test_text_rule_substring_match(tmp_path: Path):
    rules = build_rules(["Chicago, IL"], _offline_geocoder(tmp_path))
    assert preference_tier(_job(location="Chicago, IL"), rules) == 0
    assert preference_tier(_job(location="Greater Chicago, IL Area"), rules) == 0
    # City-part fallback: sources format the same place differently
    assert preference_tier(_job(location="Chicago, Cook County"), rules) == 0
    assert preference_tier(_job(location="Austin, TX"), rules) == 1


def test_radius_rule_uses_coordinates(tmp_path: Path):
    rules = build_rules(
        ["within 30 miles of Hoffman Estates, IL"], _seeded_geocoder(tmp_path)
    )
    chicago_job = _job(location="Chicago, IL", latitude=CHICAGO[0], longitude=CHICAGO[1])
    ny_job = _job(location="New York, NY", latitude=NEW_YORK[0], longitude=NEW_YORK[1])
    no_coords = _job(location="Somewhere")
    assert preference_tier(chicago_job, rules) == 0
    assert preference_tier(ny_job, rules) == 1
    assert preference_tier(no_coords, rules) == 1


def test_tier_ordering_first_match_wins(tmp_path: Path):
    rules = build_rules(["remote", "Chicago, IL"], _offline_geocoder(tmp_path))
    remote_chicago = _job(remote=True, location="Chicago, IL")
    assert preference_tier(remote_chicago, rules) == 0
    assert tier_label(0, rules) == "remote"
    assert tier_label(2, rules) == "-"


def test_clean_location_strips_noise_and_rejects_non_places():
    assert _clean_location("Columbus, OH (+2 others)") == "Columbus, OH"
    assert _clean_location("Anywhere") is None
    assert _clean_location("Remote") is None
