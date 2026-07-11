"""Narration generation policy, receipts, and budgets (NARRATE-12, ADR-0008 M3).

The generation half the NARRATE-9 worker calls once it has claimed a request. It decides HOW to
narrate and records a durable receipt for every attempt:

- **Deterministic template** for a routine state transition — the entity's material narrative
  inputs are unchanged since the last delivered narration and only status/provenance moved. Zero
  LLM charge.
- **LLM synthesis** for a material narrative change (or the first narration of an entity).
- **Explicit fallback** when the budget is exhausted, generation is disabled, or the provider
  errors/times out/returns malformed output. The fallback is visible and named; it NEVER
  overwrites or hides the failed LLM receipt — each attempt is its own immutable receipt row.

Per-project rate/token/cost ceilings and model selection are configurable via the project meta
key ``narration_generation_config`` (with a ``PM_NARRATION_DAILY_COST_USD`` env override). Budget
accounting sums the receipt ledger over a rolling window, so the cost ceiling is enforced from the
same durable record the operator audits.

The provider call is injected (``llm_fn``) exactly like ``narrate._llm``, so this module has no
network dependency and is fully golden-testable. ``generate`` returns the narration text + receipt;
publishing the visible narration (the compare-and-swap boundary) belongs to the worker/cutover.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable, Dict, Mapping, Optional

import narration_events
import narration_outbox

PROMPT_VERSION = "narrate.v1"
DEFAULT_MODEL = "taikun-summarize"

# Per-project defaults; override via store.get_meta("narration_generation_config").
DEFAULT_BUDGET: Dict[str, Any] = {
    "enabled": True,
    "model": DEFAULT_MODEL,
    "daily_cost_usd": 5.0,
    "max_tokens_per_call": 400,
    "window_seconds": 86400.0,
}

# Fields whose change is a routine *state transition*, not a material narrative change. Stripping
# them yields the "content signature"; an unchanged signature means only status/provenance moved.
_STATUS_FIELDS = frozenset({"status", "provenance_type"})


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else now


def _conn(project: str):
    from db.connection import _conn as conn
    return conn(project)


def budget_config(project: str) -> Dict[str, Any]:
    cfg = dict(DEFAULT_BUDGET)
    try:
        import store
        override = store.get_meta("narration_generation_config", default=None, project=project)
        if isinstance(override, dict):
            cfg.update(override)
    except Exception:
        pass
    env = (os.environ.get("PM_NARRATION_DAILY_COST_USD") or "").strip()
    if env:
        try:
            cfg["daily_cost_usd"] = float(env)
        except ValueError:
            pass
    return cfg


def content_signature(projection: Mapping[str, Any]) -> str:
    """Hash of the narrative-material inputs EXCLUDING status/provenance (top-level and per linked
    task). Equal signatures across two requests mean the change was a pure state transition."""
    def strip(d: Mapping[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in d.items() if k not in _STATUS_FIELDS}
    filtered = strip(projection)
    linked = filtered.get("linked_tasks")
    if isinstance(linked, list):
        filtered["linked_tasks"] = [strip(lt) if isinstance(lt, Mapping) else lt for lt in linked]
    return narration_events.canonical_source_hash(filtered)


def _projection_for(event: Mapping[str, Any], project: str) -> Optional[Dict[str, Any]]:
    entity_type = event.get("entity_type")
    entity_id = event.get("entity_id")
    if entity_type == "task":
        with _conn(project) as c:
            return narration_outbox.build_task_source_projection(c, entity_id)
    if entity_type == "deliverable":
        return narration_outbox._deliverable_projection(project, entity_id)
    return None


def spend_in_window(project: str, window_seconds: float, now: float) -> float:
    with _conn(project) as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM narration_receipts "
            "WHERE project=? AND created_at >= ?",
            (project, now - window_seconds),
        ).fetchone()
    return float(row[0] or 0.0)


def _last_delivered_signature(project: str, entity_type: str, entity_id: str) -> Optional[str]:
    with _conn(project) as c:
        row = c.execute(
            "SELECT content_sig FROM narration_receipts "
            "WHERE project=? AND entity_type=? AND entity_id=? AND outcome='delivered' "
            "ORDER BY id DESC LIMIT 1",
            (project, entity_type, entity_id),
        ).fetchone()
    return row["content_sig"] if row else None


def deterministic_narration(projection: Mapping[str, Any]) -> str:
    """The versioned deterministic template for a routine state transition (no LLM)."""
    title = (projection.get("title") or "").strip()
    status = (projection.get("status") or "updated").strip()
    if projection.get("entity") == "deliverable":
        linked = projection.get("linked_tasks") or []
        done = sum(1 for lt in linked
                   if str((lt or {}).get("status", "")).strip().lower() in {"done", "complete", "completed"})
        subject = title or "This deliverable"
        return f"**{subject}** is now _{status}_ — {done} of {len(linked)} linked tasks complete."
    subject = title or "This task"
    return f"**{subject}** is now _{status}_."


def fallback_narration(projection: Mapping[str, Any], reason: str) -> str:
    """Explicit, visible fallback text — names the reason and points at the ground truth."""
    subject = (projection.get("title") or "This item").strip()
    status = (projection.get("status") or "updated").strip()
    return (f"**{subject}** is _{status}_. CEO narration is temporarily unavailable "
            f"({reason}); trust the status, provenance, and progress above.")


def build_prompt(projection: Mapping[str, Any]) -> str:
    """Versioned prompt built from the immutable source projection (never raw row JSON)."""
    lines = [f"prompt_version: {PROMPT_VERSION}",
             f"Entity: {projection.get('entity')}",
             f"Title: {projection.get('title') or ''}",
             f"Status: {projection.get('status') or ''}"]
    for key in ("description", "deliverable", "exit_criteria", "end_state", "why_it_matters"):
        val = projection.get(key)
        if val:
            lines.append(f"{key}: {str(val)[:600]}")
    linked = projection.get("linked_tasks")
    if isinstance(linked, list) and linked:
        lines.append("Linked tasks: " + "; ".join(
            f"{(lt or {}).get('task_id')}={((lt or {}).get('status') or '')}" for lt in linked[:20]))
    return "\n".join(lines)


def _record_receipt(project: str, event: Mapping[str, Any], *, mode: str, outcome: str,
                    content_sig: Optional[str], narration: Optional[str],
                    model: Optional[str] = None, prompt_version: Optional[str] = None,
                    latency_ms: Optional[float] = None, tokens_in: int = 0, tokens_out: int = 0,
                    cost_usd: float = 0.0, fallback_reason: Optional[str] = None,
                    now: Optional[float] = None) -> Dict[str, Any]:
    now = _now(now)
    nhash = ("sha256:" + hashlib.sha256(narration.encode("utf-8")).hexdigest()) if narration else None
    from db.connection import _write_through

    def _thunk():
        with _conn(project) as c:
            cur = c.execute(
                """INSERT INTO narration_receipts
                   (event_id, project, entity_type, entity_id, source_revision, source_hash,
                    content_sig, mode, outcome, model, prompt_version, latency_ms, tokens_in,
                    tokens_out, cost_usd, fallback_reason, narration, narration_hash, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event.get("event_id"), project, event.get("entity_type"), event.get("entity_id"),
                 event.get("source_revision"), event.get("source_hash"), content_sig, mode, outcome,
                 model, prompt_version, latency_ms, int(tokens_in or 0), int(tokens_out or 0),
                 float(cost_usd or 0.0), fallback_reason, narration, nhash, now),
            )
            return cur.lastrowid

    receipt_id = _write_through(project, _thunk)
    return {
        "receipt_id": receipt_id, "project": project,
        "entity_type": event.get("entity_type"), "entity_id": event.get("entity_id"),
        "source_revision": event.get("source_revision"), "source_hash": event.get("source_hash"),
        "content_sig": content_sig, "mode": mode, "outcome": outcome, "model": model,
        "prompt_version": prompt_version, "latency_ms": latency_ms, "tokens_in": tokens_in,
        "tokens_out": tokens_out, "cost_usd": cost_usd, "fallback_reason": fallback_reason,
        "narration": narration, "narration_hash": nhash, "created_at": now,
    }


def _default_llm_fn(prompt: str, *, model: str, prompt_version: str, max_tokens: int) -> Dict[str, Any]:
    """Best-effort provider call through the same gateway as narrate._llm; extracts usage/cost
    from the response when the gateway returns it. Injected in tests, so kept import-light."""
    import narrate
    import httpx
    body = {"model": model,
            "messages": [{"role": "system", "content": narrate._SYSTEM},
                         {"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "metadata": {"source": "narrator", "prompt_version": prompt_version}}
    r = httpx.post(f"{narrate.BASE}/chat/completions",
                   headers={"Authorization": f"Bearer {narrate.KEY}"}, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    text = (data["choices"][0]["message"]["content"] or "").strip()
    usage = data.get("usage") or {}
    hidden = data.get("_hidden_params") or {}
    return {
        "text": text,
        "model": data.get("model") or model,
        "tokens_in": int(usage.get("prompt_tokens") or 0),
        "tokens_out": int(usage.get("completion_tokens") or 0),
        "cost_usd": float(hidden.get("response_cost") or usage.get("cost") or 0.0),
    }


def generate(event: Mapping[str, Any], *, projection: Optional[Mapping[str, Any]] = None,
             llm_fn: Optional[Callable[..., Dict[str, Any]]] = None,
             now: Optional[float] = None) -> Dict[str, Any]:
    """Generate a narration for one claimed request and record its receipt. Returns the receipt
    dict (with ``narration`` text). Never raises for provider/budget failures — those become an
    explicit fallback receipt."""
    now = _now(now)
    project = event["project"]
    cfg = budget_config(project)
    if projection is None:
        projection = _projection_for(event, project)
    if projection is None:
        return _record_receipt(project, event, mode="fallback", outcome="error", content_sig=None,
                               narration=None, fallback_reason="entity_missing", now=now)

    sig = content_signature(projection)
    last_sig = _last_delivered_signature(project, event["entity_type"], event["entity_id"])

    # Routine state transition → deterministic template, zero LLM charge.
    if last_sig is not None and last_sig == sig:
        text = deterministic_narration(projection)
        return _record_receipt(project, event, mode="deterministic", outcome="delivered",
                               content_sig=sig, prompt_version=PROMPT_VERSION, narration=text,
                               cost_usd=0.0, now=now)

    # Material change (or first narration) → LLM, budget-gated. Fallbacks are explicit + visible.
    if not cfg.get("enabled", True):
        return _record_receipt(project, event, mode="fallback", outcome="fallback", content_sig=sig,
                               narration=fallback_narration(projection, "generation_disabled"),
                               fallback_reason="generation_disabled", now=now)
    if spend_in_window(project, float(cfg["window_seconds"]), now) >= float(cfg["daily_cost_usd"]):
        return _record_receipt(project, event, mode="fallback", outcome="fallback", content_sig=sig,
                               narration=fallback_narration(projection, "budget_exhausted"),
                               fallback_reason="budget_exhausted", now=now)

    fn = llm_fn or _default_llm_fn
    start = time.time()
    try:
        result = fn(build_prompt(projection), model=cfg["model"], prompt_version=PROMPT_VERSION,
                    max_tokens=int(cfg["max_tokens_per_call"]))
    except Exception as exc:  # outage / timeout / connection — no usage to charge
        return _record_receipt(project, event, mode="fallback", outcome="fallback", content_sig=sig,
                               narration=fallback_narration(projection, "provider_error"),
                               fallback_reason=f"provider_error:{type(exc).__name__}", now=now)
    latency_ms = (time.time() - start) * 1000.0
    text = (result.get("text") or "").strip()
    model = result.get("model") or cfg["model"]
    tokens_in = int(result.get("tokens_in") or 0)
    tokens_out = int(result.get("tokens_out") or 0)
    cost_usd = float(result.get("cost_usd") or 0.0)
    if not text:
        # Malformed: the call may have cost money — record the FAILED llm receipt (not hidden),
        # preserving its cost, and surface an explicit fallback narration.
        return _record_receipt(project, event, mode="llm", outcome="error", content_sig=sig,
                               model=model, prompt_version=PROMPT_VERSION, latency_ms=latency_ms,
                               tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
                               fallback_reason="malformed_response",
                               narration=fallback_narration(projection, "malformed_response"),
                               now=now)
    return _record_receipt(project, event, mode="llm", outcome="delivered", content_sig=sig,
                           model=model, prompt_version=PROMPT_VERSION, latency_ms=latency_ms,
                           tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
                           narration=text, now=now)


def list_receipts(project: str, *, entity_type: Optional[str] = None,
                  entity_id: Optional[str] = None, limit: int = 200) -> list:
    sql = "SELECT * FROM narration_receipts WHERE project=?"
    params = [project]
    if entity_type:
        sql += " AND entity_type=?"; params.append(entity_type)
    if entity_id:
        sql += " AND entity_id=?"; params.append(entity_id)
    sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
    with _conn(project) as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
