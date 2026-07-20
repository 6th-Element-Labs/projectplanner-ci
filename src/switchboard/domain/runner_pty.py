"""Browser PTY relay frame contract and bind/capability constants (SIMPLIFY-9).

Binary wire format (one session transport):
  magic ``b"SB1\\0"`` + uint8 type_id + uint16 BE header_len + uint32 BE data_len
  + JSON header UTF-8 + raw data bytes.

Message kinds: ready, exit, out, in, resize, signal, snapshot.
Legacy JSON+base64 names are accepted on decode only for migration helpers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from typing import Any, Mapping, MutableMapping, Optional

FRAME_TYPES = frozenset({
    "ready",
    "exit",
    "out",
    "in",
    "resize",
    "signal",
    "snapshot",
})

# Legacy names accepted on decode only (never produced by encode_frame).
_LEGACY_TYPE_MAP = {
    "output": "out",
    "input": "in",
    "close": "exit",
    "error": "exit",
    "replay": "snapshot",
    "state": "ready",
    # Dropped first-class kinds — map to nearest survivor when present.
    "ping": "ready",
    "pong": "ready",
    "backpressure": "ready",
    "control_ack": "ready",
}

TYPE_IDS = {
    "ready": 1,
    "exit": 2,
    "out": 3,
    "in": 4,
    "resize": 5,
    "signal": 6,
    "snapshot": 7,
}
_ID_TO_TYPE = {v: k for k, v in TYPE_IDS.items()}

FRAME_MAGIC = b"SB1\0"
_HEADER_STRUCT = struct.Struct(">BHI")  # type_id, header_len, data_len
MAX_HEADER_BYTES = 0xFFFF
MAX_DATA_BYTES = 16 * 1024 * 1024

# Browser→host control frames require matching ticket scopes.
BROWSER_TO_HOST_TYPES = frozenset({"in", "resize", "signal"})
HOST_TO_BROWSER_TYPES = frozenset({"out", "ready", "exit", "snapshot"})

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
DEFAULT_WRITE_COALESCE_MS = 12  # 8–16ms coalescing window for host→hub out frames

TRANSPORT_SWITCHBOARD_PTY_RELAY = "switchboard_pty_relay"
RELAY_PATH_TEMPLATE = "/ixp/v1/runner_sessions/{runner_session_id}/pty"
HOST_RELAY_PATH_TEMPLATE = "/ixp/v1/runner_sessions/{runner_session_id}/pty/host"


def planned_runner_session_id(wake_id: Any, host_id: Any) -> str:
    """Return the one deterministic execution id reserved by Start and the host."""
    wake = str(wake_id or "").strip()
    host = str(host_id or "").strip()
    if not wake or not host:
        return ""
    return "run_" + hashlib.sha256(f"{wake}:{host}".encode("utf-8")).hexdigest()[:16]


def _normalize_type(frame_type: str, *, for_encode: bool) -> str:
    kind = str(frame_type or "").strip().lower()
    if kind in FRAME_TYPES:
        return kind
    mapped = _LEGACY_TYPE_MAP.get(kind)
    if mapped and not for_encode:
        return mapped
    raise ValueError(f"unknown_frame_type:{kind}")


def encode_frame(frame_type: str, payload: Optional[Mapping[str, Any]] = None,
                 *, data: bytes | None = None) -> bytes:
    """Encode a binary frame. Production path never emits JSON text or data_b64."""
    kind = _normalize_type(frame_type, for_encode=True)
    header: dict[str, Any] = {}
    if payload:
        for key, value in payload.items():
            if key in {"type", "data", "data_b64"}:
                continue
            # Keep JSON-serializable header fields only.
            if isinstance(value, (bytes, bytearray)):
                continue
            header[key] = value
    header_bytes = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = b"" if data is None else bytes(data)
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise ValueError("frame_header_too_large")
    if len(body) > MAX_DATA_BYTES:
        raise ValueError("frame_data_too_large")
    type_id = TYPE_IDS[kind]
    wire = FRAME_MAGIC + _HEADER_STRUCT.pack(type_id, len(header_bytes), len(body))
    return wire + header_bytes + body


def decode_frame(raw: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Decode a binary (or legacy JSON) frame into a dict with ``type`` + optional ``data``."""
    if isinstance(raw, Mapping):
        return _decode_mapping(dict(raw))

    if isinstance(raw, (bytes, bytearray)):
        blob = bytes(raw)
        if blob.startswith(FRAME_MAGIC):
            return _decode_binary(blob)
        # Legacy JSON text delivered as bytes.
        try:
            text = blob.decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("malformed_frame") from exc
        return _decode_json_text(text)

    return _decode_json_text(str(raw or ""))


def _decode_binary(blob: bytes) -> dict[str, Any]:
    if len(blob) < 4 + _HEADER_STRUCT.size:
        raise ValueError("malformed_frame")
    type_id, header_len, data_len = _HEADER_STRUCT.unpack_from(blob, 4)
    start = 4 + _HEADER_STRUCT.size
    end_header = start + header_len
    end_data = end_header + data_len
    if (end_data != len(blob) or header_len > MAX_HEADER_BYTES
            or data_len > MAX_DATA_BYTES):
        raise ValueError("malformed_frame")
    kind = _ID_TO_TYPE.get(int(type_id))
    if kind is None:
        raise ValueError(f"unknown_frame_type_id:{type_id}")
    header_raw = blob[start:end_header]
    try:
        header = json.loads(header_raw.decode("utf-8") or "{}") if header_raw else {}
    except Exception as exc:  # noqa: BLE001
        raise ValueError("malformed_frame") from exc
    if not isinstance(header, dict):
        raise ValueError("malformed_frame")
    out: dict[str, Any] = dict(header)
    out["type"] = kind
    if data_len:
        out["data"] = blob[end_header:end_data]
    return out


def _decode_json_text(text: str) -> dict[str, Any]:
    try:
        frame = json.loads(text or "{}")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("malformed_frame") from exc
    if not isinstance(frame, dict):
        raise ValueError("malformed_frame")
    return _decode_mapping(frame)


def _decode_mapping(frame: dict[str, Any]) -> dict[str, Any]:
    kind = _normalize_type(str(frame.get("type") or ""), for_encode=False)
    out: dict[str, Any] = dict(frame)
    out["type"] = kind
    if isinstance(frame.get("data"), (bytes, bytearray)):
        out["data"] = bytes(frame["data"])
    elif "data_b64" in frame and frame.get("data_b64") is not None:
        try:
            out["data"] = base64.b64decode(str(frame.get("data_b64") or ""), validate=False)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("malformed_data_b64") from exc
    return out


def frame_byte_size(frame: Mapping[str, Any]) -> int:
    """Approximate payload size for replay/backpressure accounting."""
    if "data" in frame and isinstance(frame.get("data"), (bytes, bytearray)):
        return len(frame["data"])
    if frame.get("data_b64"):
        return max(0, (len(str(frame["data_b64"])) * 3) // 4)
    try:
        encoded = encode_frame(
            str(frame.get("type") or "out"),
            {k: v for k, v in frame.items() if k not in {"type", "data", "data_b64"}},
            data=frame.get("data") if isinstance(frame.get("data"), (bytes, bytearray)) else None,
        )
        return len(encoded)
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
