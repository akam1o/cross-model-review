"""Git operations that never touch the user's index, HEAD or working tree.

Snapshots and fingerprints are computed with a *copy* of the index
(``GIT_INDEX_FILE``), so staged/unstaged state is preserved exactly.
``GIT_OPTIONAL_LOCKS=0`` stops read commands from opportunistically
rewriting the index, and hooks are disabled for every command.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cross_model_review.models import ChangedFile

_STRIPPED_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_PREFIX")
_IDENTITY = {
    "GIT_AUTHOR_NAME": "cross-model-review",
    "GIT_AUTHOR_EMAIL": "cross-model-review@localhost",
    "GIT_COMMITTER_NAME": "cross-model-review",
    "GIT_COMMITTER_EMAIL": "cross-model-review@localhost",
}
_CHANGE_KINDS = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type_changed",
}
_IN_PROGRESS_MARKERS = {
    "MERGE_HEAD": "merge",
    "CHERRY_PICK_HEAD": "cherry-pick",
    "REVERT_HEAD": "revert",
    "rebase-merge": "rebase",
    "rebase-apply": "rebase/am",
    "BISECT_LOG": "bisect",
}


class GitError(Exception):
    pass


@dataclass(frozen=True)
class RepoState:
    head_commit: str
    head_ref: str | None
    index_tree: str
    worktree_tree: str


class Git:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def _env(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        if extra:
            env.update(extra)
        return env

    def _run_sync(
        self, args: tuple[str, ...], extra_env: Mapping[str, str] | None, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
        proc = subprocess.run(
            argv, cwd=self.cwd, env=self._env(extra_env), capture_output=True, check=False
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )
        return proc

    async def run_bytes(
        self, *args: str, extra_env: Mapping[str, str] | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        return await asyncio.to_thread(self._run_sync, args, extra_env, check)

    async def run(self, *args: str, extra_env: Mapping[str, str] | None = None) -> str:
        proc = await self.run_bytes(*args, extra_env=extra_env)
        return proc.stdout.decode("utf-8", errors="replace").strip()

    # ---- repository facts -------------------------------------------------

    async def toplevel(self) -> Path:
        return Path(await self.run("rev-parse", "--show-toplevel")).resolve()

    async def common_dir(self) -> Path:
        return Path(await self.run("rev-parse", "--path-format=absolute", "--git-common-dir"))

    async def git_path(self, name: str) -> Path:
        return Path(await self.run("rev-parse", "--path-format=absolute", "--git-path", name))

    async def head_commit(self) -> str | None:
        proc = await self.run_bytes("rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
        return proc.stdout.decode().strip() if proc.returncode == 0 else None

    async def head_ref(self) -> str | None:
        proc = await self.run_bytes("symbolic-ref", "-q", "HEAD", check=False)
        return proc.stdout.decode().strip() if proc.returncode == 0 else None

    async def resolve_commit(self, ref: str) -> str:
        if ref.startswith("-"):
            raise GitError(f"invalid ref: {ref}")
        return await self.run(
            "rev-parse", "--verify", "-q", "--end-of-options", f"{ref}^{{commit}}"
        )

    async def merge_base(self, a: str, b: str) -> str:
        return await self.run("merge-base", a, b)

    async def tree_of(self, commit: str) -> str:
        return await self.run("rev-parse", "--verify", f"{commit}^{{tree}}")

    async def in_progress_operations(self) -> list[str]:
        found = []
        for marker, label in _IN_PROGRESS_MARKERS.items():
            if (await self.git_path(marker)).exists():
                found.append(label)
        return found

    async def has_unmerged_paths(self) -> bool:
        return bool(await self.run("ls-files", "-u"))

    # ---- snapshots ----------------------------------------------------------

    async def _with_index_copy(self, *commands: tuple[str, ...]) -> str:
        index = await self.git_path("index")
        with tempfile.TemporaryDirectory(prefix="cmr-index-") as tmp:
            tmp_index = Path(tmp) / "index"
            if index.exists():
                shutil.copyfile(index, tmp_index)
            env = {"GIT_INDEX_FILE": str(tmp_index)}
            output = ""
            for command in commands:
                output = await self.run(*command, extra_env=env)
            return output

    async def index_tree(self) -> str:
        """Tree of the current index (staged state), without modifying the index."""
        return await self._with_index_copy(("write-tree",))

    async def worktree_tree(self) -> str:
        """Tree of the working tree including untracked, non-ignored files."""
        return await self._with_index_copy(("add", "-A", "--", ":/"), ("write-tree",))

    async def state(self) -> RepoState:
        head = await self.head_commit()
        if head is None:
            raise GitError("repository has no commits yet")
        return RepoState(
            head_commit=head,
            head_ref=await self.head_ref(),
            index_tree=await self.index_tree(),
            worktree_tree=await self.worktree_tree(),
        )

    async def commit_tree(self, tree: str, parent: str, message: str) -> str:
        return await self.run("commit-tree", tree, "-p", parent, "-m", message, extra_env=_IDENTITY)

    async def update_ref(self, ref: str, commit: str) -> None:
        await self.run("update-ref", ref, commit)

    async def delete_ref(self, ref: str) -> None:
        await self.run("update-ref", "-d", ref)

    # ---- diffs ------------------------------------------------------------

    _DIFF_FLAGS = (
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
    )

    async def diff_text(self, a: str, b: str, max_bytes: int) -> tuple[str, bool]:
        proc = await self.run_bytes("diff", *self._DIFF_FLAGS, "-M", a, b, "--")
        data = proc.stdout
        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]
        return data.decode("utf-8", errors="ignore"), truncated

    async def binary_patch(self, a: str, b: str) -> bytes:
        proc = await self.run_bytes(
            "diff", *self._DIFF_FLAGS, "--binary", "--no-renames", a, b, "--"
        )
        return proc.stdout

    async def name_status(self, a: str, b: str) -> list[tuple[str, str, str | None]]:
        raw = (await self.run_bytes("diff", "--name-status", "-z", "-M", a, b, "--")).stdout
        fields = raw.decode("utf-8", errors="surrogateescape").split("\0")
        entries: list[tuple[str, str, str | None]] = []
        i = 0
        while i < len(fields) and fields[i]:
            status = fields[i]
            if status[0] in "RC":
                entries.append((status[0], fields[i + 2], fields[i + 1]))
                i += 3
            else:
                entries.append((status[0], fields[i + 1], None))
                i += 2
        return entries

    async def _names(self, *args: str) -> set[str]:
        raw = (await self.run_bytes(*args)).stdout
        return {p for p in raw.decode("utf-8", errors="surrogateescape").split("\0") if p}

    async def changed_files(
        self, base_commit: str, snapshot_tree: str, head: str
    ) -> list[ChangedFile]:
        committed = (
            set()
            if base_commit == head
            else await self._names("diff", "--name-only", "-z", base_commit, head, "--")
        )
        staged = await self._names("diff", "--cached", "--name-only", "-z", head, "--")
        unstaged = await self._names("diff", "--name-only", "-z", "--")
        untracked = await self._names("ls-files", "--others", "--exclude-standard", "-z")
        files = []
        for status, path, old in await self.name_status(base_commit, snapshot_tree):
            files.append(
                ChangedFile(
                    path=path,
                    change=_CHANGE_KINDS.get(status, "other"),
                    old_path=old,
                    committed=path in committed,
                    staged=path in staged,
                    unstaged=path in unstaged,
                    untracked=path in untracked,
                )
            )
        return files

    # ---- applying fixes -----------------------------------------------------

    async def apply_patch(self, patch: Path, *, check_only: bool) -> tuple[bool, str]:
        args = ["apply", "--binary", "--whitespace=nowarn"]
        if check_only:
            args.append("--check")
        proc = await self.run_bytes(*args, str(patch), check=False)
        return proc.returncode == 0, proc.stderr.decode(errors="replace").strip()

    # ---- worktrees ----------------------------------------------------------

    async def add_worktree(self, path: Path, snapshot_commit: str, head_commit: str) -> None:
        """Create a detached worktree whose files equal the snapshot.

        HEAD (and the worktree's own index) point at the user's HEAD, so
        ``git diff HEAD`` inside the worktree shows the changes under review.
        """
        await self.run("worktree", "add", "--detach", "--quiet", str(path), snapshot_commit)
        await Git(path).run("reset", "--mixed", "--quiet", head_commit)

    async def remove_worktree(self, path: Path) -> None:
        await self.run_bytes("worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        await self.run_bytes("worktree", "prune", check=False)
