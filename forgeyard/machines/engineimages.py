"""A machine's engine images on its page, and removing one it no longer uses (1.4.2, issue #123; agent feature
"image_remove").

    console  GET  /api/machines/{id}/engine-images            the images a recipe names, as the agent lists them now
    console  POST /api/machines/{id}/engine-images/remove {image}   admins only
    server   lists the machine's images again (image_list), and refuses before asking the agent:
               an id that is not in that listing (the agent only ever acts on an id of its own listing),
               an image no recipe names (not Forgeyard's to delete: someone else's image on a shared host),
               the image the agent itself runs from,
               an image a replica on that machine uses (409 with the deployments, as the model cache does)
    server   -> agent: image_remove {request_id, image}
    agent    checks again (its own image, its replicas, any container of the engine), then `docker rmi` /
             `podman rmi` without -f; answers image_removed
    server   audits `node.image_removed` (every removal the agent was asked for, with how it ended)

An image is shown when a recipe names it: a pinned image recognised by its recorded ids, or a tag-only image by its
tag (forgeyard/machines/upload.py recipe_matches). The other images of the engine are only counted.

1.4.3 (issue #140): the server keeps each agent's last listing in `nodes.runtime_info["images"]` ({at, items: [{id,
tags, content, size}]}), so an engine's page says which machines have its image, also while a machine is offline. Every
`image_listed` answer replaces it; a removal the agent confirms and an upload it loaded update it. A hello keeps it (the
agent sends no such key: its own runtime_info is filtered, forgeyard/machines/messages.py).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from sqlalchemy.orm import Session as DbSession

from forgeyard.api.deps import fail
from forgeyard.db import Deployment, Node, Replica, User, utcnow
from forgeyard.machines import cache, upload
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import FEATURE_IMAGE_REMOVE, FEATURE_IMAGE_SAVE, PROTOCOL_VERSION
from forgeyard.workloads import images

# Removing a large image takes a while on a slow disk.
REMOVE_TIMEOUT = 300.0
# 1.4.3 (#140): the key of the kept listing in nodes.runtime_info, and how many images of it are kept.
LISTING_KEY = "images"
MAX_KEPT = 256


def _kept_item(i) -> Optional[dict]:
    """The part of a listed image the matching needs (its id, tags, content id) and its size."""
    if not isinstance(i, dict) or not isinstance(i.get("id"), str) or not i["id"]:
        return None
    size = i.get("size")
    return {"id": i["id"][:80], "tags": [str(t)[:255] for t in list(i.get("tags") or [])[:16]],
            "content": i.get("content") if isinstance(i.get("content"), str) else None,
            "size": size if isinstance(size, int) and size >= 0 else None}


def remember(node: Node, answer: dict) -> None:
    """Keep the agent's listing (its answer to image_list) as the machine's last one."""
    items = [k for k in (_kept_item(i) for i in list(answer.get("images") or [])[:MAX_KEPT]) if k is not None]
    info = dict(node.runtime_info or {})
    info[LISTING_KEY] = {"at": utcnow().isoformat(), "items": items}
    node.runtime_info = info


def kept(node: Node) -> Optional[dict]:
    """The machine's last listing ({at, items}), or None when it was never listed."""
    got = (node.runtime_info or {}).get(LISTING_KEY)
    return got if isinstance(got, dict) and isinstance(got.get("items"), list) else None


def forget(node: Node, image_id: str) -> None:
    """An image the agent removed leaves the kept listing."""
    got = kept(node)
    if got is None:
        return
    info = dict(node.runtime_info or {})
    info[LISTING_KEY] = {**got, "items": [i for i in got["items"] if i.get("id") != image_id]}
    node.runtime_info = info


def add_loaded(node: Node, loaded: list) -> None:
    """Images an upload or a copy loaded join the kept listing (when there is one; else the next listing has them)."""
    got = kept(node)
    if got is None:
        return
    items = [i for i in got["items"]]
    have = {i.get("id") for i in items}
    for raw in loaded:
        k = _kept_item(raw)
        if k is not None and k["id"] not in have and len(items) < MAX_KEPT:
            items.append(k)
            have.add(k["id"])
    info = dict(node.runtime_info or {})
    info[LISTING_KEY] = {**got, "items": items}
    node.runtime_info = info


def image_on(node: Node, image: Optional[str]) -> dict:
    """Whether this machine has the image, by its last listing: {present: True | False | None (never listed), listed_at,
    size}. An image a replica pulled there since the server started counts as present (forgeyard/models/reach.py)."""
    from forgeyard.models import reach

    got = kept(node)
    item = next((i for i in got["items"] if refers(i, image)), None) if got is not None and image else None
    present: Optional[bool] = None if got is None else item is not None
    if not present and image and reach.has_image(node, image):
        present = True
    return {"present": present, "listed_at": got.get("at") if got is not None else None,
            "size": item.get("size") if item is not None else None}


def replica_image(r: Replica) -> Optional[str]:
    """The container image a replica runs (its rendered spec, else its workload's), or None."""
    spec = r.spec or (r.workload.spec if r.workload is not None else None) or {}
    runtime = spec.get("runtime") or {}
    return runtime.get("image") if runtime.get("type") == "container" else None


def refers(item: dict, image: Optional[str]) -> bool:
    """Whether a replica whose spec names `image` uses this listed image: by the recorded ids of a pinned image, or by
    its tag (a loaded copy keeps the tag; a tag-only image is only ever its tag). Errs on the side of "in use"."""
    if not image:
        return False
    from forgeyard.workloads.registry import full_name, tag_ref

    check = images.check_for(image)
    if check and (item.get("id") in check["ids"] or (item.get("content") and item["content"] in check["content"])):
        return True
    return full_name(tag_ref(image)) in {full_name(t) for t in item.get("tags") or []}


def users_of(db: DbSession, node_id: int, item: dict,
             holders: Optional[list[tuple[Replica, Optional[Deployment]]]] = None) -> list[dict]:
    seen: dict[str, dict] = {}
    for r, dep in holders if holders is not None else cache._holders(db, node_id):
        if refers(item, replica_image(r)):
            who = cache._who(r, dep)
            seen.setdefault(f"{who['kind']}:{who['id']}", who)
    return sorted(seen.values(), key=lambda w: w["name"])


def remove_note(node: Node, user: User) -> Optional[str]:
    """Why this person cannot remove an engine image from this machine now (None: they can)."""
    if user.role != "admin":
        return "Only admins can remove engine images."
    if FEATURE_IMAGE_REMOVE not in (node.features or []):
        return "This machine's agent is too old to remove engine images. Update the agent."
    if not hub.connected(node.id):
        return "The agent is not connected, so nothing can be removed now."
    return None


def listing_note(node: Node) -> Optional[str]:
    """Why this machine's images cannot be listed now (None: they can)."""
    if not hub.connected(node.id):
        return "The agent is not connected: its engine images are listed once it is."
    conn = hub.get(node.id)
    if conn is None or FEATURE_IMAGE_SAVE not in conn.features:
        return ("This machine's agent cannot list its engine images: it has no container engine, or it is older than "
                "1.4.1 (update the agent).")
    return None


def view(db: DbSession, node: Node, user: User, answer: dict) -> dict:
    """The images a recipe names, each with the recipes, what uses it on this machine, and whether it can go."""
    holders = cache._holders(db, node.id)
    own = images.normalize(answer.get("agent_image"))
    note = remove_note(node, user)
    out, others = [], 0
    for item in list(answer.get("images") or [])[:256]:
        matches = upload.recipe_matches(db, item)
        if not matches:
            others += 1
            continue
        used = users_of(db, node.id, item, holders)
        agent = own is not None and item.get("id") == own
        why = note
        if why is None and agent:
            why = "The agent runs from this image."
        if why is None and used:
            names = ", ".join(u["name"] for u in used)
            why = f"In use by {names} on {node.name}. Stop {'it' if len(used) == 1 else 'them'} first."
        out.append({**item, "matches": matches, "used_by": used, "agent": agent, "remove_note": why})
    out.sort(key=lambda i: (-(i.get("size") or 0), i["id"]))
    return {"machine_id": node.id, "listed": True, "note": None, "images": out, "others": others,
            "free_bytes": answer.get("free_bytes"), "total_bytes": answer.get("total_bytes"),
            "can_remove": user.role == "admin", "remove_note": note}


def unlisted(node: Node, user: User, note: str) -> dict:
    return {"machine_id": node.id, "listed": False, "note": note, "images": [], "others": 0, "free_bytes": None,
            "total_bytes": None, "can_remove": user.role == "admin", "remove_note": remove_note(node, user)}


def precheck(db: DbSession, node: Node, item: Optional[dict], answer: dict) -> tuple[list[dict], list[dict]]:
    """Refuse before the agent is asked: an id the machine does not list, an image no recipe names, the agent's own
    image, an image a replica there uses. Returns (the recipes, the users: empty). Raises the console's error."""
    if item is None:
        raise fail(404, "image_not_found", f"{node.name} does not list that image (any more).")
    matches = upload.recipe_matches(db, item)
    if not matches:
        raise fail(409, "not_an_engine_image", f"No recipe names that image, so it is not Forgeyard's to remove from "
                                               f"{node.name}.")
    if images.normalize(answer.get("agent_image")) == item.get("id"):
        raise fail(409, "agent_image", f"{node.name}'s agent runs from that image: it is never removed here.")
    used = users_of(db, node.id, item)
    if used:
        names = ", ".join(u["name"] for u in used)
        raise fail(409, "image_in_use", f"That image is in use by {names} on {node.name}. Stop "
                                        f"{'it' if len(used) == 1 else 'them'} first.", in_use=used)
    return matches, used


async def ask_remove(node: Node, image_id: str) -> dict:
    """Ask the agent to remove the image; its answer (ok or not). Raises the console's error when it cannot be
    asked or does not answer."""
    conn = hub.get(node.id)
    if conn is None:
        raise fail(409, "machine_offline", f"{node.name}'s agent is not connected. Try again once it is online.")
    if FEATURE_IMAGE_REMOVE not in conn.features:
        raise fail(409, "agent_too_old", f"{node.name}'s agent is too old to remove engine images. Update the agent.")
    fut = conn.request({"v": PROTOCOL_VERSION, "type": "image_remove", "image": image_id})
    try:
        return await asyncio.wait_for(asyncio.wrap_future(fut), REMOVE_TIMEOUT)
    except asyncio.TimeoutError:
        raise fail(504, "agent_no_answer", f"{node.name}'s agent did not answer in time. Look at its engine images "
                                           "again in a moment: the removal may still have happened.") from None
    except Exception:
        raise fail(409, "agent_no_answer", f"{node.name}'s agent went away before it answered.") from None
    finally:
        conn.forget(fut)


def refused(node: Node, answer: dict):
    """The console's error for an agent that did not remove the image."""
    code = answer.get("code") or "failed"
    reason = answer.get("reason") or "The agent could not remove it."
    if code == "in_use":
        return fail(409, "image_in_use", f"{node.name}: {reason}", in_use=answer.get("in_use") or [])
    if code == "agent_image":
        return fail(409, "agent_image", f"{node.name}: {reason}")
    if code == "not_found":
        return fail(404, "image_not_found", f"{node.name}: {reason}")
    return fail(409, "image_remove_failed", f"{node.name}: {reason}")
