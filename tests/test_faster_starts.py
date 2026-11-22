"""Faster starts (1.5.0, issue #37, PR 1; FY-START-*): health checked every second while a replica starts (only
with the "start_times" feature on both sides, and only within the start period), how long each start took and its
parts (agent), recorded as a "ready" event, in /metrics and in the deployment API (server), with older agents
measured by the server."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard import metrics
from forgeyard.agent.workloads import starts as agent_starts
from forgeyard.agent.workloads.records import Record
from forgeyard.agent.workloads.runtime import Observation
from forgeyard.agent.workloads.supervisor import Supervisor
from forgeyard.db import Deployment, Node, Replica, WorkloadEvent, new_session, utcnow
from forgeyard.machines.messages import StatusIn
from forgeyard.machines.protocol import FEATURE_START_TIMES, SERVER_FEATURES
from forgeyard.workloads import starts, state as wstate
from tests.test_metrics import scrape, value
from tests.test_model_supervisor import container_spec
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import converge, desired, make_node, replica, service

HEALTH = {"type": "http", "path": "/health", "interval": 10, "start_period": 60, "failure_threshold": 3}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def starting(health=HEALTH, **over) -> Record:
    rec = Record(id="r1", generation=1, workload="chat", ordinal=0, kind="service", runtime="container",
                 spec={"health": health}, ports=[{"name": "http", "port": 8000, "host_port": 31000}])
    rec.state, rec.started_at = "starting", 1_800_000_000.0
    for k, v in over.items():
        setattr(rec, k, v)
    return rec


# ------------------------------------------------------------------ the cadence (agent)
def test_fy_start_001_every_second_while_starting_then_the_recipes_interval():
    """FY-START-001: 1 s while it starts and within its start period; the recipe's interval once it was healthy,
    past the start period, without the feature, and never longer than the interval itself."""
    t0 = 1_800_000_000.0
    assert agent_starts.probe_interval(starting(), HEALTH, t0 + 5, fast=True) == 1.0
    assert agent_starts.probe_interval(starting(), HEALTH, t0 + 5, fast=False) == 10.0
    assert agent_starts.probe_interval(starting(healthy_seen=True), HEALTH, t0 + 5, fast=True) == 10.0
    assert agent_starts.probe_interval(starting(), HEALTH, t0 + 60, fast=True) == 10.0  # failures count from here
    assert agent_starts.probe_interval(starting(), {**HEALTH, "start_period": 0}, t0 + 1, fast=True) == 10.0
    assert agent_starts.probe_interval(starting(started_at=None), HEALTH, t0 + 5, fast=True) == 10.0
    assert agent_starts.probe_interval(starting(), {**HEALTH, "interval": 5}, t0 + 5, fast=True) == 1.0
    assert agent_starts.probe_interval(starting(), {**HEALTH, "interval": 1}, t0 + 5, fast=True) == 1.0


def _probes(tmp_path, features, health=HEALTH, healthy_after=None, until=40):
    """How often the supervisor probes a starting replica over `until` seconds of its clock, one round a second."""
    clock = Clock()
    calls = []

    def prober(*args, **kw):
        calls.append(clock.t)
        return None if healthy_after is not None and clock.t - 1_800_000_000.0 >= healthy_after else "refused"

    sup = Supervisor(tmp_path / "state", {}, clock=clock, prober=prober, tick=0.05)
    sup.server_features = set(features)
    rec = starting(health=health)
    sup.records[rec.id] = rec

    async def run():
        for second in range(until):
            clock.t = 1_800_000_000.0 + second
            sup._maybe_probe(rec, clock.t)
            await sup.settle()

    asyncio.run(run())
    return [round(t - 1_800_000_000.0) for t in calls], rec


def test_fy_start_002_the_feature_flag_decides_the_cadence(tmp_path):
    """FY-START-002: with "start_times" from the server, a starting replica is probed every second; without it
    (a server older than 1.5.0) every `interval` seconds, as before."""
    fast, _ = _probes(tmp_path / "a", {FEATURE_START_TIMES}, until=12)
    slow, _ = _probes(tmp_path / "b", set(), until=12)
    assert fast == list(range(12))
    assert slow == [0, 10]


def test_fy_start_003_back_to_the_interval_once_healthy_or_past_the_start_period(tmp_path):
    """FY-START-003: healthy at 7 s: probed every second until then, then every 10 s. Never healthy: every second
    for the start period (60 s), every 10 s after it, so failures are not counted any faster than before."""
    calls, rec = _probes(tmp_path / "a", {FEATURE_START_TIMES}, healthy_after=7, until=40)
    assert calls == [0, 1, 2, 3, 4, 5, 6, 7, 17, 27, 37] and rec.state == "healthy"
    calls, rec = _probes(tmp_path / "b", {FEATURE_START_TIMES}, until=90)
    assert calls == list(range(60)) + [69, 79, 89]
    assert rec.state == "unhealthy"  # three failures past the start period, 10 s apart, as before 1.5.0


# ------------------------------------------------------------------ the start, part by part (agent)
class FakeEngine:
    """A container runtime whose pull and `run` take a known time on the test's clock."""

    def __init__(self, clock, pull=3.0, run=2.0):
        self.clock, self.pull_s, self.run_s = clock, pull, run

    def info(self):
        return {"ok": True}

    def pull(self, rec, progress=None, cancel=None, relay=None):
        self.clock.t += self.pull_s

    def start(self, rec, address, secret=None):
        self.clock.t += self.run_s
        rec.container = "fy-" + rec.id

    def observe(self, rec):
        return Observation(alive=True)

    def stop(self, rec, grace):
        pass

    def cleanup(self, rec):
        pass

    def remove(self, rec):
        pass

    def tail(self, rec, lines):
        return "", False

    def trim(self, rec, max_bytes):
        pass

    def owned(self):
        return {}

    def remove_name(self, name):
        pass


class NoCache:
    root = None
    local_dirs: list = []

    def info(self):
        return {}


def _measured_start(tmp_path, features, *, model=True, healthy_after=5, spec=None):
    clock = Clock()
    probes = []
    step = 1.0 if FEATURE_START_TIMES in features else 10.0  # the time that passes between two probes

    def prober(*args, **kw):
        probes.append(clock.t)
        clock.t += step  # the engine loads meanwhile
        return None if len(probes) >= healthy_after else "refused"

    sup = Supervisor(tmp_path / "state", {"container": FakeEngine(clock)}, models=NoCache(), clock=clock,
                     prober=prober, tick=0.05)
    sup.new_connection(features)

    def model_record(rec, want):
        rec.model = {"kind": "hf"}

    async def prepare(rec):
        clock.t += 20.0  # the model's files
        return True

    sup._model_record = model_record
    sup._prepare = prepare
    entry = replica("r1", spec or container_spec())
    if model:
        entry["model"] = {"kind": "hf"}

    async def run():
        sup.set_desired(desired(entry))
        await converge(sup, lambda s: s.records["r1"].state in ("healthy", "running") and s.records["r1"].start_report,
                       timeout=20)
        return sup.take_report()

    report = asyncio.run(run())
    return sup.records["r1"], report


def test_fy_start_004_the_agent_measures_each_part_of_a_start(tmp_path):
    """FY-START-004: model 20 s, image 3 s, engine 2 s to launch and 5 probes of a second to answer: 30 s in all,
    the parts adding up to it; reported with the replica once it is healthy."""
    rec, report = _measured_start(tmp_path, {FEATURE_START_TIMES})
    assert rec.start_report == {"seconds": 30.0, "phases": {"model": 20.0, "image": 3.0, "engine": 7.0}}
    assert rec.start_marks is None
    entry = report["replicas"][0]
    assert entry["state"] == "healthy" and entry["start"] == rec.start_report


def test_fy_start_005_no_start_report_to_an_older_server(tmp_path):
    """FY-START-005: to a server without "start_times" the status is what it was before 1.5.0 (no `start`)."""
    rec, report = _measured_start(tmp_path, set())
    assert rec.start_report == {"seconds": 75.0, "phases": {"model": 20.0, "image": 3.0, "engine": 52.0}}
    assert "start" not in report["replicas"][0]


def test_fy_start_006_a_plain_container_has_no_model_part(tmp_path):
    """FY-START-006: without a model to fetch, the parts are the image and the engine."""
    rec, _ = _measured_start(tmp_path, {FEATURE_START_TIMES}, model=False, healthy_after=1)
    assert rec.start_report == {"seconds": 6.0, "phases": {"image": 3.0, "engine": 3.0}}


def test_fy_start_007_a_new_start_measures_again(tmp_path):
    """FY-START-007: `begin` clears the last report, a part is added from the last mark, nothing before `begin`
    counts, and a record without marks (adopted from an older agent) reports nothing."""
    rec = starting()
    assert agent_starts.finish(rec, 10.0) is None and rec.start_report is None
    agent_starts.begin(rec, 100.0)
    agent_starts.part(rec, "model", 112.25)
    agent_starts.part(rec, "nonsense", 113.0)
    agent_starts.part(rec, "waiting", 115.0)
    assert agent_starts.finish(rec, 130.0) == {"seconds": 30.0,
                                               "phases": {"model": 12.2, "waiting": 2.8, "engine": 15.0}}
    agent_starts.begin(rec, 200.0)
    assert rec.start_report is None and rec.start_marks["parts"] == {}


# ------------------------------------------------------------------ the server
def test_fy_start_008_status_start_is_validated():
    """FY-START-008: a `start` outside 0 s to a week, or not a number, is dropped; unknown parts too."""
    def parsed(start):
        return StatusIn.model_validate({"v": 1, "type": "status", "replicas": [
            {"id": "a", "generation": 1, "state": "healthy", "start": start}]}).replicas[0].start

    assert parsed({"seconds": 74.25, "phases": {"model": 20, "engine": 54.25, "x": 1, "image": -1}}) == {
        "seconds": 74.2, "phases": {"model": 20.0, "engine": 54.2}}
    assert parsed({"seconds": -1}) is None and parsed({"seconds": "74"}) is None
    assert parsed({"seconds": True}) is None and parsed({"seconds": 8 * 86400}) is None
    assert parsed("74 s") is None and parsed(None) is None
    assert parsed({"seconds": 3, "phases": "x"}) == {"seconds": 3.0, "phases": {}}


def test_fy_start_009_the_event_reads_back():
    """FY-START-009: the "ready" event's message says the start in words and parses back to the same numbers."""
    agent = starts.message(1, "cpu-01", {"seconds": 74.2, "phases": {"model": 20.0, "image": 3.1, "engine": 51.1}})
    assert agent == "Replica 1 on cpu-01: ready in 74.2 s (model 20.0 s, image 3.1 s, engine 51.1 s)"
    assert starts.parse(agent) == {"seconds": 74.2, "phases": {"model": 20.0, "image": 3.1, "engine": 51.1},
                                   "source": "agent"}
    server = starts.message(0, "gpu-01", {"seconds": 51.0, "phases": {"engine": 51.0}}, by_server=True)
    assert server == "Replica 0 on gpu-01: ready in 51.0 s (engine 51.0 s, measured by the server)"
    assert starts.parse(server) == {"seconds": 51.0, "phases": {"engine": 51.0}, "source": "server"}
    assert starts.parse(starts.message(0, "a", {"seconds": 2, "phases": {}})) == {"seconds": 2.0, "phases": {},
                                                                                  "source": "agent"}
    assert starts.parse("Replica 0 on a: ready in 3.0 s (foo 1.0 s, engine 2.0 s)")["phases"] == {"engine": 2.0}
    assert starts.parse("Replica 0 on a: healthy") is None and starts.parse("") is None


def _ready(dep_name, node_id, *, start=None, state="healthy", started_ago=None):
    db = new_session()
    try:
        node = db.get(Node, node_id)
        dep = db.scalar(select(Deployment).where(Deployment.name == dep_name))
        r = db.scalar(select(Replica).where(Replica.workload_id == dep.workload_id))
        st = {"id": r.uid, "generation": r.generation, "state": state}
        if started_ago is not None:
            st["started_at"] = (utcnow() - timedelta(seconds=started_ago)).timestamp()
        if start is not None:
            st["start"] = start
        wstate.apply_status(db, node, StatusIn.model_validate({"v": 1, "type": "status", "replicas": [st]}))
        db.commit()
        return [(e.kind, e.message) for e in db.scalars(select(WorkloadEvent).where(
            WorkloadEvent.workload_id == dep.workload_id).order_by(WorkloadEvent.id)).all()]
    finally:
        db.close()


@pytest.fixture()
def chat_on_cpu(owner):
    seed_catalog()
    node = make_node("cpu-01", features=MODELS)
    dep = deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})["deployment"]
    return owner, node, dep


def test_fy_start_010_ready_event_metric_and_api(chat_on_cpu):
    """FY-START-010: the replica turns healthy with the agent's `start`: one "ready" event after the state event,
    the histogram counts it under the recipe and the served name, and the deployment API's `last_start` says it.
    Staying healthy (or turning unhealthy and back) writes no second one."""
    owner, node, dep = chat_on_cpu
    before = value(scrape(owner), "forgeyard_replica_start_seconds_count", recipe="llama-cpp", model="small")
    assert owner.get(f"/api/deployments/{dep['id']}").json()["deployment"]["last_start"] is None
    start = {"seconds": 12.4, "phases": {"model": 2.0, "image": 0.4, "engine": 10.0}}
    events = _ready("small", node, start=start, started_ago=10)
    assert events[-2][0] == "state" and events[-2][1].endswith(": healthy")
    assert events[-1] == ("ready", "Replica 0 on cpu-01: ready in 12.4 s (model 2.0 s, image 0.4 s, engine 10.0 s)")
    text = scrape(owner)
    assert value(text, "forgeyard_replica_start_seconds_count", recipe="llama-cpp", model="small") == before + 1
    assert value(text, "forgeyard_replica_start_seconds_bucket", recipe="llama-cpp", model="small", le="20.0") >= 1
    got = owner.get(f"/api/deployments/{dep['id']}").json()["deployment"]["last_start"]
    assert {k: got[k] for k in ("seconds", "phases", "source", "replica")} == {**start, "source": "agent", "replica": 0}
    assert got["at"]
    assert owner.get("/api/deployments").json()["deployments"][0]["last_start"]["seconds"] == 12.4
    for state in ("healthy", "unhealthy", "healthy"):
        _ready("small", node, start=start, state=state)
    assert [k for k, _ in _ready("small", node, start=start)].count("ready") == 1
    # the latest "ready" event, not the state events written after it
    assert owner.get(f"/api/deployments/{dep['id']}").json()["deployment"]["last_start"]["seconds"] == 12.4


def test_fy_start_011_an_older_agent_is_measured_by_the_server(chat_on_cpu):
    """FY-START-011: no `start` (an agent older than 1.5.0): the server measures from the agent's started_at to now,
    the engine's part only, and says so; clocks that disagree (a start in the future) record nothing."""
    owner, node, dep = chat_on_cpu
    events = _ready("small", node, started_ago=42)
    kind, text = events[-1]
    assert kind == "ready" and "measured by the server" in text
    got = starts.parse(text)
    assert got["source"] == "server" and 41.5 <= got["seconds"] <= 50 and got["phases"] == {"engine": got["seconds"]}
    assert owner.get(f"/api/deployments/{dep['id']}").json()["deployment"]["last_start"]["source"] == "server"
    _ready("small", node, state="backoff")  # it ended: ready again later is a new start
    events = _ready("small", node, started_ago=-600)
    assert [k for k, _ in events].count("ready") == 1


def test_fy_start_012_only_model_deployments(owner):
    """FY-START-012: a plain workload turning healthy gets no "ready" event (the measure is for deployments)."""
    node = make_node("box")
    r = owner.post("/api/workloads", json=service("web", machine_id=node, health={"type": "tcp"}))
    assert r.status_code == 201, r.text
    w = r.json()["workload"]
    db = new_session()
    try:
        r = db.scalar(select(Replica).where(Replica.workload_id == w["id"]))
        n = db.get(Node, node)
        wstate.apply_status(db, n, StatusIn.model_validate({"v": 1, "type": "status", "replicas": [
            {"id": r.uid, "generation": r.generation, "state": "healthy", "start": {"seconds": 1, "phases": {}},
             "started_at": utcnow().timestamp()}]}))
        db.commit()
        kinds = [e.kind for e in db.scalars(select(WorkloadEvent).where(WorkloadEvent.workload_id == w["id"]))]
    finally:
        db.close()
    assert "state" in kinds and "ready" not in kinds


def test_fy_start_013_the_server_lists_the_feature():
    """FY-START-013: the server speaks "start_times", so a 1.5.0 agent turns it on. The engine cache needs no feature
    (an older agent ignores its block)."""
    assert "start_times" in SERVER_FEATURES and "engine_cache" not in SERVER_FEATURES


def test_fy_start_014_metric_never_raises(monkeypatch):
    """FY-START-014: a broken histogram must not fail the status that reports a start."""
    class Broken:
        def labels(self, *a):
            raise RuntimeError("boom")

    monkeypatch.setattr(metrics, "STARTS", Broken())
    metrics.replica_started("vllm", "chat", 3.0)  # logged, not raised


def test_fy_start_015_the_agents_hello_does_not_grow(tmp_path, monkeypatch):
    """FY-START-015: the agent lists neither "start_times" nor "engine_cache" in its hello: a fully equipped agent
    still lists at most 16 features, what servers up to 1.4.0 accept (an older server would refuse more). It learns
    whether the server measures starts from the welcome."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.container import ContainerRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from tests.workload_helpers import FakeDocker

    monkeypatch.setenv(SIMULATION_ENV, "1")
    inv = SimulatedInventory("nvidia-rtx4090x2")
    rt = ContainerRuntime(RecordStore(tmp_path / "s"), agent_id="a", engine=str(FakeDocker(tmp_path / "bin").path))
    sup = Supervisor(tmp_path / "state", {"container": rt}, models=ModelCache(tmp_path / "c"), tick=0.05)
    feats = Agent("http://127.0.0.1:9", "c", inv, supervisor=sup).hello(inv.full())["features"]
    assert "start_times" not in feats and "engine_cache" not in feats and len(feats) <= 16
    sup.new_connection({"workloads", FEATURE_START_TIMES})
    assert FEATURE_START_TIMES in sup.server_features


def test_fy_start_016_without_a_health_check_ready_once_it_runs(tmp_path):
    """FY-START-016: a container without a health check is ready once it runs: its start ends there."""
    spec = {**container_spec(), "health": None}
    rec, report = _measured_start(tmp_path, {FEATURE_START_TIMES}, spec=spec)
    assert rec.state == "running"
    assert rec.start_report == {"seconds": 25.0, "phases": {"model": 20.0, "image": 3.0, "engine": 2.0}}
    assert report["replicas"][0]["start"] == rec.start_report
