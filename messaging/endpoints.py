"""A2A endpoints that put the Coder and QA agents behind the broker.

Each endpoint is an agent card plus a synchronous skill handler. Handlers receive
an A2A ``Message``, pass its ``data`` part through as the JSON-only request payload
(``QAContext``, ``FinalReportRequest`` or ``FixRequest`` from orchestration/state.py),
call the agent by method name, and return the result as an artifact. This module is
duck-typed and imports no agent module, so neither agent depends on the other
(SC-110). See specs/002-qa-feedback-loop-mcp/contracts/a2a-binding.md §7, §8, §10.
"""

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
FAILED_STATUSES = ("refused", "error")
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


def qa_handler(qa) -> SkillHandler:
    """Return the skill handler for ``qa`` (any object with ``run_iteration`` and
    ``final_report``)."""

    def handle(message: Message) -> list[Artifact]:
        skill = message.get("metadata", {}).get("skillId")
        if skill == REVIEW_SKILL:
            context = _payload(message, REVIEW_KEYS)
            result = qa.run_iteration(context)
            if result.error:
                raise SkillFailed(result.error)
            return [artifact("qa-iteration", [data_part(result.to_dict())])]
        if skill == REPORT_SKILL:
            report = qa.final_report(_payload(message, REPORT_KEYS))
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


def coder_handler(coder) -> SkillHandler:
    """Return the skill handler for ``coder`` (any object with ``run``)."""

    def handle(message: Message) -> list[Artifact]:
        skill = message.get("metadata", {}).get("skillId")
        if skill != FIX_SKILL:
            raise SkillRejected(f"{CODER_AGENT} has no skill {skill!r}")
        request = _payload(message, FIX_KEYS)
        brief = find_text(message)
        if not brief:
            raise SkillRejected("the fix request needs a text part holding the fix brief")
        result = coder.run(
            brief, Path(request["workspace"]), answer_ids=list(request["answer_ids"])
        )
        if result.status in FAILED_STATUSES:
            raise SkillFailed(f"{result.status}: {result.error or result.explanation}")
        fix = {
            "status": result.status,
            "hit_step_cap": result.status == "step_limit",
            "steps_used": result.steps_used,
            "files_changed": list(result.files_written),
            "responses": list(result.finding_responses),
            "error": result.error,
        }
        item = artifact("coder-fix", [data_part(json_copy(fix))])
        item["metadata"] = {"usage": dict(result.usage)}
        return [item]

    return handle


def register_team_agents(broker: A2ABroker, coder, qa, version: str = __version__) -> None:
    """Register the team's A2A endpoints (QA and Coder) on ``broker``."""
    broker.register(qa_card(version), qa_handler(qa))
    broker.register(coder_card(version), coder_handler(coder))


def _payload(message: Message, required: tuple[str, ...]) -> dict:
    """Return the message's data part as a request payload, or reject the message."""
    data = find_data(message)
    if not isinstance(data, dict):
        raise SkillRejected("the request needs a data part holding a JSON object")
    missing = [key for key in required if key not in data]
    if missing:
        raise SkillRejected(f"the request payload is missing {', '.join(missing)}")
    return json_copy(data)
