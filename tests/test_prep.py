from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from copilot.applications import mark_applied
from copilot.config import Profile
from copilot.db import get_engine, get_session, init_db
from copilot.db.models import Company, EmailEvent, EmailEventType, Job
from copilot.prep import PrepInput, _gap_block, _interview_context_block, generate_prep_brief, prep_application
from copilot.resume import ContactInfo, ResumeProfile

RESUME = ResumeProfile(
    full_name="Jane Doe",
    contact=ContactInfo(email="jane@example.com"),
    summary="ML engineer.",
    seniority="senior",
    years_of_experience=8,
    skills=["Python", "PyTorch"],
    domains=["fintech"],
    work_experience=[],
    education=[],
)


def make_profile() -> Profile:
    return Profile.model_validate(
        {
            "identity": {"full_name": "Test User", "email": "t@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {"titles": ["ML Engineer"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "t@hotmail.com"},
        }
    )


def text_response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason
    )


def make_prep_input(**overrides) -> PrepInput:
    fields = dict(
        title="ML Engineer", company="Acme", location="Remote US",
        jd_text="Build fraud models.", fit_score=78.0,
        fit_reasoning="Strong fraud-detection background.",
        skill_match_score=80.0, experience_level_score=75.0, domain_fit_score=90.0,
    )
    fields.update(overrides)
    return PrepInput(**fields)


def test_gap_block_uses_fit_reasoning_when_scored():
    block = _gap_block(make_prep_input())
    assert "78/100" in block
    assert "Strong fraud-detection background." in block


def test_gap_block_handles_unscored_job():
    block = _gap_block(make_prep_input(fit_score=None, fit_reasoning=None))
    assert "not yet scored" in block


def test_interview_context_block_empty_when_no_emails():
    assert _interview_context_block(make_prep_input()) == ""


def test_interview_context_block_includes_email_notes():
    block = _interview_context_block(make_prep_input(interview_emails=["interview_invite: 30-min screen"]))
    assert "30-min screen" in block


def test_generate_prep_brief_calls_web_search_and_returns_text():
    with patch("copilot.llm.Anthropic") as mock_client:
        mock_client.return_value.messages.create.return_value = text_response(
            "## Company & role snapshot\nAcme builds fraud tools."
        )
        brief = generate_prep_brief(make_profile(), RESUME, make_prep_input())

    assert "Acme builds fraud tools." in brief
    _, kwargs = mock_client.return_value.messages.create.call_args
    assert kwargs["tools"][0]["type"] == "web_search_20260209"


def test_generate_prep_brief_resumes_on_pause_turn():
    with patch("copilot.llm.Anthropic") as mock_client:
        mock_client.return_value.messages.create.side_effect = [
            text_response("still researching...", stop_reason="pause_turn"),
            text_response("## Final brief", stop_reason="end_turn"),
        ]
        brief = generate_prep_brief(make_profile(), RESUME, make_prep_input())

    assert "Final brief" in brief
    assert mock_client.return_value.messages.create.call_count == 2


def test_generate_prep_brief_drops_search_narration_interleaved_in_one_turn():
    """Real bug, caught live: a single end_turn response interleaves
    "let me check X next" narration with server_tool_use/web_search_tool_result
    blocks per search, then the actual brief as the trailing text block(s).
    Joining every text block prepended that narration to the brief."""
    content = [
        SimpleNamespace(type="text", text="Let me research this company first."),
        SimpleNamespace(type="server_tool_use", name="web_search"),
        SimpleNamespace(type="web_search_tool_result"),
        SimpleNamespace(type="text", text="Good, now let me check their interview process."),
        SimpleNamespace(type="server_tool_use", name="web_search"),
        SimpleNamespace(type="web_search_tool_result"),
        SimpleNamespace(type="text", text="## Company & role snapshot\n"),
        SimpleNamespace(type="text", text="Acme builds fraud tools."),
    ]
    with patch("copilot.llm.Anthropic") as mock_client:
        mock_client.return_value.messages.create.return_value = SimpleNamespace(
            content=content, stop_reason="end_turn"
        )
        brief = generate_prep_brief(make_profile(), RESUME, make_prep_input())

    assert brief == "## Company & role snapshot\nAcme builds fraud tools."
    assert "Let me research" not in brief
    assert "let me check their interview process" not in brief


def test_prep_application_builds_input_from_orm_and_persists(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with get_session(engine) as session:
        acme = Company(name="Acme")
        session.add(acme)
        session.flush()
        job = Job(
            company_id=acme.id, title="ML Engineer", source="greenhouse",
            apply_url="https://acme.com/1", dedupe_hash="h1",
            jd_text="Build fraud models.", fit_score=78.0,
            fit_reasoning="Strong match.", skill_match_score=80.0,
            experience_level_score=75.0, domain_fit_score=90.0,
        )
        session.add(job)
        session.commit()
        application = mark_applied(session, job.id)
        session.add(
            EmailEvent(
                application_id=application.id, provider_message_id="m1", provider_thread_id="t1",
                classified_type=EmailEventType.interview_invite, subject="Next steps",
                body_text="30-minute technical screen with the hiring manager.",
                received_at=datetime.now(timezone.utc),
            )
        )
        # A rejection email must NOT be pulled into interview context.
        session.add(
            EmailEvent(
                application_id=application.id, provider_message_id="m2", provider_thread_id="t1",
                classified_type=EmailEventType.rejection, subject="Update",
                body_text="We have decided not to move forward.",
                received_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        app_id = application.id

        with patch("copilot.llm.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = text_response("## Brief\nDo well.")
            brief = prep_application(session, make_profile(), RESUME, application)

        assert "Do well." in brief
        assert application.prep_brief == brief
        assert application.prep_generated_at is not None

        # The prompt actually saw the interview-invite context, not the rejection.
        _, kwargs = mock_client.return_value.messages.create.call_args
        prompt = kwargs["messages"][0]["content"]
        assert "30-minute technical screen" in prompt
        assert "We have decided not to move forward" not in prompt

    # Persisted for real - a fresh session sees the same brief.
    with get_session(engine) as session2:
        from copilot.db.models import Application

        reloaded = session2.get(Application, app_id)
        assert reloaded.prep_brief == brief
