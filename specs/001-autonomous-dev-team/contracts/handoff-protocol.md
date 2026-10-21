# Contract: PM → Coder Handoff Protocol

How the PM side tells the Coder that a task is ready, and how the Coder reports back. Both
sides are nodes in the same LangGraph graph and talk only through `TeamState`
([../data-model.md](../data-model.md#teamstate-the-shared-langgraph-state)). Design
rationale: [../research.md](../research.md) R14, R16, R18.

## Message

The message is the state key **`handoff`**, typed `Task | None`. Its JSON form is defined
by [task-handoff.schema.json](task-handoff.schema.json):

```json
{
  "task_id": "T2",
  "title": "Convert between Celsius and Fahrenheit",
  "description": "Add celsius_to_fahrenheit(c) and fahrenheit_to_celsius(f) to converter.py, each returning a float.",
  "acceptance_criteria": [
    "celsius_to_fahrenheit(100) returns 212.0",
    "fahrenheit_to_celsius(32) returns 0.0"
  ],
  "priority": "high"
}
```

| Field | Required by user | Meaning |
|-------|:---:|---------|
| `task_id` | ✔ | `T1`…`T8`, unique in the run, assigned by the system |
| `description` | ✔ | What to build |
| `acceptance_criteria` | ✔ | ≥ 1 checkable criterion |
| `priority` | ✔ | `high` \| `medium` \| `low` |
| `title` | | Short name for progress lines and the record |

Serialization: `json.dumps(state["handoff"])`. A `TypedDict` is a plain `dict`, so no
conversion is needed. `json.loads` gives back an equal value.

## Lifecycle

| Step | Actor (node) | Reads | Writes |
|------|--------------|-------|--------|
| 1. Plan | PM (`pm`, then `consolidate` if > 8 tasks) | `requirement` | `spec`, `tasks` (1–8), `task_progress` (all `pending`), `handoff = None` |
| 2. Publish | PM (`dispatch`) | `tasks`, `task_progress` | `handoff =` first task in `tasks` order whose status is `pending`; that task's status → `in_progress` |
| 3. Consume | Coder (`coder`) | `handoff`, `spec`, `task_progress` | Runs the task in the shared workspace |
| 4. Report | Coder (`coder`) | its `CoderResult` | `task_progress[task_id]` → `completed` or `failed` with `coder_status`, `summary`, `files_written`, `steps_used`, `duration_seconds`, `error`; appends `code_output`; adds to `usage`; `handoff = None` |
| 5. Route | router | `task_progress`, `status` | Back to step 2 if a task is still `pending` and none failed, else to `finish` |
| 6. Close | `finish` | `task_progress` | Remaining `pending` → `skipped`; `status` → `completed` or `failed` |

## Rules

| # | Rule |
|---|------|
| H1 | **Ready signal**: the Coder starts a task only when `handoff` is not `None`. It never picks a task from `tasks` itself. |
| H2 | **Single writer**: only `dispatch` sets `handoff`, and only `coder` clears it. |
| H3 | **One in flight**: at most one task is `in_progress`, and it is the task in `handoff`. |
| H4 | **Order**: tasks are handed off strictly in `tasks` order. `priority` never reorders them. |
| H5 | **Acknowledgement**: when `coder` returns, `handoff` is `None` and the task's status is final (`completed` or `failed`). |
| H6 | **Cap**: no handoff happens while `len(tasks) > 8`. Consolidation always comes first. |
| H7 | **Termination**: no transition returns a task to `pending`, so a run makes at most 8 handoffs. |
| H8 | **No handoff on rejection or error**: if the PM rejects the requirement or fails, `handoff` stays `None` and the Coder never runs. |

## Coder task text

`coder_node` turns the handoff into the Coder's task (the Coder's own API is unchanged,
see [coder-agent-api.md](coder-agent-api.md)):

```text
You are building: <spec.name>. <spec.description>

Task T2 of 4 (priority: high): Convert between Celsius and Fahrenheit
<description>

Acceptance criteria:
- celsius_to_fahrenheit(100) returns 212.0
- fahrenheit_to_celsius(32) returns 0.0

Files already in the workspace from earlier tasks: converter.py, main.py.
Read a file before you change it, and keep earlier tasks' behavior working.
```

The files line is left out when the workspace is empty.
