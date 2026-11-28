"""Replicas move off a machine that stays offline (1.4.5, #164; FY-MOVE-*): after AGENT_OFFLINE_MOVE_SECONDS (10
minutes) the replicas of unpinned deployments are placed again on a machine that fits, by the deployment's own
rules; pinned ones wait; a machine that comes back in time keeps everything; the machine, back, stops what it no
longer should run; and routing skips an offline machine's replicas at once."""

import asyncio
from datetime import timedelta

import pytest

from forgeyard.config import get_settings
from forgeyard.db import Node, Replica, new_session, utcnow
from forgeyard.gateway.balancer import balancer
from forgeyard.machines import liveness
from forgeyard.machines.hub import hub
from forgeyard.workloads import offline, placement, state
from tests.gateway_helpers import FakeEngine, api_client, make_ready, new_key
from tests.test_placement_fetch import cpu, events, rows, set_node
from tests.test_scheduler import MODELS, deploy, reps, seed_catalog
from tests.workload_helpers import create_workload, make_node, service, task

SMALL = {"gpus": 0}
MIN = timedelta(minutes=1)


@pytest.fixture()
def poked(monkeypatch):
    seen = []
    monkeypatch.setattr(hub, "poke", lambda node_id: seen.append(node_id) or False)
    return seen


@pytest.fixture(autouse=True)
def no_background_sweep(monkeypatch):
    """These tests call move_off() themselves and check what it returns. The app's own liveness loop (every 2 s) also
    calls it, and could move a replica between a test's go_offline() and its sweep(), leaving nothing for the test's
    call (seen once in a loaded full run, 2026-11-27). FY-MOVE-010 runs liveness._sweep_once() itself."""
    async def idle(interval: float = 0.0):
        while True:
            await asyncio.sleep(3600)

    monkeypatch.setattr(liveness, "run_forever", idle)


@pytest.fixture(autouse=True)
def started_again():
    yield
    offline.server_started()


def go_offline(node_id, ago: timedelta):
    """The machine went silent `ago` ago (and the liveness sweep marked it offline), on a server that started
    long before (the app's startup in the test set it to now: FY-MOVE-002 is about that case)."""
    offline.server_started(utcnow() - timedelta(days=3))
    set_node(node_id, status="offline", last_heartbeat_at=utcnow() - ago)


def sweep():
    db = new_session()
    try:
        return offline.move_off(db)
    finally:
        db.close()


def place():
    db = new_session()
    try:
        return placement.place_and_poke(db)
    finally:
        db.close()


def machine_of(name, ordinal=0):
    r = rows(name)[ordinal]
    return r.node_id


def test_fy_move_001_an_unpinned_replica_moves_after_ten_minutes(owner, poked):
    """FY-MOVE-001: 9 minutes offline: nothing moves (the gateway already skips it). 10 minutes: the replica is
    taken off the machine (one row, a new id, an event "moved: cpu-a offline"), placed on the other machine by the
    deployment's rules, and that machine's agent is told; cpu-a holds nothing any more."""
    seed_catalog()
    a = cpu("cpu-a")
    d = deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params=SMALL)
    assert machine_of("small") == a
    old = rows("small")[0]
    b = cpu("cpu-b")
    go_offline(a, 9 * MIN)
    assert sweep() == [] and machine_of("small") == a
    go_offline(a, 10 * MIN)
    assert sweep() == ["small"]
    r = rows("small")[0]
    assert r.id == old.id and r.uid != old.uid and r.node_id is None and r.reason == "moved: cpu-a offline; being placed again"
    assert b in place() and b in poked
    after = rows("small")
    assert len(after) == 1 and after[0].node_id == b and after[0].state == "pending" and after[0].secret != old.secret
    msgs = events(owner, d)
    assert any(m.startswith("Replica 0: moved: cpu-a offline for 10 minutes; placing it on another machine (cpu-b fits)")
               for m in msgs), msgs
    assert any(m.startswith("Replica 0 placed on cpu-b") for m in msgs), msgs
    db = new_session()
    try:
        assert placement.holding(db, a) == [] and state.desired_replicas(db, a) == []
        assert [x.uid for x in state.desired_replicas(db, b)] == [after[0].uid]
    finally:
        db.close()
    assert reps(owner, d)[0]["machine"]["name"] == "cpu-b"


def test_fy_move_002_a_machine_back_within_the_grace_keeps_its_replicas(owner, poked):
    """FY-MOVE-002: flapping: offline 8 minutes, back (a heartbeat), offline again for 5: nothing moves (the time
    counts from the last heartbeat). And after a server restart every machine shows offline with an old heartbeat:
    the grace counts from the server's start, so nothing moves before agents had the whole time to reconnect."""
    seed_catalog()
    a = cpu("cpu-a")
    deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params=SMALL)
    cpu("cpu-b")
    go_offline(a, 8 * MIN)
    assert sweep() == []
    set_node(a, status="online", last_heartbeat_at=utcnow())
    assert sweep() == []
    go_offline(a, 5 * MIN)
    assert sweep() == [] and machine_of("small") == a
    # A restarted server: the heartbeat is an hour old, but the server started a minute ago.
    go_offline(a, 60 * MIN)
    offline.server_started(utcnow() - MIN)
    assert sweep() == [] and machine_of("small") == a
    offline.server_started(utcnow() - 10 * MIN)
    assert sweep() == ["small"]
    # The server itself restarting is what sets that start.
    from tests.conftest import make_client

    place()
    b = machine_of("small")
    assert b != a
    c = cpu("cpu-c")
    go_offline(b, 60 * MIN)
    with make_client():
        set_node(c, status="online", last_heartbeat_at=utcnow())  # its agent reconnected at once; cpu-b's did not
        assert sweep() == [] and machine_of("small") == b


def test_fy_move_003_a_pinned_deployment_waits_and_says_so_once(owner, poked):
    """FY-MOVE-003: a deployment pinned to the machine keeps its replica there: its reason and one event (not one
    per sweep) say it waits for the machine; the console still shows it there, and "not picked up" still works
    once the machine is back."""
    seed_catalog()
    a = cpu("cpu-a")
    d = deploy(owner, "pinned", "qwen3-1.7b-gguf", "llama-cpp", params=SMALL, machine_id=a)
    cpu("cpu-b")
    go_offline(a, 30 * MIN)
    for _ in range(3):
        assert sweep() == []
    r = rows("pinned")[0]
    assert r.node_id == a and r.reason == "waiting for cpu-a to come online (the deployment is pinned to it)"
    waiting = [m for m in events(owner, d) if "the deployment is pinned to it, so the replica waits for it" in m]
    assert waiting == ["Replica 0: cpu-a has been offline for 30 minutes; the deployment is pinned to it, so the "
                       "replica waits for it."]
    assert reps(owner, d)[0]["machine"]["name"] == "cpu-a"
    # Back, connected, and its agent never says a word about the replica: still told after two minutes.
    set_node(a, status="online", last_heartbeat_at=utcnow(), connected_at=utcnow() - 3 * MIN)
    db = new_session()
    try:
        row = db.get(Replica, r.id)
        row.updated_at = utcnow() - 3 * MIN
        assert state.not_picked_up(row, utcnow(), lambda _n: True) is not None
    finally:
        db.close()


def test_fy_move_004_nothing_else_fits_so_it_waits_then_moves_when_room_appears(owner, poked):
    """FY-MOVE-004: no other machine can take it: it stays on the offline machine (it may come back first), saying
    so once; a machine with room that joins later gets it on the next sweep."""
    seed_catalog()
    a = cpu("cpu-a")
    d = deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params=SMALL)
    make_node("no-models", features=("workloads",))  # its agent serves no models
    go_offline(a, 11 * MIN)
    assert sweep() == [] and sweep() == []
    r = rows("small")[0]
    assert r.node_id == a and r.reason == "waiting for cpu-a to come online (no other machine can take it)"
    assert sum(1 for m in events(owner, d) if "no other machine can take it, so it waits" in m) == 1
    b = cpu("cpu-b")
    assert sweep() == ["small"]
    place()
    assert machine_of("small") == b


def test_fy_move_005_the_deployment_s_own_rules_spread_and_fit(owner, poked):
    """FY-MOVE-005: two replicas on cpu-a and cpu-b; cpu-a goes away: its replica goes to cpu-c (spread over
    machines, as when it was placed), not next to its sibling on cpu-b. A machine without the memory is passed
    over."""
    seed_catalog()
    a, b = cpu("cpu-a"), cpu("cpu-b")
    deploy(owner, "two", "qwen3-1.7b-gguf", "llama-cpp", params=SMALL, replicas=2)
    assert sorted(r.node_id for r in rows("two")) == sorted([a, b])
    tiny = cpu("cpu-tiny")
    db = new_session()
    try:
        row = next(x for x in db.get(Node, tiny).accelerators if x.vendor == "cpu")
        row.vram_free = 1024**2  # 1 MiB of free RAM
        db.commit()
    finally:
        db.close()
    c = cpu("cpu-c")
    gone = next(r for r in rows("two") if r.node_id == a)
    go_offline(a, 15 * MIN)
    assert sweep() == ["two"]
    place()
    db = new_session()
    try:
        assert db.get(Replica, gone.id).node_id == c
    finally:
        db.close()


def test_fy_move_006_the_setting(owner, poked, monkeypatch):
    """FY-MOVE-006: AGENT_OFFLINE_MOVE_SECONDS sets the grace (60 s here); 0 never moves anything."""
    seed_catalog()
    a = cpu("cpu-a")
    deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params=SMALL)
    cpu("cpu-b")
    assert get_settings().agent_offline_move_seconds == 600
    monkeypatch.setenv("AGENT_OFFLINE_MOVE_SECONDS", "0")
    get_settings.cache_clear()
    go_offline(a, timedelta(days=2))
    assert sweep() == []
    monkeypatch.setenv("AGENT_OFFLINE_MOVE_SECONDS", "60")
    get_settings.cache_clear()
    go_offline(a, timedelta(seconds=50))
    assert sweep() == []
    go_offline(a, timedelta(seconds=61))
    assert sweep() == ["small"]


def test_fy_move_007_tasks_stay_and_services_move(owner, poked):
    """FY-MOVE-007: plain workloads follow the same rules: an unpinned service moves, a pinned one waits; a task stays (it may
    still be running there, and running it again elsewhere could do its work twice)."""
    a = make_node("box-a")
    for spec in (service("web"), service("pinned-web", machine_id=a), task("job", script="sleep 600")):
        create_workload(owner, spec)
    assert {r.node_id for name in ("web", "pinned-web", "job") for r in rows(name)} == {a}
    b = make_node("box-b")
    go_offline(a, 20 * MIN)
    assert sweep() == ["web"]
    place()
    assert machine_of("web") == b and machine_of("pinned-web") == a and machine_of("job") == a
    assert rows("pinned-web")[0].reason == "waiting for box-a to come online (the workload is pinned to it)"
    assert rows("job")[0].reason == "waiting for box-a to come online (a task is not run again elsewhere)"


def test_fy_move_008_the_machine_back_stops_the_engine_it_no_longer_runs(owner, poked, tmp_path):
    """FY-MOVE-008: the machine comes back after its replica moved: the work list it gets on connect no longer has
    the replica, and its agent (here a real supervisor that adopted the still-running process) stops it."""
    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.leaks import kill_group
    from tests.workload_helpers import alive, converge

    a = make_node("box-a")
    sleeper = service("web", runtime={"type": "process", "command": "sleep", "args": ["600"]}, ports=[])
    create_workload(owner, sleeper)
    db = new_session()
    try:
        before = state.desired_message(db, a, 1)
    finally:
        db.close()
    assert [x["workload"] for x in before["replicas"]] == ["web"]
    rt = ProcessRuntime(RecordStore(tmp_path / "state"), tmp_path / "work", euid=lambda: 4242)
    first = Supervisor(tmp_path / "state", {"process": rt}, tick=0.05, stop_grace=2.0)

    async def run_it():
        first.set_desired(before)
        await converge(first, lambda s: s.records and next(iter(s.records.values())).state == "running")
        return next(iter(first.records.values())).pid

    try:
        pid = asyncio.run(run_it())
        b = make_node("box-b")
        go_offline(a, 12 * MIN)  # (the agent died with the machine's network; its process kept running)
        assert sweep() == ["web"]
        place()
        assert machine_of("web") == b
        db = new_session()
        try:
            back = state.desired_message(db, a, 2)
            there = state.desired_message(db, b, 2)
        finally:
            db.close()
        assert back["replicas"] == [] and [x["workload"] for x in there["replicas"]] == ["web"]
        assert there["replicas"][0]["id"] != before["replicas"][0]["id"]
        second = Supervisor(tmp_path / "state", {"process": rt}, tick=0.05, stop_grace=2.0)  # the agent, restarted

        async def come_back():
            await second.adopt()
            assert alive(pid) and second.records
            second.set_desired(back)
            await converge(second, lambda s: not s.records)

        asyncio.run(come_back())
        child = rt.children.get(before["replicas"][0]["id"])
        if child is not None:
            child.wait(5)  # (reaped by the runtime when it stopped it, or here)
        assert not alive(pid)
    finally:
        for p in list(rt.children.values()):
            kill_group(p)


def test_fy_move_009_status_from_the_old_machine_no_longer_touches_the_moved_replica(owner, poked):
    """FY-MOVE-009: the machine comes back and reports the old replica (old id) as healthy before it stops it:
    the moved replica, now on another machine, is not changed by it (never two copies counted)."""
    from forgeyard.machines import agent_api
    from forgeyard.machines.messages import StatusIn

    seed_catalog()
    a = cpu("cpu-a")
    deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params=SMALL)
    old = rows("small")[0]
    b = cpu("cpu-b")
    go_offline(a, 10 * MIN)
    sweep()
    place()
    set_node(a, status="online", last_heartbeat_at=utcnow())
    db = new_session()
    try:
        msg = StatusIn.model_validate({"v": 1, "type": "status", "full": True, "replicas": [
            {"id": old.uid, "generation": old.generation, "state": "healthy", "reason": None,
             "ports": [{"name": "http", "port": 8080, "host_port": 47000, "address": "10.0.0.9"}]}]})
        agent_api._status(db, a, msg)
    finally:
        db.close()
    r = rows("small")[0]
    assert r.node_id == b and r.state == "pending" and r.ports == []


def test_fy_move_010_the_liveness_sweep_does_it(owner, poked):
    """FY-MOVE-010: no test hook: the server's own sweep (offline detection, then moving, then placing) moves it."""
    seed_catalog()
    a = cpu("cpu-a")
    deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params=SMALL)
    b = cpu("cpu-b")
    offline.server_started(utcnow() - timedelta(days=3))
    set_node(a, status="online", last_heartbeat_at=utcnow() - 11 * MIN)  # silent; not yet marked offline
    liveness._sweep_once()
    assert machine_of("small") == b and b in poked


def test_fy_move_011_routing_skips_an_offline_machine_at_once(owner, monkeypatch):
    """FY-MOVE-011: two healthy replicas, on cpu-a and cpu-b; cpu-a goes offline: every request goes to cpu-b from
    the next one on (cpu-a's engine is never dialled, no 15 s penalty is needed), and the console counts one ready
    replica. Both offline: 503, and the reason says the machine is offline."""
    seed_catalog()
    a, b = make_node("cpu-a", features=MODELS + ("replica_keys",)), make_node("cpu-b", features=MODELS + ("replica_keys",))
    ea, eb = FakeEngine("a", "secret-a"), FakeEngine("b", "secret-b")
    try:
        deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, replicas=2)
        make_ready("chat", [(a, ea.port, ea.secret), (b, eb.port, eb.secret)])
        api = api_client(owner, new_key(owner)["key"])
        body = {"model": "chat", "messages": [{"role": "user", "content": "hi"}]}
        seen = {api.post("/v1/chat/completions", json=body).json()["choices"][0]["message"]["content"] for _ in range(4)}
        assert seen == {"hello from a", "hello from b"}
        set_node(a, status="offline")
        before = len(ea.seen)
        for _ in range(4):
            r = api.post("/v1/chat/completions", json=body)
            assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello from b"
        assert len(ea.seen) == before and balancer._failed == {}
        route = owner.get("/api/routes").json()["routes"]
        target = next(t for rt in route if rt["served_name"] == "chat" for t in rt["targets"] if t["deployment"])
        assert target["deployment"]["ready"] == 1
        set_node(b, status="offline")
        r = api.post("/v1/chat/completions", json=body)
        assert r.status_code == 503 and "the machine is offline" in r.text, r.text
    finally:
        ea.close()
        eb.close()


def test_fy_move_012_replicas_already_unplaced_or_stopped_are_left_alone(owner, poked):
    """FY-MOVE-012: a stopped deployment's replica on the offline machine is not moved (it is not wanted
    anywhere), nor is anything on a revoked machine (revoking unplaces its replicas itself)."""
    seed_catalog()
    a = cpu("cpu-a")
    deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params=SMALL)
    cpu("cpu-b")
    dep_id = owner.get("/api/deployments").json()["deployments"][0]["id"]
    assert owner.post(f"/api/deployments/{dep_id}/stop").status_code == 200
    go_offline(a, 30 * MIN)
    assert sweep() == [] and machine_of("small") == a
    db = new_session()
    try:
        n = db.get(Node, a)
        assert offline.offline_for(n, utcnow()) >= 30 * MIN
        n.revoked_at = utcnow()
        assert offline.offline_for(n, utcnow()) is None
        n.status = "online"
        n.revoked_at = None
        assert offline.offline_for(n, utcnow()) is None
    finally:
        db.close()


def test_fy_move_013_right_after_a_server_start_machines_not_yet_back_keep_serving(owner):
    """FY-MOVE-013: a server that starts shows every machine offline until its agent reconnects, while the engines
    there still answer. For AGENT_OFFLINE_SECONDS after the start, a machine not heard from since keeps its replicas
    in the routes; after that, or once it was heard from and went offline again, they are skipped."""
    now = utcnow()
    db = new_session()
    try:
        a = db.get(Node, make_node("box-a"))
        a.status, a.last_heartbeat_at = "offline", now - 5 * MIN       # last heard before the start
        offline.server_started(now - timedelta(seconds=10))
        assert offline.skipped(a, now) is False                       # 10 s after the start: not yet
        assert offline.skipped(a, now + timedelta(seconds=21)) is True  # 31 s after it: offline for real
        a.last_heartbeat_at = now - timedelta(seconds=5)               # heard since the start, then lost
        assert offline.skipped(a, now) is True
        a.last_heartbeat_at = None                                     # never heard from at all
        assert offline.skipped(a, now) is False and offline.skipped(a, now + timedelta(seconds=25)) is True
        a.status = "online"
        assert offline.skipped(a, now + timedelta(days=1)) is False
        a.status, a.revoked_at = "revoked", now
        assert offline.skipped(a, now) is True
    finally:
        db.close()
