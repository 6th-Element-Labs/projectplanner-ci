#!/usr/bin/env python3
"""Dispatch tests for the closure verifier hand-off (DELIVERABLES-17).

Covers deliverable_closure.request_closure_verification — the "Verify & stamp
closure" operator dispatch that assembles deliverable context + a resolved gate
list + a closure prompt template, delivers it to a verifier's mailbox, and rouses
that verifier with a lane-less message_only wake. Also covers the gate_manifest /
build_closure_prompt helpers and the error/idempotency paths.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="closure-dispatch-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import store  # noqa: E402
import deliverable_closure as dc  # noqa: E402

passed = failed = 0
PROJ = "qa-cl17"
DELIV = "qa-cl17-deliv"


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def activity_rows(kind):
    with store._conn(PROJ) as c:
        return c.execute("SELECT payload FROM activity WHERE kind=?", (kind,)).fetchall()


def mailbox(to_agent):
    with store._conn(PROJ) as c:
        return c.execute("SELECT message, signal FROM agent_messages WHERE to_agent=?",
                         (to_agent,)).fetchall()


def wake_by_id(wake_id):
    return next((w for w in store.list_wake_intents(project=PROJ)
                 if w.get("wake_id") == wake_id), None)


store.init_project_registry()
store.init_db("switchboard")
store.create_project("Closure17 QA", project_id=PROJ, actor="test")
for title in ("A", "B", "C"):
    store.create_task({"workstream_id": "CL", "title": title}, actor="test", project=PROJ)
for tid, sha, pr in (("CL-1", "sha-a", 1), ("CL-2", "sha-b", 2), ("CL-3", "sha-c", 3)):
    store.mark_task_merged(tid, sha, pr_number=pr, project=PROJ)
store.create_deliverable({
    "id": DELIV, "title": "QA17", "status": "in_progress",
    "end_state": "The whole thing ships and is verified.",
    "acceptance_criteria": ["ships", "is verified"],
    "proof_requirements": {
        "schema": "switchboard.deliverable_proof_requirements.v1",
        "gates": [
            {"id": "scope", "required": True},
            {"id": "store:a", "kind": "store_check", "check": "task_terminal",
             "params": {"task_id": "CL-1"}, "required": True},
            {"id": "harness:ship", "kind": "script",
             "command": ["python3", "-c", "import sys;sys.exit(0)"], "required": True},
        ],
    },
}, actor="test", project=PROJ)
for tid in ("CL-1", "CL-2", "CL-3"):
    store.link_task_to_deliverable(DELIV, PROJ, tid, actor="test", project=PROJ)

# --- 1. dispatch: assembles context + gates + prompt, sends message, queues wake ---
res = dc.request_closure_verification(DELIV, PROJ, actor="operator")
ok(res.get("dispatched") is True, "dispatch returns dispatched=True")
ok(bool(res.get("wake_id")) and bool(res.get("message_id")),
   "dispatch returns a wake_id and a message_id")
ok(res.get("agent_id") == f"verifier/closure/{DELIV}",
   "default verifier agent_id is verifier/closure/<deliverable>")
ok(res.get("signal") == dc.CLOSURE_VERIFICATION_SIGNAL, "closure-verification signal set")
ok(isinstance(res.get("work_hosts_online"), int) and isinstance(res.get("queued"), bool),
   "work_hosts_online (int) + queued (bool) surface host availability")

# --- 2. gate manifest: scope + store + script, only the script runs in the agent ---
gates = {g["id"]: g for g in res.get("gates") or []}
ok(set(gates) == {"scope", "store:a", "harness:ship"}, "all resolved gates listed (scope+functional)")
ok(gates["harness:ship"]["runs_in_agent"] is True, "a script gate is flagged runs_in_agent")
ok(gates["scope"]["runs_in_agent"] is False and gates["store:a"]["runs_in_agent"] is False,
   "scope + store_check gates are graded server-side, not in the agent")

# --- 3. prompt template: names the deliverable, gates, and the closing MCP call ----
prompt = res.get("prompt") or ""
ok(DELIV in prompt and "verify_deliverable_closure" in prompt,
   "prompt references the deliverable and the verify_deliverable_closure call")
ok("status=done" in prompt and "harness:ship" in prompt,
   "prompt forbids status=done and lists the command gate to run")
ok("is verified" in prompt, "prompt inlines the acceptance criteria")

# --- 4. mailbox: the prompt is delivered to the verifier's inbox with the signal ---
box = mailbox(f"verifier/closure/{DELIV}")
ok(len(box) == 1 and box[0]["message"] == prompt and box[0]["signal"] == dc.CLOSURE_VERIFICATION_SIGNAL,
   "the closure prompt is stored in the verifier mailbox once, with the signal")

# --- 5. wake: lane-less, mode=message_only, carries deliverable + gate ids ---------
wake = wake_by_id(res["wake_id"])
ok(wake is not None, "the wake intent is persisted")
sel = (wake or {}).get("selector") or {}
pol = (wake or {}).get("policy") or {}
ok(sel.get("agent_id") == f"verifier/closure/{DELIV}" and not sel.get("lane"),
   "wake targets the verifier agent with no lane (inbox-only, never a task claim)")
ok(pol.get("mode") == "message_only" and pol.get("kind") == "closure_verification",
   "wake policy marks a message_only closure_verification dispatch")
ok(pol.get("deliverable_id") == DELIV and pol.get("message_id") == res["message_id"]
   and set(pol.get("gate_ids") or []) == set(gates),
   "wake policy carries the deliverable, message id, and gate ids")

# --- 6. audit stamp: one deliverable.closure_verification_requested per dispatch ----
ok(len(activity_rows("deliverable.closure_verification_requested")) == 1,
   "a deliverable.closure_verification_requested activity is stamped once")

# --- 7. idempotency: a re-dispatch dedupes to the same wake + message --------------
res2 = dc.request_closure_verification(DELIV, PROJ, actor="operator")
ok(res2.get("wake_id") == res["wake_id"] and res2.get("message_id") == res["message_id"],
   "a repeat dispatch is idempotent (same wake + message)")
ok(len(mailbox(f"verifier/closure/{DELIV}")) == 1, "no duplicate mailbox entry on re-dispatch")

# --- 8. a distinct target agent is a distinct dispatch (no idempotency conflict) ---
res3 = dc.request_closure_verification(DELIV, PROJ, agent_id="cursor/verify-1", actor="operator")
ok(res3.get("dispatched") is True and res3.get("agent_id") == "cursor/verify-1",
   "an explicit agent_id targets that verifier")
ok(res3.get("wake_id") != res["wake_id"] and "error" not in res3,
   "a different verifier yields a distinct wake, not an idempotency conflict")

# --- 9. error paths (fail closed, nothing dispatched) -----------------------------
ok("error" in dc.request_closure_verification("nope", PROJ)
   and not dc.request_closure_verification("nope", PROJ).get("dispatched"),
   "dispatch on a missing deliverable returns an error and does not dispatch")
bad_waiver = dc.request_closure_verification(DELIV, PROJ, waivers=[{"reason": "no task id"}])
ok("error" in bad_waiver, "an invalid waiver (no task_id) is rejected before dispatch")

# --- 10. helpers are pure and usable standalone -----------------------------------
import deliverable_gates  # noqa: E402
deliv = store.get_deliverable(DELIV, project=PROJ)
resolved = deliverable_gates.resolve_gates(deliv.get("proof_requirements"), include_scope=True)
manifest = dc.gate_manifest(resolved)
ok([g["id"] for g in manifest] == [g["id"] for g in resolved],
   "gate_manifest preserves gate order")
ok("(none recorded)" in dc.build_closure_prompt({"id": "x"}, [], project=PROJ),
   "build_closure_prompt handles a deliverable with no criteria/gates")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
