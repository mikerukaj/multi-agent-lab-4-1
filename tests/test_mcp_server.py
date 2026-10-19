"""Tests for the MCP file-tool server and client (contracts/mcp-file-server.md).

Every test talks to the real server subprocess over stdio through ``McpFileTools``;
no network, Docker or credentials are needed.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.file_tools import READ_FILE_TOOL, WRITE_FILE_TOOL
from tools.mcp_client import REPO_ROOT, McpFileTools, McpUnavailableError


class McpTestCase(unittest.TestCase):
    """Gives each test a fresh, empty workspace directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class SmokeTests(McpTestCase):
    def test_lists_exactly_read_file_and_write_file(self) -> None:
        with McpFileTools(self.workspace) as tools:
            names = [tool["name"] for tool in tools.definitions()]
            self.assertEqual(names, ["read_file", "write_file"])

    def test_write_then_read_round_trip(self) -> None:
        with McpFileTools(self.workspace) as tools:
            text, is_error = tools.call("write_file", {"path": "pkg/a.txt", "content": "héllo\n"})
            self.assertFalse(is_error, text)
            self.assertEqual(tools.call("read_file", {"path": "pkg/a.txt"}), ("héllo\n", False))
        self.assertEqual((self.workspace / "pkg" / "a.txt").read_text(encoding="utf-8"), "héllo\n")

    def test_protected_folder_refuses_writes_but_allows_reads(self) -> None:
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests" / "test_a.py").write_text("ORIGINAL", encoding="utf-8")
        with McpFileTools(self.workspace, protect=("tests",)) as tools:
            text, is_error = tools.call("write_file", {"path": "tests/a.py", "content": "x"})
            self.assertTrue(is_error)
            self.assertEqual(
                text, "Path is protected: tests/a.py (only the QA Agent may change tests)"
            )
            self.assertEqual(
                tools.call("read_file", {"path": "tests/test_a.py"}), ("ORIGINAL", False)
            )
        self.assertFalse((self.workspace / "tests" / "a.py").exists())

    @unittest.skipUnless(shutil.which("pgrep"), "pgrep is not available")
    def test_exit_stops_the_server_process(self) -> None:
        marker = str(self.workspace)
        with McpFileTools(self.workspace):
            self.assertEqual(_server_processes(marker), 1)
        self.assertEqual(_server_processes(marker), 0)


class ContractTests(McpTestCase):
    """Every row of the call-behavior table in contracts/mcp-file-server.md."""

    def setUp(self) -> None:
        super().setUp()
        self.tools = McpFileTools(self.workspace, protect=("tests",)).__enter__()
        self.addCleanup(self.tools.close)

    def assert_error(self, name: str, arguments, expected: str) -> None:
        text, is_error = self.tools.call(name, arguments)
        self.assertTrue(is_error, text)
        self.assertEqual(text, expected)
        # The server keeps serving after every error.
        _, still_failing = self.tools.call("write_file", {"path": "ok.txt", "content": "ok"})
        self.assertFalse(still_failing)

    def test_listed_schemas_are_the_existing_strict_schemas(self) -> None:
        definitions = {tool["name"]: tool for tool in self.tools.definitions()}
        self.assertEqual(definitions["read_file"]["input_schema"], READ_FILE_TOOL["input_schema"])
        self.assertEqual(
            definitions["write_file"]["input_schema"], WRITE_FILE_TOOL["input_schema"]
        )
        self.assertTrue(all(tool["strict"] for tool in definitions.values()))

    def test_large_file_is_truncated(self) -> None:
        (self.workspace / "big.txt").write_text("x" * 250_000, encoding="utf-8")
        text, is_error = self.tools.call("read_file", {"path": "big.txt"})
        self.assertFalse(is_error)
        self.assertTrue(text.endswith("[truncated: file is 250000 bytes]"))

    def test_path_errors(self) -> None:
        outside = Path(self._tmp.name) / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.workspace / "link.txt").symlink_to(outside)
        self.assert_error("read_file", {"path": "nope.txt"}, "File not found: nope.txt")
        for path in ("/etc/passwd", "../secret.txt", "link.txt"):
            with self.subTest(path=path):
                self.assert_error(
                    "read_file", {"path": path}, f"Path is outside the workspace: {path}"
                )
        self.assert_error(
            "write_file", {"path": "../evil.txt", "content": "x"},
            "Path is outside the workspace: ../evil.txt",
        )
        self.assertFalse((Path(self._tmp.name) / "evil.txt").exists())

    def test_protected_write_and_malformed_calls(self) -> None:
        self.assert_error(
            "write_file", {"path": "tests/test_x.py", "content": "x"},
            "Path is protected: tests/test_x.py (only the QA Agent may change tests)",
        )
        prefix = "Invalid arguments for read_file: "
        self.assert_error("read_file", {}, prefix + "missing argument 'path'")
        self.assert_error("read_file", {"path": 7}, prefix + "argument 'path' must be a string")
        self.assert_error(
            "write_file", {"path": "a.txt", "content": "x", "mode": "w"},
            "Invalid arguments for write_file: unexpected argument 'mode'",
        )
        self.assert_error("delete_file", {"path": "a.txt"}, "Unknown tool: delete_file")


class StartupTests(McpTestCase):
    def run_server(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "tools.mcp_server", *args],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=30, stdin=subprocess.DEVNULL,
            check=False,
        )

    def test_missing_workspace_or_bad_protect_refuses_to_start(self) -> None:
        missing = self.workspace / "missing"
        result = self.run_server("--workspace", str(missing))
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"error: workspace does not exist: {missing}", result.stderr)
        self.assertEqual(result.stdout, "")
        for prefix in ("/abs", "../x"):
            with self.subTest(prefix=prefix):
                result = self.run_server("--workspace", str(self.workspace), "--protect", prefix)
                self.assertEqual(result.returncode, 2)
                self.assertIn("protected path is outside the workspace", result.stderr)

    def test_client_reports_an_unavailable_server(self) -> None:
        with self.assertRaisesRegex(McpUnavailableError, "workspace does not exist"):
            McpFileTools(self.workspace / "missing").__enter__()

    def test_server_environment_holds_no_secrets(self) -> None:
        parameters = McpFileTools(self.workspace, protect=("tests", ".qa")).server_parameters()
        self.assertEqual(set(parameters.env), {"PATH", "PYTHONIOENCODING"})
        self.assertEqual(parameters.command, sys.executable)
        self.assertEqual(
            parameters.args[-4:], ["--protect", "tests", "--protect", ".qa"]
        )


def _server_processes(workspace: str) -> int:
    found = subprocess.run(
        ["pgrep", "-f", f"tools.mcp_server --workspace {workspace}"],
        capture_output=True, text=True, check=False,
    )
    return len(found.stdout.split())


if __name__ == "__main__":
    unittest.main()
