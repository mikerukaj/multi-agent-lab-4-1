"""Circuit breakers and the guard every call to an external dependency goes through.

``DependencyGuard.call`` runs one logical call: it checks the stop flag and the
dependency's ``CircuitBreaker``, then retries temporary failures under the run's
``RetryPolicy`` (contracts/resilience.md, "Guarded call semantics"). It never raises
for a dependency failure: it returns a ``GuardFailure`` holding the structured
``DependencyFailure``, so no node crashes (FR-209c).

The breaker counts failed *calls*, not attempts (research R5): it opens on the
threshold-th consecutive failed call, lets exactly one trial call through after the
cool-down (half-open), and any success closes it. It is thread-safe because QA and the
fix round run on broker threads; sleeps happen outside its lock.
"""

import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from orchestration.state import CircuitState, Dependency, DependencyFailure
from resilience.classify import CIRCUIT_OPEN, classify, retry_after_seconds
from resilience.retry import RetryPolicy, backoff_delay, number_setting

T = TypeVar("T")
EventSink = Callable[[dict], None]


def ignore_event(event: dict) -> None:
    """The default event sink: drop the event."""


@dataclass(frozen=True)
class CircuitSettings:
    """``CIRCUIT_FAILURE_THRESHOLD`` and ``CIRCUIT_COOLDOWN_SECONDS``."""

    threshold: int = 3
    cooldown_seconds: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "CircuitSettings":
        """Read the circuit settings from ``env``.

        Raises:
            ValueError: naming the variable, if a value is out of range.
        """
        defaults = cls()
        return cls(
            threshold=int(number_setting(
                env, "CIRCUIT_FAILURE_THRESHOLD", defaults.threshold, 1, 10, int
            )),
            cooldown_seconds=number_setting(
                env, "CIRCUIT_COOLDOWN_SECONDS", defaults.cooldown_seconds, 1, 3600
            ),
        )


class CircuitBreaker:
    """The state (closed, open, half-open) of one dependency's circuit in a run."""

    def __init__(
        self,
        key: str,
        threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        listener: Callable[[str, CircuitState, CircuitState], None] | None = None,
    ) -> None:
        self.key = key
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._listener = listener
        self._lock = threading.Lock()
        self._state: CircuitState = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._trial_running = False

    @property
    def state(self) -> CircuitState:
        """The circuit's state now."""
        with self._lock:
            return self._state

    @property
    def consecutive_failures(self) -> int:
        """Failed calls since the last success."""
        with self._lock:
            return self._failures

    @property
    def opened_at(self) -> float:
        """The clock time the circuit last opened."""
        with self._lock:
            return self._opened_at

    def cooldown_remaining(self) -> float:
        """Seconds until an open circuit lets a trial call through (0 if not open)."""
        with self._lock:
            if self._state != "open":
                return 0.0
            return max(0.0, self.cooldown_seconds - (self._clock() - self._opened_at))

    def admit(self) -> bool:
        """Whether a call may go ahead now; admits exactly one half-open trial."""
        with self._lock:
            if self._state == "closed":
                return True
            if self._trial_running:
                return False
            if self._clock() - self._opened_at < self.cooldown_seconds:
                return False
            change = self._move("half_open")
            self._trial_running = True
        self._notify(change)
        return True

    def success(self) -> None:
        """Record a successful call: the circuit closes and the count resets."""
        with self._lock:
            self._failures = 0
            self._trial_running = False
            change = self._move("closed")
        self._notify(change)

    def failure(self) -> None:
        """Record a failed call: a failed trial reopens; the threshold-th failure opens."""
        with self._lock:
            self._failures += 1
            trial_failed = self._trial_running
            self._trial_running = False
            change = None
            if trial_failed or (self._state == "closed" and self._failures >= self.threshold):
                self._opened_at = self._clock()
                change = self._move("open", force=trial_failed)
        self._notify(change)

    def _move(self, new: CircuitState, force: bool = False):
        """Change state (lock held); return the change to report, if any."""
        old = self._state
        if old == new and not force:
            return None
        self._state = new
        return (old, new)

    def _notify(self, change) -> None:
        if change and self._listener:
            self._listener(self.key, *change)


class GuardFailure:
    """What ``DependencyGuard.call`` returns instead of a result when the call failed."""

    __slots__ = ("failure",)

    def __init__(self, failure: DependencyFailure) -> None:
        self.failure = failure

    def __repr__(self) -> str:
        return f"GuardFailure({self.failure!r})"


class DependencyGuard:
    """Retries and circuit breaking for one dependency target, in one run."""

    def __init__(
        self,
        dependency: Dependency,
        target: str,
        breaker: CircuitBreaker,
        policy: RetryPolicy,
        rng: random.Random,
        sleep: Callable[[float], object],
        stop_requested: threading.Event,
        on_event: EventSink = ignore_event,
    ) -> None:
        self.dependency = dependency
        self.target = target
        self.breaker = breaker
        self.policy = policy
        self._rng = rng
        self._sleep = sleep
        self._stop = stop_requested
        self._on_event = on_event
        self.operation_seconds = 0.0
        """Time the last ``call`` spent inside the operation itself, over every attempt
        (the rest is guard bookkeeping and retry waits; SC-210)."""
        self.retries = 0
        """Retries the last ``call`` made (attempts after the first)."""

    def call(
        self,
        operation: Callable[[], T],
        classify_result: Callable[[T], tuple | None] | None = None,
    ) -> T | GuardFailure:
        """Run ``operation`` with retries; return its result or a ``GuardFailure``.

        ``classify_result`` turns a *returned* value into a failure classification,
        for operations that report failure in their result (a ``docker run`` that
        exited 125). An exception that ``classify`` does not recognise as a
        dependency failure propagates unchanged: it is a programming error or a tool
        result the caller handles.
        """
        self.operation_seconds, self.retries = 0.0, 0
        if self._stop.is_set():
            return self._failed("permanent", 0, "interrupted", "the run is stopping")
        if not self.breaker.admit():
            self.emit("degraded", "warning", outcome="circuit_open")
            return self._failed("temporary", 0, CIRCUIT_OPEN, f"{self.breaker.key} is open")
        return self._attempts(operation, classify_result)

    def _attempts(self, operation, classify_result) -> T | GuardFailure:
        attempt, kind, error_type, cause = 0, "temporary", "", ""
        for attempt in range(self.policy.max_retries + 1):
            started = time.monotonic()
            try:
                result = operation()
                problem = classify_result(result) if classify_result else None
                if problem is None:
                    self.breaker.success()
                    return result
                error = result
            except Exception as raised:
                problem = classify(raised, self.dependency)
                if problem is None:
                    raise
                error = raised
            finally:
                self.operation_seconds += time.monotonic() - started
            _, kind, cause = problem
            error_type = type(error).__name__
            if not self._retry(kind, attempt, error, error_type, cause):
                break
        if self._stop.is_set():
            # Stopped while waiting to retry: the dependency did not fail this call.
            return self._failed("permanent", attempt + 1, "interrupted", "the run is stopping")
        self.breaker.failure()
        return self._failed(kind, attempt + 1, error_type, cause)

    def _retry(self, kind: str, attempt: int, error, error_type: str, cause: str) -> bool:
        """Wait before the next attempt; return ``False`` when there should be none."""
        if kind == "permanent" or attempt == self.policy.max_retries:
            return False
        delay = backoff_delay(self.policy, attempt, retry_after_seconds(error), self._rng)
        if delay is None:
            return False
        self.emit("retry", "warning", outcome="retrying", attempt=attempt + 1,
                   delay_seconds=round(delay, 3), error_type=error_type, message=cause)
        self._sleep(delay)
        self.retries += 1
        return not self._stop.is_set()

    def _failed(self, kind, attempts: int, error_type: str, message: str) -> GuardFailure:
        state = self.breaker.state
        return GuardFailure(DependencyFailure(
            dependency=self.dependency,
            target=self.target,
            kind=kind,
            attempts=attempts,
            circuit=state,
            error_type=error_type,
            message=message,
            retry_after_seconds=(
                round(self.breaker.cooldown_remaining(), 3) if state == "open" else None
            ),
        ))

    def emit(self, event: str, severity: str, **details) -> None:
        """Record an event about this dependency (callers report restarts this way)."""
        self._on_event({
            "event": event, "severity": severity, "dependency": self.dependency,
            "target": self.target, **details,
        })
