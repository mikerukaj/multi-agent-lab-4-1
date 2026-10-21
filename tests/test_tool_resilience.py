"""Tool calls survive a dead MCP server and a sandbox Docker fails to start (FR-206, FR-207).

The MCP cases use the real file-tool server; it is killed with ``os.kill`` from outside,
never through a test-only hook in production code (FR-239). The sandbox cases replace
``run_container`` (the one place a ``docker run`` process starts), so they need no
Docker.
"""

import json
import os
import random
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agents.qa_agent import QAAgent, QAConfig
from resilience.circuit import CircuitBreaker, DependencyGuard
from resilience.retry import RetryPolicy
from tests.team_fakes import FakeFileTools
from tests.test_qa_agent import FakeClient, write_tests_reply
from tools import dispatch_tool, python_exec, test_runner
from tools.mcp_client import McpFileTools
from tools.python_exec import OWNER_LABEL, RUN_ID_LABEL, exec_python, locked_down_run

DAEMON_ERROR = b"docker: Error response from daemon: Cannot connect to the Docker daemon."
proc_available = unittest.skipUnless(Path("/proc/self/cmdline").exists(), "needs /proc")


def server_pids(workspace: Path) -> list[int]:
    """PIDs of the MCP file-tool servers serving ``workspace`` (found through /proc)."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"tools.mcp_server" in argv and str(workspace).encode() in argv:
            pids.append(int(entry.name))
    return pids


def kill_servers(workspace: Path) -> None:
    for pid in server_pids(workspace):
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while server_pids(workspace) and time.monotonic() < deadline:
        time.sleep(0.05)


def make_guard(dependency: str, events: list) -> DependencyGuard:
    return DependencyGuard(
        dependency, "target", CircuitBreaker(f"{dependency}:target"), RetryPolicy(),
        random.Random(0), lambda delay: None, threading.Event(), events.append,
    )


def docker_run(code, stdout=b"", stderr=b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["docker", "run"], code, stdout, stderr)


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name).resolve() / "ws"
        (self.workspace / "tests").mkdir(parents=True)
        (self.workspace / "main.py").write_text("print('hi')\n", encoding="utf-8")
        self.events: list[dict] = []


@proc_available
class McpRestartTests(WorkspaceTestCase):
    def test_a_killed_server_is_restarted_with_the_same_protections(self) -> None:
        guard = make_guard("mcp", self.events)
        with McpFileTools(self.workspace, protect=("tests",), guard=guard) as files:
            self.assertEqual(files.call("write_file", {"path": "a.txt", "content": "x"})[1],
                             False)
            kill_servers(self.workspace)
            text, is_error = files.call("read_file", {"path": "a.txt"})
            self.assertEqual((text, is_error), ("x", False))
            self.assertEqual(files.restarts, 1)
            self.assertIn("restart", [event["event"] for event in self.events])
            text, is_error = files.call("write_file", {"path": "tests/t.py", "content": "x"})
            self.assertTrue(is_error)
            self.assertIn("protected", text)

    def test_a_server_that_keeps_dying_gives_a_structured_failure(self) -> None:
        guard = make_guard("mcp", self.events)

        class DyingFileTools(McpFileTools):
            def _call_once(self, name, arguments):
                kill_servers(self.workspace)
                return super()._call_once(name, arguments)

        with DyingFileTools(self.workspace, guard=guard) as files:
            text, is_error = files.call("read_file", {"path": "main.py"})
        self.assertTrue(is_error)
        failure = json.loads(text)
        self.assertEqual((failure["dependency"], failure["kind"]), ("mcp", "temporary"))
        self.assertEqual(failure["attempts"], 4)
        self.assertEqual(files.restarts, 3)


class SandboxRetryTests(WorkspaceTestCase):
    def run_exec(self, outcomes: list) -> tuple:
        guard = make_guard("sandbox", self.events)
        with mock.patch.object(python_exec, "run_container", side_effect=outcomes) as run:
            result = exec_python(self.workspace, "main.py", 10, guard=guard, run_id="r1")
        return result, run

    def test_docker_start_failures_are_retried_until_the_run_starts(self) -> None:
        result, run = self.run_exec([docker_run(125, stderr=DAEMON_ERROR)] * 2
                                    + [docker_run(0, b"hi\n")])
        self.assertEqual((result.exit_code, result.stdout), (0, "hi\n"))
        self.assertIsNone(result.failure)
        self.assertEqual(run.call_count, 3)
        names = [call.args[1] for call in run.call_args_list]
        self.assertEqual(len(set(names)), 3)  # A fresh container name per attempt.

    def test_the_generated_codes_own_failure_is_returned_as_is(self) -> None:
        result, run = self.run_exec([docker_run(1, b"", b"Traceback ...")])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(run.call_count, 1)

    def test_a_timeout_is_not_retried(self) -> None:
        result, run = self.run_exec([docker_run(None)])
        self.assertTrue(result.timed_out)
        self.assertEqual(run.call_count, 1)

    def test_a_sandbox_that_never_starts_gives_a_failure_and_a_tool_error(self) -> None:
        result, _ = self.run_exec([docker_run(125, stderr=DAEMON_ERROR)] * 4)
        self.assertIsNone(result.exit_code)
        self.assertEqual((result.failure["dependency"], result.failure["attempts"]),
                         ("sandbox", 4))
        guard = make_guard("sandbox", [])
        with mock.patch.object(python_exec, "run_container",
                               side_effect=[docker_run(125, stderr=DAEMON_ERROR)] * 4):
            text, is_error, execution = dispatch_tool(
                "exec_python", {"path": "main.py"}, self.workspace, 10, guard=guard
            )
        self.assertTrue(is_error)
        self.assertIsNone(execution)
        self.assertEqual(json.loads(text)["dependency"], "sandbox")

    def test_every_container_is_labelled_with_its_run_and_owner(self) -> None:
        command = locked_down_run("n", self.workspace, run_id="20261020-run")
        labels = [command[i + 1] for i, flag in enumerate(command) if flag == "--label"]
        self.assertIn(f"{RUN_ID_LABEL}=20261020-run", labels)
        owner = next(label for label in labels if label.startswith(OWNER_LABEL))
        self.assertTrue(owner.endswith(f":{os.getpid()}"))

    def test_pytest_runs_are_retried_the_same_way(self) -> None:
        guard = make_guard("sandbox", self.events)
        outcomes = [docker_run(125, stderr=DAEMON_ERROR), docker_run(5, b"no tests ran")]
        with mock.patch.object(test_runner, "run_container", side_effect=outcomes) as run:
            test_run = test_runner.run_pytest(self.workspace, 10, guard=guard)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(test_run["exit_code"], 5)

    def test_qa_turns_an_unusable_sandbox_into_a_failure(self) -> None:
        def run_tests(workspace, timeout_seconds, image, **kwargs):
            with mock.patch.object(test_runner, "run_container",
                                   side_effect=[docker_run(125, stderr=DAEMON_ERROR)] * 4):
                return test_runner.run_pytest(workspace, timeout_seconds, image,
                                              guard=make_guard("sandbox", []))

        FakeFileTools.reset()
        agent = QAAgent(QAConfig(), client=FakeClient([write_tests_reply()]),
                        run_tests=run_tests, file_tools=FakeFileTools)
        result = agent.run_iteration({
            "iteration": 1, "workspace": str(self.workspace),
            "spec": {"name": "n", "description": "d", "assumptions": [], "tasks": [],
                     "acceptance_criteria": ["add(1, 2) returns 3"]},
            "source_files": [], "coder_notes": [], "main_file": None, "test_suite": {},
            "previous_findings": [], "responses": [],
        })
        self.assertIn("Sandbox unavailable", result.error)
        self.assertEqual(result.failure["dependency"], "sandbox")


if __name__ == "__main__":
    unittest.main()
