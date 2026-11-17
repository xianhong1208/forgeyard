""""Download to…" (1.4, issue #35): a catalog model, and by default the engine of a recipe, downloaded into
chosen machines' model caches before anything is deployed there, with progress.

- **A row per (model, machine)** in `model_downloads`. While it is queued or downloading, the machine's
  agent gets it in `desired.downloads` (protocol feature "prefetch"), beside the replicas, and fetches it
  with the code a replica's download uses: the same locks, resume, checksums, progress and cancel. Nothing
  is started. A row that leaves the list (cancelled here) is cancelled on the agent at once.
- **The engine (D7)**: for the chosen recipe, the variant that machine would run (a GPU variant when it has
  a card the variant supports, else a CPU one; one its runtime can run): a process variant's engine
  archive, or a container variant's image, pulled as a container replica's is (1.3.1).
- **Whether a model is on a machine** is never read from this table: it is what the agent reports
  (`nodes.model_cache`), so a model seeded or deleted by hand shows the truth, and placement prefers a
  machine where a download finished (#71) because the agent then lists the model as cached.
- **URL models (D8)** have no published checksum: the agent computes the SHA-256 of the file after its
  first complete download and reports it with its cache listing; the first one reported is recorded in the
  catalog entry, `models.meta.files[0].sha256` (with `sha256_from`, the machine, and `sha256_at`), which is
  where Hugging Face models keep theirs. From then on it is sent with every download of that model, direct
  or relayed, and a file that does not match is refused.
- **Through the server** (1.4, "relay", forgeyard/machines/relay.py): on a machine whose `fetch_via` allows it,
  the model and engine blocks carry `relay`; the row's `via` says how the bytes came last ("server" once the
  agent reports progress through the server).
- Finished rows are history: kept KEEP_DAYS days, at most KEEP_PER_MACHINE per machine.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from forgeyard import audit
from forgeyard.db import CatalogModel, ModelDownload, Node, utcnow
from forgeyard.machines import peers
from forgeyard.machines.protocol import FEATURE_IMAGE_IDS, FEATURE_MODELS, FEATURE_PREFETCH
from forgeyard.models import reach, render, sources
from forgeyard.models.recipes import Recipe, Variant, variant_matches

log = logging.getLogger("forgeyard.models")

ACTIVE = ("queued", "downloading")
FINAL = ("done", "failed", "cancelled")
KEEP_DAYS = 30
KEEP_PER_MACHINE = 20
DISK_MARGIN = 1024**3  # what the agent keeps free (agent/models.py)
GIB = 1024**3


# ------------------------------------------------------------------ what one machine would fetch
def _hw(a) -> dict:
    return {"vendor": a.vendor, "arch": a.arch, "driver": a.driver}


def variant_for(node: Node, recipe: Recipe) -> tuple[Optional[Variant], str]:
    """The variant of the recipe this machine would run: a GPU variant when one of its allowed cards
    supports it, else a CPU variant, and one whose runtime works there. (None, why) when none."""
    from forgeyard.models.scheduler import _runtime_problem
    from forgeyard.workloads import placement

    problems: list[str] = []
    ordered = [v for v in recipe.variants if v.gpu] + [v for v in recipe.variants if not v.gpu]
    for v in ordered:
        if v.gpu:
            fits = any(placement.is_gpu(a) and placement.is_allowed(node, a) and variant_matches(v, _hw(a)) is None
                       for a in node.accelerators)
        else:
            cpu = next((a for a in node.accelerators if a.vendor == "cpu"), None)
            fits = cpu is not None and variant_matches(v, _hw(cpu)) is None
        if not fits:
            continue
        problem = _runtime_problem(node, v)
        if problem:
            problems.append(f"{v.name}: {problem}")
            continue
        return v, ""
    return None, ("; ".join(dict.fromkeys(problems)) or f"no variant of {recipe.name} for this machine's hardware")[:300]


def engine_of(node: Node, recipe: Optional[Recipe]) -> dict:
    """{variant, engine (archive block) | None, image | None, cached, note}: the engine this machine would
    fetch for the recipe."""
    out: dict = {"variant": None, "engine": None, "image": None, "cached": None, "note": None}
    if recipe is None:
        return out
    variant, why = variant_for(node, recipe)
    if variant is None:
        out["note"] = why
        return out
    out["variant"] = variant.name
    if variant.runtime == "process":
        block = render.engine_of(recipe, variant)
        out["engine"] = block
        out["cached"] = True if block is None else reach.has_engine(node, block)  # an installed command
    else:
        out["image"] = variant.image
        out["cached"] = reach.has_image(node, variant.image)
    return out


def _block(model: CatalogModel) -> dict:
    from forgeyard.models.catalog import model_block

    return model_block(model)


def model_cached(node: Node, model: CatalogModel) -> bool:
    try:
        key = sources.cache_key(_block(model))
    except (sources.SourceError, KeyError, TypeError):
        return False
    return key in ((node.model_cache or {}).get("cached") or [])


def active_rows(db: DbSession, *, node_id: Optional[int] = None, model_id: Optional[int] = None) -> list[ModelDownload]:
    q = select(ModelDownload).where(ModelDownload.state.in_(ACTIVE)).order_by(ModelDownload.id)
    if node_id is not None:
        q = q.where(ModelDownload.node_id == node_id)
    if model_id is not None:
        q = q.where(ModelDownload.model_id == model_id)
    return list(db.scalars(q).all())


def target(db: DbSession, node: Node, model: CatalogModel, recipe: Optional[Recipe],
           active: Optional[ModelDownload] = None) -> dict:
    """What the dialog shows for one machine: whether it can take the download, and why not."""
    from forgeyard.machines.hub import hub

    feats = node.features or []
    connected = hub.connected(node.id)
    mc = node.model_cache or {}
    cached = model_cached(node, model)
    eng = engine_of(node, recipe)
    need = 0 if cached else int(model.size_bytes or 0)
    free = mc.get("free_bytes")
    out = {"machine_id": node.id, "name": node.name, "status": node.status, "connected": connected,
           "model_cached": cached, "need_bytes": need, "free_bytes": free, "variant": eng["variant"],
           "engine": ({"kind": "archive", "name": eng["engine"]["name"], "cached": eng["cached"]} if eng["engine"]
                      else {"kind": "image", "name": eng["image"], "cached": eng["cached"]} if eng["image"] else None),
           "engine_note": eng["note"], "download": None, "via": _via(node, model), "from_machine": None}
    if node.revoked_at is not None or node.status == "revoked":
        state, note = "revoked", f"{node.name} was revoked."
    elif FEATURE_MODELS not in feats:
        state, note = "no_cache", "Its agent serves no models (start it with --model-cache)."
    elif FEATURE_PREFETCH not in feats:
        state, note = "too_old", f"{node.name}'s agent cannot download ahead: update the agent."
    elif active is not None:
        state, note = "downloading", "A download of this model is already under way here."
        out["download"] = row_out(active, connected)
    elif not connected:
        state, note = "offline", f"{node.name} is offline."
    elif mc and mc.get("ok") is False:
        state, note = "no_cache", f"Its model cache cannot be used: {mc.get('reason') or 'unknown'}"
    elif cached and (eng["cached"] is not False):
        state, note = "cached", "Already here" + (" with the engine." if eng["cached"] else ".")
    elif free is not None and need and need + DISK_MARGIN > free:
        state, note = "no_space", (f"Not enough disk space in the model cache: needs {need / GIB:.1f} GiB (+1 GiB "
                                   f"margin), {free / GIB:.1f} GiB free.")
    else:
        state, note = "ok", ("The model is here; downloads the engine." if cached else "")
    out["state"], out["note"] = state, note
    if out["via"] == "server" and not cached and state == "ok":
        out["from_machine"] = _from_machine(db, node, model)
    return out


def _from_machine(db: DbSession, node: Node, model: CatalogModel) -> Optional[str]:
    """1.4.1: a machine whose cache has the model and would send it through the server (the dialog says so), if
    any: the first file's first candidate, as the relay would pick it."""
    try:
        block = _block(model)
    except (KeyError, TypeError):
        return None
    first = (block.get("files") or [{}])[0] if isinstance(block.get("files"), list) else {}
    copy = peers.file_copy(db, node, block, str(first.get("path")) if block.get("kind") == "hf" else None)
    order = peers.ranked(copy.candidates) if copy is not None else []
    return order[0].name if order else None


def _via(node: Node, model: CatalogModel) -> str:
    """How this machine would fetch the model (1.4, "relay"): "server", "auto" (directly, through the server
    after a network failure) or "direct"."""
    from forgeyard.machines import relay

    try:
        host = reach.model_host(_block(model))
    except (KeyError, TypeError):
        host = None
    return relay.mode(node, host) or "direct"


def targets(db: DbSession, model: CatalogModel, recipe: Optional[Recipe]) -> list[dict]:
    from forgeyard.models.scheduler import candidates

    active = {r.node_id: r for r in active_rows(db, model_id=model.id)}
    return [target(db, n, model, recipe, active.get(n.id)) for n in candidates(db, None)
            if n.revoked_at is None and n.status != "revoked"]


# ------------------------------------------------------------------ asking, cancelling, history
def start(db: DbSession, model: CatalogModel, nodes: list[Node], recipe: Optional[Recipe], by: str,
          *, actor=None, ip: Optional[str] = None) -> tuple[list[ModelDownload], list[dict]]:
    """A download per machine that can take it; the others with why not. Does not commit."""
    active = {r.node_id: r for r in active_rows(db, model_id=model.id)}
    created, skipped = [], []
    for node in nodes:
        t = target(db, node, model, recipe, active.get(node.id))
        if t["state"] != "ok":
            skipped.append({"machine_id": node.id, "name": node.name, "state": t["state"], "reason": t["note"]})
            continue
        row = ModelDownload(model_id=model.id, node_id=node.id, file=model.file,
                            recipe=recipe.name if recipe is not None and t["engine"] is not None else None,
                            state="queued", via="direct", requested_by=by[:64], created_at=utcnow())
        db.add(row)
        db.flush()
        audit.record(db, "model.download_requested", actor=actor, actor_name=None if actor else by, target=node.name,
                     ip=ip, machine_id=node.id, model=model.name, download_id=row.id,
                     engine=(t["engine"] or {}).get("name") if row.recipe else None, recipe=row.recipe)
        created.append(row)
    prune(db)
    return created, skipped


def cancel(db: DbSession, row: ModelDownload, by: str, *, actor=None, ip: Optional[str] = None) -> None:
    row.state, row.download, row.finished_at = "cancelled", None, utcnow()
    row.reason = f"cancelled by {by}"[:400]
    audit.record(db, "model.download_cancelled", actor=actor, actor_name=None if actor else by,
                 target=row.node.name if row.node else None, ip=ip, machine_id=row.node_id,
                 model=row.model.name if row.model else None, download_id=row.id)


def fail_all(db: DbSession, node_id: int, why: str) -> int:
    """The machine was revoked, or its agent no longer downloads ahead: its downloads end, saying why."""
    n = 0
    for row in active_rows(db, node_id=node_id):
        row.state, row.download, row.reason, row.finished_at = "failed", None, why[:400], utcnow()
        n += 1
    return n


def on_hello(db: DbSession, node: Node) -> None:
    """An agent connected without "prefetch" (an older one replaced a newer): what it was asked never comes."""
    if FEATURE_PREFETCH not in (node.features or []):
        fail_all(db, node.id, f"{node.name}'s agent ({node.agent_version or 'unknown version'}) cannot download "
                              "ahead: update the agent")


def prune(db: DbSession) -> None:
    """Finished downloads older than KEEP_DAYS go, and all but the last KEEP_PER_MACHINE of each machine."""
    db.execute(delete(ModelDownload).where(ModelDownload.state.in_(FINAL),
                                           ModelDownload.finished_at < utcnow() - timedelta(days=KEEP_DAYS)))
    rows = db.execute(select(ModelDownload.id, ModelDownload.node_id).where(ModelDownload.state.in_(FINAL))
                      .order_by(ModelDownload.node_id, ModelDownload.id.desc())).all()
    seen: dict[int, int] = {}
    old = []
    for rid, node_id in rows:
        seen[node_id] = seen.get(node_id, 0) + 1
        if seen[node_id] > KEEP_PER_MACHINE:
            old.append(rid)
    if old:
        db.execute(delete(ModelDownload).where(ModelDownload.id.in_(old)))


# ------------------------------------------------------------------ the protocol
def desired(db: DbSession, node: Node) -> list[dict]:
    """`desired.downloads` for a machine whose agent lists "prefetch": each queued or downloading row."""
    from forgeyard.machines import relay
    from forgeyard.models import hf_token
    from forgeyard.models.catalog import get_recipe
    from forgeyard.workloads import images

    rows = active_rows(db, node_id=node.id)
    if not rows:
        return []
    server_token: Optional[str] = None
    out = []
    for row in rows:
        model = row.model
        try:
            block = _block(model)
        except (KeyError, TypeError):
            continue
        if model.kind == "hf":
            # The server's token for gated repositories (there is no deployment with its own): memory only on
            # the agent, as for replicas. An https:// GGUF host never gets one.
            if server_token is None:
                server_token = hf_token.get(db) or ""
            if server_token:
                block = dict(block, token=server_token)
        item: dict = {"id": row.uid, "model": block}
        if row.recipe:
            eng = engine_of(node, get_recipe(db, row.recipe))
            if eng["engine"] is not None:
                item["engine"] = eng["engine"]
            if eng["image"]:
                item["image"] = eng["image"]
                check = images.check_for(eng["image"]) if FEATURE_IMAGE_IDS in (node.features or []) else None
                if check is not None:
                    item["image_ids"] = check
        # 1.4 ("relay"): through the server when this machine may; only through it, without the token.
        out.append(relay.attach(item, node, "download", row.uid))
    return out


def apply_status(db: DbSession, node: Node, reports: list) -> bool:
    """What the agent says of its downloads. Only this machine's rows that are still queued or downloading
    change; True when one finished (it leaves the desired list, which goes out again)."""
    if not reports:
        return False
    rows = {r.uid: r for r in active_rows(db, node_id=node.id)}
    changed = False
    now = utcnow()
    for st in reports:
        row = rows.get(st.id)
        if row is None:
            continue  # cancelled here already, or another machine's
        if st.state in ACTIVE:
            row.state, row.reason, row.download = st.state, st.reason, peers.label(st.download, "download", row.uid)
            if st.download and st.download.get("via") in ("direct", "server"):
                via, phase = st.download["via"], st.download.get("phase", "model")
                # 1.4.1: the model from another machine's cache, through the server ("machine"); the engine
                # after it does not change how the model came.
                src = peers.source_of("download", row.uid) if via == "server" and phase == "model" else None
                if src is not None:
                    row.via, row.source_node_id = "machine", src[0]
                elif phase == "model" or row.via != "machine":
                    row.via, row.source_node_id = via, None  # how it came, last (1.4, "relay")
            continue
        row.state, row.download, row.finished_at = st.state, None, now
        row.reason = None if st.state == "done" else (st.reason or st.state)
        changed = True
        if st.state == "done" and row.recipe:
            from forgeyard.models.catalog import get_recipe

            eng = engine_of(node, get_recipe(db, row.recipe))
            if eng["image"]:
                reach.seen_image(node.id, eng["image"])  # placement counts the image as on this machine
    return changed


def store_cache(db: DbSession, node: Node, model_cache: Optional[dict]) -> None:
    """Keep what the agent reports of its cache, and record the SHA-256 of URL models it computed (D8)."""
    node.model_cache = dict(model_cache or {})
    if model_cache:
        record_url_hashes(db, node, model_cache)


def record_url_hashes(db: DbSession, node: Node, model_cache: dict) -> list[str]:
    """D8: the first SHA-256 an agent reports for a URL model is kept in the catalog entry; every later
    download must match it. A different one reported later is logged and never replaces it. Returns the
    names of the models recorded."""
    found = {e["key"]: e["sha256"] for e in model_cache.get("models") or []
             if isinstance(e, dict) and e.get("kind") == "url" and e.get("sha256")}
    if not found:
        return []
    recorded = []
    for m in db.scalars(select(CatalogModel).where(CatalogModel.kind == "url")).all():
        meta = dict(m.meta or {})
        files = list(meta.get("files") or [])
        if not files:
            continue
        try:
            key = sources.cache_key(_block(m))
        except (sources.SourceError, KeyError, TypeError):
            continue
        got = found.get(key)
        if not got:
            continue
        have = files[0].get("sha256")
        if have == got:
            continue
        if have:
            log.warning("%s reports SHA-256 %s… for %s, which was recorded as %s… (from %s): kept the recorded one.",
                        node.name, got[:12], m.name, have[:12], files[0].get("sha256_from") or "?")
            continue
        files[0] = {**files[0], "sha256": got, "sha256_from": node.name, "sha256_at": utcnow().isoformat()}
        meta["files"] = files
        m.meta = meta
        audit.record(db, "model.checksum_recorded", actor_name=node.name, target=m.name, machine_id=node.id,
                     sha256=got)
        log.info("Recorded the SHA-256 of %s (%s…), computed by %s after its first download.", m.name, got[:12],
                 node.name)
        recorded.append(m.name)
    return recorded


# ------------------------------------------------------------------ the API's view
def row_out(row: ModelDownload, connected: Optional[bool] = None) -> dict:
    from forgeyard.machines.hub import hub

    node, model = row.node, row.model
    online = hub.connected(row.node_id) if connected is None else connected
    out = {"id": row.id, "model": {"id": model.id, "name": model.name, "title": model.title} if model else None,
           "machine": {"id": node.id, "name": node.name, "connected": online} if node else None,
           "file": row.file, "recipe": row.recipe, "state": row.state, "via": row.via, "download": row.download,
           "reason": row.reason, "requested_by": row.requested_by, "created_at": row.created_at,
           "finished_at": row.finished_at, "waiting": None, "source": None}
    # 1.4.1: the machine the model came from, through the server ("machine").
    src = row.source_node if row.via == "machine" and row.source_node_id is not None else None
    if src is not None:
        out["source"] = {"id": src.id, "name": src.name}
    if row.state in ACTIVE and node is not None and not online:
        out["waiting"] = f"waiting for {node.name} to come online; the download goes on from where it was"
    return out
