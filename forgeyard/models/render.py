"""From a deployment, a recipe variant and a placement to the spec its replica runs: an ordinary
container or process workload spec, so the agent runs it like any other workload.

The server fills every placeholder except what only the machine knows: the model's paths
(${MODEL_PATH}, ${MODEL_FILE}, ${MODEL_HUB}: the read-only mount in a container, the cache
directory for a process), a process's host port (${PORT}) and its bind address
(${BIND_ADDRESS}). The agent fills those, and nothing else, when it starts the replica.
"""

from __future__ import annotations

import posixpath

from forgeyard.db import Accelerator
from forgeyard.models.catalog import Context
from forgeyard.models.estimate import gpu_memory_utilization
from forgeyard.models.recipes import Variant, render_text


def values(ctx: Context, variant: Variant, cards: list[Accelerator], gpu_context: int) -> dict:
    p = ctx.params
    container = variant.runtime == "container"
    meta = ctx.model.meta or {}
    out = {k: v for k, v in p.items() if not isinstance(v, (list, dict)) and v is not None}
    out.update({
        "model_path": "${MODEL_PATH}", "model_file": "${MODEL_FILE}", "model_hub": "${MODEL_HUB}",
        "served_name": ctx.deployment.served_name if ctx.deployment else p.get("served_name", ""),
        "host": "0.0.0.0" if container else "${BIND_ADDRESS}",
        "port": str(ctx.recipe.port) if container else "${PORT}",
        "context": p.get("context", 0), "concurrency": p.get("concurrency", 1),
        "kv_tokens": ctx.estimate.kv_tokens or p.get("context", 0),
        "gpus": len(cards) if variant.gpu else 0,
        "gpu_layers": p.get("gpu_layers", 999) if variant.gpu else 0,
        "threads": p.get("threads", -1), "dtype": p.get("dtype", "auto"),
        "repo": meta.get("repo") or (ctx.deployment.served_name if ctx.deployment else ""),
    })
    if variant.gpu and cards:
        total = min(int(a.vram_total or 0) for a in cards)
        out["gpu_util"] = f"{gpu_memory_utilization(ctx.estimate.per_gpu, total, gpu_context):.3f}"
    else:
        out["gpu_util"] = "0"
    return out


def args_for(ctx: Context, variant: Variant, vals: dict) -> list[str]:
    base = list(variant.args if variant.args is not None else ctx.recipe.args)
    for oa in ctx.recipe.optional_args:
        if all(str(ctx.params.get(k)) == str(v) for k, v in oa.if_.items()):
            base += oa.args
    base += list((ctx.model.engine_args or {}).get(ctx.recipe.name) or [])
    base += list(ctx.params.get("extra_args") or [])
    return [render_text(a, vals) for a in base]


def render(ctx: Context, variant: Variant, cards: list[Accelerator], name: str, *, gpu_context: int) -> dict:
    """The replica's WorkloadSpec (as stored and sent): container or process, its health check,
    its port, its declared GPU sharing and reservation."""
    vals = values(ctx, variant, cards, gpu_context)
    env = {**ctx.recipe.env, **variant.env}
    env = {k: render_text(v, vals) for k, v in env.items()}
    args = args_for(ctx, variant, vals)
    if variant.runtime == "container":
        runtime = {"type": "container", "image": variant.image, "command": list(variant.entrypoint) if variant.entrypoint else None,
                   "args": args}
        ports = [{"name": "http", "port": ctx.recipe.port}]
    else:
        command = variant.command or posixpath.basename(variant.engine.binary)
        runtime = {"type": "process", "command": command, "args": args, "workdir": None}
        ports = [{"name": "http", "port": None}]
    h = ctx.recipe.health
    resources: dict = {"cpu": None, "memory": None, "gpus": {"count": 0, "indices": None, "vendor": None,
                                                             "vram": None, "share": "exclusive"}}
    if not variant.gpu and int(ctx.params.get("threads") or 0) > 0:
        resources["cpu"] = float(ctx.params["threads"])  # counted against the machine's threads
    if variant.gpu:
        resources["gpus"] = {"count": len(cards), "indices": None, "vendor": variant.when.vendor,
                             "vram": ctx.estimate.per_gpu, "share": ctx.share}
    return {
        "name": name, "kind": "service", "runtime": runtime, "env": env, "resources": resources, "ports": ports,
        "health": {"type": "http", "port": "http", "path": h.path, "interval": h.interval, "timeout": h.timeout,
                   "failure_threshold": h.failure_threshold, "initial_delay": 0, "start_period": h.start_period},
        "restart": "always", "max_duration": None, "retries": 0, "retry_backoff": 5, "replicas": 1, "machine_id": None,
    }


def engine_block(ctx: Context, variant: Variant):
    if variant.runtime != "process" or variant.engine is None:
        return None
    e = variant.engine
    return {"name": f"{ctx.recipe.engine}-{ctx.recipe.version}", "url": e.url, "sha256": e.sha256, "binary": e.binary}
