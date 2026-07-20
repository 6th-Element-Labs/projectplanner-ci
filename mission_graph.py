"""Deliverable dependency graph — strategic layer over linked tasks (depends_on + proof)."""
from __future__ import annotations

import html
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

GRAPH_SCHEMA = "switchboard.deliverable_dependency_graph.v1"

# Link roles that mark a task as background context rather than executable flow.
# 'foundation' = already-shipped groundwork the mission builds on; 'parked' =
# frozen tracks kept on the deliverable for the record. Both stay linked (and
# listed on the mission page), but they don't belong in the dependency FLOW map:
# rendering them inflated the DAG into disconnected islands of Done/frozen work
# whose own upstream deps dragged in stub tasks from other deliverables' stories.
CONTEXT_LINK_ROLES = {"foundation", "parked"}

_STATE_CLASS = {
    "done": "doneNode",
    "done_unproven": "doneUnprovenNode",
    "in_progress": "progressNode",
    "start_failed": "startFailedNode",
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
    done | done_unproven | in_progress | in_review | blocked | todo | missing
    | start_failed.

    'done' is a Done task WITH recorded merge provenance (proof); a Done task
    without that proof is 'done_unproven', so the two stay visually distinct
    while both still read as Done.

    SIMPLIFY-3: prefer TaskSession honest_display / lifecycle_phase over raw
    workflow status so In-Progress corpses paint as start_failed.
    """
    if detail.get("error"):
        return "missing"
    honest = detail.get("honest_display") if isinstance(
        detail.get("honest_display"), dict) else {}
    graph_state = str(honest.get("graph_state") or "").strip()
    if graph_state:
        return graph_state
    phase = str(detail.get("lifecycle_phase") or "").strip()
    if phase == "start_failed_retry":
        return "start_failed"
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


def _workstream(task_id: str) -> str:
    """The workstream prefix of a task id: everything before the trailing -N.

    CHART-1 -> CHART, HELMWEBGPU-6 -> HELMWEBGPU, QA-L-2 -> QA-L. Ids without a
    numeric tail are their own group (they end up as loose singletons)."""
    m = re.match(r"^(.*?)-\d+[A-Za-z]?$", (task_id or "").strip())
    return m.group(1) if m else (task_id or "").strip()


def _clean_title(task_id: str, title: str) -> str:
    """Drop a leading '<ID>:' prefix from the title, however many times it repeats.

    Many titles are stored with the id baked in ('FORGE-2: 20-symbol catalog').
    The label already shows the id on its own line, so the old code emitting
    '{id}: {title}' produced 'FORGE-2: FORGE-2: 20-symbol catalog'. Strip it."""
    t = (title or "").strip()
    tid = (task_id or "").strip()
    if not tid:
        return t
    low = tid.lower()
    while t.lower().startswith(low):
        rest = t[len(tid):].lstrip()
        if rest.startswith(":"):
            t = rest[1:].lstrip()
        else:
            break
    return t


def _node_label(task_id: str, title: str, state: str, external: bool = False) -> str:
    """A compact two-line node label: bold id on line 1, short title on line 2.

    Status is carried by node color + the legend, so it is intentionally not
    repeated in the text. Rendered with Mermaid htmlLabels; the title is HTML-
    escaped so it can never break the flowchart or inject markup (the <b>/<br/>
    we add survive DOMPurify even under securityLevel:'strict')."""
    tid = (task_id or "").strip()
    t = _clean_title(tid, title)
    if len(t) > 34:
        t = t[:33].rstrip() + "…"
    def _esc(v: str) -> str:
        # Keep apostrophes literal — mermaid re-escapes &#x27; into visible junk
        # (seen on real titles like "operator's"). Double quotes still escape,
        # since the label sits inside a "..." mermaid string.
        return html.escape(v, quote=False).replace('"', "&quot;")

    head = f"<b>{_esc(tid)}</b>"
    if not t or t.lower() == tid.lower():
        return head
    return f"{head}<br/>{_esc(t)}"


def build_dependency_graph(
    linked_tasks: List[Dict[str, Any]],
    *,
    deliverable_id: str = "",
    project_id: str = "",
    task_lookup: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build nodes/edges/mermaid from deliverable-linked tasks and depends_on.

    The graph is the EXECUTION flow: links whose role is in CONTEXT_LINK_ROLES
    (foundation groundwork, parked/frozen tracks) are kept off the map and
    returned separately as `context_nodes`. A context task is promoted into the
    graph only when a flow task actually depends_on it — then it is part of the
    path and renders with its real state. Context tasks' own upstream deps are
    never traversed; that history belongs to their home deliverable's story.
    """
    internal: Dict[str, Dict[str, Any]] = {}
    for link in linked_tasks or []:
        detail = link.get("task_detail") or {}
        tid = (link.get("task_id") or detail.get("task_id") or "").strip().upper()
        if not tid:
            continue
        role = (link.get("role") or "").strip().lower()
        internal[tid] = {
            "link": link,
            "detail": detail,
            "project_id": link.get("project_id") or project_id,
            "context": role in CONTEXT_LINK_ROLES,
            "role": role,
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
        if item["context"]:
            continue
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
            linked_dep = internal.get(dep_id)
            external = linked_dep is None
            dep_project = item["project_id"]
            if linked_dep is not None:
                # Linked task (flow or promoted context) — solid edge, real state.
                dep_project = linked_dep["project_id"]
                _ensure_node(dep_id, dep_project, external=False,
                             detail=linked_dep["detail"])
            else:
                if task_lookup:
                    hit = task_lookup(dep_project, dep_id) or {}
                    dep_project = hit.get("_project_id") or dep_project
                _ensure_node(dep_id, dep_project, external=True)
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

    context_nodes = [
        {
            "id": tid,
            "title": item["detail"].get("title") or tid,
            "status": item["detail"].get("status"),
            "state": node_execution_state(item["detail"]) if item["detail"] else "todo",
            "project_id": item["project_id"],
            "role": item["role"],
        }
        for tid, item in sorted(internal.items())
        if item["context"] and tid not in nodes
    ]

    # Flag the tasks holding the flow back so the map can call them out (thick dark
    # border). A blocker is a task that is itself Blocked, OR an unfinished task that
    # something else depends on / is flagged as blocking the deliverable — downstream
    # work is waiting on it. Done tasks never block.
    depended_upon = {e["from"] for e in edges}
    _DONE_STATES = {"done", "done_unproven"}
    for tid, node in nodes.items():
        blocks_deliverable = bool((internal.get(tid, {}).get("link") or {}).get("blocks_deliverable"))
        node["blocker"] = (
            node.get("state") == "blocked"
            or (node.get("state") not in _DONE_STATES
                and (tid in depended_upon or blocks_deliverable))
        )

    node_list = sorted(nodes.values(), key=lambda n: n["id"])
    mermaid = render_mermaid_flowchart(node_list, edges)
    return {
        "schema": GRAPH_SCHEMA,
        "project_id": project_id,
        "deliverable_id": deliverable_id,
        "nodes": node_list,
        "edges": edges,
        "context_nodes": context_nodes,
        "mermaid": mermaid,
        "stats": {
            "node_count": len(node_list),
            "edge_count": len(edges),
            "context_task_count": len(context_nodes),
            "external_node_count": sum(1 for n in node_list if n.get("external")),
            "done_count": sum(1 for n in node_list if n.get("state") == "done"),
            "done_unproven_count": sum(1 for n in node_list if n.get("state") == "done_unproven"),
            "in_progress_count": sum(1 for n in node_list if n.get("state") == "in_progress"),
            "start_failed_count": sum(1 for n in node_list if n.get("state") == "start_failed"),
            "in_review_count": sum(1 for n in node_list if n.get("state") == "in_review"),
            "blocked_count": sum(1 for n in node_list if n.get("state") == "blocked"),
            "blocker_count": sum(1 for n in node_list if n.get("blocker")),
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

    Prerequisites sit left, dependents right; ELK (with a dagre fallback in the
    client) places and routes the nodes. Internal tasks are wrapped in a
    `subgraph` box per workstream — but ONLY for workstreams with 2+ members.
    An earlier version boxed EVERY workstream, so a graph with nine one-task
    workstreams rendered as nine one-node boxes: pure noise, and the crossing
    cross-workstream edges around every cluster wall made a hairball. Boxing only
    the multi-task workstreams (and leaving singletons + external stubs loose)
    keeps the scannable phase-box grouping without that penalty. Direction is LR:
    these graphs are wide and shallow, so LR stays legible and scrolls sideways
    like the board, where top-down blows out into an unreadable strip.
    """
    lines = ["flowchart LR"]

    # Group internal nodes by workstream prefix; box only the 2+ member groups.
    internal = [n for n in nodes if not n.get("external")]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    group_order: List[str] = []
    for node in internal:
        ws = _workstream(node.get("id") or "")
        if ws not in groups:
            groups[ws] = []
            group_order.append(ws)
        groups[ws].append(node)

    boxed_ids: Set[str] = set()
    # UI-32: flat flowchart LR by default — no workstream bundling; state color
    # carries the story. PM_MISSION_GRAPH_SUBGRAPHS=1 restores the boxed layout.
    subgraphs_on = (os.environ.get("PM_MISSION_GRAPH_SUBGRAPHS", "0").strip().lower()
                    in ("1", "true", "yes"))
    for ws in (group_order if subgraphs_on else []):
        members = groups[ws]
        if len(members) < 2:
            continue
        safe_ws = re.sub(r"[^a-zA-Z0-9_]", "_", ws) or "WS"
        lines.append(f'  subgraph ws_{safe_ws}["{ws}"]')
        lines.append("    direction LR")
        for node in members:
            lines.append(_emit_node_line(node, "    "))
            boxed_ids.add(node.get("id"))
        lines.append("  end")

    # Loose nodes: singleton-workstream tasks and external stubs, stable order.
    for node in nodes:
        if node.get("id") not in boxed_ids:
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
        # UI-32: the house palette — soft fills, saturated strokes, near-black
        # text; waiting work recedes (grey text), live work reads at a glance.
        "  classDef doneNode fill:#eaf7ee,stroke:#2fb344,color:#101114",
        "  classDef doneUnprovenNode fill:#e6f7f1,stroke:#12b886,color:#101114",
        "  classDef progressNode fill:#eaf1fa,stroke:#206bc4,color:#101114",
        "  classDef startFailedNode fill:#fff0e8,stroke:#f76707,color:#101114",
        "  classDef reviewNode fill:#fff4cc,stroke:#e0a800,color:#101114",
        "  classDef blockedNode fill:#fdecec,stroke:#d63939,color:#101114",
        "  classDef todoNode fill:#f6f7f9,stroke:#c9ced6,color:#8b95a5",
        "  classDef externalNode fill:#fbfbfc,stroke:#adb5bd,color:#8b95a5,stroke-dasharray: 4 2",
    ])
    by_class: Dict[str, List[str]] = {}
    node_styles: Dict[str, List[str]] = {}   # mid -> extra style props, merged into one line
    for node in nodes:
        mid = node.get("mermaid_id") or _mermaid_id(node.get("id") or "")
        state = node.get("state") or "todo"
        cls = _STATE_CLASS.get(state, "todoNode")
        # An external dependency keeps its REAL state colour — a merged/Done dep is green,
        # not grey (the old code forced every external node to the neutral externalNode
        # class, so a done HARDEN-* dep read as "not done"). The external cue is the dashed
        # border + dotted edge instead. Only an unresolved external dep (state "missing")
        # keeps the neutral external style.
        if node.get("external") and cls != "externalNode":
            node_styles.setdefault(mid, []).append("stroke-dasharray: 4 2")
        # Blockers get a thick dark border on top of their state fill so the tasks holding
        # up the flow jump out at a glance.
        if node.get("blocker"):
            node_styles.setdefault(mid, []).extend(["stroke:#842029", "stroke-width:4px"])
        by_class.setdefault(cls, []).append(mid)
    for cls, ids in sorted(by_class.items()):
        if ids:
            lines.append(f"  class {','.join(ids)} {cls}")
    for mid, props in node_styles.items():
        lines.append(f"  style {mid} {','.join(props)}")
    return "\n".join(lines)
