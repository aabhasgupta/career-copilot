from datetime import datetime, timedelta, timezone
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


def test_industry_tier_matches_company_label(tmp_path: Path):
    from copilot.db.models import Company
    from copilot.ranking import industry_label, industry_tier

    prefs = ["banking", "fintech", "tech"]
    fintech_job = _job()
    fintech_job.company = Company(name="Chime", industry="fintech")
    unknown_job = _job()
    unknown_job.company = Company(name="Mystery Co", industry=None)

    assert industry_tier(fintech_job, prefs) == 1
    assert industry_tier(unknown_job, prefs) == 3
    assert industry_label(1, prefs) == "fintech"
    assert industry_label(3, prefs) == "-"


def test_staffing_deprioritized_below_unmatched(tmp_path: Path):
    from copilot.db.models import Company
    from copilot.ranking import industry_label, industry_tier

    prefs = ["banking", "fintech"]
    staffing_job = _job()
    staffing_job.company = Company(name="Kforce", industry="staffing")
    unknown_job = _job()
    unknown_job.company = Company(name="Mystery Co", industry="retail")

    assert industry_tier(unknown_job, prefs) == 2
    assert industry_tier(staffing_job, prefs) == 3  # below even unmatched
    assert industry_label(3, prefs) == "staffing↓"
    # Explicitly preferring staffing overrides the rule
    assert industry_tier(staffing_job, ["staffing"]) == 0
    # Flag off: staffing is just another unmatched industry
    assert industry_tier(staffing_job, prefs, deprioritize_staffing=False) == 2


def test_company_tier_whole_word_matching():
    from copilot.db.models import Company
    from copilot.ranking import company_tier

    prefs = ["Stripe", "JPMorgan Chase"]
    stripe_job = _job()
    stripe_job.company = Company(name="Stripe")
    stripe_inc_job = _job()
    stripe_inc_job.company = Company(name="Stripe, Inc.")
    stripes_job = _job()
    stripes_job.company = Company(name="Stripes Group")
    jpmc_job = _job()
    jpmc_job.company = Company(name="JPMorgan Chase & Co.")

    assert company_tier(stripe_job, prefs) == 0
    assert company_tier(stripe_inc_job, prefs) == 0
    assert company_tier(stripes_job, prefs) == 2  # no false positive
    assert company_tier(jpmc_job, prefs) == 1
    assert company_tier(stripe_job, []) == 0  # empty prefs: everyone tier 0


def test_rank_jobs_scored_before_unscored_by_descending_fit(tmp_path: Path):
    from copilot.config import Profile
    from copilot.ranking import rank_jobs

    profile = Profile.model_validate(
        {
            "identity": {"full_name": "X", "email": "x@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {"titles": ["Engineer"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "x@hotmail.com"},
        }
    )
    high = _job(fit_score=80)
    low = _job(fit_score=20)
    # Even a very attractive unscored job (huge salary) must not outrank a
    # scored job - "not yet scored" is not the same as "low fit".
    unscored = _job(fit_score=None, salary_max=500000)
    ranked = rank_jobs([low, unscored, high], profile, _offline_geocoder(tmp_path))
    assert ranked == [high, low, unscored]


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_freshness_tier_buckets_by_fresh_window():
    from copilot.ranking import FRESH_WINDOW_DAYS, freshness_tier

    fresh = _job(posted_at=_days_ago(FRESH_WINDOW_DAYS - 1))
    boundary = _job(posted_at=_days_ago(FRESH_WINDOW_DAYS))
    stale = _job(posted_at=_days_ago(FRESH_WINDOW_DAYS + 1))
    unknown = _job(posted_at=None)

    assert freshness_tier(fresh) == 0
    assert freshness_tier(boundary) == 0
    assert freshness_tier(stale) == 1
    assert freshness_tier(unknown) == 1  # never assumed fresh


def test_rank_jobs_prefers_fresher_posting_within_same_fit_tier(tmp_path: Path):
    from copilot.config import Profile
    from copilot.ranking import rank_jobs

    profile = Profile.model_validate(
        {
            "identity": {"full_name": "X", "email": "x@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {"titles": ["Engineer"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "x@hotmail.com"},
        }
    )
    fresh = _job(fit_score=70, posted_at=_days_ago(2))
    stale = _job(fit_score=70, posted_at=_days_ago(45))
    ranked = rank_jobs([stale, fresh], profile, _offline_geocoder(tmp_path))
    assert ranked == [fresh, stale]
