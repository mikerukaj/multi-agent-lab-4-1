"""Per-agent usage and cost for a run, and the run's budget (FR-222, FR-223, R9, R15).

``UsageLedger`` is the source of truth for per-agent totals: QA and the fix round run
on broker threads, so every ``ModelCaller`` records here (thread-safely) instead of the
totals travelling back in A2A artifacts. ``RunBudget`` is checked before every model
call, so a run overspends by at most the one call already in flight (SC-206).
"""

import threading

from orchestration.state import AgentUsage

AGENTS = ("pm", "coder", "qa")


class BudgetExceeded(Exception):
    """The run's spend has reached its budget: no new model call may start."""


def empty_usage(agent: str) -> AgentUsage:
    """An ``AgentUsage`` for ``agent`` with every counter at zero."""
    return AgentUsage(agent=agent, models=[], model_calls=0, cached_replays=0,
                      prompt_tokens=0, completion_tokens=0, cache_read_tokens=0,
                      cache_write_tokens=0, cost_usd=0.0, retries=0, errors=0,
                      tool_calls=0, duration_ms=0)


class UsageLedger:
    """Thread-safe per-agent usage and cost totals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage = {agent: empty_usage(agent) for agent in AGENTS}

    def record(
        self, agent: str, model_served: str | None, usage: dict, cost: float | None,
        served_from: str = "live", retries: int = 0,
    ) -> None:
        """Add one model call. A cached replay costs nothing; an unpriced one makes the
        agent's cost unknown (``None``) from then on."""
        with self._lock:
            entry = self._entry(agent)
            entry["retries"] += retries
            if served_from == "cache":
                entry["cached_replays"] += 1
                return
            if model_served and model_served not in entry["models"]:
                entry["models"].append(model_served)
            entry["model_calls"] += 1
            read = usage.get("cache_read_input_tokens", 0)
            written = usage.get("cache_creation_input_tokens", 0)
            entry["prompt_tokens"] += usage.get("input_tokens", 0) + read + written
            entry["completion_tokens"] += usage.get("output_tokens", 0)
            entry["cache_read_tokens"] += read
            entry["cache_write_tokens"] += written
            entry["cost_usd"] = (
                None if cost is None or entry["cost_usd"] is None
                else round(entry["cost_usd"] + cost, 8)
            )

    def record_tool_call(self, agent: str) -> None:
        with self._lock:
            self._entry(agent)["tool_calls"] += 1

    def record_error(self, agent: str) -> None:
        with self._lock:
            self._entry(agent)["errors"] += 1

    def add_duration(self, agent: str, milliseconds: int) -> None:
        with self._lock:
            self._entry(agent)["duration_ms"] += milliseconds

    def snapshot(self) -> dict[str, AgentUsage]:
        """A copy of every agent's totals."""
        with self._lock:
            return {agent: {**entry, "models": list(entry["models"])}
                    for agent, entry in self._usage.items()}

    def load(self, snapshot: dict) -> None:
        """Carry totals forward from an earlier attempt (a resumed run)."""
        with self._lock:
            for agent, entry in (snapshot or {}).items():
                self._usage[agent] = {**empty_usage(agent), **entry,
                                      "models": list(entry.get("models", []))}

    def total_cost(self) -> float | None:
        """The run's total cost, or ``None`` if any agent's cost is unknown."""
        costs = [entry["cost_usd"] for entry in self.snapshot().values()]
        return None if None in costs else round(sum(costs), 8)

    def _entry(self, agent: str) -> AgentUsage:
        if agent not in self._usage:
            self._usage[agent] = empty_usage(agent)
        return self._usage[agent]


class RunBudget:
    """The run's spending cap (``None`` = no cap) and its spend so far."""

    def __init__(
        self, cap_usd: float | None = None, spent: float = 0.0, unpriced_models=()
    ) -> None:
        """
        Raises:
            ValueError: if the cap is not positive, or a cap is set while a configured
                model has no price, so the cap could not be enforced (FR-224).
        """
        if cap_usd is not None and cap_usd <= 0:
            raise ValueError(f"the budget must be greater than 0, not {cap_usd}")
        unpriced = sorted(set(unpriced_models))
        if cap_usd is not None and unpriced:
            raise ValueError(
                f"a budget can't be enforced: {', '.join(unpriced)} has no price in the price "
                "table"
            )
        self.cap_usd = cap_usd
        self.spent = spent
        self._lock = threading.Lock()

    def check(self) -> None:
        """Raise ``BudgetExceeded`` once the spend has reached the cap."""
        with self._lock:
            if self.cap_usd is not None and self.spent >= self.cap_usd:
                raise BudgetExceeded(
                    f"spend ${self.spent:.4f} has reached the run budget ${self.cap_usd:.4f}"
                )

    def add(self, cost: float | None) -> None:
        """Add one call's cost (an unpriced call adds nothing it could measure)."""
        if cost:
            with self._lock:
                self.spent = round(self.spent + cost, 8)
