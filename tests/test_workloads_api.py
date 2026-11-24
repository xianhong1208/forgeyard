"""The workloads API: who sees what, events, logs, and the engine's stop / start (FY-WL-*).

Since 1.5.3 (#89) every workload a server makes is a model deployment's: the API reads them, and creating,
stopping, starting and deleting go through deployments (the old app routes answer 410, tests/test_apps_removed.py).
Plain workloads here are made by tests (tests/workload_helpers.py create_workload) to exercise the engine."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from forgeyard.db import AuditEvent, Replica, get_db
from tests.conftest import add_user, make_client, sign_in
from tests.workload_helpers import create_workload, delete_workload, make_node, service, start_workload, stop_workload, task


def test_fy_wl_001_users_cannot_read_logs(owner):
    """FY-WL-001: a replica's output may hold secrets: `user` gets 403 on the logs. (Until 1.5.3 also on creating,
    stopping, starting and deleting an app, routes that are gone.)"""
    make_node()
    wid = create_workload(owner, task())["workload"]["id"]
    add_user(owner, "bob", role="user")
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    r = bob.get(f"/api/workloads/{wid}/replicas/0/logs")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"
    assert [w["name"] for w in bob.get("/api/workloads").json()["workloads"]] == ["job"]


@pytest.mark.parametrize("path", ["/api/workloads", "/api/workloads/1", "/api/workloads/1/replicas/0/logs"])
def test_fy_wl_002_anonymous_gets_401(owner, path):
    """FY-WL-002"""
    c = make_client()
    assert c.get(path).status_code == 401


def test_fy_wl_003_operators_run_workloads_and_users_can_look(owner):
    """FY-WL-003: everyone signed in sees workloads; env values only for admins and operators."""
    make_node()
    add_user(owner, "olga", role="operator")
    add_user(owner, "ursula", role="user")
    olga = sign_in(owner, "olga", "quiet-meadow-4821")
    ursula = sign_in(owner, "ursula", "quiet-meadow-4821")
    w = create_workload(olga, task(env={"HF_TOKEN": "hf_secret"}), by="olga")["workload"]
    assert w["created_by"] == "olga"
    wid = w["id"]
    seen = ursula.get("/api/workloads").json()
    assert seen["can_manage"] is False and seen["workloads"][0]["spec"]["env"] == {"HF_TOKEN": "••••"}
    assert "hf_secret" not in ursula.get(f"/api/workloads/{wid}").text
    assert olga.get(f"/api/workloads/{wid}").json()["workload"]["spec"]["env"] == {"HF_TOKEN": "hf_secret"}


def test_fy_wl_006_create_places_replicas_and_records_events(owner):
    """FY-WL-006"""
    make_node("box")
    wid = create_workload(owner, service(replicas=2))["workload"]["id"]
    w = owner.get(f"/api/workloads/{wid}").json()
    reps = w["workload"]["replica_list"]
    assert [r["ordinal"] for r in reps] == [0, 1] and {r["machine"]["name"] for r in reps} == {"box"}
    assert {r["state"] for r in reps} == {"pending"} and w["workload"]["status"] == "pending"
    kinds = [e["kind"] for e in w["events"]]
    assert kinds.count("placed") == 2 and "created" in kinds


def test_fy_wl_007_stop_start_delete_and_audit(owner):
    """FY-WL-007: the engine's stop and start (what a deployment's Stop and Start use): stopping waits for the agent,
    a start while it stops is refused, a start bumps the generation; both are audited."""
    make_node("box")
    wid = create_workload(owner, service())["workload"]["id"]
    w = stop_workload(wid)["workload"]
    assert w["desired_state"] == "stopped" and w["replica_list"][0]["state"] == "stopping"
    assert "offline" in w["replica_list"][0]["reason"]
    with pytest.raises(HTTPException) as e:
        stop_workload(wid)
    assert e.value.status_code == 409 and e.value.detail["code"] == "already_stopped"
    # Not confirmed by the agent yet: starting again must wait.
    with pytest.raises(HTTPException) as e:
        start_workload(wid)
    assert e.value.status_code == 409 and e.value.detail["code"] == "still_stopping"
    db = next(get_db())
    db.scalar(select(Replica).where(Replica.workload_id == wid)).state = "stopped"
    db.commit()
    w = start_workload(wid)["workload"]
    assert w["generation"] == 2 and w["replica_list"][0]["generation"] == 2 and w["replica_list"][0]["state"] == "pending"
    with pytest.raises(HTTPException) as e:
        start_workload(wid)
    assert e.value.status_code == 409 and e.value.detail["code"] == "already_running"
    delete_workload(wid)
    assert owner.get(f"/api/workloads/{wid}").status_code == 404
    actions = [e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.action.like("workload.%"))
                                            .order_by(AuditEvent.id)).all()]
    assert actions == ["workload.stopped", "workload.started"]
    assert db.scalars(select(Replica)).all() == []


def test_fy_wl_008_logs_fall_back_to_the_stored_tail(owner):
    """FY-WL-008: no connected agent: the lines kept when the replica ended, and why."""
    make_node("box")
    wid = create_workload(owner, task())["workload"]["id"]
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
    wid = create_workload(owner, task(machine_id=nid))["workload"]["id"]
    any_id = create_workload(owner, task("anywhere"))["workload"]["id"]
    owner.delete(f"/api/machines/{nid}")
    pinned = owner.get(f"/api/workloads/{wid}").json()["workload"]["replica_list"][0]
    assert pinned["machine"] is None and pinned["state"] == "pending"
    assert "no longer exists" in pinned["reason"]
    moved = owner.get(f"/api/workloads/{any_id}").json()["workload"]["replica_list"][0]
    assert moved["machine"]["name"] == "other"


def test_fy_wl_010_task_summary_statuses(owner):
    """FY-WL-010"""
    make_node("box")
    wid = create_workload(owner, task(replicas=2))["workload"]["id"]
    db = next(get_db())
    rows = db.scalars(select(Replica).where(Replica.workload_id == wid)).all()
    for r in rows:
        r.state, r.exit_code, r.final = "exited", 0, True
    db.commit()
    assert owner.get(f"/api/workloads/{wid}").json()["workload"]["status"] == "succeeded"
    rows[1].state, rows[1].exit_code = "failed", 1
    db.commit()
    assert owner.get(f"/api/workloads/{wid}").json()["workload"]["status"] == "failed"
