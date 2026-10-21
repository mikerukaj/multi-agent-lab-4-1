---

description: "Task list for QA & Debugger Agent, Coder ⇄ QA Feedback Loop (over A2A), and MCP File Tools"
---

# Tasks: QA & Debugger Agent, Coder ⇄ QA Feedback Loop, and MCP File Tools

**Input**: Design documents from `specs/002-qa-feedback-loop-mcp/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md)
(R1–R22), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: The planning input asks for tests (the QA agent with mocked code input;
the loop ending in both the success and the iteration-limit cases). So each phase has
test tasks. Write them first and confirm they fail. All project tests use stdlib
`unittest`. Run them with `python -m unittest discover -s tests -t .`.

**Organization**: Tasks are grouped by user story. Two layers are **Foundational**
because every story uses them:
- the MCP server, client and Coder wiring (FR-123: both agents are MCP clients);
- the A2A 1.0 protocol layer and the asyncio broker (FR-129: every Coder ⇄ QA exchange
  goes through it).

Then:
- **US1** adds the QA agent and its A2A endpoint (`review-program`, `final-report`).
- **US2** adds reflection, the Coder's A2A endpoint (`fix-program`) and the loop.
- **US3** covers the standalone MCP server for outside clients.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: US1 (QA reviews), US2 (feedback loop + reflection), US3 (MCP standalone)
- Paths are relative to the repository root

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependencies, the sandbox image recipe, the constitution amendment, and the project version constant

- [X] T001 Create and activate `.venv` (`python3.12 -m venv .venv`), add `mcp==2.3.0` to `requirements.txt` (alphabetical order, keeping the existing pins), and run `pip install -r requirements.txt`. Then check these v2 imports with a one-off `python -c`:
  - `from mcp.server import Server`
  - `from mcp.server.stdio import stdio_server`
  - `from mcp import ClientSession`
  - `from mcp.client.stdio import stdio_client, StdioServerParameters`
  - `from mcp.types import Tool, ListToolsResult, CallToolResult, TextContent, CallToolRequestParams`

  If any path differs, write the working path under research R1 in `specs/002-qa-feedback-loop-mcp/research.md` and use it in T010/T011.

  Then confirm the dependency compatibility in the R16 table:
  - `pip check` must print `No broken requirements found.`
  - `python -c "from importlib.metadata import version as v; import agents.memory, chromadb, anthropic, mcp; print(v('pydantic'), v('httpx2'))"` (import `agents.memory` first: it swaps in `pysqlite3`, which `chromadb` needs on this system) must print pydantic ≥ 2.12 and httpx2 ≥ 2.10.

  If either check fails, stop and record the conflict in R16 before going on.

  Do **not** add `a2a-sdk` (research R17).
- [X] T002 [P] Create `sandbox/Dockerfile` exactly as in research R6: `ARG BASE_IMAGE`, `FROM ${BASE_IMAGE}`, `ARG PYTEST_VERSION`, `RUN pip install --no-cache-dir "pytest==${PYTEST_VERSION}"`. Use no other layers and no hard-coded versions.
- [X] T003 [P] Amend `.specify/memory/constitution.md` to v1.2.0 (MINOR). Principle III's approved baseline gains `mcp`, limited to its low-level `Server`, `stdio_server`, `ClientSession`, `stdio_client` and `mcp.types`. State that `pytest` is allowed only inside the sandbox image, never as a project import. Note that A2A is implemented with the standard library and no SDK. Update the Sync Impact Report and set Last Amended to today. Commit this separately from code changes.
- [X] T004 [P] Create `messaging/__init__.py` with a one-line module docstring ("Agent-to-agent messaging: A2A 1.0 types, the asyncio broker, and agent endpoints.") and `__version__ = "0.3.0"`. This is the repository's single version constant (increment 3, pre-1.0). The MCP server (T010) and the A2A agent cards (T032, T045) read it from here. Bump it when an increment ships.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: State types, the pytest sandbox image, the MCP server, client and Coder wiring, and the A2A protocol layer and broker. Every story uses all of these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Shared state and sandbox

- [X] T005 [P] Add every Literal and TypedDict from `specs/002-qa-feedback-loop-mcp/data-model.md` to `orchestration/state.py`:
  - Literals: `TestOutcome`, `FindingKind`, `Severity`, `FindingState`, `ResponseAction`, `DisputeDecision`, `TestChangeReason`, `CritiqueKind`, `Verdict`, and `EndReason` (including `"a2a_failed"`).
  - TypedDicts: `TestCaseResult`, `TestRun`, `QAFinding`, `FindingResponse`, `Dispute`, `TestChange`, `CritiqueItem`, `Reflection`, `CoderFix`, `QAReport`, `A2AExchange`, and `Iteration` (including `review_task_id` and `fix_task_id`).
  - New `TeamState` keys: `max_iterations`, `iterations`, `pending_reflections`, `test_suite`, `qa_report`, `end_reason`, `a2a_log`.
  - QA request payloads: `QAContext`, `FixRequest`, `FinalReportRequest` (data-model "QA request payloads"). These are JSON-only, with paths as strings. The team graph, the A2A endpoints and both agents share them from here, so neither the endpoints nor either agent imports another agent's module (SC-110).

  The A2A protocol types themselves go in `messaging/a2a.py`, not here. Update the module docstring.
- [X] T006 Extend `initial_team_state` in `orchestration/team.py` with a `max_iterations: int = 5` parameter. Initialise the new keys to `max_iterations`, `[]`, `[]`, `{}`, `None`, `None` and `[]`. Pass `max_iterations` through from `run_team` (validation comes in T047).
- [X] T007 [P] Extend `tests/test_team_state.py` with two tests. The first builds a `TeamState` with every new key filled with realistic values and asserts `json.loads(json.dumps(state)) == state`. The values are:
  - one `Iteration`, with `review_task_id` and `fix_task_id`, holding a `TestRun`, two `QAFinding`s, a `Dispute`, a `Reflection` and a `CoderFix`;
  - a `QAReport`;
  - two `A2AExchange` entries;
  - one each of `QAContext`, `FixRequest` and `FinalReportRequest`.

  The second asserts that `initial_team_state` sets the new defaults.
- [X] T008 Switch the sandbox image in `tools/python_exec.py` to the derived build:
  - Rename the current digest constant to `SANDBOX_BASE_IMAGE`.
  - Add `PYTEST_VERSION = "9.1.1"`, `SANDBOX_IMAGE = "multi-agent-sandbox:py3.12-pytest9.1.1"` and `SANDBOX_DIR = Path(__file__).resolve().parents[1] / "sandbox"`.
  - Make `ensure_sandbox(image)` run `docker build --quiet --build-arg BASE_IMAGE=… --build-arg PYTEST_VERSION=… -t <image> <SANDBOX_DIR>` (600 s timeout) when `image == SANDBOX_IMAGE` and the image is missing. Keep the pull path for any other image.
  - Raise `SandboxUnavailableError("Could not build sandbox image …: <stderr>")` when the build fails.
- [X] T009 Update `tests/test_tools.py` for T008. Move the `"@sha256:"` assertion to `SANDBOX_BASE_IMAGE`. Add offline tests that patch `tools.python_exec._docker` to check:
  - The build command and build-args when the image is missing.
  - No build when `image inspect` succeeds.
  - Pull, not build, for a custom image.
  - `SandboxUnavailableError` on a failed build.

### MCP file tools

- [X] T010 Implement `tools/mcp_server.py` (core) with the SDK's low-level `Server("file-tools", …)`, passing `on_list_tools` and `on_call_tool` to the constructor:
  - **List**: return `ListToolsResult` with exactly `read_file` then `write_file`. Each has its `READ_FILE_TOOL`/`WRITE_FILE_TOOL` description and the `input_schema` copied word for word.
  - **Call**: send each call to `tools.file_tools.read_file` / `write_file`. Always return `CallToolResult(content=[TextContent(type="text", text=…)], is_error=…)`, and never raise for tool failures.
  - **Protect**: a `write_file` path that resolves inside any `--protect` prefix gets `is_error` with `Path is protected: <path> (only the QA Agent may change tests)`.
  - **CLI**: `main(argv)` with argparse (`--workspace` required, `--protect` repeatable). It runs `asyncio.run(serve(...))` through `stdio_server()` with `server.create_initialization_options()`, under an `if __name__ == "__main__":` guard.
  - **Version**: `Server("file-tools", version=messaging.__version__)`.
  - Never import dotenv and never read secrets.
- [X] T011 Implement `tools/mcp_client.py` per [contracts/mcp-file-server.md](contracts/mcp-file-server.md):
  - Define `McpUnavailableError(RuntimeError)` and `McpFileTools(workspace, protect=(), call_timeout_seconds=30.0)` as a context manager.
  - **Background loop**: a daemon thread owns an asyncio loop. It enters `stdio_client(server_parameters())` and `ClientSession`, then calls `initialize()` and `list_tools()`. It signals readiness through a `threading.Event`, then waits on a stop event, so it exits the async contexts in the same task.
  - **Server parameters**: `server_parameters()` returns `StdioServerParameters(command=sys.executable, args=["-m", "tools.mcp_server", "--workspace", <abs>, *("--protect", p for p in protect)], env={"PATH": os.environ["PATH"], "PYTHONIOENCODING": "utf-8"}, cwd=<repo root>)`.
  - **Methods**: `definitions()` returns `[{"name", "description", "input_schema", "strict": True}]`. `call(name, arguments)` uses `asyncio.run_coroutine_threadsafe(session.call_tool(...))` and returns `(joined text content, result.is_error)`.
  - **Failures**: raise `McpUnavailableError` on a setup timeout (15 s), a server exit, a tool list that isn't exactly `{read_file, write_file}`, a JSON-RPC error, a call timeout, or a call after close.
- [X] T012 Create `tests/test_mcp_server.py` with foundational smoke tests that use the real server over stdio through `McpFileTools` on a `tempfile.TemporaryDirectory` workspace:
  - The tool names are exactly `["read_file", "write_file"]`.
  - A write-then-read round trip returns identical content and the file is on disk.
  - `protect=("tests",)` refuses `tests/a.py` with the protected message, but allows reading it.
  - `__exit__` stops the subprocess.
- [X] T013 Rework `tools/__init__.py`:
  - Add `session_tool_definitions(file_tools) -> list[dict]`, returning `[_with_purpose(t) for t in file_tools.definitions()] + [_with_purpose(EXEC_PYTHON_TOOL)]`.
  - Change `dispatch_tool(name, tool_input, workspace, timeout_seconds, image, file_tools)` so `read_file`/`write_file` go to `file_tools.call(name, {k: v for k, v in tool_input.items() if k != "purpose"})`, while `exec_python` runs in process.
  - Let `McpUnavailableError` propagate.
  - Remove the module-level `TOOL_DEFINITIONS` constant once no caller uses it.
- [X] T014 Wire MCP into `CoderAgent` in `agents/coder_agent.py`:
  - **Constructor**: add a `file_tools=McpFileTools` factory parameter.
  - **`run()` session**: open one `self._file_tools_factory(workspace, protect=("tests", ".qa"))` session. Build `tools = session_tool_definitions(session)` once and keep the session on `self` for the graph nodes.
  - **`build_request`**: becomes `build_request(config, messages, tools, output_format=None)`.
  - **Nodes**: `_act` calls `dispatch_tool(..., file_tools=session)`. `_finalize` reads the main file through `session.call("read_file", …)`.
  - **Errors**: catch `McpUnavailableError` around the session and return a `CoderResult` with `status="error"` and `error="MCP file tools unavailable: …"`.
  - **Prompt**: add to `SYSTEM_PROMPT`: "Writes under `tests/` are refused. You may read the tests to understand a failure."
- [X] T015 Add `FakeFileTools` to `tests/team_fakes.py`. It is an in-process stand-in for `McpFileTools`: same constructor signature, context manager, `definitions()` built from `READ_FILE_TOOL`/`WRITE_FILE_TOOL`, `call()` that forwards to `tools.file_tools` and honours `protect`, and a `calls` log. Then update `tests/test_coder_agent_loop.py` and `tests/test_coder_agent_smoke.py` for the new `build_request` signature and injected file tools, and add three tests:
  - Every model `read_file`/`write_file` goes through the file-tools object (FR-123).
  - The request's tool order is `read_file`, `write_file`, `exec_python`, and it is identical on every request in a run.
  - A protected write comes back to the model as an `is_error` tool result.

### A2A protocol layer and asyncio broker

- [X] T016 [P] Create `tests/test_a2a.py` (stdlib only), testing the T018 module per [contracts/a2a-binding.md](contracts/a2a-binding.md) §2 and data-model "A2A protocol types":
  - **Parts**: `text_part`/`data_part` hold exactly one content member with the right `mediaType`, and no `kind` key anywhere (search builder output recursively).
  - **Messages**: `user_message` has `messageId` (32 hex characters), `role == "ROLE_USER"`, the given `contextId`, `metadata` and `referenceTaskIds`.
  - **Tasks**: `new_task` starts with `TASK_STATE_SUBMITTED`. `now_timestamp()` matches `^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$`.
  - **Validators**: `validate_message` rejects a missing `messageId`, an empty `parts`, a Part with two content members or with `kind`, and an unknown role. `validate_envelope` rejects a missing `sender`/`recipient`/`rpc` and a wrong `jsonrpc`.
  - **Errors**: `rpc_error(A2AError(...))` gives `{code, message, data: {type, detail}}` with the codes in contract §5.
  - **Version**: `messaging.__version__ == "0.3.0"`.
  - **Cards**: `agent_card(...)` has every required AgentCard field, `supportedInterfaces[0].protocolBinding == BINDING_URN`, `protocolVersion == "1.0"`, and every capability `False`.
- [X] T017 [P] Create `tests/test_broker.py` against a real `A2ABroker`, with tiny synchronous handlers (no agents). Cover research R19/R20 and contract §4–§5:
  - **Routing and order**: delivery goes to the named recipient. Two messages to one recipient are handled in FIFO order (the handler logs them).
  - **Responsiveness**: while a handler blocks on a `threading.Event`, `get_task` returns `TASK_STATE_WORKING`.
  - **Isolation**: a handler that mutates its input message does not change the sender's dict, and mutating the returned task does not change the broker's stored copy.
  - **Errors**:
    - an unknown recipient → `A2AError` code -32602, `UnknownAgent`;
    - a `skillId` missing from the card → `TASK_STATE_REJECTED`;
    - a handler raising `SkillFailed` → `TASK_STATE_FAILED` with the status message text;
    - an unexpected exception → `FAILED` with `"<Type>: <msg>"`;
    - `serviceParameters` with `A2A-Version` `"0.3"` → -32009;
    - an unknown method → -32601;
    - `SendStreamingMessage` → -32004;
    - a push-config method → -32003;
    - `GetExtendedAgentCard` → -32007 (or the code checked in T018).
  - **Cancel and lookup**: `CancelTask` on a queued task → `TASK_STATE_CANCELED`, and the handler never runs. `CancelTask` on a working task → -32002. `GetTask` on an unknown ID → -32001.
  - **`returnImmediately`**: true → a `SUBMITTED` task is returned at once.
  - **Timeout**: a request timeout of 0.2 s with a blocked handler → `A2ATimeoutError` naming the recipient.
  - **Log**: `exchanges()` has one `A2AExchange` per request, with ordered `states`, `outcome` and ISO timestamps. `client.exchanges()` returns the same entries as `broker.exchanges()`, including other senders' exchanges. Changing the returned list changes neither the broker's log nor a later call's result.
  - **Close**: `close()` with a pending request → `BrokerClosedError`.
- [X] T018 Create `messaging/a2a.py` (the package and `__version__` come from T004) per [contracts/a2a-binding.md](contracts/a2a-binding.md) and research R17/R18:
  - **Constants**: `A2A_VERSION = "1.0"`, `BINDING_URN = "urn:multi-agent-lab:a2a-binding:asyncio-broker:1.0"`, `TERMINAL_STATES`, `ERROR_CODES` (name → code).
  - **TypedDicts**: `Part`, `Message`, `TaskStatus`, `Artifact`, `Task`, `SendMessageConfiguration`, `SendMessageRequest`, `AgentSkill`, `AgentInterface`, `AgentCapabilities`, `AgentCard`, `Envelope`, with `total=False` where the spec says optional.
  - **Builders**: `now_timestamp()`, `text_part(text)`, `data_part(obj)`, `user_message(parts, context_id, metadata, reference_task_ids=())`, `agent_message(text)`, `artifact(name, parts)`, `new_task(context_id)`, `agent_skill(id, name, description, tags)`, `agent_card(name, description, skills, version)`.
  - **Validators**: `validate_part`, `validate_message`, `validate_envelope` (they raise `A2AError` -32602).
  - **JSON-RPC helpers**: `rpc_request(method, params)`, `rpc_result(id, result)`, `rpc_error(id, error)`.
  - **Exceptions**: `A2AError(code, type_name, detail)`, `A2ATimeoutError`, `BrokerClosedError`, `SkillRejected`, `SkillFailed`.

  First check A2A 1.0 §3 and §9 (a2a-protocol.org/latest/specification) for the codes of `ExtendedAgentCardNotConfiguredError` and `ExtensionSupportRequiredError`, and for whether `ListTasks` is a §3 operation. Record the answers in `specs/002-qa-feedback-loop-mcp/contracts/a2a-binding.md` §4–§5, replacing the "check when implementing" notes.
- [X] T019 Implement `messaging/broker.py` per contract §1, §4 and §10 and research R19/R20, using only the stdlib (`asyncio`, `threading`, `json`, `copy`-free JSON round-trips).
  - **`A2ABroker(request_timeout_seconds=1800.0)`**:
    - A context manager that starts a daemon thread running a new event loop.
    - `register(card, handler)` creates an `asyncio.Queue` inbox, a task store dict and a worker task for that card's `name`.
    - The worker takes envelopes FIFO and sets `WORKING`. It runs `await asyncio.to_thread(handler, message)` and maps the outcome: artifacts → `COMPLETED`, `SkillRejected` or a `skillId` not on the card → `REJECTED`, `SkillFailed` or any other exception → `FAILED`. Each transition is timestamped in the log, and the reply future is settled.
    - Requests are dispatched by `rpc.method`: `SendMessage`, `GetTask`, `CancelTask`, `ListTasks` (if T018 confirmed it), and the unsupported methods with their error codes. Every envelope and reply goes through `json.loads(json.dumps(...))`, and `A2A-Version` is checked.
    - `exchanges()` returns copies. `close()` cancels the workers, fails pending futures with `BrokerClosedError`, and stops the loop.
  - **`A2AClient(broker, sender)`**:
    - Synchronous `send_message(recipient, message, configuration=None) -> Task`, `get_task`, `cancel_task`, `get_card` and `exchanges()` (copies of the whole run's log, delegating to the broker, so graph nodes never hold the broker), each built on `asyncio.run_coroutine_threadsafe(...).result(timeout)`.
    - On timeout it sends `CancelTask` (ignoring -32002), then raises `A2ATimeoutError("<recipient> did not answer within N s")`.
    - JSON-RPC error replies are raised as `A2AError`.

**Checkpoint**: The existing suite plus T007, T009, T012, T015, T016 and T017 pass. The Coder works through MCP, the image has pytest, and the broker routes A2A messages between test handlers.

---

## Phase 3: User Story 1 - QA Tests and Reviews the Finished Program (Priority: P1) 🎯 MVP

**Goal**: After the Coder finishes all tasks, the Coder side sends a `review-program` A2A message through the broker to `qa-debugger`. QA writes pytest tests from the spec, runs them in the sandbox, reviews the code, and returns the fix instruction list and verdict as an artifact. `finish` asks QA for the final report over A2A. The run ends after one review.

**Independent Test**: With a fake client and fake test runner, mocked code input produces findings that validate against `contracts/qa-findings.schema.json`. A real pytest run of a tiny workspace gives correct counts (with Docker). A team run with fakes behind a real broker goes build → `review-program` (coder → qa-debugger) → `final-report`, with both exchanges in `a2a_log` and a `qa_report`.

### Tests for User Story 1 ⚠️

> Write these tests first and make sure they fail before implementing

- [X] T020 [P] [US1] Add `FakeQA` and `qa_result(...)` to `tests/team_fakes.py` **without importing `agents.qa_agent`**, so the US1 tests can import them before QA exists and fail on assertions, not on imports:
  - `qa_result(verdict, findings=(), test_run=None, test_suite=None, error=None, usage=None)` returns a `ScriptedQAResult`: a small dataclass with the `QAIterationResult` fields from [contracts/qa-agent-api.md](contracts/qa-agent-api.md), an `error` attribute, and `to_dict()` returning plain JSON. The default `test_run` is a loaded run with 0 failures for `complete` and 1 failure for `fix_needed`.
  - `FakeQA(results, call_log=None)` takes the scripted results (or exceptions to raise). `run_iteration(context)` asserts that `context` survives `json.dumps`, appends it to `contexts` (and `("qa", context["iteration"])` to the shared `call_log` if one was given), and returns the next result. An exhausted script raises `AssertionError("FakeQA script exhausted")`.
  - `final_report(request)` appends the request to `report_requests` and returns a deterministic `QAReport` dict built from it, without writing files. Its `unresolved` is the last iteration's open or disputed blocking findings, and `report_path` is `".qa/report.md"`.
- [X] T021 [P] [US1] Create `tests/schema_check.py`, a stdlib helper (its name does not match `test*.py`). `assert_matches_findings_schema(findings)` loads `specs/002-qa-feedback-loop-mcp/contracts/qa-findings.schema.json` and asserts:
  - the required keys, with no extras;
  - the types;
  - the `enum` values, including `$defs.constitution_rule`;
  - the `finding_id` pattern;
  - the four `allOf` conditional rules, as explicit checks.
- [X] T022 [P] [US1] Create `tests/test_test_runner.py` (offline). `parse_junit` cases:
  - pass, fail, error and skipped testcases map to `TestCaseResult` (`test_id` = `file::name`);
  - `details` is cut to 2,000 characters and ends with `[truncated]`.

  `run_pytest` with `subprocess` patched:
  - exit 0 and exit 1 give `loaded=True`;
  - exit 2, 3, 4 or 5 gives `loaded=False`;
  - a missing XML, an XML over 2 MB, or a QA test file with no cases gives `loaded=False`;
  - a timeout gives `timed_out=True`, `loaded=True`, `exit_code=None`, and `failed=1` when there is no XML.

  `pytest_command` includes `--network none`, `--read-only`, `--cap-drop ALL`, the `tests` `:ro` mount, the `/qa-out` mount, `-c /dev/null`, `--confcutdir=/work/tests` and `--junitxml=/qa-out/junit.xml`, and no `--env` other than `HOME`, `PYTHONIOENCODING` and `PYTHONDONTWRITEBYTECODE`.

  Add one `@requires_docker` test: a temp workspace with `calc.py` and `tests/test_calc.py` (one passing and one failing test) gives `passed=1`, `failed=1`, `loaded=True`.
- [X] T023 [P] [US1] Create `tests/test_qa_agent.py`. Interface tests: `interface_summary` lists:
  - the module docstring;
  - function and class signatures with the first docstring line;
  - public methods;
  - the `__main__` guard;
  - literal `add_argument(...)` calls;
  - `could not be parsed` for a syntax error.

  It must contain no statement from any function body (assert that a unique marker string inside a body is absent).

  Iteration-1 tests use a fake Anthropic client (scripted `WRITE_TESTS_FORMAT` then `REVIEW_FORMAT` replies), a fake `run_tests` and `FakeFileTools`:
  - **Schema and coverage**: findings pass `assert_matches_findings_schema`. A failing test the review missed gets a generated blocking `failing_test` finding.
  - **Severity**: a `style` finding with `rule=""` becomes `advisory`. A style finding citing `no-wildcard-imports` stays `blocking`.
  - **Verdict**: `complete` only when the suite loaded, nothing failed, and no finding is blocking.
  - **Prompt contents**: the `write_tests` prompt has the interface summary and every acceptance criterion but no function body (FR-102a).
  - **Malformed replies**: a test path outside `tests/` → `error`. A `criteria` entry not in the spec → `error`.
  - **Load repair**: `loaded=False` twice then `True` succeeds with two repairs. Three failures → `error`.
  - **Model failures**: a refusal or an `APIError` → `error`, and no exception is raised.
  - **Timeout**: `timed_out=True` → a blocking finding with `test_ids=["<timeout>"]`.
- [X] T024 [US1] Create `tests/test_endpoints.py` with QA endpoint tests using a `FakeQA` behind `qa_handler`:
  - `qa_card(version)` has the required fields, `BINDING_URN`, all capabilities `False`, and skills `review-program` and `final-report` with their tags.
  - A `review-program` message whose `data` part holds a `QAContext` dict makes `FakeQA.run_iteration` receive that dict unchanged: equal to what was sent, `workspace` still a string, but a different object. It returns one artifact named `qa-iteration` whose one `data` part equals `result.to_dict()`.
  - A QA result with `error` set raises `SkillFailed(error)`.
  - The `final-report` skill passes the `FinalReportRequest` dict to `FakeQA.final_report` unchanged, and returns a `qa-report` artifact.
  - An unknown `skillId`, or a `review-program` message without a `data` part → `SkillRejected`.
- [X] T025 [US1] Create `tests/test_team_loop.py` with US1 graph tests over a **real `A2ABroker`**, using the fake PM client, a fake Coder, and `FakeQA` registered through `register_team_agents`:
  - **Completed build**: exactly one `review-program` exchange (sender `coder`, recipient `qa-debugger`, `context_id == run_id`) whose task ID equals `iterations[0].review_task_id`, then one `final-report` exchange (sender `team`). The run ends with `status="completed"`, `end_reason="complete"` and a `qa_report`.
  - **QA failure**: a QA task `FAILED` → `status="error"`, `end_reason="qa_failed"`.
  - **Protocol failure**: a `REJECTED` task or an `A2ATimeoutError` → `end_reason="a2a_failed"`.
  - **Report failure**: a completed run whose `final-report` task `FAILED` keeps `status="completed"` and `end_reason="complete"`, has `qa_report=None`, and has `error` containing `final report missing`.
  - **Failed build task**: no exchanges at all, and `qa_report` is `None`.
  - **Clean-up**: the broker thread is stopped after `run_team` returns.

### Implementation for User Story 1

- [X] T026 [P] [US1] Implement `tools/test_runner.py` per [contracts/qa-agent-api.md](contracts/qa-agent-api.md):
  - `pytest_command(workspace, out_dir, name, image)`: reuse the security flags and limits from `tools.python_exec`. Pull the shared flag list into a helper in `python_exec.py` rather than duplicating it.
  - `parse_junit(xml_text, test_files)`: stdlib `xml.etree.ElementTree`. Read the file only if it is 2 MB or smaller.
  - `run_pytest(workspace, timeout_seconds, image=SANDBOX_IMAGE) -> TestRun`:
    - use a `tempfile.TemporaryDirectory()` as `/qa-out`;
    - `docker kill` the container on timeout;
    - keep the last 10,000 characters of output;
    - set `loaded` per research R7;
    - list the QA test files from `workspace/tests/**/test_*.py`.
- [X] T027 [US1] Implement the first part of `agents/qa_agent.py`:
  - `QAConfig` with `from_env()` (`QA_MODEL`, `QA_EFFORT`, `QA_TEST_TIMEOUT`, `QA_REPAIR_ATTEMPTS` 0–3, `QA_USE_FALLBACKS`, `CODER_SANDBOX_IMAGE`; `ValueError` on bad values).
  - The `CONSTITUTION_RULES` tuple, matching the schema enum.
  - `WRITE_TESTS_FORMAT`, `DISPUTES_FORMAT` and `REVIEW_FORMAT` as JSON-schema dicts (every object `additionalProperties: False`, all keys required, `""` for none).
  - `QA_SYSTEM_PROMPT` per the contract's prompt summary.
  - `build_qa_request(config, user_text, output_format)`: model, `MAX_TOKENS`, adaptive thinking, `output_config={effort, format}`, and `betas=[FALLBACK_BETA]` with `fallbacks="default"` when enabled, reusing `agents/llm.py`.

  The module must not import `agents.coder_agent` (SC-110).
- [X] T028 [US1] Implement `interface_summary(sources: dict[str, str]) -> str` in `agents/qa_agent.py` with stdlib `ast`, per research R9. Use `ast.get_source_segment` only for signatures and `add_argument` calls, never for bodies.
- [X] T029 [US1] Implement `normalise_findings(raw, test_run, iteration, previous)` and `verdict_for(test_run, findings, suite_ok)` in `agents/qa_agent.py` per research R10:
  - assign `F<k>-<n>` IDs, reusing a `same_as` ID;
  - force `failing_test`/`bug` to `blocking`;
  - make style findings advisory unless their rule is in `CONSTITUTION_RULES`;
  - generate findings for uncovered failed or errored tests and for timeouts;
  - order blocking findings before advisory ones;
  - set `state="open"` and `first_seen`.
- [X] T030 [US1] Implement `QAAgent` in `agents/qa_agent.py`:
  - `__init__(config=None, client=None, run_tests=run_pytest, file_tools=McpFileTools)` calls `ensure_sandbox`.
  - `run_iteration(context: QAContext)` for iteration 1, per research R8. `context` is the JSON-only `TypedDict` from `orchestration/state.py`; convert `context["workspace"]` to a `Path` here.
    1. Open one `file_tools(workspace)` session.
    2. Delete existing files under `tests/`.
    3. Read the source files through MCP and build the interface summary.
    4. `write_tests` call; validate paths (under `tests/`) and `criteria` (in the spec).
    5. Write the files through MCP, then `run_tests`.
    6. While the suite has not loaded, call `repair_tests` up to `repair_attempts` times.
    7. `review` call with the full source and tests.
    8. `normalise_findings`, then write regression tests as `TestChange(reason="regression")`.
  - Return a `QAIterationResult` dataclass (fields as in qa-agent-api.md) with the updated `test_suite`, and a `to_dict()` that returns plain JSON.
  - Turn model, refusal, malformed-reply and `McpUnavailableError` failures into `error`, never raising.
- [X] T031 [US1] Implement `QAAgent.final_report(request: FinalReportRequest) -> QAReport` in `agents/qa_agent.py`, where `request` is the `TypedDict` from `orchestration/state.py` (fields in qa-agent-api.md). It is deterministic, per research R14. Render the Markdown (heading with the result and end reason, counts, Unresolved findings, Advisory, per-iteration lines) and write it to `.qa/report.md` through one `file_tools(workspace)` session. An MCP failure sets `report_path` to `""` and does not raise.
- [X] T032 [US1] Create `messaging/endpoints.py` per [contracts/a2a-binding.md](contracts/a2a-binding.md) §7, §8 and §10:
  - `qa_card(version=__version__)`, importing `__version__` from `messaging`.
  - `qa_handler(qa) -> SkillHandler`. It dispatches on `message["metadata"]["skillId"]`:
    - `review-program`: pass the `data` part through as the `QAContext` dict, after checking that it is a JSON object with an `iteration` and a `workspace`. Then call `qa.run_iteration(context)`, `SkillFailed(result.error)` if one was set, otherwise `[artifact("qa-iteration", [data_part(result.to_dict())])]`;
    - `final-report`: pass the `data` part through as the `FinalReportRequest`, then `[artifact("qa-report", [data_part(qa.final_report(request))])]`;
    - anything else, or a missing or invalid `data` part: `SkillRejected`.
  - `register_team_agents(broker, coder, qa, version=__version__)`, which registers the QA endpoint now. The Coder is added in T045.
  - The module uses duck typing and does not import `agents.*`.
- [X] T033 [US1] Wire QA into `orchestration/team.py` through A2A:
  - **`run_team`**: gets `qa=None` (defaults to `QAAgent()`) and `a2a_timeout_seconds=1800.0`. It opens `with A2ABroker(a2a_timeout_seconds) as broker:`, calls `register_team_agents(broker, coder, qa)`, builds `clients = {n: broker.client(n) for n in ("coder", "qa-debugger", "team")}`, and passes them to `build_team_graph(pm, coder, clients)`.
  - **Message builders** (pure functions): `review_message(state)`, whose data part is a `QAContext` built from the state, and `report_message(state, status, end_reason)`, whose data part is a `FinalReportRequest`. Both use the `messaging.a2a` builders with `contextId=run_id` and `metadata={"skillId", "sender"}`.
  - **`qa_node(state, clients)`**: `clients["coder"].send_message("qa-debugger", review_message(state))`. It parses the `qa-iteration` artifact into a new `Iteration` with `review_task_id`, and sets `test_suite`, `usage` and `a2a_log = clients["coder"].exchanges()`. A `FAILED` task → `qa_failed`. `REJECTED`, `A2AError` or `A2ATimeoutError` → `a2a_failed`.
  - **`finish_node(state, clients)`**: first decide `status` and `end_reason` from the last verdict. Then, when `iterations` is non-empty, send `final-report` from `team` with that status and end reason, and set `qa_report` and `a2a_log = clients["team"].exchanges()`. If the report exchange fails, leave `qa_report` as `None` and append `"final report missing: <cause>"` to `error` (joined with `"; "`), without changing `status` or `end_reason`.
  - **Routing**: `route_after_coder` returns `"qa"` when all tasks completed. `route_after_qa` returns `"finish"` (the loop comes in US2).
  - **Progress lines**: add `format_update` lines for `[a2a]`, `[qa]` and `[finish]` (with the report path), per [contracts/feedback-loop.md](contracts/feedback-loop.md).
  - **CLI**: builds `QAAgent(QAConfig.from_env())` and reads `A2A_REQUEST_TIMEOUT` (a float > 0; return 2 otherwise).
- [X] T034 [US1] Update `tests/test_team_handoff.py` and `tests/test_team_record.py` wherever they assumed a completed build goes straight to `finish`, passing `qa=FakeQA(...)`. Their runs now go through the real broker.

**Checkpoint**: US1 works on its own. T021–T025 pass, along with quickstart scenarios 1 and 9.

---

## Phase 4: User Story 2 - Coder ⇄ QA Feedback Loop Until Tests Pass (Priority: P2)

**Goal**: The Coder reflects (a critique, then a revision) before each review. After a `fix_needed` verdict, the QA side sends a `fix-program` A2A message carrying the fix instruction list to `coder`. The Coder answers each finding and returns a `coder-fix` artifact. The loop repeats until QA says `complete` or `TEAM_MAX_ITERATIONS` (default 5, 1–10) is reached, and then QA's final report lists the unresolved issues.

**Independent Test**: Over a real broker with fakes:
- A run that passes at iteration 3 has 3 `review-program` and 2 `fix-program` exchanges and 3 reflections, and ends `completed`. Each fix's `reference_task_ids` names the review before it.
- A run that never passes with a maximum of 5 has exactly 5 reviews and 4 fixes, and ends `failed` with `end_reason="iteration_limit"` and a non-empty `qa_report.unresolved`.
- The Coder and QA modules never import each other.

### Tests for User Story 2 ⚠️

- [X] T035 [P] [US2] Create `tests/test_coder_reflection.py` with a fake Anthropic client and `FakeFileTools`:
  - **Critique request**: `reflect` makes a critique request with no `tools` and `output_config.format == CRITIQUE_FORMAT`, containing every non-test source file in full.
  - **Numbering**: items are numbered `C1…Cn`.
  - **Empty critique**: retried once, then status `error`.
  - **Revision**: a `run()` whose task lists every `C…` ID. The returned `result` is the revision's (not the first draft).
  - **Rounds**: `reflection_rounds=2` → two critiques. A revision with `step_limit` still returns and the next round runs. A refusal stops with `refused`.
  - **Config**: `CODER_REFLECTION_ROUNDS` values `0`, `4` or `x` → `ValueError`.
  - **Responses**: `finding_responses` is parsed into `CoderResult`, and IDs not in `answer_ids` are dropped.
- [X] T036 [P] [US2] Add iteration ≥ 2 tests to `tests/test_qa_agent.py`:
  - **Tampering**: a test file changed on disk is restored, giving `TestChange(reason="restored")` and one blocking finding.
  - **Dispute kept**: the finding stays `open` under the same ID, and a `Dispute` is recorded.
  - **Dispute corrected**: the finding is `withdrawn`, the corrected file is written through MCP, and a `Dispute` is recorded.
  - **Fixed**: a previous open finding not reported again is marked `fixed`.
  - **Same problem**: `same_as` reuses the earlier ID and keeps `first_seen`.
  - **Review prompt**: it contains the previous findings and the Coder's responses.
  - **Regression test**: recorded with its `finding_id`, and the verdict for that iteration is `fix_needed`.
- [X] T037 [P] [US2] Add Coder endpoint tests to `tests/test_endpoints.py`, using a `FakeCoder` behind `coder_handler`:
  - `coder_card(version)` has the required fields and the `fix-program` skill.
  - A `fix-program` message (text = the brief; data = a `FixRequest`, `{"workspace", "answer_ids", "findings", "iteration"}`) calls `coder.run(brief, Path(workspace), answer_ids=[...])` and returns one `coder-fix` artifact holding a `CoderFix`.
  - A `step_limit` result → `COMPLETED` with `hit_step_cap=True`. A `refused` or `error` result → `SkillFailed`.
- [X] T038 [P] [US2] Add loop tests to `tests/test_team_loop.py` over a real `A2ABroker`, with `FakeCoder.reflect` and `FakeQA` verdict scripts:
  - **Success at iteration 3**: 3 QA reviews, 2 fix rounds and 3 reflections, each reflection before its review (check the shared call log).
  - **A2A record** (SC-109): `review-program` exchanges are all `coder → qa-debugger` and their count equals `len(iterations)`. `fix-program` exchanges are all `qa-debugger → coder`, and each one's `reference_task_ids == [iterations[k].review_task_id]`. All exchanges share `context_id == run_id`.
  - **Never passes, maximum 5**: exactly 5 reviews and 4 fixes, `status="failed"`, `end_reason="iteration_limit"`, `error` containing `Iteration limit reached (5 of 5)`, a non-empty `qa_report.unresolved`, and `len(iterations) <= max_iterations`.
  - **Maximum 1**: one review and no `fix-program` exchange.
  - **Fix round step limit**: the loop continues, with `hit_step_cap=True`.
  - **Fix task failures**: `FAILED` → `end_reason="coder_failed"`. `REJECTED` → `a2a_failed`.
  - **Reflection error**: `end_reason="reflection_failed"`.
  - **`max_iterations_from_env`**: unset → 5, `"3"` → 3, `"0"`/`"11"`/`"x"` → `ValueError`.
  - **`run_team(max_iterations=0)`**: raises before the fake PM is called.
  - **CLI**: `--max-iterations 3` overrides `TEAM_MAX_ITERATIONS=2`, and `--max-iterations 11` returns 2.
  - **Fix brief**: `render_fix_brief` lists every blocking finding's ID, problem, expected, likely cause, fix instruction and failing tests.
- [X] T039 [P] [US2] Create `tests/test_agent_isolation.py` (SC-110):
  - Parse `agents/coder_agent.py` and `agents/qa_agent.py` with `ast`, and assert that neither imports the other (no `import`/`from` of `agents.qa_agent` or `agents.coder_agent` respectively).
  - Assert that `messaging/endpoints.py` imports no `agents.*` module.
  - Assert, with `inspect.signature`, that `orchestration.team.qa_node`, `fix_node` and `finish_node` take `clients`, not `qa` or `coder`.
- [X] T040 [P] [US2] Add a test to `tests/test_tools.py`: `sandbox_command` includes `--volume <ws>/tests:/work/tests:ro` when `tests/` exists, and leaves it out otherwise.

### Implementation for User Story 2

- [X] T041 [US2] Add finding responses to `agents/coder_agent.py` per [contracts/coder-reflection.md](contracts/coder-reflection.md):
  - add the required `finding_responses` array to `SUMMARY_FORMAT`;
  - extend `SUMMARY_PROMPT`;
  - add `CoderResult.finding_responses: list[dict]`;
  - add an `answer_ids: Sequence[str] = ()` parameter to `run()`, and drop responses whose `finding_id` is not in `answer_ids`.

  The module must not import `agents.qa_agent`.
- [X] T042 [US2] Implement self-reflection in `agents/coder_agent.py`:
  - **Config**: `CoderConfig.reflection_rounds` (default 1, 1–3, env `CODER_REFLECTION_ROUNDS`).
  - **Constants and types**: `CRITIQUE_SYSTEM_PROMPT`, `CRITIQUE_FORMAT` (items with `minItems: 1`, plus `overall`), and a `ReflectionResult` dataclass.
  - **Helpers**: `render_revision_task(brief, items)`, and `_critique(brief, sources)`, a tool-less `beta.messages.create` with adaptive thinking, effort and fallbacks, retried once if empty.
  - **`reflect(brief, workspace)`**:
    - in each round, read the non-test sources through a `file_tools(workspace, protect=("tests", ".qa"))` session, critique, then `self.run(revision_task, workspace, answer_ids=[C ids])`;
    - stop on `refused` or `error`;
    - sum `usage`.
  - **CLI**: `main` runs `reflect(task, workspace)` after `run()` and prints the combined JSON.
- [X] T043 [P] [US2] In `tools/python_exec.py`, make `sandbox_command` add `--volume <ws>/tests:/work/tests:ro` after the workspace mount when `<ws>/tests` is a directory (research R5, layer 2).
- [X] T044 [US2] Extend `QAAgent.run_iteration` in `agents/qa_agent.py` for iteration ≥ 2:
  - **Suite check**: compare the files on disk with `context.test_suite`. Restore any difference through MCP, adding a blocking `bug` finding that lists the paths.
  - **Disputes**: when any response is `disputed`, make a `resolve_disputes` call with `DISPUTES_FORMAT`. Validate the corrected file paths and criteria, and record a `Dispute(decided_in=k)` for each.
  - **Review prompt**: include `previous_findings` and `responses`.
  - **Lifecycle**: after `normalise_findings`, apply it (`fixed`, `withdrawn`, kept `open`), and return the updated `test_suite`.
- [X] T045 [US2] Extend `messaging/endpoints.py`:
  - `coder_card(version=__version__)`.
  - `coder_handler(coder)`. For `fix-program` it takes the text part as the brief and the data part (a `FixRequest`) for `workspace`, `answer_ids` and `iteration`, then calls `coder.run(brief, Path(workspace), answer_ids=answer_ids)`.
    - On `refused` or `error`: raise `SkillFailed` with the status and error.
    - Otherwise: return `[artifact("coder-fix", [data_part(CoderFix)])]`, where the `CoderFix` holds `status`, `hit_step_cap`, `steps_used`, `files_changed`, `responses` and `error`.
    - Any other skill: `SkillRejected`.
  - `register_team_agents` now also registers the Coder endpoint.
- [X] T046 [US2] Add the loop to `orchestration/team.py` per [contracts/feedback-loop.md](contracts/feedback-loop.md):
  - **Constants**: `MAX_ITERATIONS_CAP = 10`, `DEFAULT_MAX_ITERATIONS = 5`, and `max_iterations_from_env(env=os.environ)`.
  - **Briefs and messages**: `render_reflection_brief(state)`, `render_fix_brief(state)` (exact layout from the contract), and `fix_message(state)`. The fix message has text = the fix brief, data = a `FixRequest`, `{"workspace", "answer_ids": [blocking IDs], "findings", "iteration"}`, `referenceTaskIds=[iterations[-1].review_task_id]` and `metadata={"skillId": "fix-program", "sender": "qa-debugger"}`.
  - **`reflect_node(state, coder)`**: a direct Coder call. It sets `pending_reflections`. On `refused` or `error`: `reflection_failed`.
  - **`fix_node(state, clients)`**: `clients["qa-debugger"].send_message("coder", fix_message(state))`. It sets `iterations[-1]["fix"]` and `fix_task_id` on a new list, plus `a2a_log = clients["qa-debugger"].exchanges()`. `FAILED` → `coder_failed`. `REJECTED`, `A2AError` or a timeout → `a2a_failed`.
  - **`qa_node`**: moves `pending_reflections` into the new `Iteration`.
  - **Routers**: `route_after_coder` → `"reflect"`. Add `route_after_reflect`, `route_after_qa` (`"fix"` only when running, verdict `fix_needed` and `len(iterations) < max_iterations`) and `route_after_fix`.
  - **`finish_node`**: sets `end_reason="iteration_limit"` with the error message from the contract.
  - **Wiring**: `RECURSION_LIMIT = 2 * MAX_TASKS + 3 * MAX_ITERATIONS_CAP + 6`, and update `build_team_graph` edges.
- [X] T047 [US2] Add the iteration limit to `run_team` and the CLI in `orchestration/team.py`:
  - `run_team(..., max_iterations=DEFAULT_MAX_ITERATIONS)` raises `ValueError` when the value is outside 1–10, before building state or the broker.
  - The CLI adds `--max-iterations N`. It reads `max_iterations_from_env()` when the flag is absent, and returns 2 with a range message on `ValueError`.
  - `format_update` gets the `reflect` and `fix` lines, and the `[a2a]` line for `fix-program`, and updates the `qa` line (`→ complete` or `→ N blocking, M advisory`).
  - The final JSON includes `iterations`, `qa_report`, `end_reason` and `a2a_log`.
- [X] T048 [US2] Extend `tests/team_fakes.py`:
  - `FakeCoder.reflect(brief, workspace)` returns a scripted `ReflectionResult`, and `run()` accepts `answer_ids` and scripts fix outcomes.
  - `FakeQA` gets helpers to script `fix_needed`/`complete` sequences.
  - Add a shared ordered call log across both fakes so T038 can check that reflection runs before review.

**Checkpoint**: US1 and US2 work. Quickstart scenarios 1, 6, 7 and 9 behave as documented.

---

## Phase 5: User Story 3 - File Tools Served Over MCP (Priority: P3)

**Goal**: A developer can start `python -m tools.mcp_server --workspace DIR` on its own, connect any standard MCP client, and get exactly `read_file` and `write_file` under the same rules as the agents. Bad input never stops the server, and a missing workspace is refused at start-up.

**Independent Test**: The contract tests in `tests/test_mcp_server.py` pass against the real server subprocess. Quickstart scenarios 2 and 3 behave as documented.

### Tests for User Story 3 ⚠️

- [X] T049 [P] [US3] Extend `tests/test_mcp_server.py` with the full contract from [contracts/mcp-file-server.md](contracts/mcp-file-server.md):
  - **Tool list**: the listed `input_schema`s equal `READ_FILE_TOOL["input_schema"]` and `WRITE_FILE_TOOL["input_schema"]`. `definitions()` entries have `strict: True`.
  - **Call behavior** (one test per row): a file over 200 KB is truncated with the marker; a missing file; an absolute path; a `..` escape; a symlink out of the workspace; a protected write; a missing argument, wrong type or extra key → `Invalid arguments for <tool>: …`; an unknown tool → `Unknown tool: <name>`.
  - **Keeps serving**: a valid call after each error succeeds.
  - **Start-up refusals**: `subprocess.run([sys.executable, "-m", "tools.mcp_server", "--workspace", <missing>])` exits 2 with `error: workspace does not exist`. `--protect /abs` and `--protect ../x` exit 2.
  - **Client failures**: `McpFileTools` on a missing workspace raises `McpUnavailableError`.
  - **Environment**: `server_parameters().env` keys are exactly `{"PATH", "PYTHONIOENCODING"}` (FR-125).

### Implementation for User Story 3

- [X] T050 [US3] Harden `tools/mcp_server.py` for outside clients:
  - **Start-up checks**: before serving, check `--workspace` (it must exist and be a directory; otherwise print `error: workspace does not exist: DIR` to stderr and exit 2). Check each `--protect` (not absolute, stays in the workspace; otherwise stderr plus exit 2).
  - **Argument validation in the handler** (SDK v2 does not do it): required keys, string types and no extra keys, giving `Invalid arguments for <tool>: <reason>`. Return `Unknown tool: <name>` for any other name.
  - **Logging**: send diagnostics to stderr only, never to stdout, which carries the protocol.
- [X] T051 [US3] Write "MCP integration: what and why" in `README.md`, following the plan's README section:
  - what is exposed and what is not, and why;
  - why the low-level server and stdio;
  - one server per session with Coder-only protection;
  - no secrets passed to it;
  - the CLI usage;
  - the `mcpServers` JSON snippet for connecting an outside client.

**Checkpoint**: All three stories work on their own.

---

## Phase 6: Benchmark for SC-102 and SC-103

**Purpose**: Provide the fixed 10-requirement benchmark that SC-102 and SC-103 are measured on (001 Assumption "Benchmark"), plus a runner that scores it. The design is in [contracts/benchmark.md](contracts/benchmark.md). This phase needs US1 and US2 (a full team run with the loop). It is not part of any single story, because it measures the whole team.

- [X] T052 [P] Create `tests/test_benchmark.py` (offline: no API key, no Docker), testing [contracts/benchmark.md](contracts/benchmark.md):
  - **The real file**: `load_benchmark()` on `benchmarks/requirements.json` returns exactly the 10 ids from §2, in order.
  - **Validation** (temp files): 9 or 11 entries, a duplicate id, a non-kebab-case id, an empty `requirement`, or a missing `version` → `ValueError`.
  - **SC-102 scoring**: met at 8 of 10 succeeded, not met at 7.
  - **SC-103 scoring**: 7 of 10 first-draft failures succeeding → met (0.70), 6 of 10 → not met. No first-draft failures → `"n/a"` and met.
  - **`record_for`**: `first_draft_failed` is `None` when QA never ran, `True` for `loaded=False`, and `True` for `failed + errors > 0`. An item that raised gets `status="error"` and the error text.
  - **`main(argv, run_team=fake)`**: the fake returns scripted final states.
    - The results file has the fingerprint, 10 records and the summary.
    - `--only temp-convert,stats` runs 2 items and marks SC-102 and SC-103 `"not evaluated (partial run)"` with exit code 1.
    - A raising item is recorded and the run continues.
    - Exit code 0 when both criteria are met, 1 when not.
    - An earlier results file with a different fingerprint triggers the "not comparable" warning.
    - Each item gets a fresh Chroma folder (assert the paths differ).
- [X] T053 [P] Create `benchmarks/__init__.py` (a one-line docstring: "Fixed benchmark set and runner for SC-102 and SC-103.") and `benchmarks/requirements.json` with `"version": 1` and the 10 requirements from [contracts/benchmark.md](contracts/benchmark.md) §2, copied word for word, in the same order. Create `benchmarks/results/.gitkeep`, and make sure `.gitignore` does not exclude `benchmarks/results/`, because results are committed so evaluations can be compared.
- [X] T054 Implement `benchmarks/run_benchmark.py` per [contracts/benchmark.md](contracts/benchmark.md) §3–§6, using only the stdlib plus the project's own modules:
  - **Functions**: `load_benchmark(path)`, `fingerprint(path)` (sha256 of the file bytes), `record_for(entry, final, error, duration)`, `score(records, partial)`, and `main(argv=None, run_team=run_team) -> int` with argparse (`--only`, `--max-iterations`, `--out`).
  - **Pre-flight**: check credentials and Docker once (`ensure_sandbox()`). Exit 2 if either is missing.
  - **Each item**: build new `PMAgent(PMConfig.from_env())`, `CoderAgent(replace(CoderConfig.from_env(), chroma_path=<fresh tempfile.mkdtemp()>))` and `QAAgent(QAConfig.from_env())`. Call `run_team(requirement, pm, coder, qa, workspace_root=Path("workspace") / f"benchmark-{session}", max_iterations=...)`, timing it with `time.monotonic()`. Catch exceptions into `error`.
  - **Output**: write `benchmarks/results/<UTC timestamp>.json`, print the summary table and any "not comparable" warning to stderr, and return the exit code from §6.
  - **Entry point**: an `if __name__ == "__main__": sys.exit(main())` guard.

**Checkpoint**: `python -m unittest tests.test_benchmark` passes offline. `python -m benchmarks.run_benchmark --only temp-convert` performs one live run and writes a results file (quickstart scenario 11).

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: The rest of the documentation, a compliance pass, and end-to-end validation

- [X] T055 Update the rest of `README.md`:
  - The status becomes "Increment 3: the full three-agent team".
  - Replace the system diagram with the mermaid diagram and table from `specs/002-qa-feedback-loop-mcp/plan.md` (System Diagram, including the A2A broker).
  - Add a section, "Agent-to-agent communication (A2A)", following the plan's README bullets: why A2A, the asyncio broker, the binding URN and what it does not support, the two agent cards and their skills, reading `a2a_log`, and `A2A_REQUEST_TIMEOUT`.
  - Add subsections on the QA agent, the feedback loop and its safeguards, and Coder self-reflection.
  - Add `TEAM_MAX_ITERATIONS`, `CODER_REFLECTION_ROUNDS`, `QA_MODEL`, `QA_EFFORT`, `QA_TEST_TIMEOUT`, `QA_REPAIR_ATTEMPTS`, `QA_USE_FALLBACKS` and `A2A_REQUEST_TIMEOUT` to the configuration table.
  - Mention the first-run sandbox image build, and list the new test modules.
  - Add a "Benchmark" section: what the 10 requirements are for (SC-102, SC-103), how to run `python -m benchmarks.run_benchmark` (and its cost: 10 live team runs), where results go, and the rule that the set stays fixed between evaluations.
- [X] T056 [P] Do a constitution compliance pass over `agents/qa_agent.py`, `agents/coder_agent.py`, `tools/mcp_server.py`, `tools/mcp_client.py`, `tools/test_runner.py`, `messaging/a2a.py`, `messaging/broker.py`, `messaging/endpoints.py`, `orchestration/team.py` and `benchmarks/run_benchmark.py`:
  - split any function over about 40 lines or deeper than 3 levels of nesting (for example, the broker's dispatch-by-method and worker loop);
  - type hints and PEP 257 docstrings on every public function and class;
  - grouped imports, no bare `except`, no commented-out code;
  - no logic duplicated three or more times (for example, the shared docker flags, or the JSON round-trip copy helper).
- [X] T057 Run `python -m unittest discover -s tests -t .` with and without Docker available. Fix any failure.
- [ ] T058 Run the scenarios in `specs/002-qa-feedback-loop-mcp/quickstart.md`: 1, 2, 3, 7 and 9 without credentials, then 4, 5, 6, 8, 10 and 11 with `ANTHROPIC_API_KEY`. Scenario 11 is the full benchmark: ten live runs, so expect real model spend. Confirm each expected outcome, in particular SC-104 (scenario 8) and SC-109 (scenario 10). Confirm SC-108 by reading the JSON records from scenarios 5 and 6: every iteration has findings with fix instructions, the Coder's `responses` and `test_run`, and scenario 6 has `end_reason`. Fix any gap, or record it under Notes below.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies. T001 blocks T010–T012 (the `mcp` package). T002 blocks T008. T004 (`messaging.__version__`) blocks T010 and T016–T019.
- **Foundational (Phase 2)**: depends on Setup and **blocks every story**. Four chains run independently:
  - state: T005 → T006 → T007;
  - sandbox: T008 → T009;
  - MCP: T010 → T011 → T012, and T011 → T013 → T014 → T015;
  - A2A: T016 and T017 (tests first) → T018 → T019.
- **US1 (Phase 3)**: depends on Foundational.
- **US2 (Phase 4)**: depends on Foundational and on US1's QA agent, endpoint and wiring (T030, T032, T033, T034), because the loop wraps the A2A review.
- **US3 (Phase 5)**: depends only on the MCP part of Foundational (T010–T015). It can run alongside US1 and US2.
- **Benchmark (Phase 6)**: T052 and T053 can start any time after Setup, since the tests use a fake `run_team`. T054 needs US1 and US2 (`run_team` with QA and the loop) and T053 (the file).
- **Polish (Phase 7)**: depends on all three stories and on the benchmark phase.

### Within Each Story

- Test tasks (T020, T021–T025, T035–T040, T049) are written first and fail before implementation.
- US1: T020 (`FakeQA`) comes first, because T024 and T025 import it. Then T026 and T027 → T028 → T029 → T030 → T031. Then T032 (endpoint), then T033 (team wiring), then T034.
- US2: T041 → T042, then T045 (needs `answer_ids`). T043 is independent. T044 depends on T030. T046 → T047. T048 must come before the T038 tests can pass.
- US3: T050 is independent of US1 and US2. T051 depends on T050 for the exact CLI behavior.

### Parallel Opportunities

- Setup: T002, T003 and T004 run in parallel with T001.
- Foundational: the four chains (state T005, sandbox T008, MCP T010, A2A T016/T017 → T018) touch different files and can start together.
- US1 tests: T020, T021, T022 and T023 are in different files and can run in parallel. T024 and T025 can run in parallel with each other once T020 is done, because they import `FakeQA`. T026 (`tools/test_runner.py`) runs in parallel with T027–T029 (`agents/qa_agent.py`).
- US2 tests T035–T040 are in different files and can run in parallel. T043 (`tools/python_exec.py`) runs in parallel with T041/T042 (`agents/coder_agent.py`) and T044 (`agents/qa_agent.py`).
- US3 (T049, T050) can be built alongside US1/US2 by another developer.

---

## Parallel Example: Foundational A2A layer

```bash
Task: "T016 A2A 1.0 shape/builder/validator/card tests in tests/test_a2a.py"
Task: "T017 broker routing/FIFO/isolation/errors/timeout tests in tests/test_broker.py"
# then, in order:
Task: "T018 A2A types, builders, validators, errors in messaging/a2a.py"
Task: "T019 A2ABroker + A2AClient in messaging/broker.py"
```

## Parallel Example: User Story 1

```bash
# Tests first, all in different files:
Task: "T020 self-contained FakeQA + qa_result in tests/team_fakes.py"
Task: "T021 stdlib findings-schema checker in tests/schema_check.py"
Task: "T022 JUnit parsing / pytest command tests in tests/test_test_runner.py"
Task: "T023 interface + iteration-1 QA tests in tests/test_qa_agent.py"
Task: "T024 QA endpoint tests in tests/test_endpoints.py"
Task: "T025 build → review-program → final-report graph tests in tests/test_team_loop.py"

# Then two implementation tracks in parallel:
Task: "T026 run_pytest / parse_junit in tools/test_runner.py"
Task: "T027–T029 QAConfig, schemas, interface_summary, normalise_findings in agents/qa_agent.py"
```

## Parallel Example: User Story 2

```bash
Task: "T035 reflection tests in tests/test_coder_reflection.py"
Task: "T036 iteration ≥ 2 QA tests in tests/test_qa_agent.py"
Task: "T037 Coder endpoint tests in tests/test_endpoints.py"
Task: "T038 loop termination + SC-109 A2A record tests in tests/test_team_loop.py"
Task: "T039 agent isolation tests in tests/test_agent_isolation.py"
Task: "T040 read-only tests mount test in tests/test_tools.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1, Setup: install `mcp`, add the Dockerfile, amend the constitution.
2. Phase 2, Foundational: state types, pytest image, MCP server and client, Coder over MCP, A2A layer and broker.
3. Phase 3, US1: the QA agent behind its A2A endpoint. One `review-program` exchange per run, plus the final report.
4. **Stop and validate**: T021–T025 pass, plus quickstart scenarios 1 and 9. A real run gives a QA verdict, `.qa/report.md`, and two entries in `a2a_log`.

### Incremental Delivery

1. Setup + Foundational: the Coder works through MCP, the image has pytest, and the broker routes A2A messages.
2. US1: one A2A QA review per run (MVP).
3. US2: reflection, the `fix-program` exchange, the loop and the iteration limit (the full three-agent team, decoupled over A2A).
4. US3: hardening for outside MCP clients, and the README MCP section.
5. Benchmark: the fixed 10-requirement set and runner. One full run measures SC-102 and SC-103.
6. Polish: the full README (with the A2A and Benchmark sections), a compliance pass, and the quickstart end to end.

---

## Notes

- `[P]` means a different file and no dependency on an unfinished task. Tasks that share a file (`agents/qa_agent.py`, `agents/coder_agent.py`, `orchestration/team.py`, `messaging/endpoints.py`) run in order.
- Commit after each task or logical group. Code, README and constitution changes go in separate commits (constitution, Development Workflow).
- Never make the agents async. The broker and the MCP client are the only async code, each on its own background loop thread (research R3, R19).
- Never add a fallback that bypasses MCP or the A2A broker, and never pass Python objects between the Coder and QA (FR-129).
- Keep the Coder's tool list identical within a session (preserved thinking, R4).
- Two A2A 1.0 details are checked in T018 (the two error codes, and whether `ListTasks` is required). Update the contract when they are settled.
- **Implementation notes (recorded during /speckit-implement, 2026-10-19):**
  - **T021**: `specs/` is git-ignored, so the findings schema is copied to `tests/fixtures/qa-findings.schema.json`. A test checks that the copy matches the contract whenever `specs/` is present.
  - **T033**: `run_team` keeps its first five 001 parameters in position, and the new ones (`qa`, `max_iterations`, `a2a_timeout_seconds`) are keyword-only. The contract was updated.
  - **T054**: the benchmark runner also takes `make_agents` (an injectable agent builder), so the offline tests can check for a fresh memory store per item. The contract was updated.
  - **T056**: the server-side fallback request settings (used in four places) and token-usage summing (three places) were extracted into `agents/llm.py` (`with_fallbacks`, `add_tokens`). This touches the PM agent too.
  - **T057**: the Coder loop test classes that run code in the sandbox, and the live smoke test, are now skipped without Docker. In 001 they failed instead of skipping, so the suite only passed with Docker.
