"""Claude classification of a pre-filtered inbox email: what kind of
recruiting email is it, and which candidate application (if any) does it
belong to - one call answering both, since they read the same text.

The implied application state is mapped from the classified type in code,
not asked of the model: the type->state mapping is a fixed product decision
(docs/DECISIONS.md D24), and keeping it out of the prompt means the model
can't invent a state the ladder doesn't have.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from copilot import llm
from copilot.db.models import ApplicationState, EmailEventType
from copilot.email_agent.correlator import Candidate
from copilot.email_agent.provider import InboundMessage

_BODY_LIMIT = 4000  # characters; recruiting emails are short, footers are not

# What each email type says about where the application now stands.
# "other" and "confirmation-like but vague" carry no state signal.
IMPLIED_STATE: dict[EmailEventType, ApplicationState | None] = {
    EmailEventType.confirmation: ApplicationState.applied,
    EmailEventType.rejection: ApplicationState.rejected,
    EmailEventType.interview_invite: ApplicationState.interviewing,
    EmailEventType.online_assessment: ApplicationState.interviewing,
    EmailEventType.recruiter_screen: ApplicationState.replied,
    EmailEventType.other: None,
}


class EmailClassification(BaseModel):
    job_related: bool = Field(
        description="true only if this email is about a job application, "
        "interview, assessment, or recruiting outreach for a specific role. "
        "Job-board marketing blasts, newsletters, and 'jobs you may like' "
        "digests are NOT job_related."
    )
    classified_type: EmailEventType = Field(
        description="confirmation = 'we received your application'; "
        "rejection = explicit no; interview_invite = scheduling/invite for an "
        "interview; online_assessment = coding test / HackerRank / take-home; "
        "recruiter_screen = a human recruiter reaching out or replying; "
        "other = job-related but none of the above"
    )
    matched_candidate_id: int | None = Field(
        description="The candidate number this email is about, chosen from "
        "CANDIDATE APPLICATIONS - or null if it matches none of them (or no "
        "candidates were given). Only pick a candidate you are confident "
        "about; null is always the safe answer."
    )
    company_name: str | None = Field(
        description="The hiring company's name as the email states it "
        "(the employer, not the ATS or job board sending the mail)"
    )
    job_title: str | None = Field(
        description="The role's title if the email mentions it, else null"
    )


PROMPT_TEMPLATE = """Classify this email from a job seeker's inbox.

EMAIL:
From: {sender_name} <{sender}>
Subject: {subject}
Received: {received_at}

{body}

CANDIDATE APPLICATIONS (the seeker's tracked applications this email might be about):
{candidates_block}

Rules:
- matched_candidate_id must be one of the candidate numbers above, or null. When unsure, null.
- The company is the employer, never the ATS platform (Greenhouse/Lever/Workday) or job board.
- Automated 'we received your application' mail is confirmation, not recruiter_screen."""


def _candidates_block(candidates: list[Candidate]) -> str:
    if not candidates:
        return "(none tracked that plausibly match)"
    return "\n".join(
        f"{i}. {c.company.name} - {c.job.title} (current state: {c.application.state.value})"
        for i, c in enumerate(candidates, start=1)
    )


def classify_email(
    message: InboundMessage, candidates: list[Candidate], model: str
) -> EmailClassification:
    response = llm.parse(
        model=model,
        max_tokens=1000,
        messages=[
            {
                "role": "user",
                "content": PROMPT_TEMPLATE.format(
                    sender_name=message.sender_name,
                    sender=message.sender,
                    subject=message.subject,
                    received_at=message.received_at.isoformat(),
                    body=message.body_text[:_BODY_LIMIT],
                    candidates_block=_candidates_block(candidates),
                ),
            }
        ],
        output_format=EmailClassification,
    )
    return response.parsed_output


def pick_candidate(
    classification: EmailClassification, candidates: list[Candidate]
) -> Candidate | None:
    """Resolve the model's 1-based pick back to a Candidate; out-of-range or
    null picks mean uncorrelated."""
    idx = classification.matched_candidate_id
    if idx is None or not (1 <= idx <= len(candidates)):
        return None
    return candidates[idx - 1]
