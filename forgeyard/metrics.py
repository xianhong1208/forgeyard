"""Prometheus metrics for `GET /metrics` (1.4.5, #165; forgeyard/api/metrics.py serves them).

Every metric lives in one private registry, `REGISTRY`, so the endpoint shows exactly Forgeyard's metrics plus the
process collector, never a library's.

**Labels are bounded on purpose.** A model label is a served name the gateway has a route for (a request for a
model nobody serves counts under `_unknown`, since the name comes from the client), an endpoint label one of the
gateway's endpoints, a status the HTTP status code, a machine label the machine's name and a GPU label its index.
No metric carries an API key, a key id, a user name, a prompt or a provider's model id. As a last guard, a label
that has seen MAX_VALUES different values already takes `_other`.

Requests are counted where their usage is (forgeyard/gateway/proxy.py `record`, the `/x/` passthrough and video
jobs), so the two never disagree. What describes the state now (replicas by state, machines, GPU memory, the
waiting line, budget spend) is read when Prometheus scrapes, by `_State`, instead of being kept up to date.

Recording never raises: a metrics bug must not fail a request.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional

from prometheus_client import (CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, ProcessCollector,
                               disable_created_metrics, generate_latest)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from forgeyard import __version__

log = logging.getLogger("forgeyard")

# No `*_created` series: they double the output and Prometheus does not need them.
disable_created_metrics()

REGISTRY = CollectorRegistry(auto_describe=True)
ProcessCollector(registry=REGISTRY)

UNKNOWN = "_unknown"
OTHER = "_other"
MAX_VALUES = 500          # different values one label may take before the rest count as OTHER

# Seconds. From fast embeddings to long generations and transcriptions (GATEWAY_READ_TIMEOUT is 600 s).
LATENCY_BUCKETS = (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
WAIT_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)   # a wait for a slot is at most 10 s

REQUESTS = Counter("forgeyard_gateway_requests", "Requests the gateway finished, by endpoint, model, HTTP status, "
                   "source (api: an API key; console: the playground) and target (local, or provider: a cloud or "
                   "other provider model)", ("endpoint", "model", "status", "source", "target"), registry=REGISTRY)
DURATION = Histogram("forgeyard_gateway_request_duration_seconds", "Time from a request's arrival to its end (the "
                     "whole answer sent), per model", ("endpoint", "model"), buckets=LATENCY_BUCKETS, registry=REGISTRY)
FIRST_BYTE = Histogram("forgeyard_gateway_time_to_first_byte_seconds", "Time from a request's arrival to the first "
                       "bytes of its answer from the replica or provider (a stream's first chunk; otherwise the status "
                       "line), per model; only requests that got an answer", ("endpoint", "model"),
                       buckets=LATENCY_BUCKETS, registry=REGISTRY)
TOKENS = Counter("forgeyard_gateway_tokens", "Tokens the engines and providers reported, per model; direction in: "
                 "the prompt, out: the completion", ("model", "direction"), registry=REGISTRY)
SPILLS = Counter("forgeyard_gateway_spills", "Requests that left their route's first tier (spilled to the cloud or "
                 "another tier), per model and reason: unavailable, busy, refused, too_long, budget",
                 ("model", "reason"), registry=REGISTRY)
WAITED = Histogram("forgeyard_gateway_wait_seconds", "How long a request waited in line for a free local slot "
                   "(the route's Wait for a free local slot), per model; it then got a slot or moved on",
                   ("model",), buckets=WAIT_BUCKETS, registry=REGISTRY)

# 1.5.0 (#37): automatic replicas (forgeyard/workloads/autoscale.py).
AUTOSCALED = Counter("forgeyard_autoscale", "Replicas the autoscaler added (direction up) or removed after draining "
                     "them (down), per deployment", ("deployment", "direction"), registry=REGISTRY)

SPILL_REASONS = frozenset({"unavailable", "busy", "refused", "too_long", "budget"})
SOURCES = frozenset({"api", "console"})

_seen: dict[str, set[str]] = {}
_seen_lock = threading.Lock()


def bounded(label: str, value: Optional[str], allowed: Optional[Iterable[str]] = None) -> str:
    """`value` as a label value: one of `allowed` (else OTHER), or any value while this label has fewer than
    MAX_VALUES different ones (an empty one is UNKNOWN)."""
    value = (value or "").strip()[:128]
    if not value:
        return UNKNOWN
    if allowed is not None:
        return value if value in allowed else OTHER
    with _seen_lock:
        seen = _seen.setdefault(label, set())
        if value in seen:
            return value
        if len(seen) >= MAX_VALUES:
            return OTHER
        seen.add(value)
        return value


def target_of(target: Optional[str]) -> str:
    """The usage target ("local", "pm:<id>") without the provider model's id."""
    return "local" if not target or target == "local" else "provider"


def request(*, endpoint: str, model: Optional[str], known: bool, status: int, source: str, target: Optional[str],
            seconds: float, first_byte: Optional[float] = None, prompt_tokens: int = 0, completion_tokens: int = 0,
            spilled: Optional[str] = None) -> None:
    """One finished gateway request. `known`: the model is a route the gateway has (else its name, which the client
    sent, is not used as a label)."""
    try:
        m = bounded("model", model) if known else UNKNOWN
        ep = bounded("endpoint", endpoint)
        REQUESTS.labels(ep, m, str(int(status)), bounded("source", source, SOURCES), target_of(target)).inc()
        DURATION.labels(ep, m).observe(max(0.0, seconds))
        if first_byte is not None:
            FIRST_BYTE.labels(ep, m).observe(max(0.0, first_byte))
        if prompt_tokens > 0:
            TOKENS.labels(m, "in").inc(prompt_tokens)
        if completion_tokens > 0:
            TOKENS.labels(m, "out").inc(completion_tokens)
        if spilled:
            SPILLS.labels(m, bounded("reason", spilled, SPILL_REASONS)).inc()
    except Exception:  # never fail a request over a metric
        log.exception("Counting a request for /metrics failed")


def waited(line_key: str, seconds: float) -> None:
    """A request left the waiting line `line_key` ("<served name>#<tier>") after `seconds`."""
    try:
        WAITED.labels(bounded("model", line_key.rsplit("#", 1)[0])).observe(max(0.0, seconds))
    except Exception:
        log.exception("Counting a wait for /metrics failed")


def scaled(deployment: str, direction: str) -> None:
    """The autoscaler added (up) or removed (down) one replica of `deployment`."""
    try:
        AUTOSCALED.labels(bounded("deployment", deployment), bounded("direction", direction, ("up", "down"))).inc()
    except Exception:
        log.exception("Counting an autoscaling change for /metrics failed")


# ------------------------------------------------------------------ the state, read at scrape time
REPLICA_STATES = ("pending", "starting", "running", "healthy", "unhealthy", "backoff", "exited", "failed",
                  "stopping", "stopped")


class _State(Collector):
    """Gauges read from the database and the gateway's memory when Prometheus scrapes."""

    def describe(self):
        return []   # nothing to describe up front (auto_describe would otherwise run collect at registration)

    def collect(self):
        from forgeyard.db import new_session

        families = []
        db = new_session()
        try:
            for part in (_build, _replicas, _deployment_replicas, _machines, _memory, _budgets):
                try:
                    families.extend(part(db))
                except Exception:  # one part failing (a locked database) leaves the others
                    log.exception("Reading %s for /metrics failed", part.__name__.lstrip("_"))
                    db.rollback()
        finally:
            db.close()
        try:
            families.extend(_waiting())
        except Exception:
            log.exception("Reading the waiting lines for /metrics failed")
        return families


def _build(_db):
    g = GaugeMetricFamily("forgeyard_build_info", "This server's version (always 1)", labels=("version",))
    g.add_metric((__version__,), 1)
    return [g]


def _replicas(db):
    from sqlalchemy import func, select

    from forgeyard.db import Deployment, Replica, Workload

    g = GaugeMetricFamily("forgeyard_replicas", "Replicas of each model deployment, by state (pending, starting, "
                          "running, healthy, unhealthy, backoff, exited, failed, stopping, stopped)",
                          labels=("deployment", "state"))
    rows = db.execute(select(Deployment.name, Replica.state, func.count())
                      .join(Workload, Workload.id == Deployment.workload_id)
                      .join(Replica, Replica.workload_id == Workload.id)
                      .group_by(Deployment.name, Replica.state).order_by(Deployment.name, Replica.state)).all()
    for name, state, n in rows:
        g.add_metric((name, state if state in REPLICA_STATES else OTHER), n)
    return [g]


def _deployment_replicas(db):
    """1.5.0 (#37): per deployment, its replicas desired (rows not ended), ready and draining, and (automatic
    replicas) what the load asks for and the load itself."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from forgeyard.db import Deployment, Replica, Workload
    from forgeyard.workloads import autoscale

    g = GaugeMetricFamily("forgeyard_deployment_replicas", "Replicas of each model deployment: desired (the "
                          "replicas it should run now), ready (taking requests), draining (being removed by the "
                          "autoscaler) and wanted (what its load asks for, within its bounds; automatic replicas "
                          "only)", labels=("deployment", "kind"))
    load = GaugeMetricFamily("forgeyard_deployment_load", "Requests in flight on a deployment's replicas plus those "
                             "waiting for a slot, as the autoscaler last sampled them (automatic replicas only)",
                             labels=("deployment",))
    draining = autoscale.autoscaler.draining()
    loads = autoscale.autoscaler.loads()
    rows = db.scalars(select(Deployment).options(selectinload(Deployment.workload).selectinload(Workload.replicas)
                                                 .selectinload(Replica.node)).order_by(Deployment.name)).all()
    for d in rows:
        w = d.workload
        if w is None:
            continue
        live = [r for r in w.replicas if not r.final]
        desired = len(live) if w.desired_state == "running" else 0
        g.add_metric((d.name, "desired"), desired)
        g.add_metric((d.name, "ready"), sum(1 for r in live if autoscale._ready(r, w)))
        g.add_metric((d.name, "draining"), 1 if d.id in draining else 0)
        wanted = autoscale.autoscaler.status(d)["wanted"]   # None unless automatic replicas are on
        if wanted is not None:
            g.add_metric((d.name, "wanted"), wanted)
        if d.id in loads:
            load.add_metric((d.name,), loads[d.id])
    return [g, load]


def _machines(db):
    from sqlalchemy import select

    from forgeyard.db import Node

    up = GaugeMetricFamily("forgeyard_machine_up", "1 while the machine's agent is connected (online), 0 when it is "
                           "offline or revoked", labels=("machine",))
    count = GaugeMetricFamily("forgeyard_machines", "Machines by status (online, offline, revoked)",
                              labels=("status",))
    by = {"online": 0, "offline": 0, "revoked": 0}
    for n in db.scalars(select(Node).order_by(Node.name, Node.id)).all():
        status = "revoked" if n.revoked_at is not None or n.status == "revoked" else n.status
        by[status if status in by else "offline"] += 1
        up.add_metric((n.name,), 1 if status == "online" else 0)
    for status, n in by.items():
        count.add_metric((status,), n)
    return [count, up]


def _memory(db):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from forgeyard.db import Node
    from forgeyard.workloads import placement

    total = GaugeMetricFamily("forgeyard_gpu_memory_total_bytes", "A GPU's memory, as its agent reported it",
                              labels=("machine", "gpu"))
    free = GaugeMetricFamily("forgeyard_gpu_memory_free_bytes", "A GPU's free memory, as its agent last reported it "
                             "(other work on the card counts too)", labels=("machine", "gpu"))
    reserved = GaugeMetricFamily("forgeyard_gpu_memory_reserved_bytes", "Memory Forgeyard's replicas reserve on a GPU "
                                 "(what placement counts; a replica without a declared amount counts 0)",
                                 labels=("machine", "gpu"))
    ram_total = GaugeMetricFamily("forgeyard_machine_memory_total_bytes", "A machine's RAM, as its agent reported it",
                                  labels=("machine",))
    ram_free = GaugeMetricFamily("forgeyard_machine_memory_free_bytes", "A machine's free RAM, as its agent last "
                                 "reported it", labels=("machine",))
    ram_reserved = GaugeMetricFamily("forgeyard_machine_memory_reserved_bytes", "RAM Forgeyard's replicas reserve on "
                                     "a machine (CPU replicas, and layers a GPU replica keeps on the CPU)",
                                     labels=("machine",))
    nodes = db.scalars(select(Node).options(selectinload(Node.accelerators)).where(Node.revoked_at.is_(None))
                       .order_by(Node.name, Node.id)).all()
    for n in nodes:
        used = placement.usage(db, n.id, node=n)
        for a in n.accelerators:
            if a.vendor == "cpu":
                if a.vram_total:
                    ram_total.add_metric((n.name,), a.vram_total)
                if a.vram_free is not None:
                    ram_free.add_metric((n.name,), a.vram_free)
                ram_reserved.add_metric((n.name,), sum(h.vram or 0 for h in used.ram))
                continue
            labels = (n.name, str(a.index))
            if a.vram_total:
                total.add_metric(labels, a.vram_total)
            if a.vram_free is not None:
                free.add_metric(labels, a.vram_free)
            reserved.add_metric(labels, sum(h.vram or 0 for h in used.cards.get(a.key, [])))
    return [total, free, reserved, ram_total, ram_free, ram_reserved]


def _budgets(db):
    from sqlalchemy import select

    from forgeyard.db import Provider
    from forgeyard.gateway import budgets

    spend = budgets.ledger.snapshot()
    spent = GaugeMetricFamily("forgeyard_cloud_spend_usd", "What each provider cost this UTC month (USD), as the "
                              "budgets count it", labels=("provider",))
    limit = GaugeMetricFamily("forgeyard_provider_budget_usd", "A provider's monthly budget (USD), when it has one",
                              labels=("provider",))
    for p in db.scalars(select(Provider).order_by(Provider.name)).all():
        spent.add_metric((p.name,), spend.spent.get(("provider", p.id), 0) / 1e6)
        if p.budget_micros is not None:
            limit.add_metric((p.name,), p.budget_micros / 1e6)
    # Keys and people: how many budgets are reached, never whose (no key ids or user names as labels).
    reached = GaugeMetricFamily("forgeyard_budgets_reached", "Budgets whose spend this month reached a share of them "
                                "(threshold 80 or 100 percent), per scope (key, person, provider)",
                                labels=("scope", "threshold"))
    counts = {(s, t): 0 for s in budgets.SCOPES for t in budgets.THRESHOLDS}
    for scope, ident, micros in _budget_limits(db):
        used = spend.spent.get((scope, ident), 0)
        for t in budgets.THRESHOLDS:
            if used * 100 >= micros * t:  # (a budget of 0 is reached at once)
                counts[(scope, t)] += 1
    for (scope, t), n in counts.items():
        reached.add_metric((scope, str(t)), n)
    return [spent, limit, reached]


def _budget_limits(db) -> list[tuple[str, int, int]]:
    from sqlalchemy import select

    from forgeyard.db import ApiKey, Provider, User

    out = [("key", k.id, k.budget_micros) for k in db.scalars(select(ApiKey).where(ApiKey.budget_micros.is_not(None),
                                                                                    ApiKey.revoked_at.is_(None)))]
    out += [("person", u.id, u.budget_micros) for u in db.scalars(select(User).where(User.budget_micros.is_not(None)))]
    out += [("provider", p.id, p.budget_micros) for p in db.scalars(select(Provider)
                                                                    .where(Provider.budget_micros.is_not(None)))]
    return out


def _waiting():
    from forgeyard.gateway import waiting

    g = GaugeMetricFamily("forgeyard_gateway_waiting", "Requests waiting in line for a free local slot now, per "
                          "model", labels=("model",))
    per: dict[str, int] = {}
    for key, n in waiting.lengths().items():
        name = bounded("model", key.rsplit("#", 1)[0])
        per[name] = per.get(name, 0) + n
    for name, n in sorted(per.items()):
        g.add_metric((name,), n)
    return [g]


REGISTRY.register(_State())


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def reset() -> None:
    """Tests: forget the label values seen (the counters themselves are cumulative, as Prometheus expects)."""
    with _seen_lock:
        _seen.clear()
