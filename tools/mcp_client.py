"""Synchronous client for the MCP file-tool server (contracts/mcp-file-server.md).

The agents and their LangGraph graphs are synchronous, but the MCP SDK is async.
``McpFileTools`` runs one asyncio event loop in a background thread for the life of
a session: the loop starts ``python -m tools.mcp_server`` over stdio, keeps the
``ClientSession`` open, and serves each synchronous ``call`` through
``asyncio.run_coroutine_threadsafe`` (research R3). There is no fallback to calling
the file functions directly: if the server is unavailable, ``McpUnavailableError``
is raised.
"""

import asyncio
import concurrent.futures
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from resilience.circuit import DependencyGuard, GuardFailure
from tools.mcp_errors import McpStartupError, McpUnavailableError

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {"read_file", "write_file"}
SETUP_TIMEOUT_SECONDS = 15.0


def failure_in(text: str) -> dict | None:
    """The ``DependencyFailure`` a degraded tool result carries, or ``None``.

    A guarded call that gives up returns ``(json.dumps(failure), True)``; callers that
    must tell that apart from an ordinary tool error (QA) use this.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    keys = {"dependency", "kind", "circuit", "attempts"}
    return data if isinstance(data, dict) and keys <= data.keys() else None


class McpFileTools:
    """One MCP session with a file-tool server bound to ``workspace``.

    Use it as a context manager. ``protect`` lists workspace-relative folders
    that ``write_file`` refuses (the Coder's sessions pass ``("tests", ".qa")``).
    With a ``guard``, a server that dies mid-session is restarted for the same
    workspace and protections and the call repeated; each restart is one retry
    (FR-206), and a call the guard gives up on returns ``(failure JSON, True)``.
    """

    def __init__(
        self,
        workspace: Path,
        protect: tuple[str, ...] = (),
        call_timeout_seconds: float = 30.0,
        guard: DependencyGuard | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.protect = tuple(protect)
        self.call_timeout_seconds = call_timeout_seconds
        self.guard = guard
        self.restarts = 0
        self._dead = False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._session: ClientSession | None = None
        self._tools: list = []
        self._error: BaseException | None = None
        self._errlog = None

    def server_parameters(self) -> StdioServerParameters:
        """Return how the server process is started; its environment holds no secrets."""
        args = ["-m", "tools.mcp_server", "--workspace", str(self.workspace)]
        for prefix in self.protect:
            args += ["--protect", prefix]
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
        return StdioServerParameters(command=sys.executable, args=args, env=env, cwd=REPO_ROOT)

    def __enter__(self) -> "McpFileTools":
        self._start()
        return self

    def _start(self) -> None:
        """Start the server process and its session; raise if it is unusable."""
        self._ready = threading.Event()
        self._error, self._tools, self._session = None, [], None
        self._errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self._thread = threading.Thread(target=self._run_loop, name="mcp-file-tools", daemon=True)
        self._thread.start()
        if not self._ready.wait(SETUP_TIMEOUT_SECONDS):
            self.close()
            raise McpUnavailableError(
                f"MCP file-tool server did not start within {SETUP_TIMEOUT_SECONDS:.0f} s"
            )
        if self._error is not None:
            reason = self._server_stderr() or f"{type(self._error).__name__}: {self._error}"
            self.close()
            raise McpStartupError(f"MCP file-tool server unavailable: {reason}")
        names = {tool.name for tool in self._tools}
        if names != EXPECTED_TOOLS:
            self.close()
            raise McpUnavailableError(
                f"MCP file-tool server offers {sorted(names)}, not {sorted(EXPECTED_TOOLS)}"
            )

    def __exit__(self, *exc) -> None:
        self.close()

    def definitions(self) -> list[dict]:
        """Return the listed tools as Claude tool definitions (strict, no ``purpose``)."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.input_schema),
                "strict": True,
            }
            for tool in self._tools
        ]

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Call one tool and return ``(text, is_error)``.

        Tool-level failures (missing file, refused path) come back as
        ``(text, True)``. Without a guard, raises ``McpUnavailableError`` when the
        session is closed, the server answers with a protocol error, or the call times
        out. With one, those are retried (restarting the server) and a call that still
        fails returns ``(json.dumps(failure), True)``.
        """
        if self.guard is None:
            return self._call_once(name, arguments)
        outcome = self.guard.call(lambda: self._attempt(name, arguments))
        if isinstance(outcome, GuardFailure):
            return json.dumps(outcome.failure), True
        return outcome

    def _attempt(self, name: str, arguments: dict) -> tuple[str, bool]:
        """One guarded attempt: restart a server that died, then make the call."""
        if self._dead:
            self.close()
            self._start()
            self.restarts += 1
            self.guard.emit("restart", "warning", outcome="restarted",
                            message=f"MCP file-tool server restarted ({self.restarts})")
            self._dead = False
        try:
            return self._call_once(name, arguments)
        except McpUnavailableError:
            self._dead = True
            raise

    def _call_once(self, name: str, arguments: dict) -> tuple[str, bool]:
        if self._session is None or self._loop is None or self._loop.is_closed():
            raise McpUnavailableError("MCP file-tool session is closed")
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop
        )
        try:
            result = future.result(self.call_timeout_seconds)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise McpUnavailableError(
                f"MCP call {name} timed out after {self.call_timeout_seconds:.0f} s"
            ) from error
        except Exception as error:
            raise McpUnavailableError(f"MCP call {name} failed: {error}") from error
        if not isinstance(result, CallToolResult):
            raise McpUnavailableError(f"MCP call {name} returned {type(result).__name__}")
        text = "".join(block.text for block in result.content if block.type == "text")
        return text, bool(result.is_error)

    def close(self) -> None:
        """Close the session and stop the server process and loop thread."""
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass  # The loop already stopped on its own.
        if self._thread is not None:
            self._thread.join(timeout=SETUP_TIMEOUT_SECONDS)
        self._session = None
        if self._errlog is not None:
            self._errlog.close()
            self._errlog = None

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._session_main())
        except BaseException as error:  # Reported to __enter__ through self._error.
            self._error = self._error or error
        finally:
            self._ready.set()

    async def _session_main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            async with stdio_client(self.server_parameters(), errlog=self._errlog) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    self._tools = (await session.list_tools()).tools
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as error:
            self._error = error
            raise

    def _server_stderr(self) -> str:
        if self._errlog is None:
            return ""
        self._errlog.seek(0)
        return self._errlog.read().strip()
