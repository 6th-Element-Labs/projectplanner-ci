"""OpenAPI 3.1 document built from versioned Pydantic contracts (ARCH-MS-41).

The checked-in artifact at ``openapi/switchboard.openapi.json`` is generated
from this module. REST adapters stay thin: schemas come from
``switchboard.contracts``, not hand-written OpenAPI fragments.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from . import claims, tasks  # noqa: F401 — register command schemas via package
from .claims import (
    CLAIM_NEXT_COMMAND_SCHEMA,
    CLAIM_TASK_COMMAND_SCHEMA,
    COMPLETE_CLAIM_COMMAND_SCHEMA,
)
from .tasks import (
    CREATE_TASK_COMMAND_SCHEMA,
    GET_TASK_QUERY_SCHEMA,
    MOVE_TASK_COMMAND_SCHEMA,
    UPDATE_TASK_COMMAND_SCHEMA,
)
from .registry import list_schemas

OPENAPI_VERSION = "3.1.0"
API_TITLE = "Switchboard REST"
API_VERSION = "v1"
API_DESCRIPTION = (
    "Contract-first REST surface for Switchboard. Request bodies for the "
    "paths below are generated from ``src/switchboard/contracts`` Pydantic "
    "models shared with MCP application commands."
)

# Contract-backed REST operations (task + claim surfaces from ARCH-MS-25/36/40).
# Paths match the live routers in ``api/routers/{tasks,claims}.py``.
_CONTRACT_OPERATIONS: tuple[dict[str, Any], ...] = (
    {
        "path": "/api/tasks",
        "method": "post",
        "operation_id": "createTask",
        "summary": "Create a task",
        "schema_id": CREATE_TASK_COMMAND_SCHEMA,
        "query": (
            {"name": "project", "required": True, "schema": {"type": "string"}},
        ),
    },
    {
        "path": "/api/tasks/{task_id}",
        "method": "get",
        "operation_id": "getTask",
        "summary": "Get one task",
        "schema_id": GET_TASK_QUERY_SCHEMA,
        "path_params": (
            {"name": "task_id", "required": True, "schema": {"type": "string"}},
        ),
        "query": (
            {"name": "project", "required": False, "schema": {"type": "string"}},
        ),
        "body": False,
    },
    {
        "path": "/api/tasks/{task_id}",
        "method": "patch",
        "operation_id": "updateTask",
        "summary": "Update a task",
        "schema_id": UPDATE_TASK_COMMAND_SCHEMA,
        "path_params": (
            {"name": "task_id", "required": True, "schema": {"type": "string"}},
        ),
        "query": (
            {"name": "project", "required": True, "schema": {"type": "string"}},
        ),
    },
    {
        "path": "/api/tasks/{task_id}/move",
        "method": "post",
        "operation_id": "moveTask",
        "summary": "Move a task between project boards",
        "schema_id": MOVE_TASK_COMMAND_SCHEMA,
        "path_params": (
            {"name": "task_id", "required": True, "schema": {"type": "string"}},
        ),
        "query": (
            {"name": "project", "required": True, "schema": {"type": "string"}},
        ),
    },
    {
        "path": "/txp/v1/claim_task",
        "method": "post",
        "operation_id": "claimTask",
        "summary": "Claim one exact ready task",
        "schema_id": CLAIM_TASK_COMMAND_SCHEMA,
    },
    {
        "path": "/txp/v1/claim_next",
        "method": "post",
        "operation_id": "claimNext",
        "summary": "Claim the next ready task",
        "schema_id": CLAIM_NEXT_COMMAND_SCHEMA,
    },
    {
        "path": "/txp/v1/complete_claim",
        "method": "post",
        "operation_id": "completeClaim",
        "summary": "Complete a claim and move work to In Review",
        "schema_id": COMPLETE_CLAIM_COMMAND_SCHEMA,
    },
)

# Schemas that back the contract-first REST surface in this document.
_REST_SCHEMA_IDS: frozenset[str] = frozenset(
    op["schema_id"] for op in _CONTRACT_OPERATIONS
)


def component_name(schema_id: str) -> str:
    """Map ``switchboard.task.create_command.v1`` → ``TaskCreateCommandV1``.

    Collapses a repeated leading domain segment so
    ``switchboard.claim.claim_task_command.v1`` becomes ``ClaimTaskCommandV1``
    rather than ``ClaimClaimTaskCommandV1``.
    """
    parts = [p for p in schema_id.split(".") if p and p != "switchboard"]
    if len(parts) >= 2 and parts[1].startswith(parts[0] + "_"):
        parts = parts[1:]
    out: list[str] = []
    for part in parts:
        if re.fullmatch(r"v\d+", part):
            out.append(part.upper())
            continue
        out.extend(piece.capitalize() for piece in part.split("_") if piece)
    return "".join(out) or "Schema"


def _normalize_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON Schema fragment suitable for OpenAPI 3.1 components."""
    schema = copy.deepcopy(payload)
    # OpenAPI 3.1 accepts JSON Schema 2020-12; drop the registry $id so the
    # component is addressed by its OpenAPI name instead.
    schema.pop("$id", None)
    schema.pop("$schema", None)
    return schema


def _parameter(name: str, *, location: str, required: bool,
               schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "in": location,
        "required": required,
        "schema": schema,
    }


def _operation_object(op: dict[str, Any]) -> dict[str, Any]:
    ref = f"#/components/schemas/{component_name(op['schema_id'])}"
    parameters: list[dict[str, Any]] = []
    for param in op.get("path_params") or ():
        parameters.append(_parameter(
            param["name"], location="path", required=True, schema=param["schema"]))
    for param in op.get("query") or ():
        parameters.append(_parameter(
            param["name"],
            location="query",
            required=bool(param.get("required")),
            schema=param["schema"],
        ))

    method = str(op.get("method") or "").lower()
    mutating = method in {"post", "put", "patch", "delete"}
    if mutating and op.get("idempotent", True):
        parameters.append({
            "name": "Idempotency-Key",
            "in": "header",
            "required": False,
            "description": (
                "Optional retry key. Same key + same body replays the original "
                "response; same key + different body returns 409 idem_key_conflict."
            ),
            "schema": {"type": "string", "minLength": 1},
        })

    responses: dict[str, Any] = {
        "200": {
            "description": "Success",
            "content": {
                "application/json": {
                    "schema": {"type": "object"},
                }
            },
        },
        "400": {
            "description": "Invalid request payload",
            "content": {
                "application/json": {
                    "schema": {"type": "object"},
                }
            },
        },
    }
    if mutating and op.get("idempotent", True):
        responses["409"] = {
            "description": "Idempotency-Key conflict (idem_key_conflict)",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "error": {"type": "string", "const": "idem_key_conflict"},
                            "error_code": {
                                "type": "string",
                                "const": "idem_key_conflict",
                            },
                            "message": {"type": "string"},
                            "idem_key": {"type": "string"},
                            "operation": {"type": "string"},
                        },
                    }
                }
            },
        }

    operation: dict[str, Any] = {
        "operationId": op["operation_id"],
        "summary": op["summary"],
        "tags": ["contracts"],
        "responses": responses,
    }
    if parameters:
        operation["parameters"] = parameters
    if op.get("body", True):
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": ref},
                }
            },
        }
    return operation


def build_openapi_document() -> dict[str, Any]:
    """Build a deterministic OpenAPI 3.1 document from registered contracts."""
    registered = list_schemas()
    missing = sorted(_REST_SCHEMA_IDS - set(registered))
    if missing:
        raise RuntimeError(
            "OpenAPI generation requires registered contracts; missing: "
            + ", ".join(missing)
        )

    components: dict[str, Any] = {}
    for schema_id in sorted(_REST_SCHEMA_IDS):
        components[component_name(schema_id)] = _normalize_schema(registered[schema_id])

    paths: dict[str, Any] = {}
    for op in _CONTRACT_OPERATIONS:
        path_item = paths.setdefault(op["path"], {})
        path_item[op["method"]] = _operation_object(op)

    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": API_TITLE,
            "version": API_VERSION,
            "description": API_DESCRIPTION,
        },
        "paths": paths,
        "components": {"schemas": components},
    }


def render_openapi_json(document: dict[str, Any] | None = None) -> str:
    """Serialize the OpenAPI document with stable key ordering."""
    doc = document if document is not None else build_openapi_document()
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def required_operation_ids() -> tuple[str, ...]:
    return tuple(op["operation_id"] for op in _CONTRACT_OPERATIONS)


def required_paths() -> tuple[str, ...]:
    return tuple(sorted({op["path"] for op in _CONTRACT_OPERATIONS}))
