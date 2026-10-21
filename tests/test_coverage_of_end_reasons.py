"""Every end reason and every recovery path is exercised by a test (SC-209, T091).

Each ``EndReason`` value is produced here by a short scripted run with real agents
(tests/fakes/team_script.py), so a new end reason without a scenario fails the suite.
The recovery paths are covered in their own modules; this checks that each named test
still exists, so none can be deleted unnoticed.
"""

import contextlib
import signal
import tempfile
import typing
import unittest
from pathlib import Path

from cost.ledger import RunBudget
from messaging.a2a import SkillRejected
from orchestration.services import RunServices
from orchestration.state import EndReason
from orchestration.team import run_team
from tests import TEST_ENV
from tests.fakes import scripted_claude as sc
from tests.fakes.team_script import (
    REQUIREMENT,
    TeamModel,
    offline_coder,
    request_kind,
    scripted_team,
    spec_payload,
)
from tests.test_never_crashes import failing

RECOVERY_TESTS = {
    "retry": "tests.test_resilience.GuardTests.test_one_429_then_success_returns_the_result_after_one_retry",
    "restart": "tests.test_tool_resilience.McpRestartTests.test_a_killed_server_is_restarted_with_the_same_protections",
    "breaker open": "tests.test_resilience.CircuitBreakerTests.test_an_open_circuit_fails_fast_without_calling",
    "half-open": "tests.test_resilience.CircuitBreakerTests.test_only_one_half_open_trial_is_admitted_at_a_time",
    "cache replay": "tests.test_model_caller.DegradationTests.test_an_open_circuit_serves_a_validated_cached_reply_at_no_cost",
    "structured error": "tests.test_tool_resilience.SandboxRetryTests.test_a_sandbox_that_never_starts_gives_a_failure_and_a_tool_error",
    "resume": "tests.test_resume.InterruptAndResumeTests.test_a_run_interrupted_in_any_stage_resumes_to_the_same_end",
}


class RejectingQA:
    """A QA endpoint that refuses its work, so the A2A request is not completed."""

    def run_iteration(self, context):
        raise SkillRejected("not today")

    def final_report(self, request):
        raise SkillRejected("not today")


class RejectionModel(TeamModel):
    def _spec(self, request):
        return sc.text_reply({**spec_payload(0), "rejection_reason": "Not a software request."},
                             model=request["model"])


class EndReasonCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        stack = contextlib.ExitStack()
        stack.enter_context(offline_coder())
        self.addCleanup(stack.close)

    def run_scenario(self, pm=None, coder=None, qa=None, qa_agent=None, budget=None,
                     stop_on=None, **kwargs) -> dict:
        root = Path(self._tmp.name)
        services = RunServices.create(env=TEST_ENV, response_cache=None,
                                      sleep=lambda delay: None)
        if budget:
            services.budget = RunBudget(budget)
        models = (pm or TeamModel(), coder or TeamModel(),
                  qa or TeamModel(bug_in_first_review=True))
        if stop_on:
            models[0].on_request = lambda request: (
                services.request_stop(signal.SIGINT) if request_kind(request) == stop_on else None
            )
        agents = scripted_team(root, services, *models)
        return run_team(REQUIREMENT, agents[0], agents[1], root, qa=qa_agent or agents[2],
                        services=services, **kwargs)

    def scenarios(self) -> dict:
        permanent = sc.auth_error
        return {
            "complete": {},
            "iteration_limit": {"max_iterations": 1},
            "task_failed": {"coder": TeamModel(fail_when=failing("loop", "Task T1", permanent))},
            "coder_failed": {"coder": TeamModel(fail_when=failing("loop", "You are fixing",
                                                                  permanent))},
            "reflection_failed": {"coder": TeamModel(fail_when=failing("critique",
                                                                       error=permanent))},
            "qa_failed": {"qa": TeamModel(fail_when=failing("tests", error=permanent))},
            "a2a_failed": {"qa_agent": RejectingQA()},
            "rejected": {"pm": RejectionModel()},
            "pm_error": {"pm": TeamModel(fail_when=failing(error=permanent))},
            "dependency_unavailable": {"pm": TeamModel(fail_when=failing())},
            "budget_exceeded": {"budget": 0.0001},
            "interrupted": {"stop_on": "spec"},
        }

    def test_every_end_reason_is_reached_by_a_scenario(self) -> None:
        scenarios = self.scenarios()
        self.assertEqual(set(scenarios), set(typing.get_args(EndReason)))
        for end_reason, scenario in scenarios.items():
            with self.subTest(end_reason=end_reason):
                final = self.run_scenario(**scenario)
                self.assertEqual(final["end_reason"], end_reason, final["error"])

    def test_every_recovery_path_has_its_test(self) -> None:
        loader = unittest.defaultTestLoader
        for path, name in RECOVERY_TESTS.items():
            with self.subTest(path=path):
                suite = loader.loadTestsFromName(name)
                self.assertEqual(suite.countTestCases(), 1, name)


if __name__ == "__main__":
    unittest.main()
