"""The response cache that serves a model call when its dependency is unavailable.

It is not a read-through cache: a live response is always preferred, and the cache is
consulted only after the guard gives up (research R6). Entries are keyed by a
fingerprint of the request without its ``cache_control`` markers, live in memory and
under ``.cache/llm/<model>/<fingerprint>.json`` (mode 0600, written atomically), and
expire after ``LLM_RESPONSE_CACHE_TTL_SECONDS`` (0 turns the cache off).
"""

import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from anthropic.types.beta import BetaMessage

from resilience.retry import number_setting

FINGERPRINT_KEYS = ("model", "system", "tools", "messages", "output_config")
DEFAULT_TTL_SECONDS = 86_400.0
DEFAULT_DIRECTORY = Path(".cache/llm")


def strip_cache_control(value):
    """Return ``value`` without any ``cache_control`` key, at any depth."""
    if isinstance(value, dict):
        return {key: strip_cache_control(item) for key, item in value.items()
                if key != "cache_control"}
    if isinstance(value, list):
        return [strip_cache_control(item) for item in value]
    return value


def _plain(value):
    """JSON fallback for SDK content blocks kept in a Coder history."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


class ResponseCache:
    """Stores successful model responses and returns fresh ones for an identical request."""

    def __init__(
        self, directory: Path, ttl_seconds: float, clock: Callable[[], float] = time.time
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._memory: dict[str, tuple[float, BetaMessage]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ResponseCache":
        """Build the cache from ``LLM_RESPONSE_CACHE_DIR`` and ``..._TTL_SECONDS``.

        Raises:
            ValueError: if the TTL is negative or not a number.
        """
        ttl = number_setting(
            env, "LLM_RESPONSE_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS, 0, float("inf")
        )
        return cls(Path(env.get("LLM_RESPONSE_CACHE_DIR") or DEFAULT_DIRECTORY), ttl)

    @property
    def enabled(self) -> bool:
        """Whether entries are stored and served at all."""
        return self.ttl_seconds > 0

    @staticmethod
    def fingerprint(request: dict) -> str:
        """SHA-256 of the request's canonical JSON, ignoring cache markers and key order."""
        canonical = {key: strip_cache_control(request.get(key)) for key in FINGERPRINT_KEYS}
        text = json.dumps(canonical, sort_keys=True, default=_plain)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, request: dict) -> BetaMessage | None:
        """Return the stored response for ``request`` if it is younger than the TTL."""
        if not self.enabled:
            return None
        key = self.fingerprint(request)
        with self._lock:
            entry = self._memory.get(key)
        if entry is None:
            entry = self._load(self._path(request, key))
        if entry is None or self._clock() - entry[0] > self.ttl_seconds:
            return None
        with self._lock:
            self._memory[key] = entry
        return entry[1]

    def put(self, request: dict, response) -> bool:
        """Store ``response`` for ``request``; return ``False`` if the disk write failed.

        Anything that is not an SDK message (an old-style test fake) is not stored. A
        failed write loses only the on-disk copy, never the run.
        """
        if not self.enabled or not isinstance(response, BetaMessage):
            return True
        key = self.fingerprint(request)
        stored_at = self._clock()
        with self._lock:
            self._memory[key] = (stored_at, response)
        record = {"stored_at": stored_at, "model": response.model,
                  "response": response.model_dump(mode="json")}
        return self._write(self._path(request, key), record)

    def discard(self, request: dict) -> None:
        """Forget the entry for ``request`` (a cached reply that no longer validates)."""
        key = self.fingerprint(request)
        with self._lock:
            self._memory.pop(key, None)
        self._path(request, key).unlink(missing_ok=True)

    def _path(self, request: dict, key: str) -> Path:
        model = str(request.get("model", "unknown")).replace("/", "_")
        return self.directory / model / f"{key}.json"

    @staticmethod
    def _load(path: Path) -> tuple[float, BetaMessage] | None:
        """Read one entry from disk; a corrupt file is removed and treated as a miss."""
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            return float(record["stored_at"]), BetaMessage.model_validate(record["response"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            return None

    @staticmethod
    def _write(path: Path, record: dict) -> bool:
        """Write ``record`` to ``path`` atomically, readable only by the owner."""
        temp: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
            temp = Path(name)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
            os.chmod(temp, 0o600)
            os.replace(temp, path)
            return True
        except OSError:
            if temp is not None:
                temp.unlink(missing_ok=True)
            return False
