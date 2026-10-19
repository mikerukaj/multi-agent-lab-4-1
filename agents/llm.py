"""Claude request settings and helpers shared by the PM, Coder and QA agents."""

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 16_000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def refusal_message(stop_details) -> str:
    """Describe a model refusal for the run record.

    Args:
        stop_details: The response's ``stop_details`` (``None`` if the API sent none).

    Returns:
        A sentence naming the refusal category and the API's explanation.
    """
    if stop_details is None:
        return "The model declined the request."
    return (
        f"The model declined the request (category: {stop_details.category}): "
        f"{stop_details.explanation}"
    )


def with_fallbacks(request: dict, enabled: bool) -> dict:
    """Return ``request`` with server-side refusal fallbacks turned on when ``enabled``."""
    if not enabled:
        return request
    return {**request, "betas": [FALLBACK_BETA], "fallbacks": "default"}


def add_tokens(total: dict, input_tokens: int, output_tokens: int) -> dict:
    """Return ``total`` (``input_tokens``/``output_tokens``) with the given counts added."""
    return {
        "input_tokens": total["input_tokens"] + input_tokens,
        "output_tokens": total["output_tokens"] + output_tokens,
    }
