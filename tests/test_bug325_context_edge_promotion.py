#!/usr/bin/env python3
"""BUG-325: a promoted context node must keep its edges to other linked tasks.

Regression shape taken from atlas/ATLAS-D9. PACK-1, PACK-2 and PACK-3 were all
linked with role="foundation"; DIST-2 (flow) depends_on PACK-3 only. PACK-3 was
therefore promoted onto the map, but its own deps were never traversed, so
PACK-1 and PACK-2 sat in the context footer with no visible relationship to the
work they gate. An agent "fixed" the picture by writing PACK-1/PACK-2 (and four
other non-prerequisites) into DIST-2.depends_on — corrupting a scheduler
contract to drive a rendering concern, which over-blocked DIST-2 to
blocked_by_count 5.

The fix must connect already-linked tasks WITHOUT reintroducing the DAG
inflation that CONTEXT_LINK_ROLES exists to prevent: traversal from a promoted
context node follows deps that are themselves linked to this deliverable, and
never reaches outside it.
"""
from __future__ import annotations

import unittest

from path_setup import ROOT  # noqa: F401

import mission_graph  # noqa: E402


def _link(task_id, role, depends_on=(), status="Not Started", blocks=False):
    return {
        "task_id": task_id,
        "role": role,
        "project_id": "atlas",
        "blocks_deliverable": blocks,
        "task_detail": {
            "task_id": task_id,
            "title": f"{task_id} title",
            "status": status,
            "depends_on": list(depends_on),
            "provenance": {"terminal": status == "Done"},
        },
    }


def _atlas_d9_shape():
    """DIST-2 (flow) -> PACK-3 (context) -> PACK-1/PACK-2 (context).

    PACK-1 additionally depends on CORE-4, which is NOT linked to the
    deliverable — it stands in for the "stub tasks from other deliverables'
    stories" that context filtering exists to keep off the map.
    """
    return [
        _link("DIST-2", "contributes", depends_on=["PACK-3"]),
        _link("PACK-3", "foundation", depends_on=["PACK-1", "PACK-2"]),
        _link("PACK-1", "foundation", depends_on=["CORE-4"]),
        _link("PACK-2", "foundation"),
        _link("SENSE-4", "foundation"),
    ]


class ContextEdgePromotionTest(unittest.TestCase):
    def _graph(self):
        return mission_graph.build_dependency_graph(
            _atlas_d9_shape(), deliverable_id="ATLAS-D9", project_id="atlas"
        )

    def _edges(self, graph):
        return {(e["from"], e["to"]) for e in graph["edges"]}

    def test_promoted_context_node_keeps_edges_to_linked_tasks(self):
        """PACK-1/PACK-2 -> PACK-3 must be drawn: all three are on the map."""
        edges = self._edges(self._graph())
        self.assertIn(("PACK-3", "DIST-2"), edges)
        self.assertIn(("PACK-1", "PACK-3"), edges)
        self.assertIn(("PACK-2", "PACK-3"), edges)

    def test_promoted_tasks_render_as_nodes_not_context_footer(self):
        graph = self._graph()
        node_ids = {n["id"] for n in graph["nodes"]}
        self.assertIn("PACK-1", node_ids)
        self.assertIn("PACK-2", node_ids)
        context_ids = {c["id"] for c in graph["context_nodes"]}
        self.assertNotIn("PACK-1", context_ids)
        self.assertNotIn("PACK-2", context_ids)

    def test_traversal_never_leaves_the_deliverable(self):
        """CORE-4 is unlinked; promoting PACK-1 must not drag it onto the map.

        This is the guard that keeps the original DAG-inflation fix intact.
        """
        graph = self._graph()
        node_ids = {n["id"] for n in graph["nodes"]}
        self.assertNotIn("CORE-4", node_ids)
        self.assertNotIn(("CORE-4", "PACK-1"), self._edges(graph))

    def test_unreferenced_context_task_stays_off_the_map(self):
        """SENSE-4 is foundation and nothing depends on it — still context."""
        graph = self._graph()
        self.assertNotIn("SENSE-4", {n["id"] for n in graph["nodes"]})
        self.assertIn("SENSE-4", {c["id"] for c in graph["context_nodes"]})

    def test_flow_tasks_still_pull_in_external_deps(self):
        """Unchanged behavior: a FLOW task's unlinked dep renders (dashed)."""
        graph = mission_graph.build_dependency_graph(
            [_link("DIST-1", "contributes", depends_on=["CORE-4"])],
            deliverable_id="ATLAS-D9",
            project_id="atlas",
        )
        self.assertIn(("CORE-4", "DIST-1"), self._edges(graph))
        external = {n["id"] for n in graph["nodes"] if n.get("external")}
        self.assertIn("CORE-4", external)

    def test_no_duplicate_or_self_edges(self):
        graph = self._graph()
        pairs = [(e["from"], e["to"]) for e in graph["edges"]]
        self.assertEqual(len(pairs), len(set(pairs)), "duplicate edges emitted")
        self.assertFalse([p for p in pairs if p[0] == p[1]], "self-edge emitted")

    def test_mermaid_matches_edge_list(self):
        """The drawing must not disagree with the data it claims to render."""
        graph = self._graph()
        for src, dst in self._edges(graph):
            arrow_solid = f"{mission_graph._mermaid_id(src)} --> {mission_graph._mermaid_id(dst)}"
            arrow_dashed = f"{mission_graph._mermaid_id(src)} -.-> {mission_graph._mermaid_id(dst)}"
            self.assertTrue(
                arrow_solid in graph["mermaid"] or arrow_dashed in graph["mermaid"],
                f"edge {src}->{dst} missing from mermaid",
            )


class BlockerReasonTest(unittest.TestCase):
    """`blocker` is much weaker than 'on the critical path' but renders as an
    alarm-red 4px border, so readers (human and agent) over-read the map. Expose
    WHY a node is flagged so the UI can distinguish real blockage from
    'unfinished and something depends on it'."""

    def test_blocker_reason_distinguishes_dependents_from_real_blockage(self):
        graph = mission_graph.build_dependency_graph(
            _atlas_d9_shape(), deliverable_id="ATLAS-D9", project_id="atlas"
        )
        by_id = {n["id"]: n for n in graph["nodes"]}
        # PACK-3 is unfinished with a dependent — flagged, but not *blocked*.
        self.assertTrue(by_id["PACK-3"]["blocker"])
        self.assertEqual(by_id["PACK-3"]["blocker_reason"], "has_dependents")
        # DIST-2 is unfinished with no dependent and no flag — not a blocker.
        self.assertFalse(by_id["DIST-2"]["blocker"])
        self.assertIsNone(by_id["DIST-2"]["blocker_reason"])

    def test_blocked_status_outranks_has_dependents(self):
        graph = mission_graph.build_dependency_graph(
            [
                _link("A", "contributes", depends_on=["B"]),
                _link("B", "contributes", status="Blocked"),
            ],
            deliverable_id="D",
            project_id="atlas",
        )
        by_id = {n["id"]: n for n in graph["nodes"]}
        self.assertEqual(by_id["B"]["blocker_reason"], "blocked_status")

    def test_done_task_is_never_a_blocker(self):
        graph = mission_graph.build_dependency_graph(
            [
                _link("A", "contributes", depends_on=["B"]),
                _link("B", "contributes", status="Done"),
            ],
            deliverable_id="D",
            project_id="atlas",
        )
        by_id = {n["id"]: n for n in graph["nodes"]}
        self.assertFalse(by_id["B"]["blocker"])
        self.assertIsNone(by_id["B"]["blocker_reason"])


if __name__ == "__main__":
    unittest.main()
