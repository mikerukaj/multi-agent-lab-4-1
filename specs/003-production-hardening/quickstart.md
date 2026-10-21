# Quickstart: Validating Production Hardening (003)

Each scenario below proves one directive or user story. The scenarios marked
**(offline)** cost nothing. The ones marked **(live)** make real model calls and spend
money. Shapes and rules are in [data-model.md](data-model.md) and
[contracts/](contracts/), and are not repeated here.

## Prerequisites

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
docker version && docker compose version          # Engine 24+, Compose v2
```

## 1. The whole suite, with the coverage gate (offline): directive 5, US5

```bash
unset ANTHROPIC_API_KEY LANGSMITH_TRACING
scripts/test.sh; echo "exit=$?"
```

**Expected**:

- exit 0, with coverage ≥ 85% in the final report;
- skips only with a stated reason (`Docker is not available`, `RUN_LIVE_E2E not set`);
- it finishes in under 5 minutes.

The layers can be run one at a time:

```bash
python -m unittest discover -s tests -p 'test_*.py' -t .        # unit + everything
python -m unittest discover -s tests/integration -t .            # PM+Coder, Coder+QA
python -m unittest tests.e2e.test_pipeline -v                    # scripted e2e + supplied fixture
```

**Expected (e2e)**: `test_scripted_pipeline_passes_supplied_fixture` passes, which means:

- the program compiles, runs, and passes `tests/fixtures/e2e/test_inventory_fixture.py`;
- a cost report validates against the schema;
- exactly one trace root was recorded.

## 2. Validation blocks bad LLM output (offline): directive 4

```bash
python -m unittest tests.test_validation tests.test_pm_agent tests.test_qa_agent tests.test_coder_agent_loop -k validation -v
```

**Expected**:

- every reply type rejects a missing key, a wrong `Literal` value and an unknown key;
- the schema ⇄ `TypedDict` agreement test passes;
- an invalid tool input is answered with an `is_error` tool result, and no file is
  written.

## 3. Retry, circuit breaker, degradation (offline): directive 2

```bash
python -m unittest tests.test_resilience tests.test_model_caller tests.test_never_crashes -v
```

**Expected**:

- a scripted 429 with `retry-after: 2` followed by success → one `retry` event and the
  run continues;
- four temporary failures → `DependencyFailure(attempts=4)`;
- 3 consecutive failed calls → `circuit_opened`;
- a call during the cool-down fails fast (`attempts=0`) and returns the cached response
  if one exists, otherwise a structured error;
- after the cool-down, one half-open trial; success → `circuit_closed`;
- the never-crashes matrix: every node with every dependency open ends with an end
  reason and a cost report, and nothing is raised.

## 4. A live run: cost report, prompt caching, models by role (live): directives 3 and 7

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
python -m orchestration.team "A command-line tool that converts temperatures between Celsius and Fahrenheit"
echo "exit=$?"
ls docs/cost_reports/
python - <<'EOF'
import json, glob, os
path = max(glob.glob("docs/cost_reports/*.json"), key=os.path.getmtime)
r = json.load(open(path))
for name, a in r["agents"].items():
    print(name, a["model"], a["prompt_tokens"], a["completion_tokens"], a["cache_read_tokens"], a["cost_usd"])
print("total", r["total"]["cost_usd"])
EOF
```

**Expected**:

- stderr ends with `cost report: docs/cost_reports/<run_id>.json`;
- `pm` uses `claude-sonnet-5-5`, while `coder` and `qa` use `claude-haiku-5-5`;
- `cache_read_tokens > 0` for `coder`, and for `qa` when the run had more than one QA
  call;
- `total.cost_usd` equals the sum of the agents' costs, within $0.000001;
- recomputing one agent's cost by hand from its tokens and `cost/prices.json` agrees
  within 2% (SC-204).

To try the 1-hour TTL, run with `PROMPT_CACHE_TTL=1h`. The cost report then shows
`"prompt_cache_ttl": "1h"`. `PROMPT_CACHE_TTL=off` gives `cache_read_tokens == 0`.

## 5. LangSmith trace (live, needs a LangSmith key): directive 1

```bash
export LANGSMITH_TRACING=true LANGSMITH_API_KEY=lsv2_... LANGSMITH_PROJECT=multi-agent-lab
python -m orchestration.team "A tool that prints the primes below 50" --max-iterations 2
```

**Expected**: stderr prints `trace: https://smith.langchain.com/...`. In LangSmith:

- the project shows **one** `team-run` root for the run;
- every `agent:pm`, `agent:coder` and `agent:qa` span is nested under it, including QA
  and the fix round, which ran on the broker thread;
- each has the metadata `agent_name`, `token_count`, `tool_calls` and `duration_ms`;
- `llm:*` spans show token usage;
- searching the trace for the API key's last 8 characters finds nothing.

## 6. Budget and resume (live, small spend)

```bash
python -m orchestration.team "A tool that prints the primes below 50" --budget 0.001; echo "exit=$?"   # expect 3
RUN=$(ls -t workspace/.runs | head -1)
python -m orchestration.team --resume "$RUN"; echo "exit=$?"                                        # expect 2 (budget not raised)
python -m orchestration.team --resume "$RUN" --budget 1.00; echo "exit=$?"                          # expect 0 or 1
```

**Expected**:

- the first run ends `budget_exceeded`, overshooting by at most one call;
- the resume without a higher budget is refused, naming the reason;
- the resume with a higher budget continues from the last checkpoint with the same
  `run_id`, and the cost report shows two `attempts` with cumulative totals.

Interrupt test: start a run, press Ctrl-C during the build. **Expected**:

- the process exits 130 within 30 s;
- `docker ps --filter label=multi-agent-lab.run_id` is empty;
- `--resume` continues the run.

## 7. Health check (offline except for the Docker daemon)

```bash
python -m orchestration.health; echo "exit=$?"
ANTHROPIC_API_KEY= python -m orchestration.health; echo "exit=$?"     # expect 2, "credentials missing"
```

## 8. Docker Compose from a clean state (live): directive 6, US4

```bash
docker compose down -v --remove-orphans
docker image rm -f multi-agent-lab:local multi-agent-sandbox:py3.12-pytest9.1.1 2>/dev/null
rm -rf workspace/* workspace/.runs workspace/.qa-out .cache docs/cost_reports/*.json
cat >> .env <<EOF
HOST_UID=$(id -u)
HOST_GID=$(id -g)
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
EOF
docker compose up --build --abort-on-container-exit --exit-code-from agent; echo "exit=$?"
```

**Expected**:

- the `chroma` service becomes `healthy` before `agent` starts. `docker compose ps` or
  the log shows `chroma ... (healthy)` and then the agent's `[pm]` line;
- the first run builds the sandbox image through the host engine;
- the pipeline runs end to end and the command exits 0;
- on the host: `workspace/<run_id>/` (program, `tests/`, `.qa/report.md`),
  `workspace/.runs/<run_id>/events.jsonl`, `run.json` and `checkpoint.json`, and
  `docs/cost_reports/<run_id>.json`, all owned by your user.

Follow-ups:

```bash
docker compose run --rm --entrypoint python agent -m orchestration.health        # all ok
docker compose run --rm agent "Write a word-count CLI for a text file"           # second requirement, memory via Chroma
docker history --no-trunc multi-agent-lab:local | grep -c 'sk-ant-' ; echo        # expect 0
docker run --rm --entrypoint sh multi-agent-lab:local -c 'ls -a /app | grep -c "^.env"'   # expect 0
docker compose config | grep -n docker.sock                                       # only under agent
```

**Sandbox guarantees unchanged** (US4 scenario 3): `python -m unittest tests.test_tools -k network -v`
inside the agent container (`docker compose run --rm --entrypoint python agent -m unittest tests.test_tools -v`)
passes. Generated code has no network and sees no secrets.

**Concurrency** (SC-213):

```bash
for i in 1 2 3 4 5; do docker compose run --rm -d agent "Write a CLI that reverses each line of a file (#$i)"; done
```

**Expected**: all 5 reach their end reasons, and `docker compose ps chroma` still shows
`healthy`. The automated version of this check, `tests/integration/test_memory_concurrency.py`
(Docker-gated), starts 5 writers against one Chroma server and reads back every lesson
each one reported writing.

## 9. Benchmark against the baseline (live, about 10 runs): SC-205, SC-205a

```bash
# Before the model/caching changes (phase 0):
python -m benchmarks.run_benchmark      # commit benchmarks/results/<ts>.json as the baseline
# After:
python -m benchmarks.run_benchmark --baseline benchmarks/results/<baseline>.json
```

**Expected**:

- ≥ 8/10 items pass;
- mean cost per item is ≥ 40% lower than the baseline;
- median duration is not higher than the baseline.

If quality falls below 8/10, apply the fallback in research R8 and record the result in
plan.md.

## 10. README (directive 8)

The README has:

- the updated architecture diagram (root trace, guards, the Chroma service);
- the new configuration rows (`CODER_MODEL`/`QA_MODEL` defaults changed);
- exit code 3 and `a2a_failed` 1 → 3;
- the cost reports, tracing and deployment sections, with the trust exception;
- the new test layers and `scripts/test.sh`.
