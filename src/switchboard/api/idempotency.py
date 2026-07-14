"""REST Idempotency-Key helpers wired to ``db/core.py`` primitives (ARCH-MS-44).

Retryable mutating REST calls accept the standard ``Idempotency-Key`` header
(body ``idem_key`` remains a compatibility alias). Same key + same request
payload replays the stored response; same key + different payload becomes
HTTP ``409 idem_key_conflict`` per ``docs/P0-SPEC.md`` §7.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Tuple

from fastapi import HTTPException, Request

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_CONFLICT_ERROR = "idempotency conflict"
IDEMPOTENCY_CONFLICT_CODE = "idem_key_conflict"

ExecuteFn = Callable[[], Any]
IdempotentOutcome = Tuple[Any, bool]


def resolve_idem_key(request: Request,
                     body: Optional[Mapping[str, Any]] = None) -> str:
    """Prefer the Idempotency-Key header; fall back to body ``idem_key``."""
    header = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip()
    if header:
        return header
    if body:
        return str(body.get("idem_key") or "").strip()
    return ""


def inject_idem_key(request: Request,
                    body: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Return a body copy with ``idem_key`` set from header or existing body."""
    out = dict(body or {})
    key = resolve_idem_key(request, out)
    if key:
        out["idem_key"] = key
    elif "idem_key" in out and not str(out.get("idem_key") or "").strip():
        out.pop("idem_key", None)
    return out


def is_idem_conflict(result: Any) -> bool:
    """True when a store/command result is an idempotency key conflict."""
    if not isinstance(result, Mapping):
        return False
    error = str(result.get("error") or "")
    code = str(result.get("error_code") or "")
    return (
        error == IDEMPOTENCY_CONFLICT_ERROR
        or error == IDEMPOTENCY_CONFLICT_CODE
        or code == IDEMPOTENCY_CONFLICT_CODE
    )


def conflict_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a store conflict into the P0 REST error envelope."""
    return {
        "error": IDEMPOTENCY_CONFLICT_CODE,
        "error_code": IDEMPOTENCY_CONFLICT_CODE,
        "message": "Idempotency-Key reused with a different request body",
        "idem_key": result.get("idem_key"),
        "operation": result.get("operation"),
    }


def raise_if_idem_conflict(result: Any) -> Any:
    """Raise HTTP 409 when ``result`` is an idempotency conflict; else return it."""
    if is_idem_conflict(result):
        raise HTTPException(409, conflict_payload(result))
    return result


def run_with_idempotency(
        *,
        project: str,
        operation: str,
        actor: str,
        idem_key: str,
        payload: Mapping[str, Any],
        execute: ExecuteFn,
) -> IdempotentOutcome:
    """Replay or record a mutating call via ``store._idem_hit`` / ``_idem_store``.

    Returns ``(result, replayed)``. ``replayed`` is True when the response came
    from a prior stored hit so callers can skip post-mutation audit side effects.

    Used for REST surfaces whose persistence path does not yet hash ``idem_key``
    itself (create/update/comment/ack/move). Claim/send/wake keep store-native
    idempotency; routers only inject the header and map conflicts to 409.
    """
    import store

    key = (idem_key or "").strip()
    if not key:
        return execute(), False

    request_payload = dict(payload or {})
    with store._conn(project) as c:
        hit = store._idem_hit(c, operation, key, actor, request_payload)
        if hit is not None:
            return hit, True

    result = execute()
    if not isinstance(result, dict):
        return result, False
    if is_idem_conflict(result):
        return result, False

    # Persist after the side effect. A narrow race can still double-execute under
    # concurrent first-writers; the (idem_key, operation) primary key keeps the
    # replay table consistent for subsequent retries.
    with store._conn(project) as c:
        hit = store._idem_hit(c, operation, key, actor, request_payload)
        if hit is not None:
            return hit, True
        store._idem_store(c, operation, key, actor, request_payload, result)
    return result, False
