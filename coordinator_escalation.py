"""Exception-only human escalation channel (COORD-6).

Normal coordinator progress stays agent-to-agent (claim, wake, nudge, monitor).
Humans are interrupted only for actionable exception classes from the
Coordinator Operating Contract §5, with a structured notification that names
the task, failed condition, recommended choices, and minimum decision needed.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Sequence

SCHEMA = "switchboard.coordinator_escalation.v1"
ACTIVITY_KIND = "coordinator.escalation"
SIGNAL = "coordinator_escalation"
DEFAULT_ALERT_TO = "switchboard/operator"
DEFAULT_DEDUPE_WINDOW_S = 3600

# Contract §5 classes plus COORD-6 aliases that map onto them.
ESCALATION_CLASSES = frozenset({
    "human_gate_required",
    "budget_breach",
    "failed_gate",
    "stale_branch_conflict",
    "missing_provenance",
    "absent_permission",
    "unreachable_agent_no_host",
    "unbound_identity",
    "ambiguous_requirements",
    "security_secrets_boundary",
    "policy_violation",
    "repeated_failures",
    "red_ci_product_judgment",
})

# Mission next_actions that are operator attention — never agent-to-agent progress.
MISSION_ACTION_CLASSES: Dict[str, str] = {
    "request_human_approval": "human_gate_required",
    "approve_breakdown": "human_gate_required",
    "repair_task_link": "failed_gate",
    "propose_breakdown": "ambiguous_requirements",
}

# Automatic / agent-lane actions — must never page a human.
AGENT_TO_AGENT_ACTIONS = frozenset({
    "claim_task",
    "resume_or_claim",
    "verify_merge_provenance",
})

FAILURE_CLASS_BY_ESCALATION: Dict[str, str] = {
    "human_gate_required": "failed_gate",
    "budget_breach": "failed_gate",
    "failed_gate": "failed_gate",
    "stale_branch_conflict": "stale_branch",
    "missing_provenance": "missing_data",
    "absent_permission": "absent_permission",
    "unreachable_agent_no_host": "unreachable_agent",
    "unbound_identity": "unbound_identity",
    "ambiguous_requirements": "missing_data",
    "security_secrets_boundary": "absent_permission",
    "policy_violation": "failed_gate",
    "repeated_failures": "failed_gate",
    "red_ci_product_judgment": "failed_gate",
}

CHOICES_BY_CLASS: Dict[str, List[Dict[str, str]]] = {
    "human_gate_required": [
        {"id": "approve", "label": "Approve the human gate",
         "effect": "Unblocks coordinator dispatch for this task"},
        {"id": "reject", "label": "Reject / re-scope",
         "effect": "Keep blocked; agent revises before retry"},
    ],
    "budget_breach": [
        {"id": "raise_cap", "label": "Raise budget / envelope",
         "effect": "Allows further spend on this item"},
        {"id": "halt", "label": "Halt and hand back",
         "effect": "Stop coordinator spend; leave work as-is"},
    ],
    "failed_gate": [
        {"id": "repair", "label": "Repair the failed gate",
         "effect": "Fix CI/link/provenance then re-run the gate"},
        {"id": "waive_document", "label": "Document exception (operator only)",
         "effect": "Record an auditable waiver — never silent green-wash"},
    ],
    "stale_branch_conflict": [
        {"id": "rebase", "label": "Rebase / resolve conflicts",
         "effect": "Task owner brings the branch current"},
        {"id": "reassign", "label": "Reassign ownership",
         "effect": "Route conflict resolution to another eligible agent"},
    ],
    "missing_provenance": [
        {"id": "reconcile", "label": "Run reconcile / backfill provenance",
         "effect": "Restore Done/merge truth from GitHub"},
        {"id": "revoke_done", "label": "Move off Done until proven",
         "effect": "Fail closed until merged_sha/offline evidence exists"},
    ],
    "absent_permission": [
        {"id": "grant_scope", "label": "Grant missing scope/tier",
         "effect": "Operator elevates the coordinator or worker token"},
        {"id": "abort_action", "label": "Abort the intended action",
         "effect": "Leave work blocked; do not bypass permission floor"},
    ],
    "unreachable_agent_no_host": [
        {"id": "provision_host", "label": "Bring up an eligible host",
         "effect": "Register/allow_work a host for this project/lane"},
        {"id": "retarget", "label": "Change wake selector / runtime",
         "effect": "Retry dispatch against a different eligible fleet"},
    ],
    "unbound_identity": [
        {"id": "rebind", "label": "Re-register the runtime identity",
         "effect": "Bind the live session before claim/dispatch"},
        {"id": "override", "label": "Explicit human takeover override",
         "effect": "Documented override only — never silent takeover"},
    ],
    "ambiguous_requirements": [
        {"id": "clarify", "label": "Clarify scope / acceptance",
         "effect": "Provide the missing requirement decision"},
        {"id": "approve_breakdown", "label": "Approve proposed breakdown",
         "effect": "Accept the proposed deliverable breakdown"},
    ],
    "security_secrets_boundary": [
        {"id": "approve_secure_path", "label": "Approve a secure handling path",
         "effect": "Explicit security decision before continuing"},
        {"id": "stop", "label": "Stop — do not proceed",
         "effect": "Halt work that would cross the secrets boundary"},
    ],
    "policy_violation": [
        {"id": "amend_policy", "label": "Amend policy / grant exception",
         "effect": "Audited policy change or one-off exception"},
        {"id": "enforce_block", "label": "Keep blocked",
         "effect": "Policy stands; coordinator must not bypass"},
    ],
    "repeated_failures": [
        {"id": "investigate", "label": "Investigate root cause",
         "effect": "Stop bounded retries; diagnose the recurring failure"},
        {"id": "change_approach", "label": "Change approach / reassign",
         "effect": "Pick a different agent, host, or strategy"},
    ],
    "red_ci_product_judgment": [
        {"id": "fix_ci", "label": "Fix the failing checks",
         "effect": "Agent repairs until gates are green"},
        {"id": "product_waive", "label": "Product judgment / waive",
         "effect": "Human decides the red gate is accepted or not"},
    ],
}

MINIMUM_DECISION_BY_CLASS: Dict[str, str] = {
    "human_gate_required": "Approve or reject the human gate on this task",
    "budget_breach": "Raise the budget envelope or halt further spend",
    "failed_gate": "Repair the failed gate or document an explicit exception",
    "stale_branch_conflict": "Resolve the conflict/stale branch or reassign ownership",
    "missing_provenance": "Restore merge/offline provenance or revoke Done",
    "absent_permission": "Grant the missing authority or abort the action",
    "unreachable_agent_no_host": "Provision an eligible host or retarget the wake",
    "unbound_identity": "Rebind the runtime identity or authorize takeover",
    "ambiguous_requirements": "Clarify the missing requirement or approve breakdown",
    "security_secrets_boundary": "Approve a secure path or stop the work",
    "policy_violation": "Amend policy with audit, or keep the block",
    "repeated_failures": "Investigate root cause or change approach",
    "red_ci_product_judgment": "Fix CI or make an explicit product judgment",
}

# Alias map: audit/COORD-1 spellings → canonical class ids used in payloads.
_CLASS_ALIASES = {
    "stale_branch / conflict": "stale_branch_conflict",
    "stale_branch": "stale_branch_conflict",
    "conflict": "stale_branch_conflict",
    "unreachable_agent / no_host": "unreachable_agent_no_host",
    "unreachable_agent": "unreachable_agent_no_host",
    "no_host": "unreachable_agent_no_host",
    "no_eligible_host": "unreachable_agent_no_host",
    "security": "security_secrets_boundary",
    "secrets": "security_secrets_boundary",
    "security/secrets boundary": "security_secrets_boundary",
    "policy": "policy_violation",
    "ambiguous": "ambiguous_requirements",
    "red_ci": "red_ci_product_judgment",
}


def normalize_escalation_class(value: Any) -> Optional[str]:
    raw = str(value or "").strip().lower().replace("-", "_")
    if not raw:
        return None
    if raw in ESCALATION_CLASSES:
        return raw
    aliased = _CLASS_ALIASES.get(raw) or _CLASS_ALIASES.get(raw.replace("_", " "))
    if aliased in ESCALATION_CLASSES:
        return aliased
    # Soft normalize spaces/slashes already handled; try stripping punctuation.
    compact = raw.replace(" ", "_").replace("/", "_")
    if compact in ESCALATION_CLASSES:
        return compact
    return None


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def should_notify_human(
    *,
    escalation_class: Optional[str] = None,
    action: Optional[str] = None,
    attention: Optional[bool] = None,
    automatic: Optional[bool] = None,
) -> bool:
    """Fail-closed filter: only exception classes / attention actions page humans."""
    if automatic is True:
        return False
    action_name = str(action or "").strip()
    if action_name in AGENT_TO_AGENT_ACTIONS:
        return False
    klass = normalize_escalation_class(escalation_class)
    if klass:
        return True
    if action_name in MISSION_ACTION_CLASSES:
        return True
    if attention is True:
        return True
    return False


def recommended_choices(escalation_class: str) -> List[Dict[str, str]]:
    klass = normalize_escalation_class(escalation_class) or "failed_gate"
    return [dict(row) for row in CHOICES_BY_CLASS.get(klass, CHOICES_BY_CLASS["failed_gate"])]


def minimum_decision(escalation_class: str, *, task_id: str = "") -> str:
    klass = normalize_escalation_class(escalation_class) or "failed_gate"
    base = MINIMUM_DECISION_BY_CLASS.get(klass, "Make the minimum decision that unblocks this exception")
    task = (task_id or "").strip()
    return f"{base} ({task})" if task else base


def build_escalation_plan(
    *,
    escalation_class: str,
    project: str,
    task_id: str = "",
    deliverable_id: str = "",
    failed_condition: str = "",
    source: Optional[Dict[str, Any]] = None,
    blocks: Optional[Sequence[str]] = None,
    severity: str = "high",
    notify: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Build a structured ``switchboard.coordinator_escalation.v1`` payload."""
    klass = normalize_escalation_class(escalation_class)
    if not klass:
        return None
    if not should_notify_human(escalation_class=klass):
        return None
    task = (task_id or "").strip()
    condition = (failed_condition or "").strip() or f"Exception class `{klass}` requires a human decision"
    choices = recommended_choices(klass)
    decision = minimum_decision(klass, task_id=task)
    plan = {
        "schema": SCHEMA,
        "escalation_class": klass,
        "failure_class": FAILURE_CLASS_BY_ESCALATION.get(klass, "failed_gate"),
        "project_id": (project or "").strip(),
        "task_id": task or None,
        "deliverable_id": (deliverable_id or "").strip() or None,
        "failed_condition": condition,
        "recommended_choices": choices,
        "minimum_decision": decision,
        "blocks": list(blocks) if blocks else ["dispatch", "merge"],
        "severity": (severity or "high").strip().lower() or "high",
        "notify": list(notify) if notify else ["operator"],
        "source": source if isinstance(source, dict) else {},
    }
    plan["signature"] = _digest({
        "escalation_class": plan["escalation_class"],
        "project_id": plan["project_id"],
        "task_id": plan["task_id"],
        "deliverable_id": plan["deliverable_id"],
        "failed_condition": plan["failed_condition"],
        "minimum_decision": plan["minimum_decision"],
    })
    return plan


def classify_mission_action(
    action: Dict[str, Any],
    *,
    project: str,
    deliverable_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Map a mission next_action into an escalation plan, or None if agent-lane."""
    if not isinstance(action, dict):
        return None
    name = str(action.get("action") or "").strip()
    if not should_notify_human(
        action=name,
        attention=action.get("attention"),
        automatic=action.get("automatic"),
    ):
        return None
    klass = MISSION_ACTION_CLASSES.get(name)
    if not klass and action.get("attention") is True:
        klass = "human_gate_required"
    if not klass:
        return None
    reason = (action.get("reason") or action.get("detail") or action.get("label")
              or f"Mission action `{name}` requires a human")
    blocks = ["dispatch", "merge"]
    if name == "propose_breakdown":
        blocks = ["breakdown", "dispatch"]
    return build_escalation_plan(
        escalation_class=klass,
        project=project,
        task_id=str(action.get("task_id") or ""),
        deliverable_id=deliverable_id,
        failed_condition=str(reason),
        source={"kind": "mission_next_action", "action": action},
        blocks=blocks,
        severity="high" if action.get("delivery_impact") == "blocking" else "medium",
    )


def classify_dispatch_blocked(
    dispatch_result: Dict[str, Any],
    *,
    project: str,
    deliverable_id: str = "",
    task_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Escalate only when dispatch fails for an exception-class reason (e.g. no host)."""
    if not isinstance(dispatch_result, dict):
        return None
    reason = str(
        dispatch_result.get("reason")
        or dispatch_result.get("message")
        or dispatch_result.get("error")
        or ""
    ).strip()
    eligible = dispatch_result.get("eligible_host_count")
    requested = dispatch_result.get("requested")
    wake_id = dispatch_result.get("wake_id")
    claimed = dispatch_result.get("claimed")

    text = reason.lower()
    no_host = (
        eligible == 0
        or "no eligible host" in text
        or "eligible_host_count" in text and "0" in text
        or (requested is False and not wake_id and "host" in text)
    )
    if no_host:
        return build_escalation_plan(
            escalation_class="unreachable_agent_no_host",
            project=project,
            task_id=task_id or str(
                (dispatch_result.get("task") or {}).get("task_id")
                or dispatch_result.get("task_id")
                or ""
            ),
            deliverable_id=deliverable_id,
            failed_condition=reason or "No eligible host for coordinator dispatch",
            source={"kind": "dispatch_blocked", "dispatch": dispatch_result},
            blocks=["dispatch"],
            severity="high",
        )

    if "permission" in text or "scope" in text or "absent_permission" in text:
        return build_escalation_plan(
            escalation_class="absent_permission",
            project=project,
            task_id=task_id,
            deliverable_id=deliverable_id,
            failed_condition=reason or "Missing permission for coordinator dispatch",
            source={"kind": "dispatch_blocked", "dispatch": dispatch_result},
            blocks=["dispatch"],
        )

    if "identity" in text or "unbound" in text or "takeover" in text:
        return build_escalation_plan(
            escalation_class="unbound_identity",
            project=project,
            task_id=task_id,
            deliverable_id=deliverable_id,
            failed_condition=reason or "Unbound identity blocks dispatch",
            source={"kind": "dispatch_blocked", "dispatch": dispatch_result},
            blocks=["dispatch", "claim"],
        )

    # Generic claim miss (worker busy, not ready) stays agent-to-agent — no human page.
    if claimed is False and not no_host:
        return None
    return None


def classify_audit_recommendation(
    recommendation: Dict[str, Any],
    *,
    project: str,
) -> Optional[Dict[str, Any]]:
    """Map a COORD-2 audit recommendation that already carries an escalation_class."""
    if not isinstance(recommendation, dict):
        return None
    if recommendation.get("category") not in (None, "escalation") and not recommendation.get(
            "escalation_class"):
        # Only page humans for escalation-category or explicitly classed rows.
        if recommendation.get("category") != "escalation":
            return None
    klass = normalize_escalation_class(recommendation.get("escalation_class"))
    if not klass and recommendation.get("category") == "escalation":
        # Infer from action name when audit did not stamp a class.
        action = str(recommendation.get("action") or "")
        if "human_gate" in action:
            klass = "human_gate_required"
        elif "host" in action:
            klass = "unreachable_agent_no_host"
        elif "permission" in action or "scope" in action:
            klass = "absent_permission"
        elif "provenance" in action:
            klass = "missing_provenance"
        elif "monitor" in action:
            klass = "repeated_failures"
        else:
            klass = "failed_gate"
    if not klass:
        return None
    target_id = str(recommendation.get("target_id") or "")
    task_id = target_id if recommendation.get("target_type") == "task" else ""
    return build_escalation_plan(
        escalation_class=klass,
        project=project,
        task_id=task_id,
        failed_condition=str(recommendation.get("reason") or recommendation.get("action") or ""),
        source={"kind": "coordinator_audit", "recommendation": recommendation},
        severity="high",
    )


def format_human_notification(plan: Dict[str, Any]) -> str:
    """Render the operator-facing message body."""
    klass = plan.get("escalation_class") or "failed_gate"
    task = plan.get("task_id") or "(no task)"
    project = plan.get("project_id") or "?"
    deliverable = plan.get("deliverable_id")
    lines = [
        f"Coordinator escalation ({klass}) on project `{project}`.",
        f"Task: {task}",
    ]
    if deliverable:
        lines.append(f"Deliverable: {deliverable}")
    lines.append(f"Failed condition: {plan.get('failed_condition')}")
    lines.append(f"Minimum decision needed: {plan.get('minimum_decision')}")
    lines.append("Recommended choices:")
    for choice in plan.get("recommended_choices") or []:
        lines.append(
            f"- [{choice.get('id')}] {choice.get('label')} — {choice.get('effect')}"
        )
    blocks = plan.get("blocks") or []
    if blocks:
        lines.append("Blocks: " + ", ".join(str(b) for b in blocks))
    lines.append(
        "Normal agent progress is not paused for peers — only this human decision is required."
    )
    lines.append(f"signature={plan.get('signature')}")
    return "\n".join(lines)


def _dedupe_idem_key(plan: Dict[str, Any], *, alert_to: str, window: int) -> str:
    # A signature already identifies the material escalation state.  Including
    # an hourly bucket here caused unchanged exceptions to page the operator
    # again every hour (and twice when ticks straddled a bucket boundary).
    # Keep ``window`` in the call contract for receipt compatibility, but do not
    # make time passage alone a reason to send another email.
    del window
    return (
        f"coord-esc:{plan.get('project_id')}:{plan.get('escalation_class')}:"
        f"{plan.get('task_id') or '-'}:{plan.get('signature')}:{alert_to}"
    )


def deliver_human_escalation(
    plan: Dict[str, Any],
    *,
    store_mod: Any,
    actor: str = "switchboard/coordinator",
    alert_to: str = DEFAULT_ALERT_TO,
    requires_ack: bool = True,
    notify_outbound: bool = True,
    dedupe_window_s: int = DEFAULT_DEDUPE_WINDOW_S,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Deliver a structured escalation to the operator inbox (+ optional Slack/email).

    Dedupes by project + class + task + signature + time bucket so a sticky exception
    does not spam humans every tick. Returns a receipt; never raises into the tick path.
    """
    observed = float(time.time() if now is None else now)
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        return {"ok": False, "delivered": False, "error": "invalid_escalation_plan"}
    if not should_notify_human(escalation_class=plan.get("escalation_class")):
        return {"ok": True, "delivered": False, "skipped": "not_human_exception"}

    project = str(plan.get("project_id") or "").strip()
    if not project:
        return {"ok": False, "delivered": False, "error": "missing_project"}

    alert_to = (alert_to or DEFAULT_ALERT_TO).strip() or DEFAULT_ALERT_TO
    window_s = max(60, int(dedupe_window_s or DEFAULT_DEDUPE_WINDOW_S))
    window = int(observed // window_s)
    idem_key = _dedupe_idem_key(plan, alert_to=alert_to, window=window)
    idem_payload = {
        "signature": plan.get("signature"),
        "alert_to": alert_to,
        "escalation_class": plan.get("escalation_class"),
        "task_id": plan.get("task_id"),
    }
    message = format_human_notification(plan)
    subject = (
        f"[Switchboard] {plan.get('escalation_class')} on "
        f"{plan.get('task_id') or project}"
    )

    try:
        with store_mod._conn(project) as c:
            hit = store_mod._idem_hit(
                c, "coordinator_escalation", idem_key, actor, idem_payload)
            if hit is not None:
                if "error" in hit:
                    return hit
                out = dict(hit)
                out["delivered"] = False
                out["deduped"] = True
                return out
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "delivered": False,
            "error": "idempotency_check_failed",
            "message": str(exc),
            "plan": plan,
        }

    try:
        msg = store_mod.send_agent_message(
            from_agent=actor,
            to_agent=alert_to,
            message=message,
            task_id=plan.get("task_id") or None,
            requires_ack=requires_ack,
            signal=SIGNAL,
            priority=95,
            on_ack_timeout="wake_or_operator_alert",
            idem_key=f"{idem_key}:message",
            project=project,
        )
    except Exception as exc:  # noqa: BLE001 — delivery must not fail the tick
        return {
            "ok": False,
            "delivered": False,
            "error": "send_agent_message_failed",
            "message": str(exc),
            "plan": plan,
        }

    if isinstance(msg, dict) and msg.get("error"):
        return {
            "ok": False,
            "delivered": False,
            "error": msg.get("error"),
            "message": msg.get("message") or msg.get("error"),
            "plan": plan,
        }

    notify_results: List[Dict[str, Any]] = []
    if notify_outbound:
        try:
            import notify
            notify_results = notify.send(
                subject, message, channels=("slack", "email"),
                project=project, kind="notify",
            )
        except Exception as exc:  # noqa: BLE001
            notify_results = [{"channel": "notify", "sent": False, "error": str(exc)}]

    receipt = {
        "ok": True,
        "delivered": True,
        "deduped": False,
        "schema": SCHEMA,
        "activity_kind": ACTIVITY_KIND,
        "message_id": msg.get("id"),
        "alert_to": alert_to,
        "requires_ack": requires_ack,
        "signal": SIGNAL,
        "idem_key": idem_key,
        "dedupe_window_s": window_s,
        "plan": plan,
        "notify": notify_results,
        "observed_at": observed,
    }

    activity_payload = {
        "schema": SCHEMA,
        "activity_kind": ACTIVITY_KIND,
        "message_id": receipt["message_id"],
        "alert_to": alert_to,
        "requires_ack": requires_ack,
        "signal": SIGNAL,
        "idem_key": idem_key,
        "dedupe_window_s": window_s,
        "notify": notify_results,
        "observed_at": observed,
        "escalation_class": plan.get("escalation_class"),
        "failure_class": plan.get("failure_class"),
        "failed_condition": plan.get("failed_condition"),
        "minimum_decision": plan.get("minimum_decision"),
        "recommended_choices": plan.get("recommended_choices"),
        "signature": plan.get("signature"),
        "deliverable_id": plan.get("deliverable_id"),
        "task_id": plan.get("task_id"),
        "project_id": project,
    }
    try:
        with store_mod._conn(project) as c:
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    plan.get("task_id"),
                    actor,
                    ACTIVITY_KIND,
                    json.dumps(activity_payload, sort_keys=True),
                    observed,
                ),
            )
            store_mod._idem_store(
                c, "coordinator_escalation", idem_key, actor, idem_payload, receipt)
    except Exception as exc:  # noqa: BLE001
        receipt["activity_error"] = str(exc)

    return receipt


def classify_merge_gate_result(gate: Dict[str, Any], *, project: str,
                               task_id: str = "",
                               deliverable_id: str = "") -> Optional[Dict[str, Any]]:
    """Map a blocked ``merge_gate`` receipt to a COORD-6 escalation plan (COORD-7)."""
    if not isinstance(gate, dict):
        return None
    if gate.get("ok") is True or str(gate.get("status") or "").strip().lower() == "passed":
        return None
    findings = gate.get("findings") if isinstance(gate.get("findings"), list) else []
    codes = " ".join(str((f or {}).get("code") or "") for f in findings).lower()
    classes = " ".join(str((f or {}).get("failure_class") or "") for f in findings).lower()
    detail = "; ".join(
        str((f or {}).get("detail") or (f or {}).get("message") or (f or {}).get("code") or "")
        for f in findings
    ) or str(gate.get("message") or gate.get("error") or "merge_gate blocked")

    blob = f"{codes} {classes}"
    if any(tok in blob for tok in (
            "stale_branch", "stale_head", "conflict", "not_mergeable", "pr_not_mergeable")):
        klass = "stale_branch_conflict"
    elif any(tok in blob for tok in (
            "missing_provenance", "task_not_backed", "no_provenance", "missing_pr")):
        klass = "missing_provenance"
    elif any(tok in blob for tok in (
            "absent_permission", "permission", "unauthorized", "authority")):
        klass = "absent_permission"
    elif "human_gate" in blob:
        klass = "human_gate_required"
    elif any(tok in blob for tok in (
            "required_status", "external_ci", "failed_gate", "red_ci", "checks")):
        klass = "failed_gate"
    else:
        klass = "failed_gate"

    return build_escalation_plan(
        escalation_class=klass,
        project=project,
        task_id=task_id,
        deliverable_id=deliverable_id,
        failed_condition=detail,
        source={"kind": "merge_gate", "gate": {
            "status": gate.get("status"),
            "ok": gate.get("ok"),
            "findings": findings[:8],
        }},
        blocks=["merge"],
        severity="high",
    )


def deliver_mission_escalations(
    escalations: Sequence[Dict[str, Any]],
    *,
    store_mod: Any,
    project: str,
    deliverable_id: str = "",
    actor: str = "switchboard/coordinator",
    alert_to: str = DEFAULT_ALERT_TO,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Classify + deliver each mission escalation; skip non-exception rows."""
    receipts: List[Dict[str, Any]] = []
    for raw in escalations or []:
        plan = classify_mission_action(
            raw, project=project, deliverable_id=deliverable_id)
        if not plan:
            receipts.append({
                "ok": True,
                "delivered": False,
                "skipped": "not_human_exception",
                "action": (raw or {}).get("action") if isinstance(raw, dict) else None,
            })
            continue
        receipts.append(deliver_human_escalation(
            plan, store_mod=store_mod, actor=actor, alert_to=alert_to, now=now))
    return receipts
