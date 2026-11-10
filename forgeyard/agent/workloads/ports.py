"""Host ports for services: taken from the agent's range, kept for the life of the replica."""

from __future__ import annotations

import socket
from typing import Callable, Iterable


def parse_range(text: str) -> tuple[int, int]:
    lo, sep, hi = text.partition("-")
    try:
        lo_n, hi_n = int(lo), int(hi if sep else lo)
    except ValueError:
        raise ValueError(f"--port-range wants LOW-HIGH, got {text!r}") from None
    if not (1024 <= lo_n <= hi_n <= 65535):
        raise ValueError("--port-range must lie within 1024-65535, low before high")
    return lo_n, hi_n


def bindable(port: int) -> bool:
    """Nothing listens on it now (on any address)."""
    for family, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.bind((addr, port))
        except OSError as e:
            if family == socket.AF_INET6 and e.errno in (97, 99):  # no IPv6 here
                continue
            return False
    return True


def allocate(count: int, port_range: tuple[int, int], reserved: Iterable[int],
             probe: Callable[[int], bool] = bindable) -> list[int]:
    taken = set(reserved)
    out: list[int] = []
    lo, hi = port_range
    for port in range(lo, hi + 1):
        if len(out) == count:
            break
        if port in taken or not probe(port):
            continue
        out.append(port)
        taken.add(port)
    if len(out) < count:
        raise RuntimeError(f"no free host port left in {lo}-{hi}")
    return out
