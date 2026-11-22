"""Engine compile caches kept between starts (1.5.0, issue #37, "engine_cache"; the agent's half is
forgeyard/agent/workloads/enginecache.py).

vLLM compiles the model's graph with torch.compile at every start (and Inductor and Triton compile its kernels) into
VLLM_CACHE_ROOT, which is inside the container and lost when it stops. A recipe whose `engine_cache` is enabled has
the agent keep that directory on the machine instead, so the next start of the same replica (a restart, a move back,
a wake after sleeping) loads what it compiled before. vLLM keys what it stores by a hash of the model, the vLLM
configuration that affects the graph, its own code and the compiler (vllm/compilation/backends.py, 0.32.0), so a
changed parameter compiles anew in a new subdirectory rather than using a wrong graph.

What is still kept apart, because a cache is code the engine loads:
- one directory per replica, never shared between deployments or between two replicas that may run at once:
  KEY = <deployment id>-<its creation time, hex>-<ordinal>-<first 12 hex digits of the SHA-256 of the image>, so a
  deployment id reused after a database reset, a new recipe version or another variant's image never finds an old
  directory;
- the agent removes the directories of a deployment that is gone (or whose recipe no longer keeps one) and the
  older directories of a replica that starts with a new key, never one a replica uses.

Only for container variants; an agent older than 1.5.0 ignores it (it is beside the spec). Off in every built-in recipe until a real
start on an A100 has shown what it saves (docs/guide/how-to.md, "Keep vLLM's compile cache between starts").
"""

from __future__ import annotations

import hashlib
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import Deployment, Node, Replica, as_utc
from forgeyard.machines.protocol import ENGINE_CACHE_PREFIX_PARTS


def deployment_prefix(dep: Deployment) -> str:
    created = int(as_utc(dep.created_at).timestamp()) if dep.created_at is not None else 0
    return f"{dep.id}-{created:x}"


def key_for(dep: Deployment, ordinal: int, image: str) -> str:
    return f"{deployment_prefix(dep)}-{int(ordinal)}-{hashlib.sha256(image.encode()).hexdigest()[:12]}"


def prefix_of(key: str) -> str:
    return "-".join(key.split("-")[:ENGINE_CACHE_PREFIX_PARTS])


def _enabled(db: DbSession, names: set[str]) -> dict[str, str]:
    """Recipe name -> the variable of its engine cache, for the recipes among `names` that keep one."""
    from forgeyard.models.catalog import get_recipe

    out = {}
    for name in sorted(names):
        recipe = get_recipe(db, name)
        if recipe is not None and recipe.engine_cache is not None and recipe.engine_cache.enabled:
            out[name] = recipe.engine_cache.env
    return out


def attach(db: DbSession, node: Optional[Node], message: dict, reps: list[Replica]) -> dict:
    """`message` (a desired list for `node`) with each container replica of a caching recipe's `engine_cache`, and
    `engine_caches`: the deployments whose caches the machine may keep. An agent older than 1.5.0 ignores both."""
    if node is None:
        return message
    deps = db.scalars(select(Deployment)).all()
    caching = _enabled(db, {d.recipe for d in deps})
    by_workload = {d.workload_id: d for d in deps if d.recipe in caching}
    for out, r in zip(message["replicas"], reps):
        dep = by_workload.get(r.workload_id)
        runtime = ((r.spec or {}).get("runtime") or {})
        if dep is None or runtime.get("type") != "container" or not runtime.get("image"):
            continue
        out["engine_cache"] = {"key": key_for(dep, r.ordinal, str(runtime["image"])), "env": caching[dep.recipe]}
    message["engine_caches"] = sorted(deployment_prefix(d) for d in by_workload.values())
    return message
