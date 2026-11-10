"""HTTPS out of the box: Forgeyard's own certificate authority and the server certificate.

On first start with TLS on (the default) and no certificate of your own, the server makes:

    data_dir/tls/ca.key      the CA's private key (EC P-256), mode 0600, never leaves this machine
    data_dir/tls/ca.crt      the CA certificate (10 years): what browsers and agents trust, once
    data_dir/tls/server.key  the server's private key, mode 0600
    data_dir/tls/server.crt  the server certificate, signed by the CA, valid 397 days

The server certificate names every address people and agents use: PUBLIC_URL's host, ALLOWED_HOSTS,
TLS_EXTRA_NAMES, the machine's host name (short and fully qualified), its own IP addresses, and
localhost / 127.0.0.1 / ::1. It is made again, signed by the same CA, when it has less than 60 days
left or a name is missing (a new PUBLIC_URL, a new address); a running server checks twice a day
and switches to the new certificate without a restart. Agents and browsers that trust the CA never
notice: they trust the CA, not the server certificate.

With TLS_CERT_FILE / TLS_KEY_FILE the server uses those instead (reloaded when the files change,
e.g. after certbot renews them) and does not touch its own CA. Agents then pin TLS_CA_FILE when it
is set (a company CA), or use the system's trust store (a publicly trusted certificate).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from forgeyard.config import Settings, get_settings

log = logging.getLogger("forgeyard")

CA_DAYS = 3650
SERVER_DAYS = 397          # the most any browser accepts for a server certificate
RENEW_BEFORE_DAYS = 60     # well before expiry: a server that is down for weeks still renews in time
CHECK_EVERY_SECONDS = 12 * 3600
CA_WARN_DAYS = 365


class TlsError(Exception):
    pass


# ------------------------------------------------------------------ fingerprints
def der_of(pem: str | bytes) -> bytes:
    text = pem.decode() if isinstance(pem, bytes) else pem
    return ssl.PEM_cert_to_DER_cert(first_pem(text))


def first_pem(text: str) -> str:
    begin = text.find("-----BEGIN CERTIFICATE-----")
    end = text.find("-----END CERTIFICATE-----", begin)
    if begin < 0 or end < 0:
        raise TlsError("not a PEM certificate")
    return text[begin:end + len("-----END CERTIFICATE-----")] + "\n"


def fingerprint(pem: str | bytes) -> str:
    """'sha256:<64 hex>' of the certificate's DER bytes: what the join command pins."""
    return "sha256:" + hashlib.sha256(der_of(pem)).hexdigest()


def colon_hex(fp: str) -> str:
    """'sha256:ab12…' -> 'AB:12:…', the way certificate dialogs show a SHA-256 fingerprint."""
    h = fp.removeprefix("sha256:").upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


# ------------------------------------------------------------------ files
def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_public(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _tighten(path: Path) -> None:
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        log.warning("%s was readable by others (mode %o); setting it to 0600.", path, mode)
        os.chmod(path, 0o600)


# ------------------------------------------------------------------ names
def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def local_addresses() -> list[str]:
    """This machine's own addresses: the one towards the network, and whatever the host name
    resolves to. Loopback is added separately."""
    found: set[str] = set()
    for family, probe in ((socket.AF_INET, ("192.0.2.1", 9)), (socket.AF_INET6, ("2001:db8::1", 9))):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect(probe)  # UDP: nothing is sent, the kernel only picks the source address
                found.add(s.getsockname()[0])
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0])
    except OSError:
        pass
    out = []
    for a in found:
        a = a.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast):
            out.append(str(ip))
    return sorted(out)


def wanted_names(settings: Optional[Settings] = None) -> tuple[list[str], list[str]]:
    """(DNS names, IP addresses) the server certificate must cover."""
    s = settings or get_settings()
    names: list[str] = ["localhost"]
    ips: list[str] = ["127.0.0.1", "::1"]

    def add(value: str) -> None:
        v = value.strip().lower().strip("[]").rstrip(".")
        if not v or v in ("0.0.0.0", "::"):
            return
        if _is_ip(v):
            v = str(ipaddress.ip_address(v))
            if v not in ips:
                ips.append(v)
        elif v not in names and all(part for part in v.split(".")):
            names.append(v)

    if s.public_url:
        add(urlsplit(s.public_url if "://" in s.public_url else "https://" + s.public_url).hostname or "")
    for h in s.allowed_host_list + s.tls_extra_name_list:
        add(h)
    add(s.host)
    host = socket.gethostname()
    add(host)
    try:
        add(socket.getfqdn(host))
    except OSError:
        pass
    for a in local_addresses():
        add(a)
    return names, ips


# ------------------------------------------------------------------ making certificates
def _x509():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    return x509, hashes, serialization, ec, ExtendedKeyUsageOID, NameOID


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def make_ca(tls_dir: Path, *, days: int = CA_DAYS) -> None:
    x509, hashes, serialization, ec, _, NameOID = _x509()
    key = ec.generate_private_key(ec.SECP256R1())
    tag = os.urandom(3).hex()
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Forgeyard"),
        x509.NameAttribute(NameOID.COMMON_NAME, f"Forgeyard CA {socket.gethostname()[:32]} {tag}"),
    ])
    now = _now()
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write_private(tls_dir / "ca.key", key.private_bytes(serialization.Encoding.PEM,
                                                         serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    _write_public(tls_dir / "ca.crt", cert.public_bytes(serialization.Encoding.PEM))


def _common_name(names: list[str], ips: list[str]) -> str:
    """Only for people reading the certificate: clients check the SANs. The first real name."""
    for n in names + ips:
        if n not in ("localhost", "127.0.0.1", "::1"):
            return n[:64]
    return "localhost"


def make_server_cert(tls_dir: Path, names: list[str], ips: list[str], *, days: int = SERVER_DAYS) -> None:
    x509, hashes, serialization, ec, EKU, NameOID = _x509()
    ca_key = serialization.load_pem_private_key((tls_dir / "ca.key").read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate((tls_dir / "ca.crt").read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    sans = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    now = _now()
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _common_name(names, ips))]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([EKU.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write_private(tls_dir / "server.key", key.private_bytes(serialization.Encoding.PEM,
                                                             serialization.PrivateFormat.PKCS8,
                                                             serialization.NoEncryption()))
    # The chain a client is sent: the server certificate, then the CA (harmless when the client
    # has it, and lets tools show which CA to trust when it does not).
    _write_public(tls_dir / "server.crt", cert.public_bytes(serialization.Encoding.PEM)
                  + ca_cert.public_bytes(serialization.Encoding.PEM))


@dataclass
class CertInfo:
    subject: str
    issuer: str
    not_after: dt.datetime
    names: list[str] = field(default_factory=list)
    ips: list[str] = field(default_factory=list)
    serial: int = 0
    fingerprint: str = ""


def read_cert(pem: bytes) -> CertInfo:
    x509 = _x509()[0]
    cert = x509.load_pem_x509_certificate(pem)
    names: list[str] = []
    ips: list[str] = []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(n) for n in san.get_values_for_type(x509.DNSName)]
        ips = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    return CertInfo(subject=cert.subject.rfc4514_string(), issuer=cert.issuer.rfc4514_string(),
                    not_after=cert.not_valid_after_utc, names=names, ips=ips, serial=cert.serial_number,
                    fingerprint=fingerprint(pem))


def renewal_reason(tls_dir: Path, names: list[str], ips: list[str], *, now: Optional[dt.datetime] = None) -> Optional[str]:
    """Why the server certificate must be made again, or None while it is fine."""
    cert_path, key_path = tls_dir / "server.crt", tls_dir / "server.key"
    if not cert_path.is_file() or not key_path.is_file():
        return "there is none yet"
    try:
        info = read_cert(cert_path.read_bytes())
        ca = read_cert((tls_dir / "ca.crt").read_bytes())
    except (ValueError, TlsError) as e:
        return f"it cannot be read ({e})"
    if info.issuer != ca.subject:
        return "it was not signed by the current CA"
    left = info.not_after - (now or _now())
    if left < dt.timedelta(days=RENEW_BEFORE_DAYS):
        return f"it expires in {max(0, left.days)} days"
    missing = [n for n in names if n not in info.names] + \
              [i for i in ips if str(ipaddress.ip_address(i)) not in {str(ipaddress.ip_address(x)) for x in info.ips}]
    if missing:
        return f"it does not name {', '.join(missing[:5])}"
    return None


# ------------------------------------------------------------------ what the server uses
@dataclass
class Material:
    mode: str                          # forgeyard-ca | own | off
    cert_file: Optional[Path] = None
    key_file: Optional[Path] = None
    # What agents pin and browsers trust: Forgeyard's CA, or TLS_CA_FILE. None: the system store.
    ca_pem: Optional[str] = None


def ensure(settings: Optional[Settings] = None) -> Material:
    """Make (or renew) what the server needs before it listens. Idempotent."""
    s = settings or get_settings()
    if not s.tls_on:
        return Material("off")
    if s.tls_cert_file.strip() or s.tls_key_file.strip():
        if not s.own_certificate:
            raise TlsError("Set both TLS_CERT_FILE and TLS_KEY_FILE, or neither (Forgeyard's own CA is used then).")
        cert, key = Path(s.tls_cert_file), Path(s.tls_key_file)
        for p in (cert, key):
            if not p.is_file():
                raise TlsError(f"{p} does not exist (TLS_CERT_FILE / TLS_KEY_FILE).")
        ca_pem = None
        if s.tls_ca_file.strip():
            ca_path = Path(s.tls_ca_file)
            if not ca_path.is_file():
                raise TlsError(f"{ca_path} does not exist (TLS_CA_FILE).")
            ca_pem = first_pem(ca_path.read_text())
        try:
            ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert, key)
        except (ssl.SSLError, OSError) as e:
            raise TlsError(f"TLS_CERT_FILE and TLS_KEY_FILE cannot be used together: {e}") from None
        return Material("own", cert, key, ca_pem)
    d = s.tls_dir
    if not (d / "ca.key").is_file() or not (d / "ca.crt").is_file():
        if (d / "ca.crt").exists() or (d / "server.crt").exists():
            raise TlsError(f"{d}/ca.key is missing but certificates are there: restore it from a backup, or move "
                           f"{d} away to make a new CA (every agent and browser must then trust the new one).")
        make_ca(d)
        log.info("Made Forgeyard's certificate authority in %s (fingerprint %s).", d,
                 fingerprint((d / "ca.crt").read_bytes()))
    _tighten(d / "ca.key")
    ca = read_cert((d / "ca.crt").read_bytes())
    left = ca.not_after - _now()
    if left < dt.timedelta(days=CA_WARN_DAYS):
        log.warning("Forgeyard's CA expires in %d days (%s). See docs/https-and-trust.md: making a new one means "
                    "every agent and browser trusts it again.", left.days, ca.not_after.date())
    names, ips = wanted_names(s)
    reason = renewal_reason(d, names, ips)
    if reason:
        make_server_cert(d, names, ips)
        log.info("Made a new server certificate (%s): %s", reason, ", ".join(names + ips))
    _tighten(d / "server.key")
    return Material("forgeyard-ca", d / "server.crt", d / "server.key", (d / "ca.crt").read_text())


def server_context(material: Material) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(material.cert_file), str(material.key_file))
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


# ------------------------------------------------------------------ the running server
class Live:
    """What the running server listens with, so it can switch certificates without a restart."""

    def __init__(self):
        self.material: Optional[Material] = None
        self.context: Optional[ssl.SSLContext] = None
        self._stamp: Optional[tuple] = None

    def set(self, material: Material, context: Optional[ssl.SSLContext]) -> None:
        self.material, self.context = material, context
        self._stamp = self._files_stamp()

    def reset(self) -> None:
        self.material, self.context, self._stamp = None, None, None

    def _files_stamp(self) -> Optional[tuple]:
        m = self.material
        if m is None or m.cert_file is None:
            return None
        try:
            return tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in (m.cert_file, m.key_file))
        except OSError:
            return None

    def check(self, settings: Optional[Settings] = None) -> bool:
        """Renew (own CA) or pick up changed files (own certificate). True when the listener
        switched to a new certificate."""
        if self.material is None or self.context is None or self.material.mode == "off":
            return False
        if self.material.mode == "forgeyard-ca":
            self.material = ensure(settings)
        stamp = self._files_stamp()
        if stamp == self._stamp:
            return False
        # New handshakes use the new chain; open connections keep theirs.
        self.context.load_cert_chain(str(self.material.cert_file), str(self.material.key_file))
        self._stamp = stamp
        log.info("Switched to the new server certificate in %s.", self.material.cert_file)
        return True

    async def run_forever(self, every: float = CHECK_EVERY_SECONDS) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await asyncio.to_thread(self.check)
            except Exception:
                log.exception("Checking the server certificate failed")


live = Live()


def mode(settings: Optional[Settings] = None) -> str:
    """forgeyard-ca | own | off, from the settings (the same answer ensure() gives)."""
    s = settings or get_settings()
    if not s.tls_on:
        return "off"
    return "own" if s.own_certificate else "forgeyard-ca"


def trust_anchor_pem(settings: Optional[Settings] = None) -> Optional[str]:
    """The CA certificate agents pin and browsers download: Forgeyard's CA, or TLS_CA_FILE with
    an own certificate. None when there is nothing to pin (plain HTTP, or a publicly trusted
    certificate)."""
    s = settings or get_settings()
    m = mode(s)
    try:
        if m == "forgeyard-ca":
            path = s.tls_dir / "ca.crt"
            return path.read_text() if path.is_file() else None
        if m == "own" and s.tls_ca_file.strip():
            return first_pem(Path(s.tls_ca_file).read_text())
    except (OSError, TlsError):
        return None
    return None


def uvicorn_config(app, settings: Optional[Settings] = None, **kwargs):
    """A uvicorn Config that listens with HTTPS when TLS is on (certificates made or renewed first)."""
    import uvicorn

    s = settings or get_settings()
    material = ensure(s)
    config = uvicorn.Config(app, host=s.host, port=s.port, proxy_headers=False, **kwargs)
    config.load()
    if material.mode != "off":
        config.ssl = server_context(material)
    live.set(material, config.ssl)
    return config
