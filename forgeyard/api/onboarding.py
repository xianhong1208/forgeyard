"""The "Get started" checklist: what to do first, in order, and how far this server (and this person)
has come, from real state. Nothing here is stored except whether the person hid the checklist and
whether they have looked at their usage.

Admins and operators get the six steps that take a fresh install to a first call through the
gateway: add a machine, choose its GPUs, add a model, deploy it, create an API key, call /v1.
Operators see the steps only admins can do (machines, GPU access, the catalog) with who does them.
Users get three: create a key, call /v1, see their usage.

The machine, GPU, model and deployment steps describe the server; the key, call and usage steps
describe the person asking (their own keys, their own requests).
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard.api.deps import Caller, can_run_workloads, current
from forgeyard.api.keys import gateway_url, key_status
from forgeyard.api.tls import trust_page
from forgeyard.db import ApiKey, AuditEvent, CatalogModel, Deployment, Node, UsageDaily, User, get_db
from forgeyard.models import routes
from forgeyard.workloads import placement, state as wstate

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])

HIDDEN = "onboarding_hidden"
USAGE_SEEN = "onboarding_usage_seen"
# The API answers for these; anything else in preferences is left alone.
PREFERENCE_KEYS = (HIDDEN, USAGE_SEEN)

# Which kind of request each protocol takes, for the example call.
EXAMPLE_ORDER = ("openai-chat", "openai-embeddings", "rerank", "openai-audio", "openai-speech", "openai-images")


def _step(sid: str, title: str, why: str, done: bool, detail: str, *, to: Optional[str], label: Optional[str],
          blocked_by: Optional[str] = None, who: Optional[str] = None) -> dict:
    return {"id": sid, "title": title, "why": why, "done": done, "detail": detail,
            "action": {"to": to, "label": label} if to and label else None, "blocked_by": blocked_by, "who": who}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ------------------------------------------------------------------ the server's side
def machines_step(db: DbSession, admin: bool) -> tuple[dict, list[Node]]:
    nodes = db.scalars(select(Node).where(Node.status != "revoked").options(selectinload(Node.accelerators))
                       .order_by(Node.name)).all()
    online = [n for n in nodes if n.status == "online"]
    if online:
        detail = f"{_plural(len(online), 'machine')} online" + (f", {len(nodes) - len(online)} offline"
                                                                 if len(nodes) > len(online) else "") + "."
    elif nodes:
        detail = (f"{', '.join(n.name for n in nodes)} joined but {'is' if len(nodes) == 1 else 'are'} offline: "
                  "start the agent there again.")
    else:
        detail = "No machine has joined yet."
    step = _step("machine", "Add a machine",
                 "Forgeyard runs everything on machines you add: each one runs the Forgeyard agent.",
                 bool(online), detail, to="/machines", label="Add a machine" if admin else "Open Machines",
                 who=None if admin else "An admin adds machines.")
    return step, list(nodes)


def gpu_choice_made(db: DbSession) -> set[int]:
    """The machines whose GPU access an admin saved. Deciding "all of them" leaves the allowlist
    empty, so a saved choice is read from the audit log."""
    decided_ids = set()
    for detail in db.scalars(select(AuditEvent.detail).where(AuditEvent.action == "machine.gpu_allowlist_changed")):
        if isinstance(detail, dict) and isinstance(detail.get("machine_id"), int):
            decided_ids.add(detail["machine_id"])
    return decided_ids


def gpus_step(db: DbSession, nodes: list[Node], admin: bool) -> dict:
    why = "Shared machines often have cards other people use: say which ones Forgeyard may take."
    who = None if admin else "An admin chooses the GPUs."
    if not nodes:
        return _step("gpus", "Choose which GPUs Forgeyard may use", why, False, "Add a machine first.",
                     to="/machines", label="Open Machines", blocked_by="machine", who=who)
    with_gpus = [n for n in nodes if any(placement.is_gpu(a) for a in n.accelerators)]
    if not with_gpus:
        return _step("gpus", "Choose which GPUs Forgeyard may use", why, True,
                     "Your machines have no GPU: models run on the CPU, nothing to choose.",
                     to="/machines", label="Open Machines", who=who)
    decided_ids = gpu_choice_made(db)
    open_ = [n for n in with_gpus if n.gpu_allowlist is None and n.id not in decided_ids]
    if open_:
        names = ", ".join(n.name for n in open_)
        detail = (f"Not chosen yet on {names}: until you choose, Forgeyard may use every GPU there. "
                  "Open the machine and use Change GPU access.")
    else:
        detail = f"Chosen on {_plural(len(with_gpus), 'machine')} with GPUs."
    return _step("gpus", "Choose which GPUs Forgeyard may use", why, not open_, detail,
                 to="/machines", label="Choose GPUs" if admin else "Open Machines", who=who)


def model_step(db: DbSession, admin: bool) -> dict:
    count = db.scalar(select(func.count()).select_from(CatalogModel)) or 0
    detail = f"{_plural(count, 'model')} in the catalog." if count else "The catalog is empty."
    return _step("model", "Add a model",
                 "The catalog lists the models you may deploy, each pinned to an exact version on Hugging Face.",
                 count > 0, detail, to="/models?tab=catalog", label="Add a model" if admin else "Open the catalog",
                 who=None if admin else "An admin adds models.")


def deploy_step(db: DbSession, can_deploy: bool, has_model: bool) -> dict:
    deps = db.scalars(select(Deployment).options(selectinload(Deployment.workload))).all()
    ready, waiting = [], []
    for d in deps:
        s = wstate.summary(d.workload)
        (ready if s["ready"] > 0 and d.workload.desired_state == "running" else waiting).append((d, s))
    if ready:
        detail = f"Ready: {', '.join(d.name for d, _ in ready)}."
    elif waiting:
        detail = "Not ready yet: " + ", ".join(f"{d.name} ({s['status']})" for d, s in waiting) + \
                 ". The first start downloads the model and the engine; that can take a while."
    else:
        detail = "Nothing is deployed yet." if has_model else "Add a model first."
    return _step("deploy", "Deploy it",
                 "A deployment runs the model with an engine on a machine where it fits, and gives it a name for the API.",
                 bool(ready), detail, to="/models/deploy" if can_deploy else "/models",
                 label="Deploy a model" if can_deploy else "Open Models", blocked_by=None if has_model else "model",
                 who=None if can_deploy else "Admins and operators deploy models.")


# ------------------------------------------------------------------ the person's side
def key_step(db: DbSession, user: User) -> dict:
    keys = db.scalars(select(ApiKey).where(ApiKey.user_id == user.id)).all()
    active = [k for k in keys if key_status(k) == "active"]
    detail = f"You have {_plural(len(active), 'active key')}." if active else "You have no active key."
    return _step("key", "Create an API key",
                 "Programs call the models with a key. Make one per program or computer, so you can revoke one alone.",
                 bool(active), detail, to="/keys", label="Create a key")


def call_step(db: DbSession, user: User, any_ready: bool) -> dict:
    calls = db.scalar(select(func.coalesce(func.sum(UsageDaily.requests), 0))
                      .where(UsageDaily.user_id == user.id, UsageDaily.source == "api")) or 0
    if calls:
        detail = f"You have sent {_plural(int(calls), 'request')} with your keys."
    elif any_ready:
        detail = "Run the example below with your key."
    else:
        detail = "Waiting for a deployment that is ready."
    return _step("call", "Call /v1",
                 "Any OpenAI client works: point its base URL at Forgeyard and use your key.",
                 calls > 0, detail, to="/playground", label="Try it in the playground" if any_ready else None,
                 blocked_by=None if any_ready else "deploy")


def usage_step(user: User, called: bool) -> dict:
    seen = bool((user.preferences or {}).get(USAGE_SEEN))
    return _step("usage", "See your usage",
                 "Requests, errors, tokens and latency per model and per key, counted by the gateway.",
                 seen and called, "Opened." if seen and called else "Open Usage after your first call.",
                 to="/usage", label="Open Usage", blocked_by=None if called else "call")


def example(db: DbSession) -> Optional[dict]:
    """A deployment to show in the example call: a ready chat model if there is one."""
    rows = routes.all_routes(db)
    if not rows:
        return None
    rank = {api: i for i, api in enumerate(EXAMPLE_ORDER)}
    rows.sort(key=lambda r: (not r["endpoints"], rank.get(r["api"], 99), r["served_name"]))
    r = rows[0]
    return {"model": r["served_name"], "api": r["api"], "ready": bool(r["endpoints"])}


def checklist(db: DbSession, user: User, request: Request) -> dict:
    ex = example(db)
    any_ready = bool(ex and ex["ready"])
    if can_run_workloads(user):
        admin = user.role == "admin"
        m, nodes = machines_step(db, admin)
        g = gpus_step(db, nodes, admin)
        mod = model_step(db, admin)
        dep = deploy_step(db, True, mod["done"])
        if not m["done"]:
            dep["blocked_by"] = dep["blocked_by"] or "machine"
        steps = [m, g, mod, dep, key_step(db, user), call_step(db, user, any_ready)]
        kind = "setup"
    else:
        call = call_step(db, user, any_ready)
        steps = [key_step(db, user), call, usage_step(user, call["done"])]
        kind = "use"
    done = sum(1 for s in steps if s["done"])
    prefs = user.preferences or {}
    return {"kind": kind, "steps": steps, "done": done, "total": len(steps), "complete": done == len(steps),
            "hidden": bool(prefs.get(HIDDEN)), "gateway_url": gateway_url(request),
            "trust_url": trust_page(request), "example": ex}


@router.get("")
def get_checklist(request: Request, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    return checklist(db, caller.user, request)


class ChecklistIn(BaseModel):
    hidden: bool


def _set(db: DbSession, user: User, key: str, value) -> None:
    # A new dict, so the JSON column is seen as changed.
    prefs = dict(user.preferences or {})
    prefs[key] = value
    user.preferences = prefs


@router.put("")
def set_checklist(body: ChecklistIn, request: Request, caller: Caller = Depends(current),
                  db: DbSession = Depends(get_db)):
    """Hide the checklist from the home page, or show it there again (it stays under Get started)."""
    _set(db, caller.user, HIDDEN, body.hidden)
    db.commit()
    return checklist(db, caller.user, request)


class SeenIn(BaseModel):
    step: Literal["usage"]


@router.post("/seen")
def mark_seen(body: SeenIn, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    """The Usage page says it was opened (the users' last step). Written once."""
    if not (caller.user.preferences or {}).get(USAGE_SEEN):
        _set(db, caller.user, USAGE_SEEN, True)
        db.commit()
    return {"ok": True}
