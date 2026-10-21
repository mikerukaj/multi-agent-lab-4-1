"""Test fakes, and a guard that keeps agent unit tests off every live dependency."""

import subprocess
import unittest
from unittest import mock

_REAL_RUN = subprocess.run


class LiveDependencyUsed(AssertionError):
    """A unit test reached a real model client, the Docker CLI or a Chroma store."""


def _no_docker(args, *rest, **kwargs):
    command = args if isinstance(args, (list, tuple)) else [args]
    if command and str(command[0]).endswith("docker"):
        raise LiveDependencyUsed(f"a unit test ran {command[:3]}")
    return _REAL_RUN(args, *rest, **kwargs)


def _refuse(name: str):
    def refuse(*args, **kwargs):
        raise LiveDependencyUsed(f"a unit test built a real {name}")
    return refuse


def assert_no_live_dependencies() -> None:
    """Make a real Anthropic client, ``docker`` command or embedded Chroma raise.

    Call it from an agent unit module's ``setUpModule`` (FR-235a): every test in the
    module must use fakes. The patches are undone when the module's tests finish.
    """
    for target, replacement in (
        ("anthropic.Anthropic", _refuse("Anthropic client")),
        ("subprocess.run", _no_docker),
        ("chromadb.PersistentClient", _refuse("Chroma store")),
    ):
        patcher = mock.patch(target, replacement)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)
