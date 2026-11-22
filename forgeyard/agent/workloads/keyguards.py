"""The key guards in front of model replicas (forgeyard/agent/workloads/supervisor.py, M4): one per published
port, holding the replica's internal key, serving TLS when the server dials it (1.4.5), opened again after an
agent restart once the server has sent the key. `KeyGuards` is that part of the `Supervisor`."""

from __future__ import annotations

import logging
from typing import Optional

from forgeyard.agent.workloads import plan
from forgeyard.agent.workloads.guard import Guard
from forgeyard.agent.workloads.records import Record

log = logging.getLogger("forgeyard.agent")


class KeyGuards:
    """The `Supervisor`'s key guards, in `guards`, and why one could not listen, in `guard_errors`."""

    # ------------------------------------------------------------------ the key guard
    async def _open_guard(self, rec: Record) -> Optional[str]:
        """Make sure a guard listens on each of the replica's published ports, with its current key.
        None when they do, else why not (also kept in guard_errors, which fails its health)."""
        key = self.keys.get(rec.id)
        guards = self.guards.get(rec.id)
        if guards is not None and any(g.tls != self.tls_required for g in guards):
            # 1.4.5: the server now reaches replicas the other way (it was updated): listen again that way. Only
            # at a new connection to a server of another kind, so nothing of the old one is still in flight.
            log.info("Replica %s of %s: its key guard now serves %s.", rec.id[:12], rec.workload,
                     "TLS" if self.tls_required else "plain HTTP")
            for g in self.guards.pop(rec.id, []):
                await g.close()
            guards = None
        if guards is not None:
            for g in guards:
                if not g.has_key(key[1] if key else None):
                    g.set_key(key[1] if key else None)
                g.strip_auth = rec.id in self.strip
            return None
        opened: list[Guard] = []
        for p in rec.ports:
            if not p.get("internal"):
                continue
            g = Guard(listen=(p.get("address") or self.address, int(p["host_port"])),
                      upstream=(plan.LOOPBACK, int(p["internal"])), key=key[1] if key else None,
                      name=f"{rec.workload} #{rec.ordinal}", strip_auth=rec.id in self.strip,
                      ssl_context=self.tls.context if self.tls_required else None)
            try:
                await g.start()
            except OSError as e:
                for o in opened:
                    await o.close()
                problem = (f"the agent cannot listen on {g.listen[0]}:{g.listen[1]} for the replica's key guard "
                           f"({e.strerror or e})")
                if self.guard_errors.get(rec.id) != problem:
                    log.warning("Replica %s of %s: %s.", rec.id[:12], rec.workload, problem)
                self.guard_errors[rec.id] = problem
                return problem
            opened.append(g)
        self.guards[rec.id] = opened
        if self.guard_errors.pop(rec.id, None) is not None or opened:
            log.info("Key guard of replica %s of %s listens on %s.", rec.id[:12], rec.workload,
                     ", ".join(f"{g.listen[0]}:{g.listen[1]}" for g in opened))
        return None

    async def _close_guard(self, rec_id: str) -> None:
        self.guard_errors.pop(rec_id, None)
        for g in self.guards.pop(rec_id, []):
            await g.close()

    async def _sync_guards(self) -> None:
        """A guard for every guarded replica that has its key and is not over; none for the rest.
        Adoption after an agent restart comes down to this: the engine kept running, and its guard is
        opened again as soon as the server has sent the key."""
        for rec in list(self.records.values()):
            want = (self._guarded(rec) and rec.id in self.keys and not rec.final and plan.guarded(rec.ports)
                    and rec.id not in self.stopping)
            if want:
                await self._open_guard(rec)
            elif rec.id in self.guards:  # over, or being stopped and removed
                await self._close_guard(rec.id)
        for rid in [r for r in self.guards if r not in self.records]:
            await self._close_guard(rid)
