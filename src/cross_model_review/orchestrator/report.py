"""Report rendering (JSON + Markdown)."""

from __future__ import annotations

from pathlib import Path

from cross_model_review.models import SEVERITY_RANK, Report

_ICON = {"pass": "PASS", "fail": "FAIL", "unverified": "UNVERIFIED"}


def _cell(text: str, limit: int = 160) -> str:
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_markdown(report: Report) -> str:
    lines = [
        f"# cross-model-review: {report.gate.verdict.upper()}",
        "",
        f"- Run: `{report.run_id}` — status **{report.status}**",
        f"- Repository: `{report.repo}` "
        f"(base `{report.base_ref}` = `{(report.base_commit or '')[:12]}`)",
        f"- Workspace mode: {report.workspace_mode}; iterations: {len(report.iterations)}",
    ]
    if report.review_only:
        lines.append(
            "- Review-only: **yes** — no fix/verify tasks, setup or test commands ran and "
            "nothing was applied. The snapshot cannot change, so one review round is final "
            "(max iterations is only an upper bound)."
        )
    lines.append(f"- Fixes: **{report.apply.status}** — {report.apply.detail}")
    if report.apply.patch_path:
        lines.append(f"- Patch: `{report.apply.patch_path}`")
    if report.snapshot_ref:
        lines.append(f"- Snapshot of your original changes: `{report.snapshot_ref}`")
    lines += ["", "## Quality gate", "", "| Check | Result | Detail |", "|---|---|---|"]
    for check in report.gate.checks:
        lines.append(f"| {check.name} | {_ICON[check.status]} | {_cell(check.detail, 300)} |")

    unresolved = [f for f in report.findings if f.status in ("open", "unresolved")]
    lines += ["", f"## Unresolved findings ({len(unresolved)})", ""]
    if unresolved:
        lines += ["| Key | Severity | Source | Location | Title |", "|---|---|---|---|---|"]
        for f in sorted(unresolved, key=lambda t: SEVERITY_RANK[t.finding.severity]):
            loc = f.finding.file or ""
            if f.finding.line:
                loc += f":{f.finding.line}"
            lines.append(
                f"| {f.key} | {f.finding.severity} | {f.source} | {_cell(loc, 80)} | "
                f"{_cell(f.finding.title)} |"
            )
    else:
        lines.append("None.")

    resolved = [f for f in report.findings if f.status in ("resolved", "fix_attempted")]
    if resolved:
        lines += ["", f"## Fixed during the run ({len(resolved)})", ""]
        for f in resolved:
            lines.append(
                f"- `{f.key}` [{f.finding.severity}] {_cell(f.finding.title)} — {f.status}"
            )

    other = [f for f in report.findings if f.status == "not_selected"]
    if other:
        lines += ["", f"## Other findings (not auto-fixed, {len(other)})", ""]
        for f in sorted(other, key=lambda t: SEVERITY_RANK[t.finding.severity]):
            lines.append(f"- `{f.key}` [{f.finding.severity}] {_cell(f.finding.title)}")

    lines += ["", "## Tests", "", f"Status: **{report.tests.status}** — {report.tests.detail}"]
    for run in report.tests.runs:
        lines.append(f"- {run.name}: {run.status} (exit {run.exit_code}, {run.duration_s:.1f}s)")

    lines += [
        "",
        "## A2A tasks",
        "",
        "| Iter | Agent | Skill | State | Time | Note |",
        "|---|---|---|---|---|---|",
    ]
    for it in report.iterations:
        for t in it.tasks:
            lines.append(
                f"| {it.number} | {t.agent} | {t.skill} | {t.state} | {t.duration_s:.0f}s | "
                f"{_cell(t.error or t.summary or '', 120)} |"
            )
    if report.git_issues:
        lines += ["", "## Git integrity issues", ""] + [f"- {i}" for i in report.git_issues]
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {_cell(w, 1000)}" for w in report.warnings]
    if report.errors:
        lines += ["", "## Errors", ""] + [f"- {_cell(e, 1000)}" for e in report.errors]
    lines += [
        "",
        f"Full report: `{Path(report.output_dir) / 'report.json'}`",
        "",
        "Nothing was committed or pushed.",
        "",
    ]
    return "\n".join(lines)


def write_report(report: Report, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        report.model_dump_json(indent=2, by_alias=True), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
