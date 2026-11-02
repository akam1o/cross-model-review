"""CLI adapters against fake `codex` / `claude` executables."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from conftest import FAKE_CODEX, agent_server, finding
from test_agents import call, connect, make_request

from cross_model_review.agents.backends import (
    AgentError,
    ClaudeBackend,
    CodexBackend,
    RunContext,
)
from cross_model_review.config import CodexAgentConfig, OpusAgentConfig


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Zombies (exited, not yet reaped by an unrelated parent) count as dead.
    try:
        import subprocess

        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
        return bool(state.stdout.strip()) and not state.stdout.strip().startswith("Z")
    except OSError:
        return True


async def wait_dead(pids: list[int], timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(alive(p) for p in pids):
            return True
        await asyncio.sleep(0.1)
    return False


def codex(fake_cli, **overrides) -> CodexBackend:
    return CodexBackend(CodexAgentConfig(cli_path=str(fake_cli(FAKE_CODEX)), **overrides))


def ctx(tmp_path, repo) -> RunContext:
    return RunContext(task_id="t1", workspace=repo, log_dir=tmp_path / "agent-logs")


async def test_codex_review_is_sandboxed_read_only(tmp_path, repo, fake_cli, monkeypatch):
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    backend = codex(fake_cli, model="some-model", reasoning_effort="high")
    output, info = await backend.run(make_request(repo), ctx(tmp_path, repo))

    assert output.findings == [] and info.sandbox == "read-only"
    assert info.cli_version == "codex-cli 0.154.0"
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    assert argv[0] == "exec"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    for flag in ("--ephemeral", "--ignore-user-config", "--json", "--output-schema"):
        assert flag in argv
    assert 'approval_policy="never"' in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[argv.index("--model") + 1] == "some-model"
    assert "untrusted data" in seen["stdin"]
    # stdout and stderr are captured separately.
    logs = tmp_path / "agent-logs" / "t1"
    assert "thread.started" in (logs / "codex.stdout.jsonl").read_text()
    assert "progress on stderr" in (logs / "codex.stderr.log").read_text()
    schema = json.loads((logs / "output-schema.json").read_text())
    assert schema["additionalProperties"] is False


async def test_codex_fix_is_workspace_write(tmp_path, repo, fake_cli, monkeypatch):
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    backend = codex(fake_cli)
    argv = backend.build_argv("fix_findings", repo, tmp_path / "s.json", tmp_path / "o.json")
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=false" in argv
    assert "--model" not in argv  # empty model -> CLI default


@pytest.mark.parametrize(
    ("mode", "code"),
    [("invalid", "invalid_output"), ("schema", "invalid_output"), ("fail", "cli_failed")],
)
async def test_codex_errors(tmp_path, repo, fake_cli, monkeypatch, mode, code):
    monkeypatch.setenv("FAKE_MODE", mode)
    with pytest.raises(AgentError) as excinfo:
        await codex(fake_cli).run(make_request(repo), ctx(tmp_path, repo))
    assert excinfo.value.code == code
    if mode == "fail":
        assert "boom" in excinfo.value.message


async def test_codex_missing_or_old_cli(tmp_path, repo, fake_cli):
    backend = CodexBackend(CodexAgentConfig(cli_path=str(tmp_path / "missing")))
    with pytest.raises(AgentError) as excinfo:
        await backend.run(make_request(repo), ctx(tmp_path, repo))
    assert excinfo.value.code == "cli_not_found"
    with pytest.raises(AgentError, match="older than"):
        await codex(fake_cli, min_cli_version="99.0.0").run(make_request(repo), ctx(tmp_path, repo))


async def test_codex_timeout_kills_process_group(tmp_path, repo, fake_cli, monkeypatch):
    pids_file = tmp_path / "pids.json"
    monkeypatch.setenv("FAKE_MODE", "sleep")
    monkeypatch.setenv("FAKE_PIDS", str(pids_file))
    backend = codex(fake_cli, review_timeout_s=2)
    with pytest.raises(AgentError) as excinfo:
        await backend.run(make_request(repo), ctx(tmp_path, repo))
    assert excinfo.value.code == "cli_timeout"
    assert await wait_dead(json.loads(pids_file.read_text()))


async def test_a2a_cancel_kills_cli_and_grandchildren(
    tmp_path, repo, fake_cli, monkeypatch, lock_dir
):
    pids_file = tmp_path / "pids.json"
    monkeypatch.setenv("FAKE_MODE", "sleep")
    monkeypatch.setenv("FAKE_PIDS", str(pids_file))
    backend = codex(fake_cli)
    async with agent_server(backend, lock_dir=lock_dir, log_dir=tmp_path / "l") as (url, _):
        client = await connect(url, {"review_changes"})
        task = asyncio.create_task(call(client, make_request(repo), timeout_s=60))
        deadline = time.monotonic() + 15
        while not pids_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        pids = json.loads(pids_file.read_text())
        assert all(alive(p) for p in pids)
        task.cancel()  # orchestrator-side cancellation -> A2A CancelTask
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await wait_dead(pids)
        # The workspace lock is released once the CLI is gone: a writer is
        # accepted (and fails later only because the fake prints a review).
        monkeypatch.setenv("FAKE_MODE", "ok")
        outcome = await call(client, make_request(repo, "fix_findings", [finding("F1")]))
        await client.close()
    assert outcome.record.state == "failed"
    assert "invalid_output" in outcome.record.error


FAKE_CLAUDE = r"""
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.277 (Claude Code)"); sys.exit(0)
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "w") as fh:
        json.dump({"argv": args, "cwd": os.getcwd()}, fh)
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "ok":
    out = {"type": "result", "subtype": "success", "is_error": False,
           "structured_output": {"summary": "s", "findings": []}}
elif mode == "error":
    out = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": "budget"}
else:
    out = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"x": 1}}
print(json.dumps(out))
"""


async def test_claude_is_read_only(tmp_path, repo, fake_cli, monkeypatch):
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    path = fake_cli(FAKE_CLAUDE, "fake-claude")
    backend = ClaudeBackend(OpusAgentConfig(cli_path=str(path), max_budget_usd=2.5))
    output, info = await backend.run(make_request(repo, "review_design"), ctx(tmp_path, repo))
    assert output.findings == [] and info.sandbox == "read-only"
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    for flag in (
        "--print",
        "--restricted",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--json-schema",
    ):
        assert flag in argv
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5"
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(repo)


@pytest.mark.parametrize(("mode", "code"), [("error", "cli_failed"), ("schema", "invalid_output")])
async def test_claude_errors(tmp_path, repo, fake_cli, monkeypatch, mode, code):
    monkeypatch.setenv("FAKE_MODE", mode)
    path = fake_cli(FAKE_CLAUDE, "fake-claude")
    backend = ClaudeBackend(OpusAgentConfig(cli_path=str(path)))
    with pytest.raises(AgentError) as excinfo:
        await backend.run(make_request(repo, "review_simplification"), ctx(tmp_path, repo))
    assert excinfo.value.code == code


async def test_claude_rejects_write_skill(tmp_path, repo, fake_cli):
    backend = ClaudeBackend(OpusAgentConfig(cli_path=str(fake_cli(FAKE_CLAUDE, "fake-claude"))))
    with pytest.raises(AgentError):
        await backend.run(make_request(repo, "fix_findings", [finding("F1")]), ctx(tmp_path, repo))


async def test_absolute_paths_inside_workspace_are_relativized(tmp_path, repo, fake_cli):
    from cross_model_review.agents.backends import validate_output

    data = {"summary": "s", "findings": [finding("F1", file=str(repo / "app.py"))]}
    assert validate_output("review_changes", data, repo).findings[0].file == "app.py"
    with pytest.raises(AgentError):
        validate_output(
            "review_changes",
            {"summary": "s", "findings": [finding("F1", file="/etc/passwd")]},
            repo,
        )
