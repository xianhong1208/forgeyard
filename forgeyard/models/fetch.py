"""HTTP for model sources, guarded against server-side request forgery. Shared by the server
(inspecting a model: config, safetensors headers, GGUF metadata by range reads) and the agent
(downloading weights and engines).

Every request, and every redirect hop, is checked before anything is sent:

- the scheme is https (plain http only when private hosts are explicitly allowed, for a LAN mirror);
- the host name is resolved once and **every** address must be public: loopback, private, link-local
  (which includes the cloud metadata address 169.254.169.254), carrier-grade NAT, multicast,
  reserved and unspecified addresses are refused, for IPv4 and IPv6 (also IPv4-mapped);
- the connection then goes to exactly the address that was checked (TLS still verifies the host
  name), so a DNS answer that changes between the check and the connection (rebinding) cannot
  redirect it;
- an optional host allowlist (admin-configured URL sources) applies to the first URL; redirects of
  an allowed host may go elsewhere, but only to public addresses.

Only the standard library is used, so the agent needs no new dependency.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlsplit

USER_AGENT = "forgeyard-models/1"
MAX_REDIRECTS = 5

Resolver = Callable[[str, int], list[str]]


class FetchError(Exception):
    """The request was refused or failed; the message is safe to show."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class BlockedAddress(FetchError):
    pass


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve {host}: {e.strerror or e}") from None
    return list(dict.fromkeys(info[4][0] for info in infos))


_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_BENCHMARK = ipaddress.ip_network("198.18.0.0/15")


def address_problem(addr: str) -> Optional[str]:
    """Why this address must not be fetched from, or None if it is a public unicast address."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return f"{addr} is not an IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return f"{ip} is a loopback address"
    if ip.is_link_local:
        return f"{ip} is link-local (this includes cloud metadata services)"
    if isinstance(ip, ipaddress.IPv4Address) and (ip in _CGNAT or ip in _BENCHMARK):
        return f"{ip} is a shared or benchmarking address"
    if ip.is_private:
        return f"{ip} is a private address"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified or not ip.is_global:
        return f"{ip} is not a public address"
    return None


def host_matches(host: str, allowed: Iterable[str]) -> bool:
    """`example.org` allows example.org itself; `*.example.org` allows its subdomains only."""
    host = host.lower().rstrip(".")
    for entry in allowed:
        entry = entry.strip().lower().rstrip(".")
        if not entry:
            continue
        if entry.startswith("*."):
            if host.endswith(entry[1:]) and host != entry[2:]:
                return True
        elif host == entry:
            return True
    return False


@dataclass
class Policy:
    # Private and loopback addresses (and plain http) only for an explicitly configured LAN mirror.
    allow_private: bool = False
    # When set, the first URL's host must be on it (admin allowlist for URL sources).
    allowed_hosts: Optional[list[str]] = None
    resolver: Resolver = field(default=system_resolver)
    timeout: float = 30.0
    # Plain http; by default only together with private addresses (a LAN mirror).
    allow_http: Optional[bool] = None

    def check(self, url: str, *, first: bool = True) -> tuple[str, str, int, str]:
        """(scheme, host, port, the address to connect to) or a FetchError."""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("https", "http"):
            raise BlockedAddress(f"only https:// sources are allowed, not {scheme or 'a relative URL'}")
        if scheme == "http" and not (self.allow_private if self.allow_http is None else self.allow_http):
            raise BlockedAddress("plain http is refused; use https (http only for an allowed private mirror)")
        if parts.username or parts.password:
            raise BlockedAddress("credentials in the URL are not allowed")
        host = (parts.hostname or "").lower()
        if not host:
            raise BlockedAddress("the URL has no host")
        if first and self.allowed_hosts is not None and not host_matches(host, self.allowed_hosts):
            raise BlockedAddress(f"{host} is not on the allowlist for model URLs")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError:
            raise BlockedAddress("the URL has an invalid port") from None
        addresses = self.resolver(host, port)
        if not addresses:
            raise FetchError(f"cannot resolve {host}")
        if not self.allow_private:
            for addr in addresses:
                problem = address_problem(addr)
                if problem:
                    raise BlockedAddress(f"{host} resolves to {problem}; refused")
        return scheme, host, port, addresses[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a fixed, already-checked address, verifying the certificate for the host name."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class Response:
    """An open response: status, headers, the final URL, and read() / close()."""

    def __init__(self, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse, url: str):
        self._conn = conn
        self._resp = resp
        self.status = resp.status
        self.headers = resp.headers
        self.url = url

    def read(self, n: int = -1) -> bytes:
        return self._resp.read(n) if n >= 0 else self._resp.read()

    def read1(self, n: int) -> bytes:
        """Up to n bytes of what has arrived (at least one, or b"" at the end)."""
        return self._resp.read1(n)

    def close(self) -> None:
        try:
            self._resp.close()
        finally:
            self._conn.close()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_url(url: str, policy: Policy, *, headers: Optional[dict] = None, method: str = "GET") -> Response:
    """Open a URL (following up to five redirects, each one checked). The caller closes it.
    Authorization is dropped when a redirect leaves the original host (CDN redirects are signed)."""
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    origin = (urlsplit(url).hostname or "").lower()
    for hop in range(MAX_REDIRECTS + 1):
        scheme, host, port, address = policy.check(url, first=hop == 0)
        if host != origin:
            headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        cls = _PinnedHTTPS if scheme == "https" else _PinnedHTTP
        conn = cls(host, port, address, policy.timeout)
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request(method, path, headers={**headers, "Host": parts.netloc.rsplit("@", 1)[-1]})
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise FetchError(f"{host}: {getattr(e, 'strerror', None) or e or type(e).__name__}") from None
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.read()
            conn.close()
            if not location:
                raise FetchError(f"{host} answered {resp.status} without a Location")
            url = urljoin(url, location)
            if resp.status == 303:
                method = "GET"
            continue
        return Response(conn, resp, url)
    raise FetchError(f"more than {MAX_REDIRECTS} redirects")


def _error_text(resp: Response) -> str:
    try:
        body = resp.read(2048).decode("utf-8", "replace").strip()
    except Exception:
        body = ""
    return body[:200]


def get_bytes(url: str, policy: Policy, *, headers: Optional[dict] = None, limit: int = 64 * 1024**2) -> bytes:
    """GET a small body (JSON, a config file), at most `limit` bytes."""
    with open_url(url, policy, headers=headers) as resp:
        if resp.status >= 400:
            raise FetchError(f"{urlsplit(url).hostname} answered {resp.status}: {_error_text(resp)}", resp.status)
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise FetchError(f"the answer from {urlsplit(url).hostname} is larger than {limit} bytes")
    return data


def get_range(url: str, policy: Policy, start: int, end: int, *, headers: Optional[dict] = None) -> bytes:
    """Bytes start..end (inclusive). A server that ignores Range gets cut off after `end`."""
    want = end - start + 1
    with open_url(url, policy, headers={**(headers or {}), "Range": f"bytes={start}-{end}"}) as resp:
        if resp.status >= 400:
            raise FetchError(f"{urlsplit(url).hostname} answered {resp.status}: {_error_text(resp)}", resp.status)
        if resp.status == 200 and start > 0:
            raise FetchError(f"{urlsplit(url).hostname} does not support range requests")
        return resp.read(want)
