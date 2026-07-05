"""Deliverable breakdown draft generation for coordinator/human review workflows."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import store

DEFAULT_MILESTONE_TITLES = (
    "Define shared contract",
    "Build core implementation",
    "Integrate cross-board",
    "Prove parity and ship",
)

RENDER_MILESTONE_TITLES = (
    "Define shared render model",
    "Export deterministic fixture",
    "Build WebGPU ingest",
    "Integrate into runtime",
    "Prove parity and performance",
    "Ship visible demo",
)


def _normalize_target_projects(raw: Any, owning_project: str) -> List[Dict[str, Any]]:
    parsed = raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [p.strip() for p in raw.split(",") if p.strip()]
    if parsed in (None, ""):
        return [{"project_id": owning_project}]
    if isinstance(parsed, str):
        parsed = [parsed]
    out: List[Dict[str, Any]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str):
                out.append({"project_id": item.strip()})
            elif isinstance(item, dict):
                pid = (item.get("project_id") or item.get("project") or "").strip()
                if pid:
                    out.append(dict(item, project_id=pid))
    seen = set()
    normalized: List[Dict[str, Any]] = []
    for item in out:
        pid = item["project_id"]
        if pid in seen:
            continue
        seen.add(pid)
        if not store.has_project(pid):
            raise ValueError(f"unknown target project: {pid}")
        normalized.append(item)
    return normalized or [{"project_id": owning_project}]


def _default_workstream(project_id: str, hint: str = "") -> str:
    hint = (hint or "").strip().upper()
    with store._conn(project_id) as c:
        rows = c.execute(
            "SELECT DISTINCT workstream_id FROM tasks ORDER BY workstream_id"
        ).fetchall()
    workstreams = [r[0] for r in rows if r[0]]
    if hint and hint in workstreams:
        return hint
    if workstreams:
        return workstreams[0]
    return hint or "BUILD"


def _milestone_titles(outcome: str) -> List[str]:
    lower = outcome.lower()
    if any(k in lower for k in ("webgpu", "render", "vulkan", "chart", "fixture")):
        return list(RENDER_MILESTONE_TITLES)
    return list(DEFAULT_MILESTONE_TITLES)


def _task_title(milestone_title: str, project_id: str) -> str:
    return f"{milestone_title} ({project_id})"


def generate_breakdown_draft(
    outcome: str,
    deliverable: Optional[Dict[str, Any]] = None,
    target_projects: Any = None,
    policy_constraints: Any = None,
    acceptance_criteria: Any = None,
    project: str = store.DEFAULT_PROJECT,
) -> Dict[str, Any]:
    """Build a deterministic milestone/task draft grouped for human review."""
    outcome = (outcome or "").strip()
    if not outcome:
        raise ValueError("outcome is required")
    deliverable = deliverable or {}
    owning_project = project
    targets = _normalize_target_projects(target_projects, owning_project)
    milestones: List[Dict[str, Any]] = []
    for idx, title in enumerate(_milestone_titles(outcome), start=1):
        tasks: List[Dict[str, Any]] = []
        for t_idx, target in enumerate(targets):
            pid = target["project_id"]
            ws = (target.get("workstream_id") or target.get("workstream") or
                  _default_workstream(pid, target.get("workstream_hint") or ""))
            tasks.append({
                "action": "create",
                "project_id": pid,
                "workstream_id": ws,
                "title": _task_title(title, pid),
                "description": (
                    f"Contribute to outcome: {outcome}\n\nMilestone: {title}"
                ),
                "role": target.get("role") or "contributes",
                "blocks_deliverable": bool(target.get("blocks_deliverable")),
                "depends_on": target.get("depends_on") or [],
            })
        milestones.append({
            "title": title,
            "description": f"Milestone for outcome: {outcome}",
            "sort_order": idx,
            "status": "not_started",
            "acceptance_criteria": [
                f"{title} complete for {outcome}",
            ],
            "proof_requirements": {"merge_provenance": True},
            "tasks": tasks,
        })
    policy = deliverable.get("policy_constraints") or {}
    if isinstance(policy_constraints, dict):
        policy = {**policy, **policy_constraints}
    criteria = list(deliverable.get("acceptance_criteria") or [])
    if isinstance(acceptance_criteria, list):
        criteria.extend(acceptance_criteria)
    elif isinstance(acceptance_criteria, str) and acceptance_criteria.strip():
        criteria.append(acceptance_criteria.strip())
    if not criteria:
        criteria = [
            "milestones map to explicit target projects",
            "Done requires merge/default-branch provenance",
        ]
    return {
        "schema": "switchboard.deliverable_breakdown_draft.v1",
        "outcome": outcome,
        "target_projects": targets,
        "policy_constraints": policy,
        "acceptance_criteria": criteria,
        "milestones": milestones,
        "generation": {
            "mode": "deterministic_template",
            "llm_used": False,
        },
    }


def maybe_enrich_with_llm(draft: Dict[str, Any], project: str) -> Dict[str, Any]:
    """Optionally refine a draft through the plan agent when LLM gateway is configured."""
    if os.environ.get("PM_DELIVERABLE_BREAKDOWN_LLM", "").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        return draft
    try:
        import agent  # local import: optional runtime dependency
    except Exception:
        return draft
    prompt = (
        "Return ONLY valid JSON matching switchboard.deliverable_breakdown_draft.v1. "
        "Improve milestone titles, task drafts, acceptance criteria, and policy constraints "
        "for this deliverable outcome. Keep explicit project_id on every task draft. "
        "Use action=create for new tasks and action=link only when task_id is known.\n\n"
        f"Draft to refine:\n{json.dumps(draft, indent=2)}"
    )
    try:
        result = agent.run(None, prompt, project=project, max_iters=2)
        answer = (result.get("answer") or "").strip()
        match = re.search(r"\{.*\}", answer, re.S)
        if not match:
            return draft
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict) and parsed.get("milestones"):
            parsed.setdefault("generation", {})["mode"] = "llm_refined"
            parsed["generation"]["llm_used"] = True
            return parsed
    except Exception:
        pass
    return draft
