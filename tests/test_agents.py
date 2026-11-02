"""A2A agent server behaviour: auth, workspace policy, locking, request validation."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from a2a.helpers import new_data_part, new_text_part
from a2a.types import Message, Role, SendMessageRequest
from conftest import TOKEN, MockBackend, agent_server, finding, git, review

from cross_model_review.models import (
    Finding,
    ReviewTarget,
    SkillRequest,
    TaskFinding,
    WorkspaceRef,
)
from cross_model_review.orchestrator.client import AgentClient, AgentConnectionError


def make_request(repo, skill="review_changes", findings=None) -> SkillRequest:
    head = git(repo, "rev-parse", "HEAD").strip()
    return SkillRequest(
        skill=skill,
        run_id="test",
        iteration=1,
        workspace=WorkspaceRef(kind="local_path", path=str(repo)),
        target=ReviewTarget(
            base_ref="HEAD", base_commit=head, changed_files=[], diff="", diff_truncated=False
        ),
        findings=[
            TaskFinding(source="codex", finding=Finding.model_validate(f)) for f in findings or []
        ],
    )


async def connect(url: str, skills: set[str], token: str = TOKEN) -> AgentClient:
    client = AgentClient("codex", url, token, skills)
    await client.connect()
    return client


async def call(client: AgentClient, request: SkillRequest, timeout_s: float = 20):
    return await client.call(request, timeout_s=timeout_s, cancel_timeout_s=5)


async def test_agent_card_and_auth(tmp_path, repo, lock_dir):
    async with agent_server(MockBackend("codex"), lock_dir=lock_dir, log_dir=tmp_path / "l") as (
        url,
        _,
    ):
        async with httpx.AsyncClient() as http:
            card = (await http.get(url + ".well-known/agent-card.json")).json()
            assert {s["id"] for s in card["skills"]} == {
                "review_changes",
                "fix_findings",
                "verify_changes",
            }
            assert card["capabilities"]["streaming"] is True
            assert "bearer" in card["securitySchemes"]
            # JSON-RPC requires the bearer token.
            rpc = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}
            assert (await http.post(url, json=rpc)).status_code == 401
            wrong = {"Authorization": "Bearer nope"}
            assert (await http.post(url, json=rpc, headers=wrong)).status_code == 401
            # DNS-rebinding style requests with a foreign Host header are refused.
            evil = {"Authorization": f"Bearer {TOKEN}", "Host": "evil.example"}
            assert (await http.post(url, json=rpc, headers=evil)).status_code == 400

        client = await connect(url, {"review_changes"})
        outcome = await call(client, make_request(repo))
        assert outcome.ok, outcome.record
        await client.close()

        with pytest.raises(AgentConnectionError, match="lacks skills"):
            await connect(url, {"review_design"})


async def test_rejects_workspace_outside_allowed_roots(tmp_path, repo, lock_dir):
    backend = MockBackend("codex")
    roots = [tmp_path / "elsewhere"]
    async with agent_server(backend, lock_dir=lock_dir, log_dir=tmp_path / "l", roots=roots) as (
        url,
        _,
    ):
        client = await connect(url, {"review_changes"})
        outcome = await call(client, make_request(repo))
        await client.close()
    assert outcome.record.state == "rejected"
    assert "workspace_not_allowed" in outcome.record.error
    assert backend.calls == {}


async def test_rejects_malformed_requests(tmp_path, repo, lock_dir):
    backend = MockBackend("codex")
    async with agent_server(backend, lock_dir=lock_dir, log_dir=tmp_path / "l") as (url, _):
        client = await connect(url, {"review_changes"})
        bad_payloads = [
            [new_text_part("please review")],
            [new_data_part({"skill": "review_changes"})],
            [new_data_part({**make_request(repo).model_dump(mode="json", by_alias=True), "x": 1})],
            [
                new_data_part(
                    make_request(repo).model_dump(mode="json", by_alias=True) | {"skill": "deploy"}
                )
            ],
        ]
        states = []
        for parts in bad_payloads:
            message = Message(role=Role.ROLE_USER, message_id=str(uuid.uuid4()), parts=parts)
            async for event in client._client.send_message(SendMessageRequest(message=message)):
                if event.HasField("status_update"):
                    states.append(event.status_update.status.state)
        await client.close()
    from a2a.types import TaskState

    assert states.count(TaskState.TASK_STATE_REJECTED) == len(bad_payloads)
    assert backend.calls == {}


async def test_concurrent_writes_are_rejected(tmp_path, repo, lock_dir):
    release = asyncio.Event()

    async def slow_fix(request, ctx, n):
        await release.wait()
        return {
            "summary": "s",
            "items": [{"finding_id": "F1", "status": "fixed", "notes": ""}],
            "files_changed": [],
        }

    backend = MockBackend("codex", {"fix_findings": slow_fix})
    async with agent_server(backend, lock_dir=lock_dir, log_dir=tmp_path / "l") as (url, _):
        client = await connect(url, {"fix_findings", "review_changes"})
        fix = make_request(repo, "fix_findings", [finding("F1")])
        first = asyncio.create_task(call(client, fix))
        await asyncio.wait_for(backend.started.wait(), 10)

        second = await call(client, fix)
        reader = await call(client, make_request(repo))
        release.set()
        first_outcome = await first
        await client.close()

    assert first_outcome.ok, first_outcome.record
    assert second.record.state == "rejected" and "workspace_busy" in second.record.error
    assert reader.record.state == "rejected" and "workspace_busy" in reader.record.error
    assert backend.calls["fix_findings"] == 1


async def test_writers_on_separate_agents_share_the_lock(tmp_path, repo, lock_dir):
    """The lock is a file lock, so it also excludes writers in other processes/servers."""
    from cross_model_review.locks import workspace_lock

    backend = MockBackend("codex")
    async with agent_server(backend, lock_dir=lock_dir, log_dir=tmp_path / "l") as (url, _):
        client = await connect(url, {"fix_findings"})
        with workspace_lock(lock_dir, repo, exclusive=False):  # someone is reading
            outcome = await call(client, make_request(repo, "fix_findings", [finding("F1")]))
        await client.close()
    assert outcome.record.state == "rejected"
    assert backend.calls == {}


async def test_output_referencing_unknown_findings_fails(tmp_path, repo, lock_dir):
    async def bogus_fix(request, ctx, n):
        return {
            "summary": "s",
            "items": [{"finding_id": "NOT-SENT", "status": "fixed", "notes": ""}],
            "files_changed": [],
        }

    backend = MockBackend("codex", {"fix_findings": bogus_fix})
    async with agent_server(backend, lock_dir=lock_dir, log_dir=tmp_path / "l") as (url, _):
        client = await connect(url, {"fix_findings"})
        outcome = await call(client, make_request(repo, "fix_findings", [finding("F1")]))
        await client.close()
    assert outcome.record.state == "failed"
    assert "invalid_output" in outcome.record.error


async def test_invalid_model_output_fails_task(tmp_path, repo, lock_dir):
    async def traversal(request, ctx, n):
        return review(finding("F1", file="../../etc/passwd"))

    backend = MockBackend("codex", {"review_changes": traversal})
    async with agent_server(backend, lock_dir=lock_dir, log_dir=tmp_path / "l") as (url, _):
        client = await connect(url, {"review_changes"})
        outcome = await call(client, make_request(repo))
        await client.close()
    assert outcome.record.state == "failed"
    assert "invalid_output" in outcome.record.error


async def test_non_loopback_agents_are_refused():
    with pytest.raises(AgentConnectionError, match="loopback"):
        await connect("http://192.0.2.1:8080/", {"review_changes"})
