"""LLM fit scoring: rate each discovered job against the resume, once.

Batches several jobs per Claude call (resume + rubric are shared context;
batching amortizes that cost across jobs, same call shape as industry
classification and dealbreaker compilation - docs/DECISIONS.md D13/D14). The
rubric produces a 0-100 score with written reasoning, plus the job's explicit
visa/sponsorship signal extracted from the JD text in the same call, since
scoring already reads it - no separate pass needed. Company-level sponsorship
evidence (public H1B filing data) is a distinct, non-LLM lookup; see
scoring/sponsorship.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from anthropic import Anthropic
from pydantic import BaseModel, Field

from copilot.config import Profile
from copilot.db.models import VisaSignal
from copilot.resume import ResumeProfile

_JD_TEXT_LIMIT = 6000  # characters; keeps batched prompts a sane size


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

CANDIDATE:
{resume_block}

JOBS TO SCORE:
{jobs_block}

Return one score per job_id listed above."""


def _resume_block(resume: ResumeProfile) -> str:
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
    return (
        f"--- job_id: {job.id} ---\n"
        f"{job.title} at {job.company} | {job.location or 'unknown location'}"
        f"{' (remote)' if job.remote else ''}{salary}\n"
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

    resume_block = _resume_block(resume)
    client = Anthropic()

    for i in range(0, len(jobs), batch_size):
        batch = jobs[i : i + batch_size]
        jobs_block = "\n\n".join(_job_block(j) for j in batch)
        response = client.messages.parse(
            model=profile.llm.model,
            max_tokens=8000,
            messages=[
                {
                    "role": "user",
                    "content": PROMPT_TEMPLATE.format(
                        resume_block=resume_block, jobs_block=jobs_block
                    ),
                }
            ],
            output_format=BatchScores,
        )
        for score in response.parsed_output.scores:
            results[score.job_id] = score

    return results
