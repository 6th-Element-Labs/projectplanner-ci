"""Best-effort native heap release for the memory-capped Coord process."""
from __future__ import annotations

import ctypes
import sys
import threading
from functools import lru_cache
from typing import Any, Callable

from starlette.background import BackgroundTask, BackgroundTasks
from starlette.responses import Response


_TRIM_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _malloc_trim() -> Callable[[int], int] | None:
    """Return glibc's malloc_trim when the runtime provides it."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return trim
    except (AttributeError, OSError):
        return None


def release_native_heap() -> None:
    """Return fully free glibc arenas after a response."""
    trim = _malloc_trim()
    if trim is None:
        return
    with _TRIM_LOCK:
        trim(0)


def release_heap_after(response: Any) -> Any:
    """Attach heap release after ASGI finishes sending the response body."""
    if not isinstance(response, Response):
        return response
    if response.background is None:
        response.background = BackgroundTask(release_native_heap)
    elif isinstance(response.background, BackgroundTasks):
        response.background.add_task(release_native_heap)
    else:
        response.background = BackgroundTasks([
            response.background,
            BackgroundTask(release_native_heap),
        ])
    return response
