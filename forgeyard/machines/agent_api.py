"""Endpoints the agent talks to: POST /agent/v1/join and the WebSocket /agent/v1/stream.

They live outside /api on purpose. They never read the browser's session cookie (an agent
authenticates with its join token, then with its machine credential as a Bearer token), so
the console's CSRF rules have nothing to protect here, while the host check (421) still
applies to every request and to the WebSocket handshake (see security/middleware.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import anyio
from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session as DbSession
from starlette.websockets import WebSocketDisconnect

from forgeyard import __version__, audit
from forgeyard.api.deps import fail
from forgeyard.config import get_settings
from forgeyard.db import Node, get_db, new_session
from forgeyard.machines import store
from forgeyard.machines.hub import Connection, hub
from forgeyard.machines.messages import HeartbeatIn, HelloIn, InventoryMsgIn, LogsIn, StatusIn
from forgeyard.machines.protocol import (
    CLOSE_BAD_MESSAGE,
    CLOSE_PROTOCOL,
    CLOSE_REPLACED,
    CLOSE_REVOKED,
    FEATURE_WORKLOADS,
    JOIN_PATH,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SERVER_FEATURES,
    STREAM_PATH,
)
from forgeyard.workloads import placement, state as wstate
from forgeyard.security.client import client_ip
from forgeyard.security.ratelimit import limiter, retry_after

router = APIRouter(tags=["agent"])
log = logging.getLogger("forgeyard.machines")

HELLO_TIMEOUT = 15.0


class JoinIn(BaseModel):
    join_token: str = Field(max_length=200)
    protocol: int
    name: Optional[str] = Field(default=None, max_length=255)
    hostname: Optional[str] = Field(default=None, max_length=255)
    agent_version: Optional[str] = Field(default=None, max_length=64)


@router.post(JOIN_PATH)
def join(body: JoinIn, request: Request, db: DbSession = Depends(get_db)):
    """Trade a one-time join token for this machine's own credential (shown once, stored hashed)."""
    settings = get_settings()
    ip = client_ip(request)
    wait = limiter.login_wait("__join__", ip, settings.login_free_failures, settings.login_ip_failures)
    if wait:
        raise fail(429, "rate_limited", "Too many tries. Wait a moment.", retry_after=retry_after(wait))
    if body.protocol != PROTOCOL_VERSION:
        # Checked before the token is spent, so an old agent does not burn it.
        raise fail(400, "protocol_unsupported",
                   f"This server speaks agent protocol {PROTOCOL_VERSION}; the agent speaks {body.protocol}. "
                   "Use the agent from the same Forgeyard version.", server_protocol=PROTOCOL_VERSION)
    token = store.redeem_join_token(db, body.join_token)
    if token is None:
        limiter.login_failed("__join__", ip)
        audit.record(db, "machine.join_refused", ip=ip, hostname=(body.hostname or "")[:64] or None)
        db.commit()
        raise fail(403, "join_token_invalid",
                   "That join token is unknown, used or expired. Create a new one on the Machines page.")
    credential, node = store.create_node(db, name=body.name, hostname=body.hostname,
                                         agent_version=body.agent_version, protocol=body.protocol, ip=ip)
    token.node_id = node.id
    audit.record(db, "machine.joined", actor_name=token.created_by, target=node.name, ip=ip,
                 machine_id=node.id, hostname=node.hostname, token=token.public_id)
    db.commit()
    log.info("Machine %r joined from %s.", node.name, ip)
    return {
        "node_id": node.id,
        "name": node.name,
        "credential": credential,
        "protocol": PROTOCOL_VERSION,
        "heartbeat_interval": settings.agent_heartbeat_seconds,
        "offline_after": settings.agent_offline_seconds,
    }


# ---------------------------------------------------------------------------- stream
async def deny(ws: WebSocket, status: int, code: str, message: str) -> None:
    """Refuse a WebSocket handshake with an HTTP status the agent can act on."""
    if "websocket.http.response" in ws.scope.get("extensions", {}):
        await ws.send_denial_response(JSONResponse({"detail": {"code": code, "message": message}}, status_code=status))
    else:  # servers without the denial-response extension: a plain refusal (HTTP 403)
        await ws.close(code=1008)


def _bearer(ws: WebSocket) -> Optional[str]:
    header = ws.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def _db_call(fn, *args):
    db = new_session()
    try:
        result = fn(db, *args)
        db.commit()
        return result
    finally:
        db.close()


def _auth(db: DbSession, credential: Optional[str]) -> Optional[int]:
    node = store.authenticate(db, credential)
    return node.id if node else None


def _live_node(db: DbSession, node_id: int) -> Optional[Node]:
    node = db.get(Node, node_id)
    if node is None or node.revoked_at is not None or node.credential_hash is None:
        return None
    return node


def _hello(db: DbSession, node_id: int, hello: HelloIn, ip: str) -> Optional[dict]:
    node = _live_node(db, node_id)
    if node is None:
        return None
    store.apply_hello(db, node, hello, ip)
    return {"node_id": node.id, "name": node.name}


def _heartbeat(db: DbSession, node_id: int, hb: HeartbeatIn, ip: str) -> bool:
    node = _live_node(db, node_id)
    if node is None:
        return False
    store.apply_heartbeat(db, node, hb.metrics, ip)
    return True


def _inventory(db: DbSession, node_id: int, msg: InventoryMsgIn, ip: str) -> bool:
    node = _live_node(db, node_id)
    if node is None:
        return False
    node.last_heartbeat_at = store.utcnow()
    node.status = "online"
    store.apply_inventory(db, node, msg.inventory)
    return True


def _desired(db: DbSession, node_id: int, revision: int) -> Optional[dict]:
    if _live_node(db, node_id) is None:
        return None
    return wstate.desired_message(db, node_id, revision)


def _status(db: DbSession, node_id: int, msg: StatusIn) -> Optional[bool]:
    """Apply a status message. None: the machine was revoked. True: its desired state changed."""
    node = _live_node(db, node_id)
    if node is None:
        return None
    changed = wstate.apply_status(db, node, msg)
    db.commit()
    if changed:  # a replica finished: its resources are free for what is waiting
        placement.place_and_poke(db)
    return changed


def _place(db: DbSession) -> None:
    placement.place_and_poke(db)


def _went_away(db: DbSession, node_id: int) -> None:
    node = db.get(Node, node_id)
    if node is not None and node.status == "online":
        node.status = "offline"


async def _error_close(ws: WebSocket, close_code: int, code: str, message: str) -> None:
    try:
        await ws.send_json({"v": PROTOCOL_VERSION, "type": "error", "code": code, "message": message})
        await ws.close(code=close_code, reason=code)
    except (WebSocketDisconnect, RuntimeError):
        pass


def _parse(text: str, model):
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    if data.get("v") != PROTOCOL_VERSION:
        raise _ProtocolMismatch(data.get("v"))
    return model.model_validate(data)


class _ProtocolMismatch(Exception):
    pass


@router.websocket(STREAM_PATH)
async def stream(ws: WebSocket):
    # A browser always sends Origin on a WebSocket; the agent never does. Refusing it means no
    # web page can ever drive this endpoint, whatever a browser would attach to the request.
    if ws.headers.get("origin"):
        await deny(ws, 403, "browser_refused", "This endpoint is for Forgeyard agents, not browsers.")
        return
    node_id = await run_in_threadpool(_db_call, _auth, _bearer(ws))
    if node_id is None:
        await deny(ws, 401, "credential_refused",
                   "This machine credential is unknown or was revoked. Join again with a new token.")
        return
    ip = ws.client.host if ws.client else "unknown"
    await ws.accept()

    try:
        text = await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT)
        hello = _parse(text, HelloIn)
    except _ProtocolMismatch as e:
        await _error_close(ws, CLOSE_PROTOCOL, "protocol_unsupported",
                           f"This server speaks agent protocol {PROTOCOL_VERSION}, the agent sent {e.args[0]}.")
        return
    except (asyncio.TimeoutError, ValueError, ValidationError, KeyError) as e:
        await _error_close(ws, CLOSE_BAD_MESSAGE, "bad_hello", f"The first message must be a valid hello ({type(e).__name__}).")
        return
    except WebSocketDisconnect:
        return

    features = set(hello.features) & set(SERVER_FEATURES)
    conn = Connection(node_id, features=features)
    replaced = hub.attach(conn)
    if replaced is not None:
        replaced.close(CLOSE_REPLACED, "another agent connected with this credential")
    try:
        welcome = await run_in_threadpool(_db_call, _hello, node_id, hello, ip)
        if welcome is None:
            await _error_close(ws, CLOSE_REVOKED, "revoked", "This machine was revoked.")
            return
        settings = get_settings()
        await ws.send_json({"v": PROTOCOL_VERSION, "type": "welcome", **welcome, "server_version": __version__,
                            "heartbeat_interval": settings.agent_heartbeat_seconds,
                            "offline_after": settings.agent_offline_seconds,
                            "features": sorted(SERVER_FEATURES)})
        if FEATURE_WORKLOADS in features:
            conn.dirty.set()  # the desired state goes out right after the welcome
            await run_in_threadpool(_db_call, _place)  # this machine may take what is waiting
        await _serve(ws, conn, node_id, ip)
    except WebSocketDisconnect:
        pass
    finally:
        conn.fail_pending()
        if hub.detach(conn):
            # Shielded: when the handler is cancelled (the server shutting down, or a test client
            # cancelling right after the close) the machine must still be recorded offline, or it
            # stays "online" until the offline timeout.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(_db_call, _went_away, node_id)


async def _serve(ws: WebSocket, conn: Connection, node_id: int, ip: str) -> None:
    workloads = FEATURE_WORKLOADS in conn.features
    kick = asyncio.create_task(conn.kick.wait())
    recv = dirty = out = None
    # Revisions order the desired lists on one connection. They start from the clock, not from 0, so
    # a new connection (after a network drop or a server restart) always counts above the old one:
    # agents up to 1.2.0 keep the highest revision they saw across connections and drop any lower
    # list without a word, so they took no new work until the count caught up.
    revision = time.time_ns() // 1_000_000
    try:
        while True:
            if recv is None:
                recv = asyncio.create_task(ws.receive())
            if workloads and dirty is None:
                dirty = asyncio.create_task(conn.dirty.wait())
            if workloads and out is None:
                out = asyncio.create_task(conn.outbox.get())
            done, _ = await asyncio.wait({t for t in (recv, kick, dirty, out) if t is not None},
                                         return_when=asyncio.FIRST_COMPLETED)
            if kick in done:
                try:
                    await ws.close(code=conn.code or CLOSE_REVOKED, reason=conn.reason[:120])
                except RuntimeError:
                    pass
                return
            if dirty in done:
                dirty = None
                conn.dirty.clear()
                revision += 1
                desired = await run_in_threadpool(_db_call, _desired, node_id, revision)
                if desired is None:
                    await _error_close(ws, CLOSE_REVOKED, "revoked", "This machine was revoked.")
                    return
                await ws.send_json(desired)
            if out in done:
                item, out = out.result(), None
                await ws.send_json(item)
            if recv not in done:
                continue
            message, recv = recv.result(), None
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None or len(text) > MAX_MESSAGE_BYTES:
                await _error_close(ws, CLOSE_BAD_MESSAGE, "bad_message", "Send JSON text messages under 512 KB.")
                return
            try:
                kind = json.loads(text).get("type")
                if kind == "heartbeat":
                    ok = await run_in_threadpool(_db_call, _heartbeat, node_id, _parse(text, HeartbeatIn), ip)
                elif kind == "inventory":
                    ok = await run_in_threadpool(_db_call, _inventory, node_id, _parse(text, InventoryMsgIn), ip)
                elif kind == "status" and workloads:
                    changed = await run_in_threadpool(_db_call, _status, node_id, _parse(text, StatusIn))
                    ok = changed is not None
                    if changed:
                        conn.dirty.set()
                elif kind == "logs" and workloads:
                    answer = _parse(text, LogsIn)
                    conn.answer(answer.request_id, answer.model_dump())
                    ok = True
                else:
                    raise ValueError(f"unknown message type {kind!r}")
            except _ProtocolMismatch:
                await _error_close(ws, CLOSE_PROTOCOL, "protocol_unsupported", "Protocol version changed mid-connection.")
                return
            except (ValueError, ValidationError, AttributeError) as e:
                await _error_close(ws, CLOSE_BAD_MESSAGE, "bad_message", f"Message refused ({type(e).__name__}).")
                return
            if not ok:
                await _error_close(ws, CLOSE_REVOKED, "revoked", "This machine was revoked.")
                return
    finally:
        for t in (kick, recv, dirty, out):
            if t is not None:
                t.cancel()
