"""Tests for ``orchestration.checkpoint`` (data-model §2.3, research R13)."""

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from observability.redaction import MARKER, Redactor
from orchestration import checkpoint
from orchestration.checkpoint import (
    CheckpointError,
    RunLock,
    RunLocked,
    is_locked,
    load_checkpoint,
    read_final_record,
    restore_workspace,
    save_checkpoint,
    snapshot_workspace,
    write_final_record,
)
from orchestration.team import initial_team_state

SECRET = "sk-ant-api03-checkpointsecret0123456789"


def hold_lock(directory: str, held, release) -> None:
    with RunLock(Path(directory)):
        held.set()
        release.wait(10)


class CheckpointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.workspace = root / "run-1"
        (self.workspace / "tests").mkdir(parents=True)
        (self.workspace / ".qa").mkdir()
        (self.workspace / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (self.workspace / "tests" / "test_main.py").write_text("def test(): pass\n", "utf-8")
        (self.workspace / ".qa" / "report.md").write_text("# report\n", encoding="utf-8")
        (self.workspace / "logo.bin").write_bytes(bytes(range(256)))
        os.chmod(self.workspace / "main.py", 0o755)
        self.directory = root / ".runs" / "run-1"
        self.state = initial_team_state(f"Build it. Key: {SECRET}", "run-1", self.workspace)
        self.state["test_suite"] = {"tests/test_main.py": "def test(): pass\n"}
        self.settings = {"models": {"pm": "claude-sonnet-5-5"}, "max_iterations": 5}


class SaveAndLoadTests(CheckpointTestCase):
    def test_save_then_load_round_trips_everything(self) -> None:
        ledger = {"pm": {"model_calls": 1}}
        save_checkpoint(self.directory, "build", "dispatch", self.state, self.settings, ledger)
        record = load_checkpoint(self.directory)
        self.assertEqual(record["stage"], "build")
        self.assertEqual(record["next_node"], "dispatch")
        self.assertEqual(record["state"], self.state)
        self.assertEqual(record["state"]["test_suite"], self.state["test_suite"])
        self.assertEqual(record["settings"], self.settings)
        self.assertEqual(record["ledger"], ledger)
        self.assertEqual(record["workspace"], snapshot_workspace(self.workspace))
        self.assertIn("tests/test_main.py", record["workspace"])
        self.assertIn(".qa/report.md", record["workspace"])
        self.assertIn("logo.bin", record["workspace"])
        self.assertTrue(record["complete"])

    def test_resume_from_is_never_saved(self) -> None:
        save_checkpoint(self.directory, "plan", "dispatch",
                        {**self.state, "resume_from": "qa"}, self.settings)
        self.assertIsNone(load_checkpoint(self.directory)["state"]["resume_from"])

    def test_a_crash_during_a_save_leaves_the_previous_checkpoint_loadable(self) -> None:
        save_checkpoint(self.directory, "plan", "dispatch", self.state, self.settings)
        with mock.patch.object(checkpoint.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                save_checkpoint(self.directory, "build", "reflect", self.state, self.settings)
        self.assertEqual(load_checkpoint(self.directory)["stage"], "plan")
        self.assertEqual([p.name for p in self.directory.iterdir()], ["checkpoint.json"])

    def test_unusable_checkpoints_are_refused_with_the_reason(self) -> None:
        with self.assertRaisesRegex(CheckpointError, "no checkpoint"):
            load_checkpoint(self.directory)
        save_checkpoint(self.directory, "plan", "dispatch", self.state, self.settings)
        path = self.directory / "checkpoint.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        cases = {
            "incomplete": {**record, "complete": False},
            "format": {**record, "format_version": 99},
            "incompatible": {**record, "app_version": "9.0.0"},
        }
        for reason, broken in cases.items():
            with self.subTest(reason=reason):
                path.write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaisesRegex(CheckpointError, reason):
                    load_checkpoint(self.directory)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(CheckpointError, "unreadable"):
            load_checkpoint(self.directory)

    def test_secrets_are_redacted_in_the_checkpoint_and_the_final_record(self) -> None:
        redact = Redactor.from_env({"ANTHROPIC_API_KEY": SECRET}).redact
        save_checkpoint(self.directory, "plan", "dispatch", self.state, self.settings,
                        redact=redact)
        write_final_record(self.directory, self.state, redact)
        for name in ("checkpoint.json", "run.json"):
            text = (self.directory / name).read_text(encoding="utf-8")
            self.assertNotIn(SECRET, text, name)
            self.assertIn(MARKER, text, name)
        self.assertEqual(read_final_record(self.directory)["run_id"], "run-1")

    def test_a_run_without_a_final_record_reads_as_none(self) -> None:
        self.assertIsNone(read_final_record(self.directory))


class RestoreTests(CheckpointTestCase):
    def test_restore_overwrites_recreates_and_removes_and_lists_the_files(self) -> None:
        snapshot = snapshot_workspace(self.workspace)
        (self.workspace / "main.py").write_text("print('changed')\n", encoding="utf-8")
        (self.workspace / "tests" / "test_main.py").unlink()
        (self.workspace / "extra.py").write_text("x = 1\n", encoding="utf-8")
        restored, removed = restore_workspace(self.workspace, snapshot)
        self.assertEqual(restored, ["main.py", "tests/test_main.py"])
        self.assertEqual(removed, ["extra.py"])
        self.assertEqual(snapshot_workspace(self.workspace), snapshot)
        self.assertEqual(os.stat(self.workspace / "main.py").st_mode & 0o777, 0o755)

    def test_an_unchanged_workspace_restores_nothing(self) -> None:
        self.assertEqual(restore_workspace(self.workspace, snapshot_workspace(self.workspace)),
                         ([], []))

    def test_caches_are_not_snapshotted(self) -> None:
        (self.workspace / "__pycache__").mkdir()
        (self.workspace / "__pycache__" / "main.pyc").write_bytes(b"x")
        self.assertNotIn("__pycache__/main.pyc", snapshot_workspace(self.workspace))


class RunLockTests(CheckpointTestCase):
    def test_the_lock_is_exclusive_across_processes(self) -> None:
        context = multiprocessing.get_context("fork")
        held, release = context.Event(), context.Event()
        process = context.Process(target=hold_lock, args=(str(self.directory), held, release))
        process.start()
        try:
            self.assertTrue(held.wait(10))
            self.assertTrue(is_locked(self.directory))
            with self.assertRaises(RunLocked):
                with RunLock(self.directory):
                    pass
        finally:
            release.set()
            process.join(10)
        self.assertFalse(is_locked(self.directory))
        with RunLock(self.directory):
            pass

    def test_a_missing_lock_file_is_not_locked(self) -> None:
        self.assertFalse(is_locked(self.directory))


if __name__ == "__main__":
    unittest.main()
