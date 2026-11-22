"""What an admin asks of this machine through the server (forgeyard/agent/workloads/supervisor.py): removing a
model from the model cache (1.4, cache_admin), and the container engine's images: relayed, saved for another
machine, removed, loaded from an upload (1.4 to 1.4.2). Each refuses what a replica here still needs. `Admin`
is that part of the `Supervisor`."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

log = logging.getLogger("forgeyard.agent")


class Admin:
    """The `Supervisor`'s answers to an admin's cache and image requests."""

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
