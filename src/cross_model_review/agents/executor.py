"""A2A ``AgentExecutor`` that runs one skill per Task through a CLI backend."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from a2a.helpers import get_data_parts, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from pydantic import ValidationError

from cross_model_review.agents.backends import AgentBackend, AgentError, RunContext
from cross_model_review.locks import LockBusy, workspace_lock
from cross_model_review.models import WRITE_SKILLS, AgentErrorInfo, SkillRequest, SkillResult

log = logging.getLogger(__name__)

JSON_MEDIA_TYPE = "application/json"
CANCEL_WAIT_S = 30.0


@dataclass
class _Run:
    updater: TaskUpdater
    job: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


def result_artifact_name(skill: str) -> str:
    return f"{skill}-result"


class CLIAgentExecutor(AgentExecutor):
    def __init__(
        self,
        backend: AgentBackend,
        *,
        allowed_roots: list[Path],
        lock_dir: Path,
        log_dir: Path,
    ) -> None:
        self.backend = backend
        self.allowed_roots = [Path(os.path.realpath(r)) for r in allowed_roots]
        self.lock_dir = lock_dir
        self.log_dir = log_dir
        self._runs: dict[str, _Run] = {}

    # ---- helpers ------------------------------------------------------------

    def _parse_request(self, context: RequestContext) -> SkillRequest:
        message = context.message
        if message is None:
            raise AgentError("invalid_request", "missing message")
        data = get_data_parts(message.parts)
        if len(data) != 1 or len(message.parts) != 1:
            raise AgentError("invalid_request", "expected exactly one data part")
        try:
            request = SkillRequest.model_validate(data[0])
        except ValidationError as exc:
            raise AgentError("invalid_request", f"invalid skill request: {exc}") from exc
        if request.skill not in self.backend.skill_ids():
            raise AgentError("invalid_request", f"unsupported skill: {request.skill}")
        return request

    def _check_workspace(self, request: SkillRequest) -> Path:
        workspace = Path(os.path.realpath(request.workspace.path))
        if not any(workspace == r or workspace.is_relative_to(r) for r in self.allowed_roots):
            raise AgentError(
                "workspace_not_allowed", f"workspace outside allowed roots: {workspace}"
            )
        if not workspace.is_dir() or not (workspace / ".git").exists():
            raise AgentError("workspace_not_allowed", f"not a Git working tree: {workspace}")
        return workspace

    @staticmethod
    def _error_message(updater: TaskUpdater, error: AgentError):
        info = AgentErrorInfo(code=error.code, message=error.message[:8000])
        return updater.new_agent_message(
            parts=[
                new_text_part(f"{error.code}: {error.message[:2000]}"),
                new_data_part({"error": info.model_dump(mode="json")}, media_type=JSON_MEDIA_TYPE),
            ]
        )

    async def _finish(self, run: _Run, state: TaskState, error: AgentError | None = None) -> None:
        message = self._error_message(run.updater, error) if error else None
        try:
            await run.updater.update_status(state, message=message)
        except RuntimeError:
            # Already terminal (for example cancelled concurrently).
            pass

    # ---- AgentExecutor ------------------------------------------------------

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        if context.current_task is None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        run = _Run(TaskUpdater(event_queue, task_id, context_id))
        self._runs[task_id] = run
        try:
            await self._execute(run, context)
        finally:
            run.done.set()
            self._runs.pop(task_id, None)

    async def _execute(self, run: _Run, context: RequestContext) -> None:
        if context.current_task is not None:
            # Each task is one CLI session; follow-up turns are not supported.
            error = AgentError("invalid_request", "tasks do not accept follow-up messages")
            await self._finish(run, TaskState.TASK_STATE_REJECTED, error)
            return
        try:
            request = self._parse_request(context)
            workspace = self._check_workspace(request)
        except AgentError as error:
            log.warning("request rejected", extra={"task_id": context.task_id, "code": error.code})
            await self._finish(run, TaskState.TASK_STATE_REJECTED, error)
            return

        exclusive = request.skill in WRITE_SKILLS
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(workspace_lock(self.lock_dir, workspace, exclusive=exclusive))
            except LockBusy as exc:
                await self._finish(
                    run, TaskState.TASK_STATE_REJECTED, AgentError("workspace_busy", str(exc))
                )
                return
            await run.updater.start_work(
                message=run.updater.new_agent_message(
                    parts=[
                        new_text_part(
                            f"running {request.skill} ({'write' if exclusive else 'read-only'})"
                        )
                    ]
                )
            )
            ctx = RunContext(task_id=context.task_id, workspace=workspace, log_dir=self.log_dir)
            run.job = asyncio.create_task(self.backend.run(request, ctx))
            try:
                output, info = await run.job
            except AgentError as error:
                log.warning("skill failed", extra={"task_id": ctx.task_id, "code": error.code})
                await self._finish(run, TaskState.TASK_STATE_FAILED, error)
                return
            except asyncio.CancelledError:
                # Cancellation of execute() itself (task cancel or server
                # shutdown): make sure the CLI process is gone before the lock
                # is released.
                run.job.cancel()
                await asyncio.gather(run.job, return_exceptions=True)
                raise
            except Exception as exc:
                log.exception("skill crashed", extra={"task_id": ctx.task_id})
                await self._finish(
                    run, TaskState.TASK_STATE_FAILED, AgentError("internal", repr(exc))
                )
                return

            result = SkillResult(
                skill=request.skill, agent=self.backend.agent_name, output=output, run=info
            )
            payload = json.loads(result.model_dump_json(by_alias=True))
            await run.updater.add_artifact(
                [new_data_part(payload, media_type=JSON_MEDIA_TYPE)],
                name=result_artifact_name(request.skill),
                last_chunk=True,
            )
            await self._finish(run, TaskState.TASK_STATE_COMPLETED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        run = self._runs.get(task_id)
        log.info("cancel requested", extra={"task_id": task_id, "running": run is not None})
        if run is not None and run.job is not None:
            run.job.cancel()
            # Wait until the CLI process group has been terminated.
            await asyncio.wait({run.job}, timeout=CANCEL_WAIT_S)
        updater = run.updater if run else TaskUpdater(event_queue, task_id, context.context_id)
        try:
            await updater.cancel(
                message=updater.new_agent_message(parts=[new_text_part("cancelled by client")])
            )
        except RuntimeError:
            pass

    async def shutdown(self) -> None:
        """Cancel every running CLI session (server shutdown)."""
        jobs = [r.job for r in self._runs.values() if r.job is not None]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.wait(jobs, timeout=CANCEL_WAIT_S)
