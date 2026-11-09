"""Review-only mode and --no-config-discovery."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import TOKEN, MockBackend, finding, git, mock_agents, repo_snapshot, review
from test_cli_adapters import FAKE_CLAUDE

from cross_model_review.cli import EXIT_FAILED, EXIT_UNVERIFIED, EXIT_USAGE, main
from cross_model_review.config import Config, load_config
from cross_model_review.models import IterationRecord
from cross_model_review.orchestrator.workflow import Abort, Orchestrator, RunOptions

# Reports one High finding until app.py contains "# fixed". Fix and verify
# sessions leave marker files, so a test can prove they never ran.
FAKE_CODEX_REVIEWER = r"""
import json, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.154.0"); sys.exit(0)
out = args[args.index("--output-last-message") + 1]
prompt = sys.stdin.read()
MARKERS = @MARKERS@
BLOCKING = @BLOCKING@
def ids():
    block = prompt.split("Findings:\n```json\n", 1)[1].split("\n```", 1)[0]
    return [f["id"] for f in json.loads(block)]
if "Task: fix_findings" in prompt:
    open(MARKERS + "/fix", "w").close()
    with open("app.py", "a") as fh:
        fh.write("# fixed\n")
    result = {"summary": "fixed", "files_changed": ["app.py"],
              "items": [{"finding_id": i, "status": "fixed", "notes": ""} for i in ids()]}
elif "Task: verify_changes" in prompt:
    open(MARKERS + "/verify", "w").close()
    result = {"summary": "ok", "new_findings": [],
              "items": [{"finding_id": i, "status": "resolved", "notes": ""} for i in ids()]}
else:
    pending = BLOCKING and "# fixed" not in open("app.py").read()
    result = {"summary": "s", "findings": [{
        "id": "C1", "severity": "high", "category": "correctness",
        "title": "subtraction instead of addition", "description": "d", "file": "app.py",
        "line": 2, "recommendation": "use +", "confidence": "high"}] if pending else []}
with open(out, "w") as fh:
    json.dump(result, fh)
"""

MARKER_NAMES = ("fix", "verify", "setup", "tests")


def touch(path: Path) -> list[str]:
    return [sys.executable, "-c", f"open({str(path)!r}, 'w').close()"]


def trusted_config(tmp_path, lock_dir, fake_cli, *, blocking: bool = True, **extra) -> Config:
    markers = tmp_path / "markers"
    markers.mkdir(exist_ok=True)
    codex = FAKE_CODEX_REVIEWER.replace("@MARKERS@", repr(str(markers))).replace(
        "@BLOCKING@", repr(blocking)
    )
    data = {
        "output_dir": str(tmp_path / "out"),
        "lock_dir": str(lock_dir),
        "workspace": {"setup_commands": [{"name": "setup", "command": touch(markers / "setup")}]},
        "tests": {"commands": [{"name": "unit", "command": touch(markers / "tests")}]},
        "agents": {
            "codex": {"cli_path": str(fake_cli(codex, "codex"))},
            "opus": {"cli_path": str(fake_cli(FAKE_CLAUDE, "claude"))},
        },
    }
    for key, value in extra.items():
        data[key] = {**data[key], **value} if isinstance(value, dict) else value
    return Config.model_validate(data)


def markers_seen(tmp_path) -> set[str]:
    return {name for name in MARKER_NAMES if (tmp_path / "markers" / name).exists()}


def check(report, name: str) -> str:
    return next(c.status for c in report.gate.checks if c.name == name)


# -------------------------------------------------- real orchestrator, fake CLIs


@pytest.mark.parametrize("mode", ["worktree", "in-place"])
async def test_review_only_leaves_blocking_finding_unresolved(
    tmp_path, repo, lock_dir, fake_cli, mode
):
    config = trusted_config(
        tmp_path,
        lock_dir,
        fake_cli,
        review_only=True,
        max_iterations=2,
        workspace={"mode": mode},
    )
    before = repo_snapshot(repo)
    app_before = (repo / "app.py").read_text()
    report = await Orchestrator(RunOptions(repo=repo, config=config)).run()

    assert report.review_only is True
    assert report.status == "reviewed", report.errors
    # The snapshot cannot change, so one round is final despite max_iterations=2.
    assert len(report.iterations) == 1
    assert sorted(t.skill for t in report.iterations[0].tasks) == [
        "review_changes",
        "review_design",
        "review_simplification",
    ]
    assert markers_seen(tmp_path) == set()
    assert report.gate.verdict == "failed"
    assert check(report, "high_findings") == "fail"
    assert check(report, "required_tests") == "unverified"
    assert check(report, "final_review") == "pass"
    assert check(report, "git_integrity") == "pass"
    assert check(report, "a2a_tasks") == "pass"
    assert [(f.finding.severity, f.status) for f in report.findings] == [("high", "unresolved")]
    assert report.tests.runs == [] and "not executed" in report.tests.detail
    assert report.iterations[0].tests is None
    assert report.apply.status == "not_needed" and report.apply.patch_path is None
    assert repo_snapshot(repo) == before
    assert (repo / "app.py").read_text() == app_before
    assert git(repo, "worktree", "list").count("\n") == 1

    out = Path(report.output_dir)
    assert json.loads((out / "report.json").read_text())["review_only"] is True
    markdown = (out / "report.md").read_text()
    assert "Review-only: **yes**" in markdown and "upper bound" in markdown
    assert json.loads((out / "agent-config.json").read_text())["review_only"] is True


async def test_default_mode_still_fixes(tmp_path, repo, lock_dir, fake_cli):
    config = trusted_config(tmp_path, lock_dir, fake_cli)
    report = await Orchestrator(RunOptions(repo=repo, config=config)).run()

    assert report.review_only is False
    assert report.status == "converged"
    assert report.gate.verdict == "passed", report.gate
    # The same fake CLI and commands do run when fixing is allowed.
    assert markers_seen(tmp_path) == set(MARKER_NAMES)
    assert report.findings[0].status == "resolved"
    assert report.apply.status == "applied"
    assert (repo / "app.py").read_text().endswith("# fixed\n")


# ------------------------------------------------------------- mock agents


async def test_review_only_clean_run_is_unverified(tmp_path, repo, lock_dir, make_config):
    opus = MockBackend(
        "opus", {"review_design": lambda r, c, n: _value(review(finding("D1", "low")))}
    )
    codex = MockBackend("codex")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        config = make_config(cu, ou, review_only=True, max_iterations=2)
        report = await Orchestrator(RunOptions(repo=repo, config=config, agent_token=TOKEN)).run()

    assert report.status == "reviewed"
    # No blocking findings, but tests did not run: never "passed".
    assert report.gate.verdict == "unverified"
    assert check(report, "required_tests") == "unverified"
    assert check(report, "high_findings") == "pass"
    assert [f.status for f in report.findings] == ["not_selected"]
    # All three reviews saw the same snapshot.
    targets = [r.target for r in codex.requests + opus.requests]
    assert len(targets) == 3 and all(t == targets[0] for t in targets)
    assert set(codex.calls) == {"review_changes"}


async def test_review_only_guards(tmp_path, repo, make_config):
    config = make_config("http://127.0.0.1:9/", "http://127.0.0.1:9/", review_only=True)
    orch = Orchestrator(RunOptions(repo=repo, config=config))
    it = IterationRecord(number=1, tasks=[], findings=[], fixed_files=[])
    with pytest.raises(Abort, match="review-only"):
        await orch._fix(["codex-1-1"], 1, it)
    for skill in ("fix_findings", "verify_changes"):
        with pytest.raises(Abort, match="review-only"):
            await orch._call(skill, 1, it)
    with pytest.raises(Abort, match="review-only"):
        await orch._run_commands(config.tests.commands, "tests-1")


# -------------------------------------------------------------- native CLI


def _plant_untrusted_configs(tmp_path, repo, monkeypatch) -> Path:
    """User and repository configs that would leave markers if they were used."""
    evil = tmp_path / "evil"
    evil.mkdir()
    script = tmp_path / "bin" / "evil-cli"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"#!/bin/sh\ntouch {evil}/cli\n")
    script.chmod(0o755)
    body = (
        f'[agents.codex]\ncli_path = "{script}"\n[agents.opus]\ncli_path = "{script}"\n'
        f'[[tests.commands]]\nname = "x"\ncommand = ["touch", "{evil}/tests"]\n'
        f'[[workspace.setup_commands]]\nname = "x"\ncommand = ["touch", "{evil}/setup"]\n'
    )
    xdg = tmp_path / "xdg"
    (xdg / "cross-model-review").mkdir(parents=True)
    (xdg / "cross-model-review" / "config.toml").write_text(body)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    (repo / ".cross-model-review.toml").write_text(body)
    return evil


@pytest.mark.parametrize(("blocking", "exit_code"), [(True, EXIT_FAILED), (False, EXIT_UNVERIFIED)])
def test_cli_review_only_with_trusted_json(
    tmp_path, repo, lock_dir, fake_cli, monkeypatch, capsys, blocking, exit_code
):
    monkeypatch.setattr("cross_model_review.logs.setup_logging", lambda *a, **k: None)
    evil = _plant_untrusted_configs(tmp_path, repo, monkeypatch)
    trusted = tmp_path / "trusted.json"
    trusted.write_text(
        trusted_config(tmp_path, lock_dir, fake_cli, blocking=blocking).model_dump_json()
    )
    before = repo_snapshot(repo)

    code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--review-only",
            "--max-iterations",
            "2",
            "--no-config-discovery",
            "--config",
            str(trusted),
            "--json",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == exit_code, report
    assert report["review_only"] is True
    assert report["status"] == "reviewed"
    assert len(report["iterations"]) == 1
    assert report["gate"]["verdict"] == ("failed" if blocking else "unverified")
    assert report["tests"]["status"] == "unverified"
    assert list(evil.iterdir()) == []
    assert markers_seen(tmp_path) == set()
    assert repo_snapshot(repo) == before


def test_no_config_discovery_requires_config(repo, capsys):
    assert main(["run", "--repo", str(repo), "--no-config-discovery"]) == EXIT_USAGE
    assert "requires --config" in capsys.readouterr().err


def test_load_config_without_discovery(tmp_path, repo, monkeypatch):
    evil = _plant_untrusted_configs(tmp_path, repo, monkeypatch)
    trusted = tmp_path / "trusted.json"
    expected = Config.model_validate({"review_only": True, "max_iterations": 2})
    trusted.write_text(expected.model_dump_json())

    config, used = load_config(repo, trusted, discover=False)
    assert config == expected and used == [str(trusted)]
    # With discovery (the default) the planted files are merged as before.
    config, used = load_config(repo, None)
    assert len(used) == 2 and config.agents.codex.cli_path.endswith("evil-cli")
    with pytest.raises(ValueError, match="explicit"):
        load_config(repo, None, discover=False)
    assert list(evil.iterdir()) == []


def test_json_config_must_be_an_object(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="object"):
        load_config(None, path, discover=False)


async def _value(value):
    return value
