"""Tests for the Coder Agent's two-tier memory (agents/memory.py)."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agents import memory
from agents.memory import SemanticMemory, SlidingWindowBuffer, Turn
from resilience.circuit import CircuitBreaker, DependencyGuard
from resilience.retry import RetryPolicy


class SlidingWindowBufferTests(unittest.TestCase):
    def test_sixth_turn_evicts_the_oldest(self) -> None:
        window = SlidingWindowBuffer(max_turns=5)
        for i in range(6):
            window.add(Turn(task=f"task {i}", reply=f"reply {i}"))
        tasks = [turn.task for turn in window.get()]
        self.assertEqual(tasks, ["task 1", "task 2", "task 3", "task 4", "task 5"])

    def test_get_returns_a_copy(self) -> None:
        window = SlidingWindowBuffer(max_turns=2)
        window.add(Turn(task="a", reply="b"))
        window.get().clear()
        self.assertEqual(len(window.get()), 1)

    def test_rejects_non_positive_size(self) -> None:
        with self.assertRaises(ValueError):
            SlidingWindowBuffer(max_turns=0)


class SemanticMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.memory = SemanticMemory(Path(self._tmp.name) / "chroma")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_collection_returns_nothing(self) -> None:
        self.assertEqual(self.memory.query("anything", top_k=3, exclude_run_id="r0"), [])

    def test_retrieves_semantically_similar_record(self) -> None:
        self.memory.add("run-a", "task", "The user's name is Priya and she likes tea.")
        self.memory.add("run-a", "task", "Compute the area of a circle with radius 3.")
        results = self.memory.query("What is my name?", top_k=1, exclude_run_id="run-b")
        self.assertEqual(len(results), 1)
        self.assertIn("Priya", results[0][1])

    def test_excludes_the_given_run(self) -> None:
        self.memory.add("current", "observation", "Wrote fizzbuzz.py")
        self.memory.add("earlier", "observation", "Wrote fizzbuzz.py earlier")
        results = self.memory.query("fizzbuzz", top_k=5, exclude_run_id="current")
        self.assertEqual([doc for _, doc in results], ["Wrote fizzbuzz.py earlier"])

    def test_ids_are_unique_and_prefixed_by_run(self) -> None:
        first = self.memory.add("run-x", "task", "one")
        second = self.memory.add("run-x", "task", "two")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("run-x-"))

    def test_truncates_documents_to_2000_chars(self) -> None:
        self.memory.add("run-a", "observation", "z" * 5000)
        (_, document), = self.memory.query("z", top_k=1, exclude_run_id="run-b")
        self.assertEqual(len(document), 2000)

    def test_persists_across_instances(self) -> None:
        self.memory.add("run-a", "result", "Persisted fact about penguins.")
        reopened = SemanticMemory(Path(self._tmp.name) / "chroma")
        results = reopened.query("penguins", top_k=1, exclude_run_id="run-b")
        self.assertIn("penguins", results[0][1])



class ServerModeTests(unittest.TestCase):
    """``CHROMA_HOST`` switches the memory to a Chroma server (FR-230a, T077)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def open_store(self, **kwargs) -> tuple[SemanticMemory, mock.MagicMock, mock.MagicMock]:
        http, persistent = mock.MagicMock(), mock.MagicMock()
        collection = mock.MagicMock()
        collection.count.side_effect = range(100)
        for client in (http, persistent):
            client.return_value.get_or_create_collection.return_value = collection
        with mock.patch.object(memory.chromadb, "HttpClient", http), \
                mock.patch.object(memory.chromadb, "PersistentClient", persistent):
            store = SemanticMemory(Path(self._tmp.name) / "c", **kwargs)
        return store, http, persistent

    def test_a_host_uses_the_http_client(self) -> None:
        _, http, persistent = self.open_store(host="chroma", port=8000)
        self.assertEqual((http.call_args.kwargs["host"], http.call_args.kwargs["port"]),
                         ("chroma", 8000))
        persistent.assert_not_called()

    def test_without_a_host_the_embedded_store_is_used(self) -> None:
        _, http, persistent = self.open_store()
        http.assert_not_called()
        self.assertTrue(persistent.call_args.kwargs["path"].endswith("/c"))

    def test_both_modes_open_the_collection_the_same_way(self) -> None:
        _, http, _ = self.open_store(host="chroma")
        _, _, persistent = self.open_store()
        self.assertEqual(http.return_value.get_or_create_collection.call_args,
                         persistent.return_value.get_or_create_collection.call_args)

    def test_record_ids_are_the_run_id_and_a_number(self) -> None:
        store, _, _ = self.open_store(host="chroma")
        self.assertEqual(store.add("20261020-run", "task", "a"), "20261020-run-0")
        self.assertEqual(store.add("20261020-run", "task", "b"), "20261020-run-1")

    def test_the_coder_config_reads_chroma_host_and_port(self) -> None:
        from agents.coder_agent import CoderConfig
        with mock.patch.dict("os.environ", {"CHROMA_HOST": "chroma", "CHROMA_PORT": "8100"}):
            config = CoderConfig.from_env()
        self.assertEqual((config.chroma_host, config.chroma_port), ("chroma", 8100))
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertIsNone(CoderConfig().chroma_host)


class UnreachableClient:
    """A Chroma client whose server is down: every call raises ``ConnectionError``."""

    def __init__(self, *args, **kwargs) -> None:
        raise ConnectionError("Connection refused")


class FlakyCollection:
    """A collection whose calls fail with ``ConnectionError`` after it was opened."""

    def count(self) -> int:
        raise ConnectionError("Connection reset")


class DegradedMemoryTests(unittest.TestCase):
    """With a guard, memory failures degrade instead of crashing the run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.events: list[dict] = []
        self.sleeps: list[float] = []
        self.guard = DependencyGuard(
            "chroma", "store", CircuitBreaker("chroma:store"), RetryPolicy(),
            random.Random(0), self.sleeps.append, threading.Event(), self.events.append,
        )

    def test_an_unreachable_store_gives_no_hits_and_skips_writes(self) -> None:
        with mock.patch.object(memory.chromadb, "PersistentClient", UnreachableClient):
            store = SemanticMemory(Path(self._tmp.name) / "c", guard=self.guard)
            self.assertEqual(store.query("anything", top_k=3, exclude_run_id="r"), [])
            self.assertEqual(store.add("r", "task", "remember this"), "")
            # Constructor, query and add each tried to connect, with 3 retries each;
            # the third failed call opened the circuit, so the next one fails fast.
            self.assertEqual(len(self.sleeps), 9)
            self.assertEqual(self.guard.breaker.state, "open")
            self.assertEqual(store.query("again", top_k=3, exclude_run_id="r"), [])
            self.assertEqual(len(self.sleeps), 9)
        degraded = [event for event in self.events if event["event"] == "degraded"]
        self.assertEqual(len(degraded), 5)
        self.assertEqual(degraded[0]["dependency"], "chroma")

    def test_a_failing_query_or_add_degrades_after_connecting(self) -> None:
        store = SemanticMemory(Path(self._tmp.name) / "c", guard=self.guard)
        store._collection = FlakyCollection()
        self.assertEqual(store.query("anything", top_k=3, exclude_run_id="r"), [])
        self.assertEqual(store.add("r", "task", "x"), "")
        self.assertEqual([e["outcome"] for e in self.events if e["event"] == "degraded"],
                         ["memory query skipped", "memory add skipped"])

    def test_without_a_guard_failures_still_raise(self) -> None:
        with mock.patch.object(memory.chromadb, "PersistentClient", UnreachableClient):
            with self.assertRaises(ConnectionError):
                SemanticMemory(Path(self._tmp.name) / "c")


if __name__ == "__main__":
    unittest.main()
