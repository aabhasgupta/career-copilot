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
    smartrecruiters = "smartrecruiters"
    # Workday is never name-guessed (tenant slugs aren't derivable from a
    # company name) - it's learned from an apply_url actually seen in a
    # discovered posting (docs/DECISIONS.md D45). ats_slug is stored as
    # "{tenant}:{site}", e.g. "ntrs:northerntrust".
    workday = "workday"
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


class SuggestionKind(str, enum.Enum):
    # Job-related email that correlates to nothing tracked (D20): confirming
    # creates a stub Company/Job/Application; declining leaves everything as-is.
    untracked_application = "untracked_application"
    # Email implies a state that contradicts a human-set one (D24): confirming
    # applies the email's implied state; declining keeps the manual state.
    state_conflict = "state_conflict"


class SuggestionStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    declined = "declined"


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
    # Classified once by the LLM from a controlled vocabulary; see industry.py
    industry: Mapped[str | None] = mapped_column(String(32))
    # Email domain (e.g. "stripe.com"), learned only from confirmed evidence -
    # a confirmed inbox suggestion or a high-confidence correlation - never
    # guessed from the name. Strengthens future deterministic email matching.
    domain: Mapped[str | None] = mapped_column(String(255))
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
    # Only JSearch's API states this explicitly (docs/DECISIONS.md D51) -
    # null means "unknown" (every other source, or a JSearch result that
    # didn't say), never "assumed indirect" (D10).
    apply_is_direct: Mapped[bool | None] = mapped_column()
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fit_score: Mapped[float | None] = mapped_column(Float)
    fit_reasoning: Mapped[str | None] = mapped_column(Text)
    # Per-dimension breakdown behind fit_score (docs/DECISIONS.md D17): not shown
    # in `jobs list` to avoid clutter, only in `jobs show <id>`. The model still
    # produces fit_score as its own holistic judgment, not an average of these.
    skill_match_score: Mapped[float | None] = mapped_column(Float)
    experience_level_score: Mapped[float | None] = mapped_column(Float)
    domain_fit_score: Mapped[float | None] = mapped_column(Float)
    location_fit_score: Mapped[float | None] = mapped_column(Float)
    visa_feasibility_score: Mapped[float | None] = mapped_column(Float)
    visa_signal: Mapped[VisaSignal] = mapped_column(
        Enum(VisaSignal), default=VisaSignal.unknown
    )
    # Phase 5 (docs/DECISIONS.md D43): a distinct question from fit_score -
    # "will this get a reply" vs "is this a good fit" - so it's a separate
    # column and a separate sort, not blended into fit_score or rank_jobs.
    response_likelihood_score: Mapped[float | None] = mapped_column(Float)
    response_likelihood_reasoning: Mapped[str | None] = mapped_column(Text)
    # Content hash of the resume file this job was scored against (docs/
    # DECISIONS.md D47) - lets the system tell a stale score apart from a
    # current one with zero LLM cost, instead of silently showing an old
    # fit_score as if it reflects the resume on file right now. Null means
    # "never scored" or "scored before this column existed" - either way,
    # not confirmed fresh.
    scored_resume_hash: Mapped[str | None] = mapped_column(String(64))
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
    notes: Mapped[str | None] = mapped_column(Text)
    # Phase 6 (docs/DECISIONS.md D44): the latest generated interview-prep
    # brief, not a history - regenerating overwrites, same one-shot-artifact
    # pattern as packet_base.md, just per-application instead of global.
    prep_brief: Mapped[str | None] = mapped_column(Text)
    prep_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    job: Mapped[Job] = relationship(back_populates="application")
    email_events: Mapped[list[EmailEvent]] = relationship(back_populates="application")
    state_history: Mapped[list[ApplicationStateEvent]] = relationship(
        back_populates="application", order_by="ApplicationStateEvent.created_at"
    )


class ApplicationStateEvent(Base):
    """Append-only log of Application.state transitions, tagged by who caused
    them. Application.state alone can't answer "was this human-set or
    system-set" - needed once Phase 4's email agent becomes a second writer
    to the same field, so a genuine conflict can be told apart from plain
    forward progress (docs/DECISIONS.md D24)."""

    __tablename__ = "application_state_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    from_state: Mapped[ApplicationState | None] = mapped_column(Enum(ApplicationState))
    to_state: Mapped[ApplicationState] = mapped_column(Enum(ApplicationState))
    # "manual" (CLI/dashboard action) or "email:<email_events.id>" once Phase 4 writes here.
    source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    application: Mapped[Application] = relationship(back_populates="state_history")


class EmailEvent(Base):
    """One classified, correlated inbox email. Only ever created once the
    email is tied to a real application - an uncorrelated message lives in
    `suggestions` instead until confirmed (D20), which is why application_id
    stays a required FK. The full body is stored locally (user's call,
    2026-07-25): the review surfaces show complete evidence, and later
    features (reply drafting, re-classification) reuse it without re-fetching.
    """

    __tablename__ = "email_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    # Idempotency keys for inbox sync: a message already present here (or in
    # suggestions) is never processed twice. Uniqueness enforced in code, not
    # DDL - the additive micro-migration can't add constraints to existing
    # tables, and single-writer SQLite makes check-before-insert safe.
    # provider_message_id is the Graph/API id (needed to act on the message,
    # but Outlook changes it on every move); internet_message_id is the
    # RFC 5322 Message-ID header, stable across moves - it's what keeps a
    # manually-moved email from being processed twice (intake folder, D28).
    provider_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    internet_message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    provider_thread_id: Mapped[str] = mapped_column(String(255))
    sender: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    classified_type: Mapped[EmailEventType] = mapped_column(
        Enum(EmailEventType), default=EmailEventType.other
    )
    # The application state this email implies (e.g. interview_invite ->
    # interviewing); None when the type carries no state signal ("other").
    implied_state: Mapped[ApplicationState | None] = mapped_column(Enum(ApplicationState))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Best-effort Mac Calendar event for an interview_invite/online_assessment
    # with a stated date (docs/DECISIONS.md D49) - never retried once True,
    # so a flaky Calendar.app call can't double-add on the next sync.
    calendar_event_added: Mapped[bool] = mapped_column(default=False)

    application: Mapped[Application] = relationship(back_populates="email_events")


class Suggestion(Base):
    """Persistent review queue for inbox findings that need a human decision:
    untracked applications (D20) and state conflicts (D24). Rows survive
    across sync runs so an answer is permanent - declining once means never
    being asked about that message again - and pending items aren't
    re-derived every hour."""

    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SuggestionKind] = mapped_column(Enum(SuggestionKind))
    status: Mapped[SuggestionStatus] = mapped_column(
        Enum(SuggestionStatus), default=SuggestionStatus.pending, index=True
    )
    # Dedupe keys: one suggestion per inbox message, ever (see EmailEvent for
    # why there are two ids).
    provider_message_id: Mapped[str] = mapped_column(String(255), index=True)
    internet_message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    provider_thread_id: Mapped[str | None] = mapped_column(String(255))
    # Set for state_conflict; null for untracked_application (no application
    # exists yet - confirming is what creates it).
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"))
    # Evidence shown on the review surfaces.
    sender: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What confirming would do: create Company/Job/Application at
    # proposed_state (untracked_application) or move the existing application
    # to proposed_state (state_conflict).
    company_name: Mapped[str | None] = mapped_column(String(255))
    job_title: Mapped[str | None] = mapped_column(String(255))
    proposed_state: Mapped[ApplicationState | None] = mapped_column(Enum(ApplicationState))
    classified_type: Mapped[EmailEventType | None] = mapped_column(Enum(EmailEventType))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    application: Mapped[Application | None] = relationship()
