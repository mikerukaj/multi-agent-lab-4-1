"""Team-graph tests for QA over A2A (contracts/feedback-loop.md, a2a-binding.md).

Every run goes through a real ``A2ABroker``; the PM client, the Coder and QA are fakes.
"""

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agents.pm_agent import PMAgent, PMConfig
from messaging.a2a import SkillRejected
from observability.events import read_events
from orchestration import team
from orchestration.services import RunServices
from orchestration.team import (
    format_update,
    initial_team_state,
    max_iterations_from_env,
    render_fix_brief,
    run_team,
)
from tests import TEST_ENV
from tests.fakes.recording_tracer import recording_tracer
from tests.team_fakes import (
    token_usage,
    FakeCoder,
    FakePMClient,
    FakeQA,
    pm_response,
    qa_result,
    scripted_finding,
    scripted_test_run,
    spec_payload,
)

REQUIREMENT = "Build a temperature converter CLI"


def make_pm(task_count: int = 2) -> PMAgent:
    return PMAgent(PMConfig(), client=FakePMClient([pm_response(spec_payload(task_count))]))


class SlowQA(FakeQA):
    """Answers after ``delay`` seconds, to trip the A2A request time limit."""

    def __init__(self, results: list, delay: float) -> None:
        super().__init__(results)
        self.delay = delay

    def run_iteration(self, context: dict):
        time.sleep(self.delay)
        return super().run_iteration(context)


class TeamLoopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def team(self, qa, coder=None, **kwargs) -> dict:
        return run_team(
            REQUIREMENT, make_pm(), coder or FakeCoder(), self.root, None, qa=qa, **kwargs
        )


class QaOverA2ATests(TeamLoopTestCase):
    def test_completed_build_is_reviewed_over_a2a_then_reported(self) -> None:
        qa = FakeQA([qa_result("complete")])
        final = self.team(qa)
        self.assertEqual((final["status"], final["end_reason"]), ("completed", "complete"))
        log = final["a2a_log"]
        self.assertEqual(
            [(e["sender"], e["recipient"], e["skill_id"]) for e in log],
            [("coder", "qa-debugger", "review-program"), ("team", "qa-debugger", "final-report")],
        )
        self.assertTrue(all(entry["context_id"] == final["run_id"] for entry in log))
        self.assertEqual(log[0]["task_id"], final["iterations"][0]["review_task_id"])
        self.assertEqual(log[0]["outcome"], "TASK_STATE_COMPLETED")
        self.assertEqual(final["qa_report"]["status"], "completed")
        self.assertEqual(final["iterations"][0]["verdict"], "complete")
        context = qa.contexts[0]
        self.assertEqual(context["iteration"], 1)
        self.assertEqual(context["workspace"], final["workspace"])
        self.assertEqual(sorted(context["source_files"]), ["t1.py", "t2.py"])
        self.assertEqual(qa.report_requests[0]["status"], "completed")
        self.assertEqual(json.loads(json.dumps(final)), final)

    def test_qa_failure_ends_the_run_with_qa_failed(self) -> None:
        final = self.team(FakeQA([qa_result("fix_needed", error="QA test suite failed to load")]))
        self.assertEqual((final["status"], final["end_reason"]), ("error", "qa_failed"))
        self.assertIn("failed to load", final["error"])
        self.assertIsNone(final["qa_report"])

    def test_rejected_task_or_timeout_is_an_a2a_failure(self) -> None:
        rejected = self.team(FakeQA([SkillRejected("bad payload")]))
        self.assertEqual((rejected["status"], rejected["end_reason"]), ("error", "a2a_failed"))
        self.assertIn("bad payload", rejected["error"])
        timed_out = self.team(SlowQA([qa_result("complete")], delay=1.0), a2a_timeout_seconds=0.2)
        self.assertEqual(timed_out["end_reason"], "a2a_failed")
        self.assertIn("qa-debugger did not answer within 0.2 s", timed_out["error"])

    def test_failed_report_keeps_the_result_and_records_why(self) -> None:
        qa = FakeQA([qa_result("complete")], report_error=RuntimeError("disk full"))
        final = self.team(qa)
        self.assertEqual((final["status"], final["end_reason"]), ("completed", "complete"))
        self.assertIsNone(final["qa_report"])
        self.assertIn("final report missing", final["error"])
        self.assertIn("disk full", final["error"])

    def test_failed_build_never_reaches_qa(self) -> None:
        qa = FakeQA([])
        final = self.team(qa, coder=FakeCoder(fail_on=frozenset({"T1"})))
        self.assertEqual((final["status"], final["end_reason"]), ("failed", "task_failed"))
        self.assertEqual(final["a2a_log"], [])
        self.assertIsNone(final["qa_report"])
        self.assertEqual(qa.contexts, [])

    def test_broker_thread_is_stopped_after_the_run(self) -> None:
        self.team(FakeQA([qa_result("complete")]))
        alive = [thread.name for thread in threading.enumerate() if thread.name == "a2a-broker"]
        self.assertEqual(alive, [])


class RejectingCoder(FakeCoder):
    """A Coder whose endpoint rejects every fix request."""

    def _fix(self, brief, workspace, answer_ids):
        raise SkillRejected("cannot take this fix request")


class FeedbackLoopTests(TeamLoopTestCase):
    def loop(self, verdicts: list[str], coder=None, **kwargs):
        log: list = []
        coder = coder or FakeCoder()
        coder.call_log = log
        qa = FakeQA([qa_result(verdict) for verdict in verdicts], call_log=log)
        final = self.team(qa, coder=coder, **kwargs)
        return final, log, qa

    def test_success_at_iteration_three_reflects_before_every_review(self) -> None:
        final, log, _ = self.loop(["fix_needed", "fix_needed", "complete"])
        self.assertEqual((final["status"], final["end_reason"]), ("completed", "complete"))
        self.assertEqual(log, [
            ("reflect", 1), ("qa", 1), ("fix", 1),
            ("reflect", 2), ("qa", 2), ("fix", 2),
            ("reflect", 3), ("qa", 3),
        ])
        iterations = final["iterations"]
        self.assertEqual([it["number"] for it in iterations], [1, 2, 3])
        before = [it["reflections"][0]["before_iteration"] for it in iterations]
        self.assertEqual(before, [1, 2, 3])
        self.assertTrue(all(it["fix"] and it["fix_task_id"] for it in iterations[:2]))
        self.assertIsNone(iterations[2]["fix"])
        self.assertEqual(final["pending_reflections"], [])
        self.assertEqual(json.loads(json.dumps(final)), final)

    def test_a2a_record_matches_the_iterations(self) -> None:
        final, _, _ = self.loop(["fix_needed", "fix_needed", "complete"])
        log, iterations = final["a2a_log"], final["iterations"]
        reviews = [e for e in log if e["skill_id"] == "review-program"]
        fixes = [e for e in log if e["skill_id"] == "fix-program"]
        self.assertEqual(len(reviews), len(iterations))
        self.assertTrue(all((e["sender"], e["recipient"]) == ("coder", "qa-debugger")
                            for e in reviews))
        self.assertEqual(len(fixes), len(iterations) - 1)
        self.assertTrue(all((e["sender"], e["recipient"]) == ("qa-debugger", "coder")
                            for e in fixes))
        for number, fix in enumerate(fixes):
            self.assertEqual(fix["reference_task_ids"], [iterations[number]["review_task_id"]])
            self.assertEqual(fix["task_id"], iterations[number]["fix_task_id"])
        self.assertEqual({e["context_id"] for e in log}, {final["run_id"]})

    def test_never_passing_run_stops_at_the_limit_with_a_report(self) -> None:
        final, log, qa = self.loop(["fix_needed"] * 6)
        self.assertEqual((final["status"], final["end_reason"]), ("failed", "iteration_limit"))
        self.assertIn("Iteration limit reached (5 of 5)", final["error"])
        self.assertEqual(len(final["iterations"]), 5)
        self.assertEqual([name for name, _ in log].count("qa"), 5)
        self.assertEqual([name for name, _ in log].count("fix"), 4)
        self.assertTrue(final["qa_report"]["unresolved"])
        self.assertEqual(qa.report_requests[0]["end_reason"], "iteration_limit")

    def test_limit_of_one_makes_no_fix_request(self) -> None:
        final, _, _ = self.loop(["fix_needed"], max_iterations=1)
        self.assertEqual(final["end_reason"], "iteration_limit")
        skills = [e["skill_id"] for e in final["a2a_log"]]
        self.assertEqual(skills, ["review-program", "final-report"])

    def test_fix_round_at_the_step_cap_keeps_the_loop_going(self) -> None:
        final, _, _ = self.loop(["fix_needed", "complete"],
                                coder=FakeCoder(fix_statuses=("step_limit",)))
        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["iterations"][0]["fix"]["hit_step_cap"])

    def test_failed_or_rejected_fix_round_ends_the_run(self) -> None:
        final, _, _ = self.loop(["fix_needed"], coder=FakeCoder(fix_statuses=("refused",)))
        self.assertEqual((final["status"], final["end_reason"]), ("failed", "coder_failed"))
        self.assertIn("QA review #1", final["error"])
        final, _, _ = self.loop(["fix_needed"], coder=RejectingCoder())
        self.assertEqual((final["status"], final["end_reason"]), ("error", "a2a_failed"))

    def test_failed_reflection_ends_the_run_before_qa(self) -> None:
        final, _, qa = self.loop(["complete"], coder=FakeCoder(reflect_statuses=("error",)))
        self.assertEqual((final["status"], final["end_reason"]), ("failed", "reflection_failed"))
        self.assertEqual(qa.contexts, [])
        self.assertIsNone(final["qa_report"])

    def test_usage_includes_reflection_qa_and_fix_rounds(self) -> None:
        final, _, _ = self.loop(["fix_needed", "complete"])
        # PM 100/50 + 2 build tasks 2 x 10/5 + 2 reflections 2 x 20/10
        # + 2 QA iterations 2 x 100/50 + 1 fix round 10/5.
        self.assertEqual(final["usage"], token_usage(370, 185))


class TracingAcrossA2ATests(TeamLoopTestCase):
    """One trace per run, even for spans opened on the broker's threads (FR-214a)."""

    def test_qa_and_fix_round_spans_share_the_single_root_trace(self) -> None:
        tracer, recorder = recording_tracer()
        services = RunServices.create(env=TEST_ENV, response_cache=None, tracer=tracer)
        qa = FakeQA([qa_result("fix_needed"), qa_result("complete")])
        final = run_team(REQUIREMENT, make_pm(), FakeCoder(), self.root, qa=qa,
                         services=services)
        self.assertEqual(final["end_reason"], "complete")
        roots = recorder.roots()
        self.assertEqual([root["name"] for root in roots], ["team-run"])
        trace = roots[0]["trace_id"]
        qa_spans = recorder.named("agent:qa")
        self.assertEqual(len(qa_spans), 3)  # Two reviews and the final report.
        fix_spans = [span for span in recorder.named("agent:coder")
                     if span["metadata"].get("invocation") == "fix"]
        self.assertEqual(len(fix_spans), 1)
        for span in qa_spans + fix_spans:
            self.assertEqual(span["trace_id"], trace)
            self.assertEqual(recorder.ancestors(span)[-1], "team-run")

    def test_every_a2a_exchange_is_an_event_with_its_stage(self) -> None:
        services = RunServices.create(env=TEST_ENV, response_cache=None)
        qa = FakeQA([qa_result("fix_needed"), qa_result("complete")])
        final = run_team(REQUIREMENT, make_pm(), FakeCoder(), self.root, qa=qa,
                         services=services)
        path = self.root / ".runs" / final["run_id"] / "events.jsonl"
        exchanges = [event for event in read_events(path) if event["event"] == "a2a_exchange"]
        self.assertEqual([event["stage"] for event in exchanges],
                         ["qa_review", "fix", "qa_review", "final_report"])
        self.assertEqual(len(exchanges), len(final["a2a_log"]))


class IterationLimitTests(TeamLoopTestCase):
    def test_env_parsing(self) -> None:
        self.assertEqual(max_iterations_from_env({}), 5)
        self.assertEqual(max_iterations_from_env({"TEAM_MAX_ITERATIONS": "3"}), 3)
        for bad in ("0", "11", "x"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                max_iterations_from_env({"TEAM_MAX_ITERATIONS": bad})

    def test_run_team_refuses_an_out_of_range_limit_before_the_pm_runs(self) -> None:
        client = FakePMClient([])
        pm = PMAgent(PMConfig(), client=client)
        for bad in (0, 11):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                run_team(REQUIREMENT, pm, FakeCoder(), self.root, qa=FakeQA([]),
                         max_iterations=bad)
        self.assertEqual(list(self.root.iterdir()), [])

    def run_main(self, argv: list[str], env: dict) -> tuple[int, mock.Mock, str]:
        stderr = io.StringIO()
        run = mock.Mock(return_value={"status": "completed"})
        with contextlib.ExitStack() as stack:
            for name in ("PMAgent", "CoderAgent", "QAAgent"):
                stack.enter_context(mock.patch.object(team, name))
            stack.enter_context(mock.patch.object(team, "run_team", run))
            stack.enter_context(mock.patch.object(
                team, "check_health", return_value={"ok": True, "items": []}))
            stack.enter_context(mock.patch.dict("os.environ", env))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = team.main(argv)
        return code, run, stderr.getvalue()

    def test_cli_option_overrides_the_environment(self) -> None:
        code, run, _ = self.run_main(["--max-iterations", "3", REQUIREMENT],
                                     {"TEAM_MAX_ITERATIONS": "2"})
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["max_iterations"], 3)
        code, run, _ = self.run_main([REQUIREMENT], {"TEAM_MAX_ITERATIONS": "2"})
        self.assertEqual(run.call_args.kwargs["max_iterations"], 2)

    def test_cli_refuses_out_of_range_limits(self) -> None:
        code, run, err = self.run_main(["--max-iterations", "11", REQUIREMENT], {})
        self.assertEqual(code, 2)
        self.assertIn("1 to 10", err)
        run.assert_not_called()
        code, _, _ = self.run_main([REQUIREMENT], {"TEAM_MAX_ITERATIONS": "0"})
        self.assertEqual(code, 2)


class BriefAndProgressTests(unittest.TestCase):
    def state_with_findings(self) -> dict:
        state = initial_team_state(REQUIREMENT, "run-1", Path("/tmp/ws"))
        spec = {**spec_payload(1), "tasks": []}
        spec["tasks"] = [{"task_id": "T1", **spec_payload(1)["tasks"][0]}]
        failing = {**scripted_finding("F1-1", kind="failing_test"),
                   "test_ids": ["tests/test_app.py::test_bad_1"]}
        advisory = {**scripted_finding("F1-2", severity="advisory", kind="style"),
                    "problem": "Long line."}
        state.update(spec=spec, iterations=[{
            "number": 1, "reflections": [], "disputes": [], "test_changes": [],
            "test_run": scripted_test_run(failed=1), "findings": [failing, advisory],
            "verdict": "fix_needed", "fix": None, "review_task_id": "t1", "fix_task_id": None,
        }])
        return state

    def test_fix_brief_lists_every_blocking_finding(self) -> None:
        brief = render_fix_brief(self.state_with_findings())
        for text in ("QA review #1 of 5", "[F1-1]", "Problem: Wrong output.",
                     "Expected: The output the spec names.", "Likely cause: Off by one.",
                     "Fix: Fix the off-by-one in main().",
                     "Failing tests: tests/test_app.py::test_bad_1",
                     "Advisory findings", "[F1-2]", "100 C converts to 212 F",
                     "Answer every F"):
            self.assertIn(text, brief)

    def test_reflect_and_fix_progress_lines(self) -> None:
        reflect = format_update("reflect", {"pending_reflections": [{
            "before_iteration": 2, "round": 1, "critique": [{}, {}],
            "revision_status": "completed", "files_changed": [], "responses": [], "summary": "",
        }]}, None)
        self.assertEqual(reflect, "[reflect] before QA #2: 2 critique items, revision completed")
        state = self.state_with_findings()
        fixed = {**state["iterations"][0], "fix": {
            "status": "completed", "hit_step_cap": False, "steps_used": 7, "files_changed": [],
            "responses": [{"finding_id": "F1-1", "action": "disputed", "reason": "x"}],
            "error": None,
        }}
        line = format_update("fix", {"iterations": [fixed]}, None)
        self.assertEqual(line, "[fix] #1: completed (7 steps), answered 1 of 1 (1 disputed)")


if __name__ == "__main__":
    unittest.main()
