"""Offline tests for the shared team state schema (orchestration/state.py)."""

import json
import typing
import unittest
from pathlib import Path

from orchestration.state import (
    FinalReportRequest,
    FixRequest,
    Iteration,
    Priority,
    QAContext,
    QAFinding,
    Task,
    TaskProgress,
    TeamState,
)
from orchestration.team import add_usage, initial_team_state

# Copied from contracts/task-handoff.schema.json ("required"); specs/ is not in git.
HANDOFF_KEYS = {"task_id", "title", "description", "acceptance_criteria", "priority"}

TEAM_STATE_KEYS = {
    "run_id",
    "workspace",
    "requirement",
    "spec",
    "tasks",
    "handoff",
    "task_progress",
    "code_output",
    "consolidation",
    "status",
    "error",
    "usage",
    "duration_seconds",
    "max_iterations",
    "iterations",
    "pending_reflections",
    "test_suite",
    "qa_report",
    "end_reason",
    "a2a_log",
    "agent_usage",
    "cost_usd",
    "budget_usd",
    "failure",
    "resume_from",
    "resume_history",
    "events_lost",
    "trace_url",
    "cost_report_path",
    "summary",
}

TASK_PROGRESS_KEYS = {
    "task_id",
    "status",
    "coder_status",
    "summary",
    "files_written",
    "steps_used",
    "duration_seconds",
    "error",
}


def sample_state() -> TeamState:
    task = Task(
        task_id="T1",
        title="Convert temperatures",
        description="Write convert.py.",
        acceptance_criteria=["convert.py 100 C prints 212.0 F"],
        priority="high",
    )
    return TeamState(
        run_id="20261017-120000-abcdef",
        workspace="/tmp/workspace/20261017-120000-abcdef",
        requirement="A temperature converter",
        spec={
            "name": "Temperature converter",
            "description": "Converts between Celsius and Fahrenheit.",
            "acceptance_criteria": ["Both directions work"],
            "assumptions": ["Input is a number and a unit letter"],
            "tasks": [task],
        },
        tasks=[task],
        handoff=None,
        task_progress={
            "T1": {
                "task_id": "T1",
                "status": "completed",
                "coder_status": "completed",
                "summary": "Wrote convert.py.",
                "files_written": ["convert.py"],
                "steps_used": 3,
                "duration_seconds": 12.5,
                "error": None,
            }
        },
        code_output=[{"task_id": "T1", "path": "convert.py", "content": "print(1)\n"}],
        consolidation={"proposed_count": 9, "final_count": 1, "method": "merge"},
        status="completed",
        error=None,
        usage={"input_tokens": 1200, "output_tokens": 300},
        duration_seconds=20.0,
        max_iterations=5,
        iterations=[],
        pending_reflections=[],
        test_suite={},
        qa_report=None,
        end_reason="complete",
        a2a_log=[],
    )


def sample_finding(number: str, kind: str = "failing_test") -> QAFinding:
    return QAFinding(
        finding_id=number,
        kind=kind,
        severity="blocking",
        rule=None,
        file="convert.py",
        location="convert() line 12",
        problem="100 C prints 211.9 F",
        expected="convert.py 100 C prints 212.0 F",
        likely_cause="Rounds before converting.",
        fix_instruction="Round only the final result to one decimal place.",
        test_ids=["tests/test_convert.py::test_boiling"],
        state="open",
        first_seen=1,
    )


def sample_iteration() -> Iteration:
    findings = [sample_finding("F1-1"), sample_finding("F1-2", kind="bug")]
    return Iteration(
        number=1,
        reflections=[
            {
                "before_iteration": 1,
                "round": 1,
                "critique": [
                    {
                        "item_id": "C1",
                        "kind": "edge_case",
                        "location": "convert()",
                        "description": "Negative input is not handled.",
                        "suggested_change": "Accept a leading minus sign.",
                    }
                ],
                "revision_status": "completed",
                "files_changed": ["convert.py"],
                "responses": [{"finding_id": "C1", "action": "fixed", "reason": ""}],
                "summary": "Handled negative input.",
            }
        ],
        disputes=[
            {
                "finding_id": "F1-2",
                "coder_reason": "The spec does not ask for Kelvin.",
                "decision": "keep",
                "qa_reason": "Criterion 2 names the input format.",
                "decided_in": 1,
            }
        ],
        test_changes=[
            {
                "path": "tests/test_convert.py",
                "reason": "initial",
                "finding_id": None,
                "criteria": ["Both directions work"],
            }
        ],
        test_run={
            "loaded": True,
            "exit_code": 1,
            "timed_out": False,
            "passed": 3,
            "failed": 1,
            "errors": 0,
            "skipped": 0,
            "cases": [
                {
                    "test_id": "tests/test_convert.py::test_boiling",
                    "file": "tests/test_convert.py",
                    "outcome": "failed",
                    "message": "assert '211.9 F' == '212.0 F'",
                    "details": "Traceback ...",
                }
            ],
            "output": "1 failed, 3 passed",
            "duration_seconds": 2.5,
        },
        findings=findings,
        verdict="fix_needed",
        fix={
            "status": "completed",
            "hit_step_cap": False,
            "steps_used": 4,
            "files_changed": ["convert.py"],
            "responses": [{"finding_id": "F1-1", "action": "fixed", "reason": ""}],
            "error": None,
        },
        review_task_id="3f2a",
        fix_task_id="9c1e",
    )


def full_state() -> TeamState:
    """A state with every increment-3 key filled in."""
    state = sample_state()
    iteration = sample_iteration()
    state["iterations"] = [iteration]
    state["test_suite"] = {"tests/test_convert.py": "def test_boiling(): ...\n"}
    state["qa_report"] = {
        "status": "failed",
        "end_reason": "iteration_limit",
        "iterations_run": 1,
        "max_iterations": 1,
        "final_passed": 3,
        "final_failed": 1,
        "unresolved": iteration["findings"],
        "advisory": [],
        "per_iteration": ["#1: 3 passed, 1 failed, 2 blocking"],
        "report_path": ".qa/report.md",
    }
    state["a2a_log"] = [
        {
            "sequence": 1,
            "sender": "coder",
            "recipient": "qa-debugger",
            "method": "SendMessage",
            "skill_id": "review-program",
            "context_id": state["run_id"],
            "task_id": "3f2a",
            "reference_task_ids": [],
            "states": [
                {"state": "TASK_STATE_SUBMITTED", "timestamp": "2026-10-19T14:03:22.418Z"},
                {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-10-19T14:04:10.002Z"},
            ],
            "outcome": "TASK_STATE_COMPLETED",
            "started_at": "2026-10-19T14:03:22.418Z",
            "finished_at": "2026-10-19T14:04:10.002Z",
        },
        {
            "sequence": 2,
            "sender": "qa-debugger",
            "recipient": "coder",
            "method": "SendMessage",
            "skill_id": "fix-program",
            "context_id": state["run_id"],
            "task_id": "9c1e",
            "reference_task_ids": ["3f2a"],
            "states": [{"state": "TASK_STATE_COMPLETED", "timestamp": "2026-10-19T14:05:00.000Z"}],
            "outcome": "TASK_STATE_COMPLETED",
            "started_at": "2026-10-19T14:04:10.100Z",
            "finished_at": "2026-10-19T14:05:00.000Z",
        },
    ]
    return state


class TeamStateSchemaTests(unittest.TestCase):
    def test_task_has_exactly_the_handoff_contract_keys(self) -> None:
        self.assertEqual(Task.__required_keys__, HANDOFF_KEYS)

    def test_priority_values_match_the_contract(self) -> None:
        self.assertEqual(typing.get_args(Priority), ("high", "medium", "low"))

    def test_team_state_and_task_progress_fields(self) -> None:
        self.assertEqual(TeamState.__required_keys__, TEAM_STATE_KEYS)
        self.assertEqual(TaskProgress.__required_keys__, TASK_PROGRESS_KEYS)

    def test_state_round_trips_through_json(self) -> None:
        state = sample_state()
        self.assertEqual(json.loads(json.dumps(state)), state)

    def test_state_with_every_increment_3_key_round_trips_through_json(self) -> None:
        state = full_state()
        self.assertEqual(json.loads(json.dumps(state)), state)

    def test_request_payloads_round_trip_through_json(self) -> None:
        state = full_state()
        payloads = [
            QAContext(
                iteration=2,
                workspace=state["workspace"],
                spec=state["spec"],
                source_files=["convert.py"],
                coder_notes=["Wrote convert.py."],
                main_file="convert.py",
                test_suite=state["test_suite"],
                previous_findings=state["iterations"][0]["findings"],
                responses=[{"finding_id": "F1-1", "action": "fixed", "reason": ""}],
            ),
            FixRequest(
                workspace=state["workspace"],
                iteration=1,
                answer_ids=["F1-1", "F1-2"],
                findings=state["iterations"][0]["findings"],
            ),
            FinalReportRequest(
                run_id=state["run_id"],
                workspace=state["workspace"],
                status="failed",
                end_reason="iteration_limit",
                max_iterations=1,
                iterations=state["iterations"],
            ),
        ]
        for payload in payloads:
            self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_initial_state_sets_the_increment_3_defaults(self) -> None:
        state = initial_team_state("Build it", "run-1", Path("/tmp/ws"), max_iterations=3)
        self.assertEqual(state["max_iterations"], 3)
        self.assertEqual(state["iterations"], [])
        self.assertEqual(state["pending_reflections"], [])
        self.assertEqual(state["test_suite"], {})
        self.assertIsNone(state["qa_report"])
        self.assertIsNone(state["end_reason"])
        self.assertEqual(state["a2a_log"], [])
        self.assertEqual(initial_team_state("x", "r", Path("/w"))["max_iterations"], 5)

    def test_initial_state_sets_the_hardening_defaults(self) -> None:
        state = initial_team_state("Build it", "run-1", Path("/tmp/ws"))
        self.assertEqual(
            state["usage"],
            {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        )
        self.assertEqual(set(state["agent_usage"]), {"pm", "coder", "qa"})
        for agent, usage in state["agent_usage"].items():
            self.assertEqual(usage["agent"], agent)
            self.assertEqual(usage["cost_usd"], 0.0)
            self.assertEqual(usage["model_calls"], 0)
        for key in ("cost_usd", "budget_usd", "failure", "resume_from", "trace_url",
                    "cost_report_path", "summary"):
            self.assertIsNone(state[key], key)
        self.assertEqual(state["resume_history"], [])
        self.assertFalse(state["events_lost"])

    def test_initial_state_round_trips_through_json_unchanged(self) -> None:
        state = initial_team_state("Build it", "run-1", Path("/tmp/ws"))
        self.assertEqual(json.loads(json.dumps(state)), state)


class AddUsageTests(unittest.TestCase):
    def test_all_four_counters_are_summed(self) -> None:
        total = add_usage(
            {"input_tokens": 1, "output_tokens": 2,
             "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4},
            {"input_tokens": 10, "output_tokens": 20,
             "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40},
        )
        self.assertEqual(
            total,
            {"input_tokens": 11, "output_tokens": 22,
             "cache_creation_input_tokens": 33, "cache_read_input_tokens": 44},
        )

    def test_records_without_cache_keys_count_them_as_zero(self) -> None:
        total = add_usage({"input_tokens": 5, "output_tokens": 6},
                          {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 7})
        self.assertEqual(
            total,
            {"input_tokens": 6, "output_tokens": 7,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 7},
        )


if __name__ == "__main__":
    unittest.main()
