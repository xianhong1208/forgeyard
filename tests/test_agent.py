"""The agent: backoff, state file, simulation gate, CLI, and a real run against uvicorn (FY-AGT-*)."""

import asyncio
import json
import os
import socket
import stat
import threading
import time

import pytest
import uvicorn

from forgeyard.agent import state
from forgeyard.agent.backoff import Backoff
from forgeyard.agent.client import (
    EXIT_INCOMPATIBLE,
    EXIT_REFUSED,
    Agent,
    AgentStop,
    JoinError,
    join,
    normalise_server,
    stream_url,
)
from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory, SimulationNotAllowed, fingerprint
from forgeyard.cli import main as cli_main
from forgeyard.config import get_settings
from forgeyard.machines.hub import hub
from tests.conftest import OWNER, SETUP_TOKEN


# ------------------------------------------------------------------ backoff
def test_fy_agt_001_backoff_grows_exponentially_and_is_capped_at_15s():
    """FY-AGT-001"""
    top = Backoff(rng=lambda: 1.0)
    assert [top.next() for _ in range(7)] == [1, 2, 4, 8, 15, 15, 15]
    bottom = Backoff(rng=lambda: 0.0)
    assert [bottom.next() for _ in range(6)] == [0.5, 1, 2, 4, 7.5, 7.5]


def test_fy_agt_002_backoff_jitter_stays_in_range_and_resets():
    """FY-AGT-002"""
    b = Backoff()
    for _ in range(200):
        assert 0.5 <= b.next() <= 15
    delays = {round(Backoff().next(), 6) for _ in range(20)}
    assert len(delays) > 1  # jittered, so a fleet does not reconnect in lockstep
    b.reset()
    assert b.attempt == 0 and b.next() <= 1


# ------------------------------------------------------------------ state
def test_fy_agt_003_credential_file_is_0600_in_a_0700_dir(tmp_path):
    """FY-AGT-003"""
    d = tmp_path / "state"
    path = state.save(d, {"server": "http://s", "node_id": 1, "name": "n", "credential": "fym_x"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(d.stat().st_mode) == 0o700
    assert state.load(d)["credential"] == "fym_x"
    os.chmod(path, 0o644)
    state.load(d)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # loosened by hand: tightened again
    assert state.load(tmp_path / "none") is None
    (d / "credential.json").write_text("{}")
    with pytest.raises(ValueError):
        state.load(d)


def test_fy_agt_004_default_state_dir(monkeypatch, tmp_path):
    """FY-AGT-004"""
    monkeypatch.setenv(state.STATE_ENV, str(tmp_path))
    assert state.default_state_dir() == tmp_path
    monkeypatch.delenv(state.STATE_ENV)
    expected = "/var/lib/forgeyard-agent" if os.geteuid() == 0 else "forgeyard-agent"
    assert str(state.default_state_dir()).endswith(expected)


# ------------------------------------------------------------------ simulation gate
def test_fy_agt_005_simulation_needs_the_explicit_variable():
    """FY-AGT-005"""
    with pytest.raises(SimulationNotAllowed):
        SimulatedInventory("nvidia-a100x8", environ={})
    with pytest.raises(SimulationNotAllowed):
        SimulatedInventory("nvidia-a100x8", environ={SIMULATION_ENV: "true"})
    inv = SimulatedInventory("nvidia-a100x8", environ={SIMULATION_ENV: "1"})
    out = inv.full()
    assert inv.simulated and out["collectors"]["simulation"]["source"] == "fixture nvidia-a100x8.json"
    assert len(out["accelerators"]) == 9 and len(inv.metrics()) == 9
    with pytest.raises(FileNotFoundError, match="amd-gfx1151-apu"):
        SimulatedInventory("nope", environ={SIMULATION_ENV: "1"})


def test_fy_agt_006_cli_refuses_simulation_without_the_variable(monkeypatch, capsys):
    """FY-AGT-006"""
    monkeypatch.delenv(SIMULATION_ENV, raising=False)
    with pytest.raises(SystemExit, match=SIMULATION_ENV):
        cli_main(["agent", "inventory", "--json", "--simulate-inventory", "nvidia-a100x8"])
    with pytest.raises(SystemExit, match=SIMULATION_ENV):
        cli_main(["agent", "--server", "http://x", "--simulate-inventory", "nvidia-a100x8"])


def test_fy_agt_007_inventory_json_prints_what_it_detects(monkeypatch, capsys):
    """FY-AGT-007: real collectors on this machine (CPU at least) and a simulated fixture."""
    assert cli_main(["agent", "inventory", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["simulated"] is False and any(a["vendor"] == "cpu" for a in out["accelerators"])
    assert set(out["collectors"]) == {"nvidia", "amd", "cpu"}
    monkeypatch.setenv(SIMULATION_ENV, "1")
    assert cli_main(["agent", "inventory", "--json", "--simulate-inventory", "amd-gfx1151-apu"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["simulated"] is True and out["accelerators"][0]["arch"] == "gfx1151"
    assert cli_main(["agent", "inventory"]) == 0
    assert "[cpu] ok" in capsys.readouterr().out


def test_fy_agt_011_a_redundant_agent_and_the_amd_line(monkeypatch, capsys):
    """FY-AGT-011: `docker run … IMAGE agent inventory` reaches the CLI as `agent agent inventory`
    (the image's entrypoint is `forgeyard agent`) and works; an AMD card's line names the ROCm version."""
    monkeypatch.setenv(SIMULATION_ENV, "1")
    assert cli_main(["agent", "agent", "inventory", "--simulate-inventory", "amd-gfx1151-apu"]) == 0
    out = capsys.readouterr().out
    assert "amd     0  AMD Radeon 8060S  arch=gfx1151  104.0 GiB free of 110.0 GiB  rocm=7.0.2 unified-memory" in out
    assert "driver=" in out  # the CPU row keeps "driver"
    assert cli_main(["agent", "agent", "inventory", "--json", "--simulate-inventory", "amd-gfx1151-apu"]) == 0
    assert json.loads(capsys.readouterr().out)["simulated"] is True
    with pytest.raises(SystemExit):  # only one leading "agent" is dropped
        cli_main(["agent", "agent", "agent", "inventory"])


def test_fy_agt_008_urls():
    """FY-AGT-008"""
    assert normalise_server("192.168.1.20:7860/") == "http://192.168.1.20:7860"
    assert stream_url("https://forge.lan") == "wss://forge.lan/agent/v1/stream"
    assert stream_url("http://forge.lan/fy") == "ws://forge.lan/fy/agent/v1/stream"
    with pytest.raises(ValueError):
        normalise_server("ftp://x")


def test_fy_agt_009_fatal_close_codes_stop_the_agent():
    """FY-AGT-009: revoked / protocol / replaced stop; anything else reconnects."""
    for code, exit_code in ((4001, EXIT_REFUSED), (4002, EXIT_INCOMPATIBLE), (4009, EXIT_INCOMPATIBLE)):
        with pytest.raises(AgentStop) as e:
            Agent._check_close(code, "")
        assert e.value.exit_code == exit_code
    for code in (1000, 1006, 1012, 4003, 4008, None):
        with pytest.raises(ConnectionError):
            Agent._check_close(code, "")


def test_fy_agt_010_fingerprint_ignores_changing_values():
    """FY-AGT-010"""
    inv = SimulatedInventory("nvidia-a100x8", environ={SIMULATION_ENV: "1"}).full()
    before = fingerprint(inv)
    inv["accelerators"][0]["vram_free"] = 1
    assert fingerprint(inv) == before
    inv["accelerators"].pop()
    assert fingerprint(inv) != before


# ------------------------------------------------------------------ a real server
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, port: int):
        from forgeyard.app import create_app

        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning",
                                                    lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)
        hub.reset()


def _http(port, method, path, body=None, cookie=None):
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Forgeyard": "1",
                                          **({"Cookie": cookie} if cookie else {})})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read()), r.headers.get("set-cookie", "").split(";")[0]


async def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(predicate):
            return True
        await asyncio.sleep(0.05)
    return False


def test_fy_agt_020_join_connect_heartbeat_reconnect_after_restart_then_revoke(monkeypatch):
    """FY-AGT-020: the whole loop over real HTTP and WebSocket: join, online, heartbeats change free
    memory, the server restarts and the agent comes back by itself, a revoke stops it for good."""
    monkeypatch.setenv("AGENT_HEARTBEAT_SECONDS", "1")
    monkeypatch.setenv(SIMULATION_ENV, "1")
    get_settings.cache_clear()
    port = _free_port()
    server = f"http://127.0.0.1:{port}"

    inv = SimulatedInventory("nvidia-a100x8")
    with LiveServer(port):
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        token = _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"]
        joined = join(server, token, name="sim-a100")
        with pytest.raises(JoinError, match="HTTP 403"):
            join(server, token, name="again")

    def machines():
        return _http(port, "GET", "/api/machines", cookie=cookie)[0]["machines"]

    def status():
        try:
            return machines()[0]["status"]
        except OSError:
            return None

    async def scenario():
        agent = Agent(server, joined["credential"], inv, backoff=Backoff(base=0.05, cap=0.2))
        run = asyncio.create_task(agent.run())
        with LiveServer(port):
            assert await _wait(lambda: status() == "online")
            m = machines()[0]
            assert m["simulated"] and len(m["accelerators"]) == 9
            inv._accelerators[0]["vram_free"] = 1234
            assert await _wait(lambda: machines()[0]["accelerators"][0]["vram_free"] == 1234)
        assert await _wait(lambda: agent.sessions >= 1)
        with LiveServer(port):  # restarted: the agent reconnects on its own
            assert await _wait(lambda: status() == "online")
            assert agent.sessions >= 2
            assert agent.backoff.attempt == 0  # a welcome resets the backoff
            _http(port, "POST", f"/api/machines/{joined['node_id']}/revoke", {}, cookie)
            assert await asyncio.wait_for(run, 10) == EXIT_REFUSED
            assert status() == "revoked"
        # A fresh agent with the revoked credential is refused at the handshake.
        again = Agent(server, joined["credential"], inv, backoff=Backoff(base=0.05, cap=0.2))
        with LiveServer(port):
            assert await asyncio.wait_for(again.run(), 10) == EXIT_REFUSED

    asyncio.run(scenario())


def test_fy_agt_021_stop_closes_cleanly_and_the_machine_goes_offline(monkeypatch):
    """FY-AGT-021: SIGTERM/Ctrl-C path: stop() closes the socket, the server marks it offline at once."""
    monkeypatch.setenv(SIMULATION_ENV, "1")
    get_settings.cache_clear()
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    with LiveServer(port):
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        token = _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"]
        joined = join(server, token, name="sim")

        def status():
            return _http(port, "GET", "/api/machines", cookie=cookie)[0]["machines"][0]["status"]

        async def scenario():
            agent = Agent(server, joined["credential"], SimulatedInventory("amd-gfx1151-apu"))
            run = asyncio.create_task(agent.run())
            assert await _wait(lambda: status() == "online")
            agent.stop()
            assert await asyncio.wait_for(run, 5) == 0
            assert await _wait(lambda: status() == "offline", timeout=3)

        asyncio.run(scenario())


def test_fy_agt_030_workloads_end_to_end_with_an_agent_restart(monkeypatch, tmp_path):
    """FY-AGT-030: over real HTTP and WebSocket: a task runs to the end, a service answers HTTP, live
    logs come from the agent, the agent is replaced by a new one (as after kill -9) which adopts the
    service (same pid, nothing started twice), and a stop from the API ends the process."""
    import urllib.request

    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.workload_helpers import alive

    monkeypatch.setenv("AGENT_HEARTBEAT_SECONDS", "1")
    get_settings.cache_clear()
    port, web_port = _free_port(), _free_port()
    server = f"http://127.0.0.1:{port}"
    state_dir = tmp_path / "agent"

    made = []

    def supervisor():
        rt = ProcessRuntime(RecordStore(state_dir), tmp_path / "work", euid=lambda: 4242)
        made.append(rt)
        return Supervisor(state_dir, {"process": rt}, tick=0.1, port_range=(web_port, web_port)), rt

    def api(method, path, body=None):
        return _http(port, method, path, body, cookie)[0]

    def replica0(wid):
        return api("GET", f"/api/workloads/{wid}")["workload"]["replica_list"][0]

    with LiveServer(port):
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        joined = join(server, api("POST", "/api/machines/join-tokens", {})["token"], name="cpu")

        async def scenario():
            sup1, rt1 = supervisor()
            agent = Agent(server, joined["credential"], SimulatedInventory("nvidia-rtx4090x2", environ={SIMULATION_ENV: "1"}),
                          backoff=Backoff(base=0.05, cap=0.2), supervisor=sup1)
            run = asyncio.create_task(agent.run())
            assert await _wait(lambda: api("GET", "/api/machines")["machines"][0]["features"] == ["workloads", "replica_keys",
                                                                                          "replica_guard"])
            job = api("POST", "/api/workloads", {"name": "job", "kind": "task",
                                                 "runtime": {"type": "process", "command": "sh",
                                                             "args": ["-c", "echo working; exit 0"]}})["workload"]
            assert await _wait(lambda: api("GET", f"/api/workloads/{job['id']}")["workload"]["status"] == "succeeded")
            web = api("POST", "/api/workloads", {"name": "web", "kind": "service", "ports": [{"name": "http"}],
                                                 "health": {"type": "http", "interval": 1},
                                                 "runtime": {"type": "process", "command": "python3",
                                                             "args": ["-m", "http.server", "${PORT}",
                                                                      "--bind", "127.0.0.1"]}})["workload"]
            assert await _wait(lambda: replica0(web["id"])["state"] == "healthy", timeout=20)
            rep = replica0(web["id"])
            assert rep["ports"][0]["host_port"] == web_port
            body = await asyncio.to_thread(lambda: urllib.request.urlopen(f"http://127.0.0.1:{web_port}/", timeout=5).read())
            assert b"Directory listing" in body
            # Off this event loop: the agent answering the request runs on it.
            logs = await asyncio.to_thread(api, "GET", f"/api/workloads/{web['id']}/replicas/0/logs")
            assert logs["source"] == "live" and '"GET / HTTP/1.1" 200' in logs["text"]

            # The agent goes away without stopping anything (kill -9), a new one takes over.
            agent.stop()
            await asyncio.wait_for(run, 10)
            pid = rep["pid"]
            assert alive(pid)
            sup2, rt2 = supervisor()
            again = Agent(server, joined["credential"], SimulatedInventory("nvidia-rtx4090x2", environ={SIMULATION_ENV: "1"}),
                          backoff=Backoff(base=0.05, cap=0.2), supervisor=sup2)
            run2 = asyncio.create_task(again.run())
            assert await _wait(lambda: again.sessions >= 1 and sup2.desired is not None)
            await asyncio.sleep(1.5)
            assert sup2.started == 0 and sup2.records[rep["id"]].pid == pid and alive(pid)
            assert replica0(web["id"])["pid"] == pid

            api("POST", f"/api/workloads/{web['id']}/stop", {})
            assert await _wait(lambda: replica0(web["id"])["state"] == "stopped")
            rt1.children[rep["id"]].wait(5)  # the test process is still its parent
            assert not alive(pid)
            again.stop()
            await asyncio.wait_for(run2, 10)

        try:
            asyncio.run(scenario())
        finally:  # never leave a server process behind, even when an assertion failed
            import signal

            for rt in made:
                for proc in rt.children.values():
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(5)


@pytest.mark.parametrize("server_clock_back", [False, True])
def test_fy_agt_031_after_a_server_restart_the_same_agent_takes_new_work(monkeypatch, tmp_path, server_clock_back):
    """FY-AGT-031: over real HTTP and WebSocket: a task runs, the server restarts, the agent reconnects by
    itself, and a new task placed afterwards runs on it, with no agent restart. Up to 1.2.0 the agent
    kept the highest revision across connections and silently dropped the restarted server's lists.
    With the server's clock set back (so its revisions start lower than before) the agent still takes
    the new connection's lists: it starts a new order on every connection."""
    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from forgeyard.machines import agent_api

    monkeypatch.setenv("AGENT_HEARTBEAT_SECONDS", "1")
    get_settings.cache_clear()
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    state_dir = tmp_path / "agent"
    rt = ProcessRuntime(RecordStore(state_dir), tmp_path / "work", euid=lambda: 4242)
    sup = Supervisor(state_dir, {"process": rt}, tick=0.1)

    def api(method, path, body=None):
        return _http(port, method, path, body, cookie)[0]

    def task(name):
        return api("POST", "/api/workloads", {"name": name, "kind": "task",
                                              "runtime": {"type": "process", "command": "sh", "args": ["-c", "echo ok"]}})["workload"]

    def status(wid):
        try:
            return api("GET", f"/api/workloads/{wid}")["workload"]["status"]
        except OSError:
            return None

    with LiveServer(port):
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        joined = join(server, api("POST", "/api/machines/join-tokens", {})["token"], name="cpu")

    async def scenario():
        agent = Agent(server, joined["credential"], SimulatedInventory("nvidia-rtx4090x2", environ={SIMULATION_ENV: "1"}),
                      backoff=Backoff(base=0.05, cap=0.2), supervisor=sup)
        run = asyncio.create_task(agent.run())
        with LiveServer(port):
            first = await asyncio.to_thread(task, "first")
            assert await _wait(lambda: status(first["id"]) == "succeeded")
            before = sup.revision
        assert await _wait(lambda: agent.sessions >= 1)
        if server_clock_back:  # the restarted server counts from far below the old revisions
            real = time.time_ns
            monkeypatch.setattr(agent_api.time, "time_ns", lambda: real() - 10**15)
        with LiveServer(port):  # restarted: the agent reconnects on its own
            assert await _wait(lambda: agent.sessions >= 2)
            second = await asyncio.to_thread(task, "second")
            assert await _wait(lambda: status(second["id"]) == "succeeded")
            assert (sup.revision < before) == server_clock_back
            agent.stop()
            await asyncio.wait_for(run, 10)

    asyncio.run(scenario())
