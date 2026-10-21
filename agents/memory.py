"""Two-tier memory for the agents: a short sliding window and a Chroma store."""

import sqlite3
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Chroma needs SQLite 3.35+, and some hosts (e.g. RHEL 8, with 3.26) ship an older one.
# Swap in the bundled pysqlite3 build there; this must happen before chromadb is imported.
if sqlite3.sqlite_version_info < (3, 35, 0):
    import pysqlite3

    sys.modules["sqlite3"] = pysqlite3

import chromadb  # noqa: E402 - must follow the SQLite swap above
from chromadb.config import Settings

from resilience.circuit import DependencyGuard, GuardFailure

MAX_DOCUMENT_CHARS = 2_000


@dataclass
class Turn:
    """One finished exchange: the user's task and the agent's final reply."""

    task: str
    reply: str


class SlidingWindowBuffer:
    """Keeps the most recent ``max_turns`` turns, oldest first."""

    def __init__(self, max_turns: int) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        self._turns: deque[Turn] = deque(maxlen=max_turns)

    def add(self, turn: Turn) -> None:
        """Append a turn, evicting the oldest one when the window is full."""
        self._turns.append(turn)

    def get(self) -> list[Turn]:
        """Return the turns in the window, oldest first."""
        return list(self._turns)


class SemanticMemory:
    """Long-term memory in a persistent Chroma collection.

    Uses Chroma's built-in embedding function, so no extra API key or package
    is needed (research R6).

    With a ``host``, it uses a Chroma server (``HttpClient``), which is safe for several
    runs at once (FR-230a); without one, the embedded store in ``path`` serves one run
    at a time. The embedding is computed client-side by Chroma's default function in
    both modes, so stored vectors are compatible.

    With a ``guard`` (the run's ``chroma`` dependency), connecting, ``query`` and
    ``add`` are retried and circuit-broken. A failure degrades instead of raising:
    ``query`` returns ``[]``, ``add`` skips the write and returns ``""``, and a
    ``degraded`` event is recorded. Memory is advisory, so the run goes on
    (contracts/resilience.md).
    """

    def __init__(
        self,
        path: Path,
        collection: str = "coder_memory",
        guard: DependencyGuard | None = None,
        host: str | None = None,
        port: int = 8000,
    ) -> None:
        self.guard = guard
        self._path = path
        self._host, self._port = host, port
        self._collection_name = collection
        self._collection = None
        self._collection = self._guarded(self._open, None, "connect")

    def _open(self):
        settings = Settings(anonymized_telemetry=False)
        if self._host:
            client = chromadb.HttpClient(host=self._host, port=self._port, settings=settings)
        else:
            self._path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(self._path), settings=settings)
        return client.get_or_create_collection(
            name=self._collection_name, metadata={"hnsw:space": "cosine"}
        )

    def _guarded(self, operation, fallback, action: str):
        """Run ``operation`` through the guard; on failure record it and return ``fallback``."""
        if self.guard is None:
            return operation()
        outcome = self.guard.call(operation)
        if isinstance(outcome, GuardFailure):
            self.guard.emit("degraded", "warning", outcome=f"memory {action} skipped",
                            message=outcome.failure["message"])
            return fallback
        return outcome

    def _ready(self) -> bool:
        """Whether the collection is open, connecting again if an earlier try failed."""
        if self._collection is None:
            self._collection = self._guarded(self._open, None, "connect")
        return self._collection is not None

    def add(self, run_id: str, kind: str, text: str) -> str:
        """Store ``text`` (cut to 2,000 chars) and return its record id (``""`` if skipped)."""
        if not self._ready():
            return ""
        return self._guarded(lambda: self._add(run_id, kind, text), "", "add")

    def _add(self, run_id: str, kind: str, text: str) -> str:
        record_id = f"{run_id}-{self._collection.count()}"
        self._collection.add(
            ids=[record_id],
            documents=[text[:MAX_DOCUMENT_CHARS]],
            metadatas=[
                {
                    "run_id": run_id,
                    "kind": kind,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        return record_id

    def query(self, text: str, top_k: int, exclude_run_id: str) -> list[tuple[str, str]]:
        """Return up to ``top_k`` ``(id, document)`` pairs most similar to ``text``.

        Records from ``exclude_run_id`` are skipped, since the current run's
        context is already in its message history.
        """
        if not self._ready():
            return []
        return self._guarded(lambda: self._query(text, top_k, exclude_run_id), [], "query")

    def _query(self, text: str, top_k: int, exclude_run_id: str) -> list[tuple[str, str]]:
        if self._collection.count() == 0:
            return []
        results = self._collection.query(
            query_texts=[text],
            n_results=top_k,
            where={"run_id": {"$ne": exclude_run_id}},
        )
        return list(zip(results["ids"][0], results["documents"][0]))
