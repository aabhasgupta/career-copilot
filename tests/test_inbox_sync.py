"""Sync pipeline tests: fake provider, stubbed classifier (no Claude calls),
real DB. Each test seeds a tracked Stripe application and drives run_sync
with canned inbox messages."""

from __future__ import annotations

from datetime import datetime, timezone
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

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


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
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        thread_id=f"t-{message_id}",
        sender=sender,
        sender_name=sender_name,
        subject=subject,
        body_text=body,
        received_at=NOW,
    )


class FakeProvider(EmailProvider):
    def __init__(self, messages: list[InboundMessage]):
        self.messages = messages
        self.filed: list[tuple[str, str]] = []
        self.sent: list[str] = []

    def send_mail(self, *, to: str, subject: str, body_html: str) -> None:
        self.sent.append(subject)

    def list_messages_since(self, since: datetime) -> list[InboundMessage]:
        return self.messages

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
