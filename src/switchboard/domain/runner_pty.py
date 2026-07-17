"""Browser PTY relay frame contract and bind/capability constants (ADAPTER-22)."""
from __future__ import annotations

import base64
import json
from typing import Any, Mapping, MutableMapping, Optional

FRAME_TYPES = frozenset({
    "output",
    "input",
    "resize",
    "signal",
    "state",
    "error",
    "close",
    "replay",
    "ping",
    "pong",
    "backpressure",
})

# Browser→host control frames require matching ticket scopes.
BROWSER_TO_HOST_TYPES = frozenset({"input", "resize", "signal", "kill", "ping"})
HOST_TO_BROWSER_TYPES = frozenset({
    "output", "state", "error", "close", "replay", "pong", "backpressure",
})

# Browser tickets use watch/input/resize/signal/kill.
# Host PTY tunnel uses a distinct host_tunnel scope (BUG-74) — never interchangeable.
CAPABILITY_SCOPES = frozenset({
    "watch", "input", "resize", "signal", "kill", "host_tunnel",
})
BROWSER_CAPABILITY_SCOPES = frozenset({"watch", "input", "resize", "signal", "kill"})
HOST_TUNNEL_SCOPE = "host_tunnel"

TICKET_BIND_FIELDS = (
    "tenant_id",
    "user_id",
    "project_id",
    "task_id",
    "claim_id",
    "work_session_id",
    "runner_session_id",
    "host_id",
    "wake_id",
    "execution_connection_id",
    "source_sha",
    "permission_profile",
)

# Hub / transport defaults (overridable by RelayHub constructor / env).
DEFAULT_BROWSER_QUEUE_LIMIT = 64
DEFAULT_REPLAY_FRAME_LIMIT = 256
DEFAULT_REPLAY_BYTE_LIMIT = 65536
DEFAULT_IDLE_TTL_SECONDS = 900
DEFAULT_ABSOLUTE_TTL_SECONDS = 3600
DEFAULT_TICKET_TTL_SECONDS = 900

TRANSPORT_SWITCHBOARD_PTY_RELAY = "switchboard_pty_relay"
RELAY_PATH_TEMPLATE = "/ixp/v1/runner_sessions/{runner_session_id}/pty"
HOST_RELAY_PATH_TEMPLATE = "/ixp/v1/runner_sessions/{runner_session_id}/pty/host"


def encode_frame(frame_type: str, payload: Optional[Mapping[str, Any]] = None,
                 *, data: bytes | None = None) -> str:
    """Encode a JSON text frame. Binary bodies use base64 field ``data_b64``."""
    kind = str(frame_type or "").strip().lower()
    if kind not in FRAME_TYPES:
        raise ValueError(f"unknown_frame_type:{kind}")
    body: dict[str, Any] = {"type": kind}
    if payload:
        for key, value in payload.items():
            if key in {"type", "data_b64"}:
                continue
            body[key] = value
    if data is not None:
        body["data_b64"] = base64.b64encode(data).decode("ascii")
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def decode_frame(raw: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Decode a JSON text frame into a dict with normalized ``type`` and optional bytes."""
    if isinstance(raw, Mapping):
        frame = dict(raw)
    else:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
        try:
            frame = json.loads(text or "{}")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("malformed_frame") from exc
    if not isinstance(frame, dict):
        raise ValueError("malformed_frame")
    kind = str(frame.get("type") or "").strip().lower()
    if kind not in FRAME_TYPES:
        raise ValueError(f"unknown_frame_type:{kind}")
    out: dict[str, Any] = dict(frame)
    out["type"] = kind
    if "data_b64" in frame and frame.get("data_b64") is not None:
        try:
            out["data"] = base64.b64decode(str(frame.get("data_b64") or ""), validate=False)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("malformed_data_b64") from exc
    return out


def frame_byte_size(frame: Mapping[str, Any]) -> int:
    """Approximate serialized size for replay/backpressure accounting."""
    if "data" in frame and isinstance(frame.get("data"), (bytes, bytearray)):
        return len(frame["data"])
    if frame.get("data_b64"):
        return max(0, (len(str(frame["data_b64"])) * 3) // 4)
    try:
        return len(json.dumps(frame, separators=(",", ":"), sort_keys=True))
    except Exception:
        return 0


def redact_ticket(value: Any) -> str:
    """Never log raw tickets; return a short redacted marker."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 12:
        return "<redacted>"
    return f"{text[:4]}…{text[-4:]}(<redacted>)"


def missing_ticket_bind_fields(binding: Mapping[str, Any]) -> list[str]:
    missing = []
    for name in TICKET_BIND_FIELDS:
        if not str(binding.get(name) or "").strip():
            missing.append(name)
    return missing


def normalize_scopes(scopes: Any) -> list[str]:
    if scopes is None:
        return []
    if isinstance(scopes, str):
        items = [scopes]
    elif isinstance(scopes, (list, tuple, set, frozenset)):
        items = list(scopes)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        scope = str(item or "").strip().lower()
        if not scope or scope in seen:
            continue
        if scope not in CAPABILITY_SCOPES:
            continue
        seen.add(scope)
        out.append(scope)
    return out


def binding_subset(binding: Mapping[str, Any]) -> dict[str, str]:
    return {name: str(binding.get(name) or "").strip() for name in TICKET_BIND_FIELDS}


def merge_binding(base: Mapping[str, Any], overlay: Mapping[str, Any] | None = None) -> dict[str, str]:
    merged: MutableMapping[str, str] = binding_subset(base)
    if overlay:
        for name in TICKET_BIND_FIELDS:
            value = str(overlay.get(name) or "").strip()
            if value:
                merged[name] = value
    return dict(merged)
