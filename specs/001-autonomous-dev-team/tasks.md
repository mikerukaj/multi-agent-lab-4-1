---

description: "Task list for Increment 2 (PM Agent and PM → Coder handoff) of the Autonomous Multi-Agent Development Team"
---

# Tasks: Autonomous Multi-Agent Development Team — Increment 2: PM Agent and PM → Coder Handoff

**Input**: Design documents from `/specs/001-autonomous-dev-team/`

**Prerequisites**: plan.md, spec.md, research.md (R12–R19), data-model.md (Increment 2
section), contracts/ (handoff-protocol.md, task-handoff.schema.json, pm-agent-api.md,
team-api.md, coder-agent-api.md), quickstart.md (Scenarios 6–10)

**Tests**: Included. The user asked for tests of the PM → Coder handoff, and the
constitution's Development Workflow requires tests for every new behavior. Tests use stdlib
`unittest`, run offline (no API key, no Docker), and are written before the code they
cover. `specs/` is git-ignored, so **no test may read files under `specs/`**: contract
values are hard-coded in the tests.

**Organization**: Tasks are grouped by user story. This increment delivers the PM → Coder
share of US1 and the run-record share of US3. US2 (the Coder ⇄ QA loop) needs the QA
Agent and has no tasks here. Increment 1's completed task list is archived in
[tasks-increment-1.md](tasks-increment-1.md).

## Increment Scope

| Covered in this increment | Deferred |
|---------------------------|----------|
| FR-001, FR-002, FR-003 (PM → Coder part), FR-004, FR-005, FR-006, FR-006a, FR-006b, FR-007, FR-009, FR-009a, FR-025 (run status, per-task status, assumptions, failed task, token usage, elapsed time) | FR-008a, FR-011/FR-011a, FR-012–FR-020 (QA), FR-022, FR-024 (files on disk), FR-026 |

Rules every task must respect (constitution v1.1.1 and plan.md):

- No new dependencies. Do not import `pydantic`. From `langgraph`, import only `StateGraph`,
  `START`, `END`; use no reducers, checkpointers, prebuilt agents or subgraph helpers.
- Every shared-state value has a declared `TypedDict` / `Literal` type from
  `orchestration/state.py` (no ad-hoc dicts). The state must survive `json.dumps`.
- Functions ≤ ~40 lines; type hints and docstrings on public functions; no bare `except`;
  nodes return only the fields they change and never modify their input state.
- Model: `claude-sonnet-5-5` through `client.beta.messages.create`, as the Coder does.
  Request settings both agents share (`EFFORT_LEVELS`, `FALLBACK_BETA`, `MAX_TOKENS`,
  `refusal_message`) live in `agents/llm.py` (T004), not in either agent's module.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1, US3)
- Paths are relative to the repository root

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Make `orchestration/` an importable package

- [X] T001 Create an empty package marker `orchestration/__init__.py` (leave `orchestration/gitkeep` in place), then confirm `python -c "import orchestration"` succeeds from the repository root

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The shared state schema, the shared Claude settings module, the Coder's
shared-workspace parameter, and the test fakes that every story's tests use

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T002 [P] Write `orchestration/state.py` with a module docstring saying it defines the shared LangGraph state that the PM and Coder agents read and write. Define, exactly as in data-model.md "Increment 2: Shared Team State": the `Literal` aliases `Priority = Literal["high", "medium", "low"]`, `TaskStatus = Literal["pending", "in_progress", "completed", "failed", "skipped"]`, `RunStatus = Literal["running", "completed", "failed", "rejected", "error"]`, `ConsolidationMethod = Literal["pm", "merge"]`; and the `TypedDict`s `Task` (`task_id: str`, `title: str`, `description: str`, `acceptance_criteria: list[str]`, `priority: Priority`), `TechnicalSpec` (`name`, `description`, `acceptance_criteria: list[str]`, `assumptions: list[str]`, `tasks: list[Task]`), `TaskProgress` (`task_id`, `status: TaskStatus`, `coder_status: str | None`, `summary: str`, `files_written: list[str]`, `steps_used: int`, `duration_seconds: float`, `error: str | None`), `CodeOutput` (`task_id`, `path`, `content`), `Consolidation` (`proposed_count: int`, `final_count: int`, `method: ConsolidationMethod`), `TokenUsage` (`input_tokens: int`, `output_tokens: int`) and `TeamState` (`run_id`, `workspace`, `requirement`, `spec: TechnicalSpec | None`, `tasks: list[Task]`, `handoff: Task | None`, `task_progress: dict[str, TaskProgress]`, `code_output: list[CodeOutput]`, `consolidation: Consolidation | None`, `status: RunStatus`, `error: str | None`, `usage: TokenUsage`, `duration_seconds: float`). Give each `TypedDict` a one-line docstring, and note on `Task` that it is also the handoff message (contracts/handoff-protocol.md). Types only: no functions or constants
- [X] T003 [P] Write `tests/test_team_state.py`: (a) `Task.__required_keys__ == {"task_id", "title", "description", "acceptance_criteria", "priority"}` (hard-coded copy of contracts/task-handoff.schema.json `required`); (b) `typing.get_args(Priority) == ("high", "medium", "low")`; (c) `TeamState.__required_keys__` equals the 13 field names of data-model.md, and `TaskProgress.__required_keys__` equals its 8; (d) a fully populated sample `TeamState` (one task, one progress entry, one code output, a consolidation, usage) satisfies `json.loads(json.dumps(state)) == state`
- [X] T004 Create `agents/llm.py` (module docstring: Claude request settings and helpers shared by the PM and Coder agents). Move `EFFORT_LEVELS`, `MAX_TOKENS` and `FALLBACK_BETA` out of `agents/coder_agent.py`, and move `_refusal_message` there as the public `refusal_message(stop_details) -> str` (with a docstring). In `agents/coder_agent.py`, import the four names from `agents.llm` and replace the two uses of `_refusal_message`. Keep `MAX_STEPS_CAP` and `TERMINAL_STATUSES` in `coder_agent.py` (Coder-only). Change no behavior, then run `python -m unittest tests.test_coder_agent_loop -v`; if a test referenced a moved name through `coder_agent`, point it at `agents.llm`
- [X] T005 Add a `SharedWorkspaceTests` class to `tests/test_coder_agent_loop.py`, using the existing `AgentTestCase`, `FakeClient`, `response`, `tool_use` and `text_block` helpers: create `shared = Path(self._tmp.name) / "shared"` (do not create the folder). Run one agent twice, first with a script that writes `a.py` then ends, then with one that writes `b.py` then ends, both with `workspace=shared`. Assert both `result.workspace == str(shared.resolve())`, both files exist in `shared`, the first result's `files_written == ["a.py"]` and the second's `== ["b.py"]`, and `run("...")` without `workspace` still creates a new folder under `self.config.workspace_root`. The test must fail before T006
- [X] T006 Change `CoderAgent.run` in `agents/coder_agent.py` to `run(self, task: str, workspace: Path | None = None) -> CoderResult`. When `workspace` is given, use `workspace.resolve()`, created with `mkdir(parents=True, exist_ok=True)`, instead of `self.config.workspace_root / run_id` (the run still gets a new `run_id` for memory records). Update the docstring to describe `workspace` (contracts/coder-agent-api.md). Change nothing else; T005 and the existing tests must pass
- [X] T007 [P] Write `tests/team_fakes.py`, shared offline helpers with no test cases. (1) `pm_response(payload: dict | None = None, stop_reason="end_turn", stop_details=None)` returns a `SimpleNamespace` response with `content=[SimpleNamespace(type="text", text=json.dumps(payload))]` (empty content when `payload` is None), `stop_reason`, `stop_details` and `usage=SimpleNamespace(input_tokens=100, output_tokens=50)`. (2) `spec_payload(task_count: int, rejection_reason: str = "") -> dict` returns a model-facing spec as in contracts/pm-agent-api.md `SPEC_SCHEMA`: name "Temperature converter", a description, 2 acceptance criteria, 1 assumption, and `task_count` tasks titled "Task 1"… with priorities cycling high/medium/low and 1–2 criteria each, every criterion unique (`"Task {i} criterion {j}"`) (no `task_id`; empty `tasks` when `rejection_reason` is set). (3) `tasks_payload(task_count)` returns `{"tasks": [...]}` in the same item shape with its own new criteria (used where the criteria do not matter). (3a) `regroup_payload(tasks: list[dict], group_count: int) -> dict` returns `{"tasks": [...]}` that merges `tasks` into `group_count` contiguous groups, copying every criterion word for word and in order. This is a well-behaved PM consolidation, and tests mutate it to drop, reword or reorder a criterion. (4) `FakePMClient(script: list)` exposes `beta.messages.create(**kwargs)`, which records a copy of every request in `self.requests` and returns or raises the next scripted item (a response or an `Exception`). (5) `FakeCoder(fail_on: set[str] = frozenset(), failed_status="step_limit")` has `run(task: str, workspace: Path) -> CoderResult`. It reads the task ID from the task text with `re.search(r"Task (T\d+) of", task)`, records `(task_id, task, workspace)` in `self.calls`, writes `workspace / f"{task_id.lower()}.py"` containing `print('{task_id}')\n`, and returns a `CoderResult` with `code` equal to that content, `explanation=f"Built {task_id}"`, `plan=[f"Write {task_id}"]`, `execution_result=None`, `status` = `failed_status` if `task_id in fail_on` else `"completed"`, `files_written=[the file name]`, `steps_used=2`, `duration_seconds=0.5`, `usage={"input_tokens": 10, "output_tokens": 5}`, `workspace=str(workspace)`, and `error="Step limit reached"` when failing, else `None`

**Checkpoint**: Shared schema importable, Coder accepts a shared workspace, fakes ready. `python -m unittest tests.test_team_state tests.test_coder_agent_loop -v` passes

---

## Phase 3: User Story 1 - Requirement In, Tested Code Out (Priority: P1) 🎯 MVP

**Goal**: The PM turns a plain-English requirement into a spec and at most 8 ordered tasks
in shared state. The Coder then receives them one at a time through the `handoff` key and
builds them in order in one workspace, all inside one LangGraph graph. (Testing by QA
comes in a later increment.)

**Independent Test**: `python -m unittest tests.test_pm_agent tests.test_team_handoff -v`
passes offline. Live, `python -m orchestration.team "<small requirement>"` exits 0 with
`status: "completed"`, a spec, every task `completed`, and the code in one
`workspace/<run_id>/` (quickstart Scenarios 6–7).

### Tests for User Story 1 ⚠️

> **NOTE: Write these tests FIRST and make sure they FAIL before implementation**

- [X] T008 [P] [US1] Write `tests/test_pm_agent.py` using `tests/team_fakes.py`. Cover:
  - `write_spec` with `FakePMClient([pm_response(spec_payload(3))])` returns a `PMResult` whose spec has tasks with IDs `T1`, `T2`, `T3` in order and each original field kept, `rejection_reason` and `error` `None`, and usage `{100, 50}`;
  - the request sent has no `tools`, `output_config["format"]["type"] == "json_schema"`, `output_config["effort"] == "high"`, `thinking == {"type": "adaptive"}`, `model == "claude-sonnet-5-5"`, and the fallback beta and `fallbacks="default"` by default but not when `PMConfig(use_fallbacks=False)`;
  - a rejection payload gives `spec is None` with `rejection_reason` set;
  - each of the following gives `spec is None` and a non-empty `error`, with no exception raised: an `anthropic.APIConnectionError` (build it as `test_coder_agent_loop.py` does), a refusal (`stop_reason="refusal"`, `stop_details` with `category` and `explanation`), non-JSON text, a non-rejected spec with an empty `tasks` list, and a task with an empty `acceptance_criteria`;
  - `write_spec("  ")` raises `ValueError`;
  - `consolidate(spec, requirement)` sends one request whose user message contains the requirement and the current tasks as JSON and whose format schema has only a `tasks` property. Given `regroup_payload(original, 6)` it returns 6 tasks renumbered `T1…T6` with `error is None`; a copy whose criteria differ only in case and spacing also succeeds;
  - `consolidate` returns a `PMResult` with `spec is None` and a non-empty `error`, never raising, when the reply has 9 tasks, an empty task list, a task with empty `acceptance_criteria` or an invalid priority (U1); when one original criterion is dropped or reworded; or when two criteria from different tasks are swapped in order (U2);
  - `criteria_preserved(original, merged)` is true when the original criteria, flattened in task order and compared after `" ".join(c.split()).casefold()`, appear in the merged criteria in the same order (extra merged criteria allowed), and false when one is missing or out of order;
  - `validate_tasks` returns `None` for valid tasks and a message naming the problem for each failure listed above;
  - `merge_tasks` on 11 numbered tasks with limit 8 returns 8 tasks numbered `T1…T8` in order. Every original acceptance criterion appears exactly once and in order, a group containing a `high` task gets `high`, titles of merged groups are joined with `" + "`, and lists of ≤ 8 tasks come back unchanged apart from renumbering;
  - `SPEC_SCHEMA`'s task item property names plus `"task_id"` equal `Task.__required_keys__`, and its top-level property names minus `"rejection_reason"` equal `TechnicalSpec.__required_keys__` (drift guard, research R12);
  - `PMConfig` rejects an unknown effort and an empty model, and `PMConfig.from_env()` reads `PM_MODEL`, `PM_EFFORT`, `PM_USE_FALLBACKS` (use `mock.patch.dict(os.environ, ...)`).
- [X] T009 [P] [US1] Write `tests/test_team_handoff.py` using `tests/team_fakes.py` and a temporary directory as the workspace. Cover:
  - **the user's required handoff test**: `state = initial_team_state("Build a temperature converter CLI", "test-run", workspace)`, `update = pm_node(state, PMAgent(client=FakePMClient([pm_response(spec_payload(3))])))`, `after = {**state, **update}`. Then assert: `after["tasks"]` is a non-empty list; every task's keys equal `{"task_id", "title", "description", "acceptance_criteria", "priority"}`; `task_id`, `title` and `description` are non-empty `str`; `acceptance_criteria` is a non-empty `list[str]`; `priority` is in `("high", "medium", "low")`; IDs are `T1…T3`; `after["tasks"] == after["spec"]["tasks"]`; `after["spec"]` has non-empty `name`, `description`, `acceptance_criteria`; `after["task_progress"]` has exactly one `pending` entry per task; `after["handoff"] is None`; `after["status"] == "running"`; and `pm_node` did not modify `state`;
  - `route_after_pm` returns `"dispatch"` for 3 tasks, `"consolidate"` for 9 tasks, and `"finish"` for `rejected` and `error`;
  - `dispatch_node` sets `handoff` to the first `pending` task in list order and marks only it `in_progress`; `json.loads(json.dumps(handoff)) == handoff` and its keys equal the contract keys above;
  - `coder_node(state_with_handoff, FakeCoder())` returns `handoff=None`, sets that task's progress to `completed` with `coder_status`, `summary`, `files_written`, `steps_used` from the result, appends one `CodeOutput` whose `content` was read from the workspace file, and adds the Coder's usage to `usage`, with `duration_seconds == 0.5` in the progress entry. The fake's recorded task text contains `"Task T1 of 3"`, the priority, every acceptance criterion and the spec name, and its recorded workspace equals `Path(state["workspace"])`;
  - `render_coder_task` lists the files already in the workspace only when there are some;
  - **full graph**: `build_team_graph(pm, FakeCoder()).invoke(initial_state, {"recursion_limit": 21})` with 3 tasks ends `completed`. `FakeCoder.calls` task IDs are `["T1", "T2", "T3"]` and all share one workspace, every progress entry is `completed`, `code_output` has three entries, `handoff is None`, and `json.loads(json.dumps(final)) == final`;
  - **order and acknowledgement**: iterating `graph.stream(..., stream_mode="values")`, every state where `handoff` is set has exactly one `in_progress` task and it is the handoff (rule H3), and tasks reach `completed` in list order;
  - **cap via the PM**: with `FakePMClient([pm_response(spec_payload(11)), pm_response(regroup_payload(spec_payload(11)["tasks"], 6))])`, the final state has 6 tasks, `consolidation == {"proposed_count": 11, "final_count": 6, "method": "pm"}`, and the Coder's first call happened after consolidation (no call ever sees "of 11");
  - **cap via the fallback**: one case for each consolidation failure: a reply with 10 tasks, an API error, a reply with an empty task list (U1), and a reply that drops one criterion (U2). Each ends with 8 tasks merged from the **original** 11 and `method == "merge"`, and the Coder still runs;
  - **criteria kept (FR-006a)**: in every cap case above, the original 11 tasks' criteria, flattened in order, appear in order in the final tasks' flattened criteria;
  - **stop on failure (FR-009a)**: with `FakeCoder(fail_on={"T2"})` and 4 tasks, the run is `failed`, T1 is `completed`, T2 is `failed` with `coder_status == "step_limit"`, T3 and T4 are `skipped`, the Coder was called only for T1 and T2, and `t1.py` and `t2.py` remain in the workspace;
  - **rejection and PM error**: a rejection payload gives `status == "rejected"`, `error` set to the reason, `tasks == []`, and `FakeCoder.calls == []`; an API error gives `status == "error"` and no Coder calls;
  - `initial_team_state` and `run_team` reject an empty requirement with `ValueError`.

### Implementation for User Story 1

- [X] T010 [US1] Write the data part of `agents/pm_agent.py`, following contracts/pm-agent-api.md: a module docstring; `MAX_TASKS = 8`; `PRIORITIES = ("high", "medium", "low")`; `TASK_ITEM_SCHEMA`, `SPEC_SCHEMA` (top-level properties `name`, `description`, `acceptance_criteria`, `assumptions`, `tasks`, `rejection_reason`, all required, `additionalProperties: false`, tasks using `TASK_ITEM_SCHEMA`) and `CONSOLIDATION_SCHEMA` (`{"tasks": [TASK_ITEM_SCHEMA]}`), written out as dicts (the `$ref`/`$defs` form in the contract is equivalent); a `PMConfig` dataclass (`model="claude-sonnet-5-5"`, `effort="high"`, `use_fallbacks=True`) with `__post_init__` validation and `from_env()` reading `PM_MODEL`, `PM_EFFORT`, `PM_USE_FALLBACKS` the same way `CoderConfig.from_env` does (import `EFFORT_LEVELS`, `FALLBACK_BETA` and `MAX_TOKENS` from `agents.llm`; `agents/pm_agent.py` must not import `agents.coder_agent`); and a `PMResult` dataclass (`spec: TechnicalSpec | None`, `rejection_reason: str | None`, `error: str | None`, `usage: TokenUsage`). Import `Task`, `TechnicalSpec`, `TokenUsage` from `orchestration.state`
- [X] T011 [US1] Add pure helpers to `agents/pm_agent.py`. `number_tasks(items: list[dict]) -> list[Task]` builds `Task`s with `task_id=f"T{i}"` from position 1…n and copies the four model fields. `merge_tasks(tasks: list[Task], limit: int = MAX_TASKS) -> list[Task]` works like this: if `len(tasks) <= limit`, return `number_tasks(tasks)`. Otherwise split the list into `limit` contiguous groups whose sizes differ by at most one, with the larger groups first (`divmod(len(tasks), limit)`). Merge each group: `title` joins the titles with `" + "`; `description` is `"\n".join(f"{n}. {t['title']}: {t['description']}")` numbered from 1 (a single-item group stays unchanged); `acceptance_criteria` concatenates the criteria in order; `priority` is the group's highest (high > medium > low). Then call `number_tasks`. `validate_tasks(tasks: list[dict]) -> str | None` returns a message naming the first problem (no tasks; a task with an empty title, description or `acceptance_criteria`; a priority outside `PRIORITIES`), and `None` when all tasks are valid. `_validate_spec(data: dict) -> str | None` returns an error message when a non-rejected spec has an empty `name`, `description` or `acceptance_criteria`, and otherwise returns `validate_tasks(data["tasks"])`. `criteria_preserved(original: list[Task], merged: list[dict]) -> bool` flattens each list's acceptance criteria in task order, normalizes each with `" ".join(c.split()).casefold()`, and returns whether the original sequence is a subsequence of the merged one (every original criterion kept, in order; extra criteria allowed)
- [X] T012 [US1] Add the model-facing part of `agents/pm_agent.py`. `PM_SYSTEM_PROMPT` (frozen) says, in plain sentences: you are the PM in an autonomous team; turn the requirement into a technical specification for the Coder Agent; write tasks the Coder can build, in dependency order and then priority order; give each task a short imperative title, a description with inputs and outputs, at least one acceptance criterion checkable by running the program, and a priority; aim for at most 8 tasks and never write tasks for writing tests (tests belong to the QA Agent); record every assumption instead of asking questions; the Coder can use only the Python standard library and has no network, so record an assumption when the requirement needs more; set `rejection_reason` (and leave the lists empty) only when the requirement is empty, nonsense or not software, never because it is large. `CONSOLIDATE_PROMPT` asks the PM to merge the given tasks into at most 8 by combining neighbouring tasks, keeping the order and the requirement's full scope, and copying every acceptance criterion word for word, in its original order (the system checks this). `PMAgent.__init__(config=None, client=None)` uses `PMConfig.from_env()` and `anthropic.Anthropic(max_retries=3)` by default. `_request(user_text, schema) -> dict` builds the `beta.messages.create` kwargs: model, `max_tokens=MAX_TOKENS`, system, one user message, `thinking={"type": "adaptive"}`, `output_config={"effort": ..., "format": {"type": "json_schema", "schema": schema}}`, and the fallback beta and `fallbacks="default"` when enabled; no tools. `_call(user_text, schema) -> tuple[dict | None, str | None, TokenUsage]` catches `anthropic.APIError`, handles `stop_reason == "refusal"` (message from `agents.llm.refusal_message`), and parses the joined text blocks with `json.loads`, returning an error on `json.JSONDecodeError`. `write_spec(requirement)` raises `ValueError` on blank input, renders `"Requirement:\n<text>"`, and maps the result to `PMResult`: a rejection when `rejection_reason` is non-empty; otherwise `_validate_spec`, then a `TechnicalSpec` with `number_tasks(tasks)`. `consolidate(spec, requirement)` sends the requirement plus `json.dumps(spec["tasks"], indent=2)` with `CONSOLIDATION_SCHEMA`. It accepts the reply only when `validate_tasks` passes, there are at most `MAX_TASKS` tasks, and `criteria_preserved(spec["tasks"], reply_tasks)` holds; it then returns a `PMResult` whose spec is `spec` with `number_tasks(reply_tasks)`. Otherwise it returns `spec=None` and an `error` saying which check failed (call failures included), so a successful result always meets FR-006a. T008 must pass
- [X] T013 [US1] Start `orchestration/team.py` with a module docstring and a usage line `python -m orchestration.team "<requirement>"`. Add:
  - `initial_team_state(requirement, run_id, workspace) -> TeamState`: raises `ValueError` on blank input; `status="running"`, empty lists and dicts, `None` for spec, handoff, consolidation and error, zero usage, `duration_seconds=0.0`, `workspace=str(workspace)`;
  - `add_usage(total: TokenUsage, extra: TokenUsage) -> TokenUsage`;
  - `pending_progress(tasks) -> dict[str, TaskProgress]`: all `pending`, empty summary and files, `steps_used=0`, `duration_seconds=0.0`, `coder_status` and `error` `None`;
  - `pm_node(state, pm)`: calls `pm.write_spec(state["requirement"])`. On `error`, returns status `error`, the error and the usage. On rejection, returns status `rejected`, `error=rejection_reason` and the usage. Otherwise returns `spec`, `tasks=spec["tasks"]`, `task_progress=pending_progress(...)` and the usage;
  - `consolidate_node(state, pm)`: calls `pm.consolidate(state["spec"], state["requirement"])`. With no `error` it uses the PM's tasks and method `pm`. With an `error` it uses `merge_tasks(state["tasks"])` (the **original** tasks) and method `merge`. `PMAgent.consolidate` has already checked the count, validity and criteria, so the node checks nothing further. It returns the updated spec, `tasks`, fresh `task_progress`, `consolidation={proposed_count, final_count, method}` and the summed usage;
  - `route_after_pm(state) -> str`: `"finish"` unless status is `running`; `"consolidate"` if `len(tasks) > MAX_TASKS`; else `"dispatch"`.
- [X] T014 [US1] Add the handoff and Coder side to `orchestration/team.py`, following contracts/handoff-protocol.md:
  - `dispatch_node(state)`: finds the first task in `tasks` order whose progress is `pending`, then returns `handoff=dict(task)` and a copy of `task_progress` with that entry `in_progress`;
  - `render_coder_task(handoff, spec, position, total, existing_files) -> str`: the exact layout in the contract's "Coder task text" section, leaving out the files line when `existing_files` is empty;
  - `_workspace_files(workspace: Path) -> list[str]`: sorted workspace-relative paths of regular files, skipping names that start with `.`;
  - `coder_node(state, coder)`: renders the task (position = index in `tasks` + 1), calls `coder.run(text, Path(state["workspace"]))`, and builds the `TaskProgress` (`completed` only if `result.status == "completed"`, else `failed`; `coder_status=result.status`, `summary=result.explanation`, `files_written`, `steps_used`, `duration_seconds`, `error`). It appends one `CodeOutput` per written file, read from disk with `tools.file_tools.read_file` (skipping files it reports missing), sums `usage` with `result.usage`, and returns these with `handoff=None`;
  - `route_after_coder(state) -> str`: `"finish"` if any progress is `failed` or no task is `pending`, else `"dispatch"`;
  - `finish_node(state)`: keeps `rejected` and `error` unchanged; otherwise marks every `pending` task `skipped` and sets `status="failed"` with `error=f"Task {id} failed ({coder_status}): {error or summary}"` for the first failed task, or `status="completed"` when every task completed. Always returns `handoff=None`.
- [X] T015 [US1] Add graph assembly to `orchestration/team.py`. `build_team_graph(pm, coder)` builds `StateGraph(TeamState)` with nodes `pm`, `consolidate`, `dispatch`, `coder`, `finish` bound with `functools.partial`, edges `START→pm`, `consolidate→dispatch`, `dispatch→coder`, `finish→END`, conditional edges `pm` via `route_after_pm` to `["finish", "consolidate", "dispatch"]` and `coder` via `route_after_coder` to `["dispatch", "finish"]`, and returns `graph.compile()`. `RECURSION_LIMIT = 2 * MAX_TASKS + 5`. `run_team(requirement, pm=None, coder=None, workspace_root=Path("workspace"), on_update=None) -> TeamState` checks the requirement, builds `PMAgent()` and `CoderAgent()` when not given, creates `workspace_root / new_run_id()` (reuse `new_run_id` from `agents.coder_agent`), and iterates `graph.stream(state, {"recursion_limit": RECURSION_LIMIT}, stream_mode=["updates", "values"])`, calling `on_update(node, update)` for each `"updates"` chunk and keeping the last `"values"` chunk as the final state. It times the whole call with `time.monotonic()` and returns `{**final, "duration_seconds": round(elapsed, 3)}`. This is the only field set outside a node, because the run's total time is known only after the graph finishes. T009 must pass
- [X] T016 [US1] Add the CLI to `orchestration/team.py`. `main(argv=None) -> int` calls `load_dotenv()` and uses argparse (`prog="python -m orchestration.team"`) with a positional `requirement` and the optional `--max-steps` / `--timeout` flags, applied to `CoderConfig.from_env()` with `dataclasses.replace` as in `agents/coder_agent.py`'s `main`. It builds `PMAgent(PMConfig.from_env())` and `CoderAgent(config)`, then calls `run_team(requirement, pm, coder, config.workspace_root)` and prints `json.dumps(final, indent=2)` to stdout. It returns 0 if the status is `completed`, 1 otherwise, and 2 after printing `error: ...` to stderr on `ValueError` or `SandboxUnavailableError`. Add `if __name__ == "__main__": sys.exit(main())`
- [X] T017 [US1] Run `python -m unittest tests.test_team_state tests.test_pm_agent tests.test_team_handoff tests.test_coder_agent_loop -v`, fix any failures in the code (not by weakening tests), and confirm `grep -rn "pydantic" agents orchestration` and `grep -rn "from langgraph" agents orchestration` show no import beyond `from langgraph.graph import END, START, StateGraph`

**Checkpoint**: The PM → Coder team works end to end offline. US1's share of this increment
is complete and can be demonstrated with the CLI

---

## Phase 4: User Story 2 - Automatic Review and Fix Loop (Priority: P2)

No tasks in this increment. US2 needs the QA & Debugger Agent (FR-012–FR-020), which is
planned for the next increment. The team graph's `finish` node is where the QA hand-off
will attach.

---

## Phase 5: User Story 3 - Run Record and Outcome Report (Priority: P3)

**Goal**: From one run's output, the user can see the spec, the PM's assumptions, every
task with its priority and status, which task ended a failed run and why, the code each
task wrote, and the token usage, and can follow progress live on stderr.

**Independent Test**: `python -m unittest tests.test_team_record -v` passes offline. Live,
quickstart Scenarios 7–8 show one progress line per node update and a final JSON record
with the fields above.

### Tests for User Story 3 ⚠️

- [X] T018 [P] [US3] Write `tests/test_team_record.py` using `tests/team_fakes.py`. Cover:
  - `run_team` with a fake PM (3 tasks) and `FakeCoder()` calls `on_update` with node names in the order `pm, dispatch, coder, dispatch, coder, dispatch, coder, finish`, and every `coder` update holds that task's final `TaskProgress`, so progress is reported after each task;
  - the final state keeps `spec["assumptions"]`, every task's `priority`, and `usage` equal to the PM's usage plus 3 × the Coder's (`{130, 65}` with the fakes);
  - with `fail_on={"T2"}`, `error` starts with `"Task T2 failed (step_limit)"`;
  - the final `duration_seconds` is a `float` ≥ 0 and greater than 0 when `run_team` is given a `FakeCoder` that sleeps 0.01 s; each completed task's progress has `duration_seconds == 0.5`;
  - `format_update(node, update, state_tasks)` returns exactly the progress lines in contracts/team-api.md: `[pm] "Temperature converter": 3 tasks`, `[pm] consolidated 11 → 6 tasks (pm)`, `[handoff] T2 (medium) Task 2`, `[coder] T2 completed in 2 steps (0.5 s)` (or `failed (step_limit)`), `[finish] completed` (or `failed`, `rejected`, `error`); and `None` for updates with nothing to report;
  - `main([...])` with `run_team`, `PMAgent` and `CoderAgent` patched in `orchestration.team`, `contextlib.redirect_stdout` and `redirect_stderr`: exit 0 and parseable JSON on stdout for a completed state, 1 for `failed`, `rejected` and `error`, and 2 with an `error:` line for an empty requirement and for `SandboxUnavailableError` from `CoderAgent`.

### Implementation for User Story 3

- [X] T019 [US3] In `agents/coder_agent.py`, rename `_print_progress` to `print_step_progress` (public, with a docstring) and update its one use in `main`, so the team CLI can reuse it. Run `python -m unittest tests.test_coder_agent_loop -v` to confirm nothing else referenced the old name
- [X] T020 [US3] Add `format_update(node: str, update: dict, tasks: list[Task]) -> str | None` to `orchestration/team.py`, producing the lines listed in T018 (the `[pm]` line uses `update["spec"]["name"]`, or reports `rejected: <error>` / `error: <error>`). Add `print_update(node, update)` writing non-`None` lines to stderr with `flush=True`. Wire both into `main`: pass `on_update` to `run_team` and `on_step=print_step_progress` to `CoderAgent`. Because `on_update` only gets the update, track the latest `tasks` in a small closure inside `main` (rewritten after `pm`/`consolidate` updates) so `[handoff]` lines can show priority and title. T018 must pass

**Checkpoint**: A run's printed record and progress lines meet US3's share of FR-025

> **As built (2026-10-17)**: `format_update` takes the most recently handed-off task
> (`current: Task | None`) instead of the task list, because the `dispatch` update already
> carries the handoff's priority and title and only the `[coder]` line needs to know which
> task ran. There is no `print_update`: `main` passes a closure (`_progress_printer`) that
> tracks `current` and prints, so no module-level state is needed (analysis M1).
> `finish_node` always returns `status`. `FakeCoder` gained `delay_seconds` (analysis L1),
> and `PRIORITIES` is derived from the `Priority` literal (analysis L2).

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, the full suite, a constitution review, and live validation

- [X] T021 [P] Update `README.md`:
  - "Status": Increment 2, the PM and Coder agents working as a team; QA and run limits still to come;
  - a new "How the agents work together" section with the Mermaid diagram and the state-key table copied from plan.md "System Diagram", plus three sentences on the handoff (the `handoff` key, one task at a time in order, the Coder reports back after each task), the 8-task cap with consolidation, and stopping at the first failed task;
  - "Usage": add `python -m orchestration.team "<requirement>"` with its flags, stderr progress lines, stdout JSON and exit codes (contracts/team-api.md), and a Python example using `run_team`; keep the Coder-only usage as a subsection;
  - "What a result contains": add a table of the `TeamState` fields, including per-task and run `duration_seconds`;
  - "Configuration": add `PM_MODEL` (`claude-sonnet-5-5`), `PM_EFFORT` (`high`) and `PM_USE_FALLBACKS` (`true`);
  - "Design decisions": add `TypedDict` shared state (no Pydantic), one graph with the Coder running inside the `coder` node, the handoff key, the cap with its checked consolidation and deterministic fallback, and one shared workspace per run;
  - "Project layout": add `agents/pm_agent.py`, `agents/llm.py`, `orchestration/state.py`, `orchestration/team.py`;
  - the spec-folder pointer: mention `contracts/handoff-protocol.md`.
  Keep the existing safety section unchanged
- [X] T022 [P] Review `agents/pm_agent.py`, `orchestration/state.py`, `orchestration/team.py` and the changed part of `agents/coder_agent.py` against the constitution. Check that functions are ≤ ~40 lines (split any longer one), public functions have type hints and docstrings, no bare `except` or swallowed errors, imports are grouped stdlib / third-party / local, there is no commented-out code, no abstraction or helper is unused, and `agents/pm_agent.py` does not import `agents.coder_agent` (`grep -n "coder_agent" agents/pm_agent.py` is empty)
- [X] T023 Run the full suite `python -m unittest discover -s tests -t . -v` and confirm every test passes; the live smoke test may be skipped without credentials and Docker tests without Docker
- [ ] T024 With the user's go-ahead (live runs spend API credit and need Docker), run quickstart.md Scenarios 7, 8 and 10 (Scenario 9 is best effort), and record the outcome, including any deviation, at the end of this file under a "Validation" note. Also record the two checks only a live run can make: whether the PM's task order is buildable (no task needs a later task, FR-006b) and how long it took from start to the `[pm]` line (plan goal: under 1 minute)
- [X] T025 Update the docs if implementation changed anything: signatures in `specs/001-autonomous-dev-team/contracts/pm-agent-api.md` and `contracts/team-api.md`, field tables in `data-model.md`, and progress-line formats in `quickstart.md`, so the docs match the code

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none
- **Foundational (Phase 2)**: depends on T001; blocks all user stories
- **US1 (Phase 3)**: depends on Phase 2
- **US2 (Phase 4)**: no tasks
- **US3 (Phase 5)**: depends on US1 (it records and prints what the US1 graph produces)
- **Polish (Phase 6)**: depends on US1 and US3

### Task Dependencies

- T002, T003, T007 can run in parallel after T001 (T003 imports T002's types, so write both before running it)
- T004 → T005 → T006 (all touch `agents/coder_agent.py`: move shared settings, then test first, then the `workspace` change)
- T008 and T009 can be written in parallel; both need T002 and T007
- T010 → T011 → T012 (same file); T010 needs T004; T012 makes T008 pass
- T013 → T014 → T015 → T016 (same file); T013 needs T012, and T014 needs T006; T015 makes T009 pass
- T017 after T016
- T018 can be written as soon as T007 exists; T019 → T020; T020 needs T016
- T021 and T022 can run in parallel after T020; then T023 → T024 → T025

### Within Each User Story

- Tests are written first and must fail before the implementation
- Schema and types → PM agent → graph nodes → graph assembly → CLI
- Finish each phase's checkpoint before moving on

---

## Parallel Example: Foundational Phase

```bash
Task: "Write orchestration/state.py with every TypedDict from data-model.md"   # T002
Task: "Write tests/test_team_state.py"                                       # T003
Task: "Write tests/team_fakes.py with FakePMClient, FakeCoder and payload helpers"  # T007
```

## Parallel Example: User Story 1

```bash
Task: "Write tests/test_pm_agent.py"        # T008
Task: "Write tests/test_team_handoff.py"    # T009
```

## Parallel Example: Polish

```bash
Task: "Update README.md with the system diagram and team usage"   # T021
Task: "Constitution review of the new modules"                    # T022
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 → Phase 2 (schema, shared workspace, fakes)
2. Phase 3: tests T008/T009 first, then T010–T016
3. **Stop and validate** with T017: the user's required handoff test, cap, order, and
   stop-on-failure all pass offline
4. Demo: `python -m orchestration.team "..."` prints the final JSON state

### Incremental Delivery

1. MVP above: the PM → Coder team over shared state
2. US3: progress lines and a complete run record (T018–T020)
3. Polish: README diagram, review, full suite, live validation (T021–T025)
4. Next increment: the QA Agent attaches after `finish` (US2)

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks
- `tests/team_fakes.py` does not match `unittest discover`'s `test*.py` pattern, so it is never
  run as a test module; the three new test modules import it
- Never edit `orchestration/state.py` without updating data-model.md and the
  `test_team_state.py` expectations together
- Commit after each phase checkpoint; `specs/` is git-ignored, so its documents are not
  committed with the code
