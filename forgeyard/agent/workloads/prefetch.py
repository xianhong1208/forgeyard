"""Downloads asked for ahead of a deployment (1.4, "Download to…", issue #35; protocol feature "prefetch").

The server lists them in `desired.downloads`, beside the replicas: `{id, model, engine?, image?,
image_ids?}`. Each is fetched into the model cache with the code a replica's download uses, so they share
its locks (a replica of the same model waits for it, or finds it done), its resume from `.partial`, its
checksums and its cancel; an engine image is pulled as a container replica's is (1.3.1, with progress per
layer). Nothing is started.

Like the replicas, the list is the whole truth: a download missing from the next list is cancelled at once
(a thread waiting on the network stops, the model's lock is released), and one listed again after an agent
restart simply starts again, resuming what is on disk. At most MAX_RUNNING run at a time; the others wait
("queued").

Each change is reported in `status.downloads` ({id, state, reason, download}), only to a server that
lists "prefetch".
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from forgeyard.agent.workloads.supervisor import Supervisor

log = logging.getLogger("forgeyard.agent")

MAX_RUNNING = 2


@dataclass
class Job:
    id: str
    model: Optional[dict] = None       # validated (agent.models.validate_model)
    engine: Optional[dict] = None
    image: Optional[str] = None
    image_ids: Optional[dict] = None
    label: str = ""
    state: str = "queued"              # queued | downloading | done | failed | cancelled
    reason: Optional[str] = None
    download: Optional[dict] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    task: Optional[asyncio.Task] = None

    def public(self) -> dict:
        out = {"id": self.id, "state": self.state, "reason": self.reason}
        if self.download is not None:
            out["download"] = dict(self.download)
        return out


class Prefetcher:
    def __init__(self, sup: "Supervisor"):
        self.sup = sup
        self.jobs: dict[str, Job] = {}
        self.tokens: dict[str, str] = {}   # gated models' Hugging Face tokens: memory only
        self.changed: set[str] = set()

    # ------------------------------------------------------------------ the list from the server
    def set_wanted(self, items) -> None:
        """Converge on the server's list: start what is new, cancel what is no longer listed."""
        wanted: dict[str, dict] = {}
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                wanted[item["id"][:32]] = item
        for jid, job in list(self.jobs.items()):
            if jid in wanted:
                continue
            if job.task is not None and not job.task.done():
                if not job.cancel.is_set():
                    log.info("Download %s (%s): no longer wanted; cancelling it.", jid[:12], job.label)
                    job.cancel.set()
                continue  # forgotten when its task ends
            self.jobs.pop(jid, None)
            self.tokens.pop(jid, None)
            self.changed.discard(jid)
        for jid, item in wanted.items():
            if jid not in self.jobs:
                self.jobs[jid] = self._job(jid, item)
                self.changed.add(jid)
        self._start_queued()
        self.sup._notify()

    def _job(self, jid: str, item: dict) -> Job:
        from forgeyard.agent.models import CacheError, validate_model
        from forgeyard.models.sources import SourceError
        from forgeyard.workloads import images

        job = Job(jid)
        try:
            if self.sup.models is None:
                raise CacheError("this agent has no model cache (start it with --model-cache DIR)")
            model = item.get("model")
            if isinstance(model, dict):
                if model.get("token"):
                    self.tokens[jid] = str(model["token"])  # memory only, never in a report
                # The validated block keeps only what the agent knows (never the token).
                job.model = validate_model(model, self.sup.models.local_dirs)
                job.label = job.model.get("repo") or job.model.get("url") or job.model.get("path") or jid[:12]
            if isinstance(item.get("engine"), dict):
                job.engine = dict(item["engine"])
            image = item.get("image")
            if isinstance(image, str) and image:
                job.image = image[:255]
                job.image_ids = images.parse_check(item.get("image_ids")) if item.get("image_ids") else None
            if job.model is None and job.engine is None and job.image is None:
                raise CacheError("nothing to download")
        except (CacheError, SourceError, ValueError, TypeError) as e:
            job.state, job.reason = "failed", f"refused the download: {e}"[:400]
            self.tokens.pop(jid, None)
        return job

    def _start_queued(self) -> None:
        running = sum(1 for j in self.jobs.values() if j.task is not None and not j.task.done())
        for job in self.jobs.values():
            if running >= MAX_RUNNING:
                break
            if job.state == "queued" and job.task is None:
                job.task = asyncio.create_task(self._run(job))
                running += 1
        waiting = [j for j in self.jobs.values() if j.state == "queued" and j.task is None]
        for job in waiting:
            text = "waiting for another download on this machine to finish"
            if job.reason != text:
                job.reason = text
                self.changed.add(job.id)

    # ------------------------------------------------------------------ one download
    def _progress(self, job: Job, phase: str, done: int, total: int, file: Optional[str]) -> None:
        if job.state != "downloading" or job.cancel.is_set():
            return  # a late report from a download that is over
        if total:
            done = min(done, total)
        job.download = {"phase": phase, "done": done, "total": total, **({"file": file} if file else {})}
        what = "the model" if phase == "model" else "the engine"
        job.reason = (f"downloading {what}: {done / 1024**3:.2f} of {total / 1024**3:.2f} GiB ({100 * done // total}%)"
                      if total else f"downloading {what}: {done / 1024**2:.0f} MiB")
        self._changed(job)

    def _pull_progress(self, job: Job, snap: dict) -> None:
        from forgeyard.agent.workloads.pull import describe

        if job.state != "downloading" or job.cancel.is_set():
            return
        total = int(snap.get("total") or 0)
        done = int(snap.get("done") or 0)
        job.download = {"phase": "image", "done": min(done, total) if total else done, "total": total,
                        "layers": int(snap.get("layers") or 0), "layers_done": int(snap.get("layers_done") or 0)}
        if snap.get("unpacking"):
            job.download["unpacking"] = True
        job.reason = describe(snap)
        self._changed(job)

    def _changed(self, job: Job) -> None:
        self.changed.add(job.id)
        self.sup._notify()

    async def _run(self, job: Job) -> None:
        from forgeyard.agent.models import CacheError, Cancelled
        from forgeyard.agent.workloads.pull import PullCancelled
        from forgeyard.agent.workloads.runtime import StartError
        from forgeyard.models.sources import cache_key

        loop = asyncio.get_running_loop()
        cache = self.sup.models
        job.state, job.reason = "downloading", "starting the download"
        self._changed(job)

        def progress(phase, done, total, file):
            loop.call_soon_threadsafe(self._progress, job, phase, done, total, file)

        def pulled(snap: dict) -> None:
            loop.call_soon_threadsafe(self._pull_progress, job, dict(snap))

        try:
            if job.model is not None:
                if not cache.is_cached(job.model) and cache.busy(cache_key(job.model)):
                    job.reason = "waiting for another download of the same model on this machine"
                    self._changed(job)
                await asyncio.to_thread(cache.ensure_model, job.model, token=self.tokens.get(job.id),
                                        progress=progress, cancel=job.cancel)
            if job.engine is not None:
                await asyncio.to_thread(cache.ensure_engine, job.engine, progress=progress, cancel=job.cancel)
            if job.image is not None:
                rt = self.sup.runtimes.get("container")
                if rt is None or not hasattr(rt, "ensure_image"):
                    raise StartError("this agent has no container engine to pull the engine image with")
                await asyncio.to_thread(rt.ensure_image, job.image, job.image_ids, pulled, job.cancel,
                                        who=f"download {job.id[:12]}")
            if job.cancel.is_set():
                raise Cancelled("cancelled")
            job.state, job.reason, job.download = "done", None, None
            log.info("Download %s (%s) finished.", job.id[:12], job.label)
        except (Cancelled, PullCancelled):
            job.state, job.reason, job.download = "cancelled", "cancelled: no longer wanted", None
            log.info("Download %s (%s) cancelled.", job.id[:12], job.label)
        except (CacheError, StartError, OSError) as e:
            job.state, job.reason, job.download = "failed", (str(e) or type(e).__name__)[:400], None
            log.warning("Download %s (%s) failed: %s", job.id[:12], job.label, job.reason)
        except asyncio.CancelledError:
            job.cancel.set()  # the agent stops: end the thread too, and release the model's lock
            raise
        finally:
            self.tokens.pop(job.id, None)
            self.sup.cache_dirty = True
            self._changed(job)
            if job.cancel.is_set() and self.jobs.get(job.id) is job:
                self.jobs.pop(job.id, None)  # cancelled because no list wants it any more
            try:
                self._start_queued()
            except RuntimeError:
                pass  # the loop is closing

    # ------------------------------------------------------------------ reports and the end
    def take(self, full: bool) -> list[dict]:
        if full:
            out = [j.public() for j in self.jobs.values()]
        else:
            out = [self.jobs[i].public() for i in sorted(self.changed) if i in self.jobs]
        self.changed.clear()
        return out

    def tasks(self) -> list[asyncio.Task]:
        return [j.task for j in self.jobs.values() if j.task is not None and not j.task.done()]

    async def close(self) -> None:
        tasks = self.tasks()
        for j in self.jobs.values():
            j.cancel.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
