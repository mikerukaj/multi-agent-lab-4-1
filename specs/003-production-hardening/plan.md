# Implementation Plan: Production Hardening of the Multi-Agent Development Team

**Branch**: `dev` (spec directory: `003-production-hardening`) | **Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/003-production-hardening/spec.md`, plus the
user's planning directives (2026-10-19):

1. **Observability**: LangSmith distributed tracing for all three agents. Every agent
   invocation is a child span of one root trace per run, with the metadata
   `agent_name`, `token_count`, `tool_calls` and `duration_ms`.
2. **Error handling**: retry for every LLM call and tool call (at most 3 retries,
   exponential backoff with jitter). A circuit breaker that opens after 3 consecutive
   failures and half-opens after a configurable cool-down. If the circuit is open, the
   caller gets a cached result or a structured error, and the run never crashes.
3. **Cost optimization**: per-agent token tracking (prompt tokens, completion tokens,
   estimated USD). Prompt caching with a configurable TTL. Models by role: the PM keeps
   its current model, and the Coder and QA move to Claude Haiku.
4. **Security**: every LLM output is checked against the project's `TypedDict` schemas
   before it is used.
5. **Tests**: a unit test suite for each agent with fully mocked LLM responses (existing
   agent unit tests made fully mocked), integration tests for PM + Coder and Coder + QA,
   and one end-to-end test that runs the whole pipeline on a realistic task and checks
   that the output is executable Python that passes a supplied test fixture.
6. **Docker**: a `Dockerfile` and a `docker-compose.yml` with an agent service and a
   Chroma service. The agent waits for a Chroma health check. `docker compose up` from
   a clean state runs the pipeline end to end.
7. **Cost report**: every run writes a JSON file to `docs/cost_reports/` with per-agent
   token use, per-agent cost and the total cost.
8. **README** updated.

Where a directive conflicts with the spec, the directive wins. The conflicts are listed
in [Spec deltas](#spec-deltas-directives-that-change-the-spec). The spec should be
amended to match before `/speckit-tasks`.

## Summary

Harden the existing PM → Coder ⇄ QA team without adding an agent or changing any role.
All model traffic goes through one new per-agent call layer, so that retries, circuit
breaking, degradation, prompt caching, output validation, token and cost accounting, and
tracing are each written once:

1. **`ModelCaller`** (`agents/llm.py`) replaces the five `beta.messages.create` call sites
   (PM `_call`, Coder `_critique`/`_think`/`_finalize`, QA `_ask`). On each call it:
   checks the budget, opens an LLM span, adds `cache_control` markers, makes the request
   through the dependency guard (retry inside a circuit breaker), records the usage and
   cost, writes successful responses to the response cache, and returns either a
   response or a structured `DependencyFailure`. The SDK's own retries are turned off
   (`max_retries=0`), so retries happen in one place only.
2. **Resilience** (`resilience/`): the failure classifier, `RetryPolicy` (3 retries,
   exponential backoff with equal jitter, waits capped at 60 s, honours `retry-after`),
   a thread-safe `CircuitBreaker` per dependency (`anthropic:<model>`, `mcp`, `sandbox`,
   `chroma`; opens after 3 consecutive failed calls, half-opens after
   `CIRCUIT_COOLDOWN_SECONDS`), and a TTL `ResponseCache` that serves degraded LLM calls.
   Tool calls (MCP file tools, sandbox runs, pytest runs, Chroma) use the same guard.
   Degraded tool calls return structured errors and never cached data.
3. **Observability** (`observability/`): LangSmith spans through the `langsmith` SDK. It
   is already installed through `langgraph` → `langchain-core` and is now declared. There
   is one `team-run` root per run, `agent:<name>` child spans with the four required
   metadata keys, and `llm`/`tool` grandchildren. Trace headers travel in A2A message
   metadata so QA's and the fix round's spans, which run on the broker thread, stay in
   the same trace. The spec's local JSONL event record and run summary come from the
   same instrumentation points. One redactor scrubs secrets from traces, events,
   records, checkpoints and reports.
4. **Cost** (`cost/`): a dated price table (`cost/prices.json`), per-agent
   `AgentUsage` in `TeamState`, a per-run budget, and a cost report written atomically
   to `docs/cost_reports/<run_id>.json` at the end of every run, however it ends.
   Prompt caching uses Anthropic `cache_control` with `PROMPT_CACHE_TTL` (`5m` or `1h`).
   The defaults become: PM `claude-sonnet-5-5` (unchanged), Coder and QA
   `claude-haiku-5-5`, with refusal fallbacks off for Haiku because it has no
   server-side fallback.
5. **Validation** (`orchestration/validation.py`): a small standard-library validator
   for the project's `TypedDict`/`Literal` types. Every model reply has a reply
   `TypedDict` that mirrors its JSON schema. Replies, tool-use inputs and cached
   responses are checked before use. A failure takes the agent's existing malformed-reply
   path and never raises out of a node.
6. **Run lifecycle** (spec US1): stdlib checkpoints after each stage and
   `--resume <run_id>` (a `resume` router at the graph's START), SIGINT/SIGTERM handling,
   containers labelled per run with start-up cleanup, the new end reasons
   `dependency_unavailable`, `budget_exceeded` and `interrupted`, and exit codes 0/1/2/3/130/143.
7. **Deployment**: a root `Dockerfile` (pinned base, pinned Docker CLI, non-root, Chroma
   embedding model baked in, no secrets) and a `docker-compose.yml` with `chroma`
   (`chromadb/chroma:1.5.9`, pinned by digest, heartbeat health check) and `agent`
   (`depends_on: service_healthy`, the host Docker socket as a documented trust
   exception, the workspace mounted at the same absolute path so that sandbox bind mounts
   resolve on the host). `SemanticMemory` uses `chromadb.HttpClient` when `CHROMA_HOST`
   is set, which also gives concurrent runs a safe shared store. `python -m
   orchestration.health` checks every prerequisite without a model call.
8. **Tests**: unit (each agent fully mocked: LLM, sandbox, MCP, memory),
   `tests/integration/` (PM + Coder, Coder + QA over the real broker and MCP server),
   `tests/e2e/` (the full pipeline with a scripted model and the real sandbox, checked
   against a supplied pytest fixture, plus an opt-in live variant). Coverage must be at
   least 85% (dev-only `coverage`). One command: `scripts/test.sh`.
9. **README**: architecture diagram (tracing, guards, Chroma service), configuration
   table, exit codes, deployment, cost reports, tests.

## Technical Context

**Language/Version**: Python 3.12 (application, application image and sandbox image)

**Primary Dependencies**: `anthropic==1.11.0`, `langgraph==1.2.12`, `chromadb==1.5.9`,
`mcp==2.3.0`, `python-dotenv==1.2.3`, plus **`langsmith==0.14.11`**. LangSmith is already
installed through `langgraph` → `langchain-core`. It is now declared and pinned to the
version a fresh install of `requirements.txt` resolves (2026-10-19). Dev-only:
**`coverage==7.16.2`** in `requirements-dev.txt`. Images: `python:3.12-slim` (existing digest),
`docker:26.1.3-cli` (only its static `docker` binary is copied), `chromadb/chroma:1.5.9`.

**Storage**: Files. `workspace/<run_id>/` is the program and tests, mounted into the
sandbox. `workspace/.runs/<run_id>/` holds `checkpoint.json`, `events.jsonl`, `run.json`
and `lock`; it is never mounted into the sandbox. Cost reports go to
`docs/cost_reports/<run_id>.json`. The response cache is `.cache/llm/` with a TTL. Chroma
runs embedded in `.chroma/` locally, or on the Chroma server (a `chroma-data` volume)
under Compose.

**Testing**: `unittest`, run under `coverage` (`scripts/test.sh`), with layers `tests/`
(unit), `tests/integration/` and `tests/e2e/`. Tests that need Docker or a live model
skip with a reason. LangSmith spans are captured with a recording client, never sent.

**Target Platform**: Linux host with Docker Engine 24+ and Compose v2 (verified on
Docker 26.1.3 / Compose 2.27.0, RHEL 8 with SQLite 3.26, so `pysqlite3-binary` stays).

**Project Type**: A one-shot CLI application: one container run per requirement or
resume. It opens no network interface.

**Performance Goals**: At least 40% lower average cost per benchmark item than the
pre-feature baseline (SC-205). Median item wall-clock time does not grow (SC-205a).
Retries, tracing and checkpoints add at most 5% when nothing fails (SC-210, SC-212). The
default offline suite runs in under 5 minutes (SC-209).

**Constraints**: The sandbox guarantees from 002 still hold: no network, no secrets,
non-root, limits, read-only tests, and no engine access for sandboxes. Secrets are
redacted everywhere (FR-219), including LangSmith inputs, outputs and metadata. Tracing
is off unless `LANGSMITH_TRACING=true` and a key is set, so default runs and tests make
no tracing network calls. Coder history stays append-only (preserved thinking on Haiku
5.5 and Sonnet 5.5). There is at most one model call of budget overshoot (SC-206).

**Scale/Scope**: Up to about 5 concurrent runs per host sharing one Chroma store
(SC-213). One run per process. About 13 new modules and edits to the 3 agents,
`orchestration/team.py`, `orchestration/state.py`, `tools/` and `benchmarks/`.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Rule | Status | How the design complies |
|------|--------|-------------------------|
| I. Names state intent; functions ≤ ~40 lines | PASS | New modules are small, single-purpose functions. `ModelCaller.call` is split into budget → span → guarded request → record steps. |
| I. Explicit error handling, no swallowed exceptions | PASS | Every dependency failure becomes a typed `DependencyFailure` with a classification and a cause. Event-write failures are counted (`events_lost`), never silently dropped. |
| I. Logic in 3+ places is extracted | PASS (improves) | The five copies of "create → refusal check → join text → `json.loads`" become `ModelCaller` + `parse_reply`. |
| II. No speculative features | PASS | Each new part maps to a directive or an FR. There is no read-through response cache (the cache is used only for degradation) and no pluggable tracer interface (LangSmith only). |
| II. Classes only for state or domain concepts | PASS | `ModelCaller`, `CircuitBreaker`, `ResponseCache`, `RunBudget`, `UsageLedger` and `EventLog` all hold state. Everything else is functions and `TypedDict`s. |
| II. No hidden global mutable state | PASS | Guards, budget, ledger, event log and tracer are built once per run in `run_team` (a `RunServices` bundle) and passed explicitly to the agents. There is no module-level breaker registry. |
| II. Abstractions need 2+ uses | PASS | `DependencyGuard` has four concrete dependencies: anthropic, mcp, sandbox, chroma. The validator has more than 10 reply types. |
| III. New third-party dependency justified | PASS (amended) | `langsmith` (runtime, already transitive) and `coverage` (dev-only) are justified in [Complexity Tracking](#complexity-tracking) and approved by constitution **1.3.0** (amended 2026-10-19), with `langsmith` limited to `trace`, `tracing_context`, `RunTree` and `Client`. |
| III. LangGraph core API only; no checkpointers | PASS | Checkpoint and resume are project code on the standard library (research R13). The graph gains a `resume` routing node and still uses only `StateGraph`/`START`/`END`. |
| III. No overlapping dependencies | PASS | No Pydantic models are added: validation stays on `TypedDict` (R10). LangSmith is the only tracer, with no OpenTelemetry exporter. |
| Python standards: 3.12, PEP 8/257, env-only secrets, `__main__` guards | PASS | Secrets reach the agent container only at run time (`env_file`/`-e`). The image and checkpoints hold none. New CLIs (`orchestration.health`) have `__main__` guards. |
| Tests on the standard library | PASS | Tests stay `unittest`. `coverage` only runs them. |
| Workflow: tests with every change; small commits | PASS | Every module comes with its tests (contract [test-suite.md](contracts/test-suite.md)). Phases are ordered so each one is a separable commit. |

**Gate result (pre-research)**: PASS. The Principle III amendment it depended on has
landed as constitution 1.3.0 (2026-10-19).

**Re-check after Phase 1 design**: PASS. The design artifacts add no dependency beyond
the two above. The Docker CLI binary and the Chroma server image are deployment
artifacts, not Python dependencies, and both are pinned (FR-227). The Chroma server is
the same project as the approved `chromadb` package.

## Project Structure

### Documentation (this feature)

```text
specs/003-production-hardening/
├── plan.md              # This file
├── research.md          # Phase 0: decisions R1–R20
├── data-model.md        # Phase 1: entities, TeamState additions, reply TypedDicts
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   ├── tracing.md                  # Span tree, names, metadata, propagation, redaction
│   ├── resilience.md               # Classification, retry, circuit breaker, degradation
│   ├── llm-output-validation.md    # Reply TypedDicts per call; failure mapping
│   ├── cost-report.schema.json     # docs/cost_reports/<run_id>.json
│   ├── run-event.schema.json       # One events.jsonl line
│   ├── cli.md                      # team/health CLIs, options, exit codes, resume rules
│   ├── deployment.md               # Dockerfile, docker-compose.yml, health checks
│   └── test-suite.md               # Layers, fakes, fixture, commands, coverage
├── checklists/
└── tasks.md             # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
agents/
├── llm.py               # + ModelCaller, MODEL_CAPABILITIES, cache_control placement, parse_reply
├── pm_agent.py          # uses ModelCaller; validates PMSpecReply / PMConsolidationReply
├── coder_agent.py       # uses ModelCaller; validates replies and tool inputs; Haiku default
├── qa_agent.py          # uses ModelCaller; validates every QA reply; Haiku default
└── memory.py            # + HttpClient mode (CHROMA_HOST); Chroma calls guarded
resilience/
├── __init__.py
├── classify.py          # failure → (dependency, temporary|permanent, cause)
├── retry.py             # RetryPolicy, backoff_delay (equal jitter, retry-after)
├── circuit.py           # CircuitBreaker (closed/open/half_open), DependencyGuard
└── response_cache.py    # ResponseCache (TTL, atomic files under .cache/llm/)
observability/
├── __init__.py
├── tracing.py           # LangSmith root/agent/llm/tool spans; header propagation
├── events.py            # EventLog (JSONL, severities, events_lost), run summary
└── redaction.py         # secret patterns → "[REDACTED]"
cost/
├── __init__.py
├── prices.json          # dated price table
├── pricing.py           # load table, cost_of(usage, model)
├── ledger.py            # UsageLedger (per agent), RunBudget
└── report.py            # write docs/cost_reports/<run_id>.json atomically
orchestration/
├── state.py             # + AgentUsage, DependencyFailure, ResumeAttempt, reply TypedDicts, new EndReasons
├── validation.py        # validate(value, TypedDict) -> list[str]
├── checkpoint.py        # save/load checkpoint, workspace snapshot/restore, run lock
├── health.py            # python -m orchestration.health
└── team.py              # RunServices, resume router, signals, exit codes, cost report
messaging/
└── endpoints.py         # trace headers in/out of A2A message metadata
tools/
├── python_exec.py       # run labels, start-failure classification, stale cleanup
├── test_runner.py       # QA_OUTPUT_ROOT (shared path under Compose)
└── mcp_client.py        # one restart on server death; guarded calls
benchmarks/run_benchmark.py   # cost per item and in total; baseline comparison
Dockerfile                    # application image
docker-compose.yml            # chroma + agent services
.dockerignore
requirements-dev.txt          # coverage
.coveragerc
scripts/test.sh               # one-command suite with coverage gate
docs/cost_reports/.gitkeep
tests/
├── (existing unit tests, made fully mocked)
├── test_validation.py, test_resilience.py, test_tracing.py, test_cost.py,
│   test_events.py, test_redaction.py, test_checkpoint.py, test_health.py,
│   test_lifecycle.py, test_model_caller.py
├── fakes/scripted_claude.py  # scripted model for integration and e2e
├── fixtures/e2e/             # supplied requirement + pytest fixture
├── integration/test_pm_coder.py, test_coder_qa.py, test_coder_sandbox.py,
│   test_compose_config.py, test_memory_concurrency.py
└── e2e/test_pipeline.py      # scripted (Docker) + opt-in live
```

**Structure Decision**: Keep the single-project layout. Add three small packages
(`resilience/`, `observability/`, `cost/`), each with one responsibility, rather than
growing `agents/llm.py` into a catch-all. Existing unit tests stay flat in `tests/`, so
`unittest discover` keeps working. New `tests/integration/` and `tests/e2e/` packages
make the layers explicit.

## Spec deltas (directives that change the spec)

These directives contradicted or extended `spec.md`. **All ten were applied to the spec on
2026-10-19** (spec.md, Clarifications "Session 2026-10-19 (planning directives)"). The
table records what changed and why:

| # | Spec today | Directive / plan | Spec change |
|---|-----------|------------------|-------------|
| D1 | Assumptions: "distributed tracing systems are out of scope" | LangSmith distributed tracing, one root per run, agent child spans with 4 metadata keys | Remove that exclusion. Add FR-214a (tracing) and SC for span completeness. |
| D2 | FR-204: default 4 retries | At most 3 retries, exponential backoff with jitter | FR-204 default → 3, and the Assumptions line likewise |
| D3 | FR-206 (MCP restart once) and FR-207 (sandbox retried once) | "All tool calls" get the same retry policy | FR-206/207: up to 3 retries. The MCP restart happens inside a retry attempt. Generated-code errors are still never retried. |
| D4 | No circuit breaker or degradation | Breaker: 3 consecutive failures → open, configurable cool-down → half-open. When open: cached result or structured error, never crash. | New FR-209a/b/c. Add a new entity, Circuit Breaker. |
| D5 | FR-225: context re-use, no TTL | Prompt caching with configurable TTL | FR-225: TTL setting `PROMPT_CACHE_TTL` ∈ {`5m`, `1h`} (the only values the API accepts) |
| D6 | FR-222: cost in run record and benchmark | JSON cost report per run in `docs/cost_reports/` | New FR-222a and its schema |
| D7 | FR-230a: concurrent memory (mechanism unstated); US4 has no Chroma service | Compose has a Chroma service with a health check, and the agent waits for it | FR-227/230a: Chroma runs as a separate service under Compose. Concurrency is guaranteed through the server. Embedded mode is documented as one run at a time (R16). |
| D8 | FR-219: redaction targets | Traces leave the host (LangSmith) | Add LangSmith inputs, outputs and metadata to FR-219's list |
| D9 | US5: unit tests only implied | Unit per agent (fully mocked), integration per pair, one e2e with a supplied fixture | New FR-235a/b/c |
| D10 | FR-216 lists events | Every LLM output is validated against `TypedDict` schemas before use | New FR-219a. A validation failure is an event with type `validation_failed`. |

## Implementation phases (order for `/speckit-tasks`)

Each phase is a separable commit and leaves the suite green.

0. **Governance** (done 2026-10-19): constitution amendment 1.3.0 (langsmith, coverage),
   spec deltas D1–D10 applied, baseline benchmark run *before* any model or caching
   change (SC-205), recorded under [Baseline](#baseline-benchmark-pre-feature).
1. **Foundations**: `orchestration/validation.py`, `observability/redaction.py`,
   `cost/pricing.py` + `prices.json`, new `TypedDict`s and end reasons in `state.py`.
2. **Resilience**: classifier, retry, circuit breaker, response cache, `DependencyGuard`.
3. **ModelCaller**: migrate the PM, then the Coder, then QA onto it. This brings reply
   validation, prompt caching, usage and cost per call, and `max_retries=0`.
4. **Tool guards**: MCP restart and retry, sandbox start retry and labels, pytest run
   retry, Chroma guard, tool-input validation.
5. **Accounting and reports**: `UsageLedger`, `AgentUsage` in `TeamState`, `RunBudget`,
   cost report on every exit path, benchmark cost columns.
6. **Observability**: LangSmith spans (root, agent, llm, tool), A2A header propagation,
   `EventLog` + run summary, progress lines derived from events.
7. **Models by role**: Coder and QA defaults to `claude-haiku-5-5`, the capability checks
   (FR-225b), and a benchmark run against the baseline (SC-205, with the quality fallback
   from the spec's Assumptions).
8. **Run lifecycle**: checkpoints, resume, signals, stale container cleanup, exit codes.
9. **Deployment**: `Dockerfile`, `docker-compose.yml`, `SemanticMemory` HttpClient mode,
   `orchestration.health`, clean-state Compose verification.
10. **Test suite**: integration and e2e layers, making the remaining agent unit tests fully
    mocked, coverage gate, `scripts/test.sh`.
11. **README** (directive 8) and the deployment guide section.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| `langsmith` declared as a runtime dependency (Principle III baseline amendment) | Directive 1 names LangSmith. It is the SDK for that service. MIT licensed and maintained by LangChain Inc. It is already installed transitively by the approved `langgraph` (via `langchain-core`), so declaring it adds no new package. Its use is limited to `trace`, `tracing_context`, `RunTree` header propagation and `Client(hide_inputs=, hide_outputs=, hide_metadata=)`. | Raw HTTP to the LangSmith REST API means re-implementing batching, run-tree ordering and header propagation that the SDK already ships. `langsmith.wrappers.wrap_anthropic` is rejected: it bypasses our retry, cache and redaction layer and can't attach the required agent metadata. OpenTelemetry adds several new packages. |
| `coverage` as a dev-only dependency | FR-237 requires statement coverage with an 85% gate. The standard library has no statement-coverage tool (`trace` reports line counts but has no branch-free statement report or threshold, and it is unmaintained for this use). Apache 2.0, actively maintained. It is never imported by project code or tests. | Writing a `sys.settrace` coverage tool would be fragile and slow. Dropping the gate would violate FR-237. |
| A `resume` routing node at the graph's START | FR-213a requires continuing from the stage after the checkpoint. | A LangGraph checkpointer would need a further amendment and stores LangGraph-internal state we can't redact or version. Re-running the whole graph and skipping stages inside every node spreads the resume logic across 8 nodes. |
| Host Docker socket mounted into the agent container (trust exception, FR-233) | The sandbox must be a sibling container started by the host engine. | Docker-in-Docker needs `--privileged`, which is strictly worse. Rootless remote engines are out of scope. Sandboxes still never get the socket. |

## Earlier baseline run (before the QA fix; superseded)

The first T003 run. It is kept as a record of the QA defect, and is **not** what SC-205 compares against: see [Baseline benchmark (pre-feature)](#baseline-benchmark-pre-feature) below.

| Setting | Value |
|---|---|
| Results file | `benchmarks/results/20261020T043138Z-before-qa-fix.json` |
| Run | 2026-10-20 03:55–04:31 UTC, at commit `b4ad600` |
| Benchmark | fingerprint `bfa9cb0c2125…` |
| Iteration limit | 5 |
| Models | all three agents on `claude-sonnet-5-5`, with refusal fallbacks on |
| Prompt caching | none |
| Effort | PM `high`, Coder `medium`, QA `high` |

**How cost was computed**: the pre-feature runner records only team-wide tokens. Cost =
`input_tokens × $2.00/MTok + output_tokens × $10.00/MTok` (Sonnet 5.5, price table
2026-10-06). With no prompt caching, every input token is billed at the base rate. A
call that a server-side refusal fallback served on another model can't be seen in these
totals, so this is an assumption.

| Item | Result | End reason | Input tokens | Output tokens | Cost (USD) | Duration (s) |
|---|---|---|---:|---:|---:|---:|
| temp-convert | error | qa_failed | 125,799 | 14,055 | 0.3921 | 172 |
| word-count | **passed** | complete | 202,451 | 30,161 | 0.7065 | 249 |
| roman-numerals | error | qa_failed | 87,672 | 10,655 | 0.2819 | 142 |
| stats | error | qa_failed | 124,254 | 16,971 | 0.4182 | 175 |
| bank-account | error | qa_failed | 70,059 | 9,639 | 0.2365 | 116 |
| todo-cli | error | qa_failed | 239,206 | 21,617 | 0.6946 | 281 |
| csv-summary | error | qa_failed | 108,696 | 14,377 | 0.3612 | 203 |
| password-strength | **passed** | complete | 432,655 | 38,471 | 1.2500 | 346 |
| matrix | **passed** | complete | 228,087 | 26,466 | 0.7208 | 230 |
| rate-limiter | **passed** | complete | 283,983 | 30,690 | 0.8749 | 264 |

| Measure | Value |
|---|---|
| Passed (SC-102) | **4/10 (not met)** |
| SC-103 | 3/3 = 1.00 (met) |
| Runs under 600 s | 10/10 |
| Total cost | **$5.94** |
| Mean cost per item (all items) | **$0.594** |
| Mean cost per passed item | **$0.888** |
| Median duration (all items) | **216 s** |
| Median duration (passed items) | 256 s |

**Why 6 items failed: a QA defect that predates this feature.** In all six, QA's first
`WRITE_TESTS_FORMAT` reply was rejected, so QA wrote no tests. The causes:

- **Criteria not copied word for word (4 items: temp-convert, roman-numerals, stats,
  csv-summary).** QA listed a criterion that wasn't copied word for word from the spec.
  `all_criteria` already includes the task-level criteria, so the check itself is
  correct. But one mismatched string fails the whole iteration with no second attempt
  (`_write_test_files` → `QAStepError`).
- **Path outside `tests/` (bank-account).** QA gave a test file path of `'x'`.
- **Cut-off JSON (todo-cli).** The JSON reply is cut off mid-content, which fits the
  16,000-token output cap, with thinking at `high` effort plus whole test files in one
  reply. QA doesn't check `stop_reason == "max_tokens"`.

The environment was ruled out: QA's MCP session and the sandboxed pytest both worked on a
copy of a failed workspace.

**What this means for SC-205 and the post-change benchmark (T073)**:

1. Failed items stop early and cost less ($0.24–$0.69, against $0.71–$1.25 for passed
   items). Comparing on all items would understate the baseline's real cost per
   completed program. T073 therefore reports the cost reduction on both bases:
   - **mean cost per item** (SC-205 as written);
   - **mean cost per passed item**.

   The spec's 40% target is judged on the second when the pass counts differ.
2. The pre-feature system doesn't meet SC-102 (8/10). Moving QA to Haiku can't be judged
   against an 8/10 bar the baseline itself misses. The quality fallback in research R8
   is judged against the baseline's pass rate (≥ 4/10) unless the QA defect is fixed
   first. A fix is proposed as a separate change: one corrective re-ask carrying the
   rejection message, and handling `max_tokens`. If made, the baseline should be re-run
   after it and before T067/T070, so that the comparison isolates this feature's effect.

**QA defect fix (2026-10-20, a separate change outside the 003 tasks)**:
`agents/qa_agent.py` now does the following:

- Every QA reply (initial tests, load repair, disputes, review including its regression
  tests) is checked in full before anything is written. This also fixes a half-written
  suite when a later file in a reply was bad.
- A reply that is rejected, isn't valid JSON, or stopped at `max_tokens` gets **one**
  corrected attempt (`REPLY_ATTEMPTS = 2`). The original prompt is sent again with the
  rejection reason appended.
- Refusals and failed model calls are still not retried.

The tests are in `tests/test_qa_agent.py`: 5 new cases and 3 updated. The full suite of
239 tests passes. The baseline was then re-run with the fix (below), so SC-205 measures only this feature's
effect.

## Baseline benchmark (pre-feature)

Task T003, re-run after the QA defect fix. **This is the measurement SC-205 and SC-205a
compare against.**

| Setting | Value |
|---|---|
| Results file | `benchmarks/results/20261020T053121Z-baseline.json` |
| Run | 2026-10-20 04:49–05:31 UTC |
| Code | commit `b4ad600` plus the uncommitted QA fix (`agents/qa_agent.py`) |
| Benchmark | fingerprint `bfa9cb0c2125…` |
| Iteration limit | 5 |
| Models | all three agents on `claude-sonnet-5-5`, refusal fallbacks on |
| Prompt caching | none |
| Effort | PM `high`, Coder `medium`, QA `high` |

Cost is computed the same way as in the earlier run: $2.00 per million input tokens and
$10.00 per million output tokens, with the same caveat about refusal fallbacks.

| Item | Result | End reason | QA reviews | Input tokens | Output tokens | Cost (USD) | Duration (s) |
|---|---|---|---:|---:|---:|---:|---:|
| temp-convert | **passed** | complete | 1 | 118,308 | 18,487 | 0.4215 | 161 |
| word-count | **passed** | complete | 3 | 357,416 | 41,820 | 1.1330 | 341 |
| roman-numerals | **passed** | complete | 1 | 96,089 | 16,231 | 0.3545 | 138 |
| stats | error | qa_failed | 0 | 59,773 | 8,196 | 0.2015 | 155 |
| bank-account | **passed** | complete | 2 | 175,169 | 23,816 | 0.5885 | 229 |
| todo-cli | **passed** | complete | 2 | 357,608 | 40,093 | 1.1161 | 338 |
| csv-summary | **passed** | complete | 2 | 315,121 | 40,362 | 1.0339 | 333 |
| password-strength | **passed** | complete | 2 | 164,129 | 25,053 | 0.5788 | 212 |
| matrix | **passed** | complete | 2 | 234,956 | 26,487 | 0.7348 | 218 |
| rate-limiter | **passed** | complete | 4 | 499,442 | 45,574 | 1.4546 | 398 |

| Measure | Value |
|---|---|
| Passed (SC-102) | **9/10 (met)** |
| SC-103 | 3/3 = 1.00 (met) |
| Runs under 600 s | 10/10 |
| Total cost | **$7.62** |
| Mean cost per item | **$0.762** (SC-205's baseline) |
| Mean cost per passed item | $0.824 |
| Median duration, all items | **223 s** (SC-205a's baseline) |
| Median duration, passed items | 229 s |

**The one failure (stats)**: QA's corrected reply listed a paraphrased criterion again
("median(numbers) takes a list of ints/floats and returns the middle value…"), so the
iteration ended after the second rejection. That is the fix behaving as designed: one
correction, then stop. The remaining weakness is the model rewording long criteria.

**Comparing with the earlier run**: 5 more items passed, and the mean cost rose
from $0.594 to $0.762, because finished runs go further. This is the reason the earlier
run couldn't serve as the baseline.

**For T073**:

- Both runs now finish nearly all items, so SC-205 is judged on **mean cost per item**
  against **$0.762**, with a 40% target of **≤ $0.457**. The per-passed-item figure
  ($0.824) is reported alongside.
- SC-205a: the median duration must not exceed **223 s**.
- The quality bar returns to 002's SC-102: **≥ 8/10**.
