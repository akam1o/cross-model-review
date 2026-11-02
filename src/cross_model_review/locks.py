"""Cross-process workspace locks based on ``flock(2)``.

Read-only work takes a shared lock and write-enabled work an exclusive lock,
so a writer never runs alongside another writer or a reader of the same
workspace, regardless of which process (orchestrator or agent server) holds
the lock. Acquisition never blocks: a busy workspace is reported to the caller
instead of queueing behind a possibly long-running CLI session.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockBusy(Exception):
    pass


def default_lock_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"cross-model-review-locks-{os.getuid()}"


def lock_path(lock_dir: Path, namespace: str, target: Path) -> Path:
    digest = hashlib.sha256(f"{namespace}\0{os.path.realpath(target)}".encode()).hexdigest()[:32]
    return lock_dir / f"{namespace}-{digest}.lock"


@contextmanager
def hold_lock(lock_dir: Path, namespace: str, target: Path, *, exclusive: bool) -> Iterator[None]:
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = lock_path(lock_dir, namespace, target)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            mode = "exclusive" if exclusive else "shared"
            raise LockBusy(f"{namespace} lock for {target} is held ({mode} requested)") from exc
        yield
    finally:
        os.close(fd)


def workspace_lock(lock_dir: Path, workspace: Path, *, exclusive: bool):
    return hold_lock(lock_dir, "workspace", workspace, exclusive=exclusive)


def run_lock(lock_dir: Path, repo_common_dir: Path):
    return hold_lock(lock_dir, "run", repo_common_dir, exclusive=True)
