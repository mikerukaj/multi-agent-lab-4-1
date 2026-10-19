"""Tests for the A2A 1.0 types, builders and validators (contracts/a2a-binding.md §2, §5)."""

import re
import unittest

import messaging
from messaging.a2a import (
    A2A_VERSION,
    BINDING_URN,
    ERROR_CODES,
    A2AError,
    agent_card,
    agent_skill,
    artifact,
    data_part,
    new_task,
    now_timestamp,
    rpc_error,
    rpc_request,
    text_part,
    user_message,
    validate_envelope,
    validate_message,
)

TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def contains_key(value, key: str) -> bool:
    """True if ``key`` appears as a dict key anywhere inside ``value``."""
    if isinstance(value, dict):
        return key in value or any(contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(contains_key(item, key) for item in value)
    return False


def sample_message(**overrides) -> dict:
    message = user_message(
        [text_part("Please review."), data_part({"iteration": 1})],
        context_id="run-1",
        metadata={"skillId": "review-program", "sender": "coder"},
        reference_task_ids=["t0"],
    )
    message.update(overrides)
    return message


class BuilderTests(unittest.TestCase):
    def test_parts_hold_exactly_one_content_member(self) -> None:
        self.assertEqual(text_part("hi"), {"text": "hi", "mediaType": "text/plain"})
        self.assertEqual(data_part({"a": 1}), {"data": {"a": 1}, "mediaType": "application/json"})

    def test_user_message_shape(self) -> None:
        message = sample_message()
        self.assertRegex(message["messageId"], r"^[0-9a-f]{32}$")
        self.assertEqual(message["role"], "ROLE_USER")
        self.assertEqual(message["contextId"], "run-1")
        self.assertEqual(message["metadata"], {"skillId": "review-program", "sender": "coder"})
        self.assertEqual(message["referenceTaskIds"], ["t0"])

    def test_no_kind_member_anywhere(self) -> None:
        task = new_task("run-1")
        card = agent_card("qa-debugger", "QA", [agent_skill("s", "S", "Does S.", ["x"])], "1")
        for value in (sample_message(), task, card, artifact("a", [data_part({})])):
            self.assertFalse(contains_key(value, "kind"))

    def test_new_task_starts_submitted_with_an_iso_timestamp(self) -> None:
        task = new_task("run-1")
        self.assertRegex(task["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(task["contextId"], "run-1")
        self.assertEqual(task["status"]["state"], "TASK_STATE_SUBMITTED")
        self.assertRegex(task["status"]["timestamp"], TIMESTAMP)
        self.assertRegex(now_timestamp(), TIMESTAMP)

    def test_version_constant(self) -> None:
        self.assertEqual(messaging.__version__, "0.3.0")
        self.assertEqual(A2A_VERSION, "1.0")

    def test_agent_card_has_every_required_field(self) -> None:
        card = agent_card("qa-debugger", "QA", [agent_skill("s", "S", "Does S.", ["x"])], "0.3.0")
        for key in (
            "name", "description", "supportedInterfaces", "version", "capabilities",
            "defaultInputModes", "defaultOutputModes", "skills",
        ):
            self.assertIn(key, card)
        interface = card["supportedInterfaces"][0]
        self.assertEqual(interface["protocolBinding"], BINDING_URN)
        self.assertEqual(interface["protocolVersion"], "1.0")
        self.assertEqual(interface["url"], "broker://qa-debugger")
        self.assertEqual(
            card["capabilities"],
            {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        )
        self.assertEqual(
            set(card["skills"][0]), {"id", "name", "description", "tags"}
        )


class ValidatorTests(unittest.TestCase):
    def assert_invalid(self, message) -> None:
        with self.assertRaises(A2AError) as caught:
            validate_message(message)
        self.assertEqual(caught.exception.code, -32602)

    def test_valid_message_passes(self) -> None:
        validate_message(sample_message())

    def test_rejects_bad_messages(self) -> None:
        self.assert_invalid(sample_message(messageId=""))
        self.assert_invalid({k: v for k, v in sample_message().items() if k != "messageId"})
        self.assert_invalid(sample_message(parts=[]))
        self.assert_invalid(sample_message(parts=[{"text": "a", "data": {}}]))
        self.assert_invalid(sample_message(parts=[{"text": "a", "kind": "text"}]))
        self.assert_invalid(sample_message(role="user"))
        self.assert_invalid(sample_message(kind="message"))
        self.assert_invalid("not a message")

    def test_envelope_validation(self) -> None:
        envelope = {
            "sender": "coder",
            "recipient": "qa-debugger",
            "serviceParameters": {"A2A-Version": "1.0"},
            "rpc": rpc_request("GetTask", {"id": "x"}),
        }
        validate_envelope(envelope)
        for key in ("sender", "recipient", "rpc"):
            with self.subTest(missing=key), self.assertRaises(A2AError):
                validate_envelope({k: v for k, v in envelope.items() if k != key})
        with self.assertRaises(A2AError):
            validate_envelope({**envelope, "rpc": {**envelope["rpc"], "jsonrpc": "1.0"}})


class ErrorTests(unittest.TestCase):
    def test_error_codes_match_the_contract(self) -> None:
        expected = {
            "TaskNotFoundError": -32001,
            "TaskNotCancelableError": -32002,
            "PushNotificationNotSupportedError": -32003,
            "UnsupportedOperationError": -32004,
            "ContentTypeNotSupportedError": -32005,
            "InvalidAgentResponseError": -32006,
            "ExtendedAgentCardNotConfiguredError": -32007,
            "ExtensionSupportRequiredError": -32008,
            "VersionNotSupportedError": -32009,
            "InvalidParams": -32602,
            "UnknownAgent": -32602,
            "MethodNotFound": -32601,
        }
        for name, code in expected.items():
            self.assertEqual(ERROR_CODES[name], code, name)

    def test_rpc_error_shape(self) -> None:
        response = rpc_error("r1", A2AError("TaskNotFoundError", "no task t9"))
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], "r1")
        self.assertEqual(response["error"]["code"], -32001)
        self.assertIsInstance(response["error"]["message"], str)
        self.assertEqual(
            response["error"]["data"], {"type": "TaskNotFoundError", "detail": "no task t9"}
        )

    def test_error_round_trips_from_a_reply(self) -> None:
        error = A2AError.from_rpc(rpc_error("r1", A2AError("UnknownAgent", "nobody"))["error"])
        self.assertEqual(
            (error.code, error.type_name, error.detail), (-32602, "UnknownAgent", "nobody")
        )


if __name__ == "__main__":
    unittest.main()
