"""Test-only entry point: ``python -m tests.fakes.slow_team_main "<requirement>"``.

Runs the real ``orchestration.team.main`` with every model replaced by a slow,
content-aware fake (one second per request) and the sandbox, MCP server and memory
replaced by test fakes, so a test can send it a real SIGTERM mid-run. Nothing here is
reachable from production configuration (FR-239). The file name does not match
``test*.py``.
"""

import functools
import sys
import time
from unittest import mock

import tests  # noqa: F401 - applies the test environment (no response cache)
from orchestration import team
from tests.fakes.team_script import TeamModel, fake_run_tests, offline_coder
from tests.team_fakes import FakeFileTools

SECONDS_PER_REQUEST = 1.0


class SlowModel(TeamModel):
    """A ``TeamModel`` that takes a second over every request."""

    def next_reply(self, request: dict):
        time.sleep(SECONDS_PER_REQUEST)
        return super().next_reply(request)


def main() -> int:
    with offline_coder(), \
            mock.patch("anthropic.Anthropic", lambda **kwargs: SlowModel()), \
            mock.patch.object(team, "check_health", return_value={"ok": True, "items": []}), \
            mock.patch.object(team, "CoderAgent",
                              functools.partial(team.CoderAgent, file_tools=FakeFileTools)), \
            mock.patch.object(team, "QAAgent",
                              functools.partial(team.QAAgent, run_tests=fake_run_tests,
                                                file_tools=FakeFileTools)):
        return team.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
