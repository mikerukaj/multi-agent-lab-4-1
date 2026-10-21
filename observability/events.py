"""The run's structured event record (``events.jsonl``) and its summary (FR-214-FR-221).

``EventLog.emit`` writes one redacted JSON object per line, flushed as it happens, to
``<workspace_root>/.runs/<run_id>/events.jsonl``; the file is appended to on resume, so
it covers every attempt. Events below the verbosity threshold (``LOG_LEVEL``) are not
written, but errors and ``run_end`` always are, and every event is kept in memory for
the summary. A failed write never fails the run: it sets ``events_lost`` (FR-221).

Which agent and stage an event belongs to comes from the thread's current
``context``: the team's stage wrapper sets it on the main thread, and the A2A endpoints
set it on the broker threads, so a retry deep inside a tool call is still attributed.
"""

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from observability.redaction import Redactor
from orchestration.state import RunSummary, SummaryCounters

SEVERITIES = ("debug", "info", "warning", "error")
ALWAYS_WRITTEN = ("error", "run_end")
TOP_LEVEL = ("event", "severity", "outcome", "agent", "stage", "iteration", "task_id")
Subscriber = Callable[[dict], None]


def severity_from_env(env: Mapping[str, str]) -> str:
    """Read ``LOG_LEVEL`` (default ``info``).

    Raises:
        ValueError: naming the variable, if the value is not a known severity.
    """
    value = (env.get("LOG_LEVEL") or "info").lower()
    if value not in SEVERITIES:
        raise ValueError(f"LOG_LEVEL must be one of {', '.join(SEVERITIES)}, not {value!r}")
    return value


def utc_now() -> str:
    """The current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


class EventLog:
    """Writes a run's events; ``path=None`` keeps them in memory only (unit tests)."""

    def __init__(
        self,
        path: Path | None,
        run_id: str,
        min_severity: str = "info",
        redactor: Redactor | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.path = Path(path) if path else None
        self.run_id = run_id
        self.min_severity = min_severity
        self.events: list[dict] = []
        self.events_lost = False
        self.subscribers: list[Subscriber] = []
        self._redactor = redactor or Redactor([])
        self._clock = clock
        self._lock = threading.Lock()
        self._local = threading.local()
        self._handle = None

    def emit(
        self,
        event: str,
        *,
        severity: str = "info",
        outcome: str = "ok",
        agent: str | None = None,
        stage: str | None = None,
        iteration: int | None = None,
        task_id: str | None = None,
        **details,
    ) -> dict:
        """Record one event; agent, stage, iteration and task default to the context."""
        context = self._current()
        record = {
            "ts": self._clock(),
            "run_id": self.run_id,
            "agent": agent or context.get("agent") or "team",
            "stage": stage or context.get("stage") or "team",
            "event": event,
            "severity": severity,
            "outcome": outcome,
        }
        for key, value in (("iteration", iteration or context.get("iteration")),
                           ("task_id", task_id or context.get("task_id"))):
            if value is not None:
                record[key] = value
        if details:
            record["details"] = details
        record = self._redactor.redact(record)
        with self._lock:
            self.events.append(record)
            if self._should_write(record):
                self._write(record)
        for subscriber in list(self.subscribers):
            subscriber(record)
        return record

    def record(self, raw: dict) -> dict:
        """Emit an event reported as a plain dict (guards, breakers, the model caller)."""
        top = {key: raw[key] for key in TOP_LEVEL if raw.get(key) is not None}
        details = {key: value for key, value in raw.items() if key not in TOP_LEVEL}
        return self.emit(top.pop("event"), **top, **details)

    @contextmanager
    def context(self, **fields):
        """Attribute events on this thread to ``agent``/``stage``/``iteration``/``task_id``."""
        stack = self._stack()
        stack.append({**(stack[-1] if stack else {}), **fields})
        try:
            yield
        finally:
            stack.pop()

    def close(self) -> None:
        """Close the file (events are already flushed line by line)."""
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def _should_write(self, record: dict) -> bool:
        if record["event"] in ALWAYS_WRITTEN or record["severity"] == "error":
            return True
        return SEVERITIES.index(record["severity"]) >= SEVERITIES.index(self.min_severity)

    def _write(self, record: dict) -> None:
        """Append one line (lock held); a failure marks events as lost, never raises."""
        if self.path is None:
            return
        try:
            if self._handle is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = open(self.path, "a", encoding="utf-8", buffering=1)
            self._handle.write(json.dumps(record) + "\n")
        except OSError:
            self.events_lost = True

    def _stack(self) -> list[dict]:
        if not hasattr(self._local, "stack"):
            self._local.stack = []
        return self._local.stack

    def _current(self) -> dict:
        stack = self._stack()
        return stack[-1] if stack else {}


def read_events(path: Path) -> list[dict]:
    """Every event in an ``events.jsonl`` file (all attempts), skipping torn lines."""
    events = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # A line cut short by a crash; the rest of the record still counts.
    return events


def _counters() -> SummaryCounters:
    return SummaryCounters(model_calls=0, prompt_tokens=0, completion_tokens=0,
                           cache_read_tokens=0, cost_usd=0.0, duration_ms=0, retries=0,
                           restarts=0, errors=0)


def _add(counters: SummaryCounters, event: dict) -> None:
    """Fold one event into ``counters``."""
    details = event.get("details", {})
    kind = event["event"]
    if kind == "model_call" and details.get("served_from", "live") == "live":
        tokens = details.get("tokens", {})
        counters["model_calls"] += 1
        counters["prompt_tokens"] += sum(tokens.get(key, 0) for key in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        counters["completion_tokens"] += tokens.get("output_tokens", 0)
        counters["cache_read_tokens"] += tokens.get("cache_read_input_tokens", 0)
        cost = details.get("cost_usd")
        counters["cost_usd"] = None if cost is None or counters["cost_usd"] is None else (
            round(counters["cost_usd"] + cost, 6))
    elif kind == "stage_end":
        counters["duration_ms"] += details.get("duration_ms", 0)
    counters["retries"] += kind == "retry"
    counters["restarts"] += kind == "restart"
    counters["errors"] += event["severity"] == "error"


def summarise(events: Iterable[dict], events_lost: bool = False) -> RunSummary:
    """Totals per agent, per stage and for the run, over every attempt (FR-217)."""
    per_agent: dict[str, SummaryCounters] = {}
    per_stage: dict[str, SummaryCounters] = {}
    totals = _counters()
    attempts = 1
    for event in events:
        if event["event"] == "resume":
            attempts += 1
        for counters in (per_agent.setdefault(event["agent"], _counters()),
                         per_stage.setdefault(event["stage"], _counters()), totals):
            _add(counters, event)
    return RunSummary(per_agent=per_agent, per_stage=per_stage, totals=totals,
                      events_lost=events_lost, attempts=attempts)


def progress_line(event: dict) -> str | None:
    """The human-readable stderr line an event carries, if any (FR-220)."""
    return event.get("details", {}).get("progress")
