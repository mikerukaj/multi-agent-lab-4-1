# Contract: Coder Agent Python API and CLI

Types referenced here are defined in [../data-model.md](../data-model.md).

## Python API (`agents/coder_agent.py`)

```python
class CoderAgent:
    def __init__(
        self,
        config: CoderConfig | None = None,
        client=None,
        on_step: Callable[[Step], None] | None = None,
    ) -> None: ...
    def run(self, task: str, workspace: Path | None = None) -> CoderResult: ...

def run_coder_agent(task: str, config: CoderConfig | None = None) -> CoderResult: ...
```

- `CoderAgent` holds the sliding window, the Chroma store, and the compiled LangGraph graph,
  so consecutive `run()` calls share short-term memory.
- `client` defaults to `anthropic.Anthropic(max_retries=3)`. Any object exposing
  `beta.messages.create(**kwargs)` with the same response shape may be injected (used by
  offline tests).
- `on_step`, if given, is called with each completed `Step` right after its tools run
  (the CLI uses it to print progress lines). Exceptions it raises are not caught.
- `workspace` (Increment 2): if given, the run reads, writes and executes files in that
  folder (created if missing) instead of a new `workspace/<run_id>/`. The team graph
  passes one folder for all of a run's tasks. `result.workspace` is its absolute path,
  and `files_written` lists only this run's writes.
- `run_coder_agent` is a convenience wrapper: one fresh `CoderAgent`, one `run()`.

### Behavior guarantees

| # | Guarantee |
|---|-----------|
| A1 | `run()` always returns a `CoderResult`; it never raises for model, tool, or limit failures (they set `status`/`error`). It raises `ValueError` only for an empty `task`. |
| A2 | `result.to_dict()` always contains `code` (str), `explanation` (str), `plan` (list[str]), and `execution_result` (dict or `None`). |
| A3 | Semantic memory is queried before **every** model call in the ReAct loop (the closing summary call in `finalize` reuses the history and does not query again). |
| A4 | The number of model calls in the ReAct loop never exceeds `config.max_steps` (≤ 10); hitting it yields `status == "step_limit"`. |
| A5 | All file I/O and code execution are confined to `result.workspace`. |
| A6 | `execution_result` reflects an actual run of `main_file` by the system, not a value reported by the model. |

### Example output (`to_dict()`, abridged)

```json
{
  "status": "completed",
  "run_id": "20261015-142233-a1b2c3",
  "code": "def fizzbuzz(n):\n    ...\n\nif __name__ == \"__main__\":\n    ...",
  "explanation": "Wrote fizzbuzz.py with a pure function and a main block that prints 1-15.",
  "plan": [
    "Write fizzbuzz.py with a fizzbuzz(n) function",
    "Run it and check the output for 1..15",
    "Fix the off-by-one in the range and re-run"
  ],
  "execution_result": {
    "path": "fizzbuzz.py", "exit_code": 0, "stdout": "1\n2\nFizz\n...",
    "stderr": "", "timed_out": false, "duration_seconds": 0.04
  },
  "main_file": "fizzbuzz.py",
  "files_written": ["fizzbuzz.py"],
  "steps_used": 4,
  "duration_seconds": 41.7,
  "usage": {"input_tokens": 18342, "output_tokens": 2911},
  "steps": [{"number": 1, "thought": "...", "actions": [{"tool": "write_file", "...": "..."}]}],
  "workspace": "/abs/path/workspace/20261015-142233-a1b2c3",
  "error": null
}
```

## CLI

```text
python -m agents.coder_agent "<task>" [--max-steps N] [--timeout SECONDS]
```

| Aspect | Contract |
|--------|----------|
| stdout | `CoderResult.to_dict()` as indented JSON |
| stderr | Progress lines: one per step (`[step 3] write_file fizzbuzz.py`) |
| Exit code | `0` if `status == "completed"`, `1` for any other status, `2` for usage errors (missing task, bad flag) |
| Env | Reads `.env` via `python-dotenv`; needs Anthropic credentials (see [../quickstart.md](../quickstart.md)) |
