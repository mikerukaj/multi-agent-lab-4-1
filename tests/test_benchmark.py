"""Offline tests for the benchmark set and runner.

See specs/002-qa-feedback-loop-mcp/contracts/benchmark.md.

A fake ``run_team`` and a fake agent builder stand in for live team runs, so these
tests need no API key and no Docker.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.run_benchmark import (
    BENCHMARK_FILE,
    baseline_lines,
    cost_totals,
    fingerprint,
    load_benchmark,
    main,
    record_for,
    score,
)
from tests.team_fakes import scripted_test_run

IDS = [
    "temp-convert", "word-count", "roman-numerals", "stats", "bank-account",
    "todo-cli", "csv-summary", "password-strength", "matrix", "rate-limiter",
]


def final_state(status: str = "completed", runs=(), end_reason: str = "complete") -> dict:
    """A minimal final ``TeamState`` with one iteration per test run in ``runs``."""
    iterations = [
        {"number": n, "test_run": run, "findings": [], "verdict": "complete"}
        for n, run in enumerate(runs, start=1)
    ]
    return {"run_id": "r", "status": status, "end_reason": end_reason, "error": None,
            "iterations": iterations, "usage": {"input_tokens": 10, "output_tokens": 5},
            "cost_usd": 0.12}


def record(succeeded: bool, first_failed, duration: float = 60.0) -> dict:
    return {"succeeded": succeeded, "first_draft_failed": first_failed,
            "duration_seconds": duration}


class LoadTests(unittest.TestCase):
    def test_the_real_file_has_the_ten_contract_requirements(self) -> None:
        data = load_benchmark()
        self.assertEqual(data["version"], 1)
        self.assertEqual([item["id"] for item in data["requirements"]], IDS)
        self.assertTrue(all(item["requirement"].strip() for item in data["requirements"]))

    def test_invalid_files_are_refused(self) -> None:
        good = json.loads(BENCHMARK_FILE.read_text(encoding="utf-8"))
        items = good["requirements"]
        cases = {
            "nine": {**good, "requirements": items[:9]},
            "eleven": {**good, "requirements": items + [{**items[0], "id": "extra"}]},
            "duplicate": {**good, "requirements": items[:9] + [items[0]]},
            "bad id": {**good, "requirements": items[:9] + [{**items[9], "id": "Rate_Limiter"}]},
            "empty": {**good, "requirements": items[:9] + [{**items[9], "requirement": " "}]},
            "no version": {"requirements": items},
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, data in cases.items():
                path = Path(tmp) / f"{name}.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(case=name), self.assertRaises(ValueError):
                    load_benchmark(path)


class ScoreTests(unittest.TestCase):
    def test_sc_102_needs_eight_of_ten(self) -> None:
        eight = [record(True, False)] * 8 + [record(False, True)] * 2
        seven = [record(True, False)] * 7 + [record(False, True)] * 3
        self.assertTrue(score(eight, partial=False)["sc_102"]["met"])
        self.assertEqual(score(eight, partial=False)["sc_102"]["value"], "8/10")
        self.assertFalse(score(seven, partial=False)["sc_102"]["met"])

    def test_sc_103_needs_seventy_percent_of_first_draft_failures(self) -> None:
        seven = [record(True, True)] * 7 + [record(False, True)] * 3
        six = [record(True, True)] * 6 + [record(False, True)] * 4
        self.assertTrue(score(seven, partial=False)["sc_103"]["met"])
        self.assertFalse(score(six, partial=False)["sc_103"]["met"])
        none_failed = score([record(True, False)] * 10, partial=False)["sc_103"]
        self.assertEqual(none_failed, {"value": "n/a", "met": True})

    def test_partial_runs_are_not_evaluated(self) -> None:
        summary = score([record(True, False)], partial=True)
        self.assertEqual(summary["sc_102"]["value"], "not evaluated (partial run)")
        self.assertFalse(summary["sc_103"]["met"])


class RecordTests(unittest.TestCase):
    entry = {"id": "stats", "title": "Stats", "requirement": "..."}

    def test_first_draft_failure_rules(self) -> None:
        never = record_for(self.entry, final_state("failed", runs=[], end_reason="task_failed"),
                           None, 1.0)
        self.assertIsNone(never["first_draft_failed"])
        not_loaded = record_for(self.entry, final_state(runs=[scripted_test_run(loaded=False),
                                                              scripted_test_run()]), None, 1.0)
        self.assertTrue(not_loaded["first_draft_failed"])
        failing = record_for(self.entry, final_state(runs=[scripted_test_run(failed=2),
                                                           scripted_test_run()]), None, 1.0)
        self.assertTrue(failing["first_draft_failed"])
        self.assertTrue(failing["succeeded"])
        self.assertEqual((failing["final_passed"], failing["final_failed"]), (1, 0))
        self.assertEqual(failing["iterations"], 2)

    def test_an_item_that_raised_is_an_error(self) -> None:
        raised = record_for(self.entry, None, "RuntimeError: boom", 2.0)
        self.assertEqual((raised["status"], raised["error"]), ("error", "RuntimeError: boom"))
        self.assertFalse(raised["succeeded"])


class CostTests(unittest.TestCase):
    entry = {"id": "stats", "title": "Stats", "requirement": "..."}

    def test_records_carry_the_cost_by_agent_and_the_report(self) -> None:
        final = {**final_state(runs=[scripted_test_run()]), "cost_usd": 0.42,
                 "cost_report_path": "docs/cost_reports/r.json",
                 "agent_usage": {"pm": {"cost_usd": 0.3}, "coder": {"cost_usd": 0.1},
                                 "qa": {"cost_usd": 0.02}}}
        item = record_for(self.entry, final, None, 10.0)
        self.assertEqual(item["cost_usd"], 0.42)
        self.assertEqual(item["cost_by_agent"], {"pm": 0.3, "coder": 0.1, "qa": 0.02})
        self.assertEqual(item["cost_report"], "docs/cost_reports/r.json")

    def test_totals_mean_and_median_duration(self) -> None:
        records = [{"cost_usd": 0.2, "duration_seconds": 100},
                   {"cost_usd": 0.4, "duration_seconds": 300},
                   {"cost_usd": None, "duration_seconds": 200}]
        self.assertEqual(cost_totals(records), {"items_costed": 2, "total_cost_usd": 0.6,
                                                "mean_cost_usd": 0.3,
                                                "median_duration_seconds": 200})

    def test_a_pre_feature_baseline_is_priced_from_its_tokens(self) -> None:
        # The T003 baseline format: tokens only, priced uncached on Sonnet ($2 in, $10 out).
        baseline = {"records": [
            {"input_tokens": 100_000, "output_tokens": 10_000, "duration_seconds": 200},
            {"input_tokens": 200_000, "output_tokens": 20_000, "duration_seconds": 240},
        ]}
        self.assertAlmostEqual(cost_totals(baseline["records"])["mean_cost_usd"], 0.45)
        now = {"items_costed": 2, "total_cost_usd": 0.54, "mean_cost_usd": 0.27,
               "median_duration_seconds": 210}
        self.assertEqual(baseline_lines(now, baseline), [
            "cost per item: $0.450 → $0.270 (-40%)",
            "median duration: 220 s → 210 s (-10 s)",
        ])


class FakeRuns:
    """Scripted ``run_team`` and agent builder; records what each item was given."""

    def __init__(self, outcomes: dict) -> None:
        self.outcomes = outcomes
        self.requirements: list[str] = []
        self.chroma_paths: list[Path] = []
        self.max_iterations: list[int] = []

    def make_agents(self, chroma_path: Path):
        self.chroma_paths.append(chroma_path)
        return "pm", "coder", "qa"

    def run_team(
        self, requirement, pm, coder, workspace_root, on_update, *, qa, max_iterations,
        services=None,
    ):
        self.requirements.append(requirement)
        self.max_iterations.append(max_iterations)
        outcome = self.outcomes.get(len(self.requirements), "pass")
        if isinstance(outcome, Exception):
            raise outcome
        runs = {"pass": [scripted_test_run()],
                "recover": [scripted_test_run(failed=1), scripted_test_run()],
                "fail": [scripted_test_run(failed=1)]}[outcome]
        status = "failed" if outcome == "fail" else "completed"
        return final_state(status, runs, "iteration_limit" if outcome == "fail" else "complete")


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)

    def run_main(self, argv: list[str], fake: FakeRuns) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(argv + ["--out", str(self.out)], run_team=fake.run_team,
                        make_agents=fake.make_agents)
        return code, stderr.getvalue()

    def results(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.out.glob("*.json"))]

    def test_full_run_writes_results_and_exits_0_when_both_criteria_are_met(self) -> None:
        fake = FakeRuns({1: "recover", 2: RuntimeError("Docker went away")})
        code, err = self.run_main([], fake)
        self.assertEqual(code, 0)
        self.assertIn("Running 10 benchmark requirements (live model calls)", err)
        result = self.results()[0]
        self.assertEqual(result["fingerprint"], fingerprint())
        self.assertEqual((result["benchmark_version"], result["partial"]), (1, False))
        self.assertEqual(len(result["records"]), 10)
        self.assertEqual(result["records"][1]["status"], "error")
        self.assertIn("Docker went away", result["records"][1]["error"])
        self.assertEqual(result["summary"]["sc_102"], {"value": "9/10", "met": True})
        self.assertEqual(result["summary"]["sc_103"]["met"], True)
        self.assertEqual(len(set(fake.chroma_paths)), 10)
        self.assertEqual(fake.max_iterations, [5] * 10)

    def test_failing_full_run_exits_1(self) -> None:
        fake = FakeRuns({n: "fail" for n in range(1, 4)})
        code, _ = self.run_main([], fake)
        self.assertEqual(code, 1)
        self.assertFalse(self.results()[0]["summary"]["sc_102"]["met"])

    def test_partial_run(self) -> None:
        fake = FakeRuns({})
        code, _ = self.run_main(["--only", "temp-convert,stats", "--max-iterations", "3"], fake)
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.requirements), 2)
        self.assertEqual(fake.max_iterations, [3, 3])
        result = self.results()[0]
        self.assertTrue(result["partial"])
        self.assertEqual(result["max_iterations"], 3)
        self.assertEqual(result["summary"]["sc_102"]["value"], "not evaluated (partial run)")

    def test_baseline_comparison_is_printed(self) -> None:
        baseline = self.out / "baseline" / "base.json"
        baseline.parent.mkdir()
        baseline.write_text(json.dumps({"records": [
            {"input_tokens": 100_000, "output_tokens": 10_000, "duration_seconds": 200}
        ]}), encoding="utf-8")
        _, err = self.run_main(["--only", "stats", "--baseline", str(baseline)], FakeRuns({}))
        self.assertIn("cost per item: $0.300 → $0.120 (-60%)", err)
        self.assertIn("median duration: 200 s →", err)
        self.assertEqual(self.run_main(["--baseline", str(self.out / "missing.json")],
                                       FakeRuns({}))[0], 2)

    def test_usage_errors_exit_2(self) -> None:
        self.assertEqual(self.run_main(["--only", "no-such-id"], FakeRuns({}))[0], 2)
        self.assertEqual(self.run_main(["--max-iterations", "11"], FakeRuns({}))[0], 2)

    def test_changed_benchmark_is_reported_as_not_comparable(self) -> None:
        earlier = self.out / "20260101T000000Z.json"
        earlier.write_text(json.dumps({"fingerprint": "something-else"}), encoding="utf-8")
        _, err = self.run_main(["--only", "stats"], FakeRuns({}))
        self.assertIn(f"benchmark set changed since {earlier.name}", err)
        self.assertIn("not comparable", err)


if __name__ == "__main__":
    unittest.main()
