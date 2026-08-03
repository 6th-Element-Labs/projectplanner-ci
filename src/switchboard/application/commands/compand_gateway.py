"""Pure request admission and passthrough planning for the Compand gateway."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Literal

from switchboard.contracts.compand import ScanObservation
from switchboard.domain.compand import ClientCredentialRegistry, GatewayMode


_ALLOWED_ROUTES = {
    ("GET", "/v1/models"): "models",
    ("POST", "/v1/responses"): "responses",
    ("POST", "/v1/responses/input_tokens"): "input_tokens",
}
_JSON_MEDIA_TYPES = {"application/json"}
_FROZEN_CODEX_USER_AGENT = (
    "codex_exec/0.144.5 (Mac OS 26.3.0; arm64) "
    "dumb (codex_exec; 0.144.5)"
)
_FROZEN_CODEX_VERSION = "0.144.5"
_FROZEN_MODEL = "gpt-5.4"
_HOP_BY_HOP = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"proxy-connection",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}


@dataclass(frozen=True)
class GatewayPolicy:
    mode: GatewayMode
    max_request_bytes: int = 16 * 1024 * 1024
    frozen_tuple_config_attested: bool = False


@dataclass(frozen=True)
class GatewayRequest:
    method: str
    path: str
    query: bytes
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes


@dataclass(frozen=True)
class GatewayPlan:
    credential_id: str
    feature: str
    mode: GatewayMode
    scan: ScanObservation | None
    client_version: str
    tuple_status: Literal["certified", "unknown"]


class GatewayRejection(Exception):
    def __init__(self, status_code: int, classification: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.classification = classification
        self.message = message


def plan_gateway_request(
    request: GatewayRequest,
    *,
    policy: GatewayPolicy,
    credentials: ClientCredentialRegistry,
) -> GatewayPlan:
    method = request.method.upper()
    feature = _ALLOWED_ROUTES.get((method, request.path))
    if feature is None:
        known_path = any(path == request.path for _, path in _ALLOWED_ROUTES)
        raise GatewayRejection(
            405 if known_path else 404,
            "unsupported_method" if known_path else "unsupported_route",
            "The pilot gateway exposes only the certified Responses endpoints.",
        )

    try:
        request.query.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GatewayRejection(
            400, "malformed_query", "The query string must be ASCII encoded."
        ) from exc

    authorization = _header_values(request.headers, b"authorization")
    if not authorization:
        raise GatewayRejection(
            401, "client_auth_failed", "A Compand bearer credential is required."
        )
    if len(authorization) != 1:
        raise GatewayRejection(
            401, "ambiguous_client_auth", "Exactly one bearer credential is required."
        )
    token = _bearer_token(authorization[0])
    if token is None:
        raise GatewayRejection(
            401, "client_auth_failed", "A valid Compand bearer credential is required."
        )
    authentication = credentials.authenticate(token)
    if not authentication.accepted:
        raise GatewayRejection(
            401,
            authentication.classification,
            "The Compand client credential is invalid or revoked.",
        )

    if _header_values(request.headers, b"proxy-authorization"):
        raise GatewayRejection(
            400, "security_policy_failed", "Proxy authorization is not accepted."
        )
    if len(request.body) > policy.max_request_bytes:
        raise GatewayRejection(
            413,
            "request_size_policy_failed",
            "The request exceeds the configured byte limit.",
        )
    _validate_content_length(request.headers, len(request.body))

    media_type = _media_type(_first_header(request.headers, b"content-type"))
    parsed: object | None = None
    duplicate_json_key = False
    content_encoding = (
        _first_header(request.headers, b"content-encoding").strip().lower()
    )
    json_is_cleartext = content_encoding in {"", "identity"}
    if request.body and _is_json_media_type(media_type) and json_is_cleartext:
        try:
            parsed, duplicate_json_key = _load_json_with_duplicate_detection(
                request.body
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayRejection(
                400, "malformed_json", "The declared JSON body is malformed."
            ) from exc

    scan_media_type = media_type if json_is_cleartext else ""
    scan = (
        _scan_observation(parsed, scan_media_type)
        if policy.mode is GatewayMode.SCAN
        else None
    )
    client_version, tuple_status = _classify_frozen_tuple(
        request,
        feature=feature,
        parsed=parsed,
        media_type=scan_media_type,
        config_attested=policy.frozen_tuple_config_attested,
        duplicate_json_key=duplicate_json_key,
    )
    return GatewayPlan(
        credential_id=authentication.credential_id or "",
        feature=feature,
        mode=policy.mode,
        scan=scan,
        client_version=client_version,
        tuple_status=tuple_status,
    )


def upstream_request_headers(
    headers: Iterable[tuple[bytes, bytes]], upstream_api_key: str
) -> list[tuple[bytes, bytes]]:
    materialized = tuple(headers)
    hop_by_hop = _hop_by_hop_names(materialized)
    forwarded = []
    for name, value in materialized:
        lower = name.lower()
        if lower in hop_by_hop or lower in {b"authorization", b"host"}:
            continue
        if lower.startswith(b"x-compand-"):
            continue
        forwarded.append((name, value))
    forwarded.append((b"authorization", f"Bearer {upstream_api_key}".encode("utf-8")))
    return forwarded


def downstream_response_headers(
    headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    materialized = tuple(headers)
    hop_by_hop = _hop_by_hop_names(materialized)
    return [
        (name, value) for name, value in materialized if name.lower() not in hop_by_hop
    ]


def _header_values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in headers if key.lower() == name]


def _first_header(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str:
    values = _header_values(headers, name)
    return values[0] if values else ""


def _bearer_token(value: str) -> str | None:
    scheme, separator, token = value.partition(" ")
    if separator and scheme.lower() == "bearer" and token and token == token.strip():
        return token
    return None


def _validate_content_length(
    headers: Iterable[tuple[bytes, bytes]], body_bytes: int
) -> None:
    values = _header_values(headers, b"content-length")
    if len(values) > 1:
        raise GatewayRejection(
            400,
            "ambiguous_content_length",
            "Multiple Content-Length values are not accepted.",
        )
    if not values:
        return
    try:
        declared = int(values[0])
    except ValueError as exc:
        raise GatewayRejection(
            400, "malformed_content_length", "Content-Length must be an integer."
        ) from exc
    if declared < 0 or declared != body_bytes:
        raise GatewayRejection(
            400,
            "content_length_mismatch",
            "Content-Length does not match the request body.",
        )


def _media_type(value: str) -> str:
    return value.partition(";")[0].strip().lower()


def _is_json_media_type(value: str) -> bool:
    return value in _JSON_MEDIA_TYPES or value.endswith("+json")


def _classify_frozen_tuple(
    request: GatewayRequest,
    *,
    feature: str,
    parsed: object | None,
    media_type: str,
    config_attested: bool,
    duplicate_json_key: bool,
) -> tuple[str, Literal["certified", "unknown"]]:
    user_agents = _header_values(request.headers, b"user-agent")
    user_agent = user_agents[0] if len(user_agents) == 1 else ""
    client_version = _codex_version(user_agent)
    if config_attested is not True or user_agent != _FROZEN_CODEX_USER_AGENT:
        return client_version, "unknown"

    # Duplicate keys make the request-observable tuple ambiguous. Keep the original
    # bytes on the passthrough path, but never turn last-key-wins parsing into evidence.
    if duplicate_json_key:
        return client_version, "unknown"

    if feature == "models":
        matches = (
            request.query == f"client_version={_FROZEN_CODEX_VERSION}".encode("ascii")
            and not request.body
        )
        return client_version, "certified" if matches else "unknown"

    if (
        request.query
        or media_type != "application/json"
        or _header_values(request.headers, b"content-type") != ["application/json"]
    ):
        return client_version, "unknown"
    if not isinstance(parsed, dict) or parsed.get("model") != _FROZEN_MODEL:
        return client_version, "unknown"
    if feature == "responses":
        reasoning = parsed.get("reasoning")
        matches = (
            parsed.get("store") is False
            and parsed.get("stream") is True
            and isinstance(reasoning, dict)
            and reasoning.get("effort") == "high"
            and _header_values(request.headers, b"accept") == ["text/event-stream"]
        )
        return client_version, "certified" if matches else "unknown"
    if feature == "input_tokens":
        matches = _header_values(request.headers, b"accept") == ["application/json"]
        return client_version, "certified" if matches else "unknown"
    return client_version, "unknown"


def _codex_version(user_agent: str) -> str:
    prefix = "codex_exec/"
    if not user_agent.startswith(prefix):
        return "unknown"
    version = user_agent[len(prefix) :].split(" ", 1)[0].strip()
    return version or "unknown"


def _reject_json_constant(value: str) -> object:
    raise json.JSONDecodeError(f"non-standard JSON constant: {value}", value, 0)


def _load_json_with_duplicate_detection(body: bytes) -> tuple[object, bool]:
    duplicate_key = False

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate_key
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                duplicate_key = True
            result[key] = value
        return result

    parsed = json.loads(
        body,
        object_pairs_hook=object_from_pairs,
        parse_constant=_reject_json_constant,
    )
    return parsed, duplicate_key


def _hop_by_hop_names(headers: Iterable[tuple[bytes, bytes]]) -> set[bytes]:
    names = set(_HOP_BY_HOP)
    for key, value in headers:
        if key.lower() == b"connection":
            names.update(
                item.strip().lower() for item in value.split(b",") if item.strip()
            )
    return names


def _scan_observation(parsed: object | None, media_type: str) -> ScanObservation:
    if not _is_json_media_type(media_type):
        return ScanObservation(json_kind="not_json", continuation_kind="unknown")
    if isinstance(parsed, dict):
        raw_input = parsed.get("input")
        raw_tools = parsed.get("tools")
        if "previous_response_id" in parsed:
            continuation = "previous_response_id"
        elif "conversation" in parsed:
            continuation = "conversation"
        elif isinstance(raw_input, list) and raw_input:
            continuation = "manual_history"
        else:
            continuation = "none"
        return ScanObservation(
            json_kind="object",
            input_item_count=len(raw_input) if isinstance(raw_input, list) else None,
            tool_count=len(raw_tools) if isinstance(raw_tools, list) else None,
            stream_requested=parsed.get("stream")
            if isinstance(parsed.get("stream"), bool)
            else None,
            continuation_kind=continuation,
        )
    if isinstance(parsed, list):
        return ScanObservation(json_kind="array", continuation_kind="unknown")
    return ScanObservation(json_kind="scalar", continuation_kind="unknown")
