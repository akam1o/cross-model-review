"""JSON Schemas derived from the Pydantic models.

Two flavours exist:

* *Protocol* schemas (request, result, report) are the full Pydantic schemas
  and document the A2A DataPart payloads.
* *CLI output* schemas are passed to ``codex exec --output-schema`` and
  ``claude --json-schema``. Structured-output engines accept a restricted
  dialect (every property required, ``additionalProperties: false``, no
  length/pattern keywords), so those keywords are stripped here; the full
  constraints are still enforced by Pydantic when the output is parsed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cross_model_review.models import (
    OUTPUT_MODEL_BY_SKILL,
    FixOutput,
    Report,
    ReviewOutput,
    SkillRequest,
    SkillResult,
    VerifyOutput,
)

_DROP_KEYS = {
    "title",
    "default",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
}


def _strictify(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _strictify(v) for k, v in node.items() if k not in _DROP_KEYS}
        # Keys under "properties" / "$defs" are names, not keywords: keep them all.
        for container in ("properties", "$defs"):
            if isinstance(node.get(container), dict):
                out[container] = {k: _strictify(v) for k, v in node[container].items()}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    return node


def cli_output_schema(skill: str) -> dict[str, Any]:
    model = OUTPUT_MODEL_BY_SKILL[skill]
    return _strictify(model.model_json_schema())


def protocol_schemas() -> dict[str, dict[str, Any]]:
    return {
        "skill-request.schema.json": SkillRequest.model_json_schema(by_alias=True),
        "skill-result.schema.json": SkillResult.model_json_schema(by_alias=True),
        "report.schema.json": Report.model_json_schema(by_alias=True),
        "cli-review-output.schema.json": _strictify(ReviewOutput.model_json_schema()),
        "cli-fix-output.schema.json": _strictify(FixOutput.model_json_schema()),
        "cli-verify-output.schema.json": _strictify(VerifyOutput.model_json_schema()),
    }


def export_schemas(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, schema in protocol_schemas().items():
        path = directory / name
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written
