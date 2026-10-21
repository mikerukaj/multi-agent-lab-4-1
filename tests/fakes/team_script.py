"""Scripted, content-aware model fakes and a builder for a real-agent team without Docker.

``TeamModel`` answers each request by what it asks for (the PM's spec or consolidation,
a Coder loop turn, summary or critique, QA's tests or review), so a full run works in
any order, survives retries, and can be resumed. Failures are injected only here (FR-239):
``fail_when(request)`` returns an exception to raise for a request, or ``None``.

``scripted_team`` builds the real ``PMAgent``, ``CoderAgent`` and ``QAAgent`` on these
models, with ``FakeFileTools`` for MCP, a fake sandbox and a fake memory. The file name
does not match ``test*.py``.
"""

import contextlib
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from agents import coder_agent
from agents.coder_agent import CRITIQUE_FORMAT, SUMMARY_FORMAT, CoderAgent, CoderConfig
from agents.pm_agent import CONSOLIDATION_SCHEMA, SPEC_SCHEMA, PMAgent, PMConfig
from agents.qa_agent import REVIEW_FORMAT, WRITE_TESTS_FORMAT, QAAgent, QAConfig
from resilience.circuit import GuardFailure
from tests.fakes import scripted_claude as sc
from tests.team_fakes import FakeFileTools, scripted_test_run
from tools.python_exec import ExecutionResult, SandboxUnavailableError

SPEC_CRITERION = "the program prints its result"
REQUIREMENT = "Write a command-line tool that prints a greeting."


def task_criterion(number: int) -> str:
    return f"task {number} works"


def spec_payload(task_count: int) -> dict:
    """A PM spec with ``task_count`` tasks, each with one criterion."""
    return {
        "name": "Greeter", "description": "Prints a greeting.",
        "acceptance_criteria": [SPEC_CRITERION], "assumptions": ["Plain text output."],
        "tasks": [
            {"title": f"Build part {n}", "description": f"Part {n} of the greeter.",
             "acceptance_criteria": [task_criterion(n)], "priority": "high"}
            for n in range(1, task_count + 1)
        ],
        "rejection_reason": "",
    }


def consolidation_payload(task_count: int, groups: int = 2) -> dict:
    """Merge ``task_count`` tasks into ``groups`` tasks, keeping every criterion in order."""
    numbers = list(range(1, task_count + 1))
    size = -(-task_count // groups)
    return {"tasks": [
        {"title": f"Group {index + 1}", "description": "Merged work.", "priority": "high",
         "acceptance_criteria": [task_criterion(n) for n in numbers[start:start + size]]}
        for index, start in enumerate(range(0, task_count, size))
    ]}


def _schema(request: dict):
    output_format = request.get("output_config", {}).get("format") or {}
    return output_format.get("schema", output_format) if output_format else None


def request_kind(request: dict) -> str:
    """Name what a request asks for: spec, consolidate, critique, summary, loop, tests,
    disputes or review."""
    schema = _schema(request)
    output_format = request.get("output_config", {}).get("format")
    if schema == SPEC_SCHEMA:
        return "spec"
    if schema == CONSOLIDATION_SCHEMA:
        return "consolidate"
    if output_format == CRITIQUE_FORMAT:
        return "critique"
    if output_format == SUMMARY_FORMAT:
        return "summary"
    if schema == WRITE_TESTS_FORMAT:
        return "tests"
    if schema == REVIEW_FORMAT:
        return "review"
    if schema is not None:
        return "disputes"
    return "loop"


def first_user_text(request: dict) -> str:
    content = request["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))


class TeamModel:
    """A ``beta.messages.create`` stand-in that answers by request kind; records requests.

    ``reviews`` scripts QA's reviews in order: each item is a list of raw findings (the
    last item repeats). With ``bug_in_first_review``, the first review of a run (one with
    no earlier findings in its prompt) reports a bug and later ones none, whichever
    model instance answers, so resumed runs follow the same script.
    ``fail_when(request)`` may return an exception to raise.
    """

    def __init__(
        self,
        task_count: int = 2,
        reviews: list[list[dict]] | None = None,
        fail_when: Callable[[dict], BaseException | None] | None = None,
        on_request: Callable[[dict], None] | None = None,
        bug_in_first_review: bool = False,
    ) -> None:
        self.task_count = task_count
        self.reviews = list(reviews or [[]])
        self.bug_in_first_review = bug_in_first_review
        self.fail_when = fail_when
        self.on_request = on_request
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self.beta = sc._Beta(self)
        self.max_retries = 0

    def next_reply(self, request: dict):
        with self._lock:
            self.requests.append(request)
        if self.on_request:
            self.on_request(request)
        failure = self.fail_when(request) if self.fail_when else None
        if failure is not None:
            raise failure
        return getattr(self, f"_{request_kind(request)}")(request)

    def _spec(self, request):
        return sc.text_reply(spec_payload(self.task_count), model=request["model"])

    def _consolidate(self, request):
        return sc.text_reply(consolidation_payload(self.task_count), model=request["model"])

    def _loop(self, request):
        last = request["messages"][-1]
        if last["role"] == "user" and isinstance(last["content"], list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in last["content"]
        ):
            return sc.text_reply("Done.", model=request["model"])
        task = re.search(r"Task (T\d+)", first_user_text(request))
        content = f"print('hello from {task.group(1) if task else 'fix'}')\n"
        return sc.tool_use_reply(
            [("write_file", {"path": "main.py", "content": content, "purpose": "Write it."})],
            thought="Writing main.py.", model=request["model"],
        )

    def _summary(self, request):
        ids = re.findall(r"\[([CF][\d-]+)\]", first_user_text(request))
        responses = [{"finding_id": item, "action": "fixed", "reason": ""}
                     for item in dict.fromkeys(ids)]
        return sc.text_reply({"plan": ["Write main.py"], "explanation": "Wrote main.py.",
                              "main_file": "main.py", "finding_responses": responses},
                             model=request["model"])

    def _critique(self, request):
        item = {"kind": "edge_case", "location": "main.py", "description": "No newline.",
                "suggested_change": "Keep print."}
        return sc.text_reply({"items": [item], "overall": "Fine."}, model=request["model"])

    def _tests(self, request):
        test_file = {"path": "tests/test_main.py", "content": "def test_main():\n    pass\n",
                     "criteria": [SPEC_CRITERION]}
        return sc.text_reply({"test_files": [test_file], "notes": ""}, model=request["model"])

    def _review(self, request):
        if self.bug_in_first_review:
            first = "Findings from the last review" not in first_user_text(request)
            findings = [bug_finding()] if first else []
            return sc.text_reply({"findings": findings, "regression_tests": [],
                                  "summary": "ok"}, model=request["model"])
        with self._lock:
            findings = self.reviews.pop(0) if len(self.reviews) > 1 else self.reviews[0]
        return sc.text_reply({"findings": findings, "regression_tests": [], "summary": "ok"},
                             model=request["model"])

    def _disputes(self, request):
        return sc.text_reply({"decisions": [], "corrected_files": []}, model=request["model"])


def bug_finding() -> dict:
    """One blocking bug finding, as QA's review reports it."""
    return {"kind": "bug", "rule": "", "file": "main.py", "location": "main",
            "problem": "Wrong greeting.", "expected": SPEC_CRITERION,
            "likely_cause": "Typo.", "fix_instruction": "Fix the greeting.",
            "test_ids": [], "same_as": ""}


class FakeMemory:
    """An in-memory stand-in for ``SemanticMemory`` that honours a guard."""

    def __init__(
        self, path: Path, collection: str = "coder_memory", guard=None, host=None, port=8000
    ) -> None:
        self.guard = guard
        self.records: list[str] = []

    def add(self, run_id: str, kind: str, text: str) -> str:
        outcome = self.guard.call(lambda: self.records.append(text)) if self.guard else None
        return "" if isinstance(outcome, GuardFailure) else f"{run_id}-{len(self.records)}"

    def query(self, text: str, top_k: int, exclude_run_id: str) -> list:
        outcome = self.guard.call(lambda: []) if self.guard else []
        return [] if isinstance(outcome, GuardFailure) else outcome


def fake_exec_python(workspace, path, timeout_seconds, image=None, guard=None, run_id=""):
    """The sandbox, replaced: honours an open ``sandbox`` circuit like the real one."""
    def run() -> ExecutionResult:
        return ExecutionResult(path, 0, "hello\n", "", False, 0.01)

    outcome = guard.call(run) if guard else run()
    if isinstance(outcome, GuardFailure):
        return ExecutionResult(path, None, "", "", False, 0.0, failure=outcome.failure)
    return outcome


def fake_run_tests(workspace, timeout_seconds, image, guard=None, run_id=""):
    """QA's pytest run, replaced: a passing run, or the sandbox failure under an open circuit."""
    outcome = guard.call(lambda: scripted_test_run()) if guard else scripted_test_run()
    if isinstance(outcome, GuardFailure):
        raise SandboxUnavailableError("Sandbox unavailable for the test run", outcome.failure)
    return outcome


@contextlib.contextmanager
def offline_coder():
    """Patch the Coder's module so it needs no Docker and no Chroma."""
    with mock.patch.object(coder_agent, "ensure_sandbox", lambda image: None), \
            mock.patch.object(coder_agent, "SemanticMemory", FakeMemory), \
            mock.patch.object(coder_agent, "exec_python", fake_exec_python), \
            mock.patch("tools.exec_python", fake_exec_python):
        yield


def scripted_team(
    root: Path, services, pm_model: TeamModel, coder_model: TeamModel, qa_model: TeamModel
) -> tuple[PMAgent, CoderAgent, QAAgent]:
    """Real agents on the given models, with every outside dependency faked.

    Call it inside ``offline_coder()``.
    """
    FakeFileTools.reset()
    pm = PMAgent(PMConfig(), client=pm_model, services=services)
    coder = CoderAgent(
        CoderConfig(workspace_root=root, chroma_path=root / "chroma", max_steps=4),
        client=coder_model, file_tools=FakeFileTools, services=services,
    )
    qa = QAAgent(QAConfig(), client=qa_model, run_tests=fake_run_tests,
                 file_tools=FakeFileTools, services=services)
    return pm, coder, qa


def all_requests(*models: TeamModel) -> int:
    """How many model requests the given models have received in total."""
    return sum(len(model.requests) for model in models)


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True)
