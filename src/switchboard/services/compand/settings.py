"""Typed environment settings for the Compand Responses pilot gateway."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Mapping

from switchboard.domain.compand import GatewayMode


@dataclass(frozen=True)
class CompandGatewaySettings:
    upstream_origin: str = "https://api.openai.com"
    upstream_api_key: str = field(default="", repr=False)
    client_credentials: Mapping[str, str] = field(default_factory=dict, repr=False)
    revoked_credential_ids: frozenset[str] = frozenset()
    mode: GatewayMode = GatewayMode.PASSTHROUGH
    max_request_bytes: int = 16 * 1024 * 1024
    source_version: str = "ADAPTER-39"
    allow_http_loopback: bool = False
    frozen_tuple_config_attested: bool = False

    @classmethod
    def from_env(cls) -> "CompandGatewaySettings":
        raw_credentials = os.environ.get("COMPAND_CLIENT_CREDENTIALS_JSON") or "{}"
        try:
            decoded = json.loads(raw_credentials)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "COMPAND_CLIENT_CREDENTIALS_JSON must be valid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise ValueError("COMPAND_CLIENT_CREDENTIALS_JSON must decode to an object")
        credentials: dict[str, str] = {}
        for key, value in decoded.items():
            if not isinstance(value, str) or not value:
                raise ValueError(
                    "COMPAND_CLIENT_CREDENTIALS_JSON values must be non-empty strings"
                )
            credentials[str(key)] = value
        revoked = frozenset(
            item.strip()
            for item in (os.environ.get("COMPAND_REVOKED_CREDENTIAL_IDS") or "").split(
                ","
            )
            if item.strip()
        )
        raw_mode = (
            (os.environ.get("COMPAND_GATEWAY_MODE") or "passthrough").strip().lower()
        )
        try:
            mode = GatewayMode(raw_mode)
        except ValueError as exc:
            raise ValueError(
                "COMPAND_GATEWAY_MODE must be passthrough or scan"
            ) from exc
        try:
            max_request_bytes = int(
                os.environ.get("COMPAND_MAX_REQUEST_BYTES") or str(16 * 1024 * 1024)
            )
        except ValueError as exc:
            raise ValueError("COMPAND_MAX_REQUEST_BYTES must be an integer") from exc
        if max_request_bytes <= 0:
            raise ValueError("COMPAND_MAX_REQUEST_BYTES must be positive")
        return cls(
            upstream_origin=(
                os.environ.get("COMPAND_UPSTREAM_ORIGIN") or "https://api.openai.com"
            ).strip(),
            upstream_api_key=os.environ.get("COMPAND_UPSTREAM_OPENAI_API_KEY") or "",
            client_credentials=credentials,
            revoked_credential_ids=revoked,
            mode=mode,
            max_request_bytes=max_request_bytes,
            source_version=(
                os.environ.get("COMPAND_SOURCE_VERSION") or "ADAPTER-39"
            ).strip(),
            allow_http_loopback=(os.environ.get("COMPAND_ALLOW_HTTP_LOOPBACK") or "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            frozen_tuple_config_attested=(
                os.environ.get("COMPAND_FROZEN_TUPLE_CONFIG_ATTESTED") or ""
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
        )
