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

Model replicas (M4) sit behind the agent's key guard (guard.py): the engine listens on loopback
only, on a port only the agent knows, and the guard listens on the replica's published address and
lets through only requests that carry the replica's internal key. The guard lives in the agent: it
is opened when the key is known (it comes with `desired`, in memory only), so after an agent restart
an adopted replica is unreachable from outside until the server has sent its key again.

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

from forgeyard.agent.workloads import health, plan, ports as portlib
from forgeyard.agent.workloads.guard import Guard
from forgeyard.agent.workloads.process import beyond_loopback, find_leader
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.machines.protocol import PROTOCOL_VERSION
from forgeyard.workloads import images
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
        # M4: each model replica's internal key, {replica id: (variable or None, key)}, from
        # `desired`. In memory only: never in a record, a log line or a status message. The guards in
        # front of those replicas ({replica id: one per port}) and why one could not listen.
        self.keys: dict[str, tuple[Optional[str], str]] = {}
        self.guards: dict[str, list[Guard]] = {}
        self.guard_errors: dict[str, str] = {}
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

    def new_connection(self) -> None:
        """Revisions order the desired lists of one connection: a new one starts its own order."""
        self.revision = -1

    def set_desired(self, msg: dict) -> None:
        revision = int(msg.get("revision") or 0)
        if revision and revision < self.revision:
            log.info("Ignored an older desired state (revision %d, after %d).", revision, self.revision)
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
                env = key.get("env") if isinstance(key, dict) else None
                if isinstance(key, dict) and key.get("value") and (env is None or isinstance(env, str)):
                    # env None: the engine takes no key of its own; the guard alone requires it.
                    self.keys[r["id"]] = (env or None, str(key["value"]))
                    r["key_env"] = env or None
                    r["guarded"] = True
            if "image_ids" in r:
                r = {**r, "image_ids": images.parse_check(r["image_ids"])}
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
        """The agent stops: operations end, and the guards close (their replicas are unreachable
        from outside until an agent runs again: the safe direction)."""
        tasks = list(self.ops.values()) + list(self.probes.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for rid in list(self.guards):
            await self._close_guard(rid)

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

        await self._sync_guards()
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
                continue
            if want.get("guarded") and not rec.guarded:
                rec.guarded = True
                self._save(rec)
            if want.get("image_ids") and want["image_ids"] != rec.image_ids:
                rec.image_ids = want["image_ids"]  # a record from before the server or agent knew them
                self._save(rec)
            if (self._guarded(rec) and rec.id in self.keys and rec.state in LIVE and rec.ports
                    and not plan.guarded(rec.ports)):
                # Started by an agent without the guard: its engine listens on the published port
                # itself. Restart it behind a guard (once its key is known, so it is not left down).
                self.stopping.add(rec.id)
                self._launch(rec.id, self._reguard(rec))
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
            rec.guarded = bool(want.get("guarded"))
            rec.image_ids = want.get("image_ids")
            if rec.runtime not in self.runtimes:
                rec.state, rec.final, rec.reason = "failed", True, f"this agent has no {rec.runtime} runtime"
            elif want.get("model") is not None or want.get("engine") is not None:
                self._model_record(rec, want)
            owned = self._owned.pop(rid, None)
            if owned and rec.guarded and not plan.guarded(owned["ports"]):
                owned = dict(owned, generation=None)  # started without the guard: replaced, not adopted
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
    @staticmethod
    def _guarded(rec: Record) -> bool:
        # key_env alone: a record written by an M4 agent, before the guard.
        return rec.guarded or rec.key_env is not None

    async def _start(self, rec: Record) -> None:
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
            self.tokens.pop(rec.id, None)
            self.keys.pop(rec.id, None)

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
        if self._guarded(rec) and rec.id not in self.keys:
            return  # adopted after an agent restart; probed again once the server sent its key
        rec.last_probe_at = now
        if port.get("internal"):
            # Behind the guard: the engine itself, on loopback. The guard has nothing open, not even
            # the health path; that it listens is known here without asking it.
            host, number = plan.LOOPBACK, int(port["internal"])
        elif self.address in ("0.0.0.0", "::") or (rec.runtime == "process" and rec.model is None):
            # Processes usually listen on every address (probe loopback); a model engine run as a
            # process binds only the agent's address, like a container's published port.
            host, number = "127.0.0.1", int(port["host_port"])
        else:
            host, number = self.address, int(port["host_port"])
        self.probes[rec.id] = asyncio.create_task(self._probe(rec, h, host, number))

    async def _probe(self, rec: Record, h: dict, host: str, port: int) -> None:
        args = (h.get("type", "http"), host, port, h.get("path") or "/", float(h.get("timeout") or 3))
        key = self.keys.get(rec.id) if self._guarded(rec) else None
        if key is not None:  # speaches guards /health too; vLLM and llama.cpp ignore it there
            err = await asyncio.to_thread(self.prober, *args, headers={"Authorization": f"Bearer {key[1]}"})
        else:
            err = await asyncio.to_thread(self.prober, *args)
        if self.records.get(rec.id) is not rec or rec.state not in LIVE or rec.id in self.ops:
            return
        if err is None and rec.runtime == "process" and plan.guarded(rec.ports):
            open_on = await asyncio.to_thread(beyond_loopback, [int(p["internal"]) for p in rec.ports])
            if open_on:
                # The engine ignored ${BIND_ADDRESS} and listens beyond loopback: it could be used
                # around the guard. Stopped, never served.
                self.stopping.add(rec.id)
                self._launch(rec.id, self._exposed(rec, open_on))
                return
        if err is None and self._guarded(rec) and rec.id in self.guard_errors:
            err = self.guard_errors[rec.id]
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

    # ------------------------------------------------------------------ the key guard
    async def _open_guard(self, rec: Record) -> Optional[str]:
        """Make sure a guard listens on each of the replica's published ports, with its current key.
        None when they do, else why not (also kept in guard_errors, which fails its health)."""
        key = self.keys.get(rec.id)
        guards = self.guards.get(rec.id)
        if guards is not None:
            for g in guards:
                if not g.has_key(key[1] if key else None):
                    g.set_key(key[1] if key else None)
            return None
        opened: list[Guard] = []
        for p in rec.ports:
            if not p.get("internal"):
                continue
            g = Guard(listen=(p.get("address") or self.address, int(p["host_port"])),
                      upstream=(plan.LOOPBACK, int(p["internal"])), key=key[1] if key else None,
                      name=f"{rec.workload} #{rec.ordinal}")
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
