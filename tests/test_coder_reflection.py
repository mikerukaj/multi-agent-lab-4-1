"""Offline tests for the Coder's self-reflection and finding responses.

See specs/002-qa-feedback-loop-mcp/contracts/coder-reflection.md.
"""

import json
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agents import coder_agent
from agents.coder_agent import CRITIQUE_FORMAT, SUMMARY_FORMAT, CoderAgent, CoderConfig
from orchestration.team import reflect_node
from tests.fakes import assert_no_live_dependencies
from tests.fakes import scripted_claude as sc
from tests.team_fakes import FakeFileTools, token_usage

SOURCE = "def add(a, b):\n    return a + b  # SOURCE_MARKER\n"


def text_reply(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def critique(*descriptions: str) -> SimpleNamespace:
    items = [
        {"kind": "edge_case", "location": "add()", "description": text,
         "suggested_change": "Handle it."}
        for text in descriptions
    ]
    return text_reply(json.dumps({"items": items, "overall": "Mostly fine."}))


def setUpModule() -> None:
    assert_no_live_dependencies()


class ReflectionClient:
    """Routes requests by kind: critique (CRITIQUE_FORMAT), summary, or ReAct loop step."""

    def __init__(self, critiques: list, loop_stop: str = "end_turn", responses=None) -> None:
        self._critiques = list(critiques)
        self.loop_stop = loop_stop
        self.responses = responses
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        output_format = kwargs["output_config"].get("format")
        if output_format == CRITIQUE_FORMAT:
            return self._critiques.pop(0)
        if output_format == SUMMARY_FORMAT:
            ids = self._task_ids(kwargs["messages"])
            responses = self.responses if self.responses is not None else [
                {"finding_id": item_id, "action": "fixed", "reason": ""} for item_id in ids
            ]
            summary = {"plan": ["Revise"], "explanation": "Revised add().", "main_file": "",
                       "finding_responses": responses}
            return text_reply(json.dumps(summary))
        if self.loop_stop == "refusal":
            return text_reply("", stop_reason="refusal")
        if self.loop_stop == "tool_use":
            block = SimpleNamespace(
                type="tool_use", id="t1", name="write_file",
                input={"path": "calc.py", "content": SOURCE, "purpose": "Revise."},
            )
            reply = text_reply("")
            reply.content, reply.stop_reason = [block], "tool_use"
            return reply
        return text_reply("Done.")

    @staticmethod
    def _task_ids(messages: list) -> list[str]:
        first = messages[0]["content"]
        return [word.strip("[]:") for word in first.split() if word.startswith("[C")]

    @property
    def critique_requests(self) -> list[dict]:
        return [r for r in self.requests if r["output_config"].get("format") == CRITIQUE_FORMAT]

    @property
    def revision_tasks(self) -> list[str]:
        return [
            r["messages"][0]["content"] for r in self.requests
            if "format" not in r["output_config"] and len(r["messages"]) == 1
        ]


class FakeMemory:
    def __init__(
        self, path: Path, collection: str = "coder_memory", guard=None, host=None, port=8000
    ) -> None:
        pass

    def add(self, run_id: str, kind: str, text: str) -> str:
        return "m"

    def query(self, text: str, top_k: int, exclude_run_id: str) -> list:
        return []


class ReflectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.workspace = root / "ws"
        (self.workspace / "tests").mkdir(parents=True)
        (self.workspace / "calc.py").write_text(SOURCE, encoding="utf-8")
        (self.workspace / "tests" / "test_calc.py").write_text("TEST_MARKER", encoding="utf-8")
        self.config = CoderConfig(workspace_root=root / "w", chroma_path=root / "c", max_steps=3)
        # Reflection runs no generated code here, so the Docker check is not needed.
        for name, value in (("SemanticMemory", FakeMemory), ("ensure_sandbox", lambda image: None)):
            patcher = mock.patch.object(coder_agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeFileTools.reset()

    def agent(self, client, **config_changes) -> CoderAgent:
        config = replace(self.config, **config_changes)
        return CoderAgent(config, client=client, file_tools=FakeFileTools)


class ReflectTests(ReflectionTestCase):
    def test_critique_is_tool_less_and_sees_every_source_file(self) -> None:
        client = ReflectionClient([critique("Negative numbers")])
        result = self.agent(client).reflect("Build a calculator.", self.workspace)
        request = client.critique_requests[0]
        self.assertNotIn("tools", request)
        self.assertEqual(request["output_config"]["format"], CRITIQUE_FORMAT)
        prompt = "\n\n".join(block["text"] for block in request["messages"][0]["content"])
        self.assertIn("SOURCE_MARKER", prompt)
        self.assertIn("Build a calculator.", prompt)
        self.assertNotIn("TEST_MARKER", prompt)
        self.assertEqual(result.status, "completed")
        self.assertEqual(FakeFileTools.sessions[0].protect, ("tests", ".qa"))

    def test_items_are_numbered_and_the_revision_answers_them(self) -> None:
        client = ReflectionClient([critique("Negative numbers", "Floats")])
        result = self.agent(client).reflect("Build a calculator.", self.workspace)
        reflection = result.rounds[0]
        self.assertEqual([item["item_id"] for item in reflection["critique"]], ["C1", "C2"])
        revision = client.revision_tasks[0]
        self.assertIn("[C1]", revision)
        self.assertIn("[C2]", revision)
        self.assertEqual(
            [response["finding_id"] for response in reflection["responses"]], ["C1", "C2"]
        )
        self.assertEqual(result.result.explanation, "Revised add().")
        self.assertEqual(reflection["summary"], "Revised add().")
        self.assertEqual(result.usage, token_usage(30, 15))

    def test_empty_critique_is_retried_once_then_an_error(self) -> None:
        empty = text_reply(json.dumps({"items": [], "overall": "Nothing."}))
        client = ReflectionClient([empty, critique("Overflow")])
        self.assertEqual(self.agent(client).reflect("B", self.workspace).status, "completed")
        client = ReflectionClient([empty, empty])
        result = self.agent(client).reflect("B", self.workspace)
        self.assertEqual(result.status, "error")
        self.assertIn("no potential bugs or edge cases", result.result.error)
        self.assertEqual(client.revision_tasks, [])

    def test_two_rounds_and_step_limit_does_not_stop_the_rounds(self) -> None:
        client = ReflectionClient([critique("A"), critique("B")], loop_stop="tool_use")
        agent = self.agent(client, reflection_rounds=2, max_steps=2)
        result = agent.reflect("B", self.workspace)
        self.assertEqual(len(client.critique_requests), 2)
        self.assertEqual([r["round"] for r in result.rounds], [1, 2])
        self.assertEqual([r["revision_status"] for r in result.rounds], ["step_limit"] * 2)
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(result.rounds[0]["files_changed"], ["calc.py"])

    def test_refusal_stops_with_refused(self) -> None:
        client = ReflectionClient([text_reply("", stop_reason="refusal")])
        result = self.agent(client).reflect("B", self.workspace)
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.rounds, [])
        client = ReflectionClient([critique("A"), critique("B")], loop_stop="refusal")
        result = self.agent(client, reflection_rounds=2).reflect("B", self.workspace)
        self.assertEqual(result.status, "refused")
        self.assertEqual(len(client.critique_requests), 1)

    def test_reflection_rounds_setting_is_validated(self) -> None:
        for bad in ("0", "4", "x"):
            with self.subTest(value=bad), mock.patch.dict(
                "os.environ", {"CODER_REFLECTION_ROUNDS": bad}
            ):
                with self.assertRaises(ValueError):
                    CoderConfig.from_env()
        with mock.patch.dict("os.environ", {"CODER_REFLECTION_ROUNDS": "3"}):
            self.assertEqual(CoderConfig.from_env().reflection_rounds, 3)


class FindingResponseTests(ReflectionTestCase):
    def test_responses_are_parsed_and_unknown_ids_dropped(self) -> None:
        responses = [
            {"finding_id": "F1-1", "action": "disputed", "reason": "The spec says 2 decimals."},
            {"finding_id": "F9-9", "action": "fixed", "reason": ""},
        ]
        client = ReflectionClient([], responses=responses)
        result = self.agent(client).run("Fix F1-1", self.workspace, answer_ids=["F1-1"])
        self.assertEqual(result.finding_responses, [responses[0]])
        plain = self.agent(ReflectionClient([], responses=responses)).run("Build", self.workspace)
        self.assertEqual(plain.finding_responses, [])

    def test_summary_schema_requires_finding_responses(self) -> None:
        schema = SUMMARY_FORMAT["schema"]
        self.assertIn("finding_responses", schema["required"])
        action = schema["properties"]["finding_responses"]["items"]["properties"]["action"]
        self.assertEqual(action["enum"], ["fixed", "disputed", "not_addressed"])



class DependencyFailureTests(ReflectionTestCase):
    """The Coder reports a model call it can't make, and how (FR-205, FR-209c)."""

    def test_an_unavailable_model_ends_the_session_dependency_unavailable(self) -> None:
        client = sc.ScriptedClaude([sc.overloaded_error()] * 4)
        result = self.agent(client).run("Build", self.workspace)
        self.assertEqual(result.status, "dependency_unavailable")
        self.assertTrue(result.error.startswith("Model service unavailable"), result.error)
        self.assertEqual(result.failure["kind"], "temporary")
        self.assertEqual(client.remaining, 0)

    def test_a_permanent_model_failure_is_an_error(self) -> None:
        client = sc.ScriptedClaude([sc.auth_error()])
        result = self.agent(client).run("Build", self.workspace)
        self.assertEqual(result.status, "error")
        self.assertTrue(result.error.startswith("Model call failed"), result.error)
        self.assertEqual(result.failure["kind"], "permanent")

    def test_a_failed_critique_call_carries_its_failure_into_the_reflection(self) -> None:
        reflection = self.agent(sc.ScriptedClaude([sc.overloaded_error()] * 4)).reflect(
            "B", self.workspace
        )
        self.assertEqual(reflection.status, "dependency_unavailable")
        self.assertEqual(reflection.failure["dependency"], "anthropic")
        self.assertIn("Critique call failed", reflection.result.error)


class SummaryClient(ReflectionClient):
    """Writes ``calc.py`` once, then answers the summary call with ``summary``."""

    def __init__(self, summary: dict) -> None:
        super().__init__([], loop_stop="tool_use")
        self._summary = summary

    def _create(self, **kwargs):
        if kwargs["output_config"].get("format") == SUMMARY_FORMAT:
            self.requests.append(kwargs)
            return text_reply(json.dumps(self._summary))
        return super()._create(**kwargs)


class ReplyValidationTests(ReflectionTestCase):
    """Coder replies that fail their ``TypedDict`` are never used as they are (FR-219a)."""

    def test_a_summary_missing_main_file_falls_back_and_keeps_the_code(self) -> None:
        summary = {"plan": ["Write"], "explanation": "Done.", "finding_responses": []}
        client = SummaryClient(summary)
        result = self.agent(client, max_steps=2).run("Build", self.workspace)
        self.assertEqual(result.validation_problems, ["$: missing required key 'main_file'"])
        self.assertEqual(result.plan, [])
        self.assertEqual(result.explanation, json.dumps(summary))
        self.assertEqual(result.main_file, "calc.py")
        self.assertEqual(result.code, SOURCE)

    def test_a_valid_summary_records_no_problems(self) -> None:
        summary = {"plan": ["Write"], "explanation": "Done.", "main_file": "calc.py",
                   "finding_responses": []}
        result = self.agent(SummaryClient(summary), max_steps=2).run("Build", self.workspace)
        self.assertEqual(result.validation_problems, [])
        self.assertEqual(result.plan, ["Write"])

    def test_a_critique_item_with_an_unknown_kind_fails_reflection(self) -> None:
        bad = text_reply(json.dumps({"items": [
            {"kind": "typo", "location": "add()", "description": "d", "suggested_change": "s"}
        ], "overall": "o"}))
        client = ReflectionClient([bad])
        result = self.agent(client).reflect("B", self.workspace)
        self.assertEqual(result.status, "error")
        self.assertIn("Critique output failed validation: $.items[0].kind: 'typo'",
                      result.result.error)
        self.assertEqual(client.revision_tasks, [])

    def test_the_team_maps_a_failed_critique_to_reflection_failed(self) -> None:
        bad = text_reply(json.dumps({"items": [{"kind": "typo"}], "overall": "o"}))
        state = {
            "iterations": [], "usage": token_usage(0, 0), "workspace": str(self.workspace),
            "spec": {"name": "Calc", "description": "Adds.", "acceptance_criteria": ["adds"],
                     "assumptions": [], "tasks": []},
        }
        update = reflect_node(state, self.agent(ReflectionClient([bad])))
        self.assertEqual(update["end_reason"], "reflection_failed")
        self.assertIn("Critique output failed validation", update["error"])


if __name__ == "__main__":
    unittest.main()
