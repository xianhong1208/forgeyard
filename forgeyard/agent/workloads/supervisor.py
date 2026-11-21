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
machine's model cache, reporting download progress. Container replicas then get their image, pulled
before `run` with progress per layer (1.3.1). A download or pull nobody wants any more is cancelled
the moment a desired list without it arrives, and the replica's last reason says so ("cancelled: the
deployment was removed"). Replicas that share a GPU are launched one at a time: the next starts once the one
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
import io
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from pydantic import ValidationError

from forgeyard.agent import relay
from forgeyard.agent.replica_tls import ReplicaTls
from forgeyard.agent.workloads import health, plan, ports as portlib
from forgeyard.agent.workloads.guard import Guard
from forgeyard.agent.workloads.process import beyond_loopback, find_leader
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.agent.workloads.prefetch import Prefetcher
from forgeyard.machines.protocol import FEATURE_GUARD_TLS, FEATURE_PREFETCH, FEATURE_PULL_PROGRESS, PROTOCOL_VERSION
from forgeyard.workloads import images
from forgeyard.workloads.spec import WorkloadSpec

log = logging.getLogger("forgeyard.agent")

LIVE = ("starting", "running", "healthy", "unhealthy")
TAIL_LINES = 100
TAIL_CHARS = 8000
TASK_BACKOFF_CAP = 300
SERVICE_BACKOFF_CAP = 60



def _where(ops: dict, probes: dict) -> str:
    """Each unfinished operation and probe, with the lines it is waiting on."""
    out = io.StringIO()
    for kind, tasks in (("operation", ops), ("probe", probes)):
        for rid, task in tasks.items():
            if task.done():
                continue
            out.write(f"{kind} {rid}:\n")
            task.print_stack(limit=8, file=out)
    return out.getvalue()

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
        # 1.3: replicas of workloads exposed at /x/ (`replica_key.strip`): their guard removes the internal
        # key before a request reaches the program, and their health checks never carry it.
        self.strip: set[str] = set()
        self.cancels: dict[str, threading.Event] = {}
        # 1.4 ("relay"): {replica id: {"model"|"engine": {path, mode}}} from the desired list, and the replicas
        # whose download is coming through the server right now (their progress says so).
        self.relays: dict[str, dict] = {}
        self.via_server: set[str] = set()
        # Why a download or pull in flight was cancelled ({replica id: "cancelled: …"}), for its last report.
        self.cancelled: dict[str, str] = {}
        self.stopping: set[str] = set()
        # What the server speaks (its `welcome` features): pull progress goes only to a server that
        # shows it, an older one gets the plain "pulling the image" it knows.
        self.server_features: set[str] = set()
        self.cache_dirty = models is not None
        self.gate_poll = gate_poll
        # 1.4 ("prefetch"): downloads into the model cache asked for ahead of a deployment.
        self.prefetch = Prefetcher(self)
        # 1.4.5 (#163): the guards' TLS certificate (agent/replica_tls.py), and whether they serve TLS: only for a
        # server that listed "guard_tls" (it dials https:// only), decided at each welcome before any guard opens.
        self.tls = ReplicaTls(state_dir)
        self.tls_required = False
        self._tls_said: Optional[bool] = None

    # ------------------------------------------------------------------ what the agent tells the server
    def runtime_info(self) -> dict:
        """What each runtime can do, and (1.4.2) the host port range replicas are published on: the server
        counts the ports its replicas hold there when it places one (servers before 1.4.2 drop it)."""
        info = {name: rt.info() for name, rt in self.runtimes.items()}
        info["ports"] = {"low": self.port_range[0], "high": self.port_range[1]}
        return info

    def available(self) -> list[str]:
        return [name for name, rt in self.runtimes.items() if rt.info().get("ok")]

    def set_address(self, address: Optional[str]) -> None:
        if address and not self.bind_fixed:
            self.address = address

    def model_cache_info(self) -> Optional[dict]:
        return self.models.info() if self.models is not None else None

    def new_connection(self, features=()) -> None:
        """Revisions order the desired lists of one connection: a new one starts its own order. (1.4.5) Whether the
        guards serve TLS follows the server: a guard of the other kind is opened again (`_open_guard`)."""
        self.revision = -1
        self.server_features = set(features or ())
        want = FEATURE_GUARD_TLS in self.server_features and self.tls.usable
        if want != self.tls_required or self._tls_said is None:
            if want:
                log.info("The server reaches replicas over TLS: the key guards serve TLS only.")
            elif not self.tls.usable:
                log.warning("This agent lacks the cryptography package, so its key guards serve plain HTTP and the "
                            "server reports the machine's traffic as not encrypted. Install the agent's requirements.")
            else:
                log.warning("The server is older than 1.4.5 and reaches replicas over plain HTTP: the key guards serve "
                            "plain HTTP to it. Update the server to encrypt that traffic.")
            self._tls_said = want
        if want != self.tls_required:
            self.tls_required = want
            self._wake.set()

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
            # 1.4 ("relay"): how to fetch the model and the engine through the server, if this machine may. From
            # every desired list (the machine's setting may change), kept beside the record, never in it.
            relays = {}
            for part in ("model", "engine"):
                block = r.get(part)
                if isinstance(block, dict) and "relay" in block:
                    found = relay.parse(block)
                    if found is not None:
                        relays[part] = found
                    r = {**r, part: {k: v for k, v in block.items() if k != "relay"}}
            if "image_relay" in r:
                # 1.4 PR 4 ("relay_images"): the pinned engine image through the server, beside the spec.
                found = relay.parse_image(r)
                if found is not None:
                    relays["image"] = found
                r = {k: v for k, v in r.items() if k != "image_relay"}
            if relays:
                self.relays[r["id"]] = relays
            else:
                self.relays.pop(r["id"], None)
            key = r.get("replica_key")
            if "replica_key" in r:
                r = {k: v for k, v in r.items() if k != "replica_key"}
                env = key.get("env") if isinstance(key, dict) else None
                if isinstance(key, dict) and key.get("value") and (env is None or isinstance(env, str)):
                    # env None: the engine takes no key of its own; the guard alone requires it.
                    self.keys[r["id"]] = (env or None, str(key["value"]))
                    r["key_env"] = env or None
                    r["guarded"] = True
                    if key.get("strip") is True and not env:
                        self.strip.add(r["id"])
                    else:
                        self.strip.discard(r["id"])
            if "image_ids" in r:
                r = {**r, "image_ids": images.parse_check(r["image_ids"])}
            desired[r["id"]] = r
        self.desired = desired
        for rid in [rid for rid in self.relays if rid not in desired]:
            self.relays.pop(rid, None)
        self._cancel_unwanted()
        # 1.4: the downloads asked for ahead of a deployment (none from a server without "prefetch").
        self.prefetch.set_wanted(msg.get("downloads") or [])
        self.full_due = True
        self._wake.set()

    def _cancel_unwanted(self) -> None:
        """Cancel every download and pull in flight whose replica the desired list no longer wants, now:
        not after the next round has looked at every running replica (on a machine with many, that
        took seconds, while the download kept writing and held the model's lock)."""
        if self.desired is None:
            return
        for rid, event in list(self.cancels.items()):
            rec = self.records.get(rid)
            want = self.desired.get(rid)
            if rec is None or (want is not None and want.get("generation") == rec.generation):
                continue
            if not event.is_set():
                what = "deployment" if rec.model is not None or rec.engine is not None else "workload"
                why = f"cancelled: the {what} was removed" if want is None else f"cancelled: the {what} was changed"
                self.cancelled[rid] = why
                log.info("Replica %s of %s: %s; stopping its %s.", rid[:12], rec.workload, why,
                         "image pull" if rec.download is None or rec.download.get("phase") == "image" else "download")
                event.set()

    def _notify(self, rec: Optional[Record] = None) -> None:
        if rec is not None:
            self.changed.add(rec.id)
        self.report_event.set()

    def _public(self, rec: Record) -> dict:
        return rec.public(with_tail=self.tails.get(rec.id) if rec.final else None)

    def take_report(self) -> Optional[dict]:
        prefetch = FEATURE_PREFETCH in self.server_features
        if not prefetch:
            self.prefetch.changed.clear()  # a server without "prefetch" asks for none and hears of none
        if self.full_ready:
            entries = [self._public(r) for r in self.records.values()] + self.removed
            full, self.full_ready = True, False
        elif self.changed or self.removed or self.prefetch.changed:
            entries = [self._public(self.records[i]) for i in sorted(self.changed) if i in self.records] + self.removed
            full = False
        else:
            return None
        self.changed.clear()
        self.removed = []
        msg = {"v": PROTOCOL_VERSION, "type": "status", "full": full, "replicas": entries}
        if prefetch:
            downloads = self.prefetch.take(full)
            if downloads:
                msg["downloads"] = downloads
        if self.cache_dirty and self.models is not None:
            self.cache_dirty = False
            msg["model_cache"] = self.models.info()
        return msg

    # ------------------------------------------------------------------ the model cache (1.4, cache_admin)
    def cache_users(self, target: dict) -> list[str]:
        """The workloads of the replicas here that need what a cache removal would delete: every one
        this agent has not finished with (running, starting, downloading, waiting to start, or in
        backoff) and every one the last desired list wants. Called from the removal's thread while the
        model's lock is held, on snapshots of the two lists."""
        from forgeyard.models.cache_keys import engine_uses, model_uses

        users = set()
        for rec in list(self.records.values()):
            if rec.final:
                continue
            if model_uses(rec.model, target) or engine_uses(rec.engine, target):
                users.add(rec.workload)
        for want in list((self.desired or {}).values()):
            if model_uses(want.get("model"), target) or engine_uses(want.get("engine"), target):
                users.add(str(want.get("workload") or want.get("id")))
        return sorted(users)

    async def cache_remove(self, key: Optional[str], *, partial: bool = False) -> dict:
        """Remove a model (by its cache key) or the partial downloads from this machine's cache; the
        answer to the server, with the cache as it is afterwards."""
        from forgeyard.agent.models import CacheError, CacheRefused

        if self.models is None:
            return {"ok": False, "code": "no_cache", "reason": "This agent has no model cache."}
        try:
            if partial:
                freed = await asyncio.to_thread(self.models.prune_partial)
            else:
                freed = await asyncio.to_thread(self.models.remove, str(key or ""), self.cache_users)
            answer = {"ok": True, "freed_bytes": freed}
        except CacheRefused as e:
            answer = {"ok": False, "code": e.code, "reason": str(e), "in_use": e.users}
        except (CacheError, OSError) as e:
            log.warning("Could not remove %s from the model cache: %s", "the partial downloads" if partial else key, e)
            answer = {"ok": False, "code": "failed", "reason": f"could not remove it: {e}"[:400]}
        answer["model_cache"] = await asyncio.to_thread(self.models.info)
        return answer

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
        from outside until an agent runs again: the safe direction). Downloads and pulls in flight are
        cancelled first: their threads would otherwise go on (and hold the model's lock) after their
        tasks are gone."""
        for event in self.cancels.values():
            event.set()
        await self.prefetch.close()
        tasks = list(self.ops.values()) + list(self.probes.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for rid in list(self.guards):
            await self._close_guard(rid)

    async def settle(self, timeout: float = 60.0) -> None:
        """Wait for every operation in flight (tests). It fails instead of waiting for ever: after
        `timeout` seconds with the operations still running and where each one is, and at once when a
        finished operation stays listed (it would keep this loop turning)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            pending = [t for t in (*self.ops.values(), *self.probes.values(), *self.prefetch.tasks()) if not t.done()]
            if not pending:
                for _ in range(3):  # the done callbacks that take finished operations off self.ops
                    await asyncio.sleep(0)
                if any(not t.done() for t in (*self.ops.values(), *self.probes.values(), *self.prefetch.tasks())):
                    continue
                if self.ops:
                    raise RuntimeError(f"settle: finished operations are still listed: {sorted(self.ops)}")
                return
            left = deadline - loop.time()
            if left <= 0:
                raise TimeoutError(f"settle: still running after {timeout:g} s:\n"
                               + _where(self.ops, {**self.probes, **{f"download {j.id}": j.task for j in self.prefetch.jobs.values() if j.task is not None}}))
            await asyncio.wait(pending, timeout=left)

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
                    self._cancel_unwanted()  # a download or pull nobody wants any more
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
        if rec is None or rec.id not in self.cancels:
            return  # a late report from a download that is over
        if total:
            done = min(done, total)  # never "14.54 of 12.93 GiB": a safety net, the cache counts each byte once
        rec.download = {"phase": phase, "done": done, "total": total, **({"file": file} if file else {})}
        what = "the model" if phase == "model" else "the engine"
        if rec.id in self.via_server:
            rec.download["via"] = "server"
            what += " through the server"
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

        def via(how: str) -> None:
            if how == "server":
                loop.call_soon_threadsafe(self.via_server.add, rec.id)

        relays = self.relays.get(rec.id) or {}
        try:
            if rec.engine is not None:
                exe = await asyncio.to_thread(self.models.ensure_engine, rec.engine, progress=progress, cancel=cancel,
                                              relay=relays.get("engine"), via=via)
                rec.engine_path = str(exe)
                self.via_server.discard(rec.id)  # the model may come another way
            if rec.model is not None:
                if not self.models.is_cached(rec.model) and self.models.busy(cache_key(rec.model)):
                    rec.reason = "waiting for another replica's download of the same model"
                    self._notify(rec)
                rec.paths = await asyncio.to_thread(self.models.ensure_model, rec.model, token=self.tokens.get(rec.id),
                                                    progress=progress, cancel=cancel, relay=relays.get("model"),
                                                    via=via)
        except Cancelled:
            rec.state, rec.reason, rec.download = "pending", self.cancelled.get(rec.id, "cancelled"), None
            return False
        except CacheError as e:
            rec.download = None
            await self._ended(rec, None, reason=str(e)[:300])
            return False
        except asyncio.CancelledError:
            cancel.set()  # the agent stops: the thread must stop too, and release the model's lock
            raise
        finally:
            self.cancels.pop(rec.id, None)
            self.via_server.discard(rec.id)
            self.cache_dirty = True
        rec.download = None
        return True

    def image_relay(self, found: Optional[dict]):
        """An engine image through the server (1.4 PR 4, agent/imagerelay.py), when the desired list allows it and
        this agent has a model cache to stage it in and a client to its server; else None."""
        client = getattr(self.models, "relay_client", None) if self.models is not None else None
        if found is None or client is None:
            return None
        from forgeyard.agent.imagerelay import ImageRelay

        return ImageRelay(found["path"], found["mode"], client, self.models)

    def can_relay_images(self) -> bool:
        """The agent may say "relay_images": a client to its server, a model cache, and a container engine."""
        rt = self.runtimes.get("container")
        return (self.models is not None and getattr(self.models, "relay_client", None) is not None
                and rt is not None and hasattr(rt, "load") and bool(rt.info().get("ok")))

    def can_save_images(self) -> bool:
        """1.4.1 ("image_save"): a container engine whose images can be listed and saved for another machine."""
        rt = self.runtimes.get("container")
        return rt is not None and hasattr(rt, "save_stream") and bool(rt.info().get("ok"))

    def can_remove_images(self) -> bool:
        """1.4.2 ("image_remove"): a container engine whose images an admin may remove from the console."""
        rt = self.runtimes.get("container")
        return rt is not None and hasattr(rt, "remove_image") and bool(rt.info().get("ok"))

    def image_users(self, entry: dict) -> list[str]:
        """1.4.2: the workloads of the container replicas here that use this listed image: every one this agent
        has not finished with, and every one the last desired list wants (it may be about to start)."""
        rt = self.runtimes.get("container")
        if rt is None:
            return []
        users = set()
        for rec in list(self.records.values()):
            if rec.final or rec.runtime != "container":
                continue
            if rt.refers(entry, ((rec.spec or {}).get("runtime") or {}).get("image"), rec.image_ids):
                users.add(rec.workload)
        for want in list((self.desired or {}).values()):
            runtime = ((want.get("spec") or {}).get("runtime") or {})
            if runtime.get("type") == "container" and rt.refers(entry, runtime.get("image"), want.get("image_ids")):
                users.add(str(want.get("workload") or want.get("id")))
        return sorted(users)

    async def remove_image(self, image_id: str) -> dict:
        """1.4.2 ("image_remove"): remove one of the engine's images, as an admin asked; the answer to the server."""
        from forgeyard.agent.workloads.container import RemoveRefused

        rt = self.runtimes.get("container")
        if rt is None or not self.can_remove_images():
            return {"ok": False, "code": "failed", "reason": "this agent has no container engine"}
        try:
            return await asyncio.to_thread(rt.remove_image, image_id, self.image_users)
        except RemoveRefused as e:
            log.info("Did not remove the image %s: %s", str(image_id)[:19], e)
            return {"ok": False, "code": e.code, "reason": str(e)[:400], "in_use": e.users}

    def can_load_images(self) -> bool:
        rt = self.runtimes.get("container")
        return rt is not None and hasattr(rt, "load_upload") and bool(rt.info().get("ok"))

    async def load_upload(self, opened, size: Optional[int], cancel: threading.Event) -> list[dict]:
        """1.4 ("image_load"): an image file an admin uploaded, loaded into the container engine."""
        rt = self.runtimes.get("container")
        return await asyncio.to_thread(rt.load_upload, opened, size, cancel)

    def _pull_progress(self, rec_id: str, snap: dict) -> None:
        rec = self.records.get(rec_id)
        if rec is None or rec.id not in self.cancels:
            return
        if FEATURE_PULL_PROGRESS not in self.server_features:
            # An older server knows no image progress, and records every new reason as an event.
            reason, download = "pulling the image", None
        else:
            from forgeyard.agent.workloads.pull import describe

            total = int(snap.get("total") or 0)
            download = {"phase": "image", "done": min(int(snap.get("done") or 0), total) if total else
                        int(snap.get("done") or 0), "total": total, "layers": int(snap.get("layers") or 0),
                        "layers_done": int(snap.get("layers_done") or 0)}
            if snap.get("unpacking"):
                download["unpacking"] = True
            if snap.get("via") == "server":
                download["via"] = "server"  # 1.4: through the server's relay
            reason = describe(snap)
        if (rec.reason, rec.download) != (reason, download):
            rec.reason, rec.download = reason, download
            self._notify(rec)

    async def _pull(self, rec: Record, rt) -> bool:
        """The image onto this machine (pulled when it is not local), with progress. False when it
        cannot (the replica has ended) or was cancelled."""
        from forgeyard.agent.workloads.pull import PullCancelled
        from forgeyard.agent.workloads.runtime import StartError

        loop = asyncio.get_running_loop()
        cancel = self.cancels.setdefault(rec.id, threading.Event())
        self._cancel_unwanted()  # unwanted since the model was ready: never start pulling for it

        def progress(snap: dict) -> None:
            loop.call_soon_threadsafe(self._pull_progress, rec.id, dict(snap))

        through = self.image_relay((self.relays.get(rec.id) or {}).get("image"))
        try:
            if through is not None:
                await asyncio.to_thread(rt.pull, rec, progress, cancel, through)
            else:
                await asyncio.to_thread(rt.pull, rec, progress, cancel)
        except PullCancelled:
            rec.state, rec.reason, rec.download = "pending", self.cancelled.get(rec.id, "cancelled"), None
            log.info("Replica %s of %s: image pull %s.", rec.id[:12], rec.workload, rec.reason)
            self._save(rec)
            self._notify(rec)
            return False
        except StartError as e:
            rec.download = None
            reason = str(e) or type(e).__name__
            log.warning("Replica %s of %s could not get its image: %s", rec.id[:12], rec.workload, reason)
            await self._ended(rec, None, reason=reason[:300])
            return False
        except asyncio.CancelledError:
            cancel.set()  # the agent stops: end the pull's thread too
            raise
        finally:
            self.cancels.pop(rec.id, None)
        if rec.download is not None or rec.reason != "starting the container":
            rec.download, rec.reason = None, "starting the container"
            self._notify(rec)
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
            rec.reason = "checking the image" if rec.runtime == "container" else None
            rec.started_at = self.clock()
            self._save(rec)
            self._notify(rec)
        if hasattr(rt, "pull"):
            if not await self._pull(rec, rt):
                return
            if rec.id not in self.records:
                return
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
        # An exposed workload's program never sees the internal key, not even from its health check.
        key = self.keys.get(rec.id) if self._guarded(rec) and rec.id not in self.strip else None
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
