# Data Model: Production Hardening (003)

Every shape in the run state or written to disk is a `TypedDict`, a `Literal` or plain
JSON, as in 001 and 002. Existing fields keep their meaning (FR-201). Only additions are
listed. Runtime-only objects that hold state, and are never serialised, are marked
*(class)*.

## 1. Changes to `orchestration/state.py`

### 1.1 Literals

```text
EndReason      += "dependency_unavailable" | "budget_exceeded" | "interrupted"
RunStatus       (unchanged: running | completed | failed | rejected | error)
Dependency      = "anthropic" | "mcp" | "sandbox" | "chroma" | "a2a"
FailureKind     = "temporary" | "permanent"
CircuitState    = "closed" | "open" | "half_open"
ServedFrom      = "live" | "cache"
AgentName       = "pm" | "coder" | "qa"
Stage           = "plan" | "consolidate" | "build" | "reflect" | "qa_review" | "fix" | "final_report" | "team"
Severity        = "debug" | "info" | "warning" | "error"
```

Status mapping for the new end reasons: `dependency_unavailable`, `budget_exceeded` and
`interrupted` → `status = "failed"`. Resumable end reasons are `dependency_unavailable`,
`a2a_failed`, `budget_exceeded` and `interrupted`.

### 1.2 `TokenUsage` (extended)

| Field | Type | Rule |
|---|---|---|
| `input_tokens` | int ≥ 0 | uncached input (unchanged meaning) |
| `output_tokens` | int ≥ 0 | unchanged |
| `cache_creation_input_tokens` | int ≥ 0 | **new**; 0 when absent |
| `cache_read_input_tokens` | int ≥ 0 | **new**; 0 when absent |

`add_usage` / `add_tokens` sum all four. Old records without the new keys read as 0.

### 1.3 `AgentUsage` (new): one per agent per run

| Field | Type | Rule |
|---|---|---|
| `agent` | `AgentName` | |
| `models` | list[str] | every `response.model` that served a call (fallbacks included) |
| `model_calls` | int | live calls that returned a response |
| `cached_replays` | int | calls served from the response cache (cost 0) |
| `prompt_tokens` | int | Σ (input + cache_creation + cache_read) |
| `completion_tokens` | int | Σ output |
| `cache_read_tokens` | int | Σ cache_read |
| `cache_write_tokens` | int | Σ cache_creation |
| `cost_usd` | float \| None | `None` if any serving model is missing from the price table |
| `retries` | int | retried attempts across this agent's calls |
| `errors` | int | calls that ended in `DependencyFailure` or validation failure |
| `tool_calls` | int | MCP, sandbox and pytest calls made on behalf of the agent |
| `duration_ms` | int | Σ agent span durations |

### 1.4 `DependencyFailure` (new): the structured degradation result

| Field | Type | Rule |
|---|---|---|
| `dependency` | `Dependency` | |
| `target` | str | e.g. `claude-haiku-5-5`, `mcp:workspace/<run_id>`, `sandbox:<image>` |
| `kind` | `FailureKind` | from the classifier (R4) |
| `attempts` | int ≥ 1 | attempts made in this call (0 if the circuit was open: fail-fast) |
| `circuit` | `CircuitState` | state when the call ended |
| `error_type` | str | exception class name, or `refusal`, `circuit_open` |
| `message` | str | redacted cause |
| `retry_after_seconds` | float \| None | remaining cool-down when `circuit == "open"` |

Stored in `TeamState["failure"]` when a run ends `dependency_unavailable`. Returned as
the tool result body (JSON) for degraded tool calls.

### 1.5 `ResumeAttempt` (new)

| Field | Type |
|---|---|
| `resumed_at` | ISO-8601 UTC str |
| `from_stage` | `Stage` |
| `next_node` | str (graph node name) |
| `previous_end_reason` | `EndReason` \| None (`None` = killed without a final record) |
| `budget_before_usd` / `budget_after_usd` | float \| None |
| `restored_files` | list[str]: overwritten or recreated |
| `removed_files` | list[str]: present in the workspace but not in the snapshot |

### 1.6 `TeamState` additions

| Field | Type | Meaning |
|---|---|---|
| `agent_usage` | dict[`AgentName`, `AgentUsage`] | per-agent totals (directive 3, 7) |
| `cost_usd` | float \| None | run total; `None` if any agent's cost is unknown |
| `budget_usd` | float \| None | cap for this run (no cap = `None`) |
| `failure` | `DependencyFailure` \| None | set for `dependency_unavailable` |
| `resume_from` | str \| None | graph node to resume at (transient; cleared by the `resume` node) |
| `resume_history` | list[`ResumeAttempt`] | |
| `events_lost` | bool | event writes failed at least once (FR-221) |
| `trace_url` | str \| None | LangSmith root-run URL when tracing is on |
| `cost_report_path` | str \| None | `docs/cost_reports/<run_id>.json` |
| `summary` | `RunSummary` \| None | written at run end (FR-217) |

`usage` stays as the team-wide `TokenUsage` total.

### 1.7 Reply `TypedDict`s (new, validated before use; directive 4)

One per model-facing JSON schema. Field sets mirror the schemas exactly. A unit test
enforces the match. The details are in
[contracts/llm-output-validation.md](contracts/llm-output-validation.md).

| Reply type | Produced by | Mirrors |
|---|---|---|
| `TaskItemReply` | PM | `TASK_ITEM_SCHEMA` |
| `PMSpecReply` | PM `write_spec` | `SPEC_SCHEMA` |
| `PMConsolidationReply` | PM `consolidate` | `CONSOLIDATION_SCHEMA` |
| `CoderSummaryReply` (+ `FindingResponseReply`) | Coder `_finalize` | `SUMMARY_FORMAT` |
| `CritiqueReply` (+ `CritiqueItemReply`) | Coder `_critique` | `CRITIQUE_FORMAT` |
| `QATestFilesReply` (+ `TestFileReply`) | QA initial tests and load repair | `WRITE_TESTS_FORMAT` (+ `TEST_FILE_SCHEMA`) |
| `QADisputeReply` | QA dispute resolution | `DISPUTES_FORMAT` |
| `QAReviewReply` (+ `ReviewFindingReply`, `RegressionTestReply`) | QA review | `REVIEW_FORMAT` (+ `REVIEW_FINDING_SCHEMA`) |
| `ReadFileInput`, `WriteFileInput`, `ExecPythonInput` | Coder tool calls | `READ_FILE_TOOL`, `WRITE_FILE_TOOL`, `EXEC_PYTHON_TOOL` input schemas |

## 2. Run records on disk

### 2.1 `RunEvent`: one line of `workspace/.runs/<run_id>/events.jsonl`

See [contracts/run-event.schema.json](contracts/run-event.schema.json). Required fields:
`ts`, `run_id`, `agent` (`AgentName` | `team`), `stage`, `event`, `severity`, `outcome`.
The optional `details` holds `model`, `tokens` (a `TokenUsage`), `cost_usd`,
`duration_ms`, `attempt`, `dependency`, `kind`, `circuit`, `error_type`, `message`,
`tool` and `served_from`.

Event types: `run_start`, `run_end`, `stage_start`, `stage_end`, `model_call`,
`tool_call`, `sandbox_run`, `a2a_exchange`, `retry`, `restart`, `circuit_opened`,
`circuit_half_open`, `circuit_closed`, `degraded`, `validation_failed`,
`budget_stop`, `checkpoint_saved`, `resume`, `error`, `warning`.

### 2.2 `RunSummary` (in `run.json` and `TeamState["summary"]`)

| Field | Type |
|---|---|
| `per_agent` | dict[`AgentName`, {`model_calls`, `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cost_usd`, `duration_ms`, `retries`, `restarts`, `errors`}] |
| `per_stage` | dict[`Stage`, same counters] |
| `totals` | same counters for the run |
| `events_lost` | bool |
| `attempts` | int (1 + resumes) |

### 2.3 `RunCheckpoint`: `workspace/.runs/<run_id>/checkpoint.json`

| Field | Type | Rule |
|---|---|---|
| `format_version` | int | `1`; another value → resume refused |
| `app_version` | str | git describe or package version; a mismatched major version → refused |
| `complete` | bool | must be `true` (written last, atomically via `os.replace`) |
| `saved_at` | ISO-8601 str | |
| `stage` | `Stage` | last completed stage |
| `next_node` | str | where `resume` routes |
| `state` | `TeamState` | redacted; `resume_from` is `None`. Its `test_suite` is QA's own copy of its tests (FR-213), restored on resume so QA's tamper check still works |
| `settings` | {models, efforts, max_iterations, max_steps, exec_timeout, reflection_rounds, prompt_cache_ttl, budget_usd} | compared on resume (FR-213c) |
| `ledger` | dict[`AgentName`, `AgentUsage`] | carried forward |
| `workspace` | dict[path, {`b64`: str, `mode`: int}] | full snapshot incl. `tests/`, `.qa/` |

State transitions for a run: `running → (checkpoint after each stage)* → final record`.
A final record has end reason `complete|iteration_limit|…`. A resumable end reason, or a
missing final record with the lock not held, allows `resume`. The run then goes back to
`running` with the same `run_id`.

### 2.4 `CostReport`: `docs/cost_reports/<run_id>.json`

See [contracts/cost-report.schema.json](contracts/cost-report.schema.json). It holds the
run ID, timestamps, status, end reason, price table version, prompt-cache TTL, budget,
and per-agent {`model`, `models`, `model_calls`, `cached_replays`, `prompt_tokens`,
`completion_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`}. It also has
`total` {tokens, `cost_usd`}, `attempts` and `events_lost`.

### 2.5 `PriceTable`: `cost/prices.json`

```text
{ "version": "2026-10-06", "currency": "USD", "unit": "per_million_tokens",
  "source": "<pricing page URL>",
  "models": { "<model id>": { "input": n, "output": n, "cache_read": n,
                              "cache_write_5m": n, "cache_write_1h": n,
                              "long_context": { "threshold_prompt_tokens": n, "input": n, ... } | null } } }
```

Validation: all prices ≥ 0. `version` is required. Overridable with
`PRICE_TABLE_PATH`.

### 2.6 `HealthReport` (`python -m orchestration.health --json`)

`{"ok": bool, "items": [{"name": str, "status": "ok"|"missing"|"error"|"info", "message": str}]}`.
The item names are `credentials`, `docker`, `sandbox_image`, `workspace`,
`cost_report_dir`, `chroma`, `price_table` and `langsmith`.

## 3. Runtime objects *(classes, not serialised)*

| Object | Holds | Shared by |
|---|---|---|
| `RetryPolicy` (frozen dataclass) | `max_retries`, `base_delay`, `max_delay`, `rng`, `sleep` | all guards |
| `CircuitBreaker` | key, threshold (3), cool-down, state, consecutive failures, opened_at, lock, clock | the run's callers of one dependency |
| `DependencyGuard` | breaker + policy + classifier + event/trace hooks; `call(fn) -> T \| DependencyFailure` | per dependency |
| `ResponseCache` | dir, TTL, in-memory index | all `ModelCaller`s in the run |
| `UsageLedger` | per-agent `AgentUsage`, lock | all callers; read by `finish` |
| `RunBudget` | cap, spent, lock; `check()` raises `BudgetExceeded` | all `ModelCaller`s |
| `EventLog` | file handle, min severity, `events_lost`, redactor | everyone |
| `Tracer` | LangSmith client or no-op, root `RunTree`, redactor | everyone; `headers()` for A2A |
| `ModelCaller` | agent name, Anthropic client, guard, cache, ledger, budget, tracer, events, TTL | one per agent |
| `RunServices` | all of the above, plus the stop `Event` and run ID | built in `run_team`, passed to the agents |

### CircuitBreaker state machine

```text
closed --(3rd consecutive failed call)--> open
open --(cool-down elapsed; next call admitted as trial)--> half_open
half_open --(trial succeeds)--> closed   (failures := 0)
half_open --(trial fails)--> open        (opened_at := now)
open/half_open (non-trial callers) --> fail fast: DependencyFailure(circuit=open, attempts=0)
closed --(success)--> closed (failures := 0)
```

## 4. Validation rules (summary)

- Every reply type listed above is validated with `orchestration.validation.validate`
  before any field is read. An unknown key, a missing required key, a wrong type or a
  value outside a `Literal` → invalid.
- `PROMPT_CACHE_TTL` ∈ {`5m`, `1h`, `off`}. `CIRCUIT_COOLDOWN_SECONDS` ∈ [1, 3600].
  `RETRY_MAX_RETRIES` ∈ [0, 5]. `RUN_BUDGET_USD` > 0. `LLM_RESPONSE_CACHE_TTL_SECONDS` ≥ 0
  (0 turns the cache off). A bad value → exit 2 with the variable's name.
- `CODER_MODEL`/`QA_MODEL` with `*_USE_FALLBACKS=true` on a model without fallback
  support → exit 2 (FR-225b).
