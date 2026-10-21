"""Coder + QA together over the A2A broker, with real tests in the real sandbox (FR-235b).

Real ``CoderAgent`` and ``QAAgent`` on scripted models, the real broker and endpoints,
the real MCP file-tool servers, and real pytest runs in the sandbox. The Coder's first
draft has an off-by-one bug; QA's test catches it, the fix round corrects it over A2A,
and QA's second review is complete. Skips without Docker.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.coder_agent import CoderAgent, CoderConfig
from agents.pm_agent import PMAgent, PMConfig
from agents.qa_agent import QAAgent, QAConfig
from orchestration.services import RunServices
from orchestration.team import run_team
from tests import TEST_ENV
from tests.fakes import scripted_claude as sc
from tests.fakes.recording_tracer import recording_tracer
from tests.fakes.team_script import FakeMemory, TeamModel, first_user_text

CRITERION = "add(1, 2) returns 3"
BUGGY = "def add(a: int, b: int) -> int:\n    return a + b + 1\n"
FIXED = "def add(a: int, b: int) -> int:\n    return a + b\n"
QA_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
SPEC = {
    "name": "Calculator", "description": "Adds two numbers.",
    "acceptance_criteria": [CRITERION], "assumptions": [],
    "tasks": [{"title": "Write add()", "description": "calc.py with add(a, b).",
               "acceptance_criteria": [CRITERION], "priority": "high"}],
    "rejection_reason": "",
}


class CalcCoder(TeamModel):
    """Builds calc.py with an off-by-one; the fix round corrects it."""

    def _loop(self, request):
        brief = first_user_text(request)
        turns = sum(1 for message in request["messages"] if message["role"] == "assistant")
        if turns:
            return sc.text_reply("Done.", model=request["model"])
        if "You are fixing" in brief:
            content = FIXED
        elif "Task T1 of 1" in brief.rsplit("Task:\n", 1)[-1]:
            content = BUGGY
        else:  # A reflection revision: no change.
            return sc.text_reply("No change needed.", model=request["model"])
        return sc.tool_use_reply(
            [("write_file", {"path": "calc.py", "content": content, "purpose": "Write it."})],
            model=request["model"],
        )


class CalcQA(TeamModel):
    """Writes one test; reports the failing test first, then nothing."""

    def _tests(self, request):
        test_file = {"path": "tests/test_calc.py", "content": QA_TEST, "criteria": [CRITERION]}
        return sc.text_reply({"test_files": [test_file], "notes": ""}, model=request["model"])

    def _review(self, request):
        first = "Findings from the last review" not in first_user_text(request)
        finding = {"kind": "failing_test", "rule": "", "file": "calc.py", "location": "add",
                   "problem": "add(1, 2) returns 4.", "expected": CRITERION,
                   "likely_cause": "An extra + 1.", "fix_instruction": "Return a + b.",
                   "test_ids": ["tests/test_calc.py::test_add"], "same_as": ""}
        return sc.text_reply({"findings": [finding] if first else [], "regression_tests": [],
                              "summary": "ok"}, model=request["model"])


@unittest.skipUnless(shutil.which("docker"), "Docker is not available")
class CoderQAIntegrationTests(unittest.TestCase):
    def test_a_failing_test_is_fixed_over_a2a_and_passes_on_review(self) -> None:
        tracer, recorder = recording_tracer()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("agents.coder_agent.SemanticMemory", FakeMemory):
            root = Path(tmp)
            services = RunServices.create(
                env={**TEST_ENV, "COST_REPORT_DIR": str(root / "costs"),
                     "QA_OUTPUT_ROOT": str(root / "qa-out")},
                response_cache=None, sleep=lambda delay: None, tracer=tracer,
            )
            services.connect_tracer()
            pm = PMAgent(PMConfig(), client=sc.ScriptedClaude([sc.text_reply(SPEC)]),
                         services=services)
            coder = CoderAgent(CoderConfig(workspace_root=root, chroma_path=root / "c"),
                               client=CalcCoder(), services=services)
            qa = QAAgent(QAConfig(), client=CalcQA(), services=services)
            final = run_team("Write add(a, b).", pm, coder, root, qa=qa, services=services)
            report_exists = (Path(final["workspace"]) / ".qa" / "report.md").is_file()

        self.assertEqual(final["end_reason"], "complete", final["error"])
        runs = [iteration["test_run"] for iteration in final["iterations"]]
        self.assertEqual(len(runs), 2)
        self.assertEqual([(run["failed"], run["loaded"]) for run in runs],
                         [(1, True), (0, True)])
        self.assertEqual([entry["skill_id"] for entry in final["a2a_log"]],
                         ["review-program", "fix-program", "review-program", "final-report"])
        for span in recorder.named("agent:qa"):
            self.assertEqual(recorder.ancestors(span)[-1], "team-run")
        for agent in ("coder", "qa"):
            self.assertGreater(final["agent_usage"][agent]["model_calls"], 0, agent)
        self.assertTrue(report_exists)
        self.assertEqual(json.loads(json.dumps(final)), final)


if __name__ == "__main__":
    unittest.main()
