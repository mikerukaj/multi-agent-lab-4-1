"""PM + Coder together: the plan reaches the Coder and is built (FR-235b).

Real ``PMAgent`` and ``CoderAgent``, the real team graph, and the real MCP file-tool
server over stdio; scripted models, a local stand-in for the sandbox, a fake QA that
approves, and a recording tracer.
"""

import contextlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.coder_agent import CoderAgent, CoderConfig
from agents.pm_agent import PMAgent, PMConfig
from orchestration.services import RunServices
from orchestration.team import run_team
from tests import TEST_ENV
from tests.fakes import scripted_claude as sc
from tests.fakes.local_sandbox import local_exec_python
from tests.fakes.recording_tracer import recording_tracer
from tests.fakes.team_script import FakeMemory, TeamModel, first_user_text, request_kind
from tests.schema_check import load_fixture_schema, schema_errors
from tests.team_fakes import FakeQA, qa_result

CRITERIA = {
    1: "parse_args() accepts a --units flag",
    2: "convert() turns 100 C into 212 F",
    3: "main() prints the converted value",
}
REQUIREMENT = "Build a temperature converter CLI."


def spec() -> dict:
    return {
        "name": "Converter", "description": "Converts temperatures.",
        "acceptance_criteria": ["the CLI converts temperatures"], "assumptions": [],
        "tasks": [{"title": f"Part {n}", "description": f"Build part {n}.",
                   "acceptance_criteria": [CRITERIA[n]], "priority": "high"}
                  for n in (1, 2, 3)],
        "rejection_reason": "",
    }


def current_task(request: dict) -> int | None:
    """The task a Coder request is building: the last one its brief names (earlier tasks
    appear before it in the recent-conversation note)."""
    found = re.findall(r"Task T(\d) of 3", first_user_text(request))
    return int(found[-1]) if found else None


class BuildingCoderModel(TeamModel):
    """Writes ``part<n>.py`` for task Tn through real file tools; task 2 reads part1.py."""

    def _loop(self, request):
        number = current_task(request)
        if number is None:  # A reflection revision: nothing to change.
            return super()._loop(request)
        turns = sum(1 for message in request["messages"] if message["role"] == "assistant")
        calls = []
        if number == 2 and turns == 0:
            calls = [("read_file", {"path": "part1.py", "purpose": "See part 1."})]
        elif turns == (1 if number == 2 else 0):
            calls = [("write_file", {"path": f"part{number}.py",
                                     "content": f"PART = {number}\n", "purpose": "Write it."})]
        if not calls:
            return sc.text_reply("Done.", model=request["model"])
        return sc.tool_use_reply(calls, model=request["model"])

    def _summary(self, request):
        reply = super()._summary(request)
        number = current_task(request)
        if number:
            payload = json.loads(reply.content[0].text)
            payload["main_file"] = f"part{number}.py"
            return sc.text_reply(payload, model=request["model"])
        return reply


class PMCoderIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for target, value in (("agents.coder_agent.ensure_sandbox", lambda image: None),
                              ("agents.coder_agent.exec_python", local_exec_python),
                              ("tools.exec_python", local_exec_python),
                              ("agents.coder_agent.SemanticMemory", FakeMemory)):
            stack.enter_context(mock.patch(target, value))
        self.tracer, self.recorder = recording_tracer()
        self.services = RunServices.create(
            env={**TEST_ENV, "COST_REPORT_DIR": str(self.root / "costs")},
            response_cache=None, sleep=lambda delay: None, tracer=self.tracer,
        )
        self.services.connect_tracer()

    def run_team(self, pm_reply) -> tuple[dict, sc.ScriptedClaude, BuildingCoderModel]:
        pm_client = sc.ScriptedClaude([pm_reply])
        coder_model = BuildingCoderModel()
        pm = PMAgent(PMConfig(), client=pm_client, services=self.services)
        coder = CoderAgent(CoderConfig(workspace_root=self.root, chroma_path=self.root / "c"),
                           client=coder_model, services=self.services)
        final = run_team(REQUIREMENT, pm, coder, self.root,
                         qa=FakeQA([qa_result("complete")]), services=self.services)
        return final, pm_client, coder_model

    def test_the_plan_is_built_task_by_task_in_one_workspace(self) -> None:
        final, _, coder_model = self.run_team(sc.text_reply(spec(),
                                                            model="claude-sonnet-5-5"))
        self.assertEqual(final["end_reason"], "complete", final["error"])
        builds = [r for r in coder_model.requests
                  if request_kind(r) == "loop" and current_task(r)]
        order = [current_task(r) for r in builds]
        self.assertEqual(sorted(set(order)), [1, 2, 3])
        self.assertEqual(order, sorted(order))
        for request in builds:
            brief = first_user_text(request).rsplit("Task:\n", 1)[-1]
            self.assertIn(CRITERIA[current_task(request)], brief)
        workspace = Path(final["workspace"])
        self.assertEqual(sorted(p.name for p in workspace.glob("part*.py")),
                         ["part1.py", "part2.py", "part3.py"])
        task2 = [r for r in builds if current_task(r) == 2][-1]
        self.assertIn("PART = 1", json.dumps(task2["messages"], default=str))

        for agent in ("pm", "coder"):
            usage = final["agent_usage"][agent]
            self.assertGreater(usage["model_calls"], 0, agent)
            self.assertGreater(usage["cost_usd"], 0, agent)

        self.assertEqual([root["name"] for root in self.recorder.roots()], ["team-run"])
        self.assertEqual(len(self.recorder.named("agent:pm")), 1)
        builds_spans = [s for s in self.recorder.named("agent:coder")
                        if s["metadata"].get("invocation") == "run"]
        self.assertEqual(len(builds_spans), 3)
        for span in self.recorder.named("agent:"):
            self.assertEqual(self.recorder.ancestors(span)[-1], "team-run")

        report = json.loads(Path(final["cost_report_path"]).read_text(encoding="utf-8"))
        schema = load_fixture_schema("cost-report.schema.json")
        self.assertEqual(schema_errors(report, schema, schema), [])

    def test_a_pm_reply_that_fails_validation_never_reaches_the_coder(self) -> None:
        bad = {**spec(), "tasks": [{**spec()["tasks"][0], "priority": "urgent"}]}
        final, _, coder_model = self.run_team(sc.text_reply(bad, model="claude-sonnet-5-5"))
        self.assertEqual(final["end_reason"], "pm_error")
        self.assertIn("Model output failed validation", final["error"])
        self.assertEqual(coder_model.requests, [])


if __name__ == "__main__":
    unittest.main()
