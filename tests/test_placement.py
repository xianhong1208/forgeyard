"""Placement and the per-machine GPU allowlist (FY-PLACE-*, FY-ALLOW-*)."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from forgeyard.agent.inventory.amd import ROCM_SMI, AmdCollector
from forgeyard.db import Accelerator, AuditEvent, Node, Replica, get_db, new_session
from forgeyard.machines import store
from forgeyard.machines.messages import InventoryIn
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
    """FY-PLACE-001: one allowed card (#4): every GPU workload gets #4 and nothing else,
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


# ------------------------------------------------------------------ a card's key changes (AMD, 1.1.0 -> later)
REAL = Path(__file__).with_name("fixtures") / "strix-halo-gfx1151-amdsmi26"
NEW_KEY = "amd:0000:c6:00.0"


def _strix_as_110_saw_it(name="strix") -> int:
    """The real Strix Halo as the 1.1.0 agent reported it: key "amd:0x0" (rocm-smi's Unique ID)."""
    data = json.loads((REAL / "agent-1.1.0-inventory.json").read_text())
    db = new_session()
    try:
        _, node = store.create_node(db, name=name, hostname=name, agent_version="1.1.0", protocol=1, ip="10.0.0.9")
        store.apply_inventory(db, node, InventoryIn.model_validate({"accelerators": data["accelerators"],
                                                                    "collectors": data["collectors"]}))
        node.status, node.features, node.runtime_info = "online", ["workloads"], PROCESS_OK
        node.runtimes = ["process", "container"]
        db.commit()
        return node.id
    finally:
        db.close()


def _strix_now(tmp_path) -> list[dict]:
    """What this agent reports on the same machine (its rocm-smi reading), plus the old CPU row."""
    from tests.fakes import fake_runner, make_kfd

    kfd = make_kfd(tmp_path / "nodes", json.loads((REAL / "kfd.json").read_text())["nodes"])
    run = fake_runner({" ".join(ROCM_SMI): (0, (REAL / "rocm-smi.json").read_text(), "")})
    r = AmdCollector(run=run, which=lambda n: "/x" if n == "rocm-smi" else None, kfd_root=kfd, rocm_root=[],
                     cpuinfo=REAL / "cpuinfo.txt", importer=_no_amdsmi).inventory()
    old = json.loads((REAL / "agent-1.1.0-inventory.json").read_text())["accelerators"]
    return r.accelerators + [a for a in old if a["vendor"] == "cpu"]


def _no_amdsmi(name):
    raise ImportError(f"No module named {name!r}")


def _report(nid, accelerators):
    db = new_session()
    try:
        node = db.get(Node, nid)
        store.apply_inventory(db, node, InventoryIn.model_validate({"accelerators": accelerators}))
        db.commit()
    finally:
        db.close()


def test_fy_allow_005_a_card_keeps_its_allowlist_entry_and_claims_under_a_new_key(owner, tmp_path):
    """FY-ALLOW-005: the 1.1.0 agent keyed the real Strix Halo "amd:0x0"; this one keys it by its PCI
    address and names the old key in detail.aliases. The row, the allowlist entry and the running
    replica's claim follow; it is audited once."""
    nid = _strix_as_110_saw_it()
    assert owner.put(f"/api/machines/{nid}/gpu-allowlist", json={"keys": ["amd:0x0"]}).status_code == 200
    busy = create(owner, task("busy", machine_id=nid, resources={"gpus": {"count": 1}}))["workload"]
    assert [g["key"] for g in reps(owner, busy["id"])[0]["gpus"]] == ["amd:0x0"]
    db = next(get_db())
    row_id = db.scalar(select(Accelerator.id).where(Accelerator.node_id == nid, Accelerator.key == "amd:0x0"))

    new = _strix_now(tmp_path)
    assert new[0]["key"] == NEW_KEY and "amd:0x0" in new[0]["detail"]["aliases"]
    _report(nid, new)
    m = owner.get("/api/machines").json()["machines"][0]
    (gpu,) = [a for a in m["accelerators"] if a["vendor"] == "amd"]
    assert (gpu["key"], gpu["model"], gpu["driver"]) == (NEW_KEY, "AMD Radeon 8060S", None)
    assert m["gpu_allowlist"] == [NEW_KEY] and gpu["allowed"] and gpu["assigned_to"] == "busy"
    assert [g["key"] for g in reps(owner, busy["id"])[0]["gpus"]] == [NEW_KEY]
    db.expire_all()
    assert db.scalar(select(Accelerator.id).where(Accelerator.node_id == nid, Accelerator.key == NEW_KEY)) == row_id
    ev = db.scalars(select(AuditEvent).where(AuditEvent.action == "machine.gpu_keys_changed")).one()
    assert ev.target == "strix" and ev.detail["keys"] == {"amd:0x0": NEW_KEY}
    # the card is still taken: a second GPU task waits
    second = create(owner, task("second", machine_id=nid, resources={"gpus": {"count": 1}}))["workload"]
    assert second["replica_list"][0]["machine"] is None
    _report(nid, new)  # the next report changes nothing
    assert len(db.scalars(select(AuditEvent).where(AuditEvent.action == "machine.gpu_keys_changed")).all()) == 1


def test_fy_allow_006_anything_ambiguous_fails_closed(owner, tmp_path):
    """FY-ALLOW-006: no alias (a swapped card), an alias two cards claim, an alias that is still
    reported, or one of another vendor: the old key is not carried over, so the allowlist entry
    no longer matches a card and the card is not used."""
    new = _strix_now(tmp_path)
    gpu, cpu = new[0], new[1]

    def fresh(name):
        nid = _strix_as_110_saw_it(name)
        db = next(get_db())
        db.get(Node, nid).gpu_allowlist = ["amd:0x0"]
        db.commit()
        return nid

    def state(nid):
        db = next(get_db())
        db.expire_all()
        node = db.get(Node, nid)
        return node.gpu_allowlist, sorted(a.key for a in node.accelerators if a.vendor == "amd")

    swapped = dict(gpu, detail={k: v for k, v in gpu["detail"].items() if k != "aliases"})
    nid = fresh("swapped")
    _report(nid, [swapped, cpu])
    assert state(nid) == (["amd:0x0"], [NEW_KEY])
    twin = dict(gpu, key="amd:0000:c7:00.0", index=1)
    nid = fresh("twins")
    _report(nid, [gpu, twin, cpu])
    assert state(nid) == (["amd:0x0"], [NEW_KEY, "amd:0000:c7:00.0"])
    nid = fresh("still-there")
    old = json.loads((REAL / "agent-1.1.0-inventory.json").read_text())["accelerators"][0]
    _report(nid, [old, dict(gpu, index=1), cpu])
    assert state(nid) == (["amd:0x0"], [NEW_KEY, "amd:0x0"])
    nid = fresh("vendor")
    _report(nid, [dict(gpu, vendor="nvidia", key="nvidia:GPU-x"), cpu])
    assert state(nid) == (["amd:0x0"], [])
    db = next(get_db())
    assert db.scalars(select(AuditEvent).where(AuditEvent.action == "machine.gpu_keys_changed")).all() == []
