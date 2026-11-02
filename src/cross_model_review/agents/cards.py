"""Agent Card construction."""

from __future__ import annotations

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol

from cross_model_review import __version__
from cross_model_review.agents.backends import AgentBackend
from cross_model_review.models import SKILL_REQUEST_SCHEMA, SKILL_RESULT_SCHEMA, WRITE_SKILLS

SKILL_IO_EXTENSION_URI = "urn:cross-model-review:ext:skill-io:v1"
JSON_MEDIA_TYPE = "application/json"
BEARER_SCHEME = "bearer"


def build_agent_card(backend: AgentBackend, url: str) -> AgentCard:
    skills = [
        AgentSkill(
            id=spec.id,
            name=spec.name,
            description=spec.description,
            tags=list(spec.tags),
            input_modes=[JSON_MEDIA_TYPE],
            output_modes=[JSON_MEDIA_TYPE],
        )
        for spec in backend.skills
    ]
    access = {
        spec.id: ("write-enabled" if spec.id in WRITE_SKILLS else "read-only")
        for spec in backend.skills
    }
    return AgentCard(
        name=f"cross-model-review-{backend.agent_name}",
        description=backend.description,
        version=__version__,
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extensions=[
                AgentExtension(
                    uri=SKILL_IO_EXTENSION_URI,
                    description=(
                        "Requests are a single DataPart matching the skill-request schema; "
                        "results are an Artifact named '<skill>-result' with a single DataPart "
                        "matching the skill-result schema."
                    ),
                    required=False,
                    params={
                        "request_schema": SKILL_REQUEST_SCHEMA,
                        "result_schema": SKILL_RESULT_SCHEMA,
                        "access": access,
                    },
                )
            ],
        ),
        security_schemes={
            BEARER_SCHEME: SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    description="Per-run random token shared by the orchestrator.",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={BEARER_SCHEME: StringList()})],
        default_input_modes=[JSON_MEDIA_TYPE],
        default_output_modes=[JSON_MEDIA_TYPE],
        skills=skills,
    )
