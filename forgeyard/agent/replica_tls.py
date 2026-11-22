"""TLS for the agent's key guards (1.4.5, issue #163): the agent's side. The server's side, and why the certificate
names the machine and not its address, is in forgeyard/security/replica_tls.py.

One SSLContext serves every guard of this agent. Its certificate comes from the server: the agent makes a new EC
P-256 key, sends a CSR over its authenticated WebSocket (`cert_request`) and installs the chain it gets back
(`certificate`). The key never leaves the machine. Key and chain are kept in the state directory
(`replica-tls/key.pem`, mode 0600, and `replica-tls/chain.pem`), so a restarted agent serves TLS at once; a new
certificate is loaded into the same context, so guards switch without a restart (connections already open keep the
one they started with).

When the guards serve TLS: only once the server listed "guard_tls" in its welcome (Supervisor.new_connection).
Then a guard without a certificate yet refuses every connection (the TLS handshake fails), and plain HTTP is never
served: the gateway of such a server dials only https://. An older server dials plain HTTP, so the guards serve
plain HTTP to it (the agent says so in its log).

Needs the `cryptography` package for the key and the CSR; without it the agent does not list "guard_tls" and the
server keeps reaching it over plain HTTP (and says so).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import ssl
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("forgeyard.agent")

DIR = "replica-tls"
KEY_FILE = "key.pem"
CHAIN_FILE = "chain.pem"
RENEW_BEFORE = dt.timedelta(days=10)     # security/replica_tls.py RENEW_BEFORE_DAYS
CHECK_EVERY = 3600.0                      # seconds between two looks at the certificate's age
ANSWER_WAIT = 60.0                        # a request not answered by then is sent again
RETRY_AFTER = 300.0                       # after a refusal


def available() -> bool:
    try:
        import cryptography.x509  # noqa: F401
    except ImportError:
        return False
    return True


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _fingerprint(der: bytes) -> str:
    return "sha256:" + hashlib.sha256(der).hexdigest()


def _pems(text: str) -> list[str]:
    out, begin = [], "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    i = text.find(begin)
    while i >= 0:
        j = text.find(end, i)
        if j < 0:
            break
        out.append(text[i:j + len(end)] + "\n")
        i = text.find(begin, j)
    return out


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class CertificateRefused(ValueError):
    """A certificate the agent will not serve (not for its key, its name or its CA)."""


class ReplicaTls:
    def __init__(self, state_dir: Optional[Path]):
        self.dir = Path(state_dir) / DIR if state_dir is not None else None
        self.usable = available()
        self.context = self._new_context()
        self.info: Optional[dict] = None        # {serial, not_after, name, ca} of what the guards serve
        self._pending: Optional[tuple[str, bytes]] = None   # (request id, key PEM) of the request in flight
        self.requested_at = 0.0                 # monotonic time of the last request (0: none)
        self.refused_at = 0.0
        self.load()

    @staticmethod
    def _new_context() -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["http/1.1"])
        return ctx

    # ------------------------------------------------------------------ what it serves
    @staticmethod
    def describe(chain_pem: str) -> dict:
        """{serial, not_after, name, ca} of a chain (the leaf, then its CA)."""
        from cryptography import x509

        pems = _pems(chain_pem)
        if len(pems) < 2:
            raise CertificateRefused("the chain must hold the certificate and its CA")
        leaf = x509.load_pem_x509_certificate(pems[0].encode("ascii"))
        ca = x509.load_pem_x509_certificate(pems[1].encode("ascii"))
        try:
            names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
                .get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            names = []
        from cryptography.hazmat.primitives import serialization

        return {"serial": f"{leaf.serial_number:x}", "not_after": leaf.not_valid_after_utc.isoformat(),
                "name": names[0] if names else None,
                "ca": _fingerprint(ca.public_bytes(serialization.Encoding.DER))}

    def load(self) -> None:
        """The certificate kept from an earlier run, if it is still good for its key."""
        if self.dir is None or not self.usable:
            return
        key, chain = self.dir / KEY_FILE, self.dir / CHAIN_FILE
        if not key.is_file() or not chain.is_file():
            return
        try:
            info = self.describe(chain.read_text(encoding="ascii"))
            self.context.load_cert_chain(str(chain), str(key))
        except (OSError, ValueError, ssl.SSLError) as e:
            log.warning("The replica certificate in %s cannot be used (%s); asking the server for a new one.",
                        self.dir, e)
            return
        self.info = info
        if self.expired():
            log.warning("The replica certificate in %s expired on %s; asking the server for a new one.", self.dir,
                        info["not_after"][:10])

    def report(self) -> Optional[dict]:
        """For hello's runtime_info.tls."""
        return dict(self.info) if self.info else None

    def not_after(self) -> Optional[dt.datetime]:
        if not self.info:
            return None
        try:
            return dt.datetime.fromisoformat(self.info["not_after"])
        except (KeyError, TypeError, ValueError):
            return None

    def expired(self, now: Optional[dt.datetime] = None) -> bool:
        end = self.not_after()
        return end is None or end <= (now or _now())

    def wants(self, name: Optional[str], ca: Optional[str], now: Optional[dt.datetime] = None) -> Optional[str]:
        """Why a new certificate is needed for the binding name and CA the server gave, or None."""
        if not self.info:
            return "there is none yet"
        if name and self.info.get("name") != name:
            return "it names another machine"
        if ca and self.info.get("ca") != ca:
            return "another CA signed it"
        end = self.not_after()
        if end is None:
            return "its expiry cannot be read"
        left = end - (now or _now())
        if left <= dt.timedelta(0):
            return "it has expired"
        if left < RENEW_BEFORE:
            return f"it expires in {max(0, left.days)} days"
        return None

    # ------------------------------------------------------------------ getting one
    def request(self) -> tuple[str, str]:
        """(request id, CSR PEM) for a new key, kept in memory until its certificate comes."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        # The subject is a placeholder: the server names the certificate itself.
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "forgeyard-replica-guard")]))
               .sign(key, hashes.SHA256()))
        request_id = uuid.uuid4().hex[:16]
        self._pending = (request_id, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
        return request_id, csr.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def install(self, request_id: Optional[str], chain_pem: str, *, name: Optional[str] = None,
                ca: Optional[str] = None) -> dict:
        """Serve the certificate the server signed for the pending request. Raises CertificateRefused when it is
        not for that request's key, or names another machine or CA than the welcome did."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        pending = self._pending
        if pending is None or not request_id or request_id != pending[0]:
            raise CertificateRefused("it answers no request of this agent")
        if not isinstance(chain_pem, str) or not chain_pem:
            raise CertificateRefused("it carries no certificate")
        info = self.describe(chain_pem)
        key = serialization.load_pem_private_key(pending[1], password=None)
        leaf = x509.load_pem_x509_certificate(_pems(chain_pem)[0].encode("ascii"))
        pub = serialization.PublicFormat.SubjectPublicKeyInfo
        if leaf.public_key().public_bytes(serialization.Encoding.DER, pub) != \
                key.public_key().public_bytes(serialization.Encoding.DER, pub):
            raise CertificateRefused("it is not for the key this agent made")
        if name and info["name"] != name:
            raise CertificateRefused(f"it names {info['name']}, not this machine ({name})")
        if ca and info["ca"] != ca:
            raise CertificateRefused("another CA than the server's signed it")
        if self.dir is not None:
            _write_private(self.dir / KEY_FILE, pending[1])
            _write_private(self.dir / CHAIN_FILE, chain_pem.encode("ascii"))
            self.context.load_cert_chain(str(self.dir / CHAIN_FILE), str(self.dir / KEY_FILE))
        else:  # tests without a state directory: through a temporary file all the same
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                _write_private(Path(tmp) / KEY_FILE, pending[1])
                _write_private(Path(tmp) / CHAIN_FILE, chain_pem.encode("ascii"))
                self.context.load_cert_chain(str(Path(tmp) / CHAIN_FILE), str(Path(tmp) / KEY_FILE))
        self._pending = None
        self.info = info
        self.refused_at = 0.0
        return info

    def answers(self, request_id) -> bool:
        """Whether an answer is for the request in flight (not one sent again since, or never sent)."""
        return self._pending is not None and isinstance(request_id, str) and request_id == self._pending[0]

    def refused(self, request_id: Optional[str], now: float) -> None:
        if self._pending is not None and request_id == self._pending[0]:
            self._pending = None
        self.refused_at = now
