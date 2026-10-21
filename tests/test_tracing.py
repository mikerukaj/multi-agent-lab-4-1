"""Tests for ``observability.tracing.Tracer`` (contracts/tracing.md, FR-214a)."""

import threading
import unittest
from unittest import mock

from observability.redaction import MARKER, Redactor
from observability.tracing import Tracer
from tests.fakes.recording_tracer import recording_tracer

SECRET = "sk-ant-api03-tracingsecret0123456789"


class DisabledTracerTests(unittest.TestCase):
    def test_without_the_switch_and_a_key_nothing_is_created_or_sent(self) -> None:
        for env in ({}, {"LANGSMITH_TRACING": "true"}, {"LANGSMITH_API_KEY": "k" * 20},
                    {"LANGSMITH_TRACING": "false", "LANGSMITH_API_KEY": "k" * 20}):
            with self.subTest(env=env), mock.patch("langsmith.Client") as client:
                tracer = Tracer.from_env(env, Redactor([]))
                self.assertFalse(tracer.enabled)
                client.assert_not_called()

    def test_a_disabled_tracer_still_measures(self) -> None:
        tracer = Tracer(False)
        with tracer.root_span("r", {}):
            with tracer.agent_span("coder", "run") as stats:
                tracer.add_tokens(12)
                with tracer.tool_span("read_file", {"path": "a.py"}):
                    pass
        self.assertEqual((stats.tokens, stats.tool_calls), (12, 1))
        self.assertGreaterEqual(stats.duration_ms, 0)
        self.assertEqual(tracer.headers(), {})
        self.assertIsNone(tracer.root_url())

    def test_with_the_switch_and_a_key_the_client_hides_secrets(self) -> None:
        env = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "lsv2_" + "k" * 20,
               "LANGSMITH_PROJECT": "lab"}
        redactor = Redactor.from_env(env)
        with mock.patch("langsmith.Client") as client:
            tracer = Tracer.from_env(env, redactor)
        self.assertTrue(tracer.enabled)
        self.assertEqual(tracer.project, "lab")
        options = client.call_args.kwargs
        for key in ("hide_inputs", "hide_outputs", "hide_metadata"):
            self.assertEqual(options[key](f"key {SECRET}"), f"key {MARKER}")


class SpanTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.warnings: list[str] = []
        self.tracer, self.recorder = recording_tracer(
            Redactor.from_env({"ANTHROPIC_API_KEY": SECRET}), self.warnings.append
        )

    def run_pm_and_coder(self) -> None:
        with self.tracer.root_span("run-1", {"attempt": 1}):
            with self.tracer.agent_span("pm", "write_spec"):
                with self.tracer.llm_span("claude-sonnet-5-5", {"model": "x"}) as span:
                    span.outputs = {"usage_metadata": {"input_tokens": 10, "output_tokens": 5,
                                                       "total_tokens": 15}}
                self.tracer.add_tokens(15)
            with self.tracer.agent_span("coder", "run", task_id="T1"):
                with self.tracer.llm_span("claude-haiku-5-5", {"model": "y"}):
                    pass
                self.tracer.add_tokens(7)
                for path in ("a.py", "b.py"):
                    with self.tracer.tool_span("write_file", {"path": path}):
                        pass

    def test_one_root_with_agent_children_and_llm_and_tool_grandchildren(self) -> None:
        self.run_pm_and_coder()
        roots = self.recorder.roots()
        self.assertEqual([root["name"] for root in roots], ["team-run"])
        self.assertEqual(roots[0]["metadata"]["run_id"], "run-1")
        for span in self.recorder.named("agent:"):
            self.assertEqual(self.recorder.ancestors(span), ["team-run"])
        for span in self.recorder.named("llm:") + self.recorder.named("tool:"):
            self.assertEqual(len(self.recorder.ancestors(span)), 2)
            self.assertTrue(self.recorder.ancestors(span)[0].startswith("agent:"))
        trace_ids = {span["trace_id"] for span in self.recorder.spans.values()}
        self.assertEqual(len(trace_ids), 1)

    def test_agent_spans_carry_the_four_metadata_keys(self) -> None:
        self.run_pm_and_coder()
        spans = {span["name"]: span["metadata"] for span in self.recorder.named("agent:")}
        self.assertEqual((spans["agent:pm"]["agent_name"], spans["agent:pm"]["token_count"],
                          spans["agent:pm"]["tool_calls"]), ("pm", 15, 0))
        self.assertEqual((spans["agent:coder"]["token_count"],
                          spans["agent:coder"]["tool_calls"],
                          spans["agent:coder"]["task_id"]), (7, 2, "T1"))
        for metadata in spans.values():
            self.assertIsInstance(metadata["duration_ms"], int)
            self.assertIsInstance(metadata["token_count"], int)

    def test_llm_spans_carry_usage_metadata(self) -> None:
        self.run_pm_and_coder()
        llm = next(s for s in self.recorder.named("llm:") if "sonnet" in s["name"])
        self.assertEqual(llm["outputs"]["usage_metadata"]["total_tokens"], 15)
        self.assertEqual(llm["metadata"]["ls_provider"], "anthropic")

    def test_inputs_outputs_and_metadata_are_redacted(self) -> None:
        with self.tracer.root_span("run-1", {"note": SECRET}):
            with self.tracer.agent_span("pm", "write_spec", detail=SECRET):
                with self.tracer.llm_span("m", {"prompt": f"key {SECRET}"}) as span:
                    span.outputs = {"text": SECRET}
                    span.error = f"failed {SECRET}"
        self.assertNotIn(SECRET, self.recorder.text())
        self.assertIn(MARKER, self.recorder.text())

    def test_headers_continue_the_trace_on_another_thread(self) -> None:
        seen = {}
        with self.tracer.root_span("run-1", {}):
            headers = self.tracer.headers()

            def broker_thread():
                with self.tracer.continue_from(headers):
                    with self.tracer.agent_span("qa", "review", iteration=1):
                        pass
            thread = threading.Thread(target=broker_thread)
            thread.start()
            thread.join()
        self.assertTrue(all(isinstance(value, str) for value in headers.values()))
        qa = self.recorder.named("agent:qa")[0]
        root = self.recorder.roots()[0]
        self.assertEqual(qa["trace_id"], root["trace_id"])
        self.assertEqual(self.recorder.ancestors(qa), ["team-run"])

    def test_malformed_headers_give_a_parentless_span_and_one_warning(self) -> None:
        with self.tracer.continue_from({"langsmith-trace": "garbage"}):
            with self.tracer.agent_span("qa", "review"):
                pass
        with self.tracer.continue_from({"langsmith-trace": "also-garbage"}):
            pass
        qa = self.recorder.named("agent:qa")[0]
        self.assertIsNone(qa["parent"])
        self.assertEqual(len(self.warnings), 1)

    def test_a_failing_client_never_fails_the_run(self) -> None:
        self.recorder.client.create_run.side_effect = ConnectionError("LangSmith is down")
        with self.tracer.root_span("run-1", {}):
            with self.tracer.agent_span("pm", "write_spec") as stats:
                self.tracer.add_tokens(3)
        self.assertEqual(stats.tokens, 3)
        self.assertEqual(len(self.warnings), 1)


if __name__ == "__main__":
    unittest.main()
