"""Single choke point for every Anthropic API call in the project.

Two reasons this exists instead of each module constructing its own
`Anthropic()` client (docs/DECISIONS.md D34):

1. **Usage tracking.** Anthropic exposes no "credits remaining" endpoint for a
   regular API key (only an Admin-key Usage & Cost API, a different
   credential this single-user tool doesn't hold) - so the best available
   spend signal is tracking tokens from calls this tool actually makes.
   `create`/`parse` below are drop-in replacements for
   `client.messages.create`/`.parse` that capture `response.usage` and
   persist an estimated running cost via `api_usage.py`, best-effort, so a
   usage-recording hiccup never breaks the real call.
2. One place to point every caller at instead of six independent
   `Anthropic()` instantiations.
"""

from __future__ import annotations

from anthropic import Anthropic

# $ per million tokens, (input, output). Cache write/read are derived at the
# standard 1.25x/0.1x of the input rate (docs/APIS.md caching economics) -
# Anthropic doesn't bill cache tokens as separate SKUs with their own price.
# Unlisted models: cost is not estimated (return None rather than guess).
_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),  # introductory rate through 2026-08-31; reverts to (3.00, 15.00)
    "claude-opus-4-8": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def get_client() -> Anthropic:
    return Anthropic()


def _estimate_cost(model: str, usage) -> float | None:
    rates = _PRICING.get(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        input_tokens * input_rate
        + cache_write * (input_rate * 1.25)
        + cache_read * (input_rate * 0.1)
        + output_tokens * output_rate
    ) / 1_000_000


def _record(model: str, usage) -> None:
    """Best-effort - a malformed/mocked usage object must never break the
    real API call that just happened."""
    try:
        from copilot.api_usage import record_anthropic_usage

        record_anthropic_usage(
            model=model,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cost_usd=_estimate_cost(model, usage),
        )
    except Exception:  # noqa: BLE001
        pass


def create(**kwargs):
    response = get_client().messages.create(**kwargs)
    _record(kwargs.get("model"), getattr(response, "usage", None))
    return response


def parse(**kwargs):
    response = get_client().messages.parse(**kwargs)
    _record(kwargs.get("model"), getattr(response, "usage", None))
    return response
