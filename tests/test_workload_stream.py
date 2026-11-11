"""Desired state, status and log requests over the agent WebSocket (FY-WST-*)."""

import concurrent.futures
import threading

from sqlalchemy import select

from forgeyard.db import Replica, get_db
from tests.machines_helpers import connect, expect_close, hello, joined
from tests.workload_helpers import PROCESS_OK, make_node, task


def recv(ws, timeout: float = 5.0) -> dict:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(ws.receive_json).result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise AssertionError(f"nothing arrived within {timeout} s") from None
    finally:
        pool.shutdown(wait=False)


def recv_type(ws, kind: str, timeout: float = 5.0) -> dict:
    while True:
        msg = recv(ws, timeout)
        if msg["type"] == kind:
            return msg


def online(ws, **over) -> dict:
    ws.send_json(hello(features=["workloads"], runtimes=["process", "container"], runtime_info=PROCESS_OK, **over))
    welcome = recv(ws)
    assert welcome["type"] == "welcome" and welcome["features"] == ["models", "replica_keys", "workloads"]
    return welcome


def status(rid, generation=1, **fields) -> dict:
    return {"v": 1, "type": "status", "replicas": [{"id": rid, "generation": generation, **fields}]}


def test_fy_wst_001_desired_on_connect_and_on_change(owner):
    """FY-WST-001: the full list right after the welcome, and again when a workload is placed here."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        first = recv_type(ws, "desired")
        assert first["replicas"] == [] and first["revision"] > 0
        keys = {a["index"]: a["key"] for a in owner.get("/api/machines").json()["machines"][0]["accelerators"]
                if a["vendor"] == "nvidia"}
        owner.put(f"/api/machines/{j['node_id']}/gpu-allowlist", json={"keys": [keys[4]]})
        r = owner.post("/api/workloads", json=task(machine_id=j["node_id"], resources={"gpus": {"count": 1}}))
        assert r.status_code == 201, r.text
        msg = recv_type(ws, "desired")
        assert msg["revision"] > first["revision"]
        [rep] = msg["replicas"]
        assert rep["workload"] == "job" and rep["generation"] == 1 and rep["kind"] == "task"
        assert rep["spec"]["runtime"] == {"type": "process", "command": "sh", "args": ["-c", "echo hi"], "workdir": None}
        assert [(g["index"], g["key"]) for g in rep["gpus"]] == [(4, keys[4])]
        assert rep["gpus"][0]["uuid"].startswith("GPU-")
        assert r.json()["workload"]["replica_list"][0]["id"] == rep["id"]


def test_fy_wst_002_status_updates_only_this_machines_current_replicas(owner):
    """FY-WST-002: another machine's replica and an old generation are ignored."""
    other = make_node("other")
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        recv_type(ws, "desired")
        mine = owner.post("/api/workloads", json=task("mine", machine_id=j["node_id"])).json()["workload"]
        theirs = owner.post("/api/workloads", json=task("theirs", machine_id=other)).json()["workload"]
        rid = recv_type(ws, "desired")["replicas"][0]["id"]
        ws.send_json(status(rid, state="running", pid=4321, attempt=1, started_at=1_800_000_000))
        ws.send_json(status(theirs["replica_list"][0]["id"], state="failed", reason="hijack"))
        ws.send_json(status(rid, generation=7, state="failed", reason="old"))
        ws.send_json({"v": 1, "type": "heartbeat", "seq": 1, "metrics": []})  # ordering barrier
        for _ in range(50):
            rep = owner.get(f"/api/workloads/{mine['id']}").json()["workload"]["replica_list"][0]
            if rep["state"] == "running":
                break
        assert (rep["state"], rep["pid"], rep["attempt"]) == ("running", 4321, 1)
        their = owner.get(f"/api/workloads/{theirs['id']}").json()["workload"]["replica_list"][0]
        assert their["state"] == "pending" and their["reason"] != "hijack"


def test_fy_wst_003_a_finished_replica_leaves_the_desired_state(owner):
    """FY-WST-003: final -> not desired any more, its log tail kept for the page."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        recv_type(ws, "desired")
        wid = owner.post("/api/workloads", json=task(machine_id=j["node_id"])).json()["workload"]["id"]
        rid = recv_type(ws, "desired")["replicas"][0]["id"]
        ws.send_json(status(rid, state="exited", exit_code=0, final=True, attempt=1, log_tail="hi\n"))
        assert recv_type(ws, "desired")["replicas"] == []
        w = owner.get(f"/api/workloads/{wid}").json()["workload"]
        assert w["status"] == "succeeded" and w["replica_list"][0]["final"] is True
        db = next(get_db())
        assert db.scalar(select(Replica.log_tail).where(Replica.workload_id == wid)) == "hi\n"
        events = [e["message"] for e in owner.get(f"/api/workloads/{wid}").json()["events"]]
        assert any("exited with code 0" in m for m in events)


def test_fy_wst_004_stop_is_confirmed_by_a_full_status(owner):
    """FY-WST-004"""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        recv_type(ws, "desired")
        wid = owner.post("/api/workloads", json=task(machine_id=j["node_id"], max_duration=600)).json()["workload"]["id"]
        rid = recv_type(ws, "desired")["replicas"][0]["id"]
        ws.send_json(status(rid, state="running", pid=99, attempt=1))
        owner.post(f"/api/workloads/{wid}/stop")
        assert recv_type(ws, "desired")["replicas"] == []
        # Still running according to the agent: shown as stopping.
        ws.send_json(status(rid, state="running", pid=99, attempt=1))
        ws.send_json({"v": 1, "type": "status", "full": True, "replicas": []})
        for _ in range(50):
            rep = owner.get(f"/api/workloads/{wid}").json()["workload"]["replica_list"][0]
            if rep["state"] == "stopped":
                break
        assert rep["state"] == "stopped"


def test_fy_wst_005_an_m1_agent_gets_no_work_and_cannot_send_status(owner):
    """FY-WST-005: compatibility: no features in hello -> no desired, status refused like any
    unknown message, and the machine is never chosen."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello())
        assert recv(ws)["type"] == "welcome"
        r = owner.post("/api/workloads", json=task(machine_id=j["node_id"]))
        assert r.status_code == 422 and "does not run workloads yet" in r.json()["detail"]["message"]
        ws.send_json(status("x", state="running"))
        assert recv(ws)["code"] == "bad_message"
        assert expect_close(ws) == 4003


def test_fy_wst_006_live_logs_come_from_the_agent(owner):
    """FY-WST-006: the browser asks the server, the server asks the agent over the WebSocket."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        recv_type(ws, "desired")
        wid = owner.post("/api/workloads", json=task(machine_id=j["node_id"])).json()["workload"]["id"]
        rid = recv_type(ws, "desired")["replicas"][0]["id"]
        result = {}
        t = threading.Thread(target=lambda: result.update(
            owner.get(f"/api/workloads/{wid}/replicas/0/logs?lines=50").json()))
        t.start()
        req = recv_type(ws, "logs_request")
        assert req["replica"] == rid and req["lines"] == 50 and req["request_id"]
        ws.send_json({"v": 1, "type": "logs", "request_id": req["request_id"], "replica": rid, "ok": True,
                      "text": "line 1\nline 2", "truncated": False})
        t.join(10)
        assert result["source"] == "live" and result["text"] == "line 1\nline 2"


def test_fy_wst_007_unanswered_log_requests_time_out_to_the_stored_tail(owner, monkeypatch):
    """FY-WST-007"""
    from forgeyard.api import workloads as api

    monkeypatch.setattr(api, "LOG_TIMEOUT", 0.3)
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        recv_type(ws, "desired")
        wid = owner.post("/api/workloads", json=task(machine_id=j["node_id"])).json()["workload"]["id"]
        r = owner.get(f"/api/workloads/{wid}/replicas/0/logs").json()
        assert r["source"] == "stored" and "did not answer" in r["note"]


def test_fy_wst_008_a_new_connection_counts_above_the_old_one(owner):
    """FY-WST-008: revisions keep growing across connections (a network drop, a server restart), so an
    agent that remembers the highest one it saw never takes a new connection's lists for old ones.
    Agents up to 1.2.0 did, and silently took no new work after a reconnect."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        first = recv_type(ws, "desired")
        r = owner.post("/api/workloads", json=task(machine_id=j["node_id"]))
        assert r.status_code == 201, r.text
        last = recv_type(ws, "desired")["revision"]
    with connect(owner, j["credential"]) as ws:
        online(ws)
        again = recv_type(ws, "desired")
        assert again["revision"] > last > first["revision"]
        assert [x["workload"] for x in again["replicas"]] == ["job"]
