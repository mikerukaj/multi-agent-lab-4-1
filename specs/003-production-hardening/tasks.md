---

description: "Task list for Production Hardening of the Multi-Agent Development Team"
---

# Tasks: Production Hardening of the Multi-Agent Development Team

**Input**: Design documents from `specs/003-production-hardening/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md) (amended with deltas D1–D10),
[research.md](research.md) (R1–R20), [data-model.md](data-model.md),
[contracts/](contracts/), [quickstart.md](quickstart.md), constitution **1.4.1**

**Tests**: Required. FR-235–FR-239 and directive 5 ask for them. In each story phase,
write the test tasks first and confirm they fail before implementing. Project tests use
stdlib `unittest` only. The quick run is `python -m unittest discover -s tests -t .`.
The coverage run is `scripts/test.sh` (created in T007; it gates at 85% from T091).

**Environment**: Use the repo's `.venv` (Python 3.12, created 2026-10-19 from
`requirements.txt`). Every command below assumes `source .venv/bin/activate` from the
repository root.

**Organization**: Tasks are grouped by user story, in the spec's priority order. The
**Foundational** phase builds what every story plugs into:

- the new state types;
- the `TypedDict` validator and reply types (directive 4);
- the scripted model fake;
- the secret redactor, so every record written from US1 on is already scrubbed;
- `RunServices`, the per-run bundle passed to the agents;
- `ModelCaller`, the single model-call path that replaces the five
  `beta.messages.create` call sites.

After that:

| Story | Adds | Directives |
|---|---|---|
| US1 (P1) | resilience: retry, circuit breaker, degradation; tool-input validation; run lifecycle: checkpoints, resume, signals, container cleanup, exit codes | 2, 4 |
| US2 (P2) | observability: event record, run summary, LangSmith tracing, QA report redaction (the redactor itself is Foundational) | 1 |
| US3 (P3) | cost: prices, per-agent ledger, cost report, budget, prompt caching, models by role | 3, 7 |
| US4 (P4) | deployment: Dockerfile, Compose with Chroma, health check | 6 |
| US5 (P5) | test layers: fully mocked agent unit tests, pair integration tests, e2e with supplied fixture, coverage gate | 5 |

README (directive 8) is in the Polish phase.

**Shared-file note**: `agents/llm.py` (`ModelCaller`), `orchestration/team.py` and
`orchestration/services.py` are extended by US1, US2 and US3 in turn. Those tasks are
never marked [P] across stories. The stories' new modules (`resilience/`,
`observability/`, `cost/`) can be built in parallel.

## Status after `/speckit-implement` (2026-10-20)

All code and tests are in place. `scripts/test.sh` passes offline: 519 tests, 48
skipped (each with its reason), 96% coverage. The open tasks below are left open only
because they need what this machine doesn't have, or your go-ahead:

| Task | What is done | What is left, and why |
|---|---|---|
| T073 | benchmark cost columns and `--baseline` (T072) | the live benchmark: about 10 paid runs, and Docker |
| T075, T076 | written; the Dockerfile checks pass | the Compose and five-process Chroma checks: need Docker |
| T081 | digests pinned (from the registry API), image inspected, health-check command chosen | confirm the heartbeat probe on a running container: needs Docker |
| T082, T083 | `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example` written | `docker build` and `docker compose config`: need Docker |
| T084 | none | clean-state `docker compose up`: needs Docker and one live run |
| T088, T090 | written; the e2e program passes its fixture with local pytest | run in the real sandbox: need Docker (T090 live also needs `RUN_LIVE_E2E=1`) |
| T092 | speed, offline, no-key, skip reasons and overhead done (plan.md "Quickstart validation") | the suite inside the image: needs Docker |
| T095 | quickstart §1 recorded | §2–§10: need Docker and live runs |
| T097 | none | run `/speckit-analyze` again on the delivered code |

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: US1–US5 as above
- Paths are relative to the repository root

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Governance already done, the baseline measurement, dependencies, and the
skeleton of new packages.

- [X] T001 Amend `.specify/memory/constitution.md` to v1.3.0 (MINOR): approve `langsmith` (limited to `trace`, `tracing_context`, `RunTree`, `Client` with `hide_*`) and `coverage` (dev-only, `requirements-dev.txt`). Done 2026-10-19.
- [X] T002 Apply spec deltas D1–D10 to `specs/003-production-hardening/spec.md` (FR-204, FR-206/207, FR-209a–c, FR-214a, FR-219, FR-219a, FR-222/222a, FR-225, FR-227a, FR-230a, FR-235a–c, SC-214–SC-218). Done 2026-10-19.
- [X] T003 Finish the **pre-feature baseline benchmark** started 2026-10-19 (`python -m benchmarks.run_benchmark`, all agents on `claude-sonnet-5-5`, no prompt caching, at commit `b4ad600`).
  1. When it ends, rename its results file to `benchmarks/results/<timestamp>-baseline.json`. (Done: the first run exposed a QA defect, which was fixed separately, and the baseline was re-run as `20261020T053121Z-baseline.json`. The first run is kept as `20261020T043138Z-before-qa-fix.json`.)
  2. Run `.venv/bin/python <scratchpad>/baseline_cost.py <file>`. The cost per item is `input_tokens × $2/MTok + output_tokens × $10/MTok`.
  3. Add a section "Baseline benchmark (pre-feature)" at the end of `specs/003-production-hardening/plan.md` with: a per-item table (id, succeeded, end reason, tokens, cost, duration), the pass count, the total and mean cost, the median duration, and the cause of each failed item (from the results' `error` field).

  This MUST be complete before T070 (models by role) and T067 (prompt caching), because SC-205 compares against it.
- [X] T004 Add `langsmith==0.14.11` to `requirements.txt` (alphabetical, keep all existing pins). Create `requirements-dev.txt` containing `-r requirements.txt` and `coverage==7.16.2`. Run `pip install -r requirements-dev.txt` and `pip check`, which must print `No broken requirements found.`. Confirm that `python -c "import langsmith, coverage; from langsmith import trace, tracing_context, Client; from langsmith.run_trees import RunTree"` succeeds.
- [X] T005 [P] Create the package skeletons, each `__init__.py` with only a one-line module docstring:
  - `resilience/__init__.py` ("Failure classification, retries, circuit breakers and the degraded-response cache.")
  - `observability/__init__.py` ("Secret redaction, the run event record and LangSmith tracing.")
  - `cost/__init__.py` ("Token prices, per-agent usage and cost, run budgets and cost reports.")
  - `tests/integration/__init__.py`, `tests/e2e/__init__.py`, `tests/fakes/__init__.py`, all empty.
- [X] T006 [P] Update `.gitignore`: add `.cache/`, `.coverage`, `.coverage.*`, `htmlcov/`. Do not ignore `docs/cost_reports/`. Create `docs/cost_reports/.gitkeep` (empty).
- [X] T007 [P] Create `.coveragerc` (`[run] source = agents, orchestration, tools, messaging, observability, resilience, cost, benchmarks`; `omit = tests/*`; `[report] show_missing = true`). Create the executable `scripts/test.sh`:

  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  cd "$(dirname "$0")/.."
  coverage run -m unittest discover -s tests -t . -v
  coverage report
  ```

  Mark it executable with `chmod +x scripts/test.sh`. The script reports coverage but does not gate on it yet, so `scripts/test.sh` stays green at every phase checkpoint and can serve as the pre-merge check from the start. T091 adds `--fail-under=85` once the suite reaches it (FR-237).

**Checkpoint**: The dependencies install, and the existing suite still passes with `python -m unittest discover -s tests -t .`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The new state types, validation, the secret redactor, the scripted model
fake, `RunServices`, and `ModelCaller`, with all five call sites migrated. Behaviour is
unchanged apart from reply validation.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T008 Extend `orchestration/state.py` per data-model §1.1–§1.6:
  - Add to `EndReason`: `"dependency_unavailable"`, `"budget_exceeded"`, `"interrupted"`.
  - Add the `Literal`s `Dependency`, `FailureKind`, `CircuitState`, `ServedFrom`, `AgentName`, `Stage`, `EventSeverity` (name it `EventSeverity` because `Severity` already exists for findings).
  - Extend `TokenUsage` with `cache_creation_input_tokens: int` and `cache_read_input_tokens: int`.
  - Add the `TypedDict`s `AgentUsage`, `DependencyFailure`, `ResumeAttempt` and `RunSummary`, with fields exactly as in data-model §1.3–§1.5 and §2.2.
  - Add these `TeamState` fields: `agent_usage`, `cost_usd`, `budget_usd`, `failure`, `resume_from`, `resume_history`, `events_lost`, `trace_url`, `cost_report_path`, `summary`.
  - Add `RESUMABLE_END_REASONS = ("dependency_unavailable", "a2a_failed", "budget_exceeded", "interrupted")`.

  Update `initial_team_state` in `orchestration/team.py`:
  - zeros for the four token counters;
  - an `agent_usage` entry per agent with zero counters and `cost_usd=0.0`;
  - `None` for the optional fields;
  - `[]` for `resume_history`;
  - `False` for `events_lost`.

  Update `add_usage` in `orchestration/team.py` and `add_tokens` in `agents/llm.py` to sum all four counters, treating a missing key as 0, so that old records and fakes still work. Extend `tests/test_team_state.py`: the new initial state round-trips through `json.dumps`/`json.loads` unchanged, and `add_usage` on dicts missing the cache keys works.
- [X] T009 [P] Write `tests/test_validation.py` (fails until T010/T011) for `orchestration.validation.validate(value, tp) -> list[str]`. It must cover:
  - a valid nested `TypedDict`;
  - a missing required key;
  - an unknown key;
  - `NotRequired` being absent, which is fine;
  - a wrong scalar type;
  - `bool` passed for `int`, which is rejected;
  - `int` passed for `float`, which is accepted;
  - a `Literal` value outside its set;
  - `list[T]` with one bad element, where the path is `$.tasks[2].priority`;
  - `dict[str, T]`;
  - `X | None`;
  - problems reported in document order;
  - an unsupported annotation raising `TypeError`.

  Add `SchemaAgreementTests`, which check each pair from contracts/llm-output-validation.md (`SPEC_SCHEMA`↔`PMSpecReply`, `CONSOLIDATION_SCHEMA`↔`PMConsolidationReply`, `SUMMARY_FORMAT["schema"]`↔`CoderSummaryReply`, `CRITIQUE_FORMAT["schema"]`↔`CritiqueReply`, `WRITE_TESTS_FORMAT["schema"]`↔`QATestFilesReply`, `DISPUTES_FORMAT["schema"]`↔`QADisputeReply`, `REVIEW_FORMAT["schema"]`↔`QAReviewReply`, and the `input_schema`s of `READ_FILE_TOOL`/`WRITE_FILE_TOOL`/`EXEC_PYTHON_TOOL` ↔ `ReadFileInput`/`WriteFileInput`/`ExecPythonInput`). At every object level:
  - property names equal the TypedDict keys;
  - `required` equals the required keys;
  - `enum` equals the `Literal` args;
  - JSON types map to Python types.

  Note that `tools/__init__.py::_with_purpose` adds a `purpose` property to tool schemas, so the tool-input `TypedDict`s must include `purpose: str`. Check the session definitions as well as the raw constants.
- [X] T010 Create `orchestration/validation.py` per contracts/llm-output-validation.md "Validator", about 80 lines on the standard library only (`typing.get_type_hints(tp, include_extras=True)`, `typing.get_origin/get_args`, `is_typeddict`, `__required_keys__`). Problems are strings like `"$.tasks[2].priority: 'urgent' is not one of ('high', 'medium', 'low')"`. Public API: `validate(value, tp) -> list[str]` and `first_problems(problems, limit=3) -> str`.
- [X] T011 Add the reply `TypedDict`s to `orchestration/state.py` (data-model §1.7): `TaskItemReply`, `PMSpecReply`, `PMConsolidationReply`, `FindingResponseReply`, `CoderSummaryReply`, `CritiqueItemReply`, `CritiqueReply`, `TestFileReply`, `QATestFilesReply`, `QADisputeReply` (with its nested items as in `DISPUTES_FORMAT`), `ReviewFindingReply`, `RegressionTestReply`, `QAReviewReply`, `ReadFileInput`, `WriteFileInput`, `ExecPythonInput`. Read each field set from the schema constants in `agents/pm_agent.py`, `agents/coder_agent.py`, `agents/qa_agent.py`, `tools/file_tools.py`, `tools/python_exec.py` and `tools/__init__.py`. Run `tests/test_validation.py`: all of it must pass.
- [X] T012 [P] Create `tests/fakes/scripted_claude.py` per contracts/test-suite.md "Fakes". `ScriptedClaude` exposes `.beta.messages.create(**request)` and replays a list of steps, each a reply or an exception:
  - Reply helpers build real `anthropic.types.beta.BetaMessage` objects with `BetaMessage.model_validate({...})`: `text_reply(payload: dict|str, usage=...)`, `tool_use_reply(calls: list[tuple[name, input]], thought: str)`, `refusal_reply(category, explanation)`.
  - Error helpers raise real SDK exceptions built on an `httpx2.Request`/`httpx2.Response`: `rate_limit_error(retry_after: float|None)`, `overloaded_error()` (529), `server_error(500)`, `connection_error()`, `bad_request_error()`, `auth_error()`.
  - Every request body is recorded in `.requests` (deep-copied).
  - Each step can be a callable `request -> reply`, used for content-aware scripts in integration and e2e tests.
  - Default usage per reply is `input_tokens=100`, `output_tokens=50`, cache fields 0.
  - Running past the script raises `AssertionError("script exhausted")`.

  The file name must not match `test*.py`. Add `tests/test_scripted_claude.py` with a few cases checking that the replies parse like live ones (`.content[i].type`, `.usage.input_tokens`, `.stop_reason`).
- [X] T045 [P] Write `tests/test_redaction.py` for `observability/redaction.py` (R12, FR-219). *Moved here from US2 so that checkpoints and run records are redacted from the MVP on.*
  - values of `MY_API_KEY`, `GITHUB_TOKEN`, `DB_PASSWORD`, `x_secret` and `AWS_CREDENTIAL` with ≥ 8 characters are replaced by `[REDACTED]` in nested dicts, lists and strings;
  - a 5-character value and empty values are not;
  - `sk-ant-api03-abcdefgh…` is redacted even when it isn't in the environment;
  - non-secret variables (`HOME`, `PATH`) are untouched;
  - `redact` returns a new object and leaves the input unchanged.
- [X] T049 [P] Create `observability/redaction.py`:
  - `SECRET_NAME = re.compile("KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)`, `ANTHROPIC_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")` and `MARKER = "[REDACTED]"`;
  - `class Redactor`: `from_env(env)` and `redact(value)`, which walks str, dict and list values. Replace longer secrets first. Run `tests/test_redaction.py`.
- [X] T013 Create `orchestration/services.py` with `@dataclass class RunServices` holding `run_id: str`, `stop_requested: threading.Event` and `redactor: Redactor` (T049, built with `Redactor.from_env(os.environ)`), plus `@classmethod create(run_id: str | None = None) -> RunServices`, which uses `agents.coder_agent.new_run_id()` when no run ID is given. Later stories add fields with defaults, so foundational callers never change. The docstring should explain why it exists: shared per-run state passed explicitly, never global (constitution II).
- [X] T014 Write `tests/test_model_caller.py` (foundational part, fails until T015) using `ScriptedClaude`. `ModelCaller.call(request, reply_type)` must:
  - return the parsed and validated reply for a JSON text reply;
  - report a refusal as `ModelReply.refusal` with `refusal_message`, never validated;
  - report invalid JSON as a problem;
  - report a reply failing `validate` with `problems` set and `data=None`;
  - with `reply_type=None`, return the raw response (Coder `_think`);
  - pass the request through unchanged (recorded body equals the input);
  - convert usage into a `TokenUsage` with all four counters (missing cache attributes → 0).
- [X] T015 Add `ModelCaller` to `agents/llm.py`:
  - `@dataclass class ModelReply`: `response` (the SDK message, or `None`), `data: dict | None`, `text: str`, `usage: TokenUsage`, `refusal: str | None`, `problems: list[str]`, `failure: DependencyFailure | None` (always `None` until US1), `served_from: ServedFrom` (`"live"`).
  - `class ModelCaller` with `__init__(self, agent: AgentName, client, services: RunServices)` and `call(self, request: dict, reply_type: type | None = None) -> ModelReply`. Keep the steps in small helpers, each under about 40 lines: `_send(request)`, `_usage_of(response)`, `parse_reply(response, reply_type) -> tuple[dict|None, str, list[str]]`.
  - Keep `refusal_message`, `with_fallbacks` and `add_tokens`.

  Run `tests/test_model_caller.py`.
- [X] T016 Migrate `agents/pm_agent.py` onto `ModelCaller`:
  - `PMAgent.__init__(config=None, client=None, services: RunServices | None = None)` builds `self._caller = ModelCaller("pm", client, services or RunServices.create())`.
  - Add `attach(services)`, which rebuilds the caller with the given services and keeps the client.
  - `_call` uses `self._caller.call(self._request(...), PMSpecReply or PMConsolidationReply)`.
  - A reply with problems becomes `PMResult.error = "Model output failed validation: " + first_problems(problems)`.
  - Keep the `anthropic.APIError` handling for now. US1 replaces it.
  - The existing `_validate_spec`/consolidation checks run after `validate`.

  Add cases to `tests/test_pm_agent.py`: a reply missing `assumptions`; `priority: "urgent"`; an extra key. Each must give `pm_error` with the validation message, and no task is created. All existing PM tests must stay green.
- [X] T017 Migrate `agents/coder_agent.py` onto `ModelCaller`:
  - `CoderAgent.__init__(..., services=None)`, plus `attach(services)`.
  - `_think` uses `self._caller.call(build_request(...), None)` and keeps working on `reply.response`.
  - `_finalize` uses `CoderSummaryReply`. A validation failure keeps the existing tolerant fallback (`plan=[]`, raw text as the explanation). Record the problems in a new `CoderResult.validation_problems: list[str]` (default `[]`) so US2 can emit an event.
  - `_critique` uses `CritiqueReply`. A validation failure → `"error"` with `"Critique output failed validation: …"`.
  - Remove `_parse_summary`'s `json.loads` path, which moves into `parse_reply`, but keep the `_parse_responses` filtering.

  Add cases to `tests/test_coder_agent_loop.py` and `tests/test_coder_reflection.py`: a summary missing `main_file` falls back, records `validation_problems`, and still reads the code from disk; a critique item with `kind: "typo"` → `reflection_failed`.
- [X] T018 Migrate `agents/qa_agent.py` onto `ModelCaller`:
  - `QAAgent.__init__(..., services=None)`, plus `attach(services)`.
  - `_ask(turn, user_text, schema, reply_type)` raises `QAStepError("Malformed QA reply: " + first_problems(problems))` on validation failure. Pass `QATestFilesReply` at both `WRITE_TESTS_FORMAT` call sites, `QADisputeReply` at `DISPUTES_FORMAT` and `QAReviewReply` at `REVIEW_FORMAT`.
  - Keep `turn.usage` accumulation through `reply.usage`.

  Add cases to `tests/test_qa_agent.py`: each of the three reply types failing validation (a missing `criteria`; `severity: "critical"`; a non-integer `finding_index`) → iteration `error` starts with `Malformed QA reply`, `verdict == "fix_needed"`, and no test file is written (assert `FakeFileTools` saw no `write_file`).
- [X] T019 Wire `RunServices` into `orchestration/team.py::run_team`:
  - Add the keyword `services: RunServices | None = None`. Create one when missing, using `services.run_id` as the run ID instead of calling `new_run_id()`.
  - Call `agent.attach(services)` for each of `pm`, `coder` and `qa` that has an `attach` attribute (the fakes don't).
  - `main()` and `benchmarks/run_benchmark.py::_run_item` pass a fresh `RunServices.create()` per run.

  Add a test to `tests/test_team_handoff.py`: the run ID in the final state equals `services.run_id`, and real `PMAgent` on `FakePMClient` gets attached (assert `pm._caller._services is services`).

**Checkpoint**: The suite is green. All model traffic goes through `ModelCaller`, every structured reply is validated before use (FR-219a, model side), and `services.redactor` is available to every writer.

---

## Phase 3: User Story 1 - Runs Survive Transient Failures and End Cleanly (Priority: P1) 🎯 MVP

**Goal**: Temporary failures are retried (at most 3, with backoff and jitter), and permanent ones fail fast. A circuit breaker per dependency opens after 3 failed calls and half-opens after a cool-down. Degraded calls get a cached response or a structured error, and no run ever crashes. Tool inputs are validated. Every run ends with a final record, no leftover containers, and a resumable checkpoint (directives 2 and 4, FR-203–FR-213d, FR-219a).

**Independent Test**: `python -m unittest tests.test_resilience tests.test_model_caller tests.test_never_crashes tests.test_checkpoint tests.test_lifecycle -v` (quickstart §3). Then interrupt a scripted run in each stage and resume it: it reaches the same end reason as an uninterrupted run.

### Tests for User Story 1 ⚠️ (write first, confirm they fail)

- [X] T020 [P] [US1] Write `tests/test_resilience.py` for `resilience/classify.py`, `retry.py` and `circuit.py` (contracts/resilience.md). Every case in research R4's table must be covered:
  - `RateLimitError`, 529, 500, `APIConnectionError`, `APITimeoutError`, 408 and 409 → temporary;
  - 400, 401, 403, 404, 413 and 422 → permanent;
  - `McpUnavailableError` mid-session → temporary;
  - docker exit 125 with a "Cannot connect to the Docker daemon" stderr → temporary;
  - a generated-code exit 1 → not a failure;
  - chroma `ConnectionError` → temporary.

  `backoff_delay` with an injected `random.Random(0)`:
  - delays grow;
  - each is within `[ceiling/2, ceiling]`;
  - each is capped at `max_delay`;
  - `retry-after` 2 s → ≥ 2 s;
  - `retry-after` above `max_delay` → `None`.

  `DependencyGuard.call` with an injected fake `sleep` and `clock`:
  - one 429 then success → result, one retry recorded;
  - 4 temporary failures → `DependencyFailure(attempts=4, kind="temporary")`;
  - a permanent failure → `attempts=1`, no sleep;
  - 3 consecutive failed calls → state `open`;
  - a 4th call fails fast with `attempts=0` and `circuit="open"`, with `retry_after_seconds` equal to the remaining cool-down, within 1 s, without calling the operation (SC-216);
  - after the cool-down, exactly one trial is admitted while a concurrent second caller fails fast (two threads with a barrier); trial success → `closed` with failures reset;
  - a failed trial → `open` with a new `opened_at`;
  - a success in `closed` resets the count;
  - `stop_requested` set → `DependencyFailure(error_type="interrupted")` without calling.

  Config parsing: `RETRY_MAX_RETRIES=9` and `CIRCUIT_COOLDOWN_SECONDS=0` → `ValueError` naming the variable.
- [X] T021 [P] [US1] Write `tests/test_response_cache.py` for `resilience/response_cache.py`:
  - the fingerprint ignores `cache_control` keys anywhere and dict key order, and changes when any message changes;
  - `put` then `get` within the TTL returns an equal `BetaMessage`;
  - after the TTL → `None`;
  - TTL 0 disables;
  - files are written atomically (no `*.tmp` left) with mode `0o600`;
  - a corrupt file → `None` and the file is removed;
  - an in-memory hit avoids disk reads.
- [X] T022 [P] [US1] Extend `tests/test_model_caller.py` with degradation cases, using a real `DependencyGuard` (fake sleep and clock) and `ScriptedClaude`:
  - three calls that each exhaust retries open the circuit;
  - a 4th call with a cached response for the identical request returns `served_from="cache"` and validated data, and records no token cost;
  - a 4th call without a cache entry returns `ModelReply.failure` (a `DependencyFailure` with `dependency="anthropic"`, `target` = the model, `circuit="open"`);
  - a cached entry that now fails validation is discarded, and `failure` is returned;
  - the client is built with `max_retries=0` (assert on `PMAgent()._caller._client.max_retries` with `ANTHROPIC_API_KEY` set to a dummy).
- [X] T023 [P] [US1] Write `tests/test_tool_resilience.py`:
  - **(a) MCP restart**: use the real `McpFileTools` against the real `tools.mcp_server` in a temp workspace. Kill the server process mid-session (`os.kill` on the server subprocess's PID, found from the session's process handle; do not add a test-only hook to `McpFileTools`, FR-239). The next `call` restarts the server with the same `protect` list and succeeds, a `restart` is counted, and a write under `tests/` is still refused afterwards.
  - **(b) Repeated deaths**: kill the server on every attempt → after 3 retries, `call` returns `(json.dumps(DependencyFailure), True)` and never raises (FR-206).
  - **(c) Sandbox start retry**: patch `tools.python_exec._docker`/`subprocess.run` so `docker run` returns 125 with a daemon error twice, then succeeds → `exec_python` returns the real result. A generated-code exit 1 is returned as-is with no retry. A timeout is not retried.
  - **(d) Labels**: `locked_down_run` includes `--label multi-agent-lab.run_id=<id>` and `--label multi-agent-lab.owner=<host>:<pid>`.
  - **(e) Pytest retry**: `run_pytest` retries on 125 the same way.
- [X] T024 [P] [US1] Add tool-input validation tests to `tests/test_coder_agent_loop.py`. `ScriptedClaude` requests `write_file` with `{"path": 3, "content": "x", "purpose": "p"}`, then `exec_python` with a missing `path`, then an unknown tool `rm_rf`. Each gets an `is_error` tool result starting `Invalid tool input:` or `Unknown tool:`. `FakeFileTools` records no write, and the loop continues to the summary.
- [X] T025 [P] [US1] Write `tests/test_never_crashes.py` (SC-216). For each node `pm`, `coder`, `reflect`, `qa` and `fix`, and each dependency `anthropic`, `mcp` and `sandbox` forced to fail temporarily past its retries or stay open (with the circuit pre-opened through the guard in `RunServices`), run `run_team` with real agents on `ScriptedClaude`, `FakeFileTools` and a fake sandbox, or with `FakeCoder`/`FakeQA` returning a `failure`. Assert that:
  - no exception escapes;
  - `status == "failed"` and `end_reason == "dependency_unavailable"` for a model or QA-tool failure, while a Coder tool failure lets the loop continue;
  - `state["failure"]["dependency"]` is correct;
  - `workspace/.runs/<run_id>/run.json` exists.

  Add a **permanent-failure** case per model-calling node (`pm` → `pm_error`, a `coder` build task → `task_failed`, `reflect` → `reflection_failed`, `qa` → `qa_failed`, a `fix` round → `coder_failed`; these are the existing mappings FR-205 keeps), using `ScriptedClaude.auth_error()` or `bad_request_error()` (FR-205, US1 scenario 3). Assert that:
  - `end_reason` is the agent's existing end reason, never `dependency_unavailable`;
  - `state["failure"]["kind"] == "permanent"` with `attempts == 1`;
  - `exit_code_for(state, None) == 1`;
  - `resume_team(run_id)` raises `ResumeRefused` naming the end reason.

  Permanent failures still count toward the breaker (FR-209a). Add one agent-level case: three `PMAgent.write_spec` calls on one `RunServices` each fail permanently and open the circuit, and the 4th is refused by the open breaker, so `is_unavailable` is true and `pm_node` would end `dependency_unavailable` (exit 3). This is correct because the circuit is open, and the case keeps the boundary explicit.

  Also add:
  - **Chroma** (SC-216): memory forced to fail (patched client raising `ConnectionError`, or the circuit pre-opened). The run continues, `degraded` events are recorded, it reaches the same end reason as with memory working, and the cost report exists.
  - **A2A not retried** (FR-208): a QA handler that sleeps past the A2A timeout. The run ends `a2a_failed`, the handler was invoked exactly once, no `retry` event names the broker, and the cancelled work never applies a fix round twice.

  Use a temp `CODER_WORKSPACE`.
- [X] T026 [P] [US1] Write `tests/test_checkpoint.py` for `orchestration/checkpoint.py` (data-model §2.3, contracts/cli.md resume rules):
  - save then load round-trips the state, settings, ledger and workspace snapshot (including binary and `tests/` files);
  - a write is atomic (simulate a crash by raising inside the temp-file write) and the previous checkpoint stays loadable;
  - `complete: false`, `format_version: 99` and unparsable JSON → `CheckpointError` naming the reason;
  - `restore_workspace` overwrites changed files, recreates deleted ones, removes extra ones, and returns the `restored_files`/`removed_files` lists;
  - the run lock (`fcntl.flock`) is exclusive across two processes (`multiprocessing`);
  - a checkpoint and `run.json` contain no secret (plant an `ANTHROPIC_API_KEY` value in the requirement and check both files), using the Foundational redactor (T049);
  - `state["test_suite"]` (QA's own copy of its tests, FR-213) round-trips byte for byte.
- [X] T027 [P] [US1] Write `tests/test_resume.py`:
  - **Interrupt and resume in every stage**: use scripted runs (real agents on `ScriptedClaude`, fake sandbox) interrupted by setting `services.stop_requested` at a chosen node (`pm`, `consolidate`, the 2nd `coder` task, `reflect`, `qa`, `fix`). Each ends `interrupted`. `run_team(resume_run_id=...)` then reaches the same end reason as the uninterrupted script, the model calls made after the resume never exceed those of the interrupted stage (SC-211), and the iteration count, usage and a2a_log carry forward. A `ResumeAttempt` is appended with the right `from_stage` and file lists. For the `fix` stage, also edit a test file by hand before resuming: the resume restores it from the checkpoint and lists it in `restored_files`, and because QA's own copy (`test_suite`) came back with the checkpoint, QA's next review reports no tampering (FR-213, U1).
  - **Every refused case from contracts/cli.md**: `complete`, `iteration_limit`, `rejected`, `pm_error`, `qa_failed`, lock held, missing checkpoint, `complete: false`, incompatible `format_version`, a changed `--max-iterations`, and `budget_exceeded` without a higher budget. Each raises `ResumeRefused` with the reason, and no file changes (compare the directory checksum before and after).
  - **Killed run**: a run with no `run.json` and no lock held resumes from its checkpoint.
  - **`a2a_failed`** (FR-213b): a scripted run whose QA handler times out ends `a2a_failed` (exit 3). `resume_team(run_id)` reruns the stage that sent the request from its start (the pending request is not replayed, per the spec edge case) and reaches the uninterrupted end reason.
- [X] T028 [P] [US1] Write `tests/test_lifecycle.py`:
  - `exit_code_for(state, signal_received)` returns 0, 1, 2, 3, 130 and 143 per contracts/cli.md, including `a2a_failed` → 3.
  - A SIGTERM sent to a subprocess running `python -m orchestration.team` (with a scripted, slow fake model injected through a test-only entry module `tests/fakes/slow_team_main.py`) exits 143 within 30 s, writes `run.json` with `end_reason == "interrupted"`, and leaves no labelled container (docker-gated part; skip without Docker).
  - `cleanup_stale_containers()` with a patched `_docker` removes only the containers whose run lock is not held (FR-211, FR-230b).
  - A second signal during shutdown calls `os._exit` (patched).
  - A stop requested while `finish` is writing the QA report gives a report marked `partial` (spec edge case), and `run.json` says so.

  Create `tests/fakes/slow_team_main.py` as part of this task: an entry module that builds the team with a `ScriptedClaude` whose steps sleep 1 s each, then calls `orchestration.team.main(argv)`. Its name must not match `test*.py`.

### Implementation for User Story 1

- [X] T029 [P] [US1] Create `resilience/classify.py`: `classify(error_or_outcome, dependency) -> tuple[Dependency, FailureKind, str] | None`, where `None` means "not a failure". It implements research R4's table using the SDK's typed exception classes, never message matching, except for docker stderr, where it uses the documented patterns `Cannot connect to the Docker daemon`, `Error response from daemon`, `failed to create`. Also `retry_after_seconds(error) -> float | None`, read from `error.response.headers["retry-after"]` when present.
- [X] T030 [P] [US1] Create `resilience/retry.py`:
  - `@dataclass(frozen=True) class RetryPolicy(max_retries=3, base_delay=1.0, max_delay=60.0)`;
  - `from_env(env)` with the ranges from contracts/resilience.md;
  - `backoff_delay(policy, attempt, retry_after, rng) -> float | None` implementing equal jitter and the `retry-after` rule.
- [X] T031 [US1] Create `resilience/circuit.py`:
  - `class CircuitBreaker(key, threshold=3, cooldown_seconds=30, clock=time.monotonic)` with a `threading.Lock`, the `state` property, `admit() -> bool` (which admits one half-open trial), `success()` and `failure()`, plus a `listener` callback for state changes;
  - `class DependencyGuard(dependency, target, breaker, policy, rng, sleep, stop_requested, on_event)` with `call(operation: Callable[[], T], classify_target=None) -> T | DependencyFailure`, exactly per the contracts/resilience.md pseudo-code. The sleep happens outside the lock.

  Keep each method under about 40 lines. Run `tests/test_resilience.py` until green.
- [X] T032 [P] [US1] Create `resilience/response_cache.py`: `class ResponseCache(directory: Path, ttl_seconds: float, clock=time.time)` with `fingerprint(request) -> str`, `get(request) -> BetaMessage | None` and `put(request, response)`. Write atomically (temp file in the same directory, `os.replace`, `os.chmod 0o600`), keep an in-memory dict, and build from `LLM_RESPONSE_CACHE_DIR`/`LLM_RESPONSE_CACHE_TTL_SECONDS` via `from_env`. Run `tests/test_response_cache.py`.
- [X] T033 [US1] Extend `orchestration/services.py::RunServices` with:
  - `retry_policy: RetryPolicy`;
  - `breakers: dict[str, CircuitBreaker]`, created lazily;
  - `response_cache: ResponseCache | None`;
  - `rng: random.Random`;
  - `sleep: Callable`;
  - `on_event: Callable[[dict], None]`, a no-op until US2;
  - `guard(dependency, target) -> DependencyGuard`, which returns a guard sharing one breaker per `f"{dependency}:{target}"` key (the Coder and QA on the same model share one, as in the spec edge case).

  `RunServices.create(env=os.environ)` reads `RETRY_*`, `CIRCUIT_*` and `LLM_RESPONSE_CACHE_*` and raises `ValueError` for out-of-range values.
- [X] T034 [US1] Put the guard and degradation into `agents/llm.py::ModelCaller.call`:
  1. `services.guard("anthropic", request["model"]).call(lambda: client.beta.messages.create(**request))`.
  2. On success, `response_cache.put`.
  3. On `DependencyFailure`, try `response_cache.get`. A hit is parsed and validated like a live response and returns `served_from="cache"` with zero usage. A validation failure discards the entry. On a miss, return `ModelReply(failure=...)`.

  Construct default clients with `anthropic.Anthropic(max_retries=0)` in `PMAgent`, `CoderAgent` and `QAAgent`, replacing `max_retries=3` (R3). Run `tests/test_model_caller.py`.
- [X] T035 [US1] Map `ModelReply.failure` in each agent, per contracts/resilience.md "Degradation by caller". A failure is **unavailable** only when `failure["kind"] == "temporary"` (retries exhausted) or `failure["circuit"] == "open"` (refused by an open breaker). Every other failure, meaning `kind == "permanent"` with the circuit closed, is **permanent** and keeps the agent's existing error path (FR-205). Put this test in one helper, `is_unavailable(failure) -> bool` in `resilience/classify.py`, used by all three agents and by T036.
  - PM: `PMResult` gains `failure: DependencyFailure | None` in both cases. Unavailable → `error = "Model service unavailable: <message>"`. Permanent → `error = "Model call failed: <message>"`, as today.
  - Coder: `_think`, `_finalize` and `_critique` set the new status `"dependency_unavailable"` (add it to `TERMINAL_STATUSES`) only when unavailable. Permanent → the existing `"error"` status and message. Both set the new `CoderResult.failure`/`ReflectionResult.failure` field.
  - QA: `QAStepError` gains an optional `failure` attribute, set in both cases, and `QAIterationResult.failure` is set by `_failed`.

  Remove the now-unreachable `except anthropic.APIError` blocks around model calls. Add unit cases per agent to its existing test module: one unavailable failure (temporary, exhausted) and one permanent failure (401), asserting the different status and message.
- [X] T036 [US1] Map failures to the team end reason in `orchestration/team.py`:
  - `pm_node`, `consolidate_node`, `coder_node`, `reflect_node`, `qa_node` (read `failure` from the `qa-iteration` artifact data) and `fix_node` (from the `coder-fix` artifact) always copy a result's `failure` into `state["failure"]`. They set `status="failed"` and `end_reason="dependency_unavailable"` **only when `is_unavailable(failure)`** (T035). A permanent failure keeps today's mapping to the agent's end reason (`pm_error`, `coder_failed`, `reflection_failed`, `qa_failed`), so it exits 1 and cannot be resumed (FR-201a, FR-205, FR-213b).
  - The routers send these to `finish`.
  - `_final_result` keeps them.
  - `messaging/endpoints.py` copies `failure` into the artifacts' data, which is plain JSON.

  Run `tests/test_never_crashes.py` (partially, until T037–T039).
- [X] T037 [US1] Guard MCP in `tools/mcp_client.py`. `McpFileTools.__init__` gains an optional `guard: DependencyGuard | None`. `call(name, arguments)` runs through the guard: if the server process has exited or the stdio pipe broke (`McpUnavailableError` mid-session, `BrokenPipeError`, `EOFError`), the attempt closes the session, starts a new server with the same workspace and `protect`, re-lists the tools, records a `restart` through `on_event`, and repeats the call. A final `DependencyFailure` returns `(json.dumps(failure), True)`. Start-up failures (exit 2: bad workspace or protect path) stay a permanent `McpUnavailableError`, as today. `CoderAgent`/`QAAgent` pass `services.guard("mcp", str(workspace))` when they open sessions. Run T023 (a)/(b).
- [X] T038 [US1] Guard the sandbox in `tools/python_exec.py` and `tools/test_runner.py`:
  - `locked_down_run(name, workspace, extra_volumes, run_id="")` adds the two `--label` flags (R14).
  - `exec_python(..., guard=None, run_id="")` and `run_pytest(..., guard=None, run_id="")` run `docker run` through the guard, retrying only start failures classified as temporary. The code's own exit codes and timeouts are returned unchanged.
  - An open circuit makes `exec_python` return an `ExecutionResult` with `exit_code=None`, and `dispatch_tool` reports `json.dumps(failure)` with `is_error=True`. `run_pytest` raises `SandboxUnavailableError` carrying the failure, and QA turns that into `QAStepError(failure=...)`.

  Thread `guard` and `run_id` through `tools/__init__.py::dispatch_tool` and the agents' calls (`services.guard("sandbox", image)`). Run T023 (c)–(e).
- [X] T039 [US1] Validate tool inputs in `agents/coder_agent.py::_act`. Before `dispatch_tool`, check `block.name` against the session's tool names. An unknown name gives the result `"Unknown tool: <name>"`. Then run `validate(block.input, {"read_file": ReadFileInput, "write_file": WriteFileInput, "exec_python": ExecPythonInput}[block.name])`. Problems give an `is_error` tool result `"Invalid tool input: " + first_problems(problems)`, no dispatch, and the problems are recorded on the step's `ToolCall` (`is_error=True`). Run T024.
- [X] T040 [US1] Guard the Chroma memory in `agents/memory.py`. `SemanticMemory.__init__(path, collection, guard=None)`:
  - Construction, `query` and `add` run through the guard (`dependency="chroma"`).
  - `query` on failure → `[]`. `add` on failure → returns `""` and skips the write.
  - Both report a `degraded` event through the guard's `on_event`. The run continues (contracts/resilience.md).

  Update `CoderAgent.attach` to rebuild memory with `services.guard("chroma", <path or host>)`. Add cases to `tests/test_agent_memory.py` with a patched `chromadb` client raising `ConnectionError`.
- [X] T041 [US1] Create `orchestration/checkpoint.py` (R13):
  - `runs_dir(workspace_root) -> Path` (`workspace_root/.runs`);
  - `run_dir(workspace_root, run_id)`;
  - `class RunLock`, a context manager with `fcntl.flock(LOCK_EX | LOCK_NB)` on `lock`, raising `RunLocked`, plus `is_locked(path) -> bool`;
  - `snapshot_workspace(workspace) -> dict[path, {"b64", "mode"}]`;
  - `restore_workspace(workspace, snapshot) -> (restored, removed)`;
  - `save_checkpoint(run_dir, stage, next_node, state, settings, ledger, redact)`, which writes a temp file, `fsync`s, `os.replace`s, and writes `complete: true`;
  - `load_checkpoint(run_dir) -> dict`, raising `CheckpointError(reason)`;
  - `write_final_record(run_dir, state, redact)` → `run.json`;
  - `FORMAT_VERSION = 1`, with `APP_VERSION` read from `messaging.__version__`.

  `redact` is `services.redactor.redact` (T049, T013). `state["test_suite"]` is QA's own copy of its tests, which FR-213 requires in the checkpoint: it is saved inside `state` and restored with it. Run `tests/test_checkpoint.py`.
- [X] T042 [US1] Add checkpointing and resume to `orchestration/team.py`:
  1. **Stage saves**: wrap the stage nodes so that, after `pm`, `consolidate`, each `coder` task, `reflect`, `qa` and `fix` complete without ending the run, `save_checkpoint` records `stage` and `next_node` (the node the router picks next). Put this in one helper, `_checkpointed(node_name, stage, fn)`, used in `build_team_graph`, not copied into eight nodes.
  2. **Resume router**: add a `resume` node at `START`. A conditional edge routes to `state["resume_from"]` when set, otherwise to `pm`. The node clears `resume_from`. Only `StateGraph`/`START`/`END` are used.
  3. **`run_team` lifecycle**: `run_team` takes the `RunLock` for the life of the run and calls `write_final_record` in a `finally:` block, so a final record exists however the run ends (FR-212). On an unexpected exception it writes `status="error"`.
  4. **Resume entry point**: add `resume_team(run_id, *, budget_usd=None, settings_overrides=None, services=None, agents...) -> TeamState` implementing the contracts/cli.md resume rules (raises `ResumeRefused(reason)`). It restores the workspace, appends the `ResumeAttempt`, and runs the graph with `resume_from`.

  Run `tests/test_resume.py`. The budget cases are added by T061 and pass after T071.
- [X] T043 [US1] Add signals, stale-container cleanup and exit codes to `orchestration/team.py`:
  - `install_signal_handlers(services)`: the first SIGINT or SIGTERM sets `services.stop_requested`, records the signal number, kills containers labelled with this run ID through `tools.python_exec.kill_run_containers(run_id)` (new), and cancels pending broker requests (`A2ABroker.close` path). A second signal makes a best-effort container kill, prints `interrupted again: exiting now` to stderr, then calls `os._exit(128 + signum)`.
  - Nodes check `services.stop_requested` at entry, and on a set flag return `status="failed"`, `end_reason="interrupted"`, routed to `finish`, which skips the QA report call if the broker is closing. A partial report is marked `partial` (spec edge case).
  - `tools/python_exec.py::cleanup_stale_containers(workspace_root)` lists `docker ps -a --filter label=multi-agent-lab.run_id` and removes containers whose run lock is not held.
  - `main()` calls the cleanup before the run and maps the result through `exit_code_for(state, signal_received)` (contracts/cli.md: 0, 1, 2, 3, 130 or 143; `a2a_failed` → 3).
  - `--resume RUN_ID` is added to `_parse_args`, mutually exclusive with the requirement argument and with `--max-iterations`/`--max-steps`/`--timeout`.

  Run `tests/test_lifecycle.py` and `tests/test_team_loop.py`, updating any existing test that asserted exit 1 for `a2a_failed`.
- [X] T044a [US1] Write `tests/test_injected_failures.py` (SC-201). Run 100 scripted team runs (real agents on content-aware fake models, fake sandbox, injected sleep). In run *i*, every model call fails temporarily with probability 1/5 before its scripted reply, using `random.Random(i)` to pick a 429, 529 or connection error. Compare each run's end reason with the same script run undisturbed: at least 95 of 100 must match. (100, not 20: the expected rate is about 96.7%, and 20 runs can't tell 95% from 90%.) Injection lives only in the fake (FR-239).
- [X] T044 [US1] Run the whole suite. Fix regressions in the existing tests caused only by the new state fields or by `max_retries=0`. Do not weaken assertions. Then run `python -m unittest tests.test_resilience tests.test_never_crashes tests.test_resume -v` and confirm US1's independent test.

**Checkpoint**: US1 is complete. Retries, breakers and degradation work and are tested. No node raises. Interrupted, killed and `dependency_unavailable` runs resume, and refused resumes change nothing. Exit codes follow FR-201a.

---

## Phase 4: User Story 2 - Operators Can See What Every Run Is Doing (Priority: P2)

**Goal**: A redacted JSONL event record and a run summary for every run, progress lines derived from events, and LangSmith tracing with one root per run, agent child spans carrying `agent_name`, `token_count`, `tool_calls` and `duration_ms`, and propagation across the A2A broker (directive 1, FR-214–FR-221).

**Independent Test**: `python -m unittest tests.test_events tests.test_tracing -v` (the redactor's tests, `tests.test_redaction`, run with Phase 2). Run a scripted team run with a recording LangSmith client: exactly one root, every `agent:*` span a descendant with the four metadata keys, and a planted API key found nowhere (quickstart §5 is the live version).

### Tests for User Story 2 ⚠️

The redaction tests (T045) and module (T049) are in Phase 2.

- [X] T046 [P] [US2] Write `tests/test_events.py` for `observability/events.py`:
  - each line validates against `contracts/run-event.schema.json` (with `tests/schema_check.py`, extended with `const`, `format` (ignored) and `exclusiveMinimum` if missing; copy the schema to `tests/fixtures/run-event.schema.json` with a `SchemaCopyTests`-style check like 002's);
  - lines are flushed immediately, so they are readable while the log is still open (US2 scenario 3);
  - `LOG_LEVEL=warning` drops `info` events, but `error`, `run_end` and the summary are always written;
  - an `OSError` on write sets `events_lost` and the run continues;
  - a planted key in a `message` detail is redacted;
  - `summarise(events) -> RunSummary` totals per agent and per stage match a hand-built event list.
- [X] T047 [P] [US2] Write `tests/test_tracing.py` for `observability/tracing.py` (contracts/tracing.md), using a recording client: a `unittest.mock.MagicMock(spec=langsmith.Client)` or a `langsmith.Client(auto_batch_tracing=False, api_key="test", api_url="http://127.0.0.1:9")` whose `create_run`/`update_run` are patched to capture. Confirm the exact capture hook against `langsmith 0.14.11`. Cases:
  - **Disabled**: with `LANGSMITH_TRACING` unset, no client is created, no network is touched, and `agent_span(...)` still returns a measured `duration_ms`.
  - **Span tree**: a `team-run` root with two `agent:pm` and `agent:coder` children, each with `llm:*`/`tool:*` grandchildren.
  - **Metadata**: the four keys with correct types and values (`token_count` = Σ prompt + completion of its llm spans; `tool_calls` = count of its tool spans).
  - **Usage**: `llm` spans carry `usage_metadata`.
  - **Redaction**: inputs, outputs and metadata are redacted through `hide_*`.
  - **Headers**: `headers()` returns string-only headers. `continue_from(headers)` on another thread (a `threading.Thread`) produces a span with the root's `trace_id` and the root as an ancestor.
  - **Bad headers**: malformed headers → a parentless span and one `warning` event, with no exception.
- [X] T048 [P] [US2] Add tracing-integration cases to `tests/test_team_loop.py`. Run the real team graph over the real `A2ABroker`, with `FakeCoder`/`FakeQA` wrapped by the real endpoints, `PMAgent` on `FakePMClient`, and a recording tracer. Assert that:
  - exactly one root exists;
  - every `agent:qa` span (run on the broker worker thread) and every `agent:coder` fix-round span share the root's `trace_id`;
  - `events.jsonl` has `a2a_exchange` events for every exchange, with `stage` `qa_review`/`fix`/`final_report`.

### Implementation for User Story 2

- [X] T050 [P] [US2] Create `observability/events.py`:
  - `class EventLog(path: Path | None, run_id, min_severity, redactor, clock)` with `emit(agent, stage, event, severity, outcome, **details)`, `events_lost` and `close()`. It opens in append mode with line buffering and redacts before `json.dumps`.
  - `summarise(events: Iterable[dict]) -> RunSummary`.
  - `severity_from_env(env)` reads `LOG_LEVEL`.

  Run `tests/test_events.py`.
- [X] T051 [US2] Create `observability/tracing.py`:
  - `class Tracer(enabled, client, project, redactor, on_warning)`.
  - `from_env(env, redactor)`: enabled only when `LANGSMITH_TRACING` is `true` and `LANGSMITH_API_KEY` is non-empty; builds `langsmith.Client(hide_inputs=redactor.redact, hide_outputs=redactor.redact, hide_metadata=redactor.redact, tracing_error_callback=...)`.
  - Context managers `root_span(run_id, metadata)`, `agent_span(agent, invocation, **metadata)` (yields a mutable `AgentSpanStats` with `tokens`, `tool_calls` and `duration_ms`; on exit it writes the four required keys with `run_tree.add_metadata`), `llm_span(model, request_summary)` and `tool_span(name, inputs)`.
  - `headers() -> dict[str, str]`, `continue_from(headers)`, and `root_url() -> str | None`.

  Only `trace`, `tracing_context`, `RunTree` and `Client` are used (constitution 1.4.1, Principle III). With tracing disabled, every context manager is a no-op that still measures time. Run `tests/test_tracing.py`.
- [X] T052 [US2] Extend `RunServices` with `events: EventLog` (path `workspace_root/.runs/<run_id>/events.jsonl`; `None`-path in-memory mode for unit tests) and `tracer: Tracer`. Wire `RunServices.on_event` from T033 into `events.emit`, so guards' `retry`, `restart`, `circuit_*` and `degraded` events land in the log. Breaker state changes also add a span event on the active span.
- [X] T053 [US2] Instrument `ModelCaller.call` (`agents/llm.py`): open `tracer.llm_span` around the guarded call, and fill `usage_metadata`, `served_from`, `attempts`, `circuit` and `cost_usd`, which is `None` until US3. Emit a `model_call` event with `model`, `tokens`, `duration_ms` (the whole guarded call, including waits and bookkeeping), `operation_ms` (the time inside `client.beta.messages.create` alone, summed over attempts) and `served_from`, and a `validation_failed` event (agent, `reply_type.__name__`, first 5 problems) when there are problems. Add the reply's tokens to the active agent span's stats.
- [X] T054 [US2] Open the agent spans (contracts/tracing.md table):
  - `PMAgent.write_spec`/`consolidate` → `agent:pm`;
  - `CoderAgent.run` → `agent:coder` (invocation `run`, `task_id`), `reflect` → `invocation="reflect"`, and the fix round in `messaging/endpoints.py::coder_handler` → `invocation="fix"`;
  - `QAAgent.run_iteration` → `agent:qa` (`review`, `iteration`) and `final_report` → `final_report`.

  Count tool calls through `tracer.tool_span` in `tools/__init__.py::dispatch_tool`, in QA's `_write_file`/`_read_sources`/`_run`, and in memory `query`/`add`. Emit `tool_call`/`sandbox_run` events with `duration_ms` and `operation_ms`, measured the same way as for `model_call`. The guard reports the operation time it measured. Emit `stage_start`/`stage_end`, and `checkpoint_saved` with `duration_ms`, from the `_checkpointed` helper (T042). Map the Coder's `CoderResult.validation_problems` (T017) to `validation_failed` events.
- [X] T055 [US2] Propagate trace context over A2A. In `orchestration/team.py`, `review_message`, `fix_message` and `report_message` add `"trace": services.tracer.headers()` to `metadata`. In `messaging/endpoints.py`, `qa_handler` and `coder_handler` wrap the agent call in `services.tracer.continue_from(message["metadata"].get("trace", {}))`. The endpoints receive `services` through `register_team_agents(broker, coder, qa, services=None)`, and `messaging/` imports only `observability.tracing`. `qa_node`, `fix_node` and `finish_node` emit `a2a_exchange` events. Run T048.
- [X] T056 [US2] Open the root span and write the summary in `orchestration/team.py::run_team` and `resume_team`:
  - wrap the graph stream in `tracer.root_span(run_id, {"run_id", "attempt", "requirement_chars", "max_iterations", "resumed_from_stage"})`. There is one root per attempt (FR-214a): add a case to `tests/test_resume.py` with a recording tracer where a resumed run gives a second root with the same `run_id`, `attempt == 2` and `resumed_from_stage` set;
  - emit `run_start`/`run_end`, and in `resume_team` a `resume` event with `from_stage`, `budget_usd` (old and new), `restored_files` and `removed_files` (FR-213d);
  - in the final `finally:`, compute `summary = summarise(...)` (cumulative across attempts via the checkpoint), set `state["summary"]`, `events_lost` and `trace_url`, and write them into `run.json`.

  Print `trace: <url>` to stderr in `main` when tracing is on.
- [X] T057 [US2] Derive the progress lines from events (FR-220). Move the stderr rendering in `orchestration/team.py` (`format_update` and the `_*_line` helpers) into an event formatter, `observability/events.py::progress_line(event) -> str | None`, attached as an `EventLog` subscriber in `main`, so the stderr output matches today's lines exactly. Add a golden test in `tests/test_events.py` that replays the scripted loop from `tests/test_team_loop.py` and compares the lines with the current README example lines (`[pm]`, `[handoff]`, `[coder]`, `[reflect]`, `[a2a]`, `[qa]`, `[fix]`, `[finish]`). Keep `print_step_progress` for the standalone Coder CLI.

- [X] T057a [US2] Redact the QA report and the CLI's own stderr lines (FR-219). `QAAgent.final_report`, including a report marked partial, passes the report text through `services.redactor.redact` before `_write_file`. `orchestration/team.py::main` passes every error and summary line it prints through the same redactor. Add a case to `tests/test_qa_agent.py`: a finding message containing a planted key is written to `.qa/report.md` as `[REDACTED]`. The full sweep across every output is T072a.

**Checkpoint**: US2 is complete. Every run has `events.jsonl` and a summary in `run.json`. Traces show one root per run with agent spans, including those on the broker thread, and secrets appear nowhere.

---

## Phase 5: User Story 3 - Model Spend Is Visible, Bounded and Reduced (Priority: P3)

**Goal**: Per-agent prompt and completion tokens and USD cost; a JSON cost report in `docs/cost_reports/` for every run; a per-run budget; prompt caching with a configurable TTL; the PM on Sonnet 5.5 and the Coder and QA on Haiku 5.5; benchmark cost comparison against the baseline (directives 3 and 7, FR-222–FR-226).

**Independent Test**: `python -m unittest tests.test_cost tests.test_prompt_caching -v`. A scripted run writes `docs/cost_reports/<run_id>.json` that validates against the schema, with per-agent costs summing to the total. A live run (quickstart §4) shows `cache_read_tokens > 0` for the Coder.

### Tests for User Story 3 ⚠️

- [X] T058 [P] [US3] Write `tests/test_cost.py`:
  - **`cost.pricing.cost_of(usage, model, ttl, table)`**: Sonnet 5.5 and Haiku 5.5 at the research R9 rates; cache read and write rates; the 5m/1h split from `cache_creation.ephemeral_*` when given; the Haiku long-context card above a 100K prompt; an unknown model → `None`; a table without `version` → `ValueError`.
  - **SC-204**: one call's cost is within 2% of a hand computation.
  - **`UsageLedger`**: thread-safe accumulation from 4 threads; per-agent `AgentUsage` fields; `cost_usd` becomes `None` when any served model is unpriced; a fallback model (a `response.model` different from the request's) is priced at its own rate; cached replays count in `cached_replays`, cost 0.
  - **`RunBudget`**: `check()` raises `BudgetExceeded` once spent ≥ cap; there is no cap by default; an unpriced model with a cap → `ValueError` at construction (FR-224).
  - **`cost.report.write_cost_report`**: the output validates against `contracts/cost-report.schema.json` (copied to `tests/fixtures/cost-report.schema.json`, with a copy check); the write is atomic; a planted key in the requirement is redacted in `requirement_summary`; a resume rewrites the file with two `attempts` and cumulative totals; a write failure returns `None` and is reported without raising; per-agent costs sum to the total within $0.000001 (SC-215).
- [X] T059 [P] [US3] Write `tests/test_prompt_caching.py` with `ScriptedClaude` recording request bodies (research R7 table):
  - **PM**: `system` is a list whose last block has `cache_control: {"type": "ephemeral"}`, plus `"ttl": "1h"` when `PROMPT_CACHE_TTL=1h`, and there is no marker when it is `off`.
  - **Coder `_think`**: a system-block marker plus top-level `cache_control`.
  - **Coder `_critique`**: markers on the system block and the sources block.
  - **QA `_ask`**: user content split into spec, sources and tests blocks, with a marker after the last stable block and the instruction last.
  - **Limits**: no request has more than 4 markers, and all of a request's markers use the same TTL.
  - **Append-only**: the Coder's history stays append-only, so consecutive `_think` requests share an identical prefix up to the previous message.
  - **Fingerprint**: the response-cache fingerprint is unaffected by the markers (T021).
- [X] T060 [P] [US3] Write `tests/test_model_defaults.py` (FR-225a/b, R8):
  - `CoderConfig()`/`QAConfig()` default to `claude-haiku-5-5`, with `use_fallbacks` off;
  - `PMConfig()` stays `claude-sonnet-5-5` with fallbacks on;
  - `CODER_USE_FALLBACKS=true` with a Haiku model → `ValueError` naming the agent, the setting and the model;
  - an effort the model doesn't support → `ValueError`;
  - an unknown model is allowed with a `warning` returned by `check_model_settings`;
  - `CODER_MODEL=claude-sonnet-5-5` turns fallbacks back on by default;
  - built requests contain no `betas`/`fallbacks` for Haiku.
- [X] T061 [P] [US3] Add budget cases to `tests/test_resume.py` and `tests/test_never_crashes.py`:
  - a scripted run with `budget_usd` just above the first call's cost ends `budget_exceeded` after at most one more call (SC-206), with a final record and a cost report;
  - `resume_team(run_id)` without a higher budget → `ResumeRefused`;
  - with a higher budget, it continues and the cost report shows cumulative totals;
  - the QA final report (no model call) still runs at the cap.
- [X] T062 [P] [US3] Add to `tests/test_benchmark.py`:
  - `record_for` includes `cost_usd`, `cost_by_agent` and `cost_report` from the final state;
  - `score`/the summary include `total_cost_usd`, `mean_cost_usd` and `median_duration_seconds`;
  - `--baseline FILE` prints the cost reduction (%) and the median-duration change;
  - a baseline file without cost fields (the pre-feature format) gets its cost computed from `input_tokens`/`output_tokens` at the `claude-sonnet-5-5` table rates, as in T003.

### Implementation for User Story 3

- [X] T063 [P] [US3] Create `cost/prices.json` per data-model §2.5, with version `2026-10-06`, the source URL of the Claude pricing page, and the research R9 rates for `claude-sonnet-5-5` and `claude-haiku-5-5` (Haiku with its `long_context` block, threshold 100000). Before committing, check the rates against the live pricing page. If they differ, use the live rates, bump `version` to the check date, and update research R9. Create `cost/pricing.py`: `load_price_table(path)`, `cost_of(usage, model, ttl, table) -> float | None` and `PRICE_TABLE_PATH` from the env.
- [X] T064 [P] [US3] Create `cost/ledger.py`:
  - `class UsageLedger` (lock, per-agent `AgentUsage`) with `record(agent, model_served, usage, cost, served_from, retries)`, `record_tool_call(agent)`, `record_error(agent)`, `add_duration(agent, ms)`, `snapshot() -> dict[AgentName, AgentUsage]` and `load(snapshot)`;
  - `class RunBudget(cap_usd, spent)` with `check()` and `add(cost)`;
  - `class BudgetExceeded(Exception)`.
- [X] T065 [P] [US3] Create `cost/report.py`: `write_cost_report(directory, state, ledger_snapshot, price_version, ttl, attempts, redactor) -> Path | None`, matching `contracts/cost-report.schema.json` exactly (all required keys; `requirement_summary` = the first 200 characters, redacted). Write `.<run_id>.json.tmp` and then `os.replace`. On `OSError`, print `cost report not written: <cause>` to stderr, emit an `error` event, and return `None`. `COST_REPORT_DIR` defaults to `docs/cost_reports`.
- [X] T066 [US3] Extend `RunServices` with `price_table`, `ledger: UsageLedger`, `budget: RunBudget` (`RUN_BUDGET_USD`/`--budget`) and `prompt_cache_ttl` (`PROMPT_CACHE_TTL` ∈ `5m`/`1h`/`off`, default `5m`; anything else → `ValueError`). In `ModelCaller.call`:
  1. Call `budget.check()` before anything else. `BudgetExceeded` → `ModelReply(failure=None, budget_exceeded=True)`.
  2. After a reply, compute `cost_of(usage, response.model, ttl)` and record it in the ledger.
  3. Fill `cost_usd` in the llm span and the `model_call` event.

  Map `budget_exceeded` like the failures in T035/T036: status `budget_exceeded` in the agents, end reason `budget_exceeded` in the team, and a `budget_stop` event. `finish_node` still requests QA's final report, which makes no model call.
- [X] T067 [US3] Add prompt-cache markers in `agents/llm.py`: `with_cache_markers(request, ttl, breakpoints)` places `cache_control` on the given block paths and keeps all markers on one TTL. Apply it, per research R7:
  - `pm_agent._request`: `system` becomes a one-block list with a marker;
  - `coder_agent.build_request`: a system-block marker plus top-level `cache_control` with the same TTL;
  - `_critique`: the sources moved into their own content block with a marker;
  - `qa_agent.build_qa_request`: `user_text` replaced by a content-block list (spec, sources, tests stable; instruction last), and `_review`/`_write_initial_tests`/`_resolve_disputes`/repair pass their parts separately instead of `"\n\n".join`.

  The response-cache fingerprint strips markers. Requires T003 done. Run `tests/test_prompt_caching.py` and the full agent tests.
- [X] T068 [US3] Record the ledger into the team state and write the cost report on every exit path. In `orchestration/team.py::run_team`/`resume_team`, in the same `finally:` as `write_final_record` (T042):
  - set `state["agent_usage"] = ledger.snapshot()` and `state["cost_usd"]` (the sum, or `None`);
  - compute `usage` from the ledger;
  - call `write_cost_report` and set `cost_report_path`;
  - print `cost report: <path>` to stderr in `main`.

  The checkpoint stores and restores the ledger (`ledger.load`). This applies to completed, failed, interrupted, `dependency_unavailable`, `budget_exceeded` and error runs.
- [X] T069 [US3] Count tool calls and durations per agent: `tracer.agent_span` exit calls `ledger.add_duration`, and `tool_span` calls `ledger.record_tool_call`. Check in a scripted test that, per agent, `agent_usage.tool_calls` equals the sum of that agent's spans' `tool_calls`, and that Σ span `token_count` equals the cost report's total tokens (SC-214, offline variant).
- [X] T070 [US3] Assign models by role (FR-225a/b, R8). In `agents/llm.py`, add `MODEL_CAPABILITIES = {"claude-sonnet-5-5": {"fallbacks": True, "efforts": EFFORT_LEVELS, "mid_conversation_system": True}, "claude-haiku-5-5": {"fallbacks": False, "efforts": EFFORT_LEVELS, "mid_conversation_system": True}}` and `check_model_settings(agent, model, effort, use_fallbacks) -> list[str]` (warnings), which raises `ValueError` for unsupported combinations.
  - Set `CoderConfig.model` and `QAConfig.model` to `"claude-haiku-5-5"`.
  - The `*_USE_FALLBACKS` default is "on if supported". `from_env` treats an unset variable as the default, and explicit `true` on Haiku → `ValueError`.
  - Call `check_model_settings` in each `__post_init__`.
  - `orchestration/team.py::main` refuses with exit 2 on a `ValueError` (existing path).

  Requires T003 done. Run `tests/test_model_defaults.py` and the full suite.
- [X] T071 [US3] Add budget and the remaining CLI options to `orchestration/team.py`: `--budget USD` (overrides `RUN_BUDGET_USD`; with `--resume` it may only raise the stored cap), and the start-up refusal when a budget is set and any configured model is unpriced (FR-224). `resume_team` budget rules come from contracts/cli.md. Run T061.
- [X] T072 [US3] Add cost to the benchmark in `benchmarks/run_benchmark.py`:
  - `record_for` adds `cost_usd`, `cost_by_agent` (from `agent_usage`) and `cost_report`;
  - the summary adds `total_cost_usd`, `mean_cost_usd` and `median_duration_seconds`;
  - add `--baseline FILE`, which prints `cost per item: $X → $Y (−Z%)` and the median-duration change, computing the cost of baseline records that lack `cost_usd` from tokens at the `claude-sonnet-5-5` rate (pre-feature, uncached);
  - `_run_item` passes a fresh `RunServices` per item (breakers and ledger per item, R5).

  Run `tests/test_benchmark.py`.
- [X] T072a [US3] Write `tests/test_secret_sweep.py` (SC-208, FR-219). It needs the checkpoint (T041), the QA report redaction (T057a) and the cost report on every exit path (T068), so it comes at the end of US3 and is not [P]. Plant dummy values of 8 or more characters in `MY_API_KEY`, `GH_TOKEN`, `DB_SECRET`, `DB_PASSWORD` and `CLOUD_CREDENTIAL`, plus a `sk-ant-api03-…`-shaped string that is not in the environment. Run one scripted team run (real agents on `ScriptedClaude`, fake sandbox, recording tracer) where:
  - a PM reply and a QA review finding echo the key-shaped string;
  - the fake sandbox's stdout echoes every planted value;
  - stderr is captured.

  Scan every file under `workspace/.runs/<run_id>/` (`events.jsonl`, `run.json`, `checkpoint.json`), the cost report, `.qa/report.md`, the captured stderr and the recorded trace payloads: 0 occurrences of any planted value. Generated program output stored in the workspace itself is exempt (FR-219) and is not scanned.
- [ ] T073 [US3] Run the **post-change benchmark** (live, about 10 runs, paid): `python -m benchmarks.run_benchmark --baseline benchmarks/results/<baseline>.json`. Record the results in plan.md under "Benchmark after models by role and prompt caching": the pass count (SC-102), SC-103, the mean cost reduction per item (SC-205: ≥ 40% against the baseline's $0.762, so ≤ $0.457), also reported per passed item (baseline $0.824), and the median duration (SC-205a: not higher than 223 s). See plan.md "Baseline benchmark (pre-feature)" (`benchmarks/results/20261020T053121Z-baseline.json`, 9/10 passed). If fewer than 8/10 pass (SC-102), apply research R8's quality fallback (QA back to `claude-sonnet-5-5` first), re-run, record both results, and keep the configuration that meets the quality bar as the default. Afterwards, run `grep -rlE 'sk-ant-[A-Za-z0-9_-]{8,}'` over the results file, `docs/cost_reports/` and `workspace/.runs/`, which must find nothing (SC-208, benchmark part). This is an operator task: confirm the spend before starting.

**Checkpoint**: US3 is complete. Every run writes a valid cost report. Spend is per agent and capped. Prompt caching is measurable. The Coder and QA run on Haiku by default, with the quality and cost results recorded against the baseline.

---

## Phase 6: User Story 4 - The Team Deploys as a Container (Priority: P4)

**Goal**: A pinned, non-root, secret-free application image. `docker-compose.yml` with a healthy-gated Chroma service and the agent. A health check that makes no model call. `docker compose up` from a clean state runs the pipeline end to end (directive 6, FR-227–FR-234).

**Independent Test**: quickstart §8. From a clean state, `docker compose up --build --abort-on-container-exit --exit-code-from agent` exits 0, Chroma is healthy before the agent's first line, and the outputs are on the host owned by the user. `python -m unittest tests.test_health tests.integration.test_compose_config -v` passes.

### Tests for User Story 4 ⚠️

- [X] T074 [P] [US4] Write `tests/test_health.py` for `orchestration/health.py` (contracts/cli.md, data-model §2.6), with each probe patched:
  - every item `ok` → exit 0;
  - a missing `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` → `credentials missing`, exit 2;
  - `docker version` failing → `docker error`;
  - a missing sandbox image that can't be built → `sandbox_image`;
  - an unwritable workspace or cost-report dir;
  - a failing Chroma heartbeat (server mode) or an unwritable path (embedded mode);
  - a model missing from the price table → `price_table missing`;
  - LangSmith is reported as `info` only;
  - `--json` output matches `HealthReport`;
  - no Anthropic client is ever constructed (patch `anthropic.Anthropic` to raise).
- [ ] T075 [P] [US4] Write `tests/integration/test_compose_config.py` (skip without `docker compose`). Run `docker compose config --format json` with dummy `HOST_UID`/`HOST_GID`/`DOCKER_GID` and assert the contracts/deployment.md invariants 1–6:
  - `depends_on.chroma.condition == "service_healthy"`, and `chroma` has a `healthcheck`;
  - only `agent` mounts `/var/run/docker.sock`;
  - no secret-named variables with values in any `environment`;
  - every pulled image has `@sha256:` (the locally built `multi-agent-lab:local` tag is exempt);
  - the workspace's host path equals its container path;
  - no `ports`;
  - `agent.build.target == "runtime"`.

  Also parse `Dockerfile`: a `test` stage exists and the last stage is `runtime`; every `FROM` or `COPY --from` that names an external image carries an `@sha256:` digest (internal stage references such as `FROM app AS test` are exempt), there is a `USER` other than root, and `.dockerignore` includes `.env*`.
- [ ] T076 [P] [US4] Write `tests/integration/test_memory_concurrency.py` (skip without Docker). Start a throwaway `chromadb/chroma:1.5.9@sha256:<digest>` container on a random host port, wait for the heartbeat, then start 5 `multiprocessing` processes, each using `SemanticMemory` in HttpClient mode to `add` 20 lessons with its own run ID and then `query`. Assert that there were no exceptions, `count() == 100`, every recorded ID can be fetched by ID, and every document is complete (SC-213). Remove the container in `tearDown`.
- [X] T077 [P] [US4] Add HttpClient cases to `tests/test_agent_memory.py` with a patched `chromadb.HttpClient`: `CHROMA_HOST` set → `HttpClient(host, port)` is used, otherwise `PersistentClient(path)`; the same default embedding function in both modes; record IDs stay `f"{run_id}-{n}"` and unique per run.

### Implementation for User Story 4

- [X] T078 [US4] Add a Chroma server mode in `agents/memory.py`. `SemanticMemory.__init__(path, collection="coder_memory", guard=None, host=None, port=8000)` uses `chromadb.HttpClient(host=host, port=port, settings=Settings(anonymized_telemetry=False))` when `host` is given, otherwise the existing `PersistentClient`. `CoderConfig` gains `chroma_host` and `chroma_port` from `CHROMA_HOST`/`CHROMA_PORT`, and `CoderAgent` passes them. The guarded first connection (T040) retries with backoff, so a Chroma that is still starting is waited for. Run T077 and T076.
- [X] T079 [P] [US4] Add `QA_OUTPUT_ROOT` in `tools/test_runner.py`. `run_pytest` creates its `qa-out-*` temporary folder under `os.environ.get("QA_OUTPUT_ROOT")` when set, creating it with `mkdir(parents=True, exist_ok=True)`, otherwise under the system temp dir. This lets the JUnit bind mount resolve on the host under Compose (R16). Add a case to `tests/test_test_runner.py`.
- [X] T080 [US4] Create `orchestration/health.py` (R17, contracts/cli.md): `check_health(env, coder_config, qa_config, pm_config) -> HealthReport` with one small function per item, plus `main(argv)` (`--json`, exit 0/2) and an `if __name__ == "__main__":` guard. `orchestration/team.py::main` runs `check_health` before the run and before stale cleanup, and refuses with exit 2 listing every failing item, before any model call (US4 scenario 4). Run `tests/test_health.py`.
- [ ] T081 [US4] Look up and pin the digests (network required):
  - `docker buildx imagetools inspect chromadb/chroma:1.5.9 --format '{{json .Manifest}}'` gives the index digest;
  - the same for `docker:26.1.3-cli`.

  Record both in research R16. Inspect the pinned Chroma image for a shell and HTTP tools (`docker run --rm --entrypoint sh chromadb/chroma:1.5.9@sha256:<d> -c 'command -v bash curl wget python3'`) and fix the health-check command: prefer `curl -fsS http://localhost:8000/api/v2/heartbeat`, then `python3 -c urllib…`, then the bash `/dev/tcp` probe from R16. Confirm `/api/v2/heartbeat` answers on a running container.
- [ ] T082 [US4] Create the root `Dockerfile` per contracts/deployment.md:
  - `FROM python:3.12-slim@sha256:a6e34c…`;
  - `COPY --from=docker:26.1.3-cli@sha256:<d> /usr/local/bin/docker /usr/local/bin/docker`;
  - `WORKDIR /app`, `COPY requirements.txt .`, `RUN pip install --no-cache-dir -r requirements.txt`;
  - create user `app` with `HOME=/home/app`;
  - warm the embedding model (`RUN python -c "import agents.memory as m; from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()(['warm-up'])"`, run as `app` after `COPY . .`), then `chmod -R a+rX /home/app` so that any run-time UID can read the model cache;
  - `USER app`;
  - `ENV PYTHONUNBUFFERED=1 HOME=/home/app`;
  - `ENTRYPOINT ["python", "-m", "orchestration.team"]`.

  Name that stage `app`. Add two stages after it:
  - `FROM app AS test`: as root, `pip install --no-cache-dir -r requirements-dev.txt`, which adds only `coverage`, then `USER app` again. It is used to run the suite inside the image (FR-238, US5 scenario 3).
  - `FROM app AS runtime`: the last stage, so a plain `docker build .` and Compose produce the image without dev tools.

  Create `.dockerignore` with the exclusions from the contract. Build with `docker build -t multi-agent-lab:local .` and check `docker run --rm --entrypoint docker multi-agent-lab:local --version`.
- [ ] T083 [US4] Create `docker-compose.yml` per contracts/deployment.md: the `chroma` service with its pinned digest, `chroma-data` volume, the health check from T081 and no ports; the `agent` service with `build: {context: ., target: runtime}`, `depends_on` service_healthy, `env_file: .env`, the environment block, `user`/`group_add` from `HOST_UID`/`HOST_GID`/`DOCKER_GID` (with `:?` errors), the five volumes including the same-path workspace mount and the socket, and `command: ["${TEAM_REQUIREMENT:-A command-line tool that converts temperatures between Celsius and Fahrenheit}"]`. Create `.env.example` (committed) listing `ANTHROPIC_API_KEY=`, `HOST_UID=`, `HOST_GID=`, `DOCKER_GID=`, `TEAM_REQUIREMENT=` and the optional `LANGSMITH_*`, with comments and no values. Run `tests/integration/test_compose_config.py`.
- [ ] T084 [US4] Verify **from a clean state** (quickstart §8, live, about 1 team run of spend):
  1. `docker compose down -v --remove-orphans`, remove both local images, clear `workspace/*`, `workspace/.runs`, `.cache` and `docs/cost_reports/*.json`, and fill in `HOST_UID`, `HOST_GID` and `DOCKER_GID` in `.env`.
  2. Run `docker compose up --build --abort-on-container-exit --exit-code-from agent`, capturing the log. Confirm that:
     - the exit code is 0;
     - `chroma` turns `healthy` before the agent's first `[pm]` line (SC-218);
     - the sandbox image was built through the host engine;
     - `workspace/<run_id>/`, `workspace/.runs/<run_id>/{events.jsonl,run.json,checkpoint.json}` and `docs/cost_reports/<run_id>.json` exist on the host, owned by `$(id -u)`.
  3. Run the follow-up checks from quickstart §8: the health check in a container, a second requirement via `docker compose run --rm agent "…"`, `docker history` containing no `sk-ant-`, no `.env` in `/app`, and `docker.sock` only under `agent`.

  Record the outcome (with timings) in plan.md under "Deployment verification". Fix and repeat until it passes.

**Checkpoint**: US4 is complete. The containerised team runs one-shot jobs end to end with Chroma as a service, the sandbox guarantees still hold, and the image holds no secrets.

---

## Phase 7: User Story 5 - A Full, Fast, Offline Test Suite Guards Every Change (Priority: P5)

**Goal**: Each agent has unit tests with fully mocked dependencies. There are integration tests for PM + Coder and Coder + QA, and one end-to-end test whose output passes a supplied fixture. Coverage is at least 85% through one command, and the suite is CI-ready (directive 5, FR-235–FR-239).

**Independent Test**: `scripts/test.sh; echo $?` with no API key and no network prints 0 and coverage ≥ 85%, in under 5 minutes (quickstart §1). Each skip states its reason.

- [X] T085 [US5] Make the Coder unit tests fully mocked (FR-235a). Move the 5 `@requires_docker` tests from `tests/test_coder_agent_loop.py` unchanged into `tests/integration/test_coder_sandbox.py` (same skip decorator). Re-add mocked versions in `tests/test_coder_agent_loop.py` that patch `tools.python_exec.exec_python` (and `agents.coder_agent.exec_python`/`ensure_sandbox`) with a fake returning scripted `ExecutionResult`s, so the Coder unit module needs no Docker, no MCP subprocess, no Chroma and no network. Add a guard test asserting that importing and running `tests.test_coder_agent_loop` with `shutil.which("docker")` patched to `None` skips nothing.
- [X] T086 [P] [US5] Audit `tests/test_pm_agent.py` and `tests/test_qa_agent.py` for full mocking. No test constructs a real `anthropic.Anthropic` without a fake client. QA tests use `FakeFileTools` and a fake `run_tests`, never the MCP subprocess or Docker. The schema-contract tests stay. Convert any test that touches a real dependency (move the real-dependency version to `tests/integration/`), and add a module-level assertion helper `assert_no_live_dependencies()` in `tests/fakes/__init__.py` that patches `anthropic.Anthropic`, `subprocess.run` for `docker` and `chromadb.PersistentClient` to raise, used by each agent unit module's `setUpModule`.
- [X] T087 [P] [US5] Create `tests/integration/test_pm_coder.py` (FR-235b, contracts/test-suite.md):
  - **Setup**: real `PMAgent` and `CoderAgent` on two `ScriptedClaude`s, the real team graph via `run_team` with `FakeQA` returning `complete` on the first review, the real MCP server over stdio (the Coder's real `McpFileTools`), a fake sandbox (patched `exec_python`), a temp workspace and cost-report dir, and a recording tracer.
  - **The PM script** returns a 3-task spec with distinct acceptance criteria. The Coder script writes one file per task through real `write_file` tool calls and reads the earlier file in task 2.
  - **Assertions**:
    - tasks arrive in order, and each Coder request's brief contains its task's acceptance criteria verbatim;
    - all files are in one workspace;
    - `agent_usage.pm` and `agent_usage.coder` are both > 0 and priced;
    - one trace root, with `agent:pm` and 3 `agent:coder` (`run`) descendants;
    - the cost report validates.
  - **Variant**: a PM reply failing validation → `pm_error` and the Coder is never called.
- [ ] T088 [P] [US5] Create `tests/integration/test_coder_qa.py` (FR-235b; skip without Docker).
  - **Setup**: real `CoderAgent` and `QAAgent` on `ScriptedClaude`, registered on a real `A2ABroker` through `register_team_agents`; the real MCP servers; the real sandbox and pytest; the team graph started at the build stage via `run_team` with a `FakePM`-style `PMAgent` on `FakePMClient` (one task).
  - **Script**: the Coder writes `calc.py` with a deliberate off-by-one. QA's first `WRITE_TESTS_FORMAT` reply has a test that fails on it, and its review reports a `failing_test` finding. The Coder fix round (`fix-program` over A2A) corrects the bug. QA review #2 → `complete`.
  - **Assertions**:
    - two iterations, with JUnit-derived counts (1 failed, then 0);
    - the `a2a_log` has `review-program`, `fix-program`, `review-program` and `final-report`;
    - `agent:qa` spans (run on the broker thread) are descendants of the single root;
    - `agent_usage.qa` and `agent_usage.coder` are filled;
    - `.qa/report.md` exists.
- [X] T089 [P] [US5] Create the e2e fixtures (FR-235c). `tests/fixtures/e2e/requirement.txt` holds a realistic requirement that fixes the public interface:
  - an inventory module `inventory.py` with `add_item(inventory: dict[str, int], name: str, quantity: int) -> dict[str, int]`, `remove_item(...)` (raises `ValueError` on unknown items or insufficient stock, and on non-positive quantities), `report(inventory) -> str` (sorted `name: quantity` lines);
  - a CLI `main.py` reading commands from a file argument (`add NAME QTY`, `remove NAME QTY`, `report`), exiting 1 with an `error:` line on bad input.

  `tests/fixtures/e2e/test_inventory_fixture.py` is the supplied pytest fixture, never shown to the agents: about 12 tests over the functions, the error cases and the CLI via `subprocess.run([sys.executable, "main.py", ...])`. Also add `tests/fixtures/e2e/scripted_run.py`, the content-aware `ScriptedClaude` steps for PM (spec + 2 tasks), Coder (writes `inventory.py`, `main.py`, runs them), critique, revision, QA (tests, review → one blocking finding), fix, and QA review #2 → complete, producing a program that really passes the fixture.
- [ ] T090 [US5] Create `tests/e2e/test_pipeline.py` (contracts/test-suite.md "End to end"). Both variants assert:
  1. `completed`/`complete`;
  2. `python -m py_compile` passes for every `.py` file in the sandbox (run through `exec_python` on a generated helper in a scratch copy);
  3. the main file runs with exit 0;
  4. the supplied fixture, copied into a scratch copy's `tests/` and run with `tools.test_runner.run_pytest`, gives `loaded` true and 0 failed/errors;
  5. the cost report validates against the schema;
  6. (scripted only) one trace root with `agent:pm`, `agent:coder` and `agent:qa` spans.

  The variants:
  - `test_scripted_pipeline_passes_supplied_fixture`: skip without Docker; real agents on `scripted_run.py`'s `ScriptedClaude`.
  - `test_live_pipeline_passes_supplied_fixture`: `@skipUnless(os.environ.get("RUN_LIVE_E2E") == "1" and key and docker)`; real models; prints the cost report path and total.
- [X] T091 [US5] Reach the coverage gate. Run `scripts/test.sh`. For each module below 85% in `coverage report`, add focused unit tests (prefer covering error paths and end reasons) until the total is ≥ 85%. Make sure every end reason in `EndReason` and every recovery path (retry, restart, breaker open, half-open, cache replay, structured error, resume) is exercised by at least one test: add `tests/test_coverage_of_end_reasons.py`, which asserts that a test-produced state exists for each `typing.get_args(EndReason)` value by importing the scenarios from the other test modules' helpers. Do not add `# pragma: no cover` except on `if __name__ == "__main__":` lines. Finally, change the last line of `scripts/test.sh` to `coverage report --fail-under=85` and confirm it exits 0.
- [ ] T092 [US5] Check the suite's speed and CI-readiness (FR-238, SC-209):
  - time `scripts/test.sh` on the dev VM (< 5 min); if slower, cut real sleeps (all retry and breaker tests must use injected sleeps and clocks) and Docker-heavy duplication;
  - run it with no network (`unshare -rn scripts/test.sh` where available, or with `HTTPS_PROXY=http://127.0.0.1:9`) and no API key → exit 0;
  - run it inside the image: `docker build --target test -t multi-agent-lab:test .`, then run `scripts/test.sh` in that image with the same socket mount, `group_add`, user and same-path workspace mount as the `agent` service (the exact `docker run` line from contracts/deployment.md "Running the suite in the image"). It gives the same pass and skip results as the host (US5 scenario 3);
  - confirm every skip message states a reason;
  - **overhead (SC-210, SC-212)**: run the scripted e2e pipeline 5 times with a fixed 200 ms latency in the fake model. From the events, compute per run the time spent in guard bookkeeping, which is Σ (`duration_ms` − `operation_ms`) over the `model_call`/`tool_call` events. Retry waits count as zero because no failure is injected and in `checkpoint_saved` (`duration_ms`). Each must be at most 5% of the median run duration. Record the figures in plan.md under "Quickstart validation".

**Checkpoint**: US5 is complete. One command gives a pass or fail including coverage, offline and in the container. All three test layers exist.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T093 [P] Update `README.md` (directive 8):
  - **Status**: increment 4, production hardening.
  - **Architecture**: redraw the mermaid diagram with the `team-run` root trace, the guards around the model service, MCP, sandbox and Chroma, the Chroma service, and cost reports.
  - **New sections**:
    - "Reliability: retries, circuit breakers and graceful degradation" (the settings table from contracts/resilience.md);
    - "Observability: events, run summary and LangSmith tracing" (how to enable tracing; the span tree; redaction);
    - "Cost: per-agent tokens, cost reports, budgets and prompt caching", with a sample `docs/cost_reports/<run_id>.json`;
    - "Validating model output";
    - "Resuming a run";
    - "Deployment with Docker Compose", covering: prerequisites, `.env`, `docker compose up` from a clean state, health check, `run`/`--resume` in containers, upgrade and removal, and the **trust exception** text required by FR-233 (contracts/deployment.md).
  - **Safety section**: change "No container is given the Docker socket" to "No sandbox container is given the Docker socket; the application container is, as a documented trust exception".
  - **Configuration table**: `CODER_MODEL`/`QA_MODEL` default → `claude-haiku-5-5`, the `*_USE_FALLBACKS` defaults, and every new variable from contracts/cli.md.
  - **Exit codes**: add 3, 130 and 143, and state that `a2a_failed` moved from 1 to 3 (FR-201a).
  - **`TeamState` table**: the new fields.
  - **Tests**: the layers, `scripts/test.sh`, the coverage gate, `RUN_LIVE_E2E`, and the new modules in the table.
  - **Design decisions**: Haiku for the Coder and QA (with the benchmark result), LangSmith limited use, `TypedDict` validation instead of Pydantic, checkpointing without LangGraph checkpointers.
  - **Project layout**: the new packages and files.
  - **Embedded vs server Chroma**: embedded is one run at a time (FR-230a).
- [X] T094 [P] Update `specs/003-production-hardening/quickstart.md` and `research.md` with any value that changed during implementation: the digests, the health-check command, the confirmed LangSmith capture hook, and the price-table version.
- [ ] T095 Run the full quickstart (§1–§10) top to bottom on a clean checkout. Record pass or fail per section in plan.md under "Quickstart validation", and fix any failing step.
- [X] T096 Re-check the Constitution Check in plan.md against the delivered code:
  - functions ≤ about 40 lines (`grep`/`ast` script over the new modules);
  - no bare `except:`;
  - no module-level mutable state in `resilience/`, `observability/` or `cost/`;
  - `langsmith` is used only through the approved names (`grep -rn "from langsmith\|import langsmith"`);
  - no `langchain` imports;
  - `coverage` is not imported anywhere;
  - `requirements.txt` and `requirements-dev.txt` are pinned;
  - `httpx2` is imported only under `tests/` (`grep -rln "httpx2" --include=*.py . | grep -v '^./tests/'` prints nothing; constitution 1.4.0);
  - `pysqlite3` is imported only in `agents/memory.py` (constitution 1.4.1);
  - no failure-injection switch exists in production code or configuration (FR-239): no environment variable or option named like `*INJECT*`, `*FAIL*`, `*CHAOS*` or `*FAULT*` outside `tests/`, and no test-only hook on production classes.

  Fix violations or record justified ones in Complexity Tracking.
- [ ] T097 Run `/speckit-analyze` for cross-artifact consistency (spec ⇄ plan ⇄ tasks ⇄ code) and resolve its findings.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: T001–T002 are done. T003 (baseline) runs in the background and MUST finish before T067 and T070. T004–T007 have no dependencies.
- **Foundational (Phase 2)**: depends on T004 and T005. It blocks every story.
- **US1 (Phase 3)**: depends on Foundational. It is the MVP.
- **US2 (Phase 4)**: depends on Foundational. Its new modules (T050–T051) can start in parallel with US1. Its wiring tasks (T052–T057) touch `ModelCaller`, `RunServices`, `team.py` and `checkpoint.py` after US1's T033–T043.
- **US3 (Phase 5)**: depends on Foundational. Its modules (T063–T065) can start in parallel. Its wiring (T066–T072) follows US1, since `budget_exceeded` resume and the cost report on every exit path depend on T042/T043, and follows US2's T053–T054, since span cost and tool-call counts use the tracer. T067 and T070 also need T003. T072a needs T041, T057a and T068. T073 needs all of US3.
- **US4 (Phase 6)**: the health check, compose files and Dockerfile (T074–T083) depend only on Foundational plus T040 (the guarded memory). T084 (clean-state verification) needs US1–US3 merged so that the full pipeline, cost report and events are what gets verified.
- **US5 (Phase 7)**: T085–T086 can start right after Foundational. T087–T090 need US1–US3, because they assert spans, ledgers and cost reports. T091–T092 come last.
- **Polish (Phase 8)**: after all stories.

### User story dependencies (completion order)

```text
Setup ─▶ Foundational ─▶ US1 (P1, MVP) ─▶ US2 (P2) ─▶ US3 (P3) ─▶ US4 verification (T084)
                     │         ▲              ▲            ▲
                     ├─ US2 modules (T050–T051) ─┘            │
                     ├─ US3 modules (T063–T065) ──────────────┘
                     ├─ US4 build pieces (T074–T083)
                     └─ US5 mocking (T085–T086) ─▶ US5 integration/e2e (T087–T092, after US3)
```

### Within each story

The tests come first and must fail, then the new modules, then the wiring into `ModelCaller`, the agents and `team.py`, then a story-wide test run at the checkpoint.

## Parallel Opportunities

- **Setup**: T005, T006 and T007 together.
- **Foundational**: T009 (validation tests), T012 (ScriptedClaude), T045 and T049 (redaction) in parallel with T008. T016, T017 and T018 touch different agent files but all depend on T015, so run them one after another, or in parallel once T015 is merged.
- **US1 tests**: T020–T028 are all [P] (separate test files). T044a follows the US1 implementation.
- **US1 modules**: T029, T030 and T032 in parallel. T031 follows T029/T030.
- **US2**: T046–T048 together. T050 while US1 wiring proceeds.
- **US3**: T058–T062 together. T063, T064 and T065 together.
- **US4**: T074–T077 together. T079 alongside T078.
- **US5**: T086, T087, T088 and T089 together.

### Parallel example: US1 test-writing round

```text
Task: "T020 [US1] tests/test_resilience.py — classification, backoff, breaker, guard"
Task: "T021 [US1] tests/test_response_cache.py — fingerprint, TTL, atomic files"
Task: "T023 [US1] tests/test_tool_resilience.py — MCP restart, sandbox start retry, labels"
Task: "T026 [US1] tests/test_checkpoint.py — atomic save, restore, lock"
Task: "T027 [US1] tests/test_resume.py — interrupt in every stage, refused cases"
```

### Parallel example: new modules while US1 wiring lands

```text
Task: "T051 [US2] observability/tracing.py"
Task: "T050 [US2] observability/events.py"
Task: "T063 [US3] cost/prices.json + cost/pricing.py"
Task: "T064 [US3] cost/ledger.py"
Task: "T065 [US3] cost/report.py"
```

## Implementation Strategy

### MVP first (User Story 1)

1. Phase 1 (T004–T007), with T003 running in the background.
2. Phase 2 (T008–T019, plus T045/T049). After it, all model output is validated (directive 4, model side) and every record is redacted.
3. Phase 3 (T020–T044). Then stop and validate: quickstart §3, plus the never-crashes and resume suites.

At this point, runs survive transient failures, never crash, and can be resumed. That is the foundation every other story's guarantees rely on.

### Incremental delivery

1. US1 → robust runs (directives 2 and 4).
2. US2 → visible runs, with LangSmith traces (directive 1).
3. US3 → costed and cheaper runs, with cost reports and models by role (directives 3 and 7), measured against the T003 baseline (T073).
4. US4 → the Compose deployment verified from a clean state (directive 6).
5. US5 → the full test layers and the coverage gate (directive 5).
6. Polish → README (directive 8), quickstart run, constitution re-check, `/speckit-analyze`.

Each story ends at a checkpoint with the suite green. Commit per phase. Constitution
rule: one concern per commit. Keep the governance commits (T001/T002) separate from
code.

### Live-spend tasks (confirm before running)

T003 (baseline, already running), T073 (post-change benchmark, about 10 runs), T084
(Compose verification, 2 runs), T090 live variant (opt-in), and quickstart §4–§6 and §8
in T095.

## Notes

- [P] means different files with no dependency on an unfinished task.
- [Story] labels map tasks to spec user stories for traceability.
- No failure-injection switch may exist in production configuration (FR-239). All injection goes through test fakes (`ScriptedClaude`, patched `_docker`, `FakeFileTools`, injected clocks and sleeps).
- Never send a planted secret anywhere real. Secret tests use dummy values only.
