"""The project's offline test suite.

Agents built without a ``RunServices`` read the process environment, so it is set up
here for the whole test process:

- the degraded-response cache is off: no test writes into the repository's
  ``.cache/``, and no test is served a reply cached by another (tests of the cache
  build their own ``ResponseCache`` in a temporary folder);
- retry waits are at their minimum, so a scripted transient failure costs well under a
  second (tests of the backoff inject their own policy and sleep);
- cost reports go to one temporary folder, never to the repository's
  ``docs/cost_reports/``.

- no test can reach the real model service: the API key is a dummy (set before any
  code calls ``load_dotenv``, which never overrides a variable already set, so a
  developer's ``.env`` key is never loaded) and the API base URL is a closed local port,
  so a test that wrongly reaches a real client fails at once instead of spending money.
  Only an explicit ``RUN_LIVE_E2E=1`` (the opt-in live end-to-end test) lifts this.

``TEST_ENV`` is the same settings as a mapping, for tests that build
``RunServices.create(env=TEST_ENV)`` without the developer's own environment.
"""

import atexit
import os
import shutil
import tempfile

COST_REPORT_DIR = tempfile.mkdtemp(prefix="test-cost-reports-")
atexit.register(shutil.rmtree, COST_REPORT_DIR, ignore_errors=True)
TEST_ENV = {
    "LLM_RESPONSE_CACHE_TTL_SECONDS": "0",
    "RETRY_BASE_DELAY_SECONDS": "0.1",
    "RETRY_MAX_DELAY_SECONDS": "1",
    "COST_REPORT_DIR": COST_REPORT_DIR,
}
LIVE = os.environ.get("RUN_LIVE_E2E") == "1"
"""The opt-in live end-to-end run (tests/e2e): the only time the real API is reachable."""

os.environ.update(TEST_ENV)
if not LIVE:
    os.environ["ANTHROPIC_API_KEY"] = "test-key-never-valid"
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY"):
        os.environ.pop(name, None)
