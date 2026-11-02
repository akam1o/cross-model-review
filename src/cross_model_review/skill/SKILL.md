---
name: cross-model-review
description: Run an automated cross-model review of the current uncommitted Git changes. Codex reviews and fixes defects, Claude (Opus) reviews design and simplification, coordinated over the A2A protocol, then a quality gate is evaluated. Use only when the user explicitly asks for /cross-model-review.
disable-model-invocation: true
allowed-tools: Bash(cross-model-review run:*), Bash(cross-model-review doctor:*), Read
---

# cross-model-review

Run the `cross-model-review` orchestrator on the current repository and report
the result. The orchestrator does all of the work: do not review or edit the
code yourself while it runs.

## Steps

1. Run it from the repository root, in the background (a run can take tens of
   minutes):

   ```bash
   cross-model-review run --repo "$(git rev-parse --show-toplevel)" $ARGUMENTS
   ```

   Use the Bash tool with `run_in_background: true`, then wait for the
   completion notification. Do not poll in a loop.
   Useful arguments: `--base <ref>` (review a branch since its merge base),
   `--max-iterations N`, `--mode in-place`, `--config <file>`.

2. While it runs, do not edit files in the repository. If the working tree
   changes during the run, the fixes are not applied and a patch is left
   instead.

3. When it finishes, show the user the Markdown report from stdout (it is also
   saved as `report.md` in the run directory printed at the end). Summarise:
   - the gate verdict (`PASSED`, `FAILED` or `UNVERIFIED`) and failing checks,
   - unresolved findings, highest severity first,
   - whether fixes were applied, or where the patch is,
   - test status. `UNVERIFIED` means no required tests were configured or run;
     never describe it as passing.

4. Exit codes: 0 passed (or nothing to review), 1 failed, 2 unverified,
   3 usage/configuration error, 130 cancelled. On exit code 3, show the
   error and suggest `cross-model-review doctor`.

## Rules

- Never commit, push, stage, or deploy as part of this skill.
- Never claim tests passed unless the report says the `required_tests` check
  is `PASS`.
- Do not re-run automatically after a failure; ask the user first.
