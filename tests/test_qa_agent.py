"""Offline tests for the QA & Debugger Agent (contracts/qa-agent-api.md, research R8–R10, R14).

A scripted fake Anthropic client, a fake test runner and ``FakeFileTools`` stand in
for the model, the Docker sandbox and the MCP server: the QA agent gets mocked code
input and must still produce findings that match the findings schema.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2

from agents import qa_agent
from agents.qa_agent import (
    CONSTITUTION_RULES,
    QAAgent,
    QAConfig,
    interface_summary,
    normalise_findings,
    verdict_for,
)
from tests.fakes import assert_no_live_dependencies
from tests.fakes import scripted_claude as sc
from tests.schema_check import CONTRACT, FIXTURE, assert_matches_findings_schema, load_schema
from orchestration.services import RunServices
from tests.team_fakes import FakeFileTools, scripted_test_run
from tools.mcp_client import McpUnavailableError

BODY_MARKER = "BODY_MARKER_7f3a"
APP_SOURCE = f'''"""Tiny calculator."""

import argparse


def add(a: int, b: int) -> int:
    """Add two numbers."""
    marker = "{BODY_MARKER}"
    return a + b


def setUpModule() -> None:
    assert_no_live_dependencies()


class Calculator:
    """Keeps a running total."""

    def push(self, value: int) -> None:
        """Add a value to the total."""
        self.total = value

    def _hidden(self) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("numbers", nargs="+", type=int)
    print(add(*parser.parse_args().numbers))


if __name__ == "__main__":
    main()
'''
SPEC = {
    "name": "Calculator",
    "description": "Adds numbers.",
    "acceptance_criteria": ["add(1, 2) returns 3"],
    "assumptions": [],
    "tasks": [
        {
            "task_id": "T1",
            "title": "Add",
            "description": "Write add().",
            "acceptance_criteria": ["add rejects strings with TypeError"],
            "priority": "high",
        }
    ],
}
TEST_FILE = {
    "path": "tests/test_app.py",
    "content": "from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "criteria": ["add(1, 2) returns 3"],
}


def reply(payload=None, stop_reason: str = "end_turn") -> SimpleNamespace:
    content = [] if payload is None else [SimpleNamespace(type="text", text=json.dumps(payload))]
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=100, output_tokens=40),
    )


def write_tests_reply(*files) -> SimpleNamespace:
    return reply({"test_files": list(files or [TEST_FILE]), "notes": ""})


def review_reply(findings=(), regression_tests=()) -> SimpleNamespace:
    return reply(
        {"findings": list(findings), "regression_tests": list(regression_tests), "summary": "ok"}
    )


def raw_finding(kind: str = "bug", rule: str = "", test_ids=(), same_as: str = "") -> dict:
    return {
        "kind": kind,
        "rule": rule,
        "file": "app.py",
        "location": "add()",
        "problem": "Wrong result.",
        "expected": "add(1, 2) returns 3",
        "likely_cause": "Subtracts instead of adding.",
        "fix_instruction": "Return a + b.",
        "test_ids": list(test_ids),
        "same_as": same_as,
    }


def failing_run(*test_ids: str) -> dict:
    run = scripted_test_run(failed=0)
    for test_id in test_ids:
        run["cases"].append(
            {"test_id": test_id, "file": "tests/test_app.py", "outcome": "failed",
             "message": "assert 0 == 3", "details": "Traceback"}
        )
    run["failed"] = len(test_ids)
    return run


class FakeClient:
    """Replays scripted replies (or raises scripted exceptions) and records requests."""

    def __init__(self, replies: list) -> None:
        self._replies = list(replies)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def user_text(self, index: int) -> str:
        """The request's user message as text (its content blocks joined)."""
        content = self.requests[index]["messages"][0]["content"]
        if isinstance(content, str):
            return content
        return "\n\n".join(block["text"] for block in content)


class FakeRunner:
    """Stands in for ``run_pytest``: returns scripted ``TestRun``s in order."""

    def __init__(self, runs: list) -> None:
        self._runs = list(runs)
        self.calls = 0

    def __call__(self, workspace: Path, timeout_seconds: float, image: str, **kwargs) -> dict:
        self.calls += 1
        self.kwargs = kwargs
        return self._runs.pop(0) if len(self._runs) > 1 else self._runs[0]


class QATestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "ws"
        (self.workspace / "tests").mkdir(parents=True)
        (self.workspace / "app.py").write_text(APP_SOURCE, encoding="utf-8")
        (self.workspace / "tests" / "stale_test.py").write_text("old", encoding="utf-8")
        FakeFileTools.reset()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def context(self, iteration: int = 1, **overrides) -> dict:
        context = {
            "iteration": iteration,
            "workspace": str(self.workspace),
            "spec": SPEC,
            "source_files": ["app.py"],
            "coder_notes": ["Wrote app.py with add()."],
            "main_file": "app.py",
            "test_suite": {},
            "previous_findings": [],
            "responses": [],
        }
        context.update(overrides)
        return context

    def run_qa(self, replies: list, runs: list, **context_overrides):
        self.client = FakeClient(replies)
        self.runner = FakeRunner(runs)
        agent = QAAgent(
            QAConfig(), client=self.client, run_tests=self.runner, file_tools=FakeFileTools
        )
        return agent.run_iteration(self.context(**context_overrides))


class InterfaceSummaryTests(unittest.TestCase):
    def test_lists_the_public_interface_without_bodies(self) -> None:
        summary = interface_summary({"app.py": APP_SOURCE, "broken.py": "def oops(:\n"})
        for expected in (
            "Tiny calculator.",
            "def add(a: int, b: int) -> int",
            "Add two numbers.",
            "class Calculator",
            "Keeps a running total.",
            "def push(self, value: int) -> None",
            "def main() -> None",
            'if __name__ == "__main__"',
            "parser.add_argument('numbers', nargs='+', type=int)",
            "broken.py: could not be parsed",
        ):
            self.assertIn(expected, summary)
        self.assertNotIn(BODY_MARKER, summary)
        self.assertNotIn("_hidden", summary)
        self.assertNotIn("return a + b", summary)


class IterationOneTests(QATestCase):
    def test_findings_match_the_schema_and_every_failing_test_is_covered(self) -> None:
        result = self.run_qa(
            [
                write_tests_reply(),
                review_reply([
                    raw_finding("failing_test", test_ids=["tests/test_app.py::test_a"]),
                    raw_finding("style", rule=""),
                    raw_finding("style", rule="no-wildcard-imports"),
                ]),
            ],
            [failing_run("tests/test_app.py::test_a", "tests/test_app.py::test_b")],
        )
        self.assertIsNone(result.error)
        assert_matches_findings_schema(result.findings)
        failing = [f for f in result.findings if f["kind"] == "failing_test"]
        covered = {test_id for finding in failing for test_id in finding["test_ids"]}
        self.assertEqual(covered, {"tests/test_app.py::test_a", "tests/test_app.py::test_b"})
        by_kind = {(f["kind"], f["rule"]): f["severity"] for f in result.findings}
        self.assertEqual(by_kind[("style", None)], "advisory")
        self.assertEqual(by_kind[("style", "no-wildcard-imports")], "blocking")
        severities = [f["severity"] for f in result.findings]
        self.assertEqual(severities, sorted(severities, key=lambda s: s != "blocking"))
        self.assertEqual(result.verdict, "fix_needed")
        self.assertTrue(all(f["finding_id"].startswith("F1-") for f in result.findings))

    def test_complete_only_when_loaded_passing_and_no_blocking_findings(self) -> None:
        passing = scripted_test_run(failed=0)
        result = self.run_qa([write_tests_reply(), review_reply()], [passing])
        self.assertEqual(result.verdict, "complete")
        advisory_only = self.run_qa(
            [write_tests_reply(), review_reply([raw_finding("style")])], [passing]
        )
        self.assertEqual(advisory_only.verdict, "complete")
        with_bug = self.run_qa(
            [write_tests_reply(), review_reply([raw_finding("bug")])], [passing]
        )
        self.assertEqual(with_bug.verdict, "fix_needed")

    def test_tests_are_written_through_mcp_and_old_tests_removed(self) -> None:
        result = self.run_qa([write_tests_reply(), review_reply()], [scripted_test_run()])
        session = FakeFileTools.sessions[-1]
        self.assertEqual(session.protect, ())
        self.assertIn(("write_file", {"path": TEST_FILE["path"], "content": TEST_FILE["content"]}),
                      session.calls)
        self.assertIn(("read_file", {"path": "app.py"}), session.calls)
        self.assertFalse((self.workspace / "tests" / "stale_test.py").exists())
        self.assertEqual(result.test_suite, {TEST_FILE["path"]: TEST_FILE["content"]})
        self.assertEqual(result.test_changes[0]["reason"], "initial")
        self.assertEqual(result.test_changes[0]["criteria"], TEST_FILE["criteria"])

    def test_write_tests_prompt_has_interface_and_criteria_but_no_bodies(self) -> None:
        self.run_qa([write_tests_reply(), review_reply()], [scripted_test_run()])
        write_prompt = self.client.user_text(0)
        self.assertIn("def add(a: int, b: int) -> int", write_prompt)
        self.assertIn("add(1, 2) returns 3", write_prompt)
        self.assertIn("add rejects strings with TypeError", write_prompt)
        self.assertNotIn(BODY_MARKER, write_prompt)
        self.assertIn(BODY_MARKER, self.client.user_text(1))  # The review reads the full code.
        self.assertEqual(
            self.client.requests[0]["output_config"]["format"]["type"], "json_schema"
        )

    def test_malformed_replies_end_the_turn_after_one_correction(self) -> None:
        outside = {**TEST_FILE, "path": "app_test.py"}
        unknown = {**TEST_FILE, "criteria": ["Something the spec never said"]}
        empty = reply({"test_files": [], "notes": ""})
        for bad, text in (
            (write_tests_reply(outside), "tests/"),
            (write_tests_reply(unknown), "criterion"),
            (empty, "no test files"),
        ):
            with self.subTest(text=text):
                result = self.run_qa([bad, bad], [scripted_test_run()])
                self.assertIn(text, result.error)
                self.assertEqual(len(self.client.requests), 2)
                self.assertIn("Your previous reply was rejected", self.client.user_text(1))
                self.assertEqual(self.runner.calls, 0)

    def test_a_corrected_reply_is_used_and_names_the_rejection(self) -> None:
        paraphrased = {**TEST_FILE, "criteria": ["adding 1 and 2 gives 3"]}
        result = self.run_qa(
            [write_tests_reply(paraphrased), write_tests_reply(), review_reply()],
            [scripted_test_run()],
        )
        self.assertIsNone(result.error)
        self.assertEqual(result.test_suite, {TEST_FILE["path"]: TEST_FILE["content"]})
        retry_prompt = self.client.user_text(1)
        self.assertTrue(retry_prompt.startswith(self.client.user_text(0)))
        self.assertIn("adding 1 and 2 gives 3", retry_prompt)
        self.assertIn("character for character", retry_prompt)

    def test_a_cut_off_reply_is_asked_again(self) -> None:
        result = self.run_qa(
            [reply(stop_reason="max_tokens"), write_tests_reply(), review_reply()],
            [scripted_test_run()],
        )
        self.assertIsNone(result.error)
        self.assertIn("cut off at the output limit", self.client.user_text(1))
        self.assertIn("fewer or shorter test files", self.client.user_text(1))

    def test_nothing_is_written_from_a_rejected_reply(self) -> None:
        bad_second = {**TEST_FILE, "path": "tests/test_more.py", "criteria": ["Invented"]}
        result = self.run_qa(
            [write_tests_reply(TEST_FILE, bad_second)] * 2, [scripted_test_run()]
        )
        self.assertIn("Invented", result.error)
        writes = [c for c in FakeFileTools.sessions[-1].calls if c[0] == "write_file"]
        self.assertEqual(writes, [])
        self.assertFalse((self.workspace / "tests" / "test_app.py").exists())

    def test_a_review_with_a_bad_regression_test_is_asked_again(self) -> None:
        bad_regression = {"finding_index": 0, "path": "regress.py",
                          "content": "def test_r(): pass\n", "criteria": ["add(1, 2) returns 3"]}
        good_regression = {**bad_regression, "path": "tests/test_regress.py"}
        result = self.run_qa(
            [write_tests_reply(),
             review_reply([raw_finding()], [bad_regression]),
             review_reply([raw_finding()], [good_regression])],
            [scripted_test_run()],
        )
        self.assertIsNone(result.error)
        self.assertIn("tests/test_regress.py", result.test_suite)
        self.assertIn("regress.py", self.client.user_text(2))

    def test_load_repair_retries_then_gives_up(self) -> None:
        broken = scripted_test_run(loaded=False)
        broken["output"] = "ERROR collecting tests/test_app.py\nSyntaxError"
        result = self.run_qa(
            [write_tests_reply(), write_tests_reply(), write_tests_reply(), review_reply()],
            [broken, broken, scripted_test_run()],
        )
        self.assertIsNone(result.error)
        self.assertEqual(self.runner.calls, 3)
        self.assertEqual(
            [change["reason"] for change in result.test_changes],
            ["initial", "load_repair", "load_repair"],
        )
        self.assertIn("SyntaxError", self.client.user_text(1))
        result = self.run_qa([write_tests_reply()] * 3, [broken])
        self.assertIn("failed to load", result.error)
        self.assertEqual(result.verdict, "fix_needed")

    def test_model_failures_become_errors_not_exceptions(self) -> None:
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        for script, text in (
            ([reply(stop_reason="refusal")], "declined"),
            ([anthropic.AuthenticationError(
                "invalid x-api-key", response=httpx2.Response(401, request=request), body=None
            )], "Model call failed"),
            ([self.bad_json_reply()] * 2, "not valid JSON"),
            ([reply(stop_reason="max_tokens")] * 2, "cut off at the output limit"),
        ):
            with self.subTest(text=text):
                result = self.run_qa(list(script), [scripted_test_run()])
                self.assertIn(text, result.error)
                # Refusals and failed calls are not asked again; bad replies are, once.
                self.assertEqual(len(self.client.requests), len(script))

    @staticmethod
    def bad_json_reply() -> SimpleNamespace:
        bad = reply()
        bad.content = [SimpleNamespace(type="text", text="{")]
        return bad

    def test_timeout_becomes_a_blocking_finding(self) -> None:
        timed_out = scripted_test_run(timed_out=True)
        timed_out.update(cases=[], passed=0, failed=1)
        result = self.run_qa([write_tests_reply(), review_reply()], [timed_out])
        timeout = [f for f in result.findings if f["test_ids"] == ["<timeout>"]]
        self.assertEqual(len(timeout), 1)
        self.assertEqual(timeout[0]["severity"], "blocking")
        self.assertEqual(result.verdict, "fix_needed")

    def test_unavailable_mcp_server_is_an_error(self) -> None:
        class Broken(FakeFileTools):
            def __enter__(self):
                raise McpUnavailableError("no server")

        agent = QAAgent(QAConfig(), client=FakeClient([]), run_tests=FakeRunner([{}]),
                        file_tools=Broken)
        result = agent.run_iteration(self.context())
        self.assertIn("MCP file tools unavailable", result.error)

    def test_regression_tests_are_written_and_recorded(self) -> None:
        regression = {"finding_index": 0, "path": "tests/test_regression.py",
                      "content": "def test_r(): pass\n", "criteria": ["add(1, 2) returns 3"]}
        result = self.run_qa(
            [write_tests_reply(), review_reply([raw_finding("bug")], [regression])],
            [scripted_test_run()],
        )
        change = result.test_changes[-1]
        self.assertEqual((change["reason"], change["finding_id"]), ("regression", "F1-1"))
        self.assertIn("tests/test_regression.py", result.test_suite)
        self.assertEqual(result.verdict, "fix_needed")

    def test_result_round_trips_through_json(self) -> None:
        result = self.run_qa([write_tests_reply(), review_reply()], [scripted_test_run()])
        self.assertEqual(json.loads(json.dumps(result.to_dict())), result.to_dict())


SUITE_CONTENT = TEST_FILE["content"]


def previous_findings() -> list[dict]:
    bug = normalise_findings([raw_finding("bug")], scripted_test_run(), 1, [])[0]
    failing = normalise_findings(
        [raw_finding("failing_test", test_ids=["tests/test_app.py::test_ok"])],
        scripted_test_run(), 1, [],
    )[0]
    return [bug, {**failing, "finding_id": "F1-2"}]


def disputes_reply(decision: str, corrected=()) -> SimpleNamespace:
    return reply({
        "decisions": [
            {"finding_id": "F1-1", "decision": decision, "reason": "Spec criterion 1 says so."}
        ],
        "corrected_files": list(corrected),
    })


class LaterIterationTests(QATestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.workspace / "tests" / "stale_test.py").unlink()
        (self.workspace / "tests" / "test_app.py").write_text(SUITE_CONTENT, encoding="utf-8")

    def later(self, replies: list, runs: list, responses=(), **overrides):
        return self.run_qa(
            replies, runs, iteration=2, test_suite={TEST_FILE["path"]: SUITE_CONTENT},
            previous_findings=previous_findings(), responses=list(responses), **overrides,
        )

    def test_changed_or_extra_test_files_are_restored_and_reported(self) -> None:
        (self.workspace / "tests" / "test_app.py").write_text("def test_add(): pass\n", "utf-8")
        (self.workspace / "tests" / "test_extra.py").write_text("x = 1\n", encoding="utf-8")
        result = self.later([review_reply()], [scripted_test_run()])
        self.assertEqual(
            (self.workspace / "tests" / "test_app.py").read_text(encoding="utf-8"), SUITE_CONTENT
        )
        self.assertFalse((self.workspace / "tests" / "test_extra.py").exists())
        restored = [c for c in result.test_changes if c["reason"] == "restored"]
        self.assertEqual([c["path"] for c in restored], ["tests/test_app.py"])
        tamper = [f for f in result.findings if "changed outside QA" in f["problem"]]
        self.assertEqual(len(tamper), 1)
        self.assertEqual((tamper[0]["kind"], tamper[0]["severity"]), ("bug", "blocking"))
        self.assertIn("tests/test_extra.py", tamper[0]["problem"])
        self.assertEqual(result.verdict, "fix_needed")
        assert_matches_findings_schema(result.findings)

    def test_untouched_suite_is_not_rewritten_and_no_dispute_call_is_made(self) -> None:
        result = self.later([review_reply()], [scripted_test_run()])
        self.assertEqual(len(self.client.requests), 1)  # Only the review.
        writes = [call for call in FakeFileTools.sessions[-1].calls if call[0] == "write_file"]
        self.assertEqual(writes, [])
        self.assertEqual(result.test_changes, [])

    def test_kept_dispute_leaves_the_finding_open(self) -> None:
        responses = [{"finding_id": "F1-1", "action": "disputed", "reason": "Not in the spec."}]
        result = self.later(
            [disputes_reply("keep"), review_reply([raw_finding("bug", same_as="F1-1")])],
            [scripted_test_run()], responses=responses,
        )
        self.assertEqual(result.disputes, [{
            "finding_id": "F1-1", "coder_reason": "Not in the spec.", "decision": "keep",
            "qa_reason": "Spec criterion 1 says so.", "decided_in": 2,
        }])
        kept = [f for f in result.findings if f["finding_id"] == "F1-1"]
        self.assertEqual([(f["state"], f["first_seen"]) for f in kept], [("open", 1)])
        self.assertEqual(result.verdict, "fix_needed")

    def test_corrected_dispute_withdraws_the_finding_and_rewrites_the_test(self) -> None:
        responses = [{"finding_id": "F1-1", "action": "disputed", "reason": "Not in the spec."}]
        corrected = {**TEST_FILE, "content": "def test_add():\n    assert True\n"}
        result = self.later(
            [disputes_reply("correct", [corrected]), review_reply()],
            [scripted_test_run()], responses=responses,
        )
        states = {f["finding_id"]: f["state"] for f in result.findings}
        self.assertEqual(states["F1-1"], "withdrawn")
        self.assertEqual(states["F1-2"], "fixed")
        change = result.test_changes[0]
        self.assertEqual((change["reason"], change["finding_id"]), ("dispute_correction", "F1-1"))
        self.assertEqual(result.test_suite[TEST_FILE["path"]], corrected["content"])
        self.assertEqual(result.verdict, "complete")
        assert_matches_findings_schema(result.findings)

    def test_findings_not_reported_again_are_fixed(self) -> None:
        result = self.later([review_reply()], [scripted_test_run()])
        self.assertEqual(
            {f["finding_id"]: f["state"] for f in result.findings},
            {"F1-1": "fixed", "F1-2": "fixed"},
        )
        self.assertEqual(result.verdict, "complete")

    def test_review_prompt_shows_previous_findings_and_the_coders_answers(self) -> None:
        responses = [{"finding_id": "F1-2", "action": "fixed", "reason": "RESPONSE_MARKER"}]
        self.later([review_reply()], [scripted_test_run()], responses=responses)
        prompt = self.client.user_text(0)
        self.assertIn("F1-1", prompt)
        self.assertIn("RESPONSE_MARKER", prompt)

    def test_dispute_decision_for_an_unknown_finding_is_an_error(self) -> None:
        responses = [{"finding_id": "F1-2", "action": "disputed", "reason": "x"}]
        result = self.later(
            [disputes_reply("keep")] * 2, [scripted_test_run()], responses=responses
        )
        self.assertIn("F1-1", result.error)
        self.assertEqual(len(self.client.requests), 2)

    def test_corrected_dispute_reply_is_used(self) -> None:
        responses = [{"finding_id": "F1-1", "action": "disputed", "reason": "x"}]
        bad_file = {**TEST_FILE, "criteria": ["A criterion the spec never had"]}
        result = self.later(
            [disputes_reply("correct", [bad_file]), disputes_reply("keep"), review_reply()],
            [scripted_test_run()], responses=responses,
        )
        self.assertIsNone(result.error)
        self.assertEqual([d["decision"] for d in result.disputes], ["keep"])
        self.assertIn("A criterion the spec never had", self.client.user_text(1))
        writes = [c for c in FakeFileTools.sessions[-1].calls if c[0] == "write_file"]
        self.assertEqual(writes, [])


class DependencyFailureTests(QATestCase):
    """QA reports a model call it can't make in the iteration's ``failure``."""

    def test_an_unavailable_model_is_reported_with_its_failure(self) -> None:
        result = self.run_qa([sc.overloaded_error()] * 4, [scripted_test_run()])
        self.assertTrue(result.error.startswith("Model service unavailable"), result.error)
        self.assertEqual(result.failure["kind"], "temporary")
        self.assertEqual(result.verdict, "fix_needed")
        self.assertEqual(result.to_dict()["failure"]["dependency"], "anthropic")

    def test_a_permanent_model_failure_is_a_model_call_failure(self) -> None:
        result = self.run_qa([sc.auth_error()], [scripted_test_run()])
        self.assertTrue(result.error.startswith("Model call failed"), result.error)
        self.assertEqual(result.failure["kind"], "permanent")


class ReplyValidationTests(QATestCase):
    """A QA reply that fails its ``TypedDict`` is rejected before anything is written."""

    def writes(self) -> list[str]:
        return [
            arguments["path"] for session in FakeFileTools.sessions
            for name, arguments in session.calls if name == "write_file"
        ]

    def assert_malformed(self, result, expected: str) -> None:
        self.assertTrue(result.error.startswith("Malformed QA reply: "), result.error)
        self.assertIn(expected, result.error)
        self.assertEqual(result.verdict, "fix_needed")

    def test_a_test_file_missing_its_criteria_is_rejected_twice_then_fails(self) -> None:
        bad = {key: value for key, value in TEST_FILE.items() if key != "criteria"}
        result = self.run_qa([write_tests_reply(bad)] * 2, [scripted_test_run()])
        self.assert_malformed(result, "$.test_files[0]: missing required key 'criteria'")
        self.assertEqual(len(self.client.requests), 2)
        self.assertEqual(self.writes(), [])

    def test_a_dispute_decision_outside_its_set_is_rejected(self) -> None:
        (self.workspace / "tests" / "stale_test.py").unlink()
        (self.workspace / "tests" / "test_app.py").write_text(SUITE_CONTENT, encoding="utf-8")
        responses = [{"finding_id": "F1-1", "action": "disputed", "reason": "Not in the spec."}]
        result = self.run_qa(
            [disputes_reply("critical")] * 2, [scripted_test_run()], iteration=2,
            test_suite={TEST_FILE["path"]: SUITE_CONTENT},
            previous_findings=previous_findings(), responses=responses,
        )
        self.assert_malformed(result, "$.decisions[0].decision: 'critical' is not one of")
        self.assertEqual(self.writes(), [])

    def test_a_non_integer_finding_index_is_rejected(self) -> None:
        regression = {**TEST_FILE, "path": "tests/test_regression.py", "finding_index": "0"}
        bad_review = review_reply([raw_finding()], [regression])
        result = self.run_qa(
            [write_tests_reply(), bad_review, bad_review], [scripted_test_run()]
        )
        self.assert_malformed(
            result, "$.regression_tests[0].finding_index: expected int, got str"
        )
        self.assertEqual(self.writes(), ["tests/test_app.py"])


class NormaliseTests(unittest.TestCase):
    def test_kind_and_rule_set_the_severity(self) -> None:
        findings = normalise_findings(
            [raw_finding("bug", rule="main-guard"), raw_finding("style", rule="made-up")],
            scripted_test_run(), iteration=2, previous=[],
        )
        self.assertEqual([(f["finding_id"], f["severity"], f["rule"]) for f in findings],
                         [("F2-1", "blocking", None), ("F2-2", "advisory", None)])
        assert_matches_findings_schema(findings)

    def test_verdict_rules(self) -> None:
        self.assertEqual(verdict_for(scripted_test_run(), [], suite_ok=True), "complete")
        self.assertEqual(verdict_for(scripted_test_run(), [], suite_ok=False), "fix_needed")
        not_loaded = scripted_test_run(loaded=False)
        self.assertEqual(verdict_for(not_loaded, [], suite_ok=True), "fix_needed")
        self.assertEqual(verdict_for(scripted_test_run(failed=1), [], suite_ok=True), "fix_needed")


class FinalReportTests(QATestCase):
    def request(self, status: str = "failed", end_reason: str = "iteration_limit") -> dict:
        bug = normalise_findings([raw_finding("bug")], failing_run(), 1, [])[0]
        style = normalise_findings([raw_finding("style")], failing_run(), 1, [])[0]
        iteration = {
            "number": 1, "reflections": [], "disputes": [], "test_changes": [],
            "test_run": failing_run("tests/test_app.py::test_a"),
            "findings": [bug, {**style, "finding_id": "F1-2"}], "verdict": "fix_needed",
            "fix": None, "review_task_id": "t1", "fix_task_id": None,
        }
        return {"run_id": "run-1", "workspace": str(self.workspace), "status": status,
                "end_reason": end_reason, "max_iterations": 1, "iterations": [iteration]}

    def test_report_lists_unresolved_findings_and_is_written_through_mcp(self) -> None:
        agent = QAAgent(QAConfig(), client=FakeClient([]), run_tests=FakeRunner([{}]),
                        file_tools=FakeFileTools)
        report = agent.final_report(self.request())
        self.assertEqual((report["status"], report["end_reason"]), ("failed", "iteration_limit"))
        self.assertEqual([f["finding_id"] for f in report["unresolved"]], ["F1-1"])
        self.assertEqual([f["finding_id"] for f in report["advisory"]], ["F1-2"])
        self.assertEqual((report["final_passed"], report["final_failed"]), (1, 1))
        self.assertEqual(report["report_path"], ".qa/report.md")
        markdown = (self.workspace / ".qa" / "report.md").read_text(encoding="utf-8")
        for text in ("iteration_limit", "F1-1", "Return a + b.", "Advisory", "#1:"):
            self.assertIn(text, markdown)
        self.assertEqual(FakeFileTools.sessions[-1].calls[0][0], "write_file")

    def test_secrets_in_the_report_are_redacted(self) -> None:
        secret = "sk-ant-api03-reportsecret0123456789"
        services = RunServices.create(env={"MY_API_KEY": "plantedvalue123"}, response_cache=None)
        agent = QAAgent(QAConfig(), client=FakeClient([]), run_tests=FakeRunner([{}]),
                        file_tools=FakeFileTools, services=services)
        request = self.request()
        finding = {**request["iterations"][-1]["findings"][0],
                   "problem": f"Leaks {secret} and plantedvalue123."}
        request["iterations"][-1] = {**request["iterations"][-1], "findings": [finding]}
        agent.final_report(request)
        text = (self.workspace / ".qa" / "report.md").read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertNotIn("plantedvalue123", text)
        self.assertIn("[REDACTED]", text)

    def test_mcp_failure_leaves_the_report_path_empty(self) -> None:
        class Broken(FakeFileTools):
            def __enter__(self):
                raise McpUnavailableError("no server")

        agent = QAAgent(QAConfig(), client=FakeClient([]), run_tests=FakeRunner([{}]),
                        file_tools=Broken)
        self.assertEqual(agent.final_report(self.request())["report_path"], "")


class ConfigAndSchemaTests(unittest.TestCase):
    def test_constitution_rules_match_the_schema(self) -> None:
        enum = load_schema()["$defs"]["constitution_rule"]["enum"]
        self.assertEqual(list(CONSTITUTION_RULES), enum)

    @unittest.skipUnless(CONTRACT.is_file(), "specs/ is not checked out")
    def test_schema_fixture_matches_the_contract(self) -> None:
        self.assertEqual(FIXTURE.read_text(encoding="utf-8"), CONTRACT.read_text(encoding="utf-8"))

    def test_config_from_env_validates(self) -> None:
        env = {"QA_MODEL": "m", "QA_EFFORT": "low", "QA_TEST_TIMEOUT": "30",
               "QA_REPAIR_ATTEMPTS": "1", "QA_USE_FALLBACKS": "false"}
        with mock.patch.dict("os.environ", env):
            config = QAConfig.from_env()
        self.assertEqual(
            (config.model, config.effort, config.test_timeout_seconds, config.repair_attempts,
             config.use_fallbacks),
            ("m", "low", 30.0, 1, False),
        )
        for bad in ({"QA_EFFORT": "huge"}, {"QA_TEST_TIMEOUT": "0"}, {"QA_REPAIR_ATTEMPTS": "4"}):
            with self.subTest(env=bad), mock.patch.dict("os.environ", bad):
                with self.assertRaises(ValueError):
                    QAConfig.from_env()

    def test_qa_module_never_imports_the_coder(self) -> None:
        self.assertNotIn("coder_agent", Path(qa_agent.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
