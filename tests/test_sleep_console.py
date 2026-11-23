"""Sleep when idle in the console (1.5.0, #37, PR 4): what the deployment API's `sleep` object adds for the console
(`asleep_since` and `wakes` in the last day, beside PR 3's `waking`) and the two setup-advisor findings: a deployment
nobody used for 3 days that holds GPU memory with sleep when idle off, and one that wakes more than 10 times a day
and takes over a minute to start (FY-SLC-*)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard import advisor
from forgeyard.config import get_settings
from forgeyard.db import Deployment, UsageDaily, Workload, WorkloadEvent, new_session, utcnow
from forgeyard.workloads import placement, sleep
from forgeyard.workloads.sleep import idle
from tests.test_deploy_edit import CPU_BODY, cpu_row, ready, rows, set_replica
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

GIB = 1024**3
ON = {"sleep_when_idle": True}


@pytest.fixture(autouse=True)
def no_memory_of_machines():
    placement._previous.clear()
    yield
    placement._previous.clear()


def found(fid: str) -> list[advisor.Finding]:
    db = new_session()
    try:
        return [f for f in advisor.run(db, get_settings(), strict=True) if f.id == fid]
    finally:
        db.close()


def sleep_of(c, dep_id: int) -> dict:
    return c.get(f"/api/deployments/{dep_id}").json()["deployment"]["sleep"]


def add_event(name: str, kind: str, message: str, at, replica=None) -> None:
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == name))
        db.add(WorkloadEvent(workload_id=w.id, kind=kind, message=message, at=at, replica=replica))
        db.commit()
    finally:
        db.close()


def age(name: str, days: float) -> None:
    """The deployment was made, and last started or changed, `days` ago."""
    db = new_session()
    try:
        d = db.scalar(select(Deployment).where(Deployment.name == name))
        d.created_at = utcnow() - timedelta(days=days)
        d.workload.updated_at = utcnow() - timedelta(days=days)
        db.commit()
    finally:
        db.close()


def used_on(dep_id: int, days_ago: int, requests: int = 3) -> None:
    db = new_session()
    try:
        db.add(UsageDaily(day=(utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d"), source="api", key_id=1, user_id=1,
                          username="owner", model="chat", deployment_id=dep_id, requests=requests))
        db.commit()
    finally:
        db.close()


def set_params(name: str, **params) -> None:
    db = new_session()
    try:
        d = db.scalar(select(Deployment).where(Deployment.name == name))
        d.params = {**(d.params or {}), **params}
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def cpu(owner):
    seed_catalog()
    node = make_node("cpu-a", features=MODELS)
    cpu_row(node, vram_free=100 * GIB)
    return owner


@pytest.fixture()
def gpu(owner):
    """A GPU deployment of qwen3-4b on a100 GPU #4, ready (14.4 GiB, exclusive)."""
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    d = deploy(owner, "chat", "qwen3-4b", "vllm")["deployment"]
    ready("chat", 0)
    return owner, d


# ------------------------------------------------------------------ the API's sleep object
def test_fy_slc_001_asleep_since_waking_and_wakes_today(cpu):
    """FY-SLC-001: asleep_since is when it went to sleep (its event, not a later edit); Wake now makes it waking (since
    the wake) until a replica is ready; a Start that is not a wake never says waking; wakes counts the last 24 hours."""
    c = cpu
    d = deploy(c, "chat", **{**CPU_BODY, "params": {**CPU_BODY["params"], **ON, "idle_minutes": 1}})["deployment"]
    ready("chat", 0)
    s = sleep_of(c, d["id"])
    assert (s["asleep_since"], s["waking"], s["wakes"]) == (None, None, 0)

    assert sleep.sweep_once(now=utcnow() + timedelta(minutes=10)) == ["chat"]
    s = sleep_of(c, d["id"])
    assert s["asleep"] and s["asleep_since"] is not None and s["sleeps_at"] is None
    went = s["asleep_since"]
    # an edit while asleep moves updated_at, not when it fell asleep
    assert c.patch(f"/api/deployments/{d['id']}", json={"params": {"idle_minutes": 5}}).status_code == 200
    assert sleep_of(c, d["id"])["asleep_since"] == went

    for r in rows("chat"):   # its agent confirmed the stop
        set_replica("chat", r.ordinal, state="stopped")
    assert c.post(f"/api/deployments/{d['id']}/start").status_code == 200
    s = sleep_of(c, d["id"])
    assert s["waking"]["by"] == "owner" and s["waking"]["since"] is not None and s["asleep_since"] is None
    assert s["wakes"] == 1
    ready("chat", 0)
    s = sleep_of(c, d["id"])
    assert s["waking"] is None and s["wakes"] == 1

    # old wakes do not count; a person's Stop and Start is never "waking"
    add_event("chat", "woken", "Woken by owner (generation 9).", utcnow() - timedelta(hours=25))
    add_event("chat", "woken", "Woken by owner (generation 9).", utcnow() - timedelta(hours=23))
    assert sleep_of(c, d["id"])["wakes"] == 2
    assert c.post(f"/api/deployments/{d['id']}/stop").status_code == 200
    for r in rows("chat"):
        set_replica("chat", r.ordinal, state="stopped")
    s = sleep_of(c, d["id"])
    assert (s["asleep"], s["asleep_since"], s["waking"]) == (False, None, None)
    assert c.post(f"/api/deployments/{d['id']}/start").status_code == 200
    assert sleep_of(c, d["id"])["waking"] is None


# ------------------------------------------------------------------ advisor: let it sleep?
def test_fy_slc_002_the_advisor_points_an_idle_gpu_deployment_at_sleep_when_idle(gpu):
    """FY-SLC-002: running, sleep when idle off, holding GPU memory, no request for 3 days (Usage's last day, what this
    server saw, or since it was deployed, started or changed): an info finding naming the memory and the card,
    snoozable, linking to the edit page. Not with sleep on, not within 3 days of a request or a start, not while
    starting, not when stopped or asleep."""
    c, d = gpu
    age("chat", 2)
    assert found("deployment-idle-awake") == []           # deployed two days ago
    age("chat", 4)
    [f] = found("deployment-idle-awake")
    assert f.severity == "info" and f.snoozable and f.scope == f"deployment:{d['id']}"
    assert f.title == "chat had no requests for 4 days: let it sleep when idle?"
    assert "holds 14.4 GiB on a100 GPU #4" in f.detail and "nothing restarts" in f.detail
    assert f.fix_url == f"/serving/{d['id']}/edit" and f.pages == ("serving", f"deployment:{d['id']}")

    used_on(d["id"], 2)                                    # Usage: a request two days ago
    assert found("deployment-idle-awake") == []
    db = new_session()
    try:
        db.query(UsageDaily).delete()
        db.commit()
    finally:
        db.close()
    used_on(d["id"], 0, requests=0)                        # a row without requests today is not use
    assert len(found("deployment-idle-awake")) == 1
    age("chat", 10)
    used_on(d["id"], 5)                                    # five days ago: the end of that day counts, so four
    [f] = found("deployment-idle-awake")
    assert f.title.startswith("chat had no requests for 4 days")
    idle.used([d["id"]])                                   # this server saw one just now
    assert found("deployment-idle-awake") == []
    idle.reset()
    assert len(found("deployment-idle-awake")) == 1

    set_params("chat", **ON)                               # sleep when idle on: nothing to suggest
    assert found("deployment-idle-awake") == []
    set_params("chat", sleep_when_idle=False)
    assert len(found("deployment-idle-awake")) == 1
    set_replica("chat", 0, state="starting")               # not settled: it holds nothing yet
    assert found("deployment-idle-awake") == []
    ready("chat", 0)
    assert c.post(f"/api/deployments/{d['id']}/stop").status_code == 200
    assert found("deployment-idle-awake") == []
    set_replica("chat", 0, state="healthy")                # its agent has not stopped it yet: still a person's Stop
    age("chat", 10)
    assert found("deployment-idle-awake") == []


def test_fy_slc_003_cpu_deployments_are_not_pointed_at_sleep(cpu):
    """FY-SLC-003: a CPU-only deployment holds RAM, not GPU memory: no finding however long it was unused."""
    deploy(cpu, "chat", **CPU_BODY)
    ready("chat", 0)
    age("chat", 30)
    assert found("deployment-idle-awake") == []


# ------------------------------------------------------------------ advisor: raise its idle time
def ready_event(seconds: float, at=None) -> None:
    add_event("chat", "ready", f"Replica 0 on a100: ready in {seconds:.1f} s (engine {seconds:.1f} s)", at or utcnow(), 0)


def test_fy_slc_004_the_advisor_says_raise_the_idle_time_of_a_slow_model_woken_often(gpu):
    """FY-SLC-004: sleep when idle on, woken more than 10 times in the last 24 hours, and its last start took over a
    minute: an info finding with the count, the start time and its idle time. Not at 10 wakes, not for older wakes,
    not for a start of a minute or less, not without a measured start, not with sleep when idle off."""
    c, d = gpu
    set_params("chat", **ON, idle_minutes=5)
    now = utcnow()
    for i in range(11):
        add_event("chat", "woken", "Woken by a request.", now - timedelta(hours=1, minutes=i))
    assert found("deployment-wakes-often") == []          # no start measured yet
    ready_event(80.4)
    [f] = found("deployment-wakes-often")
    assert f.severity == "info" and f.snoozable and f.scope == f"deployment:{d['id']}"
    assert f.title == "chat woke 11 times in a day: raise its idle time"
    assert "its last start took 80 s" in f.detail and "after 5 min without a request" in f.detail
    assert f.fix_url == f"/serving/{d['id']}/edit"
    ready_event(60.0)                                      # a minute is not "over a minute"
    assert found("deployment-wakes-often") == []
    ready_event(61.0)
    assert len(found("deployment-wakes-often")) == 1
    set_params("chat", sleep_when_idle=False)
    assert found("deployment-wakes-often") == []
    set_params("chat", **ON)
    db = new_session()
    try:
        oldest = db.scalars(select(WorkloadEvent).where(WorkloadEvent.kind == "woken").order_by(WorkloadEvent.at)).first()
        oldest.at = now - timedelta(hours=30)              # ten left in the last day
        db.commit()
    finally:
        db.close()
    assert found("deployment-wakes-often") == []


def test_fy_slc_005_both_findings_are_documented_and_registered():
    """FY-SLC-005: both checks run with the others (FY-ADV-024 checks they are in the reference in both languages)."""
    names = {fn.__name__ for fn in advisor.CHECKS}
    assert {"deployment_idle_awake", "deployment_wakes_often"} <= names
    assert advisor.IDLE_HINT == timedelta(days=3) and advisor.WAKES_OFTEN == 10 and advisor.SLOW_START == 60.0


def test_fy_slc_006_get_started_counts_an_asleep_deployment_as_deployed(cpu):
    """FY-SLC-006: Get started's "Deploy it" stays done while the only deployment sleeps (it was ready; Wake now
    starts it), and says it is asleep; a person's Stop still makes it not ready."""
    c = cpu
    d = deploy(c, "chat", **{**CPU_BODY, "params": {**CPU_BODY["params"], **ON, "idle_minutes": 1}})["deployment"]
    ready("chat", 0)

    def step():
        return next(s for s in c.get("/api/onboarding").json()["steps"] if s["id"] == "deploy")

    assert step()["done"] and step()["detail"] == "Ready: chat."
    assert sleep.sweep_once(now=utcnow() + timedelta(minutes=10)) == ["chat"]
    assert step()["done"] and step()["detail"] == "Ready: chat (asleep)."
    assert c.post(f"/api/deployments/{d['id']}/stop").status_code == 200
    assert not step()["done"]
