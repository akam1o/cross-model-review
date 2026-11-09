# cross-model-review

[日本語](README.ja.md)

An automated cross-model code review for Claude Code. Keep developing as usual,
then run `/cross-model-review`:

1. Your uncommitted Git changes (staged, unstaged and untracked) are snapshotted.
2. **Codex** performs an independent, read-only defect review.
3. **Codex** fixes Critical/High defects (sandboxed, write-enabled).
4. **Claude (Opus)** reviews design and unnecessary complexity (read-only).
5. **Codex** fixes what Opus flagged, if needed, and verifies its fixes.
6. Steps 2–5 repeat (default: at most 3 fix rounds), then the final state is reviewed once more.
7. A quality gate is evaluated and a report is returned. Nothing is committed or pushed.

The three components talk over the [Agent2Agent (A2A) protocol](https://a2a-protocol.org/)
using the official [`a2a-sdk`](https://pypi.org/project/a2a-sdk/) (pinned to 1.3.0, protocol 1.0, JSON-RPC binding).

```
          Claude Code  ──/cross-model-review──▶  cross-model-review run
                                                     (A2A orchestrator)
                                         ┌──────────────┴──────────────┐
                                    A2A  │ JSON-RPC + SSE, 127.0.0.1   │  A2A
                                         ▼                             ▼
                              Codex A2A Agent                  Opus A2A Agent
                         review_changes / fix_findings      review_design /
                              / verify_changes             review_simplification
                                         │                             │
                              codex exec (sandbox)        claude -p (read-only tools)
```

## Requirements

| Tool | Verified version |
|---|---|
| Python | 3.12+ |
| [uv](https://docs.astral.sh/uv/) | 0.12 |
| Git | 2.x with `git worktree` |
| Codex CLI (`codex`) | 0.154.0 (minimum enforced; configurable) |
| Claude Code (`claude`) | 2.1.277 (minimum enforced: 2.1.0; configurable) |

Both CLIs must already be logged in. cross-model-review never changes their
credentials or configuration. macOS and Linux are supported (the code uses
POSIX process groups and `flock`).

## Install

```bash
git clone <this repository> cross-model-review
cd cross-model-review
uv tool install .                     # puts `cross-model-review` on PATH
cross-model-review install-skill      # writes ~/.claude/skills/cross-model-review/SKILL.md
cross-model-review doctor             # checks git / codex / claude
```

Use `install-skill --dest <repo>/.claude/skills` to install the skill for one project only.

## Usage

In Claude Code:

```
/cross-model-review
/cross-model-review --base main          # review the branch since its merge base with main
/cross-model-review --max-iterations 2
/cross-model-review --review-only        # review only; never change code
```

From a terminal:

```bash
cross-model-review run --repo . [--base REF] [--config FILE] [--mode worktree|in-place] [--json]
                       [--review-only] [--no-config-discovery]
```

### Review-only mode

`--review-only` (or `review_only = true`) runs the Codex defect review and the
Opus design and simplification reviews on the same snapshot and changes
nothing:

- No `fix_findings` / `verify_changes` task runs, and no patch is applied (in
  `in-place` mode too).
- No `workspace.setup_commands` or `tests.commands` run, because configured
  commands could modify the code. `required_tests` is therefore `UNVERIFIED`.
- Read-only tasks cannot change the snapshot, so the run stops after one
  round. `--max-iterations` is an upper bound, not a repeat count;
  `--max-iterations 2` still gives one round.
- Critical/High findings stay `unresolved` and fail the gate. With none, the
  verdict is `unverified` (exit code 2), never `passed`.
- The report has `"review_only": true` and `"status": "reviewed"`.

`--no-config-discovery` reads only the `--config` file (required with this
option) and skips the user and repository config files. A config ending in
`.json` is read as JSON, for example the output of
`Config(...).model_dump_json()`. Use it when the caller supplies a trusted
configuration, so that files in the repository or home directory cannot change
CLI paths or commands:

```bash
cross-model-review run --repo . --review-only --max-iterations 2 \
    --no-config-discovery --config trusted.json --json
```

Exit codes: `0` passed (or nothing to review), `1` failed, `2` unverified,
`3` usage/configuration error, `130` cancelled. Ctrl-C cancels every A2A task;
cancellation reaches the CLI processes and their children.

Each run writes to `<git-common-dir>/cross-model-review/runs/<run-id>/`:

| File | Content |
|---|---|
| `report.md`, `report.json` | Human / machine report ([schema](schemas/report.schema.json)) |
| `fixes.patch` | All fixes as a binary Git patch relative to your original changes |
| `logs/<agent>/server.stderr.jsonl` | Structured agent server logs |
| `logs/<agent>/<task-id>/` | CLI stdout and stderr (kept separate), the output schema, the final message |
| `logs/tests-<n>/` | Test command output per iteration |

See [examples/](examples/) for a configuration and sample reports.

## Configuration

Copy [config/cross-model-review.example.toml](config/cross-model-review.example.toml) to
`~/.config/cross-model-review/config.toml` or `<repo>/.cross-model-review.toml`.
Models, CLI paths, timeouts, test commands, iteration count and the workspace
mode are all configurable. A minimal project file:

```toml
[[tests.commands]]
name = "unit"
command = ["uv", "run", "pytest", "-q"]
```

> A repository config can set CLI paths and commands that are executed on your
> machine. Only run cross-model-review on repositories you trust, or use
> `--no-config-discovery --config <trusted file>`.

## Quality gate

| Check | Passes when |
|---|---|
| `critical_findings` | the final review has 0 Critical findings |
| `high_findings` | the final review has 0 High findings |
| `required_tests` | every required test command exits 0 on the final state |
| `final_review` | all three reviews ran on the final state of the code |
| `git_integrity` | HEAD, branch and index are unchanged; read-only tasks wrote nothing; your working tree did not change during the run; the fix patch applied exactly |
| `a2a_tasks` | every A2A task reached `completed` with a valid artifact |

Verdict: `failed` if any check fails; otherwise `unverified` if any check could
not be verified (for example no test commands are configured); otherwise `passed`.
Medium/Low findings are reported but do not block.

## How your work is protected

- **Snapshot, not stash.** Your working tree is captured with a temporary copy
  of the index (`GIT_INDEX_FILE`) into `refs/cross-model-review/<run-id>`. Your
  real index, HEAD and files are never modified by this step.
- **Isolated worktree (default).** Agents work in a temporary detached
  worktree. Fixes are applied back with `git apply` (never `--index`), so they
  appear as unstaged changes and your staged content is untouched. If your
  working tree changed during the run, nothing is applied and the patch is
  kept instead.
- **Sandboxing is enforced by the CLIs, not by prompts.**
  - Codex: `--sandbox read-only` for review/verify, `--sandbox workspace-write`
    with network disabled for fixes, `approval_policy="never"`, `--ephemeral`,
    and `--ignore-user-config` (so a permissive `sandbox_mode`, MCP servers or
    profiles in `~/.codex/config.toml` do not apply).
  - Claude Code: `--tools Read,Grep,Glob`, `--permission-mode dontAsk`,
    `--restricted` (file access confined to the workspace), `--strict-mcp-config`
    (no MCP servers), `--disable-slash-commands`, `--no-session-persistence`.
  - The orchestrator additionally fingerprints the workspace before and after
    every task. A read-only task that changed anything, or a fix that moved
    HEAD, aborts the run.
- **One writer at a time.** `flock`-based workspace locks: write tasks take an
  exclusive lock, read tasks a shared one, and a second run on the same
  repository is refused. Busy requests are rejected, not queued.
- **No commits, pushes or deploys** are ever performed.

## A2A details

- Agent Cards: [agent-cards/](agent-cards/) (generated by `cross-model-review agent-card codex|opus`).
- Each skill call is one A2A `Task`. The request `Message` holds one `DataPart`
  ([skill-request schema](schemas/skill-request.schema.json)); a completed task
  carries exactly one `Artifact` named `<skill>-result` with one `DataPart`
  ([skill-result schema](schemas/skill-result.schema.json)). Failures use
  `TASK_STATE_FAILED` / `TASK_STATE_REJECTED` with an error `DataPart` in the
  status message. `CancelTask` stops the CLI process group.
- Servers bind to `127.0.0.1` only, check the `Host` header, and require a
  per-run random bearer token (declared as an HTTP bearer security scheme in the
  Agent Card). The orchestrator refuses non-loopback agent URLs.
- Run an agent by hand (for example with the A2A Inspector or TCK):

  ```bash
  CROSS_MODEL_REVIEW_A2A_TOKEN=secret cross-model-review serve-agent codex \
      --port 9999 --allowed-root /path/to/repo
  ```

  Then point the orchestrator at it with `agents.codex.url = "http://127.0.0.1:9999/"`
  and the same token in `CROSS_MODEL_REVIEW_A2A_TOKEN`.

## Development

```bash
uv sync
uv run pytest -q          # unit + E2E tests (mock agents and fake CLIs, no model calls)
uv run ruff check src tests
uv run cross-model-review export-schemas schemas   # after changing models
```

Design notes, trade-offs and known limitations: [docs/design.md](docs/design.md).

## License

[Apache License 2.0](LICENSE)
