"""Event replay and dispatch simulation for Switchboard.

Reconstructs lifecycle state from append-only activity events without mutating
live board data. Used to verify derived state and preflight claim_next policies.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import store

SCHEMA = "switchboard.event_replay.v1"
POLICY_VERSION = "replay.simulate_dispatch.v1"

MUTABLE_TASK_FIELDS = (
    "status", "assignee", "depends_on", "phase", "risk_level", "is_blocking",
    "sort_order", "title", "description", "owner_org", "owner_person_or_role",
    "deliverable", "entry_criteria", "exit_criteria",
)

GIT_STATE_FIELDS = (
    "branch", "head_sha", "pr_number", "pr_url", "merged_sha", "merged_at",
    "in_main_content",
)


def _json_payload(raw: Any) -> Any:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


@dataclass
class ReplaySnapshot:
  """Derived board state at an activity cursor."""

  schema: str = SCHEMA
  project: str = ""
  from_cursor: int = 0
  until_cursor: int = 0
  events_replayed: int = 0
  tasks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
  git_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
  active_claims: Dict[str, Dict[str, Any]] = field(default_factory=dict)
  lifecycles: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
  wake_events: int = 0
  message_events: int = 0

  def task_row(self, task_id: str) -> Optional[Dict[str, Any]]:
      row = self.tasks.get(task_id)
      if not row:
          return None
      out = dict(row)
      out["git_state"] = dict(self.git_states.get(task_id) or {})
      return out


def _baseline_tasks(c: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        task = store._task_row(row)
        out[task["task_id"]] = task
    return out


def _live_git_states(c: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    return {
        row["task_id"]: store._git_state_row(row)
        for row in c.execute("SELECT * FROM task_git_state ORDER BY task_id").fetchall()
    }


def _fetch_events(c: sqlite3.Connection, *,
                  from_cursor: int = 0,
                  until_cursor: Optional[int] = None,
                  task_id: str = "") -> List[sqlite3.Row]:
    clauses = ["id > ?"]
    vals: List[Any] = [int(from_cursor or 0)]
    if until_cursor:
        clauses.append("id <= ?")
        vals.append(int(until_cursor))
    if task_id:
        clauses.append("(task_id=? OR task_id IS NULL)")
        vals.append(task_id)
    sql = (
        "SELECT id, task_id, actor, kind, payload, created_at "
        f"FROM activity WHERE {' AND '.join(clauses)} ORDER BY id"
    )
    return c.execute(sql, vals).fetchall()


def _ensure_task(snapshot: ReplaySnapshot, task_id: str,
                 payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if task_id not in snapshot.tasks:
        payload = payload or {}
        snapshot.tasks[task_id] = {
            "task_id": task_id,
            "status": "Not Started",
            "assignee": None,
            "depends_on": [],
            "_wsId": (payload.get("workstream_id") or payload.get("_wsId") or "").strip(),
            "title": payload.get("title") or "",
            "sort_order": int(payload.get("sort_order") or 0),
            "is_blocking": bool(payload.get("is_blocking") or False),
            "risk_level": payload.get("risk_level") or "",
            "agent_state": {},
        }
        snapshot.git_states.setdefault(task_id, {})
        snapshot.lifecycles.setdefault(task_id, [])
    return snapshot.tasks[task_id]


def _record_lifecycle(snapshot: ReplaySnapshot, task_id: str, event: Dict[str, Any]) -> None:
    if not task_id:
        return
    snapshot.lifecycles.setdefault(task_id, []).append(event)


def _merge_git_state(snapshot: ReplaySnapshot, task_id: str,
                     updates: Dict[str, Any]) -> None:
    state = dict(snapshot.git_states.get(task_id) or {})
    evidence = dict(state.get("evidence") or {})
    for key, value in updates.items():
        if key == "evidence" and isinstance(value, dict):
            evidence.update(value)
        elif value is not None:
            state[key] = value
    if evidence:
        state["evidence"] = evidence
    snapshot.git_states[task_id] = state


def _apply_event(snapshot: ReplaySnapshot, row: sqlite3.Row) -> None:
    task_id = (row["task_id"] or "").strip()
    kind = (row["kind"] or "").strip()
    payload = _json_payload(row["payload"])
    event = {
        "cursor": int(row["id"]),
        "kind": kind,
        "actor": row["actor"],
        "created_at": row["created_at"],
        "payload": payload,
    }

    if kind in ("wake.requested", "wake.completed", "wake.cancelled"):
        snapshot.wake_events += 1
        return
    if kind in ("message.sent", "message.acked", "agent.message"):
        snapshot.message_events += 1
        return

    if kind == "create" and task_id:
        task = _ensure_task(snapshot, task_id, payload)
        if payload.get("title"):
            task["title"] = payload["title"]
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "edit" and task_id:
        task = _ensure_task(snapshot, task_id)
        for key in MUTABLE_TASK_FIELDS:
            if key in payload:
                task[key] = payload[key]
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "task.claimed" and task_id:
        task = _ensure_task(snapshot, task_id)
        task["status"] = "In Progress"
        task["assignee"] = payload.get("agent_id") or row["actor"]
        snapshot.active_claims[task_id] = {
            "claim_id": payload.get("claim_id"),
            "agent_id": payload.get("agent_id") or row["actor"],
            "cursor": int(row["id"]),
        }
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "task.claim.completed" and task_id:
        task = _ensure_task(snapshot, task_id)
        next_status = (payload.get("next_status") or "In Review").strip()
        if next_status.lower() != "done":
            task["status"] = next_status
        snapshot.active_claims.pop(task_id, None)
        evidence = payload.get("evidence") or {}
        if isinstance(evidence, dict):
            git_updates = {}
            for key in ("branch", "head_sha", "pr_number", "pr_url"):
                if evidence.get(key) not in (None, ""):
                    git_updates[key] = evidence.get(key)
            if evidence.get("head_sha"):
                git_updates["pushed_at"] = row["created_at"]
            if git_updates:
                _merge_git_state(snapshot, task_id, git_updates)
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "task.claim.abandoned" and task_id:
        task = _ensure_task(snapshot, task_id)
        if task.get("status") == "In Progress":
            task["status"] = "Not Started"
            if task.get("assignee") == payload.get("agent_id"):
                task["assignee"] = None
        snapshot.active_claims.pop(task_id, None)
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "git.pr_opened" and task_id:
        task = _ensure_task(snapshot, task_id)
        if task.get("status") not in ("Done", "Cancelled", "Canceled"):
            task["status"] = "In Review"
        _merge_git_state(snapshot, task_id, {
            "branch": payload.get("branch"),
            "head_sha": payload.get("head_sha"),
            "pr_number": payload.get("pr_number"),
            "pr_url": payload.get("pr_url"),
            "evidence": payload,
        })
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "git.pr_merged" and task_id:
        task = _ensure_task(snapshot, task_id)
        task["status"] = "Done"
        head_sha = payload.get("head_sha")
        _merge_git_state(snapshot, task_id, {
            "branch": payload.get("branch"),
            "head_sha": head_sha,
            "pr_number": payload.get("pr_number"),
            "pr_url": payload.get("pr_url"),
            "merged_sha": payload.get("merged_sha"),
            "merged_at": row["created_at"],
            "pushed_at": row["created_at"] if head_sha else None,
            "in_main_content": True,
            "evidence": payload,
        })
        snapshot.active_claims.pop(task_id, None)
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "git.pr_evidence_hydrated" and task_id:
        _merge_git_state(snapshot, task_id, {
            "branch": payload.get("branch"),
            "head_sha": payload.get("head_sha"),
            "pr_number": payload.get("pr_number"),
            "pr_url": payload.get("pr_url"),
            "evidence": payload,
        })
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "git.default_branch_backfilled" and task_id:
        task = _ensure_task(snapshot, task_id)
        task["status"] = "Done"
        commit_sha = payload.get("commit_sha") or payload.get("merged_sha")
        _merge_git_state(snapshot, task_id, {
            "merged_sha": commit_sha,
            "head_sha": commit_sha,
            "merged_at": row["created_at"],
            "in_main_content": True,
            "evidence": payload,
        })
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "task.offline_verified" and task_id:
        task = _ensure_task(snapshot, task_id)
        task["status"] = "Done"
        offline = payload.get("offline_evidence") or payload
        _merge_git_state(snapshot, task_id, {
            "merged_at": row["created_at"],
            "in_main_content": True,
            "evidence": {"offline_evidence": offline},
        })
        snapshot.active_claims.pop(task_id, None)
        _record_lifecycle(snapshot, task_id, event)
        return

    if kind == "task.done_blocked" and task_id:
        _record_lifecycle(snapshot, task_id, event)
        return

    if task_id and kind.startswith(("task.", "git.", "claim.", "bug.")):
        _record_lifecycle(snapshot, task_id, event)


def replay_board(project: str, *,
                 from_cursor: int = 0,
                 until_cursor: Optional[int] = None,
                 task_id: str = "",
                 baseline: Optional[Dict[str, Dict[str, Any]]] = None) -> ReplaySnapshot:
    store.init_db(project)
    with store._conn(project) as c:
        if until_cursor is None:
            until_cursor = int(c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0] or 0)
        baseline = baseline or _baseline_tasks(c)
        snapshot = ReplaySnapshot(
            project=project,
            from_cursor=int(from_cursor or 0),
            until_cursor=int(until_cursor or 0),
            tasks={
                tid: {
                    **dict(task),
                    "status": "Not Started",
                    "assignee": None,
                }
                for tid, task in baseline.items()
            },
            git_states={tid: {} for tid in baseline},
        )
        for row in _fetch_events(c, from_cursor=from_cursor,
                                 until_cursor=until_cursor, task_id=task_id):
            _apply_event(snapshot, row)
            snapshot.events_replayed += 1
        return snapshot


def _compare_git_state(derived: Dict[str, Any], live: Dict[str, Any]) -> List[str]:
    mismatches = []
    for key in GIT_STATE_FIELDS:
        dval = derived.get(key)
        lval = live.get(key)
        if dval in (None, "", 0) and lval in (None, "", 0):
            continue
        if str(dval or "") != str(lval or ""):
            mismatches.append(key)
    return mismatches


def verify_board(project: str, *,
                 from_cursor: int = 0,
                 until_cursor: Optional[int] = None,
                 task_id: str = "") -> Dict[str, Any]:
    store.init_db(project)
    snapshot = replay_board(project, from_cursor=from_cursor,
                            until_cursor=until_cursor, task_id=task_id)
    mismatches: List[Dict[str, Any]] = []
    with store._conn(project) as c:
        live_tasks = _baseline_tasks(c)
        live_git = _live_git_states(c)
        scope = [task_id] if task_id else sorted(live_tasks)
        for tid in scope:
            if tid not in live_tasks:
                continue
            live = live_tasks[tid]
            derived = snapshot.tasks.get(tid) or {}
            diff: Dict[str, Any] = {"task_id": tid}
            if (derived.get("status") or "") != (live.get("status") or ""):
                diff["status"] = {
                    "derived": derived.get("status"),
                    "live": live.get("status"),
                }
            if (derived.get("assignee") or None) != (live.get("assignee") or None):
                diff["assignee"] = {
                    "derived": derived.get("assignee"),
                    "live": live.get("assignee"),
                }
            git_diff = _compare_git_state(
                snapshot.git_states.get(tid) or {},
                live_git.get(tid) or {},
            )
            if git_diff:
                diff["git_state_fields"] = git_diff
                diff["derived_git_state"] = snapshot.git_states.get(tid) or {}
                diff["live_git_state"] = live_git.get(tid) or {}
            if len(diff) > 1:
                mismatches.append(diff)
    return {
        "schema": SCHEMA,
        "project": project,
        "ok": not mismatches,
        "from_cursor": snapshot.from_cursor,
        "until_cursor": snapshot.until_cursor,
        "events_replayed": snapshot.events_replayed,
        "tasks_checked": len(scope),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "wake_events": snapshot.wake_events,
        "message_events": snapshot.message_events,
        "active_claims": list(snapshot.active_claims.keys()),
    }


def simulate_dispatch(project: str, agent_id: str, *,
                      from_cursor: int = 0,
                      until_cursor: Optional[int] = None,
                      lanes: Any = None,
                      capabilities: Any = None,
                      max_risk: str = "",
                      max_budget_usd: Optional[float] = None,
                      deliverable_id: str = "",
                      tally_loader: Optional[Callable[[sqlite3.Connection, str], Dict[str, Any]]] = None,
                      ) -> Dict[str, Any]:
    """Dry-run claim_next scoring against a replayed snapshot (no writes)."""
    lanes_list = store.coerce_csv_list(lanes)
    caps_list = store.coerce_csv_list(capabilities)
    lane_set = {x.strip().upper() for x in lanes_list}
    cap_set = {x.strip().lower() for x in caps_list}
    max_risk_value = store._risk_value(max_risk)
    snapshot = replay_board(project, from_cursor=from_cursor, until_cursor=until_cursor)
    by_id = snapshot.tasks
    active_claims = set(snapshot.active_claims)
    eligible: List[Tuple[float, int, str, Dict[str, Any], Dict[str, Any]]] = []
    skipped = {"active_claim": 0, "status": 0, "lane": 0, "dependencies": 0,
               "human_approval": 0, "capability_mismatch": 0, "risk": 0, "budget": 0}
    store.init_db(project)
    with store._conn(project) as c:
        loader = tally_loader or store._task_tally_snapshot
        for t in sorted(by_id.values(), key=lambda row: (row.get("sort_order") or 0, row["task_id"])):
            tid = t["task_id"]
            if deliverable_id:
                links = c.execute(
                    "SELECT 1 FROM deliverable_task_links "
                    "WHERE task_id=? AND deliverable_id=? LIMIT 1",
                    (tid, deliverable_id),
                ).fetchone()
                if not links:
                    skipped["lane"] += 1
                    continue
            if tid in active_claims:
                skipped["active_claim"] += 1
                continue
            if t.get("status") not in store.READY_TASK_STATUSES:
                skipped["status"] += 1
                continue
            if lane_set and (t.get("_wsId") or "").upper() not in lane_set:
                skipped["lane"] += 1
                continue
            if not store._deps_done(t, by_id):
                skipped["dependencies"] += 1
                continue
            required_caps = store._task_required_capabilities(t)
            if required_caps and not set(required_caps).issubset(cap_set):
                skipped["capability_mismatch"] += 1
                continue
            if max_risk_value and store._risk_value(t.get("risk_level") or "") > max_risk_value:
                skipped["risk"] += 1
                continue
            tally = loader(c, tid)
            score = store._dispatch_score(t, lane_set, cap_set, tally, max_budget_usd)
            if score["budget"]["status"] == "over_budget":
                skipped["budget"] += 1
                continue
            eligible.append((
                score["score"],
                -int(t.get("sort_order") or 0),
                tid,
                t,
                score,
            ))
    if not eligible:
        return {
            "schema": SCHEMA,
            "project": project,
            "simulated": True,
            "claimed": False,
            "reason": "no_unblocked_work",
            "policy_version": POLICY_VERSION,
            "from_cursor": snapshot.from_cursor,
            "until_cursor": snapshot.until_cursor,
            "events_replayed": snapshot.events_replayed,
            "dispatch_reason": {
                "policy": POLICY_VERSION,
                "skipped": skipped,
                "candidate_count": 0,
            },
        }
    _, _, selected_id, selected_task, selected_score = sorted(
        eligible, key=lambda x: (-x[0], -x[1], x[2]))[0]
    return {
        "schema": SCHEMA,
        "project": project,
        "simulated": True,
        "claimed": True,
        "task_id": selected_id,
        "task": snapshot.task_row(selected_id),
        "policy_version": POLICY_VERSION,
        "from_cursor": snapshot.from_cursor,
        "until_cursor": snapshot.until_cursor,
        "events_replayed": snapshot.events_replayed,
        "dispatch_reason": {
            "policy": POLICY_VERSION,
            "score": selected_score["score"],
            "factors": selected_score["factors"],
            "required_capabilities": selected_score["required_capabilities"],
            "matched_capabilities": selected_score["matched_capabilities"],
            "skipped": skipped,
            "candidate_count": len(eligible),
            "explanation": (
                f"Would assign {selected_id} to {agent_id} with score "
                f"{selected_score['score']} from replayed snapshot @ cursor "
                f"{snapshot.until_cursor}."
            ),
        },
        "budget": selected_score["budget"],
        "recommendation": store._model_recommendation(selected_task, selected_score),
    }
