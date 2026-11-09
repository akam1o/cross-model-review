"""The review / fix / re-review workflow."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cross_model_review.agents.backends import RECURSION_GUARD_ENV
from cross_model_review.agents.server import TOKEN_ENV
from cross_model_review.config import Config, TestCommand
from cross_model_review.gitops import Git, GitError, RepoState
from cross_model_review.locks import LockBusy, default_lock_dir, run_lock, workspace_lock
from cross_model_review.models import (
    BLOCKING_SEVERITIES,
    REVIEW_SKILLS,
    ApplyResult,
    ChangedFile,
    Finding,
    FixOutput,
    IterationRecord,
    Report,
    ReviewOutput,
    ReviewTarget,
    SkillName,
    SkillRequest,
    TaskFinding,
    TaskRecord,
    TestRun,
    TestsSummary,
    TrackedFinding,
    VerifyOutput,
    WorkspaceRef,
)
from cross_model_review.orchestrator.client import AgentClient, TaskOutcome
from cross_model_review.orchestrator.gate import evaluate_gate
from cross_model_review.orchestrator.launcher import LocalAgentProcess
from cross_model_review.orchestrator.report import write_report
from cross_model_review.procs import ExecutableNotFound, run_process, shell_argv, tail

log = logging.getLogger(__name__)

CODEX_SKILLS = {"review_changes", "fix_findings", "verify_changes"}
OPUS_SKILLS = {"review_design", "review_simplification"}
FINAL_REVIEW_SKILLS: tuple[SkillName, ...] = (
    "review_changes",
    "review_design",
    "review_simplification",
)


class UsageError(Exception):
    """The run cannot start (bad repository state, another run active, ...)."""


class Abort(Exception):
    """Stop the workflow; the reason is recorded in the report."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


@dataclass
class RunOptions:
    repo: Path
    config: Config
    base: str = "HEAD"
    run_id: str = field(default_factory=new_run_id)
    # Token for agents configured by URL (agents.*.url). Spawned agents get a
    # fresh random token.
    agent_token: str | None = None


@dataclass
class _ReviewRecord:
    skill: str
    tree: str
    keys: list[str]


class Orchestrator:
    def __init__(self, options: RunOptions) -> None:
        self.opts = options
        self.cfg = options.config
        self.run_id = options.run_id
        self.findings: dict[str, TrackedFinding] = {}
        self.iterations: list[IterationRecord] = []
        self.tasks: list[TaskRecord] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.git_issues: list[str] = []
        self.reviews: list[_ReviewRecord] = []
        self.pending: list[str] = []
        self.no_progress = False
        self.latest_tests: TestsSummary | None = None
        self.latest_tests_tree: str | None = None
        self.agents: dict[str, AgentClient] = {}
        self.processes: list[LocalAgentProcess] = []

    # ------------------------------------------------------------------ setup

    async def _preflight(self) -> None:
        if os.environ.get(RECURSION_GUARD_ENV):
            raise UsageError("refusing to run inside a cross-model-review agent session")
        try:
            self.root = await Git(self.opts.repo).toplevel()
        except GitError as exc:
            raise UsageError(f"not a Git repository: {self.opts.repo}") from exc
        self.git = Git(self.root)
        if await self.git.head_commit() is None:
            raise UsageError("the repository has no commits; commit once before reviewing")
        ops = await self.git.in_progress_operations()
        if ops:
            raise UsageError(f"a Git operation is in progress ({', '.join(ops)}); finish it first")
        if await self.git.has_unmerged_paths():
            raise UsageError("the index has unmerged paths; resolve conflicts first")
        self.common_dir = await self.git.common_dir()
        self.lock_dir = Path(self.cfg.lock_dir) if self.cfg.lock_dir else default_lock_dir()
        self.out_dir = (
            Path(self.cfg.output_dir) / self.run_id
            if self.cfg.output_dir
            else self.common_dir / "cross-model-review" / "runs" / self.run_id
        )
        self.out_dir.mkdir(parents=True, exist_ok=True)

    async def _snapshot(self) -> None:
        self.initial: RepoState = await self.git.state()
        head = self.initial.head_commit
        if self.opts.base == "HEAD":
            self.base_commit = head
        else:
            try:
                base = await self.git.resolve_commit(self.opts.base)
            except GitError as exc:
                raise UsageError(f"unknown base ref: {self.opts.base}") from exc
            self.base_commit = await self.git.merge_base(base, head)
        self.base_tree = await self.git.tree_of(self.base_commit)
        self.snapshot_commit = await self.git.commit_tree(
            self.initial.worktree_tree, head, f"cross-model-review snapshot {self.run_id}"
        )
        self.snapshot_ref = f"refs/cross-model-review/{self.run_id}"
        await self.git.update_ref(self.snapshot_ref, self.snapshot_commit)
        self.initial_files = {
            f.path: f
            for f in await self.git.changed_files(
                self.base_commit, self.initial.worktree_tree, head
            )
        }

    async def _prepare_workspace(self) -> None:
        if self.cfg.workspace.mode == "in-place":
            self.workspace = self.root
        else:
            parent = Path(os.path.realpath(tempfile.mkdtemp(prefix="cmr-worktree-")))
            self.workspace = parent / self.root.name
            await self.git.add_worktree(
                self.workspace, self.snapshot_commit, self.initial.head_commit
            )
        self.ws_git = Git(self.workspace)
        self.expected_tree = await self.ws_git.worktree_tree()
        if self.expected_tree != self.initial.worktree_tree:
            raise Abort("workspace does not match the snapshot")
        if (
            self.cfg.workspace.mode == "worktree"
            and self.cfg.workspace.setup_commands
            and not self.cfg.review_only
        ):
            summary = await self._run_commands(self.cfg.workspace.setup_commands, "setup")
            if summary.status == "failed":
                raise Abort(f"workspace setup failed: {summary.detail}")

    async def _start_agents(self) -> None:
        token = secrets.token_urlsafe(32)
        config_path = self.out_dir / "agent-config.json"
        config_path.write_text(self.cfg.model_dump_json(indent=2), encoding="utf-8")
        for name, skills, agent_cfg in (
            ("codex", CODEX_SKILLS, self.cfg.agents.codex),
            ("opus", OPUS_SKILLS, self.cfg.agents.opus),
        ):
            if agent_cfg.url:
                url = agent_cfg.url
                agent_token = self.opts.agent_token or os.environ.get(TOKEN_ENV, "")
            else:
                proc = LocalAgentProcess(
                    name,
                    config_path=config_path,
                    token=token,
                    allowed_roots=[self.workspace],
                    lock_dir=self.lock_dir,
                    log_dir=self.out_dir / "logs" / name,
                )
                self.processes.append(proc)
                url = await proc.start(self.cfg.a2a.startup_timeout_s)
                agent_token = token
            client = AgentClient(name, url, agent_token, skills)
            await client.connect()
            self.agents[name] = client

    async def _stop_agents(self) -> None:
        for client in self.agents.values():
            with contextlib.suppress(Exception):
                await client.close()
        await asyncio.gather(
            *(p.stop(self.cfg.a2a.cancel_timeout_s) for p in self.processes),
            return_exceptions=True,
        )

    # ------------------------------------------------------------- primitives

    async def _fingerprint(self) -> str:
        return await self.ws_git.worktree_tree()

    async def _target(self) -> ReviewTarget:
        diff, truncated = await self.git.diff_text(
            self.base_commit, self.expected_tree, self.cfg.max_diff_bytes
        )
        files = []
        for status, path, old in await self.git.name_status(self.base_commit, self.expected_tree):
            origin = self.initial_files.get(path)
            files.append(
                origin.model_copy(update={"change": _kind(status), "old_path": old})
                if origin
                else ChangedFile(path=path, change=_kind(status), old_path=old)
            )
        return ReviewTarget(
            base_ref=self.opts.base,
            base_commit=self.base_commit,
            changed_files=files,
            diff=diff,
            diff_truncated=truncated,
        )

    def _timeout(self, skill: str) -> float:
        codex, opus = self.cfg.agents.codex, self.cfg.agents.opus
        base = {
            "review_changes": codex.review_timeout_s,
            "fix_findings": codex.fix_timeout_s,
            "verify_changes": codex.verify_timeout_s,
            "review_design": opus.review_timeout_s,
            "review_simplification": opus.review_timeout_s,
        }[skill]
        return base + self.cfg.a2a.task_timeout_margin_s

    async def _call(
        self,
        skill: SkillName,
        iteration: int,
        it: IterationRecord,
        findings: list[TrackedFinding] | None = None,
    ) -> TaskOutcome:
        if self.cfg.review_only and skill not in REVIEW_SKILLS:
            raise Abort(f"{skill} is not allowed in review-only mode")
        agent = self.agents["codex" if skill in CODEX_SKILLS else "opus"]
        request = SkillRequest(
            skill=skill,
            run_id=self.run_id,
            iteration=iteration,
            workspace=WorkspaceRef(kind="local_path", path=str(self.workspace)),
            target=await self._target(),
            findings=[
                TaskFinding(source=t.source, finding=t.finding.model_copy(update={"id": t.key}))
                for t in findings or []
            ],
            instructions=self.cfg.review_instructions,
        )
        outcome = await agent.call(
            request,
            timeout_s=self._timeout(skill),
            cancel_timeout_s=self.cfg.a2a.cancel_timeout_s,
        )
        self.tasks.append(outcome.record)
        it.tasks.append(outcome.record)
        if not outcome.ok:
            raise Abort(
                f"{agent.name}.{skill} {outcome.record.state}: {outcome.record.error or ''}".strip()
            )
        return outcome

    async def _check_read_only(self, what: str) -> None:
        after = await self._fingerprint()
        if after != self.expected_tree:
            self.git_issues.append(f"{what} modified the workspace although it is read-only")
            raise Abort(f"{what} violated read-only access")

    async def _check_git_state(self, what: str) -> None:
        """Agents must never move HEAD or (in-place) touch the user's index."""
        if await self.ws_git.head_commit() != self.initial.head_commit:
            self.git_issues.append(f"{what} changed HEAD (commit/checkout/reset is forbidden)")
            raise Abort(f"{what} changed HEAD")
        if self.cfg.workspace.mode == "in-place":
            if await self.git.index_tree() != self.initial.index_tree:
                self.git_issues.append(f"{what} changed the index (staging is forbidden)")
                raise Abort(f"{what} changed the index")
            if await self.git.head_ref() != self.initial.head_ref:
                self.git_issues.append(f"{what} switched branches")
                raise Abort(f"{what} switched branches")

    def _track(self, source: str, iteration: int, findings: list[Finding]) -> list[str]:
        keys = []
        for finding in findings:
            key = f"{source}-{iteration}-{len(self.findings) + 1}"
            selected = finding.severity in self.cfg.fix_severities
            self.findings[key] = TrackedFinding(
                key=key,
                source=source,  # type: ignore[arg-type]
                iteration=iteration,
                finding=finding,
                status="open" if selected else "not_selected",
            )
            keys.append(key)
        return keys

    def _fixable(self, keys: list[str]) -> list[str]:
        return [k for k in keys if self.findings[k].finding.severity in self.cfg.fix_severities]

    async def _review(self, skill: SkillName, iteration: int, it: IterationRecord) -> list[str]:
        outcome = await self._call(skill, iteration, it)
        await self._check_read_only(f"{outcome.record.agent}.{skill}")
        assert isinstance(outcome.result.output, ReviewOutput)
        source = "codex" if skill in CODEX_SKILLS else "opus"
        keys = self._track(source, iteration, outcome.result.output.findings)
        self.reviews.append(_ReviewRecord(skill, self.expected_tree, keys))
        it.findings.extend(keys)
        return keys

    async def _review_opus(self, iteration: int, it: IterationRecord) -> list[str]:
        # Both are read-only and share the workspace lock, so they can run together.
        results = await asyncio.gather(
            self._review("review_design", iteration, it),
            self._review("review_simplification", iteration, it),
            return_exceptions=True,
        )
        keys: list[str] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            keys.extend(result)
        return keys

    async def _fix(self, keys: list[str], iteration: int, it: IterationRecord) -> bool:
        """Fix and verify ``keys``. Returns whether the workspace changed."""
        if self.cfg.review_only:
            raise Abort("fixing is not allowed in review-only mode")
        items = [self.findings[k] for k in keys]
        before = self.expected_tree
        outcome = await self._call("fix_findings", iteration, it, items)
        output = outcome.result.output
        assert isinstance(output, FixOutput)
        _check_references(output.items, keys, "fix_findings")
        await self._check_git_state("codex.fix_findings")
        self.expected_tree = await self._fingerprint()
        changed = [p for _, p, _ in await self.git.name_status(before, self.expected_tree)]
        it.fixed_files.extend(p for p in changed if p not in it.fixed_files)
        for item in output.items:
            tracked = self.findings[item.finding_id]
            tracked.status = "fix_attempted" if item.status != "not_fixed" else "unresolved"
            tracked.notes = f"fix: {item.status}: {item.notes}"[:2000]
        if not changed:
            log.warning("fix produced no changes", extra={"iteration": iteration})
            self.no_progress = True
            for key in keys:
                self.findings[key].status = "unresolved"
            return False
        verify = await self._call("verify_changes", iteration, it, items)
        await self._check_read_only("codex.verify_changes")
        voutput = verify.result.output
        assert isinstance(voutput, VerifyOutput)
        _check_references(voutput.items, keys, "verify_changes")
        for item in voutput.items:
            tracked = self.findings[item.finding_id]
            tracked.status = "resolved" if item.status == "resolved" else "unresolved"
            tracked.notes = (tracked.notes + f" | verify: {item.status}: {item.notes}")[:4000]
        new_keys = self._track("codex", iteration, voutput.new_findings)
        it.findings.extend(new_keys)
        self.pending.extend(self._fixable(new_keys))
        return True

    # ------------------------------------------------------------------ tests

    async def _run_commands(self, commands: list[TestCommand], label: str) -> TestsSummary:
        # Configured commands are arbitrary and could modify the code.
        if self.cfg.review_only:
            raise Abort(f"{label} commands are not allowed in review-only mode")
        runs: list[TestRun] = []
        log_dir = self.out_dir / "logs" / label
        with workspace_lock(self.lock_dir, self.workspace, exclusive=True):
            for index, command in enumerate(commands, start=1):
                stem = f"{index:02d}-{_safe(command.name)}"
                try:
                    result = await run_process(
                        shell_argv(command.command),
                        cwd=self.workspace,
                        stdout_path=log_dir / f"{stem}.stdout.log",
                        stderr_path=log_dir / f"{stem}.stderr.log",
                        timeout_s=command.timeout_s,
                    )
                    status = (
                        "timeout"
                        if result.timed_out
                        else "passed"
                        if result.exit_code == 0
                        else "failed"
                    )
                    exit_code, duration = result.exit_code, result.duration_s
                except ExecutableNotFound:
                    status, exit_code, duration = "error", None, 0.0
                runs.append(
                    TestRun(
                        name=command.name,
                        command=command.command,
                        required=command.required,
                        status=status,
                        exit_code=exit_code,
                        duration_s=round(duration, 3),
                        log_path=str(log_dir / stem),
                    )
                )
        await self._discard_command_artifacts(label)
        required = [r for r in runs if r.required]
        failed = [r for r in required if r.status != "passed"]
        if failed:
            status, detail = (
                "failed",
                "failed: " + ", ".join(f"{r.name} ({r.status})" for r in failed),
            )
        elif required:
            status, detail = "passed", f"{len(required)} required command(s) passed"
        else:
            status, detail = "unverified", "no required test commands configured"
        return TestsSummary(status=status, runs=runs, detail=detail)

    async def _discard_command_artifacts(self, label: str) -> None:
        """Commands must leave the reviewed tree intact.

        New untracked files (caches such as ``__pycache__``) are removed in
        worktree mode, where they can only have been created by this run.
        Anything else, and anything at all in in-place mode (where removing
        files could delete user work), aborts the run.
        """
        after = await self._fingerprint()
        if after == self.expected_tree:
            return
        changes = await self.git.name_status(self.expected_tree, after)
        added_only = all(status == "A" for status, _, _ in changes)
        if self.cfg.workspace.mode == "worktree" and added_only:
            for _, path, _ in changes:
                target = self.workspace / path
                target.unlink(missing_ok=True)
                parent = target.parent
                while parent != self.workspace and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            if await self._fingerprint() == self.expected_tree:
                self.warnings.append(
                    f"{label} commands created {len(changes)} untracked file(s); removed them "
                    "(add them to .gitignore to avoid this)"
                )
                return
        self.git_issues.append(
            f"{label} commands modified non-ignored files in the workspace "
            f"({', '.join(p for _, p, _ in changes[:5])}); add generated files to .gitignore"
        )
        raise Abort(f"{label} commands modified the workspace")

    async def _run_tests(self, iteration: int) -> TestsSummary:
        summary = await self._run_commands(self.cfg.tests.commands, f"tests-{iteration}")
        self.latest_tests, self.latest_tests_tree = summary, self.expected_tree
        if summary.status == "failed":
            findings = [
                Finding(
                    id=f"test-{_safe(run.name)}",
                    severity="high",
                    category="testing",
                    title=f"Required test command '{run.name}' {run.status}",
                    description=tail(Path(f"{run.log_path}.stdout.log"), 3000)
                    + "\n"
                    + tail(Path(f"{run.log_path}.stderr.log"), 1000),
                    file=None,
                    line=None,
                    recommendation=(
                        "Fix the code so the test passes. Do not weaken or skip the test."
                    ),
                    confidence="high",
                )
                for run in summary.runs
                if run.required and run.status != "passed"
            ]
            self.pending.extend(self._track("tests", iteration, findings))
        return summary

    # -------------------------------------------------------------- iteration

    async def _iteration(self, number: int, allow_fix: bool) -> bool:
        """Run one review round. Returns whether the workspace changed."""
        it = IterationRecord(number=number, tasks=[], findings=[], fixed_files=[])
        self.iterations.append(it)
        start_tree = self.expected_tree
        carried, self.pending = self.pending, []

        codex_keys = await self._review("review_changes", number, it)
        to_fix = self._fixable(codex_keys) + carried
        if allow_fix and to_fix and not self.no_progress:
            await self._fix(to_fix, number, it)

        opus_keys = await self._review_opus(number, it)
        to_fix = self._fixable(opus_keys) + self.pending
        self.pending = []
        if allow_fix and to_fix and not self.no_progress:
            await self._fix(to_fix, number, it)
        elif not allow_fix:
            self.pending.extend(to_fix)

        if not self.cfg.review_only:
            it.tests = await self._run_tests(number)
        return self.expected_tree != start_tree

    def _final_review_keys(self) -> list[str] | None:
        """Findings of the latest complete review of the final tree, if any."""
        latest: dict[str, _ReviewRecord] = {}
        for record in self.reviews:
            latest[record.skill] = record
        if set(latest) != set(FINAL_REVIEW_SKILLS):
            return None
        if any(r.tree != self.expected_tree for r in latest.values()):
            return None
        return [k for r in latest.values() for k in r.keys]

    async def _review_only(self) -> str:
        """Review the snapshot once and leave every blocking finding unresolved.

        Read-only tasks cannot change the tree (``_check_read_only`` aborts),
        so another round would review the same snapshot again: max_iterations
        is an upper bound, not a repeat count.
        """
        await self._iteration(1, allow_fix=False)
        for key in self._fixable(self._final_review_keys() or []):
            self.findings[key].status = "unresolved"
        return "reviewed"

    async def _loop(self) -> str:
        if self.cfg.review_only:
            return await self._review_only()
        for number in range(1, self.cfg.max_iterations + 1):
            changed = await self._iteration(number, allow_fix=True)
            if self.no_progress:
                break
            if not changed and self.latest_tests.status != "failed":
                break
        if self._final_review_keys() is None:
            await self._iteration(self.cfg.max_iterations + 1, allow_fix=False)
        final_keys = self._final_review_keys() or []
        open_fixable = [k for k in self._fixable(final_keys)]
        for key in open_fixable:
            self.findings[key].status = "unresolved"
        test_failed = self.latest_tests is not None and self.latest_tests.status == "failed"
        if self.no_progress:
            return "no_progress"
        if open_fixable or test_failed:
            return "max_iterations_reached"
        return "converged"

    # ------------------------------------------------------------- finishing

    async def _finish_git(self, status: str) -> ApplyResult:
        patch_path = self.out_dir / "fixes.patch"
        final_tree = self.expected_tree
        patch = await self.git.binary_patch(self.initial.worktree_tree, final_tree)
        patch_path.write_bytes(patch)
        now = await self.git.state()
        for label, before, after in (
            ("HEAD", self.initial.head_commit, now.head_commit),
            ("branch", self.initial.head_ref, now.head_ref),
            ("index (staged changes)", self.initial.index_tree, now.index_tree),
        ):
            if before != after:
                self.git_issues.append(f"{label} changed during the run")
        if self.cfg.review_only:
            # Never apply anything. The review covers only the snapshot, so
            # a working tree that changed meanwhile is an integrity issue.
            if now.worktree_tree != self.initial.worktree_tree:
                self.git_issues.append(
                    "working tree changed during the run; the review covers the snapshot only"
                )
            if patch:
                self.git_issues.append("the workspace changed in a review-only run")
                return ApplyResult(
                    status="skipped",
                    patch_path=str(patch_path),
                    detail="review-only: changes were not applied",
                )
            return ApplyResult(
                status="not_needed",
                patch_path=None,
                detail="review-only: no fixes were requested or applied",
            )
        if self.cfg.workspace.mode == "in-place":
            if now.worktree_tree != final_tree:
                self.git_issues.append("working tree differs from the last reviewed state")
            return ApplyResult(
                status="in_place",
                patch_path=str(patch_path) if patch else None,
                detail="fixes were made directly in the working tree",
            )
        if not patch:
            return ApplyResult(status="not_needed", patch_path=None, detail="no fixes were made")
        if now.worktree_tree != self.initial.worktree_tree:
            self.git_issues.append("working tree changed during the run; fixes were not applied")
            return ApplyResult(
                status="conflict",
                patch_path=str(patch_path),
                detail="your working tree changed during the run; apply the patch manually",
            )
        if (
            status in ("aborted", "cancelled")
            or not self.cfg.workspace.apply_fixes
            or self.git_issues
        ):
            return ApplyResult(
                status="skipped",
                patch_path=str(patch_path),
                detail=(
                    f"not applied (status={status}, apply_fixes={self.cfg.workspace.apply_fixes})"
                ),
            )
        with workspace_lock(self.lock_dir, self.root, exclusive=True):
            ok, err = await self.git.apply_patch(patch_path, check_only=True)
            if not ok:
                self.git_issues.append(f"fix patch does not apply: {err}")
                return ApplyResult(status="conflict", patch_path=str(patch_path), detail=err)
            ok, err = await self.git.apply_patch(patch_path, check_only=False)
        after = await self.git.state()
        if (
            not ok
            or after.worktree_tree != final_tree
            or after.index_tree != self.initial.index_tree
        ):
            self.git_issues.append(f"applying fixes produced an unexpected result: {err}")
            return ApplyResult(
                status="conflict", patch_path=str(patch_path), detail=err or "mismatch"
            )
        return ApplyResult(
            status="applied",
            patch_path=str(patch_path),
            detail="fixes applied to the working tree as unstaged changes",
        )

    async def _cleanup_workspace(self) -> None:
        workspace = getattr(self, "workspace", None)
        if (
            workspace is not None
            and self.cfg.workspace.mode == "worktree"
            and not self.cfg.workspace.keep_worktree
        ):
            await self.git.remove_worktree(workspace)
            with contextlib.suppress(OSError):
                workspace.parent.rmdir()

    # -------------------------------------------------------------------- run

    async def run(self) -> Report:
        started = _now()
        await self._preflight()
        try:
            lock = run_lock(self.lock_dir, self.common_dir)
            lock.__enter__()
        except LockBusy as exc:
            raise UsageError(
                "another cross-model-review run is active for this repository"
            ) from exc
        try:
            return await self._run_locked(started)
        finally:
            lock.__exit__(None, None, None)

    async def _run_locked(self, started: str) -> Report:
        await self._snapshot()
        if self.initial.worktree_tree == self.base_tree:
            await self.git.delete_ref(self.snapshot_ref)
            return self._report(
                "no_changes",
                started,
                ApplyResult(status="not_needed", patch_path=None, detail="nothing to review"),
                snapshot_ref=None,
            )

        status = "aborted"
        apply = ApplyResult(status="skipped", patch_path=None, detail="run did not finish")
        cancelled = False
        try:
            await self._prepare_workspace()
            await self._start_agents()
            status = await self._loop()
        except Abort as exc:
            self.errors.append(str(exc))
            log.error("run aborted", extra={"reason": str(exc)})
        except asyncio.CancelledError:
            cancelled = True
            status = "cancelled"
            self.errors.append("run cancelled")
        except Exception as exc:
            self.errors.append(f"internal error: {exc!r}")
            log.exception("run failed")
        finally:
            await asyncio.shield(self._stop_agents())
            if hasattr(self, "ws_git"):
                try:
                    apply = await asyncio.shield(self._finish_git(status))
                except Exception as exc:
                    self.errors.append(f"finalization failed: {exc!r}")
            await asyncio.shield(self._cleanup_workspace())
            if not self.cfg.workspace.keep_snapshot_ref and not self.git_issues:
                with contextlib.suppress(Exception):
                    await self.git.delete_ref(self.snapshot_ref)
        report = self._report(status, started, apply, snapshot_ref=self.snapshot_ref)
        if cancelled:
            raise RunCancelled(report)
        return report

    def _report(
        self, status: str, started: str, apply: ApplyResult, snapshot_ref: str | None
    ) -> Report:
        final_keys = self._final_review_keys() if hasattr(self, "expected_tree") else None
        if status == "no_changes":
            final_keys = []
        tests = self.latest_tests
        if self.cfg.review_only:
            tests = TestsSummary(
                status="unverified",
                runs=[],
                detail="review-only: setup and test commands were not executed",
            )
        elif tests is None or self.latest_tests_tree != getattr(self, "expected_tree", None):
            tests = TestsSummary(
                status="unverified", runs=[], detail="tests did not run on the final state"
            )
        final_findings = [self.findings[k] for k in final_keys or []]
        gate = evaluate_gate(
            final_findings=final_findings,
            final_review_completed=final_keys is not None and status != "aborted",
            tests=tests,
            git_issues=self.git_issues,
            tasks=self.tasks,
            run_status=status,
        )
        report = Report(
            run_id=self.run_id,
            status=status,
            review_only=self.cfg.review_only,
            repo=str(self.root),
            base_ref=self.opts.base,
            base_commit=getattr(self, "base_commit", None),
            snapshot_ref=snapshot_ref
            if self.cfg.workspace.keep_snapshot_ref or self.git_issues
            else None,
            workspace_mode=self.cfg.workspace.mode,
            started_at=started,
            finished_at=_now(),
            iterations=self.iterations,
            findings=list(self.findings.values()),
            final_review_completed=final_keys is not None,
            tests=tests,
            git_issues=self.git_issues,
            apply=apply,
            gate=gate,
            errors=self.errors,
            warnings=self.warnings,
            output_dir=str(self.out_dir),
        )
        write_report(report, self.out_dir)
        return report


class RunCancelled(Exception):
    def __init__(self, report: Report) -> None:
        super().__init__("run cancelled")
        self.report = report


def _kind(status: str) -> str:
    return {
        "A": "added",
        "M": "modified",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
        "T": "type_changed",
    }.get(status, "other")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:40] or "cmd"


def _check_references(items, sent: list[str], skill: str) -> None:
    unknown = {i.finding_id for i in items} - set(sent)
    if unknown:
        raise Abort(f"{skill} referenced unknown findings {sorted(unknown)}")


__all__ = ["Orchestrator", "RunOptions", "RunCancelled", "UsageError", "BLOCKING_SEVERITIES"]
