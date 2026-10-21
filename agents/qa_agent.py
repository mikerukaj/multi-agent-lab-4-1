"""QA & Debugger Agent: writes and runs real pytest tests, reviews the code, reports findings.

One QA iteration is a fixed pipeline, not an open-ended agent loop (research R8):
write tests from the spec and the program's public interface only, run them in the
network-less sandbox, repair a suite that does not load, then review the full code
with the results and return a normalised findings list (the fix instruction list)
and a verdict worked out in code. Every file read and write goes through the MCP
file-tool server. The team reaches this agent only through its A2A endpoint, and this
module never imports the Coder (SC-110). See specs/002-qa-feedback-loop-mcp/.
"""

import ast
import json
import os
import shutil
import typing
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

import anthropic

from agents.llm import (
    EFFORT_LEVELS,
    MAX_TOKENS,
    SMALL_MODEL,
    check_model_settings,
    fallbacks_from_env,
    resolve_fallbacks,
    NOT_JSON,
    ModelCaller,
    add_usage,
    failure_message,
    stable_blocks,
    system_blocks,
    with_fallbacks,
    zero_usage,
)
from orchestration.services import RunServices
from orchestration.state import (
    ConstitutionRule,
    DependencyFailure,
    Dispute,
    FinalReportRequest,
    QAContext,
    QADisputeReply,
    QAFinding,
    QAReport,
    QAReviewReply,
    QATestFilesReply,
    TestChange,
    TestRun,
    TokenUsage,
)
from tools.mcp_client import McpFileTools, McpUnavailableError, failure_in
from tools.python_exec import SANDBOX_IMAGE, SandboxUnavailableError, ensure_sandbox
from orchestration.validation import first_problems
from tools.test_runner import run_pytest

MAX_REPAIR_ATTEMPTS = 3
REPLY_ATTEMPTS = 2
"""Tries per structured QA reply: the first answer, plus one corrected answer after a
rejection. A single paraphrased criterion or cut-off reply used to end the whole
iteration (baseline benchmark 2026-10-20: 6 of 10 runs)."""
CORRECTION_NOTE = """\
Your previous reply was rejected: {reason}
Reply again with the complete, corrected answer. Copy every criterion character for \
character from the specification above, put every test file under tests/ with a .py \
name, and keep the reply short enough to finish: write fewer or shorter test files if \
needed."""
REPORT_PATH = ".qa/report.md"
PARTIAL_END_REASONS = ("interrupted", "budget_exceeded", "dependency_unavailable")
"""Runs that stopped before QA could finish: their report says it is partial."""
TIMEOUT_TEST_ID = "<timeout>"
CONSTITUTION_RULES = typing.get_args(ConstitutionRule)

TEST_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
        "criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["path", "content", "criteria"],
    "additionalProperties": False,
}
WRITE_TESTS_FORMAT = {
    "type": "object",
    "properties": {
        "test_files": {"type": "array", "items": TEST_FILE_SCHEMA},
        "notes": {"type": "string"},
    },
    "required": ["test_files", "notes"],
    "additionalProperties": False,
}
DISPUTES_FORMAT = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["keep", "correct"]},
                    "reason": {"type": "string"},
                },
                "required": ["finding_id", "decision", "reason"],
                "additionalProperties": False,
            },
        },
        "corrected_files": {"type": "array", "items": TEST_FILE_SCHEMA},
    },
    "required": ["decisions", "corrected_files"],
    "additionalProperties": False,
}
REVIEW_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["failing_test", "bug", "style"]},
        "rule": {"type": "string", "enum": ["", *CONSTITUTION_RULES]},
        "file": {"type": "string"},
        "location": {"type": "string"},
        "problem": {"type": "string"},
        "expected": {"type": "string"},
        "likely_cause": {"type": "string"},
        "fix_instruction": {"type": "string"},
        "test_ids": {"type": "array", "items": {"type": "string"}},
        "same_as": {"type": "string"},
    },
    "required": [
        "kind", "rule", "file", "location", "problem", "expected",
        "likely_cause", "fix_instruction", "test_ids", "same_as",
    ],
    "additionalProperties": False,
}
REVIEW_FORMAT = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": REVIEW_FINDING_SCHEMA},
        "regression_tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_index": {"type": "integer"}, **TEST_FILE_SCHEMA["properties"]
                },
                "required": ["finding_index", "path", "content", "criteria"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["findings", "regression_tests", "summary"],
    "additionalProperties": False,
}

QA_SYSTEM_PROMPT = f"""\
You are the QA & Debugger Agent in an autonomous software team. The Coder Agent \
builds a Python program from a specification; you test it and review it.

Rules:
- You own the tests/ folder. Write pytest tests there: plain assert, pytest.raises, \
pytest.mark.parametrize, and capsys or monkeypatch for command-line behavior. Use only \
pytest and the Python standard library. Tests run from the workspace root, so import \
the program's modules by name, and run command-line programs through their main \
function or with subprocess and sys.executable.
- Expected values come only from the specification, never from what the code does. \
Name each test after the acceptance criterion it checks, and list that criterion, \
copied word for word, in the file's criteria.
- Report every failing test. Diagnose the likely cause from the code and the failure.
- Report code that special-cases the exact inputs or expected values of the tests, \
instead of doing what the specification asks, as a bug.
- Report a style problem with a rule only when it breaks one of these MUST rules of \
the project constitution: {", ".join(CONSTITUTION_RULES)}. Anything else is advisory: \
leave its rule empty.
- Never ask questions. Decide, and say why in the finding."""


class QAStepError(Exception):
    """One step of a QA iteration could not finish; the message says why.

    ``failure`` is set when the step failed because a dependency (the model service,
    MCP or the sandbox) could not be used (FR-209c).
    """

    def __init__(self, message: str, failure: DependencyFailure | None = None) -> None:
        super().__init__(message)
        self.failure = failure


class QAReplyRejected(QAStepError):
    """The model's reply was malformed, cut off or failed a check; QA may be asked again."""


@dataclass
class QAConfig:
    """Settings for the QA Agent; ``from_env`` applies ``QA_*`` overrides."""

    model: str = SMALL_MODEL
    effort: str = "high"
    test_timeout_seconds: float = 60.0
    repair_attempts: int = 2
    use_fallbacks: bool | None = None
    """``None`` means on if the model supports server-side refusal fallback."""
    sandbox_image: str = SANDBOX_IMAGE

    warnings: list[str] = field(default_factory=list, init=False, repr=False,
                                compare=False)
    """Settings the capability table could not check (an unknown model)."""

    def __post_init__(self) -> None:
        if not self.model or not self.sandbox_image:
            raise ValueError("model and sandbox_image must not be empty")
        if self.effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {', '.join(EFFORT_LEVELS)}")
        if self.test_timeout_seconds <= 0:
            raise ValueError("test_timeout_seconds must be greater than 0")
        if not 0 <= self.repair_attempts <= MAX_REPAIR_ATTEMPTS:
            raise ValueError(f"repair_attempts must be between 0 and {MAX_REPAIR_ATTEMPTS}")
        self.use_fallbacks = resolve_fallbacks(self.model, self.use_fallbacks)
        self.warnings = check_model_settings("qa", self.model, self.effort, self.use_fallbacks)

    @classmethod
    def from_env(cls) -> "QAConfig":
        """Build a config from defaults plus any ``QA_*`` environment variables.

        Raises:
            ValueError: if a variable is malformed or out of range.
        """
        env = os.environ
        defaults = cls()
        return cls(
            model=env.get("QA_MODEL", defaults.model),
            effort=env.get("QA_EFFORT", defaults.effort),
            test_timeout_seconds=float(env.get("QA_TEST_TIMEOUT", defaults.test_timeout_seconds)),
            repair_attempts=int(env.get("QA_REPAIR_ATTEMPTS", defaults.repair_attempts)),
            use_fallbacks=fallbacks_from_env(env.get("QA_USE_FALLBACKS")),
            sandbox_image=env.get("CODER_SANDBOX_IMAGE", defaults.sandbox_image),
        )


@dataclass
class QAIterationResult:
    """The outcome of one QA iteration (contracts/qa-agent-api.md)."""

    test_run: TestRun
    findings: list[QAFinding] = field(default_factory=list)
    disputes: list[dict] = field(default_factory=list)
    test_changes: list[TestChange] = field(default_factory=list)
    test_suite: dict[str, str] = field(default_factory=dict)
    verdict: str = "fix_needed"
    usage: TokenUsage = field(default_factory=zero_usage)
    error: str | None = None
    failure: DependencyFailure | None = None

    def to_dict(self) -> dict:
        """Return a plain-JSON copy (the ``qa-iteration`` artifact's data)."""
        return json.loads(json.dumps(asdict(self)))


@dataclass
class _Turn:
    """Working state for one iteration: what has been written, run and spent so far."""

    context: QAContext
    files: McpFileTools
    workspace: Path
    test_suite: dict[str, str]
    test_changes: list[TestChange] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=zero_usage)


def empty_test_run() -> TestRun:
    """A run that never happened (used when QA fails before running any test)."""
    return TestRun(
        loaded=False, exit_code=None, timed_out=False, passed=0, failed=0, errors=0,
        skipped=0, cases=[], output="", duration_seconds=0.0,
    )


class QAAgent:
    """The QA & Debugger Agent. Every iteration stands alone; state arrives in the context."""

    def __init__(
        self,
        config: QAConfig | None = None,
        client=None,
        run_tests=run_pytest,
        file_tools=McpFileTools,
        services: RunServices | None = None,
    ) -> None:
        self.config = config or QAConfig.from_env()
        if run_tests is run_pytest:
            ensure_sandbox(self.config.sandbox_image)
        # The SDK's own retries are off: the run's dependency guard retries (research R3).
        self._client = client or anthropic.Anthropic(max_retries=0)
        self._services = services or RunServices.create()
        self._caller = ModelCaller("qa", self._client, self._services)
        self._run_tests = run_tests
        self._file_tools = file_tools

    def attach(self, services: RunServices) -> None:
        """Use ``services`` (the run's shared helpers) for every later call."""
        self._services = services
        self._caller = ModelCaller("qa", self._client, services)

    def _open_files(self, workspace: Path):
        """An MCP file-tool session for ``workspace``, guarded by the run's ``mcp`` breaker."""
        return self._file_tools(workspace, guard=self._services.guard("mcp", str(workspace)))

    def run_iteration(self, context: QAContext) -> QAIterationResult:
        """Run one QA iteration on the program in ``context["workspace"]``.

        Model, reply, MCP and test-loading failures come back in ``error`` (with
        verdict ``fix_needed``); they are never raised.
        """
        workspace = Path(context["workspace"])
        try:
            with self._open_files(workspace) as files:
                turn = _Turn(context, files, workspace, dict(context["test_suite"]))
                try:
                    return self._iterate(turn)
                except QAStepError as error:
                    return self._failed(turn, str(error), error.failure)
        except McpUnavailableError as error:
            return QAIterationResult(
                test_run=empty_test_run(), test_suite=dict(context["test_suite"]),
                error=f"MCP file tools unavailable: {error}",
            )

    def final_report(self, request: FinalReportRequest) -> QAReport:
        """Summarise the run and write ``.qa/report.md`` through MCP (no model call).

        The session is not guarded: the report is the run's last, quick write, and it
        must be written even when the run is stopping (the guard refuses calls then).
        """
        report = build_report(request)
        content = self._services.redactor.redact(render_report(report))  # FR-219.
        try:
            with self._file_tools(Path(request["workspace"])) as files:
                _, is_error = files.call("write_file", {"path": REPORT_PATH, "content": content})
        except McpUnavailableError:
            is_error = True
        return {**report, "report_path": "" if is_error else REPORT_PATH}

    def _iterate(self, turn: _Turn) -> QAIterationResult:
        context = turn.context
        iteration = context["iteration"]
        sources = self._read_sources(turn)
        tampered, disputes = [], []
        if iteration == 1:
            self._clear_tests(turn.workspace)
            turn.test_suite = {}
            self._write_initial_tests(turn, sources)
        else:
            tampered = self._check_suite(turn)
            disputes = self._resolve_disputes(turn)
        test_run = self._run_until_loaded(turn)
        review = self._review(turn, sources, test_run, disputes)
        findings = normalise_findings(
            review["findings"], test_run, iteration, context["previous_findings"]
        )
        if tampered:
            findings.insert(0, tamper_finding(tampered, iteration, len(findings) + 1))
        findings = apply_lifecycle(findings, context["previous_findings"], disputes)
        self._write_regression_tests(turn, review)
        return QAIterationResult(
            test_run=test_run,
            findings=findings,
            disputes=disputes,
            test_changes=turn.test_changes,
            test_suite=turn.test_suite,
            verdict=verdict_for(test_run, findings, suite_ok=not tampered),
            usage=turn.usage,
        )

    def _check_suite(self, turn: _Turn) -> list[str]:
        """Restore QA's suite where it changed, remove unknown test files; return the paths.

        QA keeps its own copy of the suite (research R5): anything on disk that differs
        was changed by someone other than QA.
        """
        changed = []
        for path, content in sorted(turn.test_suite.items()):
            text, is_error = self._file_call(turn, "read_file", {"path": path})
            if is_error or text != content:
                self._write_file(turn, path, content)
                turn.test_changes.append(
                    TestChange(path=path, reason="restored", finding_id=None, criteria=[])
                )
                changed.append(path)
        tests = turn.workspace / "tests"
        for file in sorted(tests.rglob("*")) if tests.is_dir() else []:
            relative = file.relative_to(turn.workspace).as_posix()
            unknown = relative not in turn.test_suite and "__pycache__" not in file.parts
            if file.is_file() and unknown:
                file.unlink()
                changed.append(relative)
        return changed

    def _resolve_disputes(self, turn: _Turn) -> list[Dispute]:
        """Decide the Coder's disputes against the spec; write any corrected tests."""
        context = turn.context
        disputed = {
            response["finding_id"]: response for response in context["responses"]
            if response["action"] == "disputed"
        }
        if not disputed:
            return []
        findings = [f for f in context["previous_findings"] if f["finding_id"] in disputed]
        parts = [
            render_spec(context["spec"]),
            "The Coder disputes these findings:\n" + json.dumps(findings, indent=2),
            "The Coder's reasons:\n" + json.dumps(list(disputed.values()), indent=2),
            "Current test files:\n" + render_files(turn.test_suite),
            "Decide each dispute by checking the specification: keep the test or finding, "
            "or correct it. Return corrected test files for every 'correct' decision.",
        ]
        def check(data: dict) -> None:
            decided = {decision["finding_id"] for decision in data["decisions"]}
            unknown = sorted(decided - set(disputed))
            if unknown:
                raise QAReplyRejected(
                    f"Malformed QA reply: dispute decision for {', '.join(unknown)}, "
                    "which the Coder did not dispute"
                )
            check_test_files(data["corrected_files"], context["spec"])

        data = self._ask(turn, parts, DISPUTES_FORMAT, QADisputeReply, check)
        corrected = [d["finding_id"] for d in data["decisions"] if d["decision"] == "correct"]
        owner = corrected[0] if len(corrected) == 1 else None
        self._write_test_files(turn, data["corrected_files"], "dispute_correction", owner)
        return [
            Dispute(
                finding_id=decision["finding_id"],
                coder_reason=disputed[decision["finding_id"]]["reason"],
                decision=decision["decision"],
                qa_reason=decision["reason"],
                decided_in=context["iteration"],
            )
            for decision in data["decisions"]
        ]

    def _failed(
        self, turn: _Turn, error: str, failure: DependencyFailure | None = None
    ) -> QAIterationResult:
        return QAIterationResult(
            test_run=empty_test_run(), test_changes=turn.test_changes,
            test_suite=turn.test_suite, usage=turn.usage, error=error, failure=failure,
        )

    def _read_sources(self, turn: _Turn) -> dict[str, str]:
        """Read every source file through MCP (the full code, used only after tests exist)."""
        sources = {}
        for path in turn.context["source_files"]:
            text, is_error = self._file_call(turn, "read_file", {"path": path})
            _raise_if_unavailable(text, is_error, f"Could not read {path}")
            if not is_error:
                sources[path] = text
        return sources

    @staticmethod
    def _clear_tests(workspace: Path) -> None:
        """Remove whatever is in ``tests/`` before QA writes its own suite (iteration 1)."""
        tests = workspace / "tests"
        if tests.is_dir():
            shutil.rmtree(tests)

    def _write_initial_tests(self, turn: _Turn, sources: dict[str, str]) -> None:
        python_sources = {path: text for path, text in sources.items() if path.endswith(".py")}
        parts = [
            render_spec(turn.context["spec"]),
            "The program's public interface (function bodies are hidden on purpose: take "
            "expected behavior only from the specification):\n"
            + interface_summary(python_sources),
            "Coder notes:\n" + "\n".join(f"- {note}" for note in turn.context["coder_notes"]),
            f"Main file: {turn.context['main_file'] or '(none)'}",
            "Write the full pytest suite for this specification under tests/. Cover every "
            "acceptance criterion and the edge cases the specification names.",
        ]
        def check(data: dict) -> None:
            if not data["test_files"]:
                raise QAReplyRejected("Malformed QA reply: no test files were written")
            check_test_files(data["test_files"], turn.context["spec"])

        data = self._ask(turn, parts, WRITE_TESTS_FORMAT, QATestFilesReply, check)
        self._write_test_files(turn, data["test_files"], "initial")

    def _run_until_loaded(self, turn: _Turn) -> TestRun:
        """Run the suite; while it does not load, ask QA to repair its own tests."""
        test_run = self._run(turn)
        for _ in range(self.config.repair_attempts):
            if test_run["loaded"]:
                return test_run
            parts = [
                render_spec(turn.context["spec"]),
                "Current test files:\n" + render_files(turn.test_suite),
                "Your test suite did not load. pytest said:\n" + test_run["output"]
                + "\n\nReturn corrected versions of the test files that need changes.",
            ]
            data = self._ask(
                turn, parts, WRITE_TESTS_FORMAT, QATestFilesReply,
                lambda reply: check_test_files(reply["test_files"], turn.context["spec"]),
            )
            self._write_test_files(turn, data["test_files"], "load_repair")
            test_run = self._run(turn)
        if not test_run["loaded"]:
            first_line = next((line for line in test_run["output"].splitlines() if line), "")
            raise QAStepError(f"QA test suite failed to load: {first_line}")
        return test_run

    def _run(self, turn: _Turn) -> TestRun:
        guard = self._services.guard("sandbox", self.config.sandbox_image)
        try:
            return self._services.tool_call(
                "pytest", {"workspace": str(turn.workspace)},
                lambda: self._run_tests(
                    turn.workspace, self.config.test_timeout_seconds,
                    self.config.sandbox_image, guard=guard, run_id=self._services.run_id,
                ),
                describe=lambda run: {"exit_code": run["exit_code"],
                                      "timed_out": run["timed_out"]},
                event="sandbox_run", guard=guard,
            )
        except SandboxUnavailableError as error:
            raise QAStepError(str(error), error.failure) from error

    def _file_call(self, turn: _Turn, name: str, arguments: dict) -> tuple[str, bool]:
        """One MCP file call, as a traced tool call."""
        return self._services.tool_call(
            name, {"path": arguments["path"]}, lambda: turn.files.call(name, arguments),
            describe=lambda result: {"is_error": result[1]},
            guard=getattr(turn.files, "guard", None),
        )

    def _review(
        self, turn: _Turn, sources: dict[str, str], test_run: TestRun, disputes: list[Dispute]
    ) -> dict:
        context = turn.context
        parts = [
            render_spec(context["spec"]),
            "Program source:\n" + render_files(sources),
            "Test files:\n" + render_files(turn.test_suite),
            "Test results:\n" + render_test_run(test_run),
        ]
        if disputes:
            parts.append("Dispute decisions this iteration:\n" + json.dumps(disputes, indent=2))
        if context["previous_findings"]:
            parts.append(
                "Findings from the last review (use their finding_id in same_as when you "
                "report the same problem again):\n"
                + json.dumps(context["previous_findings"], indent=2)
                + "\nThe Coder's answers:\n" + json.dumps(context["responses"], indent=2)
            )
        parts.append(
            "Review the code against the specification. Report every failing test (kind "
            "failing_test, with its test_ids), every bug, and style problems. For a bug no "
            "test covers, you may add a regression test that reproduces it."
        )
        return self._ask(
            turn, parts, REVIEW_FORMAT, QAReviewReply,
            lambda data: check_review(data, context),
        )

    def _write_regression_tests(self, turn: _Turn, review: dict) -> None:
        iteration = turn.context["iteration"]
        for test in review["regression_tests"]:
            finding = review["findings"][test["finding_index"]]
            finding_id = raw_finding_id(finding, test["finding_index"], iteration)
            self._write_test_files(turn, [test], "regression", finding_id)

    def _write_test_files(
        self, turn: _Turn, files: list[dict], reason: str, finding_id: str | None = None
    ) -> None:
        """Write test files through MCP, recording each change.

        Every caller has already passed the files through ``check_test_files`` as part of
        the reply's check, so nothing is written from a reply that fails it.
        """
        for test_file in files:
            path = checked_test_path(test_file["path"])
            self._write_file(turn, path, test_file["content"])
            turn.test_changes.append(
                TestChange(path=path, reason=reason, finding_id=finding_id,
                           criteria=list(test_file["criteria"]))
            )

    def _write_file(self, turn: _Turn, path: str, content: str) -> None:
        """Write one test file through MCP and record it in QA's copy of the suite."""
        text, is_error = self._file_call(turn, "write_file", {"path": path, "content": content})
        _raise_if_unavailable(text, is_error, f"Could not write {path}")
        if is_error:
            raise QAStepError(f"Could not write {path}: {text}")
        turn.test_suite[path] = content

    def _ask(
        self, turn: _Turn, parts: list[str], schema: dict, reply_type: type,
        check: Callable[[dict], None] | None = None,
    ) -> dict:
        """Get a structured reply that passes ``check``, asking once more after a rejection.

        The reply must first match ``reply_type`` (FR-219a). ``check`` must not write
        anything: it raises ``QAReplyRejected`` for a reply that can't be used. A
        malformed, invalid or cut-off reply is rejected the same way. The second request
        repeats ``parts`` with the rejection reason. Refusals and failed model calls
        are not retried.

        Raises:
            QAStepError: for a refusal or failed call, or the last rejection when the
                corrected reply is rejected too.
        """
        text = parts
        for attempt in range(1, REPLY_ATTEMPTS + 1):
            try:
                data = self._ask_once(turn, text, schema, reply_type)
                if check:
                    check(data)
                return data
            except QAReplyRejected as rejection:
                if attempt == REPLY_ATTEMPTS:
                    raise
                note = CORRECTION_NOTE.format(reason=rejection)
                text = [*parts[:-1], f"{parts[-1]}\n\n{note}"]
        raise AssertionError("unreachable: the loop returns or raises")

    def _ask_once(self, turn: _Turn, parts: list[str], schema: dict, reply_type: type) -> dict:
        """Make one structured-output model call and return its validated JSON."""
        reply = self._caller.call(self._request(parts, schema), reply_type)
        if reply.failure:
            raise QAStepError(failure_message(reply.failure), reply.failure)
        turn.usage = add_usage(turn.usage, reply.usage)
        if reply.refusal:
            raise QAStepError(reply.refusal)
        if reply.response.stop_reason == "max_tokens":
            raise QAReplyRejected(
                "Malformed QA reply: the reply was cut off at the output limit before it "
                "was complete"
            )
        if reply.problems and reply.problems[0].startswith(NOT_JSON):
            raise QAReplyRejected(reply.problems[0])
        if reply.problems:
            raise QAReplyRejected(f"Malformed QA reply: {first_problems(reply.problems)}")
        return reply.data

    def _request(self, parts: list[str], schema: dict) -> dict:
        return build_qa_request(self.config, parts, schema, self._services.prompt_cache_ttl)


def _raise_if_unavailable(text: str, is_error: bool, what: str) -> None:
    """Raise ``QAStepError`` with the failure when a file call could not reach MCP at all."""
    failure = failure_in(text) if is_error else None
    if failure:
        raise QAStepError(f"{what}: MCP file tools unavailable ({failure['message']})", failure)


def build_qa_request(
    config: QAConfig, parts: list[str], schema: dict, cache_ttl: str = "off"
) -> dict:
    """Assemble the keyword arguments for one QA ``beta.messages.create`` call.

    ``parts`` are the user message's sections: every part but the last repeats across
    an iteration's calls (spec, sources, tests) and is cache-marked; the last is this
    call's instruction (research R7).
    """
    request = {
        "model": config.model,
        "max_tokens": MAX_TOKENS,
        "system": system_blocks(QA_SYSTEM_PROMPT, cache_ttl),
        "messages": [{"role": "user", "content": stable_blocks(parts[:-1], parts[-1],
                                                               cache_ttl)}],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": config.effort,
            "format": {"type": "json_schema", "schema": schema},
        },
    }
    return with_fallbacks(request, config.use_fallbacks)


def interface_summary(sources: dict[str, str]) -> str:
    """Describe each module's public interface without any function body (FR-102a)."""
    sections = []
    for path in sorted(sources):
        try:
            tree = ast.parse(sources[path])
        except SyntaxError as error:
            sections.append(f"{path}: could not be parsed ({error.msg}, line {error.lineno})")
            continue
        sections.append(f"## {path}\n" + "\n".join(_module_lines(tree)))
    return "\n\n".join(sections)


def _module_lines(tree: ast.Module) -> list[str]:
    lines = []
    docstring = ast.get_docstring(tree)
    if docstring:
        lines.append(f'"""{docstring.strip()}"""')
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(node.name):
            lines += _function_lines(node, indent="")
        elif isinstance(node, ast.ClassDef) and _public(node.name):
            lines += _class_lines(node)
        elif isinstance(node, ast.If) and ast.unparse(node.test) in _MAIN_GUARDS:
            lines.append('if __name__ == "__main__": ...')
    arguments = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
    ]
    if arguments:
        lines.append("Command-line arguments:")
        lines += [f"  {call}" for call in arguments]
    return lines


_MAIN_GUARDS = ("__name__ == '__main__'", "'__main__' == __name__")


def _public(name: str) -> bool:
    return not name.startswith("_") or name == "__init__"


def _function_lines(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str) -> list[str]:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    lines = [f"{indent}{prefix} {node.name}({ast.unparse(node.args)}){returns}"]
    docstring = ast.get_docstring(node)
    if docstring:
        lines.append(f"{indent}    {docstring.strip().splitlines()[0]}")
    return lines


def _class_lines(node: ast.ClassDef) -> list[str]:
    bases = ", ".join(ast.unparse(base) for base in node.bases)
    lines = [f"class {node.name}({bases})" if bases else f"class {node.name}"]
    docstring = ast.get_docstring(node)
    if docstring:
        lines.append(f"    {docstring.strip().splitlines()[0]}")
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(item.name):
            lines += _function_lines(item, indent="    ")
    return lines


def raw_finding_id(item: dict, index: int, iteration: int) -> str:
    """The ID a review finding gets: its ``same_as`` ID, or ``F<iteration>-<index + 1>``."""
    return item.get("same_as") or f"F{iteration}-{index + 1}"


def normalise_findings(
    raw: list[dict], test_run: TestRun, iteration: int, previous: list[QAFinding]
) -> list[QAFinding]:
    """Turn review findings into schema-valid ``QAFinding``s (research R10).

    Severity is set here, not by the model: failing tests and bugs block, and a style
    problem blocks only when it cites a constitution MUST rule. Every failed or
    errored test (and a timeout) that no finding names gets one. Blocking first.
    """
    first_seen = {finding["finding_id"]: finding["first_seen"] for finding in previous}
    findings, used = [], set()
    for index, item in enumerate(raw):
        finding_id = raw_finding_id(item, index, iteration)
        if finding_id in used:
            finding_id = f"F{iteration}-{index + 1}"
        used.add(finding_id)
        seen = first_seen.get(finding_id, iteration)
        findings.append(_finding(item, finding_id, seen, test_run))
    findings += _uncovered_failures(findings, test_run, iteration, start=len(raw) + 1)
    return sorted(findings, key=lambda finding: finding["severity"] != "blocking")


def _finding(item: dict, finding_id: str, first_seen: int, test_run: TestRun) -> QAFinding:
    kind, rule = item["kind"], item.get("rule") or None
    known_tests = {case["test_id"] for case in test_run["cases"]} | {TIMEOUT_TEST_ID}
    test_ids = [test_id for test_id in item.get("test_ids", []) if test_id in known_tests]
    if kind == "failing_test" and not test_ids:
        kind = "bug"
    if kind in ("failing_test", "bug"):
        severity, rule = "blocking", None
    else:
        rule = rule if rule in CONSTITUTION_RULES else None
        severity = "blocking" if rule else "advisory"
    return QAFinding(
        finding_id=finding_id,
        kind=kind,
        severity=severity,
        rule=rule,
        file=item.get("file", ""),
        location=item.get("location", ""),
        problem=item.get("problem") or "Problem not described by QA.",
        expected=item.get("expected") or "The behavior the specification describes.",
        likely_cause=item.get("likely_cause") or (
            "Not diagnosed by QA's review." if severity == "blocking" and kind != "style" else ""
        ),
        fix_instruction=item.get("fix_instruction") or "Make the code meet the specification.",
        test_ids=test_ids if kind == "failing_test" else [],
        state="open",
        first_seen=first_seen,
    )


def _uncovered_failures(
    findings: list[QAFinding], test_run: TestRun, iteration: int, start: int
) -> list[QAFinding]:
    """Generate a blocking finding for each failure no reported finding names."""
    covered = {test_id for finding in findings for test_id in finding["test_ids"]}
    failures = [
        (case["test_id"], f"{case['outcome']}: {case['message']}".strip(": "))
        for case in test_run["cases"]
        if case["outcome"] in ("failed", "error") and case["test_id"] not in covered
    ]
    if test_run["timed_out"] and TIMEOUT_TEST_ID not in covered:
        failures.append((TIMEOUT_TEST_ID, "the test run passed its time limit and was stopped"))
    generated = []
    for offset, (test_id, problem) in enumerate(failures):
        generated.append(
            QAFinding(
                finding_id=f"F{iteration}-{start + offset}",
                kind="failing_test",
                severity="blocking",
                rule=None,
                file="",
                location=test_id,
                problem=f"Test {test_id} {problem}",
                expected="The behavior this test checks, as the specification describes.",
                likely_cause="Not diagnosed by QA's review; see the test failure.",
                fix_instruction=(
                    "Stop the program from hanging or running too long."
                    if test_id == TIMEOUT_TEST_ID
                    else f"Make {test_id} pass by fixing the program, not the test."
                ),
                test_ids=[test_id],
                state="open",
                first_seen=iteration,
            )
        )
    return generated


def tamper_finding(paths: list[str], iteration: int, number: int) -> QAFinding:
    """The blocking finding for test files changed outside QA (FR-111, research R5)."""
    return QAFinding(
        finding_id=f"F{iteration}-{number}",
        kind="bug",
        severity="blocking",
        rule=None,
        file="",
        location="tests/",
        problem=f"Test files were changed outside QA: {', '.join(paths)}",
        expected="Only the QA Agent changes files under tests/ (FR-111).",
        likely_cause="The program, or a script the Coder ran, wrote into tests/.",
        fix_instruction="Never write under tests/; fix the program so the tests pass instead.",
        test_ids=[],
        state="open",
        first_seen=iteration,
    )


def apply_lifecycle(
    findings: list[QAFinding], previous: list[QAFinding], disputes: list[Dispute]
) -> list[QAFinding]:
    """Settle earlier findings: fixed if not reported again, withdrawn or kept by dispute.

    Returns this iteration's findings plus the earlier ones it settles, open blocking
    findings first (data-model "QAFinding" lifecycle).
    """
    decisions = {dispute["finding_id"]: dispute["decision"] for dispute in disputes}
    reported = {finding["finding_id"] for finding in findings}
    settled = [
        {**finding, "state": "withdrawn"}
        if decisions.get(finding["finding_id"]) == "correct" else finding
        for finding in findings
    ]
    for finding in previous:
        if finding["finding_id"] not in reported:
            decision = decisions.get(finding["finding_id"])
            state = {"correct": "withdrawn", "keep": "open"}.get(decision, "fixed")
            settled.append({**finding, "state": state})
    return sorted(settled, key=lambda f: (not _active(f), f["severity"] != "blocking"))


def _active(finding: QAFinding) -> bool:
    return finding["state"] in ("open", "disputed")


def verdict_for(test_run: TestRun, findings: list[QAFinding], suite_ok: bool) -> str:
    """``complete`` only if the suite loaded and passed and nothing blocks (FR-106)."""
    passed = (
        test_run["loaded"]
        and not test_run["timed_out"]
        and test_run["failed"] + test_run["errors"] == 0
    )
    blocking = any(f["severity"] == "blocking" and _active(f) for f in findings)
    return "complete" if suite_ok and passed and not blocking else "fix_needed"


def build_report(request: FinalReportRequest) -> QAReport:
    """Build QA's final report from the run's iterations (research R14)."""
    iterations = request["iterations"]
    last = iterations[-1]
    status = request["status"] if request["status"] in ("completed", "failed") else "error"
    unresolved = [
        finding for finding in last["findings"]
        if finding["severity"] == "blocking" and finding["state"] in ("open", "disputed")
    ]
    if status == "completed":
        unresolved = []
    return QAReport(
        status=status,
        end_reason=request["end_reason"],
        iterations_run=len(iterations),
        max_iterations=request["max_iterations"],
        final_passed=last["test_run"]["passed"],
        final_failed=last["test_run"]["failed"] + last["test_run"]["errors"],
        unresolved=unresolved,
        advisory=[finding for finding in last["findings"] if finding["severity"] == "advisory"],
        per_iteration=[_iteration_line(iteration) for iteration in iterations],
        report_path=REPORT_PATH,
    )


def _iteration_line(iteration: dict) -> str:
    run = iteration["test_run"]
    blocking = sum(1 for finding in iteration["findings"] if finding["severity"] == "blocking")
    return (
        f"#{iteration['number']}: {run['passed']} passed, {run['failed'] + run['errors']} "
        f"failed, {blocking} blocking"
    )


def render_report(report: QAReport) -> str:
    """Render the final report as Markdown for ``.qa/report.md``."""
    lines = [
        f"# QA report: {report['status']} ({report['end_reason']})",
        "",
        f"Iterations: {report['iterations_run']} of {report['max_iterations']} · "
        f"final tests: {report['final_passed']} passed, {report['final_failed']} failed",
        "",
        "## Unresolved findings",
    ]
    if report["end_reason"] in PARTIAL_END_REASONS:
        lines[1:1] = ["", f"This report is partial: the run ended {report['end_reason']} "
                          "before QA finished its work."]
    lines += [_finding_line(finding) for finding in report["unresolved"]] or ["- None"]
    lines += ["", "## Advisory"]
    lines += [_finding_line(finding) for finding in report["advisory"]] or ["- None"]
    lines += ["", "## Iterations"] + [f"- {line}" for line in report["per_iteration"]]
    return "\n".join(lines) + "\n"


def _finding_line(finding: QAFinding) -> str:
    where = ":".join(part for part in (finding["file"], finding["location"]) if part)
    return (
        f"- **{finding['finding_id']}** {where}: {finding['problem']} "
        f"Fix: {finding['fix_instruction']}"
    )


def render_spec(spec: dict) -> str:
    """Render the specification, with every acceptance criterion, for a QA prompt."""
    criteria = "\n".join(f"- {criterion}" for criterion in spec["acceptance_criteria"])
    tasks = "\n".join(
        f"- {task['task_id']} {task['title']}: {task['description']}\n"
        + "\n".join(f"  - {criterion}" for criterion in task["acceptance_criteria"])
        for task in spec["tasks"]
    )
    assumptions = "\n".join(f"- {item}" for item in spec.get("assumptions", [])) or "- None"
    return (
        f"Specification: {spec['name']}\n{spec['description']}\n\n"
        f"Acceptance criteria:\n{criteria}\n\nTasks and their criteria:\n{tasks}\n\n"
        f"Assumptions:\n{assumptions}"
    )


def render_files(files: dict[str, str]) -> str:
    """Render files for a prompt, each under a heading with its path."""
    return "\n\n".join(f"### {path}\n```\n{text}\n```" for path, text in sorted(files.items()))


def render_test_run(test_run: TestRun) -> str:
    """Summarise a test run for the review prompt."""
    lines = [
        f"loaded={test_run['loaded']} timed_out={test_run['timed_out']} "
        f"passed={test_run['passed']} failed={test_run['failed']} errors={test_run['errors']}"
    ]
    for case in test_run["cases"]:
        if case["outcome"] in ("failed", "error"):
            lines.append(f"- {case['test_id']} {case['outcome']}: {case['message']}")
            lines.append(case["details"])
    return "\n".join(lines)


def all_criteria(spec: dict) -> set[str]:
    """Every acceptance criterion in the spec and its tasks, normalised for comparison."""
    criteria = list(spec["acceptance_criteria"])
    for task in spec["tasks"]:
        criteria += task["acceptance_criteria"]
    return {_normal(criterion) for criterion in criteria}


def check_test_files(files: list[dict], spec: dict) -> None:
    """Check every test file in a reply before any is written.

    Raises:
        QAReplyRejected: if a path is not a ``.py`` file inside ``tests/``, or a listed
            criterion is not copied from the specification.
    """
    allowed = all_criteria(spec)
    for test_file in files:
        checked_test_path(test_file["path"])
        for criterion in test_file["criteria"]:
            if _normal(criterion) not in allowed:
                raise QAReplyRejected(
                    f"Malformed QA reply: criterion {criterion!r} is not in the specification"
                )


def check_review(data: dict, context: QAContext) -> None:
    """Check a review reply, including its regression tests, before anything is written.

    Raises:
        QAReplyRejected: if ``same_as`` names an unknown finding, a regression test points
            at no finding, or a regression test file fails ``check_test_files``.
    """
    known = {finding["finding_id"] for finding in context["previous_findings"]}
    for item in data["findings"]:
        if item["same_as"] and item["same_as"] not in known:
            raise QAReplyRejected(f"Malformed QA reply: same_as names unknown {item['same_as']}")
    for test in data["regression_tests"]:
        if not 0 <= test["finding_index"] < len(data["findings"]):
            raise QAReplyRejected("Malformed QA reply: regression test points at no finding")
    check_test_files(data["regression_tests"], context["spec"])


def checked_test_path(path: str) -> str:
    """Return ``path`` if it is a ``.py`` file inside ``tests/``; otherwise raise.

    Raises:
        QAReplyRejected: if the path leaves ``tests/`` or is not a ``.py`` file.
    """
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or posix.parts[:1] != ("tests",):
        raise QAReplyRejected(f"Malformed QA reply: test file {path!r} is not inside tests/")
    if posix.suffix != ".py":
        raise QAReplyRejected(f"Malformed QA reply: test file {path!r} is not a .py file")
    return posix.as_posix()


def _normal(text: str) -> str:
    return " ".join(text.split()).casefold()
