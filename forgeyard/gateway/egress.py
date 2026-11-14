"""Requests that leave for a cloud provider (1.2): their own HTTP client and rules, apart from the
client for replicas on the LAN.

- TLS is verified (the system's CAs, plus EGRESS_CA_FILE when set); redirects are never followed, so
  a provider key cannot be sent on to wherever a 3xx points;
- never a proxy from the environment: EGRESS_PROXY when set, else a direct connection;
- https only. Plain http, and private or loopback addresses, are allowed for a provider marked as
  inside your network (a vLLM on your VPN) whose admin ticked "allow plain HTTP"; private addresses
  over https for any provider inside your network. A provider outside your network must resolve to
  public addresses only (PROVIDER_ALLOW_PRIVATE_HOSTS lifts that, for testing with a stand-in);
- link-local addresses (which include the cloud metadata service 169.254.169.254 and fd00:ec2::254),
  multicast, reserved and unspecified addresses are always refused;
- EGRESS_ALLOWED_HOSTS limits the hosts at all;
- the host name is resolved and checked before every request, and a direct connection goes to the
  address that was checked (TLS still verifies the host name), so a DNS answer that changes in
  between cannot send the request elsewhere. Through EGRESS_PROXY the proxy resolves names itself
  (networks with an egress proxy often have no outside DNS): only addresses written in the URL are
  checked then, and EGRESS_ALLOWED_HOSTS is what limits the names.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

import httpx

from forgeyard.config import get_settings
from forgeyard.models.fetch import host_matches

METADATA = (ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"),
            ipaddress.ip_address("100.100.100.200"))
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
RESOLVE_TTL = 30.0


class EgressError(Exception):
    """The request may not go there; the message is safe to show (no key, no body)."""


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise EgressError(f"cannot resolve {host}: {e.strerror or e}") from None
    return list(dict.fromkeys(info[4][0] for info in infos))


resolver: Resolver = system_resolver   # tests replace it
_cache: dict[tuple[str, int], tuple[float, list[str]]] = {}


def reset() -> None:
    global resolver
    resolver = system_resolver
    _cache.clear()
    _clients.clear()


@dataclass
class Destination:
    scheme: str
    host: str
    port: int
    base_path: str


def check_url(base_url: str, *, external: bool, allow_http: bool) -> Destination:
    """The base URL's parts, or EgressError. Only the URL itself: addresses are checked per request."""
    s = get_settings()
    parts = urlsplit((base_url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise EgressError("the base URL must start with https://")
    if parts.username or parts.password:
        raise EgressError("credentials in the URL are not allowed; give the key in its own field")
    if parts.query or parts.fragment:
        raise EgressError("the base URL may not have a query or a fragment")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise EgressError("the base URL has no host")
    if scheme == "http" and not (allow_http and not external) and not s.provider_allow_private_hosts:
        raise EgressError("plain http is refused: use https (http only for a provider inside your network, "
                          "with \"allow plain HTTP\")")
    allowed = s.egress_allowed_host_list
    if allowed and not host_matches(host, allowed):
        raise EgressError(f"{host} is not in EGRESS_ALLOWED_HOSTS")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise EgressError("the base URL has an invalid port") from None
    return Destination(scheme, host, port, parts.path.rstrip("/"))


def address_problem(addr: str, *, private_ok: bool,
                    private_why: str = "the provider is marked as outside your network") -> Optional[str]:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return f"{addr} is not an IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in METADATA or ip.is_link_local:
        return f"{ip} is link-local (this includes cloud metadata services)"
    if ip.is_multicast or ip.is_unspecified or (ip.is_reserved and not ip.is_private):
        return f"{ip} is not a usable address"
    local = ip.is_loopback or ip.is_private or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
    if local and not private_ok:
        return f"{ip} is a private or loopback address, but {private_why}"
    if not local and not ip.is_global:
        return f"{ip} is not a public address"
    return None


async def resolve(host: str, port: int) -> list[str]:
    """The host's addresses (an IP address is its own), cached for RESOLVE_TTL seconds."""
    key = (host, port)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = await resolver(host, port)
    if not addresses:
        raise EgressError(f"cannot resolve {host}")
    _cache[key] = (now + RESOLVE_TTL, addresses)
    if len(_cache) > 1000:
        _cache.clear()
    return addresses


async def checked_address(dest: Destination, *, external: bool, allow_http: bool) -> str:
    """Resolve the host (cached briefly) and check every address it has; the one to connect to."""
    s = get_settings()
    addresses = await resolve(dest.host, dest.port)
    private_ok = (not external) or s.provider_allow_private_hosts
    for addr in addresses:
        problem = address_problem(addr, private_ok=private_ok)
        if problem:
            raise EgressError(f"{dest.host} resolves to {problem}; refused")
    if dest.scheme == "http" and not s.provider_allow_private_hosts:
        # Plain http only ever to your own network.
        if any(address_problem(a, private_ok=False) is None for a in addresses):
            raise EgressError(f"{dest.host} is a public address: plain http is refused, use https")
    return addresses[0]


async def prepare(base_url: str, path: str, *, external: bool, allow_http: bool) -> tuple[str, dict, dict]:
    """(the URL to request, extra headers, request extensions) for `path` under the base URL."""
    dest = check_url(base_url, external=external, allow_http=allow_http)
    full = f"{dest.base_path}/{path.lstrip('/')}"
    default_port = dest.port == (443 if dest.scheme == "https" else 80)
    host = f"[{dest.host}]" if ":" in dest.host else dest.host
    host_header = host if default_port else f"{host}:{dest.port}"
    if get_settings().egress_proxy.strip():
        if _is_ip(dest.host):
            await checked_address(dest, external=external, allow_http=allow_http)
        return f"{dest.scheme}://{host_header}{full}", {}, {}
    address = await checked_address(dest, external=external, allow_http=allow_http)
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    netloc = f"[{ip}]" if ip.version == 6 else str(ip)
    url = f"{dest.scheme}://{netloc}:{dest.port}{full}"
    extensions = {"sni_hostname": dest.host} if dest.scheme == "https" else {}
    return url, {"Host": host_header}, extensions


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ the client
_clients: dict[int, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    extra = get_settings().egress_ca_file.strip()
    if extra:
        ctx.load_verify_locations(cafile=extra)
    return ctx


def no_cookies() -> http.cookiejar.CookieJar:
    """A cookie jar that keeps nothing. The gateway's HTTP clients are shared by every request: a cookie
    one upstream set (a replica, a workload at /x/, a provider) must never ride along on someone
    else's next request."""
    return http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


def client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    entry = _clients.get(id(loop))
    if entry is None or entry[0] is not loop or entry[1].is_closed:
        s = get_settings()
        c = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=s.provider_connect_timeout, read=s.provider_read_timeout, write=60.0,
                                  pool=30.0),
            limits=httpx.Limits(max_connections=500, max_keepalive_connections=50),
            verify=_ssl_context(),
            trust_env=False,           # never HTTPS_PROXY & co. from the environment
            proxy=s.egress_proxy.strip() or None,
            cookies=no_cookies(),      # nothing a provider sets is kept for the next request
            follow_redirects=False)    # a key is never sent on to a redirect target
        _clients[id(loop)] = (loop, c)
        entry = (loop, c)
    return entry[1]


async def close_client() -> None:
    entry = _clients.pop(id(asyncio.get_running_loop()), None)
    if entry is not None:
        await entry[1].aclose()


def timeout_for(seconds: Optional[int]) -> Optional[httpx.Timeout]:
    if not seconds:
        return None
    s = get_settings()
    return httpx.Timeout(connect=s.provider_connect_timeout, read=float(seconds), write=60.0, pool=30.0)
