"""Trying an engine before switching to it (1.5.2, issue #95, PR 4): a basic trial run (community edition).

From an engine's page (usually a copy "Try this version" made), an operator or an admin picks a catalog model the
engine serves and a machine. The server then:

1. creates a **temporary, isolated deployment** of that model with that engine, pinned to that machine, placed like
   any deployment (the estimate, the usual refusal when it does not fit, sharing a card when the parameters say so).
   It has no gateway route, so it is never in /v1/models, no API key and no Playground can call it, the gateway never
   counts usage or cost for it; it is left out of the deployment lists (the Serving page names it as a trial in
   progress), of sleep when idle, of automatic replicas, of the route targets an admin may pick, and of /metrics;
   the deployment API answers 404 for it (it is managed here only);
2. follows it while it starts (`starting`): how long it takes and its phases (the agent's measurement, the same as a
   deployment's last start); it fails at once when the replica cannot be placed or its start fails (backoff, failed,
   exited), and when it does not become ready within the trial's time limit;
3. measures its memory (`measuring`): the machine's readings of its GPU(s) (or RAM) before the engine started and
   after it became ready, as the agent reports them, a few readings each; the difference against the estimate and
   what the replica reserved. On a card other work shares this is approximate, and the check says so;
4. asks it a few short requests of its model's kind (`asking`), straight to the replica with its internal key, as
   the console's Try it does: one to warm it up, then REQUESTS more; their latency (p50, p95), whether each answer is
   one of that kind, and what the first answer says (chat: a fixed prompt at temperature 0, short; embeddings,
   rerank, text classification, speech to text, text to speech, images, vision as their Try it requests; video is not
   asked: a video takes minutes). When a deployment of the same model on the built-in engine is ready, the same
   request goes to it once, and the two answers are compared (information, not pass or fail: a new version may
   answer differently);
5. takes one more memory reading after the answers (`settling`), then removes the temporary deployment and records
   the result: passed when no check failed.

The deployment is also removed when a check fails, when the trial is cancelled, when it runs out of time, and, for a
trial that was running when the server stopped, when the server starts again (`sweep_at_start`).

Every trial is a row of `engine_trials` (forgeyard/db.py: EngineTrial), kept after the copy, the model or the machine
is gone: the engine page's history. The enterprise edition's exportable upgrade validation reports and "only tried
versions" policies build on this table later; `switch_policy` is the seam such a policy plugs into, and it allows
every switch in the community edition.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import statistics
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard.db import (Accelerator, CatalogModel, Deployment, EngineTrial, Node, Replica, Workload, WorkloadEvent,
                          as_utc, new_session, utcnow)
from forgeyard.models.recipes import Recipe

log = logging.getLogger("forgeyard.trials")

RUNNING, PASSED, FAILED, CANCELLED = "running", "passed", "failed", "cancelled"
STATES = (RUNNING, PASSED, FAILED, CANCELLED)
PHASES = ("starting", "measuring", "asking", "settling")

TICK_SECONDS = 3.0
MAX_RUNNING = 4                       # trials at once on the server (each holds a card or RAM while it runs)
START_MINUTES_DEFAULT, START_MINUTES_MIN, START_MINUTES_MAX = 30, 5, 180   # its image may have to come first
AFTER_READY_SECONDS = 900             # measuring, asking and settling, after it became ready
REQUESTS = 5                          # measured requests, after one to warm it up
MEMORY_SAMPLES = 3                    # readings before the engine starts and after it is ready
MEMORY_TOLERANCE = 0.05               # measured may exceed the reservation by this much on a shared card's noise
SHARED_NOTE_BYTES = 512 * 1024**2     # other use of the card above this makes the measurement approximate
GIB = 1024**3
NAME_PREFIX = "trial-"

# Requests per kind: (how many measured after the warm-up; None: not asked, and why).
NOT_ASKED = {"openai-videos": "A video takes minutes to make: the trial checks that it starts and its memory only."}
FEWER = {"openai-images": 1, "openai-audio": 2, "openai-speech": 2}


class TrialError(Exception):
    def __init__(self, status: int, code: str, text: str, **extra) -> None:
        super().__init__(text)
        self.status, self.code, self.text, self.extra = status, code, text, extra


# ------------------------------------------------------------------ isolation
def deployment_ids(db: DbSession) -> set[int]:
    """The deployments that are trials' temporary ones (while they exist)."""
    return set(db.scalars(select(EngineTrial.deployment_id).where(EngineTrial.deployment_id.is_not(None))).all())


def not_trial(column=Deployment.id):
    """A filter that leaves trials' temporary deployments out of a query on deployments."""
    return column.not_in(select(EngineTrial.deployment_id).where(EngineTrial.deployment_id.is_not(None)))


def is_trial(db: DbSession, dep_id: Optional[int]) -> bool:
    if dep_id is None:
        return False
    return db.scalar(select(EngineTrial.id).where(EngineTrial.deployment_id == dep_id).limit(1)) is not None


def trial_of_deployment(db: DbSession, dep_id: int) -> Optional[EngineTrial]:
    return db.scalar(select(EngineTrial).where(EngineTrial.deployment_id == dep_id).limit(1))


# ------------------------------------------------------------------ the policy seam (enterprise later)
def switch_policy(db: DbSession, d: Deployment, recipe: Recipe) -> Optional[tuple[str, str]]:
    """Whether a deployment may move to `recipe` (another version of its engine), as far as trials go: None to allow,
    or (code, text) to refuse. The community edition lets people choose freely, tried or not (Tony, 2026-11-23); an
    enterprise policy such as "only versions that passed a trial on this kind of machine" replaces this function."""
    return None


# ------------------------------------------------------------------ what a trial starts from
def builtin_of(recipe: Recipe) -> Optional[str]:
    """The built-in engine a copy is based on; None for a built-in engine itself (or a custom one made by hand)."""
    return recipe.based_on or None


def default_params(db: DbSession, recipe: Recipe, m: CatalogModel) -> tuple[dict, Optional[str]]:
    """The parameters a trial starts from when none are given: those of a deployment of the same model on the built-in
    engine (or on this engine), the newest first, keeping only what this engine knows; else the engine's defaults.
    Returns (params, the deployment they came from or None). Sleep, automatic replicas and the gateway's own settings
    never come along: a trial uses none of them."""
    from forgeyard.api.deployments import COMMON_PARAMS

    family = {recipe.name, *([recipe.based_on] if recipe.based_on else [])}
    rows = db.scalars(select(Deployment).where(Deployment.model_id == m.id, Deployment.recipe.in_(family), not_trial())
                      .order_by(Deployment.created_at.desc(), Deployment.id.desc())).all()
    rows = sorted(rows, key=lambda d: d.recipe != (recipe.based_on or recipe.name))   # the built-in's first
    known = set(recipe.params) | set(COMMON_PARAMS)
    for d in rows:
        params = {k: v for k, v in (d.params or {}).items() if k in known}   # (plan() drops the gateway's own)
        return params, d.name
    return {}, None


def _clean(raw: dict) -> dict:
    """The parameters a person gave, without the settings a trial never uses."""
    from forgeyard.api.deployments import GATEWAY_PARAMS

    return {k: v for k, v in (raw or {}).items() if k not in GATEWAY_PARAMS}


def plan(db: DbSession, recipe: Recipe, m: CatalogModel, node: Node, raw: Optional[dict]):
    """(context, params given to build it, where they came from): the trial's deployment as it would be placed now, on
    `node`. `raw` None: default_params. Raises HTTPException (422) the way a deploy does for bad parameters."""
    from forgeyard.api.deployments import DeploymentIn, build_context
    from forgeyard.models import prefetch

    source = None
    if raw is None:
        raw, source = default_params(db, recipe, m)
    params = _clean(raw)
    if "gpus" not in params:
        variant, _ = prefetch.variant_for(node, recipe)
        params["gpus"] = 1 if variant is not None and variant.gpu else 0
    body = DeploymentIn(name="trial", model=m.name, recipe=recipe.name, params=params, replicas=1, machine_id=node.id)
    ctx, _, _ = build_context(db, body)
    return ctx, params, source


# ------------------------------------------------------------------ starting one
def _new_name(db: DbSession, trial_id: int) -> str:
    for _ in range(20):
        name = f"{NAME_PREFIX}{trial_id}-{secrets.token_hex(2)}"
        if db.scalar(select(Workload.id).where(Workload.name == name)) is None:
            return name
    raise TrialError(409, "name_taken", "Could not find a free name for the trial's deployment.")


def running_count(db: DbSession) -> int:
    return len(db.scalars(select(EngineTrial.id).where(EngineTrial.state == RUNNING)).all())


def start(db: DbSession, *, recipe: Recipe, m: CatalogModel, node: Node, raw: Optional[dict], minutes: int,
          by: Any, ip: Optional[str]) -> EngineTrial:
    """Create the trial and its temporary deployment (committed), and place it. Raises TrialError or HTTPException."""
    from forgeyard import audit
    from forgeyard.models import scheduler
    from forgeyard.workloads import placement, state as wstate
    from forgeyard.workloads.spec import WorkloadSpec

    if db.scalar(select(EngineTrial.id).where(EngineTrial.recipe == recipe.name, EngineTrial.state == RUNNING)):
        raise TrialError(409, "trial_running", f"A trial of {recipe.name} is running. Wait for it, or cancel it.")
    if running_count(db) >= MAX_RUNNING:
        raise TrialError(409, "too_many_trials", f"{MAX_RUNNING} trials are running on this server. Wait for one to "
                                                 "finish.")
    if node.status != "online":
        raise TrialError(409, "machine_offline", f"{node.name} is {node.status}: a trial needs the machine online.")
    ctx, params, source = plan(db, recipe, m, node, raw)

    class _Dep:
        served_name = m.name

    ctx.deployment = _Dep()  # type: ignore[assignment]
    with placement._lock:
        pv = scheduler.preview(db, ctx)
    if not pv["fits"]:
        raise TrialError(409, "does_not_fit", pv["summary"], machines=pv["machines"], estimate=ctx.estimate.as_dict())
    now = utcnow()
    t = EngineTrial(recipe=recipe.name, builtin=builtin_of(recipe), engine=recipe.engine, engine_version=recipe.version,
                    model_id=m.id, model_name=m.name, task=m.task, node_id=node.id, machine_name=node.name, gpus=[],
                    params=ctx.params, estimate=ctx.estimate.as_dict(), reserved=None, requested_by=by.username,
                    created_at=now, deadline_at=now + timedelta(minutes=minutes), state=RUNNING, phase="starting",
                    checks=[], comparison=None, summary=None,
                    measurements={"params_from": source, "minutes": minutes, "baseline": {}, "after": {}})
    db.add(t)
    db.flush()
    name = _new_name(db, t.id)
    spec = WorkloadSpec.model_validate({
        "name": name, "kind": "service", "runtime": {"type": "model", "recipe": recipe.name, "model": m.name},
        "ports": [{"name": "http"}],
        "health": {"type": "http", "path": recipe.health.path, "interval": recipe.health.interval,
                   "timeout": recipe.health.timeout, "failure_threshold": recipe.health.failure_threshold,
                   "start_period": recipe.health.start_period},
        "replicas": 1, "machine_id": node.id,
    }).model_dump(mode="json")
    w = Workload(name=name, kind="service", runtime="model", spec=spec, desired_state="running", generation=1,
                 owner_id=by.id, created_by=by.username)
    db.add(w)
    db.flush()
    w.replicas.append(Replica(ordinal=0, generation=1, state="pending", gpus=[], ports=[]))
    # No gateway route: nothing outside this module can reach it (see the module's docstring).
    d = Deployment(name=name, model_id=m.id, recipe=recipe.name, params=ctx.params, served_name=name, workload_id=w.id,
                   estimate=ctx.estimate.as_dict(), created_by=by.username)
    db.add(d)
    db.flush()
    t.deployment_id = d.id
    t.measurements = {**t.measurements, "upstream": _upstream(recipe, m, name), "workload": name,
                      "baseline": _readings(db, node.id, None, {})}
    wstate.event(db, w, "created", f"Trial of {recipe.name} with {m.name} by {by.username}, on {node.name}.")
    audit.record(db, "engine_trial.started", actor=by, target=recipe.name, ip=ip, trial_id=t.id, model=m.name,
                 machine=node.name, machine_id=node.id, engine_version=recipe.version, builtin=t.builtin,
                 params=ctx.params, estimate=ctx.estimate.amount, minutes=minutes)
    db.commit()
    placement.place_and_poke(db)
    return t


def _upstream(recipe: Recipe, m: CatalogModel, served: str) -> str:
    """The `model` the engine expects, as a deployment's route would send it."""
    return ((m.meta or {}).get("repo") or served) if recipe.upstream_model == "repo" else served


# ------------------------------------------------------------------ memory readings
def _used(a: Accelerator) -> Optional[int]:
    if a.vram_used is not None:
        return int(a.vram_used)
    if a.vram_total is not None and a.vram_free is not None:
        return int(a.vram_total) - int(a.vram_free)
    return None


def _readings(db: DbSession, node_id: Optional[int], keys: Optional[list[str]], into: dict,
              after: Optional[datetime] = None) -> dict:
    """Add the machine's latest reading of each card (or of the RAM, `keys` []) to `into` ({key: [[used, at], …]}), when
    newer than the last one kept (and than `after`); at most MEMORY_SAMPLES * 4 per card. `keys` None: every card and
    the RAM (before the replica is placed, its cards are not known yet)."""
    out = {k: [list(x) for x in v] for k, v in (into or {}).items()}
    if node_id is None:
        return out
    for a in db.scalars(select(Accelerator).where(Accelerator.node_id == node_id)).all():
        if keys is not None and ((keys and a.key not in keys) or (not keys and a.vendor != "cpu")):
            continue
        used = _used(a)
        at = as_utc(a.updated_at)
        if used is None or at is None or (after is not None and at <= after):
            continue
        seen = out.setdefault(a.key, [])
        if seen and seen[-1][1] >= at.isoformat():
            continue
        seen.append([used, at.isoformat()])
        del seen[:-MEMORY_SAMPLES * 4]
    return out


def _median(samples: list) -> Optional[float]:
    vals = [s[0] for s in samples]
    return statistics.median(vals) if vals else None


def measure(baseline: dict, after: dict, keys: list[str], launched: Optional[str]) -> dict:
    """{key: {before, after, delta, samples_before, samples_after}} for the replica's cards (or "cpu" RAM row keys), from
    the readings taken before the engine was launched (at or before `launched`, ISO) and after it was ready."""
    out = {}
    for k in keys:
        before = [s for s in baseline.get(k, []) if launched is None or s[1] <= launched][-MEMORY_SAMPLES:]
        got = after.get(k, [])[:MEMORY_SAMPLES * 2]
        b, a = _median(before), _median(got)
        out[k] = {"before": b, "after": a, "delta": (a - b) if a is not None and b is not None else None,
                  "samples_before": len(before), "samples_after": len(got),
                  "peak": max((s[0] for s in got), default=None)}
    return out


# ------------------------------------------------------------------ requests
def send(url: str, payload: bytes, ctype: str, secret: Optional[str], tls_name: Optional[str]) -> tuple[int, Any, int]:
    """One request to a replica, as Try it sends it: (status, answer, milliseconds); status 0 when it could not be
    made (the answer then says why). Tests replace it."""
    from fastapi import HTTPException

    from forgeyard.api import deployments as dep_api

    started = time.monotonic()
    try:
        status, answer = dep_api._post(url, payload, ctype, secret, tls_name)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        return 0, detail.get("message", ""), int((time.monotonic() - started) * 1000)
    return status, answer, int((time.monotonic() - started) * 1000)


def _first(text: str, n: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def judge(api: str, status: int, answer: Any) -> tuple[bool, str]:
    """(whether the answer is one of this kind and says what the fixed request should get, what it says in short)."""
    from forgeyard.api.deployments import VISION_TRY, summarise

    if not 200 <= status < 300:
        return False, _first(answer if isinstance(answer, str) else str(answer), 200) or f"HTTP {status}"
    if not isinstance(answer, dict):
        return False, _first(str(answer), 200)
    said = summarise(api, answer)
    try:
        if api == "openai-chat":
            msg = answer["choices"][0]["message"]
            text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            return bool(text), _first(text) or "(empty answer)"
        if api == "openai-embeddings":
            vec = answer["data"][0]["embedding"]
            ok = isinstance(vec, list) and len(vec) > 0 and all(isinstance(x, (int, float)) and math.isfinite(x)
                                                                for x in vec)
            return ok, f"{len(vec)} dimensions" if isinstance(vec, list) else "no vector"
        if api == "rerank":
            res = sorted(answer.get("results") or [], key=lambda r: -float(r.get("relevance_score", 0)))
            # The fixed request: "What is the capital of France?" and three documents, the first about Paris.
            return bool(res) and res[0].get("index") == 0, said
        if api == "classify":
            d = answer["data"][0]
            return bool(d.get("label") is not None and d.get("probs")), said
        if api == "openai-audio":
            # The fixed clip: "… ask not what your country can do for you …".
            text = re.sub(r"[^a-z ]", "", str(answer.get("text", "")).lower())
            return "ask not" in text, _first(answer.get("text", ""))
        if api == "openai-speech":
            return bool(answer.get("riff")) and int(answer.get("audio_ms") or 0) >= 300, said
        if api == "openai-images":
            return int(answer.get("images") or 0) >= 1, said
        if api in VISION_TRY:
            return True, said
    except (KeyError, IndexError, TypeError, ValueError):
        return False, _first(said, 200)
    return True, said


def _vector(answer: Any) -> Optional[list[float]]:
    try:
        vec = answer["data"][0]["embedding"]
        return [float(x) for x in vec] if isinstance(vec, list) else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def same_answer(api: str, mine: Any, theirs: Any) -> Optional[tuple[bool, str]]:
    """Whether the copy answered the fixed request as the built-in engine did: (same, what was compared), or None when
    this kind is not compared."""
    try:
        if api == "openai-chat":
            a = " ".join(str(mine["choices"][0]["message"].get("content") or "").split())
            b = " ".join(str(theirs["choices"][0]["message"].get("content") or "").split())
            return a[:60] == b[:60], _first(b, 80)
        if api == "openai-embeddings":
            x, y = _vector(mine), _vector(theirs)
            if not x or not y or len(x) != len(y):
                return False, f"{len(x or [])} vs {len(y or [])} dimensions"
            nx, ny = math.sqrt(sum(v * v for v in x)), math.sqrt(sum(v * v for v in y))
            cos = sum(a * b for a, b in zip(x, y)) / (nx * ny) if nx and ny else 0.0
            return cos >= 0.99, f"cosine {cos:.4f}"
        if api == "rerank":
            def order(ans):
                return [r.get("index") for r in sorted(ans.get("results") or [], key=lambda r: -float(r.get("relevance_score", 0)))]
            return order(mine) == order(theirs), "order " + " ".join(f"#{i}" for i in order(theirs))
        if api == "classify":
            return mine["data"][0]["label"] == theirs["data"][0]["label"], str(theirs["data"][0]["label"])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return False, "not comparable"
    return None


def percentile(values: list[int], q: float) -> Optional[int]:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(q / 100 * len(s)))
    return s[min(k, len(s)) - 1]


# ------------------------------------------------------------------ the built-in engine, for comparison
def builtin_deployments(db: DbSession, t: EngineTrial) -> list[Deployment]:
    """Deployments of the same model on the built-in engine, those with a replica on the trial's machine first."""
    if not t.builtin or t.model_id is None:
        return []
    rows = db.scalars(select(Deployment).where(Deployment.model_id == t.model_id, Deployment.recipe == t.builtin,
                                               not_trial())
                      .options(selectinload(Deployment.workload).selectinload(Workload.replicas))
                      .order_by(Deployment.id)).all()
    return sorted(rows, key=lambda d: not any(r.node_id == t.node_id for r in d.workload.replicas))


def comparison(db: DbSession, t: EngineTrial) -> Optional[dict]:
    """The built-in engine's numbers for the same model: a deployment's last start and what it reserves, and its own
    last passed trial (memory measured, start, latency), when there are any."""
    from forgeyard.workloads import starts

    if not t.builtin:
        return None
    out: dict = {"builtin": t.builtin, "deployment": None, "trial": None}
    for d in builtin_deployments(db, t):
        last = starts.last_start(db, d.workload_id)
        out["deployment"] = {"id": d.id, "name": d.name, "reserved": (d.estimate or {}).get("amount"),
                             "start_seconds": last["seconds"] if last else None,
                             "same_machine": any(r.node_id == t.node_id for r in d.workload.replicas)}
        break
    prev = db.scalars(select(EngineTrial).where(EngineTrial.recipe == t.builtin, EngineTrial.model_id == t.model_id,
                                                EngineTrial.state == PASSED)
                      .order_by(EngineTrial.id.desc()).limit(20)).all()
    prev = sorted(prev, key=lambda x: (x.node_id != t.node_id, -x.id))
    if prev:
        p = prev[0]
        m = p.measurements or {}
        out["trial"] = {"id": p.id, "machine": p.machine_name,
                        "finished_at": as_utc(p.finished_at).isoformat() if p.finished_at else None,
                        "start_seconds": (m.get("start") or {}).get("seconds"), "measured": m.get("measured"),
                        "reserved": p.reserved, "p50_ms": m.get("p50_ms"), "p95_ms": m.get("p95_ms")}
    return out if out["deployment"] or out["trial"] else {"builtin": t.builtin, "deployment": None, "trial": None}


# ------------------------------------------------------------------ the run
def _check(id_: str, label: str, value: Any, expected: Any, ok: Optional[bool], note: Optional[str] = None,
           codes: Optional[list[dict]] = None) -> dict:
    """One check. `note` is the server's English sentence; `codes` say the same as [{code, …specifics}], so the console
    can say it in the reader's language."""
    return {"id": id_, "label": label, "value": value, "expected": expected, "ok": ok, "note": note,
            "codes": codes or []}


CHECK_ORDER = ("started", "memory", "answers", "latency", "same_answer")


def _set_check(t: EngineTrial, c: dict) -> None:
    """Add or replace a check, kept in CHECK_ORDER (the memory is read after the answers, but listed second)."""
    rows = [x for x in (t.checks or []) if x["id"] != c["id"]] + [c]
    t.checks = sorted(rows, key=lambda x: CHECK_ORDER.index(x["id"]) if x["id"] in CHECK_ORDER else len(CHECK_ORDER))


def _gib(n: Optional[float]) -> str:
    return "?" if n is None else f"{n / GIB:.2f} GiB"


def _replica(db: DbSession, t: EngineTrial) -> tuple[Optional[Deployment], Optional[Replica]]:
    if t.deployment_id is None:
        return None, None
    d = db.scalar(select(Deployment).where(Deployment.id == t.deployment_id)
                  .options(selectinload(Deployment.workload).selectinload(Workload.replicas).selectinload(Replica.node)))
    if d is None:
        return None, None
    return d, next((r for r in d.workload.replicas if r.ordinal == 0), None)


def remove_deployment(db: DbSession, t: EngineTrial) -> set[int]:
    """Delete the trial's temporary deployment and its workload (its agent stops the replica). Keeps the last events
    and the replica's last output on the trial. Returns the machines to poke. Does not commit."""
    d, r = _replica(db, t)
    nodes: set[int] = set()
    if d is not None:
        events = db.scalars(select(WorkloadEvent).where(WorkloadEvent.workload_id == d.workload_id)
                            .order_by(WorkloadEvent.id.desc()).limit(15)).all()
        t.measurements = {**(t.measurements or {}),
                          "events": [{"at": e.at.isoformat() if e.at else None, "kind": e.kind, "message": e.message}
                                     for e in reversed(events)],
                          **({"log_tail": r.log_tail[-2000:]} if r is not None and r.log_tail else {})}
        nodes = {x.node_id for x in d.workload.replicas if x.node_id is not None and not x.final}
        w = d.workload
        db.delete(d)
        db.delete(w)
    t.deployment_id = None
    return nodes


def _finish(db: DbSession, t: EngineTrial, state: str, summary: str, *, actor=None, actor_name: Optional[str] = None,
            ip: Optional[str] = None) -> set[int]:
    from forgeyard import audit

    nodes = remove_deployment(db, t)
    t.state, t.phase, t.finished_at = state, None, utcnow()
    t.summary = summary[:500]
    audit.record(db, "engine_trial.finished" if state != CANCELLED else "engine_trial.cancelled", actor=actor,
                 actor_name=actor_name or (None if actor else "forgeyard"), target=t.recipe, ip=ip, trial_id=t.id,
                 state=state, model=t.model_name, machine=t.machine_name, summary=t.summary,
                 failed=[c["id"] for c in t.checks or [] if c.get("ok") is False] or None)
    return nodes


def _poke(db: DbSession, nodes: set[int]) -> None:
    from forgeyard.machines.hub import hub
    from forgeyard.workloads import placement

    for node_id in nodes:
        hub.poke(node_id)
    placement.place_and_poke(db)


def _why(t: EngineTrial, code: Optional[dict]) -> None:
    """Why it failed or ended early, as {code, …} (the console's translation of the summary)."""
    t.measurements = {**(t.measurements or {}), "why": code}


def fail_trial(db: DbSession, t: EngineTrial, why: str, check: Optional[dict] = None,
               code: Optional[dict] = None) -> set[int]:
    _why(t, code)
    if check is not None:
        _set_check(t, check)
    for c in _not_run(t):
        _set_check(t, c)
    return _finish(db, t, FAILED, f"Failed: {why}")


def _not_run(t: EngineTrial) -> list[dict]:
    """The checks that never ran, said as such (so the table always lists all of them)."""
    have = {c["id"] for c in t.checks or []}
    out = []
    for id_, label in (("started", "Starts"), ("memory", "Memory"), ("answers", "Answers"), ("latency", "Latency")):
        if id_ not in have:
            out.append(_check(id_, label, None, None, None, "Not checked: the trial ended before.", [{"code": "not_run"}]))
    return out


def cancel(db: DbSession, t: EngineTrial, by, ip: Optional[str]) -> None:
    """A person stops a running trial: its deployment is removed; what was checked so far stays. Commits."""
    for c in _not_run(t):
        _set_check(t, c)
    _why(t, {"code": "cancelled", "who": by.username})
    nodes = _finish(db, t, CANCELLED, f"Cancelled by {by.username}.", actor=by, ip=ip)
    db.commit()
    _poke(db, nodes)


ENDED = ("backoff", "failed", "exited")


def _start_problem(r: Replica) -> Optional[tuple[str, dict]]:
    """Why the replica's start failed for good, as far as a trial goes (a trial never waits for restarts): (the
    sentence, {code, reason}), or None."""
    if r.state in ENDED or r.final:
        # The agent's "; restarting in 2 s": a trial never waits for that (it removes the replica).
        why = re.sub(r";? *restarting in [\d.]+ ?s\.?$", "", r.reason or "").strip() \
            or (f"exit code {r.exit_code}" if r.exit_code is not None else r.state)
        return f"it did not start: {why}", {"code": "start_failed", "reason": why}
    if r.node_id is None and (r.reason or "").startswith("Cannot be placed"):
        why = r.reason.removeprefix("Cannot be placed: ")
        return f"it could not be placed: {why}", {"code": "unplaced", "reason": why}
    return None


LOG_LINES, LOG_SECONDS = 40, 5.0


def live_tail(r: Replica) -> Optional[str]:
    """The replica's last lines of output, asked of its agent now (an agent sends them by itself only once a replica
    has ended for good, not while it backs off to restart): what a start that failed printed. None when it cannot be
    asked. Tests replace it."""
    from forgeyard.machines.hub import hub
    from forgeyard.machines.protocol import FEATURE_WORKLOADS, PROTOCOL_VERSION

    conn = hub.get(r.node_id) if r.node_id is not None else None
    if conn is None or FEATURE_WORKLOADS not in conn.features:
        return None
    fut = conn.request({"v": PROTOCOL_VERSION, "type": "logs_request", "replica": r.uid, "lines": LOG_LINES})
    try:
        answer = fut.result(timeout=LOG_SECONDS)
    except Exception:
        return None
    finally:
        conn.forget(fut)
    return str(answer.get("text") or "") or None if answer.get("ok") else None


def _launched(r: Replica) -> bool:
    """The engine itself has been launched (after the model, the image and any wait for a shared card)."""
    return r.state in ("healthy", "running", "unhealthy") or (r.state == "starting" and r.reason == "waiting for the "
                                                                                                   "health check")


def step(db: DbSession, t: EngineTrial, now: Optional[datetime] = None) -> tuple[set[int], bool]:
    """Move one running trial on by what can be done now. Returns (machines to poke, whether it changed); the caller
    commits. Asking the replica (one step) takes up to a few minutes."""
    from forgeyard.workloads import starts
    from forgeyard.workloads.state import is_ready

    now = now or utcnow()
    if t.state != RUNNING:
        return set(), False
    d, r = _replica(db, t)
    if d is None or r is None:
        return fail_trial(db, t, "its temporary deployment is gone (removed some other way).", code={"code": "gone"}), True
    m = dict(t.measurements or {})
    if now > as_utc(t.deadline_at):
        what = {"starting": "while it started", "measuring": "while its memory was read",
                "asking": "while it answered", "settling": "after it answered"}.get(t.phase or "", "")
        return fail_trial(db, t, f"out of time {what} (the limit was {m.get('minutes', '?')} min).".replace("  ", " "),
                          _check("started", "Starts", None, f"ready within {m.get('minutes', '?')} min", False,
                                 r.reason or r.state, [{"code": "timeout_reason", "reason": r.reason or r.state}])
                          if t.phase == "starting" else None,
                          code={"code": "timeout", "phase": t.phase, "minutes": m.get("minutes")}), True
    keys = [g["key"] for g in r.gpus or []]
    if t.phase == "starting":
        if r.node_id is not None and not t.gpus and r.gpus:
            t.gpus = [{"key": g.get("key"), "index": g.get("index"), "model": g.get("model")} for g in r.gpus]
        if r.vram is not None and t.reserved is None:
            t.reserved = int(r.vram)
        if problem := _start_problem(r):
            if not r.log_tail and (text := live_tail(r)):
                r.log_tail = text[-2000:]   # kept on the trial with its events (remove_deployment)
            tail = " / ".join((r.log_tail or "").strip().splitlines()[-3:])[:400]
            return fail_trial(db, t, problem[0], _check("started", "Starts", None, "ready", False, tail or None,
                                                        [{"code": "log", "lines": tail}] if tail else []),
                              code=problem[1]), True
        if not _launched(r):
            m["baseline"] = _readings(db, t.node_id, keys if r.node_id is not None else None, m.get("baseline") or {})
            t.measurements = m
            return set(), False
        if r.started_at is not None and not m.get("launched"):
            m["launched"] = as_utc(r.started_at).isoformat()
        if not is_ready(r):
            t.measurements = m
            return set(), False
        t.ready_at = as_utc(r.ready_at) or now
        last = starts.last_start(db, d.workload_id)
        if last is None and r.started_at is not None:
            secs = round((as_utc(t.ready_at) - as_utc(t.created_at)).total_seconds(), 1)
            last = {"seconds": secs, "phases": {}, "source": "server"}
        m["start"] = {k: last[k] for k in ("seconds", "phases", "source")} if last else None
        t.comparison = comparison(db, t)
        base = ((t.comparison or {}).get("deployment") or {}).get("start_seconds") or \
            ((t.comparison or {}).get("trial") or {}).get("start_seconds")
        _set_check(t, _check("started", "Starts", m["start"]["seconds"] if m.get("start") else None,
                             f"the built-in engine: {base:.0f} s" if base else "ready",
                             True, *(("The first start of a new version can take longer (it may compile first).",
                                      [{"code": "slower"}])
                                     if base and m.get("start") and m["start"]["seconds"] > base * 1.25 else (None, None))))
        t.phase = "measuring"
        # From here on, a bound of its own: measuring, asking and settling (a start near its limit still gets asked).
        t.deadline_at = now + timedelta(seconds=AFTER_READY_SECONDS)
        m["measure_until"] = (now + timedelta(seconds=measure_wait())).isoformat()
        t.measurements = m
        return set(), True
    if t.phase == "measuring":
        m["after"] = _readings(db, t.node_id, keys, m.get("after") or {}, after=as_utc(r.ready_at) or as_utc(t.ready_at))
        enough = all(len(m["after"].get(k, [])) >= MEMORY_SAMPLES for k in (keys or _cpu_keys(db, t.node_id)))
        if not enough and now.isoformat() < m.get("measure_until", ""):
            t.measurements = m
            return set(), False
        t.phase = "asking"
        t.measurements = m
        return set(), True
    if t.phase == "asking":
        if not is_ready(r):
            if problem := _start_problem(r):
                return fail_trial(db, t, f"it stopped while it was asked: {problem[1]['reason']}",
                                  code={"code": "stopped", "reason": problem[1]["reason"]}), True
            return fail_trial(db, t, f"it is no longer ready ({r.state}{': ' + r.reason if r.reason else ''}).",
                              code={"code": "not_ready", "state": r.state}), True
        ask(db, t, d, r)
        m = dict(t.measurements or {})
        m["settle_until"] = (utcnow() + timedelta(seconds=measure_wait(1))).isoformat()
        m["asked_at"] = utcnow().isoformat()
        t.measurements = m
        t.phase = "settling"
        return set(), True
    if t.phase == "settling":
        asked = datetime.fromisoformat(m["asked_at"]) if m.get("asked_at") else None
        before = {k: len(v) for k, v in (m.get("after") or {}).items()}
        m["after"] = _readings(db, t.node_id, keys, m.get("after") or {}, after=asked)
        fresh = all(len(m["after"].get(k, [])) > before.get(k, 0) for k in (keys or _cpu_keys(db, t.node_id)))
        if not fresh and now.isoformat() < m.get("settle_until", ""):
            t.measurements = m
            return set(), False
        t.measurements = m
        memory_check(db, t, keys or _cpu_keys(db, t.node_id))
        failed = [c for c in t.checks if c.get("ok") is False]
        state = FAILED if failed else PASSED
        _why(t, {"code": "checks", "checks": [c["id"] for c in failed]} if failed else None)
        return _finish(db, t, state, summary_of(t, state)), True
    return set(), False


def _cpu_keys(db: DbSession, node_id: Optional[int]) -> list[str]:
    if node_id is None:
        return []
    return list(db.scalars(select(Accelerator.key).where(Accelerator.node_id == node_id, Accelerator.vendor == "cpu")))


def measure_wait(samples: int = MEMORY_SAMPLES) -> int:
    """How long to wait for `samples` fresh readings from the machine (its agent sends them with each heartbeat)."""
    from forgeyard.config import get_settings

    return int(max(20, get_settings().heartbeat_seconds * (samples + 1) + 5))


def memory_check(db: DbSession, t: EngineTrial, keys: list[str]) -> None:
    """The memory check: per card (or the RAM), what the engine took (after it was ready, median of a few readings,
    minus the same before it was launched) against what the replica reserved and the estimate. Approximate on a card
    other work shares; not measured when the readings are missing or went down."""
    m = dict(t.measurements or {})
    per = measure(m.get("baseline") or {}, m.get("after") or {}, keys, m.get("launched"))
    m["memory"] = per
    deltas = [v["delta"] for v in per.values() if v["delta"] is not None]
    reserved = t.reserved
    if reserved is None:
        reserved = int((t.estimate or {}).get("amount") or 0) or None
    shared = any((v["before"] or 0) > SHARED_NOTE_BYTES for v in per.values()) or _others_on(db, t, keys)
    if not deltas or len(deltas) < len(keys):
        _set_check(t, _check("memory", "Memory", None, f"at most {_gib(reserved)} (reserved)", None,
                             "Not measured: the machine sent no reading before the engine started or after it was "
                             "ready.", [{"code": "not_measured"}]))
        t.measurements = m
        return
    worst = max(deltas)
    peak = max((v["peak"] - v["before"]) for v in per.values() if v["peak"] is not None and v["before"] is not None)
    m["measured"], m["measured_peak"] = int(worst), int(peak)
    t.measurements = m
    if worst < 0:
        _set_check(t, _check("memory", "Memory", int(worst), f"at most {_gib(reserved)} (reserved)", None,
                             "Not measured: the card's use went down meanwhile (other work ended), so the difference "
                             "says nothing.", [{"code": "went_down"}]))
        return
    limit = reserved * (1 + MEMORY_TOLERANCE) if reserved else None
    ok = None if limit is None else peak <= limit
    notes, codes = [], []
    if shared and _is_cpu_keys(db, t.node_id, keys):
        notes.append("Approximate: other programs use the machine's RAM too, and their use changes the readings.")
        codes.append({"code": "approximate_ram"})
    elif shared:
        notes.append("Approximate: other work uses this card too, and its use changes the readings.")
        codes.append({"code": "approximate"})
    if len(keys) > 1:
        notes.append(f"The largest of its {len(keys)} cards.")
        codes.append({"code": "cards", "count": len(keys)})
    if peak > worst:
        notes.append(f"{_gib(peak)} after answering.")
        codes.append({"code": "after_answers", "amount": int(peak)})
    if ok is False:
        notes.append("It uses more than it reserves: replicas sharing the card could run out of memory.")
        codes.append({"code": "over"})
    _set_check(t, _check("memory", "Memory", int(worst), f"at most {_gib(reserved)} (reserved; estimate "
                                                         f"{_gib((t.estimate or {}).get('amount'))})", ok,
                         " ".join(notes) or None, codes))


def _others_on(db: DbSession, t: EngineTrial, keys: list[str]) -> bool:
    """Other Forgeyard replicas hold one of these cards (or, for the RAM, run on the machine's CPU) now."""
    if t.node_id is None:
        return False
    own = db.scalar(select(Deployment.workload_id).where(Deployment.id == t.deployment_id)) \
        if t.deployment_id is not None else None
    cpu = _is_cpu_keys(db, t.node_id, keys)
    rows = db.scalars(select(Replica).join(Workload).where(Replica.node_id == t.node_id, Replica.final.is_(False),
                                                          Workload.desired_state == "running")).all()
    for r in rows:
        if r.workload_id == own:
            continue
        cards = {g.get("key") for g in r.gpus or []}
        if cards & set(keys) or (cpu and not cards):
            return True
    return False


def _is_cpu_keys(db: DbSession, node_id: int, keys: list[str]) -> bool:
    return set(keys) <= set(_cpu_keys(db, node_id))


def ask(db: DbSession, t: EngineTrial, d: Deployment, r: Replica) -> None:
    """The requests: one to warm it up, then REQUESTS more (fewer for slow kinds), straight to the replica with its
    internal key; then the same once to a ready deployment of the built-in engine, to compare the answers."""
    from forgeyard.api.deployments import try_request
    from forgeyard.gateway import imagegen
    from forgeyard.models import catalog, routes, vision

    recipe = catalog.get_recipe(db, d.recipe)
    m = dict(t.measurements or {})
    api = recipe.api_for(t.task) if recipe is not None else None
    m["api"] = api
    if api is None or api in NOT_ASKED:
        why = NOT_ASKED.get(api or "", "This engine has no way to ask the model.")
        kind = [{"code": "not_asked", "why": "video" if api == "openai-videos" else "none"}]
        _set_check(t, _check("answers", "Answers", None, None, None, f"Not checked: {why}", kind))
        _set_check(t, _check("latency", "Latency", None, None, None, "Not checked.", kind))
        t.measurements = m
        return
    target = next(iter(routes.endpoints(d.workload, ready_only=True, tls=True)), None)
    if target is None:
        _set_check(t, _check("answers", "Answers", None, "answers", False, "Its replica has no address to ask.",
                             [{"code": "no_address"}]))
        t.measurements = m
        return
    voice = recipe.speech.default_voice if recipe.tts is not None else None
    draws = imagegen.draws(recipe, d.params or {}, d.model.meta if d.model is not None else None) \
        if "image" in recipe.tasks else None
    zero_shot = d.model is not None and vision.is_zero_shot(d.model.meta or {})
    try:
        path, payload, ctype = try_request(api, m.get("upstream") or d.served_name, None, voice, draws, zero_shot)
    except Exception as e:   # (an HTTPException: no request for this kind)
        _set_check(t, _check("answers", "Answers", None, None, None, f"Not checked: {getattr(e, 'detail', e)}",
                             [{"code": "not_asked", "why": "none"}]))
        t.measurements = m
        return
    url = target["url"].rstrip("/") + path
    count = FEWER.get(api, REQUESTS)
    warm = count > 1
    results = []
    for i in range(count + (1 if warm else 0)):
        status, answer, ms = send(url, payload, ctype, r.secret, target.get("tls_name"))
        ok, said = judge(api, status, answer)
        results.append({"status": status, "ok": ok, "said": said, "ms": ms, "warm": warm and i == 0})
        if i == 0:
            first_answer = answer
    good = [x for x in results if x["ok"]]
    measured = [x["ms"] for x in results if not x["warm"] and 200 <= x["status"] < 300]
    m["requests"] = [{k: x[k] for k in ("status", "ok", "ms", "warm")} for x in results]
    m["first_answer"] = results[0]["said"]
    m["p50_ms"], m["p95_ms"] = percentile(measured, 50), percentile(measured, 95)
    m["warmup_ms"] = results[0]["ms"] if warm else None
    bad = next((x for x in results if not x["ok"]), None)
    _set_check(t, _check("answers", "Answers", f"{len(good)}/{len(results)}", f"{len(results)}/{len(results)}",
                         len(good) == len(results),
                         (f"First answer: {results[0]['said']}" if bad is None else f"HTTP {bad['status']}: {bad['said']}")[:400],
                         [{"code": "first_answer", "text": results[0]["said"][:300]}] if bad is None
                         else [{"code": "error", "status": bad["status"], "text": bad["said"][:300]}]))
    _set_check(t, _check("latency", "Latency", {"p50_ms": m["p50_ms"], "p95_ms": m["p95_ms"], "warmup_ms": m["warmup_ms"],
                                                "count": len(measured)}, None, None,
                         None if measured else "No answer to time.", None if measured else [{"code": "no_answer"}]))
    # The same request once to the built-in engine, when a deployment of the same model on it is ready.
    for other in builtin_deployments(db, t):
        there = next(iter(routes.endpoints(other.workload, ready_only=True, tls=True)), None)
        if there is None:
            continue
        rep = next((x for x in other.workload.replicas if x.ordinal == there["replica"]), None)
        route_upstream = _upstream(recipe, d.model, other.served_name) if d.model is not None else other.served_name
        try:
            path2, payload2, ctype2 = try_request(api, route_upstream, None, voice, draws, zero_shot)
        except Exception:
            break
        status, answer, ms = send(there["url"].rstrip("/") + path2, payload2, ctype2, rep.secret if rep else None,
                                  there.get("tls_name"))
        if 200 <= status < 300 and results[0]["ok"]:
            got = same_answer(api, first_answer, answer)
            if got is not None:
                _set_check(t, _check("same_answer", "Same answer as the built-in engine", got[1],
                                     f"{other.name} ({t.builtin})", None,
                                     "The same." if got[0] else "Different: a new version may answer differently; "
                                     "read both before switching.", [{"code": "same" if got[0] else "different"}]))
                m["builtin_ms"] = ms
        break
    t.measurements = m


def summary_of(t: EngineTrial, state: str) -> str:
    m = t.measurements or {}
    parts = []
    if m.get("start"):
        parts.append(f"ready in {m['start']['seconds']:.0f} s")
    ans = next((c for c in t.checks if c["id"] == "answers"), None)
    if ans and ans.get("value"):
        parts.append(f"{ans['value']} answers")
    if m.get("p50_ms") is not None:
        parts.append(f"p50 {m['p50_ms']} ms")
    if m.get("measured") is not None:
        parts.append(f"+{_gib(m['measured'])} of {_gib(t.reserved)} reserved")
    failed = [c["label"] for c in t.checks if c.get("ok") is False]
    head = "Passed" if state == PASSED else f"Failed ({', '.join(failed)})"
    return f"{head}: {', '.join(parts)}." if parts else f"{head}."


# ------------------------------------------------------------------ the loop
class Runner:
    """Moves the running trials on, a few seconds apart, each in a worker thread of its own (one step at a time per
    trial). Tests turn it off and call advance() themselves."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.enabled = True
        self._busy: set[int] = set()
        self._lock = threading.Lock()

    def claim(self, trial_id: int) -> bool:
        with self._lock:
            if trial_id in self._busy:
                return False
            self._busy.add(trial_id)
            return True

    def release(self, trial_id: int) -> None:
        with self._lock:
            self._busy.discard(trial_id)


runner = Runner()


def advance(trial_id: int, now: Optional[datetime] = None) -> Optional[str]:
    """One step of one trial, in its own session; returns its state after it (None: no such trial). A trial cancelled
    meanwhile (by a person, in another session) is left as it is."""
    if not runner.claim(trial_id):
        return None
    db = new_session()
    try:
        t = db.get(EngineTrial, trial_id)
        if t is None:
            return None
        try:
            nodes, _ = step(db, t, now)
        except Exception as e:   # never leave a trial running with its deployment because of a bug
            log.exception("Trial %s failed unexpectedly", trial_id)
            db.rollback()
            t = db.get(EngineTrial, trial_id)
            if t is None or t.state != RUNNING:
                return t.state if t else None
            nodes = fail_trial(db, t, f"an error in the server ({type(e).__name__}).",
                               code={"code": "error", "name": type(e).__name__})
        # Someone cancelled it while this step ran (asking takes a while): theirs stands.
        state = db.scalar(select(EngineTrial.state).where(EngineTrial.id == trial_id).execution_options(
            populate_existing=True))
        if state is not None and state != RUNNING and t.state == RUNNING:
            db.rollback()
            return state
        db.commit()
        if nodes:
            _poke(db, nodes)
        return t.state
    finally:
        db.close()
        runner.release(trial_id)


def running_ids() -> list[int]:
    db = new_session()
    try:
        return list(db.scalars(select(EngineTrial.id).where(EngineTrial.state == RUNNING).order_by(EngineTrial.id)))
    finally:
        db.close()


async def run_forever(interval: float = TICK_SECONDS) -> None:
    tasks: dict[int, asyncio.Task] = {}
    while True:
        await asyncio.sleep(interval)
        if not runner.enabled:
            continue
        try:
            for trial_id in await asyncio.to_thread(running_ids):
                if trial_id in tasks and not tasks[trial_id].done():
                    continue
                tasks[trial_id] = asyncio.create_task(asyncio.to_thread(advance, trial_id))
            for k in [k for k, v in tasks.items() if v.done()]:
                tasks.pop(k)
        except Exception:
            log.exception("Moving the engine trials on failed")


def sweep_at_start() -> int:
    """The server just started: a trial that was running is over (its steps were in the old process); its temporary
    deployment is removed and it is marked failed. Also removes a trial deployment left behind by a finished trial.
    Returns how many trials it ended."""
    db = new_session()
    ended = 0
    try:
        nodes: set[int] = set()
        for t in db.scalars(select(EngineTrial).where(EngineTrial.state == RUNNING)).all():
            for c in _not_run(t):
                _set_check(t, c)
            _why(t, {"code": "restarted"})
            nodes |= _finish(db, t, FAILED, "Failed: the server restarted during the trial; its temporary deployment "
                                            "was removed.")
            ended += 1
        for t in db.scalars(select(EngineTrial).where(EngineTrial.state != RUNNING,
                                                      EngineTrial.deployment_id.is_not(None))).all():
            nodes |= remove_deployment(db, t)
        db.commit()
        if nodes:
            _poke(db, nodes)
        if ended:
            log.warning("%d engine trial(s) were running when the server stopped: ended, their deployments removed.",
                        ended)
    finally:
        db.close()
    return ended


# ------------------------------------------------------------------ what the API shows
def trial_out(t: EngineTrial, *, detail: bool = True) -> dict:
    m = t.measurements or {}
    out = {"id": t.id, "recipe": t.recipe, "builtin": t.builtin, "engine": t.engine, "engine_version": t.engine_version,
           "model": {"id": t.model_id, "name": t.model_name, "task": t.task},
           "machine": {"id": t.node_id, "name": t.machine_name}, "gpus": t.gpus or [], "params": t.params or {},
           "estimate": (t.estimate or {}).get("amount"), "device": (t.estimate or {}).get("device"),
           "reserved": t.reserved, "requested_by": t.requested_by, "created_at": t.created_at, "ready_at": t.ready_at,
           "finished_at": t.finished_at, "deadline_at": t.deadline_at, "state": t.state, "phase": t.phase,
           "summary": t.summary, "why": m.get("why"), "params_from": m.get("params_from"),
           "start": m.get("start"), "measured": m.get("measured"), "measured_peak": m.get("measured_peak"),
           "p50_ms": m.get("p50_ms"), "p95_ms": m.get("p95_ms"), "first_answer": m.get("first_answer"),
           "checks": t.checks or [], "comparison": t.comparison}
    if detail:
        out["events"] = m.get("events") or []
        out["log_tail"] = m.get("log_tail")
        out["requests"] = m.get("requests") or []
        out["memory"] = m.get("memory") or {}
    return out


def brief_out(t: EngineTrial) -> dict:
    """A running or last trial in a line (the deployments list, a copy's card, the switch form)."""
    return {"id": t.id, "recipe": t.recipe, "model": t.model_name, "machine": t.machine_name, "machine_id": t.node_id,
            "state": t.state, "phase": t.phase, "created_at": t.created_at, "finished_at": t.finished_at,
            "summary": t.summary, "why": (t.measurements or {}).get("why"), "deployment_id": t.deployment_id, "task": t.task,
            "workload": (t.measurements or {}).get("workload")}


def last_for(db: DbSession, recipe: str, model_id: Optional[int] = None) -> Optional[dict]:
    """The latest finished trial of an engine (for this model first, when one is given), in a line."""
    rows = db.scalars(select(EngineTrial).where(EngineTrial.recipe == recipe, EngineTrial.state.in_((PASSED, FAILED)))
                      .order_by(EngineTrial.id.desc()).limit(50)).all()
    if model_id is not None:
        rows = sorted(rows, key=lambda x: x.model_id != model_id)
    return brief_out(rows[0]) if rows else None


def last_per_recipe(db: DbSession) -> dict[str, dict]:
    """{recipe: its latest finished trial in a line}, for every recipe with one."""
    latest = select(EngineTrial.recipe, func.max(EngineTrial.id).label("id")) \
        .where(EngineTrial.state.in_((PASSED, FAILED))).group_by(EngineTrial.recipe).subquery()
    rows = db.scalars(select(EngineTrial).join(latest, EngineTrial.id == latest.c.id)).all()
    return {t.recipe: brief_out(t) for t in rows}


def running(db: DbSession) -> list[EngineTrial]:
    return list(db.scalars(select(EngineTrial).where(EngineTrial.state == RUNNING).order_by(EngineTrial.id)))
