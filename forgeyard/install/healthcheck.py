"""The server image's HEALTHCHECK: `python -m forgeyard.install.healthcheck` exits 0 when this
container's own server answers GET /api/health, 1 otherwise.

It asks over the loopback address inside the container, with the same HOST / PORT / TLS settings
the server reads. With TLS on, the certificate is not verified: the request never leaves the
container's network namespace, carries no credential, and only its status code is used, so there is
nothing for anyone in between to gain. Verifying would also fail for a reason that says nothing
about health: with TLS_CERT_FILE the certificate names the public host, not 127.0.0.1.

Standard library only, so it costs one Python start and no extra package in the image.
"""

from __future__ import annotations

import os
import ssl
import sys
import urllib.request

_OFF = ("off", "false", "0", "no")


def url(env: dict | None = None) -> str:
    e = os.environ if env is None else env
    host = (e.get("HOST") or "127.0.0.1").strip()
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    scheme = "http" if (e.get("TLS") or "on").strip().lower() in _OFF else "https"
    return f"{scheme}://{host}:{(e.get('PORT') or '7860').strip()}/api/health"


def check(target: str, timeout: float = 4.0) -> bool:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # loopback liveness probe: see the module docstring
    # Never through a proxy from the environment (HTTPS_PROXY): this is the container's own loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(target, timeout=timeout):
            return True  # a 4xx or 5xx raises HTTPError (an OSError)
    except (OSError, ValueError):
        return False


def main() -> int:
    target = url()
    if check(target):
        return 0
    print(f"unhealthy: no answer from {target}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
