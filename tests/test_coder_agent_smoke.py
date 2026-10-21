"""Live end-to-end check of the Coder Agent against the real model.

It makes real, paid model calls, so it runs only when asked for explicitly with
``RUN_LIVE_E2E=1`` (with a key, in the environment or .env, and Docker), never in the
default offline suite (FR-236).
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from dotenv import load_dotenv

from agents.coder_agent import CoderConfig, run_coder_agent
from tests import LIVE

load_dotenv()


@unittest.skipUnless(LIVE and os.environ.get("ANTHROPIC_API_KEY"),
                     "RUN_LIVE_E2E is not set (the live run makes paid model calls)")
@unittest.skipUnless(shutil.which("docker"), "Docker is not available")
class CoderAgentSmokeTest(unittest.TestCase):
    def test_fibonacci_task_returns_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = CoderConfig(
                workspace_root=Path(tmp) / "workspace", chroma_path=Path(tmp) / "chroma"
            )
            result = run_coder_agent(
                "Write a function that returns the n-th Fibonacci number and print fib(10)",
                config,
            ).to_dict()

        self.assertEqual(result["status"], "completed", result["error"])
        self.assertIsInstance(result["code"], str)
        self.assertIsInstance(result["explanation"], str)
        self.assertIsInstance(result["plan"], list)
        self.assertTrue(all(isinstance(step, str) for step in result["plan"]))
        self.assertIsInstance(result["execution_result"], dict)
        self.assertEqual(result["execution_result"]["exit_code"], 0)
        self.assertIn("55", result["execution_result"]["stdout"])


if __name__ == "__main__":
    unittest.main()
