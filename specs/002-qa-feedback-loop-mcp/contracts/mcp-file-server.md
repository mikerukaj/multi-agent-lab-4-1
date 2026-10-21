# Contract: MCP File-Tool Server and Client

Server: `tools/mcp_server.py`. Client: `tools/mcp_client.py`. SDK: `mcp==2.3.0`, using the
low-level `Server` (research R1–R5). The file functions behind both tools stay in
`tools/file_tools.py` and keep the behavior in
[001 contracts/tools.md](../../001-autonomous-dev-team/contracts/tools.md).

## Command line

```text
python -m tools.mcp_server --workspace DIR [--protect PREFIX]...
```

| Argument | Meaning |
|----------|---------|
| `--workspace DIR` | Required. The one folder every read and write is confined to (FR-121) |
| `--protect PREFIX` | Optional, can repeat. A workspace-relative folder that `write_file` refuses to write into. The team uses `--protect tests --protect .qa` for the Coder's sessions |

Transport: stdio, with JSON-RPC on stdin and stdout. Diagnostics go to stderr.

| Start-up case | Behavior |
|---------------|----------|
| `DIR` exists and is a directory | Serve until stdin closes, then exit 0 |
| `DIR` missing or not a directory | Print `error: workspace does not exist: DIR` to stderr and exit 2 before serving (FR-124) |
| A `--protect` value that is absolute or leaves the workspace | Print the error to stderr and exit 2 |

The server never reads `.env` and calls neither `load_dotenv` nor `os.environ` for
secrets (FR-125). The client gives it an environment containing only `PATH` and
`PYTHONIOENCODING`.

## Server identity and capabilities

- `Server("file-tools", version=messaging.__version__)`, which is `"0.3.0"`.
- The only capability is `tools`. There are no resources, prompts or sampling.
- `tools/list` returns exactly two tools, in this order (FR-120):

| `name` | `description` | `input_schema` |
|--------|---------------|----------------|
| `read_file` | `READ_FILE_TOOL["description"]` | `READ_FILE_TOOL["input_schema"]`, verbatim |
| `write_file` | `WRITE_FILE_TOOL["description"]` | `WRITE_FILE_TOOL["input_schema"]`, verbatim |

## `tools/call` behavior

The handler always returns `CallToolResult(content=[TextContent(type="text", text=…)],
is_error=…)`. It never raises for tool-level failures. In SDK v2, a raised exception
becomes a JSON-RPC error that the model would never see.

| Call | `is_error` | Text |
|------|-----------|------|
| `read_file` of a file up to 200 KB | false | the file contents |
| `read_file` of a larger file | false | the first 200 KB + `\n[truncated: file is N bytes]` |
| `read_file` of a missing file | true | `File not found: <path>` |
| `write_file`, valid | false | `Wrote <n> bytes to <path>` |
| absolute path, `..` escape, or a link out of the workspace (either tool) | true | `Path is outside the workspace: <path>` |
| `write_file` into a protected prefix | true | `Path is protected: <path> (only the QA Agent may change tests)` |
| a missing argument, a wrong type, or an extra key (validated in the handler, because SDK v2 does not) | true | `Invalid arguments for <tool>: <reason>` |
| an unknown tool name | true | `Unknown tool: <name>` |

After any error result, the server keeps serving (FR-122).

## Client: `McpFileTools`

```python
class McpUnavailableError(RuntimeError): ...

class McpFileTools:
    def __init__(self, workspace: Path, protect: tuple[str, ...] = (),
                 call_timeout_seconds: float = 30.0) -> None: ...
    def __enter__(self) -> "McpFileTools": ...  # start server, initialize, list_tools
    def __exit__(self, *exc) -> None: ...       # close session, stop server, join thread
    def definitions(self) -> list[dict]: ...    # Claude tool dicts, strict=True, no purpose
    def call(self, name: str, arguments: dict) -> tuple[str, bool]: ...  # (text, is_error)
```

- A background thread runs the asyncio loop that owns
  `stdio_client(StdioServerParameters(command=sys.executable, args=[…], env={…},
  cwd=<repo root>))` and `ClientSession` (research R3).
- `__enter__` raises `McpUnavailableError` if:
  - the server does not start or exits;
  - `initialize()` fails;
  - the tool list is not exactly `{read_file, write_file}`;
  - setup takes longer than 15 s.
- `call` raises `McpUnavailableError` if the session is closed, a JSON-RPC error comes
  back, or `call_timeout_seconds` passes. Tool-level errors come back as
  `(text, True)` and are **not** raised.
- `definitions()` maps `tool.name`, `tool.description` and `tool.input_schema` to
  `{"name", "description", "input_schema", "strict": True}`. `tools/__init__.py` then
  adds `purpose` with `_with_purpose`, as now.

## How the agents use it

| User | Session | `protect` | Calls |
|------|---------|-----------|-------|
| `CoderAgent.run()` (build task, fix round, revision) | one per `run()` | `("tests", ".qa")` | the model's `read_file`/`write_file` tool calls; `_finalize` reading the main file |
| `CoderAgent.reflect()` critique | one per round | `("tests", ".qa")` | reading the source files for the critique prompt |
| `QAAgent.run_iteration()` | one per iteration | `()` | reading the source and tests; writing test files |
| `QAAgent.final_report()` | one | `()` | writing `.qa/report.md` |

`tools.dispatch_tool(name, tool_input, workspace, timeout_seconds, image, file_tools)`
sends `read_file` and `write_file` to `file_tools.call(...)`, and runs `exec_python` in
process as now. `McpUnavailableError` is not a tool error: it ends the Coder session with
`status="error"` (research R3).

## Connecting another MCP client (FR-124, SC-106)

Any standard MCP client that launches stdio servers can use this command:

```json
{
  "mcpServers": {
    "file-tools": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["-m", "tools.mcp_server", "--workspace", "/abs/path/to/workspace/demo"],
      "cwd": "/abs/path/to/multi-agent-lab-4-1"
    }
  }
}
```

A standalone server started without `--protect` gives full access to the chosen
workspace (spec Assumption "Test protection boundary").
