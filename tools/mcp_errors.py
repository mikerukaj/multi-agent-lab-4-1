"""Errors raised when the MCP file-tool server can't be used.

Kept apart from ``tools.mcp_client`` so the failure classifier can recognise them
without importing the client, which itself uses the classifier through its guard.
"""


class McpUnavailableError(RuntimeError):
    """Raised when the MCP file-tool server cannot be started or reached."""


class McpStartupError(McpUnavailableError):
    """The server process exited at start-up (a bad workspace or protect path).

    Restarting it would fail the same way, so this is never retried.
    """
