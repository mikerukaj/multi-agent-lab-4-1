"""Tests for the A2A asyncio broker and client (contracts/a2a-binding.md §1, §4, §5, §10).

The handlers here are tiny synchronous functions; no agents, model or Docker.
"""

import threading
import unittest

from messaging.a2a import (
    A2AError,
    A2ATimeoutError,
    BrokerClosedError,
    SkillFailed,
    SkillRejected,
    agent_card,
    agent_skill,
    artifact,
    data_part,
    rpc_request,
    text_part,
    user_message,
)
from messaging.broker import A2ABroker

SKILL = "echo"


def card(name: str = "echoer") -> dict:
    skills = [agent_skill(SKILL, "Echo", "Echo.", ["t"])]
    return agent_card(name, "Echoes its data part.", skills, "1")


def message(value=1, skill: str = SKILL, context: str = "run-1") -> dict:
    return user_message(
        [text_part("hi"), data_part({"value": value})],
        context_id=context,
        metadata={"skillId": skill, "sender": "tester"},
    )


def echo(msg: dict) -> list[dict]:
    return [artifact("echo", [msg["parts"][1]])]


class BrokerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = A2ABroker(request_timeout_seconds=5)
        self.broker.__enter__()
        self.addCleanup(self.broker.close)
        self.client = self.broker.client("tester")


class RoutingTests(BrokerTestCase):
    def test_send_message_completes_with_the_handlers_artifacts(self) -> None:
        self.broker.register(card(), echo)
        task = self.client.send_message("echoer", message(7))
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(task["contextId"], "run-1")
        self.assertEqual(task["artifacts"][0]["parts"][0]["data"], {"value": 7})

    def test_messages_to_one_recipient_are_handled_in_order(self) -> None:
        seen = []

        def record(msg: dict) -> list[dict]:
            seen.append(msg["parts"][1]["data"]["value"])
            return echo(msg)

        self.broker.register(card(), record)
        ids = [
            self.client.send_message("echoer", message(n), {"returnImmediately": True})["id"]
            for n in range(5)
        ]
        for task_id in ids:
            self.wait_terminal("echoer", task_id)
        self.assertEqual(seen, [0, 1, 2, 3, 4])

    def test_get_task_reports_working_while_the_handler_runs(self) -> None:
        started, release = threading.Event(), threading.Event()

        def slow(msg: dict) -> list[dict]:
            started.set()
            release.wait(5)
            return echo(msg)

        self.broker.register(card(), slow)
        task = self.client.send_message("echoer", message(), {"returnImmediately": True})
        self.assertEqual(task["status"]["state"], "TASK_STATE_SUBMITTED")
        self.assertTrue(started.wait(5))
        self.assertEqual(
            self.client.get_task("echoer", task["id"])["status"]["state"], "TASK_STATE_WORKING"
        )
        release.set()
        self.assertEqual(self.wait_terminal("echoer", task["id"]), "TASK_STATE_COMPLETED")

    def test_payloads_are_copied_at_the_boundary(self) -> None:
        def mutate(msg: dict) -> list[dict]:
            msg["parts"][1]["data"]["value"] = "changed"
            return echo(msg)

        self.broker.register(card(), mutate)
        sent = message(1)
        task = self.client.send_message("echoer", sent)
        self.assertEqual(sent["parts"][1]["data"], {"value": 1})
        task["status"]["state"] = "TASK_STATE_FAILED"
        stored = self.client.get_task("echoer", task["id"])
        self.assertEqual(stored["status"]["state"], "TASK_STATE_COMPLETED")

    def test_list_tasks_filters_by_context(self) -> None:
        self.broker.register(card(), echo)
        self.client.send_message("echoer", message(context="a"))
        self.client.send_message("echoer", message(context="b"))
        listed = self.client.list_tasks("echoer", context_id="a")
        self.assertEqual([task["contextId"] for task in listed["tasks"]], ["a"])
        self.assertEqual(listed["totalSize"], 1)

    def wait_terminal(self, recipient: str, task_id: str) -> str:
        for _ in range(200):
            state = self.client.get_task(recipient, task_id)["status"]["state"]
            if state not in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"):
                return state
            threading.Event().wait(0.02)
        self.fail("task did not finish")


class OutcomeTests(BrokerTestCase):
    def test_unknown_skill_is_rejected(self) -> None:
        self.broker.register(card(), echo)
        task = self.client.send_message("echoer", message(skill="nope"))
        self.assertEqual(task["status"]["state"], "TASK_STATE_REJECTED")
        self.assertIn("nope", task["status"]["message"]["parts"][0]["text"])

    def test_skill_rejected_and_failed_map_to_task_states(self) -> None:
        def reject(msg: dict) -> list[dict]:
            raise SkillRejected("bad payload")

        def fail(msg: dict) -> list[dict]:
            raise SkillFailed("model refused")

        def crash(msg: dict) -> list[dict]:
            raise ZeroDivisionError("boom")

        for name, handler, state, text in (
            ("r", reject, "TASK_STATE_REJECTED", "bad payload"),
            ("f", fail, "TASK_STATE_FAILED", "model refused"),
            ("c", crash, "TASK_STATE_FAILED", "ZeroDivisionError: boom"),
        ):
            with self.subTest(name=name):
                self.broker.register(card(name), handler)
                task = self.client.send_message(name, message())
                self.assertEqual(task["status"]["state"], state)
                self.assertEqual(task["status"]["message"]["parts"][0]["text"], text)
                self.assertEqual(task["status"]["message"]["role"], "ROLE_AGENT")


class ErrorTests(BrokerTestCase):
    def assert_error(self, code: int, call, type_name: str | None = None) -> None:
        with self.assertRaises(A2AError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        if type_name:
            self.assertEqual(caught.exception.type_name, type_name)

    def test_unknown_recipient(self) -> None:
        self.assert_error(
            -32602, lambda: self.client.send_message("nobody", message()), "UnknownAgent"
        )

    def test_protocol_errors(self) -> None:
        self.broker.register(card(), echo)

        def raw(method: str, version: str = "1.0") -> dict:
            return self.broker.request({
                "sender": "tester",
                "recipient": "echoer",
                "serviceParameters": {"A2A-Version": version},
                "rpc": rpc_request(method, {"id": "x"}),
            })

        cases = [
            (raw("GetTask", version="0.3"), -32009),
            (raw("NoSuchMethod"), -32601),
            (raw("SendStreamingMessage"), -32004),
            (raw("SubscribeToTask"), -32004),
            (raw("CreateTaskPushNotificationConfig"), -32003),
            (raw("GetExtendedAgentCard"), -32007),
            (raw("GetTask"), -32001),
        ]
        for response, code in cases:
            with self.subTest(code=code):
                self.assertEqual(response["error"]["code"], code)

    def test_cancel_queued_task_and_refuse_working_one(self) -> None:
        started, release = threading.Event(), threading.Event()
        ran = []

        def slow(msg: dict) -> list[dict]:
            ran.append(msg["parts"][1]["data"]["value"])
            started.set()
            release.wait(5)
            return echo(msg)

        self.broker.register(card(), slow)
        working = self.client.send_message("echoer", message(1), {"returnImmediately": True})
        self.assertTrue(started.wait(5))
        queued = self.client.send_message("echoer", message(2), {"returnImmediately": True})
        canceled = self.client.cancel_task("echoer", queued["id"])
        self.assertEqual(canceled["status"]["state"], "TASK_STATE_CANCELED")
        self.assert_error(-32002, lambda: self.client.cancel_task("echoer", working["id"]))
        release.set()
        RoutingTests.wait_terminal(self, "echoer", working["id"])
        self.assertEqual(ran, [1])

    def test_timeout_names_the_recipient(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        self.broker.register(card(), lambda msg: release.wait(5) and echo(msg))
        client = A2ABroker.client(self.broker, "tester")
        client.timeout_seconds = 0.2
        with self.assertRaises(A2ATimeoutError) as caught:
            client.send_message("echoer", message())
        self.assertIn("echoer did not answer within 0.2 s", str(caught.exception))


class LogTests(BrokerTestCase):
    def test_every_exchange_is_logged_and_copies_are_isolated(self) -> None:
        self.broker.register(card(), echo)
        other = self.broker.client("other")
        task = self.client.send_message("echoer", message())
        other.get_task("echoer", task["id"])
        log = self.client.exchanges()
        self.assertEqual(log, self.broker.exchanges())
        self.assertEqual([entry["sequence"] for entry in log], [1, 2])
        first = log[0]
        self.assertEqual(
            (first["sender"], first["recipient"], first["method"], first["skill_id"]),
            ("tester", "echoer", "SendMessage", SKILL),
        )
        self.assertEqual((first["context_id"], first["task_id"]), ("run-1", task["id"]))
        self.assertEqual(
            [s["state"] for s in first["states"]],
            ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED"],
        )
        self.assertEqual(first["outcome"], "TASK_STATE_COMPLETED")
        self.assertTrue(first["started_at"].endswith("Z") and first["finished_at"].endswith("Z"))
        self.assertEqual(log[1]["sender"], "other")
        log.clear()
        self.assertEqual(len(self.client.exchanges()), 2)

    def test_error_outcome_is_recorded(self) -> None:
        with self.assertRaises(A2AError):
            self.client.get_task("nobody", "x")
        self.assertEqual(self.broker.exchanges()[0]["outcome"], "error:-32602")


class CloseTests(unittest.TestCase):
    def test_close_fails_pending_requests(self) -> None:
        broker = A2ABroker(request_timeout_seconds=5)
        broker.__enter__()
        release = threading.Event()
        broker.register(card(), lambda msg: release.wait(5) and echo(msg))
        client = broker.client("tester")
        errors = []

        def send() -> None:
            try:
                client.send_message("echoer", message())
            except A2AError as error:
                errors.append(error)

        sender = threading.Thread(target=send)
        sender.start()
        threading.Event().wait(0.2)
        broker.close()
        release.set()
        sender.join(5)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], BrokerClosedError)
        with self.assertRaises(BrokerClosedError):
            client.send_message("echoer", message())


if __name__ == "__main__":
    unittest.main()
