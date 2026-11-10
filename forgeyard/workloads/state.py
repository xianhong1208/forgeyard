"""The server's half of reconciliation: what each machine should run, and what its agent says runs.

The database is the desired state. For a machine it is every replica placed there whose
workload should be running and that has not finished (`final`). The agent gets that whole list
(spec + generation) on connect and whenever it changes, and answers with `status` messages; it
never receives commands like "start X", so a lost message or an agent restart cannot leave the
two sides out of step: the next `desired` converges again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import Deployment, Node, Replica, Workload, WorkloadEvent, utcnow
from forgeyard.machines.messages import StatusIn
from forgeyard.machines.protocol import FEATURE_MODELS, FEATURE_REPLICA_KEYS, PROTOCOL_VERSION

MAX_EVENTS = 300
# Agent states in which the replica is (or may be) running something.
LIVE_STATES = ("starting", "running", "healthy", "unhealthy")
ENDED_STATES = ("exited", "failed", "stopped")


def event(db: DbSession, workload: Workload, kind: str, message: str, replica: Optional[int] = None) -> None:
    db.add(WorkloadEvent(workload_id=workload.id, kind=kind, message=message[:500], replica=replica, at=utcnow()))
    db.flush()
    count = db.scalar(select(func.count()).select_from(WorkloadEvent).where(WorkloadEvent.workload_id == workload.id))
    if count and count > MAX_EVENTS:
        oldest = db.scalars(select(WorkloadEvent.id).where(WorkloadEvent.workload_id == workload.id)
                            .order_by(WorkloadEvent.id).limit(count - MAX_EVENTS)).all()
        db.execute(delete(WorkloadEvent).where(WorkloadEvent.id.in_(oldest)))


# ------------------------------------------------------------------ desired
def desired_replicas(db: DbSession, node_id: int) -> list[Replica]:
    return list(db.scalars(
        select(Replica).join(Workload)
        .where(Replica.node_id == node_id, Replica.final.is_(False), Workload.desired_state == "running")
        .order_by(Replica.id)).all())


def compat_spec(spec: dict) -> dict:
    """A spec an M2 agent accepts: without the M3 fields at their defaults (its spec model
    forbids unknown fields). Specs that use them are only placed on M3 agents anyway."""
    spec = dict(spec)
    res = dict(spec.get("resources") or {})
    gpus = dict(res.get("gpus") or {})
    if gpus.get("vram") is None and gpus.get("share", "exclusive") == "exclusive":
        gpus.pop("vram", None)
        gpus.pop("share", None)
    if res:
        res["gpus"] = gpus
        spec["resources"] = res
    if spec.get("health") and not spec["health"].get("start_period"):
        spec["health"] = {k: v for k, v in spec["health"].items() if k != "start_period"}
    return spec


def replica_desired(r: Replica, *, models: bool = True, token: Optional[str] = None,
                    key_env: Optional[str] = None) -> dict:
    w = r.workload
    spec = r.spec or w.spec
    out = {"id": r.uid, "workload_id": w.id, "workload": w.name, "ordinal": r.ordinal, "generation": r.generation,
           "kind": w.kind, "spec": spec if models else compat_spec(spec), "gpus": r.gpus or []}
    if r.model:
        out["model"] = dict(r.model, token=token) if token else dict(r.model)
    if r.engine:
        out["engine"] = dict(r.engine)
    if key_env and r.secret:
        # The engine's internal key (M4): the agent passes it in the environment variable the
        # recipe names, keeps it in memory only, and sends it with its health checks.
        out["replica_key"] = {"env": key_env, "value": r.secret}
    return out


def desired_message(db: DbSession, node_id: int, revision: int) -> dict:
    from forgeyard.models.catalog import get_recipe

    node = db.get(Node, node_id)
    features = (node.features or []) if node is not None else []
    models = FEATURE_MODELS in features
    keys = FEATURE_REPLICA_KEYS in features
    reps = desired_replicas(db, node_id)
    tokens: dict[int, Optional[str]] = {}
    key_envs: dict[int, Optional[str]] = {}
    ids = {r.workload_id for r in reps if r.model}
    if ids:
        for dep in db.scalars(select(Deployment).where(Deployment.workload_id.in_(ids))).all():
            tokens[dep.workload_id] = dep.hf_token
            if keys:
                recipe = get_recipe(db, dep.recipe)
                key_envs[dep.workload_id] = recipe.api_key_env if recipe is not None else None
    return {"v": PROTOCOL_VERSION, "type": "desired", "revision": revision,
            "replicas": [replica_desired(r, models=models, token=tokens.get(r.workload_id),
                                         key_env=key_envs.get(r.workload_id)) for r in reps]}


# ------------------------------------------------------------------ status from the agent
def _ts(value: Optional[float]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def describe(state: str, reason: Optional[str], exit_code: Optional[int], pid: Optional[int]) -> str:
    text = state
    if state == "running" and pid:
        text = f"running (pid {pid})"
    elif state == "exited" and exit_code is not None:
        text = f"exited with code {exit_code}"
    if reason:
        text += f" — {reason}"
    return text


def apply_status(db: DbSession, node: Node, msg: StatusIn) -> bool:
    """Record what the agent reports. Only replicas placed on *this* machine and of the current
    generation are touched: an agent can never change another machine's replicas. Returns True
    when the desired state of this machine changed (a replica finished)."""
    rows = {r.uid: r for r in db.scalars(select(Replica).where(Replica.node_id == node.id)).all()}
    desired_changed = False
    now = utcnow()
    if msg.model_cache is not None:
        node.model_cache = dict(msg.model_cache)
    for st in msg.replicas:
        r = rows.get(st.id)
        if r is None or st.generation != r.generation:
            continue  # deleted, moved, or an older generation being torn down
        stopping = r.workload.desired_state != "running"
        state = st.state
        if stopping and state not in ENDED_STATES:
            state = "stopping"  # still running until the agent confirms the stop
        before = (r.state, r.reason, r.exit_code)
        r.state = state
        r.reason = st.reason
        r.exit_code = st.exit_code
        r.attempt = st.attempt
        r.restarts = st.restarts
        r.pid = st.pid
        r.container = st.container
        r.ports = [p.model_dump() for p in st.ports]
        r.started_at = _ts(st.started_at) or r.started_at
        r.finished_at = _ts(st.finished_at) if state in ENDED_STATES or state == "backoff" else None
        if st.log_tail is not None:
            r.log_tail = st.log_tail
        r.download = st.download if state in ("starting", "pending") else None
        health = (r.spec or r.workload.spec or {}).get("health")
        ready = state == "healthy" or (state == "running" and not health)
        if ready and r.ready_at is None:
            r.ready_at = now
        elif state not in ("healthy", "running", "unhealthy"):
            r.ready_at = None  # its memory is not in use (any more): the reservation counts again
        r.updated_at = now
        if st.final and not r.final and not stopping:
            r.final = True
            desired_changed = True
        progress_only = before[0] == r.state and st.download is not None and (before[1] or "").startswith("downloading")
        if before != (r.state, r.reason, r.exit_code) and not progress_only:
            event(db, r.workload, "state", f"Replica {r.ordinal} on {node.name}: "
                  f"{describe(r.state, r.reason, r.exit_code, r.pid)}", r.ordinal)
    if msg.full:
        reported = {s.id for s in msg.replicas}
        for r in rows.values():
            if r.uid in reported or r.final:
                continue
            if r.state == "stopping" or (r.workload.desired_state != "running" and r.state != "stopped"):
                r.state, r.reason, r.updated_at, r.pid = "stopped", None, now, None
                r.finished_at = r.finished_at or now
                event(db, r.workload, "state", f"Replica {r.ordinal} on {node.name}: stopped", r.ordinal)
    db.flush()
    return desired_changed


# ------------------------------------------------------------------ changes from people
def reset_replica(r: Replica, generation: int) -> None:
    r.generation = generation
    r.node_id = None
    r.gpus = []
    r.state = "pending"
    r.reason = None
    r.exit_code = None
    r.attempt = 0
    r.restarts = 0
    r.final = False
    r.pid = None
    r.container = None
    r.ports = []
    r.started_at = None
    r.finished_at = None
    r.spec = None
    r.model = None
    r.engine = None
    r.vram = None
    r.download = None
    r.ready_at = None
    r.secret = None  # the next placement gets a new internal key
    r.updated_at = utcnow()


def stop(db: DbSession, w: Workload, connected) -> set[int]:
    """Ask every replica to stop. Returns the machines to poke."""
    w.desired_state = "stopped"
    w.updated_at = utcnow()
    nodes: set[int] = set()
    for r in w.replicas:
        if r.final or r.state == "stopped":
            continue
        if r.node_id is None:
            r.state, r.reason = "stopped", None
        else:
            r.state = "stopping"
            r.reason = None if connected(r.node_id) else "the machine is offline; it stops when the agent reconnects"
            nodes.add(r.node_id)
        r.updated_at = utcnow()
    return nodes


def unplace_node(db: DbSession, node_id: int, why: str) -> list[Workload]:
    """A machine was revoked or removed: its replicas go back to pending (they are placed again
    elsewhere, or wait with a reason)."""
    touched = []
    for r in db.scalars(select(Replica).where(Replica.node_id == node_id)).all():
        if r.final:
            continue
        w = r.workload
        reset_replica(r, r.generation)
        if w.desired_state != "running":
            r.state = "stopped"
        r.reason = why
        event(db, w, "unplaced", f"Replica {r.ordinal}: {why}", r.ordinal)
        touched.append(w)
    db.flush()
    return touched


def summary(w: Workload) -> dict:
    """One status for the list: running / starting / degraded / succeeded / failed / stopped…"""
    reps = w.replicas
    states = [r.state for r in reps]
    health = (w.spec or {}).get("health")
    ready = sum(1 for r in reps if r.state == "healthy" or (r.state == "running" and not health))
    if w.desired_state != "running":
        status = "stopping" if "stopping" in states else "stopped"
    elif w.kind == "task":
        if reps and all(r.final for r in reps):
            status = "succeeded" if all(r.state == "exited" and r.exit_code == 0 for r in reps) else "failed"
        elif any(s in LIVE_STATES or s == "backoff" for s in states):
            status = "running"
        else:
            status = "pending"
    else:
        if reps and ready == len(reps):
            status = "running"
        elif any(r.final for r in reps):
            status = "failed"
        elif any(s in ("unhealthy", "backoff", "failed") for s in states):
            status = "degraded"
        elif any(s in LIVE_STATES for s in states):
            status = "starting"
        else:
            status = "pending"
    return {"status": status, "ready": ready, "replicas": len(reps)}
