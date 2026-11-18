"""Interview prep briefs (Phase 6, docs/DECISIONS.md D44).

One Claude call with live web search per application - the same
web_search_20260209 + pause_turn pattern as title_suggestions.py's market
research, reused rather than reinvented. Generation only ever happens on
explicit request (`copilot prep <id>` or the dashboard's "Generate prep
brief" button), never automatically, same "assist, don't surprise" principle
as every other paid action in this codebase. Regenerating overwrites the
previous brief - this is a snapshot for an upcoming interview, not a history.

No round-type/interview-stage field exists anywhere in the schema, and no
JD reliably states one either, so "likely question areas per round type"
can't be a structured lookup - Claude is asked to infer what it can from the
JD and any stored interview-related email text, and to say plainly when the
format is unclear rather than invent rounds that were never mentioned. The
same honesty applies to company research: web search finds public interview
reports (Glassdoor/Blind-style), not this specific process, and the prompt
says so explicitly rather than letting the brief imply otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from copilot import llm
from copilot.config import Profile
from copilot.db.models import Application, EmailEventType
from copilot.resume import ResumeProfile
from copilot.scoring.rubric import resume_block

MAX_PAUSE_RESUMES = 3
_JD_TEXT_LIMIT = 6000

# Email types worth quoting for interview-format hints (scheduling logistics,
# round structure) - rejection/confirmation carry nothing useful here.
_INTERVIEW_EMAIL_TYPES = (
    EmailEventType.interview_invite,
    EmailEventType.online_assessment,
    EmailEventType.recruiter_screen,
)


@dataclass
class PrepInput:
    title: str
    company: str
    location: str | None
    jd_text: str | None
    fit_score: float | None
    fit_reasoning: str | None
    skill_match_score: float | None
    experience_level_score: float | None
    domain_fit_score: float | None
    interview_emails: list[str] = field(default_factory=list)


PROMPT_TEMPLATE = """Write an interview prep brief for this candidate, for the specific
role and company below. Use web search to find real, current information about this
company's interview process, culture, and recent news - not just training knowledge.

CANDIDATE:
{resume_block}

ROLE:
{role_block}

{gap_block}
{interview_context_block}
Structure the brief with these sections, in Markdown:

## Company & role snapshot
What this company/team actually does, and what this specific role is responsible for,
grounded in the JD and what you find searching.

## Their interview process
Search for real reports of this company's interview process for this kind of role
(Glassdoor, Blind, engineering blogs, etc.) - typical rounds, format, what's known to be
emphasized. Say plainly if you can't find anything specific to this company/role rather
than inventing a generic process and presenting it as their process.

## Your gap areas to prepare for
Concrete prep tied to the candidate's actual weaker dimensions and the fit reasoning
above - not generic interview advice. Name specific things to review or be ready to
discuss.

## Likely question areas
Grounded in the JD and any interview-context notes above; general for the role/level
where nothing more specific is known. Don't claim to know the exact round structure
unless it's actually stated somewhere above.

## Questions to ask them
3-5 genuinely useful questions for this specific role/company, not generic ones.

Keep it concrete and skimmable - the candidate needs to use this before a real
interview, not read a essay."""


def _jd_block(jd_text: str | None) -> str:
    if not jd_text:
        return "(no job description text stored for this posting)"
    return jd_text[:_JD_TEXT_LIMIT]


def _gap_block(prep_input: PrepInput) -> str:
    if prep_input.fit_score is None:
        return "FIT ASSESSMENT: not yet scored - lean on the JD and resume directly."
    dims = (
        f"skill match {prep_input.skill_match_score:.0f}/100, "
        f"experience level {prep_input.experience_level_score:.0f}/100, "
        f"domain fit {prep_input.domain_fit_score:.0f}/100"
    )
    return (
        f"FIT ASSESSMENT (overall {prep_input.fit_score:.0f}/100, {dims}):\n"
        f"{prep_input.fit_reasoning or ''}"
    )


def _interview_context_block(prep_input: PrepInput) -> str:
    if not prep_input.interview_emails:
        return ""
    joined = "\n---\n".join(prep_input.interview_emails)
    return f"\nNOTES FROM INTERVIEW-RELATED EMAILS (may hint at format/logistics):\n{joined}\n"


def generate_prep_brief(profile: Profile, resume: ResumeProfile, prep_input: PrepInput) -> str:
    """Pure generation, no DB - mirrors title_suggestions.py's resume-on-pause_turn
    pattern for server-executed web search."""
    role_block = (
        f"{prep_input.title} at {prep_input.company} | "
        f"{prep_input.location or 'unknown location'}\n"
        f"{_jd_block(prep_input.jd_text)}"
    )
    prompt = PROMPT_TEMPLATE.format(
        resume_block=resume_block(resume),
        role_block=role_block,
        gap_block=_gap_block(prep_input),
        interview_context_block=_interview_context_block(prep_input),
    )

    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    messages: list[dict] = [{"role": "user", "content": prompt}]

    response = llm.create(model=profile.llm.model, max_tokens=6000, tools=tools, messages=messages)
    resumes = 0
    while response.stop_reason == "pause_turn" and resumes < MAX_PAUSE_RESUMES:
        messages.append({"role": "assistant", "content": response.content})
        response = llm.create(model=profile.llm.model, max_tokens=6000, tools=tools, messages=messages)
        resumes += 1

    return _final_answer_text(response.content).strip()


def _final_answer_text(content: list) -> str:
    """Web search is interleaved into one response: text narration ("let me
    check their culture next"), then a server_tool_use/web_search_tool_result
    pair, repeated per search, with the real finished answer as the trailing
    text block(s) after the last search. Joining every text block (the
    original approach) prepends that search narration to the brief - caught
    live, where a real brief opened with "Good, the data is still in memory.
    Let me extract the content fields I need..." instead of its actual first
    section. Only the contiguous run of text blocks after the last non-text
    block is the answer; if there's no tool activity at all, every text
    block is the answer, same as before."""
    last_non_text = -1
    for i, block in enumerate(content):
        if block.type != "text":
            last_non_text = i
    return "".join(block.text for block in content[last_non_text + 1 :] if block.type == "text")


def prep_application(
    session: Session, profile: Profile, resume: ResumeProfile, application: Application
) -> str:
    """Build the prompt inputs from the real ORM data, generate, and persist -
    the shared apply step for both `copilot prep` and the dashboard's button,
    same split as scoring.rubric.score_and_apply."""
    job = application.job
    interview_emails = [
        f"{event.classified_type.value}: {event.subject or '(no subject)'}\n"
        f"{(event.body_text or '')[:1000]}"
        for event in application.email_events
        if event.classified_type in _INTERVIEW_EMAIL_TYPES
    ]
    prep_input = PrepInput(
        title=job.title,
        company=job.company.name if job.company else "?",
        location=job.location,
        jd_text=job.jd_text,
        fit_score=job.fit_score,
        fit_reasoning=job.fit_reasoning,
        skill_match_score=job.skill_match_score,
        experience_level_score=job.experience_level_score,
        domain_fit_score=job.domain_fit_score,
        interview_emails=interview_emails,
    )
    brief = generate_prep_brief(profile, resume, prep_input)
    application.prep_brief = brief
    application.prep_generated_at = datetime.now(timezone.utc)
    session.commit()
    return brief
