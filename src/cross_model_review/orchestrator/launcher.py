"""Spawn and stop local A2A agent server processes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path

from cross_model_review.agents.server import TOKEN_ENV

log = logging.getLogger(__name__)


class AgentStartError(Exception):
    pass


class LocalAgentProcess:
    """An agent server child process.

    The child runs in its own process group (so a terminal Ctrl-C reaches the
    orchestrator first, which then cancels tasks in order) and exits when its
    stdin is closed, which also happens if the orchestrator dies.
    """

    def __init__(
        self,
        kind: str,
        *,
        config_path: Path,
        token: str,
        allowed_roots: list[Path],
        lock_dir: Path,
        log_dir: Path,
    ) -> None:
        self.kind = kind
        self.config_path = config_path
        self.token = token
        self.allowed_roots = allowed_roots
        self.lock_dir = lock_dir
        self.log_dir = log_dir
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self._stderr = None

    async def start(self, timeout_s: float) -> str:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            sys.executable,
            "-m",
            "cross_model_review.cli",
            "serve-agent",
            self.kind,
            "--config",
            str(self.config_path),
            "--port",
            "0",
            "--lock-dir",
            str(self.lock_dir),
            "--log-dir",
            str(self.log_dir),
            "--exit-on-stdin-eof",
        ]
        for root in self.allowed_roots:
            argv += ["--allowed-root", str(root)]
        env = dict(os.environ)
        env[TOKEN_ENV] = self.token
        self._stderr = open(self.log_dir / "server.stderr.jsonl", "ab")  # noqa: SIM115
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr,
            env=env,
            start_new_session=True,
        )
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout_s)
        except TimeoutError as exc:
            await self.stop(1)
            raise AgentStartError(
                f"{self.kind} agent did not become ready in {timeout_s}s"
            ) from exc
        try:
            ready = json.loads(line)
            assert ready.get("event") == "ready"
            self.url = str(ready["url"])
        except Exception as exc:
            await self.stop(1)
            raise AgentStartError(
                f"{self.kind} agent failed to start (see {self.log_dir / 'server.stderr.jsonl'})"
            ) from exc
        log.info("agent started", extra={"agent": self.kind, "url": self.url, "pid": self.proc.pid})
        return self.url

    async def stop(self, grace_s: float) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None:
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), grace_s)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), grace_s)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        log.info("agent stopped", extra={"agent": self.kind, "exit_code": proc.returncode})
