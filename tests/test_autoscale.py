"""Automatic replicas (1.5.0, #37; forgeyard/workloads/autoscale.py), with a fake clock and a fake gateway: the
settings (off by default, 1 to 2, 80 %, bounds, video refused, nothing restarts), up after the up window above the
target and at once at twice it, one at a time, only when it fits now (pinned: on its machine), down only after the
down window below, drained first (waits for what is in flight, kept when load returns or the others are not ready),
the pauses, sleep's interplay, the API and /metrics (FY-ASC-*)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard.db import Workload, new_session, utcnow
from forgeyard.workloads import autoscale, placement, sleep
from forgeyard.workloads.autoscale import Autoscaler
from tests.test_deploy_edit import CPU_BODY, cpu_row, ready, rows, set_replica, workload
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.test_gateway import gw  # noqa: F401  (the fixture)
from tests.workload_helpers import make_node

GIB = 1024**3
AUTO = {"autoscale": True}


@pytest.fixture(autouse=True)
def no_background_scaler(monkeypatch):
    """The app's own pass every 2 s would tick the shared autoscaler on the real clock: these tests tick their own."""
    async def idle_loop(interval: float = 2.0):
        import asyncio

        while True:
            await asyncio.sleep(3600)

    monkeypatch.setattr(autoscale, "run_forever", idle_loop)
    placement._previous.clear()
    yield
    placement._previous.clear()


class Clock:
    def __init__(self) -> None:
        self.t = 10_000.0

    def __call__(self) -> float:
        return self.t


class FakeGateway:
    """What the autoscaler reads: requests in flight per replica, the waiting lines, and drains."""

    def __init__(self) -> None:
        self.flight: dict[str, int] = {}
        self.lines: dict[str, int] = {}
        self.drains: set[str] = set()

    def in_flight(self, uid: str) -> int:
        return self.flight.get(uid, 0)

    def waiting(self) -> dict[str, int]:
        return dict(self.lines)

    def drain(self, uid: str) -> None:
        self.drains.add(uid)

    def undrain(self, uid: str) -> None:
        self.drains.discard(uid)

    def drained(self, uid: str) -> bool:
        return uid in self.drains and not self.flight.get(uid)


class Run:
    """An autoscaler on a fake clock and gateway; `wall` is 3 minutes on, past the pause after a person's change."""

    def __init__(self, monkeypatch) -> None:
        self.clock = Clock()
        self.gw = FakeGateway()
        self.scaler = Autoscaler(self.gw, self.clock)
        self.wall_offset = timedelta(minutes=3)
        monkeypatch.setattr(autoscale, "autoscaler", self.scaler)

    def wall(self):
        return utcnow() + self.wall_offset

    def tick(self) -> list[str]:
        return self.scaler.tick(wall=self.wall())

    def run(self, seconds: float) -> list[str]:
        """Ticks every 2 s for `seconds` (the first one 2 s from now)."""
        done = []
        for _ in range(int(round(seconds / autoscale.SAMPLE_SECONDS))):
            self.clock.t += autoscale.SAMPLE_SECONDS
            self.wall_offset += timedelta(seconds=autoscale.SAMPLE_SECONDS)
            done += self.tick()
        return done

    def load(self, name: str, *per_replica: int) -> None:
        """Requests in flight on replicas 0, 1, … of `name`."""
        uids = [r.uid for r in rows(name)]
        self.gw.flight = {u: n for u, n in zip(uids, per_replica)}


@pytest.fixture()
def cpus(owner):
    """Two CPU machines; cpu-a has much more free RAM, so binpack prefers cpu-b."""
    seed_catalog()
    a = make_node("cpu-a", features=MODELS)
    b = make_node("cpu-b", features=MODELS)
    cpu_row(a, vram_free=100 * GIB)
    cpu_row(b, vram_free=20 * GIB)
    return owner, a, b


@pytest.fixture()
def run(monkeypatch):
    return Run(monkeypatch)


def body(**params) -> dict:
    return {**CPU_BODY, "params": {**CPU_BODY["params"], "concurrency": 1, **params}}


def auto(c, name: str = "chat", **params) -> dict:
    """A CPU deployment with automatic replicas (concurrency 1; 1 to 2 unless given), its replicas ready."""
    machine = params.pop("machine_id", None)
    d = deploy(c, name, **body(**AUTO, **params), **({"machine_id": machine} if machine else {}))["deployment"]
    for r in rows(name):
        ready(name, r.ordinal)
    return d


def events(c, dep_id: int) -> list[str]:
    return [e["message"] for e in c.get(f"/api/deployments/{dep_id}").json()["events"]]


def scaled(c, dep_id: int) -> list[str]:
    return [e["message"] for e in c.get(f"/api/deployments/{dep_id}").json()["events"] if e["kind"] == "scaled"]


def live(name: str) -> list:
    return [r for r in rows(name) if not r.final]


# ------------------------------------------------------------------ the settings
def test_fy_asc_001_off_by_default_and_checked(cpus):
    """FY-ASC-001: a deployment made without the settings has a fixed number of replicas (automatic off; 1 to 2,
    80 %, 60 s and 10 min shown); turned on it starts with its minimum whatever `replicas` says; true/false only,
    whole numbers in bounds, min at most max, min 0 only with sleep when idle on; a refusal saves nothing."""
    c, a, b = cpus
    d = deploy(c, "plain", **CPU_BODY, replicas=2)["deployment"]
    assert not any(k in d["params"] for k in autoscale.PARAMS)
    assert d["autoscale"] == {**d["autoscale"], "on": False, "min": 1, "max": 2, "target": 80,
                              "scale_up_seconds": 60, "scale_down_minutes": 10, "replicas": 2}
    on = deploy(c, "on", **body(**AUTO, min_replicas=2, max_replicas="4", autoscale_target=50, scale_up_seconds=30,
                                scale_down_minutes=5), replicas=7)["deployment"]
    assert on["params"]["autoscale"] is True and on["params"]["max_replicas"] == 4
    assert on["autoscale"] == {**on["autoscale"], "on": True, "min": 2, "max": 4, "target": 50,
                               "scale_up_seconds": 30, "scale_down_minutes": 5, "replicas": 2, "capacity": 1}
    assert len(rows("on")) == 2 and workload("on").spec["replicas"] == 2
    zero = deploy(c, "zero", **body(**AUTO, sleep_when_idle=True, min_replicas=0))["deployment"]
    assert zero["autoscale"]["min"] == 0 and len(rows("zero")) == 1   # awake: at least 1
    off = deploy(c, "off", **body(autoscale=False, max_replicas=3))["deployment"]
    assert "autoscale" not in off["params"] and off["params"]["max_replicas"] == 3 and off["autoscale"]["on"] is False
    for bad, text in [({"autoscale": "yes"}, "true or false"), ({"autoscale": 1}, "true or false"),
                      ({**AUTO, "min_replicas": 3, "max_replicas": 2}, "more than max_replicas"),
                      ({**AUTO, "min_replicas": 0}, "only with sleep_when_idle"),
                      ({**AUTO, "max_replicas": 17}, "1 to 16"), ({**AUTO, "max_replicas": 0}, "1 to 16"),
                      ({**AUTO, "min_replicas": -1}, "0 to 16"), ({**AUTO, "min_replicas": 1.5}, "whole number"),
                      ({**AUTO, "max_replicas": True}, "whole number"),
                      ({**AUTO, "autoscale_target": 10}, "20 to 100"), ({**AUTO, "autoscale_target": 101}, "20 to 100"),
                      ({**AUTO, "scale_up_seconds": 5}, "10 to 600"), ({**AUTO, "scale_down_minutes": 0}, "1 to 120"),
                      ({**AUTO, "scale_down_minutes": 121}, "1 to 120"), ({**AUTO, "scale_up_seconds": "soon"},
                                                                         "whole number")]:
        r = c.post("/api/deployments", json={"name": "bad", **body(**bad)})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_params", (bad, r.text)
        assert text in r.json()["detail"]["message"], (bad, r.json())
    assert "bad" not in [x["name"] for x in c.get("/api/deployments").json()["deployments"]]


def test_fy_asc_002_estimate_and_changes_restart_nothing(cpus):
    """FY-ASC-002: the estimate of an automatic deployment places its minimum; turning automatic replicas on and
    changing its settings restart nothing (the server reads them); with it on, a number of replicas is refused, the
    current number is kept within new bounds (above the maximum: cut to it; below the minimum: raised to it)."""
    c, a, b = cpus
    r = c.post("/api/deployments/estimate", json={"name": "x", **body(**AUTO, min_replicas=2, max_replicas=3)})
    assert r.status_code == 200 and len(r.json()["placement"]["placed"]) == 2, r.text
    d = deploy(c, "chat", **body(), replicas=3)["deployment"]
    for i in range(3):
        ready("chat", i)
    path = f"/api/deployments/{d['id']}"
    out = c.patch(path, json={"params": {**AUTO, "max_replicas": 2}}).json()
    assert out["restart"] == "none" and out["changes"]["replicas"] == {"before": 3, "after": 2}, out
    assert out["changes"]["autoscale"] == {"before": None, "after": True}
    assert len(rows("chat")) == 2 and workload("chat").generation == 1 and rows("chat")[0].state == "healthy"
    r = c.patch(path, json={"replicas": 3})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "replicas_automatic", r.text
    assert c.patch(path, json={"replicas": 2, "params": {"autoscale_target": 60}}).json()["restart"] == "none"
    out = c.patch(path, json={"params": {"min_replicas": 3, "max_replicas": 4}}).json()
    assert out["changes"]["replicas"] == {"before": 2, "after": 3} and len(rows("chat")) == 3
    out = c.patch(path, json={"params": {"autoscale": False}}).json()
    assert out["restart"] == "none" and "replicas" not in out["changes"]
    assert c.patch(path, json={"replicas": 1}).status_code == 200 and len(rows("chat")) == 1   # fixed again


def test_fy_asc_003_video_is_never_scaled(owner):
    """FY-ASC-003: a video deployment refuses automatic replicas (a video job belongs to its replica); one that has
    the setting anyway (written by hand) is left alone whatever its load."""
    from tests.model_helpers import add_catalog
    from tests.test_video_generation import FEATURES, wan_meta

    add_catalog("wan2.1-t2v-1.3b", wan_meta(), task="video", fmt="diffusers",
                source="hf://Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
    make_node("a100", allowlist=[4], features=FEATURES)
    r = owner.post("/api/deployments", json={"name": "wan", "model": "wan2.1-t2v-1.3b", "recipe": "vllm-omni-video",
                                             "params": AUTO, "wait": True})
    assert r.status_code == 422 and "video deployments are not scaled" in r.json()["detail"]["message"], r.text
    deploy(owner, "wan", "wan2.1-t2v-1.3b", "vllm-omni-video", wait=True)
    db = new_session()
    try:
        from forgeyard.db import Deployment

        dep = db.scalar(select(Deployment).where(Deployment.name == "wan"))
        dep.params = {**dep.params, **AUTO, "max_replicas": 4}
        db.commit()
    finally:
        db.close()
    ready("wan", 0)
    fake = FakeGateway()
    clock = Clock()
    s = Autoscaler(fake, clock)
    fake.flight = {rows("wan")[0].uid: 50}
    for _ in range(60):
        clock.t += 2
        assert s.tick(wall=utcnow() + timedelta(minutes=5)) == []
    assert len(rows("wan")) == 1 and s.status(_dep("wan"))["load"] is None


def _dep(name: str):
    from sqlalchemy.orm import selectinload

    from forgeyard.db import Deployment, Replica

    db = new_session()
    try:
        d = db.scalar(select(Deployment).where(Deployment.name == name).options(
            selectinload(Deployment.workload).selectinload(Workload.replicas).selectinload(Replica.node)))
        db.expunge_all()
        return d
    finally:
        db.close()


# ------------------------------------------------------------------ up
def test_fy_asc_004_up_after_a_minute_above_the_target(cpus, run):
    """FY-ASC-004: one request in flight on one replica of concurrency 1 is 1.0 per replica, above the target 0.8
    and below twice it: no replica before a minute of it, one at a minute, placed now, with an event saying why; the
    load counted from the waiting line too."""
    c, a, b = cpus
    d = auto(c)
    run.load("chat", 1)
    assert run.run(60) == []   # the first sample is 2 s in: a minute of them 62 s in
    assert len(rows("chat")) == 1
    done = run.run(2)
    assert done == ["Scaled up to 2: load 1 per replica for 60 s (target 0.8 of 1 concurrent)."], done
    new = rows("chat")[1]
    assert new.ordinal == 1 and new.node_id is not None and new.state == "pending"   # placed at once
    assert workload("chat").spec["replicas"] == 2
    assert scaled(c, d["id"]) == done
    st = c.get(f"/api/deployments/{d['id']}").json()["deployment"]["autoscale"]
    assert st["on"] and st["replicas"] == 2 and st["load"] == 1.0 and st["wanted"] == 2
    assert st["last"]["text"] == done[0]
    # The line: nothing in flight and 1 waiting for a slot of the route's tier 0 is the same load.
    auto(c, "line")
    run.gw.flight = {}
    run.gw.lines = {"line#0": 1, "other#0": 9}
    assert run.run(60) == [] and len(rows("line")) == 1
    assert [x for x in run.run(2) if x.startswith("Scaled up to 2: load 1 per replica")] and len(rows("line")) == 2


def test_fy_asc_018_the_target_is_a_share_of_concurrent_requests(cpus, run):
    """FY-ASC-018: with Concurrent requests 5 a replica takes 4 at the target: 3 in flight (below it) and 4 (at it,
    not above) add nothing however long; 5 adds one after a minute."""
    c, a, b = cpus
    auto(c, concurrency=5, max_replicas=3)
    run.load("chat", 3)
    assert run.run(300) == []
    run.load("chat", 4)
    assert run.run(300) == [] and len(rows("chat")) == 1
    run.load("chat", 5)
    done = until_scaled(run, 70)
    assert done == ["Scaled up to 2: load 4.03 per replica for 60 s (target 4 of 5 concurrent)."], done


def test_fy_asc_005_at_twice_the_target_within_ten_seconds(cpus, run):
    """FY-ASC-005: 2 in flight on one replica (2.5 times the target): one more within about 10 s, not before."""
    c, a, b = cpus
    auto(c)
    run.load("chat", 2)
    assert run.run(10) == []
    done = run.run(2)
    assert done == ["Scaled up to 2: load 2 per replica for 10 s, twice the target 0.8 of 1 concurrent."], done


def test_fy_asc_006_one_at_a_time_and_never_above_the_maximum(cpus, run):
    """FY-ASC-006: while the added replica starts nothing more is added however high the load; once it is ready the
    next one comes; never more than max_replicas."""
    c, a, b = cpus
    auto(c, max_replicas=3)
    run.load("chat", 6)
    assert len(run.run(12)) == 1 and len(rows("chat")) == 2
    set_replica("chat", 1, state="starting")
    assert run.run(120) == [] and len(rows("chat")) == 2
    ready("chat", 1)
    run.load("chat", 3, 3)
    done = run.run(2)
    assert done and done[0].startswith("Scaled up to 3: load 3 per replica"), done
    ready("chat", 2)
    assert run.run(120) == [] and len(rows("chat")) == 3


def test_fy_asc_007_only_when_it_fits_now(cpus, run):
    """FY-ASC-007: one more that fits nowhere now is not added (no pending replica waiting for room): the event
    says what it wants and why, once; when room appears it is added. An offline machine's room does not count."""
    c, a, b = cpus
    d = auto(c)
    cpu_row(a, vram_free=GIB // 2)
    cpu_row(b, vram_free=GIB // 2)
    run.load("chat", 2)
    done = run.run(20)
    assert len(done) == 1 and done[0].startswith("Wants 2 replicas, room for 1: Nothing can take a replica"), done
    assert len(rows("chat")) == 1 and "room for 1" in c.get(f"/api/deployments/{d['id']}").json()["deployment"][
        "autoscale"]["room"]
    db = new_session()
    try:
        from forgeyard.db import Node

        db.get(Node, a).status = "offline"
        db.commit()
    finally:
        db.close()
    cpu_row(a, vram_free=100 * GIB)
    assert run.run(10) == [] and len(rows("chat")) == 1   # cpu-a has room but is offline
    cpu_row(b, vram_free=20 * GIB)
    done = run.run(2)
    assert done and done[0].startswith("Scaled up to 2"), done
    assert len(rows("chat")) == 2 and rows("chat")[1].node_id == b


def test_fy_asc_008_pinned_scales_on_its_machine_only(cpus, run):
    """FY-ASC-008: pinned to cpu-a, the new replica goes there; with no room left there it is not added although
    cpu-b has room; the pinned machine offline: no change (its replica's machine is offline)."""
    c, a, b = cpus
    auto(c, machine_id=a, max_replicas=3)
    assert rows("chat")[0].node_id == a
    run.load("chat", 2)
    assert run.run(12) and rows("chat")[1].node_id == a
    ready("chat", 1)
    cpu_row(a, vram_free=GIB // 2)
    run.load("chat", 2, 2)
    done = run.run(10)
    assert len(done) == 1 and done[0].startswith("Wants 3 replicas, room for 2: Nothing can take"), done
    assert "cpu-b" not in done[0] and len(rows("chat")) == 2
    db = new_session()
    try:
        from forgeyard.db import Node

        db.get(Node, a).status = "offline"
        db.commit()
    finally:
        db.close()
    cpu_row(a, vram_free=100 * GIB)
    assert run.run(30) == [] and len(rows("chat")) == 2
    assert run.scaler.status(_dep("chat"))["paused"] == "a replica's machine is offline"
    # Right after the server starts, a machine not heard from yet still serves (#164), so nothing pauses: but one
    # more replica is never placed on a pinned machine that is offline.
    from forgeyard.workloads import offline

    offline.server_started(utcnow() + run.wall_offset)
    try:
        done = run.run(10)
        assert run.scaler.status(_dep("chat"))["paused"] is None
        assert len(rows("chat")) == 2 and "cpu-a (pinned) is offline" in run.scaler.status(_dep("chat"))["room"], done
    finally:
        offline.server_started(utcnow() - timedelta(hours=1))


# ------------------------------------------------------------------ down, drained first
def two(c, run, **params) -> dict:
    """An automatic deployment scaled up to 2 by load, both ready, then idle."""
    d = auto(c, **params)
    run.load("chat", 2)
    assert run.run(12)
    ready("chat", 1)
    run.gw.flight = {}
    return d


def until_scaled(run, most: float = 20) -> list[str]:
    """Ticks until something is done (at most `most` seconds): the moment a window fills depends on what came
    before."""
    for _ in range(int(most / autoscale.SAMPLE_SECONDS)):
        done = run.run(autoscale.SAMPLE_SECONDS)
        if done:
            return done
    return []


def test_fy_asc_009_down_only_after_ten_minutes_below(cpus, run):
    """FY-ASC-009: after scaling up, an idle deployment keeps both replicas for 10 minutes; then the newest is
    drained (an event; the gateway sends it nothing new) and, with nothing in flight on it, removed on the next pass;
    never below the minimum."""
    c, a, b = cpus
    d = two(c, run)
    assert run.run(596) == [] and len(rows("chat")) == 2
    done = run.run(4)
    assert len(done) == 1 and done[0].startswith("Draining replica 1: load 0 per replica over 10 min (target 0.8); "
                                                 "it takes no new requests and nothing is in flight on it."), done
    uid = rows("chat")[1].uid
    assert run.gw.drains == {uid} and len(rows("chat")) == 2
    st = c.get(f"/api/deployments/{d['id']}").json()["deployment"]["autoscale"]
    assert st["draining"]["replica"] == 1 and st["draining"]["in_flight"] == 0
    done = run.run(2)
    assert len(done) == 1 and done[0].startswith("Removed replica 1 after draining for 2 s; 1 replica now."), done
    assert [r.ordinal for r in rows("chat")] == [0] and run.gw.drains == set()
    assert workload("chat").spec["replicas"] == 1
    assert run.run(1300) == [] and len(rows("chat")) == 1   # the minimum
    # A minimum of 2: two idle replicas stay.
    auto(c, "pair", min_replicas=2, max_replicas=3)
    assert len(rows("pair")) == 2
    run.gw.flight = {}
    assert run.run(1300) == [] and len(rows("pair")) == 2


def test_fy_asc_010_down_needs_the_load_to_fit_one_fewer(cpus, run):
    """FY-ASC-010: 2 replicas with 1 request in flight on average (0.5 per replica, below the target, but one replica
    alone would be at 1.0, above it) both stay, however long; once the load fits one replica at the target, one is
    drained (no flapping between one and two)."""
    c, a, b = cpus
    two(c, run)
    run.load("chat", 1, 0)
    assert run.run(1300) == [] and len(rows("chat")) == 2
    run.load("chat", 0, 0)
    done = run.run(600)
    assert any(x.startswith("Draining replica 1") for x in done), done


def test_fy_asc_022_no_drain_in_a_burst(cpus, run):
    """FY-ASC-022: the 10-minute average says one replica fewer would do, but a burst is on now (2 in flight on the
    replica that would stay, for the last 10 s): no drain until the burst is over."""
    c, a, b = cpus
    two(c, run)
    uid0 = rows("chat")[0].uid
    assert run.run(588) == []
    run.gw.flight = {uid0: 2}
    assert run.run(30) == [] and len(rows("chat")) == 2 and run.gw.drains == set()
    run.gw.flight = {}
    assert run.run(30) == []   # the last minute still holds the burst
    done = until_scaled(run, 40)
    assert done and done[0].startswith("Draining replica 1"), done


def test_fy_asc_011_a_drain_waits_for_what_is_in_flight(cpus, run, monkeypatch):
    """FY-ASC-011: a request in flight on the replica being drained is waited for, however long (here 2 minutes);
    then it is removed; after DRAIN_MAX_SECONDS it goes anyway, and the event says how many were left."""
    c, a, b = cpus
    two(c, run)
    uid = rows("chat")[1].uid
    run.run(598)
    run.gw.flight = {uid: 1}
    done = run.run(2)
    assert done and done[0].endswith("it takes no new requests and is removed once the 1 in flight finishes."), done
    assert run.run(120) == [] and len(rows("chat")) == 2 and run.gw.drains == {uid}
    run.gw.flight = {}
    done = run.run(2)
    assert done and done[0].startswith("Removed replica 1 after draining for 12"), done
    # The stream it finished is not load the one left must take: nothing is added back (the real run's flap).
    assert run.run(120) == [] and len(rows("chat")) == 1
    # A request that never ends: removed after DRAIN_MAX_SECONDS.
    monkeypatch.setattr(autoscale, "DRAIN_MAX_SECONDS", 300.0)
    run.load("chat", 2)
    assert until_scaled(run)[0].startswith("Scaled up to 2")
    ready("chat", 1)
    uid = rows("chat")[1].uid
    run.gw.flight = {}
    assert run.run(598) == []
    run.gw.flight = {uid: 1}
    assert run.run(2) and run.gw.drains == {uid}
    assert run.run(298) == []
    done = run.run(2)
    assert done == ["Removed replica 1 after draining for 5 min with 1 request still in flight; 1 replica now."], done


def test_fy_asc_012_a_drain_is_kept_when_load_returns_or_the_others_fail(cpus, run):
    """FY-ASC-012: load above the target for the one replica left keeps the drained replica (no new replica
    started instead); the other replica no longer ready keeps it too."""
    c, a, b = cpus
    d = two(c, run)
    uid0, uid1 = [r.uid for r in rows("chat")]
    assert run.run(598) == []
    run.gw.flight = {uid1: 1}
    assert run.run(2)[0].startswith("Draining replica 1")
    # Its own request does not count against the one left: it is served where it is.
    assert run.run(60) == [] and run.gw.drains == {uid1}
    run.gw.flight = {uid0: 2, uid1: 1}
    assert run.run(8) == []
    done = run.run(2)
    assert done == ["Kept replica 1: load 1.67 per replica for 10 s, twice the target 0.8 of 1 concurrent."], done
    assert run.gw.drains == set() and len(rows("chat")) == 2
    run.gw.flight = {}
    assert run.run(598) == []   # a full down window again after it was kept
    run.gw.flight = {uid1: 1}
    assert run.run(2)[0].startswith("Draining replica 1") and run.gw.drains == {uid1}
    set_replica("chat", 0, state="unhealthy")
    assert run.run(2) == ["Kept replica 1: no other replica is ready."]
    assert run.gw.drains == set() and len(rows("chat")) == 2
    assert scaled(c, d["id"])[0] == "Kept replica 1: no other replica is ready."


# ------------------------------------------------------------------ pauses, off, sleep
def test_fy_asc_013_pauses(cpus, run):
    """FY-ASC-013: no change within 2 minutes of a person's change or start, while an edit restarts the replicas,
    or while a replica's machine is offline; a drain under way is kept when a person changes the deployment."""
    c, a, b = cpus
    d = auto(c)
    run.load("chat", 2)
    run.wall_offset = timedelta(0)   # just created
    assert run.run(60) == [] and len(rows("chat")) == 1
    assert run.scaler.status(_dep("chat"))["paused"] == "it was started or changed less than 2 minutes ago"
    run.wall_offset = timedelta(minutes=3)
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == "chat"))
        w.generation += 1   # an edit restarts its replicas (rolling)
        db.commit()
    finally:
        db.close()
    assert run.run(30) == []
    assert run.scaler.status(_dep("chat"))["paused"] == "an edit is restarting its replicas"
    set_replica("chat", 0, generation=workload("chat").generation)
    assert run.run(2) and len(rows("chat")) == 2
    ready("chat", 1)
    run.gw.flight = {}
    assert any(x.startswith("Draining") for x in run.run(600))
    c.patch(f"/api/deployments/{d['id']}", json={"params": {"autoscale_target": 70}})
    run.wall_offset = timedelta(0)
    done = run.run(2)
    assert done == ["Kept replica 1: it was started or changed less than 2 minutes ago."], done
    assert run.gw.drains == set() and len(rows("chat")) == 2


def test_fy_asc_014_off_never_scales_and_turning_it_off_ends_a_drain(cpus, run):
    """FY-ASC-014: a deployment without automatic replicas is never changed by load; turned off during a drain, the
    replica takes requests again and stays; a stopped deployment is left alone."""
    c, a, b = cpus
    deploy(c, "fixed", **body())
    ready("fixed", 0)
    run.load("fixed", 9)
    assert run.run(120) == [] and len(rows("fixed")) == 1
    d = two(c, run)
    assert any(x.startswith("Draining") for x in run.run(600)) and run.gw.drains
    c.patch(f"/api/deployments/{d['id']}", json={"params": {"autoscale": False}})
    assert run.run(4) == [] and run.gw.drains == set() and len(rows("chat")) == 2
    c.post(f"/api/deployments/{d['id']}/stop")
    c.patch(f"/api/deployments/{d['id']}", json={"params": AUTO})
    assert run.run(1300) == [] and len(rows("chat")) == 2


def test_fy_asc_015_sleep_takes_it_to_zero_and_it_wakes_with_its_minimum(cpus, run):
    """FY-ASC-015: with sleep when idle on and min 0, the autoscaler never goes below 1 while awake; asleep, its
    rows beyond the minimum (at least 1) go once stopped, so it wakes with one; asleep, load changes nothing."""
    c, a, b = cpus
    d = two(c, run, sleep_when_idle=True, min_replicas=0, idle_minutes=60)
    assert sleep.sweep_once(utcnow() + timedelta(minutes=90)) == ["chat"]
    assert run.run(4) == [] and len(rows("chat")) == 2   # still stopping
    set_replica("chat", 0, state="stopped")
    set_replica("chat", 1, state="stopped")
    done = run.run(2)
    assert done == ["Asleep with 1 replica (its minimum): it wakes with as many."], done
    assert len(rows("chat")) == 1 and workload("chat").spec["replicas"] == 1
    run.gw.flight = {rows("chat")[0].uid: 5}
    assert run.run(60) == [] and len(rows("chat")) == 1
    c.post(f"/api/deployments/{d['id']}/start")
    assert workload("chat").desired_state == "running" and len(rows("chat")) == 1
    ready("chat", 0)
    run.gw.flight = {}
    assert run.run(1300) == [] and len(rows("chat")) == 1   # min 0 is 1 while awake


# ------------------------------------------------------------------ what the waiting line says, /metrics
def test_fy_asc_016_a_shared_line_counts_in_equal_parts(cpus, run):
    """FY-ASC-016: requests waiting in a tier with two deployments count half for each."""
    from forgeyard.db import GatewayRoute, RouteTarget

    c, a, b = cpus
    one = auto(c, "one")
    other = auto(c, "other")
    db = new_session()
    try:
        route = db.scalar(select(GatewayRoute).where(GatewayRoute.served_name == "one"))
        route.targets.append(RouteTarget(tier=0, kind="local", deployment_id=other["id"], upstream_model="other",
                                         weight=1, enabled=True))
        db.commit()
    finally:
        db.close()
    run.gw.lines = {"one#0": 3}
    run.run(4)
    assert run.scaler.status(_dep("one"))["load"] == 1.5 and run.scaler.status(_dep("other"))["load"] == 1.5
    assert one["id"] != other["id"]


def test_fy_asc_017_metrics(cpus, run):
    """FY-ASC-017: /metrics has each deployment's replicas desired, ready, draining and (automatic) wanted, its load,
    and the autoscaler's changes by direction."""
    from tests.test_metrics import scrape

    c, a, b = cpus
    deploy(c, "fixed", **body(), replicas=2)
    ready("fixed", 0)
    two(c, run)
    run.run(598)
    run.gw.flight = {rows("chat")[1].uid: 1}
    assert run.run(2)[0].startswith("Draining replica 1")
    text = scrape(c)
    for line in ('forgeyard_deployment_replicas{deployment="chat",kind="desired"} 2.0',
                 'forgeyard_deployment_replicas{deployment="chat",kind="ready"} 2.0',
                 'forgeyard_deployment_replicas{deployment="chat",kind="draining"} 1.0',
                 'forgeyard_deployment_replicas{deployment="chat",kind="wanted"} 1.0',
                 'forgeyard_deployment_load{deployment="chat"} 1.0',
                 'forgeyard_deployment_replicas{deployment="fixed",kind="desired"} 2.0',
                 'forgeyard_deployment_replicas{deployment="fixed",kind="ready"} 1.0',
                 'forgeyard_autoscale_total{deployment="chat",direction="up"}'):
        assert line in text, (line, [x for x in text.splitlines() if "deployment_" in x or "autoscale" in x])
    assert 'forgeyard_deployment_replicas{deployment="fixed",kind="wanted"}' not in text


# ------------------------------------------------------------------ the gateway: draining and what it reads
def test_fy_asc_019_the_balancer_drains_a_replica():
    """FY-ASC-019: a replica being drained takes no new slot (with a cap or without), comes last in the order, and
    is drained only once nothing is in flight on it; undrained it takes requests again; reset forgets drains."""
    from forgeyard.gateway.balancer import Balancer, Target

    b = Balancer()
    t0, t1 = Target("u0", 0, "http://a", None, None, False), Target("u1", 1, "http://b", None, None, False)
    assert b.try_acquire("u1", None)
    b.drain("u1")
    assert b.draining("u1") and not b.drained("u1")   # one in flight
    assert not b.try_acquire("u1", None) and not b.try_acquire("u1", 8)
    b.release("u1")
    assert b.drained("u1") and not b.drained("u0")
    for _ in range(4):   # both idle: it comes last every time, not in turn
        assert b.order("r", [t1, t0])[0] is t0 and b.order("r", [t0, t1])[0] is t0
    b.undrain("u1")
    assert not b.draining("u1") and not b.drained("u1") and b.try_acquire("u1", None)
    b.drain("u0")
    b.reset()
    assert not b.draining("u0") and b.try_acquire("u0", 1)


def test_fy_asc_020_a_drain_through_the_real_gateway(gw):  # noqa: F811
    """FY-ASC-020: two ready replicas behind the gateway; the autoscaler drains replica 1 while a stream is in flight
    on it: new requests all go to replica 0 even when it is the busier one, the stream is not cut, and replica 1 is
    removed only once the stream has ended, then requests keep going to replica 0. No request reaches two engines."""
    import threading

    from forgeyard.db import Deployment
    from forgeyard.gateway.balancer import balancer
    from tests.test_gateway import chat as gw_chat
    from tests.waits import wait_until

    db = new_session()
    try:
        dep = db.scalar(select(Deployment).where(Deployment.name == "chat"))
        dep.params = {**dep.params, **AUTO}
        db.commit()
    finally:
        db.close()
    uid0, uid1 = [r.uid for r in rows("chat")]
    clock = Clock()
    scaler = Autoscaler(clock=clock)
    wall = utcnow() + timedelta(minutes=3)

    def tick():
        clock.t += autoscale.SAMPLE_SECONDS
        return scaler.tick(wall=wall)

    for _ in range(300):   # 600 s with nothing in flight, the first sample 2 s in
        assert tick() == []
    gate = threading.Event()
    gw.e2.gate, gw.e2.gate_streams = gate, True
    balancer.failed(uid0)   # so the stream goes to replica 1
    got: dict = {}
    stream = threading.Thread(target=lambda: got.update(r=gw_chat(gw.api, stream=True)))
    stream.start()
    wait_until(lambda: len(gw.e2.posts()) == 1, "the stream reached replica 1")
    balancer._failed.clear()
    done = tick()
    assert done and done[0].startswith("Draining replica 1:") and done[0].endswith(
        "it takes no new requests and is removed once the 1 in flight finishes."), done
    assert balancer.draining(uid1)
    # Replica 0 holds one request: with a request on each, replica 1 would get the next ones in turn.
    held = threading.Event()
    gw.e1.gate = held
    busy: dict = {}
    first = threading.Thread(target=lambda: busy.update(r=gw_chat(gw.api)))
    first.start()
    wait_until(lambda: len(gw.e1.held) == 1, "a request held on replica 0")
    gw.e1.gate = None
    assert [gw_chat(gw.api).headers["x-forgeyard-replica"] for _ in range(4)] == ["chat#0"] * 4
    assert len(gw.e2.posts()) == 1
    assert tick() == [] and len(rows("chat")) == 2   # the stream is still in flight on it
    held.set()
    gate.set()
    first.join(30)
    stream.join(30)
    assert busy["r"].status_code == 200 and got["r"].status_code == 200
    assert got["r"].headers["x-forgeyard-replica"] == "chat#1" and got["r"].text.rstrip().endswith("data: [DONE]")
    assert gw.e2.finished_streams == 1
    done = tick()
    assert done and done[0].startswith("Removed replica 1 after draining for ") and done[0].endswith("1 replica now."), done
    assert [r.ordinal for r in rows("chat")] == [0] and not balancer.draining(uid1)
    assert [gw_chat(gw.api).headers["x-forgeyard-replica"] for _ in range(2)] == ["chat#0"] * 2
    assert len(gw.e1.posts()) == 7 and len(gw.e2.posts()) == 1   # 1 held + 4 + 2, and the stream: each once


def test_fy_asc_021_the_gateway_signals(owner):
    """FY-ASC-021: what the autoscaler reads from the gateway: requests in flight per replica, the waiting lines and
    the image queues (added up per route tier), and the balancer's drains."""
    from forgeyard.gateway import imagegen, waiting
    from forgeyard.gateway.balancer import balancer
    from forgeyard.workloads.autoscale import GatewaySignals

    s = GatewaySignals()
    waiting.forget()
    imagegen.forget()
    try:
        assert balancer.try_acquire("u9", None) and s.in_flight("u9") == 1
        line = waiting.line("chat#0")
        class Waiting:
            def wake(self):
                pass

        line.waiting.extend([Waiting(), Waiting()])   # two waiting (the line only counts them here)
        q = imagegen.queue("draw#0")
        assert q.join(8) and q.join(8) and q.join(8)
        assert s.waiting() == {"chat#0": 2, "draw#0": 3}
        s.drain("u9")
        assert not s.drained("u9")
        balancer.release("u9")
        assert s.drained("u9")
        s.undrain("u9")
        assert not s.drained("u9") and not balancer.draining("u9")
    finally:
        waiting.forget()
        imagegen.forget()
