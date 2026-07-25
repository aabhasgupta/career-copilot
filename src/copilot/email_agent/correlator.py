"""Deterministic email-to-application correlation - the cheap stage that runs
before any LLM call (same spend-deterministic-first principle as the sync
pre-filter). Finds candidate applications for an inbound message; zero or
ambiguous candidates get arbitrated by the classifier's Claude call, never
guessed here.

Only companies with an Application row are candidates at all: mail about a
job you never acted on can't be a state update, and D20 handles the
"applied outside Copilot" case via suggestions instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot.db.models import Application, Company, Job
from copilot.email_agent.provider import InboundMessage
from copilot.scoring.sponsorship import normalize_company_name


@dataclass
class Candidate:
    application: Application
    job: Job
    company: Company
    matched_on: str  # "domain" or "name"


def find_candidates(session: Session, message: InboundMessage) -> list[Candidate]:
    rows = session.execute(
        select(Application, Job, Company)
        .join(Job, Application.job_id == Job.id)
        .join(Company, Job.company_id == Company.id)
    ).all()

    sender_domain = message.sender.rsplit("@", 1)[-1] if "@" in message.sender else ""
    haystack = normalize_company_name(f"{message.subject} {message.sender_name}")

    candidates: list[Candidate] = []
    for application, job, company in rows:
        if company.domain and sender_domain and sender_domain == company.domain:
            candidates.append(Candidate(application, job, company, matched_on="domain"))
            continue
        name_key = normalize_company_name(company.name)
        # Whole-word containment, not substring: "Meta" must not match
        # "metadata" - normalize_company_name space-joins words, so pad both.
        if name_key and f" {name_key} " in f" {haystack} ":
            candidates.append(Candidate(application, job, company, matched_on="name"))
    return candidates


def learn_domain(company: Company, sender: str, ats_domains: frozenset[str]) -> None:
    """Record a company's real email domain from confirmed evidence, unless
    the mail came via a shared ATS domain (greenhouse.io says nothing about
    which company). Never overwrites an already-learned domain."""
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    if domain and company.domain is None and domain not in ats_domains:
        company.domain = domain
