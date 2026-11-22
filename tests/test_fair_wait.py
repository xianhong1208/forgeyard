"""Fair waiting for a busy local tier (1.4.5, #169): text requests that find every replica at its
*Concurrent requests* wait in line, first come first served, woken when a slot is freed (no polling),
bounded by the route's wait and the line's length; a client that goes away leaves the line; local_busy
carries Retry-After; spilling on busy still works (FY-FW-*)."""

import asyncio
import threading
import time

import httpx

from forgeyard.gateway import proxy, waiting
from forgeyard.gateway.balancer import Target, balancer
from tests.cloud_helpers import set_tiers
from tests.gateway_helpers import LiveServer
from tests.test_routing import background, chat, in_flight_soon, mixed  # noqa: F401  (the fixture)
from tests.waits import wait_until


def line_len() -> int:
    return len(waiting.line("chat#0"))


def test_fy_fw_001_the_line_in_order_and_who_may_take_a_slot():
    """FY-FW-001: the line keeps the order requests came in; a request not in line may take a slot only
    while nobody waits, one in line only when it is first; leaving wakes the next first; a full line
    takes nobody more."""
    async def scenario():
        ln = waiting.Line()
        assert ln.may_try(None)
        a, b, c = ln.join(), ln.join(), ln.join()
        assert not ln.may_try(None) and ln.may_try(a) and not ln.may_try(b)
        ln.leave(a)
        await asyncio.sleep(0)
        assert b.event.is_set() and not c.event.is_set() and ln.may_try(b)
        ln.leave(c)                      # not first: nobody is woken
        await asyncio.sleep(0)
        assert ln.may_try(b) and len(ln) == 1
        assert ln.join(limit=1) is None and len(ln) == 1
        ln.leave(b)
        assert ln.may_try(None)
        d = ln.join()
        started = time.monotonic()
        await ln.wait(d, 5.0)            # nobody wakes it: it looks again after RECHECK
        assert waiting.RECHECK - 0.05 <= time.monotonic() - started < waiting.RECHECK + 0.5
        threading.Timer(0.1, ln.wake_first).start()   # a wake-up from another thread, while it waits
        started = time.monotonic()
        await ln.wait(d, 5.0)
        assert time.monotonic() - started < 0.3
    asyncio.run(scenario())


def test_fy_fw_002_a_released_slot_wakes_the_first_in_line():
    """FY-FW-002: the balancer tells the lines when a slot is released."""
    async def scenario():
        ln = waiting.line("fw-002#0")
        w = ln.join()
        balancer.acquire("fw-002-r")
        balancer.release("fw-002-r")
        await asyncio.sleep(0.05)
        assert w.event.is_set()
        ln.leave(w)
    asyncio.run(scenario())


def test_fy_fw_003_waiting_requests_are_served_first_come_first_served(mixed):  # noqa: F811
    """FY-FW-003: concurrency 1, one request in flight, then six more that each join the line in turn:
    the engine gets them in the order they came, none spills (the wait is long enough), and the line
    is empty afterwards."""
    set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", mixed.pm)]], wait_ms=10_000)
    mixed.engine.gate = threading.Event()
    threads = []
    try:
        threads.append(background(lambda: chat(mixed.api, messages=[{"role": "user", "content": "r0"}])))
        in_flight_soon()
        for i in range(1, 7):
            threads.append(background(lambda i=i: chat(mixed.api, messages=[{"role": "user", "content": f"r{i}"}])))
            wait_until(lambda i=i: line_len() == i, f"request {i} in line")
        mixed.engine.gate.set()
    finally:
        mixed.engine.gate.set()
    for t, _ in threads:
        t.join()
    order = [p["body"]["messages"][0]["content"] for p in mixed.engine.posts()]
    assert order == [f"r{i}" for i in range(7)]
    assert all(out["r"].status_code == 200 and out["r"].headers["x-forgeyard-target"] == "local:chat#0"
               for _, out in threads)
    assert mixed.cloud.posts() == [] and line_len() == 0


def test_fy_fw_004_a_full_line_or_the_end_of_the_wait_still_spills(mixed, monkeypatch):  # noqa: F811
    """FY-FW-004: busy local with a cloud tier still spills before the first byte: at once when the line
    is full, after the route's wait otherwise; the request that gave up leaves the line."""
    monkeypatch.setattr(waiting, "MAX_WAITERS", 1)
    set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", mixed.pm)]], wait_ms=10_000)
    mixed.engine.gate = threading.Event()
    try:
        t0, _ = background(lambda: chat(mixed.api))
        in_flight_soon()
        t1, waiter = background(lambda: chat(mixed.api))
        wait_until(lambda: line_len() == 1, "one request in line")
        started = time.monotonic()
        r = chat(mixed.api)                      # the line is full: no waiting
        assert time.monotonic() - started < 2.0
        assert r.status_code == 200 and r.headers["x-forgeyard-spilled"] == "busy"
    finally:
        mixed.engine.gate.set()
    t0.join()
    t1.join()
    assert waiter["r"].headers["x-forgeyard-target"] == "local:chat#0"
    monkeypatch.setattr(waiting, "MAX_WAITERS", 256)
    set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", mixed.pm)]], wait_ms=400)
    mixed.engine.gate = threading.Event()
    try:
        t0, _ = background(lambda: chat(mixed.api))
        in_flight_soon()
        started = time.monotonic()
        r = chat(mixed.api)
        waited = time.monotonic() - started
    finally:
        mixed.engine.gate.set()
    t0.join()
    assert r.headers["x-forgeyard-spilled"] == "busy" and 0.35 <= waited < 2.0 and line_len() == 0


def test_fy_fw_005_a_client_that_goes_away_leaves_the_line(mixed):  # noqa: F811
    """FY-FW-005: on a real server, a waiting client that hangs up leaves the line at once, and the
    requests behind it keep their order and are served."""
    set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", mixed.pm)]], wait_ms=10_000)
    mixed.engine.gate = threading.Event()
    live = LiveServer()
    try:
        auth = {"Authorization": f"Bearer {mixed.key}"}

        def post(text, timeout=30.0):
            with httpx.Client(base_url=live.url, headers=auth, timeout=timeout, trust_env=False) as c:
                return c.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": text}]})

        first, _ = background(lambda: post("r0"))
        in_flight_soon()
        gave_up = {}

        def impatient():
            try:
                post("gone", 1.0)
            except httpx.ReadTimeout:
                gave_up["ok"] = True
        gone = threading.Thread(target=impatient)
        gone.start()
        wait_until(lambda: line_len() == 1, "the impatient request in line")
        last, out = background(lambda: post("r2"))
        wait_until(lambda: line_len() == 2, "the next request in line")
        gone.join()
        assert gave_up == {"ok": True}
        wait_until(lambda: line_len() == 1, "the request that went away left the line")
        mixed.engine.gate.set()
        first.join()
        last.join()
        assert out["r"].status_code == 200
        assert [p["body"]["messages"][0]["content"] for p in mixed.engine.posts()] == ["r0", "r2"]
        assert line_len() == 0 and mixed.cloud.posts() == []
    finally:
        mixed.engine.gate.set()
        live.close()


def test_fy_fw_006_local_busy_carries_retry_after():
    """FY-FW-006: a busy tier with somewhere to move on to fails as local_busy with Retry-After (a small
    constant), and never queues when its route does not wait."""
    from starlette.requests import Request

    from forgeyard.gateway.keys import KeyCaller

    async def scenario():
        never = asyncio.get_running_loop().create_future()

        async def receive():
            return await never

        request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}, receive)
        t = Target(uid="fw-006-r", ordinal=0, url="http://127.0.0.1:9", machine=None, secret=None, protected=False)
        g = proxy.LocalGroup(target_id=1, deployment_id=1, name="busy", upstream_model="m", replicas=[t], concurrency=1)
        route = proxy.Route(served_name="busy", api="openai-chat", tiers=[proxy.Tier(0, [g], [])], wait_ms=0)
        caller = KeyCaller.__new__(KeyCaller)
        job = proxy.Job(request, caller, route, "chat/completions", proxy.Parsed(model="busy", obj={"model": "busy"}),
                        time.monotonic(), None)
        balancer.acquire(t.uid)
        try:
            out = await job.local(route.tiers[0], can_move_on=True)
        finally:
            balancer.release(t.uid)
        return out
    out = asyncio.run(scenario())
    assert out.reason == "busy" and out.error.code == "local_busy" and out.error.status == 503
    assert out.error.retry_after == proxy.LOCAL_BUSY_RETRY_AFTER == 1
    assert out.error.response().headers["retry-after"] == "1" and len(waiting.line("busy#0")) == 0


def test_fy_fw_007_nobody_takes_a_free_slot_while_others_wait():
    """FY-FW-007: a slot is free, but a request is already waiting in line: a newcomer does not take the
    slot (it joins the line behind it); when its wait is over it is busy, and the slot was never used."""
    from starlette.requests import Request

    from forgeyard.gateway.keys import KeyCaller

    async def scenario():
        never = asyncio.get_running_loop().create_future()

        async def receive():
            return await never

        request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}, receive)
        t = Target(uid="fw-007-r", ordinal=0, url="http://127.0.0.1:9", machine=None, secret=None, protected=False)
        g = proxy.LocalGroup(target_id=1, deployment_id=1, name="fair", upstream_model="m", replicas=[t], concurrency=1)
        route = proxy.Route(served_name="fair", api="openai-chat", tiers=[proxy.Tier(0, [g], [])], wait_ms=300)
        job = proxy.Job(request, KeyCaller.__new__(KeyCaller), route, "chat/completions",
                        proxy.Parsed(model="fair", obj={"model": "fair"}), time.monotonic(), None)
        ln = waiting.line("fair#0")
        ahead = ln.join()
        try:
            started = time.monotonic()
            out = await job.local(route.tiers[0], can_move_on=True)
            return out, time.monotonic() - started, balancer.snapshot(t.uid), len(ln)
        finally:
            ln.leave(ahead)
    out, waited, seen, left = asyncio.run(scenario())
    assert out.reason == "busy" and 0.25 <= waited < 2.0
    assert seen == {"in_flight": 0, "served": 0} and left == 1     # only the one ahead is still in line


def test_fy_fw_008_a_waiter_that_gets_a_slot_leaves_the_line_at_once(mixed, monkeypatch):  # noqa: F811
    """FY-FW-008: concurrency 2, both slots busy and two requests in line; both slots free up: the first
    in line takes one and leaves the line at once, so the second takes the other while the first is
    still in flight, instead of waiting for it to finish. Both are woken: the line's safety-net recheck
    and the route's wait are made longer than the test, so neither can stand in for a wake-up."""
    from sqlalchemy import select

    from forgeyard.db import GatewayRoute, new_session
    from tests.gateway_helpers import make_ready

    # #212: this test compared when the engine received the two requests (the second within 0.5 s of the
    # first, and not before it). The gateway sends them together, so on a busy machine either can arrive
    # first, and the two slots need not free up within 0.5 s. Now the engine keeps both until the test lets
    # them go: the second reaching the engine while the first is still held is the proof, at any speed.
    # Longer than the 10 s the test waits for them (and short enough that, if they are never woken, the
    # requests still finish before the test ends rather than outliving it).
    monkeypatch.setattr(waiting, "RECHECK", 20.0)
    monkeypatch.setattr(proxy, "MAX_WAIT_MS", 20_000)
    r = mixed.owner.patch(f"/api/deployments/{mixed.dep}", json={"params": {"concurrency": 2}})
    assert r.status_code == 200, r.text
    make_ready("chat", [(mixed.node, mixed.engine.port, mixed.engine.secret)])
    set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", mixed.pm)]], wait_ms=10_000)
    db = new_session()
    try:    # more than the API allows
        db.scalar(select(GatewayRoute).where(GatewayRoute.served_name == "chat")).wait_ms = 20_000
        db.commit()
    finally:
        db.close()
    first, second = threading.Event(), threading.Event()
    mixed.engine.gate = first
    threads = []
    try:
        for i in range(2):
            threads.append(background(lambda i=i: chat(mixed.api, messages=[{"role": "user", "content": f"r{i}"}])))
        in_flight_soon(2)
        wait_until(lambda: len(mixed.engine.held) == 2, "r0 and r1 held at the engine")
        for i in range(2, 4):
            threads.append(background(lambda i=i: chat(mixed.api, messages=[{"role": "user", "content": f"r{i}"}])))
            wait_until(lambda i=i: line_len() == i - 1, f"request {i} in line")
        mixed.engine.gate = second      # what reaches the engine from now on waits for `second`
        first.set()                     # r0 and r1 answer: both slots free up
        wait_until(lambda: len(mixed.engine.held) == 4, "r2 and r3 held at the engine, r2 still in flight")
        assert line_len() == 0
    finally:
        first.set()
        second.set()
        for t, _ in threads:
            t.join()
    assert sorted(p["body"]["messages"][0]["content"] for p in mixed.engine.posts()) == ["r0", "r1", "r2", "r3"]
    assert all(out["r"].status_code == 200 and out["r"].headers["x-forgeyard-target"] == "local:chat#0"
               for _, out in threads)
    assert mixed.cloud.posts() == []
