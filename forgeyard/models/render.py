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
from forgeyard.models import modalities
from forgeyard.models.catalog import Context
from forgeyard.models.estimate import CODEC_LABEL, GIB, gpu_memory_utilization, image_caps, image_side
from forgeyard.models.recipes import IMAGE_COMPONENTS, Variant, render_text


def values(ctx: Context, variant: Variant, cards: list[Accelerator], gpu_context: int) -> dict:
    p = ctx.params
    container = variant.runtime == "container"
    meta = ctx.model.meta or {}
    # A recipe's own defaults first: a deployment made before the recipe gained a parameter (1.3's
    # images, videos, max_body_mb) still renders it.
    out = {k: q.default for k, q in ctx.recipe.params.items()}
    out.update({k: v for k, v in p.items() if not isinstance(v, (list, dict)) and v is not None})
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
        # The projector file of a GGUF vision model (1.3), next to the model file in its directory.
        "mmproj_file": f"${{MODEL_PATH}}/{mmproj_path(meta)}" if mmproj_path(meta) else "",
    })
    # The largest image in pixels for an image-token cap, where tokens grow with the resolution.
    tile = int(meta.get("vision_tile_px") or 0)
    out["image_max_pixels"] = int(out.get("image_tokens") or 0) * tile * tile
    # Image generation (1.3): the default size, the largest image in pixels, the component files.
    out["width"], out["height"] = default_size(meta, p)
    side, _ = image_caps(meta, p)
    out["max_pixels"] = side * side
    # Video generation (1.3): how long the engine keeps a finished video on its own (a safety net behind
    # the gateway's expiry, which deletes each one when it expires): keep_minutes and an hour.
    keep = p.get("keep_minutes", out.get("keep_minutes"))
    keep = keep if isinstance(keep, int) and not isinstance(keep, bool) and keep > 0 else 1440
    out["keep_seconds"] = str(keep * 60 + 3600)
    comps = image_components(meta)
    for c in IMAGE_COMPONENTS:
        out[f"{c}_file"] = f"${{MODEL_PATH}}/{comps[c]}" if c in comps else ""
    if variant.gpu and cards:
        total = min(int(a.vram_total or 0) for a in cards)
        out["gpu_util"] = f"{gpu_memory_utilization(ctx.estimate.per_gpu, total, gpu_context):.3f}"
        # Two-stage engines (vLLM-Omni TTS): each stage is a vLLM engine on the same card that checks
        # for its own fraction, so the reservation is split: the audio decoder gets its part of the
        # estimate, the talker the rest.
        codec = sum(p.bytes for p in ctx.estimate.parts if p.label == CODEC_LABEL)
        if codec:
            out["gpu_util_main"] = f"{gpu_memory_utilization(ctx.estimate.per_gpu - codec, total, gpu_context):.3f}"
            out["gpu_util_codec"] = f"{gpu_memory_utilization(codec, total, gpu_context):.3f}"
        else:
            out["gpu_util_main"] = out["gpu_util"]
            out["gpu_util_codec"] = "0"
        # What an engine with its own memory budget (stable-diffusion.cpp's --max-vram) may use: the
        # reservation less the CUDA context, in GiB.
        out["gpu_budget_gib"] = f"{max(0.5, (ctx.estimate.per_gpu - gpu_context) / GIB):.1f}"
    else:
        out["gpu_util"] = out["gpu_util_main"] = out["gpu_util_codec"] = "0"
        out["gpu_budget_gib"] = "0"
    return out


def default_size(meta: dict, params: dict) -> tuple[int, int]:
    """An image deployment's default size (its default_size parameter; auto: the model's native square,
    within the deployment's largest side)."""
    side, _ = image_caps(meta, params)
    text = str(params.get("default_size") or "auto")
    if "x" in text:
        w, _, h = text.partition("x")
        if w.isdigit() and h.isdigit() and int(w) > 0 and int(h) > 0:
            return min(int(w), side), min(int(h), side)
    n = min(image_side(meta), side)
    return n, n


def image_components(meta: dict) -> dict:
    img = (meta or {}).get("image")
    comps = img.get("components") if isinstance(img, dict) else None
    return {k: v for k, v in (comps or {}).items() if k in IMAGE_COMPONENTS and isinstance(v, str)}


def mmproj_path(meta: dict) -> str:
    m = (meta or {}).get("mmproj")
    return m["path"] if isinstance(m, dict) and isinstance(m.get("path"), str) else ""


def model_has(ctx: Context, feature: str) -> bool:
    """For `if_model`: the model has a projector file, or takes that medium."""
    meta = ctx.model.meta or {}
    if feature == "mmproj":
        return bool(mmproj_path(meta))
    if feature == "image_pixels":
        return bool(meta.get("vision_tile_px")) and "image" in (modalities.of_model(meta, ctx.model.format) or [])
    if feature in ("image_single", "image_split"):
        img = meta.get("image") if isinstance(meta.get("image"), dict) else {}
        single = bool(img.get("single", True))
        return single if feature == "image_single" else not single
    if feature in IMAGE_COMPONENTS:
        return feature in image_components(meta)
    return feature in (modalities.of_model(meta, ctx.model.format) or [])


def args_for(ctx: Context, variant: Variant, vals: dict) -> list[str]:
    base = list(variant.args if variant.args is not None else ctx.recipe.args)
    for oa in ctx.recipe.optional_args:
        if all(str(ctx.params.get(k)) == str(v) for k, v in oa.if_.items()) and \
                (oa.if_model is None or model_has(ctx, oa.if_model)):
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
