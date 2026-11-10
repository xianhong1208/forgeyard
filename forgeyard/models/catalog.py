"""Recipes (built-in and custom), the source allowlists, and what a deployment needs at hand."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.config import get_settings
from forgeyard.db import CatalogModel, CustomRecipe, Deployment, ServerSetting, utcnow
from forgeyard.models import fetch, recipes
from forgeyard.models.estimate import Estimate
from forgeyard.models.recipes import Recipe, RecipeError

SOURCES_KEY = "model_sources"


# ------------------------------------------------------------------ recipes
def all_recipes(db: DbSession) -> dict[str, tuple[Recipe, str]]:
    """{name: (recipe, yaml)}: the built-in ones, then the custom ones (which cannot reuse a
    built-in name). A custom recipe that no longer validates is skipped."""
    out = dict(recipes.builtin_recipes())
    for row in db.scalars(select(CustomRecipe).order_by(CustomRecipe.name)).all():
        if row.name in out:
            continue
        try:
            out[row.name] = (recipes.parse_recipe(row.yaml), row.yaml)
        except RecipeError:
            continue
    return out


def get_recipe(db: DbSession, name: str) -> Optional[Recipe]:
    found = all_recipes(db).get(name)
    return found[0] if found else None


# ------------------------------------------------------------------ source settings
def _split(text: str) -> list[str]:
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def get_sources(db: DbSession) -> dict:
    row = db.get(ServerSetting, SOURCES_KEY)
    s = get_settings()
    value = row.value if row is not None else {"url_hosts": _split(s.model_url_hosts),
                                                "local_dirs": _split(s.model_local_dirs)}
    return {"url_hosts": list(value.get("url_hosts") or []), "local_dirs": list(value.get("local_dirs") or []),
            "hf_endpoint": s.hf_endpoint.rstrip("/"), "allow_private_hosts": s.model_allow_private_hosts}


def set_sources(db: DbSession, url_hosts: list[str], local_dirs: list[str], by: str) -> None:
    row = db.get(ServerSetting, SOURCES_KEY)
    if row is None:
        row = ServerSetting(key=SOURCES_KEY)
        db.add(row)
    row.value = {"url_hosts": url_hosts, "local_dirs": local_dirs}
    row.updated_by = by
    row.updated_at = utcnow()


def hf_policy() -> fetch.Policy:
    return fetch.Policy(allow_private=get_settings().model_allow_private_hosts)


def url_policy(db: DbSession) -> fetch.Policy:
    return fetch.Policy(allow_private=get_settings().model_allow_private_hosts,
                        allowed_hosts=get_sources(db)["url_hosts"])


# ------------------------------------------------------------------ a deployment, ready to place
@dataclass
class Context:
    deployment: Optional[Deployment]
    model: CatalogModel
    recipe: Recipe
    params: dict
    estimate: Estimate
    device: str          # gpu | cpu
    share: str           # exclusive | vram
    gpus: int
    machine_id: Optional[int]
    replicas: int = 1

    @property
    def amount(self) -> int:
        return self.estimate.amount


def model_block(model: CatalogModel, endpoint: Optional[str] = None) -> dict:
    """What the agent needs to fetch the model (never a token: that is added when sending)."""
    meta = model.meta or {}
    block = {"kind": model.kind, "format": model.format, "file": model.file}
    if model.kind == "hf":
        block.update(repo=meta.get("repo"), revision=model.revision, ref=model.ref,
                     endpoint=(endpoint or get_settings().hf_endpoint).rstrip("/"),
                     files=[{k: f[k] for k in ("path", "size", "sha256", "oid") if k in f}
                            for f in meta.get("files") or []])
    elif model.kind == "url":
        block.update(url=meta.get("url"), files=[{"path": f["path"], "size": f["size"]}
                                                 for f in meta.get("files") or []])
    else:
        block.update(path=meta.get("path"))
    return block


def estimate_from_dict(d: dict) -> Estimate:
    from forgeyard.models.estimate import Part

    est = Estimate(d["method"], d["device"], int(d.get("gpus") or 0), per_gpu=int(d.get("per_gpu") or 0),
                   ram=int(d.get("ram") or 0), notes=list(d.get("notes") or []), kv_tokens=int(d.get("kv_tokens") or 0))
    est.parts = [Part(p["label"], int(p["bytes"]), p.get("detail", "")) for p in d.get("parts") or []]
    return est


def context_for(db: DbSession, dep: Deployment) -> Optional[Context]:
    recipe = get_recipe(db, dep.recipe)
    if recipe is None:
        return None
    est = estimate_from_dict(dep.estimate)
    params = dict(dep.params or {})
    spec = dep.workload.spec or {}
    return Context(dep, dep.model, recipe, params, est, est.device, params.get("share") or recipe.share,
                   est.gpus, spec.get("machine_id"), int(spec.get("replicas") or 1))


def deployment_of(db: DbSession, workload_id: int) -> Optional[Deployment]:
    return db.scalar(select(Deployment).where(Deployment.workload_id == workload_id))
