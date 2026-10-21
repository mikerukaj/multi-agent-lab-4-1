"""Coder Agent: turns a plain-English coding task into working, executed Python code.

A LangGraph ReAct loop (recall -> think -> act, then finalize) drives Claude
through the Anthropic SDK. File tools come from the MCP file-tool server, one
session per run, with ``tests/`` and ``.qa/`` protected from writes. See
specs/001-autonomous-dev-team/ and specs/002-qa-feedback-loop-mcp/ for the design.

Before handing a program to QA the Coder reflects on it: a tool-less critique
lists potential bugs and edge cases, and a revision session answers each one
(``reflect``). Fix rounds and revisions answer findings or critique items in the
summary's ``finding_responses``.

Usage:
    python -m agents.coder_agent "<task>" [--max-steps N] [--timeout SECONDS]
"""

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence, TypedDict

import anthropic
from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

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
    cache_marker,
    failure_message,
    refusal_message,
    stable_blocks,
    system_blocks,
    with_fallbacks,
    zero_usage,
)
from agents.memory import SemanticMemory, SlidingWindowBuffer, Turn
from orchestration.services import RunServices
from orchestration.state import (
    CoderSummaryReply,
    CritiqueItem,
    CritiqueReply,
    DependencyFailure,
    ExecPythonInput,
    FindingResponse,
    ReadFileInput,
    Reflection,
    WriteFileInput,
)
from resilience.classify import BUDGET_EXCEEDED, is_unavailable
from orchestration.validation import first_problems, validate
from tools import dispatch_tool, session_tool_definitions
from tools.mcp_client import McpFileTools, McpUnavailableError
from tools.python_exec import (
    SANDBOX_IMAGE,
    ExecutionResult,
    SandboxUnavailableError,
    ensure_sandbox,
    exec_python,
)

MAX_STEPS_CAP = 10
MAX_REFLECTION_ROUNDS = 3
RESPONSE_ACTIONS = ("fixed", "disputed", "not_addressed")
TOOL_INPUTS = {
    "read_file": ReadFileInput, "write_file": WriteFileInput, "exec_python": ExecPythonInput
}
"""The ``TypedDict`` each tool's model-supplied input must match before it runs."""
PROTECTED_PATHS = ("tests", ".qa")
"""Workspace folders the Coder may read but never write (QA owns them, FR-111)."""
TERMINAL_STATUSES = ("refused", "error", "dependency_unavailable", "budget_exceeded")
"""Statuses that end a session at once: no closing summary call is made after them."""

SYSTEM_PROMPT = """\
You are the Coder Agent in an autonomous software team. You receive one coding task \
and complete it on your own by writing Python files into your workspace and running them.

How to work:
- Before each tool call, say in one short sentence what you are about to do and why. \
These notes are the run's step-by-step record, so a reader can follow your work.
- Use paths relative to the workspace root with read_file, write_file and exec_python.
- Run the program's main file with exec_python, read the output, and fix any problem \
before you finish. Finish only when the program does what the task asks.
- Do not write test files. Tests belong to the QA Agent; check your work by running \
the program. Writes under `tests/` are refused. You may read the tests to understand \
a failure.
- Use only the Python standard library. Code runs in a sandbox with no network, so \
packages cannot be installed and the program cannot reach the internet.
- Write clean, modular code: PEP 8, type hints and docstrings on public functions, \
small functions with descriptive names, and an `if __name__ == "__main__":` guard \
on the entry point.

You work with the same user across many tasks. System messages may bring in notes \
from your earlier tasks with them. Treat what the user told you there (such as their \
name or preferences) as still true unless the current task says otherwise.

When you are done, reply with a short note on what you built."""

SUMMARY_PROMPT = """\
Summarize the work for the run record: the ordered steps you actually took, a short \
explanation of the solution, and the workspace-relative path of the program's main \
file (an empty string if you wrote none). If the task listed findings (F...) or \
critique items (C...), answer each one in finding_responses: fixed, disputed (with a \
reason based on the specification) or not_addressed. Leave the list empty otherwise."""

SUMMARY_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "plan": {"type": "array", "items": {"type": "string"}},
            "explanation": {"type": "string"},
            "main_file": {"type": "string"},
            "finding_responses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding_id": {"type": "string"},
                        "action": {"type": "string", "enum": list(RESPONSE_ACTIONS)},
                        "reason": {"type": "string"},
                    },
                    "required": ["finding_id", "action", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["plan", "explanation", "main_file", "finding_responses"],
        "additionalProperties": False,
    },
}

CRITIQUE_SYSTEM_PROMPT = """\
You are the Coder Agent in an autonomous software team, reviewing your own program \
before it goes to the QA Agent. Read the specification and every source file, then \
list the concrete potential bugs and unhandled edge cases you can find: wrong \
results, missing input validation, edge values (empty, zero, negative, very large), \
error paths and exit codes the specification names. List at least one item. For each, \
give where it is, what could go wrong, and the change you would make."""

CRITIQUE_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["bug", "edge_case"]},
                        "location": {"type": "string"},
                        "description": {"type": "string"},
                        "suggested_change": {"type": "string"},
                    },
                    "required": ["kind", "location", "description", "suggested_change"],
                    "additionalProperties": False,
                },
            },
            "overall": {"type": "string"},
        },
        "required": ["items", "overall"],
        "additionalProperties": False,
    },
}


@dataclass
class CoderConfig:
    """Settings for the Coder Agent; ``from_env`` applies ``CODER_*`` overrides."""

    model: str = SMALL_MODEL
    effort: str = "medium"
    max_steps: int = MAX_STEPS_CAP
    exec_timeout_seconds: float = 30.0
    window_turns: int = 5
    memory_top_k: int = 3
    workspace_root: Path = Path("workspace")
    chroma_path: Path = Path(".chroma")
    use_fallbacks: bool | None = None
    """``None`` means on if the model supports server-side refusal fallback."""
    sandbox_image: str = SANDBOX_IMAGE
    reflection_rounds: int = 1
    chroma_host: str | None = None
    """``CHROMA_HOST``: a Chroma server for the long-term memory (embedded when unset)."""
    chroma_port: int = 8000

    warnings: list[str] = field(default_factory=list, init=False, repr=False,
                                compare=False)
    """Settings the capability table could not check (an unknown model)."""

    def __post_init__(self) -> None:
        if not self.model or not self.sandbox_image:
            raise ValueError("model and sandbox_image must not be empty")
        if not 1 <= self.reflection_rounds <= MAX_REFLECTION_ROUNDS:
            raise ValueError(f"reflection_rounds must be between 1 and {MAX_REFLECTION_ROUNDS}")
        if self.effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {', '.join(EFFORT_LEVELS)}")
        if not 1 <= self.max_steps <= MAX_STEPS_CAP:
            raise ValueError(f"max_steps must be between 1 and {MAX_STEPS_CAP}")
        if self.exec_timeout_seconds <= 0:
            raise ValueError("exec_timeout_seconds must be greater than 0")
        if self.window_turns < 1 or self.memory_top_k < 1:
            raise ValueError("window_turns and memory_top_k must be at least 1")
        self.use_fallbacks = resolve_fallbacks(self.model, self.use_fallbacks)
        self.warnings = check_model_settings("coder", self.model, self.effort,
                                             self.use_fallbacks)

    @classmethod
    def from_env(cls) -> "CoderConfig":
        """Build a config from defaults plus any ``CODER_*`` environment variables.

        Raises:
            ValueError: if a variable is malformed or out of range.
        """
        env = os.environ
        defaults = cls()
        return cls(
            model=env.get("CODER_MODEL", defaults.model),
            effort=env.get("CODER_EFFORT", defaults.effort),
            max_steps=int(env.get("CODER_MAX_STEPS", defaults.max_steps)),
            exec_timeout_seconds=float(
                env.get("CODER_EXEC_TIMEOUT", defaults.exec_timeout_seconds)
            ),
            workspace_root=Path(env.get("CODER_WORKSPACE", defaults.workspace_root)),
            chroma_path=Path(env.get("CODER_CHROMA_PATH", defaults.chroma_path)),
            use_fallbacks=fallbacks_from_env(env.get("CODER_USE_FALLBACKS")),
            sandbox_image=env.get("CODER_SANDBOX_IMAGE", defaults.sandbox_image),
            reflection_rounds=int(env.get("CODER_REFLECTION_ROUNDS", defaults.reflection_rounds)),
            chroma_host=env.get("CHROMA_HOST") or None,
            chroma_port=int(env.get("CHROMA_PORT", defaults.chroma_port)),
        )


@dataclass
class ToolCall:
    """One tool the agent ran during a step, and what it returned."""

    tool: str
    input: dict
    output: str
    is_error: bool


@dataclass
class Step:
    """One Thought -> Action -> Observation step of the ReAct loop."""

    number: int
    thought: str
    actions: list[ToolCall] = field(default_factory=list)


@dataclass
class CoderResult:
    """Structured outcome of one Coder Agent run (contracts/coder-agent-api.md)."""

    code: str
    explanation: str
    plan: list[str]
    execution_result: ExecutionResult | None
    status: str = "completed"
    main_file: str | None = None
    files_written: list[str] = field(default_factory=list)
    steps_used: int = 0
    duration_seconds: float = 0.0
    usage: dict = field(default_factory=zero_usage)
    steps: list[Step] = field(default_factory=list)
    run_id: str = ""
    workspace: str = ""
    error: str | None = None
    finding_responses: list[FindingResponse] = field(default_factory=list)
    validation_problems: list[str] = field(default_factory=list)
    """Why the closing summary failed validation, if it did (the result then falls back)."""
    failure: DependencyFailure | None = None
    """The model call that could not be served, when that ended the run (FR-209c)."""

    def to_dict(self) -> dict:
        """Return a JSON-serialisable copy of this result."""
        return asdict(self)


@dataclass
class ReflectionResult:
    """Outcome of ``CoderAgent.reflect``: every round, and the revised program's result."""

    rounds: list[Reflection]
    result: CoderResult
    status: str
    usage: dict
    failure: DependencyFailure | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable copy of this result."""
        return asdict(self)


class AgentState(TypedDict):
    """LangGraph state for one run. Nodes return the fields they change."""

    run_id: str
    task: str
    workspace: str
    window: list[Turn]
    messages: list[dict]
    step_count: int
    injected_memory_ids: list[str]
    steps: list[Step]
    files_written: list[str]
    last_execution: ExecutionResult | None
    stop_reason: str | None
    status: str
    error: str | None
    usage: dict
    result: CoderResult | None
    failure: DependencyFailure | None


def new_run_id() -> str:
    """Return a sortable, unique run id such as ``20261015-142233-a1b2c3``."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def build_request(
    config: CoderConfig,
    messages: list,
    tools: list[dict],
    output_format: dict | None = None,
    cache_ttl: str = "off",
) -> dict:
    """Assemble the keyword arguments for one ``beta.messages.create`` call.

    ``system`` and ``tools`` never change within a run, which keeps the history
    valid for preserved thinking (research R3). ``tools`` is the session's list from
    ``session_tool_definitions``. With a ``cache_ttl``, the system block is cache-marked
    and top-level ``cache_control`` caches the growing, append-only history (R7).
    """
    output_config: dict[str, Any] = {"effort": config.effort}
    if output_format:
        output_config["format"] = output_format
    request = {
        "model": config.model,
        "max_tokens": MAX_TOKENS,
        "system": system_blocks(SYSTEM_PROMPT, cache_ttl),
        "tools": tools,
        "messages": messages,
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": output_config,
    }
    marker = cache_marker(cache_ttl)
    if marker:
        request["cache_control"] = marker
    return with_fallbacks(request, config.use_fallbacks)


class CoderAgent:
    """A Coder Agent whose short-term memory lasts across ``run`` calls.

    Raises:
        SandboxUnavailableError: on construction, if Docker cannot run the sandbox
            that all generated code executes in.
    """

    def __init__(
        self,
        config: CoderConfig | None = None,
        client=None,
        on_step: Callable[[Step], None] | None = None,
        file_tools=McpFileTools,
        services: RunServices | None = None,
    ) -> None:
        self.config = config or CoderConfig.from_env()
        self._file_tools_factory = file_tools
        self._session = None
        self._tools: list[dict] = []
        ensure_sandbox(self.config.sandbox_image)
        # The SDK's own retries are off: the run's dependency guard retries (research R3).
        self._client = client or anthropic.Anthropic(max_retries=0)
        self._services = services or RunServices.create()
        self._caller = ModelCaller("coder", self._client, self._services)
        self._on_step = on_step
        self._window = SlidingWindowBuffer(self.config.window_turns)
        self._memory = SemanticMemory(
            self.config.chroma_path, guard=self._memory_guard(),
            host=self.config.chroma_host, port=self.config.chroma_port,
        )
        self._graph = self._build_graph()

    def attach(self, services: RunServices) -> None:
        """Use ``services`` (the run's shared helpers) for every later call."""
        self._services = services
        self._caller = ModelCaller("coder", self._client, services)
        self._memory.guard = self._memory_guard()

    def _memory_guard(self):
        target = (f"{self.config.chroma_host}:{self.config.chroma_port}"
                  if self.config.chroma_host else str(self.config.chroma_path))
        return self._services.guard("chroma", target)

    def _open_files(self, workspace: Path):
        """An MCP file-tool session for ``workspace``, guarded by the run's ``mcp`` breaker."""
        return self._file_tools_factory(
            workspace, protect=PROTECTED_PATHS,
            guard=self._services.guard("mcp", str(workspace)),
        )

    def _sandbox_guard(self):
        return self._services.guard("sandbox", self.config.sandbox_image)

    def run(
        self, task: str, workspace: Path | None = None, answer_ids: Sequence[str] = ()
    ) -> CoderResult:
        """Complete ``task`` and return the structured result.

        Model, tool, and limit failures are reported in the result's ``status``
        and ``error``; they are never raised.

        Args:
            task: What the code should do.
            workspace: Folder to work in, created if missing. Callers that run
                several tasks on one program pass the same folder each time. By
                default the run gets a new ``workspace_root/<run_id>/`` folder.
            answer_ids: The finding (``F...``) or critique (``C...``) IDs the task
                asks the Coder to answer; other IDs in its answers are dropped.

        Raises:
            ValueError: if ``task`` is empty.
        """
        if not task.strip():
            raise ValueError("task must not be empty")
        started = time.monotonic()
        run_id = new_run_id()
        if workspace is None:
            workspace = (self.config.workspace_root / run_id).resolve()
            workspace.mkdir(parents=True)
        else:
            workspace = workspace.resolve()
            workspace.mkdir(parents=True, exist_ok=True)
        self._remember(run_id, "task", task)

        try:
            final = self._run_session(run_id, task, workspace)
        except McpUnavailableError as error:
            return _unavailable_result(run_id, workspace, error, started)
        result = replace(
            final["result"],
            duration_seconds=round(time.monotonic() - started, 3),
            finding_responses=[
                response for response in final["result"].finding_responses
                if response["finding_id"] in answer_ids
            ],
        )
        self._window.add(Turn(task=task, reply=result.explanation))
        if result.explanation:
            self._remember(run_id, "result", f"Task: {task}\nResult: {result.explanation}")
        return result

    def reflect(self, brief: str, workspace: Path) -> ReflectionResult:
        """Critique the program in ``workspace`` against ``brief``, then revise it.

        Each round (``config.reflection_rounds``) makes one tool-less critique call
        listing at least one potential bug or edge case, then runs a revision session
        that must answer every item (``C1``...). The revision's result is returned:
        reflection always produces a revised output. A refusal or error stops it.
        """
        workspace = Path(workspace).resolve()
        rounds: list[Reflection] = []
        usage = zero_usage()
        result = None
        for number in range(1, self.config.reflection_rounds + 1):
            items, failure, usage = self._critique_round(brief, workspace, usage)
            if failure is not None:
                result = failure
                break
            ids = [item["item_id"] for item in items]
            result = self.run(render_revision_task(brief, items), workspace, answer_ids=ids)
            usage = add_usage(usage, result.usage)
            rounds.append(
                Reflection(
                    before_iteration=0,
                    round=number,
                    critique=items,
                    revision_status=result.status,
                    files_changed=list(result.files_written),
                    responses=list(result.finding_responses),
                    summary=result.explanation,
                )
            )
            if result.status in TERMINAL_STATUSES:
                break
        return ReflectionResult(
            rounds=rounds, result=result, status=result.status, usage=usage,
            failure=result.failure,
        )

    def _critique_round(
        self, brief: str, workspace: Path, usage: dict
    ) -> tuple[list[CritiqueItem], CoderResult | None, dict]:
        """Read the sources over MCP and critique them; return items or a failed result."""
        try:
            with self._open_files(workspace) as session:
                sources = _read_sources(session, workspace)
        except McpUnavailableError as error:
            return [], _unavailable_result("", workspace, error, time.monotonic()), usage
        for _ in range(2):  # One retry if the critique lists nothing.
            status, payload, usage, failure = self._critique(brief, sources, usage)
            if status != "completed":
                return [], _failed_result(status, payload, workspace, failure), usage
            if payload:
                return _number_items(payload), None, usage
        message = "Self-critique listed no potential bugs or edge cases, twice."
        return [], _failed_result("error", message, workspace), usage

    def _critique(self, brief: str, sources: dict[str, str], usage: dict):
        """Make one critique call; return ``(status, items or error text, usage, failure)``."""
        ttl = self._services.prompt_cache_ttl
        request = {
            "model": self.config.model,
            "max_tokens": MAX_TOKENS,
            "system": system_blocks(CRITIQUE_SYSTEM_PROMPT, ttl),
            "messages": [{"role": "user", "content": _critique_content(brief, sources, ttl)}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.config.effort, "format": CRITIQUE_FORMAT},
        }
        reply = self._caller.call(
            with_fallbacks(request, self.config.use_fallbacks), CritiqueReply
        )
        usage = add_usage(usage, reply.usage)
        if reply.failure:
            message = f"Critique call failed: {failure_message(reply.failure)}"
            return _failure_status(reply.failure), message, usage, reply.failure
        if reply.refusal:
            return "refused", reply.refusal, usage, None
        if reply.problems:
            if reply.problems[0].startswith(NOT_JSON):
                text = f"Critique output is not valid JSON: {reply.text[:200]!r}"
                return "error", text, usage, None
            problems = first_problems(reply.problems)
            return "error", f"Critique output failed validation: {problems}", usage, None
        return "completed", list(reply.data["items"]), usage, None

    def _run_session(self, run_id: str, task: str, workspace: Path) -> AgentState:
        """Run the graph inside one MCP file-tool session for ``workspace``."""
        with self._open_files(workspace) as session:
            self._session = session
            self._tools = session_tool_definitions(session)
            try:
                return self._graph.invoke(
                    _initial_state(run_id, task, workspace, self._window.get()),
                    {"recursion_limit": 4 * self.config.max_steps + 5},
                )
            finally:
                self._session, self._tools = None, []

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("recall", self._recall)
        graph.add_node("think", self._think)
        graph.add_node("act", self._act)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "recall")
        graph.add_edge("recall", "think")
        graph.add_conditional_edges("think", route_after_think, ["act", "finalize"])
        graph.add_edge("act", "recall")
        graph.add_edge("finalize", END)
        return graph.compile()

    def _recall(self, state: AgentState) -> dict:
        """Append memories from earlier runs that this run has not seen yet."""
        messages = list(state["messages"])
        if not messages:
            messages.append(
                {"role": "user", "content": _first_user_message(state["task"], state["window"])}
            )
        query = f"{state['task']}\n{_latest_observation(messages)}"
        hits = self._recall_memories(query, state["run_id"])
        new_hits = [(id_, doc) for id_, doc in hits if id_ not in state["injected_memory_ids"]]
        if new_hits:
            messages.append({"role": "system", "content": _render_memories(new_hits)})
        return {
            "messages": messages,
            "injected_memory_ids": state["injected_memory_ids"] + [id_ for id_, _ in new_hits],
        }

    def _think(self, state: AgentState) -> dict:
        """Ask the model for the next Thought and Action."""
        reply = self._caller.call(build_request(
            self.config, state["messages"], self._tools,
            cache_ttl=self._services.prompt_cache_ttl,
        ))
        if reply.failure:
            return {"status": _failure_status(reply.failure),
                    "error": failure_message(reply.failure), "failure": reply.failure}
        response = reply.response
        step_count = state["step_count"] + 1
        update = {
            "messages": state["messages"] + [{"role": "assistant", "content": response.content}],
            "step_count": step_count,
            "usage": add_usage(state["usage"], reply.usage),
            "stop_reason": response.stop_reason,
            "steps": state["steps"] + [Step(step_count, _thought_text(response.content))],
        }
        if response.stop_reason == "refusal":
            update.update(status="refused", error=refusal_message(response.stop_details))
        elif response.stop_reason == "tool_use" and step_count >= self.config.max_steps:
            update["status"] = "step_limit"
        elif response.stop_reason not in ("tool_use", "end_turn"):
            update.update(status="error", error=f"Unexpected stop reason: {response.stop_reason}")
        return update

    def _act(self, state: AgentState) -> dict:
        """Run every requested tool and return all results in one user message."""
        workspace = Path(state["workspace"])
        files_written = list(state["files_written"])
        last_execution = state["last_execution"]
        results, actions = [], []
        for block in _tool_uses(state["messages"][-1]):
            text, is_error, execution = self._run_tool(block, workspace)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": text,
                    "is_error": is_error,
                }
            )
            actions.append(ToolCall(block.name, dict(block.input), text, is_error))
            path = block.input.get("path")
            if block.name == "write_file" and not is_error and path not in files_written:
                files_written.append(path)
            last_execution = execution or last_execution
            observation = f"{_describe_call(block.name, block.input)} -> {text}"
            self._remember(state["run_id"], "observation", observation)

        step = replace(state["steps"][-1], actions=actions)
        if self._on_step:
            self._on_step(step)
        return {
            "messages": state["messages"] + [{"role": "user", "content": results}],
            "steps": state["steps"][:-1] + [step],
            "files_written": files_written,
            "last_execution": last_execution,
        }

    def _run_tool(self, block, workspace: Path) -> tuple[str, bool, ExecutionResult | None]:
        """Check one requested tool call, then run it (FR-219a: never act on bad input)."""
        session_names = {tool["name"] for tool in self._tools}
        if block.name not in session_names or block.name not in TOOL_INPUTS:
            return f"Unknown tool: {block.name}", True, None
        problems = validate(block.input, TOOL_INPUTS[block.name])
        if problems:
            return f"Invalid tool input: {first_problems(problems)}", True, None
        sandbox = block.name == "exec_python"
        guard = self._sandbox_guard() if sandbox else getattr(self._session, "guard", None)
        return self._services.tool_call(
            block.name, {"path": block.input["path"]},
            lambda: dispatch_tool(
                block.name, block.input, workspace, self.config.exec_timeout_seconds,
                self.config.sandbox_image, file_tools=self._session, guard=guard,
                run_id=self._services.run_id,
            ),
            describe=_describe_tool_result, event="sandbox_run" if sandbox else "tool_call",
            guard=guard,
        )

    def _remember(self, run_id: str, kind: str, text: str) -> None:
        """Store a memory, as a traced ``memory.add`` tool call."""
        self._services.tool_call("memory.add", {"kind": kind},
                                 lambda: self._memory.add(run_id, kind, text),
                                 guard=getattr(self._memory, "guard", None))

    def _recall_memories(self, query: str, exclude_run_id: str) -> list:
        """Query the long-term memory, as a traced ``memory.query`` tool call."""
        return self._services.tool_call(
            "memory.query", {"top_k": self.config.memory_top_k},
            lambda: self._memory.query(query, top_k=self.config.memory_top_k,
                                       exclude_run_id=exclude_run_id),
            guard=getattr(self._memory, "guard", None),
        )

    def _finalize(self, state: AgentState) -> dict:
        """Ask for the summary, then fill code and execution from ground truth."""
        status = "completed" if state["status"] == "running" else state["status"]
        closing = {"status": status, "error": state["error"], "usage": state["usage"],
                   "messages": state["messages"], "failure": state["failure"],
                   "problems": [], "summary": {"plan": [], "explanation": state["error"] or "",
                                               "main_file": ""}}
        if status not in TERMINAL_STATUSES:
            closing = self._closing_summary(closing)
        status, error, usage = closing["status"], closing["error"], closing["usage"]
        summary, messages = closing["summary"], closing["messages"]
        workspace = Path(state["workspace"])
        main_file = _choose_main_file(summary["main_file"], state)
        code, missing = ("", True)
        if main_file:
            code, missing = self._session.call("read_file", {"path": main_file})
        result = CoderResult(
            code="" if missing else code,
            explanation=summary["explanation"],
            plan=summary["plan"],
            finding_responses=summary.get("finding_responses", []),
            execution_result=self._execution_for(workspace, main_file, state["last_execution"]),
            status=status,
            main_file=main_file,
            files_written=state["files_written"],
            steps_used=state["step_count"],
            usage=usage,
            steps=state["steps"],
            run_id=state["run_id"],
            workspace=str(workspace),
            error=error,
            validation_problems=closing["problems"],
            failure=closing["failure"],
        )
        return {"messages": messages, "status": status, "usage": usage, "result": result}

    def _closing_summary(self, closing: dict) -> dict:
        """Ask for the structured summary; return ``closing`` updated with its outcome."""
        messages = closing["messages"] + [
            {"role": "user", "content": _closing_content(closing["messages"][-1])}
        ]
        reply = self._caller.call(
            build_request(self.config, messages, self._tools, SUMMARY_FORMAT,
                          self._services.prompt_cache_ttl),
            CoderSummaryReply,
        )
        closing = {**closing, "messages": messages,
                   "usage": add_usage(closing["usage"], reply.usage)}
        if reply.failure:
            error = f"Summary call failed: {failure_message(reply.failure)}"
            return {**closing, "failure": reply.failure, "error": error,
                    "status": _failure_status(reply.failure),
                    "summary": {**closing["summary"], "explanation": error}}
        return {**closing, "summary": _parse_summary(reply), "problems": reply.problems}

    def _execution_for(
        self, workspace: Path, main_file: str | None, last: ExecutionResult | None
    ) -> ExecutionResult | None:
        """Return a real run of ``main_file``, reusing the last one if it matches."""
        if not main_file or not main_file.endswith(".py"):
            return None
        if last and os.path.normpath(last.path) == os.path.normpath(main_file):
            return last
        try:
            result = exec_python(
                workspace, main_file, self.config.exec_timeout_seconds,
                self.config.sandbox_image, self._sandbox_guard(), self._services.run_id,
            )
        except (ValueError, FileNotFoundError):
            return None
        return None if result.failure else result


def render_revision_task(brief: str, items: list[CritiqueItem]) -> str:
    """The revision task: answer every critique item, then run the program."""
    lines = [
        "Self-review before QA. The program in the workspace was built for this brief:",
        brief,
        "",
        "Your critique of it found these potential problems:",
    ]
    for item in items:
        lines.append(
            f"[{item['item_id']}] ({item['kind']}) {item['location']}: {item['description']} "
            f"Suggested change: {item['suggested_change']}"
        )
    lines += [
        "",
        "Address each item: change the code, or explain why no change is needed. Then run "
        "the program to check it. Do not write test files. Answer every C... ID above in "
        "finding_responses.",
    ]
    return "\n".join(lines)


def _critique_content(brief: str, sources: dict[str, str], ttl: str) -> list[dict]:
    """The critique's user content: the sources (re-sent every round, so cache-marked),
    then the brief."""
    files = "\n\n".join(f"### {path}\n```python\n{text}\n```" for path, text in sources.items())
    return stable_blocks([f"Source files:\n\n{files or '(none)'}"],
                         f"Specification and brief:\n{brief}", ttl)


def _read_sources(session, workspace: Path) -> dict[str, str]:
    """Read every non-hidden ``.py`` file outside ``tests/`` through the MCP session."""
    sources = {}
    for path in sorted(workspace.rglob("*.py")):
        relative = path.relative_to(workspace)
        if relative.parts[0] == "tests" or any(part.startswith(".") for part in relative.parts):
            continue
        text, is_error = session.call("read_file", {"path": relative.as_posix()})
        if not is_error:
            sources[relative.as_posix()] = text
    return sources


def _number_items(items: list[dict]) -> list[CritiqueItem]:
    return [
        CritiqueItem(
            item_id=f"C{number}",
            kind=item.get("kind", "edge_case"),
            location=str(item.get("location", "")),
            description=str(item.get("description", "")),
            suggested_change=str(item.get("suggested_change", "")),
        )
        for number, item in enumerate(items, start=1)
    ]


def _failed_result(
    status: str, message: str, workspace: Path, failure: DependencyFailure | None = None
) -> CoderResult:
    return CoderResult(
        code="", explanation=message, plan=[], execution_result=None, status=status,
        workspace=str(workspace), error=message, failure=failure,
    )


def _failure_status(failure: DependencyFailure) -> str:
    """The Coder status for a model call that could not be served (FR-205, FR-223)."""
    if failure["error_type"] == BUDGET_EXCEEDED:
        return "budget_exceeded"
    return "dependency_unavailable" if is_unavailable(failure) else "error"


def _unavailable_result(
    run_id: str, workspace: Path, error: McpUnavailableError, started: float
) -> CoderResult:
    """The result of a run that could not use its MCP file tools."""
    message = f"MCP file tools unavailable: {error}"
    return CoderResult(
        code="",
        explanation=message,
        plan=[],
        execution_result=None,
        status="error",
        duration_seconds=round(time.monotonic() - started, 3),
        run_id=run_id,
        workspace=str(workspace),
        error=message,
    )


def run_coder_agent(task: str, config: CoderConfig | None = None) -> CoderResult:
    """Run ``task`` once on a fresh ``CoderAgent``."""
    return CoderAgent(config).run(task)


def route_after_think(state: AgentState) -> str:
    """Send tool calls to ``act`` until a limit, refusal, or error ends the loop."""
    if state["status"] == "running" and state["stop_reason"] == "tool_use":
        return "act"
    return "finalize"


def _initial_state(run_id: str, task: str, workspace: Path, window: list[Turn]) -> AgentState:
    return AgentState(
        run_id=run_id,
        task=task,
        workspace=str(workspace),
        window=window,
        messages=[],
        step_count=0,
        injected_memory_ids=[],
        steps=[],
        files_written=[],
        last_execution=None,
        stop_reason=None,
        status="running",
        error=None,
        usage=zero_usage(),
        result=None,
        failure=None,
    )


def _first_user_message(task: str, window: list[Turn]) -> str:
    """Render the task, preceded by the recent conversation if there is one."""
    if not window:
        return f"Task:\n{task}"
    history = "\n".join(f"- Task: {turn.task}\n  Result: {turn.reply}" for turn in window)
    return f"Recent conversation (oldest first):\n{history}\n\nTask:\n{task}"


def _render_memories(hits: list[tuple[str, str]]) -> str:
    notes = "\n".join(f"- {document}" for _, document in hits)
    return f"Notes from your earlier tasks with this user (long-term memory):\n{notes}"


def _latest_observation(messages: list[dict]) -> str:
    """Return the text of the most recent tool results, or an empty string."""
    content = messages[-1]["content"]
    if messages[-1]["role"] != "user" or isinstance(content, str):
        return ""
    return "\n".join(block["content"] for block in content if block["type"] == "tool_result")


def _tool_uses(message: dict) -> list:
    if message["role"] != "assistant":
        return []
    return [block for block in message["content"] if block.type == "tool_use"]


def _closing_content(last_message: dict) -> list[dict]:
    """Answer any tool calls left pending by a limit, then ask for the summary."""
    pending = [
        {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": "Not run: the step limit was reached.",
            "is_error": True,
        }
        for block in _tool_uses(last_message)
    ]
    return pending + [{"type": "text", "text": SUMMARY_PROMPT}]


def _parse_summary(reply) -> dict:
    """Read the validated summary, falling back for a refusal or a reply that failed.

    The fallback keeps the raw text as the explanation; the code and its execution
    still come from the workspace (ground truth), so a bad summary never loses work.
    """
    if reply.refusal:
        return {"plan": [], "explanation": reply.refusal, "main_file": ""}
    if reply.data is None:
        return {"plan": [], "explanation": reply.text, "main_file": ""}
    data = reply.data
    return {
        "plan": list(data["plan"]),
        "explanation": data["explanation"],
        "main_file": data["main_file"],
        "finding_responses": _parse_responses(data["finding_responses"]),
    }


def _parse_responses(items: list) -> list[FindingResponse]:
    """Keep the well-formed answers to findings and critique items."""
    return [
        FindingResponse(
            finding_id=str(item["finding_id"]), action=item["action"], reason=str(item["reason"])
        )
        for item in items
        if isinstance(item, dict) and item.get("action") in RESPONSE_ACTIONS
        and "finding_id" in item and "reason" in item
    ]


def _choose_main_file(reported: str, state: AgentState) -> str | None:
    """Prefer the model's main file if it exists; else the last file run or written."""
    if reported in state["files_written"]:
        return reported
    if state["last_execution"]:
        return state["last_execution"].path
    python_files = [path for path in state["files_written"] if path.endswith(".py")]
    return python_files[-1] if python_files else None


def _thought_text(content: list) -> str:
    """Join the step's reasoning summaries and notes.

    When the model wrote none, fall back to the ``purpose`` of each tool call,
    which the strict tool schemas require, so a step's thought is never empty.
    """
    parts = []
    for block in content:
        if block.type == "thinking" and block.thinking:
            parts.append(block.thinking)
        elif block.type == "text" and block.text:
            parts.append(block.text)
    if not parts:
        parts = [
            block.input["purpose"]
            for block in content
            if block.type == "tool_use" and block.input.get("purpose")
        ]
    return "\n".join(parts)


def _describe_tool_result(result: tuple) -> dict:
    """The span outputs and event details of one ``dispatch_tool`` result."""
    _, is_error, execution = result
    outputs = {"is_error": is_error}
    if execution is not None:
        outputs.update(exit_code=execution.exit_code, timed_out=execution.timed_out)
    return outputs


def _describe_call(name: str, tool_input: dict) -> str:
    """Short label for a tool call, e.g. ``write_file fizzbuzz.py``."""
    return f"{name} {tool_input.get('path', '')}".strip()


def print_step_progress(step: Step) -> None:
    """Print one progress line for ``step`` to stderr, e.g. ``[step 2] exec_python main.py``."""
    calls = ", ".join(_describe_call(action.tool, action.input) for action in step.actions)
    print(f"[step {step.number}] {calls}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI: build, then self-reflect once; 0 if the revision completed, 1 otherwise,
    2 for usage errors."""
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="python -m agents.coder_agent",
        description="Turn a plain-English coding task into working Python code.",
    )
    parser.add_argument("task", help="what the code should do")
    parser.add_argument("--max-steps", type=int, help=f"ReAct step cap (1-{MAX_STEPS_CAP})")
    parser.add_argument("--timeout", type=float, help="seconds before a code run is killed")
    args = parser.parse_args(argv)

    try:
        config = CoderConfig.from_env()
        if args.max_steps is not None:
            config = replace(config, max_steps=args.max_steps)
        if args.timeout is not None:
            config = replace(config, exec_timeout_seconds=args.timeout)
        agent = CoderAgent(config, on_step=print_step_progress)
        first_draft = agent.run(args.task)
        reflection = agent.reflect(args.task, Path(first_draft.workspace))
    except (ValueError, SandboxUnavailableError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    output = {
        **reflection.result.to_dict(),
        "first_draft": {
            "status": first_draft.status,
            "explanation": first_draft.explanation,
            "files_written": first_draft.files_written,
        },
        "reflection": reflection.rounds,
    }
    print(json.dumps(output, indent=2))
    return 0 if reflection.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
