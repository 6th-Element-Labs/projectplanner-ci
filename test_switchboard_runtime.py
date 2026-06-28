#!/usr/bin/env python3
"""Smoke test for the first Switchboard live-loop implementation.

Uses throwaway SQLite files only. Run:
    python3 test_switchboard_runtime.py
"""
import os
import shutil
import subprocess
import tempfile

_TMP = tempfile.mkdtemp(prefix="switchboard-runtime-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_AUTH_MODE"] = "required"

import auth  # noqa: E402
import store  # noqa: E402

P = "maxwell"
TOKEN = "test-token"

store.init_db(P)
store.init_db("switchboard")
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
    project_ids = [p["id"] for p in store.projects()]
    ok("switchboard" in project_ids, "project registry exposes the Switchboard dogfood board")
    seeded = store.seed_if_empty("switchboard")
    ok(seeded >= 20, "Switchboard seed loads a P0 dogfood board")
    switch_tasks = store.list_tasks(project="switchboard")
    ok(any(t["task_id"] == "DOGFOOD-1" for t in switch_tasks),
       "Switchboard seed includes DOGFOOD-1")
    switch_agreement = store.get_working_agreement("switchboard")
    ok("codex/<TASK-ID>" in switch_agreement["branch_convention"],
       "Switchboard working agreement serves project-specific branch convention")
    agreement = store.get_working_agreement(P)
    ok("get_working_agreement" in agreement["session_start_sequence"][0],
       "working agreement is step zero of the handshake")
    ok(agreement["protocol"]["version"] == "ixp.v1",
       "working agreement advertises protocol version")
    ok(store.check_protocol_compatibility(agreement["protocol"])["compatible"] is True,
       "current protocol envelope is compatible")
    incompatible = store.check_protocol_compatibility({"version": "ixp.v9"})
    ok(incompatible["compatible"] is False, "unsupported protocol version is incompatible")
    ok(store.coerce_csv_list("ADAPTER, ENFORCE\nDISPATCH") == ["ADAPTER", "ENFORCE", "DISPATCH"],
       "coerce_csv_list splits comma/newline REST fields")
    ok(store.coerce_csv_list(["ADAPTER, ENFORCE", " docs "]) == ["ADAPTER", "ENFORCE", "docs"],
       "coerce_csv_list normalizes list items containing CSV fragments")

    reg = store.register_agent(
        agent_id="codex/TEST#1",
        runtime="codex",
        model="gpt-5",
        lane="TEST",
        task_id="TEST-1",
        control={"mode": "advisory_poll"},
        protocol=agreement["protocol"],
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(reg["control"]["mode"] == "advisory_poll", "register_agent stores control fidelity")
    ok(reg["protocol_compatibility"]["compatible"] is True,
       "register_agent returns protocol compatibility")
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
    ok(msg.get("monitor_id") and msg.get("monitor", {}).get("kind") == "ack_deadline",
       "requires_ack creates a durable ack monitor")
    seconds_msg = store.send_agent_message(
        "codex/TEST#1",
        "claude/TEST#2",
        "ack timeout in seconds",
        task_id="TEST-1",
        requires_ack=True,
        ack_timeout_seconds=2,
        project=P,
    )
    ok(1.0 <= (seconds_msg["ack_deadline"] - seconds_msg["sent_at"]) <= 3.0,
       "ack_timeout_seconds creates a real ack deadline")
    inbox = store.list_unacked_messages("claude/TEST#2", project=P)
    ok(inbox and inbox[0]["id"] == msg["id"], "inbox returns unacked directed message")
    ack = store.ack_message(msg["id"], response="denied before tool", actor="claude/TEST#2", project=P)
    ok(ack["acked_at"] is not None, "ack_message records receipt")
    acked_status = store.get_message_status(msg["id"], project=P)
    ok(acked_status["monitor"]["status"] == "resolved", "ack_message resolves durable monitor")
    timed = store.send_agent_message(
        "codex/TEST#1",
        "claude/TEST#2",
        "please ack quickly",
        task_id="TEST-1",
        requires_ack=True,
        ack_deadline_minutes=-1,
        project=P,
    )
    pending = store.list_pending_acks("codex/TEST#1", project=P)
    ok(any(m["id"] == timed["id"] and m["monitor"]["status"] == "pending" for m in pending),
       "list_pending_acks exposes outstanding ack monitors")
    swept = store.sweep_coordination_monitors(project=P)
    ok(swept["fired"] == 1, "sweep_coordination_monitors fires timed-out ack monitor")
    timed_status = store.get_message_status(timed["id"], project=P)
    ok(timed_status["monitor"]["status"] == "fired", "timed-out monitor stays fired until resolved")
    timeout_notice = store.list_unacked_messages("codex/TEST#1", project=P)
    ok(any(m.get("signal") == "ack_timeout" for m in timeout_notice),
       "ack timeout sends a notice back to the sender")

    first = store.create_task({"workstream_id": "TEST", "title": "first"}, actor="seed", project=P)
    second = store.create_task({"workstream_id": "TEST", "title": "second",
                                "depends_on": [first["task_id"]]}, actor="seed", project=P)
    claimed = store.claim_next(
        agent_id="codex/TEST#1",
        lanes="TEST,OTHER",
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="claim-next-1",
        project=P,
    )
    ok(claimed.get("claimed") and claimed["task"]["task_id"] == first["task_id"],
       "claim_next claims the first unblocked task")
    claimed_again = store.claim_next(
        agent_id="codex/TEST#1",
        lanes=["TEST,OTHER"],
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="claim-next-1",
        project=P,
    )
    ok(claimed_again["claim_id"] == claimed["claim_id"], "claim_next is idempotent by idem_key")
    completed = store.complete_claim(
        claimed["claim_id"],
        evidence={"branch": "claude/TEST-1-first", "head_sha": "abc123", "pr_url": "https://example/pr/1"},
        actor=auth.actor(p),
        project=P,
    )
    ok(completed["status"] == "In Review", "complete_claim moves task to In Review, not Done")
    first_after_complete = store.get_task(first["task_id"], project=P)
    ok(first_after_complete["status"] == "In Review", "task remains In Review after agent completion")
    ok(first_after_complete["git_state"]["head_sha"] == "abc123", "complete_claim stores head_sha evidence")
    waiting_claim = store.claim_next(
        agent_id="codex/TEST#1",
        lanes=["TEST"],
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(not waiting_claim.get("claimed"), "claim_next will not claim dependent task before merge-derived Done")
    opened = store.mark_task_pr_opened(first["task_id"], 1, "https://example/pr/1",
                                       "claude/TEST-1-first", "abc123",
                                       actor="github-webhook", project=P)
    ok(opened["status"] == "In Review" and opened["git_state"]["pr_number"] == 1,
       "PR open records review provenance")
    merged = store.mark_task_merged(first["task_id"], "merge789", 1, "https://example/pr/1",
                                    "claude/TEST-1-first", "abc123",
                                    actor="github-webhook", project=P)
    ok(merged["status"] == "Done" and merged["git_state"]["merged_sha"] == "merge789",
       "PR merge stamps merged_sha and marks task Done")
    direct = store.create_task({"workstream_id": "TEST", "title": "direct default"},
                               actor="seed", project=P)
    store.update_task(direct["task_id"], {"status": "In Review"}, actor="seed", project=P)
    backfilled = store.mark_task_default_branch_commit(
        direct["task_id"], "direct456", branch="master",
        subject=f"feat({direct['task_id']}): direct default proof",
        actor="default-branch-backfill", project=P)
    ok(backfilled["status"] == "Done" and backfilled["git_state"]["merged_sha"] == "direct456",
       "default-branch backfill stamps commit SHA and marks In Review Done")
    not_ready = store.create_task({"workstream_id": "TEST", "title": "not ready"},
                                  actor="seed", project=P)
    skipped = store.mark_task_default_branch_commit(
        not_ready["task_id"], "skip456", branch="master",
        subject=f"feat({not_ready['task_id']}): not ready", project=P)
    ok(skipped.get("reason") == "status_not_in_review",
       "default-branch backfill skips tasks that are not In Review")
    next_claim = store.claim_next(
        agent_id="codex/TEST#1",
        lanes=["TEST"],
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(next_claim.get("claimed") and next_claim["task"]["task_id"] == second["task_id"],
       "claim_next respects dependency completion")
    delta = store.get_activity_delta(0, lane="TEST", project=P)
    ok(any(u["task_id"] == first["task_id"] and u["git_state"]["merged_sha"] == "merge789"
           for u in delta["updates"]), "delta includes git_state provenance")

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

    bad = store.create_task({"workstream_id": "TEST", "title": "bad done"}, actor="seed", project=P)
    store.update_task(bad["task_id"], {"status": "Done"}, actor="legacy", project=P)
    report = store.reconcile(project=P)
    ok(any(f["code"] == "done_without_merged_sha" and f["task_id"] == bad["task_id"]
           for f in report["findings"]), "reconcile flags Done without merged_sha")
    fixed = store.mark_task_merged(bad["task_id"], "legacyfix", actor="github-webhook", project=P)
    ok(fixed["git_state"]["merged_sha"] == "legacyfix",
       "merge webhook can stamp provenance onto a legacy Done task")
    fixed_report = store.reconcile(project=P)
    ok(not any(f["code"] == "done_without_merged_sha" and f["task_id"] == bad["task_id"]
               for f in fixed_report["findings"]), "reconcile clears Done task after merge provenance")
    ok(fixed_report["external_checks"]["git_reachability"] == "not_configured",
       "reconcile skips git reachability until canonical main is known")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    store.update_canonical_main_sha(head, actor="test", project=P)
    real_git = store.create_task({"workstream_id": "TEST", "title": "real git proof"},
                                 actor="seed", project=P)
    store.update_task(real_git["task_id"], {"status": "In Review"}, actor="seed", project=P)
    store.mark_task_default_branch_commit(
        real_git["task_id"], head, branch="HEAD",
        subject=f"test({real_git['task_id']}): real git proof",
        actor="test", project=P)
    external_report = store.reconcile(project=P)
    ok(external_report["external_checks"]["git_reachability"] == "checked",
       "reconcile checks git reachability when canonical main is known")
    ok(not any(f["task_id"] == real_git["task_id"] for f in external_report["findings"]),
       "reconcile accepts a Done task whose merged_sha is reachable from canonical main")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
