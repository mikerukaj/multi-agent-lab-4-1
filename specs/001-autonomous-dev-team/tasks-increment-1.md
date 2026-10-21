---

description: "Task list for Increment 1 (Coder Agent) of the Autonomous Multi-Agent Development Team"
---

# Tasks: Autonomous Multi-Agent Development Team — Increment 1: Coder Agent

**Input**: Design documents from `/specs/001-autonomous-dev-team/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included. The plan (research R9) and the constitution's Development Workflow require
automated tests for every new behavior. Tests use stdlib `unittest` and are written before the
code they cover.

**Organization**: Tasks are grouped by user story. This increment builds only the Coder Agent
(plan.md), so each story phase delivers the Coder Agent's share of that story.

## Increment Scope

| Covered in this increment | Deferred to later increments |
|---------------------------|------------------------------|
| FR-010 (constitution style), FR-021 (a throwaway, network-less sandbox container per code run; added in Phase 7), FR-021a (no secrets where generated code runs), FR-023 (3 retries), FR-024/FR-026 for the Coder's own run (files on disk, step trace), FR-025 partially (result, elapsed time, token usage; no dollar spend or PM assumptions yet) | PM Agent (FR-004–FR-008a), all-tasks-then-QA flow (FR-009), packages, install step and dependency file (FR-011, FR-011a), Coder ⇄ QA loop, test ownership and disputes (FR-012–FR-020), run limits on iterations/time/spend (FR-022) |

Known deviations, all recorded in plan.md's Spec Deviations table:

- The Coder's loop is capped at 10 **steps** (model calls). It is named `max_steps` /
  `steps_used` / `step_limit` so it never shares a name with FR-022's limit of 5 Coder ⇄ QA
  **iterations**.
- Generated code uses only the standard library, because the package install step
  (FR-011, FR-011a) is not built yet and the run sandbox has no network (FR-021). The
  install step's design is decided (research R11) and deferred to a later increment.
- Superseded: T004's agent container was replaced in Phase 7 by a sandbox container per code
  run, which is what FR-021 now requires.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US3)
- Paths are relative to the repository root

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Package layout, dependencies, retiring Lab 1.1, and the project container

- [X] T001 Create empty package markers `agents/__init__.py` and `tests/__init__.py` so `python -m agents.coder_agent` and `python -m unittest discover -s tests -t .` both work
- [X] T002 Edit `requirements.txt`: remove `openai`; add `anthropic` (1.x) and `langgraph` (1.x) with exact `==` pins taken from `pip index versions <pkg>` (newest release at least two weeks old); keep `python-dotenv` and `chromadb`; then run `pip install -r requirements.txt` (constitution v1.1.0, Principle III)
- [X] T003 Retire the Lab 1.1 files: delete `agent.py`, `memory.py`, and `test_memory.py` (unfinished skeletons; `memory.py` is the only `openai` importer, and the other two depend on it; all three stay recoverable from the initial commit), then confirm `grep -rn "openai" --include=*.py .` (excluding `.venv/`) returns nothing
- [X] T004 [P] Write `Dockerfile` and `docker-compose.yml`. `Dockerfile`: `FROM python:3.12-slim` (constitution Python Standards), `WORKDIR /app`, copy and `pip install --no-cache-dir -r requirements.txt` before copying the source, create and switch to a non-root user, `ENTRYPOINT ["python", "-m", "agents.coder_agent"]`. `docker-compose.yml`: one `coder` service built from `.`, `env_file: .env`, `mem_limit: 2g`, `cpus: 2`, bind mounts `./workspace:/app/workspace` and `./.chroma:/app/.chroma`, a named volume for `/home/<user>/.cache/chroma`, and no other host mounts (plan Spec Deviations, FR-021). Confirm with `docker compose build coder`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Tools, memory, and shared types that the Coder Agent's graph is built on

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests (write first, confirm they fail)

- [X] T005 [P] Write `tests/test_tools.py` covering every case table in `specs/001-autonomous-dev-team/contracts/tools.md`: `read_file` returns contents, truncates above 200 KB with `[truncated: file is N bytes]`, reports a missing file as an error; `read_file`/`write_file` reject absolute paths, `..` escapes, and symlinks that resolve outside the workspace; `write_file` creates parent folders and reports bytes written; `exec_python` returns exit code/stdout/stderr, reports a non-zero exit with `is_error` false, kills a `while True:` script after a 1 s timeout with `exit_code None` and `timed_out True`, truncates each stream above 10,000 chars, rejects non-`.py` and missing files, and runs with an environment that contains no `ANTHROPIC_API_KEY` even when the parent process has one (FR-021a). Use `tempfile.TemporaryDirectory` as the workspace
- [X] T006 [P] Write `tests/test_agent_memory.py`: `SlidingWindowBuffer(max_turns=5)` evicts the oldest turn when a sixth is added and `get()` returns turns oldest-first; `SemanticMemory` in a temporary Chroma directory returns a stored record for a semantically similar query, excludes records whose `run_id` equals the excluded run, truncates documents to 2,000 chars, and returns `[]` on an empty collection

### Implementation

- [X] T007 [P] Implement `tools/file_tools.py` per `specs/001-autonomous-dev-team/contracts/tools.md`: `ToolOutput = tuple[str, bool]`; `resolve_in_workspace(workspace: Path, path: str) -> Path` that raises `ValueError("Path is outside the workspace: <path>")` for absolute paths or any resolved path not under `workspace.resolve()`; `read_file(workspace, path) -> ToolOutput` (UTF-8, 200 KB cap); `write_file(workspace, path, content) -> ToolOutput` (creates parents); and the `READ_FILE_TOOL` / `WRITE_FILE_TOOL` JSON definitions with `"strict": true` and `"additionalProperties": false`
- [X] T008 [P] Implement `tools/python_exec.py` per `specs/001-autonomous-dev-team/contracts/tools.md` and data-model.md: `ExecutionResult` dataclass (`path`, `exit_code`, `stdout`, `stderr`, `timed_out`, `duration_seconds`) with `to_dict()`; `exec_python(workspace, path, timeout_seconds) -> ExecutionResult` running `[sys.executable, "-s", path]` (not `-I`, which drops the script's folder from `sys.path`) in its own process group with `cwd=workspace`, `stdin=subprocess.DEVNULL`, env limited to `PATH`, `PYTHONIOENCODING=utf-8`, `PYTHONDONTWRITEBYTECODE=1`, `subprocess.TimeoutExpired` mapped to `timed_out=True`, 10,000-char cap per stream with a `[truncated]` marker; path validation reuses `resolve_in_workspace` from `tools/file_tools.py`; and the `EXEC_PYTHON_TOOL` JSON definition
- [X] T009 Implement `tools/__init__.py`: export `TOOL_DEFINITIONS = [READ_FILE_TOOL, WRITE_FILE_TOOL, EXEC_PYTHON_TOOL]` and `dispatch_tool(name, tool_input, workspace, timeout_seconds) -> tuple[str, bool, ExecutionResult | None]`, which returns `exec_python` results as JSON text (`is_error` false) and turns `ValueError`/`FileNotFoundError` from any tool into `(message, True, None)` so errors never escape the tool layer (depends on T007, T008)
- [X] T010 [P] Implement `agents/memory.py`: `Turn` dataclass (`task`, `reply`); `SlidingWindowBuffer(max_turns)` with `add(turn)` and `get() -> list[Turn]`; `SemanticMemory(path: Path, collection="coder_memory")` using `chromadb.PersistentClient` with Chroma's default embedding function and `{"hnsw:space": "cosine"}`, `add(run_id, kind, text) -> str` (id `"<run_id>-<seq>"`, metadata `run_id`/`kind`/`created_at` in ISO 8601 UTC, text truncated to 2,000 chars) and `query(text, top_k, exclude_run_id) -> list[tuple[str, str]]` returning `(id, document)` pairs filtered with `{"run_id": {"$ne": exclude_run_id}}`
- [X] T011 Create the shared types in `agents/coder_agent.py` per `specs/001-autonomous-dev-team/data-model.md`: `CoderConfig` dataclass with the defaults in the CoderConfig table (including `max_steps`, env `CODER_MAX_STEPS`), a `from_env()` classmethod that applies each env override and raises `ValueError` for out-of-range values (`max_steps` 1–10, `effort` in `low/medium/high/xhigh/max`, `exec_timeout_seconds > 0`); `ToolCall`, `Step` (`number`, `thought`, `actions`), and `CoderResult` dataclasses each with `to_dict()` (CoderResult keys exactly as in the data-model table, including `steps_used`, `duration_seconds`, and `usage`); `AgentState` `TypedDict` (with `step_count` and a running `usage` total); and `new_run_id() -> str` producing `YYYYMMDD-HHMMSS-<6 hex>` (depends on T008 for `ExecutionResult`)

**Checkpoint**: `python -m unittest tests.test_tools tests.test_agent_memory -v` passes

---

## Phase 3: User Story 1 - Requirement In, Tested Code Out (Priority: P1) 🎯 MVP

**Goal**: The Coder Agent's part of User Story 1. Given a plain-English coding task, the agent
plans, writes files, runs them, fixes what fails, and returns a structured result with
`code`, `explanation`, `plan`, and a real `execution_result`, without asking anything.

**Independent Test**: With credentials set, `python -m agents.coder_agent "Write fib.py that prints fib(10)"`
(or the same task through `docker compose run --rm coder`) exits 0 and prints JSON whose
`execution_result.exit_code` is 0 and `stdout` contains `55`. Offline,
`tests/test_coder_agent_loop.py` proves the same path with a scripted fake client.

### Tests for User Story 1 (write first, confirm they fail)

- [X] T012 [P] [US1] Write `tests/test_coder_agent_loop.py` with a scripted fake client exposing `beta.messages.create(**kwargs)` that returns objects shaped like Anthropic responses (`content`, `stop_reason`, `stop_details`, `usage.input_tokens`, `usage.output_tokens`), plus a spy `SemanticMemory` and a temporary workspace and Chroma path. Cases: (a) `write_file` → `exec_python` → `end_turn` → finalize JSON gives `status "completed"`, `code` equal to the file on disk, and `execution_result` from a real run of `main_file` (contract A2, A6); (b) memory `query` is called before every model call (A3); (c) a client that always returns `tool_use` stops with `status "step_limit"` after exactly `max_steps` loop calls, `steps_used == max_steps`, and still returns all four required fields (A4); (d) `stop_reason "refusal"` gives `status "refused"` with `error` set; (e) the client raising `anthropic.APIError` gives `status "error"` and `run()` does not raise (A1); (f) `run("")` raises `ValueError`; (g) every request sent to the client has the same `system` and `tools` as the first one, and each request's `messages` starts with the previous request's `messages` (research R3, append-only); (h) for case (a), `duration_seconds > 0` and `usage` equals the sum of the fake responses' token counts, including the finalize call (FR-025)
- [X] T013 [P] [US1] Write `tests/test_coder_agent_smoke.py`: one live run of "Write a function that returns the n-th Fibonacci number and print fib(10)" through `run_coder_agent`, asserting `code` is `str`, `explanation` is `str`, `plan` is a `list` of `str`, `execution_result` is a `dict` with `exit_code == 0`; decorate with `unittest.skipUnless(os.environ.get("ANTHROPIC_API_KEY"), ...)` after `load_dotenv()`

### Implementation for User Story 1

- [X] T014 [US1] In `agents/coder_agent.py`, add the frozen `SYSTEM_PROMPT` (Coder role; use workspace-relative paths; always run the main file with `exec_python` and fix failures before finishing; do not write test files, since tests belong to the QA Agent (spec FR-013), and check your work by running it; follow PEP 8, type hints, docstrings, small functions; use only the Python standard library because this increment cannot install packages) and `build_request(config, messages) -> dict` returning `model`, `max_tokens=16000`, `system`, `tools=TOOL_DEFINITIONS`, `messages`, `thinking={"type": "adaptive", "display": "summarized"}`, `output_config={"effort": config.effort}`, and, when `config.use_fallbacks`, `betas=["server-side-fallback-2026-07-01"]` with `fallbacks="default"` (research R2)
- [X] T015 [US1] In `agents/coder_agent.py`, implement the `recall` node: on the first call, put the task and the sliding window rendered as "Recent conversation" into the run's first user message; on every call, query `SemanticMemory` with the task plus the latest observation (`top_k=config.memory_top_k`, excluding the current `run_id`), drop ids already in `injected_memory_ids`, and append any remaining memories as one `{"role": "system"}` message after the latest message, never editing earlier messages (research R3, R5)
- [X] T016 [US1] In `agents/coder_agent.py`, implement the `think` node: send `build_request(...)` through `client.beta.messages.create`, append `response.content` unchanged as one assistant message, increment `step_count`, add `response.usage.input_tokens` / `output_tokens` to the state's running `usage`, and store `stop_reason`; on `stop_reason == "refusal"` set `status "refused"` and `error` from `stop_details`; catch `anthropic.APIError` and set `status "error"` with the message (retries are left to the SDK's `max_retries=3`, FR-023)
- [X] T017 [US1] In `agents/coder_agent.py`, implement the `act` node: run every `tool_use` block through `dispatch_tool` with the run's workspace and `config.exec_timeout_seconds`, append all `tool_result` blocks (with `is_error` where set) in a single user message, add `write_file` paths to `files_written`, set `last_execution` from `exec_python` results, and store each observation in `SemanticMemory` with kind `observation`
- [X] T018 [US1] In `agents/coder_agent.py`, implement the `finalize` node: for `refused`/`error` states, build the `CoderResult` without another model call; otherwise make one more request with the same frozen `system`/`tools` plus `output_config.format` set to a JSON schema with `plan` (array of strings), `explanation` (string), and `main_file` (string or null), add its token usage to the running total, then fill `code` from `main_file` on disk and `execution_result` from `last_execution` when it ran `main_file`, otherwise by calling `exec_python` once (contract A6, research R7)
- [X] T019 [US1] In `agents/coder_agent.py`, implement the router and graph builder: `route_after_think` returns `"act"` when `stop_reason == "tool_use"` and `step_count < config.max_steps`, sets `status "step_limit"` and returns `"finalize"` when the cap is hit, and returns `"finalize"` for `end_turn`, refusal, or error; build `StateGraph(AgentState)` with `START → recall → think → {act | finalize}`, `act → recall`, `finalize → END`, using only `StateGraph`, `START`, and `END` from `langgraph` (constitution Principle III); invoke with `recursion_limit = 4 * max_steps + 5` (research R4)
- [X] T020 [US1] In `agents/coder_agent.py`, implement `CoderAgent.__init__(config=None, client=None)` (config defaults to `CoderConfig.from_env()`, client to `anthropic.Anthropic(max_retries=3)`, creates `SlidingWindowBuffer`, `SemanticMemory`, and compiles the graph once) and `CoderAgent.run(task) -> CoderResult` (raise `ValueError` for an empty task, start a `time.monotonic()` timer, create `workspace/<run_id>/`, store the task in memory with kind `task`, invoke the graph, set `duration_seconds`, add a `Turn(task, explanation)` to the window, store the explanation with kind `result`); add `run_coder_agent(task, config=None)` (contract coder-agent-api.md)
- [X] T021 [US1] In `agents/coder_agent.py`, add the CLI `main(argv=None) -> int` per the CLI contract: `load_dotenv()`, `argparse` with positional `task`, `--max-steps`, `--timeout`; print `to_dict()` as indented JSON to stdout; return 0 for `completed`, 1 for other statuses, 2 for usage errors including `ValueError` from config; guard with `if __name__ == "__main__": sys.exit(main())`
- [X] T022 [US1] Run `python -m unittest discover -s tests -t . -v`, then quickstart Scenarios 2, 3, and 4 from `specs/001-autonomous-dev-team/quickstart.md` with live credentials (Scenario 3 through `docker compose run --rm coder`), and fix any failures in `agents/coder_agent.py`, `agents/memory.py`, or `tools/`

**Checkpoint**: The Coder Agent turns a task into working code with a real execution result,
offline tests pass, memory is recalled across runs (Scenario 4), and the agent runs in the
project container

---

## Phase 4: User Story 2 - Automatic Review and Fix Loop (Priority: P2)

**Deferred.** This story needs the QA & Debugger Agent and the Coder ⇄ QA orchestration
(FR-012–FR-020), which plan.md places in a later increment. No tasks in this increment. Run
`/speckit-plan` for the next increment to generate them.

---

## Phase 5: User Story 3 - Run Record and Outcome Report (Priority: P3)

**Goal**: The Coder Agent's part of User Story 3. Every result carries a readable
Thought → Action → Observation trace, and the CLI shows progress while a run is going, so a
reviewer can see what the agent did and why a run stopped.

**Independent Test**: Run quickstart Scenario 5 (`CODER_EXEC_TIMEOUT=2`, infinite loop). The
JSON `steps` list shows each step's thought and tool calls, including an `exec_python`
call whose output has `timed_out: true`. stderr has one `[step N] ...` line per step.

### Tests for User Story 3 (write first, confirm they fail)

- [X] T023 [US3] Extend `tests/test_coder_agent_loop.py`: for the scripted run from case (a), assert `result.steps` has one `Step` per loop model call with a 1-based `number`, a non-empty `thought` built from the fake response's thinking summary and text, and `actions` whose `tool`, `input`, `output`, and `is_error` match what `dispatch_tool` returned; assert an `on_step` callback passed to `CoderAgent` receives each `Step` in order; assert `to_dict()` is `json.dumps`-serialisable; assert `main(["<task>"])` writes one `[step N] <tool> <path>` line per step to stderr (capture with `contextlib.redirect_stderr` and a patched `CoderAgent`)

### Implementation for User Story 3

- [X] T024 [US3] In `agents/coder_agent.py`, record the trace: the `think` node appends a `Step(number, thought, actions=[])` with `thought` built from the response's thinking summaries and text blocks; the `act` node fills that step's `actions` with a `ToolCall` per tool executed; `finalize` copies `steps` into `CoderResult`
- [X] T025 [US3] In `agents/coder_agent.py`, add the `on_step: Callable[[Step], None] | None = None` parameter to `CoderAgent.__init__` (contract coder-agent-api.md) and call it after each `act`; the CLI passes one that prints `[step N] <tool> <path>` (one line per step, tools joined by `, `) to stderr

**Checkpoint**: A finished or failed run can be understood from its JSON and stderr alone

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, constitution compliance, and final validation

- [X] T026 [P] Rewrite `README.md`: project vision (PM → Coder ⇄ QA team from spec.md), current status (Increment 1: Coder Agent only), setup (Python 3.12, venv, `pip install -r requirements.txt`, `ANTHROPIC_API_KEY` in `.env`), usage (CLI and Python API from `specs/001-autonomous-dev-team/contracts/coder-agent-api.md`, and `docker compose run --rm coder "<task>"`), tests, design decisions (LangGraph core graph API, Claude Sonnet 5.5, two-tier memory, append-only history), a clear warning that run locally, generated code runs as a subprocess with your OS permissions, so the container is the recommended way to run it, and a note that the Lab 1.1 files and `openai` were retired (recoverable from the initial commit)
- [X] T027 Review `agents/coder_agent.py`, `agents/memory.py`, and `tools/*.py` against `.specify/memory/constitution.md` v1.1.0: split any function over ~40 lines or 3 nesting levels, add missing type hints and docstrings on public names, remove bare `except:` and commented-out code, group imports stdlib/third-party/local, confirm every third-party import is declared in `requirements.txt`, and confirm `langgraph` is used only through `StateGraph`, `START`, and `END` (no `langgraph.prebuilt`, LangChain wrappers, or checkpointers)
- [X] T028 Run `python -m unittest discover -s tests -t . -v` and quickstart Scenarios 1 and 5 from `specs/001-autonomous-dev-team/quickstart.md` (Scenarios 2–4 ran in T022), plus `docker compose build coder`; record any scenario that cannot run (for example, no credentials) in the final report instead of marking it passed

---

## Phase 7: Post-Implementation Fixes (2026-10-17)

**Purpose**: Fix the two issues found in the implementation report: empty step thoughts, and
generated code running with the user's permissions, files, and network. The user asked for
the more secure option in every trade-off.

- [X] T029 [US3] Ask for a one-sentence note before each tool call in `SYSTEM_PROMPT` in `agents/coder_agent.py`
- [X] T030 [US3] Add a required, strict `purpose` argument to every tool in `tools/__init__.py`, and use the calls' purposes as a step's thought when the model wrote no note in `agents/coder_agent.py`
- [X] T031 Run every `exec_python` call in a new `docker run --rm` sandbox (no network, no secrets, read-only root, only the run's workspace mounted, capabilities dropped, non-root, 512 MB / 1 CPU / 128 processes, image pinned by digest) in `tools/python_exec.py`
- [X] T032 Refuse to start the agent without Docker (`ensure_sandbox`, `SandboxUnavailableError`; CLI exit code 2) in `agents/coder_agent.py`
- [X] T033 Add `pysqlite3-binary` and the SQLite swap so Chroma runs on hosts with SQLite < 3.35 in `requirements.txt` and `agents/memory.py`
- [X] T034 Remove the agent container (`Dockerfile`, `docker-compose.yml`, `.dockerignore`), which could not sandbox code and exposed the API key through `/proc`; supersedes T004
- [X] T035 [P] Add sandbox isolation, Docker-required, and purpose-schema tests in `tests/test_tools.py` and `tests/test_coder_agent_loop.py`
- [X] T036 [P] Update `README.md`, `quickstart.md`, `plan.md`, `research.md`, `contracts/tools.md`, `data-model.md`, and the constitution (v1.1.1)
- [X] T037 Run the full suite on the host plus quickstart Scenarios 3 and 5 live

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies
- **Foundational (Phase 2)**: Depends on Setup; blocks all user stories
- **User Story 1 (Phase 3)**: Depends on Foundational
- **User Story 2 (Phase 4)**: Deferred, no tasks
- **User Story 3 (Phase 5)**: Depends on User Story 1 (it extends the same nodes in `agents/coder_agent.py`)
- **Polish (Phase 6)**: Depends on User Stories 1 and 3

### Task Dependencies

- T002 → T003 (remove `openai` from the requirements, then the files that import it); T004 can run alongside T002/T003
- T005, T006 (tests) before T007–T011 (implementation)
- T007, T008 → T009; T008 → T011
- T012, T013 (tests) before T014–T021
- T014 → T015 → T016 → T017 → T018 → T019 → T020 → T021 → T022 (all edit `agents/coder_agent.py`); T022 also needs T004
- T023 before T024 → T025
- T026 can start once T021 is done; T027 → T028

### Parallel Opportunities

- Phase 1: T004 alongside T002/T003
- Phase 2 tests: T005 and T006
- Phase 2 implementation: T007, T008, and T010 (three separate files)
- Phase 3 tests: T012 and T013
- Phase 6: T026 alongside T027

---

## Parallel Example: Foundational Phase

```bash
# Tests first, together:
Task: "Write tests/test_tools.py covering every case table in contracts/tools.md"
Task: "Write tests/test_agent_memory.py for SlidingWindowBuffer and SemanticMemory"

# Then the three independent modules, together:
Task: "Implement tools/file_tools.py per contracts/tools.md"
Task: "Implement tools/python_exec.py per contracts/tools.md"
Task: "Implement agents/memory.py (SlidingWindowBuffer, SemanticMemory)"
```

## Parallel Example: User Story 1

```bash
Task: "Write tests/test_coder_agent_loop.py with a scripted fake client"
Task: "Write tests/test_coder_agent_smoke.py (live, skipped without credentials)"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: offline tests pass and quickstart Scenarios 2–4 work with live credentials

### Incremental Delivery

1. Setup + Foundational → tools and memory tested on their own, container builds
2. User Story 1 → a working Coder Agent with CLI (MVP)
3. User Story 3 → readable trace and progress output
4. Polish → README, constitution review, full validation
5. Next increment (`/speckit-plan`): PM Agent, QA Agent, Coder ⇄ QA loop, per-run containers, run limits

---

## Notes

- [P] tasks touch different files and have no dependencies on incomplete tasks
- Most of the Coder Agent lives in one file, `agents/coder_agent.py`, so its tasks run in order
- Write each test task first and confirm it fails before implementing
- Commit after each task or logical group
