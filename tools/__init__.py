"""Tools the agents can call, and a dispatcher that never raises for tool failures.

``read_file`` and ``write_file`` are served by the MCP file-tool server
(``tools.mcp_server``) and reached through an ``McpFileTools`` session; their
definitions come from the server's tool list. ``exec_python`` runs in process,
because it needs the run's sandbox image and time limit.
"""

import json
from pathlib import Path

from tools.python_exec import EXEC_PYTHON_TOOL, SANDBOX_IMAGE, ExecutionResult, exec_python

PURPOSE_PROPERTY = {
    "type": "string",
    "description": "One short sentence: what this call is for and why.",
}
FILE_TOOL_NAMES = ("read_file", "write_file")


def _with_purpose(tool: dict) -> dict:
    """Add a required ``purpose`` argument, so every step's trace says why it ran."""
    schema = tool["input_schema"]
    return {
        **tool,
        "input_schema": {
            **schema,
            "properties": {**schema["properties"], "purpose": PURPOSE_PROPERTY},
            "required": [*schema["required"], "purpose"],
        },
    }


def session_tool_definitions(file_tools) -> list[dict]:
    """Return the model's tool list for one session: the MCP file tools, then ``exec_python``.

    ``file_tools`` is an open ``McpFileTools`` session (or a stand-in with
    ``definitions()``). The list is built once per session and never changes
    within it, which keeps the history valid for preserved thinking.
    """
    return [_with_purpose(tool) for tool in file_tools.definitions()] + [
        _with_purpose(EXEC_PYTHON_TOOL)
    ]


def dispatch_tool(
    name: str,
    tool_input: dict,
    workspace: Path,
    timeout_seconds: float,
    image: str = SANDBOX_IMAGE,
    file_tools=None,
    guard=None,
    run_id: str = "",
) -> tuple[str, bool, ExecutionResult | None]:
    """Run the named tool and describe the outcome for the model.

    Returns the text for the ``tool_result`` block, whether it is an error,
    and the ``ExecutionResult`` when ``exec_python`` ran. Tool failures come
    back as error text so the model can recover; they are never raised.
    ``McpUnavailableError`` from an unguarded ``file_tools`` is not a tool failure
    and propagates to the caller. ``guard`` (the sandbox's) and ``run_id`` are
    passed to ``exec_python``; a sandbox it gives up on comes back as the
    ``DependencyFailure`` JSON with ``is_error`` set, so the session goes on.
    """
    if name in FILE_TOOL_NAMES:
        if file_tools is None:
            return f"{name} is unavailable: no MCP file-tool session", True, None
        arguments = {key: value for key, value in tool_input.items() if key != "purpose"}
        return (*file_tools.call(name, arguments), None)
    try:
        if name == "exec_python":
            result = exec_python(
                workspace, tool_input["path"], timeout_seconds, image, guard, run_id
            )
            if result.failure:
                return json.dumps(result.failure), True, None
            return json.dumps(result.to_dict()), False, result
    except (ValueError, FileNotFoundError) as error:
        return str(error), True, None
    except KeyError as error:
        return f"Missing tool argument: {error}", True, None
    return f"Unknown tool: {name}", True, None
