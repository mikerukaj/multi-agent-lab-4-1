"""A2A endpoints that put the Coder and QA agents behind the broker.

Each endpoint is an agent card plus a synchronous skill handler. Handlers receive
an A2A ``Message``, pass its ``data`` part through as the JSON-only request payload
(``QAContext``, ``FinalReportRequest`` or ``FixRequest`` from orchestration/state.py),
call the agent by method name, and return the result as an artifact. This module is
duck-typed and imports no agent module, so neither agent depends on the other
(SC-110). See specs/002-qa-feedback-loop-mcp/contracts/a2a-binding.md §7, §8, §10.

With the run's ``services`` (also duck-typed), each handler continues the trace from
the headers in the message metadata, attributes events on its broker thread to its
agent and stage, and opens the invocation's ``agent:*`` span
(specs/003-production-hardening/contracts/tracing.md).
"""

import contextlib
from pathlib import Path

from messaging import __version__
from messaging.a2a import (
    AgentCard,
    Artifact,
    Message,
    SkillFailed,
    SkillRejected,
    agent_card,
    agent_skill,
    artifact,
    data_part,
    find_data,
    find_text,
    json_copy,
)
from messaging.broker import A2ABroker, SkillHandler

QA_AGENT = "qa-debugger"
CODER_AGENT = "coder"
REVIEW_SKILL = "review-program"
REPORT_SKILL = "final-report"
FIX_SKILL = "fix-program"
FIX_KEYS = ("workspace", "iteration", "answer_ids", "findings")
FAILED_STATUSES = ("refused", "error", "dependency_unavailable", "budget_exceeded")
REVIEW_KEYS = ("iteration", "workspace", "spec", "source_files", "test_suite")
REPORT_KEYS = ("run_id", "workspace", "status", "end_reason", "max_iterations", "iterations")


def qa_card(version: str = __version__) -> AgentCard:
    """Return the QA & Debugger Agent's card."""
    return agent_card(
        QA_AGENT,
        "Writes and runs pytest tests for a program, reviews its code, and returns a fix "
        "instruction list.",
        [
            agent_skill(
                REVIEW_SKILL, "Review program",
                "Run one QA iteration: tests, sandboxed run, review, verdict.",
                ["qa", "pytest", "review"],
            ),
            agent_skill(
                REPORT_SKILL, "Final report",
                "Summarise the run's QA iterations and unresolved findings.",
                ["qa", "report"],
            ),
        ],
        version,
    )


def qa_handler(qa, services=None) -> SkillHandler:
    """Return the skill handler for ``qa`` (any object with ``run_iteration`` and
    ``final_report``)."""

    def handle(message: Message) -> list[Artifact]:
        skill = message.get("metadata", {}).get("skillId")
        if skill == REVIEW_SKILL:
            context = _payload(message, REVIEW_KEYS)
            with _observed(services, message, "qa", "qa_review", "review",
                           iteration=context["iteration"]):
                result = qa.run_iteration(context)
            item = artifact("qa-iteration", [data_part(result.to_dict())])
            if result.error:
                raise SkillFailed(result.error, [item])
            return [item]
        if skill == REPORT_SKILL:
            request = _payload(message, REPORT_KEYS)
            with _observed(services, message, "qa", "final_report", "final_report"):
                report = qa.final_report(request)
            return [artifact("qa-report", [data_part(report)])]
        raise SkillRejected(f"{QA_AGENT} has no skill {skill!r}")

    return handle


def coder_card(version: str = __version__) -> AgentCard:
    """Return the Coder Agent's card."""
    return agent_card(
        CODER_AGENT,
        "Builds and fixes a Python program in a shared workspace.",
        [
            agent_skill(
                FIX_SKILL, "Fix program",
                "Apply a fix instruction list to the workspace and answer each finding.",
                ["coder", "fix"],
            )
        ],
        version,
    )


def coder_handler(coder, services=None) -> SkillHandler:
    """Return the skill handler for ``coder`` (any object with ``run``)."""

    def handle(message: Message) -> list[Artifact]:
        skill = message.get("metadata", {}).get("skillId")
        if skill != FIX_SKILL:
            raise SkillRejected(f"{CODER_AGENT} has no skill {skill!r}")
        request = _payload(message, FIX_KEYS)
        brief = find_text(message)
        if not brief:
            raise SkillRejected("the fix request needs a text part holding the fix brief")
        with _observed(services, message, "coder", "fix", "fix",
                       iteration=request["iteration"]):
            result = coder.run(
                brief, Path(request["workspace"]), answer_ids=list(request["answer_ids"])
            )
        if result.status in FAILED_STATUSES:
            raise SkillFailed(
                f"{result.status}: {result.error or result.explanation}", [_fix_artifact(result)]
            )
        return [_fix_artifact(result)]

    return handle


def _fix_artifact(result) -> Artifact:
    """The ``coder-fix`` artifact: the fix outcome, with usage and any failure as metadata."""
    fix = {
        "status": result.status,
        "hit_step_cap": result.status == "step_limit",
        "steps_used": result.steps_used,
        "files_changed": list(result.files_written),
        "responses": list(result.finding_responses),
        "error": result.error,
    }
    item = artifact("coder-fix", [data_part(json_copy(fix))])
    # Duck-typed: a Coder result without a ``failure`` attribute reports none.
    failure = getattr(result, "failure", None)
    item["metadata"] = json_copy({"usage": dict(result.usage), "failure": failure})
    return item


def register_team_agents(
    broker: A2ABroker, coder, qa, version: str = __version__, services=None
) -> None:
    """Register the team's A2A endpoints (QA and Coder) on ``broker``."""
    broker.register(qa_card(version), qa_handler(qa, services))
    broker.register(coder_card(version), coder_handler(coder, services))


@contextlib.contextmanager
def _observed(services, message: Message, agent: str, stage: str, invocation: str, **fields):
    """On this broker thread: continue the sender's trace, set the event context, and
    open the invocation's agent span. A no-op without ``services``."""
    if services is None:
        yield
        return
    headers = message.get("metadata", {}).get("trace") or {}
    with services.tracer.continue_from(headers), \
            services.events.context(agent=agent, stage=stage, **fields), \
            services.tracer.agent_span(agent, invocation, stage=stage, **fields):
        yield


def _payload(message: Message, required: tuple[str, ...]) -> dict:
    """Return the message's data part as a request payload, or reject the message."""
    data = find_data(message)
    if not isinstance(data, dict):
        raise SkillRejected("the request needs a data part holding a JSON object")
    missing = [key for key in required if key not in data]
    if missing:
        raise SkillRejected(f"the request payload is missing {', '.join(missing)}")
    return json_copy(data)
