#!/usr/bin/env python3
"""Inventory deliverable intake contracts before enabling the production gate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

SCHEMA = "switchboard.deliverable_intake_audit.v1"
ACTIVE_STATUSES = {"in_progress", "in_review", "done"}
DRAFT_STATUSES = {"proposed", "approved"}


def classify(deliverables: Iterable[Dict[str, Any]], project: str) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    counts = {"compliant": 0, "pending_contract": 0, "grandfathered": 0, "ignored": 0}
    for deliverable in deliverables:
        status = str(deliverable.get("status") or "").strip().lower()
        validation = store._validate_deliverable_intake(deliverable)
        if validation is None:
            classification = "compliant"
        elif status in DRAFT_STATUSES:
            classification = "pending_contract"
        elif status in ACTIVE_STATUSES:
            classification = "grandfathered"
        else:
            classification = "ignored"
        counts[classification] += 1
        rows.append({
            "deliverable_id": deliverable.get("id"),
            "status": status,
            "classification": classification,
            "details": (validation or {}).get("details") or [],
        })
    return {
        "schema": SCHEMA,
        "project": project,
        "ok": counts["pending_contract"] == 0 and counts["grandfathered"] == 0,
        "counts": counts,
        "deliverables": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="switchboard")
    parser.add_argument(
        "--require-clean", action="store_true",
        help="exit nonzero when a draft or grandfathered deliverable lacks its intake contract",
    )
    args = parser.parse_args()
    if not store.has_project(args.project):
        print(json.dumps({"schema": SCHEMA, "project": args.project,
                          "error": "unknown project"}, sort_keys=True))
        return 2
    report = classify(
        store.list_deliverables(project=args.project, include_task_snapshots=False), args.project)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if args.require_clean and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
