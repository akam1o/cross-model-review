"""Child process execution with timeouts and reliable cancellation.

Each child runs in its own session (process group) so that terminating it also
terminates anything it spawned (shell commands run by the model, test
runners, ...). stdout and stderr are written to separate files.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

KILL_GRACE_S = 5.0


@dataclass(frozen=True)
class ProcResult:
    exit_code: int | None
    timed_out: bool
    duration_s: float
    stdout_path: Path
    stderr_path: Path


class ExecutableNotFound(Exception):
    pass


async def _terminate_group(proc: asyncio.subprocess.Process, grace_s: float) -> None:
    """SIGTERM the whole process group, then SIGKILL after ``grace_s``."""
    if proc.returncode is not None:
        # The leader exited but its group may still have members.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), grace_s)
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), grace_s)


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_s: float,
    stdin_data: bytes | None = None,
    env: Mapping[str, str] | None = None,
    kill_grace_s: float = KILL_GRACE_S,
) -> ProcResult:
    """Run ``argv`` to completion, a timeout, or cancellation.

    On timeout the process group is killed and ``timed_out`` is set. On
    cancellation the process group is killed before ``CancelledError`` is
    re-raised, so a cancelled caller never leaves a child behind.
    """
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE
                if stdin_data is not None
                else asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=dict(env) if env is not None else None,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ExecutableNotFound(str(argv[0])) from exc
        log.info("process started", extra={"pid": proc.pid, "argv0": argv[0], "cwd": str(cwd)})
        timed_out = False
        try:
            try:
                await asyncio.wait_for(proc.communicate(stdin_data), timeout_s)
            except TimeoutError:
                timed_out = True
                log.warning("process timed out", extra={"pid": proc.pid, "timeout_s": timeout_s})
            # Kill leftovers in the group even after a normal exit.
            await _terminate_group(proc, kill_grace_s)
        except BaseException:
            # CancelledError (or anything else): never leak the child.
            await asyncio.shield(_terminate_group(proc, kill_grace_s))
            log.warning("process cancelled", extra={"pid": proc.pid})
            raise
    return ProcResult(
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def tail(path: Path, max_bytes: int = 4000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def shell_argv(command: Sequence[str] | str) -> list[str]:
    return ["/bin/sh", "-c", command] if isinstance(command, str) else list(command)
