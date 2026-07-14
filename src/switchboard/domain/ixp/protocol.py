"""IXP protocol envelope, version negotiation, and field aliases (ARCH-MS-43).

Canonical home for the connect-time protocol contract previously inlined in
``store.py``. REST/MCP adapters and the coordination repository import from
here so version checks and ``field_aliases`` stay in one place.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping, MutableMapping, Optional

PROTOCOL_ENVELOPE: dict[str, Any] = {
    "name": "switchboard",
    "version": "ixp.v1",
    "profile": "p0-dogfood",
    "profile_version": "2026-06-28",
    "profiles": {
        "ixp_core": "1.0",
        "txp_dispatch": "0.1",
        "oxp_tally": "0.1",
        "reconcile": "0.1",
    },
    "compatible_versions": ["ixp.v1"],
    "field_aliases": {
        "send_agent_message.ack_timeout_seconds": "ack_deadline_minutes",
        "send_agent_message.ack_timeout_s": "ack_deadline_minutes",
    },
}

# Seconds-based aliases for send_agent_message that map onto minutes.
_SEND_ACK_SECONDS_ALIASES: frozenset[str] = frozenset({
    "ack_timeout_seconds",
    "ack_timeout_s",
})


def protocol_envelope() -> dict[str, Any]:
    """Return a deep copy of the server protocol envelope."""
    return copy.deepcopy(PROTOCOL_ENVELOPE)


def render_protocol_envelope_json(envelope: Mapping[str, Any] | None = None) -> str:
    """Deterministic JSON serialization for golden fixtures / drift checks."""
    payload = envelope if envelope is not None else PROTOCOL_ENVELOPE
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _advertised_version(advertised: Mapping[str, Any]) -> Any:
    return advertised.get("version") or advertised.get("ixp_version")


def _client_versions(advertised: Mapping[str, Any]) -> list[str]:
    """Versions the client claims to speak (explicit list, else single version)."""
    raw = advertised.get("compatible_versions")
    if isinstance(raw, (list, tuple)):
        versions = [str(v).strip() for v in raw if str(v or "").strip()]
        if versions:
            return versions
    version = _advertised_version(advertised)
    if version:
        return [str(version).strip()]
    return []


def check_protocol_compatibility(
    advertised: Optional[Mapping[str, Any]],
    *,
    server: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Exact allowlist check used by ``register_agent``.

    Missing advertisements stay ``legacy_assumed`` (pre-PROTO-2 agents). A
    declared version outside the server allowlist is rejected.
    """
    server_env = server or PROTOCOL_ENVELOPE
    supported = list(server_env.get("compatible_versions") or [])
    if not advertised:
        return {
            "compatible": True,
            "mode": "legacy_assumed",
            "warnings": ["agent did not advertise protocol; treating as pre-PROTO-2"],
        }
    version = _advertised_version(advertised)
    if version not in supported:
        return {
            "compatible": False,
            "mode": "reject",
            "reason": f"unsupported protocol version {version!r}; supported={supported}",
        }
    return {
        "compatible": True,
        "mode": "exact",
        "version": version,
        "profile": advertised.get("profile"),
    }


def negotiate_protocol(
    client: Optional[Mapping[str, Any]],
    server: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Intersect client/server ``compatible_versions`` and pick a wire version.

    Preference order:
    1. Client's advertised ``version`` / ``ixp_version`` when it is in the
       intersection (exact match).
    2. First server-preferred version that appears in the intersection.
    3. Reject when the intersection is empty (or client omitted versions).
    """
    server_env = dict(server or PROTOCOL_ENVELOPE)
    server_versions = [str(v) for v in (server_env.get("compatible_versions") or [])]

    if not client:
        return {
            "compatible": True,
            "mode": "legacy_assumed",
            "version": server_env.get("version"),
            "negotiated_version": server_env.get("version"),
            "intersection": list(server_versions),
            "warnings": ["agent did not advertise protocol; treating as pre-PROTO-2"],
        }

    client_versions = _client_versions(client)
    if not client_versions:
        return {
            "compatible": False,
            "mode": "reject",
            "reason": "client advertised protocol without a version",
            "intersection": [],
        }

    intersection = [v for v in server_versions if v in set(client_versions)]
    if not intersection:
        return {
            "compatible": False,
            "mode": "reject",
            "reason": (
                f"no overlapping protocol versions; "
                f"client={client_versions} server={server_versions}"
            ),
            "intersection": [],
            "client_versions": client_versions,
            "server_versions": server_versions,
        }

    preferred = _advertised_version(client)
    if preferred and str(preferred) in intersection:
        negotiated = str(preferred)
        mode = "exact"
    else:
        negotiated = intersection[0]
        mode = "negotiated"

    return {
        "compatible": True,
        "mode": mode,
        "version": negotiated,
        "negotiated_version": negotiated,
        "intersection": intersection,
        "profile": client.get("profile") or server_env.get("profile"),
    }


def field_aliases_for(
    operation: str,
    *,
    envelope: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return ``{alias_field: canonical_field}`` for one operation name."""
    env = envelope or PROTOCOL_ENVELOPE
    prefix = f"{operation}."
    out: dict[str, str] = {}
    for key, canonical in (env.get("field_aliases") or {}).items():
        if not str(key).startswith(prefix):
            continue
        alias_field = str(key)[len(prefix):]
        if alias_field:
            out[alias_field] = str(canonical)
    return out


def apply_field_aliases(
    operation: str,
    body: Mapping[str, Any] | None,
    *,
    envelope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy ``body`` with operation field aliases resolved to canonical names.

    Seconds-based send_agent_message aliases (``ack_timeout_s`` /
    ``ack_timeout_seconds``) convert into ``ack_deadline_minutes`` when the
    canonical minutes field is unset.
    """
    result: dict[str, Any] = dict(body or {})
    aliases = field_aliases_for(operation, envelope=envelope)
    if not aliases:
        return result

    for alias_field, canonical in aliases.items():
        if alias_field not in result:
            continue
        alias_value = result.get(alias_field)
        if operation == "send_agent_message" and alias_field in _SEND_ACK_SECONDS_ALIASES:
            if result.get(canonical) in (None, "", 0, "0"):
                if alias_value not in (None, "", 0, "0"):
                    result[canonical] = float(alias_value) / 60.0
            result.pop(alias_field, None)
            continue
        if result.get(canonical) in (None, ""):
            result[canonical] = alias_value
        result.pop(alias_field, None)
    return result


def normalize_send_ack_deadline(
    *,
    ack_deadline_minutes: Any = None,
    ack_timeout_seconds: Any = None,
    ack_timeout_s: Any = None,
) -> float | None:
    """Resolve send_agent_message ack deadline aliases to minutes."""
    normalized = apply_field_aliases(
        "send_agent_message",
        {
            "ack_deadline_minutes": ack_deadline_minutes,
            "ack_timeout_seconds": ack_timeout_seconds,
            "ack_timeout_s": ack_timeout_s,
        },
    )
    value = normalized.get("ack_deadline_minutes")
    if value in (None, "", 0, "0"):
        return None
    return float(value)
