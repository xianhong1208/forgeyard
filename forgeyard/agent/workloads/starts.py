"""How long a replica's start takes, and how often its health is checked meanwhile (1.5.0, issue #37, "start_times").

A start is measured from the moment the agent begins it (`begin`, at the top of `_start_steps`) to its first healthy
answer (`finish`). Between them each step closes its part (`part`): the model and engine archive into the model
cache (`model`), waiting for another replica loading on a shared GPU (`waiting`), checking or pulling the image
(`image`); the rest, from launching the engine to the first healthy answer, is `engine`. The parts add up to the
total. The measurement lives in the record (`start_marks` while it runs, `start_report` once it is ready), so an
agent restarted in the middle still counts from the real beginning.

While a service starts, its health is checked every FAST_HEALTH_INTERVAL seconds instead of its `interval`, until
the first healthy answer, and only within its `start_period`: there failures do not count, so probing more often
can only notice readiness sooner, never declare a replica unhealthy sooner. A recipe's `interval` of 10 s left a
ready vLLM unnoticed for up to 10 s.
"""

from __future__ import annotations

from typing import Optional

from forgeyard.agent.workloads.records import Record
from forgeyard.machines.protocol import FAST_HEALTH_INTERVAL

PARTS = ("model", "waiting", "image", "engine")


def begin(rec: Record, now: float) -> None:
    rec.start_marks = {"began": now, "mark": now, "parts": {}}
    rec.start_report = None


def part(rec: Record, name: str, now: float) -> None:
    """Close the part `name`: the time since the last mark is added to it."""
    marks = rec.start_marks
    if not marks or name not in PARTS:
        return
    spent = max(0.0, now - float(marks.get("mark") or now))
    parts = marks.setdefault("parts", {})
    parts[name] = parts.get(name, 0.0) + spent
    marks["mark"] = now


def finish(rec: Record, now: float) -> Optional[dict]:
    """The replica answered healthy for the first time in this start: the measurement, kept as `start_report`."""
    marks = rec.start_marks
    if not marks:
        return None
    part(rec, "engine", now)
    total = max(0.0, now - float(marks.get("began") or now))
    rec.start_report = {"seconds": round(total, 1),
                        "phases": {k: round(v, 1) for k, v in (marks.get("parts") or {}).items() if k in PARTS}}
    rec.start_marks = None
    return rec.start_report


def probe_interval(rec: Record, health: dict, now: float, fast: bool) -> float:
    """Seconds between two health checks of `rec`: FAST_HEALTH_INTERVAL while it starts (`fast`: the server listed
    "start_times"), else the recipe's `interval`."""
    normal = float(int(health.get("interval") or 10))
    if not fast or rec.healthy_seen or not rec.started_at:
        return normal
    period = int(health.get("start_period") or 0)
    if now - rec.started_at >= period:
        return normal  # failures count from here: as many probes as before
    return FAST_HEALTH_INTERVAL  # (an interval is at least 1 s)
