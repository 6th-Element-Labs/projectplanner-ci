"""OpenAPI 3.1 document for the service skeleton (ARCH-MS-73).

Generated from this package's Pydantic contracts — same pattern as
``switchboard.contracts.openapi``, scoped to the cut-out process.
"""
from __future__ import annotations

from typing import Any

from .v1 import ExamplePingResponse

OPENAPI_VERSION = "3.1.0"
API_TITLE = "Switchboard service skeleton"
API_VERSION = "v1"
API_DESCRIPTION = (
    "Contract-first OpenAPI surface for a cut-out Switchboard service. "
    "Clone ``switchboard.services._skeleton`` when extracting Auth/Tasks; "
    "do not mount this document into the live monolith until cutover."
)


def build_openapi_document(*, service_name: str = "switchboard-skeleton") -> dict[str, Any]:
    """Return an OpenAPI 3.1 dict for the skeleton health + example routes."""
    ping_schema = ExamplePingResponse.model_json_schema(ref_template="#/components/schemas/{model}")
    defs = ping_schema.pop("$defs", {})
    # Inline the top-level model under a stable component name.
    components = {**defs, "ExamplePingResponse": ping_schema}
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": API_TITLE,
            "version": API_VERSION,
            "description": API_DESCRIPTION,
            "x-switchboard-service": service_name,
        },
        "paths": {
            "/health": {
                "get": {
                    "operationId": "health",
                    "summary": "Liveness probe",
                    "responses": {
                        "200": {
                            "description": "Service is alive",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "status": {"type": "string"},
                                            "service": {"type": "string"},
                                        },
                                        "required": ["status", "service"],
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/example/ping": {
                "get": {
                    "operationId": "examplePing",
                    "summary": "Skeleton domain ping",
                    "tags": ["example"],
                    "responses": {
                        "200": {
                            "description": "Ping ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ExamplePingResponse"
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
        "components": {"schemas": components},
    }
