# Design notes

[日本語](design.ja.md)

## Components

| Component | Module | Role |
|---|---|---|
| A2A Orchestrator | `orchestrator/workflow.py`, `client.py`, `launcher.py`, `gate.py` | Snapshots Git state, spawns or connects to agents, drives the loop, runs tests, checks Git integrity, applies fixes, evaluates the gate |
| Codex A2A Agent | `agents/backends.py` (`CodexBackend`) | `review_changes`, `fix_findings`, `verify_changes` via `codex exec` |
| Opus A2A Agent | `agents/backends.py` (`ClaudeBackend`) | `review_design`, `review_simplification` via `claude -p` |
| Shared A2A server | `agents/executor.py`, `server.py`, `cards.py` | `AgentExecutor`, Agent Card, loopback HTTP server with bearer auth |

All protocol work uses `a2a-sdk` 1.3.0: `AgentExecutor`, `TaskUpdater`,
`DefaultRequestHandler`, `InMemoryTaskStore`, `create_agent_card_routes`,
`create_jsonrpc_routes`, `A2ACardResolver`, `ClientFactory`. There is no custom
inter-agent protocol; the only project-specific contract is the JSON payload
inside `DataPart`s, declared as an Agent Card extension
(`urn:cross-model-review:ext:skill-io:v1`) and published as JSON Schema.

## Loop

```
for i in 1..max_iterations:
    codex.review_changes            -> fix Critical/High (+ failing tests) -> codex.verify_changes
    opus.review_design + opus.review_simplification (parallel, read-only)
                                     -> codex.fix_findings -> codex.verify_changes
    run test commands
    stop if nothing changed and tests did not fail, or if a fix changed nothing
if the latest three reviews did not all see the final tree:
    one more review-only round
```

A round that fixes nothing (`--review-only` and the extra final round) runs all
three reviews at the same time, since they share the read-only workspace lock
and read the same tree.

The gate uses only the findings of the latest complete review of the final
tree. Findings get fresh IDs per review and are not matched across iterations;
earlier findings are kept in the report with their fix/verify status.

## Decisions and trade-offs

**Worktree by default, in-place optional.** Agents write to a temporary
detached worktree seeded from a snapshot commit. This protects your tree
completely during the run and makes the result a reviewable patch. The cost is
that ignored files (virtualenvs, `node_modules`, build output) are absent, so
tests may need `workspace.setup_commands` or `--mode in-place`. In-place mode
relies on detection (fingerprints, HEAD/index checks) instead of isolation.

**Fixes are applied even if the gate fails.** Fixes for Critical/High
defects are still improvements when other problems remain. They are applied
only if your tree is byte-identical to the snapshot, never when the run
aborted, was cancelled or found Git issues, and always as unstaged changes. The
patch and the snapshot ref allow reverting (`git apply -R fixes.patch`). Set
`workspace.apply_fixes = false` to only produce the patch.

**Snapshot via temporary index.** `git stash` would touch the user's index and
working tree. Copying the index and running `git add -A` / `write-tree` against
the copy captures tracked, untracked and staged state without side effects.
`GIT_OPTIONAL_LOCKS=0` stops read commands from rewriting the index and hooks
are disabled for every Git call.

**Agents are separate processes.** Each agent server is a child process in
its own process group, bound to `127.0.0.1:0`, reporting its URL on stdout. It
exits when its stdin closes, which also covers an orchestrator crash. This
matches how a remote agent would be deployed and keeps CLI crashes away from
the orchestrator.

**Reject instead of queue.** A busy workspace lock produces
`TASK_STATE_REJECTED` (`workspace_busy`). Queueing behind a CLI session that
can run for half an hour would hide bugs and stretch timeouts.

**Tests run in the orchestrator.** Required tests decide the gate, so they
are executed by the orchestrator itself, not reported by a model. A failing
required test becomes a High finding that Codex is asked to fix in the next
round.

**Any failed task aborts the run.** There are no retries. A failed, timed-out,
rejected or invalid task means the final state cannot be fully reviewed, so the
run stops and the gate fails. Retrying is left to the user.

**Streaming plus `GetTask`.** The orchestrator follows the SSE stream to learn
the task id early (needed for `CancelTask`), then fetches the final `Task` and
validates its artifacts from that authoritative copy.

**Strict output schemas derived from Pydantic.** `codex --output-schema` needs
the strict structured-output dialect, so the CLI schemas drop length/pattern
keywords and mark every property as required. Pydantic re-validates the full
constraints (lengths, enums, unique IDs, repository-relative paths) on the
agent and again on the orchestrator.

**Isolated CLI configuration by default.** `codex --ignore-user-config` and
`claude --restricted --strict-mcp-config` keep user MCP servers, profiles,
permissive sandbox defaults and project settings out of review sessions. Both
can be relaxed in the config when your authentication depends on them.

## Cancellation path

1. Ctrl-C / SIGTERM / SIGHUP cancels the orchestrator's main task.
2. Each in-flight `AgentClient.call` sends A2A `CancelTask` (waiting briefly for
   the task id if the first event has not arrived yet).
3. The SDK calls `AgentExecutor.cancel`, which cancels the backend job and
   waits for it.
4. `run_process` sends SIGTERM to the CLI's process group, then SIGKILL after a
   grace period, and only then lets the cancellation continue.
5. The executor reports `TASK_STATE_CANCELED`; the workspace lock is released.
6. The orchestrator stops agent servers, removes the worktree and writes a
   `cancelled` report.

Timeouts follow the same path: the agent enforces the CLI timeout itself
(`cli_timeout`) and the orchestrator waits `task_timeout_margin_s` longer before
it cancels the task on its own.

## Remote agents (future)

Prepared but not enabled:

- Agents are addressed by URL (`agents.*.url`) and discovered through their
  Agent Card; required skills and streaming support are checked.
- Authentication is declared in the Agent Card (HTTP bearer).
- `WorkspaceRef.kind` is a discriminator; only `local_path` exists. A remote
  agent needs a transferable workspace (for example a Git bundle in a
  `FilePart`) and a way to return a patch artifact.
- `a2a.allow_remote` exists but is fixed to `false`; non-loopback URLs are
  rejected, both in configuration and in Agent Card interfaces.

## Known limitations

- **Untrusted repositories.** `<repo>/.cross-model-review.toml` can set CLI
  paths and commands. Test commands run without a sandbox.
  `--no-config-discovery --config <file>` reads only the given file, and
  `--review-only` runs no setup or test commands.
- **Process escape.** Processes that create their own session (`setsid`,
  daemons) are not reached by the process-group kill.
- **Sandbox availability.** Codex sandboxing depends on the OS (Seatbelt on
  macOS, Landlock/bubblewrap on Linux). If it is unavailable Codex fails and
  the run aborts; it never falls back to an unsandboxed mode.
- **Isolated configuration vs. authentication.** `--ignore-user-config` drops
  custom Codex model providers, and `--restricted` ignores Claude Code settings
  files (for example `apiKeyHelper`). Setting `ignore_user_config = false` /
  `restricted = false` restores them, but then user MCP servers, hooks and
  settings also apply (with Codex 0.154, `-c mcp_servers={}` did not disable
  configured servers).
- **Model judgement.** The gate is only as good as the reviews: missed defects
  are not detected, and findings can be wrong.
- **Large changes.** The diff in each prompt is truncated at `max_diff_bytes`
  (agents can still read files). Large untracked files are hashed into the
  snapshot.
- **Repository states.** Repositories without commits, unmerged paths and
  in-progress merges/rebases are refused. Changes inside submodules are not
  reviewed (only gitlink updates).
- **Test side effects.** In worktree mode, untracked files created by test or
  setup commands (for example `__pycache__/` in a repository without a
  `.gitignore`) are removed and reported as a warning. Modifying or deleting
  existing files aborts the run, as does creating any file in in-place mode
  (removing files there could delete your work). Add generated files to
  `.gitignore`.
- **Generated files in fixes.** Whatever a fix task leaves in the workspace
  becomes part of the fix patch. Codex's sandbox and `.gitignore` usually keep
  caches out, but check the patch.
- **Snapshot refs accumulate.** List them with
  `git for-each-ref refs/cross-model-review` and delete with
  `git update-ref -d <ref>`, or set `workspace.keep_snapshot_ref = false`.
- **Platforms.** macOS and Linux only.

## Protocol compatibility check

The A2A TCK (`a2aproject/a2a-tck`, JSON-RPC, `--level must`) was run against
`serve-agent codex --insecure-no-auth`: **62 passed, 5 failed, 168 skipped**.
The 5 failures (`DM-ART-001` ×4, `DM-MSG-001`) expect the agent to answer the
TCK's own scenario prompts with text/file/data artifacts or a direct `Message`.
These agents accept only the skill-request `DataPart` and reject anything else
by design, so those scenario tests cannot pass; no protocol-level failures were
reported. The A2A Inspector was not run (it is an interactive web UI).
