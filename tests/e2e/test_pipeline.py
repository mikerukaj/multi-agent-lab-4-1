"""End to end: the whole pipeline builds a program that passes a supplied fixture (FR-235c).

The requirement (tests/fixtures/e2e/requirement.txt) fixes the program's interface;
the fixture (test_inventory_fixture.py) is never shown to the agents. After the run,
a scratch copy of the workspace is checked in the sandbox: every file compiles, the
CLI runs, and the fixture passes. The scripted variant (real agents on scripted models)
needs Docker; the live variant also needs ``RUN_LIVE_E2E=1`` and a key, and spends money.
"""

import json
import os
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
from tests.fakes.recording_tracer import recording_tracer
from tests.fakes.team_script import FakeMemory
from tests.fixtures.e2e import scripted_run
from tests.schema_check import load_fixture_schema, schema_errors
from tools.python_exec import exec_python
from tools.test_runner import run_pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "e2e"
HAS_DOCKER = shutil.which("docker") is not None
LIVE = (os.environ.get("RUN_LIVE_E2E") == "1" and HAS_DOCKER
        and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")))
COMPILE_ALL = '''import pathlib, py_compile, sys
for path in pathlib.Path(".").rglob("*.py"):
    if path.name != "compile_all.py" and "tests" not in path.parts:
        py_compile.compile(str(path), doraise=True)
'''
RUN_MAIN = '''import subprocess, sys
with open("commands.txt", "w", encoding="utf-8") as commands:
    commands.write("add apple 2\\nreport\\n")
sys.exit(subprocess.run([sys.executable, "main.py", "commands.txt"]).returncode)
'''


class PipelineTestCase(unittest.TestCase):
    def assert_program_passes(self, final: dict) -> None:
        """Assertions 1-5 of contracts/test-suite.md, "End to end"."""
        self.assertEqual((final["status"], final["end_reason"]), ("completed", "complete"),
                         final["error"])
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "program"
            shutil.copytree(final["workspace"], scratch,
                            ignore=shutil.ignore_patterns(".qa", "tests", "__pycache__"))
            (scratch / "compile_all.py").write_text(COMPILE_ALL, encoding="utf-8")
            (scratch / "run_main.py").write_text(RUN_MAIN, encoding="utf-8")
            compiled = exec_python(scratch, "compile_all.py", 60)
            self.assertEqual(compiled.exit_code, 0, compiled.stderr)
            ran = exec_python(scratch, "run_main.py", 60)
            self.assertEqual(ran.exit_code, 0, ran.stderr)
            (scratch / "tests").mkdir()
            shutil.copy(FIXTURES / "test_inventory_fixture.py", scratch / "tests")
            fixture_run = run_pytest(scratch, 120)
        self.assertTrue(fixture_run["loaded"], fixture_run["output"])
        self.assertEqual((fixture_run["failed"], fixture_run["errors"]), (0, 0),
                         fixture_run["output"])
        report = json.loads(Path(final["cost_report_path"]).read_text(encoding="utf-8"))
        schema = load_fixture_schema("cost-report.schema.json")
        self.assertEqual(schema_errors(report, schema, schema), [])


@unittest.skipUnless(HAS_DOCKER, "Docker is not available")
class ScriptedPipelineTests(PipelineTestCase):
    def test_scripted_pipeline_passes_supplied_fixture(self) -> None:
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
            pm_model, coder_model, qa_model = scripted_run.models()
            # Real agents with their real MCP file tools, sandbox and pytest runner.
            pm = PMAgent(PMConfig(), client=pm_model, services=services)
            coder = CoderAgent(CoderConfig(workspace_root=root, chroma_path=root / "c"),
                               client=coder_model, services=services)
            qa = QAAgent(QAConfig(), client=qa_model, services=services)
            final = run_team(scripted_run.REQUIREMENT, pm, coder, root, qa=qa,
                             services=services)
            self.assert_program_passes(final)
        self.assertEqual([root["name"] for root in recorder.roots()], ["team-run"])
        agents = {span["metadata"]["agent_name"] for span in recorder.named("agent:")}
        self.assertEqual(agents, {"pm", "coder", "qa"})


@unittest.skipUnless(LIVE, "RUN_LIVE_E2E is not set (the live run makes paid model calls)")
class LivePipelineTests(PipelineTestCase):
    def test_live_pipeline_passes_supplied_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = RunServices.create(env={**os.environ, "COST_REPORT_DIR": str(root)})
            final = run_team(scripted_run.REQUIREMENT, workspace_root=root, services=services)
            print(f"\ncost report: {final['cost_report_path']} (${final['cost_usd']})")
            self.assert_program_passes(final)


if __name__ == "__main__":
    unittest.main()
