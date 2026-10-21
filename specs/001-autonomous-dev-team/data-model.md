# Data Model: Autonomous Multi-Agent Development Team (Increments 1 and 2)

**Plan**: [plan.md](plan.md) | **Research**: [research.md](research.md)

Increment 1 (Coder Agent) types come first; [Increment 2: Shared Team State](#increment-2-shared-team-state) follows.

All types are stdlib `dataclass`es or `TypedDict`s. Field names here are the contract used in
[contracts/coder-agent-api.md](contracts/coder-agent-api.md).

## CoderConfig

Agent settings; every field has a default and an environment override.

| Field | Type | Default | Env override | Rule |
|-------|------|---------|--------------|------|
| `model` | `str` | `"claude-sonnet-5-5"` | `CODER_MODEL` | non-empty |
| `effort` | `str` | `"medium"` | `CODER_EFFORT` | one of `low/medium/high/xhigh/max` |
| `max_steps` | `int` | `10` | `CODER_MAX_STEPS` | 1–10 (hard cap of 10 ReAct steps, i.e. Coder model calls; not the spec's Coder ⇄ QA iteration limit) |
| `exec_timeout_seconds` | `float` | `30.0` | `CODER_EXEC_TIMEOUT` | > 0 |
| `window_turns` | `int` | `5` | — | ≥ 1 |
| `memory_top_k` | `int` | `3` | — | ≥ 1 |
| `workspace_root` | `Path` | `./workspace` | `CODER_WORKSPACE` | created if missing |
| `chroma_path` | `Path` | `./.chroma` | `CODER_CHROMA_PATH` | created if missing |
| `use_fallbacks` | `bool` | `True` | `CODER_USE_FALLBACKS` | — |
| `sandbox_image` | `str` | `python:3.12-slim@sha256:a6e3…` | `CODER_SANDBOX_IMAGE` | non-empty; keep pinned by digest |

## Turn (short-term memory item)

| Field | Type | Notes |
|-------|------|-------|
| `task` | `str` | The user's request for that run |
| `reply` | `str` | The run's final `explanation` |

**SlidingWindowBuffer**: holds at most `window_turns` turns; adding a sixth evicts the
oldest; `get()` returns turns oldest-first. Lives as long as the `CoderAgent` instance.

## MemoryRecord (long-term memory item, stored in Chroma)

| Field | Type | Notes |
|-------|------|-------|
| `id` | `str` | `"<run_id>-<seq>"`, unique per record |
| `document` | `str` | Embedded text, ≤ 2,000 chars |
| `metadata.run_id` | `str` | Run that produced it; used to exclude the current run |
| `metadata.kind` | `str` | `task` \| `observation` \| `result` |
| `metadata.created_at` | `str` | ISO 8601 UTC |

Retrieval returns up to `memory_top_k` documents most similar to the query, excluding the
current run. Empty collection → empty list (no error).

## ToolCall / Observation

| Field | Type | Notes |
|-------|------|-------|
| `tool` | `str` | `read_file` \| `write_file` \| `exec_python` |
| `input` | `dict` | Validated tool arguments (strict schema) |
| `output` | `str` | Text returned to the model |
| `is_error` | `bool` | True for rejected paths, missing files, etc. |

## ExecutionResult

Returned by `exec_python` and copied into `CoderResult.execution_result`.

| Field | Type | Notes |
|-------|------|-------|
| `path` | `str` | Workspace-relative file that was run |
| `exit_code` | `int \| None` | `None` if killed on timeout |
| `stdout` | `str` | ≤ 10,000 chars, `[truncated]` marker if cut |
| `stderr` | `str` | ≤ 10,000 chars, `[truncated]` marker if cut |
| `timed_out` | `bool` | True if the timeout killed the process |
| `duration_seconds` | `float` | Wall-clock time |

## Step (ReAct trace entry)

| Field | Type | Notes |
|-------|------|-------|
| `number` | `int` | 1-based step number |
| `thought` | `str` | Thinking summary + any text the model wrote; if there is none, the `purpose` of each tool call (never empty when the step called a tool) |
| `actions` | `list[ToolCall]` | Tool calls requested this step (may be empty on the last step) |

## AgentState (LangGraph graph state, internal)

| Field | Type | Notes |
|-------|------|-------|
| `run_id` | `str` | `YYYYMMDD-HHMMSS-<6 hex>` |
| `task` | `str` | User request |
| `messages` | `list[dict]` | Append-only Claude message history (R3) |
| `step_count` | `int` | Count of `think` calls; router stops at `max_steps` |
| `injected_memory_ids` | `list[str]` | Memories already shown this run |
| `steps` | `list[Step]` | Trace |
| `files_written` | `list[str]` | Workspace-relative paths |
| `last_execution` | `ExecutionResult \| None` | Most recent `exec_python` result |
| `stop_reason` | `str \| None` | Last Claude `stop_reason` |
| `status` | `str` | see state transitions |
| `result` | `CoderResult \| None` | Set by `finalize` |

## CoderResult (public output)

| Field | Type | Required by user | Notes |
|-------|------|:---:|-------|
| `code` | `str` | ✔ | Contents of `main_file` (empty string if none written) |
| `explanation` | `str` | ✔ | Natural-language description of the solution |
| `plan` | `list[str]` | ✔ | Ordered steps actually executed |
| `execution_result` | `ExecutionResult \| None` | ✔ | Ground-truth run of `main_file`; `None` only if no file exists |
| `status` | `str` | | `completed` \| `step_limit` \| `refused` \| `error` |
| `main_file` | `str \| None` | | Workspace-relative path of the primary file |
| `files_written` | `list[str]` | | All files the agent wrote |
| `steps_used` | `int` | | Number of `think` calls used (≤ `max_steps`) |
| `duration_seconds` | `float` | | Wall-clock time of the whole run, including `finalize` |
| `usage` | `dict` | | `{"input_tokens": int, "output_tokens": int}` summed over every model call in the run, from each response's `usage` |
| `steps` | `list[Step]` | | Full Thought/Action/Observation trace |
| `run_id` | `str` | | Also the workspace folder name |
| `workspace` | `str` | | Absolute path of `workspace/<run_id>/` |
| `error` | `str \| None` | | Set when `status` is `refused` or `error` |

`to_dict()` returns a JSON-serialisable dict with exactly these keys.

## State transitions (`AgentState.status`)

```text
running ──end_turn──────────────→ completed
running ──step_count == max_steps→ step_limit
running ──stop_reason=refusal───→ refused
running ──API/config failure────→ error
```

Every terminal status passes through `finalize`, so a `CoderResult` with all four required
fields is always returned (fields may be empty strings/lists on `refused`/`error`).

## CoderAgent.run change (Increment 2)

`run(task, workspace=None)`: when `workspace` is given, the run uses that folder (created
if missing) and `CoderResult.workspace` is its absolute path. Everything else is
unchanged (research R17).

---

# Increment 2: Shared Team State

Defined in `orchestration/state.py`. Every type is a `typing.TypedDict` (research R12), so
any value converts to JSON with `json.dumps` and back with `json.loads` without loss.
Field names are the contract used in [contracts/handoff-protocol.md](contracts/handoff-protocol.md)
and [contracts/team-api.md](contracts/team-api.md).

## Value sets (`Literal`)

| Name | Values |
|------|--------|
| `Priority` | `"high"`, `"medium"`, `"low"` |
| `TaskStatus` | `"pending"`, `"in_progress"`, `"completed"`, `"failed"`, `"skipped"` |
| `RunStatus` | `"running"`, `"completed"`, `"failed"`, `"rejected"`, `"error"` |
| `ConsolidationMethod` | `"pm"`, `"merge"` |

## Task (also the handoff message)

| Field | Type | Rule |
|-------|------|------|
| `task_id` | `str` | `T1`…`Tn` by position, assigned by the system after every PM output; unique in the run |
| `title` | `str` | Non-empty, short imperative ("Parse command-line arguments") |
| `description` | `str` | Non-empty; what to build, with inputs and outputs |
| `acceptance_criteria` | `list[str]` | ≥ 1 item; each is checkable by running the program |
| `priority` | `Priority` | Informs the Coder; does not change the run order (research R14) |

## TechnicalSpec

| Field | Type | Rule |
|-------|------|------|
| `name` | `str` | Non-empty, short name of the software |
| `description` | `str` | Non-empty; intended behavior, inputs and outputs (FR-005) |
| `acceptance_criteria` | `list[str]` | ≥ 1 item; criteria for the whole program |
| `assumptions` | `list[str]` | Every assumption the PM made (FR-007); may be empty |
| `tasks` | `list[Task]` | 1–8 items after the PM nodes; ordered by dependency, then priority |

## TaskProgress (per-task status)

| Field | Type | Notes |
|-------|------|-------|
| `task_id` | `str` | Matches a `Task.task_id` |
| `status` | `TaskStatus` | See transitions below |
| `coder_status` | `str \| None` | The Coder's `CoderResult.status` (`completed`, `step_limit`, `refused`, `error`); `None` until the Coder ran |
| `summary` | `str` | The Coder's `explanation`; `""` until the Coder ran |
| `files_written` | `list[str]` | Workspace-relative paths this task wrote |
| `steps_used` | `int` | Coder model calls used (≤ 10) |
| `duration_seconds` | `float` | The Coder's wall-clock time for this task; `0.0` until the Coder ran |
| `error` | `str \| None` | The Coder's error, if any |

## CodeOutput (accumulated code)

| Field | Type | Notes |
|-------|------|-------|
| `task_id` | `str` | Task that wrote the file |
| `path` | `str` | Workspace-relative path |
| `content` | `str` | File contents read from disk right after the task |

## Consolidation

| Field | Type | Notes |
|-------|------|-------|
| `proposed_count` | `int` | Number of tasks in the PM's first plan (> 8) |
| `final_count` | `int` | Number after consolidation (1–8) |
| `method` | `ConsolidationMethod` | `pm` if the PM's merge passed every check (≤ 8 valid tasks, every original criterion kept word for word and in order), `merge` if the deterministic fallback merged the original tasks |

## TokenUsage

| Field | Type |
|-------|------|
| `input_tokens` | `int` |
| `output_tokens` | `int` |

## TeamState (the shared LangGraph state)

| Field | Type | Initial | Written by |
|-------|------|---------|------------|
| `run_id` | `str` | `YYYYMMDD-HHMMSS-<6 hex>` | `run_team` |
| `workspace` | `str` | absolute `workspace/<run_id>/` | `run_team` |
| `requirement` | `str` | the user's text (non-empty) | `run_team` |
| `spec` | `TechnicalSpec \| None` | `None` | `pm`, `consolidate` |
| `tasks` | `list[Task]` | `[]` | `pm`, `consolidate` |
| `handoff` | `Task \| None` | `None` | `dispatch` (sets), `coder` (clears) |
| `task_progress` | `dict[str, TaskProgress]` | `{}` | `pm`, `consolidate` (all `pending`), `dispatch`, `coder`, `finish` |
| `code_output` | `list[CodeOutput]` | `[]` | `coder` (appends) |
| `consolidation` | `Consolidation \| None` | `None` | `consolidate` |
| `status` | `RunStatus` | `"running"` | `pm`, `finish` |
| `error` | `str \| None` | `None` | `pm`, `finish` |
| `usage` | `TokenUsage` | zeros | `pm`, `consolidate`, `coder` (summed) |
| `duration_seconds` | `float` | `0.0` | `run_team`, once, after the graph finishes (the run's wall-clock time, FR-025) |

Nodes return only the fields they change. LangGraph replaces each returned field (no
reducers), so nodes return the full new list or dict, as Increment 1's nodes do.

### Invariants

| # | Invariant | Checked by |
|---|-----------|------------|
| I-1 | After `pm` or `consolidate` with status `running`, `tasks == spec["tasks"]` and `1 ≤ len(tasks) ≤ 8` before any handoff | `test_team_handoff` |
| I-2 | `task_progress` has exactly one entry per task, keyed by `task_id` | `test_team_handoff` |
| I-3 | At most one task is `in_progress`, and it is the one in `handoff` | `test_team_handoff` |
| I-4 | Tasks reach `completed` / `failed` in list order | `test_team_handoff` |
| I-5 | `json.loads(json.dumps(state)) == state` for every state the graph produces | `test_team_handoff` |
| I-6 | After `consolidate`, every original task's acceptance criteria appear in the final tasks, word for word (ignoring case and spacing) and in their original order | `test_pm_agent`, `test_team_handoff` |

## Task status transitions

```text
pending ──dispatch──→ in_progress ──coder: CoderResult.status == "completed"──→ completed
                                  └─coder: step_limit / refused / error──────→ failed
pending ──finish (after a failure)──→ skipped
```

## Run status transitions (`TeamState.status`)

```text
running ──pm: rejection_reason non-empty────────────────→ rejected
running ──pm: API error / refusal / invalid spec────────→ error
running ──finish: every task completed──────────────────→ completed
running ──finish: a task failed (rest marked skipped)───→ failed
```

`rejected` and `error` runs never set `handoff`, so the Coder never runs for them.
