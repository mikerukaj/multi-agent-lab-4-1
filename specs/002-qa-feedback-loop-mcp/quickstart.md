# Quickstart: Validating the QA Agent, the Feedback Loop and the MCP File Tools

**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)

These scenarios show the feature working end to end. Interfaces are in
[contracts/](contracts/), and types are in [data-model.md](data-model.md).

## Prerequisites

- Linux, Python 3.12, and Docker that the current user can run.
- Network access the first time only: to install packages and build the sandbox image.
- An `ANTHROPIC_API_KEY` in `.env` (or another credential the Anthropic SDK resolves) for
  scenarios 4–7. Scenarios 1–3 and 8 need no credentials.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # now includes mcp==2.3.0
python -c "from tools.python_exec import ensure_sandbox; ensure_sandbox()"
docker image inspect multi-agent-sandbox:py3.12-pytest9.1.1 --format '{{.Id}}'
```

**Expected**: The image is built once from `sandbox/Dockerfile` (about 30 s), and
`inspect` prints an image ID. Later runs skip the build.

## 1. Offline test suite

```bash
python -m unittest discover -s tests -t .
```

**Expected**: Everything passes with no credentials. This includes the new modules
`test_qa_agent`, `test_test_runner`, `test_mcp_server`, `test_coder_reflection` and
`test_team_loop` (research R15). Docker-gated cases are skipped when Docker is missing.

## 2. Standalone MCP server with a standard client (US3, SC-106, SC-107)

```bash
mkdir -p workspace/demo
python - <<'EOF'
from pathlib import Path
from tools.mcp_client import McpFileTools

with McpFileTools(Path("workspace/demo")) as tools:
    print([d["name"] for d in tools.definitions()])           # ['read_file', 'write_file']
    print(tools.call("write_file", {"path": "hello.txt", "content": "hi"}))
    print(tools.call("read_file", {"path": "hello.txt"}))     # ('hi', False)
    print(tools.call("read_file", {"path": "../README.md"}))  # (..outside the workspace.., True)
    print(tools.call("read_file", {"path": 1}))               # (Invalid arguments.., True)
    print(tools.call("read_file", {"path": "hello.txt"}))     # still serving: ('hi', False)
EOF
python -m tools.mcp_server --workspace workspace/missing; echo "exit=$?"   # exit=2
```

To check with an outside client, add the `mcpServers` entry from
[contracts/mcp-file-server.md](contracts/mcp-file-server.md) to any MCP client that
launches stdio servers. Its tool list must show exactly `read_file` and `write_file`.

## 3. Test protection for the Coder (FR-111)

```bash
python - <<'EOF'
from pathlib import Path
from tools.mcp_client import McpFileTools

with McpFileTools(Path("workspace/demo"), protect=("tests", ".qa")) as tools:
    print(tools.call("write_file", {"path": "tests/test_x.py", "content": ""}))
    # ('Path is protected: tests/test_x.py (only the QA Agent may change tests)', True)
    print(tools.call("read_file", {"path": "hello.txt"}))     # reads still allowed
EOF
```

## 4. Coder self-reflection on its own (FR-126)

```bash
python -m agents.coder_agent "Write fizzbuzz.py that prints FizzBuzz for 1..15"
```

**Expected**: The step lines for the first draft are followed by a second session (the
revision). In the JSON:
- `reflection` has one round with at least one critique item.
- Every `C…` item is answered in `finding_responses`.
- The top-level result is the revision's result.

## 5. Full team run that succeeds (US1, US2)

```bash
python -m orchestration.team "A command-line tool temp.py that converts a temperature like '100C' or '32F' to the other scale, rejecting malformed input with exit code 2"
```

**Expected** stderr, in order:
1. `[pm]` and `[handoff]` lines, then `[coder] … completed` for each task.
2. `[reflect] before QA #1: N critique items, revision …`.
3. `[qa] #1: …`. If findings come back, `[fix] #1 …`, `[reflect] before QA #2 …` and
   `[qa] #2 …` follow.
4. `[finish] completed (complete) · report: workspace/<run_id>/.qa/report.md`.

Exit code 0. In the JSON, `iterations` has at most 5 entries, and the last one has
`verdict: "complete"` and `test_run.failed == 0`. `qa_report.unresolved` is empty.
`workspace/<run_id>/tests/` holds pytest files whose tests name acceptance criteria.

## 6. The iteration limit ends the run with a report (FR-114, FR-118, SC-101)

```bash
TEAM_MAX_ITERATIONS=1 python -m orchestration.team "A library stats.py with mean, median and mode for lists of numbers; mode returns all values tied for most frequent, sorted; every function raises ValueError on an empty list"
```

**Expected**: Exactly one `[qa]` line, and no `[fix]` line after it. If iteration 1 is
not complete, the run ends with `[finish] failed (iteration_limit)` and exit code 1.
`qa_report.unresolved` lists each blocking finding with its fix instruction, and
`.qa/report.md` says the same. (If the first draft happens to pass, the run succeeds in
one iteration, which is also correct.) The loop tests in scenario 1 check the
failure path without needing a model.

## 7. Iteration limit validation (FR-115)

```bash
python -m orchestration.team --max-iterations 0 "anything";  echo "exit=$?"  # exit=2
TEAM_MAX_ITERATIONS=11 python -m orchestration.team "anything"; echo "exit=$?"  # exit=2
TEAM_MAX_ITERATIONS=2 python -m orchestration.team --max-iterations 3 "..."  # 3 wins
```

**Expected**: The first two exit with code 2 before any model call, with a message
giving the range 1–10.

## 8. Delivered tests give the same results outside the system (SC-104)

Using the `<run_id>` from scenario 5:

```bash
W="$PWD/workspace/<run_id>"
docker run --rm --network none --read-only --tmpfs /tmp \
  -v "$W:/work" -v "$W/tests:/work/tests:ro" -w /work \
  multi-agent-sandbox:py3.12-pytest9.1.1 \
  python -s -m pytest tests -q -p no:cacheprovider -c /dev/null \
    --rootdir=/work --confcutdir=/work/tests
```

These are the same pytest options the system uses (`tools/test_runner.pytest_command`,
[contracts/qa-agent-api.md](contracts/qa-agent-api.md)), so a `pytest.ini`,
`pyproject.toml` or root `conftest.py` in the workspace cannot change the result.

**Expected**: The pass and fail counts match the last iteration's `test_run` counts.

## 9. The A2A broker on its own (FR-129–FR-131, no credentials)

```bash
python - <<'PY'
from messaging.a2a import A2AError, data_part, text_part, user_message
from messaging.broker import A2ABroker
from messaging.endpoints import qa_card

def echo(message):                      # stands in for the QA agent
    return [{"artifactId": "x", "name": "echo", "parts": [message["parts"][1]]}]

with A2ABroker(request_timeout_seconds=5) as broker:
    broker.register(qa_card("dev"), echo)
    client = broker.client("coder")
    card = client.get_card("qa-debugger")
    print(card["capabilities"], [s["id"] for s in card["skills"]])
    msg = user_message([text_part("hi"), data_part({"n": 1})],
                       context_id="demo", metadata={"skillId": "review-program", "sender": "coder"})
    task = client.send_message("qa-debugger", msg)
    print(task["status"]["state"], task["artifacts"][0]["parts"][0]["data"])  # TASK_STATE_COMPLETED {'n': 1}
    bad = user_message([text_part("x")], context_id="demo", metadata={"skillId": "nope", "sender": "coder"})
    print(client.send_message("qa-debugger", bad)["status"]["state"])         # TASK_STATE_REJECTED
    try:
        broker.client("coder").send_message("nobody", msg)
    except A2AError as error:
        print(error.code, error.type_name)                                    # -32602 UnknownAgent
    print([(e["sender"], e["recipient"], e["outcome"]) for e in broker.exchanges()])
PY
python -m unittest tests.test_a2a tests.test_broker tests.test_agent_isolation
```

**Expected**: The output matches the inline comments. The card shows all three
capabilities as `false`. The unit tests pass, including the version (-32009),
cancellation (-32002), streaming (-32004), timeout and isolation cases (research R22).

## 10. A2A exchanges in a real run's record (SC-109)

Using the JSON printed by scenario 5, saved as `run.json`:

```bash
python - <<'PY'
import json
state = json.load(open("run.json"))
log, iters = state["a2a_log"], state["iterations"]
reviews = [e for e in log if e["skill_id"] == "review-program"]
fixes = [e for e in log if e["skill_id"] == "fix-program"]
assert len(reviews) == len(iters) and len(fixes) <= len(iters) - 1
assert {e["context_id"] for e in log} == {state["run_id"]}
assert all(e["sender"] == "coder" and e["recipient"] == "qa-debugger" for e in reviews)
assert all(f["reference_task_ids"] == [iters[k]["review_task_id"]] for k, f in enumerate(fixes))
print("A2A record OK:", len(reviews), "reviews,", len(fixes), "fixes")
PY
```

**Expected**: `A2A record OK: …`. The stderr lines from scenario 5 include one
`[a2a] coder → qa-debugger review-program …` line for each QA iteration.

## 11. Benchmark for SC-102 and SC-103 (live, costs 10 team runs)

Design: [contracts/benchmark.md](contracts/benchmark.md).

```bash
python -m unittest tests.test_benchmark                      # offline: file, scoring, runner
python -m benchmarks.run_benchmark --only temp-convert       # one live item: a quick check
python -m benchmarks.run_benchmark                           # the full evaluation: all 10 items
```

**Expected**:
- The offline tests pass.
- The single-item run prints `Running 1 benchmark requirements (live model calls)`,
  writes `benchmarks/results/<timestamp>.json`, reports SC-102 and SC-103 as
  `not evaluated (partial run)`, and exits with code 1.
- The full run writes a results file with 10 records and a summary:
  - `sc_102` must be met (≥ 8 of 10 succeeded);
  - `sc_103` must be met (≥ 70% of the first-draft failures recovered, or `n/a`);
  - the exit code is 0 only when both are met.
- Commit the results file. Later evaluations must report the same `fingerprint`;
  otherwise the runner warns that the results are not comparable.
