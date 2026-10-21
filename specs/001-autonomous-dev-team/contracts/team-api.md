# Contract: Team Graph, Python API and CLI

`orchestration/team.py`. State types: [../data-model.md](../data-model.md#increment-2-shared-team-state).
Handoff rules: [handoff-protocol.md](handoff-protocol.md). Design: [../research.md](../research.md) R13–R18.

## Python API

```python
def initial_team_state(requirement: str, run_id: str, workspace: Path) -> TeamState: ...

def build_team_graph(pm: PMAgent, coder) -> CompiledStateGraph: ...

def run_team(
    requirement: str,
    pm: PMAgent | None = None,
    coder=None,
    workspace_root: Path = Path("workspace"),
    on_update: Callable[[str, dict], None] | None = None,
) -> TeamState: ...

# Node functions (public so tests can run one node on a state)
def pm_node(state: TeamState, pm: PMAgent) -> dict: ...
def consolidate_node(state: TeamState, pm: PMAgent) -> dict: ...
def dispatch_node(state: TeamState) -> dict: ...
def coder_node(state: TeamState, coder) -> dict: ...
def finish_node(state: TeamState) -> dict: ...

# Routers
def route_after_pm(state: TeamState) -> str: ...      # "finish" | "consolidate" | "dispatch"
def route_after_coder(state: TeamState) -> str: ...   # "dispatch" | "finish"

# Helpers
def add_usage(total: TokenUsage, extra: TokenUsage) -> TokenUsage: ...
def pending_progress(tasks: list[Task]) -> dict[str, TaskProgress]: ...
def render_coder_task(handoff: Task, spec: TechnicalSpec, position: int, total: int,
                      existing_files: list[str]) -> str: ...   # layout: handoff-protocol.md
def format_update(node: str, update: dict, current: Task | None) -> str | None: ...
#   current = the task most recently handed off (the CLI tracks it from `dispatch` updates
#   in a closure); the `[handoff]` line reads priority and title from the update itself
```

- `coder` is any object with `run(task: str, workspace: Path) -> CoderResult`. In
  production it is a `CoderAgent`, and tests pass a fake. `pm` and `coder` default to
  `PMAgent()` and `CoderAgent()` built from the environment. Building a `CoderAgent`
  raises `SandboxUnavailableError` without Docker.
- `run_team` creates `workspace_root/<run_id>/` and runs the graph with
  `recursion_limit = 2 × MAX_TASKS + 5`. It calls `on_update(node_name, update)` after
  each node (from `graph.stream`) and returns the final `TeamState`.
- Each node function returns only the fields it changes and does not modify its input.

## Behavior guarantees

| # | Guarantee |
|---|-----------|
| G1 | `run_team` raises `ValueError` only for an empty requirement. Every other outcome ends in `status` ∈ {`completed`, `failed`, `rejected`, `error`}, never `running`. |
| G2 | After `pm_node` with a buildable requirement, `tasks` is non-empty, schema-correct and equal to `spec["tasks"]`, and every task is `pending` in `task_progress`. |
| G3 | No `coder` step runs while `len(tasks) > 8` (rule H6). |
| G4 | Tasks are handed off one at a time, in order. `task_progress` and `code_output` are updated after each task (rules H1–H5). |
| G5 | The PM and the Coder run in one compiled `StateGraph`, in the caller's process. No subprocess, thread pool or second graph invocation is started for orchestration. |
| G6 | The final state passes `json.dumps`, and `json.loads` gives back an equal value. |
| G7 | Every task's code is written to the run's one workspace, `TeamState.workspace`. |
| G8 | The returned state's `duration_seconds` is the wall-clock time of the whole `run_team` call, and each task's `TaskProgress.duration_seconds` is that task's Coder time. |
| G9 | The `finish` update always carries the final `status`, so progress output can report it for every outcome. |

## CLI

```text
python -m orchestration.team "<requirement>" [--max-steps N] [--timeout SECONDS]
```

| Aspect | Contract |
|--------|----------|
| stdout | The final `TeamState` as indented JSON |
| stderr | One progress line per node update, plus the Coder's per-step lines: `[pm] "Temperature converter": 4 tasks`, `[pm] consolidated 11 → 8 tasks (pm)`, `[handoff] T2 (high) Convert between Celsius and Fahrenheit`, `[step 1] write_file converter.py`, `[coder] T2 completed in 3 steps (41.2 s)` or `[coder] T2 failed (step_limit)`, and last `[finish] completed` (or `failed`, `rejected`, `error`) |
| Exit code | `0` if `status == "completed"`; `1` for `failed`, `rejected` or `error`; `2` for usage errors (empty requirement, bad flag, Docker unavailable, bad `PM_*`/`CODER_*` setting) |
| Flags | `--max-steps` and `--timeout` apply to the Coder, as in `python -m agents.coder_agent` |
| Env | `.env` via `python-dotenv`; `PM_*` settings ([pm-agent-api.md](pm-agent-api.md)) and `CODER_*` settings (README) |
