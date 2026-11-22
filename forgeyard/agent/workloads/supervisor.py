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

The parts (1.4.5, #173): lifecycle.py (start, end, stop, remove), fetching.py (model, engine and image before
a start), checks.py (health), keyguards.py (the key guards) and admin.py (the cache and image requests of an
admin) are mixins of `Supervisor`; this module holds its state, the desired list, the reports and the
reconcile loop.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from forgeyard.agent import relay
from forgeyard.agent.replica_tls import ReplicaTls
from forgeyard.agent.workloads import enginecache, health, plan, ports as portlib
from forgeyard.agent.workloads.admin import Admin
from forgeyard.agent.workloads.checks import HealthChecks
from forgeyard.agent.workloads.fetching import Fetching
from forgeyard.agent.workloads.keyguards import KeyGuards
from forgeyard.agent.workloads.lifecycle import (LIVE, SERVICE_BACKOFF_CAP, TAIL_CHARS, TAIL_LINES, TASK_BACKOFF_CAP,
                                                 Lifecycle)
from forgeyard.agent.workloads.process import find_leader
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.agent.workloads.prefetch import Prefetcher
from forgeyard.agent.workloads.runtime import Runtime
from forgeyard.agent.workloads.state import Alias, Fetches, Guarding, Operations, Outbox, Secrets
from forgeyard.machines.protocol import FEATURE_GUARD_TLS, FEATURE_PREFETCH, FEATURE_START_TIMES, PROTOCOL_VERSION
from forgeyard.workloads import images

__all__ = ["LIVE", "SERVICE_BACKOFF_CAP", "TAIL_CHARS", "TAIL_LINES", "TASK_BACKOFF_CAP", "Supervisor"]

log = logging.getLogger("forgeyard.agent")


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


class Supervisor(Lifecycle, Fetching, HealthChecks, KeyGuards, Admin):
    # The state in memory (state.py), each field also under the name the agent and the tests use.
    ops, probes, stopping = Alias("work"), Alias("work"), Alias("work")
    changed, removed, tails = Alias("outbox"), Alias("outbox"), Alias("outbox")
    full_due, full_ready, report_event = Alias("outbox"), Alias("outbox"), Alias("outbox")
    tokens, keys, strip = Alias("secrets"), Alias("secrets"), Alias("secrets")
    cancels, cancelled, relays, via_server = Alias("fetches"), Alias("fetches"), Alias("fetches"), Alias("fetches")
    guards, guard_errors = Alias("guarding"), Alias("guarding", "errors")

    def __init__(self, state_dir: Path, runtimes: dict[str, Runtime], *, port_range: tuple[int, int] = (30000, 32767),
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
        # Operations and probes in flight, and the replicas being stopped.
        self.work = Operations()
        # What the next status message says.
        self.outbox = Outbox()
        self._wake = asyncio.Event()
        self._owned: dict[str, dict] = {}
        self._orphans_swept = False
        self.started = 0  # replicas started by this agent process (tests and logs)
        # M3: the model cache (None: this agent serves no models).
        self.models = models
        # M3, M4, 1.3: tokens for gated models, the replicas' internal keys, the replicas whose guard strips the
        # key: in memory only. M4: the key guards in front of model replicas, and why one could not listen.
        self.secrets = Secrets()
        self.guarding = Guarding()
        # Downloads and pulls in flight: their cancel switches, why one was cancelled, the relays (1.4).
        self.fetches = Fetches()
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
        # 1.5.0 ("engine_cache"): the deployments whose compile caches may stay, and whether to look again.
        self.engine_cache_keep: Optional[set[str]] = None
        self.engine_cache_due = False

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
            if "engine_cache" in r:
                r = {**r, "engine_cache": enginecache.parse(r["engine_cache"])}  # 1.5.0, beside the spec
            desired[r["id"]] = r
        self.desired = desired
        # 1.5.0 ("engine_cache"): the deployments whose engine caches this machine keeps; checked on the next round.
        keep = enginecache.parse_keep(msg.get("engine_caches"))
        if keep is not None:
            self.engine_cache_keep, self.engine_cache_due = keep, True
        for rid in [rid for rid in self.relays if rid not in desired]:
            self.relays.pop(rid, None)
        self._cancel_unwanted()
        # 1.4: the downloads asked for ahead of a deployment (none from a server without "prefetch").
        self.prefetch.set_wanted(msg.get("downloads") or [])
        self.full_due = True
        self._wake.set()

    def _notify(self, rec: Optional[Record] = None) -> None:
        if rec is not None:
            self.changed.add(rec.id)
        self.report_event.set()

    def _public(self, rec: Record) -> dict:
        return rec.public(with_tail=self.tails.get(rec.id) if rec.final else None,
                          start=FEATURE_START_TIMES in self.server_features)

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
            if not enginecache.same(want.get("engine_cache"), rec.engine_cache):
                rec.engine_cache = want.get("engine_cache")  # the recipe turned it on or off: from the next start
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
        if self.engine_cache_due:
            self.engine_cache_due = False
            await self._prune_engine_caches()

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
            rec.engine_cache = want.get("engine_cache")
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

    # ------------------------------------------------------------------ engine caches (1.5.0)
    def engine_cache_root(self) -> Optional[Path]:
        root = getattr(self.models, "root", None) if self.models is not None else None
        return Path(root) if root is not None else None

    def _engine_cache_keys(self) -> set[str]:
        """Every engine cache a replica here has or is wanted with."""
        out = {(r.engine_cache or {}).get("key") for r in self.records.values()}
        out |= {(w.get("engine_cache") or {}).get("key") for w in (self.desired or {}).values()}
        return {k for k in out if k}

    async def _prune_engine_caches(self) -> None:
        root = self.engine_cache_root()
        if root is None or self.engine_cache_keep is None:
            return
        try:
            await asyncio.to_thread(enginecache.prune, root, set(self.engine_cache_keep), self._engine_cache_keys())
        except OSError as e:
            log.warning("Could not look through the engine caches in %s: %s", root, e.strerror or e)

    async def _engine_cache_dir(self, rec: Record) -> None:
        """Before a container starts: its engine cache directory, made when missing (`rec.engine_cache["dir"]`).
        A cache that cannot be made is left out, with a warning: the engine then compiles as it did before."""
        block, root = rec.engine_cache, self.engine_cache_root()
        if not block or rec.runtime != "container":
            return
        rec.engine_cache = {k: v for k, v in block.items() if k != "dir"}
        if root is None:
            return
        try:
            path = await asyncio.to_thread(enginecache.directory, root, block["key"])
        except (OSError, ValueError) as e:
            log.warning("Replica %s of %s starts without its engine cache: %s", rec.id[:12], rec.workload,
                        getattr(e, "strerror", None) or e)
            return
        rec.engine_cache["dir"] = str(path)

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
