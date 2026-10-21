"""Tests for ``agents.llm.ModelCaller``, the single path every model call takes."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict
from unittest import mock

from agents.llm import ModelCaller, parse_reply
from agents.pm_agent import PMAgent, PMConfig
from orchestration.services import RunServices
from resilience.response_cache import ResponseCache
from tests import TEST_ENV
from tests.fakes import scripted_claude as sc


class Reply(TypedDict):
    answer: str
    count: int


REQUEST = {"model": "claude-haiku-5-5", "max_tokens": 10, "messages": [
    {"role": "user", "content": "hi"}
]}


def caller(steps: list) -> tuple[ModelCaller, sc.ScriptedClaude]:
    client = sc.ScriptedClaude(steps)
    services = RunServices.create("run-test", env=TEST_ENV, response_cache=None)
    return ModelCaller("pm", client, services), client


class FoundationTests(unittest.TestCase):
    def test_a_json_reply_is_parsed_and_validated(self) -> None:
        model, _ = caller([sc.text_reply({"answer": "yes", "count": 2})])
        reply = model.call(dict(REQUEST), Reply)
        self.assertEqual(reply.data, {"answer": "yes", "count": 2})
        self.assertEqual(reply.problems, [])
        self.assertIsNone(reply.refusal)
        self.assertIsNone(reply.failure)
        self.assertEqual(reply.served_from, "live")

    def test_a_refusal_is_reported_and_never_validated(self) -> None:
        model, _ = caller([sc.refusal_reply("cyber", "No thanks.")])
        reply = model.call(dict(REQUEST), Reply)
        self.assertIsNone(reply.data)
        self.assertEqual(reply.problems, [])
        self.assertIn("category: cyber", reply.refusal)
        self.assertIn("No thanks.", reply.refusal)

    def test_invalid_json_is_a_problem(self) -> None:
        model, _ = caller([sc.text_reply("not json")])
        reply = model.call(dict(REQUEST), Reply)
        self.assertIsNone(reply.data)
        self.assertEqual(len(reply.problems), 1)
        self.assertIn("not valid JSON", reply.problems[0])

    def test_a_reply_failing_validation_has_problems_and_no_data(self) -> None:
        model, _ = caller([sc.text_reply({"answer": "yes", "count": "2", "extra": 1})])
        reply = model.call(dict(REQUEST), Reply)
        self.assertIsNone(reply.data)
        self.assertEqual(
            reply.problems, ["$.count: expected int, got str", "$: unknown key 'extra'"]
        )
        self.assertEqual(reply.text, '{"answer": "yes", "count": "2", "extra": 1}')

    def test_without_a_reply_type_the_raw_response_is_returned(self) -> None:
        response = sc.tool_use_reply([("read_file", {"path": "a.py", "purpose": "p"})])
        model, _ = caller([response])
        reply = model.call(dict(REQUEST))
        self.assertIs(reply.response, response)
        self.assertIsNone(reply.data)
        self.assertEqual(reply.problems, [])

    def test_the_request_is_passed_through_unchanged(self) -> None:
        model, client = caller([sc.text_reply({"answer": "a", "count": 1})])
        model.call(dict(REQUEST), Reply)
        self.assertEqual(client.requests, [REQUEST])

    def test_usage_has_all_four_counters(self) -> None:
        model, _ = caller([sc.text_reply({"answer": "a", "count": 1},
                                         usage_block=sc.usage(7, 3, 2, 5))])
        self.assertEqual(
            model.call(dict(REQUEST), Reply).usage,
            {"input_tokens": 7, "output_tokens": 3,
             "cache_creation_input_tokens": 2, "cache_read_input_tokens": 5},
        )

    def test_missing_cache_attributes_count_as_zero(self) -> None:
        legacy = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"answer": "a", "count": 1}')],
            stop_reason="end_turn", stop_details=None, model="m",
            usage=SimpleNamespace(input_tokens=4, output_tokens=1),
        )
        model, _ = caller([legacy])
        self.assertEqual(
            model.call(dict(REQUEST), Reply).usage,
            {"input_tokens": 4, "output_tokens": 1,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        )

    def test_parse_reply_joins_text_blocks(self) -> None:
        response = sc.message([{"type": "text", "text": '{"answer": "a",'},
                               {"type": "text", "text": ' "count": 1}'}])
        self.assertEqual(parse_reply(response, Reply), ({"answer": "a", "count": 1},
                                                         '{"answer": "a", "count": 1}', []))



class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class DegradationTests(unittest.TestCase):
    """When the model service is unavailable the call degrades and never raises."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.events: list[dict] = []
        self.services = RunServices.create(
            "run-test", env=TEST_ENV, sleep=lambda delay: None, clock=Clock(),
            response_cache=ResponseCache(Path(self._tmp.name), ttl_seconds=3600),
            on_event=self.events.append,
        )

    def caller(self, steps: list) -> tuple[ModelCaller, sc.ScriptedClaude]:
        client = sc.ScriptedClaude(steps)
        return ModelCaller("pm", client, self.services), client

    def open_the_circuit(self, model: ModelCaller, client: sc.ScriptedClaude) -> None:
        client.extend([sc.overloaded_error()] * 12)
        for _ in range(3):
            reply = model.call({**REQUEST, "messages": [{"role": "user", "content": "other"}]},
                               Reply)
            self.assertEqual(reply.failure["kind"], "temporary")
        self.assertEqual(self.services.breaker("anthropic", "claude-haiku-5-5").state, "open")

    def test_three_calls_that_exhaust_retries_open_the_circuit(self) -> None:
        model, client = self.caller([])
        self.open_the_circuit(model, client)
        self.assertEqual(len(client.requests), 12)

    def test_an_open_circuit_serves_a_validated_cached_reply_at_no_cost(self) -> None:
        model, client = self.caller([sc.text_reply({"answer": "cached", "count": 1})])
        self.assertEqual(model.call(dict(REQUEST), Reply).served_from, "live")
        self.open_the_circuit(model, client)
        sent = len(client.requests)
        reply = model.call(dict(REQUEST), Reply)
        self.assertEqual(len(client.requests), sent)
        self.assertEqual(reply.served_from, "cache")
        self.assertEqual(reply.data, {"answer": "cached", "count": 1})
        self.assertIsNone(reply.failure)
        self.assertEqual(sum(reply.usage.values()), 0)
        self.assertEqual(self.events[-1]["event"], "degraded")
        self.assertEqual(self.events[-1]["served_from"], "cache")

    def test_without_a_cached_reply_the_failure_is_returned(self) -> None:
        model, client = self.caller([])
        self.open_the_circuit(model, client)
        reply = model.call(dict(REQUEST), Reply)
        self.assertIsNone(reply.response)
        self.assertEqual(reply.failure["dependency"], "anthropic")
        self.assertEqual(reply.failure["target"], "claude-haiku-5-5")
        self.assertEqual(reply.failure["circuit"], "open")
        self.assertEqual(reply.failure["attempts"], 0)

    def test_a_cached_reply_that_no_longer_validates_is_discarded(self) -> None:
        model, client = self.caller([sc.text_reply({"answer": "a", "count": 1})])
        model.call(dict(REQUEST), Reply)
        self.open_the_circuit(model, client)

        class Stricter(TypedDict):
            answer: str
            count: int
            reason: str

        reply = model.call(dict(REQUEST), Stricter)
        self.assertIsNotNone(reply.failure)
        self.assertIsNone(self.services.response_cache.get(REQUEST))

    def test_a_permanent_failure_is_returned_after_one_attempt(self) -> None:
        model, client = self.caller([sc.auth_error()])
        reply = model.call(dict(REQUEST), Reply)
        self.assertEqual((reply.failure["kind"], reply.failure["attempts"]), ("permanent", 1))
        self.assertEqual(len(client.requests), 1)

    def test_a_temporary_failure_then_success_is_retried_transparently(self) -> None:
        model, client = self.caller([sc.rate_limit_error(), sc.text_reply({"answer": "a",
                                                                            "count": 1})])
        reply = model.call(dict(REQUEST), Reply)
        self.assertEqual(reply.data, {"answer": "a", "count": 1})
        self.assertEqual([event["event"] for event in self.events], ["retry"])

    def test_default_clients_have_the_sdk_retries_turned_off(self) -> None:
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "dummy-key-for-tests"}):
            pm = PMAgent(PMConfig())
        self.assertEqual(pm._caller._client.max_retries, 0)


if __name__ == "__main__":
    unittest.main()
