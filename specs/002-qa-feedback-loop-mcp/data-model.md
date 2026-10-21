# Data Model: QA & Debugger Agent, Coder ⇄ QA Feedback Loop, and MCP File Tools

**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md) | **Date**: 2026-10-18

Every type below is a `typing.TypedDict` or `Literal` in `orchestration/state.py`,
following the rule set in 001 (data-model "Increment 2: Shared Team State"): every value
converts to JSON with `json.dumps` and back without loss. The existing types (`Task`,
`TechnicalSpec`, `TaskProgress`, `CodeOutput`, `Consolidation`, `TokenUsage`) are
unchanged.

## New literals

```python
TestOutcome = Literal["passed", "failed", "error", "skipped"]
FindingKind = Literal["failing_test", "bug", "style"]
Severity = Literal["blocking", "advisory"]
FindingState = Literal["open", "fixed", "disputed", "withdrawn"]
ResponseAction = Literal["fixed", "disputed", "not_addressed"]
DisputeDecision = Literal["keep", "correct"]
TestChangeReason = Literal["initial", "load_repair", "dispute_correction", "regression", "restored"]
CritiqueKind = Literal["bug", "edge_case"]
Verdict = Literal["complete", "fix_needed"]
EndReason = Literal[
    "complete", "iteration_limit", "task_failed", "coder_failed",
    "reflection_failed", "qa_failed", "a2a_failed", "rejected", "pm_error",
]
```

`RunStatus` stays `running | completed | failed | rejected | error`. `end_reason` gives
the detail.

## Test results

### TestCaseResult

One `<testcase>` from the JUnit XML report.

| Field | Type | Rule |
|-------|------|------|
| `test_id` | str | pytest node ID, e.g. `tests/test_convert.py::test_zero`. Unique within a `TestRun` |
| `file` | str | workspace-relative test file |
| `outcome` | TestOutcome | |
| `message` | str | failure or error message; `""` when the test passed |
| `details` | str | traceback, at most 2,000 characters, ending in `[truncated]` when cut |

### TestRun

One `run_pytest` call (research R7).

| Field | Type | Rule |
|-------|------|------|
| `loaded` | bool | False when the exit code is 2–5, the XML is missing or unreadable, or a QA test file produced no results. A timeout is still `loaded=True` (a timeout is a failure) |
| `exit_code` | int \| None | None when the run timed out |
| `timed_out` | bool | |
| `passed`, `failed`, `errors`, `skipped` | int | counted from `cases`. A timeout with no XML gives `failed=1` |
| `cases` | list[TestCaseResult] | |
| `output` | str | last 10,000 characters of stdout and stderr (the load-error text for `repair_tests`) |
| `duration_seconds` | float | |

## QA findings and the Coder's answers

### QAFinding

One problem QA reports. The blocking findings, in order, are the **fix instruction list**.

| Field | Type | Rule |
|-------|------|------|
| `finding_id` | str | `F<iteration>-<n>`. Keeps an earlier ID when the review says it is the same problem (`same_as`) |
| `kind` | FindingKind | |
| `severity` | Severity | Set in code (research R10): `failing_test` and `bug` are always `blocking`; `style` is `blocking` only if `rule` is set |
| `rule` | str \| None | One of `CONSTITUTION_RULES`. Only for `style` |
| `file` | str | workspace-relative source file (`""` if unknown) |
| `location` | str | function, class or line, e.g. `convert() line 12` |
| `problem` | str | non-empty |
| `expected` | str | expected behavior, taken from the spec. Non-empty |
| `likely_cause` | str | QA's diagnosis. Non-empty for `failing_test` and `bug` |
| `fix_instruction` | str | one imperative instruction for the Coder. Non-empty |
| `test_ids` | list[str] | the failing tests this finding covers. At least one for `failing_test` |
| `state` | FindingState | `open` when created. See the lifecycle below |
| `first_seen` | int | the iteration in which the problem was first reported |

**Lifecycle** (worked out at the end of each QA iteration):

```
open ──(not reported again in a later review)──────────────→ fixed
open ──(Coder answers "disputed")──→ disputed ──(QA: keep)───→ open   (same ID, carried on)
                                         └──────(QA: correct)→ withdrawn
```

The unresolved findings are the `open` and `disputed` findings with severity `blocking`
after the last iteration.

### FindingResponse

The Coder's answer to one finding or critique item, from its run summary (research R12).

| Field | Type | Rule |
|-------|------|------|
| `finding_id` | str | an `F…` ID from the fix brief, or a `C…` ID from a critique |
| `action` | ResponseAction | |
| `reason` | str | required for `disputed`, and must refer to the spec |

### Dispute

| Field | Type | Rule |
|-------|------|------|
| `finding_id` | str | |
| `coder_reason` | str | |
| `decision` | DisputeDecision | |
| `qa_reason` | str | refers to the spec |
| `decided_in` | int | the iteration whose step 3 decided it |

### TestChange

One write QA made to the suite.

| Field | Type | Rule |
|-------|------|------|
| `path` | str | must be under `tests/`. Anything else is rejected and QA's turn ends with an error |
| `reason` | TestChangeReason | `initial` only in iteration 1. `regression` must name a `finding_id` and trace to an acceptance criterion (FR-107a) |
| `finding_id` | str \| None | |
| `criteria` | list[str] | the acceptance criteria the file's tests trace to (FR-102). Each must appear in `spec.acceptance_criteria` or in a task's criteria |

## Coder self-reflection

### CritiqueItem

| Field | Type | Rule |
|-------|------|------|
| `item_id` | str | `C1`…`Cn`, assigned in code |
| `kind` | CritiqueKind | |
| `location` | str | |
| `description` | str | non-empty |
| `suggested_change` | str | |

### Reflection

One critique-and-revise round before a QA handoff.

| Field | Type | Rule |
|-------|------|------|
| `before_iteration` | int | the QA iteration this round prepares for |
| `round` | int | 1 to the `CODER_REFLECTION_ROUNDS` setting |
| `critique` | list[CritiqueItem] | at least one item |
| `revision_status` | str | the revision `CoderResult.status`: `completed` or `step_limit` (either way QA follows). `refused` or `error` ends the run (`reflection_failed`) |
| `files_changed` | list[str] | `CoderResult.files_written` from the revision |
| `responses` | list[FindingResponse] | answers to `C…` items |
| `summary` | str | the revision's `explanation` |

## Iterations, report and team state

### CoderFix

The fix round that follows a `fix_needed` verdict.

| Field | Type | Rule |
|-------|------|------|
| `status` | str | `completed` or `step_limit` lets the loop continue. `refused` or `error` ends the run (`coder_failed`) |
| `hit_step_cap` | bool | `status == "step_limit"` (FR-116a) |
| `steps_used` | int | |
| `files_changed` | list[str] | |
| `responses` | list[FindingResponse] | |
| `error` | str \| None | |

### Iteration

One QA review of the whole program (FR-108). Kept in `TeamState.iterations`, in order.

| Field | Type | Rule |
|-------|------|------|
| `number` | int | from 1, consecutive, ≤ `max_iterations` |
| `reflections` | list[Reflection] | the reflection before this review (at least one round) |
| `disputes` | list[Dispute] | decided in this iteration |
| `test_changes` | list[TestChange] | |
| `test_run` | TestRun | the final (post-repair) run used for the verdict |
| `findings` | list[QAFinding] | everything reported in this iteration, in order: blocking first, then advisory |
| `verdict` | Verdict | |
| `fix` | CoderFix \| None | set by the `fix` node. None for the last iteration |
| `review_task_id` | str | the A2A task ID of this iteration's `review-program` exchange |
| `fix_task_id` | str \| None | the A2A task ID of the `fix-program` exchange that followed, if any |

### QAReport

QA's final report (research R14). Also written as `<workspace>/.qa/report.md`.

| Field | Type | Rule |
|-------|------|------|
| `status` | `"completed"` \| `"failed"` \| `"error"` | |
| `end_reason` | EndReason | |
| `iterations_run` | int | |
| `max_iterations` | int | |
| `final_passed`, `final_failed` | int | from the last `TestRun`. `final_failed` includes errors |
| `unresolved` | list[QAFinding] | open or disputed blocking findings. Empty if and only if `status == "completed"` |
| `advisory` | list[QAFinding] | from the last iteration |
| `per_iteration` | list[str] | one line each: `#2: 7 passed, 1 failed, 1 blocking` |
| `report_path` | str | workspace-relative, `.qa/report.md` |

### TeamState: new keys

| Key | Type | Initial | Written by | Read by |
|-----|------|---------|-----------|---------|
| `max_iterations` | int | from `run_team(max_iterations=…)`, 1–10 | `run_team` | `route_after_qa` |
| `iterations` | list[Iteration] | `[]` | `qa` (appends), `fix` (sets `fix` on the last one) | routers, `qa`, `finish` |
| `pending_reflections` | list[Reflection] | `[]` | `reflect` | `qa` (moves them into the new iteration and clears this key) |
| `test_suite` | dict[str, str] | `{}` | `qa` (QA's own copy of the suite, path → content, research R5) | `qa` |
| `qa_report` | QAReport \| None | None | `finish` | caller |
| `end_reason` | EndReason \| None | None | the node that ends the run | `finish`, caller |
| `a2a_log` | list[A2AExchange] | `[]` | `qa`, `fix` and `finish`: after each send, the node sets it to `clients[<sender>].exchanges()`, the whole run's log as copies. Nodes never hold the broker | caller, tests |

## Validation rules (enforced in code)

- `max_iterations` must be from 1 to 10. Otherwise `run_team` raises `ValueError`, and
  the CLI exits with status 2 before any model call (FR-115).
- `CODER_REFLECTION_ROUNDS` must be from 1 to 3 (FR-127). Otherwise `CoderConfig`
  raises `ValueError`.
- Every QA `TestChange.path` must resolve inside `tests/` (FR-107).
- `verdict == "complete"` if and only if `test_run.loaded`, the run has no failures or
  errors, the suite check passed, and no finding has severity `blocking` (FR-106).
- `len(iterations) <= max_iterations` always holds (SC-101). It is checked in
  `route_after_qa`, and the loop tests assert it.

## QA request payloads (JSON-only, in `orchestration/state.py`)

These are the payloads that cross the A2A broker inside `data` parts. They are defined
in `orchestration/state.py`, not in an agent module, so the team graph, the endpoints
and the agents can share them without the endpoints or either agent importing the other
agent (SC-110). Every value is plain JSON: paths are absolute path **strings**, and the
receiving agent converts them to `Path` itself. Field names are snake_case
([contracts/a2a-binding.md](contracts/a2a-binding.md) §2).

### QAContext (`review-program` request; built by `qa_node`)

| Field | Type | Rule |
|-------|------|------|
| `iteration` | int | `len(iterations) + 1`, ≤ `max_iterations` |
| `workspace` | str | absolute path of the run's workspace |
| `spec` | TechnicalSpec | |
| `source_files` | list[str] | non-hidden workspace files outside `tests/` |
| `coder_notes` | list[str] | each task's summary, plus the last reflection's summary |
| `main_file` | str \| None | |
| `test_suite` | dict[str, str] | QA's copy of the suite. `{}` in iteration 1 |
| `previous_findings` | list[QAFinding] | the last iteration's `open` findings. `[]` in iteration 1 |
| `responses` | list[FindingResponse] | the last fix round's responses. `[]` in iteration 1 |

### FixRequest (`fix-program` request; built by `fix_node`)

| Field | Type | Rule |
|-------|------|------|
| `workspace` | str | absolute path |
| `iteration` | int | the QA iteration whose findings are being fixed |
| `answer_ids` | list[str] | the blocking finding IDs the Coder must answer |
| `findings` | list[QAFinding] | that iteration's findings, blocking first |

### FinalReportRequest (`final-report` request; built by `finish_node`)

| Field | Type | Rule |
|-------|------|------|
| `run_id` | str | |
| `workspace` | str | absolute path |
| `status` | RunStatus | the final status `finish` decided |
| `end_reason` | EndReason | the final end reason `finish` decided |
| `max_iterations` | int | |
| `iterations` | list[Iteration] | non-empty (the report is only requested when QA ran) |

## A2A protocol types (A2A 1.0; research R17–R21)

These live in `messaging/a2a.py` as `TypedDict`s (with `total=False` where the spec
marks fields optional). Field names are the protocol's camelCase JSON names. They are
**not** imported into `orchestration/state.py`. The team state stores only the
`A2AExchange` log entries below.

```python
TaskState = Literal["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED",
                    "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
                    "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"]
Role = Literal["ROLE_USER", "ROLE_AGENT"]
TERMINAL_STATES = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
                   "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
```

| Type | Fields (required **bold**) | Rules |
|------|----------------------------|-------|
| `Part` | `text` \| `data` \| `raw` \| `url` (exactly one), `mediaType`, `filename`, `metadata` | No `kind`. This binding uses `text` (`text/plain`) and `data` (`application/json`) only |
| `Message` | **`messageId`**, **`role`**, **`parts`** (≥ 1), `contextId`, `taskId`, `metadata`, `extensions`, `referenceTaskIds` | `messageId` is a `uuid4` hex. `metadata.skillId` names the skill. `metadata.sender` names the sending agent |
| `TaskStatus` | **`state`**, `message`, `timestamp` | `timestamp` is ISO-8601 UTC with ms precision and a `Z` suffix. `message` holds the failure or rejection reason |
| `Artifact` | **`artifactId`**, **`parts`** (≥ 1), `name`, `description`, `metadata`, `extensions` | `artifactId` is unique within its task |
| `Task` | **`id`**, **`contextId`**, **`status`**, `artifacts`, `history`, `metadata` | `contextId` = the team `run_id` |
| `SendMessageConfiguration` | `acceptedOutputModes`, `historyLength`, `returnImmediately` (default false) | |
| `SendMessageRequest` | **`message`**, `configuration`, `metadata` | |
| `SendMessageResponse` | `task` \| `message` (exactly one) | This binding always returns `task` |
| `AgentSkill` | **`id`**, **`name`**, **`description`**, **`tags`**, `examples`, `inputModes`, `outputModes` | |
| `AgentInterface` | **`url`**, **`protocolBinding`**, **`protocolVersion`** | `broker://<name>`, the R18 URN, `"1.0"` |
| `AgentCapabilities` | `streaming`, `pushNotifications`, `extendedAgentCard` | all `false` |
| `AgentCard` | **`name`**, **`description`**, **`supportedInterfaces`**, **`version`**, **`capabilities`**, **`defaultInputModes`**, **`defaultOutputModes`**, **`skills`** | |
| `JsonRpcRequest` | **`jsonrpc`** (`"2.0"`), **`id`**, **`method`**, `params` | |
| `JsonRpcResponse` | **`jsonrpc`**, **`id`**, `result` \| `error` | `error = {code, message, data: {type, detail}}` |
| `Envelope` | **`sender`**, **`recipient`**, **`serviceParameters`**, **`rpc`** | binding-specific (R18) |

**Task state transitions** (enforced by the broker):

```
SUBMITTED ──worker starts──→ WORKING ──handler returns artifacts──→ COMPLETED
    │                           ├──handler reports failure / raises──→ FAILED
    │                           └──skill or payload invalid──────────→ REJECTED
    └──CancelTask while queued──→ CANCELED
```

Every transition appends a timestamp to the exchange log. Terminal states never change.

### A2AExchange (stored in `TeamState.a2a_log`)

| Field | Type | Rule |
|-------|------|------|
| `sequence` | int | from 1, in the order the broker accepted requests |
| `sender` | str | `coder`, `qa-debugger` or `team` |
| `recipient` | str | an agent card name |
| `method` | str | e.g. `SendMessage`, `GetTask` |
| `skill_id` | str \| None | from `message.metadata.skillId` |
| `context_id` | str \| None | equals `run_id` for loop traffic |
| `task_id` | str \| None | |
| `reference_task_ids` | list[str] | |
| `states` | list[dict] | `[{"state": TaskState, "timestamp": str}]`, in order |
| `outcome` | str | the final `TaskState`, or `error:<code>` for JSON-RPC errors |
| `started_at`, `finished_at` | str | ISO-8601 UTC |

**Validation**: `outcome` is terminal or an error. `states` starts with
`TASK_STATE_SUBMITTED` for a `SendMessage` that was accepted. For loop traffic:
- the number of `review-program` entries equals `len(iterations)`;
- the number of `fix-program` entries is at most `len(iterations) - 1`;
- every `fix-program` entry's `reference_task_ids` is `[iterations[k].review_task_id]`
  for the iteration it answers.
