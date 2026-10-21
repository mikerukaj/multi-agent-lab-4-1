"""Runs the team on the fixed 10-requirement benchmark and scores SC-102 and SC-103.

Each requirement is one full, live team run with fresh agents and a fresh Coder
memory store, so lessons do not carry from one item to the next. The results file
records every item and a summary, plus a fingerprint of the benchmark file so later
evaluations can tell whether they are comparable.
See specs/002-qa-feedback-loop-mcp/contracts/benchmark.md.

Production hardening (003) adds the cost of every item and in total, and a comparison
with a baseline results file (SC-205, SC-205a).

Usage:
    python -m benchmarks.run_benchmark [--only ID[,ID...]] [--max-iterations N] [--out DIR]
        [--baseline FILE]
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from agents.coder_agent import CoderAgent, CoderConfig
from agents.pm_agent import PMAgent, PMConfig
from agents.llm import LARGE_MODEL
from agents.qa_agent import QAAgent, QAConfig
from cost.pricing import DEFAULT_PRICE_TABLE, load_price_table
from orchestration.services import RunServices
from orchestration.team import check_max_iterations, max_iterations_from_env, run_team
from tools.python_exec import SandboxUnavailableError, ensure_sandbox

BENCHMARK_FILE = Path(__file__).resolve().parent / "requirements.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
BENCHMARK_SIZE = 10
ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SC_102_SUCCESSES = 8
SC_103_SHARE = 0.70
SC_004_SECONDS = 600
NOT_EVALUATED = {"value": "not evaluated (partial run)", "met": False}


def load_benchmark(path: Path = BENCHMARK_FILE) -> dict:
    """Read and check the benchmark file (contract §1).

    Raises:
        ValueError: if the file breaks any rule of the format.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("version"), int) or data["version"] < 1:
        raise ValueError("benchmark 'version' must be an integer of at least 1")
    items = data.get("requirements")
    if not isinstance(items, list) or len(items) != BENCHMARK_SIZE:
        raise ValueError(f"the benchmark must have exactly {BENCHMARK_SIZE} requirements")
    ids = [item.get("id") for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("benchmark ids must be unique")
    for item in items:
        if not isinstance(item.get("id"), str) or not ID_PATTERN.match(item["id"]):
            raise ValueError(f"benchmark id {item.get('id')!r} is not lowercase kebab-case")
        for key in ("title", "requirement"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"benchmark {item['id']} needs a non-empty {key}")
    return data


def fingerprint(path: Path = BENCHMARK_FILE) -> str:
    """Return the sha256 of the benchmark file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_for(entry: dict, final: dict | None, error: str | None, duration: float) -> dict:
    """Summarise one item's run (contract §4)."""
    iterations = final["iterations"] if final else []
    first = iterations[0]["test_run"] if iterations else None
    last = iterations[-1]["test_run"] if iterations else None
    final_failed = last["failed"] + last["errors"] if last else 0
    status = final["status"] if final else "error"
    usage = final["usage"] if final else {"input_tokens": 0, "output_tokens": 0}
    return {
        "id": entry["id"],
        "run_id": final["run_id"] if final else None,
        "status": status,
        "end_reason": final["end_reason"] if final else None,
        "iterations": len(iterations),
        "first_draft_failed": None if first is None else _did_not_pass(first),
        "final_passed": last["passed"] if last else 0,
        "final_failed": final_failed,
        "succeeded": status == "completed" and final_failed == 0,
        "duration_seconds": round(duration, 3),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cost_usd": final.get("cost_usd") if final else None,
        "cost_by_agent": {
            agent: entry["cost_usd"] for agent, entry in final.get("agent_usage", {}).items()
        } if final else {},
        "cost_report": final.get("cost_report_path") if final else None,
        "error": error or (final.get("error") if final else None),
    }


def item_cost(record: dict) -> float | None:
    """An item's cost; a pre-003 record (tokens only) is priced uncached on the large model,
    as the baseline was (T003)."""
    if record.get("cost_usd") is not None:
        return record["cost_usd"]
    if "cost_usd" in record:
        return None  # Measured, but a serving model had no price.
    prices = load_price_table(DEFAULT_PRICE_TABLE)["models"][LARGE_MODEL]
    return round((record["input_tokens"] * prices["input"]
                  + record["output_tokens"] * prices["output"]) / 1_000_000, 6)


def cost_totals(records: list[dict]) -> dict:
    """Total and mean cost per item, and the median duration (contracts/cli.md).

    Items whose cost is unknown (a crashed run, or an unpriced model) are left out of
    the cost totals; ``items_costed`` says how many were counted.
    """
    costs = [cost for cost in map(item_cost, records) if cost is not None]
    total = round(sum(costs), 6) if costs else None
    return {
        "items_costed": len(costs),
        "total_cost_usd": total,
        "mean_cost_usd": round(total / len(costs), 6) if costs else None,
        "median_duration_seconds": (
            statistics.median(record["duration_seconds"] for record in records)
            if records else None
        ),
    }


def baseline_lines(totals: dict, baseline: dict) -> list[str]:
    """Compare this run with a baseline results file (SC-205 cost, SC-205a duration)."""
    before = cost_totals(baseline["records"])
    lines = []
    if before["mean_cost_usd"] and totals["mean_cost_usd"] is not None:
        change = (totals["mean_cost_usd"] - before["mean_cost_usd"]) / before["mean_cost_usd"]
        lines.append(f"cost per item: ${before['mean_cost_usd']:.3f} → "
                     f"${totals['mean_cost_usd']:.3f} ({change:+.0%})")
    if before["median_duration_seconds"] and totals["median_duration_seconds"] is not None:
        delta = totals["median_duration_seconds"] - before["median_duration_seconds"]
        lines.append(f"median duration: {before['median_duration_seconds']:.0f} s → "
                     f"{totals['median_duration_seconds']:.0f} s ({delta:+.0f} s)")
    return lines


def _did_not_pass(test_run: dict) -> bool:
    return not test_run["loaded"] or test_run["failed"] + test_run["errors"] > 0


def score(records: list[dict], partial: bool) -> dict:
    """Score SC-102 and SC-103, and report 001's SC-004 (contract §5)."""
    quick = sum(1 for item in records if item["duration_seconds"] < SC_004_SECONDS)
    sc_004 = {
        "value": f"{quick}/{len(records)} under {SC_004_SECONDS} s",
        "met": quick >= SC_102_SUCCESSES,
    }
    if partial:
        return {"sc_102": NOT_EVALUATED, "sc_103": NOT_EVALUATED, "sc_004_001": sc_004}
    succeeded = sum(1 for item in records if item["succeeded"])
    first_failed = [item for item in records if item["first_draft_failed"] is True]
    if first_failed:
        recovered = sum(1 for item in first_failed if item["succeeded"])
        share = recovered / len(first_failed)
        sc_103 = {
            "value": f"{recovered}/{len(first_failed)} = {share:.2f}",
            "met": share >= SC_103_SHARE,
        }
    else:
        sc_103 = {"value": "n/a", "met": True}
    return {
        "sc_102": {"value": f"{succeeded}/{len(records)}", "met": succeeded >= SC_102_SUCCESSES},
        "sc_103": sc_103,
        "sc_004_001": sc_004,
    }


def build_agents(chroma_path: Path):
    """Build fresh PM, Coder and QA agents from the environment (the Coder with fresh memory)."""
    coder_config = replace(CoderConfig.from_env(), chroma_path=chroma_path)
    return PMAgent(PMConfig.from_env()), CoderAgent(coder_config), QAAgent(QAConfig.from_env())


def main(argv: list[str] | None = None, run_team=run_team, make_agents=build_agents) -> int:
    """Run the benchmark; 0 if a full run meets SC-102 and SC-103, 1 otherwise, 2 for
    usage errors."""
    load_dotenv()
    args = _parse_args(argv)
    try:
        entries, max_iterations = _plan(args)
        baseline = _read_baseline(args.baseline)
        if make_agents is build_agents:
            _preflight()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    partial = len(entries) < BENCHMARK_SIZE
    print(f"Running {len(entries)} benchmark requirements (live model calls)", file=sys.stderr)
    started_at = _now()
    session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    records = [
        _run_item(entry, run_team, make_agents, max_iterations, session) for entry in entries
    ]
    summary = score(records, partial)
    result = {
        "benchmark_version": load_benchmark()["version"],
        "fingerprint": fingerprint(),
        "started_at": started_at,
        "finished_at": _now(),
        "max_iterations": max_iterations,
        "partial": partial,
        "records": records,
        "summary": summary,
        "totals": cost_totals(records),
    }
    _warn_if_not_comparable(args.out, result["fingerprint"])
    path = _write_results(args.out, result)
    _print_summary(records, summary, path)
    for line in baseline_lines(result["totals"], baseline) if baseline else []:
        print(f"  {line}", file=sys.stderr)
    both_met = summary["sc_102"]["met"] and summary["sc_103"]["met"]
    return 0 if not partial and both_met else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.run_benchmark",
        description="Run the team on the fixed benchmark and score SC-102 and SC-103.",
    )
    parser.add_argument("--only", help="comma-separated benchmark ids to run (a partial run)")
    parser.add_argument("--max-iterations", type=int, help="most QA reviews per run (1-10)")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR, help="results folder")
    parser.add_argument("--baseline", type=Path,
                        help="a results file to compare cost and duration against")
    return parser.parse_args(argv)


def _plan(args) -> tuple[list[dict], int]:
    """Pick the entries and the iteration limit; raise ValueError for bad options."""
    entries = load_benchmark()["requirements"]
    if args.only:
        wanted = [item.strip() for item in args.only.split(",") if item.strip()]
        known = {entry["id"] for entry in entries}
        unknown = [item for item in wanted if item not in known]
        if unknown:
            raise ValueError(f"unknown benchmark ids: {', '.join(unknown)}")
        entries = [entry for entry in entries if entry["id"] in wanted]
    if args.max_iterations is not None:
        return entries, check_max_iterations(args.max_iterations)
    return entries, max_iterations_from_env()


def _read_baseline(path: Path | None) -> dict | None:
    """Read a baseline results file.

    Raises:
        ValueError: if it can't be read or has no records.
    """
    if path is None:
        return None
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read the baseline {path}: {error}") from error
    if not baseline.get("records"):
        raise ValueError(f"the baseline {path} has no records")
    return baseline


def _preflight() -> None:
    """Check credentials and Docker once, before any live run."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise ValueError("set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) to run the benchmark")
    try:
        ensure_sandbox()
    except SandboxUnavailableError as error:
        raise ValueError(str(error)) from error


def _run_item(entry: dict, run_team, make_agents, max_iterations: int, session: str) -> dict:
    """Run one benchmark item with fresh agents; an exception is recorded, not raised."""
    chroma = Path(tempfile.mkdtemp(prefix="benchmark-chroma-"))
    started = time.monotonic()
    final, error = None, None
    try:
        pm, coder, qa = make_agents(chroma)
        workspace_root = Path("workspace") / f"benchmark-{session}"
        final = run_team(
            entry["requirement"], pm, coder, workspace_root, None,
            qa=qa, max_iterations=max_iterations, services=RunServices.create(),
        )
    except Exception as exc:  # One broken item must not stop the evaluation.
        error = f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(chroma, ignore_errors=True)
    item = record_for(entry, final, error, time.monotonic() - started)
    outcome = "succeeded" if item["succeeded"] else f"{item['status']} ({item['end_reason']})"
    print(f"[benchmark] {entry['id']}: {outcome}", file=sys.stderr, flush=True)
    return item


def _warn_if_not_comparable(out: Path, current: str) -> None:
    for path in sorted(out.glob("*.json"), reverse=True):
        try:
            earlier = json.loads(path.read_text(encoding="utf-8")).get("fingerprint")
        except (json.JSONDecodeError, OSError):
            continue
        if earlier and earlier != current:
            print(
                f"warning: benchmark set changed since {path.name}; results are not comparable",
                file=sys.stderr,
            )
            return


def _write_results(out: Path, result: dict) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path, n = out / f"{stamp}.json", 1
    while path.exists():
        n += 1
        path = out / f"{stamp}-{n}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


def _print_summary(records: list[dict], summary: dict, path: Path) -> None:
    for item in records:
        print(
            f"  {item['id']:<18} {item['status']:<10} iterations={item['iterations']} "
            f"first_draft_failed={item['first_draft_failed']} {item['duration_seconds']:.0f} s",
            file=sys.stderr,
        )
    for key, value in summary.items():
        print(f"  {key}: {value['value']} ({'met' if value['met'] else 'not met'})",
              file=sys.stderr)
    print(f"Results: {path}", file=sys.stderr)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    sys.exit(main())
