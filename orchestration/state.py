"""Shared LangGraph state that the PM, Coder and QA agents read and write.

Every value is a ``TypedDict``, a ``Literal`` or a plain JSON type, so any state
converts to JSON with ``json.dumps`` and back without loss
(specs/001-autonomous-dev-team/data-model.md, "Increment 2: Shared Team State").

Increment 3 adds QA's test results and findings, the Coder's self-reflection and
fix rounds, the Coder <-> QA iterations, QA's final report, the A2A exchange log,
and the JSON-only request payloads that cross the A2A broker (``QAContext``,
``FixRequest``, ``FinalReportRequest``). The payloads live here, not in an agent
module, so neither agent imports the other
(specs/002-qa-feedback-loop-mcp/data-model.md).
"""

from typing import Literal, TypedDict

Priority = Literal["high", "medium", "low"]
TaskStatus = Literal["pending", "in_progress", "completed", "failed", "skipped"]
RunStatus = Literal["running", "completed", "failed", "rejected", "error"]
ConsolidationMethod = Literal["pm", "merge"]
TestOutcome = Literal["passed", "failed", "error", "skipped"]
FindingKind = Literal["failing_test", "bug", "style"]
Severity = Literal["blocking", "advisory"]
FindingState = Literal["open", "fixed", "disputed", "withdrawn"]
ResponseAction = Literal["fixed", "disputed", "not_addressed"]
DisputeDecision = Literal["keep", "correct"]
TestChangeReason = Literal[
    "initial", "load_repair", "dispute_correction", "regression", "restored"
]
CritiqueKind = Literal["bug", "edge_case"]
Verdict = Literal["complete", "fix_needed"]
EndReason = Literal[
    "complete",
    "iteration_limit",
    "task_failed",
    "coder_failed",
    "reflection_failed",
    "qa_failed",
    "a2a_failed",
    "rejected",
    "pm_error",
]


class Task(TypedDict):
    """One engineering task; also the handoff message (contracts/handoff-protocol.md)."""

    task_id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    priority: Priority


class TechnicalSpec(TypedDict):
    """The PM's specification of the requirement, including its ordered tasks."""

    name: str
    description: str
    acceptance_criteria: list[str]
    assumptions: list[str]
    tasks: list[Task]


class TaskProgress(TypedDict):
    """Status of one task, written by the PM side and then by the Coder."""

    task_id: str
    status: TaskStatus
    coder_status: str | None
    summary: str
    files_written: list[str]
    steps_used: int
    duration_seconds: float
    error: str | None


class CodeOutput(TypedDict):
    """One file a task wrote, with its contents read from disk after the task."""

    task_id: str
    path: str
    content: str


class Consolidation(TypedDict):
    """How a plan with more than the task limit was merged down."""

    proposed_count: int
    final_count: int
    method: ConsolidationMethod


class TokenUsage(TypedDict):
    """Tokens used by model calls, summed over the run."""

    input_tokens: int
    output_tokens: int


class TestCaseResult(TypedDict):
    """One ``<testcase>`` from a pytest JUnit XML report."""

    test_id: str
    file: str
    outcome: TestOutcome
    message: str
    details: str


class TestRun(TypedDict):
    """One sandboxed pytest run of QA's suite."""

    loaded: bool
    exit_code: int | None
    timed_out: bool
    passed: int
    failed: int
    errors: int
    skipped: int
    cases: list[TestCaseResult]
    output: str
    duration_seconds: float


class QAFinding(TypedDict):
    """One problem QA reports; the blocking ones form the fix instruction list."""

    finding_id: str
    kind: FindingKind
    severity: Severity
    rule: str | None
    file: str
    location: str
    problem: str
    expected: str
    likely_cause: str
    fix_instruction: str
    test_ids: list[str]
    state: FindingState
    first_seen: int


class FindingResponse(TypedDict):
    """The Coder's answer to one finding (``F...``) or critique item (``C...``)."""

    finding_id: str
    action: ResponseAction
    reason: str


class Dispute(TypedDict):
    """A disputed test or finding and QA's decision on it."""

    finding_id: str
    coder_reason: str
    decision: DisputeDecision
    qa_reason: str
    decided_in: int


class TestChange(TypedDict):
    """One write QA made to its test suite."""

    path: str
    reason: TestChangeReason
    finding_id: str | None
    criteria: list[str]


class CritiqueItem(TypedDict):
    """One potential bug or edge case from the Coder's self-critique."""

    item_id: str
    kind: CritiqueKind
    location: str
    description: str
    suggested_change: str


class Reflection(TypedDict):
    """One critique-and-revise round before a QA review."""

    before_iteration: int
    round: int
    critique: list[CritiqueItem]
    revision_status: str
    files_changed: list[str]
    responses: list[FindingResponse]
    summary: str


class CoderFix(TypedDict):
    """The Coder's fix round after a ``fix_needed`` verdict."""

    status: str
    hit_step_cap: bool
    steps_used: int
    files_changed: list[str]
    responses: list[FindingResponse]
    error: str | None


class Iteration(TypedDict):
    """One QA review of the whole program and what led to it and followed it."""

    number: int
    reflections: list[Reflection]
    disputes: list[Dispute]
    test_changes: list[TestChange]
    test_run: TestRun
    findings: list[QAFinding]
    verdict: Verdict
    fix: CoderFix | None
    review_task_id: str
    fix_task_id: str | None


class QAReport(TypedDict):
    """QA's final report on a run that reached QA."""

    status: Literal["completed", "failed", "error"]
    end_reason: EndReason
    iterations_run: int
    max_iterations: int
    final_passed: int
    final_failed: int
    unresolved: list[QAFinding]
    advisory: list[QAFinding]
    per_iteration: list[str]
    report_path: str


class A2AExchange(TypedDict):
    """One request through the A2A broker, as recorded in the run's log."""

    sequence: int
    sender: str
    recipient: str
    method: str
    skill_id: str | None
    context_id: str | None
    task_id: str | None
    reference_task_ids: list[str]
    states: list[dict]
    outcome: str
    started_at: str
    finished_at: str


class QAContext(TypedDict):
    """The ``review-program`` request payload (paths are absolute path strings)."""

    iteration: int
    workspace: str
    spec: TechnicalSpec
    source_files: list[str]
    coder_notes: list[str]
    main_file: str | None
    test_suite: dict[str, str]
    previous_findings: list[QAFinding]
    responses: list[FindingResponse]


class FixRequest(TypedDict):
    """The ``fix-program`` request payload."""

    workspace: str
    iteration: int
    answer_ids: list[str]
    findings: list[QAFinding]


class FinalReportRequest(TypedDict):
    """The ``final-report`` request payload."""

    run_id: str
    workspace: str
    status: RunStatus
    end_reason: EndReason
    max_iterations: int
    iterations: list[Iteration]


class TeamState(TypedDict):
    """The state of one team run, shared by every node of the team graph."""

    run_id: str
    workspace: str
    requirement: str
    spec: TechnicalSpec | None
    tasks: list[Task]
    handoff: Task | None
    task_progress: dict[str, TaskProgress]
    code_output: list[CodeOutput]
    consolidation: Consolidation | None
    status: RunStatus
    error: str | None
    usage: TokenUsage
    duration_seconds: float
    max_iterations: int
    iterations: list[Iteration]
    pending_reflections: list[Reflection]
    test_suite: dict[str, str]
    qa_report: QAReport | None
    end_reason: EndReason | None
    a2a_log: list[A2AExchange]
