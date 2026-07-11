"""Layer-0 DB primitives: connection helpers, JSON/coercion, idempotency + hashing.

Extracted verbatim from store.py (ARCH-3). These have zero upward dependencies — they
call only each other, the stdlib, and constants. The project-aware connection factories
(_conn/_control_plane_conn) stay in store.py because they depend on _resolve (project
resolution, Layer 1); they call _sqlite_timeout_s from here via re-export. store.py
re-exports everything below through `from db.core import *`.
"""
import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403

__all__ = [
    "_registry_conn",
    "_sqlite_timeout_s",
    "_sqlite_busy",
    "_retry_on_locked",
    "sqlite_lock_wait_count",
    "sqlite_lock_waits_in_window",
    "register_lock_wait_observer",
    "_json_size_bytes",
    "_json_list_field",
    "_json_object_field",
    "_json_payload",
    "_json_obj",
    "_jsonish",
    "_parse_jsonish",
    "_truthy",
    "_coerce_str_list",
    "coerce_csv_list",
    "_slug_token",
    "_text_tail",
    "_request_hash",
    "_idem_hit",
    "_idem_store",
    "_canonical_payload",
    "_payload_hash",
    "hash_token",
    "_insert_row",
    "_table_columns",
]


def _registry_conn():
    os.makedirs(os.path.dirname(PROJECT_REGISTRY_DB_PATH), exist_ok=True)
    c = sqlite3.connect(PROJECT_REGISTRY_DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def hash_token(token: str) -> str:
    """Stable one-way token hash for principal lookup."""
    return hashlib.sha256(("switchboard:" + (token or "")).encode("utf-8")).hexdigest()

def coerce_csv_list(value: Any) -> List[str]:
    """Normalize REST/CLI list fields that may arrive as a list or comma/newline string."""
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in raw:
        for part in str(item).replace("\n", ",").split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out

def _sqlite_timeout_s(env_name: str, default_s: float) -> float:
    try:
        return max(0.0, float(os.environ.get(env_name, str(default_s))))
    except (TypeError, ValueError):
        return default_s

def _sqlite_busy(exc: Exception) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in text or "database is busy" in text or "locked" in text
    )


_sqlite_lock_wait_counter = 0
_sqlite_lock_wait_times: deque = deque(maxlen=4096)
_sqlite_lock_wait_lock = threading.Lock()

# Best-effort observers notified on every lock-wait retry. The MCP observability
# collector registers one (HARDEN-63) so contention is attributable to the exact
# in-flight tool; the web process leaves this empty. Kept in db.core so the
# retry loop stays the single source of truth without importing an upward layer.
_lock_wait_observers: List[Callable[[], None]] = []


def register_lock_wait_observer(callback: "Callable[[], None]") -> None:
    """Register a cheap, non-raising callback fired once per sqlite lock-wait retry."""
    _lock_wait_observers.append(callback)


def sqlite_lock_wait_count() -> int:
    """Lifetime lock-wait retry count (dashboard/observability)."""
    with _sqlite_lock_wait_lock:
        return _sqlite_lock_wait_counter


def sqlite_lock_waits_in_window(window_s: float = 60.0) -> int:
    """Lock-wait retries inside the trailing window — used for load-shed decisions."""
    cutoff = time.time() - max(1.0, float(window_s))
    with _sqlite_lock_wait_lock:
        while _sqlite_lock_wait_times and _sqlite_lock_wait_times[0] < cutoff:
            _sqlite_lock_wait_times.popleft()
        return len(_sqlite_lock_wait_times)


def _record_sqlite_lock_wait() -> None:
    global _sqlite_lock_wait_counter
    now = time.time()
    with _sqlite_lock_wait_lock:
        _sqlite_lock_wait_counter += 1
        _sqlite_lock_wait_times.append(now)
    # Notify observers outside the lock; they must be cheap and must not raise.
    for _observer in _lock_wait_observers:
        try:
            _observer()
        except Exception:
            pass


def _retry_on_locked(thunk, attempts: int = 5, base_delay: float = 0.1):
    """Deprecated — PERF-2 routes writes through the single-writer queue instead.

    Kept as a no-retry pass-through so legacy call sites and tests that patch this
    symbol keep working until they are migrated to ``_write_through``.
    """
    return thunk()


def _json_size_bytes(value: Any) -> int:
    try:
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except TypeError:
        body = str(value).encode("utf-8")
    return len(body)

def _json_list_field(value: Any) -> str:
    if value in (None, ""):
        parsed: Any = []
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [item for item in coerce_csv_list(value)]
    else:
        parsed = value
    if not isinstance(parsed, list):
        parsed = [parsed]
    return json.dumps(parsed, sort_keys=True)

def _json_object_field(value: Any) -> str:
    if value in (None, ""):
        parsed: Any = {}
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = {"text": value}
    else:
        parsed = value
    if not isinstance(parsed, dict):
        parsed = {"value": parsed}
    return json.dumps(parsed, sort_keys=True)

def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text[0:1] in ("{", "["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": value}
        return {"text": value}
    return value

def _slug_token(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (value or "").strip().lower()).strip("_")

def _json_payload(raw: str) -> Any:
    """Parse payload JSON while preserving legacy scalar payloads."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}

def _request_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _idem_hit(c: sqlite3.Connection, operation: str, idem_key: str,
              actor: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not idem_key:
        return None
    row = c.execute("SELECT request_hash, response_json FROM idempotency_keys "
                    "WHERE idem_key=? AND operation=?", (idem_key, operation)).fetchone()
    if not row:
        return None
    if row["request_hash"] != _request_hash(payload):
        return {"error": "idempotency conflict", "idem_key": idem_key, "operation": operation}
    return json.loads(row["response_json"])

def _idem_store(c: sqlite3.Connection, operation: str, idem_key: str,
                actor: str, payload: Dict[str, Any], response: Dict[str, Any]) -> None:
    if not idem_key:
        return
    c.execute(
        "INSERT OR REPLACE INTO idempotency_keys"
        "(idem_key, operation, actor, request_hash, response_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (idem_key, operation, actor, _request_hash(payload), json.dumps(response, sort_keys=True), time.time()),
    )

def _canonical_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return json.loads(json.dumps(payload or {}, sort_keys=True, separators=(",", ":")))

def _payload_hash(payload: Optional[Dict[str, Any]]) -> str:
    return _request_hash(_canonical_payload(payload))

def _json_obj(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default

def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

def _text_tail(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    return text[-limit:] if len(text) > limit else text

def _jsonish(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return {"text": value}
    return {"value": value}

def _table_columns(c: sqlite3.Connection, table: str) -> List[str]:
    return [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]

def _insert_row(c: sqlite3.Connection, table: str, row: Dict[str, Any],
                skip_columns: Optional[set] = None) -> None:
    skip_columns = skip_columns or set()
    cols = [col for col in _table_columns(c, table) if col in row and col not in skip_columns]
    if not cols:
        return
    placeholders = ",".join("?" for _ in cols)
    c.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        [row[col] for col in cols],
    )

def _coerce_str_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, tuple):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
    return [x.strip() for x in re.split(r"[\n,]+", text) if x.strip()]
