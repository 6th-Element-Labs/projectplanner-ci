"""HARDEN-79: relay auth failures must terminate in a fault, never a silent loop.

Two separate long-running processes on an agent host can be locked out by a
credential that rotated underneath them, and both used to fail the same way —
retry forever, log nothing an operator ever sees:

* the **executor companion** dials ``/pty/host`` with a server-minted ticket.
  A revoked/expired ticket closes the handshake 401/403 and the client redials.
* the **Agent Host** mints those tickets with its enrolled bearer
  (``PM_MCP_TOKEN``, sourced from the identity file at ``service-run`` time).
  A bearer that predates a rotation 401s every mint, so the companion's refresh
  never produces a fresh ticket and the redial loop cannot converge.

Observed once as 46 consecutive connect->fail cycles with no escalation. This
module supplies the shared pieces: classify a failure as auth vs transport,
count consecutive auth failures, re-read the credential from its on-disk source
before giving up, and emit exactly one typed fault per episode.

Nothing here imports Switchboard server code — it ships inside the host bundle.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

FAULT_SCHEMA = "switchboard.host_relay_auth_fault.v1"
FAULT_REASON = "relay_auth_failed"
# The incident looped 46 times. Five consecutive auth rejections is already far
# past "a ticket expired mid-flight" and well short of "nobody will ever look".
DEFAULT_FAULT_THRESHOLD = 5
# Before the fault: normal reconnect ladder. After it: stop burning the relay.
DEFAULT_BACKOFF_CEILING_S = 8.0
FAULTED_BACKOFF_CEILING_S = 60.0

AUTH_HTTP_STATUSES = frozenset({401, 403})
AUTH_CLOSE_CODES = frozenset({4401, 4403})
AUTH_REASON_MARKERS = (
    "unauthorized", "forbidden", "ticket_revoked", "ticket_expired",
    "host_mismatch", "missing_scope", "denied",
)

FAULT_FILENAME = "relay_auth_fault.json"

# ``switchboard_core._http`` deliberately raises ``RuntimeError("HTTP 401 …")``
# rather than an HTTPError subclass, so the status has to be read back out.
_HTTP_STATUS_RE = re.compile(r"\bhttp[ _]?(\d{3})\b", re.IGNORECASE)


def token_fingerprint(token) -> str:
    """Non-secret 8-hex fingerprint, the same shape the incident report used."""
    value = str(token or "")
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _status_of(exc) -> int:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    rcvd = getattr(exc, "rcvd", None)
    value = getattr(rcvd, "code", None)
    if isinstance(value, int):
        return value
    return 0


def classify_relay_failure(exc) -> str:
    """``"auth"`` when the relay refused the credential, else ``"transport"``.

    Deliberately generous: an auth rejection misread as transport is the bug
    this task exists to kill, while a transport blip misread as auth only costs
    one extra credential re-read.
    """
    status = _status_of(exc)
    if status in AUTH_HTTP_STATUSES or status in AUTH_CLOSE_CODES:
        return "auth"
    text = f"{type(exc).__name__}: {exc}"
    match = _HTTP_STATUS_RE.search(text)
    if match and int(match.group(1)) in AUTH_HTTP_STATUSES:
        return "auth"
    lowered = text.lower()
    if any(marker in lowered for marker in AUTH_REASON_MARKERS):
        return "auth"
    return "transport"


def credential_source() -> dict:
    """Where this process could re-read its bearer from, if anywhere.

    ``service-run`` exports ``PM_AGENT_HOST_IDENTITY_PATH`` alongside the bearer
    it read out of that same file, so an enrolled host can always heal from
    disk. A host started straight from a shell has only its spawn-time env, and
    that distinction is exactly what the fault has to tell the operator.
    """
    path = str(os.environ.get("PM_AGENT_HOST_IDENTITY_PATH") or "").strip()
    if path:
        return {"kind": "identity_file", "path": path,
                "restart_required": False}
    return {"kind": "spawn_env", "path": "", "restart_required": True}


def read_source_token(source=None) -> str:
    source = source or credential_source()
    if source.get("kind") != "identity_file":
        return ""
    try:
        payload = json.loads(
            Path(source["path"]).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("host_token") or "")


def reload_credential(env=None) -> dict:
    """Re-read the bearer from disk; adopt it when it differs from the live one.

    Returns a receipt that names the source either way — a reload that found
    nothing new is the evidence that a restart (or a real re-key) is required,
    so it must never be swallowed.
    """
    env = os.environ if env is None else env
    source = credential_source()
    current = str(env.get("PM_MCP_TOKEN") or "")
    receipt = {
        "source": source["kind"],
        "source_path": source.get("path") or "",
        "current_fingerprint": token_fingerprint(current),
        "source_fingerprint": "",
        "changed": False,
        "adopted": False,
        "restart_required": bool(source.get("restart_required")),
    }
    fresh = read_source_token(source)
    if not fresh:
        # No readable on-disk source: the live bearer is the only one there is.
        receipt["restart_required"] = True
        return receipt
    receipt["source_fingerprint"] = token_fingerprint(fresh)
    if fresh == current:
        # The disk copy is the stale one too — restarting would change nothing;
        # say so instead of implying a restart heals it.
        receipt["restart_required"] = True
        return receipt
    env["PM_MCP_TOKEN"] = fresh
    receipt.update({"changed": True, "adopted": True, "restart_required": False})
    return receipt


class RelayAuthFaultTracker:
    """Consecutive-auth-failure counter that escalates once and then backs off.

    One instance per relay client. ``record_failure`` returns the decision the
    caller must act on; ``record_success`` ends the episode. Exactly one fault
    is produced per episode no matter how long the failures continue.
    """

    def __init__(self, *, threshold: int = DEFAULT_FAULT_THRESHOLD,
                 on_fault=None, reload_credential=reload_credential,
                 label: str = "", clock=time.time):
        self.threshold = max(1, int(threshold))
        self.on_fault = on_fault
        self._reload_credential = reload_credential
        self.label = str(label or "")
        self._clock = clock
        self.consecutive_failures = 0
        self.first_failure_at = 0.0
        self.last_reason = ""
        self.faulted = False
        self.fault = None
        self.credential_receipt = None

    @property
    def backoff_ceiling_s(self) -> float:
        return (FAULTED_BACKOFF_CEILING_S if self.faulted
                else DEFAULT_BACKOFF_CEILING_S)

    def record_success(self) -> dict:
        healed = self.faulted or self.consecutive_failures > 0
        self.consecutive_failures = 0
        self.first_failure_at = 0.0
        self.last_reason = ""
        self.faulted = False
        self.fault = None
        self.credential_receipt = None
        return {"kind": "connected", "healed": healed,
                "backoff_ceiling_s": self.backoff_ceiling_s}

    def record_failure(self, kind: str, detail: str = "") -> dict:
        """Record one connect->fail cycle and say what the caller should do."""
        now = float(self._clock())
        decision = {
            "kind": kind,
            "detail": str(detail or ""),
            "consecutive_failures": self.consecutive_failures,
            "first_failure_at": self.first_failure_at or 0.0,
            "credential_reload": None,
            "fault": None,
            "escalated": False,
            "backoff_ceiling_s": self.backoff_ceiling_s,
        }
        if kind != "auth":
            # A transport blip neither counts toward the fault nor clears an
            # episode already in progress; only a real connect does that.
            return decision
        if self.consecutive_failures == 0:
            self.first_failure_at = now
        self.consecutive_failures += 1
        self.last_reason = str(detail or "")
        decision["consecutive_failures"] = self.consecutive_failures
        decision["first_failure_at"] = self.first_failure_at
        if self.faulted:
            decision["fault"] = self.fault
            decision["backoff_ceiling_s"] = self.backoff_ceiling_s
            return decision
        if self.consecutive_failures < self.threshold:
            decision["backoff_ceiling_s"] = self.backoff_ceiling_s
            return decision
        # Threshold reached: try the on-disk credential before declaring a
        # fault, so a rotation that already landed heals without an operator.
        receipt = None
        if self._reload_credential is not None:
            try:
                receipt = self._reload_credential()
            except Exception as exc:  # noqa: BLE001 — a broken source is a finding
                receipt = {"source": "unreadable", "changed": False,
                           "adopted": False, "restart_required": True,
                           "error": f"{type(exc).__name__}: {exc}"}
        self.credential_receipt = receipt
        decision["credential_reload"] = receipt
        if receipt is not None and receipt.get("adopted"):
            # Self-healed: the next dial uses the rotated credential. Reset so a
            # genuinely dead credential still faults on its own N cycles.
            self.consecutive_failures = 0
            self.first_failure_at = 0.0
            decision["healed"] = True
            decision["backoff_ceiling_s"] = DEFAULT_BACKOFF_CEILING_S
            return decision
        self.faulted = True
        self.fault = self.build_fault(now, receipt)
        decision["fault"] = self.fault
        decision["escalated"] = True
        decision["backoff_ceiling_s"] = self.backoff_ceiling_s
        if self.on_fault is not None:
            try:
                self.on_fault(self.fault)
            except Exception:  # noqa: BLE001 — publishing must not kill the client
                pass
        return decision

    def build_fault(self, now: float, receipt) -> dict:
        receipt = receipt or {}
        restart_required = bool(receipt.get("restart_required", True))
        source = str(receipt.get("source") or credential_source()["kind"])
        return {
            "schema": FAULT_SCHEMA,
            "reason": FAULT_REASON,
            "label": self.label,
            "attempt_count": self.consecutive_failures,
            "first_failure_at": self.first_failure_at,
            "faulted_at": now,
            "detail": self.last_reason,
            "credential_source": source,
            "credential_fingerprint": str(receipt.get("current_fingerprint") or ""),
            "restart_required": restart_required,
            "remediation": (
                "restart required: this process holds a spawn-time credential "
                "the on-disk source cannot refresh"
                if restart_required else
                "credential reloaded from disk; retrying"),
        }


def fault_path(session_dir) -> Path:
    return Path(session_dir) / FAULT_FILENAME


def publish_fault(session_dir, fault: dict) -> bool:
    """Hand the fault to the credential-owning Agent Host via the session dir.

    The companion has no coordination bearer of its own, so it cannot report to
    Switchboard directly — the same one-way file hop ``host_relay.refresh``
    already uses. Best-effort: a fault that cannot be written still gets logged
    by the caller.
    """
    try:
        path = fault_path(session_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fault, sort_keys=True), encoding="utf-8")
        return True
    except OSError:
        return False


def consume_fault(session_dir):
    """Read and clear a companion-published fault. Returns ``None`` when absent."""
    path = fault_path(session_dir)
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return payload if isinstance(payload, dict) and payload else None
