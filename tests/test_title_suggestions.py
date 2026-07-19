from types import SimpleNamespace
from unittest.mock import patch

import pytest

from copilot.config import Profile
from copilot.resume import ContactInfo, ResumeProfile
from copilot.title_suggestions import suggest_target_titles

SUGGESTIONS_JSON = """```json
[
  {"title": "Platform Engineer", "reasoning": "Matches infra skills", "demand_signal": "many postings this month"},
  {"title": "Backend Engineer", "reasoning": "Matches years of experience", "demand_signal": "steady demand"}
]
```"""


def text_response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason
    )


def make_profile() -> Profile:
    return Profile.model_validate(
        {
            "identity": {"full_name": "Test User", "email": "t@example.com"},
            "resume_path": "data/resume.pdf",
            "search": {"titles": ["Software Engineer"]},
            "visa": {"needs_sponsorship": True, "status": "h1b_transfer"},
            "email_integration": {"provider": "outlook", "address": "t@hotmail.com"},
        }
    )


def make_resume() -> ResumeProfile:
    return ResumeProfile(
        full_name="Test User",
        contact=ContactInfo(),
        summary="Backend engineer.",
        seniority="senior",
        years_of_experience=6,
        skills=["Python", "Kubernetes"],
        domains=["fintech"],
        work_experience=[],
        education=[],
    )


def test_parses_suggestions_from_response():
    with patch("copilot.title_suggestions.Anthropic") as mock_client:
        mock_client.return_value.messages.create.return_value = text_response(
            SUGGESTIONS_JSON
        )
        suggestions = suggest_target_titles(make_profile(), make_resume())

    assert len(suggestions) == 2
    assert suggestions[0].title == "Platform Engineer"
    assert suggestions[1].demand_signal == "steady demand"
    # web search tool must actually be declared on the request
    _, kwargs = mock_client.return_value.messages.create.call_args
    assert kwargs["tools"][0]["type"] == "web_search_20260209"


def test_resumes_on_pause_turn_then_parses():
    with patch("copilot.title_suggestions.Anthropic") as mock_client:
        mock_client.return_value.messages.create.side_effect = [
            text_response("still searching...", stop_reason="pause_turn"),
            text_response(SUGGESTIONS_JSON, stop_reason="end_turn"),
        ]
        suggestions = suggest_target_titles(make_profile(), make_resume())

    assert len(suggestions) == 2
    assert mock_client.return_value.messages.create.call_count == 2


def test_gives_up_after_max_resumes():
    with patch("copilot.title_suggestions.Anthropic") as mock_client:
        mock_client.return_value.messages.create.return_value = text_response(
            "never finishes", stop_reason="pause_turn"
        )
        with pytest.raises(RuntimeError, match="JSON suggestions block"):
            suggest_target_titles(make_profile(), make_resume())


def test_missing_json_block_raises():
    with patch("copilot.title_suggestions.Anthropic") as mock_client:
        mock_client.return_value.messages.create.return_value = text_response(
            "Here are some titles: Backend Engineer, Platform Engineer."
        )
        with pytest.raises(RuntimeError, match="JSON suggestions block"):
            suggest_target_titles(make_profile(), make_resume())
