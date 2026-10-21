"""Offline stand-ins for the PM's Anthropic client, the agents and their tools.

Shared by the team tests. The file name does not match ``test*.py``, so
``unittest discover`` never runs it as a test module.
"""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

from agents.coder_agent import CoderResult, ReflectionResult
from resilience.circuit import GuardFailure
from tools.file_tools import READ_FILE_TOOL, WRITE_FILE_TOOL
from tools.mcp_server import call_tool

PRIORITY_CYCLE = ("high", "medium", "low")


def token_usage(
    input_tokens: int, output_tokens: int, cache_creation: int = 0, cache_read: int = 0
) -> dict:
    """A usage dict with all four token counters, as the run state now carries them."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }


def pm_response(
    payload: dict | None = None, stop_reason: str = "end_turn", stop_details=None
) -> SimpleNamespace:
    """A model response whose text is ``payload`` as JSON (no content if ``None``)."""
    content = [] if payload is None else [SimpleNamespace(type="text", text=json.dumps(payload))]
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


def task_item(number: int, criteria_prefix: str = "Task") -> dict:
    """A model-facing task (no ``task_id``) with unique acceptance criteria."""
    criteria_count = 1 if number % 2 else 2
    return {
        "title": f"Task {number}",
        "description": f"Build part {number} of the program.",
        "acceptance_criteria": [
            f"{criteria_prefix} {number} criterion {j}" for j in range(1, criteria_count + 1)
        ],
        "priority": PRIORITY_CYCLE[(number - 1) % 3],
    }


def spec_payload(task_count: int, rejection_reason: str = "") -> dict:
    """A model-facing spec as in contracts/pm-agent-api.md ``SPEC_SCHEMA``."""
    tasks = [] if rejection_reason else [task_item(n) for n in range(1, task_count + 1)]
    return {
        "name": "Temperature converter",
        "description": "Converts temperatures between Celsius and Fahrenheit.",
        "acceptance_criteria": ["100 C converts to 212 F", "32 F converts to 0 C"],
        "assumptions": ["Input is a number followed by C or F"],
        "tasks": tasks,
        "rejection_reason": rejection_reason,
    }


def tasks_payload(task_count: int) -> dict:
    """A consolidation reply with fresh criteria (for cases where criteria do not matter)."""
    return {"tasks": [task_item(n, "Other") for n in range(1, task_count + 1)]}


def regroup_payload(tasks: list[dict], group_count: int) -> dict:
    """A well-behaved consolidation: contiguous groups, criteria copied in order."""
    size, extra = divmod(len(tasks), group_count)
    merged, start = [], 0
    for index in range(group_count):
        group = tasks[start : start + size + (1 if index < extra else 0)]
        start += len(group)
        merged.append(
            {
                "title": " + ".join(task["title"] for task in group),
                "description": " ".join(task["description"] for task in group),
                "acceptance_criteria": [c for task in group for c in task["acceptance_criteria"]],
                "priority": group[0]["priority"],
            }
        )
    return {"tasks": merged}


class FakePMClient:
    """Stands in for ``anthropic.Anthropic``: replays scripted responses or errors."""

    def __init__(self, script: list) -> None:
        self._script = iter(script)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs)))
        item = next(self._script)
        if isinstance(item, Exception):
            raise item
        return item


class FakeCoder:
    """Stands in for ``CoderAgent``: builds tasks, reflects and fixes, all scripted.

    A build task writes ``<task id>.py``. A fix round (any task that is not a build
    task) takes the next status from ``fix_statuses`` (default ``completed``) and
    answers every ``answer_ids`` entry as fixed. ``reflect`` takes the next status
    from ``reflect_statuses``. ``call_log`` (shared with ``FakeQA``) records the order.
    """

    def __init__(
        self,
        fail_on: frozenset[str] = frozenset(),
        failed_status: str = "step_limit",
        delay_seconds: float = 0.0,
        fix_statuses: tuple[str, ...] = (),
        reflect_statuses: tuple[str, ...] = (),
        call_log: list | None = None,
    ) -> None:
        self.fail_on = fail_on
        self.failed_status = failed_status
        self.delay_seconds = delay_seconds
        self.fix_statuses = list(fix_statuses)
        self.reflect_statuses = list(reflect_statuses)
        self.call_log = call_log
        self.calls: list[tuple[str, str, Path]] = []
        self.fix_calls: list[tuple[str, Path, list[str]]] = []
        self.reflect_calls: list[tuple[str, Path]] = []

    def run(self, task: str, workspace: Path, answer_ids=()) -> CoderResult:
        """Record the call; build a task, or play a scripted fix round."""
        match = re.search(r"Task (T\d+) of", task)
        if match is None:
            return self._fix(task, workspace, list(answer_ids))
        task_id = match.group(1)
        self.calls.append((task_id, task, workspace))
        time.sleep(self.delay_seconds)
        file_name = f"{task_id.lower()}.py"
        code = f"print('{task_id}')\n"
        (workspace / file_name).write_text(code, encoding="utf-8")
        failed = task_id in self.fail_on
        return CoderResult(
            code=code,
            explanation=f"Built {task_id}",
            plan=[f"Write {task_id}"],
            execution_result=None,
            status=self.failed_status if failed else "completed",
            files_written=[file_name],
            steps_used=2,
            duration_seconds=0.5,
            usage={"input_tokens": 10, "output_tokens": 5},
            workspace=str(workspace),
            error="Step limit reached" if failed else None,
        )


    def _fix(self, brief: str, workspace: Path, answer_ids: list[str]) -> CoderResult:
        self.fix_calls.append((brief, workspace, answer_ids))
        iteration = int(re.search(r"QA review #(\d+)", brief).group(1))
        if self.call_log is not None:
            self.call_log.append(("fix", iteration))
        status = self.fix_statuses.pop(0) if self.fix_statuses else "completed"
        failed = status in ("refused", "error")
        return CoderResult(
            code="", explanation=f"Fix round {iteration}", plan=["Fix"],
            execution_result=None, status=status, files_written=["t1.py"], steps_used=3,
            usage={"input_tokens": 10, "output_tokens": 5}, workspace=str(workspace),
            error="The model declined the request." if failed else None,
            finding_responses=[
                {"finding_id": finding_id, "action": "fixed", "reason": ""}
                for finding_id in answer_ids
            ],
        )

    def reflect(self, brief: str, workspace: Path) -> ReflectionResult:
        self.reflect_calls.append((brief, workspace))
        if self.call_log is not None:
            self.call_log.append(("reflect", len(self.reflect_calls)))
        status = self.reflect_statuses.pop(0) if self.reflect_statuses else "completed"
        failed = status in ("refused", "error")
        result = CoderResult(
            code="", explanation="Reflected.", plan=[], execution_result=None, status=status,
            workspace=str(workspace), error="Critique call failed" if failed else None,
        )
        rounds = [] if failed else [{
            "before_iteration": 0, "round": 1,
            "critique": [{"item_id": "C1", "kind": "edge_case", "location": "main()",
                          "description": "Empty input.", "suggested_change": "Check it."}],
            "revision_status": status, "files_changed": [],
            "responses": [{"finding_id": "C1", "action": "fixed", "reason": ""}],
            "summary": "Reflected.",
        }]
        usage = {"input_tokens": 20, "output_tokens": 10}
        return ReflectionResult(rounds=rounds, result=result, status=status, usage=usage)


class FakeFileTools:
    """In-process stand-in for ``McpFileTools``, with the same rules and a call log.

    Calls go through ``tools.mcp_server.call_tool``, the server's own handler logic,
    so results and error texts match the real server without starting a process.
    Every session opened is kept in ``FakeFileTools.sessions``; call ``reset()``
    in ``setUp``.
    """

    sessions: list["FakeFileTools"] = []

    def __init__(
        self, workspace: Path, protect: tuple[str, ...] = (), call_timeout_seconds: float = 30.0,
        guard=None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.protect = tuple(protect)
        self.guard = guard
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    @classmethod
    def reset(cls) -> None:
        cls.sessions = []

    def __enter__(self) -> "FakeFileTools":
        FakeFileTools.sessions.append(self)
        return self

    def __exit__(self, *exc) -> None:
        self.closed = True

    def definitions(self) -> list[dict]:
        return [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["input_schema"],
                "strict": True,
            }
            for tool in (READ_FILE_TOOL, WRITE_FILE_TOOL)
        ]

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Serve one call; with a guard, an open ``mcp`` circuit fails it as the real
        client does (a ``DependencyFailure`` JSON body with ``is_error`` set)."""
        self.calls.append((name, dict(arguments)))
        if self.guard is None:
            return call_tool(self.workspace, self.protect, name, arguments)
        outcome = self.guard.call(lambda: call_tool(self.workspace, self.protect, name, arguments))
        if isinstance(outcome, GuardFailure):
            return json.dumps(outcome.failure), True
        return outcome


def scripted_test_run(failed: int = 0, loaded: bool = True, timed_out: bool = False) -> dict:
    """A ``TestRun`` dict with one passing test and ``failed`` failing ones."""
    cases = [
        {"test_id": "tests/test_app.py::test_ok", "file": "tests/test_app.py",
         "outcome": "passed", "message": "", "details": ""}
    ] + [
        {"test_id": f"tests/test_app.py::test_bad_{n}", "file": "tests/test_app.py",
         "outcome": "failed", "message": "assert 1 == 2", "details": "Traceback ..."}
        for n in range(1, failed + 1)
    ]
    return {
        "loaded": loaded, "exit_code": None if timed_out else int(failed > 0),
        "timed_out": timed_out, "passed": 1, "failed": failed, "errors": 0, "skipped": 0,
        "cases": cases, "output": f"{failed} failed, 1 passed", "duration_seconds": 1.0,
    }


def scripted_finding(finding_id: str, severity: str = "blocking", kind: str = "bug") -> dict:
    """A ``QAFinding`` dict."""
    return {
        "finding_id": finding_id, "kind": kind, "severity": severity, "rule": None,
        "file": "app.py", "location": "main()", "problem": "Wrong output.",
        "expected": "The output the spec names.", "likely_cause": "Off by one.",
        "fix_instruction": "Fix the off-by-one in main().",
        "test_ids": ["tests/test_app.py::test_bad_1"] if kind == "failing_test" else [],
        "state": "open", "first_seen": 1,
    }


@dataclass
class ScriptedQAResult:
    """Stands in for ``agents.qa_agent.QAIterationResult`` (same fields, no import)."""

    test_run: dict
    findings: list = field(default_factory=list)
    disputes: list = field(default_factory=list)
    test_changes: list = field(default_factory=list)
    test_suite: dict = field(default_factory=dict)
    verdict: str = "complete"
    usage: dict = field(default_factory=lambda: {"input_tokens": 100, "output_tokens": 50})
    error: str | None = None

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self)))


def qa_result(
    verdict: str,
    findings=(),
    test_run: dict | None = None,
    test_suite: dict | None = None,
    error: str | None = None,
    usage: dict | None = None,
) -> ScriptedQAResult:
    """A scripted QA iteration result; ``fix_needed`` defaults to one failing test."""
    if findings == () and verdict == "fix_needed":
        findings = [scripted_finding("F1-1")]
    return ScriptedQAResult(
        test_run=test_run or scripted_test_run(failed=0 if verdict == "complete" else 1),
        findings=list(findings),
        test_suite=test_suite if test_suite is not None else {"tests/test_app.py": "pass\n"},
        verdict=verdict,
        usage=usage or {"input_tokens": 100, "output_tokens": 50},
        error=error,
    )


class FakeQA:
    """Stands in for ``QAAgent`` behind the A2A endpoint; never imports ``agents.qa_agent``.

    ``results`` is a list of ``ScriptedQAResult``s or exceptions, consumed one per
    ``run_iteration`` call.
    """

    def __init__(
        self, results: list, call_log: list | None = None, report_error: Exception | None = None
    ) -> None:
        self._results = list(results)
        self.call_log = call_log
        self.report_error = report_error
        self.contexts: list[dict] = []
        self.report_requests: list[dict] = []

    def run_iteration(self, context: dict) -> ScriptedQAResult:
        json.dumps(context)  # The context must be plain JSON.
        self.contexts.append(context)
        if self.call_log is not None:
            self.call_log.append(("qa", context["iteration"]))
        if not self._results:
            raise AssertionError("FakeQA script exhausted")
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def final_report(self, request: dict) -> dict:
        self.report_requests.append(request)
        if self.report_error is not None:
            raise self.report_error
        last = request["iterations"][-1]
        status = request["status"] if request["status"] in ("completed", "failed") else "error"
        unresolved = [
            f for f in last["findings"]
            if f["severity"] == "blocking" and f["state"] in ("open", "disputed")
        ]
        return {
            "status": status,
            "end_reason": request["end_reason"],
            "iterations_run": len(request["iterations"]),
            "max_iterations": request["max_iterations"],
            "final_passed": last["test_run"]["passed"],
            "final_failed": last["test_run"]["failed"] + last["test_run"]["errors"],
            "unresolved": unresolved,
            "advisory": [f for f in last["findings"] if f["severity"] == "advisory"],
            "per_iteration": [
                f"#{run['number']}: {run['test_run']['passed']} passed, "
                f"{run['test_run']['failed']} failed"
                for run in request["iterations"]
            ],
            "report_path": ".qa/report.md",
        }

