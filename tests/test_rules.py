import json
from pathlib import Path

from copilot.rules import (
    CompiledRule,
    CompiledRules,
    Matcher,
    RuleField,
    _cache_key,
    _fallback,
    job_matches_rules,
    load_dealbreaker_rules,
)

ALABAMA_RULE = CompiledRule(
    source="don't give me jobs based in Alabama",
    matchers=[Matcher(field=RuleField.location, patterns=["alabama", ", al"])],
)


def test_location_rule_matches_location_not_text():
    hit = dict(title="ML Engineer", jd_text="Great role", company="Acme")
    assert job_matches_rules([ALABAMA_RULE], location="Birmingham, AL", **hit)
    assert job_matches_rules([ALABAMA_RULE], location="Huntsville, Alabama", **hit)
    assert not job_matches_rules([ALABAMA_RULE], location="Chicago, IL", **hit)
    # "Alabama" appearing in the JD text is not "based in Alabama"
    assert not job_matches_rules(
        [ALABAMA_RULE],
        title="ML Engineer",
        jd_text="Our Alabama office opened in 2020 but this role is remote",
        location="Remote US",
        company="Acme",
    )


def test_company_rule():
    rule = CompiledRule(
        source="nothing at Meta",
        matchers=[Matcher(field=RuleField.company, patterns=["meta"])],
    )
    assert job_matches_rules(
        [rule], title="Engineer", jd_text=None, location=None, company="Meta Platforms"
    )
    assert not job_matches_rules(
        [rule], title="Engineer", jd_text=None, location=None, company="Acme"
    )


def test_fallback_preserves_old_substring_behavior():
    rules = _fallback(["clearance required"])
    assert job_matches_rules(
        rules,
        title="Engineer",
        jd_text="Active clearance required",
        location=None,
        company=None,
    )


def test_cache_hit_skips_compilation(tmp_path: Path):
    from copilot.config import Profile

    profile = Profile.model_validate(
        {
            "identity": {"full_name": "X", "email": "x@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {"titles": ["Engineer"], "dealbreakers": ["no jobs in Alabama"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "x@hotmail.com"},
        }
    )
    cache_path = tmp_path / "rules.json"
    key = _cache_key(["no jobs in Alabama"], profile.llm.model)
    cache_path.write_text(
        json.dumps({"key": key, "rules": CompiledRules(rules=[ALABAMA_RULE]).model_dump()})
    )

    # No API key in the test env: reaching the compiler would fall back and
    # return an error message, so a clean load proves the cache was used.
    rules, errors = load_dealbreaker_rules(profile, cache_path=cache_path)
    assert errors == []
    assert rules[0].matchers[0].field == RuleField.location


def test_empty_dealbreakers_compile_to_nothing(tmp_path: Path):
    from copilot.config import Profile

    profile = Profile.model_validate(
        {
            "identity": {"full_name": "X", "email": "x@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {"titles": ["Engineer"]},
            "visa": {"needs_sponsorship": False, "status": "none"},
            "email_integration": {"provider": "gmail", "address": "x@gmail.com"},
        }
    )
    rules, errors = load_dealbreaker_rules(profile, cache_path=tmp_path / "rules.json")
    assert rules == [] and errors == []
