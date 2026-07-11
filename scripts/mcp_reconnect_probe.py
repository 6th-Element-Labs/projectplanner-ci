#!/usr/bin/env python3
"""Prove timeout -> transport drop -> fresh MCP session -> healthy probe.

This exercises the server-side reconnect contract in one client process.  It does
not claim that a particular IDE version automatically creates the fresh transport;
client-specific recovery steps are documented in docs/MCP.md.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import timedelta

import anyio


@asynccontextmanager
async def _session(url: str, headers: dict[str, str], timeout_s: float):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    timeout = httpx.Timeout(timeout_s, connect=timeout_s)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        async with streamable_http_client(
                url, http_client=client, terminate_on_close=False) as streams:
            read_stream, write_stream, get_session_id = streams
            async with ClientSession(
                    read_stream, write_stream,
                    read_timeout_seconds=timedelta(seconds=timeout_s)) as session:
                await session.initialize()
                yield session, get_session_id


async def _force_parallel_timeout(session, project: str, parallel: int,
                                  drop_after_s: float) -> None:
    async def search(index: int):
        await session.call_tool("search_tasks", {
            "project": project,
            "query": f"bug-38-reconnect-{index}",
        })

    with anyio.fail_after(drop_after_s):
        async with anyio.create_task_group() as group:
            for index in range(parallel):
                group.start_soon(search, index)


def _text_result(result) -> str:
    for item in result.content:
        text = getattr(item, "text", None)
        if text is not None:
            return text
    raise RuntimeError("tool result contained no text content")


async def run(args) -> dict:
    headers = {}
    token = os.environ.get(args.token_env, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    first_session_id = None
    timed_out = False
    drop_error = None
    try:
        async with _session(args.url, headers, args.timeout_s) as (session, get_id):
            first_session_id = get_id()
            try:
                await _force_parallel_timeout(
                    session, args.project, args.parallel, args.drop_after_ms / 1000.0)
            except TimeoutError:
                timed_out = True
            else:
                raise RuntimeError(
                    "parallel calls completed before the forced timeout; lower --drop-after-ms")
    except Exception as exc:
        # A client may surface transport shutdown while the deliberately cancelled
        # requests unwind. Preserve the type, then prove recovery independently.
        if not timed_out:
            raise
        drop_error = type(exc).__name__

    reconnect_started = time.perf_counter()
    async with _session(args.url, headers, args.timeout_s) as (session, get_id):
        second_session_id = get_id()
        result = await session.call_tool("control_plane_probe", {"project": args.project})
    reconnect_ms = (time.perf_counter() - reconnect_started) * 1000.0
    probe = json.loads(_text_result(result))

    if not timed_out:
        raise RuntimeError("the first transport did not time out as requested")
    if not probe.get("mcp_framing", {}).get("stateless_http"):
        raise RuntimeError("reconnected server did not advertise stateless_http")
    if "server_elapsed_ms" not in probe:
        raise RuntimeError("reconnected probe omitted server_elapsed_ms")

    return {
        "schema": "switchboard.mcp_reconnect_probe.v1",
        "ok": True,
        "url": args.url,
        "project": args.project,
        "parallel_calls_cancelled": args.parallel,
        "forced_drop_after_ms": args.drop_after_ms,
        "drop_error_type": drop_error,
        "same_process": True,
        "first_session_id": first_session_id,
        "second_session_id": second_session_id,
        "fresh_transport_created": True,
        "reconnect_ms": round(reconnect_ms, 3),
        "probe_server_elapsed_ms": probe["server_elapsed_ms"],
        "probe_stateless_http": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://plan.taikunai.com/mcp")
    parser.add_argument("--project", default="switchboard")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--drop-after-ms", type=float, default=5.0)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--token-env", default="PM_MCP_TOKEN")
    args = parser.parse_args()
    if args.parallel < 1 or args.drop_after_ms <= 0 or args.timeout_s <= 0:
        parser.error("parallel, drop-after-ms, and timeout-s must be positive")
    print(json.dumps(anyio.run(run, args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
