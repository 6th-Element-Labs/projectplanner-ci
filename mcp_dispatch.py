"""Bounded worker-thread dispatch for synchronous FastMCP tools.

FastMCP 1.27 calls synchronous tool functions directly from its async request
handler.  One slow SQLite call can therefore block every request on the event
loop.  This adapter makes registered sync tools async while preserving their
public signatures for FastMCP's schema inspection.
"""
from __future__ import annotations

import functools
import inspect
import os
from typing import Callable, Iterable

import anyio


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


class MCPToolDispatcher:
    """Offload sync tools through one process-wide bounded worker pool."""

    def __init__(self, max_workers: int | None = None,
                 inline_tools: Iterable[str] = ()):
        self.max_workers = max_workers or _positive_int_env("PM_MCP_SYNC_WORKERS", 4)
        self.inline_tools = frozenset(inline_tools)
        self._limiter = anyio.CapacityLimiter(self.max_workers)

    def wrap(self, fn: Callable) -> Callable:
        """Return an async worker wrapper, or the original safe inline callable."""
        if inspect.iscoroutinefunction(fn) or fn.__name__ in self.inline_tools:
            return fn

        @functools.wraps(fn)
        async def threaded(*args, **kwargs):
            call = functools.partial(fn, *args, **kwargs)
            return await anyio.to_thread.run_sync(call, limiter=self._limiter)

        return threaded
