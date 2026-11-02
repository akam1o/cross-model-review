# Examples

[日本語](README.ja.md)

| File | Content |
|---|---|
| [demo.sh](demo.sh) | Creates a throw-away repository with a seeded bug and runs a real review (calls Codex and Claude Code) |
| [demo-config.toml](demo-config.toml) | Configuration used by the demo: one required test, 2 iterations, shorter timeouts, a per-call budget cap for Claude |
| [sample-report-passed.md](sample-report-passed.md) | Report of a real demo run |
| [sample-report-invalid-output.md](sample-report-invalid-output.md) | Report of a run that aborted because an agent's output failed validation |

## The demo run

The uncommitted change replaces a stub with
`[items[i:i + size] for i in range(0, len(items) - size, size)]`, which drops the
last chunk. In the [passing run](sample-report-passed.md):

1. Iteration 1: Codex `review_changes` reported a High finding ("Range stop
   excludes the final chunk"); `fix_findings` changed the range stop to
   `len(items)`; `verify_changes` confirmed it was resolved. Opus then reviewed
   design and simplification (Medium/Low/Info findings only).
2. Iteration 2: all three reviews ran on the fixed code and found nothing at
   Critical/High, so the loop converged.
3. The required test passed, the gate was `PASSED`, and the fix was applied
   to the working tree as an unstaged change. Nothing was committed.

The test command created `__pycache__/` (the demo repository has no
`.gitignore`); it was removed from the worktree and reported as a warning.

The [aborted run](sample-report-invalid-output.md) was captured with an
earlier build whose CLI output schema was missing the `title` field. Codex
followed that schema, the agent rejected the output as invalid, and the
orchestrator stopped with a failed gate without changing the repository. It
is kept as an example of how invalid agent output is handled.

Run the demo yourself (from a checkout, after `uv tool install .`):

```bash
sh examples/demo.sh
```
