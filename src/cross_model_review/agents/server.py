"""Loopback-only A2A HTTP server for one agent."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import socket
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from cross_model_review.agents.backends import AgentBackend
from cross_model_review.agents.cards import build_agent_card
from cross_model_review.agents.executor import CLIAgentExecutor

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
TOKEN_ENV = "CROSS_MODEL_REVIEW_A2A_TOKEN"


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` except for the public Agent Card."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != AGENT_CARD_WELL_KNOWN_PATH:
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"")
            if not hmac.compare_digest(provided, self.expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@dataclass
class AgentApp:
    app: Starlette
    executor: CLIAgentExecutor
    handler: DefaultRequestHandler


def build_app(
    backend: AgentBackend,
    *,
    url: str,
    token: str | None,
    allowed_roots: list[Path],
    lock_dir: Path,
    log_dir: Path,
) -> AgentApp:
    card = build_agent_card(backend, url)
    executor = CLIAgentExecutor(
        backend, allowed_roots=allowed_roots, lock_dir=lock_dir, log_dir=log_dir
    )
    handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card
    )
    middleware = [Middleware(TrustedHostMiddleware, allowed_hosts=list(LOOPBACK_HOSTS))]
    if token:
        middleware.append(Middleware(BearerAuthMiddleware, token=token))
    app = Starlette(
        routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"),
        middleware=middleware,
    )
    return AgentApp(app=app, executor=executor, handler=handler)


def bind_loopback(host: str, port: int) -> socket.socket:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing to bind non-loopback host {host!r}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    return sock


def _watch_stdin_eof(loop: asyncio.AbstractEventLoop, on_eof: Callable[[], None]) -> None:
    """Shut down when the parent closes our stdin (including when it dies)."""

    def reader() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        loop.call_soon_threadsafe(on_eof)

    threading.Thread(target=reader, name="stdin-eof-watch", daemon=True).start()


async def serve(
    backend: AgentBackend,
    *,
    port: int,
    token: str | None,
    allowed_roots: list[Path],
    lock_dir: Path,
    log_dir: Path,
    exit_on_stdin_eof: bool,
    on_ready: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    sock = bind_loopback("127.0.0.1", port)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    agent = build_app(
        backend,
        url=url,
        token=token,
        allowed_roots=allowed_roots,
        lock_dir=lock_dir,
        log_dir=log_dir,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            agent.app,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=5,
            lifespan="off",
        )
    )

    background: set[asyncio.Task] = set()

    def request_exit() -> None:
        log.info("shutdown requested")
        # Kill CLI sessions first; uvicorn's graceful shutdown waits for open
        # SSE streams, which only end once the tasks do.
        task = asyncio.ensure_future(agent.executor.shutdown())
        background.add(task)
        task.add_done_callback(background.discard)
        server.should_exit = True

    if exit_on_stdin_eof:
        _watch_stdin_eof(asyncio.get_running_loop(), request_exit)

    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if serve_task.done():
                await serve_task
                raise RuntimeError("server exited during startup")
            await asyncio.sleep(0.02)
        log.info("agent ready", extra={"agent": backend.agent_name, "url": url})
        if on_ready is not None:
            await on_ready(url)
        await serve_task
    finally:
        await agent.executor.shutdown()
        if not serve_task.done():
            server.should_exit = True
            await asyncio.gather(serve_task, return_exceptions=True)
        await agent.handler.aclose()
        sock.close()


async def print_ready_line(agent_name: str, url: str) -> None:
    sys.stdout.write(json.dumps({"event": "ready", "agent": agent_name, "url": url}) + "\n")
    sys.stdout.flush()
