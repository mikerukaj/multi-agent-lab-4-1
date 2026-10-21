# Quickstart & Validation: Autonomous Multi-Agent Development Team (Increments 1 and 2)

Proves the Coder Agent works end to end. Interfaces: [contracts/coder-agent-api.md](contracts/coder-agent-api.md),
[contracts/tools.md](contracts/tools.md). Types: [data-model.md](data-model.md).

## Prerequisites

- Python 3.12
- Docker, usable by your user (every code run happens in a throwaway sandbox container)
- Anthropic credentials: `ANTHROPIC_API_KEY` in `.env` (or an active `ant auth login` profile)
- Internet access on first run (the sandbox image is pulled, and Chroma downloads its ~80 MB
  embedding model once to `~/.cache/chroma`)

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env
```

## Scenario 1 — Offline tests (no API key needed)

```bash
python -m unittest discover -s tests -t . -v
```

Expected:
- Sandbox tests pass: generated code sees no API key (in its environment or under `/proc`),
  has no network, cannot see files outside its workspace, cannot write the root
  filesystem, and does not run as root.
- Tool tests pass: path escapes rejected, timeout kills a `while True` script, non-zero exit
  reported without `is_error`.
- Memory tests pass: sixth turn evicts the first; a stored memory is retrieved by a
  semantically similar query; other runs' memories only.
- Loop-limit test passes: a fake model that always calls a tool ends with
  `status == "step_limit"` after exactly 10 model calls, and the result still has
  `code`, `explanation`, `plan`, `execution_result`.
- Live smoke test reports **skipped** (no credentials).

## Scenario 2 — Live smoke test

```bash
python -m unittest tests.test_coder_agent_smoke -v
```

Expected: one live run of a short Python task (e.g. "write a function that returns the
n-th Fibonacci number and print fib(10)") passes; asserts the four required fields exist
with types `str`, `str`, `list[str]`, `dict`, and that `execution_result.exit_code == 0`.

## Scenario 3 — CLI, multi-step task

```bash
python -m agents.coder_agent "Write primes.py with is_prime(n), and main.py that imports it \
and prints the primes below 50 on one line separated by spaces; run main.py and fix any \
problems until the output is correct."
```

Expected:
- stderr shows one progress line per step (≤ 10).
- stdout JSON has `status: "completed"`, a non-empty `plan`, `main_file: "main.py"`, `code`
  that imports `is_prime`, and `execution_result.stdout` equal to `2 3 5 7 … 47`.
- `workspace/<run_id>/` contains `primes.py` and `main.py`, and no test files: writing tests
  is the QA Agent's job (spec FR-013), so the Coder checks its work by running it.
- Exit code 0.

## Scenario 4 — Memory across runs

```python
from agents.coder_agent import CoderAgent
agent = CoderAgent()
agent.run("Write greet.py that prints 'Hello, Priya' — my name is Priya.")
for i in range(6):
    agent.run(f"Write square_{i}.py that prints {i} squared.")
result = agent.run("Write a script that greets me by name.")
```

Expected: the first exchange has left the 5-turn window, yet the final `code` contains
`Priya` — recalled from Chroma.

## Scenario 5 — Sandbox limits

```bash
CODER_EXEC_TIMEOUT=2 python -m agents.coder_agent "Write loop.py containing an infinite \
loop and run it once; then report what happened."
```

Expected: the `exec_python` observation shows `timed_out: true` after ~2 s; the run still
finishes with a structured result; the `explanation` mentions the timeout.

---

# Increment 2: PM Agent and PM → Coder Handoff

Proves that the PM writes a spec and task list into shared state, that the Coder works
through the tasks in order from the `handoff` key, and that the 8-task cap holds.
Interfaces: [contracts/team-api.md](contracts/team-api.md),
[contracts/handoff-protocol.md](contracts/handoff-protocol.md),
[contracts/pm-agent-api.md](contracts/pm-agent-api.md). Types:
[data-model.md](data-model.md#increment-2-shared-team-state). Prerequisites and setup are
the same as above (no new packages).

## Scenario 6: Offline handoff tests (no API key, no Docker)

```bash
python -m unittest tests.test_pm_agent tests.test_team_handoff -v
python -m unittest discover -s tests -t . -v      # full suite still passes
```

Expected:
- After `pm_node` runs, `state["tasks"]` is non-empty. Every task has exactly
  `task_id, title, description, acceptance_criteria, priority` with the right types, IDs
  run `T1…Tn`, `tasks == spec["tasks"]`, and every `task_progress` entry is `pending`.
- `dispatch_node` puts the first pending task in `handoff` and marks it `in_progress`,
  and `json.dumps(handoff)` matches [task-handoff.schema.json](contracts/task-handoff.schema.json).
- A scripted 11-task plan is consolidated to ≤ 8 before the first handoff, both when the
  fake PM merges correctly (`method: "pm"`) and when its merge has too many tasks, an
  empty task list, or a dropped, reworded or reordered criterion (`method: "merge"`).
  Either way, every original acceptance criterion is still there, in order.
- A fake Coder that fails task T2 leaves T3 onwards `skipped` and the run `failed`.
- A rejected requirement never reaches the Coder.
- The final state passes `json.dumps` and round-trips unchanged.

## Scenario 7: End to end, small requirement (live)

```bash
python -m orchestration.team "A command-line tool that converts temperatures between \
Celsius and Fahrenheit. Usage: convert.py 100 C prints 212.0 F." > run.json
```

Expected:
- stderr shows `[pm] "...": N tasks` (1 ≤ N ≤ 8), then for each task, in order, a
  `[handoff] T<k> (...)` line, the Coder's `[step …]` lines, and `[coder] T<k> completed …`.
- Exit code 0. In `run.json`: `status: "completed"`, `spec` with name, description,
  acceptance criteria and assumptions, every `task_progress` entry `completed`, a
  `code_output` entry for each file written, and `handoff: null`.
- `workspace/<run_id>/` holds all tasks' files in one folder. Running the entry point
  there as the spec describes gives the requested output.
- `run.json` has the run's `duration_seconds` and each task's own `duration_seconds`.
- Checks only a live run can make (record them in tasks.md T024):
  - the `[pm]` line appears within about 1 minute of starting (plan goal);
  - the task order is buildable: no task needs code from a later task (FR-006b).

```bash
jq '{status, tasks: [.tasks[] | {task_id, priority, title}], progress: [.task_progress[] | {task_id, status}]}' run.json
```

## Scenario 8: Rejected requirement (live)

```bash
python -m orchestration.team "What's a good name for a cat?"; echo "exit $?"
```

Expected: `status: "rejected"`, `error` explains why, `tasks: []`, no `[handoff]` lines,
and exit code 1. The run's workspace folder stays empty.

## Scenario 9: Task cap (live, best effort)

```bash
python -m orchestration.team "A personal finance CLI: import CSV bank statements, \
categorise transactions by rules, monthly budgets per category, overspend alerts, \
recurring-payment detection, charts as ASCII, export to JSON and CSV, undo history, \
and a help command for every subcommand."
```

Expected: `len(tasks) ≤ 8` before the first `[handoff]` line. If the PM first proposed
more than 8 tasks, the output has `[pm] consolidated X → Y tasks (...)` and the state has
`consolidation` set. (The PM is asked to aim for 8 or fewer, so it may not need to
consolidate. Scenario 6 tests consolidation itself.)

## Scenario 10: Coder CLI unchanged

```bash
python -m agents.coder_agent "Write fizzbuzz.py that prints FizzBuzz for 1 to 15"
```

Expected: same behavior as Scenario 3. Without a `workspace` argument the Coder still
creates its own `workspace/<run_id>/`.
