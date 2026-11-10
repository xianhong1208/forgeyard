"""The workloads API: roles, create / stop / start / delete, events, audit (FY-WL-*)."""

import pytest
from sqlalchemy import select

from forgeyard.db import AuditEvent, Replica, get_db
from tests.conftest import add_user, make_client, sign_in
from tests.workload_helpers import make_node, service, task

WRITE_CALLS = [("post", "/api/workloads"), ("post", "/api/workloads/1/stop"), ("post", "/api/workloads/1/start"),
               ("delete", "/api/workloads/1"), ("get", "/api/workloads/1/replicas/0/logs")]


@pytest.mark.parametrize("method, path", WRITE_CALLS)
def test_fy_wl_001_users_cannot_run_or_change_workloads(owner, method, path):
    """FY-WL-001: creating a workload is running code on a machine; `user` gets 403 everywhere."""
    make_node()
    owner.post("/api/workloads", json=task())
    add_user(owner, "bob", role="user")
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    r = getattr(bob, method)(path, **({"json": task("other")} if method == "post" else {}))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"
    assert [w["name"] for w in owner.get("/api/workloads").json()["workloads"]] == ["job"]


@pytest.mark.parametrize("method, path", WRITE_CALLS + [("get", "/api/workloads"), ("get", "/api/workloads/1")])
def test_fy_wl_002_anonymous_gets_401(owner, method, path):
    """FY-WL-002"""
    c = make_client()
    assert getattr(c, method)(path, **({"json": {}} if method == "post" else {})).status_code == 401


def test_fy_wl_003_operators_run_workloads_and_users_can_look(owner):
    """FY-WL-003: everyone signed in sees workloads; env values only for admins and operators."""
    make_node()
    add_user(owner, "olga", role="operator")
    add_user(owner, "ursula", role="user")
    olga = sign_in(owner, "olga", "quiet-meadow-4821")
    ursula = sign_in(owner, "ursula", "quiet-meadow-4821")
    r = olga.post("/api/workloads", json=task(env={"HF_TOKEN": "hf_secret"}))
    assert r.status_code == 201 and r.json()["workload"]["created_by"] == "olga"
    wid = r.json()["workload"]["id"]
    seen = ursula.get("/api/workloads").json()
    assert seen["can_manage"] is False and seen["workloads"][0]["spec"]["env"] == {"HF_TOKEN": "••••"}
    assert "hf_secret" not in ursula.get(f"/api/workloads/{wid}").text
    assert olga.get(f"/api/workloads/{wid}").json()["workload"]["spec"]["env"] == {"HF_TOKEN": "hf_secret"}
    assert olga.post(f"/api/workloads/{wid}/stop").status_code == 200
    assert olga.delete(f"/api/workloads/{wid}").status_code == 200


def test_fy_wl_004_validation_errors_are_readable(owner):
    """FY-WL-004"""
    r = owner.post("/api/workloads", json=task(env={"CUDA_VISIBLE_DEVICES": "0"}))
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["code"] == "invalid_spec" and d["message"].startswith("env: CUDA_VISIBLE_DEVICES is set by Forgeyard")
    r = owner.post("/api/workloads", json={"name": "x", "kind": "task", "runtime": {"type": "vm"}})
    assert r.status_code == 422 and "runtime" in r.json()["detail"]["message"]
    assert owner.post("/api/workloads", json=[1]).status_code == 422


def test_fy_wl_005_names_are_unique(owner):
    """FY-WL-005"""
    make_node()
    owner.post("/api/workloads", json=task())
    r = owner.post("/api/workloads", json=task())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "name_taken"


def test_fy_wl_006_create_places_replicas_and_records_events(owner):
    """FY-WL-006"""
    make_node("box")
    w = owner.post("/api/workloads", json=service(replicas=2)).json()
    reps = w["workload"]["replica_list"]
    assert [r["ordinal"] for r in reps] == [0, 1] and {r["machine"]["name"] for r in reps} == {"box"}
    assert {r["state"] for r in reps} == {"pending"} and w["workload"]["status"] == "pending"
    kinds = [e["kind"] for e in w["events"]]
    assert kinds.count("placed") == 2 and "created" in kinds


def test_fy_wl_007_stop_start_delete_and_audit(owner):
    """FY-WL-007"""
    make_node("box")
    wid = owner.post("/api/workloads", json=service()).json()["workload"]["id"]
    w = owner.post(f"/api/workloads/{wid}/stop").json()["workload"]
    assert w["desired_state"] == "stopped" and w["replica_list"][0]["state"] == "stopping"
    assert "offline" in w["replica_list"][0]["reason"]
    assert owner.post(f"/api/workloads/{wid}/stop").status_code == 409
    # Not confirmed by the agent yet: starting again must wait.
    r = owner.post(f"/api/workloads/{wid}/start")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "still_stopping"
    db = next(get_db())
    db.scalar(select(Replica).where(Replica.workload_id == wid)).state = "stopped"
    db.commit()
    w = owner.post(f"/api/workloads/{wid}/start").json()["workload"]
    assert w["generation"] == 2 and w["replica_list"][0]["generation"] == 2 and w["replica_list"][0]["state"] == "pending"
    assert owner.post(f"/api/workloads/{wid}/start").status_code == 409
    assert owner.delete(f"/api/workloads/{wid}").status_code == 200
    assert owner.get(f"/api/workloads/{wid}").status_code == 404
    actions = [e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.action.like("workload.%"))
                                            .order_by(AuditEvent.id)).all()]
    assert actions == ["workload.created", "workload.stopped", "workload.started", "workload.deleted"]
    assert db.scalars(select(Replica)).all() == []


def test_fy_wl_008_logs_fall_back_to_the_stored_tail(owner):
    """FY-WL-008: no connected agent: the lines kept when the replica ended, and why."""
    make_node("box")
    wid = owner.post("/api/workloads", json=task()).json()["workload"]["id"]
    db = next(get_db())
    db.scalar(select(Replica).where(Replica.workload_id == wid)).log_tail = "last words"
    db.commit()
    r = owner.get(f"/api/workloads/{wid}/replicas/0/logs").json()
    assert r["source"] == "stored" and r["text"] == "last words" and "not connected" in r["note"]
    assert owner.get(f"/api/workloads/{wid}/replicas/5/logs").status_code == 404
    assert owner.get(f"/api/workloads/{wid}/replicas/0/logs?lines=0").status_code == 422


def test_fy_wl_009_removing_a_machine_sends_its_replicas_back_to_pending(owner):
    """FY-WL-009"""
    nid = make_node("box")
    make_node("other")
    wid = owner.post("/api/workloads", json=task(machine_id=nid)).json()["workload"]["id"]
    any_id = owner.post("/api/workloads", json=task("anywhere")).json()["workload"]["id"]
    owner.delete(f"/api/machines/{nid}")
    pinned = owner.get(f"/api/workloads/{wid}").json()["workload"]["replica_list"][0]
    assert pinned["machine"] is None and pinned["state"] == "pending"
    assert "no longer exists" in pinned["reason"]
    moved = owner.get(f"/api/workloads/{any_id}").json()["workload"]["replica_list"][0]
    assert moved["machine"]["name"] == "other"


def test_fy_wl_010_task_summary_statuses(owner):
    """FY-WL-010"""
    make_node("box")
    wid = owner.post("/api/workloads", json=task(replicas=2)).json()["workload"]["id"]
    db = next(get_db())
    rows = db.scalars(select(Replica).where(Replica.workload_id == wid)).all()
    for r in rows:
        r.state, r.exit_code, r.final = "exited", 0, True
    db.commit()
    assert owner.get(f"/api/workloads/{wid}").json()["workload"]["status"] == "succeeded"
    rows[1].state, rows[1].exit_code = "failed", 1
    db.commit()
    assert owner.get(f"/api/workloads/{wid}").json()["workload"]["status"] == "failed"
