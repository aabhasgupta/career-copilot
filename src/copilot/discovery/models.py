"""Common shape both discovery sources normalize into before hitting the DB."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from copilot.db.models import ATSType, SalarySource


class DiscoveredJob(BaseModel):
    title: str
    company_name: str
    location: str | None = None
    remote: bool | None = None
    employment_type: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    salary_source: SalarySource = SalarySource.unknown
    source: str
    jd_text: str | None = None
    apply_url: str
    posted_at: datetime | None = None
    ats_type: ATSType = ATSType.unknown
    ats_slug: str | None = None
