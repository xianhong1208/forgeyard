"""Offline detection on the server, with an injected clock (FY-LIV-*)."""

import asyncio
import time
from datetime import timedelta


from forgeyard.db import Node, get_db, utcnow
from forgeyard.machines import liveness
from tests.machines_helpers import expect_close, connect, hello, joined, machine


def _db():
    return next(get_db())


def _set(node_id, **values):
    db = _db()
    node = db.get(Node, node_id)
    for k, v in values.items():
        setattr(node, k, v)
    db.commit()


def test_fy_liv_001_offline_after_30_seconds_of_silence(owner):
    """FY-LIV-001: 29 s of silence is still online; 30 s is offline."""
    j = joined(owner)
    beat = utcnow()
    _set(j["node_id"], status="online", last_heartbeat_at=beat)
    assert liveness.sweep(_db(), now=beat + timedelta(seconds=29)) == []
    assert machine(owner, j["node_id"])["status"] == "online"
    assert liveness.sweep(_db(), now=beat + timedelta(seconds=30)) == [j["node_id"]]
    assert machine(owner, j["node_id"])["status"] == "offline"


def test_fy_liv_002_silent_socket_is_closed_so_the_agent_reconnects(owner):
    """FY-LIV-002: a half-open connection (pulled cable, frozen agent) is caught by the sweep, not by
    the socket closing; the server closes it with 4008."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello())
        ws.receive_json()
        assert liveness.sweep(_db(), now=utcnow() + timedelta(seconds=31)) == [j["node_id"]]
        assert expect_close(ws) == 4008
        assert machine(owner, j["node_id"])["status"] == "offline"


def test_fy_liv_003_heartbeats_keep_it_online(owner):
    """FY-LIV-003"""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello())
        ws.receive_json()
        _set(j["node_id"], last_heartbeat_at=utcnow() - timedelta(seconds=25))
        ws.send_json({"v": 1, "type": "heartbeat", "seq": 1, "metrics": []})
        ws.send_json({"v": 1, "type": "heartbeat", "seq": 2, "metrics": []})
        for _ in range(60):
            if not liveness.is_stale(_db().get(Node, j["node_id"]).last_heartbeat_at, utcnow(), 5):
                break
            time.sleep(0.05)
        assert liveness.sweep(_db(), now=utcnow() + timedelta(seconds=10)) == []


def test_fy_liv_004_revoked_and_offline_machines_are_left_alone(owner):
    """FY-LIV-004"""
    a, b = joined(owner, "a"), joined(owner, "b")
    _set(a["node_id"], status="revoked", last_heartbeat_at=utcnow() - timedelta(hours=1))
    _set(b["node_id"], status="offline", last_heartbeat_at=None)
    assert liveness.sweep(_db(), now=utcnow()) == []
    assert machine(owner, a["node_id"])["status"] == "revoked"


def test_fy_liv_005_startup_marks_everything_offline(owner):
    """FY-LIV-005: after a restart no agent is connected, whatever the database said."""
    j = joined(owner)
    _set(j["node_id"], status="online", last_heartbeat_at=utcnow())
    liveness.mark_all_offline(_db())
    assert machine(owner, j["node_id"])["status"] == "offline"


def test_fy_liv_006_is_stale():
    """FY-LIV-006"""
    now = utcnow()
    assert liveness.is_stale(None, now, 30)
    assert not liveness.is_stale(now - timedelta(seconds=29.9), now, 30)
    assert liveness.is_stale(now - timedelta(seconds=30), now, 30)
    assert liveness.is_stale((now - timedelta(seconds=31)).replace(tzinfo=None), now, 30)  # SQLite's naive values


def test_fy_liv_007_the_background_sweep_runs(monkeypatch):
    """FY-LIV-007: run_forever calls the sweep repeatedly and survives a failing round."""
    calls = []

    def fake_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database locked")

    monkeypatch.setattr(liveness, "_sweep_once", fake_once)

    async def run():
        task = asyncio.create_task(liveness.run_forever(interval=0.01))
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(run())
    assert len(calls) >= 3


def test_fy_liv_008_the_app_runs_the_sweep_in_the_background(owner):
    """FY-LIV-008: no test hook: the server itself notices a silent machine."""
    j = joined(owner)
    _set(j["node_id"], status="online", last_heartbeat_at=utcnow() - timedelta(seconds=60))
    for _ in range(80):
        if machine(owner, j["node_id"])["status"] == "offline":
            break
        time.sleep(0.05)
    assert machine(owner, j["node_id"])["status"] == "offline"


def test_fy_liv_009_a_restarted_server_starts_with_everything_offline(owner):
    """FY-LIV-009"""
    from tests.conftest import make_client

    j = joined(owner)
    _set(j["node_id"], status="online", last_heartbeat_at=utcnow())
    with make_client():
        assert _db().get(Node, j["node_id"]).status == "offline"
