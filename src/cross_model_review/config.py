"""Configuration model and loader.

Configuration is merged from (lowest to highest precedence):

1. built-in defaults,
2. the user file ``$XDG_CONFIG_HOME/cross-model-review/config.toml``
   (``~/.config/...`` when unset),
3. the repository file ``<repo>/.cross-model-review.toml``,
4. an explicit ``--config`` file.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cross_model_review.models import BLOCKING_SEVERITIES, Severity

REPO_CONFIG_NAME = ".cross-model-review.toml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


Seconds = Annotated[float, Field(gt=0, le=24 * 3600)]


class TestCommand(_Section):
    __test__ = False  # not a pytest test class

    name: Annotated[str, Field(min_length=1, max_length=100)]
    # A list is executed directly; a string is run through ``/bin/sh -c``.
    command: list[str] | str
    required: bool = True
    timeout_s: Seconds = 900

    @field_validator("command")
    @classmethod
    def _non_empty(cls, value: list[str] | str) -> list[str] | str:
        if not value or (isinstance(value, list) and not value[0]):
            raise ValueError("command must not be empty")
        return value


class TestsConfig(_Section):
    __test__ = False

    commands: list[TestCommand] = []


class WorkspaceConfig(_Section):
    # worktree: agents work in a temporary Git worktree seeded from a snapshot
    #           of your changes; fixes are applied back as a patch at the end.
    # in-place: agents work directly in your working tree.
    mode: Literal["worktree", "in-place"] = "worktree"
    # Commands run in the worktree before the first review (for example
    # dependency installation). They must not modify non-ignored files.
    setup_commands: list[TestCommand] = []
    apply_fixes: bool = True
    keep_worktree: bool = False
    keep_snapshot_ref: bool = True


class CodexAgentConfig(_Section):
    # When set, connect to this already-running A2A agent instead of spawning one.
    url: str = ""
    cli_path: str = "codex"
    min_cli_version: str = "0.154.0"
    # Empty means "use the CLI's default model".
    model: str = ""
    reasoning_effort: str = ""
    # Isolates sessions from ~/.codex/config.toml (MCP servers, profiles,
    # sandbox defaults, hooks). Authentication is still read from CODEX_HOME.
    ignore_user_config: bool = True
    review_timeout_s: Seconds = 1200
    fix_timeout_s: Seconds = 1800
    verify_timeout_s: Seconds = 1200


class OpusAgentConfig(_Section):
    url: str = ""
    cli_path: str = "claude"
    min_cli_version: str = "2.1.0"
    model: str = "opus"
    effort: str = ""
    # 0 disables the per-call budget cap.
    max_budget_usd: Annotated[float, Field(ge=0)] = 0
    # Claude Code --restricted: no command-running tools, file tools confined
    # to the workspace, user/project settings files ignored.
    restricted: bool = True
    review_timeout_s: Seconds = 1200


class AgentsConfig(_Section):
    codex: CodexAgentConfig = CodexAgentConfig()
    opus: OpusAgentConfig = OpusAgentConfig()


class A2AConfig(_Section):
    host: Literal["127.0.0.1", "::1", "localhost"] = "127.0.0.1"
    startup_timeout_s: Seconds = 30
    cancel_timeout_s: Seconds = 15
    # Extra time the orchestrator waits on an A2A task beyond the agent's own
    # CLI timeout, so the agent can report its timeout itself.
    task_timeout_margin_s: Seconds = 60
    # Connecting to non-loopback agents is not supported yet.
    allow_remote: Literal[False] = False


class Config(_Section):
    max_iterations: Annotated[int, Field(ge=1, le=10)] = 3
    fix_severities: list[Severity] = ["critical", "high"]
    max_diff_bytes: Annotated[int, Field(ge=1000, le=5_000_000)] = 200_000
    review_instructions: Annotated[str, Field(max_length=4000)] = ""
    output_dir: str = ""
    lock_dir: str = ""
    workspace: WorkspaceConfig = WorkspaceConfig()
    tests: TestsConfig = TestsConfig()
    agents: AgentsConfig = AgentsConfig()
    a2a: A2AConfig = A2AConfig()

    @field_validator("fix_severities")
    @classmethod
    def _blocking_always_fixed(cls, value: list[str]) -> list[str]:
        missing = BLOCKING_SEVERITIES - set(value)
        if missing:
            raise ValueError(f"fix_severities must include {sorted(missing)}")
        return sorted(set(value), key=["critical", "high", "medium", "low", "info"].index)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "cross-model-review" / "config.toml"


def load_config(repo_root: Path | None, explicit: Path | None = None) -> tuple[Config, list[str]]:
    """Load and merge configuration files. Returns the config and the files used."""
    candidates = [user_config_path()]
    if repo_root is not None:
        candidates.append(repo_root / REPO_CONFIG_NAME)
    merged: dict[str, Any] = {}
    used: list[str] = []
    for path in candidates:
        if path.is_file():
            merged = _deep_merge(merged, tomllib.loads(path.read_text(encoding="utf-8")))
            used.append(str(path))
    if explicit is not None:
        merged = _deep_merge(merged, tomllib.loads(explicit.read_text(encoding="utf-8")))
        used.append(str(explicit))
    return Config.model_validate(merged), used
