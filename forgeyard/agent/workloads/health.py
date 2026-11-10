"""Service health checks, run by the agent against the replica's host port."""

from __future__ import annotations

import http.client
import socket
from typing import Optional


def probe(kind: str, host: str, port: int, path: str = "/", timeout: float = 3.0,
          headers: Optional[dict[str, str]] = None) -> Optional[str]:
    """None when healthy, else a short reason. HTTP: any 2xx or 3xx answer. TCP: a connection.
    `headers`: a model replica's internal key (M4), for engines that guard /health too (speaches)."""
    if kind == "tcp":
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                # On loopback, a port in the ephemeral range can "connect" to itself when nothing
                # listens (TCP simultaneous open). That is not a healthy service.
                if s.getsockname() == s.getpeername():
                    return f"tcp {host}:{port}: nothing is listening"
                return None
        except OSError as e:
            return f"tcp {host}:{port}: {e.strerror or e}"
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": "forgeyard-health", **(headers or {})})
        status = conn.getresponse().status
    except (OSError, http.client.HTTPException) as e:
        return f"GET {path}: {getattr(e, 'strerror', None) or e or type(e).__name__}"
    finally:
        conn.close()
    return None if 200 <= status < 400 else f"GET {path} answered {status}"
