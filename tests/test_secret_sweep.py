"""No secret reaches any output the team writes or sends (SC-208, FR-219).

One scripted run plants a value for each secret-name pattern and an Anthropic-key-shaped
string that is in no environment variable. The model fakes and the generated program
echo them; every record, report, stderr line and trace payload is then scanned.
Generated program output stored in the workspace itself is exempt (FR-219), so the
workspace is not scanned, only the outputs that copy from it.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents import coder_agent
from orchestration.services import RunServices
from orchestration.team import print_progress, run_team
from tests import TEST_ENV
from tests.fakes import scripted_claude as sc
from tests.fakes.recording_tracer import recording_tracer
from tests.fakes.team_script import (
    REQUIREMENT,
    TeamModel,
    bug_finding,
    offline_coder,
    scripted_team,
    spec_payload,
)
from tools.python_exec import ExecutionResult

PLANTED = {
    "MY_API_KEY": "planted-api-key-value-1",
    "GH_TOKEN": "planted-token-value-22",
    "DB_SECRET": "planted-secret-value-333",
    "DB_PASSWORD": "planted-password-value-4",
    "CLOUD_CREDENTIAL": "planted-credential-value-5",
}
KEY_SHAPED = "sk-ant-api03-notinanyenvironment0123456789"
SECRETS = [*PLANTED.values(), KEY_SHAPED]


class LeakyModel(TeamModel):
    """A team model whose replies and generated code echo the key-shaped string."""

    def _spec(self, request):
        payload = spec_payload(self.task_count)
        payload["description"] = f"Prints a greeting. Debug key: {KEY_SHAPED}"
        return sc.text_reply(payload, model=request["model"])

    def _review(self, request):
        first = "Findings from the last review" not in str(request["messages"])
        finding = {**bug_finding(), "problem": f"The program prints {KEY_SHAPED}."}
        return sc.text_reply({"findings": [finding] if first else [], "regression_tests": [],
                              "summary": "ok"}, model=request["model"])

    def _loop(self, request):
        reply = super()._loop(request)
        if reply.stop_reason != "tool_use":
            return reply
        code = f"print({KEY_SHAPED!r})\n"
        return sc.tool_use_reply(
            [("write_file", {"path": "main.py", "content": code, "purpose": "Write it."}),
             ("exec_python", {"path": "main.py", "purpose": "Run it."})],
            model=request["model"],
        )


def echoing_exec(workspace, path, timeout_seconds, image=None, guard=None, run_id=""):
    """The sandbox, replaced: the program prints every planted value."""
    return ExecutionResult(path, 0, "\n".join(SECRETS), "", False, 0.01)


class SecretSweepTests(unittest.TestCase):
    def test_no_planted_value_appears_in_any_output(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, offline_coder(), \
                mock.patch.object(coder_agent, "exec_python", echoing_exec), \
                mock.patch("tools.exec_python", echoing_exec), \
                contextlib.redirect_stderr(stderr):
            root = Path(tmp)
            env = {**TEST_ENV, **PLANTED, "COST_REPORT_DIR": str(root / "costs")}
            services = RunServices.create(env=env, response_cache=None,
                                          sleep=lambda delay: None)
            tracer, recorder = recording_tracer(services.redactor)
            services.tracer = tracer
            services.connect_tracer()
            services.events.subscribers.append(print_progress)
            models = (LeakyModel(), LeakyModel(), LeakyModel())
            pm, coder, qa = scripted_team(root, services, *models)
            final = run_team(f"{REQUIREMENT} Use {KEY_SHAPED} as the key.", pm, coder, root,
                             qa=qa, services=services)

            outputs = {path.name: path.read_text(encoding="utf-8")
                       for path in (root / ".runs" / final["run_id"]).iterdir()
                       if path.name != "lock"}
            outputs["cost report"] = Path(final["cost_report_path"]).read_text("utf-8")
            outputs["QA report"] = (Path(final["workspace"]) / ".qa" / "report.md").read_text(
                encoding="utf-8")
        outputs["stderr"] = stderr.getvalue()
        outputs["trace"] = recorder.text()

        self.assertTrue(set(outputs) >= {"events.jsonl", "run.json", "checkpoint.json"})
        self.assertGreater(len(final["iterations"]), 0)
        for name, text in outputs.items():
            for secret in SECRETS:
                with self.subTest(output=name, secret=secret[:14]):
                    self.assertNotIn(secret, text)
        # The secrets really reached the run: the record shows where they were removed.
        self.assertIn("[REDACTED]", outputs["run.json"])


if __name__ == "__main__":
    unittest.main()
