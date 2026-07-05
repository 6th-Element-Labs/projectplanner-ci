"""Structured mission brief generation from durable deliverable events."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional

OPTIMISTIC_NARRATIVE_RE = re.compile(
    r"\b(on track|almost done|nearly complete|shipping soon|ready to ship|green across the board)\b",
    re.I,
)
STALE_BLOCKED_NARRATIVE_RE = re.compile(
    r"\b(not started|blocked|waiting on dependencies|no progress)\b",
    re.I,
)
DONE_CONTRADICTION_RE = re.compile(
    r"\b(not started|in progress|blocked|no proof|nothing done)\b",
    re.I,
)


def _cite(cite_type: str, **fields: Any) -> Dict[str, Any]:
    row = {"cite_type": cite_type}
    row.update({k: v for k, v in fields.items() if v not in (None, "")})
    return row


def _section(text: str, citations: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"text": text.strip(), "citations": citations}


def brief_source_fingerprint(mission_status: Dict[str, Any]) -> str:
    """Fingerprint durable mission signals used to detect stale briefs."""
    linked = []
    for link in mission_status.get("linked_tasks") or []:
        detail = link.get("task_detail") or link.get("task") or {}
        git_state = detail.get("git_state") or {}
        linked.append({
            "project_id": link.get("project_id"),
            "task_id": link.get("task_id"),
            "status": detail.get("status"),
            "merged_sha": git_state.get("merged_sha"),
            "head_sha": git_state.get("head_sha"),
            "blocks": bool(link.get("blocks_deliverable")),
        })
    payload = {
        "deliverable_id": mission_status.get("deliverable_id"),
        "deliverable_status": (mission_status.get("deliverable") or {}).get("status"),
        "progress": mission_status.get("progress") or {},
        "blockers": mission_status.get("blockers") or [],
        "milestones": [
            {"id": m.get("id"), "status": m.get("status"), "linked_task_count": m.get("linked_task_count")}
            for m in (mission_status.get("milestones") or [])
        ],
        "linked_tasks": linked,
        "pending_proposal": bool(mission_status.get("pending_proposal")),
        "done_with_proof_count": len(mission_status.get("done_with_proof") or []),
        "active_work_count": len(mission_status.get("active_work") or []),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_mission_brief(mission_status: Dict[str, Any],
                        recent_activity: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Build a structured operator brief from mission_status — no chat transcript."""
    deliverable = mission_status.get("deliverable") or {}
    board = mission_status.get("board") or {}
    progress = mission_status.get("progress") or {}
    blockers = mission_status.get("blockers") or []
    done = mission_status.get("done_with_proof") or []
    active = mission_status.get("active_work") or []
    next_actions = mission_status.get("next_actions") or []
    milestones = mission_status.get("milestones") or []
    end_state = deliverable.get("end_state") or board.get("end_state") or "End state not recorded."
    why = deliverable.get("why_it_matters") or "Why-it-matters not recorded."

    what_cites = [
        _cite("deliverable", deliverable_id=deliverable.get("id"), field="end_state"),
    ]
    if board.get("id"):
        what_cites.append(_cite("board", board_id=board.get("id"), field="end_state"))
    what_text = (
        f"We are building: {deliverable.get('title') or mission_status.get('deliverable_id')}\n"
        f"Target end state: {end_state}"
    )

    why_cites = [_cite("deliverable", deliverable_id=deliverable.get("id"), field="why_it_matters")]
    why_text = why

    proof_lines: List[str] = []
    proof_cites: List[Dict[str, Any]] = []
    if done:
        for item in done:
            prov = item.get("provenance") or {}
            git_state = item.get("git_state") or {}
            label = prov.get("label") or "Done with terminal provenance"
            sha = git_state.get("merged_sha") or git_state.get("head_sha") or "—"
            proof_lines.append(f"- {item.get('project_id')} {item.get('task_id')}: {item.get('title')} ({label}; sha {sha})")
            proof_cites.append(_cite(
                "task", project_id=item.get("project_id"), task_id=item.get("task_id"),
                field="provenance.terminal", merged_sha=git_state.get("merged_sha"),
            ))
    else:
        proof_lines.append("No linked tasks are Done with merge/default-branch provenance yet.")
        proof_cites.append(_cite("progress", field="done_with_proof_count", value=0))

    active_lines: List[str] = []
    active_cites: List[Dict[str, Any]] = []
    if active:
        for item in active:
            claims = ", ".join(c.get("agent_id") or "?" for c in (item.get("active_claims") or []))
            claim_note = f"; claims: {claims}" if claims else ""
            active_lines.append(
                f"- {item.get('project_id')} {item.get('task_id')}: {item.get('title')} "
                f"[{item.get('status')}]{claim_note}"
            )
            active_cites.append(_cite(
                "task", project_id=item.get("project_id"), task_id=item.get("task_id"),
                field="status", status=item.get("status"),
            ))
    else:
        active_lines.append("No linked tasks are In Progress, In Review, or actively claimed.")
        active_cites.append(_cite("mission_status", field="active_work", value=[]))

    risk_lines: List[str] = []
    risk_cites: List[Dict[str, Any]] = []
    if blockers:
        for idx, blocker in enumerate(blockers):
            label = blocker.get("title") or blocker.get("task_id") or blocker.get("message") or blocker.get("kind")
            risk_lines.append(f"- {blocker.get('kind')}: {label}")
            risk_cites.append(_cite("blocker", index=idx, kind=blocker.get("kind"),
                                    project_id=blocker.get("project_id"),
                                    task_id=blocker.get("task_id")))
    else:
        risk_lines.append("No blockers reported from linked task state, dependencies, or proof gates.")
        risk_cites.append(_cite("mission_status", field="blockers", value=[]))

    policy = deliverable.get("policy_constraints") or {}
    if policy:
        risk_lines.append(f"- Policy constraints in force: {', '.join(f'{k}={v}' for k, v in policy.items())}")
        risk_cites.append(_cite("deliverable", deliverable_id=deliverable.get("id"), field="policy_constraints"))

    milestone_lines = []
    for m in milestones:
        milestone_lines.append(
            f"- {m.get('title') or m.get('id')}: {m.get('status')} "
            f"({m.get('linked_task_count', 0)} linked tasks)"
        )
    if milestones:
        risk_cites.append(_cite("milestones", count=len(milestones)))

    if mission_status.get("pending_proposal"):
        risk_lines.append("- Pending breakdown proposal awaits human approval before new tasks land.")
        risk_cites.append(_cite("breakdown_proposal", status="proposed"))

    next_text = "No next action heuristic matched."
    next_cites: List[Dict[str, Any]] = []
    if next_actions:
        top = next_actions[0]
        next_text = (
            f"{top.get('action')}: "
            f"{top.get('title') or top.get('reason') or ''}"
            f"{(' · ' + top.get('project_id') + ' ' + top.get('task_id')) if top.get('task_id') else ''}"
        ).strip()
        next_cites.append(_cite("next_action", action=top.get("action"),
                                project_id=top.get("project_id"), task_id=top.get("task_id")))
    else:
        next_cites.append(_cite("mission_status", field="next_actions", value=[]))

    activity_lines: List[str] = []
    activity_cites: List[Dict[str, Any]] = []
    for row in (recent_activity or [])[:5]:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {"text": payload}
        summary = payload.get("title") or payload.get("reason") or payload.get("deliverable_id") or row.get("kind")
        activity_lines.append(f"- {row.get('kind')} by {row.get('actor')}: {summary}")
        activity_cites.append(_cite("activity", kind=row.get("kind"), actor=row.get("actor"),
                                    created_at=row.get("created_at")))

    sections = {
        "what_we_are_building": _section(what_text, what_cites),
        "why_it_matters": _section(why_text, why_cites),
        "completed_proof": _section("\n".join(proof_lines), proof_cites),
        "active_work": _section("\n".join(active_lines), active_cites),
        "risks_and_blockers": _section("\n".join(risk_lines + milestone_lines), risk_cites),
        "next_best_move": _section(next_text, next_cites),
    }
    if activity_lines:
        sections["recent_mission_activity"] = _section("\n".join(activity_lines), activity_cites)

    summary_parts = [
        "## What we are building",
        sections["what_we_are_building"]["text"],
        "## Why it matters",
        sections["why_it_matters"]["text"],
        "## Completed proof",
        sections["completed_proof"]["text"],
        "## Active work",
        sections["active_work"]["text"],
        "## Risks and blockers",
        sections["risks_and_blockers"]["text"],
        "## Next best move",
        sections["next_best_move"]["text"],
    ]
    if activity_lines:
        summary_parts.extend(["## Recent mission activity", sections["recent_mission_activity"]["text"]])

    citations: List[Dict[str, Any]] = []
    for section in sections.values():
        citations.extend(section.get("citations") or [])

    linked_count = int(progress.get("linked_task_count") or 0)
    done_count = int(progress.get("done_with_proof_count") or 0)
    honesty_note = (
        f"Linked tasks: {linked_count}; Done-with-proof: {done_count}; "
        f"Blockers: {len(blockers)}. "
        "This brief is derived from live task status and provenance — not agent optimism."
    )

    return {
        "schema": "switchboard.mission_brief.v1",
        "deliverable_id": mission_status.get("deliverable_id"),
        "project_id": mission_status.get("project_id"),
        "generated_at": time.time(),
        "source_fingerprint": brief_source_fingerprint(mission_status),
        "sections": sections,
        "summary_markdown": "\n\n".join(summary_parts),
        "honesty_note": honesty_note,
        "citations": citations,
        "progress_snapshot": {
            "linked_task_count": linked_count,
            "done_with_proof_count": done_count,
            "blocked_count": int(progress.get("blocked_count") or 0),
            "in_review_count": int(progress.get("in_review_count") or 0),
        },
    }


def narrative_state(mission_status: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None,
                    stored_brief: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Flag stale or contradictory narrative/brief text like rationale_state."""
    metadata = metadata or {}
    stored_brief = stored_brief or metadata.get("generated_brief") or {}
    manual = (metadata.get("narrative") or mission_status.get("narrative") or "").strip()
    progress = mission_status.get("progress") or {}
    blockers = mission_status.get("blockers") or []
    done_count = int(progress.get("done_with_proof_count") or 0)
    linked_count = int(progress.get("linked_task_count") or 0)
    current_fp = brief_source_fingerprint(mission_status)
    flags: List[str] = []

    if stored_brief and stored_brief.get("source_fingerprint") != current_fp:
        flags.append("generated_brief_stale")
    if manual and metadata.get("narrative_source") == "manual":
        brief_at = float(metadata.get("brief_generated_at") or 0)
        manual_at = float(metadata.get("narrative_updated_at") or 0)
        if brief_at and manual_at > brief_at:
            flags.append("manual_narrative_newer_than_generated_brief")
    if manual and OPTIMISTIC_NARRATIVE_RE.search(manual) and (blockers or done_count == 0):
        flags.append("optimistic_manual_narrative")
    if manual and done_count > 0 and DONE_CONTRADICTION_RE.search(manual):
        flags.append("manual_narrative_contradicts_done_proof")
    if manual and linked_count > 0 and not blockers and STALE_BLOCKED_NARRATIVE_RE.search(manual):
        dep_open = any(
            not ((link.get("task_detail") or {}).get("dependency_state") or {}).get("satisfied", True)
            for link in (mission_status.get("linked_tasks") or [])
        )
        if not dep_open:
            flags.append("manual_narrative_says_blocked_but_dependencies_satisfied")

    stale = bool(flags)
    state = {
        "stale": stale,
        "flags": flags,
        "source_fingerprint": current_fp,
        "stored_brief_fingerprint": stored_brief.get("source_fingerprint"),
        "message": (
            "Generated or manual mission narrative may be stale; trust mission_status, "
            "task provenance, blockers, and progress counts."
        ) if stale else None,
    }
    if stale:
        state["failure_class"] = "missing_data"
        state["expected_signal"] = "Brief should be regenerated from durable mission events."
    return state
