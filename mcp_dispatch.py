"""Bounded worker-thread dispatch for synchronous FastMCP tools.

FastMCP 1.27 calls synchronous tool functions directly from its async request
handler.  One slow SQLite call can therefore block every request on the event
loop.  This adapter makes registered sync tools async while preserving their
public signatures for FastMCP's schema inspection.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import time
from typing import Callable, Iterable

import anyio


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


class MCPToolDispatcher:
    """Offload sync tools through one process-wide bounded worker pool."""

    def __init__(self, max_workers: int | None = None,
                 inline_tools: Iterable[str] = (),
                 deadline_seconds: float | None = None):
        self.max_workers = max_workers or _positive_int_env("PM_MCP_SYNC_WORKERS", 4)
        self.deadline_seconds = (deadline_seconds if deadline_seconds is not None else
                                 _positive_float_env("PM_MCP_TOOL_DEADLINE_S", 28.0))
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        self.inline_tools = frozenset(inline_tools)
        self._limiter = anyio.CapacityLimiter(self.max_workers)

    @staticmethod
    def _deadline_error(tool_name: str, elapsed_ms: float) -> str:
        return json.dumps({
            "error": "tool_deadline_exceeded",
            "server_elapsed_ms": round(elapsed_ms, 3),
            "tool_name": tool_name,
            "hint": "retry serialized",
        }, sort_keys=True)

    def wrap(self, fn: Callable) -> Callable:
        """Return an async worker wrapper, or the original safe inline callable."""
        if inspect.iscoroutinefunction(fn) or fn.__name__ in self.inline_tools:
            return fn

        @functools.wraps(fn)
        async def threaded(*args, **kwargs):
            call = functools.partial(fn, *args, **kwargs)
            started = time.perf_counter()
            with anyio.move_on_after(self.deadline_seconds) as deadline_scope:
                result = await anyio.to_thread.run_sync(
                    call, limiter=self._limiter, abandon_on_cancel=True)
            if deadline_scope.cancel_called:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                return self._deadline_error(fn.__name__, elapsed_ms)
            return result

        return threaded
