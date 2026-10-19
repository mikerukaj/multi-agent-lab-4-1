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

Usage:
    python -m orchestration.team "<requirement>" [--max-iterations N]
        [--max-steps N] [--timeout SECONDS]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

from agents.coder_agent import CoderAgent, CoderConfig, new_run_id, print_step_progress
from agents.llm import add_tokens
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
from orchestration.state import (
    CodeOutput,
    CoderFix,
    Consolidation,
    FinalReportRequest,
    FixRequest,
    Iteration,
    QAContext,
    Task,
    TaskProgress,
    TeamState,
    TechnicalSpec,
    TokenUsage,
)
from tools.file_tools import read_file
from tools.mcp_client import McpUnavailableError
from tools.python_exec import SandboxUnavailableError

MAX_ITERATIONS_CAP = 10
DEFAULT_MAX_ITERATIONS = 5
# pm + consolidate + (dispatch + coder) per task + (reflect + qa + fix) per iteration
# + finish, with room to spare.
RECURSION_LIMIT = 2 * MAX_TASKS + 3 * MAX_ITERATIONS_CAP + 6
DEFAULT_A2A_TIMEOUT_SECONDS = 1800.0
CLIENT_NAMES = ("coder", QA_AGENT, "team")


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
        usage=TokenUsage(input_tokens=0, output_tokens=0),
        duration_seconds=0.0,
        max_iterations=max_iterations,
        iterations=[],
        pending_reflections=[],
        test_suite={},
        qa_report=None,
        end_reason=None,
        a2a_log=[],
    )


def add_usage(total: TokenUsage, extra: TokenUsage) -> TokenUsage:
    """Return ``total`` plus ``extra``."""
    return TokenUsage(**add_tokens(total, extra["input_tokens"], extra["output_tokens"]))


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


def pm_node(state: TeamState, pm: PMAgent) -> dict:
    """PM side: write the spec and task list into shared state."""
    result = pm.write_spec(state["requirement"])
    usage = add_usage(state["usage"], result.usage)
    if result.error:
        return {"status": "error", "error": result.error, "usage": usage}
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
        "usage": add_usage(state["usage"], result.usage),
    }


def route_after_pm(state: TeamState) -> str:
    """Stop on rejection or error, consolidate an oversized plan, else hand off."""
    if state["status"] != "running":
        return "finish"
    if len(state["tasks"]) > MAX_TASKS:
        return "consolidate"
    return "dispatch"


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
    return {
        "handoff": None,
        "task_progress": {**state["task_progress"], handoff["task_id"]: progress},
        "code_output": state["code_output"] + _read_outputs(workspace, handoff, result),
        "usage": add_usage(state["usage"], TokenUsage(**result.usage)),
    }


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
    statuses = [progress["status"] for progress in state["task_progress"].values()]
    if "failed" in statuses:
        return "finish"
    if "pending" in statuses:
        return "dispatch"
    return "reflect"


def team_clients(broker: A2ABroker) -> dict[str, A2AClient]:
    """Return one A2A client per sender the graph sends on behalf of."""
    return {name: broker.client(name) for name in CLIENT_NAMES}


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


def review_message(state: TeamState) -> Message:
    """The Coder's A2A request asking QA to review the program."""
    context = build_qa_context(state)
    return user_message(
        [text_part(f"Program ready for QA review #{context['iteration']}."), data_part(context)],
        context_id=state["run_id"],
        metadata={"skillId": REVIEW_SKILL, "sender": "coder"},
    )


def report_message(state: TeamState, status: str, end_reason: str) -> Message:
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
        metadata={"skillId": REPORT_SKILL, "sender": "team"},
    )


def qa_node(state: TeamState, clients: dict[str, A2AClient]) -> dict:
    """Coder side: send the program to QA over A2A and record the iteration."""
    client = clients["coder"]
    try:
        task = client.send_message(QA_AGENT, review_message(state))
        outcome = task["status"]["state"]
        if outcome == "TASK_STATE_FAILED":
            return _ended(client, "error", "qa_failed", _status_reason(task))
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


def _ended(client: A2AClient, status: str, end_reason: str, error: str) -> dict:
    return {
        "status": status, "end_reason": end_reason, "error": error,
        "a2a_log": client.exchanges(),
    }


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
    if reflection.status in ("refused", "error"):
        reason = reflection.result.error or reflection.result.explanation
        error = (
            f"Self-reflection before QA review #{number} failed ({reflection.status}): {reason}"
        )
        return {"status": "failed", "end_reason": "reflection_failed", "error": error,
                "usage": usage}
    rounds = [{**item, "before_iteration": number} for item in reflection.rounds]
    return {"pending_reflections": rounds, "usage": usage}


def fix_node(state: TeamState, clients: dict[str, A2AClient]) -> dict:
    """QA side: send the fix instruction list to the Coder over A2A and record its answer."""
    client = clients[QA_AGENT]
    last = state["iterations"][-1]
    try:
        task = client.send_message(CODER_AGENT, fix_message(state))
        outcome = task["status"]["state"]
        if outcome == "TASK_STATE_FAILED":
            error = f"Fix round after QA review #{last['number']} failed: {_status_reason(task)}"
            return _ended(client, "failed", "coder_failed", error)
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


def _artifact_usage(task: A2ATask, name: str) -> dict:
    item = next(item for item in task["artifacts"] if item.get("name") == name)
    return item.get("metadata", {}).get("usage", {"input_tokens": 0, "output_tokens": 0})


def fix_message(state: TeamState) -> Message:
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
        metadata={"skillId": FIX_SKILL, "sender": QA_AGENT},
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


def finish_node(state: TeamState, clients: dict[str, A2AClient]) -> dict:
    """Decide the run's result, then ask QA for its final report if QA ever ran."""
    update = _final_result(state)
    if not state["iterations"]:
        return update
    client = clients["team"]
    status, end_reason = update["status"], update["end_reason"]
    try:
        task = client.send_message(QA_AGENT, report_message(state, status, end_reason))
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


def build_team_graph(pm: PMAgent, coder, clients: dict[str, A2AClient]):
    """Compile the team graph.

    pm -> (consolidate) -> dispatch <-> coder -> reflect -> qa -> (fix -> reflect -> qa)*
    -> finish. ``clients`` are the A2A clients from ``team_clients``: every Coder <-> QA
    exchange goes through them; only the Coder's own work (build, reflect) is direct.
    """
    graph = StateGraph(TeamState)
    graph.add_node("pm", partial(pm_node, pm=pm))
    graph.add_node("consolidate", partial(consolidate_node, pm=pm))
    graph.add_node("dispatch", dispatch_node)
    graph.add_node("coder", partial(coder_node, coder=coder))
    graph.add_node("reflect", partial(reflect_node, coder=coder))
    graph.add_node("qa", partial(qa_node, clients=clients))
    graph.add_node("fix", partial(fix_node, clients=clients))
    graph.add_node("finish", partial(finish_node, clients=clients))
    graph.add_edge(START, "pm")
    graph.add_conditional_edges("pm", route_after_pm, ["finish", "consolidate", "dispatch"])
    graph.add_edge("consolidate", "dispatch")
    graph.add_edge("dispatch", "coder")
    graph.add_conditional_edges("coder", route_after_coder, ["dispatch", "reflect", "finish"])
    graph.add_conditional_edges("reflect", route_after_reflect, ["qa", "finish"])
    graph.add_conditional_edges("qa", route_after_qa, ["fix", "finish"])
    graph.add_conditional_edges("fix", route_after_fix, ["reflect", "finish"])
    graph.add_edge("finish", END)
    return graph.compile()


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
) -> TeamState:
    """Run the PM -> Coder <-> QA team on ``requirement`` and return the final state.

    ``coder`` is any object with ``run(task, workspace) -> CoderResult``; ``qa`` is
    any object with ``run_iteration(context)`` and ``final_report(request)``. QA is
    reached only through the A2A broker this function opens and closes.
    ``on_update(node, update)`` is called after every node.

    Raises:
        ValueError: if ``requirement`` is empty or ``max_iterations`` is outside 1-10.
        SandboxUnavailableError: if no ``coder``/``qa`` is given and Docker is unavailable.
    """
    check_max_iterations(max_iterations)
    started = time.monotonic()
    run_id = new_run_id()
    workspace = (workspace_root / run_id).resolve()
    state = initial_team_state(requirement, run_id, workspace, max_iterations)
    coder = coder or CoderAgent()
    qa = qa or QAAgent()
    workspace.mkdir(parents=True)
    with A2ABroker(a2a_timeout_seconds) as broker:
        register_team_agents(broker, coder, qa)
        graph = build_team_graph(pm or PMAgent(), coder, team_clients(broker))
        final = _stream(graph, state, on_update)
    # Only the caller knows when the whole run ended, so this field is set outside a node.
    return {**final, "duration_seconds": round(time.monotonic() - started, 3)}


def _stream(graph, state: TeamState, on_update: Callable[[str, dict], None] | None) -> TeamState:
    """Run the graph to the end, reporting each node's update; return the final state."""
    final = state
    for mode, chunk in graph.stream(
        state, {"recursion_limit": RECURSION_LIMIT}, stream_mode=["updates", "values"]
    ):
        if mode == "values":
            final = chunk
        elif on_update:
            for node, update in chunk.items():
                on_update(node, update)
    return final


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


def _progress_printer() -> Callable[[str, dict], None]:
    """Return an ``on_update`` callback that prints progress lines to stderr."""
    current: Task | None = None

    def on_update(node: str, update: dict) -> None:
        nonlocal current
        if node == "dispatch":
            current = update["handoff"]
        line = format_update(node, update, current)
        if line:
            print(line, file=sys.stderr, flush=True)

    return on_update


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns 0 if the run completed, 1 otherwise, 2 for usage errors."""
    load_dotenv()
    args = _parse_args(argv)
    try:
        config = CoderConfig.from_env()
        if args.max_steps is not None:
            config = replace(config, max_steps=args.max_steps)
        if args.timeout is not None:
            config = replace(config, exec_timeout_seconds=args.timeout)
        a2a_timeout = a2a_timeout_from_env()
        max_iterations = (
            check_max_iterations(args.max_iterations)
            if args.max_iterations is not None
            else max_iterations_from_env()
        )
        final = run_team(
            args.requirement,
            PMAgent(PMConfig.from_env()),
            CoderAgent(config, on_step=print_step_progress),
            config.workspace_root,
            on_update=_progress_printer(),
            qa=QAAgent(QAConfig.from_env()),
            max_iterations=max_iterations,
            a2a_timeout_seconds=a2a_timeout,
        )
    except (ValueError, SandboxUnavailableError, McpUnavailableError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(json.dumps(final, indent=2))
    report = final.get("qa_report")
    if report and report["report_path"]:
        print(f"QA report: {Path(final['workspace']) / report['report_path']}", file=sys.stderr)
    return 0 if final["status"] == "completed" else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m orchestration.team",
        description="Turn a plain-English requirement into a spec, tasks, and working code.",
    )
    parser.add_argument("requirement", help="what the software should do")
    parser.add_argument(
        "--max-iterations", type=int,
        help=f"most QA reviews (1-{MAX_ITERATIONS_CAP}; default: TEAM_MAX_ITERATIONS or 5)",
    )
    parser.add_argument("--max-steps", type=int, help="Coder step cap per task (1-10)")
    parser.add_argument("--timeout", type=float, help="seconds before a code run is killed")
    return parser.parse_args(argv)


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
