"""Pure delivery/wake semantics for directed agent messages."""
from __future__ import annotations

from typing import Any, Iterable


RUNTIME_PREFIXES = (
    ("claude", "claude-code"),
    ("codex", "codex"),
    ("cursor", "cursor"),
    ("langgraph", "langgraph"),
    ("openai", "openai-loop"),
)


def infer_runtime_for_agent(agent_id: str) -> str:
    """Infer the runtime selector used by Agent Hosts from a stable agent id."""
    value = (agent_id or "").strip().lower()
    for prefix, runtime in RUNTIME_PREFIXES:
        if value.startswith(prefix):
            return runtime
    return ""


def runtime_matches_selector(runtime: dict[str, Any], selector: dict[str, Any]) -> bool:
    """Return whether one advertised host runtime satisfies a wake selector."""
    want_runtime = (selector.get("runtime") or "").strip()
    want_lane = (selector.get("lane") or "").strip()
    want_caps = {str(c).strip() for c in selector.get("capabilities") or [] if str(c).strip()}
    have_runtime = (runtime.get("runtime") or runtime.get("name") or "").strip()
    if want_runtime and have_runtime != want_runtime:
        return False
    lanes = [str(x).strip() for x in runtime.get("lanes") or [] if str(x).strip()]
    if want_lane and lanes and want_lane not in lanes:
        return False
    caps = {str(c).strip() for c in runtime.get("capabilities") or [] if str(c).strip()}
    return not want_caps or want_caps.issubset(caps)


def _host_matches(host: dict[str, Any], selector: dict[str, Any]) -> bool:
    runtimes: list[dict[str, Any]] = []
    for item in host.get("runtimes") or []:
        runtimes.append({"runtime": item} if isinstance(item, str) else (item or {}))
    return any(runtime_matches_selector(runtime, selector) for runtime in runtimes)


def _target_wakes(wakes: Iterable[dict[str, Any]], agent_id: str) -> list[dict[str, Any]]:
    return [
        wake for wake in wakes
        if (wake.get("selector") or {}).get("agent_id") == agent_id
        and wake.get("status") in {"pending", "claimed"}
    ]


def classify_agent_delivery(
        agent_id: str,
        presence: dict[str, Any] | None,
        hosts: Iterable[dict[str, Any]],
        wakes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Classify session reachability separately from wake/queue capability."""
    agent_id = (agent_id or "").strip()
    runtime = ((presence or {}).get("runtime") or infer_runtime_for_agent(agent_id)).strip()
    selector = {"agent_id": agent_id, "runtime": runtime}
    matching_hosts = [host for host in hosts if runtime and _host_matches(host, selector)]
    live_hosts = [host for host in matching_hosts if not host.get("stale")]
    eligible_hosts = [
        host for host in live_hosts
        if host.get("available_sessions") is None or int(host.get("available_sessions") or 0) > 0
    ]
    dormant_hosts = [host for host in matching_hosts if host.get("stale")]
    active_wakes = _target_wakes(wakes, agent_id)
    claimed_wakes = [wake for wake in active_wakes if wake.get("status") == "claimed"]
    pending_wakes = [wake for wake in active_wakes if wake.get("status") == "pending"]

    session_active = bool(presence and not presence.get("stale"))
    wakeability: dict[str, Any] = {
        "runtime": runtime or None,
        "requires_agent_host": True,
        "can_wake_now": False,
        "can_queue": bool(runtime),
        "eligible_host_count": len(eligible_hosts),
        "matching_host_count": len(matching_hosts),
        "host_ids": [host.get("host_id") for host in matching_hosts if host.get("host_id")],
    }

    if session_active:
        route = "active_session"
        wakeability.update({
            "status": "not_needed",
            "operator_action": "await_session_poll_or_ack",
        })
        message = (
            "Mailbox stored for an active registered session. Delivery occurs when the "
            "runtime drains its inbox; an acknowledgement is the handling proof."
        )
    elif claimed_wakes:
        wake = claimed_wakes[-1]
        route = "wake_claimed"
        wakeability.update({
            "status": "claimed",
            "wake_id": wake.get("wake_id"),
            "claimed_by_host": wake.get("claimed_by_host"),
            "operator_action": "await_runtime_registration",
        })
        message = "Mailbox stored; an Agent Host has claimed the wake and is starting or reusing a runtime."
    elif pending_wakes:
        wake = pending_wakes[-1]
        route = "wake_queued"
        wakeability.update({
            "status": "queued",
            "wake_id": wake.get("wake_id"),
            "operator_action": "await_host_claim",
        })
        message = "Mailbox stored; a durable wake is queued but no runtime delivery is proven yet."
    elif eligible_hosts:
        route = "supervised_wake_available"
        wakeability.update({
            "status": "wakeable",
            "can_wake_now": True,
            "operator_action": "request_wake",
        })
        message = "Mailbox stored; a supervised Agent Host can start or reuse this runtime if a wake is requested."
    elif live_hosts:
        route = "wake_queue_available"
        wakeability.update({
            "status": "host_at_capacity",
            "operator_action": "queue_wake",
        })
        message = "Mailbox stored; a matching Agent Host is online but at capacity, so a wake can only queue."
    elif dormant_hosts:
        route = "dormant_registered_host"
        wakeability.update({
            "status": "host_dormant",
            "operator_action": "queue_wake_or_restore_host",
        })
        message = "Mailbox stored; a matching Agent Host registration exists but is dormant, so a wake can only queue."
    else:
        route = "mailbox_only"
        wakeability.update({
            "status": "no_registered_host" if runtime else "runtime_unknown",
            "operator_action": "queue_wake_or_register_host" if runtime else "register_agent_host",
        })
        message = (
            "Mailbox stored only; no registered Agent Host can currently start this runtime."
            if runtime else
            "Mailbox stored only; the target runtime is unknown, so Switchboard cannot create a wake selector."
        )

    return {
        "delivery_mode": route,
        "wakeability": wakeability,
        "operator_message": message,
    }


def build_message_delivery_receipt(
        delivery: dict[str, Any],
        task_comment: bool = False,
        acked_at: float | None = None) -> dict[str, Any]:
    """Build the versioned receipt returned by MCP/REST and rendered by the UI."""
    acknowledged = acked_at is not None
    message = delivery.get("operator_message") or "Mailbox stored."
    if acknowledged:
        message = "Recipient acknowledged the message; runtime handling is proven."
    elif task_comment:
        message += " A visible task comment was also created; that comment is fallback, not runtime delivery."
    return {
        "schema": "switchboard.message_delivery_receipt.v1",
        "mailbox": {
            "stored": True,
            "meaning": "Durable inbox storage only; it is not proof of wake, runtime delivery, or handling.",
        },
        "delivery_mode": delivery.get("delivery_mode") or "mailbox_only",
        "session_status": delivery.get("status") or "unreachable",
        "runtime_delivery_proven": acknowledged,
        "acknowledged": acknowledged,
        "wakeability": dict(delivery.get("wakeability") or {}),
        "visible_fallback": {"task_comment": bool(task_comment)},
        "operator_message": message,
    }
