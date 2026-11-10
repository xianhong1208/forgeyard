"""Live agent connections, so the server can close one at once (revoke, timeout, restart),
tell an agent its desired state changed, and ask it for a replica's log tail.

One process serves everything (as in M0), so the registry is in memory. `close()`, `poke()`
and `request()` may be called from any thread: sync API routes run in FastAPI's thread pool,
while each connection's handler waits on its own event loop.

The desired state itself is never queued: `poke()` only marks it stale, and the connection's
handler reads it from the database and sends it. Pokes from many threads therefore collapse
into one message, and the agent always gets the latest state in order.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import threading
from typing import Optional

_request_ids = itertools.count(1)


class Connection:
    def __init__(self, node_id: int, loop: Optional[asyncio.AbstractEventLoop] = None, features=()):
        self.node_id = node_id
        self.loop = loop or asyncio.get_running_loop()
        self.kick = asyncio.Event()
        self.code: Optional[int] = None
        self.reason = ""
        self.features = set(features)
        # The desired state changed: the handler re-reads and sends it.
        self.dirty = asyncio.Event()
        # Other messages to send (log requests), in order.
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.pending: dict[str, concurrent.futures.Future] = {}
        self._lock = threading.Lock()

    def _call(self, fn) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            fn()
        elif not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn)

    def poke(self) -> None:
        """The desired state of this machine changed. Thread-safe."""
        self._call(self.dirty.set)

    def request(self, message: dict) -> concurrent.futures.Future:
        """Send a message that the agent answers with the same request_id. Thread-safe; the
        caller waits on the future (with a timeout) and calls forget() when done."""
        rid = f"r{next(_request_ids)}"
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self.pending[rid] = fut
        self._call(lambda: self.outbox.put_nowait({**message, "request_id": rid}))
        fut.request_id = rid  # type: ignore[attr-defined]
        return fut

    def answer(self, request_id: str, message: dict) -> bool:
        with self._lock:
            fut = self.pending.pop(request_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(message)
        return True

    def forget(self, fut: concurrent.futures.Future) -> None:
        with self._lock:
            self.pending.pop(getattr(fut, "request_id", ""), None)

    def fail_pending(self) -> None:
        with self._lock:
            pending, self.pending = self.pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("the agent disconnected"))

    def close(self, code: int, reason: str) -> None:
        """Ask the handler to close the socket with this code. Thread-safe; the first code wins."""

        def _set() -> None:
            if self.code is None:
                self.code, self.reason = code, reason
            self.kick.set()

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            _set()
        elif not self.loop.is_closed():
            self.loop.call_soon_threadsafe(_set)


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[int, Connection] = {}

    def attach(self, conn: Connection) -> Optional[Connection]:
        """Register a connection; returns the one it replaced (the caller closes it)."""
        with self._lock:
            old = self._conns.get(conn.node_id)
            self._conns[conn.node_id] = conn
        return old if old is not conn else None

    def detach(self, conn: Connection) -> bool:
        """Forget a connection. False if a newer one for the same machine took its place."""
        with self._lock:
            if self._conns.get(conn.node_id) is conn:
                del self._conns[conn.node_id]
                return True
            return False

    def is_current(self, conn: Connection) -> bool:
        with self._lock:
            return self._conns.get(conn.node_id) is conn

    def connected(self, node_id: int) -> bool:
        with self._lock:
            return node_id in self._conns

    def get(self, node_id: int) -> Optional[Connection]:
        with self._lock:
            return self._conns.get(node_id)

    def poke(self, node_id: int) -> bool:
        """Tell this machine's agent (if connected) that its desired state changed."""
        conn = self.get(node_id)
        if conn is None:
            return False
        conn.poke()
        return True

    def close(self, node_id: int, code: int, reason: str) -> bool:
        with self._lock:
            conn = self._conns.get(node_id)
        if conn is None:
            return False
        conn.close(code, reason)
        return True

    def close_all(self, code: int, reason: str) -> int:
        with self._lock:
            conns = list(self._conns.values())
        for conn in conns:
            conn.close(code, reason)
        return len(conns)

    def reset(self) -> None:
        with self._lock:
            self._conns.clear()


hub = Hub()
