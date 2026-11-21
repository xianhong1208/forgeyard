"""A machine that publishes its replicas on a loopback address (1.4.5, #171; FY-LBA-*): the agent reports that
address and its own end of the connection; the server tells from the connection whether the agent runs on its own
host, and the setup advisor warns (snoozable) about any other machine on loopback whose port range is not
forwarded to the server's host. The machine page shows the finding (scope machine:N) and the address."""

import asyncio
import json
import socket

import pytest
import websockets

from forgeyard import advisor
from forgeyard.agent.client import Agent
from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
from forgeyard.config import get_settings
from forgeyard.db import Node, new_session
from forgeyard.machines.messages import HelloIn, same_end
from tests.conftest import OWNER, SETUP_TOKEN
from tests.machines_helpers import connect, hello, joined, machine
from tests.test_advisor import edit, findings, node
from tests.test_agent import LiveServer, _free_port, _http


@pytest.fixture(autouse=True)
def no_probes(monkeypatch):
    advisor.reset()
    yield
    advisor.reset()


def _hello(**runtime_info) -> dict:
    return hello(features=["workloads"], runtimes=["process"], runtime_info={"process": {"ok": True}, **runtime_info})


def test_fy_lba_001_the_address_is_kept_and_the_connection_only_compared(owner):
    """FY-LBA-001: the server keeps the address replicas are published on and whether the agent's own end of the
    connection is the peer it sees (here not: the test client's peer is not an address); the connection itself is
    not stored. Bad values are dropped, not refused; an agent before 1.4.5 sends neither, and nothing is added."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        ws.send_json(_hello(address="127.0.0.1", connection={"address": "127.0.0.1", "port": 50000}))
        assert ws.receive_json()["type"] == "welcome"
        info = machine(owner, j["node_id"])["runtime_info"]
    assert info["address"] == "127.0.0.1" and info["same_host"] is False and "connection" not in info
    db = new_session()
    try:
        assert "connection" not in db.get(Node, j["node_id"]).runtime_info
    finally:
        db.close()
    with connect(owner, j["credential"]) as ws:
        ws.send_json(_hello())  # an older agent
        assert ws.receive_json()["type"] == "welcome"
        info = machine(owner, j["node_id"])["runtime_info"]
    assert "address" not in info and "same_host" not in info
    parsed = HelloIn.model_validate(_hello(address="x" * 99, connection={"address": "not-an-ip", "port": 1}))
    assert parsed.runtime_info["address"] == "x" * 64 and "connection" not in parsed.runtime_info
    for bad in ({"address": "127.0.0.1", "port": 0}, {"address": "127.0.0.1", "port": "80"}, ["127.0.0.1", 80]):
        assert "connection" not in HelloIn.model_validate(_hello(connection=bad)).runtime_info


def test_fy_lba_002_same_end_means_nothing_sits_between():
    """FY-LBA-002: the same address and port is the same host; a different port (a tunnel's or proxy's own
    socket), a different address, or nothing reported is not. IPv4-mapped IPv6 peers compare as IPv4."""
    c = {"address": "127.0.0.1", "port": 41000}
    assert same_end(c, ("127.0.0.1", 41000)) and same_end(c, ("::ffff:127.0.0.1", 41000))
    assert not same_end(c, ("127.0.0.1", 41001)) and not same_end(c, ("10.0.0.9", 41000))
    assert not same_end(None, ("127.0.0.1", 41000)) and not same_end(c, None) and not same_end(c, ("testclient",))
    assert same_end({"address": "::1", "port": 9}, ("::1", 9)) and same_end({"address": "192.168.1.20", "port": 9},
                                                                          ("192.168.1.20", 9))


def test_fy_lba_003_a_real_agent_on_the_servers_host_is_on_the_same_host(monkeypatch, tmp_path):
    """FY-LBA-003: over a real socket, a real agent on this host (with a supervisor) reports 127.0.0.1 and its own
    end of the connection, which is the peer the server sees: same_host, so no warning. A hello that reports
    another port (what an agent behind a tunnel would: its own end is on another host) is not, and gets one."""
    from forgeyard.agent.backoff import Backoff
    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor

    monkeypatch.setenv(SIMULATION_ENV, "1")
    get_settings.cache_clear()
    port, low = _free_port(), _free_port()
    with LiveServer(port):
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        token = _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"]
        cred = _http(port, "POST", "/agent/v1/join", {"join_token": token, "protocol": 1, "name": "local",
                                                       "agent_version": "1.4.5"})[0]["credential"]

        def get(path):
            return _http(port, "GET", path, cookie=cookie)[0]

        def loopback_findings():
            return [f for f in get("/api/advisor")["findings"] if f["id"] == "machine-loopback-address"]

        state = tmp_path / "agent"
        sup = Supervisor(state, {"process": ProcessRuntime(RecordStore(state), tmp_path / "work", euid=lambda: 4242)},
                         tick=0.1, port_range=(low, low))
        agent = Agent(f"http://127.0.0.1:{port}", cred, SimulatedInventory("nvidia-rtx4090x2"),
                      backoff=Backoff(base=0.05, cap=0.2), supervisor=sup)

        async def real_agent():
            run = asyncio.create_task(agent.run())
            try:
                for _ in range(200):
                    machines = await asyncio.to_thread(get, "/api/machines")
                    if machines["machines"][0]["runtime_info"].get("same_host") is not None:
                        return machines["machines"][0]["runtime_info"]
                    await asyncio.sleep(0.05)
                raise AssertionError("the agent's hello never arrived")
            finally:
                agent.stop()
                await asyncio.wait_for(run, 10)

        info = asyncio.run(real_agent())
        assert info["address"] == "127.0.0.1" and info["same_host"] is True and "connection" not in info
        assert not loopback_findings()

        async def tunnelled_hello():
            async with websockets.connect(f"ws://127.0.0.1:{port}/agent/v1/stream",
                                          additional_headers={"Authorization": f"Bearer {cred}"}) as ws:
                local = ws.local_address
                await ws.send(json.dumps(agent.hello(agent.inventory.full(), (local[0], 1))))
                assert json.loads(await ws.recv())["type"] == "welcome"

        asyncio.run(tunnelled_hello())
        assert get("/api/machines")["machines"][0]["runtime_info"]["same_host"] is False
        [f] = loopback_findings()
        assert f["severity"] == "warning" and f["snoozable"] and f["scope"].startswith("machine:")


def test_fy_lba_004_the_advisor_warns_unless_the_range_is_forwarded(owner, monkeypatch):
    """FY-LBA-004: a machine elsewhere on 127.0.0.1 with a range of three ports: a warning naming the port nothing
    listens on here; with all three listening (the guide's tunnel) none. A machine on the server's host, one on a
    LAN address, and one whose agent does not say (before 1.4.5) get none. A default-size range is not probed and
    gets the warning saying it is too large to forward."""
    probed = []
    up = {33000, 33002}
    monkeypatch.setattr(advisor, "listening", lambda host, p: (probed.append((host, p)), p in up)[1])
    tunnel = node("amd-01")
    edit(Node, tunnel, runtime_info={"address": "127.0.0.1", "same_host": False, "ports": {"low": 33000, "high": 33002}})
    [f] = findings()["machine-loopback-address"]
    assert (f.severity, f.scope, f.pages, f.snoozable) == ("warning", f"machine:{tunnel}", ("machines",), True)
    assert f.title == "amd-01 publishes its replicas on 127.0.0.1"
    assert "Nothing listens on 1 port of its range here (33001)" in f.detail
    assert "the gateway dials that address on the server's own host, not on amd-01" in f.detail
    assert f.fix_url == "/guide/en/troubleshooting#a-machine-publishes-its-replicas-on-a-loopback-address"
    assert set(probed) == {("127.0.0.1", p) for p in (33000, 33001, 33002)}
    up.add(33001)
    assert "machine-loopback-address" not in findings()
    local = node("local")
    edit(Node, local, runtime_info={"address": "127.0.0.1", "same_host": True})
    lan = node("gpu-01")
    edit(Node, lan, runtime_info={"address": "192.168.1.21", "same_host": False})
    old = node("old")
    edit(Node, old, runtime_info={"process": {"ok": True}})
    unsure = node("unsure")  # an address with no word on the connection: not known to be elsewhere
    edit(Node, unsure, runtime_info={"address": "127.0.0.1", "ports": {"low": 33005, "high": 33005}})
    assert "machine-loopback-address" not in findings()
    probed.clear()
    big = node("far")
    edit(Node, big, runtime_info={"address": "::1", "same_host": False})
    [f] = findings()["machine-loopback-address"]
    assert f.scope == f"machine:{big}" and "Its port range (30000-32767) is too large to forward" in f.detail
    assert not [p for p in probed if p[1] < 33000 or p[1] > 33005]  # the large range was not probed
    edit(Node, big, runtime_info={"address": "localhost", "same_host": False, "ports": {"low": 40000, "high": 40063}})
    probed.clear()
    [f] = findings()["machine-loopback-address"]
    assert len([p for p in probed if p[1] >= 40000]) == 64 and "64 ports" in f.detail


def test_fy_lba_005_the_probe_is_a_plain_connect():
    """FY-LBA-005: `listening` is true for a port something accepts on, false for one nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        open_port = s.getsockname()[1]
        assert advisor._listening("127.0.0.1", open_port)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    assert not advisor._listening("127.0.0.1", closed)


def test_fy_lba_006_the_hello_names_the_address_of_this_connection(monkeypatch, tmp_path):
    """FY-LBA-006: the agent takes its replica address from this connection's own end before it sends the hello
    (not the supervisor's default 127.0.0.1), and reports that end; an address fixed with --bind-address wins."""
    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor

    monkeypatch.setenv(SIMULATION_ENV, "1")
    sent = []

    class WS:
        local_address = ("192.168.1.20", 50123)
        close_code, close_reason = 1000, None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, text):
            sent.append(json.loads(text))

        async def recv(self):
            return json.dumps({"v": 1, "type": "welcome", "name": "gpu-02", "features": [], "heartbeat_interval": 10})

        async def close(self, *a):
            pass

        def __aiter__(self):
            async def none():
                await asyncio.sleep(3600)
                yield ""
            return none()

    def hello_of(bind=None):
        sent.clear()
        state = tmp_path / (bind or "auto")
        sup = Supervisor(state, {"process": ProcessRuntime(RecordStore(state), tmp_path / "w", euid=lambda: 4242)},
                         tick=0.1, port_range=(47390, 47391), bind_address=bind)
        agent = Agent("http://127.0.0.1:1", "cred-0123456789abcdef", SimulatedInventory("nvidia-rtx4090x2"),
                      supervisor=sup, connector=lambda *a, **kw: WS())

        async def once():
            session = asyncio.create_task(agent._session("ws://x"))
            for _ in range(200):
                if sent:
                    break
                await asyncio.sleep(0.01)
            agent.stop()
            await asyncio.wait_for(session, 5)

        asyncio.run(once())
        return sent[0]["runtime_info"]

    info = hello_of()
    assert info["address"] == "192.168.1.20" and info["connection"] == {"address": "192.168.1.20", "port": 50123}
    info = hello_of("10.8.0.5")
    assert info["address"] == "10.8.0.5" and info["connection"]["address"] == "192.168.1.20"
