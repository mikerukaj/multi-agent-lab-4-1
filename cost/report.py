"""The per-run JSON cost report in ``docs/cost_reports/<run_id>.json`` (FR-222a).

Written at the end of every run however it ends, atomically, with secrets redacted, and
matching contracts/cost-report.schema.json. A resumed run rewrites it with cumulative
totals and one more entry in ``attempts``. A failed write is reported and returns
``None``; it never changes the run's outcome.
"""

import json
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from orchestration.checkpoint import write_json_atomically

SCHEMA_VERSION = 1
DEFAULT_DIRECTORY = Path("docs/cost_reports")
TOTAL_KEYS = ("model_calls", "prompt_tokens", "completion_tokens", "cache_read_tokens",
              "cache_write_tokens")
AGENT_KEYS = ("models", "model_calls", "cached_replays", *TOTAL_KEYS[1:], "cost_usd")


def cost_report_directory(env: Mapping[str, str]) -> Path:
    """``COST_REPORT_DIR``, or ``docs/cost_reports``."""
    return Path(env.get("COST_REPORT_DIR") or DEFAULT_DIRECTORY)


def write_cost_report(
    directory: Path,
    state: Mapping,
    ledger_snapshot: Mapping[str, Mapping],
    price_version: str,
    ttl: str,
    started_at: str,
    configured_models: Mapping[str, str | None],
    redact: Callable[[object], object],
    on_error: Callable[[str], None] | None = None,
) -> Path | None:
    """Write the run's cost report; return its path, or ``None`` if it couldn't be written.

    ``started_at`` is when this attempt started; earlier attempts are read back from an
    existing report for the same run.
    """
    path = Path(directory) / f"{state['run_id']}.json"
    try:
        report = build_cost_report(path, state, ledger_snapshot, price_version, ttl,
                                   started_at, configured_models, redact)
        write_json_atomically(path, report)
        return path
    except OSError as error:
        message = f"cost report not written: {error}"
        print(message, file=sys.stderr)
        if on_error:
            on_error(message)
        return None


def build_cost_report(
    path: Path, state, ledger_snapshot, price_version, ttl, started_at, configured_models,
    redact,
) -> dict:
    """The report's content (see contracts/cost-report.schema.json)."""
    agents = {
        agent: {"model": configured_models.get(agent) or "unknown",
                **{key: ledger_snapshot.get(agent, {}).get(key, [] if key == "models" else 0)
                   for key in AGENT_KEYS}}
        for agent in ("pm", "coder", "qa")
    }
    costs = [entry["cost_usd"] for entry in agents.values()]
    total_cost = None if None in costs else round(sum(costs), 8)
    total = {key: sum(entry[key] for entry in agents.values()) for key in TOTAL_KEYS}
    earlier = _earlier_attempts(path)
    ended_at = _now()
    attempt_cost = None
    if total_cost is not None and all(item["cost_usd"] is not None for item in earlier):
        attempt_cost = round(max(0.0, total_cost - sum(item["cost_usd"] for item in earlier)), 8)
    attempts = earlier + [{"started_at": started_at, "ended_at": ended_at,
                           "end_reason": state.get("end_reason"), "cost_usd": attempt_cost}]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": state["run_id"],
        "requirement_summary": redact(state["requirement"][:200])[:200],
        "started_at": attempts[0]["started_at"],
        "ended_at": ended_at,
        "status": state["status"] if state["status"] != "running" else "error",
        "end_reason": state.get("end_reason"),
        "price_table_version": price_version,
        "prompt_cache_ttl": ttl,
        "budget_usd": state.get("budget_usd"),
        "agents": agents,
        "total": {**total, "cost_usd": total_cost},
        "attempts": attempts,
        "events_lost": bool(state.get("events_lost")),
    }


def _earlier_attempts(path: Path) -> list[dict]:
    """The attempts an existing report for this run already lists (a resumed run)."""
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("attempts", []))
    except FileNotFoundError:
        return []
    except ValueError:
        return []  # A damaged earlier report: this attempt's report replaces it.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
