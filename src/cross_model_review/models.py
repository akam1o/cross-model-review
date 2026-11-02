"""Structured data exchanged between the orchestrator, the A2A agents and the CLIs.

Three layers are modelled here:

* ``*Output`` models are what a CLI (Codex / Claude Code) must return. They are
  exported as strict JSON Schemas and passed to the CLI's structured-output flag.
* ``SkillRequest`` / ``SkillResult`` are the A2A payloads (DataParts) carried in
  the request Message and the result Artifact.
* ``Report`` and friends describe the outcome of a whole run.

Every model forbids unknown fields: agent and model output is treated as
untrusted input and must match the schema exactly.
"""

from __future__ import annotations

import posixpath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SKILL_REQUEST_SCHEMA = "cross-model-review/skill-request@1"
SKILL_RESULT_SCHEMA = "cross-model-review/skill-result@1"
REPORT_SCHEMA = "cross-model-review/report@1"

Severity = Literal["critical", "high", "medium", "low", "info"]
SEVERITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"critical", "high"})

Category = Literal[
    "correctness",
    "security",
    "reliability",
    "performance",
    "data_integrity",
    "testing",
    "design",
    "maintainability",
    "simplification",
    "other",
]

SkillName = Literal[
    "review_changes",
    "fix_findings",
    "verify_changes",
    "review_design",
    "review_simplification",
]
REVIEW_SKILLS: frozenset[str] = frozenset(
    {"review_changes", "review_design", "review_simplification"}
)
WRITE_SKILLS: frozenset[str] = frozenset({"fix_findings"})

FindingId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]
ShortText = Annotated[str, Field(max_length=500)]
LongText = Annotated[str, Field(max_length=8000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def normalize_repo_path(value: str | None) -> str | None:
    """Return a normalized repository-relative POSIX path or raise ``ValueError``."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if "\x00" in value or "\\" in value:
        raise ValueError("path contains invalid characters")
    if value.startswith("/"):
        raise ValueError("path must be relative to the repository root")
    normalized = posixpath.normpath(value)
    if normalized == "." or normalized == ".." or normalized.startswith("../"):
        raise ValueError("path must stay inside the repository")
    return normalized


# --------------------------------------------------------------------------
# CLI outputs
# --------------------------------------------------------------------------


class Finding(StrictModel):
    id: FindingId
    severity: Severity
    category: Category
    title: Annotated[str, Field(min_length=1, max_length=300)]
    description: LongText
    file: Annotated[str, Field(max_length=1000)] | None
    line: Annotated[int, Field(ge=1)] | None
    recommendation: LongText
    confidence: Literal["high", "medium", "low"]

    @field_validator("file")
    @classmethod
    def _file_is_repo_relative(cls, value: str | None) -> str | None:
        return normalize_repo_path(value)


def _unique_ids(ids: list[str], what: str) -> None:
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate {what} ids")


class ReviewOutput(StrictModel):
    summary: LongText
    findings: Annotated[list[Finding], Field(max_length=200)]

    @model_validator(mode="after")
    def _check(self) -> ReviewOutput:
        _unique_ids([f.id for f in self.findings], "finding")
        return self


class FixItem(StrictModel):
    finding_id: FindingId
    status: Literal["fixed", "partially_fixed", "not_fixed"]
    notes: LongText


class FixOutput(StrictModel):
    summary: LongText
    items: Annotated[list[FixItem], Field(max_length=200)]
    # Self-reported and informational only: the orchestrator computes the real
    # change set from Git.
    files_changed: Annotated[list[Annotated[str, Field(max_length=1000)]], Field(max_length=1000)]

    @field_validator("files_changed")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        return [p for p in (normalize_repo_path(v) for v in value) if p]

    @model_validator(mode="after")
    def _check(self) -> FixOutput:
        _unique_ids([i.finding_id for i in self.items], "fix item")
        return self


class VerifyItem(StrictModel):
    finding_id: FindingId
    status: Literal["resolved", "unresolved", "unclear"]
    notes: LongText


class VerifyOutput(StrictModel):
    summary: LongText
    items: Annotated[list[VerifyItem], Field(max_length=200)]
    new_findings: Annotated[list[Finding], Field(max_length=200)]

    @model_validator(mode="after")
    def _check(self) -> VerifyOutput:
        _unique_ids([i.finding_id for i in self.items], "verify item")
        _unique_ids([f.id for f in self.new_findings], "finding")
        return self


SkillOutput = ReviewOutput | FixOutput | VerifyOutput

OUTPUT_MODEL_BY_SKILL: dict[str, type[BaseModel]] = {
    "review_changes": ReviewOutput,
    "review_design": ReviewOutput,
    "review_simplification": ReviewOutput,
    "fix_findings": FixOutput,
    "verify_changes": VerifyOutput,
}


# --------------------------------------------------------------------------
# A2A payloads
# --------------------------------------------------------------------------


class WorkspaceRef(StrictModel):
    # Only local paths exist today. A remote agent would need another kind
    # (for example a Git bundle or patch upload); the discriminator keeps that
    # extension possible without breaking v1 requests.
    kind: Literal["local_path"]
    path: Annotated[str, Field(min_length=1, max_length=4096)]

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("workspace path must be absolute")
        return value


class ChangedFile(StrictModel):
    path: Annotated[str, Field(max_length=4096)]
    change: Literal["added", "modified", "deleted", "renamed", "copied", "type_changed", "other"]
    old_path: Annotated[str, Field(max_length=4096)] | None = None
    committed: bool = False
    staged: bool = False
    unstaged: bool = False
    untracked: bool = False


class ReviewTarget(StrictModel):
    base_ref: Annotated[str, Field(max_length=256)]
    base_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]
    changed_files: Annotated[list[ChangedFile], Field(max_length=20000)]
    diff: str
    diff_truncated: bool


class TaskFinding(StrictModel):
    """A finding as sent back to an agent (fix / verify), tagged with its origin."""

    source: Literal["codex", "opus", "tests"]
    finding: Finding


class SkillRequest(StrictModel):
    schema_: Literal["cross-model-review/skill-request@1"] = Field(
        default=SKILL_REQUEST_SCHEMA, alias="schema"
    )
    skill: SkillName
    run_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    iteration: Annotated[int, Field(ge=0, le=100)]
    workspace: WorkspaceRef
    target: ReviewTarget
    findings: Annotated[list[TaskFinding], Field(max_length=200)] = []
    instructions: Annotated[str, Field(max_length=4000)] = ""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _findings_match_skill(self) -> SkillRequest:
        if self.skill in REVIEW_SKILLS and self.findings:
            raise ValueError(f"{self.skill} does not accept findings")
        if self.skill not in REVIEW_SKILLS and not self.findings:
            raise ValueError(f"{self.skill} requires at least one finding")
        _unique_ids([f.finding.id for f in self.findings], "finding")
        return self


class CLIRunInfo(StrictModel):
    cli: Literal["codex", "claude", "mock"]
    cli_version: Annotated[str, Field(max_length=200)] | None
    model: Annotated[str, Field(max_length=200)] | None
    sandbox: Literal["read-only", "workspace-write"]
    exit_code: int
    duration_s: Annotated[float, Field(ge=0)]


class SkillResult(StrictModel):
    schema_: Literal["cross-model-review/skill-result@1"] = Field(
        default=SKILL_RESULT_SCHEMA, alias="schema"
    )
    skill: SkillName
    agent: Annotated[str, Field(min_length=1, max_length=100)]
    output: SkillOutput
    run: CLIRunInfo

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _parse_output_by_skill(cls, data: Any) -> Any:
        # A plain union would let Pydantic pick whichever model happens to
        # validate; the skill decides the output type.
        if isinstance(data, dict) and isinstance(data.get("output"), dict):
            model = OUTPUT_MODEL_BY_SKILL.get(data.get("skill", ""))
            if model is not None:
                data = {**data, "output": model.model_validate(data["output"])}
        return data

    @model_validator(mode="after")
    def _output_matches_skill(self) -> SkillResult:
        if not isinstance(self.output, OUTPUT_MODEL_BY_SKILL[self.skill]):
            raise ValueError(f"output type does not match skill {self.skill}")
        return self


class AgentErrorInfo(StrictModel):
    code: Literal[
        "invalid_request",
        "workspace_not_allowed",
        "workspace_busy",
        "cli_not_found",
        "cli_failed",
        "cli_timeout",
        "invalid_output",
        "internal",
    ]
    message: Annotated[str, Field(max_length=8000)]


# --------------------------------------------------------------------------
# Run report
# --------------------------------------------------------------------------


class TaskRecord(StrictModel):
    agent: str
    skill: SkillName
    iteration: int
    task_id: str | None
    state: Literal[
        "completed", "failed", "canceled", "rejected", "timeout", "invalid_artifact", "error"
    ]
    duration_s: float
    error: str | None = None
    summary: str | None = None


class TrackedFinding(StrictModel):
    key: str
    source: Literal["codex", "opus", "tests"]
    iteration: int
    finding: Finding
    status: Literal["open", "fix_attempted", "resolved", "unresolved", "not_selected"] = "open"
    notes: str = ""


class TestRun(StrictModel):
    __test__ = False  # not a pytest test class

    name: str
    command: list[str] | str
    required: bool
    status: Literal["passed", "failed", "timeout", "error"]
    exit_code: int | None
    duration_s: float
    log_path: str | None = None


class TestsSummary(StrictModel):
    __test__ = False

    status: Literal["passed", "failed", "unverified"]
    runs: list[TestRun]
    detail: str


class IterationRecord(StrictModel):
    number: int
    tasks: list[TaskRecord]
    findings: list[str]
    fixed_files: list[str]
    tests: TestsSummary | None = None


class GateCheck(StrictModel):
    name: Literal[
        "critical_findings",
        "high_findings",
        "required_tests",
        "final_review",
        "git_integrity",
        "a2a_tasks",
    ]
    status: Literal["pass", "fail", "unverified"]
    detail: str


class GateResult(StrictModel):
    verdict: Literal["passed", "failed", "unverified"]
    checks: list[GateCheck]


class ApplyResult(StrictModel):
    status: Literal["applied", "not_needed", "skipped", "conflict", "in_place"]
    patch_path: str | None
    detail: str


class Report(StrictModel):
    schema_: Literal["cross-model-review/report@1"] = Field(default=REPORT_SCHEMA, alias="schema")
    run_id: str
    status: Literal[
        "converged",
        "max_iterations_reached",
        "no_progress",
        "aborted",
        "cancelled",
        "no_changes",
    ]
    repo: str
    base_ref: str
    base_commit: str | None
    snapshot_ref: str | None
    workspace_mode: Literal["worktree", "in-place"]
    started_at: str
    finished_at: str
    iterations: list[IterationRecord]
    findings: list[TrackedFinding]
    final_review_completed: bool
    tests: TestsSummary
    git_issues: list[str]
    apply: ApplyResult
    gate: GateResult
    errors: list[str]
    warnings: list[str] = []
    output_dir: str

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
