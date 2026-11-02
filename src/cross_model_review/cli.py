"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from importlib import resources
from pathlib import Path

from cross_model_review import __version__

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_UNVERIFIED = 2
EXIT_USAGE = 3
EXIT_CANCELLED = 130


def _load_agent_config(path: Path):
    from cross_model_review.config import Config, load_config

    if path.suffix == ".json":
        return Config.model_validate_json(path.read_text(encoding="utf-8"))
    return load_config(None, path)[0]


def _backend(kind: str, config):
    from cross_model_review.agents.backends import ClaudeBackend, CodexBackend

    if kind == "codex":
        return CodexBackend(config.agents.codex)
    return ClaudeBackend(config.agents.opus)


# ---------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    from cross_model_review.config import load_config
    from cross_model_review.logs import setup_logging
    from cross_model_review.orchestrator.report import render_markdown
    from cross_model_review.orchestrator.workflow import (
        Orchestrator,
        RunCancelled,
        RunOptions,
        UsageError,
    )

    setup_logging("orchestrator", level=args.log_level)
    repo = Path(args.repo).resolve()
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=repo, capture_output=True, text=True
        )
        root = Path(top.stdout.strip()) if top.returncode == 0 else None
        config, used = load_config(root, Path(args.config) if args.config else None)
    except Exception as exc:
        print(f"cross-model-review: invalid configuration: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.max_iterations:
        config = config.model_copy(update={"max_iterations": args.max_iterations})
    if args.mode:
        config.workspace = config.workspace.model_copy(update={"mode": args.mode})

    orchestrator = Orchestrator(RunOptions(repo=repo, config=config, base=args.base))

    async def main():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, task.cancel)
        return await orchestrator.run()

    try:
        report = asyncio.run(main())
    except UsageError as exc:
        print(f"cross-model-review: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except RunCancelled as exc:
        print(render_markdown(exc.report))
        return EXIT_CANCELLED
    except (KeyboardInterrupt, asyncio.CancelledError):
        return EXIT_CANCELLED

    if args.json:
        print(report.model_dump_json(indent=2, by_alias=True))
    else:
        if used:
            print(f"<!-- config: {', '.join(used)} -->")
        print(render_markdown(report))
    if report.status == "no_changes":
        return EXIT_PASSED
    return {"passed": EXIT_PASSED, "failed": EXIT_FAILED, "unverified": EXIT_UNVERIFIED}[
        report.gate.verdict
    ]


def cmd_serve_agent(args: argparse.Namespace) -> int:
    from cross_model_review.agents.server import TOKEN_ENV, print_ready_line, serve
    from cross_model_review.locks import default_lock_dir
    from cross_model_review.logs import setup_logging

    setup_logging(f"agent-{args.kind}", level=args.log_level)
    config = _load_agent_config(Path(args.config)) if args.config else _default_config()
    token = os.environ.get(TOKEN_ENV) or None
    if token is None and not args.insecure_no_auth:
        print(f"{TOKEN_ENV} is not set (use --insecure-no-auth for local testing)", file=sys.stderr)
        return EXIT_USAGE
    if not args.allowed_root:
        print("at least one --allowed-root is required", file=sys.stderr)
        return EXIT_USAGE
    backend = _backend(args.kind, config)
    log_dir = Path(args.log_dir) if args.log_dir else Path.cwd() / ".cross-model-review-agent-logs"

    async def on_ready(url: str) -> None:
        await print_ready_line(backend.agent_name, url)

    try:
        asyncio.run(
            serve(
                backend,
                port=args.port,
                token=token,
                allowed_roots=[Path(p) for p in args.allowed_root],
                lock_dir=Path(args.lock_dir) if args.lock_dir else default_lock_dir(),
                log_dir=log_dir,
                exit_on_stdin_eof=args.exit_on_stdin_eof,
                on_ready=on_ready,
            )
        )
    except KeyboardInterrupt:
        pass
    return 0


def _default_config():
    from cross_model_review.config import Config

    return Config()


def cmd_agent_card(args: argparse.Namespace) -> int:
    from a2a.server.request_handlers.response_helpers import agent_card_to_dict

    from cross_model_review.agents.cards import build_agent_card

    card = build_agent_card(_backend(args.kind, _default_config()), args.url)
    print(json.dumps(agent_card_to_dict(card), indent=2, ensure_ascii=False))
    return 0


def cmd_export_schemas(args: argparse.Namespace) -> int:
    from cross_model_review.schemas import export_schemas

    for path in export_schemas(Path(args.directory)):
        print(path)
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    dest = Path(args.dest).expanduser() / "cross-model-review"
    target = dest / "SKILL.md"
    if target.exists() and not args.force:
        print(f"{target} exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_USAGE
    dest.mkdir(parents=True, exist_ok=True)
    source = resources.files("cross_model_review").joinpath("skill/SKILL.md")
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"installed {target}")
    if shutil.which("cross-model-review") is None:
        print("note: `cross-model-review` is not on PATH; install it with `uv tool install .`")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from cross_model_review.agents.backends import parse_version
    from cross_model_review.config import Config

    config = Config()
    ok = True
    checks = [
        ("git", "git", None),
        ("codex", config.agents.codex.cli_path, config.agents.codex.min_cli_version),
        ("claude", config.agents.opus.cli_path, config.agents.opus.min_cli_version),
    ]
    for name, path, minimum in checks:
        exe = shutil.which(path)
        if exe is None:
            print(f"[missing] {name}: {path} not found")
            ok = False
            continue
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
        version = (out.stdout or out.stderr).strip()
        status = "ok"
        if minimum and (parse_version(version) or (0, 0, 0)) < parse_version(minimum):
            status, ok = f"too old (need >= {minimum})", False
        print(f"[{status}] {name}: {version} ({exe})")
    print(f"[ok] python: {sys.version.split()[0]}; cross-model-review {__version__}")
    return 0 if ok else 1


# -------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cross-model-review", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="review the current changes of a Git repository")
    run.add_argument("--repo", default=".", help="repository path (default: current directory)")
    run.add_argument("--base", default="HEAD", help="review changes since this ref (default: HEAD)")
    run.add_argument("--config", help="explicit config file (TOML)")
    run.add_argument("--max-iterations", type=int, choices=range(1, 11), metavar="N")
    run.add_argument("--mode", choices=["worktree", "in-place"])
    run.add_argument("--json", action="store_true", help="print report JSON instead of Markdown")
    run.add_argument("--log-level", default="INFO")
    run.set_defaults(func=cmd_run)

    serve = sub.add_parser("serve-agent", help="run an A2A agent server on 127.0.0.1")
    serve.add_argument("kind", choices=["codex", "opus"])
    serve.add_argument("--config", help="config file (TOML or JSON)")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--allowed-root", action="append", default=[])
    serve.add_argument("--lock-dir")
    serve.add_argument("--log-dir")
    serve.add_argument("--exit-on-stdin-eof", action="store_true")
    serve.add_argument(
        "--insecure-no-auth", action="store_true", help="disable bearer auth (testing only)"
    )
    serve.add_argument("--log-level", default="INFO")
    serve.set_defaults(func=cmd_serve_agent)

    card = sub.add_parser("agent-card", help="print an Agent Card")
    card.add_argument("kind", choices=["codex", "opus"])
    card.add_argument("--url", default="http://127.0.0.1:0/")
    card.set_defaults(func=cmd_agent_card)

    schemas = sub.add_parser("export-schemas", help="write JSON Schemas to a directory")
    schemas.add_argument("directory")
    schemas.set_defaults(func=cmd_export_schemas)

    install = sub.add_parser("install-skill", help="install the Claude Code skill")
    install.add_argument("--dest", default="~/.claude/skills", help="skills directory")
    install.add_argument("--force", action="store_true")
    install.set_defaults(func=cmd_install_skill)

    doctor = sub.add_parser("doctor", help="check CLI availability and versions")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
