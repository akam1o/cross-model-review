"""Artifact validation, data models, quality gate and JSON Schemas."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from a2a.helpers import new_data_part, new_text_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import Artifact, Task, TaskState, TaskStatus
from conftest import TOKEN, MockBackend, agent_server, finding, mock_agents, review
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware

from cross_model_review.agents.cards import build_agent_card
from cross_model_review.agents.server import BearerAuthMiddleware, bind_loopback
from cross_model_review.models import (
    Finding,
    GateCheck,
    SkillResult,
    TaskRecord,
    TestsSummary,
    TrackedFinding,
)
from cross_model_review.orchestrator.client import parse_result_artifact
from cross_model_review.orchestrator.gate import evaluate_gate
from cross_model_review.orchestrator.workflow import Orchestrator, RunOptions
from cross_model_review.schemas import cli_output_schema, protocol_schemas

RUN = {
    "cli": "mock",
    "cli_version": None,
    "model": None,
    "sandbox": "read-only",
    "exit_code": 0,
    "duration_s": 1.0,
}


def result_payload(**overrides):
    payload = {
        "schema": "cross-model-review/skill-result@1",
        "skill": "review_changes",
        "agent": "codex",
        "output": review(finding("F1")),
        "run": RUN,
    }
    payload.update(overrides)
    return payload


def task_with(*artifacts: Artifact) -> Task:
    return Task(
        id="t",
        context_id="c",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=list(artifacts),
    )


def artifact(payload, name="review_changes-result", extra_parts=()) -> Artifact:
    return Artifact(artifact_id="a", name=name, parts=[new_data_part(payload), *extra_parts])


def test_valid_artifact_parses():
    result = parse_result_artifact(task_with(artifact(result_payload())), "review_changes", "codex")
    assert result.output.findings[0].line == 2  # float from protobuf Value -> int


@pytest.mark.parametrize(
    ("task", "message"),
    [
        (task_with(), "exactly one artifact"),
        (task_with(artifact(result_payload()), artifact(result_payload())), "exactly one artifact"),
        (task_with(artifact(result_payload(), name="other")), "exactly one artifact"),
        (task_with(artifact(result_payload(), extra_parts=[new_text_part("x")])), "one data part"),
        (task_with(artifact(result_payload(skill="review_design"))), "does not match request"),
        (task_with(artifact(result_payload(agent="opus"))), "does not match 'codex'"),
        (task_with(artifact(result_payload(extra=1))), "schema"),
        (task_with(artifact(result_payload(output={"summary": "s"}))), "schema"),
        (
            task_with(artifact(result_payload(output=review(finding("F1", severity="fatal"))))),
            "schema",
        ),
        (
            task_with(artifact(result_payload(output=review(finding("F1", file="/etc/passwd"))))),
            "schema",
        ),
        (
            task_with(artifact(result_payload(output=review(finding("F1"), finding("F1"))))),
            "schema",
        ),
        (
            task_with(
                artifact(
                    result_payload(
                        output={"summary": "s", "items": [], "files_changed": []},
                    )
                )
            ),
            "schema",
        ),
    ],
)
def test_invalid_artifacts_are_rejected(task, message):
    with pytest.raises(ValueError, match=message):
        parse_result_artifact(task, "review_changes", "codex")


class RogueExecutor(AgentExecutor):
    """Completes every task with an artifact that violates the result schema."""

    async def execute(self, context, event_queue):
        from a2a.helpers import new_task_from_user_message

        await event_queue.enqueue_event(new_task_from_user_message(context.message))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.add_artifact(
            [new_data_part({"skill": "review_changes", "output": {"findings": "all good"}})],
            name="review_changes-result",
        )
        await updater.complete()

    async def cancel(self, context, event_queue):
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


@asynccontextmanager
async def rogue_codex_server():
    sock = bind_loopback("127.0.0.1", 0)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    card = build_agent_card(MockBackend("codex"), url)
    handler = DefaultRequestHandler(
        agent_executor=RogueExecutor(), task_store=InMemoryTaskStore(), agent_card=card
    )
    app = Starlette(
        routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"),
        middleware=[Middleware(BearerAuthMiddleware, token=TOKEN)],
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    serve = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield url
    finally:
        server.should_exit = True
        await serve
        await handler.aclose()


async def test_rogue_agent_artifact_fails_the_run(tmp_path, repo, lock_dir, make_config):
    async with (
        rogue_codex_server() as codex_url,
        agent_server(MockBackend("opus"), lock_dir=lock_dir, log_dir=tmp_path / "o") as (
            opus_url,
            _,
        ),
    ):
        report = await Orchestrator(
            RunOptions(repo=repo, config=make_config(codex_url, opus_url), agent_token=TOKEN)
        ).run()
    assert report.status == "aborted"
    assert report.iterations[0].tasks[0].state == "invalid_artifact"
    assert report.gate.verdict == "failed"
    assert report.apply.status == "not_needed"


async def test_wrong_token_fails_to_connect(tmp_path, repo, lock_dir, make_config):
    async with mock_agents(tmp_path, lock_dir, MockBackend("codex"), MockBackend("opus")) as (
        cu,
        ou,
    ):
        report = await Orchestrator(
            RunOptions(repo=repo, config=make_config(cu, ou), agent_token="wrong")
        ).run()
    assert report.status == "aborted"
    assert report.gate.verdict == "failed"


# ------------------------------------------------------------------ models


def test_finding_paths_and_extra_fields():
    assert Finding.model_validate(finding(file="./src//a.py")).file == "src/a.py"
    for bad in ("../x", "/abs", "a/../../x", "a\\b"):
        with pytest.raises(ValidationError):
            Finding.model_validate(finding(file=bad))
    with pytest.raises(ValidationError):
        Finding.model_validate({**finding(), "unexpected": True})


def test_skill_result_output_must_match_skill():
    with pytest.raises(ValidationError):
        SkillResult.model_validate(
            result_payload(skill="fix_findings", output=review(finding("F1")))
        )


# -------------------------------------------------------------------- gate


def tracked(severity: str) -> TrackedFinding:
    return TrackedFinding(
        key=f"k-{severity}",
        source="codex",
        iteration=1,
        finding=Finding.model_validate(finding(severity=severity)),
    )


def task(state="completed") -> TaskRecord:
    return TaskRecord(
        agent="codex", skill="review_changes", iteration=1, task_id="t", state=state, duration_s=1
    )


PASSED_TESTS = TestsSummary(status="passed", runs=[], detail="ok")


def gate(**overrides):
    args = {
        "final_findings": [tracked("medium")],
        "final_review_completed": True,
        "tests": PASSED_TESTS,
        "git_issues": [],
        "tasks": [task()],
        "run_status": "converged",
    }
    args.update(overrides)
    return evaluate_gate(**args)


def statuses(result) -> dict[str, str]:
    return {c.name: c.status for c in result.checks}


def test_gate_passes_when_everything_holds():
    result = gate()
    assert result.verdict == "passed"
    assert set(statuses(result).values()) == {"pass"}
    assert set(statuses(result)) == set(GateCheck.model_fields["name"].annotation.__args__)


@pytest.mark.parametrize(
    ("overrides", "failing"),
    [
        ({"final_findings": [tracked("critical")]}, "critical_findings"),
        ({"final_findings": [tracked("high")]}, "high_findings"),
        ({"tests": TestsSummary(status="failed", runs=[], detail="x")}, "required_tests"),
        ({"final_review_completed": False}, "final_review"),
        ({"git_issues": ["HEAD changed"]}, "git_integrity"),
        ({"tasks": [task(), task("timeout")]}, "a2a_tasks"),
        ({"run_status": "cancelled"}, "a2a_tasks"),
        ({"tasks": []}, "a2a_tasks"),
    ],
)
def test_gate_failures(overrides, failing):
    result = gate(**overrides)
    assert result.verdict == "failed"
    assert statuses(result)[failing] == "fail"


def test_gate_unverified_without_tests():
    result = gate(tests=TestsSummary(status="unverified", runs=[], detail="none"))
    assert result.verdict == "unverified"
    assert statuses(result)["required_tests"] == "unverified"


def test_gate_failure_beats_unverified():
    result = gate(
        tests=TestsSummary(status="unverified", runs=[], detail="none"),
        final_findings=[tracked("high")],
    )
    assert result.verdict == "failed"


# ----------------------------------------------------------------- schemas


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


@pytest.mark.parametrize("skill", ["review_changes", "fix_findings", "verify_changes"])
def test_cli_schemas_are_strict(skill):
    schema = cli_output_schema(skill)
    objects = list(_objects(schema))
    assert objects
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert "maxLength" not in json.dumps(schema)


def test_cli_schema_keeps_every_field_name():
    # Regression: the "title" annotation keyword must not remove a field named "title".
    finding_def = cli_output_schema("review_changes")["$defs"]["Finding"]
    assert set(finding_def["properties"]) == set(Finding.model_fields)
    assert "title" in finding_def["required"]


def test_exported_schemas_are_up_to_date():
    directory = Path(__file__).resolve().parents[1] / "schemas"
    for name, schema in protocol_schemas().items():
        on_disk = json.loads((directory / name).read_text())
        assert on_disk == schema, (
            f"{name} is stale: run `cross-model-review export-schemas schemas`"
        )
