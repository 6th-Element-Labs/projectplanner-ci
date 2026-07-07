"""Deliverable dependency graph — strategic layer over linked tasks (depends_on + proof)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

GRAPH_SCHEMA = "switchboard.deliverable_dependency_graph.v1"

_STATE_CLASS = {
    "done": "doneNode",
    "done_unproven": "doneUnprovenNode",
    "in_progress": "progressNode",
    "in_review": "reviewNode",
    "blocked": "blockedNode",
    "todo": "todoNode",
    "external": "externalNode",
    "missing": "externalNode",
}


def _mermaid_id(task_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", (task_id or "task").strip())
    if not safe or safe[0].isdigit():
        safe = f"T_{safe}"
    return safe


def node_execution_state(detail: Dict[str, Any]) -> str:
    """Map task detail to a status color bucket:
    done | done_unproven | in_progress | in_review | blocked | todo | missing.

    'done' is a Done task WITH recorded merge provenance (proof); a Done task
    without that proof is 'done_unproven', so the two stay visually distinct
    while both still read as Done.
    """
    if detail.get("error"):
        return "missing"
    status = (detail.get("status") or "").strip()
    provenance = detail.get("provenance") or {}
    if status == "Done":
        return "done" if provenance.get("terminal") else "done_unproven"
    if status == "In Review":
        return "in_review"
    if status == "In Progress":
        return "in_progress"
    if status == "Blocked":
        return "blocked"
    return "todo"


def _node_label(task_id: str, title: str, state: str, external: bool = False) -> str:
    short = (title or task_id or "").strip()
    if len(short) > 42:
        short = short[:39] + "..."
    suffix = {
        "done": " Done",
        "done_unproven": " Done",
        "in_progress": " In Progress",
        "in_review": " In Review",
        "blocked": " Blocked",
        "missing": " missing",
    }.get(state, "")
    if external:
        suffix = " external"
    body = f"{task_id}"
    if short and short.lower() != task_id.lower():
        body = f"{task_id}: {short}"
    return f"{body}{suffix}"


def build_dependency_graph(
    linked_tasks: List[Dict[str, Any]],
    *,
    deliverable_id: str = "",
    project_id: str = "",
    task_lookup: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build nodes/edges/mermaid from deliverable-linked tasks and depends_on."""
    internal: Dict[str, Dict[str, Any]] = {}
    for link in linked_tasks or []:
        detail = link.get("task_detail") or {}
        tid = (link.get("task_id") or detail.get("task_id") or "").strip().upper()
        if not tid:
            continue
        internal[tid] = {
            "link": link,
            "detail": detail,
            "project_id": link.get("project_id") or project_id,
        }

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    seen_edges: Set[Tuple[str, str]] = set()

    def _ensure_node(task_id: str, project_id: str, external: bool,
                     detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        tid = task_id.strip().upper()
        if tid in nodes:
            if external and not nodes[tid].get("external"):
                nodes[tid]["external"] = True
            return nodes[tid]
        det = detail or {}
        if not det and task_lookup and not external:
            det = task_lookup(project_id, tid) or {}
        if not det and external and task_lookup:
            det = task_lookup(project_id, tid) or task_lookup(project_id, tid, fallback=True) or {}
        state = node_execution_state(det) if det else ("missing" if external else "todo")
        node = {
            "id": tid,
            "mermaid_id": _mermaid_id(tid),
            "title": det.get("title") or tid,
            "status": det.get("status"),
            "state": state,
            "external": external,
            "project_id": project_id,
            "workstream": det.get("workstream") or det.get("_wsId"),
            "provenance": det.get("provenance"),
        }
        nodes[tid] = node
        return node

    for tid, item in internal.items():
        detail = item["detail"]
        _ensure_node(tid, item["project_id"], external=False, detail=detail)
        depends_on = list(detail.get("depends_on") or [])
        if not depends_on and task_lookup:
            full = task_lookup(item["project_id"], tid) or {}
            depends_on = list(full.get("depends_on") or [])
        for dep in depends_on:
            dep_id = (dep or "").strip().upper()
            if not dep_id:
                continue
            external = dep_id not in internal
            dep_project = item["project_id"]
            if external and task_lookup:
                hit = task_lookup(dep_project, dep_id) or {}
                dep_project = hit.get("_project_id") or dep_project
            _ensure_node(dep_id, dep_project, external=external)
            key = (dep_id, tid)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({
                "from": dep_id,
                "to": tid,
                "kind": "depends_on",
                "external": external,
            })

    node_list = sorted(nodes.values(), key=lambda n: n["id"])
    mermaid = render_mermaid_flowchart(node_list, edges)
    return {
        "schema": GRAPH_SCHEMA,
        "project_id": project_id,
        "deliverable_id": deliverable_id,
        "nodes": node_list,
        "edges": edges,
        "mermaid": mermaid,
        "stats": {
            "node_count": len(node_list),
            "edge_count": len(edges),
            "external_node_count": sum(1 for n in node_list if n.get("external")),
            "done_count": sum(1 for n in node_list if n.get("state") == "done"),
            "done_unproven_count": sum(1 for n in node_list if n.get("state") == "done_unproven"),
            "in_progress_count": sum(1 for n in node_list if n.get("state") == "in_progress"),
            "in_review_count": sum(1 for n in node_list if n.get("state") == "in_review"),
            "blocked_count": sum(1 for n in node_list if n.get("state") == "blocked"),
            "todo_count": sum(1 for n in node_list if n.get("state") == "todo"),
        },
    }


def _emit_node_line(node: Dict[str, Any], indent: str) -> str:
    mid = node.get("mermaid_id") or _mermaid_id(node.get("id") or "")
    label = _node_label(
        node.get("id") or "",
        node.get("title") or "",
        node.get("state") or "todo",
        external=bool(node.get("external")),
    )
    label = label.replace('"', "'")
    return f'{indent}{mid}["{label}"]'


def render_mermaid_flowchart(nodes: List[Dict[str, Any]],
                             edges: List[Dict[str, Any]]) -> str:
    """Render a clean, layered left-to-right dependency flowchart.

    Nodes are ranked by dependency depth (prerequisites on the left, dependents to
    the right) and placed by Mermaid/dagre with no per-workstream wrapper boxes. An
    earlier version wrapped each workstream in a `subgraph` cluster to "organize"
    the DAG, but that forced dagre to keep every workstream contiguous and route
    the many cross-workstream edges around the cluster walls — a hairball of
    crossing arrows. The workstream is already spelled out in every task-id prefix
    (CHART-1, ENGINE-2, WINDOWS-1), so the boxes added visual noise, not signal.
    Direction is LR: for these wide dependency graphs (dozens of tasks, shallow
    depth) top-down blows out to a multi-thousand-pixel-wide unreadable strip,
    whereas left-to-right stays legible and scrolls sideways like the board.
    """
    lines = ["flowchart LR"]

    for node in nodes:
        lines.append(_emit_node_line(node, "  "))

    for edge in edges:
        src = _mermaid_id(edge.get("from") or "")
        dst = _mermaid_id(edge.get("to") or "")
        if edge.get("external"):
            lines.append(f"  {src} -.-> {dst}")
        else:
            lines.append(f"  {src} --> {dst}")
    lines.extend([
        "",
        "  classDef doneNode fill:#d4edda,stroke:#28a745,color:#155724",
        "  classDef doneUnprovenNode fill:#d1f0e8,stroke:#20c997,color:#0f5132",
        "  classDef progressNode fill:#cfe2ff,stroke:#0d6efd,color:#084298",
        "  classDef reviewNode fill:#fff3cd,stroke:#ffc107,color:#856404",
        "  classDef blockedNode fill:#f8d7da,stroke:#dc3545,color:#842029",
        "  classDef todoNode fill:#e9ecef,stroke:#6c757d,color:#495057",
        "  classDef externalNode fill:#f8f9fa,stroke:#adb5bd,color:#6c757d,stroke-dasharray: 4 2",
    ])
    by_class: Dict[str, List[str]] = {}
    for node in nodes:
        cls = "externalNode" if node.get("external") else _STATE_CLASS.get(node.get("state") or "todo", "todoNode")
        by_class.setdefault(cls, []).append(node.get("mermaid_id") or _mermaid_id(node.get("id") or ""))
    for cls, ids in sorted(by_class.items()):
        if ids:
            lines.append(f"  class {','.join(ids)} {cls}")
    return "\n".join(lines)
