"""Sleep when idle (1.5.0, #37; forgeyard/workloads/sleep.py): the two parameters (off by default, 30 minutes,
1 to 1,440, changing them restarts nothing), the idle sweeper's timing (also after a server restart), what counts
as use and what holds a deployment awake, `asleep` as a desired state (replicas stopped, memory freed, files kept,
not audited), Start waking it (pinned: on its machine), Stop making it stopped, /v1/models still listing it and a
request waking it (tests/test_wake.py), the video rule, and what an older server reads (FY-SLP-*)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard.db import AuditEvent, Deployment, Node, Replica, VideoJob, Workload, new_session, utcnow
from forgeyard.gateway.balancer import balancer
from forgeyard.workloads import placement, sleep, state as wstate
from forgeyard.workloads.sleep import idle
from tests.gateway_helpers import make_ready
from tests.test_deploy_edit import CPU_BODY, cpu_row, ready, rows, set_replica, workload
from tests.test_gateway import chat, err, gw  # noqa: F401  (the fixture)
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.test_video_generation import make, vid  # noqa: F401  (the fixture)
from tests.workload_helpers import make_node

GIB = 1024**3
ON = {"sleep_when_idle": True}


@pytest.fixture(autouse=True)
def no_memory_of_machines():
    placement._previous.clear()
    yield
    placement._previous.clear()


@pytest.fixture()
def cpus(owner):
    """Two CPU machines; cpu-a has much more free RAM, so binpack prefers cpu-b."""
    seed_catalog()
    a = make_node("cpu-a", features=MODELS)
    b = make_node("cpu-b", features=MODELS)
    cpu_row(a, vram_free=100 * GIB)
    cpu_row(b, vram_free=20 * GIB)
    return owner, a, b


def later(minutes: float):
    return utcnow() + timedelta(minutes=minutes)


def events(c, dep_id: int) -> list[str]:
    return [e["message"] for e in c.get(f"/api/deployments/{dep_id}").json()["events"]]


def audits(action: str) -> list[dict]:
    db = new_session()
    try:
        return [e.detail | {"target": e.target} for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action))]
    finally:
        db.close()


def sleepy(c, name: str = "chat", minutes=None, **body) -> dict:
    """A CPU deployment with sleep when idle on (30 minutes unless given), its replica ready."""
    params = {**CPU_BODY["params"], **ON, **({"idle_minutes": minutes} if minutes is not None else {})}
    d = deploy(c, name, **{**CPU_BODY, "params": params}, **body)["deployment"]
    for r in rows(name):
        ready(name, r.ordinal)
    return d


def holding(node_id: int) -> list[Replica]:
    db = new_session()
    try:
        return placement.holding(db, node_id)
    finally:
        db.close()


# ------------------------------------------------------------------ the settings
def test_fy_slp_001_off_by_default_and_checked(cpus):
    """FY-SLP-001: a deployment made without the settings does not sleep (off, 30 minutes shown); turned on, it
    keeps the idle time given; true/false only, 1 to 1,440 whole minutes; idle_minutes alone may be stored while
    off; a refusal saves nothing."""
    c, a, b = cpus
    d = deploy(c, "plain", **CPU_BODY)["deployment"]
    assert "sleep_when_idle" not in d["params"] and "idle_minutes" not in d["params"]
    assert d["sleep"]["on"] is False and d["sleep"]["idle_minutes"] == 30 and d["sleep"]["asleep"] is False
    assert d["sleep"]["sleeps_at"] is None
    on = deploy(c, "on", **{**CPU_BODY, "params": {**CPU_BODY["params"], **ON, "idle_minutes": 5}})["deployment"]
    assert on["params"]["sleep_when_idle"] is True and on["params"]["idle_minutes"] == 5
    assert on["sleep"]["on"] is True and on["sleep"]["idle_minutes"] == 5 and on["sleep"]["sleeps_at"] is not None
    off = deploy(c, "off", **{**CPU_BODY, "params": {**CPU_BODY["params"], "sleep_when_idle": False,
                                                       "idle_minutes": "1440"}})["deployment"]
    assert "sleep_when_idle" not in off["params"] and off["params"]["idle_minutes"] == 1440
    assert off["sleep"]["on"] is False
    for bad, text in [({"sleep_when_idle": "yes"}, "true or false"), ({"sleep_when_idle": 1}, "true or false"),
                      ({"idle_minutes": 0}, "1 to 1,440"), ({"idle_minutes": 1441}, "1 to 1,440"),
                      ({"idle_minutes": 1.5}, "whole number"), ({"idle_minutes": True}, "whole number"),
                      ({"idle_minutes": "soon"}, "whole number")]:
        r = c.post("/api/deployments", json={"name": "bad", **CPU_BODY, "params": {**CPU_BODY["params"], **bad}})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_params", (bad, r.text)
        assert text in r.json()["detail"]["message"], (bad, r.json())
    assert c.get("/api/deployments").json()["deployments"][0]["name"] != "bad"


def test_fy_slp_002_changing_the_settings_restarts_nothing(cpus):
    """FY-SLP-002: turning sleep on, changing the idle time and turning it off again restart nothing (the server
    reads them, not the engine), are listed as changes and audited."""
    c, a, b = cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    ready("chat", 0)
    path = f"/api/deployments/{d['id']}"
    r = c.patch(path, json={"params": {**ON, "idle_minutes": 10}}).json()
    assert r["restart"] == "none", r
    assert r["changes"] == {"sleep_when_idle": {"before": None, "after": True},
                            "idle_minutes": {"before": None, "after": 10}}
    assert r["deployment"]["sleep"]["on"] is True and workload("chat").generation == 1
    assert rows("chat")[0].state == "healthy"
    r = c.patch(path, json={"params": {"sleep_when_idle": False}}).json()
    assert r["restart"] == "none" and r["changes"] == {"sleep_when_idle": {"before": True, "after": None}}
    assert r["deployment"]["sleep"] == {**r["deployment"]["sleep"], "on": False, "idle_minutes": 10}
    assert [x["changes"] for x in audits("deployment.changed")][0]["idle_minutes"]["after"] == 10
    assert workload("chat").generation == 1


# ------------------------------------------------------------------ the sweeper
def test_fy_slp_003_asleep_after_the_idle_time(cpus):
    """FY-SLP-003: not before the idle time, asleep after it: desired state `asleep`, the replica stopping (then
    stopped, holding nothing), the row and its machine kept, an event naming the time and what it frees, nothing
    audited; the sweeper leaves an asleep deployment alone."""
    c, a, b = cpus
    d = sleepy(c)
    node = rows("chat")[0].node_id
    set_replica("chat", 0, vram=3 * GIB)
    assert sleep.sweep_once(later(29)) == []
    assert workload("chat").desired_state == "running"
    assert sleep.sweep_once(later(31)) == ["chat"]
    w = workload("chat")
    assert w.desired_state == "asleep"
    r = rows("chat")[0]
    assert r.state == "stopping" and r.node_id == node and not r.final
    msgs = events(c, d["id"])
    assert any(m.startswith("Asleep: no requests for 30 min; frees 3.0 GiB of RAM on cpu-") for m in msgs), msgs
    assert audits("deployment.stopped") == [] and audits("workload.stopped") == []
    set_replica("chat", 0, state="stopped")   # the agent confirmed it
    assert holding(node) == []
    out = c.get(f"/api/deployments/{d['id']}").json()["deployment"]
    assert out["workload"]["status"] == "asleep" and out["sleep"]["asleep"] is True and out["sleep"]["sleeps_at"] is None
    assert sleep.sweep_once(later(120)) == []
    assert len([m for m in events(c, d["id"]) if m.startswith("Asleep")]) == 1


def test_fy_slp_004_its_own_idle_time_and_where_it_counts_from(cpus):
    """FY-SLP-004: idle_minutes 1 sleeps after a minute; the idle time counts from the latest of the last use,
    the deployment's last start or change, a replica becoming ready and the server's start."""
    c, a, b = cpus
    sleepy(c, "quick", minutes=1)
    assert sleep.sweep_once(later(0.5)) == []
    assert sleep.sweep_once(later(1.5)) == ["quick"]
    sleepy(c, "chat")
    set_replica("chat", 0, ready_at=later(20))      # it became ready 20 minutes from now (a restart, say)
    assert sleep.sweep_once(later(45)) == []
    assert sleep.sweep_once(later(51)) == ["chat"]
    # Turned on long after the server started and the replica became ready: it counts from the change.
    idle.server_started(utcnow() - timedelta(hours=3))
    d = deploy(c, "late", **CPU_BODY)["deployment"]
    set_replica("late", 0, state="healthy", ready_at=utcnow() - timedelta(hours=2))
    db = new_session()
    try:
        db.scalar(select(Workload).where(Workload.name == "late")).updated_at = utcnow() - timedelta(hours=2)
        db.commit()
    finally:
        db.close()
    assert c.patch(f"/api/deployments/{d['id']}", json={"params": ON}).json()["restart"] == "none"
    assert sleep.sweep_once(later(29)) == []
    assert sleep.sweep_once(later(31)) == ["late"]


def test_fy_slp_005_a_server_restart_never_puts_it_to_sleep_early(cpus):
    """FY-SLP-005: last uses live in the server's memory: after a restart, every idle time counts from the
    restart, even for a deployment whose start and readiness are long past."""
    c, a, b = cpus
    sleepy(c)
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == "chat"))
        w.updated_at = utcnow() - timedelta(hours=5)
        for r in w.replicas:
            r.ready_at = utcnow() - timedelta(hours=5)
        db.commit()
    finally:
        db.close()
    idle.used([1, 2, 3], at=utcnow() - timedelta(hours=4))
    idle.reset()                  # the server restarted: its memory is empty
    idle.server_started(later(10))  # (it started ten minutes from now)
    assert sleep.sweep_once(later(35)) == []
    assert workload("chat").desired_state == "running"
    assert sleep.sweep_once(later(41)) == ["chat"]


def test_fy_slp_006_what_holds_it_awake(cpus):
    """FY-SLP-006: not while a gateway request for it is under way (also waiting), a stream is still on its
    replica, a replica is starting (placed and pending, or starting), or an edit restarts its replicas; the API
    says why."""
    c, a, b = cpus
    d = sleepy(c)
    using = idle.begin([d["id"]])
    assert sleep.sweep_once(later(31)) == []
    assert c.get(f"/api/deployments/{d['id']}").json()["deployment"]["sleep"]["held_by"] == "a request is in flight"
    idle.end(using)
    uid = rows("chat")[0].uid
    balancer.acquire(uid)
    assert sleep.sweep_once(later(31)) == []
    balancer.release(uid)
    for state in ("starting", "pending"):
        set_replica("chat", 0, state=state)
        assert sleep.sweep_once(later(31)) == [], state
        assert c.get(f"/api/deployments/{d['id']}").json()["deployment"]["sleep"]["held_by"] == "a replica is starting"
    ready("chat", 0)
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == "chat"))
        w.generation += 1   # an edit: replica 0 still runs the old settings
        db.commit()
    finally:
        db.close()
    assert sleep.sweep_once(later(31)) == []
    assert c.get(f"/api/deployments/{d['id']}").json()["deployment"]["sleep"]["held_by"] == \
        "an edit is restarting its replicas"
    set_replica("chat", 0, generation=workload("chat").generation)
    assert sleep.sweep_once(later(31)) == ["chat"]


def test_fy_slp_007_a_request_while_it_is_put_to_sleep_keeps_it_awake(cpus, monkeypatch):
    """FY-SLP-007: a request that comes in between the sweeper's check and its commit wins: nothing is saved, the
    deployment keeps running, no event."""
    c, a, b = cpus
    d = sleepy(c)
    real = wstate.stop

    def racing(db, w, connected, to="stopped"):
        nodes = real(db, w, connected, to=to)
        idle.used([d["id"]])
        return nodes

    monkeypatch.setattr(wstate, "stop", racing)
    assert sleep.sweep_once(later(31)) == []
    assert workload("chat").desired_state == "running" and rows("chat")[0].state == "healthy"
    assert not any(m.startswith("Asleep") for m in events(c, d["id"]))


def test_fy_slp_008_off_never_sleeps_and_stopped_stays_stopped(cpus):
    """FY-SLP-008: with the setting off nothing sleeps, however long; a stopped deployment is not touched."""
    c, a, b = cpus
    deploy(c, "plain", **CPU_BODY)
    ready("plain", 0)
    d = sleepy(c)
    c.post(f"/api/deployments/{d['id']}/stop")
    assert sleep.sweep_once(later(60 * 24 * 30)) == []
    assert workload("plain").desired_state == "running" and workload("chat").desired_state == "stopped"


# ------------------------------------------------------------------ waking, stopping
def test_fy_slp_009_start_wakes_it_where_it_ran(cpus):
    """FY-SLP-009: Start on an asleep deployment wakes it: running, a new generation, the replica back on the
    machine it ran on (although binpack prefers the other), an event "Woken by", audited as a start."""
    c, a, b = cpus
    cpu_row(b, vram_free=100 * GIB)
    cpu_row(a, vram_free=20 * GIB)
    d = sleepy(c)
    node = rows("chat")[0].node_id
    assert node == a
    cpu_row(a, vram_free=30 * GIB)
    cpu_row(b, vram_free=25 * GIB)   # a new deployment would now go to cpu-b
    sleep.sweep_once(later(31))
    path = f"/api/deployments/{d['id']}"
    assert c.post(f"{path}/start").json()["detail"]["code"] == "still_stopping"
    set_replica("chat", 0, state="stopped")
    r = c.post(f"{path}/start")
    assert r.status_code == 200, r.text
    assert workload("chat").desired_state == "running" and workload("chat").generation == 2
    assert rows("chat")[0].node_id == node and rows("chat")[0].state == "pending"
    assert any(m.startswith("Woken by owner (generation 2)") for m in events(c, d["id"]))
    [rec] = audits("workload.started")
    assert rec["woken"] is True
    assert [x["target"] for x in audits("deployment.started")] == ["chat"]
    assert c.post(f"{path}/start").json()["detail"]["code"] == "already_running"


def test_fy_slp_010_a_pinned_deployment_sleeps_and_wakes_on_its_machine(cpus):
    """FY-SLP-010: pinned to cpu-a (binpack prefers cpu-b), it sleeps there and wakes there, also when the
    other machine has more room."""
    c, a, b = cpus
    d = sleepy(c, machine_id=a)
    assert rows("chat")[0].node_id == a
    sleep.sweep_once(later(31))
    set_replica("chat", 0, state="stopped")
    cpu_row(b, vram_free=200 * GIB)
    placement._previous.clear()   # no memory of where it ran: only the pin keeps it there
    c.post(f"/api/deployments/{d['id']}/start")
    assert rows("chat")[0].node_id == a
    assert c.get(f"/api/deployments/{d['id']}").json()["deployment"]["pinned"]["id"] == a


def test_fy_slp_011_stop_on_an_asleep_deployment_makes_it_stopped(cpus):
    """FY-SLP-011: a person's Stop on an asleep deployment makes it stopped (requests will not wake it), says it
    was asleep, and is audited; Stop on a stopped one is still 409."""
    c, a, b = cpus
    d = sleepy(c)
    sleep.sweep_once(later(31))
    set_replica("chat", 0, state="stopped")
    path = f"/api/deployments/{d['id']}"
    r = c.post(f"{path}/stop")
    assert r.status_code == 200, r.text
    assert workload("chat").desired_state == "stopped"
    assert r.json()["deployment"]["workload"]["status"] == "stopped"
    assert any(m == "Stopped by owner (it was asleep)." for m in events(c, d["id"]))
    assert [x["target"] for x in audits("deployment.stopped")] == ["chat"]
    assert c.post(f"{path}/stop").json()["detail"]["code"] == "already_stopped"


def test_fy_slp_012_edits_while_asleep_apply_when_it_wakes(cpus):
    """FY-SLP-012: a change that would restart the engine is saved while asleep and applies when it wakes; Try it
    says it is asleep."""
    c, a, b = cpus
    d = sleepy(c)
    sleep.sweep_once(later(31))
    set_replica("chat", 0, state="stopped")
    path = f"/api/deployments/{d['id']}"
    r = c.patch(path, json={"params": {"context": 2048}}).json()
    assert r["restart"] == "on_wake" and r["restart_text"] == "It is asleep: the new settings apply when it wakes."
    assert workload("chat").desired_state == "asleep"
    t = c.post(f"{path}/try", json={})
    assert t.status_code == 409 and t.json()["detail"]["code"] == "model_asleep"


# ------------------------------------------------------------------ the gateway
def _sleep_chat(gw):  # noqa: F811
    deps = {x["name"]: x["id"] for x in gw.owner.get("/api/deployments").json()["deployments"]}
    r = gw.owner.patch(f"/api/deployments/{deps['chat']}", json={"params": ON})
    assert r.status_code == 200 and r.json()["restart"] == "none", r.text
    return deps["chat"]


def test_fy_slp_013_what_counts_as_use(gw):  # noqa: F811
    """FY-SLP-013: a request through the gateway (and the Playground) and Try it are use; /v1/models, the
    deployment pages and their polling are not."""
    dep = _sleep_chat(gw)
    assert idle.last(dep) is None
    gw.api.get("/v1/models")
    gw.owner.get("/api/deployments")
    gw.owner.get(f"/api/deployments/{dep}")
    assert idle.last(dep) is None
    before = utcnow()
    assert chat(gw.api).status_code == 200
    assert idle.last(dep) is not None and idle.last(dep) >= before and idle.active(dep) == 0
    idle.reset()
    r = gw.owner.post("/api/playground/chat/completions", headers={"X-Forgeyard-Console": "1"},
                      json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert idle.last(dep) is not None
    idle.reset()
    gw.owner.post(f"/api/deployments/{dep}/try", json={})
    assert idle.last(dep) is not None
    # Used a minute from now: the sweeper counts from then.
    idle.used([dep], at=later(1))
    assert sleep.sweep_once(later(30.5)) == []
    assert sleep.sweep_once(later(31.5)) == ["chat"]


def test_fy_slp_019_a_request_is_use_from_its_start_to_its_end(gw):  # noqa: F811
    """FY-SLP-019: while the gateway holds a request (here: the engine has not answered yet) the deployment is in
    flight and used as of its start; a streamed answer counts as use until its last byte, not only until its
    headers."""
    import threading
    import time

    dep = _sleep_chat(gw)
    gate = threading.Event()
    gw.e1.gate = gw.e2.gate = gate
    done: list = []
    t = threading.Thread(target=lambda: done.append(chat(gw.api)), daemon=True)
    try:
        t.start()
        deadline = time.monotonic() + 10
        while not (gw.e1.held or gw.e2.held) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert gw.e1.held or gw.e2.held
        assert idle.active(dep) == 1 and idle.last(dep) is not None
        assert sleep.sweep_once(later(31)) == []
    finally:
        gate.set()
        t.join(10)
    assert done and done[0].status_code == 200 and idle.active(dep) == 0
    gw.e1.gate = gw.e2.gate = None
    gw.e1.delay = gw.e2.delay = 0.3       # three chunks: the stream takes about a second
    idle.reset()
    started = utcnow()
    r = chat(gw.api, stream=True)
    assert r.status_code == 200 and "[DONE]" in r.text
    assert (idle.last(dep) - started).total_seconds() >= 0.5, (idle.last(dep), started)


def test_fy_slp_014_an_asleep_model_stays_listed_and_a_request_wakes_it(gw):  # noqa: F811
    """FY-SLP-014: asleep, the model is still in /v1/models (GET /v1/models/chat too); a request to it wakes it
    (1.5.0 PR 3, forgeyard/gateway/waking.py: tests/test_wake.py has the rest) instead of the 503 model_asleep of
    PR 2; once a person stopped it, it leaves the list and answers that it is stopped."""
    from tests.test_routing import background
    from tests.waits import wait_until

    dep = _sleep_chat(gw)
    assert sleep.sweep_once(later(31)) == ["chat"]
    for i in (0, 1):
        set_replica("chat", i, state="stopped")
    ids = [m["id"] for m in gw.api.get("/v1/models").json()["data"]]
    assert "chat" in ids
    assert gw.api.get("/v1/models/chat").status_code == 200
    t, out = background(lambda: chat(gw.api))
    wait_until(lambda: workload("chat").desired_state == "running" and all(r.node_id for r in rows("chat")), "woken")
    assert len(gw.e1.posts()) + len(gw.e2.posts()) == 0
    make_ready("chat", [(gw.a, gw.e1.port, gw.e1.secret), (gw.b, gw.e2.port, gw.e2.secret)])
    t.join(15)
    assert out["r"].status_code == 200, out["r"].text
    gw.owner.post(f"/api/deployments/{dep}/stop")
    assert "chat" not in [m["id"] for m in gw.api.get("/v1/models").json()["data"]]
    r = chat(gw.api)
    assert r.status_code == 503 and err(r)["code"] == "no_ready_replica" and "stopped" in err(r)["message"]


def test_fy_slp_015_start_wakes_it_for_the_gateway(gw):  # noqa: F811
    """FY-SLP-015: after Start the replicas answer again (here: made ready by hand, as their agents would)."""
    dep = _sleep_chat(gw)
    sleep.sweep_once(later(31))
    for i in (0, 1):
        set_replica("chat", i, state="stopped")
    assert gw.owner.post(f"/api/deployments/{dep}/start").status_code == 200
    make_ready("chat", [(gw.a, gw.e1.port, gw.e1.secret), (gw.b, gw.e2.port, gw.e2.secret)])
    assert chat(gw.api).status_code == 200


# ------------------------------------------------------------------ video
def test_fy_slp_016_a_video_deployment_sleeps_once_its_videos_are_done_and_gone(cpus):
    """FY-SLP-016: a job waiting or being made holds it awake; a finished video kept for download too, until it
    expires; a failed job or a removed file does not (#37, Q8)."""
    c, a, b = cpus
    d = sleepy(c)
    db = new_session()
    n = [0]

    def add(status: str, expires=None, removed=False) -> int:
        n[0] += 1
        j = VideoJob(public_id=f"video_{n[0]}", source="api", key_id=1, user_id=1,
                     model="chat", deployment_id=d["id"], replica_uid="x", replica_epoch="1:0", upstream_id="u",
                     status=status, size="832x480", seconds=5, frames=81, fps=16, deadline_at=utcnow(), keep_s=60,
                     expires_at=expires, removed=removed)
        db.add(j)
        db.commit()
        return j.id

    def drop(job_id: int) -> None:
        db.delete(db.get(VideoJob, job_id))
        db.commit()

    try:
        add("failed", expires=later(60))
        add("completed", expires=later(60), removed=True)
        add("completed", expires=later(-1))
        for status in ("queued", "in_progress"):
            j = add(status)
            assert sleep.sweep_once(later(31)) == [], status
            assert c.get(f"/api/deployments/{d['id']}").json()["deployment"]["sleep"]["held_by"] == \
                "a video is waiting or being made"
            drop(j)
        j = add("completed", expires=later(45))
        assert sleep.sweep_once(later(31)) == []
        held = c.get(f"/api/deployments/{d['id']}").json()["deployment"]["sleep"]["held_by"]
        assert held.startswith("a finished video is kept until "), held
        assert sleep.sweep_once(later(46)) == ["chat"]
    finally:
        db.close()


def test_fy_slp_018_an_asleep_video_model_is_woken_by_a_video_asked_of_it(vid):  # noqa: F811
    """FY-SLP-018: a video deployment with no job sleeps like any other; a video asked of it then wakes it (1.5.0
    PR 3, instead of 503 model_asleep) and no engine hears of it until a replica is ready."""
    from tests.test_routing import background
    from tests.waits import wait_until

    dep = vid.owner.get("/api/deployments").json()["deployments"][0]
    assert vid.owner.patch(f"/api/deployments/{dep['id']}", json={"params": ON}).json()["restart"] == "none"
    assert sleep.sweep_once(later(31)) == ["wan"]
    for i in (0, 1):
        set_replica("wan", i, state="stopped")
    t, out = background(lambda: make(vid.api))
    wait_until(lambda: workload("wan").desired_state == "running" and all(r.node_id for r in rows("wan")), "woken")
    assert all(not e.posts for e in vid.engines)
    make_ready("wan", [(vid.gpu, e.port, e.secret) for e in vid.engines])
    t.join(15)
    assert out["r"].status_code == 200, out["r"].text


# ------------------------------------------------------------------ an older server (downgrade)
def test_fy_slp_017_everything_that_asks_for_running_treats_asleep_as_stopped(cpus):
    """FY-SLP-017: what a 1.4 server (and this one) does with a workload that is not `running`: its agents' work
    lists leave it out, placement does not place it, the gateway has no ready replica of it. So a downgraded
    server sees an asleep deployment as stopped, and Start (which only refuses `running`) works there too."""
    c, a, b = cpus
    d = sleepy(c, replicas=2)
    sleep.sweep_once(later(31))
    node = rows("chat")[0].node_id
    db = new_session()
    try:
        assert all(r.workload.name != "chat" for r in wstate.desired_replicas(db, node))
        w = db.scalar(select(Workload).where(Workload.name == "chat"))
        r1 = next(r for r in w.replicas if r.ordinal == 1)
        wstate.reset_replica(r1, w.generation)
        r1.state = "stopped"
        db.commit()
        placement.place_pending(db)
        db.expire_all()
        assert db.scalar(select(Replica).where(Replica.id == r1.id)).node_id is None
        from forgeyard.models import routes

        assert not any(routes._ready(r, w) for r in w.replicas)
        dep = db.get(Deployment, d["id"])
        assert dep.workload.desired_state == "asleep" and db.get(Node, node) is not None
    finally:
        db.close()
