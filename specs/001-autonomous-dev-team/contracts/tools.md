# Contract: Coder Agent Tools

Implemented in `tools/` as plain Python functions; each is also exposed to Claude as a
strict tool definition (`"strict": true`, `"additionalProperties": false`). Every function
receives the run's workspace directory from the agent — the model never chooses it.

Errors never raise out of the tool layer: they come back as text with `is_error: true` in the
`tool_result`, so the model can recover.

Every tool definition sent to the model also has a required `purpose` string ("One short
sentence: what this call is for and why."), added by `tools/__init__.py`. It is ignored by
the tool functions; the agent uses it as a step's thought when the model wrote no note, so
every step in the trace says why it ran. The schemas below show the tool-specific
arguments only.

## read_file

```json
{
  "name": "read_file",
  "description": "Read a UTF-8 text file from the task workspace. Use a path relative to the workspace root.",
  "strict": true,
  "input_schema": {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "Workspace-relative file path"}},
    "required": ["path"],
    "additionalProperties": false
  }
}
```

| Case | Result |
|------|--------|
| File exists, ≤ 200 KB | File contents |
| File > 200 KB | First 200 KB + `\n[truncated: file is N bytes]` |
| Missing file | `is_error`, `File not found: <path>` |
| Absolute path, `..` escape, or symlink outside workspace | `is_error`, `Path is outside the workspace: <path>` |

## write_file

```json
{
  "name": "write_file",
  "description": "Create or overwrite a UTF-8 text file in the task workspace. Parent folders are created as needed.",
  "strict": true,
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "Workspace-relative file path"},
      "content": {"type": "string", "description": "Full file contents"}
    },
    "required": ["path", "content"],
    "additionalProperties": false
  }
}
```

| Case | Result |
|------|--------|
| Valid path | `Wrote <n> bytes to <path>`; path added to `files_written` |
| Path outside workspace | `is_error`, `Path is outside the workspace: <path>` |

## exec_python

```json
{
  "name": "exec_python",
  "description": "Run a Python file from the task workspace in a fresh sandbox and return its exit code, stdout and stderr. The sandbox has a time limit, no stdin, no network, and no access to secrets or to files outside the workspace.",
  "strict": true,
  "input_schema": {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "Workspace-relative .py file to run"}},
    "required": ["path"],
    "additionalProperties": false
  }
}
```

Execution: a new `docker run --rm` container per call, from the pinned sandbox image
(`CoderConfig.sandbox_image`), running `python -s <path>` with `--network none`,
`--read-only` (64 MB `/tmp` tmpfs), `--cap-drop ALL`, `--security-opt no-new-privileges`,
`--memory 512m --cpus 1 --pids-limit 128`, the host user's uid:gid, only the run's
workspace mounted at `/work`, no stdin, and an environment of `HOME=/tmp`,
`PYTHONIOENCODING=utf-8`, `PYTHONDONTWRITEBYTECODE=1`. Timeout
`CoderConfig.exec_timeout_seconds` (default 30 s), container start-up included; on timeout
the container is killed.

Returned to the model as JSON text of an `ExecutionResult`
([../data-model.md](../data-model.md#executionresult)).

| Case | Result |
|------|--------|
| Runs to completion (any exit code) | `ExecutionResult`; `is_error` false (a non-zero exit is information, not a tool failure) |
| Exceeds timeout | Process killed; `exit_code: null`, `timed_out: true` |
| Missing file / not `.py` / outside workspace | `is_error` with a message |
| Output > 10,000 chars per stream | Truncated with `[truncated]` marker |

## Python signatures

```python
# tools/file_tools.py
def read_file(workspace: Path, path: str) -> ToolOutput: ...
def write_file(workspace: Path, path: str, content: str) -> ToolOutput: ...

# tools/python_exec.py
def exec_python(
    workspace: Path, path: str, timeout_seconds: float, image: str = SANDBOX_IMAGE
) -> ExecutionResult: ...
def ensure_sandbox(image: str = SANDBOX_IMAGE) -> None: ...  # raises SandboxUnavailableError
```

`ToolOutput` is `(text: str, is_error: bool)`. Tool JSON definitions live next to their
functions so the agent can import them as a list (`TOOL_DEFINITIONS`).
