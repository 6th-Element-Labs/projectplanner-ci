"""UI-30: the kickoff record — advisory Scope approvals.

Vision -> PRD -> Architecture -> Operating rules -> Scope breakdown. Approving
a gate requires every upstream gate to be approved (the frontier rule);
revising an approved gate marks every approved downstream gate stale.
``build_authorized`` records whether all five are approved and none stale. It
is planning metadata only: kickoff approvals never gate claiming or merging.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from constants import DEFAULT_PROJECT
from db.connection import _conn

KICKOFF_SCHEMA = "switchboard.kickoff_record.v1"
GATES: List[str] = ["vision", "prd", "arch", "rules", "scope"]
_STATUSES = frozenset({"pending", "approved", "stale"})


class KickoffGateError(ValueError):
    """Raised when an approve/revise violates the ladder invariant."""


def _ensure_rows(c) -> None:
    now = time.time()
    inserted = 0
    for g in GATES:
        cur = c.execute("INSERT OR IGNORE INTO kickoff_gates(gate, status, updated_at) "
                        "VALUES(?, 'pending', ?)", (g, now))
        inserted += getattr(cur, "rowcount", 0) or 0
    if inserted:
        c.commit()   # read paths call this too — first touch must be durable


def _rows(c) -> Dict[str, Dict[str, Any]]:
    _ensure_rows(c)
    out: Dict[str, Dict[str, Any]] = {}
    for r in c.execute("SELECT * FROM kickoff_gates").fetchall():
        out[str(r["gate"])] = dict(r)
    return out


def _frontier(rows: Dict[str, Dict[str, Any]]) -> str:
    """First gate (in ladder order) that is not approved — '' when all are."""
    for g in GATES:
        if rows[g]["status"] != "approved":
            return g
    return ""


def get_kickoff_state(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    with _conn(project) as c:
        rows = _rows(c)
    frontier = _frontier(rows)
    any_stale = any(rows[g]["status"] == "stale" for g in GATES)
    authorized = frontier == "" and not any_stale
    gates = []
    for i, g in enumerate(GATES):
        r = rows[g]
        # the UI state: ok / now (frontier) / stale / wait
        if r["status"] == "approved":
            s = "ok"
        elif g == frontier:
            s = "stale" if r["status"] == "stale" else "now"
        elif r["status"] == "stale":
            s = "stale"
        else:
            s = "wait"
        gates.append({"gate": g, "order": i, "s": s, "status": r["status"],
                      "version": int(r["version"] or 0),
                      "approved_by": r["approved_by"] or "",
                      "approved_at": r["approved_at"],
                      "note": r["note"] or ""})
    return {"schema": KICKOFF_SCHEMA, "gates": gates, "frontier": frontier,
            "build_authorized": authorized, "enforced": kickoff_enforce_enabled()}


def approve_kickoff_gate(gate: str, *, actor: str, note: str = "",
                         project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    gate = (gate or "").strip().lower()
    if gate not in GATES:
        raise KickoffGateError(f"unknown gate: {gate}")
    with _conn(project) as c:
        rows = _rows(c)
        idx = GATES.index(gate)
        blocked = [g for g in GATES[:idx] if rows[g]["status"] != "approved"]
        if blocked:
            raise KickoffGateError(
                f"{gate} is locked — approve {blocked[0]} first (ladder order)")
        now = time.time()
        c.execute("UPDATE kickoff_gates SET status='approved', version=version+1, "
                  "approved_by=?, approved_at=?, note=?, updated_at=? WHERE gate=?",
                  (actor or "", now, note or "", now, gate))
        c.commit()
    return get_kickoff_state(project)


def revise_kickoff_gate(gate: str, *, actor: str, note: str = "",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """The edited gate stays approved (version bumps); approved downstream gates
    go stale so the advisory completeness record remains truthful."""
    gate = (gate or "").strip().lower()
    if gate not in GATES:
        raise KickoffGateError(f"unknown gate: {gate}")
    with _conn(project) as c:
        rows = _rows(c)
        if rows[gate]["status"] != "approved":
            raise KickoffGateError(f"{gate} is not approved — nothing to revise")
        now = time.time()
        c.execute("UPDATE kickoff_gates SET version=version+1, approved_by=?, "
                  "approved_at=?, note=?, updated_at=? WHERE gate=?",
                  (actor or "", now, note or "", now, gate))
        for g in GATES[GATES.index(gate) + 1:]:
            if rows[g]["status"] == "approved":
                c.execute("UPDATE kickoff_gates SET status='stale', updated_at=? "
                          "WHERE gate=?", (now, g))
        c.commit()
    return get_kickoff_state(project)


def kickoff_enforce_enabled() -> bool:
    """Compatibility projection for clients that still display this field.

    Kickoff approval is permanently advisory. In particular, the retired
    ``PM_KICKOFF_ENFORCE`` setting must not restore a human completion gate.
    """
    return False


def kickoff_enforcement(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Compatibility verdict: the kickoff record is never an execution gate."""
    return {"enforced": False, "authorized": True, "blocking_gate": "", "reason": ""}
