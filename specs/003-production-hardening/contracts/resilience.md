# Contract: Retry, Circuit Breaker and Graceful Degradation

Modules: `resilience/classify.py`, `resilience/retry.py`, `resilience/circuit.py`,
`resilience/response_cache.py`, used by `agents/llm.py` (`ModelCaller`) and
`tools/` (MCP, sandbox, pytest) and `agents/memory.py` (Chroma). Research: R3–R6.

## Settings

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `RETRY_MAX_RETRIES` | `3` | 0–5 | retries after the first attempt (directive 2) |
| `RETRY_BASE_DELAY_SECONDS` | `1.0` | 0.1–10 | first backoff ceiling |
| `RETRY_MAX_DELAY_SECONDS` | `60` | 1–300 | cap on any single wait, including `retry-after` |
| `CIRCUIT_FAILURE_THRESHOLD` | `3` | 1–10 | consecutive failed calls that open the circuit (directive 2) |
| `CIRCUIT_COOLDOWN_SECONDS` | `30` | 1–3600 | open → half-open delay (directive 2: configurable) |
| `LLM_RESPONSE_CACHE_TTL_SECONDS` | `86400` | ≥ 0 (0 = off) | age limit for degraded cache hits |
| `LLM_RESPONSE_CACHE_DIR` | `.cache/llm` | | on-disk cache location |

Out-of-range values → exit 2 naming the variable.

## Guarded call semantics

```text
guard.call(operation) -> result | DependencyFailure
  if stop requested:                    return DependencyFailure(kind=permanent, error_type="interrupted")
  if breaker refuses (open, or half-open trial already taken):
                                        record event degraded; return DependencyFailure(circuit="open", attempts=0)
  for attempt in 0..max_retries:
      try: result = operation(); breaker.success(); return result
      except failure:
          (dependency, kind, cause) = classify(failure)
          if kind == permanent:          break
          if attempt == max_retries:     break
          delay = backoff(attempt, retry_after(failure))
          if delay is None:              break          # retry-after exceeds the cap
          record event retry; sleep(delay)
  breaker.failure()                     # one count per failed *call*
  return DependencyFailure(...)
```

- **Backoff**: `ceiling = min(max_delay, base * 2**attempt)` and
  `delay = ceiling/2 + rng.uniform(0, ceiling/2)`. With a `retry-after` value `r`, the
  wait is `max(r, delay)` if `r ≤ max_delay`. Otherwise there is no retry.
- **Where the breaker counts**: per call, not per attempt (R5). It opens on the
  `CIRCUIT_FAILURE_THRESHOLD`-th consecutive failed call. Any success resets the count.
- **Half-open**: after the cool-down, exactly one caller is admitted as the trial (with
  its full retry budget). Others fail fast until the trial resolves.
- **Thread safety**: the breaker state is guarded by a lock. Sleeps happen outside the
  lock.

## Classification

The table is in research R4. A `refusal` stop reason is not a dependency failure and is
never retried. A2A requests are never guarded or retried (FR-208).

## Degradation by caller

| Caller | When the guard returns `DependencyFailure` |
|---|---|
| `ModelCaller` (all agents) | 1) Response-cache lookup by request fingerprint. A fresh hit is validated and returned as `served_from="cache"`, with a `degraded` event (`served_from: cache`). 2) A miss returns the `DependencyFailure` to the agent. |
| PM | `PMResult.error = "Model service unavailable: <message>"`, with `failure` attached. The team ends `dependency_unavailable` (exit 3), not `pm_error`. |
| Coder `_think` / `_finalize` / `_critique` | the result status becomes `dependency_unavailable`, with `failure` attached. The team ends `dependency_unavailable`. |
| QA `_ask` | `QAStepError` carries the `failure`. The iteration result has `error` set and a `failure` field. The team ends `dependency_unavailable`. |
| Coder tool call (MCP / sandbox) | a tool result with `is_error: true`, content `json.dumps(DependencyFailure)`. The ReAct loop continues and the model decides. |
| QA MCP or pytest | `QAStepError` naming the dependency → `dependency_unavailable`. |
| Chroma (memory) | `query` → `[]` with a `degraded` event, and `add` is skipped with a `degraded` event. Memory is advisory, so the run continues. |

**Never crash**: no `DependencyFailure`, `CircuitOpen`, `BudgetExceeded` or validation
error escapes a graph node. Every node returns a state update. A
`test_never_crashes` matrix runs each node with each dependency forced open and asserts
a final state with an end reason and a cost report.

## MCP restart

The `mcp` operation wraps a session call. If the server process died
(`McpUnavailableError` mid-session, or `BrokenPipeError`), the attempt first starts a
new server for the same workspace and `--protect` list, then repeats the call. Each
restart is a `restart` event. Restarts count as attempts, so they are bounded by
`RETRY_MAX_RETRIES`.

## Sandbox

`exec_python` and `run_pytest` classify `docker run` results:

- exit 125, or stderr matching the daemon, engine or create-error patterns → temporary
  failure;
- exit codes from the generated code, and timeouts → normal results, never retried.

Every container is labelled `multi-agent-lab.run_id=<run_id>` and
`multi-agent-lab.owner=<host>:<pid>`.

## Response cache

- Key: `sha256(json.dumps({model, system, tools, messages, output_config}, sort_keys=True, default=str))`
  after `cache_control` keys are stripped.
- Value file `.cache/llm/<model>/<key>.json`:
  `{"stored_at": epoch, "model": str, "response": <BetaMessage.to_dict()>}`, mode 0600,
  written atomically.
- A hit is honoured only if `now - stored_at ≤ TTL` and the rebuilt message passes the
  same reply validation as a live response.
