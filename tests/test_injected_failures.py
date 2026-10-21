"""Runs reach the same end despite one model call in five failing temporarily (SC-201).

Failures are injected only in the test fake (FR-239): each run uses its own seeded
``random.Random``, so the result is the same on every machine.

Why 100 runs: with 1 call in 5 failing and 3 retries, a call is lost with probability
0.2**4 = 0.16%, so a run of about 21 calls ends ``dependency_unavailable`` about 3.3% of
the time; the expected rate is about 96.7%, just above the 95% target. Twenty runs can't
tell 95% from 90% (one unlucky run moves the result by 5 points; seeds 0-19 give 18/20),
while 100 runs (seeds 0-99 give 96/100) measure it to within about 2 points.
"""

import contextlib
import random
import tempfile
import unittest
from pathlib import Path

from orchestration.services import RunServices
from orchestration.team import run_team
from tests import TEST_ENV
from tests.fakes import scripted_claude as sc
from tests.fakes.team_script import REQUIREMENT, TeamModel, offline_coder, scripted_team

RUNS = 100
FAILURE_RATE = 1 / 5
TEMPORARY_ERRORS = (sc.rate_limit_error, sc.overloaded_error, sc.connection_error)


def flaky(rng: random.Random):
    """A ``fail_when`` that fails each request with probability ``FAILURE_RATE``."""
    def fail_when(request):
        if rng.random() < FAILURE_RATE:
            return rng.choice(TEMPORARY_ERRORS)()
        return None
    return fail_when


class InjectedFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        stack = contextlib.ExitStack()
        stack.enter_context(offline_coder())
        self.addCleanup(stack.close)

    def run_once(self, root: Path, fail_when=None) -> dict:
        services = RunServices.create(env=TEST_ENV, sleep=lambda delay: None, response_cache=None)
        models = [TeamModel(fail_when=fail_when), TeamModel(fail_when=fail_when),
                  TeamModel(fail_when=fail_when, bug_in_first_review=True)]
        pm, coder, qa = scripted_team(root, services, *models)
        return run_team(REQUIREMENT, pm, coder, root, qa=qa, services=services)

    def test_at_least_95_percent_reach_the_undisturbed_end_reason(self) -> None:
        root = Path(self._tmp.name)
        expected = self.run_once(root)["end_reason"]
        self.assertEqual(expected, "complete")
        outcomes = [self.run_once(root, flaky(random.Random(seed)))["end_reason"]
                    for seed in range(RUNS)]
        matching = sum(outcome == expected for outcome in outcomes)
        self.assertGreaterEqual(matching / RUNS, 0.95, outcomes)


if __name__ == "__main__":
    unittest.main()
