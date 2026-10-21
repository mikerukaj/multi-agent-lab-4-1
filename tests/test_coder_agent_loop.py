"""Offline tests for the Coder Agent's ReAct loop, using a scripted fake client."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2

from agents import coder_agent
from agents.coder_agent import (
    CoderAgent,
    CoderConfig,
    CoderResult,
    ReflectionResult,
    Step,
    ToolCall,
)
from tests.fakes import assert_no_live_dependencies
from tests.fakes import scripted_claude as sc
from tests.fakes.local_sandbox import local_exec_python
from tests.team_fakes import FakeFileTools, token_usage
from tools.mcp_client import McpUnavailableError
from tools.python_exec import SandboxUnavailableError

# The loop tests run their fixture scripts with a local stand-in for the sandbox, so they
# need no Docker; tests/integration/test_coder_sandbox.py runs the same classes with the
# real sandbox (FR-235a).
requires_docker = unittest.skipUnless(shutil.which("docker"), "Docker is not available")

SUMMARY = {
    "plan": ["Write main.py", "Run main.py"],
    "explanation": "Wrote main.py, which prints hi.",
    "main_file": "main.py",
    "finding_responses": [],
}


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def thinking_block(summary: str) -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking=summary, signature="sig")


def tool_use(block_id: str, name: str, tool_input: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


def response(content: list, stop_reason: str, stop_details=None) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def setUpModule() -> None:
    assert_no_live_dependencies()


class FakeClient:
    """Stands in for ``anthropic.Anthropic``; replays scripted loop responses.

    Requests carrying ``output_config.format`` are the finalize call and get
    ``summary`` back. ``loop`` is a list of responses or exceptions, or a
    callable returning the next one.
    """

    def __init__(self, loop, summary: dict = SUMMARY, events: list | None = None) -> None:
        self._loop = iter(loop) if isinstance(loop, list) else None
        self._loop_fn = loop if callable(loop) else None
        self._summary = summary
        self.events = events if events is not None else []
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        self.events.append("create")
        if "format" in kwargs["output_config"]:
            return response([text_block(json.dumps(self._summary))], "end_turn")
        item = self._loop_fn() if self._loop_fn else next(self._loop)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def loop_requests(self) -> list[dict]:
        return [r for r in self.requests if "format" not in r["output_config"]]


class FakeMemory:
    """Records calls instead of embedding with Chroma."""

    events: list = []
    records: list = []

    def __init__(
        self, path: Path, collection: str = "coder_memory", guard=None, host=None, port=8000
    ) -> None:
        pass

    def add(self, run_id: str, kind: str, text: str) -> str:
        FakeMemory.records.append((run_id, kind, text))
        return f"{run_id}-{len(FakeMemory.records)}"

    def query(self, text: str, top_k: int, exclude_run_id: str) -> list:
        FakeMemory.events.append("query")
        return [("old-1", "The user's name is Priya.")]


class AgentTestCase(unittest.TestCase):
    real_sandbox = False
    """Set by the integration subclasses, which run the same tests in Docker."""

    def setUp(self) -> None:
        if not self.real_sandbox:
            for target, value in (("agents.coder_agent.ensure_sandbox", lambda image: None),
                                  ("agents.coder_agent.exec_python", local_exec_python),
                                  ("tools.exec_python", local_exec_python)):
                patcher = mock.patch(target, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config = CoderConfig(
            workspace_root=root / "workspace", chroma_path=root / "chroma", max_steps=3
        )
        FakeMemory.events = []
        FakeMemory.records = []
        patcher = mock.patch.object(coder_agent, "SemanticMemory", FakeMemory)
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeFileTools.reset()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_agent(self, client: FakeClient, **kwargs) -> CoderAgent:
        kwargs.setdefault("file_tools", FakeFileTools)
        return CoderAgent(self.config, client=client, **kwargs)


def happy_path_script() -> list:
    return [
        response(
            [
                thinking_block("I should write the file first."),
                tool_use("t1", "write_file", {"path": "main.py", "content": "print('hi')\n",
                                              "purpose": "Write the program."}),
            ],
            "tool_use",
        ),
        response(
            [tool_use("t2", "exec_python", {"path": "main.py", "purpose": "Check the output."})],
            "tool_use",
        ),
        response([text_block("Done.")], "end_turn"),
    ]


class HappyPathTests(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = FakeClient(happy_path_script(), events=FakeMemory.events)
        self.steps_seen: list[Step] = []
        self.result = self.make_agent(self.client, on_step=self.steps_seen.append).run(
            "Write main.py that prints hi"
        )

    def test_completes_with_ground_truth_code_and_execution(self) -> None:
        result = self.result
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.main_file, "main.py")
        workspace = Path(result.workspace)
        self.assertEqual(result.code, (workspace / "main.py").read_text(encoding="utf-8"))
        self.assertEqual(result.execution_result.exit_code, 0)
        self.assertEqual(result.execution_result.stdout, "hi\n")
        self.assertEqual(result.files_written, ["main.py"])
        self.assertEqual(result.plan, SUMMARY["plan"])
        self.assertEqual(result.explanation, SUMMARY["explanation"])
        self.assertEqual(result.steps_used, 3)
        self.assertIsNone(result.error)

    def test_required_fields_have_contract_types(self) -> None:
        data = self.result.to_dict()
        self.assertIsInstance(data["code"], str)
        self.assertIsInstance(data["explanation"], str)
        self.assertIsInstance(data["plan"], list)
        self.assertIsInstance(data["execution_result"], dict)
        json.dumps(data)

    def test_memory_is_queried_before_every_loop_model_call(self) -> None:
        loop_events = [e for e in FakeMemory.events]
        # The finalize call is the last "create"; every earlier one follows a query.
        creates = [i for i, e in enumerate(loop_events) if e == "create"][:-1]
        self.assertEqual(len(creates), 3)
        for index in creates:
            self.assertEqual(loop_events[index - 1], "query")

    def test_memories_are_injected_once_as_system_messages(self) -> None:
        messages = self.client.requests[-1]["messages"]
        system_messages = [m for m in messages if m["role"] == "system"]
        self.assertEqual(len(system_messages), 1)
        self.assertIn("Priya", system_messages[0]["content"])

    def test_history_is_append_only(self) -> None:
        first = self.client.requests[0]
        for previous, current in zip(self.client.requests, self.client.requests[1:]):
            self.assertEqual(current["system"], first["system"])
            self.assertEqual(current["tools"], first["tools"])
            prefix = current["messages"][: len(previous["messages"])]
            self.assertEqual(prefix, previous["messages"])

    def test_usage_and_duration_are_recorded(self) -> None:
        self.assertEqual(self.result.usage, token_usage(40, 20))
        self.assertGreater(self.result.duration_seconds, 0)

    def test_requests_use_research_settings(self) -> None:
        request = self.client.requests[0]
        self.assertEqual(request["model"], "claude-haiku-5-5")  # FR-225a.
        self.assertEqual(request["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(request["output_config"], {"effort": "medium"})
        self.assertNotIn("fallbacks", request)  # Haiku 5.5 has no refusal fallback.
        self.assertNotIn("betas", request)

    def test_trace_records_each_step(self) -> None:
        steps = self.result.steps
        self.assertEqual([s.number for s in steps], [1, 2, 3])
        self.assertIn("write the file first", steps[0].thought)
        self.assertEqual(steps[1].thought, "Check the output.")  # purpose fallback
        self.assertEqual(steps[2].thought, "Done.")
        (write,) = steps[0].actions
        self.assertEqual(
            write,
            ToolCall(
                tool="write_file",
                input={"path": "main.py", "content": "print('hi')\n",
                       "purpose": "Write the program."},
                output="Wrote 12 bytes to main.py",
                is_error=False,
            ),
        )
        (run,) = steps[1].actions
        self.assertEqual(json.loads(run.output)["stdout"], "hi\n")
        self.assertEqual(steps[2].actions, [])

    def test_on_step_receives_steps_with_actions_in_order(self) -> None:
        self.assertEqual([s.number for s in self.steps_seen], [1, 2])
        self.assertEqual(self.steps_seen[0].actions[0].tool, "write_file")

    def test_task_and_result_are_stored_in_memory(self) -> None:
        kinds = [kind for _, kind, _ in FakeMemory.records]
        self.assertEqual(kinds[0], "task")
        self.assertIn("observation", kinds)
        self.assertEqual(kinds[-1], "result")


class LimitAndFailureTests(AgentTestCase):
    def test_step_limit_ends_the_run_with_a_full_result(self) -> None:
        counter = iter(range(1000))

        def always_tool():
            n = next(counter)
            return response(
                [tool_use(f"t{n}", "write_file", {"path": "main.py", "content": f"print({n})\n",
                                                  "purpose": "Rewrite it."})],
                "tool_use",
            )

        client = FakeClient(always_tool)
        result = self.make_agent(client).run("Loop forever")
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(len(client.loop_requests), 3)
        self.assertEqual(result.steps_used, 3)
        self.assertEqual(result.execution_result.stdout, "1\n")
        # The finalize request answers the pending tool call before asking for the summary.
        closing = client.requests[-1]["messages"][-1]["content"]
        self.assertEqual(closing[0]["type"], "tool_result")
        self.assertEqual(closing[0]["tool_use_id"], "t2")
        self.assertTrue(closing[0]["is_error"])

    def test_refusal_stops_without_a_summary_call(self) -> None:
        details = SimpleNamespace(category="cyber", explanation="Declined.")
        client = FakeClient([response([], "refusal", stop_details=details)])
        result = self.make_agent(client).run("Something declined")
        self.assertEqual(result.status, "refused")
        self.assertIn("cyber", result.error)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result.code, "")
        self.assertIsNone(result.execution_result)

    def test_api_error_is_reported_not_raised(self) -> None:
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        response_401 = httpx2.Response(401, request=request)
        client = FakeClient([anthropic.AuthenticationError(
            "invalid x-api-key", response=response_401, body=None)])
        result = self.make_agent(client).run("Anything")
        self.assertEqual(result.status, "error")
        self.assertIn("invalid x-api-key", result.error)

    def test_empty_task_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.make_agent(FakeClient([])).run("   ")

    def test_finalize_runs_main_file_when_the_model_did_not(self) -> None:
        client = FakeClient(
            [
                response(
                    [tool_use("t1", "write_file", {"path": "main.py", "content": "print(42)\n",
                                                   "purpose": "Write it."})],
                    "tool_use",
                ),
                response([text_block("Wrote it.")], "end_turn"),
            ]
        )
        result = self.make_agent(client).run("Print 42")
        self.assertEqual(result.execution_result.stdout, "42\n")


def write_then_finish_script(path: str) -> list:
    return [
        response(
            [tool_use("w", "write_file", {"path": path, "content": "print('ok')\n",
                                          "purpose": "Write it."})], "tool_use"
        ),
        response([text_block("Done.")], "end_turn"),
    ]


class SharedWorkspaceTests(AgentTestCase):
    def test_runs_given_the_same_workspace_build_in_one_folder(self) -> None:
        shared = Path(self._tmp.name) / "shared"
        script = write_then_finish_script("a.py") + write_then_finish_script("b.py")
        agent = self.make_agent(FakeClient(script))

        first = agent.run("Write a.py", workspace=shared)
        second = agent.run("Write b.py", workspace=shared)

        self.assertEqual(first.workspace, str(shared.resolve()))
        self.assertEqual(second.workspace, str(shared.resolve()))
        self.assertTrue((shared / "a.py").is_file())
        self.assertTrue((shared / "b.py").is_file())
        self.assertEqual(first.files_written, ["a.py"])
        self.assertEqual(second.files_written, ["b.py"])

    def test_without_workspace_each_run_gets_its_own_folder(self) -> None:
        agent = self.make_agent(FakeClient(write_then_finish_script("a.py")))

        result = agent.run("Write a.py")

        self.assertEqual(Path(result.workspace).parent, self.config.workspace_root.resolve())
        self.assertEqual(Path(result.workspace).name, result.run_id)


class McpFileToolTests(AgentTestCase):
    def test_every_file_tool_call_goes_through_the_mcp_session(self) -> None:
        result = self.make_agent(FakeClient(happy_path_script())).run("Write main.py")
        session = FakeFileTools.sessions[-1]
        self.assertEqual(session.workspace, Path(result.workspace))
        self.assertEqual(session.protect, ("tests", ".qa"))
        self.assertTrue(session.closed)
        written = ("write_file", {"path": "main.py", "content": "print('hi')\n"})
        self.assertIn(written, session.calls)
        self.assertIn(("read_file", {"path": "main.py"}), session.calls)
        self.assertTrue(all("purpose" not in arguments for _, arguments in session.calls))

    def test_tool_list_is_mcp_tools_then_exec_python_and_never_changes(self) -> None:
        client = FakeClient(happy_path_script())
        self.make_agent(client).run("Write main.py")
        tool_lists = [request["tools"] for request in client.requests]
        self.assertEqual(
            [tool["name"] for tool in tool_lists[0]], ["read_file", "write_file", "exec_python"]
        )
        self.assertTrue(all(tools == tool_lists[0] for tools in tool_lists))
        required = [tool["input_schema"]["required"] for tool in tool_lists[0]]
        self.assertTrue(all("purpose" in keys for keys in required))

    def test_write_into_tests_comes_back_as_a_tool_error(self) -> None:
        script = [
            response(
                [tool_use("t1", "write_file", {"path": "tests/test_x.py", "content": "x",
                                               "purpose": "Edit a test."})],
                "tool_use",
            ),
            response([text_block("Done.")], "end_turn"),
        ]
        client = FakeClient(script)
        result = self.make_agent(client).run("Try to edit a test")
        tool_result = client.loop_requests[1]["messages"][-1]["content"][0]
        self.assertTrue(tool_result["is_error"])
        self.assertIn("Path is protected: tests/test_x.py", tool_result["content"])
        self.assertFalse((Path(result.workspace) / "tests" / "test_x.py").exists())

    def test_unavailable_mcp_server_ends_the_run_with_an_error(self) -> None:
        class BrokenFileTools(FakeFileTools):
            def __enter__(self):
                raise McpUnavailableError("server did not start")

        client = FakeClient(happy_path_script())
        result = self.make_agent(client, file_tools=BrokenFileTools).run("Write main.py")
        self.assertEqual(result.status, "error")
        self.assertIn("MCP file tools unavailable: server did not start", result.error)
        self.assertEqual(client.requests, [])


class SandboxRequiredTests(AgentTestCase):
    # These check the real "no Docker" refusal, so the sandbox stand-in must stay out.
    real_sandbox = True

    def test_agent_refuses_to_start_without_docker(self) -> None:
        with mock.patch("tools.python_exec.shutil.which", return_value=None):
            with self.assertRaises(SandboxUnavailableError):
                self.make_agent(FakeClient([]))

    def test_cli_exits_2_without_docker(self) -> None:
        stderr = io.StringIO()
        with mock.patch("tools.python_exec.shutil.which", return_value=None), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(coder_agent.main(["Write a.py"]), 2)
        self.assertIn("Docker is required", stderr.getvalue())


class ToolInputValidationTests(AgentTestCase):
    """A tool call whose input fails its ``TypedDict`` is never run (FR-219a)."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(coder_agent, "ensure_sandbox", lambda image: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = replace(self.config, max_steps=5)

    def test_invalid_and_unknown_tool_calls_are_errors_and_the_loop_goes_on(self) -> None:
        client = sc.ScriptedClaude([
            sc.tool_use_reply([("write_file", {"path": 3, "content": "x", "purpose": "p"})]),
            sc.tool_use_reply([("exec_python", {"purpose": "Run it."})]),
            sc.tool_use_reply([("rm_rf", {"path": "/", "purpose": "Clean up."})]),
            sc.text_reply("Done."),
            sc.text_reply(SUMMARY),
        ])
        result = self.make_agent(client).run("Write main.py")

        results = [
            block for request in client.requests for message in request["messages"]
            if message["role"] == "user" and isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "tool_result"
        ]
        texts = list(dict.fromkeys(block["content"] for block in results))
        self.assertTrue(all(block["is_error"] for block in results))
        self.assertEqual(len(texts), 3)
        self.assertTrue(texts[0].startswith("Invalid tool input: $.path: expected str"))
        self.assertTrue(texts[1].startswith("Invalid tool input: $: missing required key"))
        self.assertEqual(texts[2], "Unknown tool: rm_rf")
        writes = [call for session in FakeFileTools.sessions for call in session.calls
                  if call[0] == "write_file"]
        self.assertEqual(writes, [])
        self.assertEqual(result.status, "completed")
        self.assertEqual(client.remaining, 0)
        recorded = [action for step in result.steps for action in step.actions]
        self.assertTrue(all(action.is_error for action in recorded))


class FullyMockedTests(unittest.TestCase):
    """The Coder's unit tests need no Docker: none of them is skipped without it."""

    def test_nothing_in_this_module_skips_without_docker(self) -> None:
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite(
            loader.loadTestsFromTestCase(case) for case in (
                HappyPathTests, LimitAndFailureTests, SharedWorkspaceTests,
                McpFileToolTests, ToolInputValidationTests,
            )
        )
        result = unittest.TestResult()
        with mock.patch("shutil.which", return_value=None):
            suite.run(result)
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.errors + result.failures, [])
        self.assertGreater(result.testsRun, 15)


class ConfigTests(unittest.TestCase):
    def test_env_overrides_and_validation(self) -> None:
        env = {"CODER_MAX_STEPS": "4", "CODER_EFFORT": "high", "CODER_USE_FALLBACKS": "false"}
        with mock.patch.dict("os.environ", env):
            config = CoderConfig.from_env()
        self.assertEqual(
            (config.max_steps, config.effort, config.use_fallbacks), (4, "high", False)
        )
        invalid = (
            {"CODER_MAX_STEPS": "11"},
            {"CODER_EFFORT": "huge"},
            {"CODER_EXEC_TIMEOUT": "0"},
        )
        for bad in invalid:
            with self.subTest(env=bad), mock.patch.dict("os.environ", bad):
                with self.assertRaises(ValueError):
                    CoderConfig.from_env()


class CliTests(unittest.TestCase):
    def test_prints_json_and_one_progress_line_per_step(self) -> None:
        steps = [
            Step(1, "t", [ToolCall("write_file", {"path": "a.py", "content": "x"}, "ok", False)]),
            Step(
                2,
                "t",
                [
                    ToolCall("exec_python", {"path": "a.py"}, "{}", False),
                    ToolCall("read_file", {"path": "b.txt"}, "b", False),
                ],
            ),
        ]

        class StubAgent:
            def __init__(self, config, on_step=None) -> None:
                self.on_step = on_step

            def run(self, task: str) -> CoderResult:
                for step in steps:
                    self.on_step(step)
                return CoderResult(code="x", explanation="e", plan=["p"], execution_result=None,
                                   status="completed", steps=steps, workspace="/tmp/ws")

            def reflect(self, brief: str, workspace: Path) -> ReflectionResult:
                revised = CoderResult(code="y", explanation="revised", plan=[],
                                      execution_result=None, status="completed")
                rounds = [{"round": 1, "critique": [{"item_id": "C1"}]}]
                return ReflectionResult(rounds=rounds, result=revised, status="completed",
                                        usage={"input_tokens": 1, "output_tokens": 1})

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(coder_agent, "CoderAgent", StubAgent), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = coder_agent.main(["Write a.py"])
        self.assertEqual(exit_code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["explanation"], "revised")
        self.assertEqual(output["first_draft"]["explanation"], "e")
        self.assertEqual(output["reflection"][0]["critique"][0]["item_id"], "C1")
        self.assertEqual(
            stderr.getvalue().splitlines(),
            ["[step 1] write_file a.py", "[step 2] exec_python a.py, read_file b.txt"],
        )


if __name__ == "__main__":
    unittest.main()
