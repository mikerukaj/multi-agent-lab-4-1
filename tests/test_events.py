"""Tests for the run event record and summary (observability/events.py, FR-214-FR-221)."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from agents.pm_agent import PMAgent, PMConfig
from observability.events import (
    EventLog,
    progress_line,
    read_events,
    severity_from_env,
    summarise,
)
from observability.redaction import MARKER, Redactor
from orchestration.services import RunServices
from orchestration.team import format_update, run_team
from tests import TEST_ENV
from tests.schema_check import contract_path, load_fixture_schema, schema_errors
from tests.team_fakes import (
    FakeCoder,
    FakePMClient,
    FakeQA,
    pm_response,
    qa_result,
    scripted_finding,
    scripted_test_run,
    spec_payload,
)

SCHEMA_NAME = "run-event.schema.json"
SECRET = "sk-ant-api03-eventsecret0123456789"


def assert_valid_event(test: unittest.TestCase, event: dict) -> None:
    schema = load_fixture_schema(SCHEMA_NAME)
    test.assertEqual(schema_errors(event, schema, schema), [], event)


class SchemaCopyTests(unittest.TestCase):
    def test_the_fixture_matches_the_contract(self) -> None:
        contract = contract_path("003-production-hardening", SCHEMA_NAME)
        if not contract.exists():
            self.skipTest("specs/ is not present")
        self.assertEqual(json.loads(contract.read_text(encoding="utf-8")),
                         load_fixture_schema(SCHEMA_NAME))


class EventLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / ".runs" / "r1" / "events.jsonl"

    def log(self, **kwargs) -> EventLog:
        log = EventLog(self.path, "r1", **kwargs)
        self.addCleanup(log.close)
        return log

    def test_every_line_is_a_valid_event_written_as_it_happens(self) -> None:
        log = self.log()
        log.emit("run_start")
        log.emit("model_call", agent="pm", stage="plan", model="m",
                 tokens={"input_tokens": 3, "output_tokens": 1}, duration_ms=5,
                 operation_ms=4, served_from="live", cost_usd=None)
        lines = self.path.read_text(encoding="utf-8").splitlines()  # The log is still open.
        self.assertEqual(len(lines), 2)
        for line in lines:
            assert_valid_event(self, json.loads(line))
        self.assertEqual(json.loads(lines[1])["agent"], "pm")

    def test_the_threshold_drops_lower_severities_but_keeps_errors_and_run_end(self) -> None:
        log = self.log(min_severity="warning")
        log.emit("model_call")
        log.emit("retry", severity="warning")
        log.emit("error", severity="error")
        log.emit("run_end", summary={})
        written = [event["event"] for event in read_events(self.path)]
        self.assertEqual(written, ["retry", "error", "run_end"])
        self.assertEqual(len(log.events), 4)  # The summary still sees everything.

    def test_a_write_failure_marks_events_lost_and_never_raises(self) -> None:
        blocked = Path(self._tmp.name) / "file"
        blocked.write_text("x", encoding="utf-8")
        log = EventLog(blocked / "events.jsonl", "r1")
        log.emit("run_start")
        self.assertTrue(log.events_lost)
        self.assertEqual(len(log.events), 1)

    def test_secrets_are_redacted_before_writing(self) -> None:
        log = self.log(redactor=Redactor.from_env({"ANTHROPIC_API_KEY": SECRET}))
        log.emit("error", severity="error", message=f"failed with key {SECRET}")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        self.assertIn(MARKER, text)

    def test_the_context_attributes_events_on_its_own_thread(self) -> None:
        log = self.log()
        seen = {}
        with log.context(agent="coder", stage="build", task_id="T2"):
            log.record({"event": "retry", "severity": "warning", "dependency": "anthropic",
                        "attempt": 1})

            def other_thread():
                seen["event"] = log.emit("warning", severity="warning")
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join()
        retry = log.events[0]
        self.assertEqual((retry["agent"], retry["stage"], retry["task_id"]),
                         ("coder", "build", "T2"))
        self.assertEqual(retry["details"]["dependency"], "anthropic")
        assert_valid_event(self, retry)
        self.assertEqual((seen["event"]["agent"], seen["event"]["stage"]), ("team", "team"))

    def test_log_level_is_read_and_checked(self) -> None:
        self.assertEqual(severity_from_env({}), "info")
        self.assertEqual(severity_from_env({"LOG_LEVEL": "WARNING"}), "warning")
        with self.assertRaisesRegex(ValueError, "LOG_LEVEL"):
            severity_from_env({"LOG_LEVEL": "loud"})


def event(kind: str, agent: str, stage: str, severity: str = "info", **details) -> dict:
    return {"ts": "t", "run_id": "r", "agent": agent, "stage": stage, "event": kind,
            "severity": severity, "outcome": "ok", "details": details}


class SummaryTests(unittest.TestCase):
    def test_totals_per_agent_per_stage_and_for_the_run(self) -> None:
        events = [
            event("model_call", "pm", "plan", tokens={"input_tokens": 10, "output_tokens": 5},
                  served_from="live", cost_usd=0.5),
            event("stage_end", "pm", "plan", duration_ms=100),
            event("model_call", "coder", "build",
                  tokens={"input_tokens": 1, "output_tokens": 2,
                          "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3},
                  served_from="live", cost_usd=0.25),
            event("model_call", "coder", "build", tokens={}, served_from="cache"),
            event("retry", "coder", "build", severity="warning"),
            event("restart", "coder", "build", severity="warning"),
            event("stage_end", "coder", "build", duration_ms=40),
            event("error", "qa", "qa_review", severity="error"),
            event("resume", "team", "team"),
        ]
        summary = summarise(events, events_lost=True)
        self.assertEqual(summary["per_agent"]["pm"]["prompt_tokens"], 10)
        self.assertEqual(summary["per_agent"]["pm"]["duration_ms"], 100)
        coder = summary["per_agent"]["coder"]
        self.assertEqual((coder["model_calls"], coder["prompt_tokens"],
                          coder["completion_tokens"], coder["cache_read_tokens"]),
                         (1, 11, 2, 7))
        self.assertEqual((coder["retries"], coder["restarts"]), (1, 1))
        self.assertEqual(summary["per_stage"]["qa_review"]["errors"], 1)
        totals = summary["totals"]
        self.assertEqual((totals["model_calls"], totals["cost_usd"], totals["duration_ms"]),
                         (2, 0.75, 140))
        self.assertTrue(summary["events_lost"])
        self.assertEqual(summary["attempts"], 2)

    def test_an_unknown_cost_makes_the_total_unknown(self) -> None:
        events = [event("model_call", "pm", "plan", tokens={}, cost_usd=None),
                  event("model_call", "pm", "plan", tokens={}, cost_usd=0.1)]
        self.assertIsNone(summarise(events)["totals"]["cost_usd"])


class ProgressLineTests(unittest.TestCase):
    """The CLI's stderr lines come from the events, unchanged (FR-220)."""

    def test_event_lines_match_the_progress_lines_of_every_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            services = RunServices.create(env=TEST_ENV, response_cache=None)
            expected, current = [], [None]

            def on_update(node: str, update: dict) -> None:
                if node == "dispatch":
                    current[0] = update["handoff"]
                line = format_update(node, update, current[0])
                if line:
                    expected.append(line)

            pm = PMAgent(PMConfig(), client=FakePMClient([pm_response(spec_payload(2))]))
            qa = FakeQA([qa_result("fix_needed", findings=[scripted_finding("F1-1")],
                                   test_run=scripted_test_run(failed=1)),
                         qa_result("complete")])
            run_team("Build a converter", pm, FakeCoder(), Path(tmp), on_update, qa=qa,
                     services=services)
        lines = [line for line in map(progress_line, services.events.events) if line]
        self.assertEqual(lines, expected)
        prefixes = [line.split(" ", 1)[0] for text in lines for line in text.splitlines()]
        for prefix in ("[pm]", "[handoff]", "[coder]", "[reflect]", "[a2a]", "[qa]",
                       "[fix]", "[finish]"):
            self.assertIn(prefix, prefixes)


if __name__ == "__main__":
    unittest.main()
