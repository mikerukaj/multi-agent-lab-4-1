"""Tests for the sandboxed pytest runner (contracts/qa-agent-api.md, research R7)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.python_exec import SANDBOX_IMAGE, ensure_sandbox
from tools import test_runner
from tools.test_runner import MAX_XML_BYTES, parse_junit, pytest_command, run_pytest

requires_docker = unittest.skipUnless(shutil.which("docker"), "Docker is not available")

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4">
<testcase classname="tests.test_calc" name="test_add" time="0.001"/>
<testcase classname="tests.test_calc" name="test_sub" time="0.001">
<failure message="assert 1 == 2">{details}</failure></testcase>
<testcase classname="tests.test_calc.TestDiv" name="test_zero" time="0.001">
<error message="ZeroDivisionError: division by zero">Traceback</error></testcase>
<testcase classname="tests.test_other" name="test_skip"><skipped message="not now"/></testcase>
</testsuite></testsuites>"""

TEST_FILES = ["tests/test_calc.py", "tests/test_other.py"]


class ParseJunitTests(unittest.TestCase):
    def test_maps_every_outcome_to_a_case(self) -> None:
        cases = parse_junit(JUNIT.format(details="Traceback"), TEST_FILES)
        self.assertEqual(
            [(case["test_id"], case["outcome"]) for case in cases],
            [
                ("tests/test_calc.py::test_add", "passed"),
                ("tests/test_calc.py::test_sub", "failed"),
                ("tests/test_calc.py::TestDiv::test_zero", "error"),
                ("tests/test_other.py::test_skip", "skipped"),
            ],
        )
        self.assertEqual(cases[1]["file"], "tests/test_calc.py")
        self.assertEqual(cases[1]["message"], "assert 1 == 2")
        self.assertEqual(cases[0]["message"], "")

    def test_details_are_cut_to_2000_characters(self) -> None:
        cases = parse_junit(JUNIT.format(details="x" * 5000), TEST_FILES)
        self.assertEqual(len(cases[1]["details"]), 2000)
        self.assertTrue(cases[1]["details"].endswith("[truncated]"))


class FakePopen:
    """Stands in for the ``docker run`` process: writes a JUnit file, then exits."""

    script: dict = {}

    def __init__(self, command, **kwargs) -> None:
        self.command = command
        volume = next(arg for arg in command if str(arg).endswith(":/qa-out:rw"))
        self.out_dir = Path(volume.rsplit(":/qa-out:rw", 1)[0])
        self.returncode = None
        self._calls = 0

    def communicate(self, timeout=None):
        self._calls += 1
        script = FakePopen.script
        if script.get("timeout") and self._calls == 1:
            raise subprocess.TimeoutExpired(self.command, timeout)
        if script.get("xml") is not None:
            (self.out_dir / "junit.xml").write_text(script["xml"], encoding="utf-8")
        self.returncode = script.get("exit_code", 0)
        return b"pytest output", b""


class RunPytestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "ws"
        (self.workspace / "tests").mkdir(parents=True)
        for name in ("test_calc.py", "test_other.py"):
            (self.workspace / "tests" / name).write_text("def test_x(): pass\n", encoding="utf-8")
        for target in ("tools.python_exec.subprocess.Popen", "tools.python_exec.kill_container"):
            patcher = mock.patch(target, FakePopen if "Popen" in target else mock.Mock())
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_with(self, **script):
        FakePopen.script = script
        return run_pytest(self.workspace, timeout_seconds=5)

    def test_qa_output_root_holds_the_junit_folder(self) -> None:
        root = Path(self._tmp.name) / "host" / "qa-out"
        seen = []
        original = test_runner.tempfile.TemporaryDirectory

        def recording(*args, **kwargs):
            seen.append(kwargs.get("dir"))
            return original(*args, **kwargs)

        with mock.patch.dict("os.environ", {"QA_OUTPUT_ROOT": str(root)}), \
                mock.patch.object(test_runner.tempfile, "TemporaryDirectory", recording):
            self.run_with(xml=JUNIT.format(details="tb"), exit_code=1)
        self.assertEqual(seen, [str(root)])
        self.assertTrue(root.is_dir())

    def test_exit_0_and_1_are_loaded(self) -> None:
        xml = JUNIT.format(details="tb")
        for code in (0, 1):
            with self.subTest(code=code):
                run = self.run_with(xml=xml, exit_code=code)
                self.assertTrue(run["loaded"])
                self.assertEqual(
                    (run["passed"], run["failed"], run["errors"], run["skipped"]), (1, 1, 1, 1)
                )
                self.assertEqual(run["output"], "pytest output")

    def test_exit_2_to_5_are_not_loaded(self) -> None:
        for code in (2, 3, 4, 5):
            with self.subTest(code=code):
                run = self.run_with(xml=JUNIT.format(details=""), exit_code=code)
                self.assertFalse(run["loaded"])

    def test_missing_oversized_or_incomplete_xml_is_not_loaded(self) -> None:
        self.assertFalse(self.run_with(xml=None, exit_code=0)["loaded"])
        self.assertFalse(self.run_with(xml="<" * (MAX_XML_BYTES + 1), exit_code=0)["loaded"])
        self.assertFalse(self.run_with(xml="<not xml", exit_code=0)["loaded"])
        (self.workspace / "tests" / "test_empty.py").write_text("x = 1\n", encoding="utf-8")
        self.assertFalse(self.run_with(xml=JUNIT.format(details=""), exit_code=0)["loaded"])

    def test_timeout_is_a_loaded_failure(self) -> None:
        run = self.run_with(xml=None, timeout=True)
        self.assertTrue(run["timed_out"])
        self.assertTrue(run["loaded"])
        self.assertIsNone(run["exit_code"])
        self.assertEqual(run["failed"], 1)


class PytestCommandTests(unittest.TestCase):
    def test_command_is_locked_down(self) -> None:
        workspace = Path(tempfile.gettempdir()) / "ws"
        out_dir = Path(tempfile.gettempdir()) / "out"
        command = pytest_command(workspace, out_dir, "qa-1", SANDBOX_IMAGE)
        joined = " ".join(command)
        for flag in (
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            f"--user {os.getuid()}:{os.getgid()}",
            f"--volume {workspace.resolve()}:/work:rw",
            f"--volume {workspace.resolve()}/tests:/work/tests:ro",
            f"--volume {out_dir.resolve()}:/qa-out:rw",
            "-c /dev/null",
            "--confcutdir=/work/tests",
            "--junitxml=/qa-out/junit.xml",
            "-p no:cacheprovider",
        ):
            self.assertIn(flag, joined)
        env = [command[i + 1].split("=")[0] for i, arg in enumerate(command) if arg == "--env"]
        self.assertEqual(sorted(env), ["HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING"])
        self.assertNotIn("ANTHROPIC", joined)


@requires_docker
class RealSandboxTests(unittest.TestCase):
    def test_runs_real_pytest_in_the_sandbox(self) -> None:
        ensure_sandbox()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = "def add(a, b):\n    return a + b\n"
            (workspace / "calc.py").write_text(source, encoding="utf-8")
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_calc.py").write_text(
                "from calc import add\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_wrong():\n    assert add(1, 1) == 3\n",
                encoding="utf-8",
            )
            run = run_pytest(workspace, timeout_seconds=60)
        self.assertTrue(run["loaded"], run["output"])
        self.assertEqual((run["passed"], run["failed"]), (1, 1))
        failed = [case for case in run["cases"] if case["outcome"] == "failed"]
        self.assertEqual(failed[0]["test_id"], "tests/test_calc.py::test_wrong")


if __name__ == "__main__":
    unittest.main()
