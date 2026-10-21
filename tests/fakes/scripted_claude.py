"""A scripted stand-in for the Anthropic client, for unit, integration and e2e tests.

``ScriptedClaude`` exposes ``.beta.messages.create(**request)`` and replays a script.
Each step is a reply (a real ``BetaMessage``), an exception (a real SDK error), or a
callable ``request -> reply`` for content-aware scripts. Every request body is
deep-copied into ``.requests``, so tests can assert models, cache markers and effort.

Failure injection lives only here and in the other test fakes, never in production
configuration (FR-239). The file name does not match ``test*.py``.
"""

import copy
import itertools
import json
from collections.abc import Callable

import anthropic
import httpx2
from anthropic.types.beta import BetaMessage

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"
_ids = itertools.count(1)


def usage(
    input_tokens: int = 100, output_tokens: int = 50, cache_creation: int = 0, cache_read: int = 0
) -> dict:
    """A ``usage`` block for a scripted reply."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }


def message(
    content: list[dict], stop_reason: str = "end_turn", usage_block: dict | None = None,
    model: str = DEFAULT_MODEL, stop_details: dict | None = None,
) -> BetaMessage:
    """Build a real ``BetaMessage``, exactly as the SDK would parse it from the API."""
    return BetaMessage.model_validate({
        "id": f"msg_{next(_ids)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": usage_block or usage(),
    })


def text_reply(payload: dict | str, usage_block: dict | None = None, **kwargs) -> BetaMessage:
    """A text reply; a dict payload is sent as JSON, as structured output is."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return message([{"type": "text", "text": text}], usage_block=usage_block, **kwargs)


def tool_use_reply(
    calls: list[tuple[str, dict]], thought: str = "", usage_block: dict | None = None, **kwargs
) -> BetaMessage:
    """A ``tool_use`` reply asking for each ``(name, input)`` in ``calls``."""
    content = [{"type": "text", "text": thought}] if thought else []
    content += [
        {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": tool_input}
        for name, tool_input in calls
    ]
    return message(content, stop_reason="tool_use", usage_block=usage_block, **kwargs)


def refusal_reply(category: str = "cyber", explanation: str = "Not allowed.") -> BetaMessage:
    """A refusal, with the stop details the API sends."""
    return message(
        [], stop_reason="refusal",
        stop_details={"type": "refusal", "category": category, "explanation": explanation},
    )


def _response(status: int, headers: dict | None = None) -> httpx2.Response:
    request = httpx2.Request("POST", API_URL)
    return httpx2.Response(status, request=request, headers=headers or {})


def rate_limit_error(retry_after: float | None = None) -> anthropic.RateLimitError:
    """A 429, with a ``retry-after`` header when ``retry_after`` is given."""
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return anthropic.RateLimitError("rate limited", response=_response(429, headers), body=None)


def overloaded_error() -> anthropic.OverloadedError:
    """A 529: the service is overloaded."""
    return anthropic.OverloadedError("overloaded", response=_response(529), body=None)


def server_error(status: int = 500) -> anthropic.APIStatusError:
    """A 5xx from the service."""
    return anthropic.InternalServerError("server error", response=_response(status), body=None)


def connection_error() -> anthropic.APIConnectionError:
    """The service could not be reached."""
    return anthropic.APIConnectionError(request=httpx2.Request("POST", API_URL))


def bad_request_error() -> anthropic.BadRequestError:
    """A 400: the request was invalid (permanent)."""
    return anthropic.BadRequestError("bad request", response=_response(400), body=None)


def auth_error() -> anthropic.AuthenticationError:
    """A 401: the credentials were rejected (permanent)."""
    return anthropic.AuthenticationError("invalid x-api-key", response=_response(401), body=None)


Step = BetaMessage | BaseException | Callable[[dict], BetaMessage]


class _Messages:
    def __init__(self, owner: "ScriptedClaude") -> None:
        self._owner = owner

    def create(self, **request) -> BetaMessage:
        return self._owner.next_reply(request)


class _Beta:
    def __init__(self, owner: "ScriptedClaude") -> None:
        self.messages = _Messages(owner)


class ScriptedClaude:
    """Replays ``steps`` in order, one per ``beta.messages.create`` call."""

    def __init__(self, steps: list[Step] | None = None) -> None:
        self.steps = list(steps or [])
        self.requests: list[dict] = []
        self.beta = _Beta(self)
        self.max_retries = 0

    def extend(self, steps: list[Step]) -> None:
        """Append more steps to the script."""
        self.steps.extend(steps)

    def next_reply(self, request: dict) -> BetaMessage:
        """Record ``request`` and return (or raise) the next scripted step."""
        self.requests.append(copy.deepcopy(request))
        if not self.steps:
            raise AssertionError("script exhausted")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step) and not isinstance(step, BetaMessage):
            return step(request)
        return step

    @property
    def remaining(self) -> int:
        """Steps not yet replayed."""
        return len(self.steps)
