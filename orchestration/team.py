"""The PM -> Coder <-> QA team: one LangGraph graph over the shared ``TeamState``.

The PM writes the spec and tasks, publishes one task at a time on the
``handoff`` key, and the Coder builds each task in one shared workspace and
reports back (specs/001-autonomous-dev-team/contracts/handoff-protocol.md).
Every exchange between the Coder and QA then travels as an A2A message through
the asyncio broker: the graph sends each one on behalf of the agent whose turn it
is, and never calls QA directly (specs/002-qa-feedback-loop-mcp/contracts/).

Before each QA review the Coder reflects on its program; after a ``fix_needed``
verdict QA sends its fix instruction list to the Coder, and the loop repeats until
QA says ``complete`` or the iteration limit is reached.

Production hardening (003) makes every run end cleanly and resumable: each stage saves
a checkpoint, the run holds a lock and always writes a final record, SIGINT/SIGTERM stop
it at the next call boundary, and ``--resume RUN_ID`` continues it from its last
completed stage (specs/003-production-hardening/contracts/cli.md).

Usage:
    python -m orchestration.team "<requirement>" [--max-iterations N]
        [--max-steps N] [--timeout SECONDS]
    python -m orchestration.team --resume RUN_ID
"""

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

from agents.coder_agent import CoderAgent, CoderConfig, print_step_progress
from agents.llm import add_usage, zero_usage
from agents.pm_agent import MAX_TASKS, PMAgent, PMConfig, merge_tasks
from agents.qa_agent import QAAgent, QAConfig
from messaging.a2a import (
    A2AError,
    Message,
    Task as A2ATask,
    artifact_data,
    data_part,
    text_part,
    user_message,
)
from messaging.broker import A2ABroker, A2AClient
from messaging.endpoints import (
    CODER_AGENT,
    FIX_SKILL,
    QA_AGENT,
    REPORT_SKILL,
    REVIEW_SKILL,
    register_team_agents,
)
from cost.ledger import RunBudget
from cost.pricing import is_priced
from cost.report import write_cost_report
from observability.events import progress_line, read_events, summarise
from observability.redaction import Redactor
from orchestration.checkpoint import (
    CheckpointError,
    RunLock,
    is_locked,
    load_checkpoint,
    read_final_record,
    restore_workspace,
    run_dir,
    save_checkpoint,
    write_final_record,
)
from orchestration.health import check_health, failing_items
from orchestration.services import RunServices
from orchestration.state import (
    RESUMABLE_END_REASONS,
    AgentUsage,
    CodeOutput,
    CoderFix,
    Consolidation,
    DependencyFailure,
    FinalReportRequest,
    FixRequest,
    Iteration,
    QAContext,
    ResumeAttempt,
    Task,
    TaskProgress,
    TeamState,
    TechnicalSpec,
    TokenUsage,
)
from resilience.classify import BUDGET_EXCEEDED, is_unavailable
from tools.file_tools import read_file
from tools.mcp_client import McpUnavailableError
from tools.python_exec import (
    SandboxUnavailableError,
    cleanup_stale_containers,
    kill_run_containers,
)

MAX_ITERATIONS_CAP = 10
DEFAULT_MAX_ITERATIONS = 5
# pm + consolidate + (dispatch + coder) per task + (reflect + qa + fix) per iteration
# + finish, with room to spare.
RECURSION_LIMIT = 2 * MAX_TASKS + 3 * MAX_ITERATIONS_CAP + 6
DEFAULT_A2A_TIMEOUT_SECONDS = 1800.0
CLIENT_NAMES = ("coder", QA_AGENT, "team")
AGENT_NAMES = ("pm", "coder", "qa")
EVENTS_FILE = "events.jsonl"
RUN_ENDING_REASONS = ("dependency_unavailable", "interrupted", "budget_exceeded")
"""End reasons that stop the run whatever state its tasks are in."""
EXIT_COMPLETED, EXIT_FAILED, EXIT_USAGE, EXIT_RESUMABLE = 0, 1, 2, 3
GRAPH_NODES = ("pm", "consolidate", "dispatch", "coder", "reflect", "qa", "fix", "finish")


class ResumeRefused(Exception):
    """A run can't be resumed; the message says why, and no file was changed."""


def initial_team_state(
    requirement: str, run_id: str, workspace: Path, max_iterations: int = 5
) -> TeamState:
    """Return the state a run starts from.

    Raises:
        ValueError: if ``requirement`` is empty.
    """
    if not requirement.strip():
        raise ValueError("requirement must not be empty")
    return TeamState(
        run_id=run_id,
        workspace=str(workspace),
        requirement=requirement,
        spec=None,
        tasks=[],
        handoff=None,
        task_progress={},
        code_output=[],
        consolidation=None,
        status="running",
        error=None,
        usage=TokenUsage(**zero_usage()),
        duration_seconds=0.0,
        max_iterations=max_iterations,
        iterations=[],
        pending_reflections=[],
        test_suite={},
        qa_report=None,
        end_reason=None,
        a2a_log=[],
        agent_usage={agent: empty_agent_usage(agent) for agent in AGENT_NAMES},
        cost_usd=None,
        budget_usd=None,
        failure=None,
        resume_from=None,
        resume_history=[],
        events_lost=False,
        trace_url=None,
        cost_report_path=None,
        summary=None,
    )


def empty_agent_usage(agent: str) -> AgentUsage:
    """Return an ``AgentUsage`` for ``agent`` with every counter at zero."""
    return AgentUsage(
        agent=agent, models=[], model_calls=0, cached_replays=0, prompt_tokens=0,
        completion_tokens=0, cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.0,
        retries=0, errors=0, tool_calls=0, duration_ms=0,
    )


def pending_progress(tasks: list[Task]) -> dict[str, TaskProgress]:
    """Return a fresh ``pending`` progress entry for every task."""
    return {
        task["task_id"]: TaskProgress(
            task_id=task["task_id"],
            status="pending",
            coder_status=None,
            summary="",
            files_written=[],
            steps_used=0,
            duration_seconds=0.0,
            error=None,
        )
        for task in tasks
    }


def run_ending(failure: DependencyFailure | None) -> str | None:
    """The end reason a node's failure forces on the whole run, if any.

    A spent budget ends it ``budget_exceeded`` (FR-223); an unavailable dependency
    ``dependency_unavailable`` (FR-209). A permanent failure forces nothing: the node
    keeps the agent's own end reason (FR-205).
    """
    if failure is None:
        return None
    if failure["error_type"] == BUDGET_EXCEEDED:
        return "budget_exceeded"
    return "dependency_unavailable" if is_unavailable(failure) else None


def ending_update(error: str, failure: DependencyFailure) -> dict:
    """The update that ends a run for ``failure`` (``run_ending`` must name a reason)."""
    return {"status": "failed", "end_reason": run_ending(failure), "error": error,
            "failure": failure}


def pm_node(state: TeamState, pm: PMAgent) -> dict:
    """PM side: write the spec and task list into shared state."""
    result = pm.write_spec(state["requirement"])
    usage = add_usage(state["usage"], result.usage)
    failure = getattr(result, "failure", None)
    if run_ending(failure):
        return {**ending_update(result.error, failure), "usage": usage}
    if result.error:
        return {"status": "error", "error": result.error, "usage": usage, "failure": failure}
    if result.rejection_reason:
        return {"status": "rejected", "error": result.rejection_reason, "usage": usage}
    return {
        "spec": result.spec,
        "tasks": result.spec["tasks"],
        "task_progress": pending_progress(result.spec["tasks"]),
        "usage": usage,
    }


def consolidate_node(state: TeamState, pm: PMAgent) -> dict:
    """PM side: bring a plan of more than ``MAX_TASKS`` tasks down to the limit.

    The PM's merge is used when it passed ``PMAgent.consolidate``'s checks;
    otherwise the original tasks are merged deterministically (FR-006a).
    """
    result = pm.consolidate(state["spec"], state["requirement"])
    usage = add_usage(state["usage"], result.usage)
    failure = getattr(result, "failure", None)
    if run_ending(failure):
        return {**ending_update(result.error, failure), "usage": usage}
    if result.error:
        tasks, method = merge_tasks(state["tasks"]), "merge"
    else:
        tasks, method = result.spec["tasks"], "pm"
    return {
        "spec": {**state["spec"], "tasks": tasks},
        "tasks": tasks,
        "task_progress": pending_progress(tasks),
        "consolidation": Consolidation(
            proposed_count=len(state["tasks"]), final_count=len(tasks), method=method
        ),
        "usage": usage,
    }


def route_after_consolidate(state: TeamState) -> str:
    """Hand off the consolidated plan, unless the PM could not be reached."""
    return "dispatch" if state["status"] == "running" else "finish"


def route_after_pm(state: TeamState) -> str:
    """Stop on rejection or error, consolidate an oversized plan, else hand off."""
    if state["status"] != "running":
        return "finish"
    if len(state["tasks"]) > MAX_TASKS:
        return "consolidate"
    return "dispatch"


def route_after_dispatch(state: TeamState) -> str:
    """Build the handed-off task, unless the run is stopping."""
    return "coder" if state["status"] == "running" else "finish"


def dispatch_node(state: TeamState) -> dict:
    """PM side: publish the next pending task, in list order, on ``handoff``."""
    progress = state["task_progress"]
    task = next(t for t in state["tasks"] if progress[t["task_id"]]["status"] == "pending")
    started = TaskProgress(**progress[task["task_id"]])
    started["status"] = "in_progress"
    return {"handoff": Task(**task), "task_progress": {**progress, task["task_id"]: started}}


def render_coder_task(
    handoff: Task, spec: TechnicalSpec, position: int, total: int, existing_files: list[str]
) -> str:
    """Turn a handoff into the Coder's task text (layout: contracts/handoff-protocol.md)."""
    criteria = "\n".join(f"- {criterion}" for criterion in handoff["acceptance_criteria"])
    parts = [
        f"You are building: {spec['name']}. {spec['description']}",
        f"Task {handoff['task_id']} of {total} (priority: {handoff['priority']}): "
        f"{handoff['title']}\n{handoff['description']}",
        f"Acceptance criteria:\n{criteria}",
    ]
    if existing_files:
        parts.append(
            f"Files already in the workspace from earlier tasks: {', '.join(existing_files)}.\n"
            "Read a file before you change it, and keep earlier tasks' behavior working."
        )
    return "\n\n".join(parts)


def _workspace_files(workspace: Path) -> list[str]:
    """Workspace-relative paths of the regular files, skipping hidden names."""
    relative = (path.relative_to(workspace) for path in workspace.rglob("*") if path.is_file())
    return sorted(
        str(path) for path in relative if not any(part.startswith(".") for part in path.parts)
    )


def coder_node(state: TeamState, coder) -> dict:
    """Coder side: build the task on ``handoff`` and report progress and code back."""
    handoff, workspace = state["handoff"], Path(state["workspace"])
    position = [task["task_id"] for task in state["tasks"]].index(handoff["task_id"]) + 1
    text = render_coder_task(
        handoff, state["spec"], position, len(state["tasks"]), _workspace_files(workspace)
    )
    result = coder.run(text, workspace)
    progress = TaskProgress(
        task_id=handoff["task_id"],
        status="completed" if result.status == "completed" else "failed",
        coder_status=result.status,
        summary=result.explanation,
        files_written=list(result.files_written),
        steps_used=result.steps_used,
        duration_seconds=result.duration_seconds,
        error=result.error,
    )
    update = {
        "handoff": None,
        "task_progress": {**state["task_progress"], handoff["task_id"]: progress},
        "code_output": state["code_output"] + _read_outputs(workspace, handoff, result),
        "usage": add_usage(state["usage"], TokenUsage(**result.usage)),
    }
    failure = getattr(result, "failure", None)
    if run_ending(failure):
        reason = f"Task {handoff['task_id']}: {result.error}"
        return {**update, **ending_update(reason, failure)}
    return {**update, "failure": failure} if failure else update


def _read_outputs(workspace: Path, handoff: Task, result) -> list[CodeOutput]:
    """Read each file the task wrote from disk (ground truth, not the model's claim)."""
    outputs = []
    for path in result.files_written:
        content, missing = read_file(workspace, path)
        if not missing:
            outputs.append(CodeOutput(task_id=handoff["task_id"], path=path, content=content))
    return outputs


def route_after_coder(state: TeamState) -> str:
    """Hand off the next task; stop on a failed task (FR-009a); go to QA once all are built."""
    if state["status"] != "running":
        return "finish"
    statuses = [progress["status"] for progress in state["task_progress"].values()]
    if "failed" in statuses:
        return "finish"
    if "pending" in statuses:
        return "dispatch"
    return "reflect"


def team_clients(broker: A2ABroker, earlier: list | None = None) -> dict[str, A2AClient]:
    """Return one A2A client per sender the graph sends on behalf of.

    ``earlier`` is the A2A log carried from a run's earlier attempts: a resumed run's
    log keeps them, followed by this attempt's exchanges (FR-213a).
    """
    clients = {name: broker.client(name) for name in CLIENT_NAMES}
    if not earlier:
        return clients
    return {name: CarriedLogClient(client, earlier) for name, client in clients.items()}


class CarriedLogClient:
    """An A2A client whose exchange log starts with a resumed run's earlier exchanges."""

    def __init__(self, client: A2AClient, earlier: list) -> None:
        self._client = client
        self._earlier = list(earlier)

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def exchanges(self) -> list[dict]:
        """The earlier attempts' exchanges, then this attempt's, numbered on from them."""
        offset = len(self._earlier)
        current = [{**entry, "sequence": entry["sequence"] + offset}
                   for entry in self._client.exchanges()]
        return self._earlier + current


def build_qa_context(state: TeamState) -> QAContext:
    """Build the ``review-program`` payload from the shared state (JSON only)."""
    iterations = state["iterations"]
    previous = iterations[-1] if iterations else None
    notes = [state["task_progress"][task["task_id"]]["summary"] for task in state["tasks"]]
    notes += [reflection["summary"] for reflection in state["pending_reflections"]]
    return QAContext(
        iteration=len(iterations) + 1,
        workspace=state["workspace"],
        spec=state["spec"],
        source_files=[
            path for path in _workspace_files(Path(state["workspace"]))
            if not path.startswith("tests/")
        ],
        coder_notes=notes,
        main_file=_main_file(state["code_output"]),
        test_suite=state["test_suite"],
        previous_findings=[
            finding for finding in (previous["findings"] if previous else [])
            if finding["state"] in ("open", "disputed")
        ],
        responses=(previous["fix"]["responses"] if previous and previous["fix"] else []),
    )


def _main_file(outputs: list[CodeOutput]) -> str | None:
    """The last Python file written that has a ``__main__`` guard, if any."""
    candidates = [
        output["path"] for output in outputs
        if output["path"].endswith(".py") and "__main__" in output["content"]
    ]
    return candidates[-1] if candidates else None


def review_message(state: TeamState, trace: dict | None = None) -> Message:
    """The Coder's A2A request asking QA to review the program.

    ``trace`` holds the trace headers QA's spans continue from (FR-214a).
    """
    context = build_qa_context(state)
    return user_message(
        [text_part(f"Program ready for QA review #{context['iteration']}."), data_part(context)],
        context_id=state["run_id"],
        metadata={"skillId": REVIEW_SKILL, "sender": "coder", "trace": trace or {}},
    )


def report_message(
    state: TeamState, status: str, end_reason: str, trace: dict | None = None
) -> Message:
    """The team's A2A request asking QA for its final report."""
    request = FinalReportRequest(
        run_id=state["run_id"],
        workspace=state["workspace"],
        status=status,
        end_reason=end_reason,
        max_iterations=state["max_iterations"],
        iterations=state["iterations"],
    )
    return user_message(
        [text_part("Please write the final QA report."), data_part(request)],
        context_id=state["run_id"],
        metadata={"skillId": REPORT_SKILL, "sender": "team", "trace": trace or {}},
    )


def qa_node(
    state: TeamState, clients: dict[str, A2AClient], services: RunServices | None = None
) -> dict:
    """Coder side: send the program to QA over A2A and record the iteration."""
    client = clients["coder"]
    try:
        message = review_message(state, _trace_headers(services))
        task = _a2a_send(client, QA_AGENT, message, services, "qa_review")
        outcome = task["status"]["state"]
        if outcome == "TASK_STATE_FAILED":
            return _review_failed(client, task)
        if outcome != "TASK_STATE_COMPLETED":
            reason = f"QA review {outcome}: {_status_reason(task)}"
            return _ended(client, "error", "a2a_failed", reason)
        result = artifact_data(task, "qa-iteration")
    except A2AError as error:
        return _ended(client, "error", "a2a_failed", f"QA review request failed: {error.detail}")
    iteration = Iteration(
        number=len(state["iterations"]) + 1,
        reflections=state["pending_reflections"],
        disputes=result["disputes"],
        test_changes=result["test_changes"],
        test_run=result["test_run"],
        findings=result["findings"],
        verdict=result["verdict"],
        fix=None,
        review_task_id=task["id"],
        fix_task_id=None,
    )
    return {
        "iterations": state["iterations"] + [iteration],
        "pending_reflections": [],
        "test_suite": result["test_suite"],
        "usage": add_usage(state["usage"], TokenUsage(**result["usage"])),
        "a2a_log": client.exchanges(),
    }


def _review_failed(client: A2AClient, task: A2ATask) -> dict:
    """The update for a QA review whose task failed: ``qa_failed``, unless its failure
    forces a run-wide end (an unavailable dependency or a spent budget)."""
    failure = _failed_task_failure(task, "qa-iteration")
    if run_ending(failure):
        return {**ending_update(_status_reason(task), failure), "a2a_log": client.exchanges()}
    return {**_ended(client, "error", "qa_failed", _status_reason(task)), "failure": failure}


def _ended(client: A2AClient, status: str, end_reason: str, error: str) -> dict:
    return {
        "status": status, "end_reason": end_reason, "error": error,
        "a2a_log": client.exchanges(),
    }


def _failed_task_failure(task: A2ATask, artifact_name: str) -> DependencyFailure | None:
    """The ``DependencyFailure`` a failed task reported in its artifact, if any."""
    for item in task.get("artifacts", []):
        if item.get("name") != artifact_name:
            continue
        if item.get("metadata", {}).get("failure"):
            return item["metadata"]["failure"]
        data = next((part["data"] for part in item["parts"] if "data" in part), {})
        return data.get("failure") if isinstance(data, dict) else None
    return None


def _status_reason(task: A2ATask) -> str:
    message = task["status"].get("message")
    return "".join(part.get("text", "") for part in message["parts"]) if message else ""


def route_after_qa(state: TeamState) -> str:
    """Loop to a fix round only while running, fix is needed, and iterations remain.

    This is the convergence safeguard (FR-114, SC-101): the run can never use more
    QA reviews than ``max_iterations``.
    """
    if state["status"] != "running":
        return "finish"
    if state["iterations"][-1]["verdict"] != "fix_needed":
        return "finish"
    if len(state["iterations"]) >= state["max_iterations"]:
        return "finish"
    return "fix"


def route_after_reflect(state: TeamState) -> str:
    """Hand the reflected program to QA unless reflection failed."""
    return "qa" if state["status"] == "running" else "finish"


def route_after_fix(state: TeamState) -> str:
    """Reflect again before the next review unless the fix round failed."""
    return "reflect" if state["status"] == "running" else "finish"


def reflect_node(state: TeamState, coder) -> dict:
    """Coder side: critique and revise the program before QA sees it (FR-126)."""
    number = len(state["iterations"]) + 1
    reflection = coder.reflect(render_reflection_brief(state), Path(state["workspace"]))
    usage = add_usage(state["usage"], TokenUsage(**reflection.usage))
    failure = getattr(reflection, "failure", None)
    if run_ending(failure):
        reason = f"Self-reflection before QA review #{number}: {reflection.result.error}"
        return {**ending_update(reason, failure), "usage": usage}
    if reflection.status in ("refused", "error"):
        reason = reflection.result.error or reflection.result.explanation
        error = (
            f"Self-reflection before QA review #{number} failed ({reflection.status}): {reason}"
        )
        return {"status": "failed", "end_reason": "reflection_failed", "error": error,
                "usage": usage, "failure": failure}
    rounds = [{**item, "before_iteration": number} for item in reflection.rounds]
    return {"pending_reflections": rounds, "usage": usage}


def fix_node(
    state: TeamState, clients: dict[str, A2AClient], services: RunServices | None = None
) -> dict:
    """QA side: send the fix instruction list to the Coder over A2A and record its answer."""
    client = clients[QA_AGENT]
    last = state["iterations"][-1]
    try:
        message = fix_message(state, _trace_headers(services))
        task = _a2a_send(client, CODER_AGENT, message, services, "fix")
        outcome = task["status"]["state"]
        if outcome == "TASK_STATE_FAILED":
            error = f"Fix round after QA review #{last['number']} failed: {_status_reason(task)}"
            failure = _failed_task_failure(task, "coder-fix")
            if run_ending(failure):
                return {**ending_update(error, failure), "a2a_log": client.exchanges()}
            return {**_ended(client, "failed", "coder_failed", error), "failure": failure}
        if outcome != "TASK_STATE_COMPLETED":
            reason = f"Fix request {outcome}: {_status_reason(task)}"
            return _ended(client, "error", "a2a_failed", reason)
        fix = CoderFix(**artifact_data(task, "coder-fix"))
    except A2AError as error:
        return _ended(client, "error", "a2a_failed", f"Fix request failed: {error.detail}")
    updated = Iteration(**{**last, "fix": fix, "fix_task_id": task["id"]})
    return {
        "iterations": state["iterations"][:-1] + [updated],
        "usage": add_usage(state["usage"], TokenUsage(**_artifact_usage(task, "coder-fix"))),
        "a2a_log": client.exchanges(),
    }


def _trace_headers(services: RunServices | None) -> dict:
    return services.tracer.headers() if services else {}


def _a2a_send(client, recipient: str, message: Message, services, stage: str) -> A2ATask:
    """Send one A2A request and record it as an ``a2a_exchange`` event (FR-216).

    Raises:
        A2AError: as ``A2AClient.send_message`` does.
    """
    skill = message.get("metadata", {}).get("skillId")
    try:
        task = client.send_message(recipient, message)
    except A2AError as error:
        if services:
            services.events.emit("a2a_exchange", stage=stage, severity="warning",
                                 outcome=error.code, skill=skill, recipient=recipient)
        raise
    if services:
        outcome = task["status"]["state"].removeprefix("TASK_STATE_").lower()
        services.events.emit("a2a_exchange", stage=stage, outcome=outcome, skill=skill,
                             recipient=recipient, task=task["id"])
    return task


def _artifact_usage(task: A2ATask, name: str) -> dict:
    item = next(item for item in task["artifacts"] if item.get("name") == name)
    return item.get("metadata", {}).get("usage", {"input_tokens": 0, "output_tokens": 0})


def fix_message(state: TeamState, trace: dict | None = None) -> Message:
    """QA's A2A request asking the Coder to fix the last review's findings."""
    last = state["iterations"][-1]
    request = FixRequest(
        workspace=state["workspace"],
        iteration=last["number"],
        answer_ids=[finding["finding_id"] for finding in _blocking(last)],
        findings=last["findings"],
    )
    return user_message(
        [text_part(render_fix_brief(state)), data_part(request)],
        context_id=state["run_id"],
        metadata={"skillId": FIX_SKILL, "sender": QA_AGENT, "trace": trace or {}},
        reference_task_ids=[last["review_task_id"]],
    )


def _blocking(iteration: Iteration) -> list:
    return [
        finding for finding in iteration["findings"]
        if finding["severity"] == "blocking" and finding["state"] in ("open", "disputed")
    ]


def _criteria(spec: TechnicalSpec) -> list[str]:
    criteria = list(spec["acceptance_criteria"])
    for task in spec["tasks"]:
        criteria += task["acceptance_criteria"]
    return criteria


def render_reflection_brief(state: TeamState) -> str:
    """The brief the Coder reflects against: the spec, its criteria, and every task."""
    spec = state["spec"]
    tasks = "\n".join(
        f"- {task['task_id']} {task['title']}: " + "; ".join(task["acceptance_criteria"])
        for task in spec["tasks"]
    )
    criteria = "\n".join(f"- {criterion}" for criterion in spec["acceptance_criteria"])
    return (
        f"{spec['name']}: {spec['description']}\n\nAcceptance criteria:\n{criteria}\n\n"
        f"Tasks:\n{tasks}\n\n"
        "Review the program in the workspace against this specification before it goes to QA."
    )


def render_fix_brief(state: TeamState) -> str:
    """The Coder's task text for a fix round (contracts/feedback-loop.md, "Briefs")."""
    last = state["iterations"][-1]
    run = last["test_run"]
    criteria = "\n".join(f"- {criterion}" for criterion in _criteria(state["spec"]))
    blocking = _blocking(last)
    advisory = [
        finding for finding in last["findings"]
        if finding["severity"] == "advisory" and finding["state"] == "open"
    ]
    lines = [
        f"You are fixing: {state['spec']['name']}. QA review #{last['number']} of "
        f"{state['max_iterations']} found problems.",
        f"Acceptance criteria:\n{criteria}",
        f"Test results: {run['passed']} passed, {run['failed']} failed, {run['errors']} errors.",
        "",
        "Blocking findings (fix every one, or dispute it with a reason from the specification):",
    ]
    lines += [_finding_block(finding) for finding in blocking]
    if advisory:
        lines.append("Advisory findings (optional):")
        lines += [_finding_block(finding) for finding in advisory]
    lines += [
        "",
        "Tests are in tests/. You may read them but not change them.",
        "Answer every F... ID above in finding_responses.",
    ]
    return "\n".join(lines)


def _finding_block(finding) -> str:
    block = [
        f"[{finding['finding_id']}] {finding['file']} {finding['location']}".rstrip(),
        f"  Problem: {finding['problem']}",
        f"  Expected: {finding['expected']}",
        f"  Likely cause: {finding['likely_cause']}",
        f"  Fix: {finding['fix_instruction']}",
    ]
    if finding["test_ids"]:
        block.append(f"  Failing tests: {', '.join(finding['test_ids'])}")
    return "\n".join(block)


def finish_node(
    state: TeamState, clients: dict[str, A2AClient], services: RunServices | None = None
) -> dict:
    """Decide the run's result, then ask QA for its final report if QA ever ran.

    The report makes no model call, so it is asked for even when the run is
    interrupted or over budget; it is then marked partial by its end reason.
    """
    update = {**_final_result(state), "resume_from": None}
    if not state["iterations"]:
        return update
    client = clients["team"]
    status, end_reason = update["status"], update["end_reason"]
    try:
        message = report_message(state, status, end_reason, _trace_headers(services))
        task = _a2a_send(client, QA_AGENT, message, services, "final_report")
        if task["status"]["state"] != "TASK_STATE_COMPLETED":
            raise A2AError("InternalError", f"{task['status']['state']}: {_status_reason(task)}")
        update["qa_report"] = artifact_data(task, "qa-report")
    except A2AError as error:
        cause = f"final report missing: {error.detail}"
        update["error"] = f"{update['error']}; {cause}" if update.get("error") else cause
    update["a2a_log"] = client.exchanges()
    return update


def _final_result(state: TeamState) -> dict:
    """Return the run's final status, end reason and error, and skip unbuilt tasks."""
    if state["status"] in ("rejected", "error"):
        default = "rejected" if state["status"] == "rejected" else "pm_error"
        reason = state["end_reason"] or default
        return {"handoff": None, "status": state["status"], "end_reason": reason,
                "error": state["error"]}
    progress = {
        task_id: TaskProgress(**{**entry, "status": "skipped"})
        if entry["status"] == "pending"
        else entry
        for task_id, entry in state["task_progress"].items()
    }
    update = {"handoff": None, "task_progress": progress}
    if state["status"] == "failed" and state["end_reason"] in RUN_ENDING_REASONS:
        # Checked before failed tasks: a task fails too when the run is stopped under it.
        return {**update, "status": "failed", "end_reason": state["end_reason"],
                "error": state["error"]}
    in_order = [progress[task["task_id"]] for task in state["tasks"]]
    failed = [entry for entry in in_order if entry["status"] == "failed"]
    if failed:
        first = failed[0]
        reason = first["error"] or first["summary"]
        error = f"Task {first['task_id']} failed ({first['coder_status']}): {reason}"
        return {**update, "status": "failed", "end_reason": "task_failed", "error": error}
    if state["status"] == "failed":
        return {**update, "status": "failed", "end_reason": state["end_reason"],
                "error": state["error"]}
    last = state["iterations"][-1] if state["iterations"] else None
    if last is None or last["verdict"] == "complete":
        return {**update, "status": "completed", "end_reason": "complete", "error": None}
    blocking = sum(1 for finding in last["findings"] if finding["severity"] == "blocking")
    error = (
        f"Iteration limit reached ({len(state['iterations'])} of {state['max_iterations']}) "
        f"with {blocking} unresolved blocking findings"
    )
    return {**update, "status": "failed", "end_reason": "iteration_limit", "error": error}


StageCallback = Callable[[str, str, str, dict], None]
"""``on_stage(node, stage, next_node, state)``, called after a stage leaves the run going."""


def build_team_graph(
    pm: PMAgent,
    coder,
    clients: dict[str, A2AClient],
    services: RunServices | None = None,
    on_stage: StageCallback | None = None,
):
    """Compile the team graph.

    pm -> (consolidate) -> dispatch <-> coder -> reflect -> qa -> (fix -> reflect -> qa)*
    -> finish. ``clients`` are the A2A clients from ``team_clients``: every Coder <-> QA
    exchange goes through them; only the Coder's own work (build, reflect) is direct.
    A resumed run starts at ``state["resume_from"]`` instead of ``pm``. With
    ``services``, a stop request ends the run ``interrupted`` at the next node;
    ``on_stage`` is called after each completed stage (checkpoints, stage events).
    """
    stages = {
        "pm": (partial(pm_node, pm=pm), "plan", route_after_pm),
        "consolidate": (partial(consolidate_node, pm=pm), "consolidate",
                        route_after_consolidate),
        "dispatch": (dispatch_node, None, None),
        "coder": (partial(coder_node, coder=coder), "build", route_after_coder),
        "reflect": (partial(reflect_node, coder=coder), "reflect", route_after_reflect),
        "qa": (partial(qa_node, clients=clients, services=services), "qa_review",
               route_after_qa),
        "fix": (partial(fix_node, clients=clients, services=services), "fix",
                route_after_fix),
        "finish": (partial(finish_node, clients=clients, services=services), None, None),
    }
    graph = StateGraph(TeamState)
    for name, (node, stage, router) in stages.items():
        graph.add_node(name, _checkpointed(name, stage, router, node, services, on_stage))
    _add_routes(graph)
    return graph.compile()


def _add_routes(graph: StateGraph) -> None:
    """Every edge of the team graph; each stage's router decides where it goes next."""
    graph.add_conditional_edges(START, route_from_start, list(GRAPH_NODES))
    graph.add_conditional_edges("pm", route_after_pm, ["finish", "consolidate", "dispatch"])
    graph.add_conditional_edges("consolidate", route_after_consolidate, ["dispatch", "finish"])
    graph.add_conditional_edges("dispatch", route_after_dispatch, ["coder", "finish"])
    graph.add_conditional_edges("coder", route_after_coder, ["dispatch", "reflect", "finish"])
    graph.add_conditional_edges("reflect", route_after_reflect, ["qa", "finish"])
    graph.add_conditional_edges("qa", route_after_qa, ["fix", "finish"])
    graph.add_conditional_edges("fix", route_after_fix, ["reflect", "finish"])
    graph.add_edge("finish", END)


def route_from_start(state: TeamState) -> str:
    """Start a new run at ``pm``; a resumed one at the node after its checkpoint."""
    return state.get("resume_from") or "pm"


NODE_EVENTS = {
    "pm": ("plan", "pm", "write_spec"),
    "consolidate": ("consolidate", "pm", "consolidate"),
    "dispatch": ("build", "team", None),
    "coder": ("build", "coder", "run"),
    "reflect": ("reflect", "coder", "reflect"),
    "qa": ("qa_review", "qa", None),
    "fix": ("fix", "coder", None),
    "finish": ("team", "team", None),
}
"""Node -> (the stage its events belong to, the agent they name, and the invocation of
the ``agent:*`` span it opens; ``None`` where the agent runs behind the A2A broker)."""


def _checkpointed(name: str, stage: str | None, router, node, services, on_stage):
    """Wrap one graph node: honour a stop request, observe it, and report its stage.

    The stop flag is checked before the node (no new work starts) and after it (work
    cut short by the stop is reported as ``interrupted``, not as the failure it caused).
    ``finish`` always runs, so every run gets its result and final report. With
    ``services``, the node runs in its event context and agent span, and emits its
    stage events with the progress line (FR-216, FR-220). One helper for every node,
    rather than the same lines in eight nodes.
    """
    def run(state: TeamState) -> dict:
        if name != "finish" and _stopping(services):
            return interrupted_end()
        started = time.monotonic()
        with _observed(name, state, services):
            update = node(state)
        if name != "finish" and _stopping(services):
            update = {**update, **interrupted_end()}
        _stage_event(name, state, update, services, started)
        merged = {**state, **update}
        if on_stage and stage and merged["status"] == "running":
            on_stage(name, stage, router(merged), merged)
        return update

    return run


@contextlib.contextmanager
def _observed(name: str, state: TeamState, services: RunServices | None):
    """The node's event context, and its ``agent:*`` span when it calls an agent directly."""
    if services is None:
        yield
        return
    stage, agent, invocation = NODE_EVENTS[name]
    handoff = state.get("handoff")
    fields = {"agent": agent, "stage": stage}
    if name == "coder" and handoff:
        fields["task_id"] = handoff["task_id"]
    with contextlib.ExitStack() as stack:
        stack.enter_context(services.events.context(**fields))
        if invocation:
            metadata = {"stage": stage, "run_id": state["run_id"],
                        **({"task_id": fields["task_id"]} if "task_id" in fields else {})}
            stack.enter_context(services.tracer.agent_span(agent, invocation, **metadata))
        yield


def _stage_event(name: str, state: TeamState, update: dict, services, started: float) -> None:
    """Emit the node's stage event, carrying its stderr progress line (FR-220)."""
    if services is None:
        return
    stage, agent, _ = NODE_EVENTS[name]
    line = format_update(name, update, state.get("handoff"))
    details = {"progress": line} if line else {}
    if name == "dispatch":
        handoff = update.get("handoff") or {}
        services.events.emit("stage_start", stage=stage, agent="coder",
                             task_id=handoff.get("task_id"), **details)
        return
    ended = update.get("status", state["status"])
    services.events.emit(
        "stage_end", stage=stage, agent=agent,
        severity="error" if ended == "error" else "info",
        outcome=update.get("end_reason") or ended,
        duration_ms=int((time.monotonic() - started) * 1000), **details,
    )


def _stopping(services: RunServices | None) -> bool:
    return services is not None and services.stop_requested.is_set()


def interrupted_end() -> dict:
    """The update that ends a run stopped by a signal (FR-210)."""
    return {"status": "failed", "end_reason": "interrupted",
            "error": "The run was interrupted by a stop signal."}


def run_team(
    requirement: str,
    pm: PMAgent | None = None,
    coder=None,
    workspace_root: Path = Path("workspace"),
    on_update: Callable[[str, dict], None] | None = None,
    *,
    qa=None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    a2a_timeout_seconds: float = DEFAULT_A2A_TIMEOUT_SECONDS,
    services: RunServices | None = None,
) -> TeamState:
    """Run the PM -> Coder <-> QA team on ``requirement`` and return the final state.

    ``coder`` is any object with ``run(task, workspace) -> CoderResult``; ``qa`` is
    any object with ``run_iteration(context)`` and ``final_report(request)``. QA is
    reached only through the A2A broker this function opens and closes.
    ``on_update(node, update)`` is called after every node. ``services`` holds the
    run's shared helpers (a fresh ``RunServices`` by default); its ``run_id`` names the
    run, and every agent that has ``attach`` is given it.

    The run holds its lock throughout, saves a checkpoint after each stage, and writes
    its final record (``.runs/<run_id>/run.json``) however it ends (FR-212, FR-213).

    Raises:
        ValueError: if ``requirement`` is empty or ``max_iterations`` is outside 1-10.
        SandboxUnavailableError: if no ``coder``/``qa`` is given and Docker is unavailable.
    """
    check_max_iterations(max_iterations)
    services = services or RunServices.create()
    workspace = (workspace_root / services.run_id).resolve()
    state = initial_team_state(requirement, services.run_id, workspace, max_iterations)
    pm, coder, qa = _attached(pm, coder, qa, services)
    settings = run_settings(pm, coder, qa, max_iterations, services)
    check_budget_pricing(services.budget.cap_usd, settings, services)
    state["budget_usd"] = services.budget.cap_usd
    workspace.mkdir(parents=True)
    return _execute(state, (pm, coder, qa), workspace_root, services, settings,
                    on_update, a2a_timeout_seconds)


def resume_team(
    run_id: str,
    pm: PMAgent | None = None,
    coder=None,
    workspace_root: Path = Path("workspace"),
    on_update: Callable[[str, dict], None] | None = None,
    *,
    qa=None,
    budget_usd: float | None = None,
    settings_overrides: dict | None = None,
    services: RunServices | None = None,
    a2a_timeout_seconds: float = DEFAULT_A2A_TIMEOUT_SECONDS,
) -> TeamState:
    """Continue run ``run_id`` from the stage after its last checkpoint (FR-213a-d).

    The workspace is restored to the checkpoint, a ``ResumeAttempt`` is recorded, and
    the run goes on with the same run ID, iteration count, usage, cost and A2A log.

    Raises:
        ResumeRefused: naming the reason, without changing any file, if the run is
            still going, ended for good, has an unusable checkpoint, asks for other
            settings, or ended ``budget_exceeded`` without a higher ``budget_usd``.
    """
    directory = run_dir(workspace_root, run_id)
    previous, record = _resumable_checkpoint(directory, run_id, budget_usd)
    _check_settings_unchanged(run_id, record["settings"], settings_overrides or {})
    services = services or RunServices.create(run_id)
    budget = budget_usd if budget_usd is not None else record["state"].get("budget_usd")
    check_budget_pricing(budget, record["settings"], services)
    pm, coder, qa = _attached(pm, coder, qa, services)
    state = _resumed_state(record, previous, budget_usd)
    return _execute(state, (pm, coder, qa), workspace_root, services, record["settings"],
                    on_update, a2a_timeout_seconds)


def _check_settings_unchanged(run_id: str, saved: dict, overrides: dict) -> None:
    """Refuse a resume that asks for other settings than the run started with (FR-213c).

    Raises:
        ResumeRefused: naming the settings that would change.
    """
    changed = sorted(key for key, value in overrides.items() if saved.get(key) != value)
    if changed:
        raise ResumeRefused(
            f"run {run_id} must resume with the settings it started with; it can't change "
            f"{', '.join(changed)} (only the budget can be raised)"
        )


def check_budget_pricing(cap_usd: float | None, settings: dict, services: RunServices) -> None:
    """Refuse to start a run whose budget couldn't be enforced (FR-224).

    Raises:
        ValueError: naming the model, if a budget is set and a configured model has no
            price in the price table.
    """
    models = [model for model in settings.get("models", {}).values() if model]
    unpriced = [model for model in models if not is_priced(model, services.price_table)]
    RunBudget(cap_usd, unpriced_models=unpriced)


def _resumable_checkpoint(directory: Path, run_id: str, budget_usd: float | None):
    """Apply the resume rules (contracts/cli.md); return ``(previous end reason, checkpoint)``.

    Raises:
        ResumeRefused: for every refused case.
    """
    if is_locked(directory):
        raise ResumeRefused(f"run {run_id} is still running")
    try:
        final = read_final_record(directory)
    except CheckpointError as error:
        raise ResumeRefused(f"run {run_id} can't be resumed: {error}") from error
    previous = final.get("end_reason") if final else None
    if final and previous not in RESUMABLE_END_REASONS:
        raise ResumeRefused(f"run {run_id} ended {previous}, which can't be resumed")
    try:
        record = load_checkpoint(directory)
    except CheckpointError as error:
        raise ResumeRefused(f"run {run_id} can't be resumed: {error}") from error
    if previous == "budget_exceeded":
        stored = final.get("budget_usd")
        if budget_usd is None or stored is None or budget_usd <= stored:
            raise ResumeRefused(
                f"run {run_id} ended budget_exceeded at ${stored}; resume it with a higher "
                "--budget"
            )
    return previous, record


def _resumed_state(record: dict, previous: str | None, budget_usd: float | None) -> TeamState:
    """The checkpointed state, restored to its workspace and marked as a new attempt."""
    state = record["state"]
    restored, removed = restore_workspace(Path(state["workspace"]), record["workspace"])
    budget_before = state.get("budget_usd")
    attempt = ResumeAttempt(
        resumed_at=datetime.now(timezone.utc).isoformat(),
        from_stage=record["stage"],
        next_node=record["next_node"],
        previous_end_reason=previous,
        budget_before_usd=budget_before,
        budget_after_usd=budget_usd if budget_usd is not None else budget_before,
        restored_files=restored,
        removed_files=removed,
    )
    return {
        **state,
        "status": "running", "end_reason": None, "error": None, "failure": None,
        "handoff": None, "resume_from": record["next_node"],
        "budget_usd": attempt["budget_after_usd"],
        "resume_history": state["resume_history"] + [attempt],
    }


def _attached(pm, coder, qa, services: RunServices) -> tuple:
    """Build any agent not given, and give every agent the run's services."""
    pm = pm or PMAgent(services=services)
    coder = coder or CoderAgent(services=services)
    qa = qa or QAAgent(services=services)
    for agent in (pm, coder, qa):
        if hasattr(agent, "attach"):
            agent.attach(services)
    return pm, coder, qa


def run_settings(pm, coder, qa, max_iterations: int, services: RunServices) -> dict:
    """The settings a resume must keep (FR-213c), read from whichever agents have them."""
    def config(agent, name, default=None):
        return getattr(getattr(agent, "config", None), name, default)

    agents = {"pm": pm, "coder": coder, "qa": qa}
    return {
        "models": {name: config(agent, "model") for name, agent in agents.items()},
        "efforts": {name: config(agent, "effort") for name, agent in agents.items()},
        "max_iterations": max_iterations,
        "max_steps": config(coder, "max_steps"),
        "exec_timeout": config(coder, "exec_timeout_seconds"),
        "reflection_rounds": config(coder, "reflection_rounds"),
        "prompt_cache_ttl": services.prompt_cache_ttl,
    }


def _execute(
    state: TeamState, agents: tuple, workspace_root: Path, services: RunServices,
    settings: dict, on_update, a2a_timeout_seconds: float,
) -> TeamState:
    """Run the graph from ``state`` under the run lock; always write the final record."""
    pm, coder, qa = agents
    started, prior = time.monotonic(), state["duration_seconds"]
    started_at = datetime.now(timezone.utc).isoformat()
    directory = run_dir(workspace_root, state["run_id"])
    latest = [state]
    _carry_spend(state, services)
    save_stage = _stage_saver(directory, settings, services, prior, started)
    services.events.path = directory / EVENTS_FILE
    earlier_events = read_events(services.events.path)
    with RunLock(directory), _stop_watcher(services, state["run_id"]), \
            services.tracer.root_span(state["run_id"], _root_metadata(state)):
        _run_started(state, services)
        if state["resume_from"] is None:
            # A start checkpoint, so a run stopped before its first stage can be resumed.
            save_stage("start", "team", "pm", state)
        try:
            with A2ABroker(a2a_timeout_seconds) as broker:
                register_team_agents(broker, coder, qa, services=services)
                clients = team_clients(broker, state["a2a_log"])
                graph = build_team_graph(pm, coder, clients, services, save_stage)
                _stream(graph, state, on_update, latest)
        except Exception as error:
            latest[0] = {**latest[0], "status": "error",
                         "error": f"Unexpected {type(error).__name__}: {error}"}
            raise
        finally:
            # Only the caller knows when the whole run ended, so this is set outside a node.
            ended = {**latest[0], "duration_seconds": _elapsed(prior, started)}
            latest[0] = _close_run(ended, services, settings, started_at, earlier_events)
            write_final_record(directory, latest[0], services.redactor.redact)
    return latest[0]


def _stage_saver(directory: Path, settings: dict, services: RunServices, prior: float,
                 started: float) -> StageCallback:
    """The ``on_stage`` callback: save the run's checkpoint after a completed stage."""
    def save_stage(node: str, stage: str, next_node: str, current: dict) -> None:
        saving = time.monotonic()
        current = {**current, "duration_seconds": _elapsed(prior, started),
                   "agent_usage": services.ledger.snapshot()}
        save_checkpoint(directory, stage, next_node, current, settings,
                        services.ledger.snapshot(), services.redactor.redact)
        services.events.emit("checkpoint_saved", stage=stage, next_node=next_node,
                             duration_ms=int((time.monotonic() - saving) * 1000))

    return save_stage


def _close_run(final: TeamState, services: RunServices, settings: dict, started_at: str,
               earlier_events: list) -> TeamState:
    """Fill in the run's usage and cost, emit ``run_end``, and write the cost report."""
    final = {**final, "agent_usage": services.ledger.snapshot(),
             "cost_usd": services.ledger.total_cost()}
    final = _run_ended(final, services, earlier_events)
    return _with_cost_report(final, services, settings, started_at)


def _carry_spend(state: TeamState, services: RunServices) -> None:
    """A resumed run continues with its earlier usage, cost and budget (FR-213a)."""
    if any(entry["model_calls"] or entry["cached_replays"]
           for entry in state["agent_usage"].values()):
        services.ledger.load(state["agent_usage"])
    services.budget.spent = services.ledger.total_cost() or 0.0
    if state.get("budget_usd") is not None:
        services.budget.cap_usd = state["budget_usd"]


def _with_cost_report(
    final: TeamState, services: RunServices, settings: dict, started_at: str
) -> TeamState:
    """Write the run's cost report (FR-222a); a failed write never changes the outcome."""
    def report_error(message: str) -> None:
        services.events.emit("error", severity="error", outcome="cost_report", message=message)

    path = write_cost_report(
        services.cost_report_dir, final, services.ledger.snapshot(),
        services.price_table.get("version", "unknown"), services.prompt_cache_ttl,
        started_at, settings.get("models", {}), services.redactor.redact, report_error,
    )
    return {**final, "cost_report_path": str(path) if path else None}


def _root_metadata(state: TeamState) -> dict:
    """The ``team-run`` root span's metadata: one root per attempt (FR-214a)."""
    history = state["resume_history"]
    return {
        "attempt": len(history) + 1,
        "requirement_chars": len(state["requirement"]),
        "max_iterations": state["max_iterations"],
        "resumed_from_stage": history[-1]["from_stage"] if history else None,
    }


def _run_started(state: TeamState, services: RunServices) -> None:
    """Emit ``run_start``, and ``resume`` for a resumed attempt (FR-213d)."""
    services.events.emit("run_start", attempt=len(state["resume_history"]) + 1)
    if state["resume_history"]:
        attempt = state["resume_history"][-1]
        services.events.emit(
            "resume", outcome="resumed", from_stage=attempt["from_stage"],
            budget_usd={"before": attempt["budget_before_usd"],
                        "after": attempt["budget_after_usd"]},
            restored_files=attempt["restored_files"], removed_files=attempt["removed_files"],
        )


def _run_ended(final: TeamState, services: RunServices, earlier_events: list) -> TeamState:
    """Emit ``run_end`` with the summary of every attempt; fill the state's run fields.

    This attempt's events come from memory, so the summary is complete whatever
    ``LOG_LEVEL`` is; earlier attempts' come from ``events.jsonl``, so at a level above
    ``info`` their info events (model calls) are not in it.
    """
    events = services.events
    summary = summarise(earlier_events + events.events, events.events_lost)
    events.emit("run_end", severity="error" if final["status"] == "error" else "info",
                outcome=final.get("end_reason") or final["status"], summary=summary)
    events.close()
    return {**final, "summary": summary, "events_lost": events.events_lost,
            "trace_url": services.tracer.root_url()}


def _elapsed(prior: float, started: float) -> float:
    return round(prior + time.monotonic() - started, 3)


@contextlib.contextmanager
def _stop_watcher(services: RunServices, run_id: str):
    """While the run is going, kill its containers as soon as a stop is requested.

    The work runs on this thread, never in the signal handler, so the handler only
    sets a flag (R14).
    """
    done, wake = threading.Event(), threading.Event()
    services.stop_wakers.append(wake)
    if services.stop_requested.is_set():
        wake.set()

    def watch() -> None:
        wake.wait()
        if services.stop_requested.is_set() and not done.is_set():
            kill_run_containers(run_id)
            for callback in services.stop_callbacks:
                callback()

    watcher = threading.Thread(target=watch, name="stop-watcher", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        done.set()
        wake.set()
        watcher.join(5)
        services.stop_wakers.remove(wake)


def _stream(
    graph, state: TeamState, on_update: Callable[[str, dict], None] | None, latest: list
) -> TeamState:
    """Run the graph to the end, reporting each node's update; return the final state.

    ``latest[0]`` always holds the newest full state, so a crash still leaves a record.
    """
    for mode, chunk in graph.stream(
        state, {"recursion_limit": RECURSION_LIMIT}, stream_mode=["updates", "values"]
    ):
        if mode == "values":
            latest[0] = chunk
        elif on_update:
            for node, update in chunk.items():
                on_update(node, update)
    return latest[0]


def exit_code_for(state: TeamState, signal_received: int | None = None) -> int:
    """The command's exit code for a finished run (FR-201a, contracts/cli.md)."""
    if state["status"] == "completed":
        return EXIT_COMPLETED
    if state.get("end_reason") == "interrupted":
        signum = signal_received or signal.SIGINT
        return 128 + signum
    if state.get("end_reason") in RESUMABLE_END_REASONS:
        return EXIT_RESUMABLE
    return EXIT_FAILED


def install_signal_handlers(services: RunServices) -> Callable[[], None]:
    """Make SIGINT and SIGTERM stop the run cleanly; a second signal exits at once.

    Returns a function that puts the previous handlers back.
    """
    def handle(signum, frame) -> None:
        if services.stop_requested.is_set():
            kill_run_containers(services.run_id)
            print("interrupted again: exiting now", file=sys.stderr, flush=True)
            os._exit(128 + signum)
        services.request_stop(signum)

    previous = {signum: signal.signal(signum, handle) for signum in (signal.SIGINT, signal.SIGTERM)}

    def restore() -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore


def format_update(node: str, update: dict, current: Task | None) -> str | None:
    """Return the progress line for one node's update, or ``None`` if there is none.

    ``current`` is the task most recently handed off, which the ``coder`` line
    reports on (the Coder's update holds every task's progress).
    """
    if node == "pm":
        if update.get("spec"):
            count = len(update["spec"]["tasks"])
            return f'[pm] "{update["spec"]["name"]}": {count} task{"" if count == 1 else "s"}'
        return f"[pm] {update['status']}: {update['error']}"
    if node == "consolidate":
        merge = update["consolidation"]
        return (
            f"[pm] consolidated {merge['proposed_count']} → {merge['final_count']} tasks "
            f"({merge['method']})"
        )
    if node == "dispatch":
        handoff = update["handoff"]
        return f"[handoff] {handoff['task_id']} ({handoff['priority']}) {handoff['title']}"
    if node == "coder" and current:
        progress = update["task_progress"][current["task_id"]]
        if progress["status"] == "completed":
            return (
                f"[coder] {current['task_id']} completed in {progress['steps_used']} steps "
                f"({progress['duration_seconds']} s)"
            )
        return f"[coder] {current['task_id']} failed ({progress['coder_status']})"
    if node == "reflect":
        return _reflect_line(update)
    if node == "qa":
        return "\n".join(filter(None, [_a2a_line(update), _qa_line(update)]))
    if node == "fix":
        return "\n".join(filter(None, [_a2a_line(update), _fix_line(update)]))
    if node == "finish":
        return _finish_line(update)
    return None


def _a2a_line(update: dict) -> str | None:
    """Describe the exchange a node just made, e.g. ``[a2a] coder → qa-debugger ...``."""
    if not update.get("a2a_log"):
        return None
    entry = update["a2a_log"][-1]
    seconds = ""
    if entry["finished_at"]:
        elapsed = datetime.fromisoformat(entry["finished_at"]) - datetime.fromisoformat(
            entry["started_at"]
        )
        seconds = f" ({elapsed.total_seconds():.1f} s)"
    outcome = entry["outcome"].removeprefix("TASK_STATE_").lower()
    task = f" task {entry['task_id'][:8]}…" if entry["task_id"] else ""
    return (
        f"[a2a] {entry['sender']} → {entry['recipient']} {entry['skill_id'] or entry['method']}"
        f"{task} {outcome}{seconds}"
    )


def _qa_line(update: dict) -> str:
    if "iterations" not in update:
        return f"[qa] {update.get('end_reason')}: {update.get('error')}"
    iteration = update["iterations"][-1]
    run = iteration["test_run"]
    counts = f"{run['passed']} passed, {run['failed']} failed, {run['errors']} errors"
    if iteration["verdict"] == "complete":
        return f"[qa] #{iteration['number']}: {counts} → complete"
    blocking = sum(1 for finding in iteration["findings"] if finding["severity"] == "blocking")
    advisory = len(iteration["findings"]) - blocking
    return f"[qa] #{iteration['number']}: {counts} → {blocking} blocking, {advisory} advisory"


def _reflect_line(update: dict) -> str:
    rounds = update.get("pending_reflections")
    if not rounds:
        return f"[reflect] {update.get('end_reason')}: {update.get('error')}"
    items = sum(len(item["critique"]) for item in rounds)
    return (
        f"[reflect] before QA #{rounds[-1]['before_iteration']}: {items} critique items, "
        f"revision {rounds[-1]['revision_status']}"
    )


def _fix_line(update: dict) -> str:
    if "iterations" not in update:
        return f"[fix] {update.get('end_reason')}: {update.get('error')}"
    iteration = update["iterations"][-1]
    fix = iteration["fix"]
    expected = len(_blocking(iteration))
    disputed = sum(1 for response in fix["responses"] if response["action"] == "disputed")
    return (
        f"[fix] #{iteration['number']}: {fix['status']} ({fix['steps_used']} steps), "
        f"answered {len(fix['responses'])} of {expected} ({disputed} disputed)"
    )


def _finish_line(update: dict) -> str:
    line = f"[finish] {update['status']}"
    if update.get("end_reason"):
        line += f" ({update['end_reason']})"
    report = update.get("qa_report")
    if report and report["report_path"]:
        line += f" · report: {report['report_path']}"
    return line


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns the exit code from ``exit_code_for`` (0, 1, 3, 130 or 143),
    or 2 for usage errors, bad settings, a missing sandbox or a refused resume."""
    load_dotenv()
    args = _parse_args(argv)
    try:
        config = CoderConfig.from_env()
        if args.max_steps is not None:
            config = replace(config, max_steps=args.max_steps)
        if args.timeout is not None:
            config = replace(config, exec_timeout_seconds=args.timeout)
        services = RunServices.create(args.resume)
        if args.budget is not None and not args.resume:
            services.budget = RunBudget(args.budget)
        services.events.subscribers.append(print_progress)
        restore_handlers = install_signal_handlers(services)
        try:
            final = _run_or_resume(args, config, services)
        finally:
            restore_handlers()
    except (ValueError, SandboxUnavailableError, McpUnavailableError, ResumeRefused) as error:
        print(Redactor.from_env(os.environ).redact(f"error: {error}"), file=sys.stderr)
        return EXIT_USAGE

    print(json.dumps(services.redactor.redact(final), indent=2))
    report = final.get("qa_report")
    if report and report["report_path"]:
        print(f"QA report: {Path(final['workspace']) / report['report_path']}", file=sys.stderr)
    if final.get("trace_url"):
        print(f"trace: {final['trace_url']}", file=sys.stderr)
    return exit_code_for(final, services.signal_received)


def print_progress(event: dict) -> None:
    """Print an event's progress line to stderr: the CLI's lines come from the events."""
    line = progress_line(event)
    if line:
        print(line, file=sys.stderr, flush=True)


def _run_or_resume(args: argparse.Namespace, config: CoderConfig, services: RunServices):
    """Build the agents, clear stale containers, then start or resume the run."""
    a2a_timeout = a2a_timeout_from_env()
    pm_config, qa_config = PMConfig.from_env(), QAConfig.from_env()
    failing = failing_items(check_health(os.environ, config, qa_config, pm_config))
    if failing:
        raise ValueError("the team can't run yet:\n  " + "\n  ".join(failing))
    for settings in (pm_config, config, qa_config):
        for warning in settings.warnings:
            services.events.emit("warning", severity="warning", outcome="settings",
                                 message=warning)
    agents = {
        "pm": PMAgent(pm_config, services=services),
        "coder": CoderAgent(config, on_step=print_step_progress, services=services),
        "qa": QAAgent(qa_config, services=services),
    }
    cleanup_stale_containers(config.workspace_root)
    if args.resume:
        return resume_team(
            args.resume, workspace_root=config.workspace_root, budget_usd=args.budget,
            services=services, a2a_timeout_seconds=a2a_timeout, **agents,
        )
    max_iterations = (
        check_max_iterations(args.max_iterations)
        if args.max_iterations is not None
        else max_iterations_from_env()
    )
    return run_team(
        args.requirement, workspace_root=config.workspace_root,
        max_iterations=max_iterations, a2a_timeout_seconds=a2a_timeout, services=services,
        **agents,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m orchestration.team",
        description="Turn a plain-English requirement into a spec, tasks, and working code.",
    )
    parser.add_argument("requirement", nargs="?", help="what the software should do")
    parser.add_argument(
        "--max-iterations", type=int,
        help=f"most QA reviews (1-{MAX_ITERATIONS_CAP}; default: TEAM_MAX_ITERATIONS or 5)",
    )
    parser.add_argument("--max-steps", type=int, help="Coder step cap per task (1-10)")
    parser.add_argument("--timeout", type=float, help="seconds before a code run is killed")
    parser.add_argument(
        "--budget", type=float, metavar="USD",
        help="per-run spending cap (default: RUN_BUDGET_USD, or no cap); with --resume it "
             "may only raise the run's stored cap",
    )
    parser.add_argument(
        "--resume", metavar="RUN_ID",
        help="continue an interrupted or resumable run from its last checkpoint",
    )
    args = parser.parse_args(argv)
    if args.resume:
        given = [flag for flag, value in (
            ("a requirement", args.requirement), ("--max-iterations", args.max_iterations),
            ("--max-steps", args.max_steps), ("--timeout", args.timeout),
        ) if value is not None]
        if given:
            parser.error(f"--resume keeps the run's settings; it can't be used with "
                         f"{', '.join(given)}")
    elif not args.requirement:
        parser.error("give a requirement, or --resume RUN_ID")
    return args


def check_max_iterations(value: int) -> int:
    """Return ``value`` if it is a whole number from 1 to 10 (FR-115).

    Raises:
        ValueError: otherwise.
    """
    whole = isinstance(value, int) and not isinstance(value, bool)
    if not whole or not 1 <= value <= MAX_ITERATIONS_CAP:
        raise ValueError(
            f"the maximum number of iterations must be a whole number from 1 to "
            f"{MAX_ITERATIONS_CAP}, not {value!r}"
        )
    return value


def max_iterations_from_env(env=os.environ) -> int:
    """Read ``TEAM_MAX_ITERATIONS`` (default 5, from 1 to 10).

    Raises:
        ValueError: if the value is not a whole number from 1 to 10.
    """
    raw = env.get("TEAM_MAX_ITERATIONS", str(DEFAULT_MAX_ITERATIONS))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            f"TEAM_MAX_ITERATIONS must be a whole number from 1 to {MAX_ITERATIONS_CAP}, "
            f"not {raw!r}"
        ) from error
    return check_max_iterations(value)


def a2a_timeout_from_env(env=os.environ) -> float:
    """Read ``A2A_REQUEST_TIMEOUT`` (seconds, > 0; default 1800).

    Raises:
        ValueError: if the value is not a positive number.
    """
    raw = env.get("A2A_REQUEST_TIMEOUT", str(DEFAULT_A2A_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(
            f"A2A_REQUEST_TIMEOUT must be a number of seconds, not {raw!r}"
        ) from error
    if value <= 0:
        raise ValueError("A2A_REQUEST_TIMEOUT must be greater than 0")
    return value


if __name__ == "__main__":
    sys.exit(main())
