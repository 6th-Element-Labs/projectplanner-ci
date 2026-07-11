#!/usr/bin/env python3
"""BUG-35 — synchronous MCP tools must not block the request event loop."""
import asyncio
import inspect
import threading
import time

from mcp_dispatch import MCPToolDispatcher


def check(condition, message):
    if not condition:
        raise AssertionError(message)


dispatcher = MCPToolDispatcher(max_workers=2, inline_tools={"control_plane_probe"})
loop_thread = None
slow_threads = []


def slow_tool(delay: float = 0.25):
    slow_threads.append(threading.get_ident())
    time.sleep(delay)
    return "slow-complete"


def control_plane_probe():
    return {"thread": threading.get_ident(), "at": time.perf_counter()}


async_slow = dispatcher.wrap(slow_tool)
inline_probe = dispatcher.wrap(control_plane_probe)

check(inspect.iscoroutinefunction(async_slow), "ordinary sync tools register as async handlers")
check(inline_probe is control_plane_probe, "tiny probe remains an inline handler")
check(inspect.signature(async_slow) == inspect.signature(slow_tool),
      "worker wrapper preserves the FastMCP schema signature")


async def responsiveness_test():
    global loop_thread
    loop_thread = threading.get_ident()
    started = time.perf_counter()
    pending = asyncio.create_task(async_slow(0.25))
    await asyncio.sleep(0.03)
    probe = inline_probe()
    probe_elapsed = time.perf_counter() - started
    check(not pending.done(), "slow tool is still running when probe returns")
    check(probe_elapsed < 0.1,
          f"probe stays below 100ms while slow tool runs ({probe_elapsed * 1000:.1f}ms)")
    check(probe["thread"] == loop_thread, "probe executes on the request event loop")
    check(await pending == "slow-complete", "worker returns the original tool result")


asyncio.run(responsiveness_test())
check(slow_threads == [slow_threads[0]] and slow_threads[0] != loop_thread,
      "slow sync tool executes off the event-loop thread")


active = 0
peak = 0
lock = threading.Lock()


def counted_tool():
    global active, peak
    with lock:
        active += 1
        peak = max(peak, active)
    time.sleep(0.08)
    with lock:
        active -= 1


async_counted = dispatcher.wrap(counted_tool)


async def bounded_pool_test():
    await asyncio.gather(*(async_counted() for _ in range(6)))


asyncio.run(bounded_pool_test())
check(peak == 2, f"worker concurrency is capped at configured size (peak={peak})")

print("MCP threadpool tests passed")
