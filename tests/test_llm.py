from types import SimpleNamespace
from unittest.mock import patch

from copilot import llm


def _usage(**overrides):
    base = dict(input_tokens=1000, output_tokens=500,
                cache_creation_input_tokens=0, cache_read_input_tokens=0)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_create_records_usage_and_returns_response(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    monkeypatch.setattr("copilot.api_usage.USAGE_PATH", path)
    fake_response = SimpleNamespace(usage=_usage())

    with patch("copilot.llm.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = fake_response
        result = llm.create(model="claude-sonnet-5", max_tokens=100, messages=[])

    assert result is fake_response
    from copilot.api_usage import get_anthropic_spend
    spend = get_anthropic_spend(path=path)
    assert spend is not None
    assert spend.calls == 1
    assert spend.input_tokens == 1000
    assert spend.output_tokens == 500


def test_parse_records_usage(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    monkeypatch.setattr("copilot.api_usage.USAGE_PATH", path)
    fake_response = SimpleNamespace(usage=_usage(), parsed_output="whatever")

    with patch("copilot.llm.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.parse.return_value = fake_response
        result = llm.parse(model="claude-sonnet-5", max_tokens=100, messages=[])

    assert result is fake_response
    from copilot.api_usage import get_anthropic_spend
    assert get_anthropic_spend(path=path).calls == 1


def test_estimate_cost_known_model():
    usage = _usage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = llm._estimate_cost("claude-sonnet-5", usage)
    assert cost == 2.00 + 10.00  # $2/M in + $10/M out at the introductory rate


def test_estimate_cost_unknown_model_returns_none():
    assert llm._estimate_cost("some-future-model", _usage()) is None


def test_estimate_cost_includes_cache_tokens():
    usage = _usage(input_tokens=0, output_tokens=0,
                    cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    cost = llm._estimate_cost("claude-sonnet-5", usage)
    # cache write = 1.25x input rate, cache read = 0.1x input rate
    assert cost == 2.00 * 1.25 + 2.00 * 0.1


def test_record_survives_missing_usage_fields(tmp_path, monkeypatch):
    """A mocked response with no .usage attributes set must not crash the
    real call it's wrapping - matches the jsearch quota best-effort pattern."""
    path = tmp_path / "usage.json"
    monkeypatch.setattr("copilot.api_usage.USAGE_PATH", path)
    fake_response = SimpleNamespace()  # no .usage at all

    with patch("copilot.llm.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = fake_response
        result = llm.create(model="claude-sonnet-5", max_tokens=100, messages=[])

    assert result is fake_response  # did not raise
