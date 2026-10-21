# Research: QA & Debugger Agent, Coder ⇄ QA Feedback Loop, and MCP File Tools

**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md) | **Date**: 2026-10-18

The planning input fixed several choices: pytest tests, a sandbox for QA's code runs,
a Coder self-critique step, the official Python MCP SDK, and a maximum of 5 iterations
set by an environment variable. The decisions below settle the rest. Facts about the
`mcp` package come from its PyPI page and its v2 migration guide
(py.sdk.modelcontextprotocol.io/migration), read on 2026-10-18.

---

## R1. MCP SDK version and API layer

**Decision**: Pin `mcp==2.3.0`. Build the server on the SDK's **low-level** `Server`
(`from mcp.server import Server`), passing the handlers as constructor arguments
(`on_list_tools=`, `on_call_tool=`), and run it with `mcp.server.stdio.stdio_server()`.
Use `ClientSession` with `mcp.client.stdio.stdio_client` on the client side. Do not use
the high-level `MCPServer`.

**Rationale**:
- 2.3.0 is the current release (2026-10-02). It is MIT-licensed and needs Python 3.10 or
  later. It is the current major version, so new code is written against the API the SDK
  will keep, not the v1 API the migration guide replaces. Its requirements fit the
  existing pins (R16). The HTTP library each version uses does not decide the choice:
  the project already has both `httpx` (from `chromadb`) and `httpx2` (from `anthropic`),
  and the stdio transport uses neither.
- The low-level server publishes each tool's `input_schema` exactly as written. The
  existing `READ_FILE_TOOL` and `WRITE_FILE_TOOL` schemas are strict
  (`additionalProperties: false`), and the Coder sends them to Claude with
  `"strict": true`. `MCPServer` generates schemas from type hints, adding `title` keys
  and leaving out `additionalProperties`, so the client would have to rewrite them.
- In v2, a low-level handler builds its own `CallToolResult(is_error=..., content=[TextContent(...)])`.
  That lets the server return the existing tool layer's error text word for word, such as
  `File not found: x.py` (FR-120). In v2, an exception raised in a handler becomes a
  JSON-RPC error, not an error result the model can read, so handlers never raise.
- v2's low-level server no longer validates arguments against `input_schema`. The
  handler does this itself: `path` is required and must be a string, `content` likewise
  for `write_file`, and no other keys are allowed. A malformed call gets an
  `is_error` result and the server keeps running (FR-122).
- v2 attribute names are snake_case (`is_error`, `input_schema`, `structured_content`).
  The wire format stays camelCase.

**Alternatives considered**:
- `mcp` 1.30.x with decorator handlers: the older major version, which v2's migration
  guide replaces. v1 also turned exceptions into error results automatically. Rejected
  in favour of the current major version.
- `MCPServer` with `ToolError`: needs schema rewriting on the client, and it frames
  error text itself.
- The Anthropic SDK's MCP helpers (`anthropic.lib.tools.mcp`, `anthropic[mcp]`): they
  convert tools for the beta tool runner. The Coder has its own LangGraph ReAct loop, so
  they would add a second loop style and a beta extra for one dict conversion.
- The Messages API `mcp_servers` connector: only for remote URL servers, and the
  workspace lives on the user's machine.

## R2. Transport and process model

**Decision**: Use stdio. Each **agent session** starts one server subprocess, bound to
one workspace, and stops it when the session ends:

```
<sys.executable> -m tools.mcp_server --workspace <abs path> [--protect tests --protect .qa]
```

- A Coder `run()` (a build task, a fix round, or a reflection revision) starts a
  server with `--protect tests --protect .qa`.
- Each QA iteration, and the final report, starts a server with no protected paths.
- The subprocess's working directory is the repository root, so `-m tools.mcp_server`
  imports. It gets an **explicit** environment of `PATH` and `PYTHONIOENCODING=utf-8`
  only, so no API key or `.env` value reaches it (FR-125).
- The server checks `--workspace` before serving. A folder that is missing or not a
  directory makes it exit with status 2 and a one-line message on stderr (FR-124).

**Rationale**: stdio is the standard way to run a local MCP server, needs no ports, and
ends with its client (spec Assumption "MCP connection"). One server per session fits a
server tied to one workspace (FR-121). It also lets the Coder's and QA's servers differ
only in the protected paths, enforced on the server side. Startup costs about a second.
A run has at most about 30 sessions, which is small next to the model calls.

**Alternatives considered**: One long-lived server for the whole run, with a per-call
role flag: the role would come from the caller's arguments, so the Coder's model could
claim to be QA. Streamable HTTP: adds a port and a web server, and nothing here needs it.

## R3. A synchronous client over the async SDK

**Decision**: `tools/mcp_client.py` provides `McpFileTools`, a context manager. A
background thread runs one asyncio event loop. That loop enters `stdio_client(...)` and
`ClientSession(...)`, calls `initialize()` and `list_tools()`, then waits on a stop event.
The agents call it synchronously, `call(name, arguments) -> (text, is_error)`, which
submits `session.call_tool` to the loop with `asyncio.run_coroutine_threadsafe(...)`
and waits for the result with a timeout (default 30 s). The loop leaves the async
context managers in the same task it entered them in, which the SDK's anyio streams
require.

**Rationale**: The agents, the LangGraph graphs and the `anthropic` client are all
synchronous. Making them async would rewrite two working agents for one tool
transport. One loop thread per session is the smallest bridge that keeps the stdio
session open across many calls.

**Failure handling**: If the server cannot start, or `initialize()` or a call fails or
times out, the client raises `McpUnavailableError`. There is **no fallback** to direct
file calls; with one, nobody could tell whether FR-123 holds. The Coder ends that
session with `status="error"`. QA ends its turn with an error. The run ends as FR-116
describes. This settles the open point from `/speckit-clarify`.

## R4. Turning MCP tools into Claude tool definitions

**Decision**: When a session starts, `McpFileTools.definitions()` maps each listed tool
to `{"name", "description", "input_schema", "strict": True}`. `tools/__init__.py` adds
the required `purpose` property, as now. The model sees the MCP tools in server order
(`read_file`, `write_file`), then the local `exec_python`. `exec_python` stays in process
(FR-123) because it needs the sandbox settings and timeout of the run. The client checks
that exactly `{read_file, write_file}` were listed, and fails the session otherwise.

**Rationale**: The model's tool definitions now come from MCP discovery, not from
constants imported into the agent. Because the server publishes the same schemas
(R1), what the model sees does not change, and nor does any test about it. The tool list
is fixed for the whole session, so the history stays append-only, which preserved
thinking on Sonnet 5.5 requires (001 research R3).

## R5. Protecting test files from the Coder (FR-111)

**Decision**: There are two layers, both enforced outside the model:

1. **MCP server**: `--protect <prefix>` (repeatable) makes `write_file` refuse any path
   that resolves inside a protected folder. The error reads `Path is protected: tests/x.py
   (only the QA Agent may change tests)`. Reads stay allowed (clarification 5).
2. **Sandbox mounts**: when a Coder `exec_python` run starts and `tests/` exists, that
   folder is mounted **read-only** over the read-write workspace
   (`--volume <ws>/tests:/work/tests:ro`). Code the Coder runs therefore cannot change the
   tests either.

As a backstop, QA keeps its own copy of the suite (path → content) in team state. Before
each test run it compares the files on disk with that copy. A difference becomes a
blocking finding ("test files were changed outside QA"), and the copy is written back.
At iteration 1, QA removes anything already in `tests/` before writing its suite.

**Rationale**: Layer 1 meets the FR-111 wording: the refusal is a tool error the Coder
can read. Layer 2 closes the obvious workaround of writing a Python file that edits the
tests and running it. The backstop makes tampering visible however it happens.

## R6. Sandbox image with pytest

**Decision**: Build one derived image, `multi-agent-sandbox:py3.12-pytest9.1.1`, from
`sandbox/Dockerfile`:

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG PYTEST_VERSION
RUN pip install --no-cache-dir "pytest==${PYTEST_VERSION}"
```

`BASE_IMAGE` (the current pinned `python:3.12-slim@sha256:a6e3…`) and `PYTEST_VERSION`
(`9.1.1`) live only in `tools/python_exec.py`, which passes them as `--build-arg`.
`ensure_sandbox()` builds the image when it is missing. This is the only step with
network access, it runs no generated code, and it uses the existing `_docker` helper with
a 600 s timeout. The Coder's `exec_python` and QA's test runs both use this image.
`CODER_SANDBOX_IMAGE` still overrides it.

**Rationale**: QA must run real pytest tests (planning input) in a sandbox with no
network (001 FR-021), so pytest has to be in the image before a run starts. A
derived image is the smallest way to do that. One image keeps one Python version
everywhere (constitution, Python Standards). pytest's dependencies (`iniconfig`,
`packaging`, `pluggy`, `pygments`) are pure Python. The image is built on the user's
machine, not pulled from a registry, so the tag names a build recipe, not a digest.

**Alternatives considered**: Installing pytest on the host and mounting it read-only
on `PYTHONPATH`: two copies of the packages to keep in step, and host packages
inside the sandbox. A per-run install step (001 FR-011a): still deferred, and too heavy
for a fixed test runner.

## R7. Running the tests and reading the results

**Decision**: `tools/test_runner.py` provides `run_pytest(workspace, timeout_seconds,
image) -> TestRun`. It uses the same locked-down `docker run` flags as `exec_python`, plus:

- `tests/` is mounted read-only.
- A fresh host temporary folder is mounted at `/qa-out` (read-write) for the report,
  so nothing is written into the delivered workspace.
- The command is:
  `python -s -m pytest tests -q -p no:cacheprovider -c /dev/null --rootdir=/work
  --confcutdir=/work/tests --junitxml=/qa-out/junit.xml -o junit_family=xunit2`

Notes on that command:
- `python -m` puts `/work` on `sys.path`, so tests import the program's modules.
- `-c /dev/null` and `--confcutdir` stop a `pytest.ini`, `pyproject.toml` or root
  `conftest.py` written by the Coder from changing how the tests run.
- The time limit is `QA_TEST_TIMEOUT`, 60 s by default (001 FR-021). A timeout marks the
  run as `timed_out`, and every test without a result counts as failed.

The JUnit XML is read with `xml.etree.ElementTree`, but only if it is 2 MB or smaller.
Python 3.12's bundled expat guards against entity-expansion attacks. Each `<testcase>`
becomes a `TestCaseResult` with an outcome of `passed`, `failed`, `error` or `skipped`,
plus the failure message and up to 2,000 characters of traceback.

pytest's exit code is checked against the XML:
- 0 means all passed.
- 1 means some failed.
- 2 (collection errors or an interrupted run), 3, 4, 5 (no tests collected), a missing
  or unreadable XML file, or a QA test file with no results all mean **the suite did not
  load** (`loaded=False`).

**Rationale**: JUnit XML is pytest's built-in, machine-readable result format, and it
needs no plugins. Checking the exit code against the XML catches a crashed or
interrupted run. The same checks give "a suite that never loads is not counted as
passing" (spec edge case).

## R8. How QA works: a fixed pipeline, not a ReAct agent

**Decision**: `agents/qa_agent.py` provides `QAAgent.run_iteration(context) ->
QAIterationResult`. Each step is a structured-output Claude call
(`output_config.format = json_schema`, as `pm_agent.py` does now) or a deterministic
step:

| Step | When | Kind |
|------|------|------|
| 1. Check the suite (R5 backstop) | every iteration from 2 on | code |
| 2. `write_tests`: spec + public interface → test files | iteration 1 | model |
| 3. `resolve_disputes`: disputes + spec + tests → keep/correct decisions, corrected files | iteration ≥ 2, only if the Coder disputed something | model |
| 4. Write the test files through MCP, then `run_pytest` | every iteration | code |
| 5. `repair_tests`: load error output → corrected files, then run again | only while `loaded=False`, at most `QA_REPAIR_ATTEMPTS` (2) times | model |
| 6. `review`: spec, full source (read through MCP), tests, results, earlier findings and the Coder's answers → findings + regression tests | every iteration | model |
| 7. Normalise the findings and compute the verdict (R10); write any regression tests | every iteration | code |

QA uses Claude Sonnet 5.5 (`claude-sonnet-5-5`) at effort `high`, with adaptive
thinking and server-side fallbacks (`fallbacks: "default"`). That matches the PM and the
Coder, and `QA_MODEL` and `QA_EFFORT` override it. A refusal, an API error, a reply
that fails its checks, or a suite still broken after the repair attempts ends QA's
turn with an error (FR-116).

**Rationale**: QA's job has a fixed shape: write, run, read, report. The planning input
asks for real test execution and structured output, not open-ended exploration. A fixed
pipeline keeps the number of model calls per iteration bounded (two to four), makes each
step testable with a fake client, and leaves running and parsing tests to code, which
the model cannot get wrong. QA still writes and reads every file through MCP (FR-123).

**Alternatives considered**: A second ReAct agent with tools. That would mean more
steps and cost, a looser test count, and an exit-code check the model could talk its way
round. It would also need its own step cap.

## R9. The public interface QA sees before writing tests (FR-102a)

**Decision**: `interface_summary(sources) -> str` uses stdlib `ast` on each non-test
`.py` file (read through MCP). It lists:
- the module docstring;
- the signatures of top-level functions and classes, with each first docstring line;
- each class's public methods;
- whether the file has an `if __name__ == "__main__":` guard;
- every `argparse` `add_argument(...)` call written as literal source.

It does **not** include function bodies. The Coder's task summaries and the main file's
path go alongside it. A file that does not parse is listed as `<path>: could not be
parsed (<error>)`.

**Rationale**: The rule "read only the interface before writing tests" is enforced by
what the prompt contains, not by asking the model to look away. `ast` is stdlib and
deterministic.

## R10. Findings and the fix instruction list

**Decision**: The `review` call returns `review.schema.json`
([contracts/qa-findings.schema.json](contracts/qa-findings.schema.json)). Code then
enforces the spec rules. The model's output is never trusted for them:

- **Failing tests are always covered**: each failed or errored test with no
  `failing_test` finding naming it gets a blocking finding generated from its JUnit
  message (FR-103, FR-105).
- **Severity rule** (FR-104a): `failing_test` and `bug` findings are always `blocking`.
  A `style` finding stays `blocking` only if its `rule` is one of the constitution's
  MUST rules. These form a fixed enum in the schema (`CONSTITUTION_RULES` in
  `agents/qa_agent.py`), such as `no-wildcard-imports`, `no-hardcoded-secrets` and
  `main-guard`. Any other style finding becomes `advisory`.
- **Hard-coded test values** (FR-111a) are a `bug` finding, which is blocking.
- **IDs and state**: each finding gets the ID `F<iteration>-<n>`. A finding the model
  marks as the same problem as an earlier one keeps the earlier ID. An earlier
  open finding not reported again becomes `fixed`. A finding whose dispute was decided
  `correct` becomes `withdrawn`.
- **Verdict**: `complete` only if `loaded`, `failed + errors == 0`, the suite check
  passed, and there are no blocking findings (FR-106). Otherwise `fix_needed`.

The **fix instruction list** for the Coder is the blocking findings in order, each with
`fix_instruction`, `file`, `location`, `expected` and `likely_cause`, followed by the
advisory findings. The list is plain data in state, and `render_fix_brief()` turns it
into the Coder's task text ([contracts/feedback-loop.md](contracts/feedback-loop.md)).

## R11. Coder self-reflection

**Decision**: Add `CoderAgent.reflect(brief, workspace) -> ReflectionResult`. Each round
has two parts:

1. **Critique**: one Claude call with no tools, and structured output
   `CRITIQUE_FORMAT` (`items`: `kind` bug | edge_case, `location`, `description`,
   `suggested_change`; `minItems: 1`). Its input is the brief (spec and acceptance
   criteria) and every non-test source file, read through MCP. If the reply has no
   items, the call is retried once. If it still has none, the round is an error.
2. **Revision**: a normal `self.run(revision_task, workspace)` ReAct session. The task
   lists the critique items and tells the Coder to address each one: change the code, or
   say why no change is needed. It answers through the summary's `finding_responses`,
   with IDs `C1`…`Cn` (R12). The revised program is the revision's output.

`CODER_REFLECTION_ROUNDS` sets the number of rounds: 1 by default, from 1 to 3 (FR-127).
The team runs a `reflect` node before **every** QA iteration, so after the build and
after each fix round (FR-126). The standalone `python -m agents.coder_agent` CLI also
reflects once after its run, so "the Coder always reflects at least once" holds there
too. Its JSON output gains a `reflection` key.

**Rationale**: A separate critique call (no tools, fresh context) gives a self-review
that is not biased by the conversation that wrote the code. Reusing `run()` for the
revision reuses the step cap, memory, MCP tools and summary. Making the critique list at
least one item, and always running the revision, meets "must produce a revised output"
without trusting the model to opt in.

## R12. How the Coder answers findings

**Decision**: Extend the Coder's `SUMMARY_FORMAT` with
`finding_responses: [{finding_id, action: fixed | disputed | not_addressed, reason}]`,
always present and empty for plain build tasks. `CoderResult` gains the same field. The
fix brief and the revision task both list the IDs to answer. The team copies the
responses into the iteration record. A `disputed` response becomes a `Dispute` that QA
decides in step 3 of the next iteration. A blocking finding with no response, or with
`not_addressed`, stays open (FR-116a).

**Rationale**: Disputes need structured answers (FR-110, FR-112). The summary call
already exists and already uses structured output, so this adds one field and no extra
model call.

## R13. Wiring the loop and the iteration limit

**Decision**: Add the nodes `reflect`, `qa` and `fix` to the existing team graph
([contracts/feedback-loop.md](contracts/feedback-loop.md)):

```
... coder ─(all tasks completed)→ reflect → qa ─(complete or limit)→ finish
                                     ↑        └(fix needed and iterations left)→ fix ┐
                                     └───────────────────────────────────────────────┘
```

- The limit is `TEAM_MAX_ITERATIONS`: 5 by default, from 1 to 10. `--max-iterations`
  overrides it. Checked when the run starts (FR-115).
- `qa` routes to `finish` when the iteration count reaches the limit. No `fix` or
  `reflect` runs after the last allowed review, so a run never uses more reviews than
  its limit (SC-101).
- Run end reasons: `complete`, `iteration_limit`, `task_failed`, `coder_failed` (fix
  round), `reflection_failed`, `qa_failed`, `rejected`, `pm_error`.
- The recursion limit becomes `2 * MAX_TASKS + 3 * MAX_ITERATIONS_CAP + 6` (48).

**Rationale**: The loop is part of the same `StateGraph` over `TeamState`, using only
`add_node`, `add_edge` and `add_conditional_edges` (constitution III). The guarantee
that the loop ends lives in one pure routing function that is easy to test.

## R14. QA's final report (convergence safeguard)

**Decision**: `QAAgent.final_report(request: FinalReportRequest) -> QAReport` is
deterministic, with no model call. `finish` asks for it, through the `final-report` A2A
skill (R21), whenever at least one QA iteration ran. The request is built from
`TeamState` (data-model "QA request payloads"). It records:
- the result and why the run ended;
- iterations run against the limit;
- the final pass and fail counts;
- the unresolved blocking findings with their fix instructions;
- the advisory findings;
- one line per iteration.

It goes into `TeamState.qa_report`, and QA writes it through MCP to
`<workspace>/.qa/report.md`. The CLI prints that path.

**Rationale**: The planning input requires a report "rather than looping indefinitely".
A deterministic report from recorded data is exact and costs nothing.

## R15. Testing strategy (project tests stay `unittest`)

| Test module | Covers | Needs |
|---|---|---|
| `tests/test_qa_agent.py` | Mocked code input with a fake client produces findings that validate against the findings schema. Every failing test gets a finding. Style findings are downgraded. Disputes. Load repair. The verdict. The interface summary leaves out function bodies | nothing |
| `tests/test_test_runner.py` | JUnit parsing from sample XML, the exit-code/XML cross-check, the docker command (read-only tests mount, `-c /dev/null`) | nothing; one end-to-end case gated on Docker |
| `tests/test_mcp_server.py` | The real server over stdio through `McpFileTools`: exactly 2 tools, write-then-read, paths outside the workspace, protected writes, malformed calls, a missing workspace, a call after an error | the `mcp` package only |
| `tests/test_coder_reflection.py` | Critique then revision with a fake client. At least one round. The revised result is returned. Rounds setting. Step cap passes through, refusal fails | nothing |
| `tests/test_team_loop.py` | Fake Coder and fake QA. Success in iteration 1 and in iteration 3. The limit stops after exactly N reviews with a failure report. Step cap in a fix round. QA error. Reflection before every QA call. `TEAM_MAX_ITERATIONS` parsing and range | nothing |

Findings are checked against
[contracts/qa-findings.schema.json](contracts/qa-findings.schema.json) by a small stdlib
helper in the test. It loads the schema file and asserts the `required` keys, types,
`enum` values and `pattern`, then checks the four conditional rules in `allOf` with
explicit assertions. The constitution allows no test dependencies (such as `jsonschema`)
beyond the standard library, and `jsonschema` is only present as a dependency of `mcp`.

## R16. Dependencies and the constitution

**Decision**: Add `mcp==2.3.0` to `requirements.txt`. pytest is **not** a project
dependency: it is installed only inside the sandbox image (R6), and project code never
imports it.

The justification Principle III requires:
- **Problem solved**: serving the file tools over the standard Model Context Protocol,
  which the user asked for.
- **Why not the standard library**: MCP is a JSON-RPC 2.0 protocol with an
  initialization handshake, capability negotiation and typed messages. Writing it by
  hand would be more code than the feature, and not "the official SDK" the user asked
  for.
- **Maintenance and license**: published by the Model Context Protocol project, MIT
  licence, released monthly. 2.3.0 is from 2026-10-02.
- **Transitive packages**: `pydantic>=2.12`, `anyio`, `httpx2`, `jsonschema`,
  `starlette` and `uvicorn` (used only by the HTTP transports we do not use),
  `opentelemetry-api`, `pyjwt`.
- **Compatibility with the existing pins** (PyPI metadata, checked 2026-10-19):

  | Requirement | `mcp` 2.3.0 needs | Existing constraint | Result |
  |-------------|-------------------|---------------------|--------|
  | `pydantic` | `>=2.12.0` | `anthropic` 1.11.0: `>=1.10.0,<3`; `chromadb` 1.5.9: `>=2.0` | ✅ 2.12–2.x |
  | `httpx2` | `>=2.10.0` | `anthropic` 1.11.0: `>=2.0.0,<3` | ✅ 2.10–2.x |
  | `anyio` | `>=4.9` | `anthropic` 1.11.0: `>=4.1.0,<5` | ✅ 4.9–4.x |
  | `jsonschema` | `>=4.20.0` | `chromadb` 1.5.9: `>=4.19.0` | ✅ |
  | `typing-extensions` | `>=4.13.0` | `anthropic` 1.11.0: `>=4.14,<5` | ✅ |
  | `mcp` itself | — | `anthropic` 1.11.0 `[mcp]` extra: `>=1.0,<3` | ✅ (the extra is not installed; this only shows the SDK supports MCP 2.x) |

  No range conflicts. **Confirmed on install (T001, 2026-10-19)**: `pip check` reports
  "No broken requirements found". The resolved versions are `pydantic` 2.14.1, `httpx2`
  2.14.0 and `anyio` 4.15.1. `agents.memory` (which swaps in `pysqlite3`), `chromadb`,
  `anthropic` and `mcp` import together, and the existing 97 tests pass. All MCP v2
  import paths match R1.
- **Overlap**: `mcp` adds no second package for any purpose. The HTTP libraries
  were already doubled before this feature: `chromadb` 1.5.9 depends on `httpx>=0.27.0`,
  and `anthropic` 1.11.0 on `httpx2`. Both come in transitively through
  baseline-approved packages, not as direct dependencies, and `mcp` uses `httpx2`, which
  is already present. `starlette`, `uvicorn` and `opentelemetry-api` are also already
  present through `chromadb`.

The constitution's approved baseline names four packages. Adding `mcp` is allowed with
the justification above. The plan still recommends a MINOR amendment (v1.2.0) that adds
`mcp` to the baseline. It would also say that pytest is allowed inside the sandbox image
only, so the baseline matches `requirements.txt`. That amendment is a separate task and
pull request (constitution, Development Workflow).

---

# A2A Communication via an asyncio Broker (planning input, 2026-10-19)

The user asked for Google's Agent2Agent (A2A) protocol between the Coder and QA, carried
by an asyncio broker that decouples the two agents' messages. Facts about A2A come from
the specification at a2a-protocol.org/latest/specification (version **1.0.0**) and from
the `a2a-sdk` PyPI page, both read on 2026-10-19.

## R17. A2A version, and no SDK

**Decision**: Implement the A2A **1.0** data model and operations in
`messaging/a2a.py`, using stdlib `TypedDict`s and builder and validation functions. Do
**not** add `a2a-sdk`.

**Rationale**:
- `a2a-sdk` 1.2.3 (Apache-2.0) requires `protobuf`, `google-api-core`,
  `googleapis-common-protos`, `json-rpc`, `httpx` and `pydantic`. Its transports are
  HTTP JSON-RPC, REST and gRPC, and this feature uses none of them. Only the custom
  broker binding (R18) is needed, and the SDK does not provide one.
- The deciding reasons are that its value is in transports this feature does not use,
  and that it would still add `google-api-core` and `json-rpc` (plus whatever they pull
  in) to model a few JSON shapes the stdlib handles. (`httpx`, `protobuf` and
  `googleapis-common-protos` are *not* reasons: the T001 install showed that `chromadb`
  already brings in all three, through `httpx` and its OpenTelemetry gRPC exporter.)
- The A2A objects this feature needs are a handful of plain JSON shapes. They are about
  150 lines as `TypedDict`s with builders, and the standard library covers them
  (Principle III: "stdlib whenever it reasonably solves the problem").

**Protocol facts this design follows (A2A 1.0)**:
- JSON-RPC method names are PascalCase: `SendMessage`, `SendStreamingMessage`,
  `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`, the push-notification config
  operations, and `GetExtendedAgentCard`.
- Enums use the protobuf-style names: `TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING`,
  `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `TASK_STATE_CANCELED`,
  `TASK_STATE_REJECTED`, `TASK_STATE_INPUT_REQUIRED`, `TASK_STATE_AUTH_REQUIRED`, and
  `ROLE_USER` / `ROLE_AGENT`.
- **There is no `kind` field.** A Part holds exactly one of `text`, `raw`, `url` or
  `data`, optionally with `mediaType`, `filename` and `metadata`. A `SendMessage` result
  is `{"task": …}` or `{"message": …}`.
- `Message` has `messageId`, `contextId`, `taskId`, `role`, `parts`, `metadata`,
  `extensions` and `referenceTaskIds`. `Task` has `id`, `contextId`, `status`,
  `artifacts`, `history` and `metadata`. `Artifact` has `artifactId`, `name`,
  `description`, `parts` (at least one), `metadata` and `extensions`.
- `SendMessageRequest` has `message` (required), `configuration` and `metadata`.
  `SendMessageConfiguration` has `acceptedOutputModes`, `historyLength` and
  `returnImmediately`, which defaults to false: the call waits for a terminal or
  interrupted state.
- `AgentCard` requires `name`, `description`, `supportedInterfaces`, `version`,
  `capabilities`, `defaultInputModes`, `defaultOutputModes` and `skills`. `AgentSkill`
  requires `id`, `name`, `description` and `tags`. `AgentInterface` requires `url`,
  `protocolBinding` and `protocolVersion`.
- A2A error codes: `TaskNotFoundError` -32001, `TaskNotCancelableError` -32002,
  `PushNotificationNotSupportedError` -32003, `UnsupportedOperationError` -32004,
  `ContentTypeNotSupportedError` -32005, `InvalidAgentResponseError` -32006, and
  `VersionNotSupportedError` -32009, plus the standard JSON-RPC -32600, -32601, -32602
  and -32603. `ExtendedAgentCardNotConfiguredError` is -32007 and
  `ExtensionSupportRequiredError` is -32008 (confirmed against spec §5.4 during T018).
  `ListTasks` is a core operation (spec §5.3).
- Section 12 allows custom bindings. A custom binding must support every Section 3
  operation, define the data, enum, timestamp and service-parameter mappings, map every
  A2A error, document whether it streams (and if not, say so in the card), document
  authentication, and appear in the agent card under a URI `protocolBinding`.

**Alternatives considered**: `a2a-sdk` used only for its types and request handler
(rejected for the dependency weight and HTTP overlap above). A2A over real HTTP between
two local servers (out of scope by the spec's "A2A scope" assumption, and it would
bring in a web server for an in-process exchange).

## R18. The custom binding: "asyncio broker"

**Decision**: Define one documented custom binding,
`urn:multi-agent-lab:a2a-binding:asyncio-broker:1.0`, specified in full in
[contracts/a2a-binding.md](contracts/a2a-binding.md).

**Envelope**: each request is a dict
`{"sender": str, "recipient": str, "serviceParameters": {"A2A-Version": "1.0"}, "rpc": <JSON-RPC 2.0 request>}`.
Each reply is a JSON-RPC 2.0 response object.

**Mappings**:
- *Data*: ProtoJSON camelCase field names; enums as their full names; timestamps as
  ISO-8601 UTC strings with millisecond precision and a `Z` suffix (spec §5.6.1); `raw`
  bytes as base64 (never used here).
- *Service parameters*: sent in `serviceParameters`, the binding's stand-in for HTTP
  headers. A missing `A2A-Version`, or any value other than `1.0`, gets
  `VersionNotSupportedError`.
- *Errors*: native JSON-RPC error objects with the A2A codes from R17. The `data` field
  carries `{"type": "<ErrorName>", "detail": str}`.

**Endpoint URL**: `AgentInterface.url` is `broker://<agent-name>`, for example
`broker://qa-debugger`.

**Capabilities**:
- **Streaming**: not supported. `capabilities.streaming = false`, and
  `SendStreamingMessage` and `SubscribeToTask` get `UnsupportedOperationError`.
- **Push notifications**: not supported. `capabilities.pushNotifications = false`, and
  every push-config operation gets `PushNotificationNotSupportedError`.
- **Extended card**: not supported. `capabilities.extendedAgentCard = false`, and
  `GetExtendedAgentCard` gets `ExtendedAgentCardNotConfiguredError`.

**Authentication**: none. The card has no `securitySchemes`. The trust boundary is the
single process, and this is documented in the binding.

## R19. The broker: asyncio queues in a background loop

**Decision**: `messaging/broker.py` provides `A2ABroker`.
- **Event loop**: it owns one asyncio event loop running in a daemon thread, the same
  sync-bridge pattern as `McpFileTools` (R3), because the LangGraph team graph is
  synchronous.
- **Registration**: `register(card, handler)` creates an **inbox** (`asyncio.Queue`) and
  a **worker task** for that agent. The worker takes one envelope at a time in FIFO order
  (FR-130), runs the agent's synchronous handler with `await asyncio.to_thread(...)` so
  the loop stays free (a `GetTask` while QA works returns `TASK_STATE_WORKING`), then
  settles the waiting reply future.
- **Clients**: `client(sender)` returns an `A2AClient` with synchronous `send_message`,
  `get_task`, `cancel_task` and `get_card`. Each one wraps the request in an envelope,
  submits it with `asyncio.run_coroutine_threadsafe`, and waits with a timeout.
- **Decoupling**: every envelope and every reply is copied with
  `json.loads(json.dumps(...))` as it enters and leaves the broker. Agents only ever see
  plain JSON data and never share Python objects (FR-129). Senders name a recipient
  string and never hold the recipient object.
- **Log**: the broker appends an `A2AExchange` entry for every request (FR-132), and
  `exchanges()` returns copies.
- **Task store**: each recipient's tasks are kept in a dict in the broker, keyed by
  task ID. This backs `GetTask`, `ListTasks` and `CancelTask`.
- **Shutdown**: `close()` cancels the workers, fails any waiting futures with
  `BrokerClosedError`, and stops the loop.

**Rationale**: asyncio queues meet the user's requirement as written. One loop thread
keeps the rest of the code synchronous. Per-recipient FIFO queues plus futures give
ordering and correlation with no external message system.

**Alternatives considered**: Making the whole team async (rewrites both agents and the
graph). A broker thread per agent (more threads and no gain). An external broker such as
Redis or NATS (a new service and dependency for one process).

## R20. Task lifecycle and operation semantics

| Operation | Behavior in this binding |
|-----------|--------------------------|
| `SendMessage` | Validate the envelope and the message. Create a `Task` (`TASK_STATE_SUBMITTED`, `contextId` from the message), store it, and queue it. If `configuration.returnImmediately` is true, return `{"task": <submitted copy>}` at once. Otherwise wait until it is terminal and return `{"task": <final copy>}` |
| `GetTask` | `{"id", "historyLength"?}` → the stored Task, or `TaskNotFoundError` |
| `ListTasks` | Filter by `contextId` and status. Implemented if A2A 1.0 §3 defines it (check when implementing) |
| `CancelTask` | `TASK_STATE_SUBMITTED` (still queued) → removed from the inbox and set to `TASK_STATE_CANCELED`. `TASK_STATE_WORKING` → `TaskNotCancelableError`, because synchronous agent work cannot be interrupted safely. Already terminal → `TaskNotCancelableError` |
| Streaming, push and extended-card operations | Errors as in R18 |

**Worker states**: `SUBMITTED` → `WORKING` when the worker starts → `COMPLETED` (with
artifacts), `FAILED` (with a `status.message` holding the reason text), or `REJECTED`.
`REJECTED` covers a skill the card does not list, or a payload that fails validation.

**Selecting a skill**: A2A messages carry no skill field, so the sender puts it in
`message.metadata.skillId`, and the recipient checks it against its card.

**Timeouts** (FR-133): `A2A_REQUEST_TIMEOUT` defaults to 1800 s. When the client stops
waiting, it sends `CancelTask`. If that fails because the task is `WORKING`, the task is
left running and the run ends with
`A2ATimeoutError("qa-debugger did not answer within 1800 s")`.

## R21. The Coder and QA as A2A agents, and how the loop uses them

**Decision**: `messaging/endpoints.py` wraps each agent behind A2A, so neither agent
module imports the other (SC-110).

| Agent card `name` | Skill `id` | Input (message parts) | Result (`Task.artifacts`) |
|-------------------|------------|-----------------------|---------------------------|
| `qa-debugger` | `review-program` | text: "Please review …"; data: `QAContext` as JSON | artifact `qa-iteration`: data `QAIterationResult` |
| `qa-debugger` | `final-report` | data: `FinalReportRequest` | artifact `qa-report`: data `QAReport` |
| `coder` | `fix-program` | text: `render_fix_brief(...)`; data: `FixRequest` | artifact `coder-fix`: data `CoderFix` |

Each card has
`supportedInterfaces = [{"url": "broker://<name>", "protocolBinding": <R18 URN>, "protocolVersion": "1.0"}]`,
`defaultInputModes` and `defaultOutputModes` of `["text/plain", "application/json"]`,
and `version` set to `messaging.__version__` (`"0.3.0"`), the repository's single
version constant.

**Payloads are JSON-only shared types**: `QAContext`, `FixRequest` and
`FinalReportRequest` live in `orchestration/state.py`, with paths as strings. The
endpoints pass `data` parts through unchanged, call the agent's method by name, and read
results through `.error`, `.to_dict()` or attributes. So `messaging/endpoints.py`
imports no `agents.*` module, and neither agent imports the other (SC-110).

**Mapping agent outcomes to task states**:
- *QA*: `QAIterationResult.error` → `TASK_STATE_FAILED`, with the error as the status
  message.
- *Coder*: `refused` or `error` → `TASK_STATE_FAILED`. `completed` or `step_limit` →
  `TASK_STATE_COMPLETED` (FR-116a: hitting the step cap still continues the loop).
- *Any agent*: an unexpected exception in a handler → `TASK_STATE_FAILED` with the text
  `"<Type>: <message>"`.

**Loop wiring** (an update to [contracts/feedback-loop.md](contracts/feedback-loop.md)):
`run_team` creates the broker and registers both endpoints. The graph's loop nodes no
longer hold a QA object.

| Node | Sends | On behalf of | To |
|------|-------|--------------|----|
| `qa` | `SendMessage` (skill `review-program`) | `coder` | `qa-debugger` |
| `fix` | `SendMessage` (skill `fix-program`), `referenceTaskIds=[review task]` | `qa-debugger` | `coder` |
| `finish` | `SendMessage` (skill `final-report`) | `team` | `qa-debugger` |

Notes:
- `reflect` and the build tasks stay as direct Coder calls, because they are the Coder's
  own work (FR-129).
- Every message in a run uses `contextId = run_id`.
- An A2A error, a `FAILED` or `REJECTED` task, or a timeout ends the run with the same
  end reasons as before (`qa_failed`, `coder_failed`). The new `a2a_failed` reason
  covers broker and protocol errors.

**Threading**: the Coder object is used by the graph thread (build tasks, `reflect`)
and by the broker's worker thread (`fix-program`). Never at the same time, though: every
`send_message` blocks the graph until the task is terminal, and the Coder worker takes
one message at a time. So the Coder's memory, its Anthropic client and its MCP sessions
are always used by one thread at a time.

**Rationale**: The graph keeps sole control of the loop, which keeps the end guarantees
(SC-101) in one router. All traffic between the agents goes through the broker. The
graph sends each message "on behalf of" the agent whose turn produced it. That models
the conversation truthfully, and it avoids nested calls inside a handler, where QA would
wait on the Coder from within its own worker.

## R22. Testing the A2A layer (stdlib `unittest`)

| Module | Covers |
|--------|--------|
| `tests/test_a2a.py` | Builders produce valid 1.0 shapes: no `kind`, exactly one content member per Part, enum spelling, ISO timestamps with `Z`. Validators reject a bad Part, a missing `messageId` or a bad role. Both agent cards have every required field, the binding URN, and `streaming: false` |
| `tests/test_broker.py` | Routing by recipient. FIFO per recipient. A handler runs in a thread while `GetTask` returns `WORKING`. Payload mutation by a handler does not reach the sender (copy isolation). Unknown recipient and unknown skill. Version error -32009. `CancelTask` of a submitted task versus a working one (-32002). Streaming and push operations give -32004 / -32003. Timeout → `A2ATimeoutError`. Every exchange logged. `close()` fails pending futures |
| `tests/test_team_loop.py` (additions) | The loop over a real `A2ABroker` with fake agents behind real endpoints. A run with *k* reviews logs exactly *k* `review-program` exchanges (coder → qa-debugger) and *k − 1* `fix-program` exchanges (qa-debugger → coder), all sharing `contextId == run_id`, with each fix's `referenceTaskIds` naming the review before it (SC-109). A `FAILED` QA task ends the run with `qa_failed` |
| `tests/test_agent_isolation.py` | Parse `agents/coder_agent.py` and `agents/qa_agent.py` with `ast` and assert neither imports the other (SC-110). Assert that `orchestration/team.py`'s `qa_node`, `fix_node` and `finish_node` take a client, not an agent object |
