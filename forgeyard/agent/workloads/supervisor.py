"""The agent's reconciler: converge what runs on this machine on what the server wants.

The server sends the full desired list (replica id, generation, spec, GPUs). Every second, and
whenever that list changes, the supervisor:

1. looks at what runs (adopting, after a restart, the processes and labelled containers it
   started before: the same pids, never a second copy);
2. stops and removes what is not wanted any more (or wanted in another generation);
3. starts what is wanted and not running — only once every stop of this round has finished, so
   a GPU handed from one replica to another is never used by both;
4. applies the policies: task retries with backoff, max_duration, service restarts, health;
5. reports every change in a `status` message (all replicas after each new desired list).

Model replicas (M3) first get their model (and, for a process variant, their engine) into the
machine's model cache, reporting download progress; a download nobody wants any more is
cancelled. Replicas that share a GPU are launched one at a time: the next starts once the one
before has loaded (passed its health check), so two engines never size themselves against the
same free memory at once.

Everything is idempotent: running it again with the same desired list changes nothing, and an
agent restart or a reconnect simply converges again. Until the first desired list arrives
after a start, the agent keeps watching what it adopted but starts and stops nothing new.

Stopping the agent leaves workloads running. They are stopped from the server.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from pydantic import ValidationError

from forgeyard.agent.workloads import health, ports as portlib
from forgeyard.agent.workloads.process import find_leader
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.machines.protocol import PROTOCOL_VERSION
from forgeyard.workloads.spec import WorkloadSpec

log = logging.getLogger("forgeyard.agent")

LIVE = ("starting", "running", "healthy", "unhealthy")
TAIL_LINES = 100
TAIL_CHARS = 8000
TASK_BACKOFF_CAP = 300
SERVICE_BACKOFF_CAP = 60


class Supervisor:
    def __init__(self, state_dir: Path, runtimes: dict, *, port_range: tuple[int, int] = (30000, 32767),
                 log_max_bytes: int = 10 * 1024**2, stop_grace: float = 10.0, tick: float = 1.0,
                 clock: Callable[[], float] = time.time, port_probe=portlib.bindable, prober=health.probe,
                 bind_address: Optional[str] = None, models=None, gate_poll: float = 1.0):
        self.store = RecordStore(state_dir)
        self.records: dict[str, Record] = self.store.load_all()
        self.runtimes = runtimes
        self.port_range = port_range
        self.log_max_bytes = log_max_bytes
        self.stop_grace = stop_grace
        self.tick = tick
        self.clock = clock
        self.port_probe = port_probe
        self.prober = prober
        self.address = bind_address or "127.0.0.1"
        self.bind_fixed = bind_address is not None
        self.desired: Optional[dict[str, dict]] = None
        self.revision = -1
        self.ops: dict[str, asyncio.Task] = {}
        self.probes: dict[str, asyncio.Task] = {}
        self.changed: set[str] = set()
        self.removed: list[dict] = []
        self.tails: dict[str, str] = {}
        self.full_due = False
        self.full_ready = False
        self.report_event = asyncio.Event()
        self._wake = asyncio.Event()
        self._owned: dict[str, dict] = {}
        self._orphans_swept = False
        self.started = 0  # replicas started by this agent process (tests and logs)
        # M3: the model cache (None: this agent serves no models), tokens for gated models (in
        # memory only), cancel switches for downloads in flight, and the stops in flight.
        self.models = models
        self.tokens: dict[str, str] = {}
        # M4: each model replica's internal key, {replica id: (variable, key)}, from `desired`. In
        # memory only: never in a record, a log line or a status message.
        self.keys: dict[str, tuple[str, str]] = {}
        self.cancels: dict[str, threading.Event] = {}
        self.stopping: set[str] = set()
        self.cache_dirty = models is not None
        self.gate_poll = gate_poll

    # ------------------------------------------------------------------ what the agent tells the server
    def runtime_info(self) -> dict:
        return {name: rt.info() for name, rt in self.runtimes.items()}

    def available(self) -> list[str]:
        return [name for name, rt in self.runtimes.items() if rt.info().get("ok")]

    def set_address(self, address: Optional[str]) -> None:
        if address and not self.bind_fixed:
            self.address = address

    def model_cache_info(self) -> Optional[dict]:
        return self.models.info() if self.models is not None else None

    def set_desired(self, msg: dict) -> None:
        revision = int(msg.get("revision") or 0)
        if revision and revision < self.revision:
            return  # an older list, overtaken
        self.revision = revision
        desired = {}
        for r in msg.get("replicas") or []:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            model = r.get("model")
            if isinstance(model, dict) and model.get("token"):
                self.tokens[r["id"]] = str(model["token"])  # kept in memory only, never in a record
                r = {**r, "model": {k: v for k, v in model.items() if k != "token"}}
            key = r.get("replica_key")
            if "replica_key" in r:
                r = {k: v for k, v in r.items() if k != "replica_key"}
                if isinstance(key, dict) and isinstance(key.get("env"), str) and key.get("value"):
                    self.keys[r["id"]] = (key["env"], str(key["value"]))
                    r["key_env"] = key["env"]
            desired[r["id"]] = r
        self.desired = desired
        self.full_due = True
        self._wake.set()

    def _notify(self, rec: Optional[Record] = None) -> None:
        if rec is not None:
            self.changed.add(rec.id)
        self.report_event.set()

    def _public(self, rec: Record) -> dict:
        return rec.public(with_tail=self.tails.get(rec.id) if rec.final else None)

    def take_report(self) -> Optional[dict]:
        if self.full_ready:
            entries = [self._public(r) for r in self.records.values()] + self.removed
            full, self.full_ready = True, False
        elif self.changed or self.removed:
            entries = [self._public(self.records[i]) for i in sorted(self.changed) if i in self.records] + self.removed
            full = False
        else:
            return None
        self.changed.clear()
        self.removed = []
        msg = {"v": PROTOCOL_VERSION, "type": "status", "full": full, "replicas": entries}
        if self.cache_dirty and self.models is not None:
            self.cache_dirty = False
            msg["model_cache"] = self.models.info()
        return msg

    async def logs(self, replica_id: str, lines: int) -> dict:
        rec = self.records.get(replica_id)
        if rec is None:
            return {"ok": False, "reason": "This agent is not running that replica (any more)."}
        rt = self.runtimes[rec.runtime]
        text, truncated = await asyncio.to_thread(rt.tail, rec, max(1, min(int(lines), 2000)))
        return {"ok": True, "text": text, "truncated": truncated}

    # ------------------------------------------------------------------ the loop
    async def run(self) -> None:
        await self.adopt()
        while True:
            try:
                await self.reconcile_once()
            except Exception:  # one bad round must never stop the supervision of the rest
                log.exception("Reconcile round failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.tick)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        tasks = list(self.ops.values()) + list(self.probes.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def settle(self) -> None:
        """Wait for every operation in flight (tests)."""
        while self.ops or any(not p.done() for p in self.probes.values()):
            await asyncio.gather(*self.ops.values(), *self.probes.values(), return_exceptions=True)

    def _save(self, rec: Record) -> None:
        if rec.id in self.records:
            self.store.save(rec)

    def _launch(self, rec_id: str, coro) -> None:
        task = asyncio.create_task(coro)
        self.ops[rec_id] = task
        task.add_done_callback(lambda t, i=rec_id: self._op_done(i, t))

    def _op_done(self, rec_id: str, task: asyncio.Task) -> None:
        if self.ops.get(rec_id) is task:
            del self.ops[rec_id]
        if not task.cancelled() and task.exception() is not None:
            log.error("Replica %s: %s", rec_id, task.exception())
        self._wake.set()

    async def adopt(self) -> None:
        """After a start: find what is still running from before, and take it over."""
        container = self.runtimes.get("container")
        if container is not None:
            self._owned = await asyncio.to_thread(container.owned)
        for rec in list(self.records.values()):
            if rec.state not in LIVE:
                continue
            if rec.runtime == "process" and rec.pid is None:
                found = await asyncio.to_thread(find_leader, rec.id)
                if found:  # started just before the agent died, before the pid was saved
                    rec.pid, rec.pgid, rec.proc_start = found
            if rec.runtime == "container" and rec.container is None:
                owned = self._owned.get(rec.id)
                if owned and owned["generation"] == rec.generation:
                    rec.container = owned["name"]
            if (rec.runtime == "process" and rec.pid is None) or (rec.runtime == "container" and rec.container is None):
                rec.state, rec.reason = "pending", None  # never got going: start it again
                if rec.kind == "task":
                    rec.attempt = max(0, rec.attempt - 1)
                self._save(rec)
                continue
            obs = await asyncio.to_thread(self.runtimes[rec.runtime].observe, rec)
            if obs.alive:
                log.info("Adopted replica %s of %s (%s).", rec.id[:12], rec.workload,
                         f"pid {rec.pid}" if rec.runtime == "process" else rec.container)
            self.changed.add(rec.id)

    async def reconcile_once(self) -> None:
        now = self.clock()
        for rec in list(self.records.values()):
            if rec.id in self.ops or rec.state not in LIVE:
                continue
            rt = self.runtimes[rec.runtime]
            obs = await asyncio.to_thread(rt.observe, rec)
            if not obs.alive:
                await self._ended(rec, obs.exit_code, unknown=obs.exit_code is None)
                continue
            limit = rec.spec.get("max_duration")
            if rec.kind == "task" and limit and rec.started_at and now - rec.started_at >= limit:
                self.stopping.add(rec.id)
                self._launch(rec.id, self._deadline(rec))
                continue
            if rec.kind == "service" and rec.spec.get("health"):
                self._maybe_probe(rec, now)
            await asyncio.to_thread(rt.trim, rec, self.log_max_bytes)

        if self.desired is None:
            self._start_due(now, restarts_only=True)  # keep adopted services alive; nothing new yet
            return

        for rec in list(self.records.values()):
            want = self.desired.get(rec.id)
            unwanted = want is None or want.get("generation") != rec.generation
            if rec.id in self.ops:
                if unwanted and rec.id in self.cancels:
                    self.cancels[rec.id].set()  # a download nobody wants any more
                continue
            if unwanted:
                self.stopping.add(rec.id)
                self._launch(rec.id, self._remove(rec))
        if not self._orphans_swept:
            self._orphans_swept = True
            container = self.runtimes.get("container")
            for rid, owned in self._owned.items():
                if rid not in self.records and rid not in self.desired and container is not None:
                    log.info("Removing container %s: nothing wants replica %s any more.", owned["name"], rid[:12])
                    await asyncio.to_thread(container.remove_name, owned["name"])
        if self.stopping:
            return  # stops first; starts on a later round, once every stop has finished

        for rid, want in self.desired.items():
            if rid in self.records:
                continue
            rec = Record(id=rid, generation=int(want.get("generation") or 0), workload=str(want.get("workload")),
                         ordinal=int(want.get("ordinal") or 0), kind=str(want.get("kind")),
                         runtime=str(((want.get("spec") or {}).get("runtime") or {}).get("type")),
                         spec=want.get("spec") or {}, gpus=list(want.get("gpus") or []))
            rec.key_env = want.get("key_env")
            if rec.runtime not in self.runtimes:
                rec.state, rec.final, rec.reason = "failed", True, f"this agent has no {rec.runtime} runtime"
            elif want.get("model") is not None or want.get("engine") is not None:
                self._model_record(rec, want)
            owned = self._owned.pop(rid, None)
            if owned and owned["generation"] == rec.generation and rec.runtime == "container":
                rec.container, rec.ports, rec.state = owned["name"], owned["ports"], "running"
                rec.started_at = now
                log.info("Adopted container %s for replica %s.", owned["name"], rid[:12])
            elif owned:
                await asyncio.to_thread(self.runtimes["container"].remove_name, owned["name"])
            self.records[rid] = rec
            self._save(rec)
            self._notify(rec)
        self._start_due(now)
        if self.full_due:
            self.full_due = False
            self.full_ready = True
            self._notify()

    def _start_due(self, now: float, restarts_only: bool = False) -> None:
        for rec in list(self.records.values()):
            if rec.id in self.ops or rec.final:
                continue
            if (rec.state == "pending" and not restarts_only) or (
                    rec.state == "backoff" and (rec.next_start_at or 0) <= now):
                self._launch(rec.id, self._start(rec))

    def _model_record(self, rec: Record, want: dict) -> None:
        from forgeyard.agent.models import CacheError, validate_model
        from forgeyard.models.sources import SourceError

        if self.models is None:
            rec.state, rec.final = "failed", True
            rec.reason = "this agent has no model cache (start it with --model-cache DIR)"
            return
        try:
            if want.get("model") is not None:
                rec.model = validate_model(want["model"], self.models.local_dirs)
            if want.get("engine") is not None:
                rec.engine = dict(want["engine"])
        except (CacheError, SourceError, ValueError, TypeError) as e:
            rec.state, rec.final, rec.reason = "failed", True, f"refused the model: {e}"[:400]

    def _progress(self, rec_id: str, phase: str, done: int, total: int, file: Optional[str]) -> None:
        rec = self.records.get(rec_id)
        if rec is None:
            return
        rec.download = {"phase": phase, "done": done, "total": total, **({"file": file} if file else {})}
        what = "the model" if phase == "model" else "the engine"
        if total:
            rec.reason = f"downloading {what}: {done / 1024**3:.2f} of {total / 1024**3:.2f} GiB ({100 * done // total}%)"
        else:
            rec.reason = f"downloading {what}: {done / 1024**2:.0f} MiB"
        self._notify(rec)

    async def _prepare(self, rec: Record) -> bool:
        """Model and engine into the cache. False when it cannot (the replica has ended) or was cancelled."""
        from forgeyard.agent.models import CacheError, Cancelled
        from forgeyard.models.sources import cache_key

        loop = asyncio.get_running_loop()
        cancel = self.cancels.setdefault(rec.id, threading.Event())

        def progress(phase, done, total, file):
            loop.call_soon_threadsafe(self._progress, rec.id, phase, done, total, file)

        try:
            if rec.engine is not None:
                exe = await asyncio.to_thread(self.models.ensure_engine, rec.engine, progress=progress, cancel=cancel)
                rec.engine_path = str(exe)
            if rec.model is not None:
                if not self.models.is_cached(rec.model) and self.models.busy(cache_key(rec.model)):
                    rec.reason = "waiting for another replica's download of the same model"
                    self._notify(rec)
                rec.paths = await asyncio.to_thread(self.models.ensure_model, rec.model, token=self.tokens.get(rec.id),
                                                    progress=progress, cancel=cancel)
        except Cancelled:
            rec.state, rec.reason, rec.download = "pending", None, None
            return False
        except CacheError as e:
            rec.download = None
            await self._ended(rec, None, reason=str(e)[:300])
            return False
        finally:
            self.cancels.pop(rec.id, None)
            self.cache_dirty = True
        rec.download = None
        return True

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
    async def _start(self, rec: Record) -> None:
        rt = self.runtimes[rec.runtime]
        if rec.key_env and rec.id not in self.keys:
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
        if spec.ports and not rec.ports:
            # On the event loop, before any await: starts run concurrently, and a port must be
            # reserved (in rec.ports) before the next start looks for a free one.
            reserved = [p["host_port"] for r in self.records.values() for p in r.ports]
            try:
                hosts = portlib.allocate(len(spec.ports), self.port_range, reserved, self.port_probe)
            except RuntimeError as e:
                await self._ended(rec, None, reason=str(e))
                return
            rec.ports = [{"name": p.name, "port": p.port, "host_port": h, "address": self.address}
                         for p, h in zip(spec.ports, hosts)]
        rec.state = "starting"
        rec.reason = "pulling the image and starting the container" if rec.runtime == "container" else None
        rec.exit_code = None
        rec.started_at, rec.finished_at, rec.next_start_at = now, None, None
        rec.deadline_hit = rec.stop_requested = rec.healthy_seen = rec.loading = False
        rec.health_failures, rec.last_probe_at, rec.pid = 0, None, None
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
            rec.loading = bool(rec.gpus) and bool(spec.health)
            rec.reason = "pulling the image and starting the container" if rec.runtime == "container" else None
            rec.started_at = self.clock()
            self._save(rec)
            self._notify(rec)
        try:
            key = self.keys.get(rec.id) if rec.key_env else None
            if key is not None:
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
            self.tokens.pop(rec.id, None)
            self.keys.pop(rec.id, None)

    async def _remove_inner(self, rec: Record) -> None:
        rt = self.runtimes[rec.runtime]
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
        if not rec.final:
            entry = rec.public(with_tail=tail)
            entry.update(state="stopped", reason=None, pid=None, finished_at=self.clock())
            self.removed.append(entry)
            self._notify()
        log.info("Stopped and removed replica %s of %s.", rec.id[:12], rec.workload)

    # ------------------------------------------------------------------ health
    def _maybe_probe(self, rec: Record, now: float) -> None:
        h = rec.spec["health"]
        running = self.probes.get(rec.id)
        if running is not None and not running.done():
            return
        if rec.started_at and now - rec.started_at < int(h.get("initial_delay") or 0):
            return
        if rec.last_probe_at and now - rec.last_probe_at < int(h.get("interval") or 10):
            return
        port = next((p for p in rec.ports if p["name"] == h.get("port")), rec.ports[0] if rec.ports else None)
        if port is None:
            return
        if rec.key_env and rec.id not in self.keys:
            return  # adopted after an agent restart; probed again once the server sent its key
        rec.last_probe_at = now
        # Processes usually listen on every address (probe loopback); a model engine run as a process
        # binds only the agent's address, like a container's published port.
        if self.address in ("0.0.0.0", "::") or (rec.runtime == "process" and rec.model is None):
            host = "127.0.0.1"
        else:
            host = self.address
        self.probes[rec.id] = asyncio.create_task(self._probe(rec, h, host, int(port["host_port"])))

    async def _probe(self, rec: Record, h: dict, host: str, port: int) -> None:
        args = (h.get("type", "http"), host, port, h.get("path") or "/", float(h.get("timeout") or 3))
        key = self.keys.get(rec.id) if rec.key_env else None
        if key is not None:  # speaches guards /health too; vLLM and llama.cpp ignore it there
            err = await asyncio.to_thread(self.prober, *args, headers={"Authorization": f"Bearer {key[1]}"})
        else:
            err = await asyncio.to_thread(self.prober, *args)
        if self.records.get(rec.id) is not rec or rec.state not in LIVE or rec.id in self.ops:
            return
        before = (rec.state, rec.reason)
        if err is None:
            rec.health_failures = 0
            rec.healthy_seen = True
            rec.loading = False
            rec.state, rec.reason = "healthy", None
        elif (not rec.healthy_seen and rec.started_at
              and self.clock() - rec.started_at < int(h.get("start_period") or 0)):
            # Still within the start period (a model loading): failures do not count yet.
            rec.reason = "loading; waiting for the health check to pass"
        else:
            rec.health_failures += 1
            if rec.health_failures >= int(h.get("failure_threshold") or 3):
                rec.state, rec.reason = "unhealthy", err[:300]
                rec.loading = False
        if (rec.state, rec.reason) != before:
            self._save(rec)
            self._notify(rec)
