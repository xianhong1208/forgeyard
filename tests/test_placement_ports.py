"""A machine's host port range is a resource (1.4.2, #125; FY-PORT-*): the agent reports the range it publishes
replicas on, a replica takes one port of it per published port (two behind the agent's key guard), and a machine
without enough free ports is skipped like one without memory, with the reason in the plan. A replica the agent
could not start for want of a port is taken off the machine when the server's count agrees, and waits unplaced."""

from sqlalchemy import select

from forgeyard.agent.workloads.supervisor import Supervisor
from forgeyard.cli import _describe_runtimes
from forgeyard.db import Node, Replica, Workload, new_session
from forgeyard.machines import agent_api, store
from forgeyard.machines.messages import HelloIn, StatusIn
from forgeyard.workloads import placement
from tests.test_scheduler import deploy, reps, seed_catalog
from tests.workload_helpers import PROCESS_OK, make_node, service, task

GUARDED = ("workloads", "models", "replica_keys", "replica_guard", "workload_guard")
LLAMA = {"model": "qwen3-1.7b-gguf", "recipe": "llama-cpp", "params": {"gpus": 0, "threads": 2}}


def ranged(low, high):
    return {**PROCESS_OK, "ports": {"low": low, "high": high}}


def create(c, spec, status=201):
    r = c.post("/api/workloads", json=spec)
    assert r.status_code == status, r.text
    return r.json()["workload"]


def rows(name):
    db = new_session()
    try:
        return list(db.scalars(select(Replica).join(Workload).where(Workload.name == name).order_by(Replica.ordinal)))
    finally:
        db.close()


def report(node_id, r, state, reason, final=False):
    """The agent of node_id reports replica r, through the server's own status handler."""
    db = new_session()
    try:
        msg = StatusIn.model_validate({"v": 1, "type": "status", "replicas": [
            {"id": r.uid, "generation": r.generation, "state": state, "reason": reason, "final": final}]})
        return agent_api._status(db, node_id, msg)
    finally:
        db.close()


def set_range(node_id, low, high):
    db = new_session()
    try:
        node = db.get(Node, node_id)
        node.runtime_info = ranged(low, high)
        db.commit()
    finally:
        db.close()


def test_fy_port_001_the_agent_reports_its_range_and_the_server_keeps_only_a_valid_one(owner, tmp_path):
    """FY-PORT-001: the agent's runtime info carries its --port-range; the hello keeps it (and the server stores
    it) only when it is a valid range; `forgeyard agent runtimes` shows it."""
    sup = Supervisor(tmp_path, {}, port_range=(33000, 33002))
    assert sup.runtime_info()["ports"] == {"low": 33000, "high": 33002}
    base = {"v": 1, "type": "hello", "agent_version": "1.4.2", "inventory": {"accelerators": []}}
    hello = HelloIn.model_validate({**base, "runtime_info": {**PROCESS_OK, "ports": {"low": 33000, "high": 33002}}})
    assert hello.runtime_info["ports"] == {"low": 33000, "high": 33002} and hello.runtime_info["process"]["ok"]
    for bad in ({"low": 33002, "high": 33000}, {"low": 0, "high": 5}, {"low": 1, "high": 70000},
                {"low": "1", "high": 5}, {"low": True, "high": 5}, {"low": 1.5, "high": 5}, [1, 5], "1-5", None):
        assert "ports" not in HelloIn.model_validate({**base, "runtime_info": {"ports": bad}}).runtime_info, bad
    db = new_session()
    try:
        _, node = store.create_node(db, name="box", hostname="box", agent_version="1.4.2", protocol=1, ip="10.0.0.9")
        store.apply_hello(db, node, hello, "10.0.0.9")
        db.commit()
        assert placement.port_range(node) == (33000, 33002)
        node.runtime_info = PROCESS_OK  # an agent older than 1.4.2
        assert placement.port_range(node) is None
        for bad in ({"low": 5, "high": 3}, {"low": 0, "high": 3}, {"low": "1", "high": 3}, [1, 3]):
            node.runtime_info = {**PROCESS_OK, "ports": bad}
            assert placement.port_range(node) is None, bad
    finally:
        db.close()
    lines = _describe_runtimes(sup.runtime_info())
    assert lines[-1].startswith("ports     33000-33002 (3 host ports")
    assert not any(line.startswith("ports") for line in _describe_runtimes(PROCESS_OK))


def test_fy_port_002_a_guarded_model_replica_takes_two_ports_and_the_plan_says_when_none_are_left(owner):
    """FY-PORT-002: on a machine whose agent has the key guard and a 3-port range, a llama.cpp replica takes two
    ports; the second deployment does not fit there (the preview, the 409 and its summary say why), and goes to a
    machine with room instead; two replicas pinned there: the first fits, the second does not."""
    seed_catalog()
    small = make_node("tunnel", features=GUARDED, runtime_info=ranged(33000, 33002))
    first = deploy(owner, "one", **LLAMA)
    assert reps(owner, first)[0]["machine"]["name"] == "tunnel"
    plan = owner.post("/api/deployments/estimate", json={"name": "two", **LLAMA}).json()["placement"]
    assert plan["fits"] is False
    reason = plan["machines"][0]["reason"]
    assert reason == "not enough free ports: 2 of 3 in use, it needs 2 (host ports 33000-33002)"
    assert plan["machines"][0]["short_by"] is None
    r = owner.post("/api/deployments", json={"name": "two", **LLAMA})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "does_not_fit"
    assert f"tunnel: {reason}" in r.json()["detail"]["message"]
    make_node("roomy", features=GUARDED, runtime_info=ranged(40000, 40099))
    assert reps(owner, deploy(owner, "two", **LLAMA))[0]["machine"]["name"] == "roomy"
    pinned = owner.post("/api/deployments/estimate", json={"name": "p", **LLAMA, "replicas": 2,
                                                           "machine_id": small}).json()["placement"]
    assert pinned["fits"] is False and pinned["failed_replica"] == 0
    assert pinned["machines"][0]["reason"] == reason
    set_range(small, 33000, 33005)  # 6 ports: room for two more, planned one after the other
    two = owner.post("/api/deployments/estimate", json={"name": "p", **LLAMA, "replicas": 2,
                                                        "machine_id": small}).json()["placement"]
    assert two["fits"] is True and [p["machine"] for p in two["placed"]] == ["tunnel", "tunnel"]
    three = owner.post("/api/deployments/estimate", json={"name": "p", **LLAMA, "replicas": 3,
                                                          "machine_id": small}).json()["placement"]
    assert three["fits"] is False and three["failed_replica"] == 2
    assert three["machines"][0]["reason"] == "no free port: 6 of 6 in use (host ports 33000-33005)"


def test_fy_port_003_plain_workloads_take_one_port_each_and_two_when_exposed(owner):
    """FY-PORT-003: a service takes one port per published port; with the range used up the next one waits
    unplaced with the reason (not in backoff on the machine); a task without ports still goes there; a workload
    exposed at /x/ (behind the guard) needs two."""
    nid = make_node("tunnel", "nvidia-rtx4090x2", features=GUARDED, runtime_info=ranged(33000, 33002))
    for name in ("a", "b"):
        assert create(owner, service(name))["replica_list"][0]["machine"]["name"] == "tunnel"
    exposed = create(owner, {**service("x", machine_id=nid), "gateway": {"expose": True}})["replica_list"][0]
    assert exposed["machine"] is None
    assert exposed["reason"] == ("Cannot be placed: tunnel: not enough free ports: 2 of 3 in use, it needs 2 "
                                 "(host ports 33000-33002)")
    assert create(owner, service("c"))["replica_list"][0]["machine"]["name"] == "tunnel"
    waits = create(owner, service("d"))["replica_list"][0]
    assert waits["machine"] is None and waits["state"] == "pending"
    assert waits["reason"] == "Cannot be placed: tunnel: no free port: 3 of 3 in use (host ports 33000-33002)"
    assert create(owner, task("t"))["replica_list"][0]["machine"]["name"] == "tunnel"
    # A service that ends for good frees its port, and the one waiting is placed on the next pass.
    a = rows("a")[0]
    report(nid, a, "failed", "exited with code 1", final=True)
    db = new_session()
    try:
        placement.place_pending(db)
    finally:
        db.close()
    assert rows("d")[0].node_id == nid


def test_fy_port_004_a_machine_whose_agent_does_not_report_its_range_is_not_counted(owner):
    """FY-PORT-004: an agent older than 1.4.2 says nothing about its range: its ports are not counted, as before."""
    make_node("old", "nvidia-rtx4090x2", features=GUARDED)
    for i in range(5):
        assert create(owner, service(f"s{i}"))["replica_list"][0]["machine"]["name"] == "old"


def test_fy_port_005_a_replica_the_agent_could_not_give_a_port_waits_unplaced_when_the_count_agrees(owner):
    """FY-PORT-005: a replica placed before ports were counted reports "no free host port left": the server's
    count agrees, so it is taken off the machine and waits unplaced with the reason (or goes to a machine with
    room); when the count says there is room (something outside Forgeyard holds the ports) it stays."""
    nid = make_node("tunnel", "nvidia-rtx4090x2", features=GUARDED)  # no range reported yet: nothing counted
    for name in ("a", "b", "c"):
        create(owner, service(name))
    c = rows("c")[0]
    stuck = "no free host port left in 33000-33001; restarting in 2 s"
    assert report(nid, c, "backoff", stuck) is False  # its range is not known: it stays, as before 1.4.2
    assert rows("c")[0].node_id == nid
    set_range(nid, 33000, 33001)  # its agent, updated, reports its 2-port range
    # 3 of 2 held: a task publishes no port and still goes there.
    assert create(owner, task("t"))["replica_list"][0]["machine"]["name"] == "tunnel"
    assert report(nid, c, "backoff", stuck) is True  # the same report again (after it reconnects)
    c = rows("c")[0]
    assert c.node_id is None
    assert c.reason == "Cannot be placed: tunnel: no free port: 2 of 2 in use (host ports 33000-33001)"
    events = [e["message"] for e in owner.get(f"/api/workloads/{c.workload_id}").json()["events"]]
    assert any("tunnel has no free port: 2 of 2 in use" in e and "placing it again" in e for e in events)
    # Room by the count: the agent's own probe found the ports held by something else. It stays and retries.
    set_range(nid, 33000, 33009)
    b = rows("b")[0]
    assert report(nid, b, "backoff", "no free host port left in 33000-33009; restarting in 2 s") is False
    assert rows("b")[0].node_id == nid
    db = new_session()
    try:
        placement.place_pending(db)
    finally:
        db.close()
    assert rows("c")[0].node_id == nid  # room again: back on the machine
    # Another backoff reason is not about ports, even on a machine whose ports are all taken.
    set_range(nid, 33000, 33001)
    a = rows("a")[0]
    report(nid, a, "backoff", "exited with code 1; restarting in 2 s")
    assert rows("a")[0].node_id == nid
