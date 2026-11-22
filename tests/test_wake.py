"""Waking an asleep model on a request (1.5.0, #37, Tony's Q3 A; forgeyard/gateway/waking.py): one wake however
many requests arrive, the wake line (its order, its bound, a client leaving it), spilling to a cloud tier while it
wakes (not structured output a provider would lose; back to waiting when the cloud fails), the honest give-ups
(cannot_wake with the reason, at once), the wait running out (model_waking with Retry-After), a person's Stop never
woken, rate limits counting a held request once, a stream starting only when the model answers, what does not
wake it, Wake now, what counts as use, `wake_wait` and the API (FY-WAKE-*). Every engine and provider is a local
stand-in; "the agents" are the test making the woken replicas ready."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard import metrics
from forgeyard.db import Deployment, Node, Workload, WorkloadEvent, new_session, utcnow
from forgeyard.gateway import waiting, waking
from forgeyard.gateway.route import LocalGroup
from forgeyard.gateway.upstream import ClientGone
from forgeyard.workloads import placement, sleep
from forgeyard.workloads.sleep import idle
from tests.gateway_helpers import api_client, make_ready, new_key
from tests.test_deploy_edit import CPU_BODY, cpu_row, rows, set_replica, workload
from tests.test_gateway import chat, err, gw  # noqa: F401  (the fixture)
from tests.test_routing import mixed  # noqa: F401  (the fixture)
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.test_structured_spill import SCHEMA
from tests.test_video_generation import make, vid  # noqa: F401  (the fixture)
from tests.waits import wait_until
from tests.workload_helpers import make_node

GIB = 1024**3
ON = {"sleep_when_idle": True}


@pytest.fixture(autouse=True)
def quick_looks(monkeypatch):
    """Looks at the wake shared for 50 ms (1 s in the server), and no memory of earlier machines."""
    monkeypatch.setattr(waking, "LOOK_TTL", 0.05)
    placement._previous.clear()
    yield
    placement._previous.clear()


def background(fn):
    """fn() in a thread; daemon, so a failing test never keeps the run waiting for a request still in line."""
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()), daemon=True)
    t.start()
    return t, out


def later(minutes: float):
    return utcnow() + timedelta(minutes=minutes)


def dep_id(c, name: str) -> int:
    return next(x["id"] for x in c.get("/api/deployments").json()["deployments"] if x["name"] == name)


def nap(c, name: str = "chat", replicas: int = 2, **params) -> int:
    """Sleep when idle on, then asleep (its replicas stopped, as its agents would confirm)."""
    d = dep_id(c, name)
    r = c.patch(f"/api/deployments/{d}", json={"params": {**ON, **params}})
    assert r.status_code == 200 and r.json()["restart"] == "none", r.text
    assert name in sleep.sweep_once(later(31))
    for i in range(replicas):
        set_replica(name, i, state="stopped")
    assert workload(name).desired_state == "asleep"
    return d


def wake_up(name: str, targets) -> None:
    """What the agents do after the wake: the replicas start and become ready; their status reaches the server."""
    wait_until(lambda: workload(name).desired_state == "running" and all(r.node_id for r in rows(name)),
               f"{name} woken and placed")
    make_ready(name, targets)
    waking.changed()


def gw_targets(g):
    return [(g.a, g.e1.port, g.e1.secret), (g.b, g.e2.port, g.e2.secret)]


def woken_events(name: str) -> list[str]:
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == name))
        return [e.message for e in db.scalars(select(WorkloadEvent).where(WorkloadEvent.workload_id == w.id,
                                                                          WorkloadEvent.kind == "woken"))]
    finally:
        db.close()


def in_line(key: str = "chat#0", n: int = 1) -> None:
    wait_until(lambda: waking.lengths().get(key, 0) >= n, f"{n} request(s) waiting for the wake in {key}")


def set_params(name: str, **params) -> None:
    """Straight in the database (values the API refuses, for a short test)."""
    db = new_session()
    try:
        d = db.scalar(select(Deployment).where(Deployment.name == name))
        d.params = {**(d.params or {}), **params}
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------ a request wakes it
def test_fy_wake_001_a_request_wakes_it_and_waits_for_it(gw):  # noqa: F811
    """FY-WAKE-001: a request to an asleep model wakes it (desired running, a new generation, the event naming
    the key, not audited, counted in /metrics) and waits, with nothing sent back, until a replica is ready; then a
    replica answers it. The deployment API shows the wake while it lasts."""
    d = nap(gw.owner)
    gen = workload("chat").generation
    t, out = background(lambda: chat(gw.api))
    in_line()
    assert workload("chat").desired_state == "running" and workload("chat").generation == gen + 1
    assert woken_events("chat") == [f"Woken by a request (key laptop) (generation {gen + 1})."]
    shown = gw.owner.get(f"/api/deployments/{d}").json()["deployment"]["sleep"]
    assert shown["asleep"] is False and shown["waking"]["by"] == "a request (key laptop)"
    assert shown["waking"]["waiting"] == 1 and shown["wake_wait"] == 300
    time.sleep(0.3)
    assert "r" not in out and len(gw.e1.posts()) + len(gw.e2.posts()) == 0
    wake_up("chat", gw_targets(gw))
    t.join(10)
    r = out["r"]
    assert r.status_code == 200 and r.headers["x-forgeyard-target"].startswith("local:chat#"), r.text
    assert "x-forgeyard-spilled" not in r.headers
    assert gw.owner.get(f"/api/deployments/{d}").json()["deployment"]["sleep"]["waking"] is None
    assert waking.lengths().get("chat#0", 0) == 0
    db = new_session()
    try:
        from forgeyard.db import AuditEvent

        assert not [a for a in db.scalars(select(AuditEvent).where(AuditEvent.action == "workload.started"))]
    finally:
        db.close()
    text = metrics.render()[0].decode()
    assert 'forgeyard_deployment_wakes_total{by="request",deployment="chat"}' in text
    assert 'forgeyard_gateway_wake_wait_seconds_count{model="chat"}' in text


def test_fy_wake_002_twenty_parallel_requests_one_wake(gw, monkeypatch):  # noqa: F811
    """FY-WAKE-002: 20 requests at once: the deployment is woken once (one generation, one event, one thread
    doing it), all 20 wait and all 20 are answered by the replicas."""
    nap(gw.owner)
    gen = workload("chat").generation
    calls = []
    real = waking._wake
    monkeypatch.setattr(waking, "_wake", lambda *a: calls.append(a) or real(*a))
    threads = [background(lambda: chat(gw.api)) for _ in range(20)]
    in_line(n=20)
    assert len(calls) == 1 and workload("chat").generation == gen + 1 and len(woken_events("chat")) == 1
    wake_up("chat", gw_targets(gw))
    for t, _ in threads:
        t.join(20)
    assert [o["r"].status_code for _, o in threads] == [200] * 20
    assert len(gw.e1.posts()) + len(gw.e2.posts()) == 20
    assert workload("chat").generation == gen + 1 and len(woken_events("chat")) == 1


def test_fy_wake_003_a_second_wake_of_the_same_deployment_does_nothing(gw):  # noqa: F811
    """FY-WAKE-003: the wake itself is single flight in the database too: asked twice (two threads that both got
    past the gateway's own check), it wakes once; the second finds it awake."""
    d = nap(gw.owner)
    gen = workload("chat").generation
    assert waking._wake(d, "a request (key a)") == "woken"
    assert waking._wake(d, "a request (key b)") == "awake"
    assert workload("chat").generation == gen + 1 and len(woken_events("chat")) == 1


def test_fy_wake_004_still_stopping_it_is_woken_once_stopped(gw):  # noqa: F811
    """FY-WAKE-004: a request right after it fell asleep, while its replicas still stop: it waits, the wake is
    asked again once they have stopped, and it is answered."""
    d = dep_id(gw.owner, "chat")
    gw.owner.patch(f"/api/deployments/{d}", json={"params": ON})
    assert sleep.sweep_once(later(31)) == ["chat"]
    assert {r.state for r in rows("chat")} == {"stopping"}
    t, out = background(lambda: chat(gw.api))
    in_line()
    time.sleep(0.2)
    assert workload("chat").desired_state == "asleep"
    for i in (0, 1):
        set_replica("chat", i, state="stopped")
    wake_up("chat", gw_targets(gw))
    t.join(10)
    assert out["r"].status_code == 200 and len(woken_events("chat")) == 1


# ------------------------------------------------------------------ the line
def _group(dep: int = 1) -> LocalGroup:
    return LocalGroup(target_id=1, deployment_id=dep, name="chat", upstream_model="chat", asleep=True, wake_wait=30)


async def _never():
    await asyncio.Event().wait()


def test_fy_wake_005_the_line_keeps_arrival_order_and_only_the_first_looks(monkeypatch):
    """FY-WAKE-005: five requests wait in one wake line; only the first in line looks how the wake goes, and once
    a replica is ready they leave in the order they came (each looking in turn)."""
    import contextvars

    who: contextvars.ContextVar = contextvars.ContextVar("who")
    state = {"ready": False}
    lookers: list[int] = []

    async def look(served, dep_ids):
        lookers.append(who.get())
        if state["ready"]:
            ready = LocalGroup(target_id=1, deployment_id=1, name="chat", upstream_model="chat", replicas=[object()])
            return waking.Look("ready", {1: ready})
        return waking.Look("waiting", {1: _group()})

    async def ask(groups, by):
        asked.append(who.get())

    asked: list[int] = []
    monkeypatch.setattr(waking, "look", look)
    monkeypatch.setattr(waking, "ask", ask)   # (still asleep: the first in line asks again; nothing to wake here)

    async def main():
        order: list[int] = []

        async def one(i):
            who.set(i)
            g = _group()
            out = await waking.until_awake(_never, "chat#0", "chat", [g], [g], "a request")
            assert out[0].replicas and out[0] is not g
            order.append(i)

        tasks = []
        for i in range(5):
            tasks.append(asyncio.ensure_future(one(i)))
            await asyncio.sleep(0.02)
        await asyncio.sleep(1.2)
        assert len(waking.line("chat#0")) == 5
        assert lookers and set(lookers) == {0} and set(asked) == {0}, (lookers, asked)
        lookers.clear()
        state["ready"] = True
        waking.changed()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert lookers == [0, 1, 2, 3, 4], lookers
        return order

    assert asyncio.run(main()) == [0, 1, 2, 3, 4]
    assert len(waking.line("chat#0")) == 0


def test_fy_wake_006_a_client_leaving_the_line_is_out_of_it(gw):  # noqa: F811
    """FY-WAKE-006: a request whose client goes away while it waits leaves the line at once (ClientGone), the
    others keep their places, and the wake carries on."""
    async def main():
        gone = asyncio.Event()

        async def receive():
            await gone.wait()
            return {"type": "http.disconnect"}

        g = _group(dep_id(gw.owner, "chat"))
        stays = asyncio.ensure_future(waking.until_awake(_never, "chat#0", "chat", [g], [g], "x"))
        await asyncio.sleep(0.05)
        leaves = asyncio.ensure_future(waking.until_awake(receive, "chat#0", "chat", [g], [g], "x"))
        await asyncio.sleep(0.1)
        assert len(waking.line("chat#0")) == 2
        gone.set()
        with pytest.raises(ClientGone):
            await asyncio.wait_for(leaves, 5)
        assert len(waking.line("chat#0")) == 1
        stays.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stays
        assert len(waking.line("chat#0")) == 0

    d = nap(gw.owner)
    assert waking._wake(d, "a request") == "woken"
    asyncio.run(main())
    assert workload("chat").desired_state == "running"   # the wake carries on


def test_fy_wake_007_the_line_is_bounded(gw, monkeypatch):  # noqa: F811
    """FY-WAKE-007: at most MAX_WAITERS wait per line; one more gets 503 model_waking at once, with Retry-After."""
    monkeypatch.setattr(waking, "MAX_WAITERS", 2)
    nap(gw.owner)
    threads = [background(lambda: chat(gw.api)) for _ in range(2)]
    in_line(n=2)
    started = time.monotonic()
    r = chat(gw.api)
    assert time.monotonic() - started < 3
    assert r.status_code == 503 and err(r)["code"] == "model_waking" and "2 requests already wait" in err(r)["message"]
    assert int(r.headers["retry-after"]) >= waking.RETRY_AFTER_MIN
    wake_up("chat", gw_targets(gw))
    for t, o in threads:
        t.join(10)
        assert o["r"].status_code == 200


# ------------------------------------------------------------------ spill while waking
def test_fy_wake_008_a_key_that_may_spill_uses_the_cloud_meanwhile(mixed):  # noqa: F811
    """FY-WAKE-008: a fallback key on a route with a cloud tier: the request wakes the model and goes to the
    cloud at once (X-Forgeyard-Spilled: waking, counted as a spill); once a replica is ready, local again. A key
    without the cloud waits for the wake instead."""
    nap(mixed.owner, replicas=1)
    r = mixed.api.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.headers["x-forgeyard-spilled"] == "waking", r.text
    assert r.headers["x-forgeyard-target"].startswith("provider:"), r.headers
    assert workload("chat").desired_state == "running" and len(woken_events("chat")) == 1
    assert 'forgeyard_gateway_spills_total{model="chat",reason="waking"}' in metrics.render()[0].decode()
    local = api_client(mixed.owner, new_key(mixed.owner, "local-only")["key"])
    t, out = background(lambda: local.post("/v1/chat/completions",
                                           json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]}))
    in_line()
    wake_up("chat", [(mixed.node, mixed.engine.port, mixed.engine.secret)])
    t.join(10)
    assert out["r"].status_code == 200 and out["r"].headers["x-forgeyard-target"] == "local:chat#0"
    r = mixed.api.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:chat#0"
    assert "x-forgeyard-spilled" not in r.headers
    assert len(mixed.cloud.posts()) == 1 and len(woken_events("chat")) == 1


def test_fy_wake_009_structured_output_is_not_spilled_while_it_wakes(mixed):  # noqa: F811
    """FY-WAKE-009: a request with guided_json, which the provider would drop, waits for the wake instead of
    spilling; a JSON schema the provider keeps spills."""
    nap(mixed.owner, replicas=1)
    t, out = background(lambda: mixed.api.post("/v1/chat/completions", json={
        "model": "chat", "messages": [{"role": "user", "content": "hi"}], "guided_json": {"type": "object"}}))
    in_line()
    assert mixed.cloud.posts() == []
    r = mixed.api.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "hi"}],
                                                     "response_format": SCHEMA})
    assert r.status_code == 200 and r.headers["x-forgeyard-spilled"] == "waking"
    wake_up("chat", [(mixed.node, mixed.engine.port, mixed.engine.secret)])
    t.join(10)
    r = out["r"]
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:chat#0"
    assert mixed.engine.posts()[-1]["body"]["guided_json"] == {"type": "object"}
    assert len(mixed.cloud.posts()) == 1


def test_fy_wake_010_when_the_cloud_fails_it_waits_for_the_wake(mixed):  # noqa: F811
    """FY-WAKE-010: the spill target answers 500: the request does not get that error but waits for the local
    model after all, and is answered by it."""
    mixed.cloud.status = 500
    nap(mixed.owner, replicas=1)
    t, out = background(lambda: mixed.api.post("/v1/chat/completions", json={
        "model": "chat", "messages": [{"role": "user", "content": "hi"}]}))
    in_line()
    assert len(mixed.cloud.posts()) >= 1
    wake_up("chat", [(mixed.node, mixed.engine.port, mixed.engine.secret)])
    t.join(10)
    r = out["r"]
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:chat#0", r.text
    assert "x-forgeyard-spilled" not in r.headers


# ------------------------------------------------------------------ giving up
@pytest.fixture()
def cpus(owner):
    seed_catalog()
    a = make_node("cpu-a", features=MODELS)
    b = make_node("cpu-b", features=MODELS)
    cpu_row(a, vram_free=100 * GIB)
    cpu_row(b, vram_free=20 * GIB)
    return owner, a, b


def sleepy(c, name="chat", **body) -> int:
    d = deploy(c, name, **{**CPU_BODY, "params": {**CPU_BODY["params"], **ON}}, **body)["deployment"]
    for r in rows(name):
        set_replica(name, r.ordinal, state="healthy", ready_at=utcnow() - timedelta(minutes=5))
    assert sleep.sweep_once(later(31)) == [name]
    for r in rows(name):
        set_replica(name, r.ordinal, state="stopped")
    return d["id"]


def _api(c):
    return api_client(c, new_key(c, "laptop")["key"])


def test_fy_wake_011_cannot_wake_no_room(cpus):
    """FY-WAKE-011: while it slept its memory was taken: no machine has room, so the request gets 503 cannot_wake
    at once with the scheduler's reason (it stays woken: it starts when room appears)."""
    c, a, b = cpus
    sleepy(c)
    for n in (a, b):
        cpu_row(n, vram_free=0)
    started = time.monotonic()
    r = chat(_api(c))
    assert time.monotonic() - started < 5
    assert r.status_code == 503 and err(r)["code"] == "cannot_wake", r.text
    assert "Cannot be placed" in err(r)["message"] and "cannot wake up now" in err(r)["message"]
    assert "retry-after" not in r.headers
    r = chat(_api(c))   # the next one too, at once (it is being woken, the reason stands)
    assert r.status_code == 503 and err(r)["code"] == "cannot_wake"
    assert len(woken_events("chat")) == 1


def test_fy_wake_012_cannot_wake_its_pinned_machine_is_offline(cpus):
    """FY-WAKE-012: a deployment pinned to a machine that is offline: 503 cannot_wake at once, naming it."""
    c, a, b = cpus
    sleepy(c, machine_id=b)
    db = new_session()
    try:
        node = db.get(Node, b)
        node.status = "offline"
        node.last_heartbeat_at = utcnow()
        db.commit()
    finally:
        db.close()
    started = time.monotonic()
    r = chat(_api(c))
    assert time.monotonic() - started < 5
    assert r.status_code == 503 and err(r)["code"] == "cannot_wake", r.text
    assert "pinned to cpu-b, which is offline" in err(r)["message"]


def test_fy_wake_013_cannot_wake_its_engine_is_failing(cpus):
    """FY-WAKE-013: the woken replica's engine does not come up (backoff after an exit): the waiting request
    stops at once with 503 cannot_wake and what the agent said."""
    c, a, b = cpus
    sleepy(c)
    t, out = background(lambda: chat(_api(c)))
    in_line()
    set_replica("chat", 0, state="backoff", reason="exited with code 1; restarting in 4 s")
    waking.changed()
    t.join(10)
    r = out["r"]
    assert r.status_code == 503 and err(r)["code"] == "cannot_wake", r.text
    assert "replica 0 on cpu-" in err(r)["message"] and "backoff" in err(r)["message"]


def test_fy_wake_014_the_wait_runs_out_model_waking_with_retry_after(gw, monkeypatch):  # noqa: F811
    """FY-WAKE-014: still starting after its wake_wait: 503 model_waking with Retry-After from its last start
    time minus what passed (START_DEFAULT when none was measured; at least RETRY_AFTER_MIN)."""
    monkeypatch.setattr(sleep, "WAKE_WAIT_MIN", 1)
    nap(gw.owner)
    set_params("chat", wake_wait=1)
    started = time.monotonic()
    r = chat(gw.api)
    took = time.monotonic() - started
    assert r.status_code == 503 and err(r)["code"] == "model_waking", r.text
    assert 1 <= took < 5 and "not ready within 1 s" in err(r)["message"]
    after = int(r.headers["retry-after"])
    assert waking.START_DEFAULT - 5 <= after <= waking.START_DEFAULT
    wk = waking.wake_of(dep_id(gw.owner, "chat"))
    wk.expected, wk.t0 = 120, time.monotonic() - 100
    assert waking.retry_after([wk.dep_id]) == 20
    wk.t0 = time.monotonic() - 500
    assert waking.retry_after([wk.dep_id]) == waking.RETRY_AFTER_MIN


# ------------------------------------------------------------------ what does not wake it
def test_fy_wake_015_a_persons_stop_stays_stopped(gw):  # noqa: F811
    """FY-WAKE-015: stopped by a person (also an asleep one) it is never woken by a request: 503 no_ready_replica
    "stopped" at once; a Stop while requests wait for the wake ends their wait with the same."""
    d = nap(gw.owner)
    assert gw.owner.post(f"/api/deployments/{d}/stop").status_code == 200
    gen = workload("chat").generation
    r = chat(gw.api)
    assert r.status_code == 503 and err(r)["code"] == "no_ready_replica" and "stopped" in err(r)["message"]
    assert workload("chat").desired_state == "stopped" and workload("chat").generation == gen
    # Also when a request read the route while it was still asleep and the Stop came before its wake: no wake.
    assert waking._wake(d, "a request (key laptop)") == "stopped"
    assert workload("chat").desired_state == "stopped" and workload("chat").generation == gen
    for i in (0, 1):
        set_replica("chat", i, state="stopped")
    assert gw.owner.post(f"/api/deployments/{d}/start").status_code == 200
    make_ready("chat", gw_targets(gw))
    nap(gw.owner)
    t, out = background(lambda: chat(gw.api))
    in_line()
    assert gw.owner.post(f"/api/deployments/{d}/stop").status_code == 200
    waking.changed()
    t.join(10)
    assert out["r"].status_code == 503 and err(out["r"])["code"] == "no_ready_replica"
    assert "stopped" in err(out["r"])["message"]


def test_fy_wake_016_models_list_health_and_pages_never_wake_it(gw):  # noqa: F811
    """FY-WAKE-016: /v1/models, GET /v1/models/chat, /health, the deployment list and page leave it asleep."""
    d = nap(gw.owner)
    gw.api.get("/v1/models")
    gw.api.get("/v1/models/chat")
    gw.api.get("/health")
    gw.owner.get("/api/deployments")
    gw.owner.get(f"/api/deployments/{d}")
    assert workload("chat").desired_state == "asleep" and woken_events("chat") == []
    assert waking.wake_of(d) is None


def test_fy_wake_017_wake_now_requests_meanwhile_wait(gw):  # noqa: F811
    """FY-WAKE-017: Start on an asleep deployment (Wake now) is a person's wake: a request arriving while it
    starts waits for it instead of 503 no_ready_replica; no second wake."""
    d = nap(gw.owner)
    assert gw.owner.post(f"/api/deployments/{d}/start").status_code == 200
    gen = workload("chat").generation
    assert gw.owner.get(f"/api/deployments/{d}").json()["deployment"]["sleep"]["waking"]["by"] == "owner"
    t, out = background(lambda: chat(gw.api))
    in_line()
    wake_up("chat", gw_targets(gw))
    t.join(10)
    assert out["r"].status_code == 200 and workload("chat").generation == gen
    assert 'forgeyard_deployment_wakes_total{by="person",deployment="chat"}' in metrics.render()[0].decode()


# ------------------------------------------------------------------ limits, streams, use
def test_fy_wake_018_rate_limits_count_a_held_request_once(gw):  # noqa: F811
    """FY-WAKE-018: a key with 1 request per minute: the request that waited for the wake is admitted once and
    answered; the next one is 429."""
    nap(gw.owner)
    one = api_client(gw.owner, new_key(gw.owner, "one-a-minute", rpm=1)["key"])
    t, out = background(lambda: chat(one))
    in_line()
    wake_up("chat", gw_targets(gw))
    t.join(10)
    assert out["r"].status_code == 200
    r = chat(one)
    assert r.status_code == 429 and err(r)["code"] == "rate_limit_exceeded"


def test_fy_wake_019_a_stream_starts_only_when_the_model_answers(gw):  # noqa: F811
    """FY-WAKE-019: a streamed request during the wake gets nothing until a replica answers; then a normal 200
    stream to its end."""
    nap(gw.owner)
    t, out = background(lambda: chat(gw.api, stream=True))
    in_line()
    time.sleep(0.3)
    assert "r" not in out
    wake_up("chat", gw_targets(gw))
    t.join(10)
    r = out["r"]
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "[DONE]" in r.text


def test_fy_wake_020_what_counts_as_use(gw):  # noqa: F811
    """FY-WAKE-020: a request that was served or woke the model is use; one the gateway refused at once (its
    deployment stopped by a person) is not: its start is taken back."""
    d = nap(gw.owner)
    idle.reset()
    t, out = background(lambda: chat(gw.api))
    in_line()
    wake_up("chat", gw_targets(gw))
    t.join(10)
    assert out["r"].status_code == 200 and idle.last(d) is not None
    idle.reset()
    gw.owner.post(f"/api/deployments/{d}/stop")
    assert chat(gw.api).status_code == 503
    assert idle.last(d) is None and idle.active(d) == 0
    seen = utcnow() - timedelta(minutes=3)
    idle.used([d], at=seen)
    assert chat(gw.api).status_code == 503
    assert idle.last(d) == seen


def test_fy_wake_021_a_new_deployment_with_a_removed_ones_id_starts_from_nothing(cpus):
    """FY-WAKE-021: a deployment removed and made again gets the same id here (SQLite reuses the highest one): its
    last request and any wake are forgotten with the old one, so the new one's idle time starts fresh."""
    c, a, b = cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]["id"]
    idle.used([d], at=later(-50))
    waking._wakes[d] = waking.Wake(d, "chat", "a request")
    assert c.delete(f"/api/deployments/{d}").status_code == 200
    assert idle.last(d) is None and waking.wake_of(d) is None
    idle.used([d], at=later(-50))   # (a request still finishing for the removed one)
    again = deploy(c, "chat", **CPU_BODY)["deployment"]
    assert again["id"] == d
    assert idle.last(d) is None and again["sleep"]["last_request_at"] is None


# ------------------------------------------------------------------ settings, video
def test_fy_wake_022_wake_wait_is_checked_and_restarts_nothing(gw):  # noqa: F811
    """FY-WAKE-022: wake_wait, 10 to 1,800 whole seconds, 300 when unset; changing it restarts nothing."""
    d = dep_id(gw.owner, "chat")
    path = f"/api/deployments/{d}"
    r = gw.owner.patch(path, json={"params": {"wake_wait": 120}}).json()
    assert r["restart"] == "none" and r["changes"] == {"wake_wait": {"before": None, "after": 120}}
    assert r["deployment"]["sleep"]["wake_wait"] == 120
    for bad, text in [(9, "10 to 1,800"), (1801, "10 to 1,800"), (2.5, "whole number"), ("soon", "whole number"),
                      (True, "whole number")]:
        r = gw.owner.patch(path, json={"params": {"wake_wait": bad}})
        assert r.status_code == 422 and text in r.json()["detail"]["message"], (bad, r.text)
    assert sleep.wake_wait_of({}) == 300 and sleep.wake_wait_of({"wake_wait": 5}) == 300


def test_fy_wake_023_a_video_asked_of_an_asleep_model_waits_for_the_wake(vid):  # noqa: F811
    """FY-WAKE-023: a video deployment asleep: POST /v1/videos wakes it, waits, and once a replica is ready the job
    is created there."""
    dep = vid.owner.get("/api/deployments").json()["deployments"][0]
    nap(vid.owner, name="wan")
    t, out = background(lambda: make(vid.api))
    in_line(key="wan#video")
    assert all(not e.posts for e in vid.engines)
    wake_up("wan", [(vid.gpu, e.port, e.secret) for e in vid.engines])
    t.join(15)
    r = out["r"]
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("queued", "in_progress") and len(woken_events("wan")) == 1
    assert dep["name"] == "wan"


def test_fy_wake_024_a_line_does_not_mix_with_the_fair_line():
    """FY-WAKE-024: the wake lines are their own (the fair line's /metrics gauge does not count them)."""
    ln = waking.line("x#0")

    async def main():
        w = ln.join()
        assert waking.lengths() == {"x#0": 1} and "x#0" not in waiting.lengths()
        ln.leave(w)

    asyncio.run(main())
    assert isinstance(ln, waiting.Line) and waking.lengths() == {"x#0": 0}


def test_fy_wake_025_without_lines_a_status_costs_nothing():
    """FY-WAKE-025: changed() with no request waiting returns at once, and forgets the wakes of workloads that
    became ready."""
    waking._wakes[7] = waking.Wake(7, "x", "a request", woken=True, workload_id=70)
    waking.changed([70])
    assert waking.wake_of(7) is None
    waking.changed()
    assert waking.lengths() == {}
