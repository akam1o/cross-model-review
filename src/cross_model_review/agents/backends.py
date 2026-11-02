"""CLI backends that execute one skill in one fresh, non-persistent CLI session.

Access control is enforced by the CLI itself, never only by the prompt:

* Codex: ``--sandbox read-only`` for review/verify and ``workspace-write``
  (network disabled) for fixes; ``--ephemeral`` sessions; optionally
  ``--ignore-user-config`` so user MCP servers, profiles and a permissive
  ``sandbox_mode`` cannot leak into the run.
* Claude Code: read-only tool set (``Read, Grep, Glob``), ``dontAsk``
  permission mode, no MCP servers, no skills, ``--restricted`` (file tools
  confined to the workspace) and ``--no-session-persistence``.

Neither backend writes CLI configuration or credentials.
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from cross_model_review.agents.prompts import build_prompt
from cross_model_review.config import CodexAgentConfig, OpusAgentConfig
from cross_model_review.models import (
    OUTPUT_MODEL_BY_SKILL,
    WRITE_SKILLS,
    CLIRunInfo,
    SkillName,
    SkillRequest,
)
from cross_model_review.procs import ExecutableNotFound, ProcResult, run_process, tail
from cross_model_review.schemas import cli_output_schema

log = logging.getLogger(__name__)

ErrorCode = Literal[
    "invalid_request",
    "workspace_not_allowed",
    "workspace_busy",
    "cli_not_found",
    "cli_failed",
    "cli_timeout",
    "invalid_output",
    "internal",
]

# Set in every CLI child so a nested `cross-model-review run` refuses to start.
RECURSION_GUARD_ENV = "CROSS_MODEL_REVIEW_ACTIVE"


class AgentError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message


@dataclass(frozen=True)
class SkillSpec:
    id: SkillName
    name: str
    description: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class RunContext:
    task_id: str
    workspace: Path
    log_dir: Path


class AgentBackend(ABC):
    """Executes skills for one A2A agent."""

    agent_name: str
    description: str
    skills: tuple[SkillSpec, ...]

    def skill_ids(self) -> set[str]:
        return {s.id for s in self.skills}

    @abstractmethod
    async def run(self, request: SkillRequest, ctx: RunContext) -> tuple[BaseModel, CLIRunInfo]:
        """Run the skill and return the validated output plus CLI metadata."""


def _relativize(path: Any, workspace: Path) -> Any:
    """Turn absolute paths inside the workspace into repo-relative paths.

    Absolute paths outside the workspace are left as-is so validation rejects them.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return path
    for root in {str(workspace), os.path.realpath(workspace)}:
        if path.startswith(root.rstrip("/") + "/"):
            return path[len(root.rstrip("/")) + 1 :]
    return path


def validate_output(skill: str, data: Any, workspace: Path) -> BaseModel:
    if isinstance(data, dict):
        for key in ("findings", "new_findings"):
            for finding in data.get(key) or []:
                if isinstance(finding, dict) and "file" in finding:
                    finding["file"] = _relativize(finding["file"], workspace)
        if isinstance(data.get("files_changed"), list):
            data["files_changed"] = [_relativize(p, workspace) for p in data["files_changed"]]
    try:
        return OUTPUT_MODEL_BY_SKILL[skill].model_validate(data)
    except ValidationError as exc:
        raise AgentError("invalid_output", f"CLI output does not match schema: {exc}") from exc


def check_output_references(request: SkillRequest, output: BaseModel) -> None:
    """Fix/verify items must refer to the findings that were sent."""
    sent = {f.finding.id for f in request.findings}
    items = getattr(output, "items", None)
    if items is None:
        return
    unknown = {i.finding_id for i in items} - sent
    if unknown:
        raise AgentError("invalid_output", f"output references unknown findings: {sorted(unknown)}")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env[RECURSION_GUARD_ENV] = "1"
    return env


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(text)
    return tuple(int(x) for x in match.groups()) if match else None  # type: ignore[return-value]


async def cli_version(cli_path: str, log_dir: Path) -> str | None:
    try:
        result = await run_process(
            [cli_path, "--version"],
            cwd=Path.cwd(),
            stdout_path=log_dir / "cli-version.stdout",
            stderr_path=log_dir / "cli-version.stderr",
            timeout_s=30,
            env=child_env(),
        )
    except ExecutableNotFound as exc:
        raise AgentError("cli_not_found", f"CLI not found: {cli_path}") from exc
    if result.exit_code != 0:
        return None
    return result.stdout_path.read_text(errors="replace").strip() or None


def ensure_min_version(name: str, version: str | None, minimum: str) -> None:
    have, need = parse_version(version or ""), parse_version(minimum)
    if have is None or need is None:
        raise AgentError("cli_failed", f"cannot determine {name} version ({version!r})")
    if have < need:
        raise AgentError("cli_failed", f"{name} {version} is older than required {minimum}")


def _task_dir(ctx: RunContext) -> Path:
    path = ctx.log_dir / ctx.task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _raise_for_process(name: str, result: ProcResult, timeout_s: float) -> None:
    if result.timed_out:
        raise AgentError("cli_timeout", f"{name} did not finish within {timeout_s:.0f}s")
    if result.exit_code != 0:
        raise AgentError(
            "cli_failed",
            f"{name} exited with {result.exit_code}: {tail(result.stderr_path, 2000)}"
            f"{_last_error_event(result.stdout_path)}",
        )


def _last_error_event(stdout_path: Path) -> str:
    """Extract the last error event from Codex JSONL output, if any."""
    last = ""
    try:
        for line in stdout_path.read_text(errors="replace").splitlines():
            if '"error"' in line or '"turn.failed"' in line:
                last = line
    except OSError:
        return ""
    return f" | last error event: {last[:2000]}" if last else ""


class CodexBackend(AgentBackend):
    agent_name = "codex"
    description = (
        "Defect reviewer and fixer backed by the Codex CLI. Review and verification run "
        "in a read-only sandbox; fixes run in a workspace-write sandbox without network."
    )
    skills = (
        SkillSpec(
            "review_changes",
            "Review changes (read-only)",
            "Independent defect review of the uncommitted change set. Read-only sandbox.",
            ("review", "defects", "read-only"),
        ),
        SkillSpec(
            "fix_findings",
            "Fix findings (write-enabled)",
            "Fix the given findings with minimal edits. workspace-write sandbox, no network, "
            "exclusive workspace lock.",
            ("fix", "write-enabled"),
        ),
        SkillSpec(
            "verify_changes",
            "Verify fixes (read-only)",
            "Check whether given findings are resolved and whether fixes introduced new "
            "defects. Read-only sandbox.",
            ("verify", "read-only"),
        ),
    )

    def __init__(self, config: CodexAgentConfig) -> None:
        self.config = config

    def _timeout(self, skill: str) -> float:
        return {
            "review_changes": self.config.review_timeout_s,
            "fix_findings": self.config.fix_timeout_s,
            "verify_changes": self.config.verify_timeout_s,
        }[skill]

    def build_argv(
        self, skill: str, workspace: Path, schema_path: Path, out_path: Path
    ) -> list[str]:
        sandbox = "workspace-write" if skill in WRITE_SKILLS else "read-only"
        argv = [
            self.config.cli_path,
            "exec",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "-c",
            'approval_policy="never"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "--cd",
            str(workspace),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(out_path),
        ]
        if self.config.ignore_user_config:
            argv.append("--ignore-user-config")
        if self.config.model:
            argv += ["--model", self.config.model]
        if self.config.reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={json.dumps(self.config.reasoning_effort)}"]
        argv.append("-")  # prompt from stdin
        return argv

    async def run(self, request: SkillRequest, ctx: RunContext) -> tuple[BaseModel, CLIRunInfo]:
        skill = request.skill
        task_dir = _task_dir(ctx)
        version = await cli_version(self.config.cli_path, task_dir)
        ensure_min_version("codex", version, self.config.min_cli_version)
        schema_path = task_dir / "output-schema.json"
        schema_path.write_text(json.dumps(cli_output_schema(skill)), encoding="utf-8")
        out_path = task_dir / "last-message.json"
        argv = self.build_argv(skill, ctx.workspace, schema_path, out_path)
        timeout_s = self._timeout(skill)
        log.info("codex session start", extra={"task_id": ctx.task_id, "skill": skill})
        try:
            result = await run_process(
                argv,
                cwd=ctx.workspace,
                stdout_path=task_dir / "codex.stdout.jsonl",
                stderr_path=task_dir / "codex.stderr.log",
                timeout_s=timeout_s,
                stdin_data=build_prompt(request).encode(),
                env=child_env(),
            )
        except ExecutableNotFound as exc:
            raise AgentError("cli_not_found", f"CLI not found: {self.config.cli_path}") from exc
        _raise_for_process("codex", result, timeout_s)
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError("invalid_output", f"codex final message is not JSON: {exc}") from exc
        output = validate_output(skill, data, ctx.workspace)
        check_output_references(request, output)
        info = CLIRunInfo(
            cli="codex",
            cli_version=version,
            model=self.config.model or None,
            sandbox="workspace-write" if skill in WRITE_SKILLS else "read-only",
            exit_code=result.exit_code or 0,
            duration_s=result.duration_s,
        )
        return output, info


class ClaudeBackend(AgentBackend):
    agent_name = "opus"
    description = (
        "Design and simplification reviewer backed by the Claude Code CLI. Runs with a "
        "read-only tool set (Read, Grep, Glob) and no MCP servers."
    )
    skills = (
        SkillSpec(
            "review_design",
            "Review design (read-only)",
            "Design and maintainability review of the change set. Read-only tools only.",
            ("review", "design", "read-only"),
        ),
        SkillSpec(
            "review_simplification",
            "Review simplification (read-only)",
            "Detect unnecessary complexity and propose simpler alternatives. Read-only tools only.",
            ("review", "simplification", "read-only"),
        ),
    )
    READ_ONLY_TOOLS = "Read,Grep,Glob"

    def __init__(self, config: OpusAgentConfig) -> None:
        self.config = config

    def build_argv(self, skill: str) -> list[str]:
        argv = [
            self.config.cli_path,
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(cli_output_schema(skill)),
            "--no-session-persistence",
            "--tools",
            self.READ_ONLY_TOOLS,
            "--permission-mode",
            "dontAsk",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
        if self.config.restricted:
            argv.append("--restricted")
        if self.config.model:
            argv += ["--model", self.config.model]
        if self.config.effort:
            argv += ["--effort", self.config.effort]
        if self.config.max_budget_usd > 0:
            argv += ["--max-budget-usd", str(self.config.max_budget_usd)]
        return argv

    async def run(self, request: SkillRequest, ctx: RunContext) -> tuple[BaseModel, CLIRunInfo]:
        skill = request.skill
        if skill in WRITE_SKILLS:
            raise AgentError("invalid_request", f"{skill} is not supported by this agent")
        task_dir = _task_dir(ctx)
        version = await cli_version(self.config.cli_path, task_dir)
        ensure_min_version("claude", version, self.config.min_cli_version)
        timeout_s = self.config.review_timeout_s
        log.info("claude session start", extra={"task_id": ctx.task_id, "skill": skill})
        try:
            result = await run_process(
                self.build_argv(skill),
                cwd=ctx.workspace,
                stdout_path=task_dir / "claude.stdout.json",
                stderr_path=task_dir / "claude.stderr.log",
                timeout_s=timeout_s,
                stdin_data=build_prompt(request).encode(),
                env=child_env(),
            )
        except ExecutableNotFound as exc:
            raise AgentError("cli_not_found", f"CLI not found: {self.config.cli_path}") from exc
        if result.timed_out:
            raise AgentError("cli_timeout", f"claude did not finish within {timeout_s:.0f}s")
        try:
            envelope = json.loads(result.stdout_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _raise_for_process("claude", result, timeout_s)
            raise AgentError("invalid_output", f"claude output is not JSON: {exc}") from exc
        if not isinstance(envelope, dict) or envelope.get("type") != "result":
            raise AgentError("invalid_output", "claude output is not a result envelope")
        if (
            result.exit_code != 0
            or envelope.get("is_error")
            or envelope.get("subtype") != "success"
        ):
            raise AgentError(
                "cli_failed",
                f"claude failed (exit {result.exit_code}, subtype {envelope.get('subtype')}): "
                f"{str(envelope.get('result'))[:2000]}",
            )
        output = validate_output(skill, envelope.get("structured_output"), ctx.workspace)
        check_output_references(request, output)
        info = CLIRunInfo(
            cli="claude",
            cli_version=version,
            model=self.config.model or None,
            sandbox="read-only",
            exit_code=0,
            duration_s=result.duration_s,
        )
        return output, info
