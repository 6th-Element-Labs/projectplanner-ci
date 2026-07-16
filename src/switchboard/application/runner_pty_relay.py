"""Authenticated Switchboard-controlled full-duplex browser PTY relay (ADAPTER-22)."""
from __future__ import annotations

import os
import threading
import time
import uuid
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

from switchboard.api.routers.auth.jwt_util import decode as jwt_decode
from switchboard.api.routers.auth.jwt_util import encode as jwt_encode
from switchboard.domain import runner_pty as domain

SendFn = Callable[[str], None]

_REVOKED_JTIS: set[str] = set()
_REVOKE_LOCK = threading.Lock()


def relay_secret() -> str:
    return str(
        os.environ.get("PM_RUNNER_PTY_RELAY_SECRET")
        or os.environ.get("PM_RUNNER_STREAM_SECRET")
        or os.environ.get("PM_MCP_TOKEN")
        or "switchboard-runner-stream-dev"
    )


def public_base_from_env() -> str:
    return str(
        os.environ.get("PM_RUNNER_PTY_RELAY_PUBLIC_BASE")
        or os.environ.get("PM_SWITCHBOARD_PUBLIC_BASE")
        or ""
    ).rstrip("/")


def revoke_ticket_jti(jti: str) -> bool:
    token = str(jti or "").strip()
    if not token:
        return False
    with _REVOKE_LOCK:
        _REVOKED_JTIS.add(token)
    return True


def is_jti_revoked(jti: str) -> bool:
    with _REVOKE_LOCK:
        return str(jti or "") in _REVOKED_JTIS


def clear_revoked_jtis_for_tests() -> None:
    with _REVOKE_LOCK:
        _REVOKED_JTIS.clear()


def mint_capability_ticket(
    binding: Mapping[str, Any],
    scopes: Any,
    ttl_seconds: int = domain.DEFAULT_TICKET_TTL_SECONDS,
    *,
    now: float | None = None,
    secret: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Mint a short-lived least-privilege PTY relay ticket."""
    issued = float(now if now is not None else time.time())
    ttl = max(30, int(ttl_seconds))
    expires = issued + ttl
    normalized_scopes = domain.normalize_scopes(scopes)
    if not normalized_scopes:
        raise ValueError("scopes_required")
    bind = domain.merge_binding(binding)
    missing = domain.missing_ticket_bind_fields(bind)
    if missing:
        raise ValueError(f"incomplete_bind:{','.join(missing)}")
    jti = uuid.uuid4().hex
    payload = {
        **bind,
        "scopes": normalized_scopes,
        "scope": "runner_pty_relay",
        "jti": jti,
        "iat": int(issued),
        "exp": int(expires),
    }
    token = jwt_encode(payload, secret or relay_secret())
    return token, payload


def verify_capability_ticket(
    ticket: str,
    *,
    required_scope: str = "",
    expected_binding_subset: Mapping[str, Any] | None = None,
    now: float | None = None,
    secret: str | None = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """Verify ticket; fail closed on wrong/stale/revoked/cross-* mismatch."""
    payload, reason = jwt_decode(ticket or "", secret or relay_secret(), now=now)
    if payload is None:
        return None, reason or "invalid_ticket"
    if str(payload.get("scope") or "") != "runner_pty_relay":
        return None, "wrong_scope"
    jti = str(payload.get("jti") or "")
    if not jti:
        return None, "missing_jti"
    if is_jti_revoked(jti):
        return None, "revoked"
    scopes = domain.normalize_scopes(payload.get("scopes"))
    if not scopes:
        return None, "scopes_missing"
    need = str(required_scope or "").strip().lower()
    if need and need not in scopes:
        return None, "missing_scope"
    bind = domain.binding_subset(payload)
    missing = domain.missing_ticket_bind_fields(bind)
    if missing:
        return None, f"incomplete_bind:{','.join(missing)}"
    if expected_binding_subset:
        for key, expected in expected_binding_subset.items():
            want = str(expected or "").strip()
            if not want:
                continue
            have = str(bind.get(key) or payload.get(key) or "").strip()
            if have != want:
                return None, f"{key}_mismatch"
    out = dict(payload)
    out["scopes"] = scopes
    out.update(bind)
    return out, ""


def revoke_capability_ticket(ticket: str, *, secret: str | None = None) -> tuple[bool, str]:
    # Allow revoking expired tickets: verify signature with a far-future clock.
    payload, reason = jwt_decode(ticket or "", secret or relay_secret(), now=10**12)
    if payload is None:
        return False, reason or "invalid_ticket"
    jti = str(payload.get("jti") or "")
    if not jti:
        return False, "missing_jti"
    revoke_ticket_jti(jti)
    return True, ""


def is_loopback_url(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        return True
    if text.startswith("http://127.0.0.1") or text.startswith("https://127.0.0.1"):
        return True
    if "://localhost" in text.lower() or text.lower().startswith("localhost"):
        return True
    return False


def public_relay_url(public_base: str, runner_session_id: str, ticket: str) -> str:
    base = str(public_base or "").rstrip("/")
    if not base:
        raise ValueError("public_base_required")
    if is_loopback_url(base):
        raise ValueError("public_base_must_not_be_loopback")
    path = domain.RELAY_PATH_TEMPLATE.format(
        runner_session_id=urllib.parse.quote(str(runner_session_id), safe=""),
    )
    if base.startswith("https://"):
        scheme_ws = "wss://"
        rest = base[len("https://"):]
    elif base.startswith("http://"):
        scheme_ws = "ws://"
        rest = base[len("http://"):]
    elif base.startswith("wss://") or base.startswith("ws://"):
        return f"{base}{path}?{urllib.parse.urlencode({'ticket': ticket})}"
    else:
        scheme_ws = "wss://"
        rest = base
    return f"{scheme_ws}{rest}{path}?{urllib.parse.urlencode({'ticket': ticket})}"


def sanitize_browser_stream_metadata(
    meta: Mapping[str, Any] | None,
    *,
    relay_url: str = "",
) -> dict[str, Any]:
    """Strip/replace loopback stream URLs before browser-facing responses."""
    out = dict(meta or {})
    relay = str(relay_url or out.get("relay_url") or "").strip()
    for key in ("stream_url", "relay_url", "watch_url", "pty_url"):
        value = str(out.get(key) or "")
        if not value:
            continue
        if is_loopback_url(value):
            if relay and not is_loopback_url(relay):
                out[key] = relay
            else:
                out.pop(key, None)
    # local_stream_url is host-private only — never publish to browsers/control plane.
    out.pop("local_stream_url", None)
    # Never leave a loopback in any remaining URL-ish field values.
    for key, value in list(out.items()):
        if isinstance(value, str) and is_loopback_url(value):
            if relay and not is_loopback_url(relay) and key in {
                "stream_url", "relay_url", "watch_url", "pty_url",
            }:
                out[key] = relay
            else:
                out.pop(key, None)
    if relay and not is_loopback_url(relay):
        out.setdefault("relay_url", relay)
        out.setdefault("stream_url", relay)
        out["transport"] = domain.TRANSPORT_SWITCHBOARD_PTY_RELAY
        out["browser_safe"] = True
        out["relay_required"] = False
    return out


@dataclass
class _BrowserClient:
    client_id: str
    send_fn: SendFn
    scopes: list[str]
    ticket_jti: str
    queue: list[str] = field(default_factory=list)
    disconnected: bool = False
    last_active: float = field(default_factory=time.time)


@dataclass
class _RelaySession:
    runner_session_id: str
    binding: dict[str, str] = field(default_factory=dict)
    host_send: SendFn | None = None
    browsers: dict[str, _BrowserClient] = field(default_factory=dict)
    replay: list[str] = field(default_factory=list)
    replay_bytes: int = 0
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    closed: bool = False
    close_reason: str = ""


class RelayHub:
    """In-memory fan-out hub between one host tunnel and N browser watchers."""

    def __init__(
        self,
        *,
        browser_queue_limit: int = domain.DEFAULT_BROWSER_QUEUE_LIMIT,
        replay_frame_limit: int = domain.DEFAULT_REPLAY_FRAME_LIMIT,
        replay_byte_limit: int = domain.DEFAULT_REPLAY_BYTE_LIMIT,
        idle_ttl_seconds: int = domain.DEFAULT_IDLE_TTL_SECONDS,
        absolute_ttl_seconds: int = domain.DEFAULT_ABSOLUTE_TTL_SECONDS,
    ):
        self.browser_queue_limit = max(1, int(browser_queue_limit))
        self.replay_frame_limit = max(1, int(replay_frame_limit))
        self.replay_byte_limit = max(1024, int(replay_byte_limit))
        self.idle_ttl_seconds = max(30, int(idle_ttl_seconds))
        self.absolute_ttl_seconds = max(60, int(absolute_ttl_seconds))
        self._sessions: dict[str, _RelaySession] = {}
        self._lock = threading.RLock()

    def ensure_session(
        self,
        runner_session_id: str,
        binding: Mapping[str, Any] | None = None,
    ) -> _RelaySession:
        sid = str(runner_session_id or "").strip()
        if not sid:
            raise ValueError("runner_session_id_required")
        with self._lock:
            self.cleanup_expired()
            session = self._sessions.get(sid)
            if session is None:
                session = _RelaySession(
                    runner_session_id=sid,
                    binding=domain.merge_binding(binding or {"runner_session_id": sid}),
                )
                self._sessions[sid] = session
            elif binding:
                session.binding = domain.merge_binding(session.binding, binding)
            return session

    def attach_host(self, session_id: str, send_fn: SendFn,
                    binding: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            session = self.ensure_session(session_id, binding)
            if session.closed:
                return {"ok": False, "error": "session_closed", "reason": session.close_reason}
            session.host_send = send_fn
            session.last_active = time.time()
            return {"ok": True, "runner_session_id": session.runner_session_id}

    def detach_host(self, session_id: str, send_fn: SendFn | None = None) -> None:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return
            if send_fn is None or session.host_send is send_fn:
                session.host_send = None

    def attach_browser(
        self,
        session_id: str,
        ticket_payload: Mapping[str, Any],
        send_fn: SendFn,
        *,
        client_id: str = "",
    ) -> dict[str, Any]:
        scopes = domain.normalize_scopes(ticket_payload.get("scopes"))
        if "watch" not in scopes:
            return {"ok": False, "error": "missing_scope", "reason": "watch_required"}
        sid = str(session_id or "").strip()
        ticket_sid = str(ticket_payload.get("runner_session_id") or "").strip()
        if ticket_sid and ticket_sid != sid:
            return {"ok": False, "error": "session_mismatch"}
        with self._lock:
            session = self.ensure_session(sid, ticket_payload)
            if session.closed:
                return {"ok": False, "error": "session_closed", "reason": session.close_reason}
            # Fail closed on cross-session/host/task when session already bound.
            for key in ("host_id", "task_id", "claim_id", "wake_id", "work_session_id"):
                have = str(session.binding.get(key) or "").strip()
                want = str(ticket_payload.get(key) or "").strip()
                if have and want and have != want:
                    return {"ok": False, "error": f"{key}_mismatch"}
            cid = str(client_id or uuid.uuid4().hex)
            client = _BrowserClient(
                client_id=cid,
                send_fn=send_fn,
                scopes=scopes,
                ticket_jti=str(ticket_payload.get("jti") or ""),
            )
            session.browsers[cid] = client
            session.last_active = time.time()
            replay_frames = list(session.replay)
        for encoded in replay_frames:
            self._enqueue_browser(session_id, cid, encoded, is_replay=True)
        return {
            "ok": True,
            "client_id": cid,
            "runner_session_id": sid,
            "replay_frames": len(replay_frames),
        }

    def detach_browser(self, session_id: str, client_id: str) -> None:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return
            session.browsers.pop(str(client_id), None)

    def route_browser_to_host(
        self,
        session_id: str,
        client_id: str,
        frame: Mapping[str, Any] | str | bytes,
    ) -> dict[str, Any]:
        try:
            decoded = domain.decode_frame(frame)
        except ValueError as exc:
            return {"ok": False, "error": "malformed_frame", "reason": str(exc)}
        kind = decoded["type"]
        if kind == "ping":
            pong = domain.encode_frame("pong", {"ts": decoded.get("ts") or time.time()})
            self._send_to_browser(session_id, client_id, pong)
            return {"ok": True, "type": "pong"}
        if kind == "close":
            self.detach_browser(session_id, client_id)
            return {"ok": True, "type": "close"}
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session or session.closed:
                return {"ok": False, "error": "session_closed"}
            client = session.browsers.get(str(client_id))
            if not client or client.disconnected:
                return {"ok": False, "error": "client_disconnected"}
            scopes = set(client.scopes)
            if kind == "input" and "input" not in scopes:
                return {"ok": False, "error": "missing_scope", "reason": "input"}
            if kind == "resize" and "resize" not in scopes:
                return {"ok": False, "error": "missing_scope", "reason": "resize"}
            if kind == "signal" and "signal" not in scopes:
                return {"ok": False, "error": "missing_scope", "reason": "signal"}
            # kill is a logical control; may arrive as type=signal name=kill or type via payload
            if kind not in {"input", "resize", "signal"}:
                return {"ok": False, "error": "unsupported_frame", "reason": kind}
            if str(decoded.get("action") or "").lower() == "kill" or (
                kind == "signal" and str(decoded.get("name") or "").upper() == "KILL"
            ):
                if "kill" not in scopes:
                    return {"ok": False, "error": "missing_scope", "reason": "kill"}
            host_send = session.host_send
            session.last_active = time.time()
            client.last_active = session.last_active
        if host_send is None:
            return {"ok": False, "error": "host_detached"}
        encoded = domain.encode_frame(
            kind,
            {k: v for k, v in decoded.items() if k not in {"type", "data", "data_b64"}},
            data=decoded.get("data") if isinstance(decoded.get("data"), (bytes, bytearray)) else None,
        )
        try:
            host_send(encoded)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "host_send_failed", "reason": type(exc).__name__}
        return {"ok": True, "type": kind}

    def route_host_to_browsers(
        self,
        session_id: str,
        frame: Mapping[str, Any] | str | bytes,
    ) -> dict[str, Any]:
        try:
            decoded = domain.decode_frame(frame)
        except ValueError as exc:
            return {"ok": False, "error": "malformed_frame", "reason": str(exc)}
        kind = decoded["type"]
        if kind not in domain.HOST_TO_BROWSER_TYPES and kind != "ping":
            return {"ok": False, "error": "unsupported_frame", "reason": kind}
        if kind == "ping":
            # Host ping is answered locally; browsers do not need it.
            return {"ok": True, "type": "ping"}
        encoded = domain.encode_frame(
            kind,
            {k: v for k, v in decoded.items() if k not in {"type", "data", "data_b64"}},
            data=decoded.get("data") if isinstance(decoded.get("data"), (bytes, bytearray)) else None,
        )
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return {"ok": False, "error": "unknown_session"}
            if session.closed and kind not in {"close", "error"}:
                return {"ok": False, "error": "session_closed"}
            session.last_active = time.time()
            if kind in {"output", "state", "replay"}:
                self._append_replay(session, encoded, domain.frame_byte_size(decoded))
            client_ids = list(session.browsers.keys())
            if kind in {"close", "error"}:
                session.closed = True
                session.close_reason = str(decoded.get("reason") or kind)
        delivered = 0
        for cid in client_ids:
            if self._enqueue_browser(session_id, cid, encoded):
                delivered += 1
        if kind in {"close", "error"}:
            with self._lock:
                session = self._sessions.get(str(session_id))
                if session:
                    session.browsers.clear()
                    session.host_send = None
        return {"ok": True, "type": kind, "delivered": delivered}

    def publish_output(self, session_id: str, data: bytes,
                       *, replay: bool = False) -> dict[str, Any]:
        kind = "replay" if replay else "output"
        return self.route_host_to_browsers(
            session_id, domain.encode_frame(kind, data=data or b""))

    def close_session(self, session_id: str, *, reason: str = "closed") -> dict[str, Any]:
        return self.route_host_to_browsers(
            session_id,
            domain.encode_frame("close", {"reason": reason}),
        )

    def error_session(self, session_id: str, *, reason: str = "error",
                      message: str = "") -> dict[str, Any]:
        return self.route_host_to_browsers(
            session_id,
            domain.encode_frame("error", {"reason": reason, "message": message}),
        )

    def cleanup_expired(self, *, now: float | None = None) -> list[str]:
        ts = float(now if now is not None else time.time())
        removed: list[str] = []
        with self._lock:
            for sid, session in list(self._sessions.items()):
                idle = ts - float(session.last_active or session.created_at)
                age = ts - float(session.created_at)
                if session.closed or idle > self.idle_ttl_seconds or age > self.absolute_ttl_seconds:
                    removed.append(sid)
                    self._sessions.pop(sid, None)
        return removed

    def session_info(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return None
            return {
                "runner_session_id": session.runner_session_id,
                "binding": dict(session.binding),
                "host_attached": session.host_send is not None,
                "browser_count": len(session.browsers),
                "replay_frames": len(session.replay),
                "closed": session.closed,
                "close_reason": session.close_reason,
                "created_at": session.created_at,
                "last_active": session.last_active,
            }

    def _append_replay(self, session: _RelaySession, encoded: str, nbytes: int) -> None:
        session.replay.append(encoded)
        session.replay_bytes += max(0, int(nbytes))
        while (
            len(session.replay) > self.replay_frame_limit
            or session.replay_bytes > self.replay_byte_limit
        ):
            if not session.replay:
                break
            dropped = session.replay.pop(0)
            try:
                session.replay_bytes -= domain.frame_byte_size(domain.decode_frame(dropped))
            except Exception:
                session.replay_bytes = max(0, session.replay_bytes - len(dropped))
        if session.replay_bytes < 0:
            session.replay_bytes = 0

    def _enqueue_browser(
        self,
        session_id: str,
        client_id: str,
        encoded: str,
        *,
        is_replay: bool = False,
    ) -> bool:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return False
            client = session.browsers.get(str(client_id))
            if not client or client.disconnected:
                return False
            if len(client.queue) >= self.browser_queue_limit:
                bp = domain.encode_frame(
                    "backpressure",
                    {"reason": "queue_overflow", "limit": self.browser_queue_limit},
                )
                try:
                    client.send_fn(bp)
                except Exception:
                    pass
                client.disconnected = True
                session.browsers.pop(str(client_id), None)
                return False
            client.queue.append(encoded)
            send_fn = client.send_fn
            # Drain immediately for callable bridges / tests.
            pending = list(client.queue)
            client.queue.clear()
        for item in pending:
            try:
                send_fn(item)
            except Exception:
                with self._lock:
                    session = self._sessions.get(str(session_id))
                    if session:
                        client = session.browsers.get(str(client_id))
                        if client:
                            client.disconnected = True
                            session.browsers.pop(str(client_id), None)
                return False
        return True

    def _send_to_browser(self, session_id: str, client_id: str, encoded: str) -> None:
        self._enqueue_browser(session_id, client_id, encoded)


# Process-wide default hub used by the API router.
_DEFAULT_HUB: RelayHub | None = None
_DEFAULT_HUB_LOCK = threading.Lock()


def get_default_hub() -> RelayHub:
    global _DEFAULT_HUB
    with _DEFAULT_HUB_LOCK:
        if _DEFAULT_HUB is None:
            _DEFAULT_HUB = RelayHub()
        return _DEFAULT_HUB


def reset_default_hub_for_tests() -> RelayHub:
    global _DEFAULT_HUB
    with _DEFAULT_HUB_LOCK:
        _DEFAULT_HUB = RelayHub()
        return _DEFAULT_HUB
