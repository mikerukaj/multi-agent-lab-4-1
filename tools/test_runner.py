"""Runs QA's pytest suite in the locked-down sandbox and reads the JUnit XML results.

Each run gets a fresh container from the shared sandbox image with no network, a
read-only root and a read-only ``tests/`` mount; the JUnit report goes to a host
temporary folder mounted at ``/qa-out``, so nothing is written into the delivered
workspace. ``-c /dev/null`` and ``--confcutdir`` stop a ``pytest.ini``,
``pyproject.toml`` or root ``conftest.py`` in the workspace from changing how the
tests run. See specs/002-qa-feedback-loop-mcp/research.md R7.
"""

import os
import secrets
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from orchestration.state import TestCaseResult, TestRun
from resilience.circuit import DependencyGuard, GuardFailure
from tools.python_exec import (
    SANDBOX_IMAGE,
    SANDBOX_WORKDIR,
    SandboxUnavailableError,
    cap_output,
    guarded_run,
    locked_down_run,
    run_container,
)

MAX_XML_BYTES = 2 * 1024 * 1024
DETAILS_LIMIT = 2000
OUTPUT_TAIL_CHARS = 10_000
TRUNCATED = "[truncated]"
LOADED_EXIT_CODES = (0, 1)
OUT_DIR = "/qa-out"


def pytest_command(
    workspace: Path, out_dir: Path, name: str, image: str, run_id: str = ""
) -> list[str]:
    """Build the ``docker run`` command for one sandboxed pytest run."""
    volumes = [f"{out_dir.resolve()}:{OUT_DIR}:rw"]
    tests = workspace.resolve() / "tests"
    volumes.insert(0, f"{tests}:{SANDBOX_WORKDIR}/tests:ro")
    return [
        *locked_down_run(name, workspace, volumes, run_id),
        image,
        "python", "-s", "-m", "pytest", "tests", "-q",
        "-p", "no:cacheprovider",
        "-c", "/dev/null",
        f"--rootdir={SANDBOX_WORKDIR}",
        f"--confcutdir={SANDBOX_WORKDIR}/tests",
        f"--junitxml={OUT_DIR}/junit.xml",
        "-o", "junit_family=xunit2",
    ]


def run_pytest(
    workspace: Path,
    timeout_seconds: float,
    image: str = SANDBOX_IMAGE,
    guard: DependencyGuard | None = None,
    run_id: str = "",
) -> TestRun:
    """Run the workspace's ``tests/`` with pytest in a throwaway sandbox container.

    A run past ``timeout_seconds`` is killed and reported as ``timed_out`` (a
    loaded failure, not a load error). With a ``guard``, a container Docker fails to
    start is retried.

    Raises:
        SandboxUnavailableError: carrying the ``DependencyFailure``, if the guard gave up.
    """
    workspace = workspace.resolve()
    test_files = qa_test_files(workspace)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="qa-out-", dir=qa_output_root()) as out:
        out_dir = Path(out)

        def attempt() -> subprocess.CompletedProcess:
            name = f"qa-pytest-{secrets.token_hex(6)}"
            command = pytest_command(workspace, out_dir, name, image, run_id)
            return run_container(command, name, timeout_seconds)

        outcome = guarded_run(attempt, guard)
        if isinstance(outcome, GuardFailure):
            raise SandboxUnavailableError(
                f"Sandbox unavailable for the test run: {outcome.failure['message']}",
                outcome.failure,
            )
        cases, xml_ok = _read_report(out_dir / "junit.xml", test_files)
    output = (cap_output(outcome.stdout) + cap_output(outcome.stderr))[-OUTPUT_TAIL_CHARS:]
    duration = round(time.monotonic() - started, 3)
    exit_code, timed_out = outcome.returncode, outcome.returncode is None
    return summarise(cases, exit_code, timed_out, xml_ok, test_files, output, duration)


def qa_output_root() -> str | None:
    """Where QA's JUnit folder is created: ``QA_OUTPUT_ROOT``, or the system temp dir.

    Under Compose the agent asks the host engine to mount this folder into the sandbox,
    so it must be a host path (research R16).
    """
    root = os.environ.get("QA_OUTPUT_ROOT")
    if not root:
        return None
    Path(root).mkdir(parents=True, exist_ok=True)
    return root


def qa_test_files(workspace: Path) -> list[str]:
    """Return QA's test files, workspace-relative, in a stable order."""
    tests = workspace / "tests"
    if not tests.is_dir():
        return []
    return sorted(path.relative_to(workspace).as_posix() for path in tests.rglob("test_*.py"))


def summarise(
    cases: list[TestCaseResult],
    exit_code: int | None,
    timed_out: bool,
    xml_ok: bool,
    test_files: list[str],
    output: str,
    duration_seconds: float,
) -> TestRun:
    """Turn parsed cases and the process outcome into a ``TestRun`` (research R7)."""
    counts = {outcome: 0 for outcome in ("passed", "failed", "error", "skipped")}
    for case in cases:
        counts[case["outcome"]] += 1
    if timed_out:
        loaded = True
        counts["failed"] = max(counts["failed"], 0 if cases else 1)
    else:
        files_with_cases = {case["file"] for case in cases}
        loaded = (
            exit_code in LOADED_EXIT_CODES
            and xml_ok
            and bool(test_files)
            and set(test_files) <= files_with_cases
        )
    return TestRun(
        loaded=loaded,
        exit_code=exit_code,
        timed_out=timed_out,
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["error"],
        skipped=counts["skipped"],
        cases=cases,
        output=output,
        duration_seconds=duration_seconds,
    )


def parse_junit(xml_text: str, test_files: list[str]) -> list[TestCaseResult]:
    """Parse a pytest JUnit (xunit2) report into ``TestCaseResult``s.

    Raises:
        ElementTree.ParseError: if ``xml_text`` is not well-formed XML.
    """
    root = ElementTree.fromstring(xml_text)
    cases = []
    for element in root.iter("testcase"):
        file, test_id = _locate(element.get("classname", ""), element.get("name", ""), test_files)
        outcome, message, details = "passed", "", ""
        for tag, mapped in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            child = element.find(tag)
            if child is not None:
                outcome, message, details = mapped, child.get("message", ""), child.text or ""
                break
        cases.append(
            TestCaseResult(
                test_id=test_id, file=file, outcome=outcome,
                message=message, details=_cut(details, DETAILS_LIMIT),
            )
        )
    return cases


def _read_report(path: Path, test_files: list[str]) -> tuple[list[TestCaseResult], bool]:
    """Parse the JUnit file if it exists, is small enough, and is valid XML."""
    if not path.is_file() or path.stat().st_size > MAX_XML_BYTES:
        return [], False
    try:
        return parse_junit(path.read_text(encoding="utf-8", errors="replace"), test_files), True
    except ElementTree.ParseError:
        return [], False


def _locate(classname: str, name: str, test_files: list[str]) -> tuple[str, str]:
    """Map a JUnit ``classname`` to its test file and pytest node ID."""
    for file in sorted(test_files, key=len, reverse=True):
        module = file.removesuffix(".py").replace("/", ".")
        if classname == module or classname.startswith(module + "."):
            classes = classname[len(module):].lstrip(".")
            node = "::".join(part for part in (file, *classes.split("."), name) if part)
            return file, node
    file = classname.replace(".", "/") + ".py" if classname else ""
    return file, f"{file}::{name}" if file else name


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED)] + TRUNCATED
