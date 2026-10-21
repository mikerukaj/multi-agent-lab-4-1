"""The Coder's loop tests again, with generated code in the real Docker sandbox.

tests/test_coder_agent_loop.py runs these classes with a local stand-in for the sandbox,
so the unit suite needs no Docker (FR-235a); here ``real_sandbox`` keeps the real one.
The module is imported (not its classes), so discovery doesn't run the unit versions
twice.
"""

import shutil
import unittest

from tests import test_coder_agent_loop as loop

requires_docker = unittest.skipUnless(shutil.which("docker"), "Docker is not available")


@requires_docker
class HappyPathSandboxTests(loop.HappyPathTests):
    real_sandbox = True


@requires_docker
class LimitAndFailureSandboxTests(loop.LimitAndFailureTests):
    real_sandbox = True


@requires_docker
class SharedWorkspaceSandboxTests(loop.SharedWorkspaceTests):
    real_sandbox = True


@requires_docker
class McpFileToolSandboxTests(loop.McpFileToolTests):
    real_sandbox = True


if __name__ == "__main__":
    unittest.main()
