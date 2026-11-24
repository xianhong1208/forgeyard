"""1.3.1: changing a deployment (PATCH), rolling restarts, a restarted replica going back to the machine
it ran on (FY-EDIT-*); pointing out GPU sharing when it would fit (FY-SHR-*); a replica that no agent
picks up (FY-PICK-*)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard import __version__
from forgeyard.db import Accelerator, AuditEvent, Node, Replica, Workload, new_session, utcnow
from forgeyard.machines.hub import hub
from forgeyard.workloads import placement, state as wstate
from tests.conftest import add_user, sign_in
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

GIB = 1024**3
PW = "quiet-meadow-4821"
CPU_BODY = {"model": "qwen3-1.7b-gguf", "recipe": "llama-cpp", "params": {"gpus": 0, "threads": 2}}


@pytest.fixture(autouse=True)
def no_memory_of_machines():
    placement._previous.clear()
    yield
    placement._previous.clear()


def rows(name: str) -> list[Replica]:
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == name))
        out = sorted(w.replicas, key=lambda r: r.ordinal)
        for r in out:
            db.expunge(r)
        return out
    finally:
        db.close()


def workload(name: str) -> Workload:
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == name))
        db.expunge(w)
        return w
    finally:
        db.close()


def set_replica(name: str, ordinal: int, **fields) -> None:
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == name))
        r = next(r for r in w.replicas if r.ordinal == ordinal)
        for k, v in fields.items():
            setattr(r, k, v)
        db.commit()
    finally:
        db.close()


def ready(name: str, ordinal: int) -> None:
    """The replica's agent reported it healthy a while ago (its memory is in the agent's numbers)."""
    set_replica(name, ordinal, state="healthy", ready_at=utcnow() - timedelta(minutes=5),
                started_at=utcnow() - timedelta(minutes=6))


def cpu_row(node_id: int, **fields) -> None:
    db = new_session()
    try:
        a = db.scalar(select(Accelerator).where(Accelerator.node_id == node_id, Accelerator.vendor == "cpu"))
        for k, v in fields.items():
            setattr(a, k, v)
        db.commit()
    finally:
        db.close()


def card(node_id: int, index: int, **fields) -> None:
    db = new_session()
    try:
        a = db.scalar(select(Accelerator).where(Accelerator.node_id == node_id, Accelerator.index == index,
                                                Accelerator.vendor != "cpu"))
        for k, v in fields.items():
            setattr(a, k, v)
        db.commit()
    finally:
        db.close()


def node_field(node_id: int, **fields) -> None:
    db = new_session()
    try:
        n = db.get(Node, node_id)
        for k, v in fields.items():
            setattr(n, k, v)
        db.commit()
    finally:
        db.close()


def sweep() -> None:
    db = new_session()
    try:
        placement.place_pending(db)
    finally:
        db.close()


def audit(action):
    db = new_session()
    try:
        return [e.detail | {"target": e.target} for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action))]
    finally:
        db.close()


def arg(r: Replica, flag: str) -> str:
    """A flag's value in the replica's command (llama.cpp's -c is the context times the concurrency, 2)."""
    args = r.spec["runtime"]["args"]
    return args[args.index(flag) + 1]


@pytest.fixture()
def two_cpus(owner):
    """Two CPU machines; cpu-a has much more free RAM, so binpack prefers cpu-b."""
    seed_catalog()
    a = make_node("cpu-a", features=MODELS)
    b = make_node("cpu-b", features=MODELS)
    cpu_row(a, vram_free=100 * GIB)
    cpu_row(b, vram_free=20 * GIB)
    return owner, a, b


# ------------------------------------------------------------------ editing (#26)
def test_fy_edit_001_a_change_restarts_the_replica_with_the_new_settings(two_cpus):
    """FY-EDIT-001: PATCH with a new context length checks it, saves the parameters and the estimate,
    restarts the only replica (new generation, the same machine, the new value in its command), and
    is audited and recorded as an event."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    before = rows("chat")[0]
    ready("chat", 0)
    r = c.patch(f"/api/deployments/{d['id']}", json={"params": {"context": 2048}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restart"] == "all" and body["changes"] == {"context": {"before": 4096, "after": 2048}}
    assert body["deployment"]["params"]["context"] == 2048
    assert body["deployment"]["estimate"]["amount"] < d["estimate"]["amount"]
    after = rows("chat")[0]
    assert after.generation == before.generation + 1 and after.node_id == before.node_id
    assert arg(after, "-c") == "4096" and after.state == "pending"
    assert after.uid != before.uid  # a new id: the agent replaces it, with its new internal key
    assert workload("chat").generation == 2
    events = [e["message"] for e in c.get(f"/api/deployments/{d['id']}").json()["events"]]
    assert any(m.startswith("Changed by owner: context 4096 → 2048.") for m in events)
    assert any("placed again, where it ran before, on" in m for m in events)
    [rec] = audit("deployment.changed")
    assert rec["target"] == "chat" and rec["restart"] == "all" and rec["changes"]["context"]["after"] == 2048


def test_fy_edit_002_dry_run_and_no_change_save_nothing(two_cpus):
    """FY-EDIT-002: dry_run answers the estimate, where it would go, what changes and what restarts,
    and saves nothing; a PATCH that changes nothing restarts nothing and is not audited."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    r = c.patch(f"/api/deployments/{d['id']}", json={"params": {"context": 2048}, "dry_run": True}).json()
    assert r["restart"] == "all" and r["changes"]["context"]["after"] == 2048 and r["placement"]["fits"] is True
    assert r["placement"]["placed"][0]["previous"] is True and r["estimate"]["amount"] < d["estimate"]["amount"]
    assert "deployment" not in r
    assert workload("chat").generation == 1 and arg(rows("chat")[0], "-c") == "8192"
    same = c.patch(f"/api/deployments/{d['id']}", json={"params": {"context": 4096}}).json()
    assert same["changes"] == {} and workload("chat").generation == 1 and audit("deployment.changed") == []


def test_fy_edit_003_refusals_like_a_deploy(two_cpus, owner):
    """FY-EDIT-003: the new estimate must fit (409 does_not_fit, nothing saved) unless wait; unknown
    parameters, the model or recipe, and people without the operator role are refused; a stopped
    deployment is saved without a fit check and its settings apply when it starts."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    huge = {"params": {"vram": "500Gi"}}
    r = c.patch(path, json=huge)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "does_not_fit"
    assert "of RAM" in r.json()["detail"]["message"] and workload("chat").generation == 1
    assert c.patch(path, json={"params": {"dance": 1}}).json()["detail"]["code"] == "invalid_params"
    assert c.patch(path, json={"params": {"context": 1}}).json()["detail"]["code"] == "invalid_params"
    assert c.patch(path, json={"model": "qwen3-4b"}).status_code == 422
    assert c.patch(path, json={"machine_id": 999}).json()["detail"]["code"] == "machine_not_found"
    add_user(owner, "ursula", role="user")
    assert sign_in(owner, "ursula", PW).patch(path, json={"params": {"context": 2048}}).status_code == 403
    r = c.patch(path, json={**huge, "wait": True})
    assert r.status_code == 200 and r.json()["placement"]["fits"] is False
    rep = rows("chat")[0]
    assert rep.node_id is None and rep.reason.startswith("Cannot be placed")
    c.post(f"{path}/stop")
    r = c.patch(path, json={"params": {"vram": "900Gi"}})
    assert r.status_code == 200 and r.json()["restart"] == "on_start" and r.json()["placement"]["fits"] is False
    assert workload("chat").desired_state == "stopped"


def test_fy_edit_004_several_replicas_restart_one_at_a_time(two_cpus):
    """FY-EDIT-004: with two replicas, an edit restarts replica 0 only; replica 1 keeps serving the old
    settings until replica 0 is ready on the new ones, then restarts; each goes back to its machine."""
    c, a, b = two_cpus
    d = deploy(c, "chat", replicas=2, **CPU_BODY)["deployment"]
    first = {r.ordinal: r.node_id for r in rows("chat")}
    assert set(first.values()) == {a, b}
    ready("chat", 0)
    ready("chat", 1)
    r = c.patch(f"/api/deployments/{d['id']}", json={"params": {"context": 2048}}).json()
    assert r["restart"] == "rolling"
    r0, r1 = rows("chat")
    assert (r0.generation, r0.state) == (2, "pending") and (r1.generation, r1.state) == (1, "healthy")
    assert arg(r1, "-c") == "8192" and arg(r0, "-c") == "4096"
    roll = c.get(f"/api/deployments/{d['id']}").json()["deployment"]["rollout"]
    assert roll == {"generation": 2, "updated": 1, "total": 2, "next": 1}
    sweep()
    assert rows("chat")[1].generation == 1  # replica 0 is not ready yet: replica 1 waits
    set_replica("chat", 0, state="starting")
    sweep()
    assert rows("chat")[1].generation == 1
    ready("chat", 0)
    sweep()
    r0, r1 = rows("chat")
    assert r1.generation == 2 and arg(r1, "-c") == "4096"
    assert {0: r0.node_id, 1: r1.node_id} == first
    ready("chat", 1)
    sweep()
    assert c.get(f"/api/deployments/{d['id']}").json()["deployment"]["rollout"] is None


def test_fy_edit_005_a_restart_goes_back_to_the_previous_machine(two_cpus):
    """FY-EDIT-005: a stopped, unpinned deployment that ran on cpu-a starts on cpu-a again, although
    binpack now prefers cpu-b; when cpu-a no longer takes it, it goes elsewhere."""
    c, a, b = two_cpus
    node_field(b, status="offline")
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    assert rows("chat")[0].node_id == a
    node_field(b, status="online")
    assert c.post("/api/deployments/estimate", json={"name": "x", **CPU_BODY}).json()["placement"]["placed"][0][
        "machine"] == "cpu-b"  # a new deployment would go to the tighter machine
    path = f"/api/deployments/{d['id']}"
    c.post(f"{path}/stop")
    set_replica("chat", 0, state="stopped")
    c.post(f"{path}/start")
    assert rows("chat")[0].node_id == a
    events = [e["message"] for e in c.get(path).json()["events"]]
    assert any("placed again, where it ran before, on cpu-a" in m for m in events)
    c.post(f"{path}/stop")
    set_replica("chat", 0, state="stopped")
    cpu_row(a, vram_free=GIB // 2)  # no room on cpu-a any more
    c.post(f"{path}/start")
    assert rows("chat")[0].node_id == b


def test_fy_edit_006_pin_move_and_unpin(two_cpus):
    """FY-EDIT-006: choosing a machine pins the deployment and moves its replica there (a restart);
    going back to automatic unpins it without a restart."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    assert rows("chat")[0].node_id == b and d["pinned"] is None
    path = f"/api/deployments/{d['id']}"
    r = c.patch(path, json={"machine_id": a}).json()
    assert r["restart"] == "all" and r["changes"] == {"machine_id": {"before": None, "after": a}}
    assert r["deployment"]["pinned"] == {"id": a, "name": "cpu-a"} and rows("chat")[0].node_id == a
    events = [e["message"] for e in c.get(path).json()["events"]]
    assert any(m.startswith("Changed by owner: machine automatic → cpu-a.") for m in events)
    r = c.patch(path, json={"machine_id": a}).json()
    assert r["changes"] == {}
    r = c.patch(path, json={"machine_id": None}).json()
    assert r["restart"] == "none" and r["deployment"]["pinned"] is None
    assert rows("chat")[0].node_id == a and workload("chat").generation == 2


def test_fy_edit_007_more_or_fewer_replicas(two_cpus):
    """FY-EDIT-007: more replicas are added without restarting the running one; fewer remove the
    highest-numbered ones."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    ready("chat", 0)
    r = c.patch(path, json={"replicas": 3}).json()
    assert r["restart"] == "none" and r["changes"] == {"replicas": {"before": 1, "after": 3}}
    reps = rows("chat")
    assert [x.ordinal for x in reps] == [0, 1, 2] and reps[0].state == "healthy" and reps[0].generation == 1
    assert reps[1].node_id is not None and workload("chat").spec["replicas"] == 3
    r = c.patch(path, json={"replicas": 1}).json()
    assert [x.ordinal for x in rows("chat")] == [0] and rows("chat")[0].state == "healthy"
    assert len(r["deployment"]["workload"]["replica_list"]) == 1


def test_fy_edit_008_its_own_memory_counts_as_free_for_the_restart(owner):
    """FY-EDIT-008: an exclusive replica on #4 uses its memory there (the agent's numbers show it); a
    bigger context that fits in the card once the old replica stops is accepted and placed on #4."""
    seed_catalog()
    a100 = make_node("a100", allowlist=[4], features=MODELS)
    d = deploy(owner, "chat", "qwen3-4b", "vllm")["deployment"]
    used = rows("chat")[0].vram
    ready("chat", 0)
    db = new_session()
    try:
        acc = db.scalar(select(Accelerator).where(Accelerator.node_id == a100, Accelerator.index == 4,
                                                  Accelerator.vendor == "nvidia"))
        free_before = acc.vram_free
        acc.vram_free, acc.updated_at = free_before - used, utcnow()
        db.commit()
    finally:
        db.close()
    room = free_before - 512 * 1024**2  # the card's free memory once the replica stops, less the headroom
    est = owner.post("/api/deployments/estimate", json={"name": "x", "model": "qwen3-4b", "recipe": "vllm",
                                                        "params": {"context": 40960}}).json()["estimate"]
    assert free_before - used - 512 * 1024**2 < est["amount"] <= room  # only fits with its own memory back
    r = owner.patch(f"/api/deployments/{d['id']}", json={"params": {"context": 40960}})
    assert r.status_code == 200, r.text
    rep = rows("chat")[0]
    assert rep.node_id == a100 and rep.gpus[0]["index"] == 4 and rep.generation == 2 and rep.vram == est["amount"]


# ------------------------------------------------------------------ sharing (#27)
def test_fy_shr_001_the_preview_points_out_sharing_for_a_small_exclusive_model(owner):
    """FY-SHR-001: an exclusive deployment that needs half its card or less gets a hint naming both
    sizes; none when it shares, none when it needs more than half."""
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    body = {"name": "chat", "model": "qwen3-4b", "recipe": "vllm"}
    hint = owner.post("/api/deployments/estimate", json=body).json()["placement"]["share_hint"]
    assert hint["text"] == ("This model needs 14.38 GiB of an 80.00 GiB card; with Share VRAM the rest can hold other "
                            "models. They will share compute.")
    assert hint["machine"] == "a100" and hint["gpu"] == 4 and hint["card_total"] == 80 * GIB
    shared = owner.post("/api/deployments/estimate", json={**body, "params": {"share": "vram"}}).json()
    assert shared["placement"]["share_hint"] is None
    db = new_session()
    try:
        for acc in db.scalars(select(Accelerator).where(Accelerator.vendor == "nvidia")):
            acc.vram_total = 24 * GIB  # a 24 GiB card: 14.38 GiB is more than half
        db.commit()
    finally:
        db.close()
    assert owner.post("/api/deployments/estimate", json=body).json()["placement"]["share_hint"] is None


def test_fy_shr_002_a_refusal_says_it_fits_if_both_share(owner):
    """FY-SHR-002: #4 is held by an exclusive replica and has the memory: the refusal says it fits if
    both share VRAM and names the holder; when only one side needs to change it says which; when the
    memory is not there either, it says nothing about sharing."""
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    deploy(owner, "chat", "qwen3-4b", "vllm")
    body = {"name": "chat2", "model": "qwen3-4b", "recipe": "vllm"}
    r = owner.post("/api/deployments", json=body)
    assert r.status_code == 409
    v = next(m for m in r.json()["detail"]["machines"] if m["name"] == "a100")
    assert "in use: #4 (chat)" in v["reason"] and "fits if both share VRAM: #4 has" in v["reason"]
    assert "held by chat" in v["reason"] and "fits if both share VRAM" in r.json()["detail"]["message"]
    assert v["share_fits"] == {"gpus": [4], "holders": ["chat"], "exclusive": ["chat"], "both": True}
    assert v["short_by"] is None  # memory is not what is missing
    r = owner.post("/api/deployments/estimate", json={**body, "params": {"share": "vram"}}).json()
    v = next(m for m in r["placement"]["machines"] if m["name"] == "a100")
    assert "fits if chat shares VRAM too" in v["reason"] and v["share_fits"]["both"] is False
    big = owner.post("/api/deployments/estimate", json={**body, "params": {"context": 40960}}).json()
    v = next(m for m in big["placement"]["machines"] if m["name"] == "a100")
    assert "share_fits" not in v and "fits if" not in v["reason"]
    owner.delete(f"/api/deployments/{owner.get('/api/deployments').json()['deployments'][0]['id']}")
    deploy(owner, "embed", "qwen3-embedding-0.6b", "vllm-pooling")  # shares by default
    v = next(m for m in owner.post("/api/deployments/estimate", json=body).json()["placement"]["machines"]
             if m["name"] == "a100")
    assert "fits if this deployment shares VRAM: #4 has" in v["reason"] and "shared by embed" in v["reason"]


def test_fy_shr_003_the_advisor_notes_an_exclusive_replica_on_a_large_card(owner):
    """FY-SHR-003: a running exclusive replica reserving under half of a 24 GiB-or-larger card gives a
    snoozable note linking to the edit page; not while it starts, not when it shares, not on a small
    card."""
    from forgeyard import advisor
    from forgeyard.config import get_settings

    def found():
        db = new_session()
        try:
            return [f for f in advisor.run(db, get_settings(), strict=True) if f.id == "deployment-could-share"]
        finally:
            db.close()

    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    d = deploy(owner, "chat", "qwen3-4b", "vllm")["deployment"]
    assert found() == []  # pending
    ready("chat", 0)
    [f] = found()
    assert f.severity == "info" and f.snoozable and f.scope == f"deployment:{d['id']}"
    assert f.fix_url == f"/serving/{d['id']}/edit" and "GPU #4 on a100" in f.detail
    assert "14.4 GiB of the 80.0 GiB" in f.detail and f"deployment:{d['id']}" in f.pages
    db = new_session()
    try:
        for acc in db.scalars(select(Accelerator).where(Accelerator.vendor == "nvidia")):
            acc.vram_total = 16 * GIB
        db.commit()
    finally:
        db.close()
    assert found() == []
    card(1, 4, vram_total=28 * GIB)  # large enough, but 14.38 GiB is more than half of it
    assert found() == []
    card(1, 4, vram_total=20 * GIB)
    set_replica("chat", 0, vram=8 * GIB)  # under half, but the card is not a large one
    assert found() == []
    set_replica("chat", 0, vram=15443688448)
    card(1, 4, vram_total=80 * GIB)
    owner.patch(f"/api/deployments/{d['id']}", json={"params": {"share": "vram"}})
    ready("chat", 0)
    assert found() == []


# ------------------------------------------------------------------ never picked up (#29)
@pytest.fixture()
def connected(monkeypatch):
    nodes: set[int] = set()
    monkeypatch.setattr(hub, "connected", lambda node_id: node_id in nodes)
    return nodes


def test_fy_pick_001_a_replica_its_agent_never_takes_says_so(two_cpus, connected):
    """FY-PICK-001: placed on a connected machine and not reported by its agent for 2 minutes: the
    replica says which agent, since when, and to update it (an older agent) or restart it (the same
    version); nothing before 2 minutes, when offline, once the agent reported it, or after the agent
    reconnected recently."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    node_field(b, agent_version="1.2.0", connected_at=utcnow() - timedelta(hours=1))
    connected.add(b)

    def stuck():
        return c.get(path).json()["deployment"]["workload"]["replica_list"][0]["not_picked_up"]

    assert stuck() is None  # just placed
    set_replica("chat", 0, updated_at=utcnow() - timedelta(minutes=3))
    assert stuck() == f"cpu-b's agent (1.2.0) has not taken this replica for 3 minutes; update the agent (the server is {__version__})"
    wid = c.get(path).json()["deployment"]["workload"]["id"]
    assert c.get(f"/api/workloads/{wid}").json()["workload"]["replica_list"][0]["not_picked_up"] == stuck()
    node_field(b, agent_version=__version__)
    assert stuck() == f"cpu-b's agent ({__version__}) has not taken this replica for 3 minutes; restart the agent on cpu-b and look at its log"
    node_field(b, connected_at=utcnow() - timedelta(seconds=30))
    assert stuck() is None  # it reconnected: it gets its work list now
    node_field(b, connected_at=utcnow() - timedelta(hours=1))
    connected.discard(b)
    assert stuck() is None
    connected.add(b)
    set_replica("chat", 0, state="starting", started_at=utcnow())
    assert stuck() is None


def test_fy_pick_002_the_rules_of_not_picked_up():
    """FY-PICK-002: only a pending replica of a running workload on a machine, with no reason of its
    own (or the stale "waiting for … to come online"), that its agent never reported."""
    now = utcnow()

    class N:
        id, name, agent_version, protocol, connected_at = 3, "gpu-01", "1.3.0", 1, None

    class W:
        desired_state = "running"

    class R:
        node, final, state, started_at, download, reason, workload = N(), False, "pending", None, None, None, W()
        updated_at = now - timedelta(minutes=2)

    yes = lambda _id: True  # noqa: E731
    r = R()
    assert wstate.not_picked_up(r, now, yes).startswith("gpu-01's agent (1.3.0) has not taken this replica for 2 minutes")
    r.updated_at = now - timedelta(minutes=1, seconds=59)
    assert wstate.not_picked_up(r, now, yes) is None
    r.updated_at = now - timedelta(minutes=10)
    r.reason = "waiting for gpu-01 to come online"
    assert wstate.not_picked_up(r, now, yes) is not None
    r.reason = "downloading the model: 1 of 2 GiB"
    assert wstate.not_picked_up(r, now, yes) is None
    r.reason = None
    r.download = {"phase": "model", "done": 1, "total": 2}
    assert wstate.not_picked_up(r, now, yes) is None
    r.download = None
    r.workload.desired_state = "stopped"
    assert wstate.not_picked_up(r, now, yes) is None
    r.workload.desired_state = "running"
    r.final = True
    assert wstate.not_picked_up(r, now, yes) is None
    r.final = False
    r.started_at = now - timedelta(hours=1)  # its agent started it once: not a replica it never took
    assert wstate.not_picked_up(r, now, yes) is None
    r.started_at = None
    r.node.protocol = 0
    assert "update the agent" in wstate.not_picked_up(r, now, yes)
    r.node.protocol, r.node.agent_version = 1, None
    assert wstate.not_picked_up(r, now, yes).startswith("gpu-01's agent (unknown version)")
    assert wstate.not_picked_up(r, now, lambda _id: False) is None


def test_fy_edit_009_a_plain_workload_also_restarts_where_it_ran(owner):
    """FY-EDIT-009: a service workload started again goes back to its machine, not the first by name."""
    from tests.workload_helpers import create_workload, service, start_workload, stop_workload

    a = make_node("box-a")
    b = make_node("box-b")
    node_field(a, status="offline")
    w = create_workload(owner, service("web"))["workload"]
    assert rows("web")[0].node_id == b
    node_field(a, status="online")
    stop_workload(w["id"])
    set_replica("web", 0, state="stopped")
    start_workload(w["id"])
    assert rows("web")[0].node_id == b
    events = [e["message"] for e in owner.get(f"/api/workloads/{w['id']}").json()["events"]]
    assert "Replica 0 placed again, where it ran before, on box-b." in events


def test_fy_edit_010_a_parameter_the_recipe_dropped_does_not_block_a_change(two_cpus):
    """FY-EDIT-010: a stored parameter that the (updated) recipe no longer has is left out, not refused."""
    from forgeyard.db import Deployment

    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    db = new_session()
    try:
        row = db.get(Deployment, d["id"])
        row.params = {**row.params, "flash_attn": "on"}
        db.commit()
    finally:
        db.close()
    r = c.patch(f"/api/deployments/{d['id']}", json={"params": {"context": 2048}})
    assert r.status_code == 200, r.text
    assert "flash_attn" not in r.json()["deployment"]["params"] and "flash_attn" in r.json()["changes"]
    path = f"/api/deployments/{d['id']}"
    assert c.patch(path, json={"params": {"vram": "1Gi"}}).json()["deployment"]["params"]["vram"] == GIB
    back = c.patch(path, json={"params": {"vram": None}}).json()
    assert "vram" not in back["deployment"]["params"] and back["changes"]["vram"] == {"before": GIB, "after": None}
    default = c.patch(path, json={"params": {"context": None}}).json()  # back to the recipe's default
    assert default["deployment"]["params"]["context"] == 4096 and default["changes"]["context"]["before"] == 2048


def test_fy_edit_011_an_edit_restarts_a_replica_on_its_own_machine(two_cpus):
    """FY-EDIT-011: a replica that ran on cpu-a restarts there after an edit, although binpack now
    prefers cpu-b."""
    c, a, b = two_cpus
    node_field(b, status="offline")
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    node_field(b, status="online")
    ready("chat", 0)
    assert rows("chat")[0].node_id == a
    c.patch(f"/api/deployments/{d['id']}", json={"params": {"context": 2048}})
    assert rows("chat")[0].node_id == a and rows("chat")[0].generation == 2


def test_fy_shr_004_sharing_must_give_every_gpu_a_replica_needs():
    """FY-SHR-004: a replica that needs two GPUs, with one roomy card held exclusively: no "fits if
    shared", since one card is not enough."""
    from forgeyard.models import scheduler
    from forgeyard.workloads.placement import Hold

    class Ctx:
        amount, gpus, share = 10 * GIB, 2, "exclusive"

    class A:
        def __init__(self, index, key):
            self.index, self.key = index, key

    held, small = A(0, "k0"), A(1, "k1")
    cards = {"k0": [Hold("chat", "exclusive", 5 * GIB, False)]}
    assert scheduler._share_fits(Ctx(), [(40 * GIB, held), (2 * GIB, small)], cards) is None
    Ctx.gpus = 1
    assert scheduler._share_fits(Ctx(), [(40 * GIB, held), (2 * GIB, small)], cards)[0]["holders"] == ["chat"]
