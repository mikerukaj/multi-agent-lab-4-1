"""A test-only stand-in for the Docker sandbox that runs a test's own fixture scripts.

Unit tests of the Coder's loop script tiny programs (``print('hi')``) and check their
real output. This runs those fixture scripts with the local interpreter (isolated mode,
no stdin, a time limit), so the unit tests need no Docker. It is never used for
generated code: production always runs code in the sandbox container, and the Docker
versions of these tests live in tests/integration/test_coder_sandbox.py. The file name
does not match ``test*.py``.
"""

import subprocess
import sys
import time
from pathlib import Path

from tools.file_tools import resolve_in_workspace
from tools.python_exec import ExecutionResult


def local_exec_python(workspace: Path, path: str, timeout_seconds: float, image=None,
                      guard=None, run_id: str = "") -> ExecutionResult:
    """Run a fixture script the way ``exec_python`` reports it.

    Raises:
        ValueError: if ``path`` is outside the workspace or not a ``.py`` file.
        FileNotFoundError: if ``path`` does not exist.
    """
    target = resolve_in_workspace(Path(workspace), path)
    if target.suffix != ".py":
        raise ValueError(f"Not a Python file: {path}")
    if not target.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    started = time.monotonic()
    try:
        done = subprocess.run([sys.executable, "-I", str(target)], cwd=workspace,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout_seconds, check=False)
        exit_code, stdout, stderr, timed_out = done.returncode, done.stdout, done.stderr, False
    except subprocess.TimeoutExpired:
        exit_code, stdout, stderr, timed_out = None, "", "", True
    return ExecutionResult(path, exit_code, stdout, stderr, timed_out,
                           round(time.monotonic() - started, 3))
