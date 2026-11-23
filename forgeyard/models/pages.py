"""What a catalog model's page and an engine's page show (1.4.3, issue #140), from what the server already keeps.

A model's page (GET /api/models/{name}):
    its files and sizes: the files a machine downloads, recorded on the catalog row when the model was added (the
        agents download exactly these), and the other files of the repository the inspection saw (other GGUF
        quantisations and projector files, other image model files) that are not downloaded;
    each machine: whether its model cache has it (as its agent last reported), with the size there, what uses it and
        any download under way; for each engine that runs it, the variant that machine would use and whether that
        engine's image (its last image listing) or program (its cache report) is there;
    where it would fit now: for each engine and each device it runs on (GPU, CPU), the estimate with the engine's
        defaults and every machine's verdict (the deploy page's estimate, for everyone signed in to read).
An engine's page (GET /api/recipes/{name}): each machine with the variant it would run and whether the image or
program is there, and the deployments and catalog models that use the engine.

Nothing here is stored: the image listings are kept by forgeyard/machines/engineimages.py.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import CatalogModel, Deployment, Node
from forgeyard.machines import cache, engineimages
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import FEATURE_IMAGE_SAVE
from forgeyard.models import cache_keys, prefetch, reach, render, scheduler, sources, trials
from forgeyard.models.catalog import Context, model_block
from forgeyard.models.estimate import EstimateError, estimate as compute_estimate
from forgeyard.models.recipes import Recipe

# The other files of a repository the inspection saw, by the meta key that lists them, and what they are.
OTHER_FILES = (("gguf_files", "other quantisation"), ("mmproj_files", "other projector"),
               ("image_files", "other model file"))


# ------------------------------------------------------------------ a model's files
def files_of(m: CatalogModel) -> dict:
    """{recorded, files: [{path, size, role, downloaded}], downloaded_bytes, downloaded_count}. The downloaded files
    first, in the order the catalog recorded them; then the others."""
    meta = m.meta or {}
    listed = meta.get("files") if isinstance(meta.get("files"), list) else []
    mm = meta.get("mmproj") if isinstance(meta.get("mmproj"), dict) else {}
    comps = ((meta.get("image") or {}).get("components") or {}) if isinstance(meta.get("image"), dict) else {}
    by_comp = {p: role for role, p in comps.items() if isinstance(p, str)}
    out: list[dict] = []
    seen: set[str] = set()
    for f in listed:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or f["path"] in seen:
            continue
        path = f["path"]
        role = ("model file" if path == m.file else "projector" if path == mm.get("path")
                else by_comp[path] if path in by_comp else "")
        out.append({"path": path, "size": f.get("size"), "role": role, "downloaded": True})
        seen.add(path)
    for key, role in OTHER_FILES:
        for f in meta.get(key) or []:
            if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"] not in seen:
                out.append({"path": f["path"], "size": f.get("size"), "role": role, "downloaded": False})
                seen.add(f["path"])
    down = [f for f in out if f["downloaded"]]
    return {"recorded": bool(down), "files": out, "downloaded_count": len(down),
            "downloaded_bytes": sum(int(f["size"] or 0) for f in down)}


# ------------------------------------------------------------------ machines
def machines(db: DbSession) -> list[Node]:
    return [n for n in scheduler.candidates(db, None) if n.revoked_at is None and n.status != "revoked"]


def _key(m: CatalogModel) -> Optional[str]:
    try:
        return sources.cache_key(model_block(m))
    except (sources.SourceError, KeyError, TypeError):
        return None


def cache_entry(db: DbSession, node: Node, m: CatalogModel, key: Optional[str]) -> dict:
    """{reported, cached, bytes, files, used_by}: the model in this machine's cache, as its agent last reported."""
    mc = node.model_cache or {}
    out: dict = {"reported": bool(mc), "cached": False, "bytes": None, "files": None, "used_by": []}
    if not mc or key is None:
        return out
    for e in mc.get("models") or []:
        if e.get("key") == key:
            out.update(cached=True, bytes=e.get("bytes"), files=e.get("files"))
            break
        part = next((p for p in e.get("parts") or [] if p.get("key") == key), None)
        if part is not None:
            out.update(cached=True, bytes=part.get("bytes"), files=1)
            break
    if not out["cached"] and key in (mc.get("cached") or []):
        out["cached"] = True       # an agent from before 1.4: the key, without a size
    if out["cached"]:
        target = cache_keys.parse(key)
        if target is not None:
            out["used_by"] = cache.users_of(db, node.id, target)
    return out


def engine_on(node: Node, recipe: Recipe) -> dict:
    """{variant, runtime, kind (image | program | command), engine_name, present (True | False | None: not known), listed_at,
    note}: the variant of the engine this machine would run, and whether its image or program is there."""
    variant, why = prefetch.variant_for(node, recipe)
    out: dict = {"variant": None, "runtime": None, "kind": None, "engine_name": None, "present": None, "listed_at": None,
                 "note": None}
    if variant is None:
        out["note"] = why
        return out
    out.update(variant=variant.name, runtime=variant.runtime)
    if variant.runtime == "container":
        seen = engineimages.image_on(node, variant.image)
        out.update(kind="image", engine_name=variant.image, present=seen["present"], listed_at=seen["listed_at"])
        if seen["present"] is None:
            out["note"] = ("Its images were never listed: they are once its agent is connected."
                           if not hub.connected(node.id) else "Its images are not listed yet.")
        return out
    block = render.engine_of(recipe, variant)
    if block is None:   # a command installed on the machine
        out.update(kind="command", engine_name=variant.command, present=True)
        return out
    out.update(kind="program", engine_name=block.get("name"))
    out["present"] = True if reach.has_engine(node, block) else (False if node.model_cache else None)
    return out


def node_out(node: Node) -> dict:
    return {"machine_id": node.id, "name": node.name, "status": node.status, "connected": hub.connected(node.id)}


# ------------------------------------------------------------------ where a model fits
def fits(db: DbSession, m: CatalogModel, recipe: Recipe) -> dict:
    """For each device the engine runs on: the estimate with its defaults and each machine's verdict now."""
    from forgeyard.api.deployments import fit_default_context, normalise_params  # deployments imports this module's neighbours
    from fastapi import HTTPException

    out: dict = {}
    devices = (["gpu"] if any(v.gpu for v in recipe.variants) else []) + (
        ["cpu"] if any(not v.gpu for v in recipe.variants) else [])
    for dev in devices:
        try:
            params = normalise_params(recipe, {"gpus": 1 if dev == "gpu" else 0})
            fit_default_context(recipe, {}, params, m.meta or {})
            est = compute_estimate(m.meta or {}, recipe, params, device=dev, gpu_context=scheduler.gpu_context(),
                                   task=m.task)
        except EstimateError as e:
            out[dev] = {"error": str(e)}
            continue
        except HTTPException as e:  # a custom recipe whose defaults do not validate
            out[dev] = {"error": e.detail.get("message") if isinstance(e.detail, dict) else str(e.detail)}
            continue

        class _Dep:  # what rendering needs before a deployment exists
            served_name = m.name

        ctx = Context(_Dep(), m, recipe, params, est, dev, params["share"], params["gpus"], None, 1)  # type: ignore[arg-type]
        pv = scheduler.preview(db, ctx)
        out[dev] = {"estimate": est.as_dict(), "fits": pv["fits"], "machines": pv.get("machines") or []}
    return out


# ------------------------------------------------------------------ the pages
def model_page(db: DbSession, m: CatalogModel, all_recipes: dict) -> dict:
    from forgeyard.api.models import compatible

    names = compatible(m, all_recipes)
    key = _key(m)
    active = {r.node_id: r for r in prefetch.active_rows(db, model_id=m.id)}
    rows = []
    for node in machines(db):
        rows.append({**node_out(node), "cache": cache_entry(db, node, m, key),
                     "download": prefetch.row_out(active[node.id]) if node.id in active else None,
                     "engines": {n: engine_on(node, all_recipes[n][0]) for n in names}})
    deps = db.scalars(select(Deployment).where(Deployment.model_id == m.id, trials.not_trial())
                      .order_by(Deployment.name)).all()
    return {"files": files_of(m), "machines": rows,
            "fits": {n: fits(db, m, all_recipes[n][0]) for n in names},
            "deployments": [{"id": d.id, "name": d.name, "recipe": d.recipe} for d in deps]}


ENGINE_DOWNLOADS = 20   # the engine page's list: those under way and the latest finished ones


def engine_page(db: DbSession, recipe: Recipe) -> dict:
    from forgeyard.db import ModelDownload

    active = {r.node_id: r for r in prefetch.active_rows(db, engine_only=recipe.name)}
    rows = []
    for node in machines(db):
        conn = hub.get(node.id)
        rows.append({**node_out(node), **engine_on(node, recipe),
                     "can_list": conn is not None and FEATURE_IMAGE_SAVE in conn.features,
                     # 1.5.2 (#95): a download of this engine alone under way there.
                     "download": prefetch.row_out(active[node.id]) if node.id in active else None})
    recent = db.scalars(select(ModelDownload).where(ModelDownload.model_id.is_(None), ModelDownload.recipe == recipe.name)
                        .order_by(ModelDownload.id.desc()).limit(ENGINE_DOWNLOADS)).all()
    downloads = sorted(recent, key=lambda r: (r.state not in prefetch.ACTIVE, -r.id))
    deps = db.scalars(select(Deployment).where(Deployment.recipe == recipe.name, trials.not_trial())
                      .order_by(Deployment.name)).all()
    models = [m for m in db.scalars(select(CatalogModel).order_by(CatalogModel.name)).all()
              if m.format in recipe.formats and m.task in recipe.tasks]
    return {"machines": rows, "deployments": [{"id": d.id, "name": d.name, "model_id": d.model_id} for d in deps],
            "models": [{"id": m.id, "name": m.name, "title": m.title} for m in models],
            "downloads": [prefetch.row_out(r) for r in downloads], "caught_up": caught_up(recipe),
            # 1.5.2 (#95, PR 4): its trial running now, and its latest finished one (the history: /trials).
            "trial_running": next((trials.brief_out(t) for t in trials.running(db) if t.recipe == recipe.name), None),
            "last_trial": trials.last_for(db, recipe.name)}


def caught_up(recipe: Recipe) -> Optional[dict]:
    """1.5.2 (#95): a copy whose built-in engine has moved (with a Forgeyard release) to the copy's version or past it:
    {builtin, version, same}, so the page can say the deployments on the copy can switch back. Nothing is done."""
    from forgeyard.models.engineversions import version_key
    from forgeyard.models.recipes import builtin_recipes

    if not recipe.based_on:   # a built-in recipe never has one
        return None
    found = builtin_recipes().get(recipe.based_on)
    if found is None:
        return None
    base = found[0]
    have, mine = version_key(base.version), version_key(recipe.version)
    if not have or not mine or have < mine:
        return None
    return {"builtin": base.name, "title": base.title, "version": base.version, "same": have == mine}


def listable(db: DbSession) -> list[Node]:
    """The machines whose images can be listed now (connected, with a container engine and an agent from 1.4.1)."""
    out = []
    for node in machines(db):
        conn = hub.get(node.id)
        if conn is not None and FEATURE_IMAGE_SAVE in conn.features:
            out.append(node)
    return out
