"""Offline detection on the server.

A machine is online while heartbeats arrive. Waiting for the socket to close is not enough:
a pulled cable or a frozen agent leaves a half-open TCP connection that can look alive for
minutes. So a background task sweeps every couple of seconds and marks offline any machine
with nothing heard for AGENT_OFFLINE_SECONDS (30 s), and closes its connection (code 4008)
so the agent, if it is still there, reconnects with a fresh inventory.

1.4.5 (#164): a machine offline for AGENT_OFFLINE_MOVE_SECONDS (10 minutes) has the replicas of its unpinned
deployments placed again elsewhere (forgeyard.workloads.offline), in the same sweep.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from forgeyard.config import get_settings
from forgeyard.db import Node, as_utc, new_session, utcnow
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import CLOSE_TIMEOUT

log = logging.getLogger("forgeyard.machines")

SWEEP_SECONDS = 2.0


def is_stale(last_heartbeat: Optional[datetime], now: datetime, offline_after: int) -> bool:
    return last_heartbeat is None or now - as_utc(last_heartbeat) >= timedelta(seconds=offline_after)


def sweep(db: DbSession, now: Optional[datetime] = None) -> list[int]:
    """Mark silent machines offline and close their connections. Returns their ids."""
    now = now or utcnow()
    offline_after = get_settings().agent_offline_seconds
    stale = [n for n in db.scalars(select(Node).where(Node.status == "online")).all()
             if is_stale(n.last_heartbeat_at, now, offline_after)]
    for node in stale:
        node.status = "offline"
        hub.close(node.id, CLOSE_TIMEOUT, "no heartbeat")
        log.info("Machine %r went offline: nothing heard for %s s.", node.name, offline_after)
    if stale:
        db.commit()
    return [n.id for n in stale]


def mark_all_offline(db: DbSession) -> None:
    """At startup no agent is connected yet, whatever the database says."""
    from forgeyard.workloads import offline

    db.execute(update(Node).where(Node.status == "online").values(status="offline"))
    db.commit()
    offline.server_started()  # 1.4.5 (#164): offline time, for moving replicas, counts from here at the earliest


def _sweep_once() -> None:
    from forgeyard.workloads import offline
    from forgeyard.workloads.placement import place_and_poke

    db = new_session()
    try:
        sweep(db)
        # 1.4.5 (#164): a machine offline for AGENT_OFFLINE_MOVE_SECONDS: its unpinned replicas are taken off it,
        # and placed elsewhere just below.
        offline.move_off(db)
        # Replicas waiting for room (a machine coming online, a card freed) are placed here
        # too, so nothing depends on one event having been seen.
        place_and_poke(db)
    finally:
        db.close()


async def run_forever(interval: float = SWEEP_SECONDS) -> None:
    while True:
        try:
            await asyncio.to_thread(_sweep_once)
        except Exception:  # keep sweeping whatever one round hit (a locked database, say)
            log.exception("Liveness sweep failed")
        await asyncio.sleep(interval)
