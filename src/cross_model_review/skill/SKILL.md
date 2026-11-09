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
   `--max-iterations N`, `--mode in-place`, `--config <file>`,
   `--review-only`, `--no-config-discovery`.

   `--review-only` reviews without changing code: no fix or verify task runs,
   no setup or test command runs, and nothing is applied. The snapshot cannot
   change, so the run stops after one review round even with
   `--max-iterations 2` (the value is only an upper bound). Critical/High
   findings stay unresolved and fail the gate; with none, the verdict is
   `UNVERIFIED` because no tests ran. The report says `review_only: true`.

   `--no-config-discovery` reads only the `--config` file (TOML or `.json`)
   and ignores `~/.config/cross-model-review/config.toml` and the repository's
   `.cross-model-review.toml`.

2. While it runs, do not edit files in the repository. If the working tree
   changes during the run, the fixes are not applied and a patch is left
   instead.

3. When it finishes, show the user the Markdown report from stdout (it is also
   saved as `report.md` in the run directory printed at the end). Summarise:
   - the gate verdict (`PASSED`, `FAILED` or `UNVERIFIED`) and failing checks,
   - unresolved findings, highest severity first,
   - whether fixes were applied, or where the patch is (in review-only mode,
     say that no fixes were attempted),
   - test status. `UNVERIFIED` means no required tests were configured or run;
     never describe it as passing. In review-only mode no tests were run.

4. Exit codes: 0 passed (or nothing to review), 1 failed, 2 unverified,
   3 usage/configuration error, 130 cancelled. On exit code 3, show the
   error and suggest `cross-model-review doctor`.

## Rules

- Never commit, push, stage, or deploy as part of this skill.
- Never claim tests passed unless the report says the `required_tests` check
  is `PASS`.
- Do not re-run automatically after a failure; ask the user first.
