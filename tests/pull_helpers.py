"""A stand-in for the Docker Engine API on a Unix socket: `POST /images/create` answers with a script of
JSON progress events, sent as Docker sends them (HTTP/1.1, chunked, one object per line).

The events follow what dockerd prints for a real pull: "Pulling from", then per layer "Pulling fs
layer" / "Waiting", "Downloading" with progressDetail {current, total}, "Verifying Checksum",
"Download complete", "Extracting", "Pull complete", or "Already exists"; then "Digest:" and "Status:".
"""

from __future__ import annotations

import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

MIB = 1024**2


def layer_events(lid: str, size: int, steps: int = 4) -> list[dict]:
    """A layer downloaded in `steps` parts, then unpacked."""
    out = [{"status": "Pulling fs layer", "progressDetail": {}, "id": lid}]
    for n in range(1, steps + 1):
        out.append({"status": "Downloading", "progressDetail": {"current": size * n // steps, "total": size},
                    "progress": "[==>   ]", "id": lid})
    out += [{"status": "Verifying Checksum", "progressDetail": {}, "id": lid},
            {"status": "Download complete", "progressDetail": {}, "id": lid},
            {"status": "Extracting", "progressDetail": {"current": size, "total": size}, "id": lid},
            {"status": "Pull complete", "progressDetail": {}, "id": lid}]
    return out


def pull_script(tag: str = "v1") -> list:
    """Two layers of 300 and 100 MiB and one that is already here, interleaved as dockerd does."""
    a, b = layer_events("aaaaaaaaaaaa", 300 * MIB), layer_events("bbbbbbbbbbbb", 100 * MIB)
    script: list = [{"status": "Pulling from acme/engine", "id": tag},
                    {"status": "Already exists", "progressDetail": {}, "id": "cccccccccccc"}]
    script += [a[0], b[0]]
    for x, y in zip(a[1:], b[1:]):
        script += [x, y]
    script += [{"status": "Digest: sha256:" + "d" * 64}, {"status": f"Status: Downloaded newer image for acme/engine:{tag}"}]
    return script


class FakeEngineAPI:
    """Serves `POST /images/create` on a Unix socket. `script` items are an event (dict, sent as a JSON
    line), ("sleep", seconds), ("raw", bytes) or ("status", http status, body) as the whole answer."""

    def __init__(self, path: Path, script: Optional[list] = None, *, delay: float = 0.0,
                 on_done: Optional[Callable[[dict], None]] = None):
        self.path = Path(path)
        self.script = list(script if script is not None else pull_script())
        self.delay = delay
        self.on_done = on_done
        self.requests: list[dict] = []
        self.disconnected = threading.Event()
        self._closing = threading.Event()
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def address_string(self):
                return "unix"

            def _chunk(self, data: bytes) -> bool:
                try:
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                    return True
                except OSError:
                    api.disconnected.set()
                    return False

            def do_POST(self):
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                api.requests.append({"path": url.path, "query": query})
                if url.path != "/images/create":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                first = api.script[0] if api.script else None
                if isinstance(first, tuple) and first[0] == "status":
                    body = json.dumps({"message": first[2]}).encode()
                    self.send_response(first[1])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for item in api.script:
                    if isinstance(item, tuple) and item[0] == "sleep":
                        if api._closing.wait(item[1]):
                            return
                        continue
                    data = item[1] if isinstance(item, tuple) else json.dumps(item).encode() + b"\r\n"
                    if not self._chunk(data):
                        return
                    if api.delay:
                        time.sleep(api.delay)
                if api.on_done is not None:
                    api.on_done(query)
                self._chunk(b"")

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(str(self.path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self._closing.set()
        self.server.shutdown()
        self.server.server_close()
        self.path.unlink(missing_ok=True)
