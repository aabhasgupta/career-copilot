"""Resume ingestion: turn the user's resume file into a structured profile.

This is the first agentic piece of the system. Claude reads the resume
(PDF sent directly, so layout and columns survive; plain text also accepted)
and returns a validated ResumeProfile via structured outputs. The result is
cached to data/resume_profile.json keyed by a hash of the file contents, so
downstream prompts (fit scoring, tailoring) reuse it without re-calling the
API until the resume actually changes.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from anthropic import Anthropic
from pydantic import BaseModel, Field

from copilot.config import RESUME_CACHE_PATH, Profile


class WorkExperience(BaseModel):
    title: str
    company: str
    start: str | None = Field(None, description="e.g. 2021-06 or 2021")
    end: str | None = Field(None, description="e.g. 2023-01, or null if current")
    location: str | None = None
    highlights: list[str] = Field(
        default_factory=list, description="Achievement bullets, most impressive first"
    )


class Education(BaseModel):
    degree: str
    institution: str
    graduation_year: str | None = None


class ContactInfo(BaseModel):
    """Header/contact details, when the resume states them. Feeds profile.yaml's
    identity section via 'copilot profile fill' - never invented, only extracted."""

    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None


class ResumeProfile(BaseModel):
    full_name: str
    contact: ContactInfo = Field(default_factory=ContactInfo)
    summary: str = Field(description="2-3 sentence professional summary")
    seniority: str = Field(description="e.g. junior, mid, senior, staff, principal")
    years_of_experience: float
    skills: list[str] = Field(description="Technical skills, most prominent first")
    domains: list[str] = Field(
        description="Industry/problem domains worked in, e.g. fintech, data platforms"
    )
    work_experience: list[WorkExperience]
    education: list[Education]
    certifications: list[str] = Field(default_factory=list)


EXTRACTION_PROMPT = """Extract a structured profile from this resume.

Guidelines:
- Be faithful to what the resume says; do not invent or embellish.
- contact: pull directly from the header (email, phone, city/state, LinkedIn/GitHub/
  portfolio URLs). Leave a field null if the resume doesn't state it - never guess.
- years_of_experience: total professional (non-internship) experience; estimate
  from employment dates if not stated.
- seniority: judge from titles, scope, and years, not just the latest title.
- skills: everything technical the resume evidences, most prominent first.
- domains: the industries and problem spaces this person has actually worked in.
"""


def _resume_content_block(resume_path: Path) -> dict:
    data = resume_path.read_bytes()
    if resume_path.suffix.lower() == ".pdf":
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(data).decode(),
            },
        }
    return {"type": "text", "text": data.decode()}


def _cache_key(resume_path: Path, model: str) -> str:
    digest = hashlib.sha256(resume_path.read_bytes()).hexdigest()
    return f"{digest}:{model}"


def extract_resume_profile(
    profile: Profile,
    cache_path: Path = RESUME_CACHE_PATH,
    force: bool = False,
) -> ResumeProfile:
    """Return the structured resume profile, extracting via Claude on cache miss."""
    resume_path = profile.resume_path
    if not resume_path.exists():
        raise FileNotFoundError(
            f"Resume not found at {resume_path}. Put your resume there or update "
            "resume_path in profile.yaml."
        )

    key = _cache_key(resume_path, profile.llm.model)
    if not force and cache_path.exists():
        cached = json.loads(cache_path.read_text())
        if cached.get("cache_key") == key:
            return ResumeProfile.model_validate(cached["profile"])

    client = Anthropic()
    response = client.messages.parse(
        model=profile.llm.model,
        max_tokens=16000,
        messages=[
            {
                "role": "user",
                "content": [
                    _resume_content_block(resume_path),
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
        output_format=ResumeProfile,
    )
    extracted = response.parsed_output
    if extracted is None:
        raise RuntimeError("Resume extraction returned no parseable output.")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {"cache_key": key, "profile": extracted.model_dump()}, indent=2
        )
    )
    return extracted
