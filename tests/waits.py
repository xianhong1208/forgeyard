"""Waiting for another thread, server or task to reach a state, instead of sleeping a fixed time.

A fixed sleep ("start it, sleep 0.2 s, it must be there by now") holds on a fast machine and fails on a
busy CI runner with two cores. These poll for the state itself and give up with a message after a
deadline that is generous on purpose: a passing test returns as soon as the state is reached, so a long
deadline costs nothing; only a failing one waits for it. See docs/development.md ("Writing tests that
do not flake").
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar, Union

T = TypeVar("T")


def wait_until(predicate: Callable[[], T], what: str, timeout: float = 10.0, interval: float = 0.01) -> T:
    """Call `predicate` until it returns something truthy and return that; fail naming `what` after
    `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        got = predicate()
        if got:
            return got
        assert time.monotonic() < deadline, f"gave up after {timeout} s waiting for {what}"
        time.sleep(interval)


async def until(predicate: Callable[[], Union[T, Awaitable[T]]], what: str, timeout: float = 10.0,
                interval: float = 0.01) -> T:
    """wait_until for async tests: polls on the event loop (so tasks on it keep running); `predicate`
    may be a coroutine function."""
    deadline = time.monotonic() + timeout
    while True:
        got = predicate()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return got
        assert time.monotonic() < deadline, f"gave up after {timeout} s waiting for {what}"
        await asyncio.sleep(interval)


class NoSleep:
    """A stand-in for the `time` module of one module under test: sleep() returns at once, everything else is the
    real time module. Patch that module's own attribute (`monkeypatch.setattr(mod, "time", NoSleep())`), never
    `time.sleep` or `time.monotonic` themselves: those are shared by every thread in the pytest session, so a
    background thread left by another test would spin, or use up a fake clock's values (#239)."""

    def sleep(self, seconds: float) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(time, name)
