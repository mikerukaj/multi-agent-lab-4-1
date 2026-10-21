"""Checkpoints, final run records and the run lock, on the standard library (research R13).

Everything for a run lives in ``<workspace_root>/.runs/<run_id>/``, outside the folder the
sandbox mounts, so generated code can't read or change it:

- ``checkpoint.json``: the state after the last completed stage, the run's settings and
  ledger, and a snapshot of the workspace. It is written to a temporary file, flushed to
  disk and renamed into place, so a half-written checkpoint is never read (FR-213).
- ``run.json``: the final record, written however the run ends (FR-212).
- ``lock``: held with ``fcntl.flock`` for the life of the run, so a run that is still
  going (in another process or container on this host) is never resumed or cleaned up.

LangGraph checkpointers are not used: the constitution forbids them, and their state
could not be redacted or versioned (plan, Complexity Tracking).
"""

import base64
import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from messaging import __version__ as APP_VERSION

FORMAT_VERSION = 1
CHECKPOINT_FILE = "checkpoint.json"
RECORD_FILE = "run.json"
LOCK_FILE = "lock"
SKIPPED_DIRS = ("__pycache__", ".pytest_cache")
"""Caches left by running code: not part of the program, so never snapshotted."""

Redact = Callable[[object], object]


def _identity(value):
    return value


class CheckpointError(Exception):
    """A checkpoint can't be used for a resume; the message says why."""


class RunLocked(Exception):
    """Another process holds the run's lock: the run is still going."""


def runs_dir(workspace_root: Path) -> Path:
    """The folder holding every run's records."""
    return Path(workspace_root) / ".runs"


def run_dir(workspace_root: Path, run_id: str) -> Path:
    """The folder holding one run's checkpoint, final record, events and lock."""
    return runs_dir(workspace_root) / run_id


class RunLock:
    """An exclusive, non-blocking ``flock`` on a run's ``lock`` file, as a context manager.

    Raises:
        RunLocked: on entry, if another process holds the lock.
    """

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / LOCK_FILE
        self._handle = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise RunLocked(f"run lock {self.path} is held by another process") from error
        self._handle = handle
        return self

    def __exit__(self, *exc) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def is_locked(directory: Path) -> bool:
    """Whether a process holds the lock in ``directory`` (a run that is still going)."""
    path = Path(directory) / LOCK_FILE
    if not path.exists():
        return False
    with open(path, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def snapshot_workspace(workspace: Path) -> dict[str, dict]:
    """Every file in ``workspace`` (including ``tests/`` and ``.qa/``) as base64 and mode.

    The snapshot is the program itself, so it is not redacted: generated files are
    restored exactly as they were (FR-219 exempts workspace contents).
    """
    workspace = Path(workspace)
    snapshot = {}
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if path.is_file() and not any(part in SKIPPED_DIRS for part in relative.parts):
            snapshot[relative.as_posix()] = {
                "b64": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mode": path.stat().st_mode & 0o777,
            }
    return snapshot


def restore_workspace(workspace: Path, snapshot: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Make ``workspace`` match ``snapshot``; return ``(restored, removed)`` paths.

    ``restored`` lists files overwritten or recreated; ``removed`` lists files that were
    not in the snapshot (FR-213d).
    """
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    current = snapshot_workspace(workspace)
    restored = []
    for relative, entry in sorted(snapshot.items()):
        if current.get(relative) == entry:
            continue
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(entry["b64"]))
        os.chmod(target, entry["mode"])
        restored.append(relative)
    removed = sorted(set(current) - set(snapshot))
    for relative in removed:
        (workspace / relative).unlink()
    return restored, removed


def save_checkpoint(
    directory: Path,
    stage: str,
    next_node: str,
    state: dict,
    settings: dict,
    ledger: dict | None = None,
    redact: Redact = _identity,
) -> Path:
    """Write the run's checkpoint after ``stage``; the graph resumes at ``next_node``.

    The state is redacted; ``resume_from`` is cleared. The file only replaces the
    previous checkpoint once it is completely written.
    """
    record = {
        "format_version": FORMAT_VERSION,
        "app_version": APP_VERSION,
        "saved_at": _now(),
        "stage": stage,
        "next_node": next_node,
        "state": redact({**state, "resume_from": None}),
        "settings": settings,
        "ledger": ledger or {},
        "workspace": snapshot_workspace(Path(state["workspace"])),
        "complete": True,
    }
    path = Path(directory) / CHECKPOINT_FILE
    write_json_atomically(path, record)
    return path


def load_checkpoint(directory: Path) -> dict:
    """Read a run's checkpoint and check it can be resumed from.

    Raises:
        CheckpointError: naming the reason, if it is missing, unparsable, incomplete,
            of another format version, or written by an incompatible application version.
    """
    path = Path(directory) / CHECKPOINT_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CheckpointError(f"no checkpoint at {path}") from error
    except (OSError, ValueError) as error:
        raise CheckpointError(f"the checkpoint {path} is unreadable: {error}") from error
    if not isinstance(record, dict) or record.get("complete") is not True:
        raise CheckpointError(f"the checkpoint {path} is incomplete")
    if record.get("format_version") != FORMAT_VERSION:
        raise CheckpointError(
            f"the checkpoint format {record.get('format_version')!r} is not {FORMAT_VERSION}"
        )
    if _major(record.get("app_version")) != _major(APP_VERSION):
        raise CheckpointError(
            f"the checkpoint was written by version {record.get('app_version')}, "
            f"which is incompatible with {APP_VERSION}"
        )
    return record


def write_final_record(directory: Path, state: dict, redact: Redact = _identity) -> Path:
    """Write ``run.json``, the run's final record (redacted)."""
    path = Path(directory) / RECORD_FILE
    write_json_atomically(path, redact(state))
    return path


def read_final_record(directory: Path) -> dict | None:
    """The run's final record, or ``None`` if it has none (a killed run)."""
    path = Path(directory) / RECORD_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise CheckpointError(f"the final record {path} is unreadable: {error}") from error


def write_json_atomically(path: Path, value) -> None:
    """Write ``value`` as JSON to ``path``: temp file, ``fsync``, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _major(version: object) -> str:
    return str(version).split(".", 1)[0]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
