"""The key guard: a small reverse proxy the agent puts in front of every model replica.

An engine's own API key does not cover all of its routes. vLLM 0.32's VLLM_API_KEY guards only
/v1, /v2, /inference and /cohere: /invocations (chat), /rerank, /pooling, /score, /classify and
/tokenize answer anyone. So the engine listens on loopback only (a process binds 127.0.0.1, a
container publishes on 127.0.0.1) on a port only the agent knows, and the guard listens on the
replica's published address and port, the one the server and the gateway know. A request gets
through only with `Authorization: Bearer <the replica's internal key>`, on every path and every
method; everything else gets 401 from the guard and never reaches the engine.

- **Nothing is open.** Not even the engine's health path: the agent checks health against the
  engine's loopback port itself, so the guard has no reason to let anything through unasked.
- **Headers:** hop-by-hop headers (Connection and what it names, Keep-Alive, TE, Trailer,
  Upgrade, Expect, Proxy-*) are dropped both ways; everything else passes unchanged. The
  `Authorization` header goes on to the engine as it came: it is the internal key, which the engine
  already has in its environment, and engines that check it on their own routes (vLLM's /v1) keep
  doing so (defence in depth). Nothing is added, so the engine learns nothing new.
- **Streaming, both ways:** bodies are relayed as they arrive, a read at a time, with back-pressure
  (each write waits for the socket to drain): SSE, chunked uploads, multipart files and large
  answers never sit in memory. One connection to the engine per request, so nothing of one request
  can leak into another.
- **Disconnects:** when the client goes away mid-request, the engine's connection is closed at once,
  so the engine stops generating.
- **Engine down:** if the engine's port does not accept the connection (restarting, crashed), the
  guard closes the client's connection without an answer. The gateway treats that as "this replica
  did not take the request" and tries the next replica, exactly as when it reached the engine
  directly. A refused or broken request never becomes a 502 that the gateway would pass on.
- **Timeouts:** 30 s for a request's head, 75 s idle between requests on a kept-alive connection,
  300 s without a byte of an upload, 10 s to connect to the engine, 300 s for a write to drain. No
  limit on how long the engine takes to answer: the gateway has its own read timeout, and when it
  gives up, its disconnect stops the engine.
- **The key** is compared in constant time (hmac.compare_digest). It lives in the agent's memory only.

The guard lives in the agent. While the agent is down, the published port is closed and the
replica is unreachable from outside; the engine keeps running and a restarted agent puts a guard in
front of it again (the safe direction).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from typing import Optional

import h11

log = logging.getLogger("forgeyard.agent")

READ_SIZE = 64 * 1024
MAX_HEAD = 64 * 1024          # request or response line plus headers
MAX_CONNECTIONS = 512
HEAD_TIMEOUT = 30.0
IDLE_TIMEOUT = 75.0
BODY_TIMEOUT = 300.0
CONNECT_TIMEOUT = 10.0
WRITE_TIMEOUT = 300.0
MAX_PIPELINED = 1024 * 1024   # bytes of a next request a client may send while waiting for an answer
REFUSAL_LOG_EVERY = 60.0

HOP_BY_HOP = {b"connection", b"keep-alive", b"proxy-connection", b"proxy-authorization", b"proxy-authenticate",
              b"te", b"trailer", b"upgrade", b"expect"}

REFUSAL = json.dumps({"error": {
    "message": "This port belongs to a Forgeyard model replica. Call the model through the Forgeyard gateway "
               "(/v1 on the server) with an API key.",
    "type": "invalid_request_error", "param": None, "code": "replica_key_required"}}).encode()


class _Gone(Exception):
    """The client went away (or broke the protocol): stop, close everything."""


def _relayed(headers) -> list[tuple[bytes, bytes]]:
    """Headers minus hop-by-hop ones and whatever `Connection` names."""
    named = set()
    for name, value in headers:
        if name == b"connection":
            named.update(t.strip().lower() for t in value.split(b",") if t.strip())
    out = []
    for name, value in headers:
        if name in HOP_BY_HOP or name in named:
            continue
        out.append((name, value))
    return out


class Guard:
    def __init__(self, *, listen: tuple[str, int], upstream: tuple[str, int], key: Optional[str], name: str = "",
                 head_timeout: float = HEAD_TIMEOUT, idle_timeout: float = IDLE_TIMEOUT,
                 body_timeout: float = BODY_TIMEOUT, connect_timeout: float = CONNECT_TIMEOUT,
                 write_timeout: float = WRITE_TIMEOUT):
        self.listen = listen
        self.upstream = upstream
        self.name = name
        self._key = key.encode() if key else None
        self.head_timeout, self.idle_timeout, self.body_timeout = head_timeout, idle_timeout, body_timeout
        self.connect_timeout, self.write_timeout = connect_timeout, write_timeout
        self._server: Optional[asyncio.base_events.Server] = None
        self._conns: set[asyncio.Task] = set()
        self.refused = 0          # requests turned away without the key (tests, logs)
        self.forwarded = 0        # requests passed to the engine
        self._refused_logged = 0
        self._refusal_at = 0.0

    # ------------------------------------------------------------------ lifecycle
    @property
    def listening(self) -> bool:
        return self._server is not None and self._server.is_serving()

    def set_key(self, key: Optional[str]) -> None:
        self._key = key.encode() if key else None

    def has_key(self, key: Optional[str]) -> bool:
        return (key.encode() if key else None) == self._key

    async def start(self) -> None:
        """Listen on the published address. Raises OSError when it cannot (the port is taken)."""
        host, port = self.listen
        self._server = await asyncio.start_server(self._accept, host, port, reuse_address=True,
                                                  limit=READ_SIZE)

    async def close(self) -> None:
        """Stop listening and cut every connection (the engine's ones with them)."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        conns = list(self._conns)
        for t in conns:
            t.cancel()
        await asyncio.gather(*conns, return_exceptions=True)
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 5)

    # ------------------------------------------------------------------ one client connection
    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if len(self._conns) >= MAX_CONNECTIONS:
            writer.close()
            return
        self._conns.add(task)
        try:
            await self._serve(reader, writer)
        except (_Gone, OSError, asyncio.TimeoutError, h11.ProtocolError):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:  # never let one connection take the agent down
            log.exception("Guard %s: a connection failed", self.name)
        finally:
            self._conns.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)

    async def _next(self, conn: h11.Connection, reader: asyncio.StreamReader, timeout: float):
        """The client's next event, within `timeout` for the whole of it (a request head sent a byte
        at a time does not get more time)."""
        async with asyncio.timeout(timeout):
            while True:
                event = conn.next_event()
                if event is not h11.NEED_DATA:
                    return event
                conn.receive_data(await reader.read(READ_SIZE))  # b"": closed; h11 reports it

    async def _write(self, writer: asyncio.StreamWriter, data: Optional[bytes]) -> None:
        if data:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), self.write_timeout)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEAD)
        first = True
        while True:
            try:
                event = await self._next(conn, reader, self.head_timeout if first else self.idle_timeout)
            except h11.RemoteProtocolError as e:
                await self._error(conn, writer, e.error_status_hint)
                return
            except asyncio.TimeoutError:
                return
            first = False
            if not isinstance(event, h11.Request):
                return  # ConnectionClosed (or PAUSED, which cannot happen at the start of a cycle)
            if not self._authorized(event.headers):
                await self._refuse(conn, writer, event, writer.get_extra_info("peername"))
                return  # never read the body of a request that will not be served
            if not await self._forward(conn, reader, writer, event):
                return
            if conn.our_state is not h11.DONE or conn.their_state is not h11.DONE:
                return
            conn.start_next_cycle()

    def _authorized(self, headers) -> bool:
        if self._key is None:
            return False
        values = [v for n, v in headers if n == b"authorization"]
        if len(values) != 1:
            return False
        scheme, _, token = values[0].strip().partition(b" ")
        if scheme.lower() != b"bearer":
            return False
        return hmac.compare_digest(token.strip(), self._key)

    async def _refuse(self, conn: h11.Connection, writer: asyncio.StreamWriter, req: h11.Request, peer) -> None:
        self.refused += 1
        now = time.monotonic()
        if now - self._refusal_at >= REFUSAL_LOG_EVERY:
            count, self._refused_logged = self.refused - self._refused_logged, self.refused
            self._refusal_at = now
            who = peer[0] if isinstance(peer, tuple) and peer else "?"
            log.warning("Guard of %s on %s:%s refused %d request(s) without the internal key (latest: %s %s from %s).",
                        self.name, self.listen[0], self.listen[1], count, req.method.decode("latin-1"),
                        req.target[:80].decode("latin-1"), who)
        body = b"" if req.method == b"HEAD" else REFUSAL
        await self._write(writer, conn.send(h11.Response(status_code=401, headers=[
            (b"content-type", b"application/json"), (b"content-length", str(len(REFUSAL)).encode()),
            (b"www-authenticate", b'Bearer realm="forgeyard-replica"'), (b"connection", b"close")])))
        await self._write(writer, conn.send(h11.Data(data=body)) if body else None)
        await self._write(writer, conn.send(h11.EndOfMessage()))

    async def _error(self, conn: h11.Connection, writer: asyncio.StreamWriter, status: int) -> None:
        if conn.our_state not in (h11.IDLE, h11.SEND_RESPONSE):
            return
        with contextlib.suppress(Exception):
            await self._write(writer, conn.send(h11.Response(status_code=status, headers=[
                (b"content-length", b"0"), (b"connection", b"close")])))
            await self._write(writer, conn.send(h11.EndOfMessage()))

    # ------------------------------------------------------------------ one request to the engine
    async def _forward(self, conn: h11.Connection, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       req: h11.Request) -> bool:
        """Relay one authorised request. True when the client connection can carry another one."""
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(*self.upstream, limit=READ_SIZE), self.connect_timeout)
        except (OSError, asyncio.TimeoutError):
            # The engine is not there: no answer, so the gateway tries another replica.
            return False
        self.forwarded += 1
        up = h11.Connection(h11.CLIENT, max_incomplete_event_size=MAX_HEAD)
        try:
            if conn.they_are_waiting_for_100_continue:
                await self._write(writer, conn.send(h11.InformationalResponse(status_code=100, headers=[])))
            headers = _relayed(req.headers) + [(b"connection", b"close")]
            await self._write(up_writer, up.send(h11.Request(method=req.method, target=req.target, headers=headers)))
            upload = asyncio.ensure_future(self._upload(conn, reader, up, up_writer))
            download = asyncio.ensure_future(self._download(conn, writer, up, up_reader))
            try:
                done, _ = await asyncio.wait({upload, download}, return_when=asyncio.FIRST_COMPLETED)
                if download in done:
                    download.result()  # raises when the answer broke off
                    if not upload.done():
                        upload.cancel()
                    with contextlib.suppress(BaseException):
                        await upload
                    return conn.their_state is h11.DONE
                upload.result()  # the client went away (or broke the protocol) before the answer was done
                return False
            finally:
                for t in (upload, download):
                    t.cancel()
                for t in (upload, download):
                    with contextlib.suppress(BaseException):
                        await t
        except (_Gone, OSError, asyncio.TimeoutError, h11.ProtocolError):
            return False
        finally:
            up_writer.close()  # the engine sees the connection close and stops
            with contextlib.suppress(Exception):
                await asyncio.wait_for(up_writer.wait_closed(), 2)

    async def _upload(self, conn: h11.Connection, reader: asyncio.StreamReader, up: h11.Connection,
                      up_writer: asyncio.StreamWriter) -> None:
        """The request body to the engine; then watch the client until it goes away. Returns (or
        raises) only when the client is gone or broke the protocol."""
        while True:
            try:
                event = await self._next(conn, reader, self.body_timeout)
            except (h11.RemoteProtocolError, asyncio.TimeoutError):
                raise _Gone() from None
            if isinstance(event, h11.Data):
                await self._write(up_writer, up.send(h11.Data(data=event.data)))
            elif isinstance(event, h11.EndOfMessage):
                await self._write(up_writer, up.send(h11.EndOfMessage()))
                break
            else:
                raise _Gone()  # ConnectionClosed: the client left in the middle of its request
        pending = 0
        while True:  # the request is sent; a client that closes now has given up on the answer
            data = await reader.read(READ_SIZE)
            if not data:
                raise _Gone()
            pending += len(data)
            if pending > MAX_PIPELINED:
                raise _Gone()
            conn.receive_data(data)  # a pipelined next request, kept for the next cycle

    async def _download(self, conn: h11.Connection, writer: asyncio.StreamWriter, up: h11.Connection,
                        up_reader: asyncio.StreamReader) -> None:
        """The engine's answer to the client, as it comes."""
        while True:
            event = up.next_event()
            if event is h11.NEED_DATA:
                up.receive_data(await up_reader.read(READ_SIZE))
                continue
            if isinstance(event, h11.InformationalResponse):
                continue  # 1xx from the engine (the client's Expect was answered by the guard)
            if isinstance(event, h11.Response):
                await self._write(writer, conn.send(h11.Response(
                    status_code=event.status_code, reason=event.reason, headers=_relayed(event.headers))))
            elif isinstance(event, h11.Data):
                await self._write(writer, conn.send(h11.Data(data=event.data)))
            elif isinstance(event, h11.EndOfMessage):
                await self._write(writer, conn.send(h11.EndOfMessage()))
                return
            else:
                raise _Gone()  # the engine closed before its answer was complete
