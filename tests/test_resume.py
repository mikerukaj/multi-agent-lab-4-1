"""Interrupted and resumable runs continue from their last checkpoint (FR-213a-d, SC-211).

Real agents run on content-aware fake models (tests/fakes/team_script.py). A run is
interrupted inside a chosen stage by requesting a stop from the model fake when that
stage's first request arrives, then resumed with fresh agents and models.
"""

import contextlib
import hashlib
import json
import signal
import tempfile
import time
import unittest
from pathlib import Path

from agents.qa_agent import QAAgent
from cost.ledger import RunBudget
from orchestration.checkpoint import (
    RunLock,
    run_dir,
    save_checkpoint,
    write_final_record,
)
from orchestration.services import RunServices
from orchestration.team import (
    ResumeRefused,
    exit_code_for,
    initial_team_state,
    resume_team,
    run_team,
)
from tests import TEST_ENV
from tests.fakes.team_script import (
    REQUIREMENT,
    TeamModel,
    all_requests,
    first_user_text,
    offline_coder,
    request_kind,
    scripted_team,
)

TASKS = 9  # More than the task limit, so the run goes through consolidation.
TRIGGERS = {
    "pm": (0, lambda r: request_kind(r) == "spec", "team"),
    "consolidate": (0, lambda r: request_kind(r) == "consolidate", "plan"),
    "coder": (1, lambda r: request_kind(r) == "loop" and "Task T2" in first_user_text(r),
              "build"),
    "reflect": (1, lambda r: request_kind(r) == "critique", "build"),
    "qa": (2, lambda r: request_kind(r) == "tests", "reflect"),
    "fix": (1, lambda r: request_kind(r) == "loop" and "You are fixing" in first_user_text(r),
            "qa_review"),
}
"""Node -> (which model sees it: pm 0, coder 1, qa 2; the request that starts it; the
stage of the checkpoint a resume continues from)."""


def tree_digest(root: Path) -> str:
    """A digest of every file under ``root`` (names, contents and modes)."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
            digest.update(str(path.stat().st_mode).encode())
    return digest.hexdigest()


class ResumeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        stack = contextlib.ExitStack()
        stack.enter_context(offline_coder())
        self.addCleanup(stack.close)

    def services(self, run_id: str | None = None) -> RunServices:
        return RunServices.create(run_id, env=TEST_ENV, sleep=lambda delay: None, response_cache=None)

    def models(self, trigger=None, services=None) -> tuple[TeamModel, ...]:
        models = [TeamModel(task_count=TASKS), TeamModel(task_count=TASKS),
                  TeamModel(task_count=TASKS, bug_in_first_review=True)]
        if trigger:
            index, matches, _ = trigger
            fired = []

            def stop_on(request) -> None:
                if not fired and matches(request):
                    fired.append(True)
                    services.request_stop(signal.SIGINT)

            models[index].on_request = stop_on
        return tuple(models)

    def start(self, models, services, **kwargs) -> dict:
        pm, coder, qa = scripted_team(self.root, services, *models)
        return run_team(REQUIREMENT, pm, coder, self.root, qa=qa, services=services, **kwargs)

    def resume(self, run_id: str, models=None, **kwargs) -> dict:
        services = self.services(run_id)
        models = models or self.models()
        pm, coder, qa = scripted_team(self.root, services, *models)
        self.resumed_models = models
        return resume_team(run_id, pm, coder, self.root, qa=qa, services=services, **kwargs)


class InterruptAndResumeTests(ResumeTestCase):
    def baseline(self) -> tuple[dict, dict, int]:
        """An uninterrupted run, the model calls each node made, and the total."""
        models, calls, seen = self.models(), {}, [0]

        def count(node: str, update: dict) -> None:
            now = all_requests(*models)
            calls.setdefault(node, []).append(now - seen[0])
            seen[0] = now

        final = self.start(models, self.services(), on_update=count)
        return final, calls, all_requests(*models)

    def test_a_run_interrupted_in_any_stage_resumes_to_the_same_end(self) -> None:
        baseline, node_calls, baseline_total = self.baseline()
        self.assertEqual(baseline["end_reason"], "complete")
        self.assertEqual(len(baseline["iterations"]), 2)
        for node, trigger in TRIGGERS.items():
            with self.subTest(node=node):
                services = self.services()
                models = self.models(trigger, services)
                first = self.start(models, services)
                self.assertEqual(first["end_reason"], "interrupted")
                self.assertEqual(exit_code_for(first, services.signal_received), 130)

                final = self.resume(first["run_id"])
                self.assertEqual(final["end_reason"], baseline["end_reason"])
                self.assertEqual(final["run_id"], first["run_id"])
                self.assertEqual(len(final["iterations"]), len(baseline["iterations"]))
                self.assertGreaterEqual(final["usage"]["input_tokens"],
                                        baseline["usage"]["input_tokens"])
                self.assertEqual(len(final["a2a_log"]), len(baseline["a2a_log"]))
                self.assertEqual([entry["sequence"] for entry in final["a2a_log"]],
                                 list(range(1, len(final["a2a_log"]) + 1)))
                attempt = final["resume_history"][-1]
                self.assertEqual(attempt["from_stage"], trigger[2])
                self.assertEqual(attempt["previous_end_reason"], "interrupted")
                # SC-211: only the interrupted stage's calls are made again.
                repeated = all_requests(*models, *self.resumed_models) - baseline_total
                stage = node_calls[node][1 if node == "coder" else 0]
                self.assertLessEqual(repeated, stage)

    def test_restored_and_removed_files_are_recorded(self) -> None:
        services = self.services()
        first = self.start(self.models(TRIGGERS["fix"], services), services)
        workspace = Path(first["workspace"])
        (workspace / "main.py").write_text("print('edited by hand')\n", encoding="utf-8")
        (workspace / "tests" / "test_main.py").write_text("def test(): assert 0\n", "utf-8")
        (workspace / "notes.txt").write_text("scratch", encoding="utf-8")
        final = self.resume(first["run_id"])
        attempt = final["resume_history"][-1]
        self.assertEqual(attempt["restored_files"], ["main.py", "tests/test_main.py"])
        # The interrupted attempt's partial QA report postdates the checkpoint too; the
        # resumed run writes a new one.
        self.assertEqual(attempt["removed_files"], [".qa/report.md", "notes.txt"])
        self.assertTrue((workspace / ".qa" / "report.md").exists())
        self.assertEqual(final["end_reason"], "complete")
        # QA's own copy of its suite came back with the checkpoint, so the restored
        # file matches it and QA reports no tampering.
        problems = [f["problem"] for f in final["iterations"][-1]["findings"]]
        self.assertFalse([p for p in problems if "changed outside QA" in p])

    def test_a_killed_run_without_a_final_record_resumes(self) -> None:
        services = self.services()
        first = self.start(self.models(TRIGGERS["reflect"], services), services)
        (run_dir(self.root, first["run_id"]) / "run.json").unlink()
        final = self.resume(first["run_id"])
        self.assertEqual(final["end_reason"], "complete")
        self.assertIsNone(final["resume_history"][-1]["previous_end_reason"])

    def test_an_a2a_failed_run_resumes_and_reruns_the_stage(self) -> None:
        class SlowOnce(QAAgent):
            calls = 0

            def run_iteration(self, context):
                SlowOnce.calls += 1
                time.sleep(1.0)
                raise AssertionError("cancelled work must never finish")

        services = self.services()
        pm, coder, qa = scripted_team(self.root, services, *self.models())
        qa.__class__ = SlowOnce
        first = run_team(REQUIREMENT, pm, coder, self.root, qa=qa, services=services,
                         a2a_timeout_seconds=0.3)
        self.assertEqual(first["end_reason"], "a2a_failed")
        self.assertEqual(exit_code_for(first), 3)
        final = self.resume(first["run_id"])
        self.assertEqual(final["end_reason"], "complete")
        self.assertEqual(final["resume_history"][-1]["from_stage"], "reflect")


class BudgetTests(ResumeTestCase):
    """A run stops at its budget, can be resumed with a higher one, and keeps its costs."""

    def costs_of_a_full_run(self) -> tuple[float, float]:
        """In an uninterrupted run: the spend once QA's first review is done, and the
        most expensive single call."""
        services = self.services()
        self.start(self.models(), services)
        calls = [event for event in services.events.events if event["event"] == "model_call"]
        spent, after_review = 0.0, None
        for call in calls:
            spent += call["details"]["cost_usd"]
            if call["stage"] == "qa_review" and after_review is None:
                after_review = spent  # QA's first call writes tests...
            elif call["stage"] == "qa_review":
                after_review = spent  # ...and its second reviews; keep the latest.
                break
        return after_review, max(call["details"]["cost_usd"] for call in calls)

    def test_the_cap_stops_the_run_and_a_higher_budget_resumes_it(self) -> None:
        after_review, largest_call = self.costs_of_a_full_run()
        cap = round(after_review * 1.01, 8)  # Reached in the fix round that follows.
        services = self.services()
        services.budget = RunBudget(cap)
        first = self.start(self.models(), services)
        self.assertEqual(first["end_reason"], "budget_exceeded")
        self.assertEqual(exit_code_for(first), 3)
        self.assertLessEqual(first["cost_usd"] - cap, largest_call)  # SC-206
        self.assertEqual(first["budget_usd"], cap)
        report = json.loads(Path(first["cost_report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(report["end_reason"], "budget_exceeded")
        # The final report makes no model call, so it is written even at the cap.
        text = (Path(first["workspace"]) / ".qa" / "report.md").read_text(encoding="utf-8")
        self.assertIn("partial: the run ended budget_exceeded", text)

        with self.assertRaisesRegex(ResumeRefused, "higher --budget"):
            self.resume(first["run_id"])
        with self.assertRaisesRegex(ResumeRefused, "higher --budget"):
            self.resume(first["run_id"], budget_usd=cap)
        final = self.resume(first["run_id"], budget_usd=cap * 10)
        self.assertEqual(final["end_reason"], "complete")
        self.assertEqual(final["budget_usd"], cap * 10)
        report = json.loads(Path(final["cost_report_path"]).read_text(encoding="utf-8"))
        self.assertEqual([a["end_reason"] for a in report["attempts"]],
                         ["budget_exceeded", "complete"])
        self.assertGreater(report["total"]["cost_usd"], first["cost_usd"])
        self.assertEqual(final["resume_history"][-1]["budget_after_usd"], cap * 10)


class RefusedResumeTests(ResumeTestCase):
    def fake_run(self, end_reason: str | None, **record_changes) -> str:
        """A run folder with a checkpoint and (unless ``None``) a final record."""
        run_id = f"run-{end_reason or 'killed'}"
        workspace = self.root / run_id
        workspace.mkdir()
        (workspace / "main.py").write_text("x = 1\n", encoding="utf-8")
        state = initial_team_state(REQUIREMENT, run_id, workspace)
        directory = run_dir(self.root, run_id)
        save_checkpoint(directory, "build", "dispatch", state, {"max_iterations": 5})
        if end_reason:
            write_final_record(directory, {**state, "status": "failed",
                                           "end_reason": end_reason, **record_changes})
        return run_id

    def assert_refused(self, run_id: str, pattern: str, **kwargs) -> None:
        before = tree_digest(self.root)
        with self.assertRaisesRegex(ResumeRefused, pattern):
            self.resume(run_id, **kwargs)
        self.assertEqual(tree_digest(self.root), before)

    def test_runs_that_ended_for_good_are_refused(self) -> None:
        for end_reason in ("complete", "iteration_limit", "rejected", "pm_error",
                           "qa_failed", "task_failed"):
            with self.subTest(end_reason=end_reason):
                self.assert_refused(self.fake_run(end_reason), end_reason)

    def test_a_run_that_is_still_going_is_refused(self) -> None:
        run_id = self.fake_run(None)
        with RunLock(run_dir(self.root, run_id)):
            self.assert_refused(run_id, "still running")

    def test_unusable_checkpoints_are_refused(self) -> None:
        run_id = self.fake_run("interrupted")
        path = run_dir(self.root, run_id) / "checkpoint.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        for change, pattern in (({"complete": False}, "incomplete"),
                                ({"format_version": 99}, "format")):
            with self.subTest(pattern=pattern):
                path.write_text(json.dumps({**record, **change}), encoding="utf-8")
                self.assert_refused(run_id, pattern)
        path.unlink()
        self.assert_refused(run_id, "no checkpoint")

    def test_changed_settings_are_refused(self) -> None:
        run_id = self.fake_run("interrupted")
        self.assert_refused(run_id, "max_iterations", settings_overrides={"max_iterations": 3})

    def test_a_budget_stop_needs_a_higher_budget(self) -> None:
        run_id = self.fake_run("budget_exceeded", budget_usd=1.0)
        self.assert_refused(run_id, "higher --budget")
        self.assert_refused(run_id, "higher --budget", budget_usd=0.5)


if __name__ == "__main__":
    unittest.main()
