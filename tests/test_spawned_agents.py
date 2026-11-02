"""Full path with spawned agent server processes and fake CLIs."""

from __future__ import annotations

import asyncio
import json
import sys

from conftest import FAKE_CODEX, repo_snapshot
from test_cli_adapters import FAKE_CLAUDE

from cross_model_review.config import Config
from cross_model_review.orchestrator.launcher import LocalAgentProcess
from cross_model_review.orchestrator.workflow import Orchestrator, RunOptions


async def test_full_run_with_spawned_agents(tmp_path, repo, lock_dir, fake_cli):
    config = Config.model_validate(
        {
            "output_dir": str(tmp_path / "out"),
            "lock_dir": str(lock_dir),
            "tests": {"commands": [{"name": "unit", "command": [sys.executable, "-c", "pass"]}]},
            "agents": {
                "codex": {"cli_path": str(fake_cli(FAKE_CODEX, "codex"))},
                "opus": {"cli_path": str(fake_cli(FAKE_CLAUDE, "claude"))},
            },
        }
    )
    before = repo_snapshot(repo)
    report = await Orchestrator(RunOptions(repo=repo, config=config)).run()

    assert report.gate.verdict == "passed", report.model_dump_json(indent=2)
    assert [t.state for t in report.iterations[0].tasks] == ["completed"] * 3
    assert repo_snapshot(repo) == before
    logs = tmp_path / "out" / report.run_id / "logs"
    assert (logs / "codex" / "server.stderr.jsonl").read_text().strip()
    # Structured JSON logs from the agent server process.
    first = json.loads((logs / "codex" / "server.stderr.jsonl").read_text().splitlines()[0])
    assert first["component"] == "agent-codex"


async def test_agent_exits_when_parent_goes_away(tmp_path, repo, lock_dir):
    config_path = tmp_path / "config.json"
    config_path.write_text(Config().model_dump_json())
    proc = LocalAgentProcess(
        "codex",
        config_path=config_path,
        token="t",
        allowed_roots=[repo],
        lock_dir=lock_dir,
        log_dir=tmp_path / "logs",
    )
    await proc.start(30)
    # Simulate the orchestrator dying: its end of the stdin pipe closes.
    proc.proc.stdin.close()
    await asyncio.wait_for(proc.proc.wait(), 15)
    assert proc.proc.returncode is not None
    await proc.stop(1)
