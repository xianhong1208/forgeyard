"""Installing an agent from this server: GET /install.sh and the agent's wheel.

Public on purpose, like the CA: a new machine has nothing but its join token. Both are served only
over HTTPS (directly, or through a trusted TLS proxy): the one-line install checks the CA first and
then trusts nothing else, so a plain-HTTP copy could be swapped on the way. The wheel is the code
this server runs (no secrets in it).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from forgeyard.api.deps import request_is_https
from forgeyard.install import commands, wheel

router = APIRouter(tags=["install"])


def _own_base(request: Request) -> str:
    """The address this request came to (it passed the host check): the address that worked."""
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and request_is_https(request):
        base = "https://" + base[len("http://"):]
    return base


def _refuse(message: str, status: int = 403) -> PlainTextResponse:
    # A shell script that only fails, in case someone pipes it into sh without curl -f.
    text = message.replace("'", "")
    return PlainTextResponse(f"echo 'Forgeyard: {text}' >&2\nexit 1\n", status_code=status,
                             media_type="text/x-shellscript")


@router.get("/install.sh", include_in_schema=False)
def install_sh(request: Request):
    if not request_is_https(request):
        return _refuse("the install script is served only over HTTPS. Use the command on the Machines page.")
    server = _own_base(request)
    if not commands.safe_server(server):
        return _refuse(f"cannot serve the install script for the address {server}.", 400)
    return Response(commands.install_script(server), media_type="text/x-shellscript",
                    headers={"Cache-Control": "no-store"})


@router.get("/install/{name}", include_in_schema=False)
def agent_wheel(name: str, request: Request):
    if not request_is_https(request):
        return _refuse("the agent is served only over HTTPS.")
    fname, data, sha = wheel.cached()
    if name != fname:
        return PlainTextResponse(f"This server has the agent {fname} only.\n", status_code=404)
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"', "X-Checksum-SHA256": sha,
                             "Cache-Control": "no-store"})
