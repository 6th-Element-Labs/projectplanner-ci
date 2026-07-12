"""Deliverable lifecycle policy kept outside the legacy store shell."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

DONE_GRADES = {"pass", "waive"}
CLOSURE_METADATA_KEYS = {
    "closure_reports", "last_closure_report", "last_closure_grade", "last_closure_at"
}


def _metadata(value: Any) -> Dict[str, Any]:
    """Match the store's permissive JSON-object coercion for upsert metadata."""
    if value in (None, ""):
        parsed: Any = {}
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = {"text": value}
    else:
        parsed = value
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def prepare_upsert(connection: Any, deliverable_id: str, status: str,
                   metadata_value: Any) -> Tuple[Any, Dict[str, Any], Optional[Dict[str, Any]]]:
    """Load trusted lifecycle state, protect verifier fields, and enforce Done entry.

    Closure metadata is owned by ``record_deliverable_closure``. General upserts may
    neither fabricate it on a new record nor overwrite/erase it on an existing record.
    """
    prior = connection.execute(
        "SELECT status, metadata_json FROM deliverables WHERE id=?", (deliverable_id,)
    ).fetchone()
    prior_metadata = _metadata(prior["metadata_json"]) if prior else {}
    incoming_metadata = _metadata(metadata_value)

    for key in CLOSURE_METADATA_KEYS:
        if key in prior_metadata:
            incoming_metadata[key] = prior_metadata[key]
        else:
            incoming_metadata.pop(key, None)

    grade = str(prior_metadata.get("last_closure_grade") or "").lower()
    if status == "done" and grade not in DONE_GRADES:
        return prior, incoming_metadata, {
            "error": "deliverable closure grade required",
            "deliverable_id": deliverable_id,
            "requested_status": status,
            "last_closure_grade": grade or None,
            "allowed_closure_grades": sorted(DONE_GRADES),
            "action": "run verify_deliverable_closure and persist a pass or waive grade",
            "spec": "docs/DELIVERABLE-CLOSURE-GATE.md",
        }
    return prior, incoming_metadata, None
