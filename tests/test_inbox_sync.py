"""Sync pipeline tests: fake provider, stubbed classifier (no Claude calls),
real DB. Each test seeds a tracked Stripe application and drives run_sync
with canned inbox messages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from copilot.applications import mark_applied, state_relation
from copilot.config import Profile
from copilot.db import get_engine, get_session, init_db
from copilot.db.models import (
    ApplicationState,
    ApplicationStateEvent,
    Company,
    EmailEvent,
    EmailEventType,
    Job,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
)
from copilot.email_agent.classifier import EmailClassification
from copilot.email_agent.provider import EmailProvider, InboundMessage
from copilot.email_agent.sync import prefilter, run_sync

# Relative, not a fixed date: the alert age guard compares received_at to the
# real clock, and a pinned date would make alert tests start failing two days
# after it was written.
NOW = datetime.now(timezone.utc)


def _profile(tmp_path: Path) -> Profile:
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("resume content")
    return Profile.model_validate(
        {
            "identity": {"full_name": "Jane Doe", "email": "jane@example.com"},
            "resume_path": str(resume_path),
            "search": {"titles": ["Engineer"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "jane@hotmail.com"},
        }
    )


def _message(
    message_id: str = "m1",
    sender: str = "no-reply@greenhouse.io",
    sender_name: str = "Stripe",
    subject: str = "Thank you for applying to Stripe",
    body: str = "We received your application to the ML Engineer role.",
    internet_id: str = "",
    received_at: datetime = NOW,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        thread_id=f"t-{message_id}",
        sender=sender,
        sender_name=sender_name,
        subject=subject,
        body_text=body,
        received_at=received_at,
        internet_message_id=internet_id,
    )


class FakeProvider(EmailProvider):
    def __init__(
        self,
        messages: list[InboundMessage],
        intake: list[InboundMessage] | None = None,
        filed_folder: list[InboundMessage] | None = None,
    ):
        self.messages = messages
        self.intake = intake or []
        self.filed_folder = filed_folder or []  # contents of "Career Copilot"
        self.filed: list[tuple[str, str]] = []
        self.sent: list[str] = []
        self.full_fetches: list[str] = []  # get_message calls, for cost asserts

    def send_mail(self, *, to: str, subject: str, body_html: str) -> None:
        self.sent.append(subject)

    def list_messages_since(self, since: datetime) -> list[InboundMessage]:
        return self.messages

    def list_intake_messages(self) -> list[InboundMessage]:
        return self.intake

    def list_filed_headers(self) -> list[InboundMessage]:
        from dataclasses import replace

        return [replace(m, body_text="") for m in self.filed_folder]

    def get_message(self, message_id: str) -> InboundMessage:
        self.full_fetches.append(message_id)
        return next(m for m in self.filed_folder if m.message_id == message_id)

    def file_message(self, message_id: str, *, category: str) -> None:
        self.filed.append((message_id, category))


def _session(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    return get_session(engine)


def _seed_tracked_application(session, state: ApplicationState = ApplicationState.applied) -> int:
    company = Company(name="Stripe")
    session.add(company)
    session.flush()
    job = Job(
        company_id=company.id,
        title="ML Engineer",
        source="greenhouse",
        apply_url="https://stripe.com/jobs/1",
        dedupe_hash="stripe-ml",
    )
    session.add(job)
    session.flush()
    application = mark_applied(session, job.id)
    if state != ApplicationState.applied:
        application.state = state
    session.commit()
    return job.id


def _stub_classifier(monkeypatch, **overrides) -> None:
    defaults = dict(
        job_related=True,
        classified_type=EmailEventType.confirmation,
        matched_candidate_id=1,
        company_name="Stripe",
        job_title="ML Engineer",
    )
    defaults.update(overrides)
    classification = EmailClassification(**defaults)
    monkeypatch.setattr(
        "copilot.email_agent.sync.classify_email", lambda *a, **k: classification
    )


def test_prefilter_matches_ats_senders_and_subject_patterns():
    assert prefilter(_message(sender="no-reply@greenhouse.io", subject="anything"))
    assert prefilter(_message(sender="jobs@us.greenhouse.io", subject="anything"))
    assert prefilter(_message(sender="recruiter@stripe.com", subject="Your application update"))
    assert not prefilter(_message(sender="deals@retailer.com", subject="50% off this weekend"))


def test_sync_advances_state_forward_with_email_tagged_history(tmp_path, monkeypatch):
    with _session(tmp_path) as session:
        _seed_tracked_application(session)
        _stub_classifier(monkeypatch, classified_type=EmailEventType.interview_invite)
        provider = FakeProvider([_message()])

        result = run_sync(
            session, provider, _profile(tmp_path), state_path=tmp_path / "state.json"
        )

        assert result.advanced == 1
        event = session.query(EmailEvent).one()
        assert event.application.state == ApplicationState.interviewing
        history = (
            session.query(ApplicationStateEvent)
            .filter_by(to_state=ApplicationState.interviewing)
            .one()
        )
        assert history.source == f"email:{event.id}"
        assert provider.filed == [("m1", "Interview Invite")]


def test_sync_is_idempotent_across_runs(tmp_path, monkeypatch):
    with _session(tmp_path) as session:
        _seed_tracked_application(session)
        _stub_classifier(monkeypatch)
        provider = FakeProvider([_message()])
        profile = _profile(tmp_path)

        run_sync(session, provider, profile, state_path=tmp_path / "state.json")
        second = run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        assert second.skipped_seen == 1
        assert session.query(EmailEvent).count() == 1


def test_sync_flags_conflict_instead_of_overwriting(tmp_path, monkeypatch):
    with _session(tmp_path) as session:
        job_id = _seed_tracked_application(session, state=ApplicationState.rejected)
        _stub_classifier(monkeypatch, classified_type=EmailEventType.interview_invite)
        provider = FakeProvider([_message()])

        result = run_sync(
            session, provider, _profile(tmp_path), state_path=tmp_path / "state.json"
        )

        assert result.conflicts == 1
        suggestion = session.query(Suggestion).one()
        assert suggestion.kind == SuggestionKind.state_conflict
        assert suggestion.proposed_state == ApplicationState.interviewing
        job = session.get(Job, job_id)
        assert job.application.state == ApplicationState.rejected  # untouched


def test_sync_suggests_untracked_application_and_leaves_email_alone(tmp_path, monkeypatch):
    with _session(tmp_path) as session:
        # No tracked applications at all - nothing can correlate.
        _stub_classifier(monkeypatch, matched_candidate_id=None, company_name="Acme")
        provider = FakeProvider([_message(sender="careers@acme.com", sender_name="Acme")])

        result = run_sync(
            session, provider, _profile(tmp_path), state_path=tmp_path / "state.json"
        )

        assert result.suggestions == 1
        suggestion = session.query(Suggestion).one()
        assert suggestion.kind == SuggestionKind.untracked_application
        assert suggestion.status == SuggestionStatus.pending
        assert provider.filed == []  # D20: email untouched until confirmed


def test_sync_dry_run_writes_and_moves_nothing(tmp_path, monkeypatch):
    with _session(tmp_path) as session:
        _seed_tracked_application(session)
        _stub_classifier(monkeypatch, classified_type=EmailEventType.interview_invite)
        provider = FakeProvider([_message()])

        result = run_sync(
            session, provider, _profile(tmp_path), dry_run=True,
            state_path=tmp_path / "state.json",
        )

        assert result.advanced == 1  # reported as what *would* happen
        assert session.query(EmailEvent).count() == 0
        assert provider.filed == []
        assert provider.sent == []
        assert not (tmp_path / "state.json").exists()  # watermark not advanced


def test_sync_alerts_for_interview_invite_but_not_confirmation(tmp_path, monkeypatch):
    with _session(tmp_path) as session:
        _seed_tracked_application(session)
        provider = FakeProvider([_message()])
        profile = _profile(tmp_path)

        _stub_classifier(monkeypatch, classified_type=EmailEventType.confirmation)
        run_sync(session, provider, profile, state_path=tmp_path / "state.json")
        assert provider.sent == []

        provider.messages = [_message(message_id="m2", subject="Interview with Stripe")]
        _stub_classifier(monkeypatch, classified_type=EmailEventType.interview_invite)
        run_sync(session, provider, profile, state_path=tmp_path / "state.json")
        assert len(provider.sent) == 1
        assert "Interview invite" in provider.sent[0]


def test_known_thread_bypasses_prefilter_and_correlates(tmp_path, monkeypatch):
    """A reply like "Re: Next steps" carries no ATS domain and no pattern
    match - thread continuity is what keeps the conversation tracked."""
    with _session(tmp_path) as session:
        _seed_tracked_application(session)
        profile = _profile(tmp_path)

        # First message establishes the thread on the Stripe application.
        _stub_classifier(monkeypatch, classified_type=EmailEventType.recruiter_screen)
        provider = FakeProvider([_message(message_id="m1")])
        run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        # The reply would fail the pre-filter on its own merits...
        reply = InboundMessage(
            message_id="m2",
            thread_id="t-m1",  # ...but shares the classified thread.
            sender="jane.recruiter@stripe-mail.example",
            sender_name="Jane",
            subject="Re: Following up",
            body_text="Are you free Thursday?",
            received_at=NOW,
        )
        assert not prefilter(reply)
        _stub_classifier(monkeypatch, classified_type=EmailEventType.interview_invite)
        provider.messages = [reply]

        result = run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        assert result.skipped_prefilter == 0
        assert result.advanced == 1
        assert session.query(EmailEvent).count() == 2


def test_prefilter_miss_subjects_are_reported(tmp_path, monkeypatch):
    with _session(tmp_path) as session:
        provider = FakeProvider(
            [_message(sender="deals@retailer.com", subject="50% off this weekend")]
        )

        result = run_sync(
            session, provider, _profile(tmp_path), dry_run=True,
            state_path=tmp_path / "state.json",
        )

        assert result.skipped_prefilter == 1
        assert result.skipped_subjects == ["deals@retailer.com: 50% off this weekend"]


def test_second_message_in_pending_thread_does_not_duplicate_suggestion(tmp_path, monkeypatch):
    with _session(tmp_path) as session:
        _stub_classifier(monkeypatch, matched_candidate_id=None, company_name="Acme")
        profile = _profile(tmp_path)
        first = _message(message_id="m1", sender="careers@acme.com", sender_name="Acme")
        provider = FakeProvider([first])
        run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        follow_up = InboundMessage(
            message_id="m2",
            thread_id=first.thread_id,
            sender="careers@acme.com",
            sender_name="Acme",
            subject="Your application status",
            body_text="Still under review.",
            received_at=NOW,
        )
        provider.messages = [follow_up]
        result = run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        assert result.suggestions == 0
        assert session.query(Suggestion).count() == 1
        # And the follow-up isn't re-examined on the next run either.
        provider.messages = [follow_up]
        third = run_sync(session, provider, profile, state_path=tmp_path / "state.json")
        assert third.skipped_seen == 0  # never recorded, but...
        assert session.query(Suggestion).count() == 1  # ...still exactly one


def test_pending_thread_followup_costs_no_claude_calls(tmp_path, monkeypatch):
    """A follow-up parked in the intake folder while its conversation awaits
    review must be held BEFORE classification - otherwise it would burn one
    Claude call every hourly run until the suggestion is resolved."""
    with _session(tmp_path) as session:
        calls: list[str] = []
        classification = EmailClassification(
            job_related=True,
            classified_type=EmailEventType.confirmation,
            matched_candidate_id=None,
            company_name="Acme",
            job_title=None,
        )

        def counting_classifier(message, *a, **k):
            calls.append(message.message_id)
            return classification

        monkeypatch.setattr("copilot.email_agent.sync.classify_email", counting_classifier)
        profile = _profile(tmp_path)
        first = _message(message_id="m1", sender="careers@acme.com", sender_name="Acme")
        provider = FakeProvider([first])
        run_sync(session, provider, profile, state_path=tmp_path / "state.json")
        assert calls == ["m1"]  # suggestion created, one call spent

        follow_up = InboundMessage(
            message_id="m2",
            thread_id=first.thread_id,
            sender="careers@acme.com",
            sender_name="Acme",
            subject="Re: your application",
            body_text="Still under review.",
            received_at=NOW,
            internet_message_id="<m2@acme.com>",
        )
        provider.messages = []
        provider.intake = [follow_up]
        run_sync(session, provider, profile, state_path=tmp_path / "state.json")
        run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        assert calls == ["m1"]  # two more runs, zero more Claude calls
        assert session.query(Suggestion).count() == 1


def test_intake_bypasses_prefilter_and_old_mail_never_alerts(tmp_path, monkeypatch):
    """D28: mail dragged into the intake folder is processed no matter how
    old or unrecognizable - but a months-old invite's clock already ran, so
    it's recorded and filed without buzzing the user."""
    with _session(tmp_path) as session:
        _seed_tracked_application(session)
        _stub_classifier(monkeypatch, classified_type=EmailEventType.interview_invite)
        old = _message(
            message_id="m9",
            sender="jane@obscure-agency.example",
            subject="Hello again",  # no pattern match, no ATS domain
            internet_id="<old@agency.example>",
            received_at=NOW - timedelta(days=90),
        )
        assert not prefilter(old)
        provider = FakeProvider([], intake=[old])

        result = run_sync(
            session, provider, _profile(tmp_path), state_path=tmp_path / "state.json"
        )

        assert result.intake == 1
        assert result.advanced == 1
        assert provider.filed == [("m9", "Interview Invite")]
        assert provider.sent == []  # alert suppressed: 90 days old
        assert any("alert suppressed" in a for a in result.actions)


def test_manually_moved_message_not_reprocessed(tmp_path, monkeypatch):
    """Outlook gives a message a new provider id on every move - the
    Internet Message-ID is what stops a user dragging processed mail into
    the intake folder from double-processing it."""
    with _session(tmp_path) as session:
        _seed_tracked_application(session)
        _stub_classifier(monkeypatch)
        profile = _profile(tmp_path)
        provider = FakeProvider([_message(internet_id="<x@ats.example>")])
        run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        moved = _message(message_id="TOTALLY-NEW-ID", internet_id="<x@ats.example>")
        provider.messages = []
        provider.intake = [moved]
        result = run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        assert result.skipped_seen == 1
        assert result.intake == 0
        assert session.query(EmailEvent).count() == 1


def test_intake_overrides_not_job_mail_verdict(tmp_path, monkeypatch):
    """The human vouched for intake mail: even a 'not job related' verdict
    becomes a suggestion the user can decline (which also stops the message
    being re-classified every run while it sits in the folder)."""
    with _session(tmp_path) as session:
        _stub_classifier(
            monkeypatch, job_related=False, matched_candidate_id=None, company_name=None
        )
        msg = _message(
            message_id="m7",
            sender="person@nowhere.example",
            subject="hey",
            internet_id="<hey@nowhere.example>",
        )
        provider = FakeProvider([], intake=[msg])

        result = run_sync(
            session, provider, _profile(tmp_path), state_path=tmp_path / "state.json"
        )

        assert result.intake == 1
        assert result.suggestions == 1
        suggestion = session.query(Suggestion).one()
        assert suggestion.kind == SuggestionKind.untracked_application
        assert provider.filed == []  # still untouched until confirmed (D20)


def test_filed_folder_rescues_unknown_mail_in_place(tmp_path, monkeypatch):
    """D29: dropping new job mail on "Career Copilot" instead of "Process"
    still gets it processed - in place, since it's already in the folder."""
    with _session(tmp_path) as session:
        _seed_tracked_application(session)
        _stub_classifier(monkeypatch, classified_type=EmailEventType.rejection)
        stranger = _message(
            message_id="m5",
            sender="talent@stripe.com",
            subject="An update from Stripe",
            internet_id="<stranger@stripe.com>",
        )
        provider = FakeProvider([], filed_folder=[stranger])

        result = run_sync(
            session, provider, _profile(tmp_path), state_path=tmp_path / "state.json"
        )

        assert result.intake == 1
        assert result.advanced == 1  # applied -> rejected
        assert session.query(EmailEvent).one().subject == "An update from Stripe"
        assert provider.full_fetches == ["m5"]
        assert provider.filed == []  # processed in place, never re-filed


def test_filed_folder_scan_skips_known_mail_without_body_fetches(tmp_path, monkeypatch):
    """The filed folder is mostly Copilot's own filings - the hourly sweep
    must recognize them from headers alone, costing zero body downloads and
    zero Claude calls."""
    with _session(tmp_path) as session:
        _seed_tracked_application(session)
        _stub_classifier(monkeypatch)
        original = _message(internet_id="<known@ats.example>")
        provider = FakeProvider([original])
        profile = _profile(tmp_path)
        run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        # After filing, the message reappears in the folder under a NEW
        # provider id (Outlook regenerates ids on move) - only the
        # internet_message_id still matches the stored event.
        from dataclasses import replace

        provider.messages = []
        provider.filed_folder = [replace(original, message_id="NEW-ID-AFTER-MOVE")]
        result = run_sync(session, provider, profile, state_path=tmp_path / "state.json")

        assert result.intake == 0
        assert provider.full_fetches == []  # deduped from headers alone
        assert session.query(EmailEvent).count() == 1


@pytest.mark.parametrize(
    ("current", "implied", "expected"),
    [
        (ApplicationState.applied, ApplicationState.interviewing, "forward"),
        (ApplicationState.applied, ApplicationState.rejected, "forward"),
        (ApplicationState.interviewing, ApplicationState.applied, "stale"),
        (ApplicationState.applied, ApplicationState.applied, "stale"),
        (ApplicationState.rejected, ApplicationState.interviewing, "conflict"),
        (ApplicationState.ghosted, ApplicationState.replied, "conflict"),
        (ApplicationState.found, ApplicationState.applied, "forward"),
    ],
)
def test_state_relation(current, implied, expected):
    assert state_relation(current, implied) == expected
