"""What every runtime offers the supervisor.

A runtime runs replicas one way: as processes (process.py) or as containers (container.py). The supervisor
holds them by name (`{"process": …, "container": …}`) and calls them only through `Runtime` (1.4.5, #173), each
call in a thread: they block. The container runtime also offers `ContainerEngine`: the engine's images, which
the supervisor, the downloads ahead of a deployment and the agent's image requests use (they look for these
methods before calling them, so a runtime without them simply has no images)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Callable, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from forgeyard.agent.workloads.records import Record


class StartError(Exception):
    """The replica could not be started; the message says why (shown in the UI)."""


@dataclass
class Observation:
    alive: bool
    exit_code: Optional[int] = None   # when not alive; None when it cannot be known
    missing: bool = False             # the container or process is gone without a trace


@runtime_checkable
class Runtime(Protocol):
    """A way to run replicas, as the supervisor uses it."""

    def info(self) -> dict:
        """What it can do here; `ok` False when it cannot run anything (reported to the server)."""

    def start(self, rec: Record, address: str, secret: Optional[dict[str, str]] = None) -> None:
        """Start the replica (its ports bound to `address`; `secret` is its engine's key variable). Raises
        StartError, saying why."""

    def observe(self, rec: Record) -> Observation:
        """Whether it still runs, or how it ended."""

    def stop(self, rec: Record, grace: float) -> None:
        """Stop it: politely, then after `grace` seconds for good."""

    def cleanup(self, rec: Record) -> None:
        """What is left once it ended (a container, a process group's leftovers)."""

    def remove(self, rec: Record) -> None:
        """Everything of it on this machine, once nobody wants it."""

    def tail(self, rec: Record, lines: int) -> tuple[str, bool]:
        """Its last log lines, and whether they were cut."""

    def trim(self, rec: Record, max_bytes: int) -> None:
        """Keep its log under `max_bytes`."""

    def owned(self) -> dict[str, dict]:
        """What runs here from before an agent restart, by replica id (for adopting it)."""


@runtime_checkable
class ContainerEngine(Protocol):
    """The images of a container engine (the "container" runtime)."""

    def pull(self, rec: Record, progress=None, cancel: Optional[threading.Event] = None, relay=None) -> None:
        """The replica's image onto this machine, with progress (before `start`)."""

    def ensure_image(self, image: str, check: Optional[dict], progress=None,
                     cancel: Optional[threading.Event] = None, *, who: str = "a download",
                     relay=None) -> tuple[str, str]:
        """An image onto this machine (what to run, what was found), for a download ahead of a deployment."""

    def remove_name(self, name: str) -> None:
        """A container of this agent's that nothing wants any more."""

    def refers(self, entry: dict, image: Optional[str], check: Optional[dict] = None) -> bool:
        """Whether a listed image is the one `image` (with its recorded ids) names."""

    def list_images(self) -> list[dict]: ...

    def agent_image(self) -> Optional[str]: ...

    def disk(self) -> dict: ...

    def save_stream(self, image_id: str, cancel: threading.Event): ...

    def load(self, write: Callable[[BinaryIO], None], cancel: Optional[threading.Event] = None) -> list[str]: ...

    def load_upload(self, opened, size: Optional[int], cancel: Optional[threading.Event] = None) -> list[dict]: ...

    def remove_image(self, image_id: str, users: Callable[[dict], list[str]] = lambda entry: []) -> dict: ...
