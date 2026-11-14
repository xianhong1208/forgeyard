#!/usr/bin/env python3
"""1.3 acceptance on real machines: the generic passthrough at /x/<name>, a service workload's own HTTP API
behind the gateway.

Through Forgeyard's API only (an admin session), on --machine, for each kind of --only:

  process    a tiny HTTP app (python3, standard library only) run as a process workload, listening on
             ${BIND_ADDRESS}:${PORT}; any machine with the process runtime
  container  the same app in a container (CONTAINER_IMAGE), when the machine has a container engine

each exposed at /x/accept-x-<kind> and checked:

  1. it becomes ready behind the agent's key guard (the workload's page says so);
  2. keys: a new key (Workloads: none) gets 403 workloads_not_allowed; one listing another workload 404;
     one listing it 200;
  3. the request: the prefix stripped, the path and query as sent, no Authorization or Cookie reaching the
     app (not the client's key, not the internal one), fresh X-Forwarded-*, X-Forwarded-Prefix, the same
     X-Request-Id both ways;
  4. the answer: the forced CSP sandbox, nosniff, DENY, no-store, CORP, no-referrer; the app's Set-Cookie and
     Access-Control-* gone; X-Forgeyard-Target; a redirect rewritten under /x/<name>, not followed;
  5. refused paths on the wire (../, %2e%2e, %2f): 400 invalid_path, never reaching the app;
  6. streaming: SSE events arrive while the app is still sending; a client that hangs up mid-stream leaves
     nothing in flight (the next request is answered at once);
  7. limits: a body over max_body_mb (413), rpm on a key (429 with Retry-After), concurrency 1 (429
     workload_busy while one request is in flight), a read timeout (504);
  8. the replica's published port answers 401 without the gateway's internal key, also with an API key;
  9. usage: the run's key shows requests, bytes in and out and busy time under x:<name>;
 10. turning Expose off: 404 at once.

then removes what it created (workloads, keys) and prints a report (also JSON with --out). Everything is
named accept-x-*, and leftovers of an earlier run are removed first, so it can be run again.

    FORGEYARD_PASSWORD=… python3 scripts/accept_x.py --server https://<server>:7860 --cacert forgeyard-ca.pem \\
        --user <admin> --machine <machine> --only process,container --out accept-x.json

Standard library only: nothing to install.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept_m3  # noqa: E402
from accept_m3 import Client, Fail, log, machine  # noqa: E402

PREFIX = "accept-x-"
KINDS = ("process", "container")
CONTAINER_IMAGE = "python:3.13-slim"
FORCED = {"content-security-policy": "sandbox; default-src 'none'; frame-ancestors 'none'",
          "x-content-type-options": "nosniff", "x-frame-options": "DENY", "cache-control": "no-store",
          "cross-origin-resource-policy": "same-origin", "referrer-policy": "no-referrer"}

# The app behind /x/: what reached it (/echo…), a redirect, an SSE stream, a slow answer, an upload.
APP = r'''
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, obj, extra=()):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)
    def _chunk(self, d):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(d), d))
        self.wfile.flush()
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health":
            return self._send(200, {"ok": True})
        if p == "/redirect":
            return self._send(302, {}, [("Location", "/landing")])
        if p == "/slow":
            time.sleep(float(self.path.split("s=")[1]) if "s=" in self.path else 3)
            return self._send(200, {"slow": True})
        if p in ("/sse", "/forever"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            n = 4 if p == "/sse" else 3000
            try:
                for i in range(n):
                    self._chunk(f"data: {i} {time.time():.3f}\n\n".encode())
                    time.sleep(0.5 if p == "/sse" else 0.05)
                self._chunk(b"")
            except OSError:
                pass
            return
        self._send(200, {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                         "uid": os.getuid()},
                   [("Set-Cookie", "app=1; Path=/"), ("Access-Control-Allow-Origin", "*"), ("X-Frame-Options", "ALLOWALL")])
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        left, got = n, 0
        while left:
            b = self.rfile.read(min(left, 1 << 16))
            if not b:
                break
            got += len(b)
            left -= len(b)
        self._send(200, {"len": got, "auth": self.headers.get("Authorization")})
ThreadingHTTPServer((sys.argv[1], int(os.environ["PORT"])), H).serve_forever()
'''


# ------------------------------------------------------------------ HTTP on the wire
def _conn(base: str, timeout: float) -> http.client.HTTPConnection:
    u = urllib.parse.urlsplit(base)
    if u.scheme == "https":
        ctx = accept_m3.TLS or ssl.create_default_context()
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def request(base: str, method: str, path: str, *, key: Optional[str] = None, body: Optional[bytes] = None,
            headers: Optional[dict] = None, timeout: float = 60.0) -> tuple[int, dict, bytes]:
    """One request with the path sent exactly as given (http.client does not normalise it)."""
    c = _conn(base, timeout)
    try:
        h = dict(headers or {})
        if key:
            h["Authorization"] = f"Bearer {key}"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        c.close()


def headers_list(base: str, path: str, key: str) -> list[tuple[str, str]]:
    c = _conn(base, 30)
    try:
        c.request("GET", path, headers={"Authorization": f"Bearer {key}"})
        r = c.getresponse()
        r.read()
        return [(k.lower(), v) for k, v in r.getheaders()]
    finally:
        c.close()


def body_json(data: bytes) -> dict:
    try:
        out = json.loads(data or b"null")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def code(data: bytes) -> Optional[str]:
    return (body_json(data).get("error") or {}).get("code")


# ------------------------------------------------------------------ setup
def spec_for(kind: str, name: str, m: dict) -> dict:
    if kind == "process":
        runtime = {"type": "process", "command": "python3", "args": ["-I", "-c", APP, "${BIND_ADDRESS}"]}
        ports = [{"name": "http"}]
    else:
        runtime = {"type": "container", "image": CONTAINER_IMAGE, "command": ["python3"], "args": ["-I", "-c", APP, "0.0.0.0"]}
        ports = [{"name": "http", "port": 8000}]
    return {"name": name, "kind": "service", "machine_id": m["id"], "runtime": runtime, "ports": ports,
            "health": {"type": "http", "path": "/health", "interval": 2, "start_period": 300},
            "gateway": {"expose": True}}


def wait_running(c: Client, wid: int, timeout: float) -> dict:
    deadline, last = time.time() + timeout, ""
    while time.time() < deadline:
        w = c.ok("GET", f"/api/workloads/{wid}")["workload"]
        text = "; ".join(f"#{r['ordinal']} {r['state']}{': ' + r['reason'] if r.get('reason') else ''}" for r in w["replica_list"])
        if text != last:
            log(f"  {w['name']}: {text}")
            last = text
        if w["status"] == "running":
            return w
        if w["status"] == "failed":
            raise Fail(f"{w['name']} failed: {text}")
        time.sleep(2)
    raise Fail(f"{wid} not running after {timeout:.0f} s: {last}")


def cleanup(c: Client) -> None:
    """Workloads and keys named accept-x-* (also an earlier run's)."""
    for w in c.ok("GET", "/api/workloads")["workloads"]:
        if w["name"].startswith(PREFIX):
            c.call("DELETE", f"/api/workloads/{w['id']}")
    for k in c.ok("GET", "/api/keys")["keys"]:
        if k["name"].startswith(PREFIX) and k["status"] == "active":
            c.call("DELETE", f"/api/keys/{k['id']}")


# ------------------------------------------------------------------ the checks
def exercise(c: Client, base: str, w: dict, check: Callable, new_key: Callable[[str, dict], str],
             label: str = "") -> dict:
    """Every check on one exposed workload `w` (running). `new_key(what, body)` makes an API key and returns it;
    `check(name, ok, detail="")` records a result. Returns what it measured."""
    name = w["name"]
    x = f"/x/{name}"
    t = f"{label}{name}"
    out: dict = {}
    gw = w.get("gateway") or {}
    check(f"{t}: exposed at {x} behind the agent's key guard", gw.get("exposed") and gw.get("guarded") == [0]
          and not gw.get("unguarded"), f"guarded {gw.get('guarded')}, unguarded {gw.get('unguarded')}")

    none_key = new_key("none", {})
    key = new_key("listed", {"workloads": "listed", "workload_list": [name]})
    s, _, data = request(base, "GET", f"{x}/echo", key=none_key)
    check(f"{t}: a key with Workloads none gets 403 workloads_not_allowed", s == 403 and code(data) == "workloads_not_allowed",
          f"{s} {code(data)}")
    s, _, data = request(base, "GET", "/x/accept-x-nothing-here/echo", key=key)
    check(f"{t}: an unknown or unlisted workload is 404 workload_not_found", s == 404 and code(data) == "workload_not_found",
          f"{s} {code(data)}")

    # 3. the request
    s, h, data = request(base, "GET", f"{x}/echo/a/b?q=1&r=%20x", key=key,
                         headers={"Cookie": "fy_session=accept", "X-Forwarded-For": "203.0.113.7", "X-Real-IP": "203.0.113.7",
                                  "X-App": "kept"})
    seen = body_json(data)
    hdrs = seen.get("headers") or {}
    check(f"{t}: the prefix is stripped, path and query passed as sent", s == 200 and seen.get("path") == "/echo/a/b?q=1&r=%20x",
          f"{s}, the app saw {seen.get('path')!r}")
    check(f"{t}: no Authorization or Cookie reaches the app", s == 200 and "authorization" not in hdrs and "cookie" not in hdrs,
          f"app headers: {sorted(hdrs)}")
    check(f"{t}: forwarded headers are fresh, X-Forwarded-Prefix and X-Request-Id set",
          "203.0.113.7" not in json.dumps(hdrs) and hdrs.get("x-forwarded-prefix") == x and hdrs.get("x-app") == "kept"
          and hdrs.get("x-request-id") == h.get("x-request-id"),
          f"x-forwarded-for {hdrs.get('x-forwarded-for')}, prefix {hdrs.get('x-forwarded-prefix')}, request id "
          f"{h.get('x-request-id')}")
    if name.endswith("-process"):
        # The agent runs process workloads as its unprivileged workload account; a container runs as its
        # image's user (python:3.13-slim is root inside its own container), so that is only reported.
        check(f"{t}: the program runs as the workload account", seen.get("uid") not in (None, 0), f"uid {seen.get('uid')}")
    else:
        check(f"{t}: the program's user (the image's own)", True, f"uid {seen.get('uid')}", info=True)

    # 4. the answer
    forced = {k: h.get(k) for k in FORCED}
    check(f"{t}: the sandbox and security headers are forced", forced == FORCED,
          ", ".join(f"{k}: {v}" for k, v in forced.items() if v != FORCED[k]) or "all six")
    hl = headers_list(base, f"{x}/echo", key)
    names = [k for k, _ in hl]
    check(f"{t}: the app's Set-Cookie and Access-Control-* are dropped, one X-Frame-Options",
          "set-cookie" not in names and not any(n.startswith("access-control-") for n in names)
          and names.count("x-frame-options") == 1, f"{len(names)} headers")
    check(f"{t}: X-Forgeyard-Target names the replica", h.get("x-forgeyard-target") == f"workload:{name}#0",
          h.get("x-forgeyard-target", "missing"))
    s, h2, _ = request(base, "GET", f"{x}/redirect", key=key)
    check(f"{t}: a redirect is passed on under the prefix, not followed", s == 302 and h2.get("location") == f"{x}/landing",
          f"{s} Location {h2.get('location')}")

    # 5. refused paths on the wire
    for raw in (f"{x}/../api/users", f"{x}/%2e%2e/%2e%2e/api/users", f"{x}/a%2f..%2fb"):
        s, _, data = request(base, "GET", raw, key=key)
        check(f"{t}: {raw[len(x):]} is refused (400 invalid_path)", s == 400 and code(data) == "invalid_path", f"{s} {code(data)}")

    # 6. streaming and a client that goes away
    times, t0 = [], time.monotonic()
    c2 = _conn(base, 30)
    try:
        c2.request("GET", f"{x}/sse", headers={"Authorization": f"Bearer {key}"})
        r = c2.getresponse()
        buf = b""
        while len(times) < 4:
            chunk = r.read1(65536) if hasattr(r, "read1") else r.read(1)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                _, buf = buf.split(b"\n\n", 1)
                times.append(time.monotonic() - t0)
    finally:
        c2.close()
    spread = times[-1] - times[0] if len(times) >= 2 else 0
    check(f"{t}: SSE events arrive as they are sent", len(times) == 4 and spread > 1.0,
          f"{len(times)} events over {spread:.1f} s (the app sends one every 0.5 s)")
    c3 = _conn(base, 30)
    c3.request("GET", f"{x}/forever", headers={"Authorization": f"Bearer {key}"})
    r3 = c3.getresponse()
    first = r3.read1(256) if hasattr(r3, "read1") else r3.read(16)
    c3.sock.shutdown(socket.SHUT_RDWR)
    c3.close()
    time.sleep(1.0)
    s, _, _ = request(base, "GET", f"{x}/echo", key=key, timeout=10)
    check(f"{t}: hanging up mid-stream leaves the replica free", bool(first) and s == 200, f"next request {s}")

    # 7. limits
    wid = w["id"]
    c.ok("PUT", f"/api/workloads/{wid}/gateway", {"expose": True, "max_body_mb": 1, "read_timeout": 2, "concurrency": 1})
    s, _, data = request(base, "POST", f"{x}/upload", key=key, body=b"x" * (1024 * 1024))
    check(f"{t}: a body at max_body_mb passes", s == 200 and body_json(data).get("len") == 1024 * 1024
          and body_json(data).get("auth") is None, f"{s} {body_json(data).get('len')}")
    s, _, data = request(base, "POST", f"{x}/upload", key=key, body=b"x" * (1024 * 1024 + 1))
    check(f"{t}: a body over max_body_mb is 413 request_too_large", s == 413 and code(data) == "request_too_large", f"{s} {code(data)}")
    t0 = time.monotonic()
    s, _, data = request(base, "GET", f"{x}/slow?s=5", key=key)
    check(f"{t}: slower than read_timeout is 504 upstream_timeout", s == 504 and code(data) == "upstream_timeout"
          and time.monotonic() - t0 < 4.5, f"{s} {code(data)} after {time.monotonic() - t0:.1f} s")
    c.ok("PUT", f"/api/workloads/{wid}/gateway", {"expose": True, "concurrency": 1})
    holder: dict = {}

    def slow():
        holder["r"] = request(base, "GET", f"{x}/slow?s=3", key=key)

    th = threading.Thread(target=slow)
    th.start()
    time.sleep(1.0)
    s, h3, data = request(base, "GET", f"{x}/echo", key=key)
    th.join()
    check(f"{t}: concurrency 1: a second request while one runs is 429 workload_busy with Retry-After",
          s == 429 and code(data) == "workload_busy" and h3.get("retry-after") == "1" and holder["r"][0] == 200,
          f"{s} {code(data)}, the first {holder['r'][0]}")
    c.ok("PUT", f"/api/workloads/{wid}/gateway", {"expose": True})
    rpm_key = new_key("rpm", {"workloads": "listed", "workload_list": [name], "rpm": 3})
    codes = [request(base, "GET", f"{x}/echo", key=rpm_key)[0] for _ in range(3)]
    s, h4, data = request(base, "GET", f"{x}/echo", key=rpm_key)
    check(f"{t}: the key's rpm applies (4th in a minute: 429 with Retry-After)", codes == [200] * 3 and s == 429
          and code(data) == "rate_limit_exceeded" and int(h4.get("retry-after") or 0) >= 1, f"{codes} then {s}")

    # 8. the published port itself
    rep = c.ok("GET", f"/api/workloads/{wid}")["workload"]["replica_list"][0]
    port = (rep.get("ports") or [{}])[0]
    direct = f"http://{port.get('address')}:{port.get('host_port')}"
    try:
        s1, _, _ = request(direct, "GET", "/echo", timeout=10)
        s2, _, _ = request(direct, "GET", "/echo", key=key, timeout=10)
    except OSError as e:
        s1 = s2 = f"unreachable ({e})"
    check(f"{t}: the replica's published port answers 401 without the internal key, also with an API key",
          s1 == 401 and s2 == 401, f"{direct}: {s1}, {s2}")

    # 9. usage
    time.sleep(1)
    rows = c.ok("GET", "/api/usage?days=1&group=model")["rows"]
    row = next((r for r in rows if r["id"] == f"x:{name}"), None)
    out["usage"] = row
    check(f"{t}: usage under x:{name}: requests, bytes in and out, busy time",
          row is not None and row["requests"] >= 15 and row["bytes_in"] >= 1024 * 1024 and row["bytes_out"] > 0
          and row["busy_ms"] > 0, json.dumps({k: row.get(k) for k in ("requests", "errors", "limited", "bytes_in", "bytes_out",
                                                                       "busy_ms", "latency_p50_ms")}) if row else "no row")

    # 10. turning it off
    c.ok("PUT", f"/api/workloads/{wid}/gateway", {"expose": False})
    s, _, data = request(base, "GET", f"{x}/echo", key=key)
    check(f"{t}: Expose off: 404 at once", s == 404 and code(data) == "workload_not_found", f"{s} {code(data)}")
    return out


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860")
    ap.add_argument("--cacert", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--machine", required=True, help="the machine to run the workloads on")
    ap.add_argument("--only", default="process,container", help=f"comma-separated, of {', '.join(KINDS)}")
    ap.add_argument("--timeout", type=float, default=900, help="seconds a workload may take to be ready (an image pull)")
    ap.add_argument("--keep", action="store_true", help="leave the workloads running afterwards")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args()
    kinds = [k for k in args.only.split(",") if k]
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise Fail(f"unknown kind(s) {unknown}; known: {', '.join(KINDS)}")
    accept_m3.use_cacert(args.cacert)
    password = os.environ.get("FORGEYARD_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    c = Client(args.server)
    c.ok("POST", "/api/auth/login", {"username": args.user, "password": password})
    m = machine(c, args.machine)
    if m["status"] != "online":
        raise Fail(f"{m['name']} is {m['status']}")
    if "workload_guard" not in (m.get("features") or []):
        raise Fail(f"{m['name']}'s agent does not list workload_guard: update it to this Forgeyard version")
    report: dict = {"server": args.server, "machine": m["name"], "checks": [], "workloads": {}}

    def check(name: str, ok, detail="", info: bool = False, skip: bool = False) -> None:
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail, "info": info or skip, "skip": skip})
        tag = "SKIP" if skip else "INFO" if info else "PASS" if ok else "FAIL"
        log(f"{tag}  {name}" + (f"  ({detail})" if detail else ""))

    def new_key(what: str, body: dict) -> str:
        return c.ok("POST", "/api/keys", {"name": f"{PREFIX}{what}-{int(time.time())}", **body})["key"]

    cleanup(c)
    info = m.get("runtime_info") or {}
    try:
        for kind in kinds:
            if not (info.get(kind) or {}).get("ok"):
                check(f"{kind}: exposed workload", False, f"no {kind} runtime on {m['name']}", skip=True)
                continue
            name = f"{PREFIX}{kind}"
            w = c.ok("POST", "/api/workloads", spec_for(kind, name, m))["workload"]
            t0 = time.monotonic()
            try:
                w = wait_running(c, w["id"], args.timeout)
            except Fail as e:
                check(f"{name}: ready", False, str(e))
                continue
            check(f"{name}: ready on {m['name']}", True, f"after {time.monotonic() - t0:.0f} s")
            report["workloads"][name] = exercise(c, args.server, w, check, new_key)
    finally:
        if not args.keep:
            cleanup(c)
    failed = [x for x in report["checks"] if not x["ok"] and not x["info"]]
    passed = sum(1 for x in report["checks"] if x["ok"] and not x["info"])
    skipped = sum(1 for x in report["checks"] if x["skip"])
    infos = sum(1 for x in report["checks"] if x["info"] and not x["skip"])
    log(f"{passed} passed, {len(failed)} failed, {skipped} skipped" + (f", {infos} for information" if infos else ""))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        log(f"STOP  {e}")
        sys.exit(2)
