"""SQLAlchemy models. The SQLite database is the source of truth for the whole
system: CLI output, digest emails, and any future dashboard are read layers
over these tables. Timestamps are kept on everything so later phases (response
likelihood scoring, dashboards) can reconstruct the full timeline.

Unknown is a first-class value throughout: jobs with no visa or salary info
are stored and surfaced as unknown, never dropped.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ATSType(str, enum.Enum):
    greenhouse = "greenhouse"
    lever = "lever"
    ashby = "ashby"
    # none = probed, no public board found; unknown = not yet probed
    none = "none"
    unknown = "unknown"


class SponsorshipStatus(str, enum.Enum):
    transfers_h1b = "transfers_h1b"
    sponsors = "sponsors"
    no_sponsor = "no_sponsor"
    unknown = "unknown"


class VisaSignal(str, enum.Enum):
    explicit_yes = "explicit_yes"
    explicit_no = "explicit_no"
    unknown = "unknown"


class SalarySource(str, enum.Enum):
    posted = "posted"
    aggregator_estimate = "aggregator_estimate"
    unknown = "unknown"


class ApplicationState(str, enum.Enum):
    found = "found"
    queued = "queued"
    applied = "applied"
    replied = "replied"
    interviewing = "interviewing"
    offer = "offer"
    rejected = "rejected"
    ghosted = "ghosted"


class EmailEventType(str, enum.Enum):
    rejection = "rejection"
    interview_invite = "interview_invite"
    online_assessment = "online_assessment"
    recruiter_screen = "recruiter_screen"
    confirmation = "confirmation"
    other = "other"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    ats_type: Mapped[ATSType] = mapped_column(Enum(ATSType), default=ATSType.unknown)
    ats_slug: Mapped[str | None] = mapped_column(String(255))
    watchlisted: Mapped[bool] = mapped_column(default=False)
    sponsorship_status: Mapped[SponsorshipStatus] = mapped_column(
        Enum(SponsorshipStatus), default=SponsorshipStatus.unknown
    )
    h1b_filing_count: Mapped[int | None] = mapped_column(Integer)
    sponsorship_evidence: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    jobs: Mapped[list[Job]] = relationship(back_populates="company")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    remote: Mapped[bool | None] = mapped_column()
    employment_type: Mapped[str | None] = mapped_column(String(64))
    seniority_level: Mapped[str | None] = mapped_column(String(64))
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    salary_source: Mapped[SalarySource] = mapped_column(
        Enum(SalarySource), default=SalarySource.unknown
    )
    source: Mapped[str] = mapped_column(String(64))
    jd_text: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fit_score: Mapped[float | None] = mapped_column(Float)
    fit_reasoning: Mapped[str | None] = mapped_column(Text)
    visa_signal: Mapped[VisaSignal] = mapped_column(
        Enum(VisaSignal), default=VisaSignal.unknown
    )
    response_likelihood_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[Company] = relationship(back_populates="jobs")
    application: Mapped[Application | None] = relationship(back_populates="job")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True, index=True)
    state: Mapped[ApplicationState] = mapped_column(
        Enum(ApplicationState), default=ApplicationState.found, index=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tailored_materials_path: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    job: Mapped[Job] = relationship(back_populates="application")
    email_events: Mapped[list[EmailEvent]] = relationship(back_populates="application")


class EmailEvent(Base):
    __tablename__ = "email_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    provider_thread_id: Mapped[str] = mapped_column(String(255))
    classified_type: Mapped[EmailEventType] = mapped_column(
        Enum(EmailEventType), default=EmailEventType.other
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    application: Mapped[Application] = relationship(back_populates="email_events")
