"""``RunServices``: the per-run state every agent and node shares, passed explicitly.

One run needs a handful of shared, stateful helpers: the stop flag set by a signal, the
secret redactor, the circuit breakers and retry policy, the degraded-response cache and,
as later parts of 003 add them, the usage ledger, the budget, the event log and the
tracer. The constitution forbids hidden global mutable state (Principle II), so they are
built once per run in ``run_team``, bundled here, and handed to each agent with
``attach(services)``. Every field has a default, so callers that predate a field never
change.
"""

import os
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cost.ledger import RunBudget, UsageLedger
from cost.pricing import load_price_table, price_table_path
from cost.report import DEFAULT_DIRECTORY as DEFAULT_COST_REPORT_DIR, cost_report_directory
from observability.events import EventLog, severity_from_env
from observability.redaction import Redactor
from observability.tracing import Tracer
from orchestration.state import CircuitState, Dependency
from resilience.circuit import CircuitBreaker, CircuitSettings, DependencyGuard, EventSink
from resilience.response_cache import ResponseCache
from resilience.retry import RetryPolicy

PROMPT_CACHE_TTLS = ("5m", "1h", "off")
CIRCUIT_EVENTS = {"open": "circuit_opened", "half_open": "circuit_half_open",
                  "closed": "circuit_closed"}


def budget_from_env(env: Mapping[str, str]) -> float | None:
    """``RUN_BUDGET_USD``: the per-run cap in USD, or ``None`` (no cap) when unset.

    Raises:
        ValueError: naming the variable, if it is not a number greater than 0.
    """
    raw = env.get("RUN_BUDGET_USD")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"RUN_BUDGET_USD must be a number of USD, not {raw!r}") from error
    if value <= 0:
        raise ValueError(f"RUN_BUDGET_USD must be greater than 0, not {raw!r}")
    return value


def prompt_cache_ttl_from_env(env: Mapping[str, str]) -> str:
    """``PROMPT_CACHE_TTL`` (default ``5m``).

    Raises:
        ValueError: naming the variable, if it is not ``5m``, ``1h`` or ``off``.
    """
    value = env.get("PROMPT_CACHE_TTL") or "5m"
    if value not in PROMPT_CACHE_TTLS:
        raise ValueError(f"PROMPT_CACHE_TTL must be one of {', '.join(PROMPT_CACHE_TTLS)}, "
                         f"not {value!r}")
    return value


@dataclass
class RunServices:
    """Shared per-run helpers; build one with ``RunServices.create``."""

    run_id: str
    stop_requested: threading.Event = field(default_factory=threading.Event)
    redactor: Redactor = field(default_factory=lambda: Redactor([]))
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    circuit_settings: CircuitSettings = field(default_factory=CircuitSettings)
    response_cache: ResponseCache | None = None
    rng: random.Random = field(default_factory=random.Random)
    sleep: Callable[[float], object] | None = None
    """How guards wait between retries; by default the stop flag's ``wait``, so a stop
    request cuts the wait short."""
    on_event: EventSink | None = None
    """Where guards and breakers report events; the run's event log by default."""
    events: EventLog | None = None
    """The run's event record (in memory until the run opens its ``events.jsonl``)."""
    tracer: Tracer | None = None
    """LangSmith tracing; off (but still measuring) unless configured."""
    clock: Callable[[], float] | None = None
    """The breakers' clock (``time.monotonic`` when ``None``); injected in tests."""
    price_table: dict = field(default_factory=lambda: {"version": "none", "models": {}})
    ledger: UsageLedger = field(default_factory=UsageLedger)
    budget: RunBudget = field(default_factory=RunBudget)
    prompt_cache_ttl: str = "5m"
    """``PROMPT_CACHE_TTL``: ``5m``, ``1h`` or ``off`` (no cache markers)."""
    cost_report_dir: Path = DEFAULT_COST_REPORT_DIR
    """``COST_REPORT_DIR``: where the run's JSON cost report is written."""
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    signal_received: int | None = None
    """The signal that asked the run to stop, if one did (for the exit code)."""
    stop_callbacks: list[Callable[[], object]] = field(default_factory=list)
    """Run once, off the signal handler, when a stop is requested (kill containers)."""
    stop_wakers: list[threading.Event] = field(default_factory=list)
    """Events set by ``request_stop``, so a watcher thread wakes without polling."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.sleep is None:
            self.sleep = self.stop_requested.wait
        if self.events is None:
            self.events = EventLog(None, self.run_id, redactor=self.redactor)
        if self.on_event is None:
            self.on_event = self.events.record
        if self.tracer is None:
            self.tracer = Tracer(False, redactor=self.redactor, on_warning=self._tracing_warning)
        self.connect_tracer()

    def connect_tracer(self) -> None:
        """Count each agent span's duration and each tool span in the ledger (T069)."""
        self.tracer.on_agent_end = lambda stats: self.ledger.add_duration(
            stats.agent, stats.duration_ms
        )
        self.tracer.on_tool_call = self.ledger.record_tool_call

    def _tracing_warning(self, message: str) -> None:
        self.events.emit("warning", severity="warning", outcome="tracing", message=message)

    @classmethod
    def create(
        cls, run_id: str | None = None, env: Mapping[str, str] | None = None, **overrides
    ) -> "RunServices":
        """Build the services for one run, reading settings from ``env``.

        ``run_id`` defaults to a new ``agents.coder_agent.new_run_id()``; ``env`` to the
        process environment. ``overrides`` replace fields (tests inject sleeps and
        clocks this way).

        Raises:
            ValueError: naming the variable, if a ``RETRY_*``, ``CIRCUIT_*`` or
                ``LLM_RESPONSE_CACHE_*`` setting is out of range.
        """
        # Deferred: agents.coder_agent imports agents.llm, which imports this module.
        from agents.coder_agent import new_run_id

        env = os.environ if env is None else env
        run_id = run_id or new_run_id()
        redactor = overrides.pop("redactor", None) or Redactor.from_env(env)
        events = overrides.pop("events", None) or EventLog(
            None, run_id, severity_from_env(env), redactor
        )
        settings = {
            "redactor": redactor,
            "events": events,
            "retry_policy": RetryPolicy.from_env(env),
            "circuit_settings": CircuitSettings.from_env(env),
            "response_cache": ResponseCache.from_env(env),
            "price_table": load_price_table(price_table_path(env)),
            "budget": RunBudget(budget_from_env(env)),
            "prompt_cache_ttl": prompt_cache_ttl_from_env(env),
            "cost_report_dir": cost_report_directory(env),
        }
        services = cls(run_id=run_id, **{**settings, **overrides})
        if "tracer" not in overrides:
            services.tracer = Tracer.from_env(env, redactor, services._tracing_warning)
            services.connect_tracer()
        return services

    def request_stop(self, signum: int | None = None) -> None:
        """Ask the run to stop at its next call boundary (FR-210).

        Only sets flags, so it is safe inside a signal handler; ``stop_callbacks`` run
        on the run's stop watcher thread.
        """
        if signum is not None and self.signal_received is None:
            self.signal_received = signum
        self.stop_requested.set()
        for waker in list(self.stop_wakers):
            waker.set()

    def tool_call(
        self,
        name: str,
        inputs: dict,
        operation: Callable[[], object],
        describe: Callable[[object], dict] | None = None,
        event: str = "tool_call",
        guard: DependencyGuard | None = None,
    ):
        """Run one tool call in its ``tool:<name>`` span and record it as an event.

        ``describe(result)`` gives the span outputs and event details (``is_error``,
        ``exit_code``, ``timed_out``). ``guard`` is the call's dependency guard, whose
        measured operation time separates the tool's own time from guard bookkeeping
        (``operation_ms``, SC-210).
        """
        started = time.monotonic()
        with self.tracer.tool_span(name, inputs) as span:
            result = operation()
            outputs = describe(result) if describe else {}
            span.outputs = outputs
        duration_ms = int((time.monotonic() - started) * 1000)
        operation_ms = int(guard.operation_seconds * 1000) if guard else duration_ms
        self.events.emit(event, outcome="error" if outputs.get("is_error") else "ok",
                         tool=name, duration_ms=duration_ms,
                         operation_ms=min(operation_ms, duration_ms), **outputs)
        return result

    def breaker(self, dependency: Dependency, target: str) -> CircuitBreaker:
        """The run's breaker for ``dependency:target``, created on first use.

        Callers on the same key share it: the Coder and QA on one model share one
        circuit, so failures seen by one count for the other (spec edge case).
        """
        key = f"{dependency}:{target}"
        with self._lock:
            if key not in self.breakers:
                extra = {"clock": self.clock} if self.clock else {}
                self.breakers[key] = CircuitBreaker(
                    key, self.circuit_settings.threshold,
                    self.circuit_settings.cooldown_seconds,
                    listener=self._circuit_changed, **extra,
                )
            return self.breakers[key]

    def guard(self, dependency: Dependency, target: str) -> DependencyGuard:
        """A guard for one dependency target, sharing the run's breaker for its key."""
        return DependencyGuard(
            dependency, target, self.breaker(dependency, target), self.retry_policy,
            self.rng, self.sleep, self.stop_requested, self.on_event,
        )

    def _circuit_changed(self, key: str, old: CircuitState, new: CircuitState) -> None:
        dependency, _, target = key.partition(":")
        self.tracer.span_event(CIRCUIT_EVENTS[new], key=key, previous=old)
        self.on_event({
            "event": CIRCUIT_EVENTS[new], "severity": "warning" if new == "open" else "info",
            "dependency": dependency, "target": target, "outcome": new, "previous": old,
        })
