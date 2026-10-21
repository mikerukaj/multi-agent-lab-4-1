"""Signals, exit codes and container hygiene (FR-201a, FR-210, FR-211, FR-230b)."""

import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from orchestration.checkpoint import RunLock, read_final_record, run_dir
from orchestration.services import RunServices
from agents.qa_agent import build_report, render_report
from orchestration.team import exit_code_for, install_signal_handlers, main
from tests import TEST_ENV
from tools import python_exec
from tools.python_exec import cleanup_stale_containers

REPO_ROOT = Path(__file__).resolve().parents[1]
requires_docker = unittest.skipUnless(shutil.which("docker"), "Docker is not available")


class ExitCodeTests(unittest.TestCase):
    def test_exit_codes_follow_the_contract(self) -> None:
        cases = [
            ({"status": "completed", "end_reason": "complete"}, None, 0),
            ({"status": "failed", "end_reason": "iteration_limit"}, None, 1),
            ({"status": "error", "end_reason": "pm_error"}, None, 1),
            ({"status": "rejected", "end_reason": "rejected"}, None, 1),
            ({"status": "error", "end_reason": "qa_failed"}, None, 1),
            ({"status": "failed", "end_reason": "dependency_unavailable"}, None, 3),
            ({"status": "error", "end_reason": "a2a_failed"}, None, 3),
            ({"status": "failed", "end_reason": "budget_exceeded"}, None, 3),
            ({"status": "failed", "end_reason": "interrupted"}, signal.SIGINT, 130),
            ({"status": "failed", "end_reason": "interrupted"}, signal.SIGTERM, 143),
        ]
        for state, signum, code in cases:
            with self.subTest(end_reason=state["end_reason"], signum=signum):
                self.assertEqual(exit_code_for(state, signum), code)

    def test_usage_errors_exit_2(self) -> None:
        for argv in (["--resume", "r1", "a requirement"], ["--resume", "r1", "--max-steps", "3"],
                     []):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(argv)
                self.assertEqual(caught.exception.code, 2)


class PartialReportTests(unittest.TestCase):
    def test_the_report_of_an_interrupted_run_says_it_is_partial(self) -> None:
        iteration = {"number": 1, "findings": [], "verdict": "fix_needed",
                     "test_run": {"passed": 1, "failed": 0, "errors": 0}}
        request = {"run_id": "r", "workspace": "/w", "status": "failed",
                   "end_reason": "interrupted", "max_iterations": 5, "iterations": [iteration]}
        text = render_report(build_report(request))
        self.assertIn("This report is partial: the run ended interrupted", text)
        complete = {**request, "status": "completed", "end_reason": "complete"}
        self.assertNotIn("partial", render_report(build_report(complete)))


class SignalHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = RunServices.create("run-sig", env=TEST_ENV)
        restore = install_signal_handlers(self.services)
        self.addCleanup(restore)
        self.handler = signal.getsignal(signal.SIGTERM)

    def test_the_first_signal_requests_a_stop(self) -> None:
        self.handler(signal.SIGTERM, None)
        self.assertTrue(self.services.stop_requested.is_set())
        self.assertEqual(self.services.signal_received, signal.SIGTERM)

    def test_a_second_signal_exits_at_once(self) -> None:
        self.handler(signal.SIGINT, None)
        stderr = io.StringIO()
        with mock.patch.object(os, "_exit") as exit_now, \
                mock.patch("orchestration.team.kill_run_containers") as kill, \
                redirect_stderr(stderr):
            self.handler(signal.SIGTERM, None)
        kill.assert_called_once_with("run-sig")
        exit_now.assert_called_once_with(143)
        self.assertIn("interrupted again: exiting now", stderr.getvalue())


class StaleContainerTests(unittest.TestCase):
    def test_only_containers_of_runs_that_are_not_going_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listed = subprocess.CompletedProcess(
                [], 0, "aaa run-live\nbbb run-dead\nccc \n", ""
            )
            calls = []

            def docker(args, timeout=30):
                calls.append(args)
                return listed if args[0] == "ps" else subprocess.CompletedProcess([], 0, "", "")

            with RunLock(run_dir(root, "run-live")), \
                    mock.patch.object(python_exec, "_docker", docker), \
                    mock.patch.object(python_exec.shutil, "which", lambda name: "/bin/docker"):
                removed = cleanup_stale_containers(root)
        self.assertEqual(removed, ["bbb"])
        self.assertEqual(calls[-1], ["rm", "--force", "bbb"])

    def test_without_docker_there_is_nothing_to_clean(self) -> None:
        with mock.patch.object(python_exec.shutil, "which", lambda name: None):
            self.assertEqual(cleanup_stale_containers(Path("/nowhere")), [])
            self.assertEqual(python_exec.kill_run_containers("run-x"), [])


class SigtermTests(unittest.TestCase):
    """A real SIGTERM to a running team process ends it cleanly (US1 scenario 5)."""

    def run_and_terminate(self, workspace: Path) -> tuple[int, float]:
        env = {**os.environ, "CODER_WORKSPACE": str(workspace), "PYTHONPATH": str(REPO_ROOT)}
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.fakes.slow_team_main", "Build a greeter"],
            cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        runs = workspace / ".runs"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not list(runs.glob("*/checkpoint.json")):
            time.sleep(0.1)
        time.sleep(1.5)  # Into the run's first model calls.
        sent = time.monotonic()
        process.send_signal(signal.SIGTERM)
        code = process.wait(60)
        process.stderr.close()
        return code, time.monotonic() - sent

    def test_sigterm_exits_143_within_30_seconds_with_a_final_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            code, seconds = self.run_and_terminate(workspace)
            self.assertEqual(code, 143)
            self.assertLess(seconds, 30)
            record_dir = next((workspace / ".runs").iterdir())
            record = read_final_record(record_dir)
            self.assertEqual(record["end_reason"], "interrupted")

    @requires_docker
    def test_no_labelled_container_is_left_behind(self) -> None:
        listed = subprocess.run(
            ["docker", "ps", "-a", "-q", "--filter", f"label={python_exec.RUN_ID_LABEL}"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(listed.stdout.split(), [])


if __name__ == "__main__":
    unittest.main()
