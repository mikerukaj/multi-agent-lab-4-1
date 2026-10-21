# Research: Production Hardening (003)

Every "NEEDS CLARIFICATION" from the Technical Context is resolved here. Facts about
the Claude API come from the bundled Claude API reference (models and pricing cached
2026-10-06). Facts about LangSmith, LangGraph and Chroma come from the installed
packages (`langsmith 0.14.11`, `langchain-core 1.6.12`, `langgraph 1.2.12`,
`chromadb 1.5.9`) and from Docker Hub manifests checked on 2026-10-19.

---

## R1. Tracing: LangSmith through its SDK, explicit spans, one root per run

**Decision**: Use `langsmith` directly from a new `observability/tracing.py`:

- `run_team` opens the root span `team-run` (`run_type="chain"`) with
  `langsmith.trace(...)` and holds the `RunTree`.
- Each agent invocation opens a child span `agent:<name>` (`chain`) with the metadata
  `agent_name`, `token_count`, `tool_calls` and `duration_ms`. The last three are filled
  in when the span closes. The invocations are: PM `write_spec`/`consolidate`, Coder
  `run` (each build task), Coder `reflect`, Coder fix round, QA `run_iteration`, and QA
  `final_report`.
- Each model call is an `llm` span with `usage_metadata` (input, output and cache
  tokens) and `ls_model_name`, so LangSmith shows tokens and cost natively.
- Each tool call (MCP `read_file`/`write_file`, `exec_python`, a pytest run, a Chroma
  query or add) is a `tool` span.

Tracing is on only when `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` is set.
Otherwise every helper is a no-op that still measures `duration_ms`, because the event
record needs it. The project name comes from `LANGSMITH_PROJECT`, default
`multi-agent-lab`.

**Why explicit spans**: The directive's metadata (`token_count`, `tool_calls`) is known
only to our code, and the call has to pass through our retry, cache and redaction layer.
`wrap_anthropic` would trace below that layer and duplicate retried attempts as
separate calls.

**LangGraph's automatic tracing**: With `LANGSMITH_TRACING=true`, `langchain-core`
also traces each graph node, using the ambient LangSmith context. Opening our root
before `graph.stream` makes those node runs descendants of `team-run`, so the tree is
`team-run → <node> → agent:<name> → llm/tool`. That satisfies "child span of a single
root trace": every agent span has the root's `trace_id`, and the root is an ancestor.
The contract test checks ancestry and trace ID, not direct parenthood. We don't import
`langchain-core`.

**Alternatives**: OpenTelemetry with the OTLP exporter to LangSmith (several new
packages; rejected under Principle III). `@traceable` decorators on agent methods
(rejected: metadata known only at the end needs `run_tree.add_metadata`, which the
context-manager form gives us more plainly).

## R2. Trace propagation across the A2A broker thread

**Problem**: QA's `run_iteration`, QA's `final_report` and the Coder's fix round run in
the broker's thread pool on its background asyncio loop. LangSmith keeps the current
run in `contextvars`, which do not cross that boundary, so those spans would become
separate root traces.

**Decision**: This is real distributed tracing over the A2A binding. Before `send`, the
team node puts `run_tree.to_headers()` (`langsmith-trace`, `baggage`) into the A2A
message's `metadata["trace"]`. The agent endpoint opens
`tracing_context(parent=headers)` around the handler. The headers are plain strings, so
they survive the broker's JSON copy, and A2A 1.0 permits extension keys in `metadata`.
The QA module still imports nothing from the Coder (SC-110), and `messaging/` imports
only `observability.tracing`.

**Alternatives**: `contextvars.copy_context().run` in the broker (rejected: it couples
the broker to the caller's context and fails if an agent later moves to another
process, which the header approach already supports).

## R3. Retry policy

**Decision**: `RetryPolicy(max_retries=3, base_delay=1.0, max_delay=60.0)`. The defaults
come from directive 2 (overriding the spec's 4 retries, see delta D2). The settings are
`RETRY_MAX_RETRIES` (0–5), `RETRY_BASE_DELAY_SECONDS` and `RETRY_MAX_DELAY_SECONDS`.

- **Backoff with equal jitter**: `ceiling = min(max_delay, base * 2**attempt)` and
  `delay = ceiling/2 + uniform(0, ceiling/2)`. Equal jitter is chosen over full jitter so
  the waits really grow (spec: "growing, randomized waits") and are never close to zero.
  `random` comes from an injected `random.Random`, so tests are deterministic.
- **`retry-after`**: on a 429 or 529 with a `retry-after` header, wait
  `max(header, computed)` if that fits within `max_delay`. Otherwise stop retrying and
  fail with the cause recorded (spec edge case).
- **The SDK's retries are off**: `anthropic.Anthropic(max_retries=0)`. Today the clients
  use `max_retries=3`, which combined with our policy would give up to 16 attempts.
- **Sleeping goes through an injected `sleep`**, so tests never wait.
- **A billed retried call** counts its tokens once: only a response that arrives is
  counted, and failed attempts report no usage.

## R4. Failure classification

**Decision**: `resilience/classify.py` maps each exception or outcome to
`(dependency, kind, cause)`, where `kind` is `temporary` or `permanent`:

| Dependency | Temporary (retried) | Permanent (not retried) |
|---|---|---|
| `anthropic:<model>` | `RateLimitError` (429), `InternalServerError` (≥ 500, including 529 overloaded), `APIConnectionError`/`APITimeoutError`, status 408 and 409 | `BadRequestError` (400), `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (404), 413, 422; `stop_reason == "refusal"` (not an exception: handled by the existing refusal paths and never retried) |
| `mcp` | the server process exited or the stdio pipe broke mid-session (`McpUnavailableError` raised after start-up). The attempt restarts the server with the same workspace and protections, then repeats the call. | the server can't start at all because the workspace is missing or a protect path is outside it (exit 2); tool-level errors such as `File not found` are results, not failures |
| `sandbox` | `docker run` exit 125 with a daemon or engine message, "Cannot connect to the Docker daemon", container-create races | the image is missing after `ensure_sandbox`; the generated code's own non-zero exit or timeout is a result, never a failure (FR-207) |
| `chroma` | connection refused or reset, HTTP 5xx from the server, SQLite `database is locked` in embedded mode | an invalid collection or query argument |
| `a2a` | none: A2A requests are never retried (FR-208); a timeout is cancelled and ends `a2a_failed` | all |

An LLM reply that fails schema validation is not a dependency failure. It takes the
agent's existing malformed-reply path (R10).

## R5. Circuit breaker

**Decision**: one `CircuitBreaker` per dependency key per run, held in the `RunServices`
bundle and shared by every agent that uses that dependency (the Coder and QA share
`anthropic:claude-haiku-5-5`, `mcp` and `sandbox`). It is thread-safe with a
`threading.Lock`, because QA and the fix round run on broker threads.

- **What counts as a failure**: a logical call that ended in failure after its retries,
  or a permanent failure. Directive: "opens after 3 consecutive failures". Any success
  resets the count to 0.
- **Breaker outside retry**: each guarded call runs `breaker → retry(attempts)`.
  Counting individual attempts instead was rejected: with a threshold of 3 and up to 4
  attempts, the breaker would always trip before the 3rd retry, which would make the
  retry limit meaningless.
- **States**: `closed`, `open` and `half_open`. After `CIRCUIT_COOLDOWN_SECONDS`
  (default 30, range 1–3600), the next call is a single half-open trial. Concurrent
  callers keep failing fast until the trial ends. A successful trial goes back to
  `closed`. A failed trial goes back to `open` with a new cool-down. The clock is
  injected (`time.monotonic`) for tests.
- **Open circuit**: fail fast with `CircuitOpen`, which is never retried, then degrade
  (R6).
- **Scope**: per run. The benchmark builds fresh `RunServices` per item, so items don't
  affect each other and the 10 items stay comparable.
- **Recording**: every state change is an event (`circuit_opened`, `circuit_half_open`,
  `circuit_closed`) and a span event on the active span.

## R6. Graceful degradation and the response cache

**Decision**: when a model call can't be served, because the circuit is open or the
retries are exhausted, `ModelCaller` does the following:

1. Looks up the **response cache** under the request fingerprint (SHA-256 of the
   canonical JSON of `model`, `system`, `tools`, `messages` and `output_config`, with
   sorted keys and no `cache_control` markers). A fresh hit (younger than
   `LLM_RESPONSE_CACHE_TTL_SECONDS`, default 86 400) is rebuilt with
   `BetaMessage.model_validate`, validated like a live reply, and returned with
   `served_from="cache"`. Its tokens are recorded as `cached_replay` and cost $0.
2. Otherwise it returns a structured `DependencyFailure` (data-model) and never raises
   out of the node. Each agent maps it to its existing failure result. The team turns
   that into end reason `dependency_unavailable` (resumable, exit 3), with the
   dependency, the classification, the attempts and the circuit state.

**Writes**: every successful live response is written to the cache, atomically
(temp file then `os.replace`, mode 0600) under `.cache/llm/<model>/<fingerprint>.json`,
which is git-ignored. In-memory lookups come first.

**Where hits are useful**: the PM spec for a requirement on a resumed run, QA's
`final-report` (no model call), and benchmark re-runs. A Coder ReAct turn almost never
repeats exactly, so for the Coder degradation is in practice the structured error.

**Tool calls are never served from a cache.** A stale `read_file` or `exec_python`
result is wrong. When the `mcp` or `sandbox` circuit is open, the tool returns
`is_error: true` with a JSON body
`{"error": "dependency_unavailable", "dependency": "...", "circuit": "open", "retry_after_seconds": N}`
as the tool result, so the Coder's model sees why and the session goes on. QA turns it
into a `QAStepError` naming the dependency.

**Alternatives**: a read-through cache on every call (rejected: speculative, and it
changes model behaviour on re-runs). Caching tool results (rejected: correctness).

## R7. Prompt caching (Anthropic `cache_control`) with a configurable TTL

**Decision**: `PROMPT_CACHE_TTL` ∈ {`5m` (default), `1h`, `off`}. The API accepts only
those two TTLs. All markers in a request use the same TTL, which avoids the
"longer TTL first" ordering rule. The marker positions are fixed and explicit, with at
most 4 per request:

| Agent / call | Breakpoints | Why |
|---|---|---|
| PM `write_spec`, `consolidate` | the system block | The system prompt (about 700 tokens) is above Sonnet 5.5's 512-token minimum. `consolidate` re-uses it. |
| Coder `_think` / `_finalize` | the system block (covers `tools` + `system`), plus top-level automatic `cache_control` for the growing `messages` tail | Agent loop. The history is append-only, so every turn reads the previous prefix. |
| Coder `_critique` | system block + the sources block | Every reflection round re-sends the same sources. |
| QA `_ask` | system block + a stable user prefix, split into content blocks: spec → sources → tests (marker after the last stable one), then the per-call instruction | Test writing, load repair and review in one iteration share the spec and sources. |

- **Verification**: `usage.cache_read_input_tokens` > 0 on the second and later calls of
  a scripted run, asserted on recorded request bodies in `test_model_caller` and checked
  live in the quickstart.
- **Known risk**: QA calls use different `output_config.format` schemas. If the format
  turns out to be part of the cached prefix, QA's hit rate will be low. The quickstart
  measures it, and the fallback is one shared QA reply schema with a `kind` field.
- **Minimum lengths**: 512 tokens on Sonnet 5.5 and Haiku 5.5. A shorter prefix just
  doesn't cache, which costs nothing extra.
- **The 5-minute default is right**: within a run, calls sharing a prefix start well
  under 5 minutes apart. `1h` only helps across runs (the benchmark), at twice the write
  price.

## R8. Models by role and capability checks

**Decision**:

| Agent | Default model | Effort | Refusal fallbacks |
|---|---|---|---|
| PM | `claude-sonnet-5-5` (unchanged, directive 3) | `high` | on (`server-side-fallback-2026-07-01`, `"default"`) |
| Coder | `claude-haiku-5-5` | `medium` | **off**: Haiku 5.5 has no server-side refusal fallback |
| QA | `claude-haiku-5-5` | `high` | **off** |

Facts about Haiku 5.5 relevant to the existing code:

- Adaptive thinking is supported and on by default, so the Coder's
  `thinking={"type": "adaptive", "display": "summarized"}` still works.
- Effort `low`–`max` is supported, with default `medium`, so it is set explicitly.
- Structured outputs are supported.
- Mid-conversation `system` messages are supported. The Coder injects memories this way.
- Prefill and non-default sampling return 400. Neither is used.
- Preserved thinking is enforced, and the Coder is already append-only.

**FR-225b**: `agents/llm.py` gets a `MODEL_CAPABILITIES` table (fallbacks, effort levels,
mid-conversation system). `*Config.__post_init__` refuses a combination the model
doesn't support, with a message naming the agent, the setting and the model. For
example, `CODER_USE_FALLBACKS=true` with a Haiku model fails with exit 2. The
`*_USE_FALLBACKS` default becomes "on if the model supports it". A model missing from
the table is allowed with a warning event, and no capability is assumed.

**Quality guard**: if the post-change benchmark falls below 8/10 (SC-102), apply the
spec's Assumptions step (QA back to Sonnet first, since review quality drives the loop),
record the result in this plan, and measure SC-205 on that configuration.

## R9. Token, usage and cost accounting

**Decision**: extend `TokenUsage` with `cache_creation_input_tokens` and
`cache_read_input_tokens`. The existing keys keep their meaning: `input_tokens` is the
uncached input. Per call, `prompt_tokens = input + cache_creation + cache_read` and
`completion_tokens = output`. Cost comes from `cost/prices.json`:

| Model | Input | Output | Cache read | Cache write 5m | Cache write 1h | Long-context rule |
|---|---|---|---|---|---|---|
| `claude-sonnet-5-5` | $2.00 | $10.00 | $0.20 | $2.50 | $4.00 | none |
| `claude-haiku-5-5` | $0.10 | $0.50 | $0.01 | $0.125 | $0.20 | prompt > 100K tokens: $0.50 / $2.50 / $0.05 / $0.625 / $1.00 |

All prices are per million tokens. The table carries `"version": "2026-10-06"` and a
source note, and is checked against the live pricing page at implementation. The
5m/1h split comes from `usage.cache_creation.ephemeral_5m_input_tokens` and
`ephemeral_1h_input_tokens` when present. Otherwise the configured TTL is used.

- Cost is priced by **`response.model`**, so a Sonnet refusal fallback that ran on
  another model is billed at that model's rates.
- A model missing from the table gives `cost_usd: null` for that agent with a warning,
  and a run with a budget refuses to start (FR-224).
- Each agent's `ModelCaller` records into the run's thread-safe `UsageLedger`, keyed by
  agent name. QA and the fix round run on broker threads, so the ledger, not the A2A
  artifacts, is the source of truth for per-agent totals. `TeamState["usage"]` stays the
  team total for compatibility.

## R10. Validating LLM output against `TypedDict` schemas

**Decision**: the project uses `TypedDict` everywhere and has no Pydantic models, so
validation stays on `TypedDict` (directive 4: "whatever is currently being used").
`orchestration/validation.py` provides `validate(value, tp) -> list[str]` (empty means
valid). It walks `typing.get_type_hints(tp, include_extras=True)` and supports
`TypedDict` (with `Required`/`NotRequired` and `total`), `Literal`, `list[T]`,
`dict[str, T]`, `X | None`/`Union`, `str`, `int` (rejecting `bool`), `float`, `bool`
and `None`. Unknown keys are rejected, matching `additionalProperties: false`. The
validator is about 80 lines and has no dependency.

- **Reply types**: each model-facing JSON schema gets a reply `TypedDict` in
  `orchestration/state.py`: `PMSpecReply`, `PMConsolidationReply`, `CoderSummaryReply`,
  `CritiqueReply`, `QATestFilesReply` (initial tests and load repair), `QADisputeReply`,
  `QAReviewReply`
  (see [contracts/llm-output-validation.md](contracts/llm-output-validation.md)). A unit
  test checks that every JSON schema and its `TypedDict` agree on property names,
  required keys and enums, so the two can't drift.
- **Order of checks**: `json.loads` → `validate(reply, ReplyType)` → the existing
  semantic checks (criteria preserved, `same_as` known, paths under `tests/`, ≤ 8 tasks)
  → use. Structured outputs already constrain the shape, so this is defense in depth.
  It also covers cached replays (R6), refusal-fallback models, and a future change of
  `output_config`.
- **Tool inputs**: `ReadFileInput`, `WriteFileInput` and `ExecPythonInput` are checked
  before `dispatch_tool`. A failure returns an `is_error` tool result naming the field,
  and no tool runs.
- **On failure**: PM → `PMResult.error = "Model output failed validation: <first 3 problems>"`
  (`pm_error`). Coder summary → the existing tolerant fallback, plus a
  `validation_failed` event. Critique → `"error"` status (`reflection_failed`). QA →
  `QAStepError("Malformed QA reply: ...")` (`qa_failed`). Nothing raises out of a node.

**Alternatives**: Pydantic `TypeAdapter` (Pydantic is transitive through `langgraph`,
but adopting it adds a second schema system next to `TypedDict`; rejected under
"overlapping dependencies"). Generating JSON schemas from the `TypedDict`s (rejected for
now: it rewrites five hand-checked schemas for no behavioural gain. The agreement test
gives the same safety).

## R11. The event record and run summary (spec US2)

**Decision**: `observability/events.py` `EventLog` appends one JSON object per line to
`workspace/.runs/<run_id>/events.jsonl`, flushing after every line. Fields follow
[contracts/run-event.schema.json](contracts/run-event.schema.json). The severity filter
is `LOG_LEVEL` (`debug|info|warning|error`, default `info`). Errors, `run_end` and the
summary are always written.

- **One instrumentation point**: the same helpers that open LangSmith spans also emit
  events, so traces and events always agree.
- **Progress lines** on stderr are rendered from events by a formatter that replaces
  `format_update`'s inputs (FR-220).
- **A write failure** (`OSError`) sets `events_lost=true` in the run record and the cost
  report. The run continues (FR-221).
- **Relation to tracing**: events are the always-on local record. LangSmith is the
  optional remote view. Neither depends on the other.

## R12. Secret redaction

**Decision**: `observability/redaction.py` `Redactor.from_env(os.environ)` collects
values of variables whose names contain `KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL`
(case-insensitive, at least 8 characters), plus the regex `sk-ant-[A-Za-z0-9_-]{8,}`.
`redact(obj)` walks strings in nested JSON and replaces them with `[REDACTED]`.

It is applied to:

- events and the run record;
- checkpoints and the cost report;
- QA's report text;
- LangSmith, via `Client(hide_inputs=redact, hide_outputs=redact, hide_metadata=redact)`.

Generated program output inside the workspace is not changed (FR-219).

## R13. Checkpoint and resume without a LangGraph checkpointer

**Decision**: `orchestration/checkpoint.py`:

- **Location**: `workspace/.runs/<run_id>/`, outside the run's workspace folder, so the
  sandbox (which mounts only `workspace/<run_id>`) can't read or change it.
- **Saving**: after each stage node (pm/consolidate, each coder task, reflect, qa, fix),
  the team writes `checkpoint.json`. It holds `format_version`, `app_version`, `stage`,
  `next_node`, the redacted `TeamState`, the settings, the ledger, the resume history, a
  workspace snapshot (path → base64 content, including `tests/` and `.qa/`) and
  `complete: true`. The write goes to a temp file, then `fsync`, then `os.replace`, so a
  half-written file is never read.
- **Run lock**: `fcntl.flock` on `lock`, held for the life of the process. It detects a
  run that is still going, from another process or a container on the same host,
  because the lock is on the shared bind mount and the same kernel.
- **Resume**: `python -m orchestration.team --resume <run_id> [--budget USD]`.
  1. Refuse a run that is locked, has a final record with a non-resumable end reason, or
     has a missing, corrupt or incompatible checkpoint.
  2. Restore the workspace, recording overwritten and removed files.
  3. Append a `ResumeAttempt`.
  4. Start the graph with `state["resume_from"] = next_node`. A `resume` node at START
     routes there.

  A2A requests pending at the interruption are not replayed: the stage restarts from its
  beginning (spec edge case).
- **Budget on resume**: a `budget_exceeded` run may resume only with a higher
  `--budget`. Any other setting change is refused (FR-213c).

## R14. Signals and container hygiene

**Decision**:

- **Signal handling**: `main` installs SIGINT and SIGTERM handlers that set a
  `threading.Event` in `RunServices`. Each model and tool call checks it, so the run
  stops at the next call boundary. The handlers also stop this run's labelled
  containers (`docker kill --signal KILL` on `label=multi-agent-lab.run_id=<id>`) and
  cancel pending broker requests. The graph then routes to `finish`, which ends the run
  `interrupted`, writes the final record, checkpoint and cost report, and exits 130 or
  143. A second signal calls `os._exit` after a best-effort container kill.
- **Labels**: every sandbox container gets the labels `multi-agent-lab.run_id` and
  `multi-agent-lab.owner=<hostname>:<pid>`.
- **Start-up cleanup**: list labelled containers, and remove those whose run lock is not
  held (FR-211, FR-230b).

## R15. Per-run budget

**Decision**: `RUN_BUDGET_USD` (or `--budget`), with no cap by default.

- **Shared object**: `RunBudget` lives in `RunServices` and is shared by all callers.
- **Check before every model call**: if spend ≥ cap, raise `BudgetExceeded`. The agent
  turns it into its failure result, and the team ends the run `budget_exceeded`
  (resumable, exit 3). The check happens before the call, so a run can overshoot by at
  most the one call that was already in flight (SC-206).
- **The final report** makes no model call (002), so it always runs.
- **A model missing from the price table** with a budget set means the run refuses to
  start (FR-224).

## R16. Deployment: application image, Compose, Chroma service

**Decision**: the application image (`Dockerfile`):

- **Base**: `python:3.12-slim@sha256:a6e34c…` (the same digest the sandbox uses).
- **Docker CLI**: `COPY --from=docker:26.1.3-cli@sha256:<digest> /usr/local/bin/docker /usr/local/bin/docker`.
  This gives a pinned static CLI and no apt repository.
- **Python packages**: `pip install --no-cache-dir -r requirements.txt`, every version
  pinned.
- **Embedding model at build time**: a build step runs Chroma's
  `DefaultEmbeddingFunction` once, so the ONNX model (about 80 MB) is baked into the
  image. No download is needed at run time.
- **User**: a non-root `app` user, with `HOME=/home/app`.
- **Entrypoint**: `ENTRYPOINT ["python", "-m", "orchestration.team"]`.
- **No secrets**: `.dockerignore` excludes `.env*`, `workspace/`, `.chroma/`, `.cache/`,
  `docs/cost_reports/*.json` and `.git`.

`docker-compose.yml`:

- **`chroma`**: `chromadb/chroma:1.5.9@sha256:<index digest>`, the same version as the
  client package. The tag was verified on Docker Hub on 2026-10-19. It has a
  `chroma-data` volume and no host port publishing.
  - Health check: `GET /api/v2/heartbeat` via
    `bash -c 'exec 3<>/dev/tcp/localhost/8000 && printf "GET /api/v2/heartbeat HTTP/1.0\r\n\r\n" >&3 && grep -q nanosecond <&3'`,
    because the image may not include curl. The exact command is checked against the
    pinned image at implementation.
  - Timing: `interval: 5s`, `retries: 20`, `start_period: 10s`.
- **`agent`**: `build: .`.
  - Startup: `depends_on: {chroma: {condition: service_healthy}}`, plus a client-side
    guarded connect, the `chroma` dependency with retry, before the first memory call.
  - Environment: `env_file: .env` for run-time secrets, never baked in; `CHROMA_HOST=chroma`
    and `CHROMA_PORT=8000`.
  - Volumes:
    - `/var/run/docker.sock:/var/run/docker.sock` (the trust exception);
    - `${PWD}/workspace:${PWD}/workspace`, with `CODER_WORKSPACE=${PWD}/workspace`;
    - `${PWD}/docs/cost_reports:/app/docs/cost_reports`;
    - `${PWD}/benchmarks/results:/app/benchmarks/results`;
    - `${PWD}/.cache:/app/.cache`.
  - User: `user: "${HOST_UID}:${HOST_GID}"` and `group_add: ["${DOCKER_GID}"]`, so the
    sandbox's `--user $(id -u)` and the files written match the host user.
  - QA output: `QA_OUTPUT_ROOT=${PWD}/workspace/.qa-out`, so QA's JUnit mount is a host
    path.
  - Command: `command: ["${TEAM_REQUIREMENT:-<default realistic requirement>}"]`, so
    `docker compose up` runs the pipeline once and exits.

**Why the same-path workspace mount**: the agent container asks the host engine to bind
mount the workspace into each sandbox. The engine resolves that path on the host, so
the path inside the agent container must be the host path. The alternative, translating
paths with a `HOST_WORKSPACE` variable, adds a mapping that every mount must apply
correctly. The same-path mount needs none.

**Memory in server mode**: `SemanticMemory` uses `chromadb.HttpClient(host, port)` when
`CHROMA_HOST` is set, and the existing embedded `PersistentClient` otherwise. The
embedding is still computed client-side with the same default function, so stored
vectors are compatible across modes. Concurrent runs (FR-230a, SC-213) are safe through
the server, which serialises writes, and record IDs are unique per run. **Embedded mode
is documented as one run at a time** (spec delta D7). The README tells operators to use
the Compose Chroma service, or `chroma run`, for concurrent runs.

**The `.env` file and Compose**: `.env` is also read by Compose for variable
substitution. `HOST_UID`, `HOST_GID` and `DOCKER_GID` go there, with a `make`-free
one-liner in the quickstart. Secrets in `.env` reach only the `agent` service, never
`chroma`.

## R17. Health check

**Decision**: `python -m orchestration.health [--json]` reports each item as
`ok`, `missing` or `error`, with a message. It never calls a model.

The items checked:

- credentials present (any of `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`);
- Docker engine reachable (`docker version`);
- sandbox image present or buildable;
- workspace writable;
- cost-report directory writable;
- Chroma reachable (heartbeat, server mode) or the path writable (embedded mode);
- price-table entries for each configured model;
- LangSmith configured, informational only.

The exit code is 0 when all items pass and 2 otherwise. `run_team` runs the same checks
before the first model call (FR-231, US4 scenario 4). The Compose health check for the
agent itself is not needed, because it is a one-shot job.

## R18. Test strategy

**Decision**:

- **Unit** (`tests/test_*.py`): each agent with fully mocked dependencies. Fake
  Anthropic clients (existing `FakePMClient` and Coder `FakeClient`, plus a new
  `ScriptedClaude`), `FakeFileTools` (no MCP subprocess), a fake `exec_python` and a fake
  `run_tests` (no Docker), and `FakeMemory`. The 5 `requires_docker` cases in
  `test_coder_agent_loop.py` move to `tests/integration/test_coder_sandbox.py`. New unit
  modules cover validation, resilience, model caller, tracing, cost, events, redaction,
  checkpoint, health and lifecycle.
- **Integration** (`tests/integration/`):
  - `test_pm_coder.py`: real `PMAgent` and `CoderAgent`, each on `ScriptedClaude`, the
    real team graph for the PM → dispatch → coder stages, the real MCP server over stdio,
    and a fake sandbox.
  - `test_coder_qa.py`: real `CoderAgent` and `QAAgent` over the real A2A broker and real
    MCP, with `ScriptedClaude` and the real Docker sandbox and pytest; skipped without
    Docker. It covers one fix round to `complete`.
  - `test_compose_config.py`: parses `docker-compose.yml` with `docker compose config`
    (skipped without Docker) and checks the health check, `depends_on`, the absence of a
    socket mount on `chroma`, and no secrets in `environment`.
- **End to end** (`tests/e2e/test_pipeline.py`): `run_team` with all three real agents.
  `ScriptedClaude` plays a realistic task: `tests/fixtures/e2e/requirement.txt`, an
  inventory CLI and library with add, remove and report, and validation errors. The real
  sandbox and MCP are used. After the run it:
  1. compiles every `.py` in the workspace with `python -m py_compile` in the sandbox;
  2. runs the main file in the sandbox (exit 0);
  3. copies the supplied fixture `tests/fixtures/e2e/test_inventory_fixture.py` into a
     scratch copy of the workspace and runs it with `tools.test_runner.run_pytest`;
  4. asserts the fixture passes, `status == "completed"`, a cost report exists, and one
     root span is captured.

  The live variant `RUN_LIVE_E2E=1` (plus a key and Docker) runs the same requirement
  and fixture on the real models and prints the cost. The requirement text fixes the
  public interface (module name, function signatures, CLI usage), so a real model can
  meet the fixture.
- **Tracing assertions** use a recording `langsmith.Client` stand-in (`create_run` and
  `update_run` captured, `auto_batch_tracing=False`) passed through
  `tracing_context(enabled=True, client=...)`. No network is used. The exact client hook
  is confirmed against `langsmith 0.14.11` in the first tracing task.
- **Coverage**: `scripts/test.sh` runs `coverage run -m unittest discover -s tests -t .`
  and then `coverage report --fail-under=85`. `.coveragerc` sets the source to
  `agents, orchestration, tools, messaging, observability, resilience, cost, benchmarks`.

## R19. Benchmark and baseline

**Decision**: before any model or caching change, run the full benchmark on the current
configuration and commit it as `benchmarks/results/<ts>-baseline.json`. The runner gains
per-item `cost_usd`, `cost_by_agent`, total cost, median duration, and an optional
`--baseline <file>` comparison that prints the cost reduction and the duration change
(SC-205/205a). This is a live run, about 10 team runs with real spend, so the operator
runs it. It is not part of the suite.

## R20. Cost report file

**Decision**: written by `cost/report.py` from `finish` and from every early-exit path
(interrupt, budget, dependency failure, error after the run started). The flow is:
render the ledger → redact → write `docs/cost_reports/.<run_id>.json.tmp` → `os.replace`.
The directory is configurable with `COST_REPORT_DIR`. A resumed run rewrites the same
file with cumulative totals and an `attempts` list. A failure to write is reported on
stderr and in the event log, and never changes the run's exit code. The schema is
[contracts/cost-report.schema.json](contracts/cost-report.schema.json).
`docs/cost_reports/.gitkeep` is committed. Whether to commit reports is the operator's
choice, like `benchmarks/results/`.
