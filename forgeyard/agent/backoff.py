"""Reconnect delays: exponential, with jitter, never above the cap (15 s)."""

from __future__ import annotations

import random
from typing import Callable


class Backoff:
    def __init__(self, base: float = 1.0, cap: float = 15.0, factor: float = 2.0,
                 rng: Callable[[], float] = random.random):
        self.base, self.cap, self.factor, self.rng = base, cap, factor, rng
        self.attempt = 0

    def next(self) -> float:
        """Between half and all of min(cap, base * factor**attempt) ("equal jitter"), so a fleet
        that lost the server at the same moment does not come back in lockstep."""
        ceiling = min(self.cap, self.base * self.factor ** min(self.attempt, 32))
        self.attempt += 1
        return ceiling / 2 + self.rng() * ceiling / 2

    def reset(self) -> None:
        self.attempt = 0
