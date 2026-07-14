"""Coordination repository (ARCH-MS-33).

Owns wake intents, coordination monitors, unblock requests, and agent
messaging previously planned for ``coordination_store.py`` /
``messaging_store.py``. Cross-cutting helpers (hosts/agents, idempotency,
external effects, runner session upsert) stay on the store facade and are
reached via ``_store_facade()``. ``store.py`` re-exports these symbols; root
``coordination_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.connection import _conn
from db.core import _json_obj  # noqa: F401
from switchboard.domain.coordination.delivery import (
    build_message_delivery_receipt,
    classify_agent_delivery,
)
from switchboard.domain.coordination.placement import (
    claim_decision,
    plan_hybrid_placement,
)
from switchboard.domain.coordination.terminal import TERMINAL_WAKE_STATUSES
from switchboard.domain.ixp.protocol import (
    PROTOCOL_ENVELOPE,
    check_protocol_compatibility,
    normalize_send_ack_deadline,
    protocol_envelope,
)
from switchboard.domain.provider_credentials import CredentialPrincipal
from switchboard.storage.repositories.provider_capacity import (
    PROVIDER_CAPACITY_DECISION_SCHEMA,
    default_provider_capacity_repository,
)
from switchboard.storage.repositories.provider_credentials import (
    CredentialVaultError,
    default_provider_credential_repository,
)


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store

def _wake_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["selector"] = _json_obj(d.pop("selector_json", "{}"), {})
    d["policy"] = _json_obj(d.pop("policy_json", "{}"), {})
    d["result"] = _json_obj(d.pop("result_json", "{}"), {})
    d["placement"] = _json_obj(d.pop("placement_json", "{}"), {})
    return d


def _host_rows_in(c: sqlite3.Connection, now: float) -> List[Dict[str, Any]]:
    rows = c.execute("SELECT * FROM agent_hosts ORDER BY heartbeat_at DESC").fetchall()
    return [_store_facade()._host_row(row, now=now) for row in rows]


def _placement_reservations_in(c: sqlite3.Connection) -> Dict[str, int]:
    reservations: Dict[str, int] = {}
    rows = c.execute(
        "SELECT placement_json FROM wake_intents WHERE status IN ('pending','claimed')"
    ).fetchall()
    for row in rows:
        placement = _json_obj(row["placement_json"], {})
        host_id = str(placement.get("selected_host_id") or "")
        if host_id:
            reservations[host_id] = reservations.get(host_id, 0) + 1
    return reservations


def _audit_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Keep placement activity useful without copying credential/account identifiers."""
    binding = policy.get("account_binding") or {}
    placement = policy.get("placement") or {}
    resources = placement.get("resources") or {}
    scheduler = policy.get("scheduler") or {}
    return {
        "mode": policy.get("mode"),
        "scheduler": {
            key: scheduler.get(key) for key in (
                "mode", "prefer_persistent", "allow_persistent", "allow_ephemeral",
                "burst_enabled", "max_host_loss_reschedules", "fair_share_key",
            ) if scheduler.get(key) is not None
        },
        "placement": {
            "canonical_repo": placement.get("canonical_repo"),
            "session_policy": placement.get("session_policy"),
            "isolation": placement.get("isolation"),
            "runtime_binaries": placement.get("runtime_binaries") or [],
            "resources": {
                key: resources.get(key) for key in ("cpu", "memory_mb", "disk_gb")
                if resources.get(key) is not None
            },
        },
        "allow_on_demand": policy.get("allow_on_demand") is True,
        "account_binding": {
            "present": bool(binding),
            "provider": binding.get("provider"),
            "tenant_bound": bool(binding.get("tenant_id")),
            "account_affinity_bound": bool(binding.get("account_affinity_id")),
            "credential_lease_bound": bool(binding.get("credential_lease_id")),
        },
    }


def _provider_capacity_decision(
    policy: Dict[str, Any], *, task_id: str, project: str,
    host_id: str = "", runner_session_id: str = "",
    exclude_lease_id: str = "", require_execution_binding: bool = False,
) -> Dict[str, Any]:
    """Read CO-8 admission without persisting provider/account identifiers to activity."""
    binding = dict(policy.get("account_binding") or {})
    if not binding:
        return {}
    exact = {
        **binding,
        "project": project,
        "task_id": task_id,
        "host_id": host_id or binding.get("host_id") or "",
        "runner_session_id": runner_session_id or binding.get("runner_session_id") or "",
    }
    try:
        return default_provider_capacity_repository.admission_decision(
            exact,
            task_policy={
                "customer_user_id": binding.get("user_id"),
                "requested_provider": binding.get("provider"),
                "allow_provider_substitution": False,
            },
            lane_policy=policy.get("provider_lane_policy") or {},
            host_available=True,
            require_execution_binding=require_execution_binding,
            exclude_lease_id=exclude_lease_id,
        )
    except CredentialVaultError as exc:
        return {
            "schema": PROVIDER_CAPACITY_DECISION_SCHEMA,
            "allowed": False,
            "state": "policy_blocked",
            "reason_code": exc.code,
        }


def _credential_lease_decision(
    policy: Dict[str, Any], *, task_id: str, project: str, host_id: str,
    runner_session_id: str, credential_lease_id: str,
) -> Dict[str, Any]:
    binding = dict(policy.get("account_binding") or {})
    return default_provider_credential_repository.lease_admission_decision(
        credential_lease_id,
        project=project,
        credential_reference=binding.get("credential_reference") or "",
        user_id=binding.get("user_id") or "",
        provider=binding.get("provider") or "",
        provider_account_id=binding.get("provider_account_id") or "",
        task_id=task_id,
        host_id=host_id,
        runner_session_id=runner_session_id,
        work_session_id=binding.get("work_session_id") or "",
    )


def _release_lost_credential_lease(policy: Dict[str, Any], *, project: str) -> bool:
    binding = dict(policy.get("account_binding") or {})
    lease_id = str(binding.get("credential_lease_id") or "")
    if not lease_id:
        return True
    principal = CredentialPrincipal.from_mapping({
        "principal_id": "switchboard/wake",
        "principal_kind": "system",
        "scopes": ["use:credentials"],
    })
    try:
        released = default_provider_credential_repository.release_lease(
            lease_id, project=project, actor="switchboard/wake",
            reason="host_lost", principal=principal,
        )
    except CredentialVaultError:
        return False
    return str(released.get("state") or "") in {"released", "expired", "fenced"}


def _clear_execution_credential_binding(policy: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(policy)
    binding = dict(updated.get("account_binding") or {})
    for key in ("host_id", "runner_session_id", "credential_lease_id"):
        binding.pop(key, None)
    if binding:
        updated["account_binding"] = binding
    return updated

def _insert_wake_intent(c: sqlite3.Connection, selector: Dict[str, Any],
                        reason: str, source: str, policy: Dict[str, Any],
                        task_id: Optional[str], principal_id: str, actor: str,
                        now: float, project: str, idem_key: str = "",
                        effect_key: str = "") -> Dict[str, Any]:
    deadline_s = (policy.get("deadline_seconds") or policy.get("claim_timeout_s") or
                  policy.get("ttl_s"))
    deadline = now + float(deadline_s) if deadline_s else None
    hybrid = str((policy.get("scheduler") or {}).get("mode") or "") == "hybrid"
    placement: Dict[str, Any] = {}
    if hybrid:
        hosts = _host_rows_in(c, now)
        placement = plan_hybrid_placement(
            hosts, selector, policy, project=project,
            reserved_by_host=_placement_reservations_in(c),
        )
        eligible_ids = {
            item.get("host_id") for item in placement.get("candidates") or []
            if item.get("eligible")
        }
        eligible = [host for host in hosts if host.get("host_id") in eligible_ids]
    else:
        eligible = _store_facade()._eligible_hosts_in(c, selector, now)
    no_host_policy = (policy.get("no_eligible_host") or "wait").strip()
    burst_pending = placement.get("action") == "provision_ephemeral"
    placement_denied = placement.get("action") == "deny"
    status = ("failed" if placement_denied
              or (no_host_policy == "fail" and not eligible and not burst_pending)
              else "pending")
    result = ({
        "reason": (placement.get("reason_code") if placement_denied else "no_eligible_host"),
        "eligible_host_count": 0,
    } if status == "failed" else {})
    wake_id = "wake-" + uuid.uuid4().hex[:16]
    c.execute(
        "INSERT INTO wake_intents(wake_id, source, reason, selector_json, policy_json, "
        "status, requested_at, deadline, result_json, placement_json, task_id, principal_id, "
        "idem_key, effect_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (wake_id, source, reason, json.dumps(selector, sort_keys=True),
         json.dumps(policy, sort_keys=True), status, now, deadline,
         json.dumps(result, sort_keys=True), json.dumps(placement, sort_keys=True),
         task_id, principal_id or None,
         idem_key or None, effect_key or None),
    )
    payload = {"wake_id": wake_id, "source": source, "reason": reason,
               "selector": selector, "policy": _audit_policy(policy), "status": status,
               "placement": placement,
               "eligible_host_count": len(eligible), "effect_key": effect_key or None}
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id, actor, "wake.requested", json.dumps(payload, sort_keys=True), now))
    if not eligible:
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "wake.no_eligible_host",
                   json.dumps({"wake_id": wake_id, "selector": selector,
                               "status": status}, sort_keys=True), now))
    if placement:
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (task_id, actor, "wake.placement_decided",
             json.dumps({"wake_id": wake_id, "placement": placement}, sort_keys=True), now),
        )
    row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    wake = _wake_row(row)
    wake["eligible_host_count"] = len(eligible)
    wake["eligible_hosts"] = [h["host_id"] for h in eligible]
    return wake

def request_wake(selector: Dict[str, Any], reason: str = "",
                 source: str = "", policy: Optional[Dict[str, Any]] = None,
                 task_id: Optional[str] = None, principal_id: str = "",
                 actor: str = "system", idem_key: str = "",
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    policy = dict(policy or {})
    selector = dict(selector or {})
    if not selector.get("runtime") and selector.get("agent_id"):
        runtime = _store_facade()._selector_runtime_for_agent(str(selector.get("agent_id") or ""))
        if runtime:
            selector["runtime"] = runtime
    if not selector.get("runtime") and not selector.get("agent_id"):
        return {"error": "selector.runtime or selector.agent_id required"}
    payload = {"selector": selector, "reason": reason or "wake requested",
               "source": source or actor, "policy": dict(policy), "task_id": task_id}
    if (str((policy.get("scheduler") or {}).get("mode") or "") == "hybrid"
            and policy.get("account_binding")):
        policy["provider_capacity"] = _provider_capacity_decision(
            policy, task_id=str(task_id or ""), project=project,
            require_execution_binding=False,
        )
    try:
        with _store_facade()._control_plane_conn(project) as c:
            hit = _store_facade()._idem_hit(c, "request_wake", idem_key, actor, payload)
            if hit is not None:
                return hit
            effect_claim = _store_facade()._claim_external_effect_in(
                c, "wake", "agent_host", json.dumps(selector, sort_keys=True),
                payload, task_id=task_id, agent_id=selector.get("agent_id") or "",
                idem_key=idem_key, actor=actor, principal_id=principal_id,
                project=project, now=now)
            if not effect_claim.get("claimed"):
                out = {"requested": False, "reason": effect_claim.get("reason"),
                       "effect": effect_claim.get("effect"),
                       "effect_key": effect_claim.get("effect_key"),
                       "readback_required": effect_claim.get("readback_required", False)}
                if effect_claim.get("verified"):
                    out["verified"] = True
                    out["proof"] = effect_claim.get("proof")
                _store_facade()._idem_store(c, "request_wake", idem_key, actor, payload, out)
                return out
            wake = _insert_wake_intent(
                c, selector=selector, reason=reason or "wake requested",
                source=source or actor, policy=policy, task_id=task_id,
                principal_id=principal_id, actor=actor, now=now, idem_key=idem_key,
                effect_key=effect_claim["effect_key"], project=project)
            _store_facade()._update_external_effect_in(
                c, effect_claim["effect_key"], "issued",
                readback={"wake_id": wake["wake_id"], "wake_status": wake["status"]},
                actor=actor, task_id=task_id, project=project, now=now)
            wake["effect_key"] = effect_claim["effect_key"]
            _store_facade()._idem_store(c, "request_wake", idem_key, actor, payload, wake)
            return wake
    except sqlite3.OperationalError as exc:
        if _store_facade()._sqlite_busy(exc):
            return _store_facade()._control_plane_unavailable("request_wake", project, started_at, exc)
        raise

def list_wake_intents(status: str = "", host_id: str = "", runtime: str = "",
                      project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    started_at = time.time()
    q = "SELECT * FROM wake_intents WHERE 1=1"
    params: List[Any] = []
    if status:
        q += " AND status=?"; params.append(status)
    if host_id:
        q += " AND claimed_by_host=?"; params.append(host_id)
    q += " ORDER BY requested_at"
    try:
        with _store_facade()._control_plane_conn(project) as c:
            wakes = [_wake_row(r) for r in c.execute(q, params).fetchall()]
    except sqlite3.OperationalError as exc:
        if _store_facade()._sqlite_busy(exc):
            return [_store_facade()._control_plane_unavailable("list_wake_intents", project, started_at, exc)]
        raise
    if runtime:
        wakes = [w for w in wakes if (w.get("selector") or {}).get("runtime") == runtime]
    return wakes

def claim_wake(host_id: str, wake_id: str, actor: str = "system",
               project: str = DEFAULT_PROJECT, runner_session_id: str = "",
               credential_lease_id: str = "") -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    try:
        with _store_facade()._control_plane_conn(project) as c:
            wake_row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
            if not wake_row:
                return {"claimed": False, "error": "wake not found", "wake_id": wake_id}
            wake = _wake_row(wake_row)
            if wake["status"] != "pending":
                return {"claimed": False, "reason": f"wake is {wake['status']}", "wake": wake}
            if wake.get("deadline") and wake["deadline"] <= now:
                result = {"reason": "deadline_expired", "deadline": wake["deadline"]}
                c.execute("UPDATE wake_intents SET status='failed', completed_at=?, result_json=? "
                          "WHERE wake_id=?",
                          (now, json.dumps(result, sort_keys=True), wake_id))
                return {"claimed": False, "reason": "deadline_expired", "wake_id": wake_id}
            host_row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
            if not host_row:
                return {"claimed": False, "reason": "host_not_registered", "host_id": host_id}
            host = _store_facade()._host_row(host_row, now=now)
            policy = dict(wake.get("policy") or {})
            binding = dict(policy.get("account_binding") or {})
            placement_claim = claim_decision(
                host, wake, project=project, credential_rebound=bool(binding),
            )
            if not placement_claim.get("allowed"):
                return {"claimed": False, "reason": "host_not_eligible",
                        "reason_codes": (placement_claim.get("candidate") or {}).get(
                            "reason_codes") or [],
                        "host_id": host_id, "wake_id": wake_id}
            if binding:
                runner_id = str(runner_session_id or "").strip()
                lease_id = str(credential_lease_id or "").strip()
                if not runner_id or not lease_id:
                    missing = []
                    if not runner_id:
                        missing.append("runner_session_required_for_credential_lease")
                    if not lease_id:
                        missing.append("credential_lease_required")
                    return {"claimed": False, "reason": "credential_admission_denied",
                            "reason_codes": missing, "host_id": host_id,
                            "wake_id": wake_id}
                lease_decision = _credential_lease_decision(
                    policy, task_id=str(wake.get("task_id") or ""), project=project,
                    host_id=host_id, runner_session_id=runner_id,
                    credential_lease_id=lease_id,
                )
                if not lease_decision.get("allowed"):
                    return {"claimed": False, "reason": "credential_admission_denied",
                            "reason_codes": [lease_decision.get("reason_code")],
                            "host_id": host_id, "wake_id": wake_id}
                capacity = _provider_capacity_decision(
                    policy, task_id=str(wake.get("task_id") or ""), project=project,
                    host_id=host_id, runner_session_id=runner_id,
                    exclude_lease_id=lease_id, require_execution_binding=True,
                )
                if not capacity.get("allowed"):
                    return {"claimed": False, "reason": "provider_capacity_denied",
                            "reason_codes": [capacity.get("reason_code")],
                            "host_id": host_id, "wake_id": wake_id}
                binding.update({
                    "host_id": host_id,
                    "runner_session_id": runner_id,
                    "credential_lease_id": lease_id,
                })
                policy["account_binding"] = binding
                policy["provider_capacity"] = capacity
                wake["policy"] = policy
            placement = dict(wake.get("placement") or {})
            if placement.get("scheduler_mode") == "hybrid":
                candidate = placement_claim.get("candidate") or {}
                placement.update({
                    "action": f"claimed_{candidate.get('host_class') or 'host'}",
                    "reason_code": "eligible_host_claimed",
                    "selected_host_id": host_id,
                    "selected_host_class": candidate.get("host_class"),
                    "cost_class": candidate.get("cost_class"),
                    "claimed_at": now,
                    "credential_rebind_required": False,
                    "credential_lease_state": (
                        "issued" if binding else placement.get("credential_lease_state")
                    ),
                })
            cur = c.execute(
                "UPDATE wake_intents SET status='claimed', claimed_at=?, claimed_by_host=?, "
                "placement_json=?, policy_json=? "
                "WHERE wake_id=? AND status='pending'",
                (now, host_id, json.dumps(placement, sort_keys=True),
                 json.dumps(policy, sort_keys=True), wake_id),
            )
            if cur.rowcount == 0:
                row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
                return {"claimed": False, "reason": "lost_race", "wake": _wake_row(row)}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (wake.get("task_id"), actor, "wake.claimed",
                       json.dumps({"wake_id": wake_id, "host_id": host_id}, sort_keys=True), now))
            if placement.get("scheduler_mode") == "hybrid":
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (wake.get("task_id"), actor, "wake.placement_claimed",
                     json.dumps({"wake_id": wake_id, "placement": placement}, sort_keys=True),
                     now),
                )
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if _store_facade()._sqlite_busy(exc):
            err = _store_facade()._control_plane_unavailable("claim_wake", project, started_at, exc)
            return {"claimed": False, **err}
        raise
    return {"claimed": True, "wake": _wake_row(row)}

def complete_wake(wake_id: str, runner_session_id: str = "",
                  agent_id: str = "", result: Optional[Dict[str, Any]] = None,
                  actor: str = "system",
                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    result = dict(result or {})
    success = bool(result.get("started") or runner_session_id or agent_id)
    status = "completed" if success else "failed"
    if "reason" not in result:
        result["reason"] = "started" if success else "launch_failed"
    try:
        with _store_facade()._control_plane_conn(project) as c:
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
            if not row:
                return {"error": "wake not found", "wake_id": wake_id}
            wake = _wake_row(row)
            c.execute(
                "UPDATE wake_intents SET status=?, completed_at=?, runner_session_id=?, "
                "agent_id=?, result_json=? WHERE wake_id=?",
                (status, now, runner_session_id or None, agent_id or None,
                 json.dumps(result, sort_keys=True), wake_id),
            )
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (wake.get("task_id"), actor,
                       "wake.completed" if status == "completed" else "wake.failed",
                       json.dumps({"wake_id": wake_id, "status": status,
                                   "runner_session_id": runner_session_id or None,
                                   "agent_id": agent_id or None,
                                   "result": result}, sort_keys=True), now))
            if status == "completed" and runner_session_id:
                selector = wake.get("selector") or {}
                runner_metadata = {"wake_id": wake_id, "wake_result": result}
                for key in ("vendor_id", "provider_session_id", "session_url", "branch",
                            "head_sha", "billing_mode"):
                    if result.get(key) is not None:
                        runner_metadata[key] = result.get(key)
                _store_facade()._upsert_runner_session_in(
                    c,
                    {
                        "runner_session_id": runner_session_id,
                        "host_id": wake.get("claimed_by_host") or "",
                        "agent_id": agent_id or selector.get("agent_id") or "",
                        "runtime": selector.get("runtime") or "",
                        "task_id": wake.get("task_id") or result.get("task_id") or "",
                        "claim_id": result.get("claim_id") or "",
                        "pid": result.get("pid"),
                        "status": "running" if result.get("started") else "unknown",
                        "cwd": result.get("cwd") or "",
                        "control": result.get("control") or {"managed_process": True,
                                                              "runner_kill": bool(runner_session_id)},
                        "metadata": runner_metadata,
                        "heartbeat_ttl_s": result.get("heartbeat_ttl_s") or 60,
                    },
                    principal_id=actor,
                    actor=actor,
                    now=now,
                )
            if wake.get("effect_key"):
                effect_readback = {"wake_id": wake_id, "status": status,
                                   "runner_session_id": runner_session_id or None,
                                   "agent_id": agent_id or None, "result": result}
                _store_facade()._update_external_effect_in(
                    c, wake["effect_key"],
                    "verified" if status == "completed" else "failed",
                    readback=effect_readback,
                    last_error="" if status == "completed" else result.get("reason", "launch_failed"),
                    actor=actor, task_id=wake.get("task_id"), project=project, now=now)
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if _store_facade()._sqlite_busy(exc):
            return _store_facade()._control_plane_unavailable("complete_wake", project, started_at, exc)
        raise
    return _wake_row(row)

def cancel_wake(wake_id: str, reason: str = "cancelled", actor: str = "system",
                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    try:
        with _store_facade()._control_plane_conn(project) as c:
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
            if not row:
                return {"error": "wake not found", "wake_id": wake_id}
            wake = _wake_row(row)
            if wake["status"] in ("completed", "failed", "cancelled"):
                return wake | {"note": "already terminal"}
            result = dict(wake.get("result") or {})
            result.update({"reason": reason, "cancelled_by": actor})
            c.execute("UPDATE wake_intents SET status='cancelled', completed_at=?, result_json=? "
                      "WHERE wake_id=?",
                      (now, json.dumps(result, sort_keys=True), wake_id))
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (wake.get("task_id"), actor, "wake.cancelled",
                       json.dumps({"wake_id": wake_id, "reason": reason}, sort_keys=True), now))
            if wake.get("effect_key"):
                _store_facade()._update_external_effect_in(
                    c, wake["effect_key"], "void",
                    readback={"wake_id": wake_id, "status": "cancelled", "reason": reason},
                    actor=actor, task_id=wake.get("task_id"), project=project, now=now)
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if _store_facade()._sqlite_busy(exc):
            return _store_facade()._control_plane_unavailable("cancel_wake", project, started_at, exc)
        raise
    return _wake_row(row)

def sweep_wake_intents(project: str = DEFAULT_PROJECT,
                       now: Optional[float] = None) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time() if now is None else float(now)
    failed = 0
    requeued = 0
    events: List[Dict[str, Any]] = []
    try:
        with _store_facade()._control_plane_conn(project) as c:
            rows = c.execute(
                "SELECT * FROM wake_intents WHERE status IN ('pending','claimed') "
                "AND deadline IS NOT NULL AND deadline<=?",
                (now,),
            ).fetchall()
            for row in rows:
                wake = _wake_row(row)
                result = dict(wake.get("result") or {})
                result.update({"reason": "deadline_expired", "deadline": wake.get("deadline")})
                c.execute("UPDATE wake_intents SET status='failed', completed_at=?, result_json=? "
                          "WHERE wake_id=?",
                          (now, json.dumps(result, sort_keys=True), wake["wake_id"]))
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (wake.get("task_id"), "switchboard/wake", "wake.failed",
                           json.dumps({"wake_id": wake["wake_id"], "reason": "deadline_expired"},
                                      sort_keys=True), now))
                if wake.get("effect_key"):
                    _store_facade()._update_external_effect_in(
                        c, wake["effect_key"], "failed",
                        readback={"wake_id": wake["wake_id"], "status": "failed",
                                  "reason": "deadline_expired"},
                        last_error="deadline_expired",
                        actor="switchboard/wake", task_id=wake.get("task_id"),
                        project=project, now=now)
                failed += 1
                events.append({"wake_id": wake["wake_id"], "status": "failed",
                               "reason": "deadline_expired"})

            recovery_rows = c.execute(
                "SELECT * FROM wake_intents WHERE status IN ('pending','claimed') "
                "AND (deadline IS NULL OR deadline>?)",
                (now,),
            ).fetchall()
            for row in recovery_rows:
                wake = _wake_row(row)
                placement = dict(wake.get("placement") or {})
                if placement.get("scheduler_mode") != "hybrid":
                    continue
                original_status = str(wake.get("status") or "")
                host_id = str(
                    wake.get("claimed_by_host")
                    or placement.get("selected_host_id")
                    or ""
                )
                if not host_id:
                    continue
                host_row = c.execute(
                    "SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)
                ).fetchone() if host_id else None
                host = _store_facade()._host_row(host_row, now=now) if host_row else None
                if host and not host.get("stale"):
                    continue

                scheduler = dict((wake.get("policy") or {}).get("scheduler") or {})
                recovery_count = int(placement.get("host_loss_recovery_count") or 0)
                max_recoveries = max(0, int(scheduler.get("max_host_loss_reschedules") or 3))
                if recovery_count >= max_recoveries:
                    result = dict(wake.get("result") or {})
                    result["recovery"] = {
                        "reason": "host_loss_recovery_exhausted",
                        "lost_host_id": host_id,
                        "attempts": recovery_count,
                    }
                    c.execute(
                        "UPDATE wake_intents SET status='failed', completed_at=?, result_json=? "
                        "WHERE wake_id=? AND status=?",
                        (now, json.dumps(result, sort_keys=True), wake["wake_id"],
                         original_status),
                    )
                    failed += 1
                    events.append({"wake_id": wake["wake_id"], "status": "failed",
                                   "reason": "host_loss_recovery_exhausted"})
                    continue

                policy = dict(wake.get("policy") or {})
                account_bound = bool(policy.get("account_binding"))
                if (original_status == "claimed" and account_bound
                        and not _release_lost_credential_lease(policy, project=project)):
                    events.append({
                        "wake_id": wake["wake_id"], "status": "claimed",
                        "reason": "credential_lease_fence_failed",
                        "lost_host_id": host_id,
                    })
                    continue
                policy = _clear_execution_credential_binding(policy)
                if account_bound:
                    policy["provider_capacity"] = _provider_capacity_decision(
                        policy, task_id=str(wake.get("task_id") or ""), project=project,
                        require_execution_binding=False,
                    )
                hosts = _host_rows_in(c, now)
                recovered = plan_hybrid_placement(
                    hosts, wake.get("selector") or {}, policy,
                    project=project, reserved_by_host=_placement_reservations_in(c),
                )
                recovered.update({
                    "host_loss_recovery_count": recovery_count + 1,
                    "lost_host_id": host_id,
                    "requeued_at": now,
                    "checkpoint_required": original_status == "claimed",
                    "workspace_reconstruction": "switchboard_claim_plus_git_provenance",
                    "credential_rebind_required": account_bound,
                })
                updated = c.execute(
                    "UPDATE wake_intents SET status='pending', claimed_at=NULL, "
                    "claimed_by_host=NULL, placement_json=?, policy_json=? "
                    "WHERE wake_id=? AND status=?",
                    (json.dumps(recovered, sort_keys=True),
                     json.dumps(policy, sort_keys=True), wake["wake_id"],
                     original_status),
                )
                if updated.rowcount == 0:
                    continue
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (wake.get("task_id"), "switchboard/wake", "wake.placement_recovered",
                     json.dumps({"wake_id": wake["wake_id"],
                                 "placement": recovered}, sort_keys=True), now),
                )
                requeued += 1
                events.append({"wake_id": wake["wake_id"], "status": "pending",
                               "reason": (
                                   "claimed_host_lost" if original_status == "claimed"
                                   else "pending_host_lost"
                               ), "lost_host_id": host_id})
    except sqlite3.OperationalError as exc:
        if _store_facade()._sqlite_busy(exc):
            err = _store_facade()._control_plane_unavailable("sweep_wake_intents", project, started_at, exc)
            return {"project": project, "failed": failed, "events": events, **err}
        raise
    return {"project": project, "failed": failed, "requeued": requeued, "events": events}

def request_unblock(requesting_agent: str, blocking_task_id: str,
                    blocked_task_id: str, message: str,
                    owner_agent: str, ack_deadline_minutes: int = 60,
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Send a blocking dep request: agent on blocked_task_id asks owner_agent (working
    on blocking_task_id) to unblock. Returns message record with id to poll via
    get_message_status. Records the request as a 'dep_request' activity on both tasks."""
    payload = (f"[DEP REQUEST] Agent {requesting_agent} is blocked on {blocking_task_id} "
               f"while working on {blocked_task_id}. {message}")
    msg = send_agent_message(requesting_agent, owner_agent, payload,
                             task_id=blocked_task_id,
                             requires_ack=True,
                             ack_deadline_minutes=ack_deadline_minutes,
                             project=project)
    # Activity trail on both tasks
    for tid in (blocked_task_id, blocking_task_id):
        _store_facade().add_comment(tid, requesting_agent,
                    f"Unblock request sent to {owner_agent} re {blocking_task_id}: {message[:120]}",
                    kind="dep_request", project=project)
    return {"request_id": msg["id"], "from": requesting_agent, "to": owner_agent,
            "blocking_task_id": blocking_task_id, "blocked_task_id": blocked_task_id,
            "poll_with": "get_message_status"}

def list_unblock_requests(owner_agent: str,
                          project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Return unacked blocking dep requests directed to this agent."""
    msgs = list_unacked_messages(owner_agent, project=project)
    return [m for m in msgs if "[DEP REQUEST]" in (m.get("message") or "")]

def send_agent_message(from_agent: str, to_agent: str, message: str,
                       task_id: Optional[str] = None, requires_ack: bool = False,
                       ack_deadline_minutes: Optional[int] = None,
                       ack_timeout_seconds: Optional[float] = None,
                       ack_timeout_s: Optional[float] = None,
                       signal: Optional[str] = None, priority: int = 0,
                       on_ack_timeout: str = "notify_sender",
                       principal_id: str = "", idem_key: str = "",
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Send a directed message from one agent to another. Returns the message record."""
    now = time.time()
    ack_deadline_minutes = normalize_send_ack_deadline(
        ack_deadline_minutes=ack_deadline_minutes,
        ack_timeout_seconds=ack_timeout_seconds,
        ack_timeout_s=ack_timeout_s,
    )
    deadline = (now + ack_deadline_minutes * 60) if ack_deadline_minutes else None
    payload = {"from_agent": from_agent, "to_agent": to_agent, "message": message,
               "task_id": task_id, "requires_ack": requires_ack,
               "ack_deadline_minutes": ack_deadline_minutes,
               "ack_timeout_seconds": ack_timeout_seconds,
               "ack_timeout_s": ack_timeout_s,
               "signal": signal, "priority": priority,
               "on_ack_timeout": on_ack_timeout}
    with _conn(project) as c:
        hit = _store_facade()._idem_hit(c, "send", idem_key, from_agent, payload)
        if hit is not None:
            return hit
        delivery = _store_facade()._agent_delivery_state(c, to_agent, now)
        identity_state = (_store_facade()._task_identity_state_in(c, task_id, now)
                          if task_id else {"status": "clear", "takeover_safe": True})
        if (not delivery.get("reachable") and
                identity_state.get("status") == "unbound_live_runtime_possible"):
            delivery = dict(delivery)
            delivery.update({
                "status": "identity_unbound",
                "reason": "not_registered_but_recent_unbound_activity",
                "identity": identity_state,
                "takeover_safe": False,
                "message": (
                    "Target agent_id is not registered, but this task has recent "
                    "unbound activity. The runtime may be live outside Switchboard "
                    "identity binding; require re-registration or human override "
                    "before takeover."
                ),
            })
        cur = c.execute(
            "INSERT INTO agent_messages(from_agent, to_agent, task_id, message, requires_ack, "
            "ack_deadline, sent_at, signal, priority, idem_key, principal_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (from_agent, to_agent, task_id, message, 1 if requires_ack else 0, deadline, now,
             signal or None, int(priority or 0), idem_key or None, principal_id or None),
        )
        msg_id = cur.lastrowid
        task_exists = bool(
            task_id and c.execute("SELECT 1 FROM tasks WHERE task_id=?",
                                  (task_id,)).fetchone()
        )
        response = {"id": msg_id, "from_agent": from_agent, "to_agent": to_agent,
                    "task_id": task_id, "message": message, "requires_ack": requires_ack,
                    "ack_deadline": deadline, "sent_at": now, "acked_at": None,
                    "signal": signal, "priority": int(priority or 0),
                    "mailbox_stored": True,
                    "delivery": delivery,
                    "delivery_status": delivery["status"]}
        response["delivery_receipt"] = build_message_delivery_receipt(
            delivery, task_comment=(not delivery.get("reachable") and task_exists))
        if identity_state.get("status") != "clear":
            response["identity"] = identity_state
        if not delivery.get("reachable"):
            failure_class = (
                "unbound_identity"
                if delivery.get("status") == "identity_unbound"
                else "unreachable_agent"
            )
            response["warning"] = delivery.get("message")
            response["fallback"] = {
                "task_comment": task_exists,
                "reason": delivery.get("reason"),
                "takeover_safe": delivery.get("takeover_safe", True),
                "failure_class": failure_class,
                "expected_signal": _store_facade().FAIL_FIX_FAILURE_CLASSES[failure_class]["expected_signal"],
            }
        if requires_ack:
            monitor = _create_ack_monitor(c, msg_id, from_agent, to_agent, task_id,
                                          deadline, now, on_ack_timeout=on_ack_timeout)
            response["monitor_id"] = monitor["id"]
            response["monitor"] = monitor
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, from_agent, "message.sent", json.dumps(response, sort_keys=True), now))
        if not delivery.get("reachable"):
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                (task_id, "switchboard/delivery", "message.delivery_unreachable",
                 json.dumps({
                     "message_id": msg_id,
                     "from_agent": from_agent,
                     "to_agent": to_agent,
                     "delivery": delivery,
                     "failure_class": failure_class,
                     "expected_signal": _store_facade().FAIL_FIX_FAILURE_CLASSES[failure_class]["expected_signal"],
                 }, sort_keys=True), now),
            )
            if task_exists:
                fallback_text = (
                    f"Directed message #{msg_id} to `{to_agent}` was queued in the "
                    f"durable inbox, but the target is not currently reachable "
                    f"({delivery.get('reason')}). Treat this task comment as the "
                    "visible fallback until that runtime registers, heartbeats, and "
                    "drains its Switchboard inbox."
                )
                if delivery.get("takeover_safe") is False:
                    fallback_text += (
                        " Recent unbound activity exists on this task, so do not "
                        "treat absence from active_agents as proof that takeover is safe."
                    )
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, "switchboard/delivery", "comment",
                     json.dumps({
                         "text": fallback_text,
                         "failure_class": failure_class,
                         "expected_signal": _store_facade().FAIL_FIX_FAILURE_CLASSES[failure_class]["expected_signal"],
                     }, sort_keys=True), now),
                )
        _store_facade()._idem_store(c, "send", idem_key, from_agent, payload, response)
        return response

def _monitor_row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not r:
        return None
    d = dict(r)
    for k in ("condition_json", "on_timeout_json", "result_json"):
        raw = d.pop(k, "{}")
        d[k[:-5] if k.endswith("_json") else k] = json.loads(raw or "{}")
    return d

def _create_ack_monitor(c: sqlite3.Connection, message_id: int, from_agent: str,
                        to_agent: str, task_id: Optional[str], deadline: Optional[float],
                        now: float, on_ack_timeout: str = "notify_sender") -> Dict[str, Any]:
    monitor_id = f"mon-{uuid.uuid4().hex[:16]}"
    condition = {"type": "message_ack", "message_id": message_id}
    action = (on_ack_timeout or "notify_sender").strip()
    if action not in ("notify_sender", "wake_target", "wake_or_operator_alert"):
        action = "notify_sender"
    on_timeout = {"action": action, "signal": "ack_timeout"}
    c.execute(
        "INSERT INTO coordination_monitors"
        "(id, kind, target_type, target_id, task_id, owner_agent, subject_agent, status, "
        "deadline, condition_json, on_timeout_json, result_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (monitor_id, "ack_deadline", "agent_message", str(message_id), task_id,
         from_agent, to_agent, "pending", deadline,
         json.dumps(condition, sort_keys=True), json.dumps(on_timeout, sort_keys=True),
         "{}", now, now),
    )
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id, "switchboard/monitor", "monitor.created",
               json.dumps({"monitor_id": monitor_id, "kind": "ack_deadline",
                           "message_id": message_id, "deadline": deadline,
                           "owner_agent": from_agent, "subject_agent": to_agent},
                          sort_keys=True), now))
    return _monitor_row(c.execute("SELECT * FROM coordination_monitors WHERE id=?",
                                  (monitor_id,)).fetchone()) or {}

def _load_monitor_for_message(c: sqlite3.Connection, message_id: int) -> Optional[Dict[str, Any]]:
    return _monitor_row(c.execute(
        "SELECT * FROM coordination_monitors WHERE kind='ack_deadline' "
        "AND target_type='agent_message' AND target_id=? ORDER BY created_at DESC LIMIT 1",
        (str(message_id),),
    ).fetchone())

def ack_message(message_id: int, response: str = "",
                actor: str = "system",
                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Mark a message as acknowledged by the receiving agent. Returns updated record."""
    now = time.time()
    with _conn(project) as c:
        cur = c.execute(
            "UPDATE agent_messages SET acked_at=?, ack_response=? WHERE id=? AND acked_at IS NULL",
            (now, response or None, message_id),
        )
        if cur.rowcount == 0:
            r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
            if r:
                msg = dict(r) | {"note": "already acked"}
                msg["monitor"] = _load_monitor_for_message(c, message_id)
                return msg
            return {"error": "message not found", "id": message_id}
        r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
        mon = _load_monitor_for_message(c, message_id)
        if mon and mon.get("status") in ("pending", "fired"):
            c.execute(
                "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
                "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
                (now, now, now,
                 json.dumps({"acked_at": now, "ack_response": response}, sort_keys=True),
                 mon["id"]),
            )
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (r["task_id"], "switchboard/monitor", "monitor.resolved",
                       json.dumps({"monitor_id": mon["id"], "message_id": message_id,
                                   "reason": "acked"}, sort_keys=True), now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (r["task_id"], actor, "message.acked",
                   json.dumps({"message_id": message_id, "response": response}, sort_keys=True), now))
        out = dict(r)
        out["monitor"] = _load_monitor_for_message(c, message_id)
    return out

def list_unacked_messages(to_agent: str, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Messages directed to this agent that have not been acknowledged yet."""
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM agent_messages WHERE to_agent=? AND requires_ack=1 "
            "AND acked_at IS NULL "
            "ORDER BY priority DESC, id",
            (to_agent,),
        ).fetchall()
    return [dict(r) for r in rows]

def list_agent_messages(project: str = DEFAULT_PROJECT, *, limit: int = 500,
                        task_id: str = "", agent: str = "") -> List[Dict[str, Any]]:
    """Directed-message history for a board (the agent-to-agent bus), oldest-first.

    Read-only projection over agent_messages for the coordination view: unlike
    list_unacked_messages (one recipient's open mailbox) this returns the whole
    conversation so an operator can replay who told whom what, and whether it was
    acked. Optionally scope to one task_id or one agent (as sender or recipient).

    When the bus exceeds `limit`, this returns the most RECENT `limit` messages (not
    the oldest) — a current-coordination view must not silently hide the newest
    traffic — then presents them oldest-first for chronological reading."""
    inner = "SELECT * FROM agent_messages WHERE 1=1"
    p: List[Any] = []
    if task_id:
        inner += " AND task_id=?"; p.append(task_id)
    if agent:
        inner += " AND (from_agent=? OR to_agent=?)"; p.extend([agent, agent])
    inner += " ORDER BY sent_at DESC, id DESC"
    if limit and limit > 0:
        inner += " LIMIT ?"; p.append(int(limit))
    # Take the newest window (inner, DESC) then re-sort ascending for display.
    q = f"SELECT * FROM ({inner}) ORDER BY sent_at ASC, id ASC"
    with _conn(project) as c:
        rows = c.execute(q, p).fetchall()
    return [dict(r) for r in rows]

def get_message_status(message_id: int, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    """Sender polls this to see whether a message has been acked."""
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
        if not r:
            return None
        out = dict(r)
        out["monitor"] = _load_monitor_for_message(c, message_id)
        out["mailbox_stored"] = True
        out["delivery"] = _store_facade()._agent_delivery_state(c, out.get("to_agent") or "", now)
        out["delivery_status"] = out["delivery"]["status"]
        task_exists = bool(
            out.get("task_id") and c.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (out["task_id"],)
            ).fetchone()
        )
        out["delivery_receipt"] = build_message_delivery_receipt(
            out["delivery"],
            task_comment=(not out["delivery"].get("reachable") and task_exists),
            acked_at=out.get("acked_at"),
        )
        return out

def list_pending_acks(agent_id: str = "", project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Unacked required messages plus their durable monitor state."""
    q = ("SELECT * FROM agent_messages WHERE requires_ack=1 AND acked_at IS NULL")
    params: List[Any] = []
    if agent_id:
        q += " AND (from_agent=? OR to_agent=?)"
        params.extend([agent_id, agent_id])
    q += " ORDER BY COALESCE(ack_deadline, 9999999999999), priority DESC, id"
    with _conn(project) as c:
        rows = c.execute(q, params).fetchall()
        out = []
        for r in rows:
            msg = dict(r)
            msg["monitor"] = _load_monitor_for_message(c, int(r["id"]))
            out.append(msg)
        return out

def list_coordination_monitors(status: str = "", kind: str = "", task_id: str = "",
                               project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM coordination_monitors WHERE 1=1"
    params: List[Any] = []
    if status:
        q += " AND status=?"; params.append(status)
    if kind:
        q += " AND kind=?"; params.append(kind)
    if task_id:
        q += " AND task_id=?"; params.append(task_id)
    q += " ORDER BY COALESCE(deadline, 9999999999999), created_at"
    with _conn(project) as c:
        return [_monitor_row(r) or {} for r in c.execute(q, params).fetchall()]

def resolve_monitor(monitor_id: str, reason: str = "manual",
                    actor: str = "system",
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM coordination_monitors WHERE id=?", (monitor_id,)).fetchone()
        if not r:
            return {"error": "monitor not found", "monitor_id": monitor_id}
        mon = _monitor_row(r) or {}
        if mon.get("status") == "resolved":
            return mon | {"note": "already resolved"}
        result = dict(mon.get("result") or {})
        result.update({"resolved_by": actor, "reason": reason})
        c.execute(
            "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
            "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
            (now, now, now, json.dumps(result, sort_keys=True), monitor_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (mon.get("task_id"), actor, "monitor.resolved",
                   json.dumps({"monitor_id": monitor_id, "reason": reason}, sort_keys=True), now))
        return _monitor_row(c.execute("SELECT * FROM coordination_monitors WHERE id=?",
                                      (monitor_id,)).fetchone()) or {}

def cancel_monitor(monitor_id: str, reason: str = "cancelled",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM coordination_monitors WHERE id=?", (monitor_id,)).fetchone()
        if not r:
            return {"error": "monitor not found", "monitor_id": monitor_id}
        mon = _monitor_row(r) or {}
        if mon.get("status") == "cancelled":
            return mon | {"note": "already cancelled"}
        result = dict(mon.get("result") or {})
        result.update({"cancelled_by": actor, "reason": reason})
        c.execute(
            "UPDATE coordination_monitors SET status='cancelled', resolved_at=?, "
            "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
            (now, now, now, json.dumps(result, sort_keys=True), monitor_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (mon.get("task_id"), actor, "monitor.cancelled",
                   json.dumps({"monitor_id": monitor_id, "reason": reason}, sort_keys=True), now))
        return _monitor_row(c.execute("SELECT * FROM coordination_monitors WHERE id=?",
                                      (monitor_id,)).fetchone()) or {}

def sweep_coordination_monitors(project: str = DEFAULT_PROJECT,
                                now: Optional[float] = None) -> Dict[str, Any]:
    """Evaluate durable monitors. Designed for a Switchboard-owned timer or explicit tool call."""
    now = time.time() if now is None else float(now)
    checked = resolved = fired = 0
    events: List[Dict[str, Any]] = []
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM coordination_monitors WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        for row in rows:
            checked += 1
            mon = _monitor_row(row) or {}
            if mon.get("kind") != "ack_deadline":
                c.execute("UPDATE coordination_monitors SET last_checked_at=?, updated_at=? WHERE id=?",
                          (now, now, mon["id"]))
                continue
            msg = c.execute("SELECT * FROM agent_messages WHERE id=?",
                            (int(mon.get("target_id") or 0),)).fetchone()
            if not msg:
                result = {
                    "reason": "target_missing",
                    "failure_class": "missing_data",
                    "expected_signal": _store_facade().FAIL_FIX_FAILURE_CLASSES["missing_data"]["expected_signal"],
                }
                c.execute(
                    "UPDATE coordination_monitors SET status='cancelled', resolved_at=?, "
                    "last_checked_at=?, updated_at=?, result_json=? WHERE id=?",
                    (now, now, now, json.dumps(result, sort_keys=True), mon["id"]),
                )
                events.append({"monitor_id": mon["id"], "status": "cancelled",
                               "reason": "target_missing",
                               "failure_class": "missing_data"})
                continue
            if msg["acked_at"] is not None:
                result = {"acked_at": msg["acked_at"], "ack_response": msg["ack_response"]}
                c.execute(
                    "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
                    "last_checked_at=?, updated_at=?, result_json=? WHERE id=?",
                    (now, now, now, json.dumps(result, sort_keys=True), mon["id"]),
                )
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (mon.get("task_id"), "switchboard/monitor", "monitor.resolved",
                           json.dumps({"monitor_id": mon["id"], "message_id": msg["id"],
                                       "reason": "acked"}, sort_keys=True), now))
                resolved += 1
                events.append({"monitor_id": mon["id"], "status": "resolved",
                               "message_id": msg["id"]})
                continue
            deadline = mon.get("deadline")
            if deadline is not None and deadline <= now:
                action = (mon.get("on_timeout") or {}).get("action") or "notify_sender"
                result = {"reason": "ack_timeout", "deadline": deadline, "fired_at": now,
                          "on_timeout": action,
                          "failure_class": "unreachable_agent",
                          "expected_signal": _store_facade().FAIL_FIX_FAILURE_CLASSES["unreachable_agent"]["expected_signal"]}
                c.execute(
                    "UPDATE coordination_monitors SET status='fired', fired_at=?, "
                    "last_checked_at=?, updated_at=?, result_json=? WHERE id=?",
                    (now, now, now, json.dumps(result, sort_keys=True), mon["id"]),
                )
                payload = {"monitor_id": mon["id"], "message_id": msg["id"],
                           "from_agent": msg["from_agent"], "to_agent": msg["to_agent"],
                           "deadline": deadline,
                           "failure_class": "unreachable_agent",
                           "expected_signal": _store_facade().FAIL_FIX_FAILURE_CLASSES["unreachable_agent"]["expected_signal"]}
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (msg["task_id"], "switchboard/monitor", "monitor.timeout",
                           json.dumps(payload, sort_keys=True), now))
                notice = (f"Ack timeout for message {msg['id']} to {msg['to_agent']} "
                          f"on task {msg['task_id'] or '(none)'}.")
                cur = c.execute(
                    "INSERT INTO agent_messages(from_agent, to_agent, task_id, message, "
                    "requires_ack, ack_deadline, sent_at, signal, priority, idem_key, principal_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("switchboard/monitor", msg["from_agent"], msg["task_id"], notice,
                     1, None, now, "ack_timeout", 100, None, None),
                )
                notice_payload = {"id": cur.lastrowid, "from_agent": "switchboard/monitor",
                                  "to_agent": msg["from_agent"], "task_id": msg["task_id"],
                                  "message": notice, "requires_ack": True,
                                  "signal": "ack_timeout", "priority": 100,
                                  "sent_at": now,
                                  "failure_class": "unreachable_agent",
                                  "expected_signal": _store_facade().FAIL_FIX_FAILURE_CLASSES["unreachable_agent"]["expected_signal"]}
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (msg["task_id"], "switchboard/monitor", "message.sent",
                           json.dumps(notice_payload, sort_keys=True), now))
                wake = None
                if action in ("wake_target", "wake_or_operator_alert"):
                    selector = {"agent_id": msg["to_agent"]}
                    runtime = _store_facade()._selector_runtime_for_agent(msg["to_agent"])
                    if runtime:
                        selector["runtime"] = runtime
                    wake = _insert_wake_intent(
                        c, selector=selector, reason="ack_timeout",
                        source=f"monitor:{mon['id']}",
                        policy={"no_eligible_host": "wait",
                                "operator_alert": action == "wake_or_operator_alert"},
                        task_id=msg["task_id"], principal_id="",
                        actor="switchboard/monitor", now=now,
                        project=project,
                        idem_key=f"ack-timeout:{mon['id']}")
                    result["wake_id"] = wake["wake_id"]
                    result["wake_status"] = wake["status"]
                    c.execute(
                        "UPDATE coordination_monitors SET result_json=? WHERE id=?",
                        (json.dumps(result, sort_keys=True), mon["id"]),
                    )
                fired += 1
                event = {"monitor_id": mon["id"], "status": "fired",
                         "message_id": msg["id"], "notice_id": cur.lastrowid,
                         "failure_class": "unreachable_agent"}
                if wake:
                    event["wake_id"] = wake["wake_id"]
                    event["wake_status"] = wake["status"]
                events.append(event)
            else:
                c.execute("UPDATE coordination_monitors SET last_checked_at=?, updated_at=? WHERE id=?",
                          (now, now, mon["id"]))
    wake_sweep = sweep_wake_intents(project=project, now=now)
    return {"project": project, "checked": checked, "resolved": resolved,
            "fired": fired, "events": events, "wake_sweep": wake_sweep}


class StoreCoordinationRepository:
    """SQL-backed coordination repository (ARCH-MS-33)."""

    def request_wake(self, selector: dict[str, Any], **kwargs) -> dict[str, Any]:
        return request_wake(selector, **kwargs)

    def claim_wake(self, host_id: str, wake_id: str, **kwargs) -> dict[str, Any]:
        return claim_wake(host_id, wake_id, **kwargs)

    def complete_wake(self, wake_id: str, **kwargs) -> dict[str, Any]:
        return complete_wake(wake_id, **kwargs)

    def send_agent_message(self, from_agent: str, to_agent: str, message: str, **kwargs) -> dict[str, Any]:
        return send_agent_message(from_agent, to_agent, message, **kwargs)

    def ack_message(self, message_id: int, **kwargs) -> dict[str, Any]:
        return ack_message(message_id, **kwargs)

    def request_unblock(self, requesting_agent: str, blocking_task_id: str, **kwargs) -> dict[str, Any]:
        return request_unblock(requesting_agent, blocking_task_id, **kwargs)

    def sweep_coordination_monitors(self, **kwargs) -> dict[str, Any]:
        return sweep_coordination_monitors(**kwargs)


def default_coordination_repository() -> StoreCoordinationRepository:
    return StoreCoordinationRepository()


__all__ = [
    "StoreCoordinationRepository",
    "default_coordination_repository",
    "PROTOCOL_ENVELOPE",
    "protocol_envelope",
    "check_protocol_compatibility",
    "_wake_row",
    "_insert_wake_intent",
    "request_wake",
    "list_wake_intents",
    "claim_wake",
    "complete_wake",
    "cancel_wake",
    "sweep_wake_intents",
    "request_unblock",
    "list_unblock_requests",
    "send_agent_message",
    "_monitor_row",
    "_create_ack_monitor",
    "_load_monitor_for_message",
    "ack_message",
    "list_unacked_messages",
    "list_agent_messages",
    "get_message_status",
    "list_pending_acks",
    "list_coordination_monitors",
    "resolve_monitor",
    "cancel_monitor",
    "sweep_coordination_monitors",
]
