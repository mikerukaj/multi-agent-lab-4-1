"""Prompt-cache markers on every agent's requests (FR-225, research R7).

Real agents run on content-aware fake models, so the request bodies the model would
receive are recorded and checked.
"""

import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from orchestration.services import RunServices
from orchestration.team import run_team
from resilience.response_cache import ResponseCache
from tests import TEST_ENV
from tests.fakes.team_script import (
    REQUIREMENT,
    TeamModel,
    offline_coder,
    request_kind,
    scripted_team,
)

EPHEMERAL = {"type": "ephemeral"}


def markers(value) -> list[dict]:
    """Every ``cache_control`` marker anywhere in ``value``."""
    found = []
    if isinstance(value, dict):
        if "cache_control" in value:
            found.append(value["cache_control"])
        for item in value.values():
            found += markers(item)
    elif isinstance(value, list):
        for item in value:
            found += markers(item)
    return found


class PromptCachingTests(unittest.TestCase):
    def run_scripted(self, ttl: str) -> tuple:
        models = (TeamModel(), TeamModel(), TeamModel(bug_in_first_review=True))
        with tempfile.TemporaryDirectory() as tmp, offline_coder():
            services = RunServices.create(env={**TEST_ENV, "PROMPT_CACHE_TTL": ttl},
                                          response_cache=None, sleep=lambda delay: None)
            pm, coder, qa = scripted_team(Path(tmp), services, *models)
            final = run_team(REQUIREMENT, pm, coder, Path(tmp), qa=qa, services=services)
        self.assertEqual(final["end_reason"], "complete")
        return models

    def requests(self, models, kind: str) -> list[dict]:
        return [r for model in models for r in model.requests if request_kind(r) == kind]

    def test_pm_marks_its_system_block(self) -> None:
        models = self.run_scripted("5m")
        spec = self.requests(models, "spec")[0]
        self.assertIsInstance(spec["system"], list)
        self.assertEqual(spec["system"][-1]["cache_control"], EPHEMERAL)

    def test_the_1h_ttl_is_on_every_marker_and_off_means_none(self) -> None:
        hour = self.run_scripted("1h")
        for model in hour:
            for request in model.requests:
                found = markers(request)
                self.assertTrue(found, request_kind(request))
                self.assertTrue(all(m == {"type": "ephemeral", "ttl": "1h"} for m in found))
        off = self.run_scripted("off")
        for model in off:
            for request in model.requests:
                self.assertEqual(markers(request), [])
                self.assertIsInstance(request["system"], str)

    def test_coder_turns_mark_the_system_block_and_cache_the_history(self) -> None:
        models = self.run_scripted("5m")
        for request in self.requests(models, "loop") + self.requests(models, "summary"):
            self.assertEqual(request["system"][-1]["cache_control"], EPHEMERAL)
            self.assertEqual(request["cache_control"], EPHEMERAL)

    def test_the_critique_marks_the_system_and_the_sources_block(self) -> None:
        critique = self.requests(self.run_scripted("5m"), "critique")[0]
        content = critique["messages"][0]["content"]
        self.assertTrue(content[0]["text"].startswith("Source files:"))
        self.assertEqual(content[0]["cache_control"], EPHEMERAL)
        self.assertNotIn("cache_control", content[-1])
        self.assertEqual(len(markers(critique)), 2)

    def test_qa_marks_its_stable_prefix_and_puts_the_instruction_last(self) -> None:
        models = self.run_scripted("5m")
        for request in self.requests(models, "tests") + self.requests(models, "review"):
            content = request["messages"][0]["content"]
            self.assertTrue(content[0]["text"].startswith("Specification:"))
            self.assertEqual([i for i, block in enumerate(content) if "cache_control" in block],
                             [len(content) - 2])
            self.assertNotIn("cache_control", content[-1])

    def test_no_request_has_more_than_four_markers_or_mixed_ttls(self) -> None:
        for ttl in ("5m", "1h"):
            for model in self.run_scripted(ttl):
                for request in model.requests:
                    found = markers(request)
                    self.assertLessEqual(len(found), 4)
                    self.assertEqual(len({json.dumps(m, sort_keys=True) for m in found}), 1)

    def test_the_coder_history_stays_append_only(self) -> None:
        sessions: dict[str, list[dict]] = {}
        for request in self.requests(self.run_scripted("5m"), "loop"):
            key = json.dumps(request["messages"][0], default=str)
            sessions.setdefault(key, []).append(request)
        self.assertTrue(any(len(turns) >= 2 for turns in sessions.values()))
        for turns in sessions.values():
            for earlier, later in zip(turns, turns[1:]):
                prefix = earlier["messages"]
                self.assertEqual(later["messages"][:len(prefix)], prefix)

    def test_markers_do_not_change_the_response_cache_fingerprint(self) -> None:
        spec = self.requests(self.run_scripted("5m"), "spec")[0]
        plain = self.requests(self.run_scripted("off"), "spec")[0]
        unmarked = {**spec, "system": [{"type": "text", "text": spec["system"][0]["text"]}]}
        self.assertEqual(ResponseCache.fingerprint(spec), ResponseCache.fingerprint(unmarked))
        self.assertNotEqual(plain["system"], spec["system"])


if __name__ == "__main__":
    unittest.main()
