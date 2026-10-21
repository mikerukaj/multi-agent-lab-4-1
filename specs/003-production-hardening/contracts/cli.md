# Contract: Command-Line Interfaces

Research: R8, R13–R17. Existing options and output keep their meaning (FR-201).

## `python -m orchestration.team`

```text
python -m orchestration.team "<requirement>" [--max-iterations N] [--max-steps N]
                             [--timeout SECONDS] [--budget USD]
python -m orchestration.team --resume <run_id> [--budget USD]
```

| Option | New? | Meaning |
|---|---|---|
| `--max-iterations`, `--max-steps`, `--timeout` | no | unchanged; refused with `--resume` (FR-213c) |
| `--budget USD` | yes | per-run cap (overrides `RUN_BUDGET_USD`). With `--resume`, it may only raise the stored cap. |
| `--resume RUN_ID` | yes | continue a run from its last checkpoint (rules below). A requirement argument is refused alongside it. |

**Before the first model call**, the command runs the health checks (see
`orchestration.health`) and removes stale labelled containers. It refuses with exit 2
on a failing prerequisite, a bad setting, an unsupported model or setting combination
(FR-225b), or a budget with an unpriced model (FR-224).

**Output**:

- stdout: the final `TeamState` as JSON, as before, now with `agent_usage`, `cost_usd`,
  `failure`, `resume_history`, `events_lost`, `trace_url`, `cost_report_path` and
  `summary`.
- stderr: the progress lines (now derived from events), then
  `cost report: docs/cost_reports/<run_id>.json`, then `QA report: …`, then
  `trace: <url>` when tracing is on.

### Exit codes (FR-201a)

| Code | When |
|---|---|
| 0 | `status == "completed"` |
| 1 | ended for good: `failed`/`rejected`/`error` with a non-resumable end reason |
| 2 | usage error, bad setting, failing health item, Docker unavailable, refused resume |
| 3 | resumable end: `dependency_unavailable`, `a2a_failed`, `budget_exceeded` (**`a2a_failed` was 1 before this feature**) |
| 130 / 143 | `interrupted` by SIGINT / SIGTERM |

### Resume rules (FR-213a–d)

| Previous outcome | `--resume` |
|---|---|
| `interrupted`, `dependency_unavailable`, `a2a_failed` | allowed |
| `budget_exceeded` | allowed only with `--budget` higher than the stored cap |
| no final record, lock not held (killed) | allowed |
| lock held (still running) | refused, exit 2 |
| `complete`, `iteration_limit`, `rejected`, `pm_error`, `task_failed`, `coder_failed`, `reflection_failed`, `qa_failed` | refused, exit 2, message names the end reason |
| checkpoint missing, `complete != true`, unknown `format_version`, incompatible `app_version`, unparsable | refused, exit 2; no file changed |

On resume the workspace is restored from the snapshot, the restored and removed files
are listed in the `ResumeAttempt`, and the graph continues at `next_node` with the same
`run_id`. Iterations, usage, cost, the A2A log and the budget carry forward. The cost
report is rewritten with cumulative totals.

## `python -m orchestration.health`

```text
python -m orchestration.health [--json]
```

It checks `credentials`, `docker`, `sandbox_image`, `workspace`, `cost_report_dir`,
`chroma`, `price_table` and `langsmith` (info only). It makes no model call. Exit 0 if
every non-info item is `ok`, otherwise 2. With `--json` the output is a `HealthReport`
(data-model §2.6). Otherwise there is one line per item:
`ok      docker        Docker 26.1.3 reachable`.

## `python -m benchmarks.run_benchmark` (additions)

| Option | Meaning |
|---|---|
| `--baseline FILE` | compare against a stored results file: print the cost reduction (%) and the change in median duration (SC-205/205a) |

Each results item gains `cost_usd`, `cost_by_agent` and `cost_report`. The totals gain
`total_cost_usd`, `mean_cost_usd` and `median_duration_seconds`.

## Configuration added by this feature

| Variable | Default | Meaning |
|---|---|---|
| `CODER_MODEL` | `claude-haiku-5-5` (**was** `claude-sonnet-5-5`) | Coder model |
| `QA_MODEL` | `claude-haiku-5-5` (**was** `claude-sonnet-5-5`) | QA model |
| `PM_MODEL` | `claude-sonnet-5-5` (unchanged) | PM model |
| `*_USE_FALLBACKS` | on if the model supports server-side fallback (PM: on; Haiku: off) | `true` with a Haiku model → exit 2 |
| `PROMPT_CACHE_TTL` | `5m` | `5m`, `1h` or `off` |
| `RUN_BUDGET_USD` | unset (no cap) | per-run cap |
| `PRICE_TABLE_PATH` | `cost/prices.json` | price table |
| `COST_REPORT_DIR` | `docs/cost_reports` | cost report folder |
| `RETRY_*`, `CIRCUIT_*`, `LLM_RESPONSE_CACHE_*` | see [resilience.md](resilience.md) | |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`, `LANGSMITH_LOG_PROMPTS` | see [tracing.md](tracing.md) | |
| `LOG_LEVEL` | `info` | event severity threshold |
| `CHROMA_HOST`, `CHROMA_PORT` | unset, `8000` | use the Chroma server (HttpClient) instead of embedded `CODER_CHROMA_PATH` |
| `QA_OUTPUT_ROOT` | system temp | where QA's JUnit output folder is created (a host path under Compose) |
