"""Claude request settings and helpers shared by the PM, Coder and QA agents.

Every model call goes through one ``ModelCaller`` per agent (specs/003-production-
hardening/plan.md, Summary 1), so reply validation, usage accounting and, as 003 adds
them, retries, circuit breaking, prompt caching, cost and tracing are written once.
"""

import json
import time
from dataclasses import dataclass, field, replace

from cost.ledger import BudgetExceeded
from cost.pricing import cost_of
from orchestration.services import RunServices
from orchestration.state import AgentName, DependencyFailure, ServedFrom, TokenUsage
from orchestration.validation import first_problems, validate
from resilience.circuit import GuardFailure
from resilience.classify import BUDGET_EXCEEDED, is_unavailable

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
LARGE_MODEL = "claude-sonnet-5-5"
SMALL_MODEL = "claude-haiku-5-5"
MODEL_CAPABILITIES = {
    LARGE_MODEL: {"fallbacks": True, "efforts": EFFORT_LEVELS, "mid_conversation_system": True},
    SMALL_MODEL: {"fallbacks": False, "efforts": EFFORT_LEVELS, "mid_conversation_system": True},
}
"""What each known model supports (research R8). Haiku 5.5 has no server-side refusal
fallback."""
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


def resolve_fallbacks(model: str, requested: bool | None) -> bool:
    """``*_USE_FALLBACKS``: unset means "on if the model supports server-side fallback"."""
    if requested is not None:
        return requested
    return MODEL_CAPABILITIES.get(model, {}).get("fallbacks", False)


def check_model_settings(agent: str, model: str, effort: str, use_fallbacks: bool) -> list[str]:
    """Check an agent's settings against its model (FR-225b); return warnings.

    A model missing from ``MODEL_CAPABILITIES`` is allowed with a warning, and no
    capability is assumed.

    Raises:
        ValueError: naming the agent, the setting and the model, for a setting the
            model doesn't support.
    """
    capabilities = MODEL_CAPABILITIES.get(model)
    if capabilities is None:
        return [f"{agent}: model {model} is not in the capability table; its settings "
                "are not checked"]
    prefix = agent.upper()
    if use_fallbacks and not capabilities["fallbacks"]:
        raise ValueError(f"{agent}: {prefix}_USE_FALLBACKS=true is not supported by {model}, "
                         "which has no server-side refusal fallback")
    if effort not in capabilities["efforts"]:
        raise ValueError(f"{agent}: {prefix}_EFFORT={effort} is not supported by {model}")
    return []


def fallbacks_from_env(value: str | None) -> bool | None:
    """Read a ``*_USE_FALLBACKS`` value; unset (or empty) means the model's default."""
    if not value:
        return None
    return value.lower() not in ("0", "false", "no")


def cache_marker(ttl: str) -> dict | None:
    """The ``cache_control`` marker for ``PROMPT_CACHE_TTL`` (``None`` when ``off``).

    Every marker in a request uses the same TTL, so the API's "longer TTL first" rule
    never applies (research R7).
    """
    if ttl == "off":
        return None
    return {"type": "ephemeral"} if ttl == "5m" else {"type": "ephemeral", "ttl": ttl}


def system_blocks(text: str, ttl: str):
    """The ``system`` value: one cache-marked text block, or the plain text when off."""
    marker = cache_marker(ttl)
    return text if marker is None else [{"type": "text", "text": text, "cache_control": marker}]


def stable_blocks(stable: list[str], instruction: str, ttl: str) -> list[dict]:
    """User content: the parts that repeat across calls, then this call's instruction.

    The marker goes on the last stable part, so the repeated prefix is billed at the
    cache-read rate on later calls (FR-225).
    """
    blocks = [{"type": "text", "text": text} for text in stable if text]
    marker = cache_marker(ttl)
    if marker and blocks:
        blocks[-1] = {**blocks[-1], "cache_control": marker}
    return blocks + [{"type": "text", "text": instruction}]


def with_fallbacks(request: dict, enabled: bool) -> dict:
    """Return ``request`` with server-side refusal fallbacks turned on when ``enabled``."""
    if not enabled:
        return request
    return {**request, "betas": [FALLBACK_BETA], "fallbacks": "default"}


NOT_JSON = "Model output is not valid JSON"
TOKEN_COUNTERS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
)


def add_tokens(
    total: dict,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> dict:
    """Return ``total`` with the given counts added, as a dict of all four token counters.

    A counter missing from ``total`` counts as 0, so records and fakes from before
    prompt caching still add up.
    """
    extra = (input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens)
    return {key: total.get(key, 0) + value for key, value in zip(TOKEN_COUNTERS, extra)}


def add_usage(total: dict, extra: dict) -> TokenUsage:
    """Return ``total`` plus ``extra``; a counter missing from either counts as 0."""
    return TokenUsage(**add_tokens(total, *(extra.get(key, 0) for key in TOKEN_COUNTERS)))


def zero_usage() -> dict:
    """Return a token usage with every counter at 0."""
    return dict.fromkeys(TOKEN_COUNTERS, 0)


@dataclass
class ModelReply:
    """The outcome of one ``ModelCaller.call``.

    ``data`` is set only for a structured reply that parsed and passed validation;
    ``problems`` lists why it did not. ``refusal`` is the refusal message when the model
    declined. ``response`` is the SDK message (``None`` when no call was served).
    """

    response: object | None
    data: dict | None = None
    text: str = ""
    usage: TokenUsage = field(default_factory=zero_usage)
    refusal: str | None = None
    problems: list[str] = field(default_factory=list)
    failure: DependencyFailure | None = None
    served_from: ServedFrom = "live"
    cost_usd: float | None = 0.0
    """The call's cost (``None`` when its model has no price; 0 for a cached replay)."""


class ModelCaller:
    """Makes one agent's model calls and checks every structured reply before use."""

    def __init__(self, agent: AgentName, client, services: RunServices) -> None:
        self.agent = agent
        self._client = client
        self._services = services

    def call(self, request: dict, reply_type: type | None = None) -> ModelReply:
        """Send ``request`` and return the reply, validated against ``reply_type``.

        With ``reply_type=None`` the raw response is returned for the caller to read
        (the Coder's tool-using turns). A refusal is reported, never validated. The call
        goes through the run's ``anthropic:<model>`` guard (retries and circuit
        breaker); when the guard gives up, a cached reply to the identical request is
        served if it still validates, otherwise ``ModelReply.failure`` says why. It
        never raises for a model-service failure.
        """
        try:
            self._services.budget.check()
        except BudgetExceeded as error:
            return self._budget_stop(request, str(error))
        started, operation_seconds = time.monotonic(), [0.0]

        def send():
            sent = time.monotonic()
            try:
                return self._client.beta.messages.create(**request)
            finally:
                operation_seconds[0] += time.monotonic() - sent

        with self._services.tracer.llm_span(request["model"], request_summary(request)) as span:
            guard = self._services.guard("anthropic", request["model"])
            outcome = guard.call(send)
            reply = self._priced(request, self._reply_for(request, reply_type, outcome))
            span.outputs = llm_outputs(reply)
            span.metadata = {"served_from": reply.served_from, "retries": guard.retries,
                             "attempts": reply.failure["attempts"] if reply.failure else None,
                             "cost_usd": reply.cost_usd}
            if reply.failure:
                span.error = failure_message(reply.failure)
        self._record(request, reply_type, reply, started, operation_seconds[0], guard.retries)
        return reply

    def _priced(self, request: dict, reply: ModelReply) -> ModelReply:
        """Price a live reply by the model that served it (a fallback at its own rates)."""
        if reply.response is None or reply.served_from == "cache":
            return replace(reply, cost_usd=0.0)
        services = self._services
        writes = getattr(getattr(reply.response.usage, "cache_creation", None), "model_dump",
                         None)
        cost = cost_of(reply.usage, served_model(reply.response, request),
                       services.prompt_cache_ttl, services.price_table,
                       writes() if writes else None)
        return replace(reply, cost_usd=cost)

    def _budget_stop(self, request: dict, message: str) -> ModelReply:
        """The reply for a call not started because the budget is spent (FR-223)."""
        failure = DependencyFailure(
            dependency="anthropic", target=request["model"], kind="permanent", attempts=0,
            circuit="closed", error_type=BUDGET_EXCEEDED, message=message,
            retry_after_seconds=None,
        )
        self._services.events.emit("budget_stop", agent=self.agent, severity="warning",
                                   outcome="budget_exceeded", message=message)
        return ModelReply(None, failure=failure)

    def _reply_for(self, request: dict, reply_type: type | None, outcome) -> ModelReply:
        """The reply for a guarded call's outcome: live, cached, or the failure."""
        if isinstance(outcome, GuardFailure):
            return self._degraded(request, reply_type, outcome.failure)
        reply = read_reply(outcome, reply_type)
        cache = self._services.response_cache
        if cache and reply.refusal is None and not reply.problems:
            if not cache.put(request, outcome):
                self._event("warning", "warning", message="response cache write failed")
        return reply

    def _record(
        self, request: dict, reply_type, reply: ModelReply, started: float, operation: float,
        retries: int,
    ) -> None:
        """Record the call in the ledger and budget; emit its events; count its tokens."""
        services = self._services
        if reply.failure:
            services.ledger.record_error(self.agent)
        elif reply.response is not None:
            services.ledger.record(self.agent, served_model(reply.response, request),
                                   reply.usage, reply.cost_usd, reply.served_from, retries)
            services.budget.add(reply.cost_usd)
        services.tracer.add_tokens(sum(reply.usage.values()))
        outcome = "failed" if reply.failure else "refused" if reply.refusal else "ok"
        self._services.events.emit(
            "model_call", agent=self.agent, severity="warning" if reply.failure else "info",
            outcome=outcome, model=request["model"], tokens=dict(reply.usage),
            duration_ms=int((time.monotonic() - started) * 1000),
            operation_ms=int(operation * 1000), served_from=reply.served_from,
            cost_usd=reply.cost_usd,
        )
        if reply.problems:
            self._services.events.emit(
                "validation_failed", agent=self.agent, severity="warning", outcome="rejected",
                reply_type=reply_type.__name__, problems=reply.problems[:5],
            )

    def _degraded(
        self, request: dict, reply_type: type | None, failure: DependencyFailure
    ) -> ModelReply:
        """Serve a validated cached reply for ``request``, or report ``failure``."""
        cache = self._services.response_cache
        cached = cache.get(request) if cache else None
        if cached is None:
            return ModelReply(None, failure=failure)
        reply = read_reply(cached, reply_type)
        if reply.refusal or reply.problems:
            cache.discard(request)
            return ModelReply(None, failure=failure)
        self._event("degraded", "warning", served_from="cache", model=request["model"])
        return replace(reply, usage=TokenUsage(**zero_usage()), served_from="cache")

    def _event(self, event: str, severity: str, **details) -> None:
        self._services.on_event({"event": event, "severity": severity, "agent": self.agent,
                                 "dependency": "anthropic", **details})


def served_model(response, request: dict) -> str:
    """The model that served a response (a fallback may differ from the one requested)."""
    return getattr(response, "model", None) or request["model"]


def request_summary(request: dict) -> dict:
    """What an ``llm`` span shows of a request: its shape, never the prompts themselves."""
    output_format = request.get("output_config", {}).get("format") or {}
    return {
        "model": request["model"],
        "system_chars": len(json.dumps(request.get("system", ""))),
        "message_count": len(request.get("messages", [])),
        "tools": [tool["name"] for tool in request.get("tools", [])],
        "output_format": output_format.get("type"),
    }


def llm_outputs(reply: ModelReply) -> dict:
    """What an ``llm`` span shows of a reply, with LangSmith's ``usage_metadata``."""
    usage = reply.usage
    prompt = (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
              + usage.get("cache_read_input_tokens", 0))
    response = reply.response
    content = getattr(response, "content", None) or []
    return {
        "stop_reason": getattr(response, "stop_reason", None),
        "served_from": reply.served_from,
        "text_chars": len(reply.text),
        "tool_uses": [block.name for block in content if block.type == "tool_use"],
        "usage_metadata": {
            "input_tokens": prompt,
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": prompt + usage.get("output_tokens", 0),
            "input_token_details": {
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "cache_creation": usage.get("cache_creation_input_tokens", 0),
            },
        },
    }


def read_reply(response, reply_type: type | None) -> ModelReply:
    """Turn one SDK response into a ``ModelReply``, validating it against ``reply_type``."""
    usage = usage_of(response)
    if response.stop_reason == "refusal":
        return ModelReply(response, usage=usage, refusal=refusal_message(response.stop_details))
    if reply_type is None:
        return ModelReply(response, text=_text_of(response), usage=usage)
    data, text, problems = parse_reply(response, reply_type)
    return ModelReply(response, data=data, text=text, usage=usage, problems=problems)


def usage_of(response) -> TokenUsage:
    """The response's token counts as a ``TokenUsage``; missing cache counts are 0."""
    reported = response.usage
    return TokenUsage(**{
        key: getattr(reported, key, 0) or 0 for key in TOKEN_COUNTERS
    })


def parse_reply(response, reply_type: type) -> tuple[dict | None, str, list[str]]:
    """Join the reply's text, parse it as JSON and validate it against ``reply_type``.

    Returns ``(data, text, problems)``; ``data`` is ``None`` whenever ``problems`` is
    not empty.
    """
    text = _text_of(response)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, text, [f"{NOT_JSON}: {text[:200]!r}"]
    problems = validate(data, reply_type)
    return (None if problems else data), text, problems


def output_problem(problems: list[str]) -> str:
    """Describe why a structured reply can't be used, for an agent's error message."""
    if problems[0].startswith(NOT_JSON):
        return problems[0]
    return f"Model output failed validation: {first_problems(problems)}"


def failure_message(failure: DependencyFailure) -> str:
    """Describe a model call that could not be served, for an agent's error message."""
    if failure["error_type"] == BUDGET_EXCEEDED:
        return f"Run budget reached: {failure['message']}"
    if is_unavailable(failure):
        return f"Model service unavailable: {failure['message']}"
    return f"Model call failed: {failure['message']}"


def _text_of(response) -> str:
    return "".join(block.text for block in response.content if block.type == "text")
