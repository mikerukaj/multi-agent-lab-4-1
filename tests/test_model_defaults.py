"""Models by role and their capability checks (FR-225a, FR-225b, research R8)."""

import os
import unittest
from unittest import mock

from agents.coder_agent import CoderConfig, build_request
from agents.llm import LARGE_MODEL, SMALL_MODEL, check_model_settings
from agents.pm_agent import PMConfig
from agents.qa_agent import QAConfig, build_qa_request


class DefaultModelTests(unittest.TestCase):
    def test_the_coder_and_qa_use_the_small_model_without_fallbacks(self) -> None:
        for config in (CoderConfig(), QAConfig()):
            with self.subTest(config=type(config).__name__):
                self.assertEqual(config.model, SMALL_MODEL)
                self.assertFalse(config.use_fallbacks)

    def test_the_pm_keeps_the_large_model_with_fallbacks(self) -> None:
        config = PMConfig()
        self.assertEqual(config.model, LARGE_MODEL)
        self.assertTrue(config.use_fallbacks)

    def test_requests_for_the_small_model_carry_no_fallbacks(self) -> None:
        coder = build_request(CoderConfig(), [], [])
        qa = build_qa_request(QAConfig(), ["spec", "Write tests."], {"type": "object"})
        for request in (coder, qa):
            self.assertNotIn("betas", request)
            self.assertNotIn("fallbacks", request)
            self.assertEqual(request["model"], SMALL_MODEL)


class CapabilityTests(unittest.TestCase):
    def test_fallbacks_on_the_small_model_are_refused_by_name(self) -> None:
        with mock.patch.dict(os.environ, {"CODER_USE_FALLBACKS": "true"}):
            with self.assertRaisesRegex(ValueError, "coder: CODER_USE_FALLBACKS=true .*"
                                                    "claude-haiku-5-5"):
                CoderConfig.from_env()
        with mock.patch.dict(os.environ, {"QA_USE_FALLBACKS": "yes"}):
            with self.assertRaisesRegex(ValueError, "qa: QA_USE_FALLBACKS"):
                QAConfig.from_env()

    def test_an_effort_the_model_does_not_support_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "QA_EFFORT=ultra is not supported by"):
            check_model_settings("qa", SMALL_MODEL, "ultra", False)

    def test_an_unknown_model_is_allowed_with_a_warning(self) -> None:
        config = CoderConfig(model="claude-future-9")
        self.assertFalse(config.use_fallbacks)
        self.assertEqual(len(config.warnings), 1)
        self.assertIn("claude-future-9", config.warnings[0])

    def test_moving_an_agent_back_to_the_large_model_turns_fallbacks_on(self) -> None:
        with mock.patch.dict(os.environ, {"CODER_MODEL": LARGE_MODEL}):
            config = CoderConfig.from_env()
        self.assertTrue(config.use_fallbacks)
        self.assertIn("betas", build_request(config, [], []))
        with mock.patch.dict(os.environ, {"QA_MODEL": LARGE_MODEL, "QA_USE_FALLBACKS": "false"}):
            self.assertFalse(QAConfig.from_env().use_fallbacks)


if __name__ == "__main__":
    unittest.main()
