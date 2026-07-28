"""Inbox sync orchestration: the five-stage pipeline that turns raw inbox
mail into application state updates, filed emails, suggestions, and alerts.

Stages, cheapest first - each exists to shrink what reaches the next:
1. watermark   - only fetch mail newer than the last successful sync
2. pre-filter  - deterministic ATS-sender/subject patterns; zero LLM. A miss
                 gets one rescue check: a thread id we've classified before
                 is known job mail even when the subject mutates to "Re: ...".
                 Mail failing both is skipped (subjects surfaced in --dry-run
                 so filter recall stays auditable)
3. idempotency - a message id already in email_events or suggestions is done
4. correlate   - deterministic candidate matching (correlator.py)
5. classify+act- one Claude call (classifier.py), then the D20/D24 rules:
                 forward progress writes state, conflicts and untracked
                 applications become Suggestions, never silent writes

A second entry lane skips stages 1-2 entirely: the intake folder ("Career
Copilot/Process" in the mailbox, D28). Anything the user drags there is
processed on the next run regardless of age or filter patterns - the human
override for mail the pipeline missed. Idempotency (stage 3) still applies,
keyed on the move-stable Internet Message-ID rather than the provider id,
which Outlook regenerates on every move. The filed "Career Copilot" folder
itself is also swept as a forgiveness measure (D29): a headers-only scan
where known mail dedupes out instantly, so dropping new mail one folder too
high still gets it processed (in place, without re-filing).

Everything acts through a single session commit per message, so a crash
mid-run loses at most the in-flight message - the watermark only advances
after the run completes, and stage 3 makes re-processing the finished ones
free.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from copilot.applications import advance_from_email, state_relation
from copilot.config import DATA_DIR, Profile
from copilot.db.models import (
    ApplicationStateEvent,
    EmailEvent,
    EmailEventType,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
)
from copilot.email_agent.classifier import IMPLIED_STATE, classify_email, pick_candidate
from copilot.email_agent.correlator import find_candidates, find_thread_candidates, learn_domain
from copilot.email_agent.provider import EmailProvider, InboundMessage
from copilot.notify.alerts import ALERT_TYPES, send_event_alert

SYNC_STATE_PATH = DATA_DIR / "inbox_state.json"
FIRST_RUN_LOOKBACK_DAYS = 7
# Alerts exist to beat a clock; an invite/OA email older than this has
# already run its clock (typically old mail dropped in the intake folder) -
# record and file it, but don't buzz the user about it.
ALERT_MAX_AGE = timedelta(days=2)

# Shared ATS/recruiting mail domains. A match here passes the pre-filter but
# must never be learned as a company's own domain (correlator.learn_domain).
ATS_SENDER_DOMAINS = frozenset(
    {
        "greenhouse.io",
        "greenhouse-mail.io",
        "lever.co",
        "hire.lever.co",
        "ashbyhq.com",
        "myworkday.com",
        "workday.com",
        "icims.com",
        "smartrecruiters.com",
        "jobvite.com",
        "bamboohr.com",
        "successfactors.com",
        "taleo.net",
        "workablemail.com",
        "hackerrankforwork.com",
        "codesignal.com",
    }
)

_SUBJECT_PATTERNS = re.compile(
    r"your application|thank you for applying|application (received|update|status)"
    r"|interview|assessment|next steps|recruiting|recruiter|talent (team|acquisition)"
    r"|we('d| would) like to (meet|speak|talk)|phone screen|coding challenge|take.home",
    re.IGNORECASE,
)

CATEGORY_BY_TYPE = {
    EmailEventType.confirmation: "Confirmation",
    EmailEventType.rejection: "Rejection",
    EmailEventType.interview_invite: "Interview Invite",
    EmailEventType.online_assessment: "Online Assessment",
    EmailEventType.recruiter_screen: "Recruiter Screen",
    EmailEventType.other: "Job Mail",
}


def _last_synced_at(state_path: Path = SYNC_STATE_PATH) -> datetime:
    if state_path.exists():
        return datetime.fromisoformat(json.loads(state_path.read_text())["last_synced_at"])
    return datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)


def mark_synced(state_path: Path = SYNC_STATE_PATH) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"last_synced_at": datetime.now(timezone.utc).isoformat()})
    )


def prefilter(message: InboundMessage) -> bool:
    domain = message.sender.rsplit("@", 1)[-1] if "@" in message.sender else ""
    if domain in ATS_SENDER_DOMAINS or any(domain.endswith("." + d) for d in ATS_SENDER_DOMAINS):
        return True
    return bool(_SUBJECT_PATTERNS.search(message.subject))


def _already_processed(session: Session, message: InboundMessage) -> bool:
    # Two keys: the provider id (mutates when Outlook moves a message - so a
    # user dragging processed mail between folders gives it a fresh id) and
    # the Internet Message-ID header, which survives moves. Either match
    # means done.
    for model in (EmailEvent, Suggestion):
        clauses = [model.provider_message_id == message.message_id]
        if message.internet_message_id:
            clauses.append(model.internet_message_id == message.internet_message_id)
        if session.scalar(select(model.id).where(or_(*clauses))) is not None:
            return True
    return False


@dataclass
class SyncResult:
    fetched: int = 0
    intake: int = 0  # messages found in the manual intake folder
    skipped_prefilter: int = 0
    skipped_seen: int = 0
    events: int = 0
    advanced: int = 0
    conflicts: int = 0
    suggestions: int = 0
    alerts: int = 0
    # Human-readable one-liners, in processing order - the CLI prints these,
    # and --dry-run is just this list without any of the writes.
    actions: list[str] = field(default_factory=list)
    # "sender: subject" per pre-filter miss. A wrongly filtered message leaves
    # no other trace, so --dry-run prints these for the user to audit recall.
    skipped_subjects: list[str] = field(default_factory=list)


def run_sync(
    session: Session,
    provider: EmailProvider,
    profile: Profile,
    *,
    dry_run: bool = False,
    state_path: Path = SYNC_STATE_PATH,
) -> SyncResult:
    result = SyncResult()
    messages = provider.list_messages_since(_last_synced_at(state_path))
    result.fetched = len(messages)

    for message in messages:
        # A message in a thread we've already classified is job mail by
        # definition - the pattern filter never gets to veto a known thread.
        if not prefilter(message) and not find_thread_candidates(session, message):
            result.skipped_prefilter += 1
            result.skipped_subjects.append(f"{message.sender}: {message.subject}")
            continue
        if _already_processed(session, message):
            result.skipped_seen += 1
            continue
        _process_message(session, provider, profile, message, result, dry_run=dry_run)
        if not dry_run:
            session.commit()

    # Intake folder: mail the user dragged in by hand (D28). No watermark, no
    # pre-filter - a human placing a message there IS the classification
    # "this is job mail, look at it". Idempotency still applies (by the
    # move-stable internet_message_id), so already-processed mail that gets
    # dragged around is never double-counted.
    for message in provider.list_intake_messages():
        if _already_processed(session, message):
            result.skipped_seen += 1
            continue
        result.intake += 1
        _process_message(
            session, provider, profile, message, result,
            dry_run=dry_run, from_intake=True,
        )
        if not dry_run:
            session.commit()

    # Filed-area rescue (D29): "drop it on the Career Copilot folder" is the
    # nearly-right version of the intake gesture, and punishing it with
    # silence would be the failure mode D28 exists to close. Headers only -
    # everything Copilot filed itself dedupes out on its Message-ID in
    # milliseconds; only a stranger gets its body fetched and processed,
    # in place (it's already in the right folder, so no re-filing).
    for header in provider.list_filed_headers():
        if _already_processed(session, header):
            continue  # the normal case: mail Copilot filed itself
        result.intake += 1
        _process_message(
            session, provider, profile, provider.get_message(header.message_id),
            result, dry_run=dry_run, from_intake=True, skip_filing=True,
        )
        if not dry_run:
            session.commit()

    if not dry_run:
        mark_synced(state_path)
    return result


def _process_message(
    session: Session,
    provider: EmailProvider,
    profile: Profile,
    message: InboundMessage,
    result: SyncResult,
    *,
    dry_run: bool,
    from_intake: bool = False,
    skip_filing: bool = False,
) -> None:
    # A conversation already awaiting the user's decision holds ALL its
    # follow-ups - before classification, so a message parked in the intake
    # folder doesn't burn a Claude call every hour while the suggestion sits
    # unresolved. One conversation, one pending decision.
    if message.thread_id and session.scalar(
        select(Suggestion.id).where(
            Suggestion.provider_thread_id == message.thread_id,
            Suggestion.status == SuggestionStatus.pending,
        )
    ):
        result.actions.append(f"already awaiting review (same thread): {message.subject!r}")
        return

    # Thread continuity beats name/domain heuristics: a reply in a known
    # conversation is that application, full stop.
    candidates = find_thread_candidates(session, message) or find_candidates(session, message)
    classification = classify_email(message, candidates, model=profile.llm.model)

    if not classification.job_related and not from_intake:
        result.skipped_prefilter += 1
        result.actions.append(f"not job mail after all, ignored: {message.subject!r}")
        return
    # from_intake: the human vouched for it, so even a "not job mail" verdict
    # flows on - worst case it becomes a suggestion the user can decline
    # (which also stops it being re-examined every run while it sits there).

    candidate = pick_candidate(classification, candidates)
    implied = IMPLIED_STATE[classification.classified_type]
    category = CATEGORY_BY_TYPE[classification.classified_type]

    if candidate is None:
        # D20: job-related but tracks to nothing - suggest, touch nothing.
        result.suggestions += 1
        result.actions.append(
            f"suggestion (untracked application): {classification.company_name or '?'}"
            f" - {message.subject!r}"
        )
        if not dry_run:
            session.add(
                Suggestion(
                    kind=SuggestionKind.untracked_application,
                    provider_message_id=message.message_id,
                    internet_message_id=message.internet_message_id or None,
                    provider_thread_id=message.thread_id,
                    sender=message.sender,
                    subject=message.subject,
                    body_text=message.body_text,
                    received_at=message.received_at,
                    company_name=classification.company_name,
                    job_title=classification.job_title,
                    proposed_state=implied,
                    classified_type=classification.classified_type,
                )
            )
        _maybe_alert(
            classification.classified_type, message,
            classification.company_name or "unknown company", classification.job_title,
            result, dry_run=dry_run,
        )
        return

    application, job, company = candidate.application, candidate.job, candidate.company
    relation = "stale" if implied is None else state_relation(application.state, implied)

    if relation == "conflict":
        # D24: contradicts the stored state - flag it, write no state.
        result.conflicts += 1
        result.actions.append(
            f"CONFLICT flagged: {company.name} - email implies {implied.value}, "
            f"stored state is {application.state.value} ({_state_setter(session, application.id)})"
        )
        if not dry_run:
            session.add(
                Suggestion(
                    kind=SuggestionKind.state_conflict,
                    provider_message_id=message.message_id,
                    internet_message_id=message.internet_message_id or None,
                    provider_thread_id=message.thread_id,
                    application_id=application.id,
                    sender=message.sender,
                    subject=message.subject,
                    body_text=message.body_text,
                    received_at=message.received_at,
                    company_name=company.name,
                    # The EMAIL's extracted title, not the tracked job's: if
                    # the user resolves the conflict as "this is a different,
                    # new application" (D31), the new job must be built from
                    # what the email said, never a duplicate of the flagged
                    # job's title (whose dedupe hash would collide back onto
                    # the flagged application).
                    job_title=classification.job_title,
                    proposed_state=implied,
                    classified_type=classification.classified_type,
                )
            )
            if not skip_filing:
                provider.file_message(message.message_id, category=category)
    else:
        event = EmailEvent(
            application_id=application.id,
            provider_message_id=message.message_id,
            internet_message_id=message.internet_message_id or None,
            provider_thread_id=message.thread_id,
            sender=message.sender,
            subject=message.subject,
            body_text=message.body_text,
            classified_type=classification.classified_type,
            implied_state=implied,
            received_at=message.received_at,
        )
        result.events += 1
        if not dry_run:
            session.add(event)
            session.flush()  # event.id needed for the history tag
        if relation == "forward":
            result.advanced += 1
            result.actions.append(
                f"{company.name}: {application.state.value} -> {implied.value}"
                f" ({classification.classified_type.value})"
            )
            if not dry_run:
                advance_from_email(application, implied, event.id)
        else:
            result.actions.append(
                f"{company.name}: recorded {classification.classified_type.value},"
                f" state stays {application.state.value}"
            )
        if not dry_run:
            learn_domain(company, message.sender, ATS_SENDER_DOMAINS)
            if not skip_filing:
                provider.file_message(message.message_id, category=category)

    # Stale events are old news by definition (a duplicate ATS send, our own
    # historical mail) - recording them is right, buzzing the user is not
    # (found live 2026-07-26: a notification for an invite already acted on).
    if relation != "stale":
        _maybe_alert(
            classification.classified_type, message,
            company.name, job.title, result, dry_run=dry_run,
        )


def _maybe_alert(
    event_type: EmailEventType,
    message: InboundMessage,
    company_name: str,
    job_title: str | None,
    result: SyncResult,
    *,
    dry_run: bool,
) -> None:
    if event_type not in ALERT_TYPES:
        return
    if datetime.now(timezone.utc) - message.received_at > ALERT_MAX_AGE:
        result.actions.append(
            f"alert suppressed (email is older than {ALERT_MAX_AGE.days} days):"
            f" {event_type.value} at {company_name}"
        )
        return
    result.alerts += 1
    result.actions.append(f"desktop notification: {event_type.value} at {company_name}")
    if not dry_run:
        send_event_alert(event_type, message, company_name, job_title)


def _state_setter(session: Session, application_id: int) -> str:
    """Who last touched this application's state, for conflict messaging."""
    last = session.scalars(
        select(ApplicationStateEvent)
        .where(ApplicationStateEvent.application_id == application_id)
        .order_by(ApplicationStateEvent.created_at.desc(), ApplicationStateEvent.id.desc())
    ).first()
    if last is None:
        return "set before history tracking"
    return "human-set" if last.source == "manual" else f"set by {last.source}"
