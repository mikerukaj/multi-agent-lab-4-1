"""Tests for prices, the usage ledger, the run budget and the cost report (FR-222-FR-224)."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from cost.ledger import BudgetExceeded, RunBudget, UsageLedger
from cost.pricing import DEFAULT_PRICE_TABLE, cost_of, is_priced, load_price_table
from cost.report import write_cost_report
from observability.redaction import MARKER, Redactor
from orchestration.services import RunServices
from orchestration.team import initial_team_state, run_team
from tests import TEST_ENV
from tests.fakes.recording_tracer import recording_tracer
from tests.fakes.team_script import REQUIREMENT, TeamModel, offline_coder, scripted_team
from tests.schema_check import contract_path, load_fixture_schema, schema_errors

TABLE = load_price_table(DEFAULT_PRICE_TABLE)
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"
SCHEMA_NAME = "cost-report.schema.json"
RUN_ID = "20261020-120000-abcdef"
SECRET = "sk-ant-api03-costreportsecret0123456"


def usage(input_tokens=0, output_tokens=0, cache_write=0, cache_read=0) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_write, "cache_read_input_tokens": cache_read}


class PricingTests(unittest.TestCase):
    def test_base_rates_per_million_tokens(self) -> None:
        self.assertAlmostEqual(cost_of(usage(1_000_000, 1_000_000), SONNET, "5m", TABLE), 12.0)
        # Haiku below its 100K-token long-context threshold: $0.10 in, $0.50 out per MTok.
        self.assertAlmostEqual(cost_of(usage(100_000, 100_000), HAIKU, "5m", TABLE), 0.06)

    def test_cache_reads_and_writes(self) -> None:
        self.assertAlmostEqual(cost_of(usage(cache_read=1_000_000), SONNET, "5m", TABLE), 0.10)
        self.assertAlmostEqual(cost_of(usage(cache_write=1_000_000), SONNET, "5m", TABLE), 2.5)
        self.assertAlmostEqual(cost_of(usage(cache_write=1_000_000), SONNET, "1h", TABLE), 4.0)
        self.assertAlmostEqual(cost_of(usage(cache_read=100_000), HAIKU, "5m", TABLE), 0.001)

    def test_the_reported_5m_and_1h_split_wins_over_the_ttl(self) -> None:
        split = {"ephemeral_5m_input_tokens": 500_000, "ephemeral_1h_input_tokens": 500_000}
        self.assertAlmostEqual(
            cost_of(usage(cache_write=1_000_000), SONNET, "5m", TABLE, split), 1.25 + 2.0
        )

    def test_haiku_prompts_over_100k_tokens_use_the_long_context_rates(self) -> None:
        short = cost_of(usage(90_000, 1000, cache_read=10_000), HAIKU, "5m", TABLE)
        long = cost_of(usage(90_000, 1000, cache_read=10_001), HAIKU, "5m", TABLE)
        self.assertAlmostEqual(short, (90_000 * 0.10 + 1000 * 0.50 + 10_000 * 0.01) / 1e6)
        self.assertAlmostEqual(long, (90_000 * 0.50 + 1000 * 2.50 + 10_001 * 0.05) / 1e6)

    def test_an_unknown_model_has_no_price(self) -> None:
        self.assertIsNone(cost_of(usage(10, 10), "claude-mystery-1", "5m", TABLE))
        self.assertFalse(is_priced("claude-mystery-1", TABLE))
        self.assertTrue(is_priced(HAIKU, TABLE))

    def test_a_table_without_a_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            path.write_text(json.dumps({"models": {}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no version"):
                load_price_table(path)

    def test_one_calls_cost_is_within_2_percent_of_a_hand_computation(self) -> None:
        # SC-204: 12,345 uncached + 2,000 written + 50,000 read in, 3,210 out, on Sonnet.
        computed = cost_of(usage(12_345, 3_210, 2_000, 50_000), SONNET, "5m", TABLE)
        by_hand = (12_345 * 2.0 + 3_210 * 10.0 + 2_000 * 2.5 + 50_000 * 0.10) / 1_000_000
        self.assertLessEqual(abs(computed - by_hand) / by_hand, 0.02)


class LedgerTests(unittest.TestCase):
    def test_accumulation_from_several_threads(self) -> None:
        ledger = UsageLedger()

        def record() -> None:
            for _ in range(100):
                ledger.record("coder", HAIKU, usage(10, 5, 2, 3), 0.001)
        threads = [threading.Thread(target=record) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        coder = ledger.snapshot()["coder"]
        self.assertEqual(coder["model_calls"], 400)
        self.assertEqual(coder["prompt_tokens"], 400 * 15)
        self.assertEqual((coder["completion_tokens"], coder["cache_read_tokens"],
                          coder["cache_write_tokens"]), (2000, 1200, 800))
        self.assertAlmostEqual(coder["cost_usd"], 0.4)

    def test_an_unpriced_call_makes_the_agents_cost_unknown(self) -> None:
        ledger = UsageLedger()
        ledger.record("pm", SONNET, usage(10), 0.01)
        ledger.record("pm", "claude-mystery-1", usage(10), None)
        ledger.record("pm", SONNET, usage(10), 0.01)
        self.assertIsNone(ledger.snapshot()["pm"]["cost_usd"])
        self.assertIsNone(ledger.total_cost())
        self.assertEqual(ledger.snapshot()["pm"]["models"], [SONNET, "claude-mystery-1"])

    def test_a_fallback_model_is_recorded_and_priced_at_its_own_rate(self) -> None:
        ledger = UsageLedger()
        fallback = cost_of(usage(1000, 100), HAIKU, "5m", TABLE)
        ledger.record("pm", HAIKU, usage(1000, 100), fallback)
        self.assertEqual(ledger.snapshot()["pm"]["models"], [HAIKU])
        self.assertAlmostEqual(ledger.snapshot()["pm"]["cost_usd"], fallback)

    def test_cached_replays_are_counted_and_cost_nothing(self) -> None:
        ledger = UsageLedger()
        ledger.record("qa", HAIKU, usage(), 0.0, served_from="cache")
        qa = ledger.snapshot()["qa"]
        self.assertEqual((qa["cached_replays"], qa["model_calls"], qa["cost_usd"]), (1, 0, 0.0))

    def test_a_snapshot_loads_into_a_new_ledger(self) -> None:
        ledger = UsageLedger()
        ledger.record("pm", SONNET, usage(10, 5), 0.5)
        resumed = UsageLedger()
        resumed.load(ledger.snapshot())
        resumed.record("pm", SONNET, usage(10, 5), 0.25)
        self.assertEqual(resumed.snapshot()["pm"]["model_calls"], 2)
        self.assertAlmostEqual(resumed.total_cost(), 0.75)


class BudgetTests(unittest.TestCase):
    def test_no_cap_by_default(self) -> None:
        budget = RunBudget()
        budget.add(1000.0)
        budget.check()

    def test_spend_reaching_the_cap_stops_new_calls(self) -> None:
        budget = RunBudget(1.0)
        budget.add(0.6)
        budget.check()
        budget.add(0.4)
        with self.assertRaisesRegex(BudgetExceeded, "reached the run budget"):
            budget.check()

    def test_a_cap_with_an_unpriced_model_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "claude-mystery-1 has no price"):
            RunBudget(5.0, unpriced_models=["claude-mystery-1"])
        RunBudget(None, unpriced_models=["claude-mystery-1"])
        with self.assertRaisesRegex(ValueError, "greater than 0"):
            RunBudget(0)


class CostReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "cost_reports"
        self.state = {**initial_team_state(f"Build it with key {SECRET}", RUN_ID,
                                           Path("/w")), "status": "completed",
                      "end_reason": "complete"}
        self.ledger = UsageLedger()
        self.ledger.record("pm", SONNET, usage(1000, 200), cost_of(usage(1000, 200), SONNET,
                                                                     "5m", TABLE))
        self.ledger.record("coder", HAIKU, usage(5000, 900, 300, 2000),
                           cost_of(usage(5000, 900, 300, 2000), HAIKU, "5m", TABLE))
        self.redact = Redactor.from_env({"ANTHROPIC_API_KEY": SECRET}).redact
        self.models = {"pm": SONNET, "coder": HAIKU, "qa": HAIKU}

    def write(self, state=None, directory=None, errors=None):
        return write_cost_report(directory or self.directory, state or self.state,
                                 self.ledger.snapshot(), TABLE["version"], "5m",
                                 "2026-10-20T12:00:00+00:00", self.models, self.redact,
                                 errors.append if errors is not None else None)

    def report(self) -> dict:
        return json.loads((self.directory / f"{RUN_ID}.json").read_text(encoding="utf-8"))

    def test_the_report_matches_the_schema(self) -> None:
        path = self.write()
        self.assertEqual(path, self.directory / f"{RUN_ID}.json")
        schema = load_fixture_schema(SCHEMA_NAME)
        self.assertEqual(schema_errors(self.report(), schema, schema), [])
        self.assertEqual(list(self.directory.iterdir()), [path])  # No temp file left.

    def test_per_agent_costs_add_up_to_the_total(self) -> None:
        self.write()
        report = self.report()
        agents = sum(entry["cost_usd"] for entry in report["agents"].values())
        self.assertLessEqual(abs(agents - report["total"]["cost_usd"]), 0.000001)  # SC-215

    def test_the_requirement_summary_is_redacted(self) -> None:
        self.write()
        self.assertNotIn(SECRET, json.dumps(self.report()))
        self.assertIn(MARKER, self.report()["requirement_summary"])

    def test_a_resumed_run_rewrites_it_with_two_attempts_and_cumulative_totals(self) -> None:
        self.write({**self.state, "status": "failed", "end_reason": "interrupted"})
        first_total = self.report()["total"]["cost_usd"]
        self.ledger.record("qa", HAIKU, usage(3000, 400), cost_of(usage(3000, 400), HAIKU,
                                                                   "5m", TABLE))
        self.write()
        report = self.report()
        self.assertEqual([a["end_reason"] for a in report["attempts"]],
                         ["interrupted", "complete"])
        self.assertGreater(report["total"]["cost_usd"], first_total)
        self.assertAlmostEqual(sum(a["cost_usd"] for a in report["attempts"]),
                               report["total"]["cost_usd"], places=6)
        schema = load_fixture_schema(SCHEMA_NAME)
        self.assertEqual(schema_errors(report, schema, schema), [])

    def test_a_failed_write_is_reported_and_returns_none(self) -> None:
        blocked = Path(self._tmp.name) / "file"
        blocked.write_text("x", encoding="utf-8")
        errors: list[str] = []
        self.assertIsNone(self.write(directory=blocked / "reports", errors=errors))
        self.assertEqual(len(errors), 1)
        self.assertIn("cost report not written", errors[0])

    def test_the_fixture_matches_the_contract(self) -> None:
        contract = contract_path("003-production-hardening", SCHEMA_NAME)
        if not contract.exists():
            self.skipTest("specs/ is not present")
        self.assertEqual(json.loads(contract.read_text(encoding="utf-8")),
                         load_fixture_schema(SCHEMA_NAME))



class TeamAccountingTests(unittest.TestCase):
    """The ledger, the agent spans and the cost report agree (T069, SC-214 offline)."""

    def test_spans_ledger_and_cost_report_agree(self) -> None:
        tracer, recorder = recording_tracer()
        with tempfile.TemporaryDirectory() as tmp, offline_coder():
            root = Path(tmp)
            services = RunServices.create(env={**TEST_ENV, "COST_REPORT_DIR": tmp},
                                          response_cache=None, tracer=tracer,
                                          sleep=lambda delay: None)
            services.connect_tracer()
            pm, coder, qa = scripted_team(root, services, TeamModel(), TeamModel(),
                                          TeamModel(bug_in_first_review=True))
            final = run_team(REQUIREMENT, pm, coder, root, qa=qa, services=services)
            report = json.loads(Path(final["cost_report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(final["end_reason"], "complete")
        spans = recorder.named("agent:")
        for agent in ("pm", "coder", "qa"):
            mine = [span for span in spans if span["metadata"]["agent_name"] == agent]
            self.assertTrue(mine, agent)
            self.assertEqual(final["agent_usage"][agent]["tool_calls"],
                             sum(span["metadata"]["tool_calls"] for span in mine), agent)
        self.assertGreater(final["agent_usage"]["coder"]["tool_calls"], 0)
        span_tokens = sum(span["metadata"]["token_count"] for span in spans)
        total = report["total"]
        self.assertEqual(span_tokens, total["prompt_tokens"] + total["completion_tokens"])
        self.assertEqual(final["cost_usd"], report["total"]["cost_usd"])
        self.assertGreater(final["cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()
