"""Live agent connections, so the server can close one at once (revoke, timeout, restart).

One process serves everything (as in M0), so the registry is in memory. `close()` may be
called from any thread: sync API routes run in FastAPI's thread pool, while each
connection's handler waits on its own event loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional


class Connection:
    def __init__(self, node_id: int, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.node_id = node_id
        self.loop = loop or asyncio.get_running_loop()
        self.kick = asyncio.Event()
        self.code: Optional[int] = None
        self.reason = ""

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
