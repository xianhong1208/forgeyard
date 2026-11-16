"""A machine's model cache as the console shows it (1.4, issue #35): what its agent reported in
`nodes.model_cache`, with the catalog models and the deployments that use each entry, and the
removal (D6): refused while a deployment on that machine uses the model, naming it; asked of the
agent otherwise (`cache_remove`), which checks again before it deletes anything.

Whether a model is on a machine is only ever what its agent says: a model seeded or deleted by hand
shows as it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DbSession

from forgeyard.api.deps import WORKLOAD_ROLES, fail
from forgeyard.db import CatalogModel, Deployment, Node, Replica, User, Workload
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import FEATURE_CACHE_ADMIN, PROTOCOL_VERSION
from forgeyard.models import cache_keys, sources
from forgeyard.workloads.state import ENDED_STATES

# How long the console waits for the agent to delete (unlinking files is quick even for 100 GB).
TIMEOUT = 60.0


@dataclass
class Found:
    target: dict
    title: str
    entry: dict


def find(model_cache: dict, key: str) -> Optional[Found]:
    """The entry the agent reported under this key, and what removing it would delete. A GGUF file
    that is the last model file of its revision takes the whole revision (as the agent does)."""
    for e in model_cache.get("models") or []:
        title = e.get("repo") or e.get("file") or e["key"]
        if e["key"] == key:
            return Found(cache_keys.parse(key), title, e)
        for p in e.get("parts") or []:
            if p["key"] == key:
                target = cache_keys.parse(key)
                if len(e["parts"]) <= 1:
                    target = {k: v for k, v in target.items() if k != "file"}
                return Found(target, f"{title} · {p['file']}", e)
    for e in model_cache.get("engines_detail") or []:
        if e["key"] == key:
            return Found(cache_keys.parse(key), f"engine {e.get('name') or e['id']}", e)
    return None


def _holders(db: DbSession, node_id: int) -> list[tuple[Replica, Optional[Deployment]]]:
    """The replicas on this machine that hold on to their files: placed there, not finished, and
    either wanted running or not yet confirmed stopped."""
    reps = list(db.scalars(
        select(Replica).join(Workload)
        .where(Replica.node_id == node_id, Replica.final.is_(False),
               or_(Workload.desired_state == "running", Replica.state.notin_(ENDED_STATES)))
        .order_by(Replica.id)).all())
    ids = {r.workload_id for r in reps}
    deps = {d.workload_id: d for d in db.scalars(select(Deployment).where(Deployment.workload_id.in_(ids)))} if ids else {}
    return [(r, deps.get(r.workload_id)) for r in reps]


def _who(r: Replica, dep: Optional[Deployment]) -> dict:
    if dep is not None:
        return {"name": dep.name, "kind": "deployment", "id": dep.id}
    return {"name": r.workload.name, "kind": "workload", "id": r.workload_id}


def users_of(db: DbSession, node_id: int, target: dict,
             holders: Optional[list[tuple[Replica, Optional[Deployment]]]] = None) -> list[dict]:
    seen: dict[str, dict] = {}
    for r, dep in holders if holders is not None else _holders(db, node_id):
        if cache_keys.model_uses(r.model, target) or cache_keys.engine_uses(r.engine, target):
            who = _who(r, dep)
            seen.setdefault(f"{who['kind']}:{who['id']}", who)
    return sorted(seen.values(), key=lambda w: w["name"])


def _catalog(db: DbSession, cached: set[str]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Catalog models by their cache key, and Hugging Face ones also by their revision's key: those
    the machine has whole (one GGUF file of a revision is a model of its own)."""
    from forgeyard.models.catalog import model_block

    full: dict[str, list[dict]] = {}
    revision: dict[str, list[dict]] = {}
    for m in db.scalars(select(CatalogModel).order_by(CatalogModel.name)).all():
        try:
            block = model_block(m)
            key = sources.cache_key(block)
        except (sources.SourceError, KeyError, TypeError):
            continue
        ref = {"id": m.id, "name": m.name}
        full.setdefault(key, []).append(ref)
        if m.kind == "hf" and key in cached:
            base = "/".join(key.split("/", 2)[:2])
            if base != key:
                revision.setdefault(base, []).append(ref)
    return full, revision


def _parts_target(entry: dict, part: dict) -> dict:
    target = cache_keys.parse(part["key"])
    if len(entry.get("parts") or []) <= 1:
        target = {k: v for k, v in target.items() if k != "file"}
    return target


def remove_note(node: Node, user: User) -> Optional[str]:
    """Why this person cannot remove anything from this machine's cache right now (None: they can)."""
    if user.role not in WORKLOAD_ROLES:
        return "Only admins and operators can remove models from a machine's cache."
    if FEATURE_CACHE_ADMIN not in (node.features or []):
        return "This machine's agent is too old to remove models from its cache. Update the agent."
    if not hub.connected(node.id):
        return "The agent is not connected, so nothing can be removed now."
    return None


def view(db: DbSession, node: Node, user: User) -> dict:
    mc = dict(node.model_cache or {})
    holders = _holders(db, node.id)
    full, revision = _catalog(db, set(mc.get("cached") or []))
    sizes = "models" in mc
    models = []
    if sizes:
        for e in mc.get("models") or []:
            out = dict(e)
            out["catalog"] = full.get(e["key"], []) + revision.get(e["key"], [])
            out["used_by"] = users_of(db, node.id, cache_keys.parse(e["key"]), holders)
            if e.get("parts"):
                out["parts"] = [{**p, "catalog": full.get(p["key"], []),
                                 "used_by": users_of(db, node.id, _parts_target(e, p), holders)} for p in e["parts"]]
            models.append(out)
    else:
        # An agent from before 1.4 reports only its keys: the list, without sizes and without Remove.
        for key in mc.get("cached") or []:
            target = cache_keys.parse(key)
            if target is None or target.get("file"):
                continue
            out = {"key": key, "kind": target["kind"], "bytes": None,
                   "catalog": full.get(key, []) + revision.get(key, []),
                   "used_by": users_of(db, node.id, target, holders)}
            if target["kind"] == "hf":
                out["revision"] = target["revision"]
                out["repo"] = target["dir"].removeprefix("hub/models--").replace("--", "/", 1)
            models.append(out)
    engines = [{**e, "used_by": users_of(db, node.id, cache_keys.parse(e["key"]), holders)}
               for e in mc.get("engines_detail") or []]
    return {
        "machine_id": node.id,
        "connected": hub.connected(node.id),
        "reported": bool(mc),
        "sizes": sizes,
        "ok": mc.get("ok", False) if mc else None,
        "reason": mc.get("reason"),
        "free_bytes": mc.get("free_bytes"),
        "total_bytes": mc.get("total_bytes"),
        "used_bytes": mc.get("used_bytes"),
        "partial_bytes": mc.get("partial_bytes"),
        "partial_files": mc.get("partial_files", 0),
        "truncated": bool(mc.get("truncated")),
        "models": models,
        "engines": engines,
        "remove_note": remove_note(node, user) if sizes else (
            "This machine's agent reports no sizes and cannot remove models: update the agent." if mc else None),
    }


def ask(node: Node, message: dict) -> dict:
    """Send a removal to the agent and wait for its answer; a refusal or a failure becomes the error
    the console shows."""
    conn = hub.get(node.id)
    if conn is None:
        raise fail(409, "machine_offline", f"{node.name}'s agent is not connected. Try again once it is online.")
    if FEATURE_CACHE_ADMIN not in conn.features:
        raise fail(409, "agent_too_old", f"{node.name}'s agent is too old to remove models from its cache. "
                                         "Update the agent.")
    fut = conn.request({"v": PROTOCOL_VERSION, **message})
    try:
        answer = fut.result(timeout=TIMEOUT)
    except Exception:
        raise fail(409, "agent_no_answer", f"{node.name}'s agent did not answer in time. Look at the machine's "
                                           "cache again in a moment: the removal may still have happened.") from None
    finally:
        conn.forget(fut)
    if not answer.get("ok"):
        code = answer.get("code") or "failed"
        reason = answer.get("reason") or "The agent could not remove it."
        if code == "in_use":
            raise fail(409, "model_in_use", f"{node.name}: {reason}", in_use=answer.get("in_use") or [])
        if code == "busy":
            raise fail(409, "cache_busy", f"{node.name}: {reason}")
        if code == "not_found":
            raise fail(404, "not_in_cache", f"{node.name}: {reason}")
        raise fail(409, "cache_remove_failed", f"{node.name}: {reason}")
    return answer
