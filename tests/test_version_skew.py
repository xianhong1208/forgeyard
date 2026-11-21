"""Mixed server and agent versions keep working (1.4.5, #170; FY-SKEW-*): a newer agent's card of a vendor this
server does not know is left out (with a warning and a detection note), not a reason to refuse the hello; a
message type a side does not know is ignored with one warning instead of closing the socket, in both directions.
Refused, a newer agent would reconnect and be refused again forever (4003 is not a fatal close code)."""

import asyncio
import inspect
import json
import logging
import re

from forgeyard.agent.client import Agent
from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
from forgeyard.machines import agent_api
from forgeyard.machines.messages import UNKNOWN_VENDOR, HelloIn
from forgeyard.machines.protocol import AGENT_MESSAGES, SERVER_MESSAGES
from tests.machines_helpers import connect, expect_close, hello, joined, machine
from tests.waits import wait_until


def _online(ws, msg) -> dict:
    ws.send_json(msg)
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome", welcome
    return welcome


def _newer_card(n: int = 0) -> dict:
    return {"key": f"intel:0000:0{n}:00.0", "vendor": "intel", "index": n, "model": "Some Future GPU",
            "vram_total": 48 * 1024**3, "new_field": {"x": 1}}


def test_fy_skew_001_a_card_of_an_unknown_vendor_is_left_out_not_the_hello(owner, caplog):
    """FY-SKEW-001: a newer agent reports two cards of a vendor this server does not know next to its eight A100s:
    the machine comes online with the A100s (and the CPU), the unknown cards are not stored, a detection note says
    what was left out and why, and the server logs one warning naming the vendor."""
    j = joined(owner)
    msg = hello()
    msg["inventory"]["accelerators"][1:1] = [_newer_card(0), _newer_card(1)]
    with caplog.at_level(logging.WARNING, logger="forgeyard.machines"):
        with connect(owner, j["credential"]) as ws:
            _online(ws, msg)
            m = machine(owner, j["node_id"])
    assert m["status"] == "online"
    vendors = [a["vendor"] for a in m["accelerators"]]
    assert vendors.count("nvidia") == 8 and vendors[-1] == "cpu" and "intel" not in vendors
    note = m["collectors"][UNKNOWN_VENDOR]
    assert note["ok"] is False and note["count"] == 2
    assert note["reason"] == ("2 cards of a vendor this server does not know (intel) left out; "
                              "update the server to use them")
    assert sum("vendor this server does not know (intel)" in r.getMessage() for r in caplog.records) == 1


def test_fy_skew_002_the_same_holds_for_a_later_inventory_and_known_vendors_are_untouched(owner):
    """FY-SKEW-002: an inventory message with an unknown card is applied too (the other cards replace the old
    ones); a hello of known vendors only gets no note; the validator keeps everything else as it was (a card
    with no vendor at all is still a bad card)."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        msg = hello()
        _online(ws, msg)
        assert UNKNOWN_VENDOR not in machine(owner, j["node_id"])["collectors"]
        inv = dict(msg["inventory"])
        inv["accelerators"] = [_newer_card(), *inv["accelerators"][1:]]
        ws.send_json({"v": 1, "type": "inventory", "inventory": inv})
        wait_until(lambda: len(machine(owner, j["node_id"])["accelerators"]) == 8, "the new inventory")
        m = machine(owner, j["node_id"])
        assert m["collectors"][UNKNOWN_VENDOR]["reason"].startswith("1 card of a vendor this server does not know")
        assert "intel" not in [a["vendor"] for a in m["accelerators"]]
    one = hello(inventory={"accelerators": [_newer_card()], "collectors": {}})
    parsed = HelloIn.model_validate(one)
    assert parsed.inventory.accelerators == [] and parsed.inventory.collectors[UNKNOWN_VENDOR].count == 1
    # Sixteen collectors already (the most a hello may carry): the card is still left out, no note is added.
    full = hello(inventory={"accelerators": [_newer_card()],
                            "collectors": {f"c{i}": {"ok": True, "count": 0} for i in range(16)}})
    parsed = HelloIn.model_validate(full)
    assert parsed.inventory.accelerators == [] and len(parsed.inventory.collectors) == 16
    bad = hello()
    del bad["inventory"]["accelerators"][0]["vendor"]
    try:
        HelloIn.model_validate(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("a card without a vendor must still be refused")


def test_fy_skew_003_an_unknown_message_type_is_ignored_with_one_warning(owner, caplog):
    """FY-SKEW-003: after the hello, a message type this server does not know is ignored, twice, with one warning;
    the connection stays up and the next heartbeat is applied. A known type that does not validate, and a known
    workloads message from an agent that did not list "workloads", are still refused with 4003."""
    j = joined(owner)
    with caplog.at_level(logging.WARNING, logger="forgeyard.machines"):
        with connect(owner, j["credential"]) as ws:
            _online(ws, hello())
            ws.send_json({"v": 1, "type": "telemetry", "samples": [1, 2, 3]})
            ws.send_json({"v": 1, "type": "telemetry", "samples": [4]})
            key = machine(owner, j["node_id"])["accelerators"][0]["key"]
            ws.send_json({"v": 1, "type": "heartbeat", "seq": 1, "metrics": [{"key": key, "vram_free": 321}]})
            wait_until(lambda: machine(owner, j["node_id"])["accelerators"][0]["vram_free"] == 321,
                       "the heartbeat after the unknown messages")
            assert machine(owner, j["node_id"])["status"] == "online"
            ws.send_json({"v": 1, "type": "heartbeat", "metrics": "not a list"})
            assert ws.receive_json()["code"] == "bad_message"
            assert expect_close(ws) == 4003
    warned = [r.getMessage() for r in caplog.records if "does not know ('telemetry')" in r.getMessage()]
    assert len(warned) == 1 and f"Machine {j['node_id']}" in warned[0]
    with connect(owner, j["credential"]) as ws:
        _online(ws, hello())  # no "workloads" feature
        ws.send_json({"v": 1, "type": "status", "replicas": []})
        assert ws.receive_json()["code"] == "bad_message"
        assert expect_close(ws) == 4003
    with connect(owner, j["credential"]) as ws:
        _online(ws, hello())
        ws.send_json({"v": 1, "type": ["not", "a", "name"]})
        assert ws.receive_json()["code"] == "bad_message"
        assert expect_close(ws) == 4003


class _WS:
    def __init__(self, msgs):
        self.msgs = msgs

    def __aiter__(self):
        async def gen():
            for m in self.msgs:
                yield m if isinstance(m, str) else json.dumps(m)
        return gen()


class _Sup:
    def __init__(self):
        self.desired = []

    def set_desired(self, data):
        self.desired.append(data)


def test_fy_skew_004_the_agent_ignores_a_newer_servers_message_with_one_warning(caplog, monkeypatch):
    """FY-SKEW-004: a newer server sends the agent a message type it does not know (twice), a JSON value that is
    not an object, and text that is not JSON: each is skipped, one warning names the unknown type, and the
    `desired` after them still reaches the supervisor. Before, a non-object ended the receive loop (and with it
    the connection)."""
    monkeypatch.setenv(SIMULATION_ENV, "1")
    agent = Agent("http://127.0.0.1:1", "cred-0123456789abcdef", SimulatedInventory("nvidia-rtx4090x2"),
                  supervisor=_Sup())
    agent.server_features = {"workloads"}
    desired = {"v": 1, "type": "desired", "revision": 7, "replicas": []}
    msgs = [{"v": 1, "type": "rollout_plan", "steps": []}, [1, 2], "not json", "42",
            {"v": 1, "type": "rollout_plan"}, desired]
    with caplog.at_level(logging.WARNING, logger="forgeyard.agent"):
        asyncio.run(asyncio.wait_for(agent._receive_loop(_WS(msgs)), 5))
    assert agent.supervisor.desired == [desired]
    warned = [r.getMessage() for r in caplog.records if "'rollout_plan'" in r.getMessage()]
    assert len(warned) == 1 and "does not know" in warned[0]


def _handled(fn) -> set[str]:
    """The message types a handler compares `kind` with (`kind == "x"`, `kind in ("x", "y")`)."""
    src = inspect.getsource(fn)
    out = set(re.findall(r'kind == "([a-z_]+)"', src))
    for group in re.findall(r"kind in \(([^)]*)\)", src):
        out |= set(re.findall(r'"([a-z_]+)"', group))
    return out


def test_fy_skew_005_the_lists_of_known_types_match_what_each_side_handles():
    """FY-SKEW-005: every type the server's stream handles is in AGENT_MESSAGES and every type the agent's receive
    loop handles is in SERVER_MESSAGES; one missing from a list would now be ignored without a word. The
    hello and welcome are the first messages (read before those loops)."""
    server = _handled(agent_api._serve)
    agent = _handled(Agent._receive_loop)
    assert server and agent
    assert server <= set(AGENT_MESSAGES) and set(AGENT_MESSAGES) - server == {"hello"}
    assert agent <= set(SERVER_MESSAGES) and set(SERVER_MESSAGES) - agent == {"welcome"}
