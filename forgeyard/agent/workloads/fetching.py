"""What a replica needs before it starts (forgeyard/agent/workloads/supervisor.py, model replicas and container
replicas): its model and engine in the model cache, its image on this machine, with progress, through the
server when the desired list says so, and cancelled the moment nobody wants them. `Fetching` is that part of
the `Supervisor`."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from forgeyard.agent.workloads.records import Record
from forgeyard.machines.protocol import FEATURE_PULL_PROGRESS

log = logging.getLogger("forgeyard.agent")


class Fetching:
    """The `Supervisor`'s downloads and image pulls, and their cancel switches."""

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
