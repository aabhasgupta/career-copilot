from types import SimpleNamespace
from unittest.mock import patch

from copilot.config import Profile
from copilot.industry import Industry, IndustryPreferenceSuggestion, suggest_industry_preference


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


def _parsed(industries: list[Industry]):
    return SimpleNamespace(
        usage=None,
        parsed_output=IndustryPreferenceSuggestion(industries=industries),
    )


def test_suggest_industry_preference_empty_domains_skips_llm_call():
    with patch("copilot.llm.Anthropic") as mock_client:
        result = suggest_industry_preference(make_profile(), [])
    assert result == []
    mock_client.return_value.messages.parse.assert_not_called()


def test_suggest_industry_preference_returns_ranked_values():
    with patch("copilot.llm.Anthropic") as mock_client:
        mock_client.return_value.messages.parse.return_value = _parsed(
            [Industry.fintech, Industry.tech]
        )
        result = suggest_industry_preference(make_profile(), ["fintech", "data platforms"])
    assert result == ["fintech", "tech"]


def test_suggest_industry_preference_every_value_is_a_real_industry_member():
    """The whole point: whatever comes back must be usable by industry.py's
    classify_companies() / ranking.py's exact-match comparison - never a
    value outside the same closed vocabulary."""
    with patch("copilot.llm.Anthropic") as mock_client:
        mock_client.return_value.messages.parse.return_value = _parsed(
            [Industry.banking, Industry.healthcare]
        )
        result = suggest_industry_preference(make_profile(), ["banking", "healthcare"])
    valid_values = {i.value for i in Industry}
    assert all(value in valid_values for value in result)


def test_suggest_industry_preference_filters_out_other():
    with patch("copilot.llm.Anthropic") as mock_client:
        mock_client.return_value.messages.parse.return_value = _parsed(
            [Industry.other, Industry.tech]
        )
        result = suggest_industry_preference(make_profile(), ["something vague"])
    assert result == ["tech"]


def test_suggest_industry_preference_dedupes_preserving_rank_order():
    with patch("copilot.llm.Anthropic") as mock_client:
        mock_client.return_value.messages.parse.return_value = _parsed(
            [Industry.tech, Industry.fintech, Industry.tech]
        )
        result = suggest_industry_preference(make_profile(), ["software", "fintech"])
    assert result == ["tech", "fintech"]
