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
    switch_payload = store.board_payload(project="switchboard")
    ok(switch_payload["rollups"]["total_tasks"] == len(switch_tasks),
       "Switchboard board_payload rollups match live task rows")
    ok(switch_payload["rollups"]["total_workstreams"] ==
       len({t["_wsId"] for t in switch_tasks}),
       "Switchboard board_payload workstream rollup matches visible workstreams")
    ok(sum(switch_payload["rollups"]["status_counts"].values()) == len(switch_tasks),
       "Switchboard status rollups add up to visible tasks")
    switch_agreement = store.get_working_agreement("switchboard")
    ok("codex/<TASK-ID>" in switch_agreement["branch_convention"],
       "Switchboard working agreement serves project-specific branch convention")
    agreement = store.get_working_agreement(P)
    ok("get_working_agreement" in agreement["session_start_sequence"][0],
       "working agreement is step zero of the handshake")
    ok(agreement["protocol"]["version"] == "ixp.v1",
       "working agreement advertises protocol version")
    ok(agreement["done_policy"]["agent_may_set_done"] is False and
       agreement["done_policy"]["requires_merge_provenance"] is True,
       "working agreement reserves Done for merge provenance")
    ok("safe_merge_protocol" in agreement and
       "rerun the relevant tests/checks after the rebase or conflict resolution"
       in agreement["safe_merge_protocol"]["pre_merge"],
       "working agreement instructs agents how to merge safely")
    policy = agreement.get("fail_fix_early_policy", {})
    ok(policy and "placeholder values" in policy["do_not_hide_with"],
       "working agreement tells agents not to hide failures")
    ok("visible" in policy["fallback_rule"] and "original failing signal" in policy["fallback_rule"],
       "working agreement allows only visible fallbacks")
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

    host = store.register_host(
        {
            "host_id": "host/test",
            "hostname": "testbox",
            "agent_host_version": "0.1.0",
            "repo_root": os.getcwd(),
            "runtimes": [{
                "runtime": "claude-code",
                "lanes": ["TEST"],
                "capabilities": ["docs", "python"],
                "control": {"mode": "hook_deny", "runner_kill": True},
            }],
            "limits": {"max_sessions": 2},
            "heartbeat_ttl_s": 60,
        },
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(host["host_id"] == "host/test" and not host["stale"],
       "register_host stores live Agent Host inventory")
    host_hb = store.heartbeat_host("host/test", active_sessions=1,
                                   actor=auth.actor(p), project=P)
    ok(host_hb["capacity"]["active_sessions"] == 1,
       "heartbeat_host renews capacity")
    hosts = store.list_agent_hosts(runtime="claude-code", lane="TEST",
                                   capability="docs", project=P)
    ok(len(hosts) == 1 and hosts[0]["host_id"] == "host/test",
       "list_agent_hosts filters by runtime, lane, and capability")
    host_status = store.host_status("host/test", project=P)
    ok(host_status["available_sessions"] == 1,
       "host_status reports remaining capacity")
    wake = store.request_wake(
        selector={"runtime": "claude-code", "agent_id": "claude/TEST#2",
                  "lane": "TEST", "capabilities": ["docs"]},
        reason="operator proof",
        source="test",
        policy={"deadline_seconds": 60},
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(wake["status"] == "pending" and wake["eligible_host_count"] == 1,
       "request_wake records a durable wake intent with eligible host count")
    wake_claim = store.claim_wake("host/test", wake["wake_id"],
                                  actor=auth.actor(p), project=P)
    ok(wake_claim["claimed"] is True and wake_claim["wake"]["status"] == "claimed",
       "claim_wake atomically assigns a wake to an eligible host")
    wake_claim_again = store.claim_wake("host/test", wake["wake_id"],
                                        actor=auth.actor(p), project=P)
    ok(wake_claim_again["claimed"] is False,
       "claim_wake refuses already-claimed wakes")
    wake_done = store.complete_wake(
        wake["wake_id"],
        runner_session_id="run_test",
        agent_id="claude/TEST#2",
        result={"started": True},
        actor=auth.actor(p),
        project=P,
    )
    ok(wake_done["status"] == "completed" and wake_done["runner_session_id"] == "run_test",
       "complete_wake records runtime start evidence")

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

    message_task = store.create_task({"workstream_id": "MSG", "title": "message fallback"},
                                     actor="seed", project=P)
    msg = store.send_agent_message(
        "codex/TEST#1",
        "claude/TEST#2",
        "stop before editing store.py",
        task_id=message_task["task_id"],
        requires_ack=True,
        signal="stop",
        priority=10,
        principal_id=p["id"],
        idem_key="stop-msg-1",
        project=P,
    )
    ok(msg["signal"] == "stop" and msg["priority"] == 10, "messages carry signal and priority")
    ok(msg["delivery_status"] == "unreachable" and msg["delivery"]["reason"] == "not_registered",
       "send_agent_message marks absent target agents as unreachable")
    ok(msg.get("fallback", {}).get("task_comment") is True,
       "unreachable directed messages advertise the visible task-comment fallback")
    message_task_after_send = store.get_task(message_task["task_id"], project=P)
    ok(any(a["kind"] == "message.delivery_unreachable"
           for a in message_task_after_send["activity"]),
       "unreachable directed messages write structured delivery failure activity")
    ok(any(a["kind"] == "comment" and a["actor"] == "switchboard/delivery"
           for a in message_task_after_send["activity"]),
       "unreachable directed messages add a visible fallback task comment")
    ok(msg.get("monitor_id") and msg.get("monitor", {}).get("kind") == "ack_deadline",
       "requires_ack creates a durable ack monitor")
    seconds_msg = store.send_agent_message(
        "codex/TEST#1",
        "claude/TEST#2",
        "ack timeout in seconds",
        task_id=message_task["task_id"],
        requires_ack=True,
        ack_timeout_seconds=2,
        project=P,
    )
    ok(1.0 <= (seconds_msg["ack_deadline"] - seconds_msg["sent_at"]) <= 3.0,
       "ack_timeout_seconds creates a real ack deadline")
    inbox = store.list_unacked_messages("claude/TEST#2", project=P)
    ok(inbox and inbox[0]["id"] == msg["id"], "inbox returns unacked directed message")
    no_ack_msg = store.send_agent_message(
        "codex/TEST#1",
        "claude/TEST#2",
        "fire-and-forget notice",
        task_id=message_task["task_id"],
        requires_ack=False,
        project=P,
    )
    inbox_after_no_ack = store.list_unacked_messages("claude/TEST#2", project=P)
    ok(all(m["id"] != no_ack_msg["id"] for m in inbox_after_no_ack),
       "fire-and-forget messages do not appear in unacked inbox")
    no_ack_status = store.get_message_status(no_ack_msg["id"], project=P)
    ok(no_ack_status["monitor"] is None and not no_ack_status["requires_ack"],
       "fire-and-forget messages remain stored without an ack monitor")
    ack = store.ack_message(msg["id"], response="denied before tool", actor="claude/TEST#2", project=P)
    ok(ack["acked_at"] is not None, "ack_message records receipt")
    acked_status = store.get_message_status(msg["id"], project=P)
    ok(acked_status["monitor"]["status"] == "resolved", "ack_message resolves durable monitor")
    timed = store.send_agent_message(
        "codex/TEST#1",
        "claude/TEST#2",
        "please ack quickly",
        task_id=message_task["task_id"],
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
    wake_timed = store.send_agent_message(
        "codex/TEST#1",
        "claude/TEST#2",
        "wake if absent",
        task_id=message_task["task_id"],
        requires_ack=True,
        ack_deadline_minutes=-1,
        on_ack_timeout="wake_target",
        project=P,
    )
    swept_wake = store.sweep_coordination_monitors(project=P)
    wake_events = [e for e in swept_wake["events"] if e.get("message_id") == wake_timed["id"]]
    ok(wake_events and wake_events[0].get("wake_id"),
       "ack-timeout monitor can create a wake intent")
    wake_status = store.get_message_status(wake_timed["id"], project=P)
    ok(wake_status["monitor"]["result"]["wake_status"] == "pending",
       "monitor result records the created wake intent")
    wakes = store.list_wake_intents(status="pending", runtime="claude-code", project=P)
    ok(any(w["source"] == f"monitor:{wake_status['monitor']['id']}" and
           w["selector"]["agent_id"] == "claude/TEST#2" for w in wakes),
       "list_wake_intents exposes monitor-created wakes")
    live_reg = store.register_agent(
        agent_id="claude/LIVE#1",
        runtime="claude-code",
        model="opus",
        lane="TEST",
        task_id=message_task["task_id"],
        control={"mode": "advisory_poll"},
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(live_reg["agent_id"] == "claude/LIVE#1", "test target can register as a live agent")
    live_msg = store.send_agent_message(
        "codex/TEST#1",
        "claude/LIVE#1",
        "live target check",
        task_id=message_task["task_id"],
        project=P,
    )
    ok(live_msg["delivery_status"] == "active" and live_msg["delivery"]["reachable"] is True,
       "send_agent_message marks live registered targets as active")

    first = store.create_task({"workstream_id": "TEST", "title": "first"}, actor="seed", project=P)
    second = store.create_task({"workstream_id": "TEST", "title": "second",
                                "depends_on": [first["task_id"]]}, actor="seed", project=P)
    unbound_task = store.create_task({"workstream_id": "UNBOUND", "title": "unbound write"},
                                     actor="seed", project=P)
    store.add_comment(unbound_task["task_id"], "env-mcp-token",
                      "posted through a shared MCP token", project=P)
    unbound = store.get_task(unbound_task["task_id"], project=P)
    ok(any(a["kind"] == "principal.unbound_write" for a in unbound["activity"]),
       "shared env-token task comments create an unbound principal audit")
    ok(unbound["identity"]["status"] == "unbound_live_runtime_possible" and
       unbound["identity"]["takeover_safe"] is False,
       "task detail surfaces recent unbound activity as takeover risk")
    unbound_msg = store.send_agent_message(
        "codex/TEST#1",
        "claude/UNBOUND#1",
        "are you still active?",
        task_id=unbound_task["task_id"],
        requires_ack=True,
        principal_id=p["id"],
        idem_key="unbound-msg-1",
        project=P,
    )
    ok(unbound_msg["delivery_status"] == "identity_unbound" and
       unbound_msg["fallback"]["takeover_safe"] is False,
       "directed message reports identity_unbound when task has recent unbound activity")
    unbound_takeover = store.claim_task(
        unbound_task["task_id"],
        "codex/TAKEOVER#1",
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="unbound-takeover-1",
        project=P,
    )
    ok(unbound_takeover.get("claimed") is False and
       unbound_takeover.get("reason") == "identity_unknown_recent_activity",
       "claim_task refuses takeover of a task with recent unbound activity")
    unbound_next = store.claim_next(
        agent_id="codex/TAKEOVER#1",
        lanes="UNBOUND",
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="unbound-next-1",
        project=P,
    )
    ok(unbound_next.get("claimed") is False and
       unbound_next["dispatch_reason"]["skipped"]["identity_unknown"] == 1,
       "claim_next skips tasks with recent unbound activity")
    unbound_override = store.claim_task(
        unbound_task["task_id"],
        "codex/TAKEOVER#1",
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="unbound-takeover-override-1",
        override_identity_risk=True,
        project=P,
    )
    ok(unbound_override.get("claimed") is True and
       "identity_override" in unbound_override["dispatch_reason"],
       "explicit override can claim after preserving identity-risk evidence")
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
    runner = store.upsert_runner_session(
        {
            "runner_session_id": "run_claimed",
            "host_id": "host/test",
            "agent_id": "codex/TEST#1",
            "runtime": "codex",
            "task_id": first["task_id"],
            "claim_id": claimed["claim_id"],
            "pid": 12345,
            "status": "running",
            "cwd": os.getcwd(),
            "control": {"tier": "T3", "managed_process": True, "runner_kill": True},
        },
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok("kill" in runner["available_actions"] and "snapshot" in runner["available_actions"],
       "managed runner session advertises snapshot and kill actions")
    runner_list = store.list_runner_sessions(task_id=first["task_id"], project=P)
    ok(runner_list and runner_list[0]["claim_id"] == claimed["claim_id"],
       "list_runner_sessions exposes current task claim")
    snap_req = store.request_runner_control(
        "run_claimed",
        "snapshot",
        reason="operator wants state",
        actor=auth.actor(p),
        principal_id=p["id"],
        project=P,
    )
    ok(snap_req.get("requested") and snap_req["status"] == "pending",
       "request_runner_control queues snapshot request")
    kill_req = store.request_runner_control(
        "run_claimed",
        "kill",
        reason="operator stop",
        options={"grace_seconds": 0.1, "signal": "TERM"},
        actor=auth.actor(p),
        principal_id=p["id"],
        project=P,
    )
    ok(kill_req.get("requested") and
       kill_req["snapshot"]["runner_session_id"] == "run_claimed",
       "kill request carries a pre-kill registry snapshot")
    control_claim = store.claim_runner_control_request(
        "host/test", kill_req["request_id"], actor="host/test", project=P)
    ok(control_claim.get("claimed"), "owning host can claim runner control request")
    control_done = store.complete_runner_control_request(
        kill_req["request_id"],
        result={"status": "killed"},
        snapshot={"runner_session_id": "run_claimed", "source": "supervisor", "status": "killed"},
        actor="host/test",
        project=P,
    )
    ok(control_done["status"] == "completed" and
       store.get_runner_session("run_claimed", project=P)["status"] == "killed",
       "runner control completion updates runner session state")
    ok(store.get_task(first["task_id"], project=P)["status"] == "In Progress",
       "runner kill control never marks task complete")
    unmanaged = store.upsert_runner_session(
        {
            "runner_session_id": "run_unmanaged",
            "agent_id": "codex/unmanaged",
            "runtime": "codex",
            "task_id": first["task_id"],
            "status": "running",
            "control": {"runner_kill": True},
        },
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(unmanaged["control"]["runner_kill"] is False and
       "kill" not in unmanaged["available_actions"],
       "unmanaged session cannot advertise runner_kill")
    refused_kill = store.request_runner_control(
        "run_unmanaged",
        "kill",
        reason="should fail closed",
        actor=auth.actor(p),
        principal_id=p["id"],
        project=P,
    )
    ok(refused_kill.get("requested") is False and refused_kill["status"] == "refused",
       "kill request for unmanaged session is visibly refused")
    exact_target = store.create_task({"workstream_id": "EXACT", "title": "human-selected"},
                                     actor="seed", project=P)
    exact_dep = store.create_task({"workstream_id": "EXACT", "title": "blocked exact",
                                   "depends_on": [exact_target["task_id"]]},
                                  actor="seed", project=P)
    exact_blocked = store.claim_task(
        exact_dep["task_id"],
        agent_id="codex/EXACT#1",
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(not exact_blocked.get("claimed") and exact_blocked["reason"] == "dependencies_unmet",
       "claim_task refuses exact tasks with unmet dependencies")
    exact = store.claim_task(
        exact_target["task_id"],
        agent_id="codex/EXACT#1",
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="claim-task-exact-1",
        project=P,
    )
    ok(exact.get("claimed") and exact["task"]["task_id"] == exact_target["task_id"],
       "claim_task claims the exact human-selected task")
    exact_again = store.claim_task(
        exact_target["task_id"],
        agent_id="codex/EXACT#1",
        principal_id=p["id"],
        actor=auth.actor(p),
        idem_key="claim-task-exact-1",
        project=P,
    )
    ok(exact_again["claim_id"] == exact["claim_id"], "claim_task is idempotent by idem_key")
    exact_busy = store.claim_task(
        exact_target["task_id"],
        agent_id="codex/EXACT#2",
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(not exact_busy.get("claimed") and exact_busy["reason"] == "active_claim",
       "claim_task refuses tasks with an active claim")
    exact_abandoned = store.abandon_claim(exact["claim_id"], "human redirected",
                                          actor=auth.actor(p), project=P)
    exact_after_abandon = store.get_task(exact_target["task_id"], project=P)
    ok(exact_abandoned.get("abandoned") and exact_after_abandon["status"] == "Not Started" and
       exact_after_abandon.get("assignee") is None,
       "abandon_claim clears claim-owned assignee when returning task to queue")
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
    late_terminal = store.create_task({"workstream_id": "TEST", "title": "late terminal"},
                                      actor="seed", project=P)
    late_claim = store.claim_task(
        late_terminal["task_id"],
        agent_id="codex/LATE#1",
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    store.mark_task_pr_opened(late_terminal["task_id"], 2, "https://example/pr/2",
                              "codex/late-terminal", "latehead",
                              actor="github-webhook", project=P)
    store.mark_task_merged(late_terminal["task_id"], "latemerge", 2, "https://example/pr/2",
                           "codex/late-terminal", "latehead",
                           actor="github-webhook", project=P)
    late_done = store.complete_claim(
        late_claim["claim_id"],
        evidence={"branch": "codex/late-terminal", "head_sha": "latehead",
                  "pr_number": 2, "pr_url": "https://example/pr/2",
                  "merged_sha": "latemerge"},
        actor=auth.actor(p),
        project=P,
    )
    late_after = store.get_task(late_terminal["task_id"], project=P)
    ok(late_done["status"] == "Done" and late_after["status"] == "Done",
       "late complete_claim preserves terminal Done status")
    ok(late_after["active_claims"] == [] and late_after["git_state"]["merged_sha"] == "latemerge",
       "late complete_claim releases claim and keeps merge provenance")
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
    cap_skip = store.create_task({
        "workstream_id": "SCORE",
        "title": "needs python",
        "description": "requires capabilities: python, tests",
        "risk_level": "Low",
    }, actor="seed", project=P)
    budget_skip = store.create_task({
        "workstream_id": "SCORE",
        "title": "spent too much",
        "description": "requires capabilities: docs",
        "risk_level": "Low",
    }, actor="seed", project=P)
    risk_skip = store.create_task({
        "workstream_id": "SCORE",
        "title": "too risky",
        "description": "requires capabilities: docs",
        "risk_level": "High",
    }, actor="seed", project=P)
    scored_winner = store.create_task({
        "workstream_id": "SCORE",
        "title": "right sized docs task",
        "description": "requires capabilities: docs",
        "risk_level": "Medium",
    }, actor="seed", project=P)
    store.report_usage(
        source="agent_report", confidence="reported", task_id=budget_skip["task_id"],
        agent_id="codex/SCORE#1", cost_usd=2.50, principal_id=p["id"], project=P)
    scored = store.claim_next(
        agent_id="codex/SCORE#1",
        lanes=["SCORE"],
        capabilities=["docs"],
        max_risk="medium",
        max_budget_usd=1.0,
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(scored.get("claimed") and scored["task"]["task_id"] == scored_winner["task_id"],
       "claim_next scores only candidates inside capability/risk/budget constraints")
    ok(scored["dispatch_reason"]["skipped"]["capability_mismatch"] >= 1 and
       scored["dispatch_reason"]["skipped"]["budget"] >= 1 and
       scored["dispatch_reason"]["skipped"]["risk"] >= 1,
       "claim_next explains skipped candidates by constraint")
    ok(scored["dispatch_reason"]["required_capabilities"] == ["docs"] and
       scored["budget"]["status"] == "ok" and scored["budget"]["remaining_usd"] == 1.0,
       "claim_next returns dispatch reason and budget status")
    ok(scored["recommendation"]["model_tier"] == "balanced",
       "claim_next returns model guidance from risk/budget/capability context")
    override = store.create_task({
        "workstream_id": "OVERRIDE",
        "title": "operator needs to redirect this claim",
        "risk_level": "Medium",
    }, actor="seed", project=P)
    override_claim = store.claim_next(
        agent_id="claude/OVERRIDE#1",
        lanes=["OVERRIDE"],
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(override_claim.get("claimed") and
       override_claim["task"]["task_id"] == override["task_id"],
       "claim_next creates a revocable active claim")
    override_detail = store.get_task(override["task_id"], project=P)
    ok(override_detail["active_claims"][0]["claim_id"] == override_claim["claim_id"],
       "task detail exposes active claims for operator UI")
    revoked = store.revoke_claim(
        override_claim["claim_id"],
        reason="operator redirect to Codex",
        reassign_to="codex/OVERRIDE#2",
        sort_order=1,
        partial_evidence={"branch": "claude/OVERRIDE-spike", "head_sha": "deadbeef"},
        expected_task_id=override["task_id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(revoked.get("revoked") and revoked["revoked_agent"] == "claude/OVERRIDE#1",
       "revoke_claim records an operator override")
    override_after = store.get_task(override["task_id"], project=P)
    ok(override_after["status"] == "Not Started" and
       override_after["assignee"] == "codex/OVERRIDE#2" and
       override_after["active_claims"] == [],
       "revoke_claim requeues the task and clears active claims")
    ok(override_after["git_state"]["head_sha"] == "deadbeef" and
       override_after["git_state"]["evidence"]["operator_revoke"]["branch"] == "claude/OVERRIDE-spike",
       "revoke_claim preserves partial evidence")
    revoke_msgs = store.list_unacked_messages("claude/OVERRIDE#1", project=P)
    ok(any(m.get("signal") == "claim_revoked" and m.get("requires_ack") for m in revoke_msgs),
       "revoke_claim sends an ack-required stop message")
    late_complete = store.complete_claim(
        override_claim["claim_id"],
        evidence={"head_sha": "late"},
        actor="claude/OVERRIDE#1",
        project=P,
    )
    ok(late_complete.get("error") == "claim is not active",
       "revoked claims cannot be completed later by the displaced agent")
    redirected = store.claim_next(
        agent_id="codex/OVERRIDE#2",
        lanes=["OVERRIDE"],
        principal_id=p["id"],
        actor=auth.actor(p),
        project=P,
    )
    ok(redirected.get("claimed") and redirected["task"]["task_id"] == override["task_id"],
       "redirected task can be claimed again after revoke")
    cleanup = store.abandon_claim(redirected["claim_id"], "test cleanup",
                                  actor=auth.actor(p), project=P)
    ok(cleanup.get("abandoned"), "abandon_claim still releases a current active claim")
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
    proposed = store.record_outcome(
        outcome_type="feature",
        title="claim_next unblocks dependent work",
        task_id=second["task_id"],
        claim_id=next_claim["claim_id"],
        evidence={"test": "runtime smoke"},
        project=P,
    )
    pending_tally = store.task_tally(second["task_id"], project=P)
    ok(pending_tally["outcomes"]["proposed"] == 1 and
       pending_tally["unit_cost"]["cost_per_verified_outcome"] is None,
       "proposed outcomes do not count in Tally denominator")
    verified = store.verify_outcome(
        proposed["id"], verifier="test", verification="unit",
        evidence={"verified_by": "test_switchboard_runtime"}, project=P)
    ok(verified["status"] == "verified" and verified["verified_at"],
       "verify_outcome marks outcome verified")
    kpi = store.create_kpi(
        name="verified scheduler outcomes", unit="items", direction="increase",
        baseline_value=0, target_value=10, project=P)
    link = store.link_outcome_to_kpi(
        verified["id"], kpi["id"], contribution=1, confidence="measured",
        rationale="one verified runtime outcome", project=P)
    ok(link["confidence"] == "measured", "KPI links preserve confidence")
    outcome_usage = store.report_usage(
        source="gateway",
        confidence="exact",
        outcome_id=verified["id"],
        agent_id="gateway/test",
        runtime="gateway",
        model="gpt-5-mini",
        cost_usd=0.08,
        request_id="outcome-spend-1",
        project=P,
    )
    ok(outcome_usage["task_id"] == second["task_id"],
       "outcome-attached spend resolves back to the owning task")
    tally = store.task_tally(second["task_id"], project=P)
    ok(tally["outcomes"]["verified"] == 1 and
       tally["unit_cost"]["cost_per_verified_outcome"] == 0.5,
       "verified outcomes count in Tally denominator")
    ok(tally["kpis"][0]["verified_contribution"] == 1.0 and
       tally["kpis"][0]["cost_per_contribution_unit"] == 0.5,
       "task_tally reports cost per KPI movement")
    kt = store.kpi_tally(kpi["id"], project=P)
    ok(kt["verified_contribution"] == 1.0 and
       kt["unit_cost"]["cost_per_contribution_unit"] == 0.5,
       "kpi_tally reports spend per verified contribution unit")
    pt = store.project_tally(project=P)
    ok(pt["totals"]["spend"]["cost_usd"] == 3.0 and
       pt["totals"]["unit_cost"]["cost_per_verified_outcome"] == 3.0,
       "project_tally reports project-level cost per verified outcome")
    ok(pt["totals"]["verified_kpi_contribution"] == 1.0 and
       pt["totals"]["unit_cost"]["cost_per_kpi_contribution_unit"] == 3.0,
       "project_tally reports cost per KPI contribution")
    ok(any(w["workstream_id"] == "TEST" and w["verified_outcomes"] == 1
           for w in pt["by_workstream"]),
       "project_tally groups economics by workstream")
    ok(any(t["task_id"] == second["task_id"] and t["outcomes"]["verified"] == 1
           for t in pt["by_task"]),
       "project_tally exposes task-level economics for the board surface")
    empty_done = store.complete_claim(
        next_claim["claim_id"],
        evidence={},
        final_status="Done",
        actor=auth.actor(p),
        project=P,
    )
    ok("error" in empty_done and "evidence required" in empty_done["error"],
       "complete_claim requires evidence for Done")
    agent_done = store.complete_claim(
        next_claim["claim_id"],
        evidence={"done": True, "verification": "agent ran the focused checks"},
        final_status="Done",
        actor=auth.actor(p),
        project=P,
    )
    ok(agent_done["status"] == "In Review" and
       agent_done.get("done_gate", {}).get("code") == "done_requires_merge_provenance",
       "complete_claim downgrades agent-requested Done to In Review")
    second_done = store.get_task(second["task_id"], project=P)
    ok(second_done["status"] == "In Review", "agent-completed task waits In Review until merge")
    agent_done_report = store.reconcile(project=P)
    ok(not any(f["code"] == "done_without_merged_sha" and f["task_id"] == second["task_id"]
               for f in agent_done_report["findings"]),
       "reconcile does not see agent-completed In Review as Done")
    missing_offline = store.mark_task_offline_done(second["task_id"], evidence={},
                                                  actor="switchboard/operator", project=P)
    ok(missing_offline.get("error") == "offline evidence required",
       "offline completion requires explicit evidence")
    invalid_offline_hash = store.mark_task_offline_done(
        second["task_id"],
        evidence={"claim_id": next_claim["claim_id"], "verification": "operator reviewed"},
        evidence_hash="sha256:not-computed-placeholder",
        actor="switchboard/operator",
        project=P,
    )
    ok(invalid_offline_hash.get("error") == "invalid_evidence_hash",
       "offline completion rejects invalid evidence hashes")
    premature_offline_task = store.create_task(
        {"workstream_id": "TEST", "title": "offline too early"}, actor="seed", project=P)
    premature_offline = store.mark_task_offline_done(
        premature_offline_task["task_id"],
        evidence={"review": "looked good"}, actor="switchboard/operator", project=P)
    ok(premature_offline.get("error") == "offline_done_requires_in_review",
       "offline completion requires In Review status first")
    offline_done = store.mark_task_offline_done(
        second["task_id"],
        evidence={"claim_id": next_claim["claim_id"],
                  "verification": "operator reviewed offline run log"},
        artifact_url="https://example.test/run-log",
        verifier="switchboard/operator",
        reviewed_at=1234,
        actor="switchboard/operator",
        project=P)
    ok(offline_done["status"] == "Done" and
       offline_done["provenance"]["type"] == "offline_evidence",
       "offline verifier can mark In Review non-PR task Done")
    idempotent_offline = store.mark_task_offline_done(
        second["task_id"],
        evidence={"claim_id": next_claim["claim_id"],
                  "verification": "operator reviewed offline run log"},
        artifact_url="https://example.test/run-log",
        verifier="switchboard/operator",
        reviewed_at=1234,
        actor="switchboard/operator",
        project=P)
    ok(idempotent_offline.get("idempotent"),
       "offline verifier replay with same evidence is idempotent")
    corrected_hash = "sha256:f29f6989f8fe2ffe9d52aa38bb43fb053967dbc052789ecfde99c73eb3095fc4"
    corrected_offline = store.mark_task_offline_done(
        second["task_id"],
        evidence={"claim_id": next_claim["claim_id"],
                  "verification": "operator corrected offline run log hash"},
        artifact_url="https://example.test/run-log",
        evidence_hash=corrected_hash,
        verifier="switchboard/operator",
        reviewed_at=1235,
        actor="switchboard/operator",
        project=P)
    ok(corrected_offline.get("corrected") and
       corrected_offline["provenance"]["evidence_hash"] == corrected_hash,
       "offline verifier can correct evidence metadata with provenance")
    second_offline = store.get_task(second["task_id"], project=P)
    ok(second_offline["git_state"]["provenance_type"] == "offline_evidence" and
       second_offline["provenance"]["artifact_url"] == "https://example.test/run-log" and
       second_offline["provenance"]["evidence_hash"] == corrected_hash,
       "offline Done records verifier evidence, artifact, and hash")
    ok(any(a["kind"] == "task.offline_evidence_corrected" for a in second_offline["activity"]),
       "offline evidence corrections are audited")
    listed_second = next(t for t in store.list_tasks(project=P) if t["task_id"] == second["task_id"])
    ok(listed_second["provenance"]["type"] == "offline_evidence",
       "task lists expose offline provenance for the UI badge")
    offline_report = store.reconcile(project=P)
    ok(not any(f["code"] == "done_without_merged_sha" and f["task_id"] == second["task_id"]
               for f in offline_report["findings"]),
       "reconcile accepts verifier-stamped offline Done provenance")

    bad = store.create_task({"workstream_id": "TEST", "title": "bad done"}, actor="seed", project=P)
    blocked_done = store.update_task(bad["task_id"], {"status": "Done"}, actor="legacy", project=P)
    ok(blocked_done.get("error") == "done_requires_merge_provenance",
       "update_task blocks naked Done without merge provenance")
    with store._conn(P) as c:
        c.execute("UPDATE tasks SET status='Done', updated_at=? WHERE task_id=?",
                  (0, bad["task_id"]))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (bad["task_id"], "legacy", "edit", "{\"status\":\"Done\"}", 0))
    report = store.reconcile(project=P)
    ok(any(f["code"] == "done_without_merged_sha" and f["task_id"] == bad["task_id"]
           for f in report["findings"]), "reconcile flags Done without merged_sha")
    alert = store.run_reconcile_alerts(
        project=P, alert_to="codex/test", actor="test/reconcile",
        dedupe_window_s=3600, now=123456)
    ok(alert["alert_sent"] and alert["finding_count"] >= 1,
       "reconcile_alerts sends an actionable alert when drift exists")
    alert_msgs = [m for m in store.list_unacked_messages("codex/test", project=P)
                  if m.get("signal") == "reconcile_alert"]
    ok(any(bad["task_id"] in m["message"] and "done_without_merged_sha" in m["message"]
           for m in alert_msgs), "reconcile_alert message names the drifting task")
    duplicate_alert = store.run_reconcile_alerts(
        project=P, alert_to="codex/test", actor="test/reconcile",
        dedupe_window_s=3600, now=123456)
    ok(duplicate_alert["deduped"] and not duplicate_alert["alert_sent"],
       "reconcile_alerts dedupes repeat findings inside the window")
    fixed = store.mark_task_merged(bad["task_id"], "legacyfix", actor="github-webhook", project=P)
    ok(fixed["git_state"]["merged_sha"] == "legacyfix",
       "merge webhook can stamp provenance onto a legacy Done task")
    fixed_report = store.reconcile(project=P)
    ok(not any(f["code"] == "done_without_merged_sha" and f["task_id"] == bad["task_id"]
               for f in fixed_report["findings"]), "reconcile clears Done task after merge provenance")
    ok(fixed_report["external_checks"]["git_reachability"] == "not_configured",
       "reconcile skips git reachability until canonical main is known")
    stale_task = store.create_task({"workstream_id": "TEST", "title": "stale active claim"},
                                   actor="seed", project=P)
    with store._conn(P) as c:
        c.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, status, claimed_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            ("claim-stale-test", stale_task["task_id"], "codex/stale", "active", 1, 2),
        )
    stale_report = store.reconcile(project=P)
    ok(any(f["code"] == "stale_task_claim" and f["task_id"] == stale_task["task_id"]
           for f in stale_report["findings"]), "reconcile flags stale active task claims")
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
    squashed = store.create_task({"workstream_id": "TEST", "title": "squashed branch head"},
                                 actor="seed", project=P)
    store.mark_task_merged(
        squashed["task_id"], head, branch="deleted-branch",
        head_sha="0000000000000000000000000000000000000000",
        actor="github-webhook", project=P)
    squash_report = store.reconcile(project=P)
    ok(not any(f["task_id"] == squashed["task_id"] and f["code"] == "head_sha_not_found"
               for f in squash_report["findings"]),
       "reconcile trusts merged_sha for Done tasks after squash branch deletion")
    open_pr = store.create_task({"workstream_id": "TEST", "title": "open PR head"},
                                actor="seed", project=P)
    store.mark_task_pr_opened(
        open_pr["task_id"], 999,
        "https://github.com/6th-Element-Labs/projectplanner/pull/999",
        branch=f"codex/{open_pr['task_id']}-open-pr",
        head_sha="1111111111111111111111111111111111111111",
        actor="github-webhook", project=P)
    original_github_pr = store._github_pr
    store._github_pr = lambda repo, pr_number, token="": {
        "merged_at": None,
        "html_url": f"https://github.com/{repo}/pull/{pr_number}",
        "base": {"ref": "master", "repo": {"default_branch": "master"}},
        "head": {"ref": f"codex/{open_pr['task_id']}-open-pr",
                 "sha": "1111111111111111111111111111111111111111"},
    }
    try:
        open_pr_report = store.reconcile(project=P)
    finally:
        store._github_pr = original_github_pr
    ok(not any(f["task_id"] == open_pr["task_id"] and f["code"] == "head_sha_not_found"
               for f in open_pr_report["findings"]),
       "reconcile trusts GitHub state for open PR heads not fetched locally")
    store.init_db("helm")
    helm_sha = "f" * 40
    helm_task = store.create_task({"workstream_id": "OFFLINE", "title": "foreign repo proof"},
                                  actor="seed", project="helm")
    store.update_task(helm_task["task_id"], {"status": "In Review"}, actor="seed", project="helm")
    store.mark_task_default_branch_commit(
        helm_task["task_id"], helm_sha, branch="main",
        subject=f"test({helm_task['task_id']}): foreign repo proof",
        actor="test", project="helm")
    store.update_canonical_main_sha(helm_sha, actor="test", project="helm")
    helm_report = store.reconcile(project="helm")
    ok(helm_report["external_checks"]["git_reachability"] == "skipped_repo_mismatch",
       "reconcile skips local git reachability when project repo differs from checkout repo")
    ok(not any(f["task_id"] == helm_task["task_id"] and
               f["code"] in ("merged_sha_not_found", "merged_sha_not_on_canonical_main")
               for f in helm_report["findings"]),
       "foreign project Done provenance does not get false local-git SHA findings")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
