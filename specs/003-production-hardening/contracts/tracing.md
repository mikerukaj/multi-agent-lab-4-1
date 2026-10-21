# Contract: LangSmith Tracing

Module: `observability/tracing.py`. Research: R1, R2, R12.

## Enablement

| Variable | Default | Effect |
|---|---|---|
| `LANGSMITH_TRACING` | unset | `true` together with a non-empty `LANGSMITH_API_KEY` → spans are sent. Anything else → no-op tracer (durations are still measured). |
| `LANGSMITH_API_KEY` | unset | secret; never logged; redacted like every `*KEY*` |
| `LANGSMITH_PROJECT` | `multi-agent-lab` | project for every run |
| `LANGSMITH_ENDPOINT` | SDK default | optional self-hosted endpoint |

With tracing on but the LangSmith service unreachable, the run is unaffected. The SDK
sends in the background, and send errors become one `warning` event per run, through
`tracing_error_callback`.

## Span tree (one trace per run)

```text
team-run                                   run_type=chain   (root; one per run attempt)
├── <langgraph node runs, automatic>       pm / consolidate / dispatch / coder / reflect / qa / fix / finish
│   └── agent:pm | agent:coder | agent:qa  run_type=chain   (one per agent invocation; see below)
│       ├── llm:<model>                    run_type=llm     (one per model call, incl. degraded/cached)
│       └── tool:<name>                    run_type=tool    (read_file, write_file, exec_python, pytest, memory.query, memory.add)
```

**Invariant (tested)**: every `agent:*` span in a run has the root's `trace_id`, and the
`team-run` span is its ancestor. There is exactly one span with no parent per run
attempt. Root metadata is `run_id`, `attempt` (1 for the first run, +1 per resume),
`requirement_chars`, `max_iterations` and `resumed_from_stage` (`None` on attempt 1). A
resumed run opens a new `team-run` root with the same `run_id`, the next `attempt` and
`resumed_from_stage` set. That is one root per attempt, linked by `run_id`.

### Agent invocations that open an `agent:*` span

| Agent | Invocation | Span name | Thread |
|---|---|---|---|
| PM | `write_spec`, `consolidate` | `agent:pm` | main |
| Coder | `run` (each build task) | `agent:coder` | main |
| Coder | `reflect` (each round group) | `agent:coder` | main |
| Coder | fix round (`fix-program` handler) | `agent:coder` | broker worker: parent from A2A headers |
| QA | `run_iteration` (`review-program` handler) | `agent:qa` | broker worker: parent from A2A headers |
| QA | `final_report` (`final-report` handler) | `agent:qa` | broker worker: parent from A2A headers |

### Required metadata on every `agent:*` span (directive 1)

| Key | Type | Value |
|---|---|---|
| `agent_name` | str | `pm`, `coder` or `qa` |
| `token_count` | int | Σ over this invocation's model calls of `input + cache_creation + cache_read + output` tokens (cached replays count 0) |
| `tool_calls` | int | number of `tool:*` calls made in this invocation (PM is always 0) |
| `duration_ms` | int | wall-clock milliseconds of the invocation |

Also present: `run_id`, `stage`, `model`, `iteration` (QA/fix), `task_id` (Coder build),
`invocation` (`write_spec`, `consolidate`, `run`, `reflect`, `fix`, `review`,
`final_report`), `cost_usd`, and `outcome` (`completed` or the failure status).

### `llm:<model>` span

- **inputs**: `{"model", "system_chars", "message_count", "tools": [names], "output_format": name|null, "cache_ttl"}`.
  Full prompts are not attached by default. With `LANGSMITH_LOG_PROMPTS=true` the
  redacted messages are attached.
- **outputs**: `{"stop_reason", "served_from": "live"|"cache", "text_chars", "tool_uses": [names]}`.
- **metadata**: `ls_provider="anthropic"`, `ls_model_name=<response.model>`, `attempts`,
  `retries`, `circuit`, `cost_usd`.
- **usage_metadata**: `{"input_tokens": prompt_tokens, "output_tokens", "total_tokens", "input_token_details": {"cache_read", "cache_creation"}}`.
- **error**: set when the call ended in `DependencyFailure`. The message is redacted.

### `tool:<name>` span

- **inputs**: `{"path"}` for file tools and `exec_python`. Content is never included.
- **outputs**: `{"is_error", "exit_code", "timed_out", "duration_ms"}` as relevant.
- **metadata**: `dependency`, `attempts`, `circuit`, `served_from` (always `live`).

## Propagation across A2A (distributed tracing)

1. Before `A2AClient.send`, the team node sets
   `message["metadata"]["trace"] = tracer.headers()`, a dict of strings with the keys
   `langsmith-trace` and optional `baggage`, or `{}` when tracing is off.
2. `messaging/endpoints.py` handlers wrap the agent call in
   `tracer.continue_from(message["metadata"].get("trace"))`. That is
   `tracing_context(parent=headers)` when tracing is on, and a no-op otherwise.
3. The metadata survives the broker's JSON copy unchanged (all strings). Missing or
   malformed headers start the agent span under no parent and emit a `warning` event.
   The run is never failed for a tracing reason.

## Redaction

`langsmith.Client(hide_inputs=redact, hide_outputs=redact, hide_metadata=redact)`, where
`redact` is `observability.redaction.Redactor.redact`. A planted key in a prompt, an
error or tool output never reaches LangSmith (SC-208 test with a recording client).

## Test hooks

`Tracer(client=<recording client>, enabled=True)` captures runs in memory. The tests
check the following:

- the root count;
- the agent span ancestry and trace IDs;
- the four metadata keys and their types;
- the `token_count` sum against the scripted usage;
- `tool_calls` against the scripted tool calls;
- propagation through a real `A2ABroker`;
- redaction;
- no network when disabled.
