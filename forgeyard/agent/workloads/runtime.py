"""What every runtime offers the supervisor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class StartError(Exception):
    """The replica could not be started; the message says why (shown in the UI)."""


@dataclass
class Observation:
    alive: bool
    exit_code: Optional[int] = None   # when not alive; None when it cannot be known
    missing: bool = False             # the container or process is gone without a trace
