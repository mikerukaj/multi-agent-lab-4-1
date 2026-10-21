"""Tell temporary dependency failures (retried) from permanent ones (research R4, FR-203).

``classify`` maps an exception, or a sandbox ``docker run`` outcome, to
``(dependency, kind, cause)``, or ``None`` when it is not a dependency failure at all
(a tool result, the generated code's own exit code, a timeout). The SDK's typed
exception classes decide; messages are matched only for the Docker CLI's stderr,
which has no other signal.
"""

import subprocess

from orchestration.state import Dependency, DependencyFailure, FailureKind

TEMPORARY_STATUS_CODES = (408, 409)
DOCKER_DAEMON_PATTERNS = (
    "Cannot connect to the Docker daemon",
    "Error response from daemon",
    "failed to create",
)
DOCKER_ERROR_EXIT_CODE = 125
"""``docker run`` exits 125 when Docker itself, not the container, failed."""

CIRCUIT_OPEN = "circuit_open"
"""``DependencyFailure.error_type`` of a call refused because its circuit was open."""
BUDGET_EXCEEDED = "budget_exceeded"
"""``DependencyFailure.error_type`` of a model call not started because the run's spend
reached its budget (FR-223). Not a dependency failure: it ends the run
``budget_exceeded``, which is resumable with a higher budget."""

Classification = tuple[Dependency, FailureKind, str]


def classify(error_or_outcome: object, dependency: Dependency) -> Classification | None:
    """Classify one failed attempt at calling ``dependency``.

    Returns ``(dependency, "temporary" | "permanent", cause)``, or ``None`` if
    ``error_or_outcome`` is not a dependency failure.
    """
    if dependency == "anthropic":
        return _classify_anthropic(error_or_outcome)
    if dependency == "mcp":
        return _classify_mcp(error_or_outcome)
    if dependency == "sandbox":
        return _classify_sandbox(error_or_outcome)
    if dependency == "chroma":
        return _classify_chroma(error_or_outcome)
    return None


def _classify_anthropic(error: object) -> Classification | None:
    # Deferred: the SDK takes about a second to import, and the MCP file-tool server
    # process imports this module (through the tools package) without needing it.
    import anthropic

    if not isinstance(error, anthropic.APIError):
        return None
    cause = f"{type(error).__name__}: {error}"
    if isinstance(error, anthropic.APIConnectionError):  # Includes APITimeoutError.
        return "anthropic", "temporary", cause
    if isinstance(error, anthropic.APIStatusError):
        status = error.status_code
        temporary = status == 429 or status >= 500 or status in TEMPORARY_STATUS_CODES
        return "anthropic", "temporary" if temporary else "permanent", cause
    return "anthropic", "permanent", cause


def _classify_mcp(error: object) -> Classification | None:
    # Deferred: importing the tools package loads the sandbox runner, which imports the
    # guard, which imports this module.
    from tools.mcp_errors import McpStartupError, McpUnavailableError

    if isinstance(error, McpStartupError):
        return "mcp", "permanent", str(error)
    if isinstance(error, (McpUnavailableError, BrokenPipeError, EOFError, ConnectionError)):
        return "mcp", "temporary", f"{type(error).__name__}: {error}"
    return None


def _classify_sandbox(outcome: object) -> Classification | None:
    if isinstance(outcome, subprocess.CompletedProcess):
        stderr = outcome.stderr if isinstance(outcome.stderr, str) else (
            outcome.stderr or b""
        ).decode("utf-8", errors="replace")
        daemon = any(pattern in stderr for pattern in DOCKER_DAEMON_PATTERNS)
        if outcome.returncode == DOCKER_ERROR_EXIT_CODE and daemon:
            return "sandbox", "temporary", stderr.strip()[:500]
        return None
    if isinstance(outcome, FileNotFoundError) and "docker" in str(outcome):
        return "sandbox", "permanent", "the docker command is not installed"
    return None


def _classify_chroma(error: object) -> Classification | None:
    # By name: agents.memory may swap pysqlite3 in for sqlite3, so the class can differ.
    if type(error).__name__ == "OperationalError" and "locked" in str(error):
        return "chroma", "temporary", f"OperationalError: {error}"
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return "chroma", "temporary", f"{type(error).__name__}: {error}"
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "chroma", "permanent", f"{type(error).__name__}: {error}"
    if isinstance(error, Exception) and type(error).__module__.startswith(("chromadb", "httpx")):
        return "chroma", "temporary", f"{type(error).__name__}: {error}"
    return None


def retry_after_seconds(error: object) -> float | None:
    """The wait the service asked for (``retry-after``, in seconds), if any."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None  # An HTTP-date: fall back to our own backoff.


def is_unavailable(failure: DependencyFailure | None) -> bool:
    """Whether ``failure`` means the dependency is unavailable (resumable), not refused.

    Unavailable: a temporary failure that ran out of retries, or a call refused by an
    open circuit (``error_type == "circuit_open"``). Anything else is permanent and
    keeps the agent's own end reason (FR-205), even when that failure is the one that
    opened the circuit.
    """
    if failure is None:
        return False
    return failure["kind"] == "temporary" or failure["error_type"] == CIRCUIT_OPEN
