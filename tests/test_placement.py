"""Placement and the per-machine GPU allowlist (FY-PLACE-*, FY-ALLOW-*)."""

import pytest
from sqlalchemy import select

from forgeyard.db import AuditEvent, Node, Replica, get_db
from tests.conftest import add_user, sign_in
from tests.workload_helpers import PROCESS_OK, make_node, task


def create(c, spec, status=201):
    r = c.post("/api/workloads", json=spec)
    assert r.status_code == status, r.text
    return r.json()


def reps(c, wid):
    return c.get(f"/api/workloads/{wid}").json()["workload"]["replica_list"]


def gpu_indices(rep):
    return [g["index"] for g in rep["gpus"]]


# ------------------------------------------------------------------ allowlist
def test_fy_place_001_gpus_only_from_the_allowlist(owner):
    """FY-PLACE-001: GPU #4 only (Tony's shared host): every GPU workload gets #4 and nothing else,
    a second one waits, and asking for another card by index is refused."""
    nid = make_node("a100", allowlist=[4])
    w = create(owner, task("one", machine_id=nid, resources={"gpus": {"count": 1}}))["workload"]
    assert gpu_indices(w["replica_list"][0]) == [4]
    w2 = create(owner, task("two", resources={"gpus": {"count": 1}}))["workload"]
    rep = w2["replica_list"][0]
    assert rep["machine"] is None and rep["gpus"] == []
    assert "needs 1 free GPU, 0 free of the allowed (#4)" in rep["reason"]
    r = owner.post("/api/workloads", json=task("three", machine_id=nid, resources={"gpus": {"indices": [3]}}))
    assert r.status_code == 422 and "GPU #3 is not allowed for Forgeyard here (allowed: #4)" in r.json()["detail"]["message"]
    r = owner.post("/api/workloads", json=task("four", machine_id=nid, resources={"gpus": {"count": 2}}))
    assert r.status_code == 201
    assert gpu_indices(r.json()["workload"]["replica_list"][0]) == []  # waits; never takes a card outside


def test_fy_place_002_empty_allowlist_means_no_gpu(owner):
    """FY-PLACE-002"""
    nid = make_node("a100", allowlist=[])
    rep = create(owner, task(machine_id=nid, resources={"gpus": {"count": 1}}))["workload"]["replica_list"][0]
    assert rep["machine"] is None and "no GPU is allowed for Forgeyard here" in rep["reason"]
    # CPU work still goes there.
    rep = create(owner, task("cpu", machine_id=nid))["workload"]["replica_list"][0]
    assert rep["machine"]["name"] == "a100" and rep["gpus"] == []


def test_fy_place_003_no_allowlist_means_all_and_cards_are_exclusive(owner):
    """FY-PLACE-003"""
    make_node("a100")
    got = []
    for i in range(8):
        got += gpu_indices(create(owner, task(f"t{i}", resources={"gpus": {"count": 1}}))["workload"]["replica_list"][0])
    assert sorted(got) == list(range(8))
    rep = create(owner, task("ninth", resources={"gpus": {"count": 1}}))["workload"]["replica_list"][0]
    assert rep["machine"] is None and "0 free" in rep["reason"]


def test_fy_place_004_explicit_indices(owner):
    """FY-PLACE-004"""
    nid = make_node("a100")
    rep = create(owner, task(machine_id=nid, resources={"gpus": {"indices": [2, 5]}}))["workload"]["replica_list"][0]
    assert gpu_indices(rep) == [2, 5]
    r = owner.post("/api/workloads", json=task("x", machine_id=nid, resources={"gpus": {"indices": [9]}}))
    assert r.status_code == 422 and "has no GPU #9" in r.json()["detail"]["message"]
    rep = create(owner, task("y", machine_id=nid, resources={"gpus": {"indices": [5]}}))["workload"]["replica_list"][0]
    assert rep["machine"] is None and "GPU #5 is in use by job" in rep["reason"]


def test_fy_place_005_first_fit_over_online_machines(owner):
    """FY-PLACE-005: "any" skips offline machines and machines that cannot run it."""
    make_node("a-off", status="offline")
    make_node("b-old", features=())
    make_node("c-nocontainer", "amd-gfx1151-apu", runtime_info={"process": PROCESS_OK["process"],
                                                               "container": {"ok": False, "reason": "docker not found"}})
    make_node("d-ok", "nvidia-rtx4090x2")
    w = create(owner, {"name": "ctr", "kind": "task", "runtime": {"type": "container", "image": "alpine"}})["workload"]
    assert w["replica_list"][0]["machine"]["name"] == "d-ok"
    w = create(owner, task("p", replicas=3))["workload"]
    assert [r["machine"]["name"] for r in w["replica_list"]] == ["c-nocontainer"] * 3


def test_fy_place_006_cpu_and_memory_are_counted(owner):
    """FY-PLACE-006: the 4090 fixture has 32 threads and 64 GiB."""
    make_node("small", "nvidia-rtx4090x2")
    create(owner, task("a", resources={"cpu": 30}))
    rep = create(owner, task("b", resources={"cpu": 4}))["workload"]["replica_list"][0]
    assert rep["machine"] is None and "needs 4 CPU cores, 2 free of 32" in rep["reason"]
    rep = create(owner, task("c", resources={"memory": "1Ti"}))["workload"]["replica_list"][0]
    assert rep["machine"] is None and "memory" in rep["reason"]


def test_fy_place_007_pinned_machine_problems_are_refused_up_front(owner):
    """FY-PLACE-007"""
    old = make_node("old", features=())
    nocap = make_node("nocap", runtime_info={"process": {"ok": False, "reason": "the agent runs as root and no "
                                                         "--workload-user is set"}, "container": {"ok": False}})
    nonv = make_node("nonv", runtime_info={"process": PROCESS_OK["process"],
                                           "container": {"ok": True, "engine": "docker", "nvidia": False}})
    r = owner.post("/api/workloads", json=task(machine_id=old))
    assert r.status_code == 422 and "does not run workloads yet" in r.json()["detail"]["message"]
    r = owner.post("/api/workloads", json=task(machine_id=nocap))
    assert r.status_code == 422 and "--workload-user" in r.json()["detail"]["message"]
    r = owner.post("/api/workloads", json={"name": "g", "kind": "task", "machine_id": nonv,
                                           "runtime": {"type": "container", "image": "nvidia/cuda:12.4.1-base"},
                                           "resources": {"gpus": {"count": 1, "vendor": "nvidia"}}})
    assert r.status_code == 422 and "cannot pass NVIDIA GPUs" in r.json()["detail"]["message"]
    assert owner.post("/api/workloads", json=task(machine_id=999)).status_code == 404


def test_fy_place_008_waiting_replicas_are_placed_when_room_appears(owner):
    """FY-PLACE-008: a finished task frees its card; the sweep places what waits."""
    from forgeyard.workloads.placement import place_pending

    nid = make_node("a100", allowlist=[4])
    first = create(owner, task("first", resources={"gpus": {"count": 1}}))["workload"]
    second = create(owner, task("second", resources={"gpus": {"count": 1}}))["workload"]
    assert second["replica_list"][0]["machine"] is None
    db = next(get_db())
    db.scalar(select(Replica).where(Replica.workload_id == first["id"])).final = True
    db.commit()
    assert place_pending(db) == {nid}
    assert gpu_indices(reps(owner, second["id"])[0]) == [4]


# ------------------------------------------------------------------ the allowlist editor
def test_fy_allow_001_admin_sets_the_allowlist_and_it_is_audited(owner):
    """FY-ALLOW-001"""
    nid = make_node("a100")
    m = owner.get("/api/machines").json()["machines"][0]
    assert m["gpu_allowlist"] is None and all(a["allowed"] for a in m["accelerators"] if a["vendor"] != "cpu")
    keys = {a["index"]: a["key"] for a in m["accelerators"] if a["vendor"] == "nvidia"}
    r = owner.put(f"/api/machines/{nid}/gpu-allowlist", json={"keys": [keys[4]]})
    assert r.status_code == 200, r.text
    m = r.json()["machine"]
    assert m["gpu_allowlist"] == [keys[4]]
    assert [a["index"] for a in m["accelerators"] if a["allowed"]] == [4]
    assert not next(a for a in m["accelerators"] if a["vendor"] == "cpu")["allowed"]
    db = next(get_db())
    ev = db.scalars(select(AuditEvent).where(AuditEvent.action == "machine.gpu_allowlist_changed")).one()
    assert ev.target == "a100" and ev.detail["before"] == "all" and ev.detail["after"] == ["#4"]
    r = owner.put(f"/api/machines/{nid}/gpu-allowlist", json={"keys": None})
    assert r.json()["machine"]["gpu_allowlist"] is None


def test_fy_allow_002_unknown_keys_and_cpu_are_refused(owner):
    """FY-ALLOW-002"""
    nid = make_node("a100")
    assert owner.put(f"/api/machines/{nid}/gpu-allowlist", json={"keys": ["nvidia:GPU-nope"]}).status_code == 422
    assert owner.put(f"/api/machines/{nid}/gpu-allowlist", json={"keys": ["cpu:0"]}).status_code == 422
    assert owner.put("/api/machines/999/gpu-allowlist", json={"keys": []}).status_code == 404


def test_fy_allow_003_a_card_in_use_cannot_be_taken_off(owner):
    """FY-ALLOW-003: nothing may keep running outside the allowlist."""
    nid = make_node("a100")
    create(owner, task("busy", machine_id=nid, resources={"gpus": {"indices": [3]}}))
    m = owner.get("/api/machines").json()["machines"][0]
    keys = {a["index"]: a["key"] for a in m["accelerators"] if a["vendor"] == "nvidia"}
    assert next(a for a in m["accelerators"] if a["index"] == 3 and a["vendor"] == "nvidia")["assigned_to"] == "busy"
    r = owner.put(f"/api/machines/{nid}/gpu-allowlist", json={"keys": [keys[4]]})
    assert r.status_code == 409 and "#3 (busy)" in r.json()["detail"]["message"]
    assert owner.put(f"/api/machines/{nid}/gpu-allowlist", json={"keys": [keys[3], keys[4]]}).status_code == 200


@pytest.mark.parametrize("role", ["operator", "user"])
def test_fy_allow_004_only_admins_change_it(owner, role):
    """FY-ALLOW-004"""
    nid = make_node("a100")
    add_user(owner, "bob", role=role)
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    r = bob.put(f"/api/machines/{nid}/gpu-allowlist", json={"keys": []})
    assert r.status_code == 403
    db = next(get_db())
    assert db.get(Node, nid).gpu_allowlist is None
