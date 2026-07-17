"""Browser PTY relay REST + WebSocket routes (ADAPTER-22)."""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

import auth
from switchboard.application import runner_pty_relay as relay
from switchboard.application.commands import runner_pty as runner_pty_command
from switchboard.domain import runner_pty as domain


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


class PtyTicketBody(BaseModel):
    """Typed ticket mint request (keeps ARCH-MS-84 untyped-body ceiling flat)."""

    model_config = ConfigDict(extra="allow")

    project: Optional[str] = None
    actor: Optional[str] = None
    scopes: list[str] = Field(default_factory=lambda: ["watch"])
    ttl_seconds: Optional[int] = None
    tenant_id: Optional[str] = None
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    claim_id: Optional[str] = None
    work_session_id: Optional[str] = None
    runner_session_id: Optional[str] = None
    host_id: Optional[str] = None
    wake_id: Optional[str] = None
    execution_connection_id: Optional[str] = None
    source_sha: Optional[str] = None
    permission_profile: Optional[str] = None


class PtyRevokeBody(BaseModel):
    """Typed ticket revoke request."""

    model_config = ConfigDict(extra="allow")

    project: Optional[str] = None
    actor: Optional[str] = None
    jti: Optional[str] = None
    ticket: Optional[str] = None


def create_router(
    *,
    resolve_project: ProjectResolver,
    resolve_principal: PrincipalResolver,
    resolve_body_project: BodyProjectResolver,
    hub: relay.RelayHub | None = None,
) -> APIRouter:
    router = APIRouter()

    def _hub() -> relay.RelayHub:
        return hub or relay.get_default_hub()

    @router.post("/ixp/v1/runner_sessions/{runner_session_id}/pty/ticket")
    async def mint_pty_ticket(request: Request, runner_session_id: str,
                              body: PtyTicketBody):
        payload = body.model_dump()
        project = resolve_body_project(payload)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.actor or "switchboard/operator")
        scopes = body.scopes or ["watch"]
        overlay = {
            key: payload.get(key)
            for key in domain.TICKET_BIND_FIELDS
            if payload.get(key)
        }
        result = runner_pty_command.mint_ticket_for_session(
            runner_session_id=runner_session_id,
            project=project,
            scopes=scopes,
            ttl_seconds=int(body.ttl_seconds or domain.DEFAULT_TICKET_TTL_SECONDS),
            binding_overlay=overlay,
            actor=auth.actor(principal),
        )
        if result.get("error"):
            raise HTTPException(400, result)
        return result

    @router.post("/ixp/v1/runner_sessions/{runner_session_id}/pty/revoke")
    async def revoke_pty_ticket(request: Request, runner_session_id: str,
                                body: PtyRevokeBody):
        payload = body.model_dump()
        project = resolve_body_project(payload)
        resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.actor or "switchboard/operator")
        result = runner_pty_command.revoke_ticket(
            jti=str(body.jti or ""),
            ticket=str(body.ticket or ""),
            project=project,
            hub=_hub(),
        )
        if not result.get("revoked"):
            raise HTTPException(400, result)
        result["runner_session_id"] = runner_session_id
        return result

    @router.get("/ixp/v1/runner_sessions/{runner_session_id}/pty")
    async def describe_pty_relay(runner_session_id: str, project: str = Query(...),
                                 ticket: str = ""):
        result = runner_pty_command.open_relay_descriptor(
            runner_session_id=runner_session_id,
            project=resolve_project(project),
            ticket=ticket,
        )
        if result.get("error"):
            raise HTTPException(400, result)
        # Hard guarantee: never return loopback URLs.
        for key in ("stream_url", "relay_url", "local_stream_url"):
            value = str(result.get(key) or "")
            if value and relay.is_loopback_url(value):
                result.pop(key, None)
        result["browser_safe"] = True
        result["transport"] = domain.TRANSPORT_SWITCHBOARD_PTY_RELAY
        return result

    def _ticket_from_ws(websocket: WebSocket) -> str:
        ticket = websocket.query_params.get("ticket") or ""
        if ticket:
            return ticket
        auth_header = websocket.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            return auth_header.split(" ", 1)[1].strip()
        return websocket.headers.get("x-switchboard-pty-ticket") or ""

    @router.websocket("/ixp/v1/runner_sessions/{runner_session_id}/pty")
    async def browser_pty_ws(websocket: WebSocket, runner_session_id: str):
        ticket = _ticket_from_ws(websocket)
        payload, reason = relay.verify_capability_ticket(
            ticket,
            required_scope="watch",
            expected_binding_subset={"runner_session_id": runner_session_id},
        )
        if payload is None:
            await websocket.close(code=4401, reason=(reason or "unauthorized")[:120])
            return
        await websocket.accept()
        loop = asyncio.get_running_loop()
        outbound: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=256)
        closed = {"flag": False}

        def send_fn(frame: str) -> None:
            if closed["flag"]:
                return
            try:
                loop.call_soon_threadsafe(outbound.put_nowait, frame)
            except Exception:
                closed["flag"] = True

        def close_fn() -> None:
            closed["flag"] = True
            try:
                loop.call_soon_threadsafe(outbound.put_nowait, None)
            except Exception:
                pass

        attached = _hub().attach_browser(
            runner_session_id, payload, send_fn, close_fn=close_fn)
        if not attached.get("ok"):
            await websocket.close(code=4403, reason=str(attached.get("error") or "denied")[:120])
            return
        client_id = str(attached.get("client_id") or "")

        async def writer() -> None:
            while not closed["flag"]:
                frame = await outbound.get()
                if frame is None:
                    break
                await websocket.send_text(frame)
            try:
                await websocket.close(code=4401, reason="ticket_revoked")
            except Exception:
                pass

        writer_task = asyncio.create_task(writer())
        try:
            while not closed["flag"]:
                message = await websocket.receive_text()
                result = _hub().route_browser_to_host(
                    runner_session_id, client_id, message)
                if result.get("error") == "revoked":
                    break
                if not result.get("ok") and result.get("error") in {
                    "missing_scope", "session_mismatch", "host_mismatch",
                    "task_mismatch", "session_closed",
                }:
                    err = domain.encode_frame(
                        "error",
                        {"reason": result.get("error"),
                         "detail": result.get("reason") or ""},
                    )
                    await websocket.send_text(err)
        except WebSocketDisconnect:
            pass
        finally:
            closed["flag"] = True
            writer_task.cancel()
            _hub().detach_browser(runner_session_id, client_id)

    @router.websocket("/ixp/v1/runner_sessions/{runner_session_id}/pty/host")
    async def host_pty_ws(websocket: WebSocket, runner_session_id: str,
                          host_id: str = Query("")):
        # BUG-74: host tunnel requires a distinct host_tunnel ticket — never a
        # browser watch ticket. host_id query must match the ticket bind.
        ticket = _ticket_from_ws(websocket)
        host_bind = str(host_id or "").strip()
        if not host_bind:
            await websocket.close(code=4403, reason="host_id_required")
            return
        if not ticket:
            await websocket.close(code=4401, reason="unauthorized")
            return
        payload, reason = relay.verify_capability_ticket(
            ticket,
            required_scope=domain.HOST_TUNNEL_SCOPE,
            expected_binding_subset={
                "runner_session_id": runner_session_id,
                "host_id": host_bind,
            },
        )
        if payload is None:
            await websocket.close(code=4401, reason=(reason or "unauthorized")[:120])
            return
        allowed, deny_reason = relay.ticket_allows_host_tunnel(payload)
        if not allowed:
            await websocket.close(code=4403, reason=deny_reason[:120])
            return
        await websocket.accept()
        loop = asyncio.get_running_loop()
        outbound: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=256)
        closed = {"flag": False}

        def send_fn(frame: str) -> None:
            if closed["flag"]:
                return
            try:
                loop.call_soon_threadsafe(outbound.put_nowait, frame)
            except Exception:
                closed["flag"] = True

        def close_fn() -> None:
            closed["flag"] = True
            try:
                loop.call_soon_threadsafe(outbound.put_nowait, None)
            except Exception:
                pass

        attached = _hub().attach_host(
            runner_session_id, send_fn, binding=payload, close_fn=close_fn)
        if not attached.get("ok"):
            await websocket.close(code=4403, reason=str(attached.get("error") or "denied")[:120])
            return

        async def writer() -> None:
            while not closed["flag"]:
                frame = await outbound.get()
                if frame is None:
                    break
                await websocket.send_text(frame)
            try:
                await websocket.close(code=4401, reason="ticket_revoked")
            except Exception:
                pass

        writer_task = asyncio.create_task(writer())
        try:
            while not closed["flag"]:
                message = await websocket.receive_text()
                result = _hub().route_host_to_browsers(runner_session_id, message)
                if result.get("error") == "revoked":
                    break
        except WebSocketDisconnect:
            _hub().close_session(runner_session_id, reason="host_disconnect")
        finally:
            closed["flag"] = True
            writer_task.cancel()
            _hub().detach_host(runner_session_id, send_fn)

    return router
