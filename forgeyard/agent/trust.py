"""Which server the agent trusts: the CA pinned when it joined, never "whatever the network says".

    --ca-fingerprint sha256:…   the join command's pin. The agent fetches the server's CA once
                                (GET /api/tls/ca.pem, before it trusts anything), refuses it unless
                                its SHA-256 matches, stores it in the state directory (server-ca.pem)
                                and from then on accepts only server certificates signed by that CA
                                (not by any CA in the system store). A machine in the middle with
                                another CA is refused: its CA does not match the pin, and the real
                                CA does not sign its certificate.
    --ca-file PATH              the same with a CA file you already have (a company CA, a proxy's).
    neither, https://           the system trust store (a publicly trusted certificate).
    http://                     plain HTTP, only for a server started with TLS=off.

`forgeyard agent trust` changes this for an agent that already joined (over plain HTTP before
Forgeyard 1.1, or when the server's CA changed): it keeps the machine's credential.
"""

from __future__ import annotations

import hashlib
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from forgeyard import __version__

CA_FILE = "server-ca.pem"
CA_PATH = "/api/tls/ca.pem"
HEALTH_PATH = "/api/health"
_FP_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")


class TrustError(Exception):
    pass


def normalise_fingerprint(text: str) -> str:
    """'sha256:ab…', 'AB:CD:…' or plain hex -> 'sha256:<64 lowercase hex>'."""
    cleaned = text.strip().lower().replace(":", "").replace(" ", "")
    cleaned = cleaned.removeprefix("sha256")
    m = _FP_RE.match(cleaned)
    if not m:
        raise TrustError(f"{text!r} is not a SHA-256 fingerprint (sha256: and 64 hex digits)")
    return "sha256:" + m.group(1)


def pem_certificate(text: str) -> str:
    begin = text.find("-----BEGIN CERTIFICATE-----")
    end = text.find("-----END CERTIFICATE-----", begin)
    if begin < 0 or end < 0:
        raise TrustError("not a PEM certificate")
    return text[begin:end + len("-----END CERTIFICATE-----")] + "\n"


def fingerprint_of(pem: str) -> str:
    return "sha256:" + hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem_certificate(pem))).hexdigest()


def context(ca_pem: Optional[str]) -> ssl.SSLContext:
    """Verify against `ca_pem` only; None: the system trust store. Host names are always checked."""
    ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if ca_pem:
        # The pinned CA may be an intermediate (a company CA's issuing CA): it is the anchor.
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


def _get(url: str, ctx: ssl.SSLContext, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"forgeyard-agent/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read(256 * 1024)


def explain(e: BaseException, server: str) -> str:
    # urllib wraps the TLS error in a URLError; websockets raises it as it is (an SSLError's own
    # .reason is only a short code, so it is not unwrapped).
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (f"{server} is not the server this agent trusts: {reason.verify_message or reason}. If the "
                f"server's certificate authority changed on purpose, run `forgeyard agent trust` with the "
                f"fingerprint shown on its Machines page.")
    if isinstance(reason, ssl.SSLError):
        return f"TLS with {server} failed: {reason}"
    return f"Cannot reach {server}: {reason}"


def fetch_pinned_ca(server: str, fingerprint: str, *, timeout: float = 15.0) -> str:
    """The server's CA, if (and only if) its fingerprint is the pinned one; then checked by
    talking to the server through it."""
    if not server.startswith("https://"):
        raise TrustError("A CA fingerprint needs an https:// server address")
    want = normalise_fingerprint(fingerprint)
    # Read before anything is trusted: the pin, not the connection, decides. Nothing secret is sent.
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    try:
        body = _get(server + CA_PATH, unverified, timeout).decode("ascii", "replace")
    except urllib.error.HTTPError as e:
        raise TrustError(f"{server} has no CA to download (HTTP {e.code}): does it use its own certificate? "
                         "Then give its CA with --ca-file, or leave the fingerprint out.") from None
    except (urllib.error.URLError, OSError) as e:
        raise TrustError(explain(e, server)) from None
    try:
        pem = pem_certificate(body)
        got = fingerprint_of(pem)
    except (TrustError, ValueError):
        raise TrustError(f"{server}{CA_PATH} did not return a certificate") from None
    if got != want:
        raise TrustError(f"Refused: the CA {server} sent has fingerprint {got}, not {want}. Someone may be between "
                         "this machine and the server, or the fingerprint is from another server. Check it on the "
                         "server's Machines page.")
    verify(server, pem, timeout=timeout)
    return pem


def verify(server: str, ca_pem: Optional[str], *, timeout: float = 15.0) -> None:
    """Talk to the server the way the agent will: through the pinned CA (or the system store)."""
    try:
        _get(server + HEALTH_PATH, context(ca_pem), timeout)
    except urllib.error.HTTPError:
        return  # TLS worked; whatever the route says is not about trust
    except (urllib.error.URLError, OSError) as e:
        raise TrustError(explain(e, server)) from None


def read_ca_file(path: str) -> str:
    try:
        return pem_certificate(Path(path).read_text())
    except OSError as e:
        raise TrustError(f"Cannot read {path}: {e}") from None


def save_ca(state_dir: Path, pem: Optional[str]) -> Optional[Path]:
    path = state_dir / CA_FILE
    if pem is None:
        path.unlink(missing_ok=True)
        return None
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(pem)
    tmp.chmod(0o644)
    tmp.replace(path)
    return path


def load_ca(state_dir: Path) -> Optional[str]:
    try:
        return pem_certificate((state_dir / CA_FILE).read_text())
    except FileNotFoundError:
        return None


def resolve(server: str, *, fingerprint: Optional[str], ca_file: Optional[str]) -> Optional[str]:
    """The CA to pin for this server (None: system store or plain HTTP), checked against it."""
    if fingerprint and ca_file:
        raise TrustError("Give --ca-fingerprint or --ca-file, not both")
    if ca_file:
        if not server.startswith("https://"):
            raise TrustError("A CA file needs an https:// server address")
        pem = read_ca_file(ca_file)
        verify(server, pem)
        return pem
    if fingerprint:
        return fetch_pinned_ca(server, fingerprint)
    if server.startswith("https://"):
        verify(server, None)
    return None
