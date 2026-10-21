"""Tests for failure classification, backoff, the circuit breaker and the guard.

See specs/003-production-hardening/contracts/resilience.md and research R4–R5.
Sleeps and clocks are injected, so nothing here waits for real.
"""

import random
import sqlite3
import subprocess
import threading
import unittest

import anthropic
import httpx2

from resilience.circuit import CircuitBreaker, CircuitSettings, DependencyGuard, GuardFailure
from resilience.classify import classify, is_unavailable, retry_after_seconds
from resilience.response_cache import ResponseCache
from resilience.retry import RetryPolicy, backoff_delay
from tests.fakes import scripted_claude as sc
from tools.mcp_client import McpStartupError, McpUnavailableError

REQUEST = httpx2.Request("POST", sc.API_URL)


def status_error(status: int) -> anthropic.APIStatusError:
    response = httpx2.Response(status, request=REQUEST)
    return anthropic.APIStatusError("error", response=response, body=None)


def docker_outcome(code: int, stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["docker", "run"], code, b"", stderr.encode())


class ClassificationTests(unittest.TestCase):
    def test_temporary_model_failures(self) -> None:
        errors = [
            sc.rate_limit_error(), sc.overloaded_error(), sc.server_error(500),
            sc.connection_error(), anthropic.APITimeoutError(request=REQUEST),
            status_error(408), status_error(409),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(classify(error, "anthropic")[:2], ("anthropic", "temporary"))

    def test_permanent_model_failures(self) -> None:
        for status in (400, 401, 403, 404, 413, 422):
            with self.subTest(status=status):
                self.assertEqual(
                    classify(status_error(status), "anthropic")[:2], ("anthropic", "permanent")
                )

    def test_mcp_failures(self) -> None:
        self.assertEqual(classify(McpUnavailableError("gone"), "mcp")[1], "temporary")
        self.assertEqual(classify(BrokenPipeError(), "mcp")[1], "temporary")
        self.assertEqual(classify(McpStartupError("bad workspace"), "mcp")[1], "permanent")
        self.assertIsNone(classify(ValueError("x"), "mcp"))

    def test_sandbox_outcomes(self) -> None:
        daemon = docker_outcome(125, "docker: Cannot connect to the Docker daemon at unix://")
        self.assertEqual(classify(daemon, "sandbox")[1], "temporary")
        self.assertIsNone(classify(docker_outcome(1, "Traceback: ValueError"), "sandbox"))
        self.assertIsNone(classify(docker_outcome(125, "my program chose 125"), "sandbox"))
        self.assertIsNone(classify(docker_outcome(0, ""), "sandbox"))

    def test_chroma_failures(self) -> None:
        self.assertEqual(classify(ConnectionError("refused"), "chroma")[1], "temporary")
        locked = sqlite3.OperationalError("database is locked")
        self.assertEqual(classify(locked, "chroma")[1], "temporary")
        self.assertEqual(classify(ValueError("bad query"), "chroma")[1], "permanent")

    def test_retry_after_is_read_from_the_response(self) -> None:
        self.assertEqual(retry_after_seconds(sc.rate_limit_error(2)), 2.0)
        self.assertIsNone(retry_after_seconds(sc.rate_limit_error()))
        self.assertIsNone(retry_after_seconds(ValueError()))

    def test_is_unavailable(self) -> None:
        base = {"dependency": "anthropic", "target": "m", "attempts": 1, "circuit": "closed",
                "message": "", "retry_after_seconds": None}
        self.assertTrue(is_unavailable({**base, "kind": "temporary", "error_type": "X"}))
        self.assertFalse(is_unavailable({**base, "kind": "permanent", "error_type": "X"}))
        opened_by_this_call = {**base, "kind": "permanent", "error_type": "X", "circuit": "open"}
        self.assertFalse(is_unavailable(opened_by_this_call))
        refused = {**base, "kind": "temporary", "error_type": "circuit_open", "circuit": "open"}
        self.assertTrue(is_unavailable(refused))
        self.assertFalse(is_unavailable(None))


class BackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = RetryPolicy(max_retries=5, base_delay=1.0, max_delay=8.0)

    def test_delays_grow_with_equal_jitter_and_are_capped(self) -> None:
        rng = random.Random(0)
        delays = [backoff_delay(self.policy, attempt, None, rng) for attempt in range(6)]
        for attempt, delay in enumerate(delays):
            ceiling = min(8.0, 2**attempt)
            self.assertGreaterEqual(delay, ceiling / 2)
            self.assertLessEqual(delay, ceiling)
        self.assertLess(delays[0], delays[3])
        self.assertLessEqual(max(delays), 8.0)

    def test_retry_after_is_honoured_when_it_fits(self) -> None:
        delay = backoff_delay(self.policy, 0, 2.0, random.Random(0))
        self.assertGreaterEqual(delay, 2.0)

    def test_retry_after_above_the_cap_means_no_retry(self) -> None:
        self.assertIsNone(backoff_delay(self.policy, 0, 9.0, random.Random(0)))

    def test_settings_out_of_range_name_the_variable(self) -> None:
        with self.assertRaisesRegex(ValueError, "RETRY_MAX_RETRIES"):
            RetryPolicy.from_env({"RETRY_MAX_RETRIES": "9"})
        with self.assertRaisesRegex(ValueError, "CIRCUIT_COOLDOWN_SECONDS"):
            CircuitSettings.from_env({"CIRCUIT_COOLDOWN_SECONDS": "0"})
        with self.assertRaisesRegex(ValueError, "RETRY_BASE_DELAY_SECONDS"):
            RetryPolicy.from_env({"RETRY_BASE_DELAY_SECONDS": "fast"})
        self.assertEqual(RetryPolicy.from_env({}), RetryPolicy(3, 1.0, 60.0))
        self.assertEqual(CircuitSettings.from_env({}), CircuitSettings(3, 30.0))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.sleeps: list[float] = []
        self.events: list[dict] = []
        self.stop = threading.Event()
        self.changes: list[tuple] = []
        self.breaker = CircuitBreaker(
            "anthropic:m", threshold=3, cooldown_seconds=30, clock=self.clock,
            listener=lambda key, old, new: self.changes.append((old, new)),
        )
        self.guard = DependencyGuard(
            "anthropic", "m", self.breaker, RetryPolicy(), random.Random(0),
            self.sleeps.append, self.stop, self.events.append,
        )

    @staticmethod
    def failing(*errors):
        script = list(errors)

        def operation():
            item = script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return operation

    def fail_call(self) -> GuardFailure:
        result = self.guard.call(self.failing(*[sc.overloaded_error()] * 4))
        self.assertIsInstance(result, GuardFailure)
        return result


class GuardTests(GuardTestCase):
    def test_one_429_then_success_returns_the_result_after_one_retry(self) -> None:
        result = self.guard.call(self.failing(sc.rate_limit_error(), "ok"))
        self.assertEqual(result, "ok")
        self.assertEqual(len(self.sleeps), 1)
        self.assertEqual([event["event"] for event in self.events], ["retry"])
        self.assertEqual(self.events[0]["attempt"], 1)

    def test_four_temporary_failures_give_up_after_three_retries(self) -> None:
        failure = self.fail_call().failure
        self.assertEqual(failure["attempts"], 4)
        self.assertEqual(failure["kind"], "temporary")
        self.assertEqual(failure["dependency"], "anthropic")
        self.assertEqual(failure["target"], "m")
        self.assertEqual(failure["error_type"], "OverloadedError")
        self.assertEqual(len(self.sleeps), 3)

    def test_a_permanent_failure_is_not_retried(self) -> None:
        failure = self.guard.call(self.failing(sc.auth_error())).failure
        self.assertEqual((failure["attempts"], failure["kind"]), (1, "permanent"))
        self.assertEqual(self.sleeps, [])

    def test_retry_after_above_the_cap_stops_retrying(self) -> None:
        failure = self.guard.call(self.failing(sc.rate_limit_error(120))).failure
        self.assertEqual(failure["attempts"], 1)
        self.assertEqual(self.sleeps, [])

    def test_unrecognised_exceptions_propagate(self) -> None:
        with self.assertRaises(KeyError):
            self.guard.call(self.failing(KeyError("bug")))

    def test_a_stop_request_fails_without_calling(self) -> None:
        self.stop.set()
        calls = []
        failure = self.guard.call(lambda: calls.append(1)).failure
        self.assertEqual(failure["error_type"], "interrupted")
        self.assertEqual(calls, [])

    def test_a_stop_during_a_retry_wait_ends_as_interrupted(self) -> None:
        guard = DependencyGuard(
            "anthropic", "m", self.breaker, RetryPolicy(), random.Random(0),
            lambda delay: self.stop.set(), self.stop,
        )
        failure = guard.call(self.failing(sc.overloaded_error(), "ok")).failure
        self.assertEqual(failure["error_type"], "interrupted")
        self.assertEqual(self.breaker.consecutive_failures, 0)

    def test_a_returned_value_can_be_classified_as_a_failure(self) -> None:
        bad = docker_outcome(125, "docker: Error response from daemon: conflict")
        guard = DependencyGuard(
            "sandbox", "img", self.breaker, RetryPolicy(), random.Random(0),
            self.sleeps.append, self.stop,
        )
        good = docker_outcome(0, "")
        result = guard.call(
            self.failing(bad, good), classify_result=lambda r: classify(r, "sandbox")
        )
        self.assertIs(result, good)
        self.assertEqual(len(self.sleeps), 1)


class CircuitBreakerTests(GuardTestCase):
    def test_three_failed_calls_open_the_circuit(self) -> None:
        for _ in range(3):
            self.fail_call()
        self.assertEqual(self.breaker.state, "open")
        self.assertEqual(self.changes, [("closed", "open")])

    def test_an_open_circuit_fails_fast_without_calling(self) -> None:
        for _ in range(3):
            self.fail_call()
        self.clock.now += 10
        calls = []
        failure = self.guard.call(lambda: calls.append(1)).failure
        self.assertEqual(calls, [])
        self.assertEqual(failure["attempts"], 0)
        self.assertEqual(failure["circuit"], "open")
        self.assertEqual(failure["error_type"], "circuit_open")
        self.assertEqual(failure["retry_after_seconds"], 20.0)
        self.assertTrue(is_unavailable(failure))
        self.assertEqual(self.events[-1]["event"], "degraded")

    def test_a_success_in_closed_resets_the_count(self) -> None:
        self.fail_call()
        self.fail_call()
        self.guard.call(lambda: "ok")
        self.assertEqual(self.breaker.consecutive_failures, 0)
        self.fail_call()
        self.assertEqual(self.breaker.state, "closed")

    def test_after_the_cooldown_one_trial_closes_the_circuit_on_success(self) -> None:
        for _ in range(3):
            self.fail_call()
        self.clock.now += 30
        self.assertEqual(self.guard.call(lambda: "ok"), "ok")
        self.assertEqual(self.breaker.state, "closed")
        self.assertEqual(self.breaker.consecutive_failures, 0)
        self.assertEqual(
            self.changes, [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]
        )

    def test_a_failed_trial_reopens_with_a_new_cooldown(self) -> None:
        for _ in range(3):
            self.fail_call()
        first_opened = self.breaker.opened_at
        self.clock.now += 31
        self.fail_call()
        self.assertEqual(self.breaker.state, "open")
        self.assertGreater(self.breaker.opened_at, first_opened)
        self.assertEqual(self.changes[-1], ("half_open", "open"))

    def test_only_one_half_open_trial_is_admitted_at_a_time(self) -> None:
        for _ in range(3):
            self.fail_call()
        self.clock.now += 30
        inside, release = threading.Event(), threading.Event()
        results = {}

        def trial():
            def operation():
                inside.set()
                release.wait(5)
                return "trial"
            results["trial"] = self.guard.call(operation)

        thread = threading.Thread(target=trial)
        thread.start()
        inside.wait(5)
        results["second"] = self.guard.call(lambda: "second")
        release.set()
        thread.join(5)
        self.assertEqual(results["trial"], "trial")
        self.assertIsInstance(results["second"], GuardFailure)
        self.assertEqual(results["second"].failure["error_type"], "circuit_open")
        self.assertEqual(self.breaker.state, "closed")


if __name__ == "__main__":
    unittest.main()
