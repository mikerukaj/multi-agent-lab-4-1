"""Runs a workspace Python file in a throwaway, locked-down container (contracts/tools.md).

Every run gets a fresh container that has no network, no secrets, a read-only
filesystem apart from the run's own workspace, no Linux capabilities, and
memory, CPU and process limits. Generated code never runs on the host.
"""

import os
import secrets
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

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
    """Raised when Docker, which every code run depends on, cannot be used."""


@dataclass
class ExecutionResult:
    """Outcome of one run of a workspace Python file."""

    path: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float

    def to_dict(self) -> dict:
        """Return a JSON-serialisable copy of this result."""
        return asdict(self)


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
    workspace: Path, path: str, timeout_seconds: float, image: str = SANDBOX_IMAGE
) -> ExecutionResult:
    """Run ``path`` in a new sandbox container that sees only ``workspace``.

    A run that exceeds ``timeout_seconds`` (container start-up included) is
    killed and reported with ``timed_out=True``.

    Raises:
        ValueError: if ``path`` is outside the workspace or not a ``.py`` file.
        FileNotFoundError: if ``path`` does not exist.
    """
    target = resolve_in_workspace(workspace, path)
    if target.suffix != ".py":
        raise ValueError(f"Not a Python file: {path}")
    if not target.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    name = f"coder-exec-{secrets.token_hex(6)}"
    command = sandbox_command(workspace, target, name, image)
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        exit_code, timed_out = process.returncode, False
    except subprocess.TimeoutExpired:
        kill_container(name)
        stdout, stderr = process.communicate()
        exit_code, timed_out = None, True

    return ExecutionResult(
        path=path,
        exit_code=exit_code,
        stdout=cap_output(stdout),
        stderr=cap_output(stderr),
        timed_out=timed_out,
        duration_seconds=round(time.monotonic() - started, 3),
    )


def locked_down_run(name: str, workspace: Path, extra_volumes: list[str] = ()) -> list[str]:
    """Return the ``docker run`` prefix shared by every sandbox run (code and tests).

    No network, a read-only root, no capabilities, resource limits, the host user,
    the workspace as the only read-write mount (plus ``extra_volumes``), and no
    environment beyond three harmless variables. The caller appends image and command.
    """
    volumes = [f"{workspace.resolve()}:{SANDBOX_WORKDIR}:rw", *extra_volumes]
    return [
        "docker", "run", "--rm", "--name", name,
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


def sandbox_command(workspace: Path, target: Path, name: str, image: str) -> list[str]:
    """Build the ``docker run`` command for one locked-down, throwaway run.

    When QA's ``tests/`` folder exists it is mounted read-only over the workspace,
    so code the Coder runs cannot change the tests (FR-111, research R5).
    """
    script = target.relative_to(workspace.resolve()).as_posix()
    tests = workspace.resolve() / "tests"
    extra = [f"{tests}:{SANDBOX_WORKDIR}/tests:ro"] if tests.is_dir() else []
    return [*locked_down_run(name, workspace, extra), image, "python", "-s", script]


def kill_container(name: str) -> None:
    """Kill a running sandbox container by name (used when a run passes its time limit)."""
    _docker(["kill", name])


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
