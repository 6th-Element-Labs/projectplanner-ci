#!/usr/bin/env python3
"""Smoke test for the first Switchboard live-loop implementation.

Uses throwaway SQLite files only. Run:
    python3 test_switchboard_runtime.py
"""
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="switchboard-runtime-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_AUTH_MODE"] = "required"

import auth  # noqa: E402
import store  # noqa: E402

P = "maxwell"
TOKEN = "test-token"

store.init_db(P)
principal = store.create_principal(
    kind="agent",
    display_name="codex/test",
    token=TOKEN,
    scopes=["read", "write:tasks", "write:ixp"],
    principal_id="agent-codex-test",
    project=P,
)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    try:
        auth.authenticate(P, "", ("write:ixp",))
        ok(False, "required auth rejects missing bearer token")
    except PermissionError:
        ok(True, "required auth rejects missing bearer token")

    p = auth.authenticate(P, TOKEN, ("write:ixp",))
    ok(p["id"] == principal["id"], "principal authenticates by bearer token")

    reg = store.register_agent(
        agent_id="codex/TEST#1",
        runtime="codex",
        model="gpt-5",
        lane="TEST",
        task_id="TEST-1",
        control={"mode": "advisory_poll"},
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(reg["control"]["mode"] == "advisory_poll", "register_agent stores control fidelity")
    ok(len(store.list_active_agents(lane="TEST", project=P)) == 1, "list_active_agents returns live session")
    hb = store.heartbeat("codex/TEST#1", actor=auth.actor(p), project=P)
    ok(not hb.get("error"), "heartbeat renews registered session")

    lease = store.claim_resources(
        agent_id="codex/TEST#1",
        resource_type="file",
        names=["store.py"],
        task_id="TEST-1",
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="claim-file-1",
        project=P,
    )
    ok("lease_id" in lease, "claim_resources grants a free resource")
    again = store.claim_resources(
        agent_id="codex/TEST#1",
        resource_type="file",
        names=["store.py"],
        task_id="TEST-1",
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="claim-file-1",
        project=P,
    )
    ok(again["lease_id"] == lease["lease_id"], "claim_resources is idempotent by idem_key")
    conflict = store.claim_resources(
        agent_id="claude/TEST#2",
        resource_type="file",
        names=["store.py"],
        task_id="TEST-2",
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(conflict.get("conflict") == "codex/TEST#1", "claim_resources reports conflicts")
    rel = store.release_resource_lease(lease["lease_id"], actor=auth.actor(p), project=P)
    ok(rel.get("released") is True, "release_resource_lease releases a lease")

    msg = store.send_agent_message(
        "codex/TEST#1",
        "claude/TEST#2",
        "stop before editing store.py",
        task_id="TEST-1",
        requires_ack=True,
        signal="stop",
        priority=10,
        principal_id=p["id"],
        idem_key="stop-msg-1",
        project=P,
    )
    ok(msg["signal"] == "stop" and msg["priority"] == 10, "messages carry signal and priority")
    inbox = store.list_unacked_messages("claude/TEST#2", project=P)
    ok(inbox and inbox[0]["id"] == msg["id"], "inbox returns unacked directed message")
    ack = store.ack_message(msg["id"], response="denied before tool", actor="claude/TEST#2", project=P)
    ok(ack["acked_at"] is not None, "ack_message records receipt")

    first = store.create_task({"workstream_id": "TEST", "title": "first"}, actor="seed", project=P)
    second = store.create_task({"workstream_id": "TEST", "title": "second",
                                "depends_on": [first["task_id"]]}, actor="seed", project=P)
    claimed = store.claim_next(
        agent_id="codex/TEST#1",
        lanes=["TEST"],
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="claim-next-1",
        project=P,
    )
    ok(claimed.get("claimed") and claimed["task"]["task_id"] == first["task_id"],
       "claim_next claims the first unblocked task")
    claimed_again = store.claim_next(
        agent_id="codex/TEST#1",
        lanes=["TEST"],
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="claim-next-1",
        project=P,
    )
    ok(claimed_again["claim_id"] == claimed["claim_id"], "claim_next is idempotent by idem_key")
    store.complete_claim(claimed["claim_id"], evidence="smoke", actor=auth.actor(p), project=P)
    store.update_task(first["task_id"], {"status": "Done"}, actor="verifier", project=P)
    next_claim = store.claim_next(
        agent_id="codex/TEST#1",
        lanes=["TEST"],
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(next_claim.get("claimed") and next_claim["task"]["task_id"] == second["task_id"],
       "claim_next respects dependency completion")

    usage = store.report_usage(
        source="agent_report",
        confidence="reported",
        task_id=second["task_id"],
        claim_id=next_claim["claim_id"],
        agent_id="codex/TEST#1",
        runtime="codex",
        model="gpt-5",
        prompt_tokens=1000,
        completion_tokens=200,
        cost_usd=0.42,
        principal_id=p["id"],
        project=P,
    )
    ok(usage["total_tokens"] == 1200, "report_usage stores total tokens")
    tally = store.task_tally(second["task_id"], project=P)
    ok(tally["spend"]["cost_usd"] == 0.42, "task_tally sums cost")
    ok(tally["spend"]["by_source"]["agent_report"]["total_tokens"] == 1200,
       "task_tally preserves spend source")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

