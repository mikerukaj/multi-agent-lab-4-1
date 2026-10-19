"""MCP server exposing ``read_file`` and ``write_file`` for one workspace.

Run it on its own with ``python -m tools.mcp_server --workspace DIR [--protect PREFIX]``;
the Coder and QA agents start it the same way, one server per session, through
``tools.mcp_client.McpFileTools``. It speaks MCP over stdio using the official SDK's
low-level ``Server``. See specs/002-qa-feedback-loop-mcp/contracts/mcp-file-server.md.

The server never reads ``.env`` or any secret, and tool failures come back as
``is_error`` results instead of protocol errors, so the server keeps serving.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from messaging import __version__
from tools.file_tools import (
    READ_FILE_TOOL,
    WRITE_FILE_TOOL,
    protected_prefix,
    read_file,
    resolve_in_workspace,
    write_file,
)

SERVER_NAME = "file-tools"
TOOL_SPECS = {tool["name"]: tool for tool in (READ_FILE_TOOL, WRITE_FILE_TOOL)}
PROTECTED_MESSAGE = "Path is protected: {path} (only the QA Agent may change tests)"


def list_tool_definitions() -> list[Tool]:
    """Return the two tools with their descriptions and schemas copied unchanged."""
    return [
        Tool(name=spec["name"], description=spec["description"], input_schema=spec["input_schema"])
        for spec in TOOL_SPECS.values()
    ]


def validate_arguments(name: str, arguments: dict | None) -> str | None:
    """Check ``arguments`` against the tool's schema; return the problem, or ``None``.

    SDK v2 no longer validates tool input, so the handler does: every required key
    must be present, every value a string, and no other keys allowed.
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    schema = TOOL_SPECS[name]["input_schema"]
    missing = [key for key in schema["required"] if key not in arguments]
    if missing:
        return f"missing argument {', '.join(repr(key) for key in missing)}"
    extra = sorted(set(arguments) - set(schema["properties"]))
    if extra:
        return f"unexpected argument {', '.join(repr(key) for key in extra)}"
    wrong = [key for key, value in arguments.items() if not isinstance(value, str)]
    if wrong:
        return f"argument {', '.join(repr(key) for key in wrong)} must be a string"
    return None


def call_tool(
    workspace: Path, protect: tuple[str, ...], name: str, arguments: dict | None
) -> tuple[str, bool]:
    """Run one tool call and return ``(text, is_error)``; never raises."""
    if name not in TOOL_SPECS:
        return f"Unknown tool: {name}", True
    problem = validate_arguments(name, arguments)
    if problem:
        return f"Invalid arguments for {name}: {problem}", True
    if name == "read_file":
        return read_file(workspace, arguments["path"])
    if protected_prefix(workspace, arguments["path"], protect):
        return PROTECTED_MESSAGE.format(path=arguments["path"]), True
    return write_file(workspace, arguments["path"], arguments["content"])


def build_server(workspace: Path, protect: tuple[str, ...] = ()) -> Server:
    """Build the low-level MCP server bound to ``workspace``."""

    async def on_list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=list_tool_definitions())

    async def on_call_tool(
        ctx: ServerRequestContext, params: CallToolRequestParams
    ) -> CallToolResult:
        text, is_error = call_tool(workspace, protect, params.name, params.arguments)
        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)

    return Server(
        SERVER_NAME, version=__version__, on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )


def check_startup(workspace: Path, protect: list[str]) -> str | None:
    """Return the reason the server must not start, or ``None`` if it may."""
    if not workspace.is_dir():
        return f"workspace does not exist: {workspace}"
    for prefix in protect:
        try:
            resolve_in_workspace(workspace, prefix)
        except ValueError:
            return f"protected path is outside the workspace: {prefix}"
    return None


async def serve(workspace: Path, protect: tuple[str, ...]) -> None:
    """Serve MCP over stdin/stdout until the client closes the connection."""
    server = build_server(workspace, protect)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns 0 when the client disconnects, 2 for bad arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.mcp_server",
        description="Serve read_file and write_file for one workspace over MCP (stdio).",
    )
    parser.add_argument(
        "--workspace", required=True, help="folder every read and write is confined to"
    )
    parser.add_argument(
        "--protect", action="append", default=[], metavar="PREFIX",
        help="workspace-relative folder that write_file refuses (repeatable)",
    )
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).resolve()
    problem = check_startup(workspace, args.protect)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    asyncio.run(serve(workspace, tuple(args.protect)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
