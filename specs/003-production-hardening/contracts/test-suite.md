# Contract: Test Suite

Research: R18. Requirements: FR-235–FR-239, directive 5.

## Command (one, non-interactive; FR-238)

```bash
scripts/test.sh            # = coverage run -m unittest discover -s tests -t . -v
                           #   && coverage report --fail-under=85
```

- The exit status alone signals the result, including a coverage shortfall. It runs the
  same way on the host and inside the application image:
  `docker compose run --rm --entrypoint scripts/test.sh agent`.
- `requirements-dev.txt`: `coverage==7.16.2`.
- `.coveragerc`: `source = agents, orchestration, tools, messaging, observability, resilience, cost, benchmarks`.
  `omit = tests/*`.
- No paid model call and no LangSmith network call by default (FR-236). Tests that need
  Docker or a live model are skipped with a stated reason.

## Layers

### Unit: `tests/test_*.py` (fully mocked)

"Fully mocked" means: no Anthropic network (a fake client replays scripted
`BetaMessage`-shaped responses or raises SDK exceptions), no Docker (fake `exec_python`
and fake `run_tests`), no MCP subprocess (`FakeFileTools`), no Chroma (`FakeMemory`) and
no LangSmith network (recording client or disabled tracer).

| Agent | Module(s) | Must cover (new or changed) |
|---|---|---|
| PM | `test_pm_agent.py` | spec/consolidate happy paths; refusal; invalid JSON; **reply fails `PMSpecReply` validation** (missing key, bad `priority`, extra key); `DependencyFailure` → `dependency_unavailable`; cached replay on open circuit; prompt-cache marker on the system block; usage and cost per call; `agent:pm` span metadata |
| Coder | `test_coder_agent_loop.py`, `test_coder_reflection.py` | the existing loop cases, with the 5 `requires_docker` cases replaced by fake-sandbox versions (the Docker originals move to `tests/integration/test_coder_sandbox.py`); **invalid tool input not dispatched**; **invalid summary/critique reply**; MCP restart then success; sandbox open circuit → structured tool error; Haiku defaults and FR-225b refusal; cache markers (system + automatic); `tool_calls` count on the span |
| QA | `test_qa_agent.py` | the existing cases; **each of `WRITE_TESTS_FORMAT`, `DISPUTES_FORMAT`, `REVIEW_FORMAT` replies failing validation** → `qa_failed`; model `DependencyFailure` → iteration `failure`; pytest-run retry; split cache-marked user content; `agent:qa` span under an A2A-propagated parent |

Shared new unit modules: `test_validation.py` (validator, plus schema ⇄ TypedDict
agreement), `test_resilience.py` (classification table; backoff bounds and growth;
`retry-after`; the breaker state machine with an injected clock; half-open single
trial; thread safety), `test_model_caller.py`, `test_tracing.py`, `test_cost.py`
(pricing incl. the long-context tier, fallback-model pricing, ledger, budget, report
schema), `test_events.py`, `test_redaction.py`, `test_checkpoint.py`, `test_health.py`,
`test_lifecycle.py` (signals, exit codes, stale container cleanup) and
`test_never_crashes.py` (each node × each dependency open → a final state and a cost
report).

### Integration: `tests/integration/`

| Module | Real components | Fakes | Skips |
|---|---|---|---|
| `test_pm_coder.py` | `PMAgent`, `CoderAgent`, team graph `pm → consolidate? → dispatch → coder`, MCP server over stdio, validation, ledger, tracer (recording) | `ScriptedClaude` for both agents, fake sandbox | none |
| `test_coder_qa.py` | `CoderAgent`, `QAAgent`, `A2ABroker` + endpoints, MCP server, sandbox + pytest, trace propagation | `ScriptedClaude` | without Docker |
| `test_coder_sandbox.py` | the 5 cases moved from `test_coder_agent_loop.py` | `ScriptedClaude` | without Docker |
| `test_compose_config.py` | `docker compose config` output | none | without Docker Compose |
| `test_memory_concurrency.py` | 5 processes sharing one Chroma server (a throwaway `chromadb/chroma` container), via `SemanticMemory` HttpClient mode | none | without Docker |

Pair assertions:

- **PM + Coder**: the tasks reach the Coder in order with the PM's acceptance criteria
  in the brief; files land in one workspace; `agent_usage.pm` and `agent_usage.coder`
  are both filled; one root, with `agent:pm` and `agent:coder` spans as descendants.
- **Coder + QA**: a scripted QA finds one failing test; the Coder fixes it over A2A; QA
  says `complete`; the pytest run is real; `agent:qa` spans are descendants of the same
  root despite running on the broker thread.

### End to end: `tests/e2e/test_pipeline.py`

- **Supplied inputs**: `tests/fixtures/e2e/requirement.txt` is a realistic requirement
  (an inventory library + CLI with explicit function signatures and error cases).
  `tests/fixtures/e2e/test_inventory_fixture.py` is the supplied pytest fixture, never
  shown to the agents.
- **Scripted run** (default when Docker is available): `run_team` with real PM, Coder
  and QA on `ScriptedClaude` (tests/fakes/scripted_claude.py). The script produces the
  program through real `write_file`/`exec_python` tool calls, then QA tests, one fix
  round, and `complete`.
- **Live run** (`RUN_LIVE_E2E=1`, plus a key and Docker): the same requirement and
  fixture on the configured real models. It prints the run's cost report. It is not part
  of the default suite and makes paid calls.

Assertions for both variants:

1. `status == "completed"` and `end_reason == "complete"`.
2. Every `.py` file in the workspace compiles (`python -m py_compile` in the sandbox).
3. The main file runs in the sandbox with exit 0.
4. The supplied fixture, copied into a scratch copy of the workspace and run with
   `tools.test_runner.run_pytest`, has `loaded` true, `failed == 0` and
   `errors == 0`.
5. `docs/cost_reports/<run_id>.json` exists (in a temp `COST_REPORT_DIR`) and validates
   against `contracts/cost-report.schema.json` (with the stdlib checker
   `tests/schema_check.py`, extended with `const` and `exclusiveMinimum` if missing).
6. The recording tracer saw exactly one root and an `agent:*` span for each of PM,
   Coder and QA (scripted variant).

## Fakes

- `ScriptedClaude`: a `beta.messages.create` stand-in that returns real
  `anthropic.types.beta.BetaMessage` objects (via `model_validate`), built from a script
  of `(match, reply)` steps. It records every request body, so tests can assert the
  cache markers, models, effort and fallbacks. It can inject SDK exceptions
  (`RateLimitError` with `retry-after`, `InternalServerError` 529, `APIConnectionError`,
  `BadRequestError`).
- `FakeFileTools`, `FakeMemory`, `FakeQA` and `FakeCoder` are the existing fakes. They
  gain support for a failure-injection script, used **only in tests** (FR-239: no
  production switch).
