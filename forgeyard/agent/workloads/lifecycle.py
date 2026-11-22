"""A replica's life on this machine (forgeyard/agent/workloads/supervisor.py, steps 2 to 4): started (ports
reserved, model and image fetched, its key guard first), ended (retry, restart or done, by its policy),
stopped at its max_duration, removed, and restarted behind the key guard. `Lifecycle` is that part of the
`Supervisor`."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pydantic import ValidationError

from forgeyard.agent.workloads import plan, ports as portlib, starts
from forgeyard.agent.workloads.records import Record
from forgeyard.workloads.spec import WorkloadSpec

log = logging.getLogger("forgeyard.agent")

LIVE = ("starting", "running", "healthy", "unhealthy")
TAIL_LINES = 100
TAIL_CHARS = 8000
TASK_BACKOFF_CAP = 300
SERVICE_BACKOFF_CAP = 60


class Lifecycle:
    """The `Supervisor`'s operations on one replica, each run as its task in `ops` (`_launch`)."""

    def _gate_blockers(self, rec: Record) -> list[Record]:
        """Other replicas still loading on one of this replica's GPUs."""
        keys = {g.get("key") for g in rec.gpus}
        if not keys:
            return []
        now = self.clock()
        out = []
        for other in self.records.values():
            if other is rec or not other.loading or other.state not in LIVE:
                continue
            period = int(((other.spec.get("health") or {}).get("start_period")) or 0)
            if other.started_at and now - other.started_at > period + 60:
                continue  # stuck loading: do not wait for it forever
            if keys & {g.get("key") for g in other.gpus}:
                out.append(other)
        return out

    # ------------------------------------------------------------------ operations
    @staticmethod
    def _guarded(rec: Record) -> bool:
        # key_env alone: a record written by an M4 agent, before the guard.
        return rec.guarded or rec.key_env is not None

    async def _start(self, rec: Record) -> None:
        """Start the replica; an unexpected error on the way (a bug, a missing module) ends this attempt with that
        error as its reason, shown in the console, and the usual backoff follows. Without this the replica was
        left "starting" with no process or container, and the next pass reported it as "ended while the agent was
        not watching": the real error was only in the agent's log."""
        try:
            await self._start_steps(rec)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - shown, never fatal
            log.exception("Replica %s of %s: the agent failed while starting it.", rec.id[:12], rec.workload)
            if rec.id in self.records and rec.state in LIVE:
                text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                await self._ended(rec, None, reason=f"the agent failed while starting it ({text})"[:300])

    async def _start_steps(self, rec: Record) -> None:
        rt = self.runtimes[rec.runtime]
        if self._guarded(rec) and rec.id not in self.keys:
            # Its internal key comes with the server's next `desired` (an agent restarted while the
            # server is away does not know it). Never start the engine without it: it would answer
            # anyone on its port.
            rec.state, rec.next_start_at = "backoff", self.clock() + 2
            reason = "waiting for the server (the engine's internal key is kept in memory only)"
            if rec.reason != reason:
                rec.reason = reason
                self._save(rec)
                self._notify(rec)
            return
        try:
            spec = WorkloadSpec.model_validate(rec.spec)
        except ValidationError as e:
            rec.state, rec.final = "failed", True
            rec.reason = f"this agent refuses the spec: {e.errors()[0].get('msg', 'invalid')}"[:400]
            self._save(rec)
            self._notify(rec)
            return
        now = self.clock()
        if rec.kind == "task":
            rec.attempt += 1
        elif rec.started_at is not None:
            rec.restarts += 1
        guard = self._guarded(rec)
        if spec.ports and (not rec.ports or (guard and not plan.guarded(rec.ports))):
            # On the event loop, before any await: starts run concurrently, and a port must be
            # reserved (in rec.ports) before the next start looks for a free one. A guarded replica
            # takes two per port: the published one (the guard's) and an internal one (the engine's,
            # on loopback; agent-local, never reported).
            reserved = [n for r in self.records.values() for p in r.ports for n in (p["host_port"], p.get("internal")) if n]
            fresh = not rec.ports
            try:
                need = (len(spec.ports) if fresh else 0) + (len(spec.ports) if guard else 0)
                got = portlib.allocate(need, self.port_range, reserved, self.port_probe)
            except RuntimeError as e:
                await self._ended(rec, None, reason=str(e))
                return
            if fresh:
                hosts, got = got[:len(spec.ports)], got[len(spec.ports):]
                rec.ports = [{"name": p.name, "port": p.port, "host_port": h, "address": self.address}
                             for p, h in zip(spec.ports, hosts)]
            if guard:
                for p, internal in zip(rec.ports, got):
                    p["internal"] = internal
        rec.state = "starting"
        rec.reason = "checking the image" if rec.runtime == "container" else None
        rec.exit_code = None
        rec.started_at, rec.finished_at, rec.next_start_at = now, None, None
        rec.deadline_hit = rec.stop_requested = rec.healthy_seen = rec.loading = False
        rec.health_failures, rec.last_probe_at, rec.pid = 0, None, None
        starts.begin(rec, now)  # 1.5.0: how long this start takes, part by part
        self.tails.pop(rec.id, None)
        self._save(rec)
        self._notify(rec)
        if rec.model is not None or rec.engine is not None:
            rec.reason = "preparing the model"
            self._notify(rec)
            if not await self._prepare(rec):
                self._save(rec)
                self._notify(rec)
                return
            starts.part(rec, "model", self.clock())
            waited = False
            while self._gate_blockers(rec):
                if rec.id not in self.records or (self.desired is not None and rec.id not in self.desired):
                    return
                if not waited:
                    rec.reason = (f"waiting for {self._gate_blockers(rec)[0].workload} to finish loading on the "
                                  "shared GPU")
                    self._notify(rec)
                    waited = True
                await asyncio.sleep(self.gate_poll)
            if waited:
                starts.part(rec, "waiting", self.clock())
            rec.loading = bool(rec.gpus) and bool(spec.health)
            rec.reason = "checking the image" if rec.runtime == "container" else None
            rec.started_at = self.clock()
            self._save(rec)
            self._notify(rec)
        if hasattr(rt, "pull"):
            if not await self._pull(rec, rt):
                return
            if rec.id not in self.records:
                return
            starts.part(rec, "image", self.clock())
        await self._engine_cache_dir(rec)  # 1.5.0: a container's compile cache kept between starts
        if guard and rec.ports:
            # The guard first: if the published port cannot be held, the engine is not started.
            problem = await self._open_guard(rec)
            if problem:
                await self._ended(rec, None, reason=problem)
                return
        try:
            key = self.keys.get(rec.id) if guard else None
            if key is not None and key[0]:
                await asyncio.to_thread(rt.start, rec, self.address, {key[0]: key[1]})
            else:
                await asyncio.to_thread(rt.start, rec, self.address)
        except Exception as e:  # StartError, and anything unexpected: shown, never fatal
            reason = str(e) or type(e).__name__
            log.warning("Replica %s of %s could not start: %s", rec.id[:12], rec.workload, reason)
            await self._ended(rec, None, reason=reason[:300])
            return
        self.started += 1
        if rec.id not in self.records:
            return
        rec.state = "starting" if spec.health else "running"
        rec.reason = "waiting for the health check" if spec.health else None
        rec.started_at = self.clock()
        if not spec.health:
            starts.finish(rec, rec.started_at)  # ready once it runs
        self._save(rec)
        self._notify(rec)
        log.info("Started replica %s of %s (%s).", rec.id[:12], rec.workload,
                 f"pid {rec.pid}" if rec.runtime == "process" else rec.container)

    async def _ended(self, rec: Record, code: Optional[int], *, reason: Optional[str] = None,
                     unknown: bool = False) -> None:
        """The replica's main process or container ended: decide what happens next."""
        rt = self.runtimes[rec.runtime]
        rec.loading = False
        await asyncio.to_thread(rt.cleanup, rec)
        now = self.clock()
        spec = rec.spec
        rec.finished_at = now
        rec.exit_code = code
        rec.pid = None if rec.runtime == "process" else rec.pid
        if reason:
            what = reason
        elif code is not None:
            what = f"exited with code {code}"
        else:
            what = "ended while the agent was not watching (exit code unknown)" if unknown else "ended"
        failed = bool(reason) or code != 0
        if rec.kind == "task":
            retries = int(spec.get("retries") or 0)
            if rec.deadline_hit:
                rec.state, rec.final = "failed", True
                rec.reason = f"ran longer than max_duration ({spec.get('max_duration')} s) and was stopped"
            elif not failed:
                rec.state, rec.final, rec.reason = "exited", True, None
            elif rec.attempt <= retries:
                delay = min(TASK_BACKOFF_CAP, int(spec.get("retry_backoff") or 5) * 2 ** (rec.attempt - 1))
                rec.state, rec.next_start_at = "backoff", now + delay
                rec.reason = f"{what}; retry {rec.attempt} of {retries} in {delay} s"
            else:
                rec.state, rec.final = "failed", True
                rec.reason = what + (f" (attempt {rec.attempt} of {retries + 1})" if retries else "")
        else:
            policy = spec.get("restart") or "always"
            if policy == "always" or (policy == "on-failure" and failed):
                ran = now - (rec.started_at or now)
                delay = 1 if ran >= 60 else min(SERVICE_BACKOFF_CAP, 2 ** min(rec.restarts, 6))
                rec.state, rec.next_start_at = "backoff", now + delay
                rec.reason = f"{what}; restarting in {delay} s"
            else:
                rec.state, rec.final = ("failed" if failed else "exited"), True
                rec.reason = what if failed else None
        if rec.final:
            await self._close_guard(rec.id)
            text, _ = await asyncio.to_thread(rt.tail, rec, TAIL_LINES)
            self.tails[rec.id] = text[-TAIL_CHARS:]
            log.info("Replica %s of %s ended: %s.", rec.id[:12], rec.workload, rec.state + (f" ({rec.reason})" if rec.reason else ""))
        self._save(rec)
        self._notify(rec)

    async def _deadline(self, rec: Record) -> None:
        try:
            await self._deadline_inner(rec)
        finally:
            self.stopping.discard(rec.id)

    async def _deadline_inner(self, rec: Record) -> None:
        rt = self.runtimes[rec.runtime]
        rec.deadline_hit = rec.stop_requested = True
        rec.reason = f"max_duration ({rec.spec.get('max_duration')} s) reached; stopping"
        self._save(rec)
        self._notify(rec)
        await asyncio.to_thread(rt.stop, rec, self.stop_grace)
        rec.stop_requested = False
        obs = await asyncio.to_thread(rt.observe, rec)
        await self._ended(rec, obs.exit_code if not obs.alive else None)

    async def _remove(self, rec: Record) -> None:
        try:
            await self._remove_inner(rec)
        finally:
            self.stopping.discard(rec.id)
            if self.desired is None or rec.id not in self.desired:
                # Its key and token go with it. Not when the desired list wants the same replica id in
                # a new generation (an edit restarts it): what set_desired stored is that generation's,
                # and dropping it left the new replica waiting for the server for good.
                self.tokens.pop(rec.id, None)
                self.keys.pop(rec.id, None)
                self.strip.discard(rec.id)

    async def _remove_inner(self, rec: Record) -> None:
        rt = self.runtimes[rec.runtime]
        await self._close_guard(rec.id)
        was_running = rec.state in LIVE
        if was_running or rec.state == "backoff":
            rec.stop_requested = True
            self._save(rec)
            if was_running:
                await asyncio.to_thread(rt.stop, rec, self.stop_grace)
            await asyncio.to_thread(rt.cleanup, rec)
        tail = None
        if not rec.final:
            text, _ = await asyncio.to_thread(rt.tail, rec, TAIL_LINES)
            tail = text[-TAIL_CHARS:]
        await asyncio.to_thread(rt.remove, rec)
        self.records.pop(rec.id, None)
        self.store.delete(rec.id)
        self.changed.discard(rec.id)
        self.tails.pop(rec.id, None)
        probe = self.probes.pop(rec.id, None)
        if probe is not None:
            probe.cancel()
        cancelled = self.cancelled.pop(rec.id, None)
        if not rec.final:
            entry = rec.public(with_tail=tail)
            entry.update(state="stopped", reason=cancelled, pid=None, finished_at=self.clock())
            entry.pop("download", None)
            self.removed.append(entry)
            self._notify()
        log.info("Stopped and removed replica %s of %s.", rec.id[:12], rec.workload)

    async def _reguard(self, rec: Record) -> None:
        try:
            rt = self.runtimes[rec.runtime]
            log.info("Replica %s of %s was started without the key guard; restarting it behind one.",
                     rec.id[:12], rec.workload)
            rec.stop_requested = True
            rec.reason = "restarting behind the agent's key guard"
            self._save(rec)
            self._notify(rec)
            await asyncio.to_thread(rt.stop, rec, self.stop_grace)
            await asyncio.to_thread(rt.cleanup, rec)
            rec.stop_requested = rec.loading = False
            rec.state, rec.next_start_at = "pending", None
            rec.pid = None if rec.runtime == "process" else rec.pid
            self._save(rec)
            self._notify(rec)
        finally:
            self.stopping.discard(rec.id)

    async def _exposed(self, rec: Record, where: list[str]) -> None:
        try:
            rt = self.runtimes[rec.runtime]
            await self._close_guard(rec.id)
            rec.stop_requested = True
            self._save(rec)
            await asyncio.to_thread(rt.stop, rec, self.stop_grace)
            await asyncio.to_thread(rt.cleanup, rec)
            rec.stop_requested = rec.loading = False
            rec.state, rec.final, rec.finished_at = "failed", True, self.clock()
            rec.pid = None
            rec.reason = (f"the engine listens on {', '.join(where)}, not only on loopback, so it could be used "
                          "around the key guard; it was stopped (its recipe must bind {host})")[:400]
            log.warning("Replica %s of %s: %s.", rec.id[:12], rec.workload, rec.reason)
            text, _ = await asyncio.to_thread(rt.tail, rec, TAIL_LINES)
            self.tails[rec.id] = text[-TAIL_CHARS:]
            self._save(rec)
            self._notify(rec)
        finally:
            self.stopping.discard(rec.id)
