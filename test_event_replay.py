#!/usr/bin/env python3
"""Tests for event replay and dispatch simulation (RECON-8)."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="event-replay-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")

import event_replay  # noqa: E402
import store  # noqa: E402

P = "replay-test"
store.create_project("Replay Test", project_id=P, actor="test")
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


store.init_db(P)
alpha = store.create_task({"workstream_id": "RECON", "title": "Replay alpha",
                           "sort_order": 10, "risk_level": "Low"}, actor="test", project=P)
beta = store.create_task({"workstream_id": "RECON", "title": "Replay beta",
                          "sort_order": 20, "depends_on": [alpha["task_id"]],
                          "risk_level": "High"}, actor="test", project=P)
alpha_id = alpha["task_id"]
beta_id = beta["task_id"]

claim = store.claim_task(alpha_id, "agent/replay", actor="test", project=P)
ok(claim.get("claimed"), "claim_task seeds lifecycle for replay")

complete = store.complete_claim(
    claim["claim_id"],
    evidence='{"branch":"codex/RECON-R1-replay","head_sha":"abc123","pr_url":"https://example/pr/1"}',
    actor="test",
    project=P,
)
ok(complete.get("status") == "In Review", "complete_claim moves task to In Review")

store.mark_task_merged(
    alpha_id, "merge111", pr_number=1,
    pr_url="https://example/pr/1", branch="codex/RECON-R1-replay",
    head_sha="abc123", actor="test", project=P,
)

with store._conn(P) as c:
    cursor_before_merge = int(c.execute(
        "SELECT id FROM activity WHERE kind='git.pr_merged' AND task_id=?",
        (alpha_id,),
    ).fetchone()[0]) - 1

verify = event_replay.verify_board(P)
ok(verify["ok"], "replay verify matches live board after PR lifecycle")
ok(verify["events_replayed"] > 0, "replay verify replays activity events")

sim_blocked = event_replay.simulate_dispatch(
    P, "agent/replay", lanes="RECON", capabilities="python",
)
ok(sim_blocked.get("claimed") and sim_blocked["task_id"] == beta_id,
   "dispatch simulation picks dependent-ready task after alpha Done")
ok(sim_blocked.get("simulated") and not sim_blocked.get("claim_id"),
   "dispatch simulation does not create live claims")

claim_beta = store.claim_task(beta_id, "agent/live", actor="test", project=P)
ok(claim_beta.get("claimed"), "live claim still works after simulation")

sim_after = event_replay.simulate_dispatch(P, "agent/other", lanes="RECON")
ok(not sim_after.get("claimed") and sim_after.get("reason") == "no_unblocked_work",
   "simulation respects replayed active-claim state when beta is claimed")

store.abandon_claim(claim_beta["claim_id"], "test cleanup", actor="test", project=P)

partial = event_replay.replay_board(P, until_cursor=cursor_before_merge)
ok(partial.tasks[alpha_id]["status"] != "Done",
   "historical replay before merge leaves task not Done")

mismatch = event_replay.verify_board(P, until_cursor=cursor_before_merge)
ok(not mismatch["ok"] and mismatch["mismatch_count"] >= 1,
   "verify reports drift when live board advanced past replay cursor")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
