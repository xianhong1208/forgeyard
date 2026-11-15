"""Is a provider outside your network? The suggestion behind "Outside your network (cloud)" on a new
provider (1.3.1, decision D4). It costs money and sends prompts out, so a wrong "inside" matters:
keys that may not use the cloud still reach a provider inside your network, and no budget applies.

- A kind that is a cloud service (OpenAI, Anthropic, Gemini, Azure OpenAI, OpenRouter) is outside,
  whatever its address.
- An OpenAI-compatible server is judged by its base URL's host:
  - an IP address: inside when it is private (RFC 1918, 100.64.0.0/10 shared by VPNs such as
    Tailscale, loopback, link-local, IPv6 ULA fc00::/7), outside when it is any other unicast address
    (documentation addresses count as public); anything else (multicast, unspecified) is unknown;
  - a name for your own network (localhost, a single label like `gpu-box`, or one ending in .local,
    .localhost, .lan, .internal, .home.arpa): inside;
  - any other name is resolved on the server (where requests are sent from), with a short timeout:
    inside when every address is private, outside when every address is public, and unknown when it
    does not resolve or the answers are mixed;
  - unknown keeps the kind's default (inside) and says so.

Admins may always change the tick; existing providers are never changed (Setup health flags one
inside your network whose address is public).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit

from forgeyard.gateway import quirks

PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",   # RFC 1918
    "100.64.0.0/10",                                    # shared address space (CGNAT, VPNs)
    "127.0.0.0/8", "169.254.0.0/16",                    # loopback, link-local
    "::1/128", "fe80::/10", "fc00::/7",                 # IPv6 loopback, link-local, ULA
))
LOCAL_SUFFIXES = (".local", ".localhost", ".lan", ".internal", ".home.arpa")


@dataclass
class Where:
    external: bool
    basis: str          # kind | address | name | resolved | unknown
    reason: str

    def out(self) -> dict:
        return {"external": self.external, "basis": self.basis, "reason": self.reason}


def address_kind(addr: str) -> Optional[str]:
    """"private", "public", or None (not an address, or one that is neither)."""
    try:
        ip = ipaddress.ip_address(addr.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in net for net in PRIVATE_NETS if net.version == ip.version):
        return "private"
    if ip.is_multicast or ip.is_unspecified or (ip.version == 4 and ip in ipaddress.ip_network("240.0.0.0/4")):
        return None
    return "public"


def local_name(host: str) -> bool:
    host = host.strip().rstrip(".").lower()
    return bool(host) and (host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES))


def _resolve(host: str) -> list[str]:
    from forgeyard import advisor

    return advisor.addresses(host)  # cached, with a timeout; [] when it does not resolve in time


def classify(kind: str, base_url: Optional[str], resolve: Callable[[str], list[str]] = _resolve) -> Where:
    k = quirks.get(kind)
    url = (base_url or "").strip() or k.base_url
    if k.default_external:
        return Where(True, "kind", f"{k.label} is a cloud service: requests leave your network and cost money.")
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    default = k.default_external
    if not host:
        return Where(default, "unknown", "Enter the base URL to see whether it is inside your network.")
    found = address_kind(host)
    if found == "private":
        return Where(False, "address", f"{host} is a private address: a server inside your network.")
    if found == "public":
        return Where(True, "address", f"{host} is a public address: probably a third party, which may cost money.")
    try:
        ipaddress.ip_address(host)
        return Where(default, "unknown", f"{host} is neither a private nor a public address; check the tick.")
    except ValueError:
        pass
    if local_name(host):
        return Where(False, "name", f"{host} is a name on your own network.")
    addrs = resolve(host)
    kinds = {address_kind(a) for a in addrs}
    shown = ", ".join(addrs[:2])
    if addrs and kinds == {"private"}:
        return Where(False, "resolved", f"{host} resolves to a private address ({shown}): inside your network.")
    if addrs and kinds == {"public"}:
        return Where(True, "resolved", f"{host} resolves to a public address ({shown}): probably a third party, which "
                                       "may cost money.")
    if not addrs:
        why = f"{host} did not resolve from the server"
    else:
        why = f"{host} resolves to both private and public addresses ({shown})"
    return Where(default, "unknown", f"{why}, so Forgeyard cannot tell. Tick it if this is a third party.")
