"""KPIs / outcomes / spend economics repository (ARCH-MS-49).

Owns spend ingest, outcome/KPI CRUD, kpi tallies, list endpoints, and the
budget/dispatch scoring helpers previously living in ``repositories/shell.py``.
Cross-cutting helpers (``_risk_value``, ``_task_required_capabilities``) stay
reachable via ``_store_facade()`` during the strangler. ``store.py`` /
``shell.py`` re-export these symbols; root ``kpis_economics_store.py`` is a
compatibility shim.
"""
from __future__ import annotations

import json
import calendar
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from constants import *  # noqa: F401,F403
from db.connection import _conn, _write_through
from db.core import *  # noqa: F401,F403
from switchboard.security import redact_provider_secrets


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


def _risk_value(*args, **kwargs):
    return _store_facade()._risk_value(*args, **kwargs)


def _task_required_capabilities(*args, **kwargs):
    return _store_facade()._task_required_capabilities(*args, **kwargs)


def _budget_status(max_budget_usd: Optional[float], spent_usd: float) -> Dict[str, Any]:
    remaining = max_budget_usd - spent_usd if max_budget_usd is not None else None
    if max_budget_usd is None:
        status = "not_limited"
    elif remaining is not None and remaining < 0:
        status = "over_budget"
    elif max_budget_usd and spent_usd >= max_budget_usd * 0.9:
        status = "tight"
    else:
        status = "ok"
    return {"budget_usd": max_budget_usd, "spent_usd": round(spent_usd, 6),
            "remaining_usd": round(remaining, 6) if remaining is not None else None,
            "status": status}


def _dispatch_score(task: Dict[str, Any], requested_lanes: set,
                    requested_caps: set, tally: Dict[str, Any],
                    max_budget_usd: Optional[float]) -> Dict[str, Any]:
    sort_order = int(task.get("sort_order") or 0)
    lane = (task.get("_wsId") or "").upper()
    required_caps = _task_required_capabilities(task)
    matched_caps = sorted(set(required_caps) & requested_caps)
    capability_fit = ((len(matched_caps) / len(required_caps)) if required_caps else 1.0)
    budget = _budget_status(max_budget_usd, float(tally["spend"]["cost_usd"] or 0.0))
    verified = len([o for o in tally.get("outcomes", []) if o.get("status") == "verified"])
    proposed = len([o for o in tally.get("outcomes", []) if o.get("status") == "proposed"])
    factors = {
        "blocking": 10000 if task.get("is_blocking") else 0,
        "sort_order": max(0, 1000 - min(sort_order, 1000)),
        "lane_affinity": 250 if requested_lanes and lane in requested_lanes else 0,
        "capability_fit": int(capability_fit * 200),
        "risk_fit": max(0, 120 - (_risk_value(task.get("risk_level") or "") * 20)),
        "budget_fit": 100 if budget["status"] in ("not_limited", "ok") else 0,
        "verified_outcome_signal": min(verified, 5) * 15,
        "pending_value_signal": min(proposed, 5) * 5,
    }
    return {"score": sum(factors.values()), "factors": factors,
            "required_capabilities": required_caps, "matched_capabilities": matched_caps,
            "budget": budget}


def _model_recommendation(task: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, str]:
    risk = _risk_value(task.get("risk_level") or "")
    budget_status = score["budget"]["status"]
    if risk >= 3:
        tier = "high"
    elif budget_status == "tight":
        tier = "small"
    elif score["required_capabilities"]:
        tier = "balanced"
    else:
        tier = "small"
    return {"model_tier": tier,
            "reason": f"risk={task.get('risk_level') or 'unspecified'}, "
                      f"budget={budget_status}, "
                      f"capabilities={','.join(score['required_capabilities']) or 'none'}"}


def report_usage(source: str, confidence: str, task_id: Optional[str] = None,
                 claim_id: Optional[str] = None, outcome_id: Optional[str] = None,
                 agent_id: Optional[str] = None, principal_id: str = "",
                 runtime: str = "", call_site: str = "", provider: str = "",
                 model: str = "", prompt_tokens: int = 0,
                 completion_tokens: int = 0, total_tokens: Optional[int] = None,
                 cost_usd: float = 0.0, latency_ms: Optional[float] = None,
                 status: str = "ok", metadata: Optional[Dict[str, Any]] = None,
                 request_id: Optional[str] = None,
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    total = int(total_tokens if total_tokens is not None else prompt_tokens + completion_tokens)
    now = time.time()
    with _conn(project) as c:
        if outcome_id and not task_id:
            outcome = c.execute("SELECT task_id FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
            if outcome:
                task_id = outcome["task_id"]
        if request_id:
            old = c.execute("SELECT * FROM llm_spend WHERE request_id=?", (request_id,)).fetchone()
            if old:
                return _spend_row(old)
        cur = c.execute(
            "INSERT INTO llm_spend(request_id, source, confidence, task_id, claim_id, outcome_id, "
            "agent_id, principal_id, runtime, call_site, provider, model, prompt_tokens, "
            "completion_tokens, total_tokens, cost_usd, latency_ms, status, metadata_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, source, confidence, task_id, claim_id, outcome_id, agent_id,
             principal_id or None, runtime or None, call_site or None, provider or None, model or None,
             int(prompt_tokens or 0), int(completion_tokens or 0), total, float(cost_usd or 0.0),
             latency_ms, status or "ok",
             json.dumps(redact_provider_secrets(metadata or {}), sort_keys=True), now),
        )
        row = c.execute("SELECT * FROM llm_spend WHERE id=?", (cur.lastrowid,)).fetchone()
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, agent_id or principal_id or "tally", "tally.usage_reported",
                   json.dumps({"spend_id": cur.lastrowid, "source": source,
                               "cost_usd": float(cost_usd or 0.0)}, sort_keys=True), now))
    return _spend_row(row)


def _usd_micros(value: Any, *, allow_zero: bool = False) -> int:
    """Convert public USD values to exact integer micro-dollars."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("cost_usd must be a finite number") from None
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError("cost_usd must be positive" if not allow_zero else "cost_usd must be non-negative")
    return int((amount * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _reservation_row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    out["reserved_cost_usd"] = out["reserved_micros"] / 1_000_000
    out["actual_cost_usd"] = (out["actual_micros"] / 1_000_000
                              if out["actual_micros"] is not None else None)
    out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
    return out


def set_spend_envelope(principal_id: str, daily_limit_usd: Any,
                       monthly_limit_usd: Any,
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if not (principal_id or "").strip():
        return {"error": "principal_id required", "failure_class": "unbound_identity"}
    try:
        daily = _usd_micros(daily_limit_usd, allow_zero=True)
        monthly = _usd_micros(monthly_limit_usd, allow_zero=True)
    except ValueError as exc:
        return {"error": str(exc), "failure_class": "invalid_input"}
    now = time.time()
    with _conn(project) as c:
        c.execute(
            "INSERT INTO spend_envelopes(principal_id,daily_limit_micros,monthly_limit_micros,created_at,updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(principal_id) DO UPDATE SET "
            "daily_limit_micros=excluded.daily_limit_micros,monthly_limit_micros=excluded.monthly_limit_micros,updated_at=excluded.updated_at",
            (principal_id, daily, monthly, now, now))
    return {"principal_id": principal_id, "daily_limit_usd": daily / 1_000_000,
            "monthly_limit_usd": monthly / 1_000_000}


def reserve_spend(principal_id: str, request_id: str, worst_case_cost_usd: Any,
                  metadata: Optional[Dict[str, Any]] = None,
                  now: Optional[float] = None,
                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Atomically account for worst-case cost before a provider call.

    This is an accounting decision only: callers decide whether a rejected
    reservation prevents execution.
    """
    if not (principal_id or "").strip():
        return {"error": "principal_id required", "failure_class": "unbound_identity"}
    if not (request_id or "").strip():
        return {"error": "request_id required", "failure_class": "missing_data"}
    try:
        reserved = _usd_micros(worst_case_cost_usd)
    except ValueError as exc:
        return {"error": str(exc), "failure_class": "missing_data"}
    at = float(now if now is not None else time.time())
    day_start = at - (at % 86400)
    month = time.gmtime(at)
    month_start = calendar.timegm((month.tm_year, month.tm_mon, 1, 0, 0, 0, 0, 0, 0))
    def reserve_transaction():
      with _conn(project) as c:
        old = c.execute("SELECT * FROM spend_reservations WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if old["principal_id"] != principal_id or old["reserved_micros"] != reserved:
                return {"error": "request_id already used with different reservation", "failure_class": "invalid_input"}
            return _reservation_row(old)
        envelope = c.execute("SELECT * FROM spend_envelopes WHERE principal_id=?", (principal_id,)).fetchone()
        if not envelope:
            return {"error": "spend envelope required", "failure_class": "missing_data"}
        def used_since(start: float) -> int:
            row = c.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='reconciled' THEN actual_micros "
                "WHEN status='reserved' THEN reserved_micros ELSE 0 END),0) used "
                "FROM spend_reservations WHERE principal_id=? AND reserved_at>=?",
                (principal_id, start)).fetchone()
            return int(row["used"] or 0)
        daily_used, monthly_used = used_since(day_start), used_since(month_start)
        if daily_used + reserved > int(envelope["daily_limit_micros"]):
            return {"error": "daily spend envelope exceeded", "failure_class": "budget_exceeded",
                    "remaining_usd": max(0, int(envelope["daily_limit_micros"]) - daily_used) / 1_000_000}
        if monthly_used + reserved > int(envelope["monthly_limit_micros"]):
            return {"error": "monthly spend envelope exceeded", "failure_class": "budget_exceeded",
                    "remaining_usd": max(0, int(envelope["monthly_limit_micros"]) - monthly_used) / 1_000_000}
        reservation_id = "spendres-" + uuid.uuid4().hex[:16]
        c.execute(
            "INSERT INTO spend_reservations(reservation_id,request_id,principal_id,reserved_micros,status,reserved_at,metadata_json) "
            "VALUES (?,?,?,?, 'reserved',?,?)",
            (reservation_id, request_id, principal_id, reserved, at,
             json.dumps(metadata or {}, sort_keys=True)))
        return _reservation_row(c.execute(
            "SELECT * FROM spend_reservations WHERE reservation_id=?", (reservation_id,)).fetchone())
    return _write_through(project, reserve_transaction)


def reconcile_spend(principal_id: str, request_id: str, actual_cost_usd: Any,
                    provider: str, model: str, prompt_tokens: int = 0,
                    completion_tokens: int = 0,
                    metadata: Optional[Dict[str, Any]] = None,
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if not (principal_id or "").strip():
        return {"error": "principal_id required", "failure_class": "unbound_identity"}
    if not provider or not model:
        return {"error": "provider and model required", "failure_class": "missing_data"}
    try:
        actual = _usd_micros(actual_cost_usd, allow_zero=True)
    except ValueError as exc:
        return {"error": str(exc), "failure_class": "missing_data"}
    now = time.time()
    def reconcile_transaction():
      with _conn(project) as c:
        row = c.execute("SELECT * FROM spend_reservations WHERE request_id=?", (request_id,)).fetchone()
        if not row or row["principal_id"] != principal_id:
            return {"error": "matching reservation required", "failure_class": "missing_data"}
        if row["status"] == "reconciled":
            same = (row["actual_micros"] == actual and row["provider"] == provider and
                    row["model"] == model and row["prompt_tokens"] == int(prompt_tokens) and
                    row["completion_tokens"] == int(completion_tokens))
            return _reservation_row(row) if same else {
                "error": "request_id already reconciled with different actuals",
                "failure_class": "invalid_input"}
        if row["status"] != "reserved":
            return {"error": "reservation is not active", "failure_class": "invalid_input"}
        merged = json.loads(row["metadata_json"] or "{}")
        merged.update(metadata or {})
        c.execute(
            "UPDATE spend_reservations SET actual_micros=?,provider=?,model=?,prompt_tokens=?,completion_tokens=?,"
            "status='reconciled',reconciled_at=?,metadata_json=? WHERE reservation_id=?",
            (actual, provider, model, int(prompt_tokens), int(completion_tokens), now,
             json.dumps(merged, sort_keys=True), row["reservation_id"]))
        return _reservation_row(c.execute(
            "SELECT * FROM spend_reservations WHERE reservation_id=?", (row["reservation_id"],)).fetchone())
    return _write_through(project, reconcile_transaction)


def _spend_row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
    return redact_provider_secrets(out)


def _outcome_row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    out["evidence"] = json.loads(out.pop("evidence_json") or "{}")
    out["value"] = json.loads(out.pop("value_json") or "{}")
    return redact_provider_secrets(out)


def _kpi_row(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def _outcome_kpi_link_row(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def record_outcome(outcome_type: str, title: str,
                   task_id: Optional[str] = None, claim_id: Optional[str] = None,
                   epic_id: Optional[str] = None, status: str = "proposed",
                   verifier: str = "", verification: str = "",
                   evidence: Optional[Dict[str, Any]] = None,
                   value: Optional[Dict[str, Any]] = None,
                   actor: str = "tally",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    status = (status or "proposed").strip().lower()
    title = redact_provider_secrets(title)
    verification = redact_provider_secrets(verification)
    evidence = redact_provider_secrets(evidence)
    value = redact_provider_secrets(value)
    if status not in ("proposed", "verified", "rejected", "superseded"):
        return {"error": "invalid outcome status", "status": status}
    if not outcome_type or not title:
        return {"error": "outcome_type and title required"}
    now = time.time()
    outcome_id = "outcome-" + uuid.uuid4().hex[:16]
    verified_at = now if status == "verified" else None
    with _conn(project) as c:
        c.execute(
            "INSERT INTO outcomes(id, project, task_id, epic_id, claim_id, type, title, status, "
            "verifier, verification, evidence_json, value_json, created_at, verified_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (outcome_id, project, task_id or None, epic_id or None, claim_id or None,
             outcome_type, title, status, verifier or None, verification or None,
             json.dumps(_jsonish(evidence), sort_keys=True),
             json.dumps(_jsonish(value), sort_keys=True), now, verified_at),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "tally.outcome_recorded",
                   json.dumps({"outcome_id": outcome_id, "status": status,
                               "type": outcome_type, "title": title}, sort_keys=True), now))
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
    return _outcome_row(row)


def verify_outcome(outcome_id: str, verifier: str, verification: str = "",
                   evidence: Optional[Dict[str, Any]] = None,
                   actor: str = "tally",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not row:
            return {"error": "outcome not found", "outcome_id": outcome_id}
        merged_evidence = json.loads(row["evidence_json"] or "{}")
        merged_evidence.update(_jsonish(redact_provider_secrets(evidence)))
        verification = redact_provider_secrets(verification)
        c.execute(
            "UPDATE outcomes SET status='verified', verifier=?, verification=?, "
            "evidence_json=?, verified_at=? WHERE id=?",
            (verifier or actor, verification or None,
             json.dumps(merged_evidence, sort_keys=True), now, outcome_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "tally.outcome_verified",
                   json.dumps({"outcome_id": outcome_id, "verifier": verifier or actor,
                               "verification": verification or None}, sort_keys=True), now))
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
    return _outcome_row(row)


def reject_outcome(outcome_id: str, verifier: str, reason: str,
                   actor: str = "tally",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not row:
            return {"error": "outcome not found", "outcome_id": outcome_id}
        evidence = json.loads(row["evidence_json"] or "{}")
        reason = redact_provider_secrets(reason)
        evidence["rejection_reason"] = reason
        c.execute(
            "UPDATE outcomes SET status='rejected', verifier=?, verification='rejected', "
            "evidence_json=? WHERE id=?",
            (verifier or actor, json.dumps(evidence, sort_keys=True), outcome_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "tally.outcome_rejected",
                   json.dumps({"outcome_id": outcome_id, "reason": reason}, sort_keys=True), now))
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
    return _outcome_row(row)


def create_kpi(name: str, unit: str, direction: str,
               owner: str = "", baseline_value: Optional[float] = None,
               current_value: Optional[float] = None,
               target_value: Optional[float] = None,
               period: str = "", actor: str = "tally",
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    direction = (direction or "").strip().lower()
    if direction not in ("increase", "decrease", "maintain"):
        return {"error": "direction must be increase, decrease, or maintain"}
    if not name or not unit:
        return {"error": "name and unit required"}
    now = time.time()
    kpi_id = "kpi-" + uuid.uuid4().hex[:16]
    if current_value is None:
        current_value = baseline_value
    with _conn(project) as c:
        c.execute(
            "INSERT INTO kpis(id, project, name, unit, direction, owner, baseline_value, "
            "current_value, target_value, period, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (kpi_id, project, name, unit, direction, owner or None, baseline_value,
             current_value, target_value, period or None, now, now),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "tally.kpi_created",
                   json.dumps({"kpi_id": kpi_id, "name": name, "unit": unit,
                               "direction": direction}, sort_keys=True), now))
        row = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
    return _kpi_row(row)


def update_kpi_value(kpi_id: str, current_value: float,
                     evidence: Optional[Dict[str, Any]] = None,
                     actor: str = "tally",
                     project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
        if not row:
            return {"error": "kpi not found", "kpi_id": kpi_id}
        c.execute("UPDATE kpis SET current_value=?, updated_at=? WHERE id=?",
                  (current_value, now, kpi_id))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "tally.kpi_updated",
                   json.dumps({"kpi_id": kpi_id, "current_value": current_value,
                               "evidence": _jsonish(evidence)}, sort_keys=True), now))
        row = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
    return _kpi_row(row)


def link_outcome_to_kpi(outcome_id: str, kpi_id: str,
                        contribution: Optional[float] = None,
                        contribution_unit: str = "",
                        confidence: str = "directional",
                        rationale: str = "",
                        actor: str = "tally",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    confidence = (confidence or "directional").strip().lower()
    if confidence not in ("measured", "estimated", "directional"):
        return {"error": "confidence must be measured, estimated, or directional"}
    now = time.time()
    link_id = "okpi-" + uuid.uuid4().hex[:16]
    with _conn(project) as c:
        outcome = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not outcome:
            return {"error": "outcome not found", "outcome_id": outcome_id}
        kpi = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
        if not kpi:
            return {"error": "kpi not found", "kpi_id": kpi_id}
        c.execute(
            "INSERT INTO outcome_kpi_links(id, project, outcome_id, kpi_id, contribution, "
            "contribution_unit, confidence, rationale, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (link_id, project, outcome_id, kpi_id, contribution, contribution_unit or kpi["unit"],
             confidence, rationale or None, now),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (outcome["task_id"], actor, "tally.outcome_kpi_linked",
                   json.dumps({"link_id": link_id, "outcome_id": outcome_id, "kpi_id": kpi_id,
                               "contribution": contribution, "confidence": confidence},
                              sort_keys=True), now))
        row = c.execute("SELECT * FROM outcome_kpi_links WHERE id=?", (link_id,)).fetchone()
    return _outcome_kpi_link_row(row)


def _spend_for_task(c: sqlite3.Connection, task_id: str,
                    outcomes: List[Dict[str, Any]]) -> List[sqlite3.Row]:
    outcome_ids = [o["id"] for o in outcomes]
    claim_ids = [o["claim_id"] for o in outcomes if o.get("claim_id")]
    clauses = ["task_id=?"]
    params: List[Any] = [task_id]
    if outcome_ids:
        clauses.append("outcome_id IN (%s)" % ",".join("?" for _ in outcome_ids))
        params.extend(outcome_ids)
    if claim_ids:
        clauses.append("claim_id IN (%s)" % ",".join("?" for _ in claim_ids))
        params.extend(claim_ids)
    return c.execute("SELECT * FROM llm_spend WHERE " + " OR ".join(clauses), params).fetchall()


def _spend_summary(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    spend = {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}, "by_model": {}}
    seen = set()
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        cost = float(row["cost_usd"] or 0.0)
        tokens = int(row["total_tokens"] or 0)
        source = row["source"]
        bucket = spend["by_source"].setdefault(source, {"cost_usd": 0.0, "total_tokens": 0,
                                                        "confidence": row["confidence"]})
        bucket["cost_usd"] += cost
        bucket["total_tokens"] += tokens
        # UI-12: per-model breakdown drives the model-mix line in the Economics panels.
        model = row["model"] or "unknown"
        mbucket = spend["by_model"].setdefault(model, {"cost_usd": 0.0, "total_tokens": 0})
        mbucket["cost_usd"] += cost
        mbucket["total_tokens"] += tokens
        spend["cost_usd"] += cost
        spend["total_tokens"] += tokens
    spend["cost_usd"] = round(spend["cost_usd"], 6)
    for bucket in spend["by_source"].values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    for mbucket in spend["by_model"].values():
        mbucket["cost_usd"] = round(mbucket["cost_usd"], 6)
    return spend


def kpi_tally(kpi_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    with _conn(project) as c:
        kpi = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
        if not kpi:
            return {"error": "kpi not found", "kpi_id": kpi_id}
        rows = c.execute(
            "SELECT o.*, l.id link_id, l.contribution, l.contribution_unit, "
            "l.confidence link_confidence, l.rationale "
            "FROM outcome_kpi_links l JOIN outcomes o ON o.id=l.outcome_id "
            "WHERE l.kpi_id=? ORDER BY l.created_at",
            (kpi_id,),
        ).fetchall()
    outcomes = []
    verified_contribution = 0.0
    task_ids = set()
    for row in rows:
        outcome = _outcome_row(row)
        outcome["link"] = {
            "id": row["link_id"],
            "contribution": row["contribution"],
            "contribution_unit": row["contribution_unit"],
            "confidence": row["link_confidence"],
            "rationale": row["rationale"],
        }
        outcomes.append(outcome)
        if outcome["status"] == "verified" and row["contribution"] is not None:
            verified_contribution += float(row["contribution"] or 0.0)
        if outcome.get("task_id"):
            task_ids.add(outcome["task_id"])
    spend_rows = []
    for task_id in task_ids:
        with _conn(project) as c:
            task_outcomes = [_outcome_row(r) for r in c.execute(
                "SELECT * FROM outcomes WHERE task_id=?", (task_id,)).fetchall()]
            spend_rows.extend(_spend_for_task(c, task_id, task_outcomes))
    spend = _spend_summary(spend_rows)
    return {
        "kpi": _kpi_row(kpi),
        "spend": spend,
        "outcomes": outcomes,
        "verified_contribution": round(verified_contribution, 6),
        "unit_cost": {
            "cost_per_contribution_unit": (
                round(spend["cost_usd"] / verified_contribution, 6)
                if verified_contribution else None
            )
        },
    }


def _merge_spend_totals(target: Dict[str, Any], spend: Dict[str, Any]) -> None:
    target["cost_usd"] = round(float(target.get("cost_usd") or 0.0) +
                              float(spend.get("cost_usd") or 0.0), 6)
    target["total_tokens"] = int(target.get("total_tokens") or 0) + int(spend.get("total_tokens") or 0)
    by_source = target.setdefault("by_source", {})
    for source, bucket in (spend.get("by_source") or {}).items():
        dst = by_source.setdefault(source, {
            "cost_usd": 0.0,
            "total_tokens": 0,
            "confidence": bucket.get("confidence"),
        })
        dst["cost_usd"] = round(float(dst.get("cost_usd") or 0.0) +
                                float(bucket.get("cost_usd") or 0.0), 6)
        dst["total_tokens"] = int(dst.get("total_tokens") or 0) + int(bucket.get("total_tokens") or 0)
        if bucket.get("confidence"):
            dst["confidence"] = bucket["confidence"]
    by_model = target.setdefault("by_model", {})
    for model, bucket in (spend.get("by_model") or {}).items():
        dst = by_model.setdefault(model, {"cost_usd": 0.0, "total_tokens": 0})
        dst["cost_usd"] = round(float(dst.get("cost_usd") or 0.0) +
                                float(bucket.get("cost_usd") or 0.0), 6)
        dst["total_tokens"] = int(dst.get("total_tokens") or 0) + int(bucket.get("total_tokens") or 0)


def list_kpis(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """All KPIs for a project with their live rollup (UI-2 tiles).

    Each entry is the KPI row plus verified_contribution, spend, and cost-per-unit
    from kpi_tally so the tile can show movement and unit economics without a
    second round-trip per KPI."""
    with _conn(project) as c:
        rows = c.execute("SELECT * FROM kpis ORDER BY created_at").fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        kpi = _kpi_row(row)
        tally = kpi_tally(kpi["id"], project=project)
        kpi["verified_contribution"] = tally.get("verified_contribution", 0.0)
        kpi["spend"] = tally.get("spend", {})
        kpi["unit_cost"] = tally.get("unit_cost", {})
        kpi["outcome_count"] = len(tally.get("outcomes", []))
        out.append(kpi)
    return out


def list_outcomes(project: str = DEFAULT_PROJECT, status: str = "",
                  limit: int = 200) -> List[Dict[str, Any]]:
    """Outcomes for a project, newest first, each with its KPI links (UI-2 queue).

    status filters to one lifecycle state (e.g. 'proposed' for the verify queue);
    empty returns all. limit caps the result."""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    clauses = ""
    params: List[Any] = []
    if status:
        clauses = " WHERE status=?"
        params.append(status)
    params.append(limit)
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM outcomes" + clauses + " ORDER BY created_at DESC LIMIT ?",
            params).fetchall()
        outcomes = [_outcome_row(r) for r in rows]
        if outcomes:
            ids = [o["id"] for o in outcomes]
            links = c.execute(
                "SELECT l.outcome_id, l.kpi_id, l.contribution, l.contribution_unit, "
                "l.confidence, k.name kpi_name, k.unit kpi_unit "
                "FROM outcome_kpi_links l JOIN kpis k ON k.id=l.kpi_id "
                "WHERE l.outcome_id IN (%s)" % ",".join("?" for _ in ids), ids).fetchall()
            by_outcome: Dict[str, List[Dict[str, Any]]] = {}
            for link in links:
                by_outcome.setdefault(link["outcome_id"], []).append(dict(link))
            for outcome in outcomes:
                outcome["kpi_links"] = by_outcome.get(outcome["id"], [])
    return outcomes



class StoreKpisEconomicsRepository:
    """Thin repository wrapper over module-level KPI/economics helpers."""

    def report_usage(self, *args, **kwargs):
        return report_usage(*args, **kwargs)

    def set_spend_envelope(self, *args, **kwargs):
        return set_spend_envelope(*args, **kwargs)

    def reserve_spend(self, *args, **kwargs):
        return reserve_spend(*args, **kwargs)

    def reconcile_spend(self, *args, **kwargs):
        return reconcile_spend(*args, **kwargs)

    def record_outcome(self, *args, **kwargs):
        return record_outcome(*args, **kwargs)

    def verify_outcome(self, *args, **kwargs):
        return verify_outcome(*args, **kwargs)

    def reject_outcome(self, *args, **kwargs):
        return reject_outcome(*args, **kwargs)

    def create_kpi(self, *args, **kwargs):
        return create_kpi(*args, **kwargs)

    def update_kpi_value(self, *args, **kwargs):
        return update_kpi_value(*args, **kwargs)

    def link_outcome_to_kpi(self, *args, **kwargs):
        return link_outcome_to_kpi(*args, **kwargs)

    def kpi_tally(self, kpi_id, project=DEFAULT_PROJECT):
        return kpi_tally(kpi_id, project=project)

    def list_kpis(self, project=DEFAULT_PROJECT):
        return list_kpis(project=project)

    def list_outcomes(self, project=DEFAULT_PROJECT, status="", limit=200):
        return list_outcomes(project=project, status=status, limit=limit)


def default_kpis_economics_repository() -> StoreKpisEconomicsRepository:
    return StoreKpisEconomicsRepository()


__all__ = [
    "StoreKpisEconomicsRepository",
    "default_kpis_economics_repository",
    "report_usage",
    "set_spend_envelope",
    "reserve_spend",
    "reconcile_spend",
    "record_outcome",
    "verify_outcome",
    "reject_outcome",
    "create_kpi",
    "update_kpi_value",
    "link_outcome_to_kpi",
    "kpi_tally",
    "list_kpis",
    "list_outcomes",
    "_budget_status",
    "_dispatch_score",
    "_model_recommendation",
    "_spend_row",
    "_outcome_row",
    "_kpi_row",
    "_outcome_kpi_link_row",
    "_spend_for_task",
    "_spend_summary",
    "_merge_spend_totals",
]
