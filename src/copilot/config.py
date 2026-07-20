"""Load and validate the user's profile.yaml.

All personal data flows through here so the rest of the codebase never
hardcodes a specific user. Anyone can clone the repo, copy
profile.yaml.example to profile.yaml, and run.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, EmailStr, Field

PROFILE_FILENAME = "profile.yaml"
DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "copilot.db"
RESUME_CACHE_PATH = DATA_DIR / "resume_profile.json"


class RemotePreference(str, Enum):
    remote = "remote"
    hybrid = "hybrid"
    onsite = "onsite"
    any = "any"


class VisaStatus(str, Enum):
    none = "none"
    f1_opt = "f1_opt"
    stem_opt = "stem_opt"
    h1b_transfer = "h1b_transfer"
    other = "other"


class EmailProviderName(str, Enum):
    outlook = "outlook"
    gmail = "gmail"


class Links(BaseModel):
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None


class Identity(BaseModel):
    full_name: str
    email: EmailStr
    phone: str | None = None
    location: str | None = None
    links: Links = Field(default_factory=Links)


class SearchPreferences(BaseModel):
    titles: list[str] = Field(min_length=1)
    locations: list[str] = Field(default_factory=lambda: ["United States"])
    remote: RemotePreference = RemotePreference.any
    min_salary: int | None = None
    salary_currency: str = "USD"
    dealbreakers: list[str] = Field(default_factory=list)
    # Ordered, most preferred first. Reorders listings, never drops them.
    # Entries: "remote", "within <N> miles of <place>", or plain text matched
    # against the job's location (e.g. "Chicago, IL").
    location_preference: list[str] = Field(default_factory=list)
    # Ordered industries, most preferred first, from the controlled vocabulary
    # in industry.py (banking, fintech, tech, consulting, ...). Reorders
    # listings, never drops them.
    industry_preference: list[str] = Field(default_factory=list)
    # Direct employer postings rank above staffing-agency postings. Listing
    # "staffing" in industry_preference overrides this.
    deprioritize_staffing: bool = True


class VisaPreferences(BaseModel):
    needs_sponsorship: bool
    status: VisaStatus


class EmailIntegration(BaseModel):
    provider: EmailProviderName = EmailProviderName.outlook
    address: EmailStr


class LLMSettings(BaseModel):
    model: str = "claude-sonnet-5"


class Profile(BaseModel):
    identity: Identity
    resume_path: Path
    search: SearchPreferences
    visa: VisaPreferences
    email_integration: EmailIntegration
    llm: LLMSettings = Field(default_factory=LLMSettings)


def load_profile(path: Path | None = None) -> Profile:
    """Load profile.yaml from the given path or the current directory."""
    profile_path = path or Path(PROFILE_FILENAME)
    if not profile_path.exists():
        raise FileNotFoundError(
            f"{profile_path} not found. Run 'copilot init' and edit profile.yaml "
            "with your details."
        )
    with open(profile_path) as f:
        raw = yaml.safe_load(f)
    return Profile.model_validate(raw)
