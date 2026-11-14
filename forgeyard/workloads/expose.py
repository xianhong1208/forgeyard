"""Exposing a service workload behind the gateway at /x/<name> (1.3, generic passthrough).

A workload's `gateway` setting (kept beside its spec, never in it, and changeable while it runs):

    expose           false (the default): the workload is not reachable through the gateway
    port             which of its ports; default: the first
    read_timeout     seconds the gateway waits for each piece of an answer (600)
    max_body_mb      the largest request body it takes (32 MiB; 413 above)
    concurrency      requests one replica takes at once (none: no limit; 429 when every replica is full)
    key_concurrency  requests one API key may have in flight to it at once (none: no limit)

An exposed workload runs behind the agent's key guard, like a model replica: the program listens on
loopback, the guard on the published port, and only the gateway (which holds the replica's internal
key) gets through. The guard strips that key before the request reaches the program. Every port of
the workload is guarded, not only the exposed one. Only agents that list "workload_guard" can run
an exposed workload; elsewhere it waits with a reason.
"""

from __future__ import annotations

import secrets
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forgeyard.db import Node, Replica, Workload
from forgeyard.machines.protocol import FEATURE_REPLICA_GUARD, FEATURE_REPLICA_KEYS, FEATURE_WORKLOAD_GUARD

PREFIX = "/x"
DEFAULT_READ_TIMEOUT = 600
DEFAULT_MAX_BODY_MB = 32
MAX_BODY_MB = 1024
NEEDS = (FEATURE_REPLICA_KEYS, FEATURE_REPLICA_GUARD, FEATURE_WORKLOAD_GUARD)


class Exposure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expose: bool = False
    port: Optional[str] = Field(default=None, max_length=15)
    read_timeout: int = Field(default=DEFAULT_READ_TIMEOUT, ge=1, le=3600)
    max_body_mb: int = Field(default=DEFAULT_MAX_BODY_MB, ge=1, le=MAX_BODY_MB)
    concurrency: Optional[int] = Field(default=None, ge=1, le=10_000)
    key_concurrency: Optional[int] = Field(default=None, ge=1, le=10_000)


def check(spec: dict, value: Exposure) -> Optional[str]:
    """Why this workload cannot be exposed like this (None: it can)."""
    if not value.expose:
        return None
    if spec.get("kind") != "service":
        return "only services can be exposed: a task has no port to call"
    runtime = (spec.get("runtime") or {}).get("type")
    if runtime == "model":
        return "a model deployment is served at /v1, not /x/"
    ports = [p.get("name") for p in spec.get("ports") or []]
    if not ports:
        return "the workload has no port: add one (the program's HTTP port)"
    if value.port is not None and value.port not in ports:
        return f"port {value.port!r} is not one of the workload's ports ({', '.join(ports)})"
    return None


def of(w: Workload) -> Optional[Exposure]:
    """The workload's setting when it is exposed, else None."""
    if not w.gateway or w.runtime == "model":
        return None
    try:
        value = Exposure.model_validate(w.gateway)
    except ValidationError:
        return None
    return value if value.expose else None


def settings_of(w: Workload) -> Exposure:
    """The setting as saved, exposed or not (the defaults when there is none)."""
    try:
        return Exposure.model_validate(w.gateway or {})
    except ValidationError:
        return Exposure()


def port_name(w: Workload, value: Exposure) -> Optional[str]:
    if value.port:
        return value.port
    ports = (w.spec or {}).get("ports") or []
    return ports[0].get("name") if ports else None


def can_guard(node: Optional[Node]) -> bool:
    """The machine's agent puts its key guard in front of a workload and strips the key."""
    features = set((node.features if node is not None else None) or [])
    return all(f in features for f in NEEDS)


def guard_problem(node: Node) -> str:
    return "its agent cannot guard a workload exposed at /x/ (update the agent to this Forgeyard version)"


def give_key(r: Replica) -> None:
    """A placed replica of an exposed workload gets its internal key (once per placement)."""
    if not r.secret:
        r.secret = secrets.token_urlsafe(32)


def url_path(name: str) -> str:
    return f"{PREFIX}/{name}"
