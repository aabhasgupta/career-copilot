"""Plain-English dealbreakers, compiled once into structured filters.

The user writes rules in natural language ("no clearance jobs", "don't give
me jobs based in Alabama") in profile.yaml. Claude translates each one into
matchers against specific job fields (text / location / company), and the
result is cached until the dealbreaker list changes - so filtering every
discovered job stays deterministic and free, same compile-once pattern as
industry classification (docs/DECISIONS.md D13/D14).
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path

from anthropic import Anthropic
from pydantic import BaseModel

from copilot.config import DATA_DIR, Profile

RULES_CACHE_PATH = DATA_DIR / "dealbreaker_rules.json"


class RuleField(str, Enum):
    text = "text"  # matched against title + job description
    location = "location"
    company = "company"


class Matcher(BaseModel):
    field: RuleField
    # Lowercase substrings; ANY pattern hit triggers the matcher.
    patterns: list[str]


class CompiledRule(BaseModel):
    source: str  # the user's original plain-English rule, verbatim
    matchers: list[Matcher]


class CompiledRules(BaseModel):
    rules: list[CompiledRule]


PROMPT_TEMPLATE = """Translate each plain-English job-search dealbreaker below into
structured matchers. A job is dropped when ANY pattern of ANY matcher of any
rule is found as a case-insensitive substring of the matcher's field.

Fields:
- "text": the job title plus full job description
- "location": the job's location string (e.g. "Birmingham, AL", "Remote US")
- "company": the company name

Guidelines:
- Choose the narrowest field that captures the intent: a rule about where the
  job is based belongs on "location", not "text".
- Prefer precise patterns that won't false-positive. For a US state, include
  the full state name and the ", XX" abbreviation form (e.g. "alabama" and
  ", al"). Avoid patterns that are substrings of common words.
- Keep each rule's `source` exactly as given.

Dealbreakers:
{rules_block}"""


def _cache_key(dealbreakers: list[str], model: str) -> str:
    payload = "\n".join(dealbreakers) + ":" + model
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compile(profile: Profile) -> CompiledRules:
    block = "\n".join(f"- {rule}" for rule in profile.search.dealbreakers)
    client = Anthropic()
    response = client.messages.parse(
        model=profile.llm.model,
        max_tokens=4000,
        messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(rules_block=block)}],
        output_format=CompiledRules,
    )
    return response.parsed_output


def _fallback(dealbreakers: list[str]) -> list[CompiledRule]:
    """Pre-compiler behavior: raw substring against title+JD text."""
    return [
        CompiledRule(
            source=rule, matchers=[Matcher(field=RuleField.text, patterns=[rule.lower()])]
        )
        for rule in dealbreakers
    ]


def load_dealbreaker_rules(
    profile: Profile, cache_path: Path = RULES_CACHE_PATH
) -> tuple[list[CompiledRule], list[str]]:
    """Return (rules, errors). Compiles via Claude only when the dealbreaker
    list (or model) changed since the cache was written; falls back to plain
    substring matching if compilation fails."""
    dealbreakers = [d for d in profile.search.dealbreakers if d.strip()]
    if not dealbreakers:
        return [], []

    key = _cache_key(dealbreakers, profile.llm.model)
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        if cached.get("key") == key:
            return CompiledRules.model_validate(cached["rules"]).rules, []

    try:
        compiled = _compile(profile)
    except Exception as exc:  # noqa: BLE001 - degrade to old substring behavior
        return _fallback(dealbreakers), [
            f"dealbreaker compilation failed ({exc}); using plain substring matching"
        ]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"key": key, "rules": compiled.model_dump()}, indent=1))
    return compiled.rules, []


def job_matches_rules(
    rules: list[CompiledRule],
    *,
    title: str,
    jd_text: str | None,
    location: str | None,
    company: str | None,
) -> bool:
    haystacks = {
        RuleField.text: f"{title}\n{jd_text or ''}".lower(),
        RuleField.location: (location or "").lower(),
        RuleField.company: (company or "").lower(),
    }
    for rule in rules:
        for matcher in rule.matchers:
            haystack = haystacks[matcher.field]
            if any(p.lower() in haystack for p in matcher.patterns if p.strip()):
                return True
    return False
