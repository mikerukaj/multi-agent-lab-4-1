"""Runs a workspace Python file in a throwaway, locked-down container (contracts/tools.md).

Every run gets a fresh container that has no network, no secrets, a read-only
filesystem apart from the run's own workspace, no Linux capabilities, and
memory, CPU and process limits. Generated code never runs on the host.

Every container is labelled with its run ID and owner (host and process), so a
run's containers can be killed on a stop signal and those left by dead runs
removed at start-up (FR-211). With a guard, a container that Docker itself fails
to start is retried; the generated code's own exit codes and timeouts never are
(FR-207).
"""

import os
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from orchestration.checkpoint import is_locked, run_dir
from resilience.circuit import DependencyGuard, GuardFailure
from resilience.classify import classify
from tools.file_tools import resolve_in_workspace

SANDBOX_BASE_IMAGE = (
    "python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
)
PYTEST_VERSION = "9.1.1"
SANDBOX_IMAGE = f"multi-agent-sandbox:py3.12-pytest{PYTEST_VERSION}"
"""The image every code and test run uses: the pinned base plus pytest, built locally."""
SANDBOX_DIR = Path(__file__).resolve().parents[1] / "sandbox"
BUILD_TIMEOUT_SECONDS = 600
SANDBOX_WORKDIR = "/work"
SANDBOX_LIMITS = [
    "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "128"
]
OUTPUT_LIMIT_CHARS = 10_000
TRUNCATION_MARKER = "\n[truncated]"
RUN_ID_LABEL = "multi-agent-lab.run_id"
OWNER_LABEL = "multi-agent-lab.owner"

EXEC_PYTHON_TOOL = {
    "name": "exec_python",
    "description": (
        "Run a Python file from the task workspace in a fresh sandbox and return its "
        "exit code, stdout and stderr. The sandbox has a time limit, no stdin, no "
        "network, and no access to secrets or to files outside the workspace."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative .py file to run"}
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


class SandboxUnavailableError(RuntimeError):
    """Raised when Docker, which every code run depends on, cannot be used.

    ``failure`` is the structured ``DependencyFailure`` when a guarded run gave up.
    """

    def __init__(self, message: str, failure: dict | None = None) -> None:
        super().__init__(message)
        self.failure = failure


@dataclass
class ExecutionResult:
    """Outcome of one run of a workspace Python file."""

    path: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    failure: dict | None = None
    """The ``DependencyFailure`` when the sandbox could not be used at all."""

    def to_dict(self) -> dict:
        """Return a JSON-serialisable copy of this result (``failure`` only when set)."""
        data = asdict(self)
        if data["failure"] is None:
            del data["failure"]
        return data


def ensure_sandbox(image: str = SANDBOX_IMAGE) -> None:
    """Check that Docker works and the sandbox image is present.

    The project's own image (``SANDBOX_IMAGE``) is built from ``sandbox/Dockerfile``
    when missing; any other image is pulled.

    Raises:
        SandboxUnavailableError: if Docker is missing or the image cannot be built
            or pulled.
    """
    if shutil.which("docker") is None:
        raise SandboxUnavailableError(
            "Docker is required: generated code only ever runs in a sandbox container."
        )
    inspect = _docker(["image", "inspect", image])
    if inspect.returncode == 0:
        return
    if image == SANDBOX_IMAGE:
        _build_sandbox_image()
        return
    pull = _docker(["pull", "--quiet", image], timeout=BUILD_TIMEOUT_SECONDS)
    if pull.returncode != 0:
        raise SandboxUnavailableError(
            f"Could not pull sandbox image {image}: {pull.stderr.strip()}"
        )


def _build_sandbox_image() -> None:
    """Build ``SANDBOX_IMAGE``: the only step with network access; it runs no generated code."""
    build = _docker(
        [
            "build", "--quiet",
            "--build-arg", f"BASE_IMAGE={SANDBOX_BASE_IMAGE}",
            "--build-arg", f"PYTEST_VERSION={PYTEST_VERSION}",
            "--tag", SANDBOX_IMAGE,
            str(SANDBOX_DIR),
        ],
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if build.returncode != 0:
        raise SandboxUnavailableError(
            f"Could not build sandbox image {SANDBOX_IMAGE}: {build.stderr.strip()}"
        )


def exec_python(
    workspace: Path,
    path: str,
    timeout_seconds: float,
    image: str = SANDBOX_IMAGE,
    guard: DependencyGuard | None = None,
    run_id: str = "",
) -> ExecutionResult:
    """Run ``path`` in a new sandbox container that sees only ``workspace``.

    A run that exceeds ``timeout_seconds`` (container start-up included) is
    killed and reported with ``timed_out=True``. With a ``guard``, a container
    Docker fails to start is retried; if the guard gives up, the result has
    ``exit_code=None`` and ``failure`` set.

    Raises:
        ValueError: if ``path`` is outside the workspace or not a ``.py`` file.
        FileNotFoundError: if ``path`` does not exist.
    """
    target = _python_file(workspace, path)

    def attempt() -> subprocess.CompletedProcess:
        name = f"coder-exec-{secrets.token_hex(6)}"
        command = sandbox_command(workspace, target, name, image, run_id)
        return run_container(command, name, timeout_seconds)

    started = time.monotonic()
    outcome = guarded_run(attempt, guard)
    duration = round(time.monotonic() - started, 3)
    if isinstance(outcome, GuardFailure):
        return ExecutionResult(path, None, "", "", False, duration, failure=outcome.failure)
    return ExecutionResult(
        path=path,
        exit_code=outcome.returncode,
        stdout=cap_output(outcome.stdout),
        stderr=cap_output(outcome.stderr),
        timed_out=outcome.returncode is None,
        duration_seconds=duration,
    )


def _python_file(workspace: Path, path: str) -> Path:
    """The workspace file ``path`` names, checked to be an existing ``.py`` file.

    Raises:
        ValueError: if ``path`` is outside the workspace or not a ``.py`` file.
        FileNotFoundError: if ``path`` does not exist.
    """
    target = resolve_in_workspace(workspace, path)
    if target.suffix != ".py":
        raise ValueError(f"Not a Python file: {path}")
    if not target.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    return target


def run_container(command: list[str], name: str, timeout_seconds: float):
    """Run one ``docker run`` command; a run past its time limit is killed.

    Returns a ``CompletedProcess`` with the raw output bytes, and ``returncode``
    ``None`` when the run timed out.
    """
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        kill_container(name)
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(command, None, stdout, stderr)


def guarded_run(attempt, guard: DependencyGuard | None):
    """Run ``attempt`` through ``guard`` (when given), retrying Docker start failures."""
    if guard is None:
        return attempt()
    return guard.call(attempt, classify_result=lambda outcome: classify(outcome, "sandbox"))


def run_labels(run_id: str) -> list[str]:
    """The ``--label`` flags that tie a container to its run and owner (research R14)."""
    owner = f"{socket.gethostname()}:{os.getpid()}"
    return ["--label", f"{RUN_ID_LABEL}={run_id}", "--label", f"{OWNER_LABEL}={owner}"]


def locked_down_run(
    name: str, workspace: Path, extra_volumes: list[str] = (), run_id: str = ""
) -> list[str]:
    """Return the ``docker run`` prefix shared by every sandbox run (code and tests).

    No network, a read-only root, no capabilities, resource limits, the host user,
    the workspace as the only read-write mount (plus ``extra_volumes``), and no
    environment beyond three harmless variables. The container is labelled with
    ``run_id`` and its owner. The caller appends image and command.
    """
    volumes = [f"{workspace.resolve()}:{SANDBOX_WORKDIR}:rw", *extra_volumes]
    return [
        "docker", "run", "--rm", "--name", name, *run_labels(run_id),
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        *SANDBOX_LIMITS,
        "--user", f"{os.getuid()}:{os.getgid()}",
        *[flag for volume in volumes for flag in ("--volume", volume)],
        "--workdir", SANDBOX_WORKDIR,
        "--env", "HOME=/tmp",
        "--env", "PYTHONIOENCODING=utf-8",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
    ]


def sandbox_command(
    workspace: Path, target: Path, name: str, image: str, run_id: str = ""
) -> list[str]:
    """Build the ``docker run`` command for one locked-down, throwaway run.

    When QA's ``tests/`` folder exists it is mounted read-only over the workspace,
    so code the Coder runs cannot change the tests (FR-111, research R5).
    """
    script = target.relative_to(workspace.resolve()).as_posix()
    tests = workspace.resolve() / "tests"
    extra = [f"{tests}:{SANDBOX_WORKDIR}/tests:ro"] if tests.is_dir() else []
    return [*locked_down_run(name, workspace, extra, run_id), image, "python", "-s", script]


def kill_container(name: str) -> None:
    """Kill a running sandbox container by name (used when a run passes its time limit)."""
    _docker(["kill", name])


def kill_run_containers(run_id: str) -> list[str]:
    """Kill every running sandbox container labelled with ``run_id``; return their IDs.

    Used when the run is asked to stop (FR-210). Without Docker there is nothing to kill.
    """
    if not run_id or shutil.which("docker") is None:
        return []
    listed = _docker(["ps", "-q", "--filter", f"label={RUN_ID_LABEL}={run_id}"])
    ids = listed.stdout.split()
    if ids:
        _docker(["kill", "--signal", "KILL", *ids])
    return ids


def cleanup_stale_containers(workspace_root: Path) -> list[str]:
    """Remove labelled sandbox containers whose run is no longer going; return their IDs.

    A run is going while its lock is held, so containers of concurrent runs on this
    host are kept (FR-211, FR-230b). Containers without a run ID (the standalone Coder
    CLI) are left alone. Without Docker there is nothing to clean up.
    """
    if shutil.which("docker") is None:
        return []
    template = "{{.ID}} {{.Label \"" + RUN_ID_LABEL + "\"}}"
    listed = _docker(["ps", "-a", "--filter", f"label={RUN_ID_LABEL}", "--format", template])
    stale = []
    for line in listed.stdout.splitlines():
        container_id, _, run_id = line.partition(" ")
        if run_id and not is_locked(run_dir(workspace_root, run_id)):
            stale.append(container_id)
    if stale:
        _docker(["rm", "--force", *stale])
    return stale


def _docker(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    """Run one ``docker`` CLI command and capture its output."""
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def cap_output(output: bytes) -> str:
    """Decode process output and cut it to the per-stream limit."""
    text = output.decode("utf-8", errors="replace")
    if len(text) <= OUTPUT_LIMIT_CHARS:
        return text
    return text[:OUTPUT_LIMIT_CHARS] + TRUNCATION_MARKER
