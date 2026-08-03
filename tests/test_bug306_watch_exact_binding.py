#!/usr/bin/env python3
"""BUG-306: Watch upgrades provisional relay identity once, then fails closed."""
from __future__ import annotations

from path_setup import ROOT  # noqa: E402,F401

from switchboard.application import runner_pty_relay as relay  # noqa: E402


def binding(sid: str, *, claim: str, work_session: str,
            project: str = "switchboard", task: str = "BUG-306") -> dict[str, str]:
    return {
        "tenant_id": "tenant/default",
        "user_id": "operator",
        "project_id": project,
        "task_id": task,
        "claim_id": claim,
        "work_session_id": work_session,
        "runner_session_id": sid,
        "host_id": "host/bug306",
        "wake_id": "wake-bug306",
        "execution_connection_id": "execconn/bug306",
        "source_sha": "abc123",
        "permission_profile": "operator_watch",
    }


sid = "run_bug306"
provisional = f"direct/{sid}"
direct = binding(sid, claim=provisional, work_session=provisional)
exact = binding(sid, claim="taskclaim-bug306", work_session="worksession-bug306")

# Host-first is the live failure: the old Host tunnel creates the in-memory
# relay at direct/*, then a freshly minted Watch ticket carries durable ids.
hub = relay.RelayHub()
_, direct_host = relay.mint_host_tunnel_ticket(direct, ttl_seconds=120)
_, exact_browser = relay.mint_capability_ticket(exact, ["watch"], ttl_seconds=120)
assert hub.attach_host(sid, lambda _frame: True, binding=direct_host)["ok"]
assert hub.attach_browser(sid, exact_browser, lambda _frame: True)["ok"]
info = hub.session_info(sid) or {}
assert info["binding"]["claim_id"] == "taskclaim-bug306"
assert info["binding"]["work_session_id"] == "worksession-bug306"

# An older provisional browser ticket may reconnect, but never downgrades the
# now-exact in-memory identity.
_, direct_browser = relay.mint_capability_ticket(direct, ["watch"], ttl_seconds=120)
assert hub.attach_browser(sid, direct_browser, lambda _frame: True)["ok"]
assert (hub.session_info(sid) or {})["binding"]["claim_id"] == "taskclaim-bug306"

# A second exact authority cannot replace the durable lifecycle identity.
conflicting = binding(sid, claim="taskclaim-other", work_session="worksession-other")
_, conflict_browser = relay.mint_capability_ticket(conflicting, ["watch"], ttl_seconds=120)
assert hub.attach_browser(sid, conflict_browser, lambda _frame: True)["error"] == "claim_id_mismatch"

# Immutable project/task/host/wake identity is still fenced.
wrong_project = binding(
    sid, claim="taskclaim-bug306", work_session="worksession-bug306",
    project="helm")
_, wrong_project_browser = relay.mint_capability_ticket(
    wrong_project, ["watch"], ttl_seconds=120)
assert hub.attach_browser(sid, wrong_project_browser, lambda _frame: True)["error"] == "project_id_mismatch"

# Browser-first is symmetrical: a provisional Watch reservation may be upgraded
# by the exact Host ticket without changing the runner/task/project/host/wake.
sid2 = "run_bug306_browser_first"
pending = f"pending/{sid2}"
pending_binding = binding(sid2, claim=pending, work_session=pending)
pending_binding["permission_profile"] = "operator_watch_pending"
exact2 = binding(sid2, claim="taskclaim-browser-first", work_session="worksession-browser-first")
hub2 = relay.RelayHub()
_, pending_browser = relay.mint_capability_ticket(
    pending_binding, ["watch"], ttl_seconds=120)
_, exact_host = relay.mint_host_tunnel_ticket(exact2, ttl_seconds=120)
assert hub2.attach_browser(sid2, pending_browser, lambda _frame: True)["ok"]
assert hub2.attach_host(sid2, lambda _frame: True, binding=exact_host)["ok"]
info2 = hub2.session_info(sid2) or {}
assert info2["binding"]["claim_id"] == "taskclaim-browser-first"
assert info2["binding"]["work_session_id"] == "worksession-browser-first"

print("PASS BUG-306 provisional relay identity upgrades exactly once and stays fenced")
