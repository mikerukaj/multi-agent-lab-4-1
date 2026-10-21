"""Five runs share one Chroma server safely (FR-230a, SC-213).

A throwaway ``chromadb/chroma`` container serves five processes, each writing 20
lessons through ``SemanticMemory`` in server mode. Skips without Docker.
"""

import multiprocessing
import shutil
import socket
import subprocess
import time
import unittest
import urllib.request
from pathlib import Path

CHROMA_IMAGE = (
    "chromadb/chroma:1.5.9@sha256:1e0b73a187a28757c572acba508c46f48c9e8b0acaf5c20e6d95cdedce1acdf6"
)
RUNS, LESSONS = 5, 20


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def write_lessons(port: int, run: int, queue) -> None:
    from agents.memory import SemanticMemory

    try:
        store = SemanticMemory(Path("/unused"), host="127.0.0.1", port=port)
        run_id = f"20261020-00000{run}-run{run:03d}"
        ids = [store.add(run_id, "lesson", f"lesson {n} of run {run}") for n in range(LESSONS)]
        store.query("lesson", top_k=3, exclude_run_id=run_id)
        queue.put(("ok", ids))
    except Exception as error:  # Reported to the test, which fails on it.
        queue.put(("error", f"{type(error).__name__}: {error}"))


@unittest.skipUnless(shutil.which("docker"), "Docker is not available")
class MemoryConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = free_port()
        started = subprocess.run(
            ["docker", "run", "-d", "--rm", "-p", f"127.0.0.1:{self.port}:8000", CHROMA_IMAGE],
            capture_output=True, text=True, check=True,
        )
        self.container = started.stdout.strip()
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", self.container],
                        capture_output=True, check=False)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/v2/heartbeat", timeout=2)
                return
            except OSError:
                time.sleep(0.5)
        self.fail("the Chroma server did not become healthy")

    def test_five_concurrent_runs_keep_every_lesson(self) -> None:
        import chromadb

        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [context.Process(target=write_lessons, args=(self.port, run, queue))
                     for run in range(RUNS)]
        for process in processes:
            process.start()
        results = [queue.get(timeout=300) for _ in processes]
        for process in processes:
            process.join(60)
        self.assertEqual([status for status, _ in results], ["ok"] * RUNS, results)
        client = chromadb.HttpClient(host="127.0.0.1", port=self.port)
        collection = client.get_collection("coder_memory")
        self.assertEqual(collection.count(), RUNS * LESSONS)
        ids = [record_id for _, run_ids in results for record_id in run_ids]
        self.assertEqual(len(set(ids)), RUNS * LESSONS)
        fetched = collection.get(ids=ids)
        self.assertEqual(len(fetched["ids"]), RUNS * LESSONS)
        self.assertTrue(all(document.startswith("lesson ") for document in fetched["documents"]))


if __name__ == "__main__":
    unittest.main()
