"""Tests for ``orchestration.health`` (FR-231, contracts/cli.md, data-model §2.6)."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.coder_agent import CoderConfig
from agents.pm_agent import PMConfig
from agents.qa_agent import QAConfig
from orchestration import health
from orchestration.health import check_health, failing_items

ENV = {"ANTHROPIC_API_KEY": "sk-ant-api03-healthtest000000000000"}


def completed(code: int = 0, stdout: str = "26.1.3", stderr: str = ""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class HealthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.coder = CoderConfig(workspace_root=root / "workspace", chroma_path=root / "chroma")
        self.env = {**ENV, "COST_REPORT_DIR": str(root / "costs")}
        self.docker = mock.Mock(return_value=completed())
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(health.shutil, "which", lambda name: "/bin/docker"))
        stack.enter_context(mock.patch.object(health.subprocess, "run", self.docker))
        # No check may ever build an Anthropic client (no paid model call).
        stack.enter_context(mock.patch("anthropic.Anthropic",
                                       side_effect=AssertionError("no model client")))

    def report(self, env=None, coder=None) -> dict:
        return check_health(env if env is not None else self.env, coder or self.coder,
                            QAConfig(), PMConfig())

    def item(self, report: dict, name: str) -> dict:
        return next(item for item in report["items"] if item["name"] == name)


class HealthItemTests(HealthTestCase):
    def test_every_item_ok_means_ok(self) -> None:
        report = self.report()
        self.assertTrue(report["ok"], failing_items(report))
        self.assertEqual([item["name"] for item in report["items"]],
                         ["credentials", "docker", "sandbox_image", "workspace",
                          "cost_report_dir", "chroma", "price_table", "langsmith"])
        self.assertEqual(self.item(report, "langsmith")["status"], "info")

    def test_missing_credentials(self) -> None:
        report = self.report(env={"COST_REPORT_DIR": self.env["COST_REPORT_DIR"]})
        self.assertFalse(report["ok"])
        self.assertEqual(self.item(report, "credentials")["status"], "missing")
        self.assertIn("ANTHROPIC_API_KEY", self.item(report, "credentials")["message"])

    def test_an_unreachable_docker_engine(self) -> None:
        self.docker.return_value = completed(1, "", "Cannot connect to the Docker daemon")
        report = self.report()
        self.assertEqual(self.item(report, "docker")["status"], "error")
        self.assertIn("Cannot connect", self.item(report, "docker")["message"])

    def test_a_sandbox_image_that_is_missing_and_cannot_be_built(self) -> None:
        self.docker.return_value = completed(1, "", "No such image")
        qa = QAConfig(sandbox_image="someone/else:1")
        report = check_health(self.env, self.coder, qa, PMConfig())
        self.assertEqual(self.item(report, "sandbox_image")["status"], "missing")
        buildable = self.report()  # The project's own image can be built.
        self.assertEqual(self.item(buildable, "sandbox_image")["status"], "ok")

    def test_unwritable_workspace_and_cost_report_folder(self) -> None:
        blocked = Path(self._tmp.name) / "file"
        blocked.write_text("x", encoding="utf-8")
        coder = CoderConfig(workspace_root=blocked / "ws", chroma_path=self.coder.chroma_path)
        report = self.report(env={**self.env, "COST_REPORT_DIR": str(blocked / "costs")},
                             coder=coder)
        self.assertEqual(self.item(report, "workspace")["status"], "error")
        self.assertEqual(self.item(report, "cost_report_dir")["status"], "error")

    def test_chroma_server_heartbeat_and_embedded_path(self) -> None:
        server = CoderConfig(workspace_root=self.coder.workspace_root, chroma_host="chroma")
        with mock.patch.object(health.urllib.request, "urlopen",
                               side_effect=OSError("Connection refused")):
            report = self.report(coder=server)
        self.assertEqual(self.item(report, "chroma")["status"], "error")
        self.assertIn("/api/v2/heartbeat", self.item(report, "chroma")["message"])
        blocked = Path(self._tmp.name) / "file"
        blocked.write_text("x", encoding="utf-8")
        embedded = CoderConfig(workspace_root=self.coder.workspace_root,
                               chroma_path=blocked / "chroma")
        self.assertEqual(self.item(self.report(coder=embedded), "chroma")["status"], "error")

    def test_an_unpriced_model_blocks_only_a_run_with_a_budget(self) -> None:
        coder = CoderConfig(workspace_root=self.coder.workspace_root,
                            chroma_path=self.coder.chroma_path, model="claude-future-9")
        report = self.report(coder=coder)
        self.assertEqual(self.item(report, "price_table")["status"], "info")
        self.assertTrue(report["ok"])
        budgeted = self.report(env={**self.env, "RUN_BUDGET_USD": "2"}, coder=coder)
        self.assertEqual(self.item(budgeted, "price_table")["status"], "missing")
        self.assertIn("claude-future-9", self.item(budgeted, "price_table")["message"])


class HealthCliTests(HealthTestCase):
    def run_cli(self, argv: list[str], env: dict) -> tuple[int, str]:
        stdout = io.StringIO()
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(health, "load_dotenv"), \
                contextlib.redirect_stdout(stdout):
            code = health.main(argv)
        return code, stdout.getvalue()

    def test_json_output_is_a_health_report_and_the_exit_code_follows_it(self) -> None:
        env = {**self.env, "CODER_WORKSPACE": str(self.coder.workspace_root),
               "CODER_CHROMA_PATH": str(self.coder.chroma_path)}
        code, out = self.run_cli(["--json"], env)
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(set(report), {"ok", "items"})
        self.assertTrue(all(set(item) == {"name", "status", "message"}
                            for item in report["items"]))
        code, out = self.run_cli([], {k: v for k, v in env.items()
                                      if k != "ANTHROPIC_API_KEY"})
        self.assertEqual(code, 2)
        self.assertIn("missing credentials", " ".join(out.split()))


if __name__ == "__main__":
    unittest.main()
