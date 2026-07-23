#!/usr/bin/env python3
"""SIMPLIFY-18: exactly one authority answers "is this execution alive?".

ADR-0008 plane 1. `runner_sessions` + its execution lease is the canonical
execution-presence registry. Claims, Work Sessions, agent presence, and wake
intents are ownership, evidence, diagnostics, or transport -- never liveness.

Before this task the repo carried at least six terminal-status vocabularies and
several independent staleness predicates, so "is it alive?" had a different
answer depending on which module you asked. This pins the single authority and
the single vocabulary, and proves the deleted proxies stay deleted.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="simplify18-")
os.environ["PM_DB_PATH"] = os.path.join(TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


print("SIMPLIFY-18 one execution-liveness authority")

# --- 1. The canonical module exists and owns the vocabulary -----------------
from switchboard.domain import execution_liveness as live  # noqa: E402

ok(live.TERMINAL_EXECUTION_STATES == frozenset({
    "completed", "failed", "cancelled", "expired", "lost", "killed",
    "exited", "stopped"}),
   "one canonical terminal vocabulary")
ok(live.is_terminal("EXITED") and live.is_terminal("exited"),
   "terminal test is case-insensitive")
ok(not live.is_terminal("running") and not live.is_terminal(""),
   "non-terminal statuses are not terminal")

# --- 2. One liveness predicate: not terminal AND lease not expired ----------
NOW = 1_000_000.0
fresh = {"status": "running", "heartbeat_at": NOW, "heartbeat_ttl_s": 60}
expired = {"status": "running", "heartbeat_at": NOW - 3600, "heartbeat_ttl_s": 60}
term = {"status": "exited", "heartbeat_at": NOW, "heartbeat_ttl_s": 60}

ok(live.is_live(fresh, now=NOW) is True, "a fresh non-terminal execution is live")
ok(live.is_live(expired, now=NOW) is False, "an expired lease is not live")
ok(live.is_live(term, now=NOW) is False, "a terminal status is not live regardless of heartbeat")
ok(live.expires_at(fresh) == NOW + 60, "expiry is heartbeat + ttl")

# --- 3. Every surface delegates; no private vocabularies remain -------------
import inspect  # noqa: E402

sources = {
    "task_session": ROOT / "src/switchboard/application/queries/task_session.py",
    "co_fleet": ROOT / "co_fleet.py",
    "agent_host": ROOT / "adapters/agent_host.py",
}
for name, path in sources.items():
    text = path.read_text(encoding="utf-8")
    # A module may import/alias the canonical set, but must not re-spell it.
    inline = ('"completed", "failed", "cancelled", "expired"' in text
              or "'completed', 'failed', 'cancelled', 'expired'" in text)
    ok(not inline, f"{name} does not re-declare the terminal vocabulary inline")

# --- 4. Claims / Work Sessions / presence are not liveness ------------------
import store  # noqa: E402

P = "switchboard"
store.init_db(P)
task = store.create_task({"workstream_id": "SIMPLIFY", "title": "liveness authority"},
                         actor="seed", project=P)
tid = task["task_id"]
agent = "claude/SIMPLIFY-18-probe"
store.register_agent(agent, "claude-code", task_id=tid, ttl_s=300, project=P)
claim = store.claim_task(tid, agent_id=agent, project=P)
ok(claim.get("claim_id"), "probe task claimed")

# A live claim + live presence, but NO runner row -> not executing.
presence = store.list_active_agents(project=P)
ok(any(a["agent_id"] == agent for a in presence),
   "agent presence exists (diagnostic only)")
runners = store.list_runner_sessions(task_id=tid, project=P)
ok(runners == [], "no runner row exists for the claimed task")
# Layering: pure predicates live in domain; the DB-backed authority lives with
# runner_sessions in the storage layer and consumes those predicates.
ok(store.task_has_live_execution(tid, project=P) is False,
   "a live claim and live presence do NOT constitute a live execution")

# --- 5. A stale claim/Work Session cannot block start_task (acceptance 3) ---
ok(store.blocking_execution_for(tid, project=P) is None,
   "no execution lease blocks start_task when only a claim exists")
ok(not hasattr(live, "task_has_live_execution"),
   "the domain module stays pure -- no storage access leaked into it")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
