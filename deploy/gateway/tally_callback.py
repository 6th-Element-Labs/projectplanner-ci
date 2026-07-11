"""LiteLLM success callback → ProjectPlanner tally ledger (UI-12).

The gateway is the only place that sees *every* LLM call and the provider's own
token/cost accounting. Without this, the `llm_spend` ledger is populated only by
explicit `report_usage` calls that the live jobs never make, so the Economics
panels read near-zero. This callback closes that gap: on every successful
completion it POSTs the provider-actual usage to `/tally/v1/spend/ingest`
(idempotent on the LiteLLM call id), so the ledger is complete even before any
caller tags its calls.

Attribution rides on LiteLLM request `metadata`: callers thread
`task_id`/`claim_id`/`agent_id`/`outcome_id`/`source`/`project`. Untagged calls
still land as `source=gateway` so nothing is silently dropped.

Registered from deploy/gateway/config.yaml:

    litellm_settings:
      callbacks: ["tally_callback.tally_logger"]

Env:
  PM_TALLY_INGEST_URL    default http://127.0.0.1:8110/tally/v1/spend/ingest
  PM_TALLY_INGEST_TOKEN  bearer token with write:ixp for the ingest endpoint
  PM_TALLY_PROJECT       default project when metadata omits one (default: switchboard)

This module must never raise into the LLM response path — every failure is
swallowed and logged, because a broken ledger write must not break a completion.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

INGEST_URL = os.environ.get(
    "PM_TALLY_INGEST_URL", "http://127.0.0.1:8110/tally/v1/spend/ingest"
)
INGEST_TOKEN = os.environ.get("PM_TALLY_INGEST_TOKEN", "")
DEFAULT_PROJECT = os.environ.get("PM_TALLY_PROJECT", "switchboard")
_TIMEOUT = float(os.environ.get("PM_TALLY_INGEST_TIMEOUT", "8"))

# Metadata keys we forward as first-class attribution columns.
_ATTRIBUTION_KEYS = ("task_id", "claim_id", "agent_id", "outcome_id", "source", "project")

# LiteLLM's PROXY injects internal bookkeeping into litellm_params.metadata
# (a UserAPIKeyAuth object, the caller's api-key hash, spend-log config, etc.).
# None of it is caller attribution, some of it is opaque/non-JSON-serializable,
# and some is sensitive (key hashes) — so it must never reach the ingest POST.
_PROXY_META_SKIP_PREFIXES = ("user_api_key", "litellm", "hidden_params",
                             "spend_logs", "proxy_server")


def _dig_metadata(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the attribution metadata LiteLLM exposes across proxy versions.

    The request body `metadata` lands on `litellm_params.metadata`; newer proxy
    builds nest the caller-supplied portion under `requester_metadata`. We union
    the top-level `metadata`, `litellm_params.metadata`, and any nested
    `requester_metadata` so a caller's tags are found wherever they surface.
    """
    merged: Dict[str, Any] = {}
    sources = [
        kwargs.get("metadata"),
        (kwargs.get("litellm_params") or {}).get("metadata"),
    ]
    nested = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    if isinstance(nested, dict):
        sources.append(nested.get("requester_metadata"))
    for src in sources:
        if isinstance(src, dict):
            merged.update(src)
    return merged


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _latency_ms(start_time: Any, end_time: Any) -> Optional[float]:
    if start_time is None or end_time is None:
        return None
    try:
        return round((end_time - start_time).total_seconds() * 1000.0, 3)
    except Exception:  # pragma: no cover - defensive across datetime/number types
        try:
            return round((float(end_time) - float(start_time)) * 1000.0, 3)
        except (TypeError, ValueError):
            return None


def build_spend_payload(
    kwargs: Dict[str, Any],
    response_obj: Any,
    start_time: Any = None,
    end_time: Any = None,
) -> Dict[str, Any]:
    """Translate a LiteLLM success event into a `/tally/v1/spend/ingest` body.

    Pure and dependency-free so it can be unit-tested without litellm or a live
    ingest endpoint. `source` defaults to `gateway` and `confidence` to
    `provider_actual` — this is the provider's own accounting.
    """
    kwargs = kwargs or {}
    meta = _dig_metadata(kwargs)

    usage: Dict[str, Any] = {}
    if response_obj is not None:
        raw_usage = None
        if isinstance(response_obj, dict):
            raw_usage = response_obj.get("usage")
        else:
            raw_usage = getattr(response_obj, "usage", None)
        if hasattr(raw_usage, "model_dump"):
            raw_usage = raw_usage.model_dump()
        elif hasattr(raw_usage, "dict"):
            raw_usage = raw_usage.dict()
        if isinstance(raw_usage, dict):
            usage = raw_usage

    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    completion_tokens = _as_int(usage.get("completion_tokens"))
    total_tokens = _as_int(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)

    litellm_params = kwargs.get("litellm_params") or {}
    provider = (
        kwargs.get("custom_llm_provider")
        or litellm_params.get("custom_llm_provider")
        or ""
    )
    model = kwargs.get("model") or ""
    if not model and isinstance(response_obj, dict):
        model = response_obj.get("model") or ""
    elif not model and response_obj is not None:
        model = getattr(response_obj, "model", "") or ""

    # request_id: prefer the stable LiteLLM call id (the ledger dedupes on it),
    # fall back to the provider response id.
    request_id = kwargs.get("litellm_call_id")
    if not request_id and isinstance(response_obj, dict):
        request_id = response_obj.get("id")
    elif not request_id and response_obj is not None:
        request_id = getattr(response_obj, "id", None)

    payload: Dict[str, Any] = {
        "request_id": request_id,
        "source": (meta.get("source") or "gateway"),
        "confidence": (meta.get("confidence") or "provider_actual"),
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": _as_float(kwargs.get("response_cost")),
        "latency_ms": _latency_ms(start_time, end_time),
        "runtime": "litellm-gateway",
        "call_site": meta.get("call_site") or "",
        "status": "ok",
        "project": meta.get("project") or DEFAULT_PROJECT,
    }
    for key in ("task_id", "claim_id", "agent_id", "outcome_id"):
        if meta.get(key):
            payload[key] = meta[key]
    # Anything else the caller explicitly tagged is preserved for forensics, but
    # ONLY JSON-serializable scalars and never proxy-internal keys — the whole
    # payload is about to be json-encoded for the ingest POST, so an opaque object
    # here (e.g. UserAPIKeyAuth) would raise and drop the spend row entirely.
    extra = {
        k: v for k, v in meta.items()
        if k not in _ATTRIBUTION_KEYS and k not in ("confidence", "call_site")
        and isinstance(v, (str, int, float, bool))
        and not any(k.startswith(p) for p in _PROXY_META_SKIP_PREFIXES)
    }
    if extra:
        payload["metadata"] = extra
    return payload


def _headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if INGEST_TOKEN:
        headers["Authorization"] = f"Bearer {INGEST_TOKEN}"
    return headers


try:  # pragma: no cover - litellm is only present in the gateway venv
    from litellm.integrations.custom_logger import CustomLogger  # type: ignore

    class TallySpendLogger(CustomLogger):
        """Posts each successful completion's provider-actual spend to the ledger."""

        def log_success_event(self, kwargs, response_obj, start_time, end_time):
            self._emit(kwargs, response_obj, start_time, end_time)

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
            await self._aemit(kwargs, response_obj, start_time, end_time)

        def _emit(self, kwargs, response_obj, start_time, end_time):
            import httpx  # litellm depends on httpx; import lazily
            try:
                payload = build_spend_payload(kwargs, response_obj, start_time, end_time)
                httpx.post(INGEST_URL, json=payload, headers=_headers(), timeout=_TIMEOUT)
            except Exception as exc:  # never break the completion path
                print(f"[tally_callback] spend ingest failed: {exc}", flush=True)

        async def _aemit(self, kwargs, response_obj, start_time, end_time):
            import httpx
            try:
                payload = build_spend_payload(kwargs, response_obj, start_time, end_time)
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    await client.post(INGEST_URL, json=payload, headers=_headers())
            except Exception as exc:
                print(f"[tally_callback] spend ingest failed: {exc}", flush=True)

    tally_logger = TallySpendLogger()
except Exception as _exc:  # pragma: no cover - keep import-safe without litellm
    TallySpendLogger = None  # type: ignore
    tally_logger = None
    if os.environ.get("PM_TALLY_CALLBACK_DEBUG"):
        print(f"[tally_callback] CustomLogger unavailable: {_exc}", flush=True)
