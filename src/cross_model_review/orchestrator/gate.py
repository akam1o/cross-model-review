"""Quality gate evaluation.

The verdict is ``failed`` if any check fails, otherwise ``unverified`` if any
check could not be verified (for example no test commands are configured),
otherwise ``passed``. Nothing is assumed to pass by default.
"""

from __future__ import annotations

from cross_model_review.models import (
    GateCheck,
    GateResult,
    TaskRecord,
    TestsSummary,
    TrackedFinding,
)


def _severity_check(
    name: str, severity: str, findings: list[TrackedFinding], final_review_completed: bool
) -> GateCheck:
    if not final_review_completed:
        return GateCheck(name=name, status="fail", detail="no complete review of the final state")
    hits = [f for f in findings if f.finding.severity == severity]
    if not hits:
        return GateCheck(name=name, status="pass", detail=f"0 {severity} findings")
    titles = "; ".join(f"{f.key}: {f.finding.title}" for f in hits[:10])
    return GateCheck(
        name=name, status="fail", detail=f"{len(hits)} {severity} finding(s): {titles}"
    )


def evaluate_gate(
    *,
    final_findings: list[TrackedFinding],
    final_review_completed: bool,
    tests: TestsSummary,
    git_issues: list[str],
    tasks: list[TaskRecord],
    run_status: str,
) -> GateResult:
    if run_status == "no_changes":
        checks = [
            GateCheck(name=name, status="unverified", detail="no changes to review")
            for name in ("critical_findings", "high_findings", "required_tests", "final_review")
        ] + [
            GateCheck(name="git_integrity", status="pass", detail="no changes"),
            GateCheck(name="a2a_tasks", status="unverified", detail="no tasks were needed"),
        ]
        return GateResult(verdict="unverified", checks=checks)

    checks = [
        _severity_check("critical_findings", "critical", final_findings, final_review_completed),
        _severity_check("high_findings", "high", final_findings, final_review_completed),
        GateCheck(
            name="required_tests",
            status={"passed": "pass", "failed": "fail", "unverified": "unverified"}[tests.status],
            detail=tests.detail,
        ),
        GateCheck(
            name="final_review",
            status="pass" if final_review_completed else "fail",
            detail="all reviews ran on the final state"
            if final_review_completed
            else "the final state was not fully reviewed",
        ),
        GateCheck(
            name="git_integrity",
            status="fail" if git_issues else "pass",
            detail="; ".join(git_issues) or "HEAD, branch, index and working tree are consistent",
        ),
    ]
    bad = [t for t in tasks if t.state != "completed"]
    if run_status in ("aborted", "cancelled") or bad or not tasks:
        detail = "; ".join(f"{t.agent}.{t.skill}: {t.state}" for t in bad) or f"run {run_status}"
        checks.append(GateCheck(name="a2a_tasks", status="fail", detail=detail))
    else:
        checks.append(
            GateCheck(name="a2a_tasks", status="pass", detail=f"{len(tasks)} task(s) completed")
        )

    statuses = {c.status for c in checks}
    verdict = (
        "failed" if "fail" in statuses else "unverified" if "unverified" in statuses else "passed"
    )
    return GateResult(verdict=verdict, checks=checks)
