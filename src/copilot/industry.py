"""Classify companies into a controlled industry vocabulary, once each.

One batched Claude call per discover run covers every never-before-seen
company; the label is stored on the companies table forever after. That keeps
the `industry_preference` ordering deterministic and free at read time
(docs/DECISIONS.md D12/D13) - the LLM cost is one-time per company, not
per listing view.

The vocabulary is fixed so profile.yaml preferences can match labels exactly
instead of fuzzy-matching free text.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from copilot import llm
from copilot.config import Profile


class Industry(str, Enum):
    banking = "banking"
    fintech = "fintech"
    insurance = "insurance"
    healthcare = "healthcare"
    tech = "tech"
    consulting = "consulting"
    staffing = "staffing"
    government = "government"
    retail = "retail"
    manufacturing = "manufacturing"
    energy = "energy"
    education = "education"
    media = "media"
    nonprofit = "nonprofit"
    other = "other"


class CompanyIndustry(BaseModel):
    company_name: str
    industry: Industry


class IndustryClassification(BaseModel):
    companies: list[CompanyIndustry]


PROMPT_TEMPLATE = """Classify each company below into exactly one industry.

Guidelines:
- "banking" is traditional banks and credit unions; "fintech" is technology-first
  financial companies including neo-banks, payments, lending platforms.
- "staffing" is recruiting/staffing agencies that hire on behalf of clients
  (distinct from "consulting", which delivers professional services itself).
- "tech" is companies whose product is software/hardware/AI itself.
- Use "other" only when nothing else fits.
- The sample job title is context from a real posting at that company.

Companies:
{companies_block}

Return every company, using its name exactly as given."""


def classify_companies(
    profile: Profile, companies: list[tuple[str, str | None]]
) -> dict[str, str]:
    """companies: (name, sample_job_title) pairs. Returns {name: industry}."""
    if not companies:
        return {}

    block = "\n".join(
        f"- {name}" + (f" (posting: {title})" if title else "")
        for name, title in companies
    )
    response = llm.parse(
        model=profile.llm.model,
        max_tokens=8000,
        messages=[
            {"role": "user", "content": PROMPT_TEMPLATE.format(companies_block=block)}
        ],
        output_format=IndustryClassification,
    )
    return {
        c.company_name: c.industry.value for c in response.parsed_output.companies
    }


class IndustryPreferenceSuggestion(BaseModel):
    industries: list[Industry]


SUGGEST_PREFERENCE_PROMPT_TEMPLATE = """A candidate's resume states these domains/
industries they've worked in:
{domains_block}

From the fixed list below, choose which ones are a genuine match for the
candidate's stated domains, ranked most-relevant first. Only include an
industry if there's a real match - it's fine to return fewer than all the
domains listed, or none at all if nothing maps cleanly. Never return "other".

Fixed industries: {vocab}"""


def suggest_industry_preference(profile: Profile, domains: list[str]) -> list[str]:
    """Map a resume's free-text `domains` onto the same closed Industry
    vocabulary classify_companies() assigns to companies - the two are kept
    on one enum (`Industry`, above) specifically so a suggested preference
    can never be a value ranking.py's industry_preference matching wouldn't
    also recognize on a company. Structured output (`output_format`) makes
    this a type guarantee, not just a prompting convention: Claude can only
    return `Industry` members, never invented free text."""
    if not domains:
        return []

    response = llm.parse(
        model=profile.llm.model,
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": SUGGEST_PREFERENCE_PROMPT_TEMPLATE.format(
                domains_block="\n".join(f"- {d}" for d in domains),
                vocab=", ".join(i.value for i in Industry if i != Industry.other),
            ),
        }],
        output_format=IndustryPreferenceSuggestion,
    )
    seen: set[str] = set()
    ranked: list[str] = []
    for industry in response.parsed_output.industries:
        if industry == Industry.other or industry.value in seen:
            continue
        seen.add(industry.value)
        ranked.append(industry.value)
    return ranked
