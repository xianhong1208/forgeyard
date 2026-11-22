"""Calling a replica's own port directly, the way the gateway reaches it (1.4.5, #163): for the acceptance scripts.

The acceptance scripts call a replica's published port around the gateway, to prove the key guard refuses a call
without the gateway's internal key (401 replica_key_required, also with a client's API key) and that vLLM's open
paths are closed. Since 1.4.5 an agent that lists "guard_tls" serves those ports over **TLS only**, with a
certificate Forgeyard's CA signed for one machine, whose only name is the machine's binding name
`m<id>-<hash>.replica.forgeyard.invalid` (/api/machines -> runtime_info.tls.name). Plain HTTP to such a port is
closed without an answer.

`Replicas` does what the gateway does: for a TLS machine it connects to the endpoint's address and port, sends the
binding name as SNI and checks the certificate against Forgeyard's CA with the binding name as the host name (never
the system's trust store); for an older agent's machine it speaks plain HTTP, as before. `transport_checks` adds the
checks a TLS port must pass (plain HTTP refused, the certificate verifies with the binding name, another machine's
name refused) and, for a plain machine, an information line that its traffic is not encrypted.

Forgeyard's CA, in this order: the scripts' --ca / --cacert (the server's CA when it uses Forgeyard's own), the file
named by FORGEYARD_REPLICA_CA (data/tls/ca.crt, for a server with its own certificate or TLS=off), else what the
server serves at /api/tls/ca.pem. Without any, the direct calls still run over TLS without verifying the
certificate and the certificate checks are skipped (with that reason).
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

BINDING_SUFFIX = ".replica.forgeyard.invalid"
INVENTED = "m0-0000000000000000" + BINDING_SUFFIX   # a binding name no machine has (ids start at 1)
ENV_CA = "FORGEYARD_REPLICA_CA"
HOSTNAME_MISMATCH = 62   # X509_V_ERR_HOSTNAME_MISMATCH: the chain verified, the name is not in the certificate

# Outcomes of transport_checks.
PASS, FAIL, SKIP, INFO = "pass", "fail", "skip", "info"


@dataclass(frozen=True)
class Port:
    """One published port of a replica: where it listens, its machine and, for a TLS machine, the binding name."""

    host: str
    port: int
    machine: Optional[str]
    encrypted: bool
    tls_name: Optional[str] = None

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{'https' if self.encrypted else 'http'}://{host}:{self.port}"


class Answer(NamedTuple):
    """status 0 when there was no HTTP answer; `failure` then says why: "unreachable" (could not connect: another
    network, a stopped replica), "closed" (connected, closed without an answer) or "tls" (the handshake or the
    certificate check failed)."""

    status: int
    headers: dict
    body: bytes
    error: str = ""
    failure: str = ""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace") if self.body else self.error


class _TLSConnection(http.client.HTTPConnection):
    """HTTP over TLS to an address, with another name for SNI and the host name check (the binding name)."""

    def __init__(self, host: str, port: int, *, context: ssl.SSLContext, server_hostname: Optional[str],
                 timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._context = context
        self._server_hostname = server_hostname

    def connect(self) -> None:
        super().connect()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_hostname)


def context_of(cadata: list) -> Optional[ssl.SSLContext]:
    """A client context that trusts only these CA certificates (PEM text or DER bytes), as the gateway's does."""
    if not cadata:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # check_hostname and CERT_REQUIRED; no system store
    for item in cadata:
        ctx.load_verify_locations(cadata=item)
    return ctx


def _unverified() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def find_ca(server: str, tls: Optional[ssl.SSLContext]) -> tuple[Optional[ssl.SSLContext], str]:
    """(context trusting only Forgeyard's CA or None, where it came from) for the replica ports."""
    cadata: list = []
    where = []
    if tls is not None:
        given = tls.get_ca_certs(binary_form=True)
        if given:
            cadata += list(given)
            where.append("--ca")
    path = os.environ.get(ENV_CA, "").strip()
    if path:
        with open(path, encoding="utf-8") as f:
            cadata.append(f.read())
        where.append(ENV_CA)
    if not cadata and server.startswith("https://"):
        try:
            req = urllib.request.Request(server.rstrip("/") + "/api/tls/ca.pem")
            with urllib.request.urlopen(req, timeout=15, context=tls) as r:
                pem = r.read().decode("ascii", "replace")
            if "BEGIN CERTIFICATE" in pem:
                cadata.append(pem)
                where.append("/api/tls/ca.pem")
        except (OSError, ValueError):
            pass
    return context_of(cadata), " + ".join(where)


def binding_names(machines: list[dict]) -> dict[str, Optional[str]]:
    """Machine name -> its binding name, for the machines whose agent serves TLS (replica_tls state not "plain");
    None when such a machine has no certificate recorded yet. Machines with plain HTTP are not in it."""
    out: dict[str, Optional[str]] = {}
    for m in machines:
        state = (m.get("replica_tls") or {}).get("state")
        if state is None and "guard_tls" in (m.get("features") or []):
            state = "unknown"
        if state in (None, "plain"):
            continue
        info = (m.get("runtime_info") or {}).get("tls")
        name = info.get("name") if isinstance(info, dict) else None
        out[m["name"]] = name if isinstance(name, str) and name.endswith(BINDING_SUFFIX) else None
    return out


class Replicas:
    """How to reach the replica ports of one server's machines."""

    def __init__(self, machines: list[dict], ctx: Optional[ssl.SSLContext], ca_source: str = ""):
        self.names = binding_names(machines)
        self.ctx = ctx
        self.ca_source = ca_source

    @classmethod
    def of(cls, client, server: str, tls: Optional[ssl.SSLContext]) -> "Replicas":
        """From a signed-in client of either script family (its .ok returns the parsed JSON)."""
        ctx, where = find_ca(server, tls)
        return cls(client.ok("GET", "/api/machines")["machines"], ctx, where)

    # -- the ports
    def endpoint(self, e: dict) -> Port:
        """A deployment's endpoint (/api/deployments/{id} -> endpoints[]: url, machine, encrypted)."""
        u = urllib.parse.urlsplit(e["url"])
        encrypted = bool(e.get("encrypted")) or u.scheme == "https"
        machine = e.get("machine")
        return Port(u.hostname or "", u.port or (443 if encrypted else 80), machine, encrypted,
                    self.names.get(machine) if encrypted else None)

    def published(self, machine: Optional[str], port: dict) -> Port:
        """A workload replica's published port (replica_list[].ports[0]: address, host_port) on `machine`."""
        encrypted = machine in self.names
        return Port(str(port.get("address")), int(port.get("host_port")), machine, encrypted,
                    self.names.get(machine) if encrypted else None)

    def other_name(self, p: Port) -> Optional[str]:
        """Another TLS machine's binding name, or None when there is no second one."""
        return next((n for m, n in sorted(self.names.items(), key=lambda x: str(x[0])) if m != p.machine and n), None)

    # -- one request
    def request(self, p: Port, method: str, path: str, body: Optional[bytes] = None,
                headers: Optional[dict] = None, timeout: float = 30) -> Answer:
        if p.encrypted:
            if not p.tls_name:
                return Answer(0, {}, b"", f"{p.machine} serves TLS but has no binding name in /api/machines "
                                          "(runtime_info.tls.name): its certificate is missing", "tls")
            conn = _TLSConnection(p.host, p.port, context=self.ctx or _unverified(), server_hostname=p.tls_name,
                                  timeout=timeout)
        else:
            conn = http.client.HTTPConnection(p.host, p.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=dict(headers or {}))
            r = conn.getresponse()
            data = r.read()
            return Answer(r.status, {k.lower(): v for k, v in r.getheaders()}, data)
        except ssl.SSLError as e:  # SSLCertVerificationError included
            return Answer(0, {}, b"", f"{type(e).__name__}: {getattr(e, 'verify_message', None) or e}", "tls")
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as e:
            return Answer(0, {}, b"", f"{type(e).__name__}: {e}", "closed")
        except (OSError, http.client.HTTPException) as e:
            return Answer(0, {}, b"", f"{type(e).__name__}: {e}", "unreachable")
        finally:
            conn.close()

    def post(self, p: Port, path: str, body: bytes, ctype: str, headers: Optional[dict] = None,
             timeout: float = 30) -> tuple[int, str]:
        """(status, text) like the scripts' raw_post: status 0 and the reason when there was no answer."""
        a = self.request(p, "POST", path, body, {"Content-Type": ctype, **(headers or {})}, timeout)
        return a.status, a.text[:4000]

    # -- what a TLS port must do
    def plain_refused(self, p: Port, timeout: float = 10) -> tuple[Optional[bool], str]:
        """Plain HTTP to the port: (True, how) when no HTTP answer comes back, (False, what came) when one does,
        (None, why) when the port cannot be reached at all."""
        try:
            s = socket.create_connection((p.host, p.port), timeout=timeout)
        except OSError as e:
            return None, f"{type(e).__name__}: {e}"
        try:
            s.sendall(f"GET /v1/models HTTP/1.1\r\nHost: {p.host}:{p.port}\r\nConnection: close\r\n\r\n".encode())
            got = b""
            try:
                while len(got) < 64:
                    chunk = s.recv(64 - len(got))
                    if not chunk:
                        break
                    got += chunk
            except socket.timeout:
                return (not got.startswith(b"HTTP/")), f"no answer in {timeout:.0f} s"
            except OSError as e:
                if not got:
                    return True, f"closed ({type(e).__name__})"
            if got.startswith(b"HTTP/"):
                return False, "answered: " + got.split(b"\r\n", 1)[0].decode("latin-1")
            if got[:1] == b"\x15":
                return True, "closed after a TLS alert, without an HTTP answer"
            return True, "closed without an answer" if not got else f"closed, {len(got)} bytes but no HTTP answer"
        finally:
            s.close()

    def handshake(self, p: Port, name: str, timeout: float = 10) -> tuple[Optional[bool], str, Optional[int]]:
        """A TLS handshake with `name` as SNI and host name, verified against Forgeyard's CA: (True, the certificate's
        names and expiry, None), (False, why it was refused, the OpenSSL verify code or None) or (None, why the port
        cannot be reached, None)."""
        assert self.ctx is not None
        try:
            raw = socket.create_connection((p.host, p.port), timeout=timeout)
        except OSError as e:
            return None, f"{type(e).__name__}: {e}", None
        try:
            with self.ctx.wrap_socket(raw, server_hostname=name) as s:
                cert = s.getpeercert() or {}
                sans = [v for k, v in cert.get("subjectAltName", ()) if k in ("DNS", "IP Address")]
                return True, f"names {', '.join(sans) or '-'}, valid until {cert.get('notAfter', '?')}", None
        except ssl.SSLCertVerificationError as e:
            return False, f"refused: {e.verify_message or e}", e.verify_code
        except (ssl.SSLError, OSError) as e:
            return False, f"refused: {type(e).__name__}: {e}", None
        finally:
            raw.close()

    def _name_refused(self, p: Port, name: str, what: str) -> tuple[str, str, str]:
        """Only a host name mismatch counts: the chain verified, the certificate just does not carry `name`."""
        ok, how, code = self.handshake(p, name)
        if ok is None:
            return what, SKIP, f"not reachable from here: {how}"
        return what, PASS if ok is False and code == HOSTNAME_MISMATCH else FAIL, f"{name}: {how}"

    def transport_checks(self, p: Port, label: str) -> list[tuple[str, str, str]]:
        """(check name, PASS/FAIL/SKIP/INFO, detail) for how the port is reached; give them to `record`. The name
        checks run only once the certificate verified with the machine's own name."""
        if not p.encrypted:
            return [(f"{label}: traffic to {p.machine} is plain HTTP", INFO,
                     "not encrypted: its agent is older than 1.4.5 (no guard_tls); update it")]
        out = []
        ok, how = self.plain_refused(p)
        if ok is None:
            out.append((f"{label}: plain HTTP to the TLS port is refused", SKIP, f"not reachable from here: {how}"))
        else:
            out.append((f"{label}: plain HTTP to the TLS port is refused", PASS if ok else FAIL, how))
        what = f"{label}: the certificate verifies against Forgeyard's CA with {p.machine}'s binding name"
        if self.ctx is None:
            out.append((what, SKIP, f"no CA to verify with: pass --ca (or {ENV_CA}=data/tls/ca.crt)"))
            return out
        if not p.tls_name:
            out.append((what, FAIL, f"{p.machine} serves TLS but /api/machines has no binding name for it"))
            return out
        ok, how, _ = self.handshake(p, p.tls_name)
        out.append((what, SKIP if ok is None else PASS if ok else FAIL,
                    f"{p.tls_name}: {how} (CA from {self.ca_source or '?'})"))
        if not ok:
            return out
        other = self.other_name(p)
        what = f"{label}: another machine's binding name is refused"
        if other is None:
            out.append((what, SKIP, "only one machine serves TLS"))
        else:
            out.append(self._name_refused(p, other, what))
        out.append(self._name_refused(p, INVENTED, f"{label}: a binding name no machine has is refused"))
        return out


def record(results: list[tuple[str, str, str]], check: Callable) -> None:
    """Give transport_checks' results to a script's check(name, ok, detail, info=…, skip=…)."""
    for name, outcome, detail in results:
        if outcome == SKIP:
            check(name, True, detail, skip=True)
        elif outcome == INFO:
            check(name, True, detail, info=True)
        else:
            check(name, outcome == PASS, detail)
