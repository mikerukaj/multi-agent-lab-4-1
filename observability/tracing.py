"""LangSmith distributed tracing for a team run (directive 1, FR-214a).

One ``team-run`` root per run attempt; an ``agent:<name>`` span per agent invocation,
carrying ``agent_name``, ``token_count``, ``tool_calls`` and ``duration_ms``; and
``llm:<model>`` and ``tool:<name>`` spans under their agent's span
(contracts/tracing.md). QA's reviews and the fix round run on broker threads: the team
sends the trace headers in the A2A message metadata, and the endpoints continue the
trace from them, so every span shares the root's trace.

Only ``RunTree`` and ``Client`` from ``langsmith`` are used, within the API surface the
constitution approves. Spans have explicit parents (kept on a per-thread stack), so no
ambient context leaks between threads or runs. Tracing is off unless
``LANGSMITH_TRACING=true`` and a key are set; it then sends nothing, but durations,
tokens and tool counts are still measured. A tracing error never fails the run: it
becomes one warning.
"""

import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from observability.redaction import Redactor

DEFAULT_PROJECT = "multi-agent-lab"


@dataclass
class AgentSpanStats:
    """What one agent invocation used, filled in while it runs."""

    agent: str
    tokens: int = 0
    tool_calls: int = 0
    duration_ms: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class _Frame:
    run: object | None
    """The span's ``RunTree`` (``None`` when tracing is off or the span failed)."""
    agent: AgentSpanStats | None = None


@dataclass
class SpanResult:
    """Outputs, metadata and an error to set on a span when it ends."""

    outputs: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    error: str | None = None


class Tracer:
    """Opens the run's spans; a no-op that still measures when tracing is off."""

    def __init__(
        self,
        enabled: bool = False,
        client=None,
        project: str = DEFAULT_PROJECT,
        redactor: Redactor | None = None,
        on_warning: Callable[[str], None] | None = None,
        log_prompts: bool = False,
    ) -> None:
        self.enabled = enabled and client is not None
        self.client = client
        self.project = project
        self.log_prompts = log_prompts
        self.on_agent_end: Callable[[AgentSpanStats], None] | None = None
        self.on_tool_call: Callable[[str], None] | None = None
        self._redact = (redactor or Redactor([])).redact
        self._on_warning = on_warning or (lambda message: None)
        self._warned = False
        self._local = threading.local()
        self._root = None

    @classmethod
    def from_env(
        cls, env: Mapping[str, str], redactor: Redactor,
        on_warning: Callable[[str], None] | None = None,
    ) -> "Tracer":
        """Turn tracing on only with ``LANGSMITH_TRACING=true`` and a non-empty key."""
        on = env.get("LANGSMITH_TRACING", "").lower() == "true" and env.get("LANGSMITH_API_KEY")
        if not on:
            return cls(False, redactor=redactor, on_warning=on_warning)
        from langsmith import Client  # Deferred: only loaded when tracing is on.

        tracer = cls(True, None, env.get("LANGSMITH_PROJECT") or DEFAULT_PROJECT, redactor,
                     on_warning, env.get("LANGSMITH_LOG_PROMPTS", "").lower() == "true")
        options = {"api_key": env["LANGSMITH_API_KEY"],
                   "hide_inputs": redactor.redact, "hide_outputs": redactor.redact,
                   "hide_metadata": redactor.redact,
                   "tracing_error_callback": lambda error: tracer.warn(
                       f"LangSmith tracing error: {error}")}
        if env.get("LANGSMITH_ENDPOINT"):
            options["api_url"] = env["LANGSMITH_ENDPOINT"]
        tracer.client = Client(**options)
        tracer.enabled = True
        return tracer

    # Spans -------------------------------------------------------------------------

    @contextmanager
    def root_span(self, run_id: str, metadata: dict):
        """The ``team-run`` root of one run attempt."""
        run = self._start("team-run", "chain", {"run_id": run_id}, {"run_id": run_id,
                                                                     **metadata}, root=True)
        self._root = run
        with self._frame(run) as result:
            yield result

    @contextmanager
    def agent_span(self, agent: str, invocation: str, **metadata):
        """One agent invocation; yields its ``AgentSpanStats``.

        On exit the span gets ``agent_name``, ``token_count``, ``tool_calls`` and
        ``duration_ms`` (directive 1), plus ``metadata``.
        """
        stats = AgentSpanStats(agent=agent, metadata=dict(metadata))
        run = self._start(f"agent:{agent}", "chain", {"invocation": invocation},
                          {"agent_name": agent, "invocation": invocation, **metadata})
        started = time.monotonic()
        with self._frame(run, stats) as result:
            try:
                yield stats
            finally:
                stats.duration_ms = int((time.monotonic() - started) * 1000)
                result.metadata.update({
                    "agent_name": agent, "token_count": stats.tokens,
                    "tool_calls": stats.tool_calls, "duration_ms": stats.duration_ms,
                    **stats.metadata,
                })
                if self.on_agent_end:
                    self.on_agent_end(stats)

    @contextmanager
    def llm_span(self, model: str, inputs: dict):
        """One model call; set ``result.outputs`` (incl. ``usage_metadata``) before exit."""
        run = self._start(f"llm:{model}", "llm", inputs,
                          {"ls_provider": "anthropic", "ls_model_name": model})
        with self._frame(run) as result:
            yield result

    @contextmanager
    def tool_span(self, name: str, inputs: dict):
        """One tool call; counted on the active agent span (and by ``on_tool_call``)."""
        stats = self.active_agent()
        if stats is not None:
            stats.tool_calls += 1
        if self.on_tool_call and stats is not None:
            self.on_tool_call(stats.agent)
        run = self._start(f"tool:{name}", "tool", inputs, {})
        with self._frame(run) as result:
            yield result

    def span_event(self, name: str, **attributes) -> None:
        """Attach an event (a circuit state change) to the active span, if traced."""
        run = self._parent()
        if run is None:
            return
        try:
            run.add_event({"name": name, "time": time.time(), "kwargs": self._redact(attributes)})
        except Exception as error:  # A tracing failure must never fail the run.
            self.warn(f"span event {name} not recorded: {error}")

    def add_tokens(self, tokens: int) -> None:
        """Count a model call's tokens on the active agent span."""
        stats = self.active_agent()
        if stats is not None:
            stats.tokens += tokens

    def active_agent(self) -> AgentSpanStats | None:
        """The innermost agent span open on this thread."""
        for frame in reversed(self._stack()):
            if frame.agent is not None:
                return frame.agent
        return None

    # Propagation -------------------------------------------------------------------

    def headers(self) -> dict[str, str]:
        """Headers that let another thread continue this trace (``{}`` when off)."""
        run = self._parent()
        if run is None:
            return {}
        try:
            return {str(key): str(value) for key, value in run.to_headers().items()}
        except Exception as error:  # A tracing failure must never fail the run.
            self.warn(f"trace headers unavailable: {error}")
            return {}

    @contextmanager
    def continue_from(self, headers: Mapping[str, str] | None):
        """Make spans opened on this thread children of the span ``headers`` came from.

        Missing or malformed headers start the spans with no parent (one warning).
        """
        parent = None
        if self.enabled and headers:
            from langsmith.run_trees import RunTree  # Deferred: tracing is on.

            try:
                parent = RunTree.from_headers(dict(headers), client=self.client,
                                              project_name=self.project)
            except Exception as error:  # Malformed headers: trace on, unparented.
                self.warn(f"could not continue the trace from A2A headers: {error}")
        elif self.enabled:
            self.warn("an A2A message carried no trace headers")
        stack = self._stack()
        stack.append(_Frame(parent))
        try:
            yield
        finally:
            stack.pop()

    def root_url(self) -> str | None:
        """The LangSmith URL of the run's root trace, when tracing is on."""
        if not self.enabled or self._root is None:
            return None
        try:
            url = self._root.get_url()
        except Exception as error:  # The URL is informational only.
            self.warn(f"trace URL unavailable: {error}")
            return None
        return url if isinstance(url, str) else None

    def warn(self, message: str) -> None:
        """Report a tracing problem once per run; tracing problems never fail the run."""
        if not self._warned:
            self._warned = True
            self._on_warning(message)

    # Internals ---------------------------------------------------------------------

    def _stack(self) -> list[_Frame]:
        if not hasattr(self._local, "stack"):
            self._local.stack = []
        return self._local.stack

    def _parent(self):
        for frame in reversed(self._stack()):
            if frame.run is not None:
                return frame.run
        return None

    def _start(self, name: str, run_type: str, inputs: dict, metadata: dict, root=False):
        """Create and post a span under the current parent; ``None`` when off or failing."""
        if not self.enabled:
            return None
        from langsmith.run_trees import RunTree  # Deferred: tracing is on.

        inputs, metadata = self._redact(inputs), self._redact(metadata)
        parent = None if root else self._parent()
        try:
            if parent is None:
                run = RunTree(name=name, run_type=run_type, inputs=inputs, client=self.client,
                              project_name=self.project, extra={"metadata": metadata})
            else:
                run = parent.create_child(name=name, run_type=run_type, inputs=inputs,
                                          extra={"metadata": metadata})
            run.post()
            return run
        except Exception as error:  # A tracing failure must never fail the run.
            self.warn(f"LangSmith span {name} not recorded: {error}")
            return None

    @contextmanager
    def _frame(self, run, stats: AgentSpanStats | None = None):
        """Push a span for the duration of the block, then end and send it."""
        result = SpanResult()
        stack = self._stack()
        stack.append(_Frame(run, stats))
        try:
            yield result
        except BaseException as error:
            result.error = result.error or f"{type(error).__name__}: {error}"
            raise
        finally:
            stack.pop()
            self._finish(run, result)

    def _finish(self, run, result: SpanResult) -> None:
        if run is None:
            return
        try:
            if result.metadata:
                run.add_metadata(self._redact(result.metadata))
            run.end(outputs=self._redact(result.outputs), error=self._redact(result.error))
            run.patch()
        except Exception as error:  # A tracing failure must never fail the run.
            self.warn(f"LangSmith span {run.name} not completed: {error}")
