"""The supervisor's state in memory (forgeyard/agent/workloads/supervisor.py), beside the records on disk: what
it was told only in memory (keys, tokens), what it has in flight (operations, probes, downloads, guards), and
what it has still to report. Each structure is one kind of state with one lifetime.

The `Supervisor` keeps each field under its old attribute name too (`Alias`: `sup.keys` is `sup.secrets.keys`,
the same object), which the rest of the agent and the tests use."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from forgeyard.agent.workloads.guard import Guard


@dataclass
class Secrets:
    """Told by the server with `desired`, kept in memory only: never in a record, a log line or a status
    message. An agent restart forgets them until the server sends them again."""

    # Tokens for gated models, by replica id (M3).
    tokens: dict[str, str] = field(default_factory=dict)
    # Each model replica's internal key, {replica id: (variable or None, key)} (M4).
    keys: dict[str, tuple[Optional[str], str]] = field(default_factory=dict)
    # 1.3: replicas of workloads exposed at /x/ (`replica_key.strip`): their guard removes the internal key
    # before a request reaches the program, and their health checks never carry it.
    strip: set[str] = field(default_factory=set)


@dataclass
class Fetches:
    """Downloads and image pulls in flight, by replica id."""

    # Their cancel switches (set when the desired list no longer wants the replica, or the agent stops).
    cancels: dict[str, threading.Event] = field(default_factory=dict)
    # Why one was cancelled ("cancelled: …"), for the replica's last report.
    cancelled: dict[str, str] = field(default_factory=dict)
    # 1.4 ("relay"): {"model"|"engine"|"image": {path, mode}} from the desired list, kept beside the record.
    relays: dict[str, dict] = field(default_factory=dict)
    # The replicas whose download comes through the server right now (their progress says so).
    via_server: set[str] = field(default_factory=set)


@dataclass
class Guarding:
    """The key guards in front of model replicas (M4): one per published port, and why one could not listen
    (which fails the replica's health)."""

    guards: dict[str, list[Guard]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class Operations:
    """The tasks in flight, by replica id: one operation at a time (start, stop, remove, …), a health probe,
    and the replicas being stopped (no start happens in a round while any stop runs)."""

    ops: dict[str, asyncio.Task] = field(default_factory=dict)
    probes: dict[str, asyncio.Task] = field(default_factory=dict)
    stopping: set[str] = field(default_factory=set)


@dataclass
class Outbox:
    """What the next `status` message says: the replicas that changed, the ones removed since, the last log
    lines of the ones that ended, and whether it lists every replica (after each new desired list)."""

    changed: set[str] = field(default_factory=set)
    removed: list[dict] = field(default_factory=list)
    tails: dict[str, str] = field(default_factory=dict)
    full_due: bool = False
    full_ready: bool = False
    report_event: asyncio.Event = field(default_factory=asyncio.Event)


class Alias:
    """An attribute of the supervisor kept under its old name, stored in one of its state structures: reading,
    assigning and changing it in place all reach the structure's field."""

    def __init__(self, part: str, name: Optional[str] = None):
        self.part = part
        self.name = name

    def __set_name__(self, owner, name: str) -> None:
        self.name = self.name or name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return getattr(getattr(obj, self.part), self.name)

    def __set__(self, obj, value) -> None:
        setattr(getattr(obj, self.part), self.name, value)
