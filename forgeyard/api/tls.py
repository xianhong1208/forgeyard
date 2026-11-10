"""HTTPS and trust: what the "Trust this server" page shows, and the CA to download.

Everything here except the admin details is public on purpose: a device has to fetch the CA before
it can trust the server, so before anyone signs in on it. A CA certificate is public information
(every TLS client receives it); its private key never leaves data_dir/tls.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session as DbSession

from forgeyard.api.deps import fail, optional_user, public_base, request_is_https
from forgeyard.config import get_settings
from forgeyard.db import get_db
from forgeyard.security import tls

router = APIRouter(prefix="/api/tls", tags=["tls"])

AGENT = "uv run --no-sync python -m forgeyard agent"


def agent_commands(server: str, token: Optional[str] = None) -> dict:
    """The join command for a new machine and the one-time trust command for agents that joined
    before (over plain HTTP, or before the CA changed)."""
    anchor = tls.trust_anchor_pem()
    pin = f" --ca-fingerprint {tls.fingerprint(anchor)}" if anchor and server.startswith("https://") else ""
    out = {"trust_command": f"{AGENT} trust --server {server}{pin}" if server.startswith("https://") else None}
    if token is not None:
        out["command"] = f"{AGENT} --server {server} --join-token {token}{pin}"
    return out


def trust_page(request: Request) -> Optional[str]:
    """Where people get the CA that /v1 clients must trust (None: nothing to trust, plain HTTP or
    a publicly trusted certificate). Shown next to the gateway address and in the code snippets."""
    base = public_base(request)
    if not base.startswith("https://") or not tls.trust_anchor_pem():
        return None
    return base + "/trust"


def tls_state(request: Request) -> dict:
    """The part of the TLS picture the web UI needs on every page (in /api/auth/state)."""
    settings = get_settings()
    return {"mode": tls.mode(settings), "https": request_is_https(request)}


@router.get("")
def tls_info(request: Request, db: DbSession = Depends(get_db)):
    settings = get_settings()
    mode = tls.mode(settings)
    anchor = tls.trust_anchor_pem(settings)
    out: dict = {"mode": mode, "https": request_is_https(request), "hsts": mode == "own", "ca": None,
                 "server_url": public_base(request)}
    if anchor:
        info = tls.read_cert(anchor.encode())
        fp = tls.fingerprint(anchor)
        out["ca"] = {"name": info.subject, "expires_at": info.not_after, "fingerprint": fp,
                     "fingerprint_colons": tls.colon_hex(fp), "own": mode == "forgeyard-ca"}
    user, _ = optional_user(request, db)
    if user is not None and user.role == "admin":
        out["admin"] = {**agent_commands(public_base(request)), "server_certificate": None}
        material = tls.live.material
        cert_file = material.cert_file if material is not None else None
        if cert_file is None and mode == "forgeyard-ca":
            cert_file = settings.tls_dir / "server.crt"
        if cert_file is not None and cert_file.is_file():
            try:
                c = tls.read_cert(cert_file.read_bytes())
                out["admin"]["server_certificate"] = {"names": c.names, "ips": c.ips, "expires_at": c.not_after,
                                                      "issuer": c.issuer}
            except (ValueError, tls.TlsError):
                pass
    return out


def _anchor() -> str:
    anchor = tls.trust_anchor_pem()
    if not anchor:
        raise fail(404, "no_ca", "This server has no certificate authority to trust: it uses plain HTTP or a "
                                 "publicly trusted certificate.")
    return anchor


@router.get("/ca.pem")
def ca_pem():
    """PEM: Linux, macOS, Firefox, curl --cacert, Python (SSL_CERT_FILE, httpx verify=)."""
    return Response(_anchor(), media_type="application/x-pem-file",
                    headers={"Content-Disposition": 'attachment; filename="forgeyard-ca.pem"'})


@router.get("/ca.crt")
def ca_crt():
    """DER: Windows, Android, iOS (the type makes phones offer to install it)."""
    return Response(tls.der_of(_anchor()), media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="forgeyard-ca.crt"'})
