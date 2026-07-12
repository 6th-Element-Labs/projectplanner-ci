#!/usr/bin/env python3
"""BUG-7: unreachable, dormant-host, wakeable, queued, and mailbox-only receipts."""

import os
import shutil
import sys
import tempfile


_TMP = tempfile.mkdtemp(prefix="bug7-wake-semantics-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402


P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(P)
    task = store.create_task(
        {"workstream_id": "BUG", "title": "BUG-7 delivery receipt proof"},
        actor="test", project=P)

    mailbox = store.send_agent_message(
        "codex/BUG-7", "claude/NO-HOST", "mailbox only",
        task_id=task["task_id"], project=P)
    receipt = mailbox["delivery_receipt"]
    ok(mailbox["delivery_status"] == "unreachable" and
       receipt["delivery_mode"] == "mailbox_only",
       "unregistered session with no Agent Host is explicitly mailbox-only")
    ok(receipt["mailbox"]["stored"] is True and
       receipt["runtime_delivery_proven"] is False and
       "not proof" in receipt["mailbox"]["meaning"],
       "mailbox storage is not represented as runtime delivery proof")
    ok(receipt["visible_fallback"]["task_comment"] is True and
       "fallback, not runtime delivery" in receipt["operator_message"],
       "task comment is exposed as a visible fallback, not delivery")

    unknown = store.send_agent_message(
        "codex/BUG-7", "worker-without-runtime", "unknown runtime", project=P)
    ok(unknown["delivery_receipt"]["wakeability"]["can_queue"] is False and
       unknown["delivery_receipt"]["wakeability"]["status"] == "runtime_unknown",
       "unknown runtime cannot be misrepresented as wakeable")

    store.register_host(
        {"host_id": "host/claude", "hostname": "agent-box",
         "runtimes": [{"runtime": "claude-code"}],
         "limits": {"max_sessions": 2}, "capacity": {"active_sessions": 0}},
        actor="test", project=P)
    wakeable = store.send_agent_message(
        "codex/BUG-7", "claude/WAKEABLE", "wakeable", project=P)
    wake_receipt = wakeable["delivery_receipt"]
    ok(wake_receipt["delivery_mode"] == "supervised_wake_available" and
       wake_receipt["wakeability"]["can_wake_now"] is True,
       "live eligible Agent Host is reported as supervised wake capacity")

    queued_wake = store.request_wake(
        selector={"agent_id": "claude/QUEUED", "runtime": "claude-code"},
        reason="BUG-7 proof", source="test", project=P)
    queued = store.send_agent_message(
        "codex/BUG-7", "claude/QUEUED", "queued wake", project=P)
    ok(queued["delivery_receipt"]["delivery_mode"] == "wake_queued" and
       queued["delivery_receipt"]["wakeability"]["wake_id"] == queued_wake["wake_id"],
       "pending wake is distinguished from a runtime that actually started")
    store.claim_wake("host/claude", queued_wake["wake_id"], actor="test", project=P)
    claimed = store.send_agent_message(
        "codex/BUG-7", "claude/QUEUED", "claimed wake", project=P)
    ok(claimed["delivery_receipt"]["delivery_mode"] == "wake_claimed" and
       claimed["delivery_receipt"]["wakeability"]["claimed_by_host"] == "host/claude",
       "claimed wake is distinguished from queued wake and runtime registration")

    with store._conn(P) as conn:
        conn.execute("UPDATE agent_hosts SET heartbeat_at=0 WHERE host_id='host/claude'")
    dormant = store.send_agent_message(
        "codex/BUG-7", "claude/DORMANT", "dormant host", project=P)
    dormant_receipt = dormant["delivery_receipt"]
    ok(dormant_receipt["delivery_mode"] == "dormant_registered_host" and
       dormant_receipt["wakeability"]["can_wake_now"] is False and
       dormant_receipt["wakeability"]["can_queue"] is True,
       "stale Agent Host is reported as dormant queue-only inventory")

    store.register_agent(
        agent_id="claude/ACTIVE", runtime="claude-code", task_id=task["task_id"],
        control={"mode": "advisory_poll"}, actor="test", project=P)
    active = store.send_agent_message(
        "codex/BUG-7", "claude/ACTIVE", "active session", requires_ack=True, project=P)
    ok(active["delivery_receipt"]["delivery_mode"] == "active_session" and
       active["delivery_receipt"]["runtime_delivery_proven"] is False,
       "active presence still requires poll/ack before handling is proven")
    store.ack_message(active["id"], response="handled", actor="claude/ACTIVE", project=P)
    handled = store.get_message_status(active["id"], project=P)
    ok(handled["delivery_receipt"]["runtime_delivery_proven"] is True and
       handled["delivery_receipt"]["acknowledged"] is True,
       "acknowledgement upgrades the receipt to proven runtime handling")

    app_js = open("static/app.js", encoding="utf-8").read()
    spec = open("docs/AGENT-HOST-SPEC.md", encoding="utf-8").read()
    ok("stored — not delivered" in app_js and "receipt.operator_message" in app_js,
       "operator UI names mailbox storage and renders the semantic receipt")
    ok("Dormant registered host" in spec and
       '{"runtime":"claude-code"}' in spec and
       "vendor UI" in spec,
       "Agent Host spec defines dormant and Claude Code registration requirements")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
