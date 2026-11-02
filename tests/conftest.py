from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from cross_model_review.agents.backends import (
    AgentBackend,
    ClaudeBackend,
    CodexBackend,
    RunContext,
    check_output_references,
    validate_output,
)
from cross_model_review.agents.server import bind_loopback, build_app
from cross_model_review.config import Config
from cross_model_review.models import WRITE_SKILLS, CLIRunInfo, SkillRequest

TOKEN = "test-token"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture(autouse=True)
def _isolated_git_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("CROSS_MODEL_REVIEW_ACTIVE", raising=False)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repository with staged, unstaged and untracked changes."""
    root = Path(os.path.realpath(tmp_path)) / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("# demo\n")
    (root / ".gitignore").write_text("ignored/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    (root / "app.py").write_text("def add(a, b):\n    return a - b\n")
    git(root, "add", "app.py")  # staged
    (root / "README.md").write_text("# demo\n\nunstaged edit\n")  # unstaged
    (root / "new.py").write_text("print('untracked')\n")  # untracked
    (root / "ignored").mkdir()
    (root / "ignored" / "cache.txt").write_text("ignored\n")
    return root


def repo_snapshot(root: Path) -> dict[str, str]:
    return {
        "head": git(root, "rev-parse", "HEAD").strip(),
        "branch": git(root, "symbolic-ref", "HEAD").strip(),
        "staged": git(root, "diff", "--cached", "--binary"),
        "staged_app": git(root, "show", ":app.py"),
        "status": git(root, "status", "--porcelain=v1", "--untracked-files=all"),
    }


# ------------------------------------------------------------- mock agents


def finding(fid: str = "F1", severity: str = "high", file: str | None = "app.py", **kw) -> dict:
    return {
        "id": fid,
        "severity": severity,
        "category": kw.get("category", "correctness"),
        "title": kw.get("title", f"{severity} issue {fid}"),
        "description": "description",
        "file": file,
        "line": kw.get("line", 2),
        "recommendation": "fix it",
        "confidence": "high",
    }


def review(*findings: dict) -> dict:
    return {"summary": f"{len(findings)} finding(s)", "findings": list(findings)}


def fix_all(request: SkillRequest, status: str = "fixed") -> dict:
    return {
        "summary": "fixed",
        "items": [
            {"finding_id": f.finding.id, "status": status, "notes": ""} for f in request.findings
        ],
        "files_changed": ["app.py"],
    }


def verify_all(request: SkillRequest, status: str = "resolved") -> dict:
    return {
        "summary": "verified",
        "items": [
            {"finding_id": f.finding.id, "status": status, "notes": ""} for f in request.findings
        ],
        "new_findings": [],
    }


Script = Callable[[SkillRequest, RunContext, int], Awaitable[Any]]


async def _default(request: SkillRequest, ctx: RunContext, n: int) -> dict:
    if request.skill == "fix_findings":
        with open(ctx.workspace / "app.py", "a") as fh:
            fh.write(f"# fix {n}\n")
        return fix_all(request)
    if request.skill == "verify_changes":
        return verify_all(request)
    return review()


class MockBackend(AgentBackend):
    """Scripted backend: no CLI, same A2A surface as the real agents."""

    def __init__(self, kind: str, script: dict[str, Script] | None = None) -> None:
        real = CodexBackend if kind == "codex" else ClaudeBackend
        self.agent_name = kind
        self.description = f"mock {kind}"
        self.skills = real.skills
        self.script = script or {}
        self.calls: dict[str, int] = defaultdict(int)
        self.requests: list[SkillRequest] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, request: SkillRequest, ctx: RunContext):
        self.calls[request.skill] += 1
        self.requests.append(request)
        self.started.set()
        fn = self.script.get(request.skill, _default)
        try:
            data = await fn(request, ctx, self.calls[request.skill])
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        output = validate_output(request.skill, data, ctx.workspace)
        check_output_references(request, output)
        info = CLIRunInfo(
            cli="mock",
            cli_version=None,
            model=None,
            sandbox="workspace-write" if request.skill in WRITE_SKILLS else "read-only",
            exit_code=0,
            duration_s=0.0,
        )
        return output, info


@asynccontextmanager
async def agent_server(backend: AgentBackend, *, lock_dir: Path, log_dir: Path, roots=None):
    sock = bind_loopback("127.0.0.1", 0)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    allowed = roots or [Path(os.path.realpath(tempfile.gettempdir()))]
    agent = build_app(
        backend, url=url, token=TOKEN, allowed_roots=allowed, lock_dir=lock_dir, log_dir=log_dir
    )
    server = uvicorn.Server(
        uvicorn.Config(agent.app, log_level="warning", lifespan="off", timeout_graceful_shutdown=1)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield url, agent
    finally:
        await agent.executor.shutdown()
        server.should_exit = True
        await asyncio.gather(task, return_exceptions=True)
        await agent.handler.aclose()


@pytest.fixture
def lock_dir(tmp_path) -> Path:
    return tmp_path / "locks"


@pytest.fixture
def make_config(tmp_path, lock_dir):
    def factory(codex_url: str, opus_url: str, **overrides: Any) -> Config:
        data: dict[str, Any] = {
            "output_dir": str(tmp_path / "out"),
            "lock_dir": str(lock_dir),
            "tests": {"commands": [{"name": "ok", "command": [sys.executable, "-c", "pass"]}]},
            "agents": {"codex": {"url": codex_url}, "opus": {"url": opus_url}},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        return Config.model_validate(data)

    return factory


@asynccontextmanager
async def mock_agents(tmp_path: Path, lock_dir: Path, codex: MockBackend, opus: MockBackend):
    async with (
        agent_server(codex, lock_dir=lock_dir, log_dir=tmp_path / "logs-codex") as (codex_url, _),
        agent_server(opus, lock_dir=lock_dir, log_dir=tmp_path / "logs-opus") as (opus_url, _),
    ):
        yield codex_url, opus_url


# --------------------------------------------------------------- fake CLIs

FAKE_CODEX = r"""
import json, os, subprocess, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.154.0"); sys.exit(0)
mode = os.environ.get("FAKE_MODE", "ok")
out = args[args.index("--output-last-message") + 1]
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "w") as fh:
        json.dump({"argv": args, "stdin": sys.stdin.read(), "pid": os.getpid()}, fh)
print(json.dumps({"type": "thread.started"}), flush=True)
print("progress on stderr", file=sys.stderr, flush=True)
if mode == "ok":
    with open(out, "w") as fh:
        json.dump({"summary": "ok", "findings": []}, fh)
elif mode == "invalid":
    with open(out, "w") as fh:
        fh.write("not json at all")
elif mode == "schema":
    with open(out, "w") as fh:
        json.dump({"summary": "ok", "findings": [{"id": "x", "severity": "fatal"}]}, fh)
elif mode == "fail":
    print(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}), flush=True)
    sys.exit(2)
elif mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.environ["FAKE_PIDS"], "w") as fh:
        json.dump([os.getpid(), child.pid], fh)
    time.sleep(120)
"""


@pytest.fixture
def fake_cli(tmp_path) -> Callable[[str], Path]:
    def write(source: str, name: str = "fake-codex") -> Path:
        path = tmp_path / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)
        return path

    return write
