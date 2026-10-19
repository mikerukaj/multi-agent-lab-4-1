"""Tests for the A2A agent endpoints (contracts/a2a-binding.md §7, §8, §10)."""

import json
import unittest
from pathlib import Path

from messaging import __version__
from messaging.a2a import (
    BINDING_URN,
    SkillFailed,
    SkillRejected,
    data_part,
    text_part,
    user_message,
)
from messaging.endpoints import coder_card, coder_handler, qa_card, qa_handler
from tests.team_fakes import FakeCoder, FakeQA, qa_result, scripted_finding, scripted_test_run

CONTEXT = {
    "iteration": 1,
    "workspace": "/tmp/ws",
    "spec": {"name": "S", "description": "", "acceptance_criteria": [], "assumptions": [],
             "tasks": []},
    "source_files": ["app.py"],
    "coder_notes": [],
    "main_file": "app.py",
    "test_suite": {},
    "previous_findings": [],
    "responses": [],
}


def request(skill: str, data=None, with_data: bool = True) -> dict:
    parts = [text_part("Please review.")]
    if with_data:
        parts.append(data_part(data))
    return user_message(parts, context_id="run-1", metadata={"skillId": skill, "sender": "coder"})


class QACardTests(unittest.TestCase):
    def test_card_fields(self) -> None:
        card = qa_card()
        self.assertEqual(card["name"], "qa-debugger")
        self.assertEqual(card["version"], __version__)
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], BINDING_URN)
        self.assertFalse(any(card["capabilities"].values()))
        skills = {skill["id"]: skill for skill in card["skills"]}
        self.assertEqual(set(skills), {"review-program", "final-report"})
        self.assertIn("qa", skills["review-program"]["tags"])


class QAHandlerTests(unittest.TestCase):
    def test_review_passes_the_context_through_and_returns_the_result(self) -> None:
        result = qa_result("fix_needed")
        qa = FakeQA([result])
        message = request("review-program", json.loads(json.dumps(CONTEXT)))
        artifacts = qa_handler(qa)(message)
        received = qa.contexts[0]
        self.assertEqual(received, CONTEXT)
        self.assertIsInstance(received["workspace"], str)
        self.assertIsNot(received, message["parts"][1]["data"])
        self.assertEqual([item["name"] for item in artifacts], ["qa-iteration"])
        self.assertEqual(artifacts[0]["parts"], [data_part(result.to_dict())])

    def test_qa_error_fails_the_skill(self) -> None:
        qa = FakeQA([qa_result("fix_needed", error="QA test suite failed to load: boom")])
        with self.assertRaisesRegex(SkillFailed, "failed to load: boom"):
            qa_handler(qa)(request("review-program", CONTEXT))

    def test_final_report_passes_the_request_through(self) -> None:
        iteration = {"number": 1, "test_run": scripted_test_run(), "findings": []}
        report_request = {"run_id": "run-1", "workspace": "/tmp/ws", "status": "completed",
                          "end_reason": "complete", "max_iterations": 5,
                          "iterations": [iteration]}
        qa = FakeQA([])
        artifacts = qa_handler(qa)(request("final-report", report_request))
        self.assertEqual(qa.report_requests, [report_request])
        self.assertEqual(artifacts[0]["name"], "qa-report")
        self.assertEqual(artifacts[0]["parts"][0]["data"]["status"], "completed")

    def test_unknown_skill_or_missing_payload_is_rejected(self) -> None:
        handler = qa_handler(FakeQA([]))
        for message in (
            request("fix-program", CONTEXT),
            request("review-program", with_data=False),
            request("review-program", {"iteration": 1}),
            request("review-program", ["not", "an", "object"]),
            request("final-report", {"run_id": "x"}),
        ):
            with self.subTest(skill=message["metadata"]["skillId"]):
                with self.assertRaises(SkillRejected):
                    handler(message)


def fix_request(statuses=("completed",)) -> tuple[FakeCoder, dict]:
    coder = FakeCoder(fix_statuses=statuses)
    payload = {"workspace": "/tmp/ws", "iteration": 1, "answer_ids": ["F1-1"],
               "findings": [scripted_finding("F1-1")]}
    message = user_message(
        [text_part("You are fixing: S. QA review #1 of 5 found problems."), data_part(payload)],
        context_id="run-1", metadata={"skillId": "fix-program", "sender": "qa-debugger"},
    )
    return coder, message


class CoderEndpointTests(unittest.TestCase):
    def test_card(self) -> None:
        card = coder_card()
        self.assertEqual(card["name"], "coder")
        self.assertEqual([skill["id"] for skill in card["skills"]], ["fix-program"])
        self.assertEqual(card["supportedInterfaces"][0]["url"], "broker://coder")

    def test_fix_request_runs_a_fix_round_and_returns_a_coder_fix(self) -> None:
        coder, message = fix_request()
        artifacts = coder_handler(coder)(message)
        brief, workspace, answer_ids = coder.fix_calls[0]
        self.assertEqual(brief, "You are fixing: S. QA review #1 of 5 found problems.")
        self.assertEqual((workspace, answer_ids), (Path("/tmp/ws"), ["F1-1"]))
        self.assertEqual(artifacts[0]["name"], "coder-fix")
        fix = artifacts[0]["parts"][0]["data"]
        keys = {"status", "hit_step_cap", "steps_used", "files_changed", "responses", "error"}
        self.assertEqual(set(fix), keys)
        self.assertEqual((fix["status"], fix["hit_step_cap"]), ("completed", False))
        self.assertEqual(fix["responses"][0]["finding_id"], "F1-1")
        usage = artifacts[0]["metadata"]["usage"]
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 5})

    def test_step_limit_completes_and_refusal_fails(self) -> None:
        coder, message = fix_request(("step_limit",))
        fix = coder_handler(coder)(message)[0]["parts"][0]["data"]
        self.assertEqual((fix["status"], fix["hit_step_cap"]), ("step_limit", True))
        for status in ("refused", "error"):
            with self.subTest(status=status):
                coder, message = fix_request((status,))
                with self.assertRaisesRegex(SkillFailed, status):
                    coder_handler(coder)(message)

    def test_other_skills_are_rejected(self) -> None:
        coder, message = fix_request()
        message["metadata"]["skillId"] = "review-program"
        with self.assertRaises(SkillRejected):
            coder_handler(coder)(message)


if __name__ == "__main__":
    unittest.main()
