"""A2A client wrapper: one call = one Task, validated end to end."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.helpers import get_data_parts, get_text_parts, new_data_part
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)
from pydantic import ValidationError

from cross_model_review.models import AgentErrorInfo, SkillRequest, SkillResult, TaskRecord

log = logging.getLogger(__name__)

JSON_MEDIA_TYPE = "application/json"
TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}
# The agents never ask for input; treat these as protocol violations.
INTERRUPTED_STATES = {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}
_STATE_NAMES = {
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_CANCELED: "canceled",
    TaskState.TASK_STATE_REJECTED: "rejected",
}


class AgentConnectionError(Exception):
    pass


def is_loopback_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return False
    if parts.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        return False


@dataclass
class TaskOutcome:
    record: TaskRecord
    result: SkillResult | None

    @property
    def ok(self) -> bool:
        return self.record.state == "completed" and self.result is not None


class AgentClient:
    def __init__(self, name: str, url: str, token: str, required_skills: set[str]) -> None:
        self.name = name
        self.url = url
        self.token = token
        self.required_skills = required_skills
        self.card: AgentCard | None = None
        self._http: httpx.AsyncClient | None = None
        self._client: Client | None = None

    async def connect(self) -> None:
        if not is_loopback_url(self.url):
            raise AgentConnectionError(
                f"{self.name}: only loopback agents are supported ({self.url})"
            )
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.token}"},
            # Streams may stay quiet for a long CLI session; overall time is
            # bounded by the per-task timeout instead.
            timeout=httpx.Timeout(connect=10, read=None, write=30, pool=30),
            trust_env=False,
        )
        try:
            card = await A2ACardResolver(self._http, self.url).get_agent_card()
        except Exception as exc:
            raise AgentConnectionError(f"{self.name}: cannot fetch agent card: {exc}") from exc
        missing = self.required_skills - {s.id for s in card.skills}
        if missing:
            raise AgentConnectionError(f"{self.name}: agent card lacks skills {sorted(missing)}")
        if not card.capabilities.streaming:
            raise AgentConnectionError(f"{self.name}: agent does not support streaming")
        for interface in card.supported_interfaces:
            if not is_loopback_url(interface.url):
                raise AgentConnectionError(
                    f"{self.name}: agent card advertises non-loopback interface {interface.url}"
                )
        self.card = card
        self._client = ClientFactory(
            ClientConfig(
                httpx_client=self._http,
                streaming=True,
                accepted_output_modes=[JSON_MEDIA_TYPE],
            )
        ).create(card)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._http is not None:
            await self._http.aclose()

    async def _cancel(self, task_id: str, timeout_s: float) -> None:
        assert self._client is not None
        try:
            await asyncio.wait_for(
                self._client.cancel_task(CancelTaskRequest(id=task_id)), timeout_s
            )
            log.info("task cancelled", extra={"agent": self.name, "task_id": task_id})
        except Exception as exc:
            log.warning(
                "task cancel failed",
                extra={"agent": self.name, "task_id": task_id, "error": repr(exc)},
            )

    async def call(
        self,
        request: SkillRequest,
        *,
        timeout_s: float,
        cancel_timeout_s: float,
    ) -> TaskOutcome:
        assert self._client is not None, "connect() first"
        started = time.monotonic()
        skill = request.skill
        message = Message(
            role=Role.ROLE_USER,
            message_id=str(uuid.uuid4()),
            parts=[
                new_data_part(
                    request.model_dump(mode="json", by_alias=True), media_type=JSON_MEDIA_TYPE
                )
            ],
        )
        send = SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(accepted_output_modes=[JSON_MEDIA_TYPE]),
        )
        task_id: str | None = None
        task_known = asyncio.Event()

        def record(state, error=None, summary=None) -> TaskRecord:
            return TaskRecord(
                agent=self.name,
                skill=skill,
                iteration=request.iteration,
                task_id=task_id,
                state=state,
                duration_s=round(time.monotonic() - started, 3),
                error=error,
                summary=summary,
            )

        async def consume() -> TaskOutcome | None:
            """Follow the stream until a terminal state. Returns early outcomes only."""
            nonlocal task_id
            async for event in self._client.send_message(send):
                kind = event.WhichOneof("payload")
                if kind == "message":
                    return TaskOutcome(
                        record("invalid_artifact", "agent replied with a Message, expected a Task"),
                        None,
                    )
                if kind == "task":
                    task_id = event.task.id
                    state = event.task.status.state
                elif kind == "status_update":
                    task_id = task_id or event.status_update.task_id
                    state = event.status_update.status.state
                else:
                    continue
                task_known.set()
                if state in INTERRUPTED_STATES:
                    await self._cancel(task_id, cancel_timeout_s)
                    return TaskOutcome(
                        record("error", f"unexpected state {TaskState.Name(state)}"), None
                    )
                if state in TERMINAL_STATES:
                    return None
            return None

        log.info("task send", extra={"agent": self.name, "skill": skill})
        consumer = asyncio.create_task(consume())
        try:
            early = await asyncio.wait_for(asyncio.shield(consumer), timeout_s)
        except TimeoutError:
            consumer.cancel()
            if task_id:
                await self._cancel(task_id, cancel_timeout_s)
            return TaskOutcome(record("timeout", f"no result within {timeout_s:.0f}s"), None)
        except asyncio.CancelledError:
            # The agent may have created the task before its id reached us;
            # wait briefly for the id so the task (and its CLI) can be cancelled.
            if task_id is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(task_known.wait(), cancel_timeout_s)
            if task_id:
                await self._cancel(task_id, cancel_timeout_s)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            raise
        except Exception as exc:
            if task_id:
                await self._cancel(task_id, cancel_timeout_s)
            return TaskOutcome(record("error", f"A2A error: {exc!r}"), None)
        if early is not None:
            return early

        if task_id is None:
            return TaskOutcome(record("error", "stream ended without a task"), None)
        try:
            task = await asyncio.wait_for(self._client.get_task(GetTaskRequest(id=task_id)), 30)
        except Exception as exc:
            return TaskOutcome(record("error", f"cannot fetch task: {exc!r}"), None)
        return self._evaluate(task, task_id, request, record)

    def _evaluate(self, task: Task, task_id: str, request: SkillRequest, record) -> TaskOutcome:
        if task.id != task_id:
            return TaskOutcome(record("invalid_artifact", "task id mismatch"), None)
        state = task.status.state
        if state not in TERMINAL_STATES:
            return TaskOutcome(record("error", f"task not terminal: {TaskState.Name(state)}"), None)
        if state != TaskState.TASK_STATE_COMPLETED:
            return TaskOutcome(record(_STATE_NAMES[state], _status_error(task)), None)
        try:
            result = parse_result_artifact(task, request.skill, self.name)
        except ValueError as exc:
            log.warning(
                "invalid artifact",
                extra={"agent": self.name, "task_id": task_id, "error": str(exc)},
            )
            return TaskOutcome(record("invalid_artifact", str(exc)[:4000]), None)
        return TaskOutcome(record("completed", summary=result.output.summary[:500]), result)


def _status_error(task: Task) -> str:
    message = task.status.message
    for data in get_data_parts(message.parts):
        if isinstance(data, dict) and "error" in data:
            try:
                info = AgentErrorInfo.model_validate(data["error"])
                return f"{info.code}: {info.message}"
            except ValidationError:
                break
    text = " ".join(get_text_parts(message.parts)).strip()
    return text[:4000] or "no error detail"


def parse_result_artifact(task: Task, skill: str, agent: str) -> SkillResult:
    """Validate the single result artifact of a completed task."""
    name = f"{skill}-result"
    artifacts = [a for a in task.artifacts if a.name == name]
    if len(task.artifacts) != 1 or len(artifacts) != 1:
        raise ValueError(
            f"expected exactly one artifact named {name!r}, got {[a.name for a in task.artifacts]}"
        )
    parts = artifacts[0].parts
    data = get_data_parts(parts)
    if len(parts) != 1 or len(data) != 1:
        raise ValueError("result artifact must contain exactly one data part")
    try:
        result = SkillResult.model_validate(data[0])
    except ValidationError as exc:
        raise ValueError(f"result artifact does not match schema: {exc}") from exc
    if result.skill != skill:
        raise ValueError(f"result skill {result.skill!r} does not match request {skill!r}")
    if result.agent != agent:
        raise ValueError(f"result agent {result.agent!r} does not match {agent!r}")
    return result
