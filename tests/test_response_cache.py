"""Tests for ``resilience.response_cache.ResponseCache`` (research R6)."""

import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from resilience.response_cache import ResponseCache
from tests.fakes import scripted_claude as sc

REQUEST = {
    "model": "claude-haiku-5-5",
    "system": [{"type": "text", "text": "You are QA.", "cache_control": {"type": "ephemeral"}}],
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "spec", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": "Write tests."},
    ]}],
    "output_config": {"effort": "high"},
}


class Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


class ResponseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "llm"
        self.clock = Clock()
        self.cache = ResponseCache(self.directory, ttl_seconds=60, clock=self.clock)

    def test_the_fingerprint_ignores_cache_markers_and_key_order(self) -> None:
        plain = {
            "output_config": {"effort": "high"},
            "messages": [{"content": [{"text": "spec", "type": "text"},
                                      {"type": "text", "text": "Write tests."}],
                          "role": "user"}],
            "system": [{"type": "text", "text": "You are QA."}],
            "model": "claude-haiku-5-5",
        }
        self.assertEqual(ResponseCache.fingerprint(REQUEST), ResponseCache.fingerprint(plain))

    def test_the_fingerprint_changes_when_a_message_changes(self) -> None:
        changed = {**REQUEST, "messages": [{"role": "user", "content": "other"}]}
        self.assertNotEqual(ResponseCache.fingerprint(REQUEST), ResponseCache.fingerprint(changed))

    def test_put_then_get_within_the_ttl_returns_an_equal_message(self) -> None:
        response = sc.text_reply({"ok": True})
        self.cache.put(REQUEST, response)
        self.clock.now += 59
        self.assertEqual(self.cache.get(REQUEST), response)

    def test_an_entry_older_than_the_ttl_is_a_miss(self) -> None:
        self.cache.put(REQUEST, sc.text_reply({"ok": True}))
        self.clock.now += 61
        self.assertIsNone(self.cache.get(REQUEST))

    def test_a_ttl_of_zero_turns_the_cache_off(self) -> None:
        cache = ResponseCache(self.directory, ttl_seconds=0, clock=self.clock)
        cache.put(REQUEST, sc.text_reply({"ok": True}))
        self.assertIsNone(cache.get(REQUEST))
        self.assertFalse(self.directory.exists())

    def test_files_are_written_atomically_and_private(self) -> None:
        self.cache.put(REQUEST, sc.text_reply({"ok": True}))
        files = list(self.directory.rglob("*"))
        entries = [path for path in files if path.is_file()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].suffix, ".json")
        self.assertEqual(entries[0].parent.name, "claude-haiku-5-5")
        self.assertEqual(stat.S_IMODE(entries[0].stat().st_mode), 0o600)
        self.assertEqual([path for path in files if path.name.endswith(".tmp")], [])

    def test_entries_survive_a_new_cache_instance(self) -> None:
        response = sc.text_reply({"ok": True})
        self.cache.put(REQUEST, response)
        fresh = ResponseCache(self.directory, ttl_seconds=60, clock=self.clock)
        self.assertEqual(fresh.get(REQUEST), response)

    def test_a_corrupt_file_is_a_miss_and_is_removed(self) -> None:
        self.cache.put(REQUEST, sc.text_reply({"ok": True}))
        entry = next(path for path in self.directory.rglob("*.json"))
        entry.write_text("{not json", encoding="utf-8")
        fresh = ResponseCache(self.directory, ttl_seconds=60, clock=self.clock)
        self.assertIsNone(fresh.get(REQUEST))
        self.assertFalse(entry.exists())

    def test_an_in_memory_hit_does_not_read_the_disk(self) -> None:
        self.cache.put(REQUEST, sc.text_reply({"ok": True}))
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("disk read")):
            self.assertIsNotNone(self.cache.get(REQUEST))

    def test_discard_forgets_an_entry(self) -> None:
        self.cache.put(REQUEST, sc.text_reply({"ok": True}))
        self.cache.discard(REQUEST)
        self.assertIsNone(self.cache.get(REQUEST))
        self.assertEqual(list(self.directory.rglob("*.json")), [])

    def test_a_failed_disk_write_is_reported(self) -> None:
        blocked = Path(self._tmp.name) / "file"
        blocked.write_text("x", encoding="utf-8")
        cache = ResponseCache(blocked / "llm", ttl_seconds=60, clock=self.clock)
        self.assertFalse(cache.put(REQUEST, sc.text_reply({"ok": True})))

    def test_settings_come_from_the_environment(self) -> None:
        cache = ResponseCache.from_env({"LLM_RESPONSE_CACHE_DIR": "/tmp/x",
                                        "LLM_RESPONSE_CACHE_TTL_SECONDS": "0"})
        self.assertEqual((cache.directory, cache.ttl_seconds), (Path("/tmp/x"), 0))
        with self.assertRaisesRegex(ValueError, "LLM_RESPONSE_CACHE_TTL_SECONDS"):
            ResponseCache.from_env({"LLM_RESPONSE_CACHE_TTL_SECONDS": "-1"})


if __name__ == "__main__":
    unittest.main()
