<!-- config: examples/demo-config.toml -->
# cross-model-review: FAILED

- Run: `20261102T055107Z-48a6da` — status **aborted**
- Repository: `/path/to/demo-repo` (base `HEAD` = `07190242cb98`)
- Workspace mode: worktree; iterations: 1
- Fixes: **not_needed** — no fixes were made
- Snapshot of your original changes: `refs/cross-model-review/20261102T055107Z-48a6da`

## Quality gate

| Check | Result | Detail |
|---|---|---|
| critical_findings | FAIL | no complete review of the final state |
| high_findings | FAIL | no complete review of the final state |
| required_tests | UNVERIFIED | tests did not run on the final state |
| final_review | FAIL | the final state was not fully reviewed |
| git_integrity | PASS | HEAD, branch, index and working tree are consistent |
| a2a_tasks | FAIL | codex.review_changes: failed |

## Unresolved findings (0)

None.

## Tests

Status: **unverified** — tests did not run on the final state

## A2A tasks

| Iter | Agent | Skill | State | Time | Note |
|---|---|---|---|---|---|
| 1 | codex | review_changes | failed | 22s | invalid_output: CLI output does not match schema: 1 validation error for ReviewOutput findings.0.title Field required [… |

## Errors

- codex.review_changes failed: invalid_output: CLI output does not match schema: 1 validation error for ReviewOutput findings.0.title Field required [type=missing, input_value={'id': 'F1', 'severity': ...', 'confidence': 'high'}, input_type=dict] For further information visit https://errors.pydantic.dev/2.14/v/missing

Full report: `/path/to/demo-repo/.git/cross-model-review/runs/20261102T055107Z-48a6da/report.json`

Nothing was committed or pushed.

