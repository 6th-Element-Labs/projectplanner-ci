"""Handshake result cache (ADAPTER-59).

Redis holds per-principal "already sent" digests so a second
``get_working_agreement`` / ``get_project_contract`` in the same session returns
a short not-modified stub. Unset ``PM_REDIS_URL`` uses in-process memory. Redis
errors fail open to memory so MCP still serves.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import threading
import time
import urllib.parse
from typing import Any, Callable, Optional

LOGGER = logging.getLogger("switchboard.mcp.handshake_cache")
SCHEMA = "switchboard.mcp_result_cache.v1"
DEFAULT_TTL_S = 6 * 60 * 60
_DUMPS_DEFAULT: Callable[[Any], str] = lambda obj: json.dumps(obj, sort_keys=True)
_PROCESS_CACHE: Optional["HandshakeCache"] = None
_PROCESS_LOCK = threading.Lock()


def _digests_equal(left: str, right: str) -> bool:
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def payload_digest(body: Any, dumps: Callable[[Any], str] = _DUMPS_DEFAULT) -> str:
    encoded = dumps(body).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class MemoryBackend:
    """Process-local TTL map used when Redis is unset or unreachable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> Optional[str]:
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._items.pop(key, None)
                return None
            return value

    def setex(self, key: str, ttl_s: int, value: str) -> None:
        expires_at = time.time() + max(1, int(ttl_s))
        with self._lock:
            self._items[key] = (expires_at, str(value))


class RedisBackend:
    """Minimal Redis GET/SETEX client. Failures fall back to memory."""

    def __init__(self, url: str, timeout_s: float = 0.2,
                 fallback: Optional[MemoryBackend] = None) -> None:
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = int(parsed.port or 6379)
        path = (parsed.path or "/0").strip("/")
        self.db = int(path or "0")
        self.timeout_s = timeout_s
        self.fallback = fallback or MemoryBackend()

    def get(self, key: str) -> Optional[str]:
        try:
            reply = self._command(b"GET", key.encode("utf-8"))
        except (OSError, TimeoutError, ValueError) as exc:
            LOGGER.warning("redis GET failed; using memory cache: %s", exc)
            return self.fallback.get(key)
        if reply is None:
            return self.fallback.get(key)
        self.fallback.setex(key, DEFAULT_TTL_S, reply)
        return reply

    def setex(self, key: str, ttl_s: int, value: str) -> None:
        self.fallback.setex(key, ttl_s, value)
        try:
            self._command(
                b"SETEX",
                key.encode("utf-8"),
                str(max(1, int(ttl_s))).encode("ascii"),
                value.encode("utf-8"),
            )
        except (OSError, TimeoutError, ValueError) as exc:
            LOGGER.warning("redis SETEX failed; memory cache kept: %s", exc)

    def _command(self, *parts: bytes) -> Optional[str]:
        payload = _encode_resp(parts)
        with socket.create_connection((self.host, self.port), self.timeout_s) as conn:
            conn.settimeout(self.timeout_s)
            if self.db:
                conn.sendall(_encode_resp((b"SELECT", str(self.db).encode("ascii"))))
                _read_resp(conn)
            conn.sendall(payload)
            return _read_resp(conn)


def _encode_resp(parts: tuple[bytes, ...]) -> bytes:
    chunks = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        chunks.append(f"${len(part)}\r\n".encode("ascii"))
        chunks.append(part)
        chunks.append(b"\r\n")
    return b"".join(chunks)


def _read_resp(conn: socket.socket) -> Optional[str]:
    header = _read_line(conn)
    if not header:
        raise ValueError("empty redis reply")
    kind = header[:1]
    if kind == b"+":
        return header[1:].decode("utf-8")
    if kind == b"-":
        raise ValueError(header[1:].decode("utf-8", "replace"))
    if kind == b":":
        return header[1:].decode("ascii")
    if kind == b"$":
        size = int(header[1:])
        if size < 0:
            return None
        data = _read_exact(conn, size + 2)
        return data[:-2].decode("utf-8")
    raise ValueError(f"unsupported redis reply {header!r}")


def _read_line(conn: socket.socket) -> bytes:
    buf = bytearray()
    while True:
        chunk = conn.recv(1)
        if not chunk:
            break
        buf.extend(chunk)
        if buf.endswith(b"\r\n"):
            return bytes(buf[:-2])
    return bytes(buf)


def _read_exact(conn: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = conn.recv(size - len(buf))
        if not chunk:
            raise ValueError("truncated redis bulk reply")
        buf.extend(chunk)
    return bytes(buf)


class HandshakeCache:
    def __init__(self, backend: Any, ttl_s: int = DEFAULT_TTL_S) -> None:
        self.backend = backend
        self.ttl_s = max(1, int(ttl_s))

    def wrap(
        self,
        name: str,
        project: str,
        body: Any,
        *,
        principal_id: str = "",
        if_none_match: str = "",
        dumps: Callable[[Any], str] = _DUMPS_DEFAULT,
    ) -> str:
        digest = payload_digest(body, dumps)
        if if_none_match and _digests_equal(if_none_match, digest):
            return dumps(_not_modified(name, project, digest))
        seen_key = f"sb:mcp:seen:{principal_id}:{name}:{project}"
        if principal_id:
            seen = self.backend.get(seen_key)
            if seen and _digests_equal(seen, digest):
                return dumps(_not_modified(name, project, digest))
            self.backend.setex(seen_key, self.ttl_s, digest)
        payload = dict(body)
        payload["cache"] = {"digest": digest, "name": name, "schema": SCHEMA}
        return dumps(payload)


def _not_modified(name: str, project: str, digest: str) -> dict[str, Any]:
    return {
        "digest": digest,
        "name": name,
        "project": project,
        "schema": SCHEMA,
        "unchanged": True,
    }


def open_backend(url: Optional[str] = None) -> Any:
    resolved = (url if url is not None else os.environ.get("PM_REDIS_URL", "")).strip()
    if not resolved:
        return MemoryBackend()
    return RedisBackend(resolved)


def configure_from_env() -> "HandshakeCache":
    global _PROCESS_CACHE
    with _PROCESS_LOCK:
        _PROCESS_CACHE = HandshakeCache(open_backend())
        return _PROCESS_CACHE


def cache_for_process() -> HandshakeCache:
    global _PROCESS_CACHE
    cache = _PROCESS_CACHE
    if cache is not None:
        return cache
    return configure_from_env()
