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

Production hardening (003) adds per-agent usage and cost, structured dependency
failures, resume history, the run summary, and one reply ``TypedDict`` per model-facing
JSON schema, so every model output is validated before it is used
(specs/003-production-hardening/data-model.md).
"""

from typing import Literal, NotRequired, TypedDict

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
ConstitutionRule = Literal[
    "intent-revealing-names",
    "function-does-one-thing",
    "public-api-type-hints-and-docstrings",
    "comments-explain-why",
    "no-commented-out-code",
    "explicit-error-handling",
    "extract-triplicated-logic",
    "python-3.12-only",
    "pep8-pep257",
    "import-grouping",
    "no-wildcard-imports",
    "no-hardcoded-secrets",
    "main-guard",
]
"""The constitution MUST rules a QA style finding may cite to become blocking."""
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
    "dependency_unavailable",
    "budget_exceeded",
    "interrupted",
]
RESUMABLE_END_REASONS = ("dependency_unavailable", "a2a_failed", "budget_exceeded", "interrupted")
Dependency = Literal["anthropic", "mcp", "sandbox", "chroma", "a2a"]
FailureKind = Literal["temporary", "permanent"]
CircuitState = Literal["closed", "open", "half_open"]
ServedFrom = Literal["live", "cache"]
AgentName = Literal["pm", "coder", "qa"]
Stage = Literal["plan", "consolidate", "build", "reflect", "qa_review", "fix", "final_report", "team"]
# Named EventSeverity because ``Severity`` already means a finding's severity.
EventSeverity = Literal["debug", "info", "warning", "error"]


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
    """Tokens used by model calls, summed over the run.

    ``input_tokens`` is uncached input. Records written before 003 have no cache keys;
    readers treat a missing key as 0.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: NotRequired[int]
    cache_read_input_tokens: NotRequired[int]


class AgentUsage(TypedDict):
    """One agent's model and tool use in a run, with its cost (data-model §1.3)."""

    agent: AgentName
    models: list[str]
    model_calls: int
    cached_replays: int
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float | None
    retries: int
    errors: int
    tool_calls: int
    duration_ms: int


class DependencyFailure(TypedDict):
    """A call to an external dependency that could not be served (data-model §1.4)."""

    dependency: Dependency
    target: str
    kind: FailureKind
    attempts: int
    circuit: CircuitState
    error_type: str
    message: str
    retry_after_seconds: float | None


class ResumeAttempt(TypedDict):
    """One continuation of an interrupted or resumable run (data-model §1.5)."""

    resumed_at: str
    from_stage: Stage
    next_node: str
    previous_end_reason: EndReason | None
    budget_before_usd: float | None
    budget_after_usd: float | None
    restored_files: list[str]
    removed_files: list[str]


class SummaryCounters(TypedDict):
    """The totals the run summary keeps per agent, per stage and for the run."""

    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cost_usd: float | None
    duration_ms: int
    retries: int
    restarts: int
    errors: int


class RunSummary(TypedDict):
    """Totals for a run across every attempt (data-model §2.2, FR-217)."""

    per_agent: dict[str, SummaryCounters]
    per_stage: dict[str, SummaryCounters]
    totals: SummaryCounters
    events_lost: bool
    attempts: int


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
    agent_usage: dict[str, AgentUsage]
    cost_usd: float | None
    budget_usd: float | None
    failure: DependencyFailure | None
    resume_from: str | None
    resume_history: list[ResumeAttempt]
    events_lost: bool
    trace_url: str | None
    cost_report_path: str | None
    summary: RunSummary | None


# Reply types: one per JSON schema sent to the model, mirrored field for field
# (tests/test_validation.py::SchemaAgreementTests keeps them in step). Every reply is
# checked with ``orchestration.validation.validate`` before anything in it is used.


class TaskItemReply(TypedDict):
    """One task in a PM reply (``TASK_ITEM_SCHEMA``)."""

    title: str
    description: str
    acceptance_criteria: list[str]
    priority: Priority


class PMSpecReply(TypedDict):
    """The PM's ``write_spec`` reply (``SPEC_SCHEMA``)."""

    name: str
    description: str
    acceptance_criteria: list[str]
    assumptions: list[str]
    tasks: list[TaskItemReply]
    rejection_reason: str


class PMConsolidationReply(TypedDict):
    """The PM's ``consolidate`` reply (``CONSOLIDATION_SCHEMA``)."""

    tasks: list[TaskItemReply]


class FindingResponseReply(TypedDict):
    """One answer in the Coder's summary (``SUMMARY_FORMAT`` ``finding_responses``)."""

    finding_id: str
    action: ResponseAction
    reason: str


class CoderSummaryReply(TypedDict):
    """The Coder's closing summary (``SUMMARY_FORMAT``)."""

    plan: list[str]
    explanation: str
    main_file: str
    finding_responses: list[FindingResponseReply]


class CritiqueItemReply(TypedDict):
    """One item of the Coder's self-critique (``CRITIQUE_FORMAT`` ``items``)."""

    kind: CritiqueKind
    location: str
    description: str
    suggested_change: str


class CritiqueReply(TypedDict):
    """The Coder's self-critique (``CRITIQUE_FORMAT``)."""

    items: list[CritiqueItemReply]
    overall: str


class TestFileReply(TypedDict):
    """One test file QA writes (``TEST_FILE_SCHEMA``)."""

    path: str
    content: str
    criteria: list[str]


class QATestFilesReply(TypedDict):
    """QA's initial tests or load repair (``WRITE_TESTS_FORMAT``)."""

    test_files: list[TestFileReply]
    notes: str


class DisputeDecisionReply(TypedDict):
    """QA's decision on one disputed finding (``DISPUTES_FORMAT`` ``decisions``)."""

    finding_id: str
    decision: DisputeDecision
    reason: str


class QADisputeReply(TypedDict):
    """QA's dispute resolution (``DISPUTES_FORMAT``)."""

    decisions: list[DisputeDecisionReply]
    corrected_files: list[TestFileReply]


class ReviewFindingReply(TypedDict):
    """One finding in QA's review (``REVIEW_FINDING_SCHEMA``)."""

    kind: FindingKind
    rule: Literal["", ConstitutionRule]
    file: str
    location: str
    problem: str
    expected: str
    likely_cause: str
    fix_instruction: str
    test_ids: list[str]
    same_as: str


class RegressionTestReply(TypedDict):
    """A regression test QA adds for a finding (``REVIEW_FORMAT`` ``regression_tests``)."""

    finding_index: int
    path: str
    content: str
    criteria: list[str]


class QAReviewReply(TypedDict):
    """QA's review (``REVIEW_FORMAT``)."""

    findings: list[ReviewFindingReply]
    regression_tests: list[RegressionTestReply]
    summary: str


class ReadFileInput(TypedDict):
    """The input the model gives ``read_file`` (with the session's ``purpose``)."""

    path: str
    purpose: str


class WriteFileInput(TypedDict):
    """The input the model gives ``write_file`` (with the session's ``purpose``)."""

    path: str
    content: str
    purpose: str


class ExecPythonInput(TypedDict):
    """The input the model gives ``exec_python`` (with the session's ``purpose``)."""

    path: str
    purpose: str
