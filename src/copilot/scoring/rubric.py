"""LLM fit scoring: rate each discovered job against the resume, once.

Batches several jobs per Claude call (resume + rubric are shared context;
batching amortizes that cost across jobs, same call shape as industry
classification and dealbreaker compilation - docs/DECISIONS.md D13/D14). The
rubric produces a 0-100 score with written reasoning, plus the job's explicit
visa/sponsorship signal extracted from the JD text in the same call, since
scoring already reads it - no separate pass needed. Company-level sponsorship
evidence (public H1B filing data) is a distinct, non-LLM lookup; see
scoring/sponsorship.py.

Phase 5 (docs/DECISIONS.md D43) piggybacks a second judgment on the same
call: response_likelihood_score - "will this get a reply", a different
question from fit_score's "is this a good fit" (a great-fit posting at a
huge company can still be a long shot; a mediocre-fit niche direct-ATS
posting can be a good bet). No source reports actual applicant counts, so
competition is a holistic judgment from posting age, application channel,
and how generic/prominent the role and company read - not a formula. The
user's own historical response rate is a deliberately absent input for now:
with only a handful of tracked applications so far, there isn't enough data
to calibrate anything real, and guessing would just fabricate confidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from copilot import llm
from copilot.config import Profile
from copilot.db.models import Job, VisaSignal
from copilot.formatting import posting_age_days
from copilot.resume import ResumeProfile, resume_content_hash

_JD_TEXT_LIMIT = 6000  # characters; keeps batched prompts a sane size

# Direct employer ATS vs a broad-net aggregator/board repost - the same
# posting is easier to stand out in on the former (docs/DECISIONS.md D43).
_DIRECT_ATS_SOURCES = {"greenhouse", "lever", "ashby"}


def _channel_label(source: str) -> str:
    if source in _DIRECT_ATS_SOURCES:
        return "direct employer ATS"
    if source in ("adzuna", "jsearch"):
        return "aggregator repost"
    if source == "email":
        return "manually tracked (from an inbox email)"
    return f"job board ({source})"


@dataclass
class JobToScore:
    id: int
    title: str
    company: str
    location: str | None
    remote: bool | None
    salary_min: int | None
    salary_max: int | None
    jd_text: str | None
    source: str = "unknown"
    posting_age_days: int | None = None


class JobScore(BaseModel):
    job_id: int
    skill_match_score: int = Field(ge=0, le=100)
    experience_level_score: int = Field(ge=0, le=100)
    domain_fit_score: int = Field(ge=0, le=100)
    location_fit_score: int = Field(ge=0, le=100)
    visa_feasibility_score: int = Field(ge=0, le=100)
    fit_score: int = Field(
        ge=0,
        le=100,
        description="Your own holistic judgment - not an average of the five "
        "dimension scores above. Weigh skill match and experience level most "
        "heavily; a job can score high overall despite one weak dimension, or "
        "low overall despite strong individual dimensions, if that's the honest "
        "read.",
    )
    reasoning: str = Field(
        description="2-4 sentences citing specific resume experience vs this JD"
    )
    response_likelihood_score: int = Field(
        ge=0,
        le=100,
        description="0-100: how likely a real reply (not necessarily an offer) is, "
        "independent of fit_score - a great-fit posting at a famous company with "
        "hundreds of applicants can be a worse bet than a decent-fit niche direct-ATS "
        "posting. Weigh posting age (fresher = fewer applicants so far), application "
        "channel (direct employer ATS beats an aggregator repost, which is likely "
        "cross-posted everywhere), and how generic/prominent the title and company "
        "are (a common title at a famous company draws the most competition).",
    )
    response_likelihood_reasoning: str = Field(
        description="1-2 sentences naming the specific factors that drove the score "
        "(e.g. 'fresh direct-ATS posting for a niche title, but a well-known employer "
        "will still draw volume')"
    )
    visa_signal: VisaSignal = Field(
        description="explicit_yes if the JD states it sponsors/supports work visas, "
        "explicit_no if it states US citizenship/no-sponsorship requirements, "
        "unknown if the JD says nothing either way"
    )


class BatchScores(BaseModel):
    scores: list[JobScore]


PROMPT_TEMPLATE = """You are scoring how well each job below fits this candidate. Score
each of these five dimensions 0-100, independently:

1. skill_match_score - do the candidate's actual skills/tools cover what the job asks for?
2. experience_level_score - does the candidate's seniority and years match the role's level?
3. domain_fit_score - has the candidate worked in a similar industry/problem space?
4. location_fit_score - is the job's location workable for the candidate?
5. visa_feasibility_score - does anything in the JD suggest a problem for someone who
   will need H1B sponsorship/transfer? (100 = no concern, low = explicit blocker)

Then give fit_score, your own holistic 0-100 judgment of overall fit - weigh skill match
and experience level most heavily. This is NOT an average of the five scores above: a
job can score high overall on the strength of skills/experience despite one weak
dimension, or low overall despite decent individual scores, if that is the honest
holistic read.

Be honest and discriminating: scores should spread across the range based on real fit,
not cluster near 70. A job requiring skills the candidate doesn't have should score low
even if the title matches. Reasoning should be concrete - name the specific skills or
experience that matched or didn't, not generic praise.

Also extract visa_signal for each job, based ONLY on what the JD explicitly states:
- explicit_yes: JD states it sponsors/supports work visas (e.g. "H1B sponsorship available")
- explicit_no: JD states US citizenship, security clearance, or "no sponsorship" requirements
- unknown: JD says nothing either way (the default - most JDs don't mention visas)

Separately, give response_likelihood_score: how likely a real reply is, independent of
fit_score - this is "will they get back to me", not "should they." No applicant-count data
exists, so judge competition from what's given for each job: posting age in days (fresher
postings have had less time to accumulate applicants), channel (a direct employer ATS
posting is easier to stand out in than an aggregator repost, which is likely cross-posted
on many boards at once), and how generic or prominent the title/company read (a common
title at a well-known company draws the most volume; a niche title or a smaller/less
prominent company draws less). State the specific factors in response_likelihood_reasoning.

CANDIDATE:
{resume_block}

JOBS TO SCORE:
{jobs_block}

Return one score per job_id listed above."""


def resume_block(resume: ResumeProfile) -> str:
    experience = "\n".join(
        f"  - {w.title} at {w.company} ({w.start or '?'} to {w.end or 'present'}): "
        + "; ".join(w.highlights)
        for w in resume.work_experience
    )
    return (
        f"{resume.full_name} - {resume.seniority}, {resume.years_of_experience:g} years\n"
        f"Summary: {resume.summary}\n"
        f"Skills: {', '.join(resume.skills)}\n"
        f"Domains: {', '.join(resume.domains)}\n"
        f"Experience:\n{experience}"
    )


def _job_block(job: JobToScore) -> str:
    salary = ""
    if job.salary_min or job.salary_max:
        salary = f", salary {job.salary_min or '?'}-{job.salary_max or '?'}"
    jd = (job.jd_text or "(no description text available)")[:_JD_TEXT_LIMIT]
    age = "unknown posting age" if job.posting_age_days is None else f"posted {job.posting_age_days}d ago"
    return (
        f"--- job_id: {job.id} ---\n"
        f"{job.title} at {job.company} | {job.location or 'unknown location'}"
        f"{' (remote)' if job.remote else ''}{salary}\n"
        f"{age} | via {_channel_label(job.source)}\n"
        f"{jd}"
    )


def score_jobs(
    profile: Profile, resume: ResumeProfile, jobs: list[JobToScore], batch_size: int = 8
) -> dict[int, JobScore]:
    """Score jobs in batches of `batch_size`. Returns {job_id: JobScore} - jobs the
    model fails to return a score for are simply absent from the result, left
    unscored for a future run rather than guessed at."""
    results: dict[int, JobScore] = {}
    if not jobs:
        return results

    resume_text = resume_block(resume)

    for i in range(0, len(jobs), batch_size):
        batch = jobs[i : i + batch_size]
        jobs_block = "\n\n".join(_job_block(j) for j in batch)
        response = llm.parse(
            model=profile.llm.model,
            max_tokens=8000,
            messages=[
                {
                    "role": "user",
                    "content": PROMPT_TEMPLATE.format(
                        resume_block=resume_text, jobs_block=jobs_block
                    ),
                }
            ],
            output_format=BatchScores,
        )
        for score in response.parsed_output.scores:
            results[score.job_id] = score

    return results


@dataclass
class ScoreRunResult:
    scored: int
    missing: int


def score_and_apply(
    session: Session, profile: Profile, resume: ResumeProfile, jobs: list[Job], batch_size: int = 8
) -> ScoreRunResult:
    """Score `jobs` (ORM rows, not yet scored) and write the results back onto
    them, committing - the shared apply step both `copilot score` and the
    dashboard's score routes use, so what "scored" means can't drift between
    the two (same reasoning as ranking.py's single rank_jobs())."""
    if not jobs:
        return ScoreRunResult(scored=0, missing=0)
    resume_hash = resume_content_hash(profile.resume_path)
    to_score = [
        JobToScore(
            id=j.id, title=j.title, company=j.company.name if j.company else "?",
            location=j.location, remote=j.remote, salary_min=j.salary_min,
            salary_max=j.salary_max, jd_text=j.jd_text, source=j.source,
            posting_age_days=posting_age_days(j),
        )
        for j in jobs
    ]
    scores = score_jobs(profile, resume, to_score, batch_size=batch_size)
    by_id = {j.id: j for j in jobs}
    for job_id, result in scores.items():
        job = by_id[job_id]
        job.fit_score = result.fit_score
        job.fit_reasoning = result.reasoning
        job.visa_signal = result.visa_signal
        job.skill_match_score = result.skill_match_score
        job.experience_level_score = result.experience_level_score
        job.domain_fit_score = result.domain_fit_score
        job.location_fit_score = result.location_fit_score
        job.visa_feasibility_score = result.visa_feasibility_score
        job.response_likelihood_score = result.response_likelihood_score
        job.response_likelihood_reasoning = result.response_likelihood_reasoning
        job.scored_resume_hash = resume_hash
    session.commit()
    return ScoreRunResult(scored=len(scores), missing=len(jobs) - len(scores))


def is_score_stale(job: Job, current_resume_hash: str) -> bool:
    """True if this job has a fit score but it wasn't confirmed scored
    against the resume as it exists right now - either scored under a
    resume that's since been replaced, or scored before this tracking
    existed (scored_resume_hash is null). Never claims a score is fresh
    without positive confirmation (docs/DECISIONS.md D47)."""
    return job.fit_score is not None and job.scored_resume_hash != current_resume_hash


def confirm_scores_still_fresh(session: Session, current_resume_hash: str) -> int:
    """The user's own override for a resume edit they know didn't change
    anything scoring-relevant (typo fix, reformatting) - re-stamps every
    scored job's resume hash to the current one without touching the score
    itself or spending anything. A deliberate escape hatch, not an attempt
    to auto-detect "cosmetic" edits - that would mean guessing at something
    only the user actually knows (docs/DECISIONS.md D48)."""
    jobs = session.scalars(select(Job).where(Job.fit_score.is_not(None))).all()
    updated = 0
    for job in jobs:
        if job.scored_resume_hash != current_resume_hash:
            job.scored_resume_hash = current_resume_hash
            updated += 1
    session.commit()
    return updated


def stale_score_filter(current_resume_hash: str):
    """SQL-level equivalent of is_score_stale's "not confirmed fresh" half,
    for querying without loading every Job into Python first. SQL's `!=` is
    NULL (not TRUE) when the column itself is NULL, so a plain inequality
    would silently exclude every job scored before scored_resume_hash
    existed - exactly the jobs this feature most needs to catch - unless
    the NULL case is named explicitly."""
    return Job.scored_resume_hash.is_(None) | (Job.scored_resume_hash != current_resume_hash)
