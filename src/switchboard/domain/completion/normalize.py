"""Bring production payloads into the classifier's contract.

The classifier is only as honest as what it is fed, and two live shapes do not
arrive in its contract:

**Raw GitHub status contexts.**  A ``StatusContext`` has ``state`` and
``description``; it has no ``failure_attribution``.  Fed straight in, every red
required check falls through to ``required_ci_failure_unknown`` and becomes
``route=human`` -- the machine hands an ordinary failing test to a person.

**COORD-20 review findings.**  They are emitted as ``class=auto|escalate``, not
``finding_class``.  Worse, the classifier's original "all findings automatic"
rule means one escalation in a mixed batch collapses the whole batch to
``human``, stranding automatic work that a coder could have fixed.

Attribution is inferred conservatively from evidence that is actually present,
and an attribution supplied upstream is always preferred over anything guessed
here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence


#: Signals in a check's description that mean the *system* failed, not the code.
_INFRASTRUCTURE_SIGNALS = (
    "host key verification", "runner lost", "did not respond", "lost communication",
    "no space left", "connection reset", "connection refused", "network",
    "timed out waiting for", "could not resolve host", "docker", "rate limit",
    "infrastructure", "startup failure", "runner offline", "self-hosted runner",
)

#: Signals that mean a human with authority has to act.
_AUTHORITY_SIGNALS = (
    "not accessible by integration", "permission", "forbidden", "unauthorized",
    "requires approval", "approval required", "protected branch", "denied",
)

#: Conclusions that are infrastructure by definition, whatever the text says.
_INFRASTRUCTURE_CONCLUSIONS = {
    "cancelled", "canceled", "stale", "startup_failure", "timed_out",
}

_FAILED = {"failure", "failed", "error"}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _prose(row: Mapping[str, Any]) -> str:
    parts = (row.get("description"), row.get("summary"), row.get("title"),
             row.get("output"), row.get("text"))
    return " ".join(str(p) for p in parts if p).lower()


def normalize_status_context(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Add ``failure_attribution`` to one check row when it is missing."""
    out = deepcopy(dict(row)) if isinstance(row, Mapping) else {}
    if out.get("failure_attribution"):
        return out  # upstream attribution always wins over inference
    state = _text(out.get("conclusion") or out.get("state") or out.get("status"))
    if state == "action_required":
        out["failure_attribution"] = "authority"
        return out
    if state in _INFRASTRUCTURE_CONCLUSIONS:
        out["failure_attribution"] = "infrastructure"
        return out
    if state not in _FAILED:
        return out  # passing/pending rows are not attributed

    prose = _prose(out)
    if any(signal in prose for signal in _AUTHORITY_SIGNALS):
        out["failure_attribution"] = "authority"
    elif any(signal in prose for signal in _INFRASTRUCTURE_SIGNALS):
        out["failure_attribution"] = "infrastructure"
    else:
        # A required check that simply went red, with no infrastructure or
        # authority signal, is the pull request's own failure. Defaulting to
        # product is what keeps ordinary red CI on the automatic remediation
        # path instead of paging a human.
        out["failure_attribution"] = "product"
    return out


def normalize_review_findings(
        findings: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Split COORD-20's ``class=auto|escalate`` findings into the two routes."""
    automatic: list[dict[str, Any]] = []
    escalated: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    for item in findings or []:
        if not isinstance(item, Mapping):
            continue
        row = deepcopy(dict(item))
        kind = _text(row.get("finding_class") or row.get("kind") or row.get("class"))
        if kind in {"auto", "automatic", "product", "code"}:
            row["finding_class"] = "automatic"
            automatic.append(row)
        elif kind in {"escalate", "escalation", "judgment", "authority",
                      "policy", "human"}:
            row["finding_class"] = "judgment" if kind in {
                "escalate", "escalation", "judgment"} else kind
            escalated.append(row)
        else:
            # Unclassifiable findings are escalated, never silently automated.
            row["finding_class"] = "judgment"
            escalated.append(row)
        normalized.append(row)
    return {
        "automatic": automatic,
        "escalated": escalated,
        "normalized": normalized,
        "has_automatic": bool(automatic),
        "has_escalated": bool(escalated),
    }


def normalize_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the snapshot with production shapes mapped onto the contract."""
    snap = deepcopy(dict(snapshot)) if isinstance(snapshot, Mapping) else {}
    contexts = snap.get("status_contexts")
    if isinstance(contexts, Mapping):
        snap["status_contexts"] = {
            name: normalize_status_context(row)
            for name, row in contexts.items()
        }
    review = snap.get("review")
    if isinstance(review, Mapping) and review.get("findings"):
        review = dict(review)
        split = normalize_review_findings(review.get("findings"))
        review["findings"] = split["normalized"]
        review["automatic_findings"] = split["automatic"]
        review["escalated_findings"] = split["escalated"]
        snap["review"] = review
    return snap


__all__ = [
    "normalize_review_findings",
    "normalize_snapshot",
    "normalize_status_context",
]
