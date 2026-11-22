"""TLS from the gateway to every replica (1.4.5, issue #163): the server's side.

Since 1.0 the gateway reached a replica's key guard over plain HTTP, so the internal key, every prompt and every
answer crossed the network in clear between the server and the machine. An agent that lists "guard_tls"
(machines/protocol.py) now serves its key guards with TLS, and the gateway dials it at https://.

**Certificates.** The agent makes its key itself (EC P-256; the key never leaves the machine) and sends a CSR over
its authenticated WebSocket. The server takes only the CSR's public key and signs a certificate with Forgeyard's
CA (data_dir/tls/ca.key, made here if the server runs with its own certificate or TLS=off and has none):

    subject / SAN   DNS:m<machine id>-<16 hex>.replica.forgeyard.invalid   (`binding_name`), nothing else
    validity        VALIDITY_DAYS (30); the agent asks for a new one when fewer than RENEW_BEFORE_DAYS (10) are
                    left, at every connection and every hour
    usage           serverAuth, digitalSignature; not a CA

The binding name ties the certificate to one machine: <16 hex> comes from that machine's credential hash, so a
machine joined again (a new row that may get the same id in SQLite) has another name, and no other machine can get
a certificate with it (the server names it from the authenticated connection, never from the CSR). The replica's
address is deliberately **not** in the certificate: the agent reports it, and this CA is the one agents and browsers
trust for the server, so a certificate naming an address an agent chose could impersonate the server at that address.
For the same reason nothing in the certificate comes from the agent. The address may change freely: the gateway
verifies the machine, not the address.

**The gateway** (`ReplicaTransport`, used by forgeyard/gateway/proxy.py's one HTTP client, and `request_sync` for
the console's Try it) trusts only Forgeyard's CA (never the system store) and checks the binding name as the host
name (SNI and hostname check: the `sni_hostname` request extension), so machine A's certificate is refused when
dialing machine B. A request to an https:// replica without a binding name is refused before it connects. One pool
of connections per machine, so a connection made for one machine never carries another's request.

**Plain HTTP** only for agents that do not list "guard_tls" (older than 1.4.5): the setup advisor and the machine's
page say "traffic to this machine is not encrypted: update the agent". A TLS-capable agent is never dialled over
plain HTTP: while its certificate is missing or expired its replicas cannot be reached (the gateway tries the next
replica, as for any connection that fails), and the advisor reports an error.

Nothing new is stored but `nodes.runtime_info["tls"]` ({serial, not_after, name, ca}: what the agent serves, from
its hello, or what the server issued since) and `["tls_error"]` (why the last request was refused).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import ssl
import threading
from pathlib import Path
from typing import Optional

import httpx

from forgeyard.config import Settings, get_settings
from forgeyard.security import tls

log = logging.getLogger("forgeyard")

BINDING_SUFFIX = ".replica.forgeyard.invalid"   # RFC 2606: .invalid never resolves, so the name means one thing
VALIDITY_DAYS = 30
RENEW_BEFORE_DAYS = 10      # the agent asks for a new certificate with fewer days than this left
ERROR_BEFORE_DAYS = 3       # fewer left: renewal keeps failing (an advisor error)
MISSING_GRACE = dt.timedelta(minutes=2)  # a TLS agent that just connected has this long to get its first one
ISSUE_EVERY = 30.0          # seconds between two certificates for one connection
MAX_CSR = 8 * 1024


class IssueError(Exception):
    """A certificate request that is refused (code `refused`) or cannot be signed now (code `failed`)."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ------------------------------------------------------------------ which machine
def binding_name(node) -> Optional[str]:
    """The name in this machine's replica certificate and the one the gateway checks; None for a machine without a
    credential (revoked), which is never dialled."""
    if node is None or not getattr(node, "credential_hash", None) or not getattr(node, "id", None):
        return None
    tag = hashlib.sha256(b"forgeyard-replica-binding:" + node.credential_hash.encode()).hexdigest()[:16]
    return f"m{int(node.id)}-{tag}{BINDING_SUFFIX}"


def speaks_tls(node) -> bool:
    from forgeyard.machines.protocol import FEATURE_GUARD_TLS

    return node is not None and FEATURE_GUARD_TLS in (node.features or [])


def dial(replica, port: dict) -> Optional[tuple[str, Optional[str]]]:
    """(base URL, binding name or None for plain HTTP) to reach one published port of a replica; None when it
    must not be dialled at all (a TLS agent's machine without a binding name)."""
    host = port["address"]
    host = f"[{host}]" if ":" in host else host
    node = getattr(replica, "node", None)
    if speaks_tls(node):
        name = binding_name(node)
        if name is None:
            return None
        return f"https://{host}:{port['host_port']}", name
    return f"http://{host}:{port['host_port']}", None


def ext(name: Optional[str]) -> dict:
    """The request extensions for a replica: its binding name as the TLS host name (SNI and hostname check)."""
    return {"sni_hostname": name} if name else {}


# ------------------------------------------------------------------ the CA
_ca_lock = threading.Lock()


def ensure_ca(settings: Optional[Settings] = None) -> Path:
    """data_dir/tls with Forgeyard's CA in it: made when there is none (a server with its own certificate or TLS=off
    has none until a TLS agent joins). Never over a CA certificate whose key is missing."""
    s = settings or get_settings()
    d = s.tls_dir
    with _ca_lock:
        key, crt = d / "ca.key", d / "ca.crt"
        if key.is_file() and crt.is_file():
            return d
        if key.exists() or crt.exists():
            raise IssueError("failed", f"{d} has only one of ca.key and ca.crt: restore the other from a backup, or "
                                       "move both away to make a new CA")
        try:
            tls.make_ca(d)
        except OSError as e:
            raise IssueError("failed", f"Forgeyard's CA cannot be made in {d} ({e.strerror or e})") from None
        log.info("Made Forgeyard's certificate authority in %s for the replicas' certificates (fingerprint %s).", d,
                 tls.fingerprint(crt.read_bytes()))
    return d


_fp_cache: dict[Path, tuple[tuple, str]] = {}


def _stamp(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def ca_fingerprint(settings: Optional[Settings] = None) -> Optional[str]:
    """The fingerprint of the CA that signs replica certificates now, or None when there is none yet."""
    path = (settings or get_settings()).tls_dir / "ca.crt"
    stamp = _stamp(path)
    if stamp is None:
        return None
    hit = _fp_cache.get(path)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        fp = tls.fingerprint(path.read_bytes())
    except (OSError, ValueError, tls.TlsError):
        return None
    _fp_cache[path] = (stamp, fp)
    return fp


# ------------------------------------------------------------------ issuing
def issue(node, csr_pem: str, settings: Optional[Settings] = None, *,
          now: Optional[dt.datetime] = None) -> tuple[str, dict]:
    """Sign a certificate for this machine's replica guards: (chain PEM: the leaf, then the CA; what to record)."""
    x509, hashes, serialization, ec, EKU, NameOID = tls._x509()
    from cryptography.hazmat.primitives.asymmetric import rsa

    name = binding_name(node)
    if name is None:
        raise IssueError("refused", "this machine has no credential")
    if not isinstance(csr_pem, str) or not csr_pem.strip() or len(csr_pem) > MAX_CSR:
        raise IssueError("refused", "the request carries no CSR")
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise IssueError("refused", "the CSR cannot be read") from None
    if not csr.is_signature_valid:
        raise IssueError("refused", "the CSR is not signed by its own key")
    public = csr.public_key()
    if isinstance(public, ec.EllipticCurvePublicKey):
        if public.curve.name not in ("secp256r1", "secp384r1"):
            raise IssueError("refused", f"the key's curve ({public.curve.name}) is not P-256 or P-384")
    elif isinstance(public, rsa.RSAPublicKey):
        if public.key_size < 2048:
            raise IssueError("refused", f"the RSA key is too short ({public.key_size} bits)")
    else:
        raise IssueError("refused", "the key is neither EC nor RSA")
    s = settings or get_settings()
    d = ensure_ca(s)
    try:
        ca_key = serialization.load_pem_private_key((d / "ca.key").read_bytes(), password=None)
        ca_pem = (d / "ca.crt").read_bytes()
        ca_cert = x509.load_pem_x509_certificate(ca_pem)
    except (OSError, ValueError, TypeError) as e:
        raise IssueError("failed", f"Forgeyard's CA cannot be read ({e})") from None
    when = now or _now()
    not_after = when + dt.timedelta(days=VALIDITY_DAYS)
    serial = x509.random_serial_number()
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Forgeyard"),
                                     x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_cert.subject).public_key(public)
            .serial_number(serial)
            .not_valid_before(when - dt.timedelta(minutes=5)).not_valid_after(not_after)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([EKU.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    chain = cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    info = {"serial": f"{serial:x}", "not_after": not_after.replace(microsecond=0).isoformat(),
            "name": name, "ca": tls.fingerprint(ca_pem), "issued_at": when.replace(microsecond=0).isoformat()}
    return chain.decode("ascii"), info


def record(node, info: dict) -> None:
    """What the machine's guards serve from now on (until its next hello says otherwise)."""
    ri = {k: v for k, v in (node.runtime_info or {}).items() if k != "tls_error"}
    ri["tls"] = {k: info[k] for k in ("serial", "not_after", "name", "ca") if k in info}
    node.runtime_info = ri


def record_error(node, reason: str, now: Optional[dt.datetime] = None) -> None:
    ri = dict(node.runtime_info or {})
    ri["tls_error"] = {"reason": str(reason)[:300], "at": (now or _now()).replace(microsecond=0).isoformat()}
    node.runtime_info = ri


# ------------------------------------------------------------------ what the console shows
def _when(value) -> Optional[dt.datetime]:
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def status(node, *, now: Optional[dt.datetime] = None, settings: Optional[Settings] = None) -> Optional[dict]:
    """How the gateway reaches this machine's replicas, for its page and the setup advisor:

        plain     an agent before 1.4.5: plain HTTP (update the agent)
        ok        TLS with a certificate for this machine from the current CA, more than ERROR_BEFORE_DAYS left
        pending   TLS, its first certificate not there yet (connected less than MISSING_GRACE ago)
        missing   TLS, no certificate for this machine from the current CA (or the last request was refused)
        expiring  TLS, renewal keeps failing: fewer than ERROR_BEFORE_DAYS left
        expired   TLS, the certificate has expired

    None for a machine that runs no workloads (an inventory-only agent): nothing is dialled there."""
    from forgeyard.machines.protocol import FEATURE_WORKLOADS

    if node is None or FEATURE_WORKLOADS not in (node.features or []) or getattr(node, "status", "") == "revoked":
        return None
    if not speaks_tls(node):
        return {"state": "plain", "not_after": None, "reason": None}
    when = now or _now()
    ri = node.runtime_info or {}
    info = ri.get("tls") if isinstance(ri.get("tls"), dict) else {}
    error = ri.get("tls_error") if isinstance(ri.get("tls_error"), dict) else None
    ca = ca_fingerprint(settings)
    current = bool(info) and info.get("name") == binding_name(node) and (ca is None or info.get("ca") == ca)
    not_after = _when(info.get("not_after")) if current else None
    if not_after is None:
        reason = error.get("reason") if error else None
        connected = node.connected_at
        if connected is not None and connected.tzinfo is None:
            connected = connected.replace(tzinfo=dt.timezone.utc)
        fresh = connected is not None and when - connected < MISSING_GRACE
        if not reason and info and not current:
            reason = "its certificate was made for another machine or by another CA"
        return {"state": "pending" if fresh and not error else "missing", "not_after": None, "reason": reason}
    out = {"not_after": not_after.isoformat(), "reason": error.get("reason") if error else None}
    if not_after <= when:
        return {**out, "state": "expired"}
    if not_after - when < dt.timedelta(days=ERROR_BEFORE_DAYS):
        return {**out, "state": "expiring"}
    return {**out, "state": "ok"}


# ------------------------------------------------------------------ the gateway's side
_ctx_lock = threading.Lock()
_ctx: dict[Path, tuple[tuple, ssl.SSLContext]] = {}


def client_context(settings: Optional[Settings] = None) -> tuple[ssl.SSLContext, tuple]:
    """(an SSLContext trusting only Forgeyard's CA, with host name checks; the CA file's stamp). Checking needs only
    ca.crt: a server whose ca.key is missing cannot sign new certificates, but still verifies the ones out there."""
    s = settings or get_settings()
    path = s.tls_dir / "ca.crt"
    if not path.is_file():
        path = ensure_ca(s) / "ca.crt"
    stamp = _stamp(path) or ()
    with _ctx_lock:
        hit = _ctx.get(path)
        if hit is not None and hit[0] == stamp:
            return hit[1], stamp
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(path))  # no system store
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        _ctx[path] = (stamp, ctx)
        return ctx, stamp


def _binding_of(request: httpx.Request) -> str:
    name = request.extensions.get("sni_hostname")
    if not isinstance(name, str) or len(name) <= len(BINDING_SUFFIX) or not name.endswith(BINDING_SUFFIX):
        # Never fall back to checking the address: the certificate names a machine, not an address.
        raise httpx.ConnectError("A replica is reached over TLS only with its machine's binding name.", request=request)
    return name


class ReplicaTransport(httpx.AsyncBaseTransport):
    """Plain HTTP for agents before 1.4.5; TLS with a machine binding for the rest, one pool per machine."""

    def __init__(self, *, limits: httpx.Limits):
        self.limits = limits
        self._plain = httpx.AsyncHTTPTransport(limits=limits, trust_env=False)
        self._tls: dict[str, tuple[tuple, httpx.AsyncHTTPTransport]] = {}
        self._old: list[httpx.AsyncHTTPTransport] = []

    def _for(self, name: str) -> httpx.AsyncHTTPTransport:
        try:
            ctx, stamp = client_context()
        except IssueError as e:
            raise httpx.ConnectError(f"Forgeyard's CA cannot be used: {e.reason}") from None
        hit = self._tls.get(name)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        if hit is not None:
            self._old.append(hit[1])  # the CA changed: new connections only; closed with the client
        t = httpx.AsyncHTTPTransport(verify=ctx, limits=self.limits, trust_env=False)
        self._tls[name] = (stamp, t)
        return t

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return await self._plain.handle_async_request(request)
        if request.url.scheme != "https":
            raise httpx.UnsupportedProtocol(f"Replicas are reached over http or https, not {request.url.scheme}.",
                                            request=request)
        return await self._for(_binding_of(request)).handle_async_request(request)

    async def aclose(self) -> None:
        for t in [self._plain, *(t for _, t in self._tls.values()), *self._old]:
            try:
                await t.aclose()
            except Exception:  # closing never fails the shutdown
                log.debug("Closing a replica connection pool failed", exc_info=True)
        self._tls.clear()
        self._old.clear()


def request_sync(method: str, url: str, name: Optional[str], *, headers: dict, content: Optional[bytes] = None,
                 timeout: float = 30.0, limit: int = 2 * 1024**2, error_limit: int = 64 * 1024) -> tuple[int, bytes]:
    """One request to a replica from a worker thread (the console's Try it), the same way the gateway makes it:
    plain HTTP for an agent before 1.4.5, otherwise TLS with the machine binding. (status, the first `limit` bytes of
    the answer, `error_limit` for an error status). Raises httpx.HTTPError when it cannot be made."""
    if url.startswith("https://"):
        probe = httpx.Request(method, url, extensions=ext(name))
        _binding_of(probe)
        try:
            ctx, _ = client_context()
        except IssueError as e:
            raise httpx.ConnectError(f"Forgeyard's CA cannot be used: {e.reason}") from None
        transport = httpx.HTTPTransport(verify=ctx, trust_env=False, retries=0)
    else:
        transport = httpx.HTTPTransport(trust_env=False, retries=0)
    with httpx.Client(transport=transport, timeout=timeout, trust_env=False, follow_redirects=False) as c:
        with c.stream(method, url, headers=headers, content=content, extensions=ext(name)) as r:
            most = error_limit if r.status_code >= 400 else limit
            buf = bytearray()
            for chunk in r.iter_raw():
                buf += chunk
                if len(buf) >= most:
                    break
            return r.status_code, bytes(buf[:most])
