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
from switchboard.domain import pty_screen
from switchboard.domain import runner_pty as domain

SendFn = Callable[[bytes], Optional[bool]]
CloseFn = Callable[[], None]

# In-process cache: jti -> expires_at (unix seconds). Shared DB is authoritative
# across instances; this cache avoids a DB round-trip on every frame.
_REVOKED_JTIS: dict[str, float] = {}
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


def _remember_revoked_jti(jti: str, expires_at: float) -> None:
    token = str(jti or "").strip()
    if not token:
        return
    with _REVOKE_LOCK:
        prior = _REVOKED_JTIS.get(token)
        _REVOKED_JTIS[token] = max(float(expires_at), float(prior or 0.0))


def _purge_expired_memory(now: float) -> None:
    stale = [jti for jti, exp in _REVOKED_JTIS.items() if float(exp) <= now]
    for jti in stale:
        _REVOKED_JTIS.pop(jti, None)


def revoke_ticket_jti(
    jti: str,
    *,
    project: str = "",
    expires_at: float | None = None,
    hub: "RelayHub | None" = None,
    now: float | None = None,
) -> bool:
    """Revoke a ticket jti, persist until expiry, and drop matching live clients."""
    token = str(jti or "").strip()
    if not token:
        return False
    ts = float(now if now is not None else time.time())
    exp = float(expires_at) if expires_at is not None else (
        ts + float(domain.DEFAULT_ABSOLUTE_TTL_SECONDS)
    )
    if exp <= ts:
        exp = ts + 1.0
    project_id = str(project or "").strip()
    if project_id:
        from switchboard.storage.repositories import runner_pty_revocations as rev_store
        rev_store.persist_revoked_jti(
            token, expires_at=exp, project=project_id, now=ts)
    _remember_revoked_jti(token, exp)
    target = hub if hub is not None else get_default_hub()
    target.disconnect_by_jti(token, reason="ticket_revoked")
    return True


def is_jti_revoked(
    jti: str,
    *,
    project: str = "",
    now: float | None = None,
) -> bool:
    token = str(jti or "").strip()
    if not token:
        return False
    ts = float(now if now is not None else time.time())
    with _REVOKE_LOCK:
        _purge_expired_memory(ts)
        exp = _REVOKED_JTIS.get(token)
        if exp is not None:
            return float(exp) > ts
    project_id = str(project or "").strip()
    if not project_id:
        return False
    try:
        from switchboard.storage.repositories import runner_pty_revocations as rev_store
        expires_at = rev_store.is_jti_revoked_persisted(
            token, project=project_id, now=ts)
        if expires_at is not None:
            _remember_revoked_jti(token, float(expires_at))
            return True
    except Exception:
        return False
    return False


def clear_revoked_jtis_for_tests(project: str = "") -> None:
    with _REVOKE_LOCK:
        _REVOKED_JTIS.clear()
    project_id = str(project or "").strip()
    if not project_id:
        return
    try:
        from switchboard.storage.repositories import runner_pty_revocations as rev_store
        rev_store.clear_revoked_jtis_for_tests(project_id)
    except Exception:
        pass


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
    if is_jti_revoked(jti, project=str(payload.get("project_id") or ""), now=now):
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


def revoke_capability_ticket(
    ticket: str,
    *,
    secret: str | None = None,
    project: str = "",
    hub: "RelayHub | None" = None,
    now: float | None = None,
) -> tuple[bool, str]:
    # Far-past clock: jwt_decode rejects when now >= exp. Use now=0 so expired
    # tickets can still be revoked by signature (BUG-75).
    payload, reason = jwt_decode(ticket or "", secret or relay_secret(), now=0.0)
    if payload is None:
        return False, reason or "invalid_ticket"
    jti = str(payload.get("jti") or "")
    if not jti:
        return False, "missing_jti"
    exp_raw = payload.get("exp")
    expires_at = float(exp_raw) if exp_raw is not None else None
    project_id = str(project or payload.get("project_id") or "").strip()
    revoke_ticket_jti(
        jti,
        project=project_id,
        expires_at=expires_at,
        hub=hub,
        now=now,
    )
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


def _public_ws_url(public_base: str, path: str, ticket: str) -> str:
    base = str(public_base or "").rstrip("/")
    if not base:
        raise ValueError("public_base_required")
    if is_loopback_url(base):
        raise ValueError("public_base_must_not_be_loopback")
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


def public_relay_url(public_base: str, runner_session_id: str, ticket: str) -> str:
    path = domain.RELAY_PATH_TEMPLATE.format(
        runner_session_id=urllib.parse.quote(str(runner_session_id), safe=""),
    )
    return _public_ws_url(public_base, path, ticket)


def public_host_relay_url(public_base: str, runner_session_id: str, ticket: str) -> str:
    """Browser-facing hosts must use /pty/host with a host_tunnel ticket (BUG-74)."""
    path = domain.HOST_RELAY_PATH_TEMPLATE.format(
        runner_session_id=urllib.parse.quote(str(runner_session_id), safe=""),
    )
    return _public_ws_url(public_base, path, ticket)


def mint_host_tunnel_ticket(
    binding: Mapping[str, Any],
    ttl_seconds: int = domain.DEFAULT_TICKET_TTL_SECONDS,
    *,
    now: float | None = None,
    secret: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Mint a host-only tunnel ticket (never includes browser watch scopes)."""
    return mint_capability_ticket(
        binding,
        [domain.HOST_TUNNEL_SCOPE],
        ttl_seconds=ttl_seconds,
        now=now,
        secret=secret,
    )


def ticket_allows_host_tunnel(payload: Mapping[str, Any] | None) -> tuple[bool, str]:
    """Fail closed: host tunnel requires host_tunnel and forbids browser scopes."""
    scopes = set(domain.normalize_scopes((payload or {}).get("scopes")))
    if domain.HOST_TUNNEL_SCOPE not in scopes:
        return False, "host_tunnel_required"
    if scopes & domain.BROWSER_CAPABILITY_SCOPES:
        return False, "browser_ticket_forbidden"
    return True, ""


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
    queue: list[bytes] = field(default_factory=list)
    disconnected: bool = False
    last_active: float = field(default_factory=time.time)
    close_fn: CloseFn | None = None


@dataclass
class _RelaySession:
    runner_session_id: str
    binding: dict[str, str] = field(default_factory=dict)
    host_send: SendFn | None = None
    host_ticket_jti: str = ""
    host_close_fn: CloseFn | None = None
    host_queue: list[bytes] = field(default_factory=list)
    browsers: dict[str, _BrowserClient] = field(default_factory=dict)
    replay: list[bytes] = field(default_factory=list)
    replay_bytes: int = 0
    # UI-25: headless screen model fed by the PTY byte stream, so a newly
    # attached browser gets a full-frame snapshot of the current screen
    # instead of a blank when the source app (a TUI) is idle and the
    # byte-replay ring has rolled past its last full paint.
    screen: pty_screen.ScreenModel = field(default_factory=pty_screen.ScreenModel)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    closed: bool = False
    close_reason: str = ""
    backpressure: bool = False
    backpressured_browsers: set[str] = field(default_factory=set)


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

    def attach_host(
        self,
        session_id: str,
        send_fn: SendFn,
        binding: Mapping[str, Any] | None = None,
        *,
        close_fn: CloseFn | None = None,
    ) -> dict[str, Any]:
        bind = dict(binding or {})
        allowed, deny_reason = ticket_allows_host_tunnel(bind)
        if not allowed:
            return {
                "ok": False,
                "error": "missing_scope" if deny_reason == "host_tunnel_required"
                else deny_reason,
                "reason": deny_reason,
            }
        ticket_host = str(bind.get("host_id") or "").strip()
        if not ticket_host:
            return {
                "ok": False,
                "error": "host_id_required",
                "reason": "absent_permission",
            }
        with self._lock:
            sid = str(session_id or "").strip()
            existing = self._sessions.get(sid)
            # Compare against the already-bound session before merge overlays.
            if existing is not None:
                if existing.closed:
                    return {
                        "ok": False,
                        "error": "session_closed",
                        "reason": existing.close_reason,
                    }
                session_host = str(existing.binding.get("host_id") or "").strip()
                if session_host and session_host != ticket_host:
                    return {
                        "ok": False,
                        "error": "host_mismatch",
                        "reason": "host_id_mismatch",
                    }
                pending_reservation = (
                    str(existing.binding.get("permission_profile") or "")
                    == "operator_watch_pending"
                )
                for key in ("task_id", "claim_id", "wake_id", "work_session_id",
                            "runner_session_id"):
                    have = str(existing.binding.get(key) or "").strip()
                    want = str(bind.get(key) or "").strip()
                    mutable_pending = pending_reservation and key in {
                        "claim_id", "work_session_id",
                    }
                    if have and want and have != want and not mutable_pending:
                        return {"ok": False, "error": f"{key}_mismatch"}
                # BUG-74: one active host tunnel — never silently replace host_send.
                if existing.host_send is not None:
                    return {
                        "ok": False,
                        "error": "host_already_attached",
                        "reason": "single_host_tunnel",
                    }
            session = self.ensure_session(session_id, bind)
            if existing is not None and pending_reservation:
                # Host authentication upgrades the reservation to the durable
                # claim/Work Session bind without changing its exact
                # runner/task/host/wake identity.
                session.binding = domain.merge_binding(bind)
            if session.closed:
                return {"ok": False, "error": "session_closed", "reason": session.close_reason}
            jti = str(bind.get("jti") or "").strip()
            project = str(bind.get("project_id") or session.binding.get("project_id") or "")
            if jti and is_jti_revoked(jti, project=project):
                return {"ok": False, "error": "revoked", "reason": "ticket_revoked"}
            session.host_send = send_fn
            session.host_ticket_jti = jti
            session.host_close_fn = close_fn
            session.last_active = time.time()
            buffered = len(session.host_queue)
            browser_ids = list(session.browsers)
        drained = self._drain_host(session_id)
        ready = domain.encode_frame("ready", {
            "connection_state": "host_attached",
            "host_attached": True,
            "relay_ready": True,
        })
        for client_id in browser_ids:
            self._enqueue_browser(session_id, client_id, ready)
        return {
            "ok": True,
            "runner_session_id": str(session_id),
            "buffered_frames": buffered,
            "backpressure": not drained,
        }

    def detach_host(self, session_id: str, send_fn: SendFn | None = None) -> None:
        browser_ids: list[str] = []
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return
            if send_fn is None or session.host_send is send_fn:
                session.host_send = None
                session.host_ticket_jti = ""
                session.host_close_fn = None
                browser_ids = list(session.browsers)
        waiting = domain.encode_frame("ready", {
            "connection_state": "waiting_for_host",
            "host_attached": False,
            "relay_ready": True,
        })
        for client_id in browser_ids:
            self._enqueue_browser(session_id, client_id, waiting)

    def attach_browser(
        self,
        session_id: str,
        ticket_payload: Mapping[str, Any],
        send_fn: SendFn,
        *,
        client_id: str = "",
        close_fn: CloseFn | None = None,
    ) -> dict[str, Any]:
        scopes = domain.normalize_scopes(ticket_payload.get("scopes"))
        if "watch" not in scopes:
            return {"ok": False, "error": "missing_scope", "reason": "watch_required"}
        sid = str(session_id or "").strip()
        ticket_sid = str(ticket_payload.get("runner_session_id") or "").strip()
        if ticket_sid and ticket_sid != sid:
            return {"ok": False, "error": "session_mismatch"}
        jti = str(ticket_payload.get("jti") or "").strip()
        project = str(ticket_payload.get("project_id") or "")
        if jti and is_jti_revoked(jti, project=project):
            return {"ok": False, "error": "revoked", "reason": "ticket_revoked"}
        with self._lock:
            existing = self._sessions.get(sid)
            session = self.ensure_session(
                sid, ticket_payload if existing is None else None)
            if session.closed:
                return {"ok": False, "error": "session_closed", "reason": session.close_reason}
            # Fail closed on cross-session/host/task when session already bound.
            pending_reservation = (
                str(ticket_payload.get("permission_profile") or "")
                == "operator_watch_pending"
            )
            for key in ("host_id", "task_id", "claim_id", "wake_id", "work_session_id"):
                have = str(session.binding.get(key) or "").strip()
                want = str(ticket_payload.get(key) or "").strip()
                mutable_pending = pending_reservation and key in {
                    "claim_id", "work_session_id",
                }
                if have and want and have != want and not mutable_pending:
                    return {"ok": False, "error": f"{key}_mismatch"}
            cid = str(client_id or uuid.uuid4().hex)
            client = _BrowserClient(
                client_id=cid,
                send_fn=send_fn,
                scopes=scopes,
                ticket_jti=jti,
                close_fn=close_fn,
            )
            session.browsers[cid] = client
            session.last_active = time.time()
            host_attached = session.host_send is not None
            # UI-25: prefer a full-frame snapshot of the current screen over the
            # raw byte-replay ring. The snapshot IS the current screen, so it
            # renders instantly even for an idle TUI whose last paint has rolled
            # out of the ring; the ring would also double-apply on top of it, so
            # they are mutually exclusive. Fall back to the ring when no screen
            # model is available (pyte missing) or nothing has been drawn yet.
            snapshot = session.screen.snapshot_bytes()
            replay_frames = [] if snapshot else list(session.replay)
        self._enqueue_browser(
            session_id,
            cid,
            domain.encode_frame("ready", {
                "connection_state": (
                    "host_attached" if host_attached else "waiting_for_host"),
                "host_attached": host_attached,
                "relay_ready": True,
            }),
            is_replay=True,
        )
        sent_snapshot = False
        if snapshot:
            snapshot_frame = domain.encode_frame("snapshot", {}, data=snapshot)
            sent_snapshot = self._enqueue_browser(
                session_id, cid, snapshot_frame, is_replay=True)
        for encoded in replay_frames:
            self._enqueue_browser(session_id, cid, encoded, is_replay=True)
        return {
            "ok": True,
            "client_id": cid,
            "runner_session_id": sid,
            "replay_frames": len(replay_frames),
            "snapshot": bool(sent_snapshot),
        }

    def disconnect_by_jti(self, jti: str, *, reason: str = "ticket_revoked") -> dict[str, Any]:
        """Drop every live browser/host client bound to ``jti`` and notify them."""
        token = str(jti or "").strip()
        if not token:
            return {"ok": True, "browsers": 0, "hosts": 0}
        close_frame = domain.encode_frame("exit", {"reason": reason})
        browser_targets: list[tuple[str, str, SendFn, CloseFn | None]] = []
        host_targets: list[tuple[str, SendFn, CloseFn | None]] = []
        with self._lock:
            for sid, session in list(self._sessions.items()):
                for cid, client in list(session.browsers.items()):
                    if client.ticket_jti != token or client.disconnected:
                        continue
                    client.disconnected = True
                    browser_targets.append(
                        (sid, cid, client.send_fn, client.close_fn))
                    session.browsers.pop(cid, None)
                    session.backpressured_browsers.discard(cid)
                if session.host_ticket_jti == token and session.host_send is not None:
                    host_targets.append(
                        (sid, session.host_send, session.host_close_fn))
                    session.host_send = None
                    session.host_ticket_jti = ""
                    session.host_close_fn = None
                session.backpressure = bool(session.backpressured_browsers)
        for _sid, _cid, send_fn, close_fn in browser_targets:
            try:
                send_fn(close_frame)
            except Exception:
                pass
            if close_fn is not None:
                try:
                    close_fn()
                except Exception:
                    pass
        for _sid, send_fn, close_fn in host_targets:
            try:
                send_fn(close_frame)
            except Exception:
                pass
            if close_fn is not None:
                try:
                    close_fn()
                except Exception:
                    pass
        return {
            "ok": True,
            "browsers": len(browser_targets),
            "hosts": len(host_targets),
            "reason": reason,
        }

    def _client_revoked_locked(
        self,
        session: _RelaySession,
        client: _BrowserClient,
        *,
        now: float | None = None,
    ) -> bool:
        if not client.ticket_jti:
            return False
        project = str(session.binding.get("project_id") or "")
        return is_jti_revoked(client.ticket_jti, project=project, now=now)

    def detach_browser(self, session_id: str, client_id: str) -> None:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return
            session.browsers.pop(str(client_id), None)
            session.backpressured_browsers.discard(str(client_id))
            session.backpressure = bool(session.backpressured_browsers)

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
        if kind == "exit":
            self.detach_browser(session_id, client_id)
            return {"ok": True, "type": "exit"}
        revoked_send: SendFn | None = None
        revoked_close: CloseFn | None = None
        host_send: SendFn | None = None
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session or session.closed:
                return {"ok": False, "error": "session_closed"}
            client = session.browsers.get(str(client_id))
            if not client or client.disconnected:
                return {"ok": False, "error": "client_disconnected"}
            if self._client_revoked_locked(session, client):
                client.disconnected = True
                revoked_send = client.send_fn
                revoked_close = client.close_fn
                session.browsers.pop(str(client_id), None)
            else:
                scopes = set(client.scopes)
                if kind == "in" and "input" not in scopes:
                    return {"ok": False, "error": "missing_scope", "reason": "input"}
                if kind == "resize" and "resize" not in scopes:
                    return {"ok": False, "error": "missing_scope", "reason": "resize"}
                if kind == "signal" and "signal" not in scopes:
                    return {"ok": False, "error": "missing_scope", "reason": "signal"}
                if kind not in {"in", "resize", "signal"}:
                    return {"ok": False, "error": "unsupported_frame", "reason": kind}
                if str(decoded.get("action") or "").lower() == "kill" or (
                    kind == "signal" and str(decoded.get("name") or "").upper() == "KILL"
                ):
                    if "kill" not in scopes:
                        return {"ok": False, "error": "missing_scope", "reason": "kill"}
                # UI-25: keep the screen model sized to the PTY. The host applies
                # this resize to the PTY, so the reconstructed snapshot must use
                # the same dimensions or it reflows wrong for late joiners.
                if kind == "resize":
                    session.screen.resize(
                        decoded.get("rows") or decoded.get("row"),
                        decoded.get("cols") or decoded.get("col") or decoded.get("columns"),
                    )
                host_send = session.host_send
                session.last_active = time.time()
                client.last_active = session.last_active
        if revoked_send is not None or revoked_close is not None:
            try:
                if revoked_send is not None:
                    revoked_send(domain.encode_frame(
                        "exit", {"reason": "ticket_revoked"}))
            except Exception:
                pass
            if revoked_close is not None:
                try:
                    revoked_close()
                except Exception:
                    pass
            return {"ok": False, "error": "revoked", "reason": "ticket_revoked"}
        encoded = domain.encode_frame(
            kind,
            {k: v for k, v in decoded.items() if k not in {"type", "data", "data_b64"}},
            data=decoded.get("data") if isinstance(decoded.get("data"), (bytes, bytearray)) else None,
        )
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session or session.closed:
                return {"ok": False, "error": "session_closed"}
            session.host_queue.append(encoded)
            buffered = len(session.host_queue)
        drained = self._drain_host(session_id)
        return {
            "ok": True,
            "type": kind,
            "buffered": buffered if not drained else 0,
            "backpressure": not drained,
            "host_attached": host_send is not None,
        }

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
        if kind not in domain.HOST_TO_BROWSER_TYPES:
            return {"ok": False, "error": "unsupported_frame", "reason": kind}
        encoded = domain.encode_frame(
            kind,
            {k: v for k, v in decoded.items() if k not in {"type", "data", "data_b64"}},
            data=decoded.get("data") if isinstance(decoded.get("data"), (bytes, bytearray)) else None,
        )
        host_send: SendFn | None = None
        host_close: CloseFn | None = None
        client_ids: list[str] = []
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return {"ok": False, "error": "unknown_session"}
            host_jti = str(session.host_ticket_jti or "")
            project = str(session.binding.get("project_id") or "")
            if host_jti and is_jti_revoked(host_jti, project=project):
                host_send = session.host_send
                host_close = session.host_close_fn
                session.host_send = None
                session.host_ticket_jti = ""
                session.host_close_fn = None
            elif session.closed and kind != "exit":
                return {"ok": False, "error": "session_closed"}
            else:
                session.last_active = time.time()
                if kind in {"out", "ready", "snapshot"}:
                    self._append_replay(session, encoded, domain.frame_byte_size(decoded))
                    # UI-25: keep the screen model current so late joiners can be
                    # handed a full frame. Only real output bytes carry screen state.
                    if kind == "out":
                        data = decoded.get("data")
                        if isinstance(data, (bytes, bytearray)):
                            session.screen.feed(bytes(data))
                client_ids = list(session.browsers.keys())
                if kind == "exit":
                    session.closed = True
                    session.close_reason = str(decoded.get("reason") or kind)
        if host_send is not None or host_close is not None:
            try:
                if host_send is not None:
                    host_send(domain.encode_frame(
                        "exit", {"reason": "ticket_revoked"}))
            except Exception:
                pass
            if host_close is not None:
                try:
                    host_close()
                except Exception:
                    pass
            return {"ok": False, "error": "revoked", "reason": "ticket_revoked"}
        delivered = 0
        backpressure = False
        for cid in client_ids:
            result = self._enqueue_browser(session_id, cid, encoded)
            if result:
                delivered += 1
            else:
                # Queue pressure or slow client — keep browser; signal host to slow.
                backpressure = True
        if kind == "exit":
            with self._lock:
                session = self._sessions.get(str(session_id))
                if session:
                    session.browsers.clear()
                    session.backpressured_browsers.clear()
                    session.backpressure = False
                    session.host_send = None
                    session.host_ticket_jti = ""
                    session.host_close_fn = None
                    session.host_queue.clear()
        if backpressure:
            with self._lock:
                session = self._sessions.get(str(session_id))
                if session:
                    session.backpressure = True
        return {
            "ok": True,
            "type": kind,
            "delivered": delivered,
            "backpressure": backpressure,
        }

    def publish_output(self, session_id: str, data: bytes,
                       *, replay: bool = False) -> dict[str, Any]:
        kind = "snapshot" if replay else "out"
        return self.route_host_to_browsers(
            session_id, domain.encode_frame(kind, data=data or b""))

    def close_session(self, session_id: str, *, reason: str = "closed") -> dict[str, Any]:
        return self.route_host_to_browsers(
            session_id,
            domain.encode_frame("exit", {"reason": reason}),
        )

    def error_session(self, session_id: str, *, reason: str = "error",
                      message: str = "") -> dict[str, Any]:
        return self.route_host_to_browsers(
            session_id,
            domain.encode_frame("exit", {"reason": reason, "message": message}),
        )

    def set_browser_backpressure(
        self, session_id: str, client_id: str, value: bool = True,
    ) -> None:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return
            cid = str(client_id or "")
            if value and cid:
                session.backpressured_browsers.add(cid)
            elif cid:
                session.backpressured_browsers.discard(cid)
            session.backpressure = bool(session.backpressured_browsers)

    def set_backpressure(self, session_id: str, value: bool = True) -> None:
        """Compatibility helper for non-router bridges and older tests."""
        self.set_browser_backpressure(session_id, "__transport__", value)

    def clear_backpressure(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if session:
                session.backpressured_browsers.clear()
                session.backpressure = False

    def flush_browser(self, session_id: str, client_id: str) -> bool:
        """Retry bytes retained when the WebSocket outbound queue was full."""
        return self._drain_browser(session_id, client_id)

    def host_should_pause(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(str(session_id))
            return bool(session and session.backpressure)

    def browser_should_pause(self, session_id: str) -> bool:
        """Stop browser receives when host input is waiting for WS capacity."""
        with self._lock:
            session = self._sessions.get(str(session_id))
            return bool(
                session and len(session.host_queue) >= self.browser_queue_limit)

    def flush_host(self, session_id: str) -> bool:
        return self._drain_host(session_id)

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
                "backpressure": session.backpressure,
                "host_buffered_frames": len(session.host_queue),
                "created_at": session.created_at,
                "last_active": session.last_active,
            }

    def _append_replay(self, session: _RelaySession, encoded: bytes, nbytes: int) -> None:
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
        encoded: bytes,
        *,
        is_replay: bool = False,
    ) -> bool:
        """Enqueue+drain a frame to a browser. On overflow or send failure, keep
        the browser attached and signal backpressure so the host slows PTY reads
        instead of silently disconnecting the watcher (SIMPLIFY-9).
        """
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return False
            client = session.browsers.get(str(client_id))
            if not client or client.disconnected:
                return False
            # The limit is a flow-control high-water mark, not a loss policy.
            # Retain the boundary frame, then stop reading the host socket so
            # TCP pressure reaches the executor's master-fd loop.
            client.queue.append(encoded)
            if len(client.queue) >= self.browser_queue_limit:
                session.backpressured_browsers.add(str(client_id))
                session.backpressure = True
        return self._drain_browser(session_id, client_id)

    def _drain_browser(self, session_id: str, client_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return False
            client = session.browsers.get(str(client_id))
            if not client or client.disconnected:
                return False
            send_fn = client.send_fn
            pending = list(client.queue)
            client.queue.clear()
        for index, item in enumerate(pending):
            try:
                accepted = send_fn(item)
                if accepted is False:
                    raise BlockingIOError("browser_outbound_full")
            except Exception:
                # Slow/stuck client: hold the browser, ask host to pause reads.
                with self._lock:
                    session = self._sessions.get(str(session_id))
                    if session:
                        client = session.browsers.get(str(client_id))
                        if client:
                            # Preserve the failed frame and everything behind it,
                            # in order. The host route stops receiving until this
                            # queue drains, so no terminal bytes are discarded.
                            client.queue[0:0] = pending[index:]
                            session.backpressured_browsers.add(str(client_id))
                            session.backpressure = True
                return False
        with self._lock:
            session = self._sessions.get(str(session_id))
            if session:
                session.backpressured_browsers.discard(str(client_id))
                session.backpressure = bool(session.backpressured_browsers)
        return True

    def _send_to_browser(self, session_id: str, client_id: str, encoded: bytes) -> None:
        self._enqueue_browser(session_id, client_id, encoded)

    def _drain_host(self, session_id: str) -> bool:
        """Deliver retained browser control frames to the attached executor."""
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session or session.closed:
                return False
            send_fn = session.host_send
            if send_fn is None:
                return False
            pending = list(session.host_queue)
            session.host_queue.clear()
        for index, encoded in enumerate(pending):
            try:
                accepted = send_fn(encoded)
                if accepted is False:
                    raise BlockingIOError("host_outbound_full")
            except Exception:
                with self._lock:
                    session = self._sessions.get(str(session_id))
                    if session and not session.closed:
                        session.host_queue[0:0] = pending[index:]
                return False
        return True


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


def host_attached_for(runner_session_id: str,
                      hub: "RelayHub | None" = None) -> bool | None:
    """WATCH-4 liveness signal: is a host tunnel attached for this session *here*?

    Tri-state, resolved against the in-process RelayHub -- the only authority for
    live attachment:

      * ``True``/``False`` when this process's hub owns the relay session (host
        tunnel present / absent).
      * ``None`` when this process's hub has never seen the session. This is the
        safe fallback for any caller that does not hold the tunnel (the MCP
        process, a second web worker): the watch gate then keeps DB-row inference
        instead of falsely reporting the run detached.

    Cross-process callers read the owning process's state via the
    ``/ixp/v1/runner_sessions/{id}/relay_attachment`` endpoint, which returns this
    same value from the process that terminates the host WebSocket.
    """
    sid = str(runner_session_id or "").strip()
    if not sid:
        return None
    try:
        info = (hub or get_default_hub()).session_info(sid)
    except Exception:
        return None
    if not info:
        return None
    return bool(info.get("host_attached"))
