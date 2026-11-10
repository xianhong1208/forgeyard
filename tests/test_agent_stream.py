"""The agent WebSocket: authentication, hello, heartbeats, revocation (FY-AGS-*)."""

import time

import pytest
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from forgeyard.config import get_settings
from forgeyard.db import Node, get_db
from tests.machines_helpers import expect_close, agent_client, connect, hello, joined, machine


def _online(ws, msg=None) -> dict:
    ws.send_json(msg or hello())
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome", welcome
    return welcome


def test_fy_ags_001_hello_brings_the_machine_online_with_its_inventory(owner):
    """FY-AGS-001: 8 x A100 as reported, free VRAM exactly as NVML gave it."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        welcome = _online(ws)
        assert welcome == {"v": 1, "type": "welcome", "node_id": j["node_id"], "name": "gpu-box",
                           "server_version": welcome["server_version"], "heartbeat_interval": 10, "offline_after": 30,
                           "features": ["models", "workloads"]}
        m = machine(owner, j["node_id"])
        assert m["status"] == "online" and m["connected"] is True and m["simulated"] is True
        assert m["labels"] == {"rack": "a"} and m["agent_version"] == "0.1.0"
        gpus = [a for a in m["accelerators"] if a["vendor"] == "nvidia"]
        assert len(gpus) == 8 and {g["model"] for g in gpus} == {"NVIDIA A100 80GB PCIe"}
        assert {g["arch"] for g in gpus} == {"8.0"} and {g["driver"] for g in gpus} == {"580.178.04"}
        assert [g["vram_total"] for g in gpus] == [85899345920] * 8
        assert [g["vram_free"] for g in gpus] == [4023779328, 7219380224, 6169165824, 9480110080, 48294330368,
                                                  15321071616, 9689825280, 7716405248]
        assert gpus[0]["vram_used"] // 2**20 == 77315  # what nvidia-smi showed as memory.used (MiB)
        assert m["accelerators"][-1]["vendor"] == "cpu"
    assert machine(owner, j["node_id"])["status"] == "offline"  # closing the socket is noticed at once


def test_fy_ags_002_apu_inventory_keeps_unified_memory(owner):
    """FY-AGS-002"""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        _online(ws, hello("amd-gfx1151-apu"))
        gpu = machine(owner, j["node_id"])["accelerators"][0]
        assert (gpu["vendor"], gpu["arch"], gpu["unified_memory"]) == ("amd", "gfx1151", True)
        assert gpu["vram_total"] == 110 * 1024**3 and gpu["detail"]["vram_carveout"] == 512 * 1024**2


def test_fy_ags_003_heartbeat_updates_only_changing_values(owner):
    """FY-AGS-003"""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        _online(ws)
        before = machine(owner, j["node_id"])
        key = before["accelerators"][0]["key"]
        ws.send_json({"v": 1, "type": "heartbeat", "seq": 1,
                      "metrics": [{"key": key, "vram_free": 123, "vram_used": 456, "utilization": 101, "temperature": 55},
                                  {"key": "nvidia:unknown", "vram_free": 1}]})
        ws.send_json({"v": 1, "type": "heartbeat", "seq": 2, "metrics": []})  # ordering: the first is applied
        for _ in range(50):
            gpu = machine(owner, j["node_id"])["accelerators"][0]
            if gpu["vram_free"] == 123:
                break
            time.sleep(0.05)
        assert (gpu["vram_free"], gpu["vram_used"], gpu["utilization"], gpu["temperature"]) == (123, 456, 100, 55)
        assert gpu["model"] == before["accelerators"][0]["model"] and gpu["vram_total"] == 85899345920


def test_fy_ags_004_inventory_message_replaces_the_cards(owner):
    """FY-AGS-004: hardware changed (a card gone)."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        msg = hello()
        _online(ws, msg)
        inv = dict(msg["inventory"])
        inv["accelerators"] = inv["accelerators"][1:]
        ws.send_json({"v": 1, "type": "inventory", "inventory": inv})
        for _ in range(50):
            if len(machine(owner, j["node_id"])["accelerators"]) == 8:
                break
            time.sleep(0.05)
        assert len(machine(owner, j["node_id"])["accelerators"]) == 8


def _denied(app, credential=None, **kw) -> int:
    try:
        with connect(app, credential or "", **kw):
            pass
    except WebSocketDenialResponse as e:
        return e.status_code
    except WebSocketDisconnect as e:
        return -e.code
    return 0


def test_fy_ags_005_unknown_or_missing_credential_is_refused(owner):
    """FY-AGS-005"""
    assert _denied(owner, "fym_nope") == 401
    assert _denied(owner, "") == 401
    with pytest.raises(WebSocketDenialResponse):
        with agent_client(owner).websocket_connect("ws://localhost/agent/v1/stream"):
            pass


def test_fy_ags_006_a_browser_session_cookie_never_authenticates(owner):
    """FY-AGS-006: the admin's cookie (with or without a bearer of garbage) gets nowhere."""
    cookie = "; ".join(f"{k}={v}" for k, v in owner.cookies.items())
    with pytest.raises(WebSocketDenialResponse) as e:
        with agent_client(owner).websocket_connect("ws://localhost/agent/v1/stream", headers={"Cookie": cookie}):
            pass
    assert e.value.status_code == 401
    assert _denied(owner, "garbage", headers={"Cookie": cookie}) == 401


def test_fy_ags_007_browsers_are_refused_even_with_a_credential(owner):
    """FY-AGS-007: any Origin header means a browser; the agent never sends one."""
    j = joined(owner)
    assert _denied(owner, j["credential"], headers={"Origin": "http://localhost"}) == 403


def test_fy_ags_008_revoke_closes_the_open_socket_at_once(owner):
    """FY-AGS-008"""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        _online(ws)
        r = owner.post(f"/api/machines/{j['node_id']}/revoke")
        assert r.json()["connection_closed"] is True
        assert expect_close(ws) == 4001
    assert machine(owner, j["node_id"])["status"] == "revoked"
    assert _denied(owner, j["credential"]) == 401  # and it cannot come back


def test_fy_ags_009_remove_closes_the_socket_too(owner):
    """FY-AGS-009"""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        _online(ws)
        assert owner.delete(f"/api/machines/{j['node_id']}").json()["connection_closed"] is True
        assert expect_close(ws) == 4001
    assert _denied(owner, j["credential"]) == 401


def test_fy_ags_010_first_message_must_be_a_valid_hello(owner):
    """FY-AGS-010"""
    j = joined(owner)
    for bad in ({"v": 1, "type": "heartbeat"}, {"v": 1, "type": "hello"}, [1, 2]):
        with connect(owner, j["credential"]) as ws:
            ws.send_json(bad)
            assert ws.receive_json()["code"] == "bad_hello"
            assert expect_close(ws) == 4003
    assert machine(owner, j["node_id"])["status"] == "offline"


def test_fy_ags_011_other_protocol_versions_are_told_to_upgrade(owner):
    """FY-AGS-011"""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello(v=2))
        assert ws.receive_json()["code"] == "protocol_unsupported"
        assert expect_close(ws) == 4002


def test_fy_ags_012_bad_message_after_hello_closes_with_4003(owner):
    """FY-AGS-012"""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        _online(ws)
        ws.send_json({"v": 1, "type": "launch_missiles"})
        assert ws.receive_json()["code"] == "bad_message"
        assert expect_close(ws) == 4003


def test_fy_ags_013_second_connection_replaces_the_first(owner):
    """FY-AGS-013: the older socket is closed with 4009; the machine stays online."""
    j = joined(owner)
    with connect(owner, j["credential"]) as first:
        _online(first)
        with connect(owner, j["credential"]) as second:
            _online(second)
            assert expect_close(first) == 4009
            assert machine(owner, j["node_id"])["status"] == "online"


@pytest.mark.parametrize("host, ok", [("192.168.1.20:7860", True), ("forge.lan", True), ("[::1]:7860", True),
                                      ("evil.example", False)])
def test_fy_ags_014_host_guard_applies_to_the_websocket(owner, monkeypatch, host, ok):
    """FY-AGS-014"""
    monkeypatch.setenv("ALLOWED_HOSTS", "forge.lan")
    get_settings.cache_clear()
    j = joined(owner)
    if ok:
        with connect(owner, j["credential"], host=host) as ws:
            _online(ws)
    else:
        assert _denied(owner, j["credential"], host=host) == 421


def test_fy_ags_015_odd_values_are_clamped_not_refused(owner):
    """FY-AGS-015: a weird reading must never keep a machine from connecting."""
    j = joined(owner)
    msg = hello()
    card = msg["inventory"]["accelerators"][0]
    card.update({"model": "X" * 500, "utilization": "N/A", "temperature": 99999, "vram_free": -5, "detail": {"x": "y" * 9000}})
    with connect(owner, j["credential"]) as ws:
        _online(ws, msg)
        gpu = machine(owner, j["node_id"])["accelerators"][0]
    assert len(gpu["model"]) == 160 and gpu["utilization"] is None and gpu["temperature"] is None
    assert gpu["vram_free"] is None and gpu["detail"] == {"truncated": True}


def test_fy_ags_016_revoked_between_auth_and_hello(owner):
    """FY-AGS-016: the hello re-checks the machine, so a revoke in between still wins."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        db = next(get_db())
        node = db.get(Node, j["node_id"])
        node.revoked_at = node.joined_at
        db.commit()
        ws.send_json(hello())
        assert ws.receive_json()["code"] == "revoked"
        assert expect_close(ws) == 4001


def test_fy_ags_017_a_revoked_flag_alone_refuses_the_credential(owner):
    """FY-AGS-017: even if the credential hash were left in place, revoked means refused."""
    j = joined(owner)
    db = next(get_db())
    node = db.get(Node, j["node_id"])
    node.revoked_at, node.status = node.joined_at, "revoked"
    db.commit()
    assert _denied(owner, j["credential"]) == 401
