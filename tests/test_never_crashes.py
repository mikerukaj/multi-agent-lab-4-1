"""No dependency failure crashes a run (SC-216, FR-209c, FR-205, FR-208).

Real agents run on content-aware fake models (tests/fakes/team_script.py), with each
dependency forced to fail in turn. Every run must end with an end reason and a final
record, never an exception.
"""

import contextlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agents.pm_agent import PMAgent, PMConfig
from orchestration.checkpoint import read_final_record, run_dir
from orchestration.services import RunServices
from orchestration.team import ResumeRefused, exit_code_for, resume_team, run_team
from resilience.classify import is_unavailable
from tests import TEST_ENV
from tests.fakes import scripted_claude as sc
from tests.fakes.team_script import (
    REQUIREMENT,
    TeamModel,
    bug_finding,
    first_user_text,
    offline_coder,
    request_kind,
    scripted_team,
)
from tests.team_fakes import FakeFileTools, qa_result

MODEL = "claude-sonnet-5-5"


def failing(kind: str | None = None, text: str | None = None, error=sc.overloaded_error):
    """A ``fail_when`` for requests of ``kind`` (any if None) whose brief contains ``text``."""
    def fail_when(request):
        if kind and request_kind(request) != kind:
            return None
        if text and text not in first_user_text(request):
            return None
        return error()
    return fail_when


class TeamRunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        stack = contextlib.ExitStack()
        stack.enter_context(offline_coder())
        self.addCleanup(stack.close)
        self.events: list[dict] = []

    def services(self, run_id: str | None = None) -> RunServices:
        return RunServices.create(run_id, env=TEST_ENV, sleep=lambda delay: None,
                                  response_cache=None, on_event=self.events.append)

    def run_team(self, pm_model=None, coder_model=None, qa_model=None, services=None,
                 **kwargs) -> dict:
        self.models = (pm_model or TeamModel(), coder_model or TeamModel(),
                       qa_model or TeamModel())
        self.team_services = services or self.services()
        pm, coder, qa = scripted_team(self.root, self.team_services, *self.models)
        return run_team(REQUIREMENT, pm, coder, self.root, qa=qa,
                        services=self.team_services, **kwargs)

    def assert_ended(self, final: dict, end_reason: str, dependency: str | None) -> None:
        self.assertEqual(final["end_reason"], end_reason, final["error"])
        if dependency:
            self.assertEqual(final["failure"]["dependency"], dependency)
        record = read_final_record(run_dir(self.root, final["run_id"]))
        self.assertIsNotNone(record)
        self.assertEqual(record["end_reason"], end_reason)
        self.assertTrue(Path(final["cost_report_path"]).is_file())  # SC-216


class UnavailableModelTests(TeamRunTestCase):
    """The model service failing past its retries ends the run ``dependency_unavailable``."""

    def test_each_model_calling_node(self) -> None:
        cases = {
            "pm": dict(pm_model=TeamModel(fail_when=failing())),
            "coder": dict(coder_model=TeamModel(fail_when=failing("loop", "Task T1"))),
            "reflect": dict(coder_model=TeamModel(fail_when=failing("critique"))),
            "qa": dict(qa_model=TeamModel(fail_when=failing("tests"))),
            "fix": dict(coder_model=TeamModel(fail_when=failing("loop", "You are fixing")),
                        qa_model=TeamModel(reviews=[[bug_finding()], []])),
        }
        for node, models in cases.items():
            with self.subTest(node=node):
                final = self.run_team(**models)
                self.assertEqual(final["status"], "failed")
                self.assert_ended(final, "dependency_unavailable", "anthropic")
                self.assertEqual(final["failure"]["kind"], "temporary")
                self.assertEqual(exit_code_for(final), 3)

    def test_an_open_model_circuit_fails_fast(self) -> None:
        services = self.services()
        breaker = services.breaker("anthropic", MODEL)
        for _ in range(3):
            breaker.failure()
        final = self.run_team(services=services)
        self.assert_ended(final, "dependency_unavailable", "anthropic")
        self.assertEqual(final["failure"]["attempts"], 0)
        self.assertEqual(final["failure"]["error_type"], "circuit_open")
        self.assertEqual(self.models[0].requests, [])


class OpenToolCircuitTests(TeamRunTestCase):
    def open_circuit(self, dependency: str, target_of) -> RunServices:
        services = self.services()
        breaker = services.breaker(dependency, target_of(services))
        for _ in range(3):
            breaker.failure()
        return services

    def test_an_open_mcp_circuit_lets_the_coder_go_on_and_stops_qa(self) -> None:
        services = self.open_circuit(
            "mcp", lambda s: str((self.root / s.run_id).resolve())
        )
        final = self.run_team(services=services)
        self.assert_ended(final, "dependency_unavailable", "mcp")
        self.assertTrue(all(p["status"] == "completed" for p in final["task_progress"].values()))
        coder_results = [
            block for request in self.models[1].requests for message in request["messages"]
            if message["role"] == "user" and isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "tool_result"
        ]
        self.assertTrue(coder_results)
        self.assertTrue(all(block["is_error"] for block in coder_results))
        self.assertIn('"dependency": "mcp"', coder_results[0]["content"])

    def test_an_open_sandbox_circuit_stops_qa(self) -> None:
        services = self.open_circuit("sandbox", lambda s: "multi-agent-sandbox:py3.12-pytest9.1.1")
        final = self.run_team(services=services)
        self.assert_ended(final, "dependency_unavailable", "sandbox")


class PermanentFailureTests(TeamRunTestCase):
    """A permanent failure keeps the agent's own end reason and can't be resumed (FR-205)."""

    def test_each_node_keeps_its_own_end_reason(self) -> None:
        cases = {
            "pm_error": dict(pm_model=TeamModel(fail_when=failing(error=sc.auth_error))),
            "task_failed": dict(coder_model=TeamModel(
                fail_when=failing("loop", "Task T1", sc.auth_error))),
            "reflection_failed": dict(coder_model=TeamModel(
                fail_when=failing("critique", error=sc.auth_error))),
            "qa_failed": dict(qa_model=TeamModel(fail_when=failing("tests", error=sc.auth_error))),
            "coder_failed": dict(
                coder_model=TeamModel(fail_when=failing("loop", "You are fixing", sc.auth_error)),
                qa_model=TeamModel(reviews=[[bug_finding()], []])),
        }
        for end_reason, models in cases.items():
            with self.subTest(end_reason=end_reason):
                final = self.run_team(**models)
                self.assert_ended(final, end_reason, "anthropic")
                self.assertEqual(final["failure"]["kind"], "permanent")
                self.assertEqual(final["failure"]["attempts"], 1)
                self.assertEqual(exit_code_for(final), 1)
                with self.assertRaisesRegex(ResumeRefused, end_reason):
                    resume_team(final["run_id"], workspace_root=self.root,
                                services=self.services(final["run_id"]))

    def test_permanent_failures_still_open_the_circuit(self) -> None:
        services = self.services()
        pm = PMAgent(PMConfig(), client=sc.ScriptedClaude([sc.auth_error()] * 3),
                     services=services)
        results = [pm.write_spec(REQUIREMENT) for _ in range(4)]
        self.assertTrue(all(not is_unavailable(result.failure) for result in results[:3]))
        self.assertEqual(results[2].failure["circuit"], "open")
        self.assertEqual(results[3].failure["error_type"], "circuit_open")
        self.assertTrue(is_unavailable(results[3].failure))


class ChromaAndA2ATests(TeamRunTestCase):
    def test_an_unavailable_memory_store_does_not_stop_the_run(self) -> None:
        services = self.services()
        breaker = services.breaker("chroma", str(self.root / "chroma"))
        for _ in range(3):
            breaker.failure()
        final = self.run_team(services=services)
        self.assert_ended(final, "complete", None)
        degraded = [e for e in self.events if e["event"] == "degraded"
                    and e["dependency"] == "chroma"]
        self.assertTrue(degraded)

    def test_an_a2a_timeout_is_not_retried(self) -> None:
        calls = []

        class SlowQA:
            def run_iteration(self, context):
                calls.append(time.monotonic())
                time.sleep(1.0)
                raise AssertionError("cancelled work must never finish")

            def final_report(self, request):
                return {**qa_result("complete").to_dict(), "report_path": ""}

        services = self.services()
        pm, coder, _ = scripted_team(self.root, services, TeamModel(), TeamModel(), TeamModel())
        final = run_team(REQUIREMENT, pm, coder, self.root, qa=SlowQA(), services=services,
                         a2a_timeout_seconds=0.3)
        self.assertEqual(final["end_reason"], "a2a_failed")
        self.assertEqual(len(calls), 1)
        self.assertFalse([e for e in self.events if e["event"] == "retry"
                          and e["dependency"] == "a2a"])
        self.assertEqual(exit_code_for(final), 3)


if __name__ == "__main__":
    unittest.main()
