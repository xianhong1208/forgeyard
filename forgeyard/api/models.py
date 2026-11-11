"""The model catalog, recipes and model sources.

Everyone signed in sees the catalog and the recipes. Admins add and remove models, add, change
and remove custom recipes, and set which URL hosts and local directories models may come from.
Operators deploy (forgeyard.api.deployments).

Adding a model starts from the recommended list (forgeyard/models/recommended.yaml), a Hugging Face
search run by the server, or a source typed in. Inspecting one also says which recipe would serve
it, what one replica would reserve, and on which machines it would fit right now.
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, can_run_workloads, current, fail, require_admin
from forgeyard.config import get_settings
from forgeyard.db import CatalogModel, CustomRecipe, Deployment, Node, get_db, utcnow
from forgeyard.models import (catalog, hub as hf_hub, inspect as inspector, modalities, recipes, recommended as rec,
                              scheduler, sources)
from forgeyard.models.catalog import Context
from forgeyard.models.estimate import EstimateError, estimate as compute_estimate
from forgeyard.models.recipes import Recipe, RecipeError
from forgeyard.security.ratelimit import limiter, retry_after
from forgeyard.workloads import placement

router = APIRouter(prefix="/api", tags=["models"])

MODEL_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?$")
TASK_LABEL = {"chat": "Chat", "embed": "Embeddings", "rerank": "Rerank", "asr": "Speech to text"}


def _engine_args(raw) -> dict:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > 16:
        raise fail(422, "invalid_engine_args", "engine_args: {recipe name: [arguments]}")
    out = {}
    for name, args in raw.items():
        if not isinstance(name, str) or not recipes.NAME_RE.match(name):
            raise fail(422, "invalid_engine_args", f"engine_args: {name!r} is not a recipe name")
        if not isinstance(args, list) or len(args) > 32 or not all(isinstance(a, str) for a in args):
            raise fail(422, "invalid_engine_args", f"engine_args.{name}: a list of at most 32 strings")
        for a in args:
            if len(a) > 4096 or any(c in a for c in "\x00\r\n"):
                raise fail(422, "invalid_engine_args", f"engine_args.{name}: one line each, at most 4096 characters")
        out[name] = list(args)
    return out


def compatible(model: CatalogModel, all_recipes: dict[str, tuple[Recipe, str]]) -> list[str]:
    return [n for n, (r, _) in all_recipes.items() if model.format in r.formats and model.task in r.tasks]


def model_summary(m: CatalogModel) -> dict:
    meta = m.meta or {}
    out: dict = {}
    if meta.get("config"):
        c = meta["config"]
        out.update(layers=c.get("layers"), kv_heads=c.get("kv_heads"), head_dim=c.get("head_dim"),
                   max_context=c.get("max_context"), dtype=c.get("dtype"), architecture=(c.get("architectures") or [None])[0])
    if meta.get("weights"):
        out["params"] = sum((meta["weights"].get("params") or {}).values())
        out["weights_bytes"] = meta["weights"].get("bytes")
    if meta.get("gguf"):
        g = meta["gguf"]
        out.update(layers=g.get("layers"), kv_heads=g.get("kv_heads"), max_context=g.get("context_length"),
                   architecture=g.get("architecture"), params=g.get("elements"), weights_bytes=g.get("tensor_bytes"))
    if meta.get("weights_bytes"):
        out["weights_bytes"] = meta["weights_bytes"]
    return out


def model_out(m: CatalogModel, all_recipes: dict, deployments: int = 0) -> dict:
    return {"id": m.id, "name": m.name, "title": m.title, "source": m.source, "kind": m.kind, "ref": m.ref,
            "revision": m.revision, "file": m.file, "format": m.format, "task": m.task,
            "task_label": TASK_LABEL.get(m.task, m.task), "size_bytes": m.size_bytes, "engine_args": m.engine_args or {},
            "summary": model_summary(m), "recipes": compatible(m, all_recipes), "deployments": deployments,
            "gguf_files": (m.meta or {}).get("gguf_files") or [], "created_by": m.created_by, "created_at": m.created_at,
            **media_out(m.meta or {}, m.format)}


def media_out(meta: dict, fmt: str) -> dict:
    """Licence, gated, the media it takes (None: unknown, a catalog entry from before 1.3) and its
    projector file, for the catalog and the inspection preview."""
    mm = meta.get("mmproj") if isinstance(meta.get("mmproj"), dict) else None
    return {"license": meta.get("license"), "gated": bool(meta.get("gated")),
            "modalities": modalities.of_model(meta, fmt),
            "mmproj": {"path": mm.get("path"), "size": mm.get("size")} if mm else None,
            "mmproj_files": meta.get("mmproj_files") or []}


def recipe_out(r: Recipe, text: Optional[str] = None) -> dict:
    out = {"name": r.name, "title": r.title, "description": r.description, "engine": r.engine, "version": r.version,
           "tasks": r.tasks, "formats": r.formats, "api": r.api, "share": r.share, "builtin": r.builtin,
           "estimate": r.estimate.method,
           "media": r.media.model_dump() if r.media is not None else None,
           "params": {k: p.model_dump(exclude_none=True) for k, p in r.params.items()},
           "devices": sorted({"gpu" if v.gpu else "cpu" for v in r.variants}),
           "variants": [{"name": v.name, "vendor": v.when.vendor, "runtime": v.runtime, "image": v.image,
                         "arch": v.when.arch, "compute_capability": v.when.compute_capability,
                         "driver": v.when.driver,
                         "engine": v.engine.url if v.engine else v.command} for v in r.variants]}
    if text is not None:
        out["yaml"] = text
    return out


def _counts(db: DbSession) -> dict[int, int]:
    return dict(db.execute(select(Deployment.model_id, func.count()).group_by(Deployment.model_id)).all())


@router.get("/models")
def list_models(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    all_r = catalog.all_recipes(db)
    counts = _counts(db)
    rows = db.scalars(select(CatalogModel).order_by(CatalogModel.name)).all()
    out = {"models": [model_out(m, all_r, counts.get(m.id, 0)) for m in rows],
           "recipes": [recipe_out(r) for r, _ in all_r.values()],
           "can_manage": caller.user.role == "admin", "can_deploy": can_run_workloads(caller.user)}
    if caller.user.role == "admin":
        out["sources"] = catalog.get_sources(db)
    return out


class InspectIn(BaseModel):
    source: str = Field(max_length=1024)
    revision: Optional[str] = Field(default=None, max_length=128)
    file: Optional[str] = Field(default=None, max_length=512)
    # GGUF vision models: the projector file (mmproj-*.gguf) to use with it. Not given: the
    # repository's F16 one (else BF16, Q8_0, the first) when it has any; "none": none.
    mmproj: Optional[str] = Field(default=None, max_length=512)
    # Gated repositories: used for these requests only, never stored with the catalog entry.
    token: Optional[str] = Field(default=None, max_length=512)
    format: Optional[str] = Field(default=None, max_length=16)       # local models
    size_bytes: Optional[int] = Field(default=None, ge=0)             # local models


class ModelIn(InspectIn):
    name: str = Field(max_length=64)
    title: Optional[str] = Field(default=None, max_length=120)
    task: Optional[str] = Field(default=None, max_length=16)
    engine_args: Optional[dict] = None


def _inspect(db: DbSession, body: InspectIn) -> dict:
    try:
        src = sources.parse_source(body.source)
    except sources.SourceError as e:
        raise fail(422, "invalid_source", str(e)) from None
    s = catalog.get_sources(db)
    try:
        return inspector.inspect_source(src, ref=body.revision, file=body.file, endpoint=s["hf_endpoint"],
                                        policy=catalog.hf_policy(), url_policy=catalog.url_policy(db),
                                        local_dirs=s["local_dirs"], token=(body.token or "").strip() or None,
                                        fmt=body.format, size_bytes=body.size_bytes, mmproj=body.mmproj)
    except (inspector.InspectError, sources.SourceError) as e:
        raise fail(422, "cannot_inspect", str(e)) from None


class InspectPreviewIn(InspectIn):
    # What the suggestion is for: the task chosen (default: what Hugging Face says), the recipe
    # and device to estimate with (default: the first recipe that serves it, on a GPU if one is free).
    task: Optional[str] = Field(default=None, max_length=16)
    recipe: Optional[str] = Field(default=None, max_length=40)
    device: Optional[str] = Field(default=None, max_length=8)


def _gpu_available(db: DbSession) -> bool:
    """Some online machine has a GPU Forgeyard may use."""
    nodes = db.scalars(select(Node).where(Node.status == "online").options(selectinload(Node.accelerators))).all()
    return any(placement.is_gpu(a) and placement.is_allowed(n, a) for n in nodes for a in n.accelerators)


def suggest(db: DbSession, meta: dict, task: Optional[str], recipe_name: Optional[str] = None,
            device: Optional[str] = None) -> dict:
    """The recipe to serve an inspected (not yet added) model with, what one replica would reserve
    with that recipe's defaults, and where it would fit now."""
    from forgeyard.api.deployments import normalise_params  # deployments imports this module's neighbours

    all_r = catalog.all_recipes(db)
    fmt = meta.get("format")
    usable = [n for n, (r, _) in all_r.items() if fmt in r.formats and (task is None or task in r.tasks)]
    if not usable:
        what = f"{fmt} {TASK_LABEL.get(task, task).lower()}" if task else str(fmt)
        return {"recipe": None, "recipes": [], "error": f"No recipe serves {what} models."}
    name = recipe_name if recipe_name in usable else usable[0]
    recipe = all_r[name][0]
    has_gpu = any(v.gpu for v in recipe.variants)
    has_cpu = any(not v.gpu for v in recipe.variants)
    if device == "cpu" and has_cpu or device == "gpu" and has_gpu:
        dev = device
    else:
        dev = "gpu" if has_gpu and (not has_cpu or _gpu_available(db)) else "cpu"
    out: dict = {"recipe": name, "recipes": usable, "device": dev, "devices": (["gpu"] if has_gpu else []) + (["cpu"] if has_cpu else [])}
    try:
        params = normalise_params(recipe, {"gpus": 1 if dev == "gpu" else 0})
        est = compute_estimate(meta, recipe, params, device=dev, gpu_context=scheduler.gpu_context())
    except EstimateError as e:
        out["error"] = str(e)
        return out
    except HTTPException as e:  # normalise_params: a custom recipe whose defaults do not validate
        out["error"] = e.detail.get("message") if isinstance(e.detail, dict) else str(e.detail)
        return out
    model = CatalogModel(name="new-model", source=meta.get("repo") or "", kind=meta.get("kind") or "hf",
                         revision=meta.get("revision"), file=meta.get("file"), format=fmt, task=task or "chat",
                         meta=meta, engine_args={})

    class _Dep:  # what rendering needs before the deployment exists
        served_name = "new-model"

    ctx = Context(_Dep(), model, recipe, params, est, dev, params["share"], params["gpus"], None, 1)  # type: ignore[arg-type]
    pv = scheduler.preview(db, ctx)
    machines = [{"name": m["name"], "status": m["status"], "fits": m["fits"], "reason": m["reason"],
                 "gpus": m.get("gpus") or [], "short_by": m.get("short_by")} for m in pv.get("machines") or []]
    out.update(params=params, estimate=est.as_dict(), fits=pv["fits"], machines=machines,
               summary=pv.get("summary"))
    return out


@router.post("/models/inspect")
def inspect_model(body: InspectPreviewIn, _: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    meta = _inspect(db, body)
    all_r = catalog.all_recipes(db)
    fmt = meta.get("format")
    task = body.task if body.task in recipes.TASKS else meta.get("task_hint")
    fits = [n for n, (r, _) in all_r.items() if fmt in r.formats and (task is None or task in r.tasks)]
    preview = {k: v for k, v in meta.items() if k not in ("files",)}
    preview["file_count"] = len(meta.get("files") or [])
    preview.update(media_out(meta, fmt))
    return {"meta": preview, "recipes": fits,
            "suggestion": suggest(db, meta, task, body.recipe, body.device if body.device in ("gpu", "cpu") else None)}


# ------------------------------------------------------------------ recommended models, Hugging Face search
def _catalog_names(db: DbSession) -> dict[tuple[str, Optional[str]], str]:
    """(source, GGUF file) -> catalog name, for "already in the catalog"."""
    return {(m.source, m.file): m.name for m in db.scalars(select(CatalogModel)).all()}


@router.get("/models/recommended")
def recommended_models(_: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    try:
        data = rec.recommended()
    except rec.RecommendedError as e:
        raise fail(500, "recommended_broken", f"The recommended models file is broken: {e}") from None
    have = _catalog_names(db)
    return {"groups": [g.model_dump() for g in data.groups],
            "models": [e.model_dump() | {"in_catalog": have.get((e.source, e.file))} for e in data.models]}


# A person searching sends a request per keystroke pause; Hugging Face is asked at most this often.
SEARCH_PER_MINUTE = 30


@router.get("/models/search")
def search_hub(q: str = Query(default="", max_length=200), task: Optional[str] = Query(default=None, max_length=16),
               format: Optional[str] = Query(default=None, max_length=16), limit: int = Query(default=20),
               caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    """Search Hugging Face through the server (admins: the people who add models)."""
    try:
        query = hf_hub.make_query(q, task, format, limit)
    except ValueError as e:
        raise fail(422, "bad_search", str(e)) from None
    wait = limiter.hit(f"hub-search:{caller.user.id}", SEARCH_PER_MINUTE)
    if wait:
        raise fail(429, "rate_limited", "Too many searches in a minute. Wait a moment.", retry_after=retry_after(wait))
    s = get_settings()
    try:
        results, cached = hf_hub.hub.search(s.hf_endpoint, query, hf_hub.policy(s.model_allow_private_hosts))
    except hf_hub.HubError as e:
        raise fail(503, "hub_unreachable",
                   f"Cannot reach Hugging Face from the server ({e}). The recommended models still work; "
                   "a model can also be added by its source.") from None
    have = {src: name for (src, _f), name in _catalog_names(db).items()}
    all_r = catalog.all_recipes(db)
    out = []
    for r in results:
        r = dict(r)
        r["in_catalog"] = have.get(r["source"])
        r["recipes"] = [n for n, (rc, _) in all_r.items() if r["format"] in rc.formats and (r["task"] is None or r["task"] in rc.tasks)]
        out.append(r)
    return {"results": out, "cached": cached, "endpoint": s.hf_endpoint.rstrip("/")}


@router.post("/models", status_code=201)
def add_model(body: ModelIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    name = body.name.strip().lower()
    if not MODEL_NAME_RE.match(name):
        raise fail(422, "bad_name", "name: 1-64 characters, a-z, 0-9, '.', '_' and '-'")
    if db.scalar(select(CatalogModel.id).where(CatalogModel.name == name)) is not None:
        raise fail(409, "name_taken", f"The catalog already has a model named {name}.")
    engine_args = _engine_args(body.engine_args)
    meta = _inspect(db, body)
    task = (body.task or meta.get("task_hint") or "").strip()
    if task not in recipes.TASKS:
        raise fail(422, "task_required", "task: say what the model does (chat, embed, rerank or asr)")
    src = sources.parse_source(body.source)
    m = CatalogModel(name=name, title=(body.title or "").strip() or None, source=str(src), kind=src.kind,
                     ref=meta.get("ref"), revision=meta.get("revision"), file=meta.get("file"),
                     format=meta["format"], task=task, meta=meta, engine_args=engine_args,
                     size_bytes=int(meta.get("size_bytes") or 0), created_by=caller.user.username)
    db.add(m)
    db.flush()
    audit.record(db, "model.added", actor=caller.user, target=name, ip=caller.ip, model_id=m.id, source=m.source,
                 revision=m.revision, file=m.file, task=task)
    db.commit()
    return {"model": model_out(m, catalog.all_recipes(db))}


class ModelPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=120)
    task: Optional[str] = Field(default=None, max_length=16)
    engine_args: Optional[dict] = None


@router.patch("/models/{model_id}")
def change_model(model_id: int, body: ModelPatch, caller: Caller = Depends(require_admin),
                 db: DbSession = Depends(get_db)):
    m = db.get(CatalogModel, model_id)
    if m is None:
        raise fail(404, "not_found", "No such model.")
    changed = {}
    if body.title is not None:
        m.title = body.title.strip() or None
        changed["title"] = m.title
    if body.task is not None:
        if body.task not in recipes.TASKS:
            raise fail(422, "bad_task", "task: chat, embed, rerank or asr")
        m.task = body.task
        changed["task"] = m.task
    if body.engine_args is not None:
        m.engine_args = _engine_args(body.engine_args)
        changed["engine_args"] = m.engine_args
    audit.record(db, "model.changed", actor=caller.user, target=m.name, ip=caller.ip, model_id=m.id, **changed)
    db.commit()
    return {"model": model_out(m, catalog.all_recipes(db), _counts(db).get(m.id, 0))}


@router.delete("/models/{model_id}")
def remove_model(model_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    m = db.get(CatalogModel, model_id)
    if m is None:
        raise fail(404, "not_found", "No such model.")
    used = db.scalars(select(Deployment.name).where(Deployment.model_id == m.id)).all()
    if used:
        raise fail(409, "model_in_use", f"{m.name} is deployed as {', '.join(used)}. Delete those deployments first.")
    name = m.name
    db.delete(m)
    audit.record(db, "model.removed", actor=caller.user, target=name, ip=caller.ip, model_id=model_id)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ recipes
@router.get("/recipes")
def list_recipes(_: Caller = Depends(current), db: DbSession = Depends(get_db)):
    return {"recipes": [recipe_out(r, t) for r, t in catalog.all_recipes(db).values()]}


@router.get("/recipes/{name}")
def get_recipe(name: str, _: Caller = Depends(current), db: DbSession = Depends(get_db)):
    found = catalog.all_recipes(db).get(name)
    if found is None:
        raise fail(404, "not_found", "No such recipe.")
    return {"recipe": recipe_out(*found)}


class RecipeIn(BaseModel):
    yaml: str = Field(max_length=recipes.MAX_RECIPE_BYTES)


def _parse(text: str) -> Recipe:
    try:
        return recipes.parse_recipe(text)
    except RecipeError as e:
        raise fail(422, "invalid_recipe", str(e)) from None


@router.post("/recipes", status_code=201)
def add_recipe(body: RecipeIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    r = _parse(body.yaml)
    if r.name in recipes.builtin_recipes():
        raise fail(409, "builtin_name", f"{r.name} is a built-in recipe; give yours another name.")
    if db.scalar(select(CustomRecipe.id).where(CustomRecipe.name == r.name)) is not None:
        raise fail(409, "name_taken", f"A recipe named {r.name} already exists.")
    db.add(CustomRecipe(name=r.name, yaml=body.yaml, created_by=caller.user.username))
    audit.record(db, "recipe.created", actor=caller.user, target=r.name, ip=caller.ip, engine=r.engine,
                 images=[v.image or (v.engine.url if v.engine else v.command) for v in r.variants])
    db.commit()
    return {"recipe": recipe_out(r, body.yaml)}


@router.put("/recipes/{name}")
def change_recipe(name: str, body: RecipeIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    row = db.scalar(select(CustomRecipe).where(CustomRecipe.name == name))
    if row is None:
        if name in recipes.builtin_recipes():
            raise fail(409, "builtin", "Built-in recipes cannot be changed; add a custom one with another name.")
        raise fail(404, "not_found", "No such recipe.")
    r = _parse(body.yaml)
    if r.name != name:
        raise fail(422, "name_changed", "The recipe's name cannot change; add a new recipe instead.")
    row.yaml = body.yaml
    row.updated_at = utcnow()
    audit.record(db, "recipe.updated", actor=caller.user, target=name, ip=caller.ip,
                 images=[v.image or (v.engine.url if v.engine else v.command) for v in r.variants])
    db.commit()
    return {"recipe": recipe_out(r, body.yaml)}


@router.delete("/recipes/{name}")
def remove_recipe(name: str, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    row = db.scalar(select(CustomRecipe).where(CustomRecipe.name == name))
    if row is None:
        if name in recipes.builtin_recipes():
            raise fail(409, "builtin", "Built-in recipes cannot be removed.")
        raise fail(404, "not_found", "No such recipe.")
    used = db.scalars(select(Deployment.name).where(Deployment.recipe == name)).all()
    if used:
        raise fail(409, "recipe_in_use", f"{name} is used by {', '.join(used)}. Delete those deployments first.")
    db.delete(row)
    audit.record(db, "recipe.deleted", actor=caller.user, target=name, ip=caller.ip)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ sources
class SourcesIn(BaseModel):
    url_hosts: list[str] = Field(default_factory=list, max_length=64)
    local_dirs: list[str] = Field(default_factory=list, max_length=64)


_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+$")


@router.get("/model-sources")
def get_sources(_: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    return {"sources": catalog.get_sources(db)}


@router.put("/model-sources")
def set_sources(body: SourcesIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    hosts = []
    for h in body.url_hosts:
        h = h.strip().lower().rstrip(".")
        if not h:
            continue
        if not _HOST_RE.match(h):
            raise fail(422, "bad_host", f"{h!r} is not a host name (example.org or *.example.org)")
        hosts.append(h)
    dirs = []
    for d in body.local_dirs:
        d = d.strip()
        if not d:
            continue
        norm = re.sub(r"/+", "/", d).rstrip("/")
        if not norm.startswith("/") or norm in ("", "/") or ".." in norm.split("/") or "\x00" in norm:
            raise fail(422, "bad_dir", f"{d!r}: an absolute directory other than /")
        dirs.append(norm)
    before = catalog.get_sources(db)
    catalog.set_sources(db, sorted(set(hosts)), sorted(set(dirs)), caller.user.username)
    audit.record(db, "model_sources.changed", actor=caller.user, ip=caller.ip,
                 before={"url_hosts": before["url_hosts"], "local_dirs": before["local_dirs"]},
                 after={"url_hosts": sorted(set(hosts)), "local_dirs": sorted(set(dirs))})
    db.commit()
    return {"sources": catalog.get_sources(db)}
