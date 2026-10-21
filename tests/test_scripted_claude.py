"""Checks that ``ScriptedClaude`` replies and errors look exactly like the real SDK's."""

import unittest

import anthropic

from tests.fakes import scripted_claude as sc


class ScriptedClaudeTests(unittest.TestCase):
    def test_text_reply_parses_like_a_live_message(self) -> None:
        client = sc.ScriptedClaude([sc.text_reply({"ok": True})])
        response = client.beta.messages.create(model="m", messages=[])
        self.assertEqual(response.content[0].type, "text")
        self.assertEqual(response.content[0].text, '{"ok": true}')
        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(response.usage.input_tokens, 100)
        self.assertEqual(response.usage.output_tokens, 50)
        self.assertEqual(response.usage.cache_read_input_tokens, 0)

    def test_tool_use_reply_carries_each_call(self) -> None:
        reply = sc.tool_use_reply([("write_file", {"path": "a.py"})], thought="Writing.")
        self.assertEqual(reply.stop_reason, "tool_use")
        self.assertEqual([block.type for block in reply.content], ["text", "tool_use"])
        self.assertEqual(reply.content[1].name, "write_file")
        self.assertEqual(reply.content[1].input, {"path": "a.py"})

    def test_refusal_reply_has_stop_details(self) -> None:
        reply = sc.refusal_reply("bio", "No.")
        self.assertEqual(reply.stop_reason, "refusal")
        self.assertEqual(reply.stop_details.category, "bio")

    def test_errors_are_real_sdk_exceptions(self) -> None:
        self.assertIsInstance(sc.rate_limit_error(2), anthropic.RateLimitError)
        self.assertEqual(sc.rate_limit_error(2).response.headers["retry-after"], "2")
        self.assertEqual(sc.overloaded_error().status_code, 529)
        self.assertIsInstance(sc.connection_error(), anthropic.APIConnectionError)
        self.assertEqual(sc.auth_error().status_code, 401)
        self.assertEqual(sc.bad_request_error().status_code, 400)

    def test_steps_replay_in_order_and_requests_are_recorded(self) -> None:
        client = sc.ScriptedClaude([
            sc.rate_limit_error(), lambda request: sc.text_reply(request["model"])
        ])
        request = {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "x"}]}
        with self.assertRaises(anthropic.RateLimitError):
            client.beta.messages.create(**request)
        self.assertEqual(client.beta.messages.create(**request).content[0].text,
                         "claude-haiku-5-5")
        self.assertEqual(client.requests, [request, request])
        request["messages"].append("mutated")
        self.assertEqual(len(client.requests[0]["messages"]), 1)

    def test_running_past_the_script_fails_loudly(self) -> None:
        with self.assertRaisesRegex(AssertionError, "script exhausted"):
            sc.ScriptedClaude().beta.messages.create(model="m")


if __name__ == "__main__":
    unittest.main()
