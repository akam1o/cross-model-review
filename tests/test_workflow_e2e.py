"""Workflow end-to-end tests: real Git, real A2A over loopback HTTP, mock agents."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from conftest import (
    TOKEN,
    MockBackend,
    finding,
    fix_all,
    git,
    mock_agents,
    repo_snapshot,
    review,
    verify_all,
)

from cross_model_review.agents.backends import AgentError
from cross_model_review.orchestrator.workflow import (
    Orchestrator,
    RunCancelled,
    RunOptions,
    UsageError,
)


def orchestrator(repo: Path, config) -> Orchestrator:
    return Orchestrator(RunOptions(repo=repo, config=config, agent_token=TOKEN))


def check(report, name: str) -> str:
    return next(c.status for c in report.gate.checks if c.name == name)


async def test_clean_review_passes(tmp_path, repo, lock_dir, make_config):
    codex, opus = MockBackend("codex"), MockBackend("opus")
    opus.script["review_design"] = lambda r, c, n: _value(review(finding("D1", "low")))
    before = repo_snapshot(repo)
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou)).run()

    assert report.status == "converged"
    assert report.gate.verdict == "passed", report.gate
    assert report.final_review_completed
    assert report.apply.status == "not_needed"
    assert len(report.iterations) == 1
    assert {t.skill for t in report.iterations[0].tasks} == {
        "review_changes",
        "review_design",
        "review_simplification",
    }
    assert [f.status for f in report.findings] == ["not_selected"]
    assert repo_snapshot(repo) == before
    # The review target carried staged / unstaged / untracked classification.
    target = codex.requests[0].target
    flags = {f.path: (f.staged, f.unstaged, f.untracked) for f in target.changed_files}
    assert flags == {
        "app.py": (True, False, False),
        "README.md": (False, True, False),
        "new.py": (False, False, True),
    }
    assert "ignored/cache.txt" not in target.diff
    assert (Path(report.output_dir) / "report.md").is_file()
    # Worktree was cleaned up and the snapshot ref kept for recovery.
    assert git(repo, "worktree", "list").count("\n") == 1
    assert git(repo, "rev-parse", "--verify", report.snapshot_ref).strip()


async def test_finding_is_fixed_then_rereviewed(tmp_path, repo, lock_dir, make_config):
    async def codex_review(request, ctx, n):
        return review(finding("C1", "high")) if n == 1 else review()

    codex = MockBackend("codex", {"review_changes": codex_review})
    opus = MockBackend("opus")
    before = repo_snapshot(repo)
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou)).run()

    assert report.status == "converged"
    assert report.gate.verdict == "passed", report.gate
    assert len(report.iterations) == 2
    skills = [t.skill for t in report.iterations[0].tasks]
    assert skills[:3] == ["review_changes", "fix_findings", "verify_changes"]
    fixed = report.findings[0]
    assert fixed.status == "resolved"
    # The fix request referenced the orchestrator-assigned key.
    fix_request = next(r for r in codex.requests if r.skill == "fix_findings")
    assert [f.finding.id for f in fix_request.findings] == [fixed.key]

    # Fixes are applied as unstaged changes; staged content and HEAD untouched.
    assert report.apply.status == "applied"
    after = repo_snapshot(repo)
    assert after["head"] == before["head"]
    assert after["staged"] == before["staged"]
    assert after["staged_app"] == before["staged_app"]
    assert (repo / "app.py").read_text().endswith("# fix 1\n")
    assert (repo / "README.md").read_text() == "# demo\n\nunstaged edit\n"
    assert (repo / "new.py").exists()
    assert Path(report.apply.patch_path).read_text().count("+# fix 1") == 1


async def test_max_iterations_exceeded(tmp_path, repo, lock_dir, make_config):
    async def always_high(request, ctx, n):
        return review(finding(f"C{n}", "high"))

    async def unresolved(request, ctx, n):
        return verify_all(request, "unresolved")

    codex = MockBackend("codex", {"review_changes": always_high, "verify_changes": unresolved})
    opus = MockBackend("opus")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou, max_iterations=2)).run()

    assert report.status == "max_iterations_reached"
    assert report.gate.verdict == "failed"
    assert check(report, "high_findings") == "fail"
    assert check(report, "final_review") == "pass"
    # Two fix rounds plus one final review-only round.
    assert len(report.iterations) == 3
    assert codex.calls["fix_findings"] == 2
    assert "fix_findings" not in {t.skill for t in report.iterations[-1].tasks}
    unresolved_keys = [f.key for f in report.findings if f.status == "unresolved"]
    assert (
        unresolved_keys
        and "Unresolved findings" in (Path(report.output_dir) / "report.md").read_text()
    )


async def test_opus_findings_are_fixed_by_codex(tmp_path, repo, lock_dir, make_config):
    async def design(request, ctx, n):
        return review(finding("D1", "critical", category="design")) if n == 1 else review()

    codex, opus = MockBackend("codex"), MockBackend("opus", {"review_design": design})
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou)).run()

    assert report.gate.verdict == "passed", report.gate
    fix_request = next(r for r in codex.requests if r.skill == "fix_findings")
    assert [f.source for f in fix_request.findings] == ["opus"]


async def test_no_tests_is_unverified(tmp_path, repo, lock_dir, make_config):
    codex, opus = MockBackend("codex"), MockBackend("opus")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou, tests={"commands": []})).run()

    assert report.gate.verdict == "unverified"
    assert check(report, "required_tests") == "unverified"
    assert report.tests.status == "unverified"


async def test_failing_tests_fail_the_gate(tmp_path, repo, lock_dir, make_config):
    async def no_change_fix(request, ctx, n):
        return fix_all(request, "not_fixed")

    codex = MockBackend("codex", {"fix_findings": no_change_fix})
    opus = MockBackend("opus")
    failing = {
        "commands": [{"name": "unit", "command": [sys.executable, "-c", "raise SystemExit(1)"]}]
    }
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou, tests=failing)).run()

    assert report.tests.status == "failed"
    assert check(report, "required_tests") == "fail"
    assert report.gate.verdict == "failed"
    # The failing test was handed to Codex as a finding, which made no change.
    fix_request = next(r for r in codex.requests if r.skill == "fix_findings")
    assert fix_request.findings[0].source == "tests"
    assert report.status == "no_progress"


async def test_cli_failure_aborts(tmp_path, repo, lock_dir, make_config):
    async def broken(request, ctx, n):
        raise AgentError("cli_failed", "codex exited with 2")

    codex, opus = MockBackend("codex", {"review_changes": broken}), MockBackend("opus")
    before = repo_snapshot(repo)
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou)).run()

    assert report.status == "aborted"
    assert report.gate.verdict == "failed"
    assert check(report, "a2a_tasks") == "fail"
    assert report.iterations[0].tasks[0].state == "failed"
    assert "cli_failed" in report.iterations[0].tasks[0].error
    assert repo_snapshot(repo) == before


async def test_task_timeout_cancels_agent(tmp_path, repo, lock_dir, make_config):
    async def hang(request, ctx, n):
        await asyncio.sleep(60)

    codex, opus = MockBackend("codex", {"review_changes": hang}), MockBackend("opus")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        config = make_config(
            cu,
            ou,
            agents={"codex": {"url": cu, "review_timeout_s": 0.5}, "opus": {"url": ou}},
            a2a={"task_timeout_margin_s": 0.5},
        )
        report = await orchestrator(repo, config).run()
        await asyncio.wait_for(codex.cancelled.wait(), 5)

    assert report.status == "aborted"
    assert report.iterations[0].tasks[0].state == "timeout"
    assert report.gate.verdict == "failed"


async def test_orchestrator_cancellation_propagates(tmp_path, repo, lock_dir, make_config):
    async def hang(request, ctx, n):
        await asyncio.sleep(60)

    codex, opus = MockBackend("codex", {"review_changes": hang}), MockBackend("opus")
    before = repo_snapshot(repo)
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        run = asyncio.create_task(orchestrator(repo, make_config(cu, ou)).run())
        await asyncio.wait_for(codex.started.wait(), 10)
        run.cancel()
        with pytest.raises(RunCancelled) as excinfo:
            await run
        await asyncio.wait_for(codex.cancelled.wait(), 5)

    report = excinfo.value.report
    assert report.status == "cancelled"
    assert report.gate.verdict == "failed"
    assert report.iterations[0].tasks == [] or report.iterations[0].tasks[0].state != "completed"
    assert repo_snapshot(repo) == before
    assert git(repo, "worktree", "list").count("\n") == 1


async def test_read_only_violation_is_detected(tmp_path, repo, lock_dir, make_config):
    async def sneaky(request, ctx, n):
        (ctx.workspace / "app.py").write_text("tampered\n")
        return review()

    codex, opus = MockBackend("codex", {"review_changes": sneaky}), MockBackend("opus")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou)).run()

    assert report.status == "aborted"
    assert check(report, "git_integrity") == "fail"
    # The tampered tree is never adopted, so nothing is applied.
    assert report.apply.status == "not_needed"
    assert "tampered" not in (repo / "app.py").read_text()


async def test_user_edits_during_run_are_preserved(tmp_path, repo, lock_dir, make_config):
    async def codex_review(request, ctx, n):
        return review(finding("C1", "high")) if n == 1 else review()

    async def fix_and_user_edit(request, ctx, n):
        (ctx.workspace / "app.py").write_text("fixed by agent\n")
        (repo / "README.md").write_text("user kept working\n")  # concurrent user edit
        return fix_all(request)

    codex = MockBackend(
        "codex", {"review_changes": codex_review, "fix_findings": fix_and_user_edit}
    )
    opus = MockBackend("opus")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou)).run()

    assert report.apply.status == "conflict"
    assert check(report, "git_integrity") == "fail"
    assert report.gate.verdict == "failed"
    assert (repo / "README.md").read_text() == "user kept working\n"
    assert (repo / "app.py").read_text() == "def add(a, b):\n    return a - b\n"
    assert "fixed by agent" in Path(report.apply.patch_path).read_text()


async def test_agent_commit_is_detected(tmp_path, repo, lock_dir, make_config):
    async def codex_review(request, ctx, n):
        return review(finding("C1", "high"))

    async def committing_fix(request, ctx, n):
        (ctx.workspace / "app.py").write_text("x\n")
        git(ctx.workspace, "commit", "-qam", "sneaky")
        return fix_all(request)

    codex = MockBackend("codex", {"review_changes": codex_review, "fix_findings": committing_fix})
    opus = MockBackend("opus")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou)).run()

    assert report.status == "aborted"
    assert any("HEAD" in issue for issue in report.git_issues)
    assert report.gate.verdict == "failed"


async def test_in_place_mode(tmp_path, repo, lock_dir, make_config):
    async def codex_review(request, ctx, n):
        return review(finding("C1", "high")) if n == 1 else review()

    codex = MockBackend("codex", {"review_changes": codex_review})
    opus = MockBackend("opus")
    before = repo_snapshot(repo)
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        config = make_config(cu, ou, workspace={"mode": "in-place"})
        report = await orchestrator(repo, config).run()

    assert report.gate.verdict == "passed", report.gate
    assert report.apply.status == "in_place"
    assert codex.requests[0].workspace.path == str(repo)
    after = repo_snapshot(repo)
    assert after["staged"] == before["staged"]
    assert (repo / "app.py").read_text().endswith("# fix 1\n")


async def test_no_changes(tmp_path, repo, lock_dir, make_config):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "all")
    report = await orchestrator(
        repo, make_config("http://127.0.0.1:9/", "http://127.0.0.1:9/")
    ).run()
    assert report.status == "no_changes"
    assert report.gate.verdict == "unverified"


async def test_refuses_in_progress_merge_and_parallel_runs(tmp_path, repo, lock_dir, make_config):
    from cross_model_review.locks import run_lock

    config = make_config("http://127.0.0.1:9/", "http://127.0.0.1:9/")
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    with run_lock(lock_dir, common), pytest.raises(UsageError, match="another"):
        await orchestrator(repo, config).run()

    (Path(common) / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))
    with pytest.raises(UsageError, match="merge"):
        await orchestrator(repo, config).run()


async def test_branch_review_with_base(tmp_path, repo, lock_dir, make_config):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature work")
    codex, opus = MockBackend("codex"), MockBackend("opus")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await Orchestrator(
            RunOptions(repo=repo, config=make_config(cu, ou), base="HEAD~1", agent_token=TOKEN)
        ).run()
    assert report.gate.verdict == "passed"
    files = {f.path: f.committed for f in codex.requests[0].target.changed_files}
    assert files == {"app.py": True, "README.md": True, "new.py": True}


CREATE_CACHE = "import os; os.makedirs('__pycache__', exist_ok=True); open('__pycache__/x.pyc','w')"
TOUCH_TRACKED = "open('app.py','a').write('# test side effect\\n')"


async def test_test_caches_are_removed_in_worktree(tmp_path, repo, lock_dir, make_config):
    tests = {"commands": [{"name": "unit", "command": [sys.executable, "-c", CREATE_CACHE]}]}
    codex, opus = MockBackend("codex"), MockBackend("opus")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        report = await orchestrator(repo, make_config(cu, ou, tests=tests)).run()
    assert report.gate.verdict == "passed", report.gate
    assert report.warnings and "untracked" in report.warnings[0]
    assert not (repo / "__pycache__").exists()


@pytest.mark.parametrize(
    ("mode", "script"),
    [("worktree", TOUCH_TRACKED), ("in-place", CREATE_CACHE)],
)
async def test_test_side_effects_abort(tmp_path, repo, lock_dir, make_config, mode, script):
    tests = {"commands": [{"name": "unit", "command": [sys.executable, "-c", script]}]}
    codex, opus = MockBackend("codex"), MockBackend("opus")
    async with mock_agents(tmp_path, lock_dir, codex, opus) as (cu, ou):
        config = make_config(cu, ou, tests=tests, workspace={"mode": mode})
        report = await orchestrator(repo, config).run()
    assert report.status == "aborted"
    assert check(report, "git_integrity") == "fail"
    assert ".gitignore" in report.git_issues[0]


def test_example_config_is_valid():
    from cross_model_review.config import load_config

    example = Path(__file__).resolve().parents[1] / "config" / "cross-model-review.example.toml"
    config, used = load_config(None, example)
    assert used == [str(example)] and config.max_iterations == 3


async def _value(value):
    return value
