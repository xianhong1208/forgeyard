"""The agent's key guard in front of every model replica: the engine on loopback only, the guard on the
published port, the internal key required on every path (vLLM 0.32 leaves /invocations, /rerank,
/pooling, /score, /classify and /tokenize open), bodies streamed both ways, disconnects passed on,
re-created after an agent restart (FY-RG-*)."""

import asyncio
import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from forgeyard.agent.workloads import plan
from forgeyard.agent.workloads.container import ContainerRuntime
from forgeyard.agent.workloads.guard import Guard
from forgeyard.agent.workloads.process import ProcessRuntime, beyond_loopback
from forgeyard.agent.workloads.records import RecordStore
from forgeyard.agent.workloads.supervisor import Supervisor
from tests.gateway_helpers import free_port
from tests.guard_helpers import KEY, GuardThread, OpenEngine
from tests.workload_helpers import FakeDocker, container, converge, desired, replica, cleanup_world, world_dir

OPEN_PATHS = ["/invocations", "/rerank", "/pooling", "/score", "/classify", "/tokenize", "/v1/chat/completions",
              "/v1/models", "/health", "/metrics", "/", "/docs"]
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def guarded():
    engine = OpenEngine()
    g = GuardThread(engine.port)
    yield engine, g
    g.close()
    engine.close()


def _raw(port: int, data: bytes, timeout: float = 5.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        return out


def test_fy_rg_001_every_path_needs_the_key(guarded):
    """FY-RG-001: without the key, with a wrong one, with another scheme or with two Authorization
    headers, every path and method gets 401 from the guard, and nothing reaches the engine; with the key
    the request passes unchanged (the engine sees the key: it still checks /v1 itself)."""
    engine, g = guarded
    with httpx.Client(base_url=g.url, trust_env=False) as c:
        for path in OPEN_PATHS:
            for method in ("GET", "POST", "HEAD", "OPTIONS", "DELETE"):
                for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {KEY}"},
                                {"Authorization": f"Bearer {KEY}x"}, {"Authorization": KEY}):
                    r = c.request(method, path, headers=headers, content=b'{"x":1}' if method == "POST" else None)
                    assert r.status_code == 401, (method, path, headers)
                    if method != "HEAD":
                        assert r.json()["error"]["code"] == "replica_key_required"
                    assert r.headers["www-authenticate"].startswith("Bearer")
        assert engine.seen == []
        assert g.guard.refused == len(OPEN_PATHS) * 5 * 5
        for path in ("/invocations", "/rerank", "/pooling"):
            r = c.post(path, headers=AUTH, json={"input": "hi"})
            assert r.status_code == 200 and r.json()["path"] == path and r.json()["auth"] == f"Bearer {KEY}"
            assert r.headers["x-engine"] == "open"
        r = c.post("/invocations", headers={"authorization": f"bearer {KEY}"}, json={})
        assert r.status_code == 200  # the scheme is case-insensitive, the key is not
    two = _raw(g.port, (f"POST /invocations HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {KEY}\r\n"
                        f"Authorization: Bearer {KEY}\r\nContent-Length: 0\r\n\r\n").encode())
    assert two.startswith(b"HTTP/1.1 401")
    assert len(engine.seen) == 4


def test_fy_rg_002_hop_by_hop_headers_are_dropped_the_rest_passes(guarded):
    """FY-RG-002: Connection and what it names, Keep-Alive, Proxy-Authorization, TE, Upgrade and Expect
    stay with the guard; every other header (and the key) reaches the engine as sent; HEAD works."""
    engine, g = guarded
    req = (f"POST /pooling?x=1 HTTP/1.1\r\nHost: replica\r\nAuthorization: Bearer {KEY}\r\n"
           "Connection: close, X-Hop\r\nX-Hop: secret\r\nKeep-Alive: 5\r\nProxy-Authorization: Basic abc\r\n"
           "Upgrade: websocket\r\nX-Request-Id: r-1\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    out = _raw(g.port, req.encode())
    assert out.startswith(b"HTTP/1.1 200")
    [seen] = engine.seen
    assert seen["path"] == "/pooling?x=1" and seen["headers"]["x-request-id"] == "r-1"
    assert seen["headers"]["authorization"] == f"Bearer {KEY}" and seen["headers"]["content-type"] == "application/json"
    for gone in ("x-hop", "keep-alive", "proxy-authorization", "upgrade"):
        assert gone not in seen["headers"]
    with httpx.Client(base_url=g.url, trust_env=False) as c:
        r = c.head("/tokenize", headers=AUTH)
        assert r.status_code == 200 and r.content == b""


def test_fy_rg_003_sse_passes_through_as_it_comes(guarded):
    """FY-RG-003: the first event reaches the client while the engine still holds the second one: the
    guard does not buffer a streamed answer."""
    engine, g = guarded
    with httpx.Client(base_url=g.url, trust_env=False, timeout=10) as c:
        with c.stream("POST", "/sse", headers=AUTH, json={"stream": True}) as r:
            assert r.status_code == 200 and r.headers["content-type"] == "text/event-stream"
            it = r.iter_raw()
            first = next(it)
            assert first == b"data: one\n\n" and not engine.release.is_set()
            engine.release.set()
            rest = b"".join(it)
    assert rest == b"data: two\n\n"


def test_fy_rg_004_uploads_stream_and_large_answers_pass(guarded):
    """FY-RG-004: a multipart upload reaches the engine while the client is still sending it (the
    engine had half of it before the second half left the client); a chunked upload arrives intact;
    a 20 MiB answer passes byte for byte."""
    engine, g = guarded
    boundary = "fyb0undary"
    audio = os.urandom(4 * 1024 * 1024)
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper\r\n--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n"
            ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    half = len(body) // 2
    engine.threshold = half
    head = (f"POST /v1/audio/transcriptions HTTP/1.1\r\nHost: r\r\nAuthorization: Bearer {KEY}\r\n"
            f"Content-Type: multipart/form-data; boundary={boundary}\r\nContent-Length: {len(body)}\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", g.port), timeout=10) as s:
        s.sendall(head + body[:half])
        assert engine.progress.wait(10), engine.received  # the engine has it before the rest is sent
        s.sendall(body[half:])
        answer = b""
        while b"}" not in answer:
            answer += s.recv(65536)
    seen = engine.seen[-1]
    assert seen["len"] == len(body) and seen["sha"] == hashlib.sha256(body).hexdigest()

    def pieces():
        for i in range(0, len(audio), 100_000):
            yield audio[i:i + 100_000]

    with httpx.Client(base_url=g.url, trust_env=False, timeout=30) as c:
        r = c.post("/pooling", headers=AUTH, content=pieces())  # Transfer-Encoding: chunked
        assert r.status_code == 200 and r.json()["sha"] == hashlib.sha256(audio).hexdigest()
        r = c.get("/big", headers=AUTH)
        assert r.status_code == 200 and len(r.content) == engine.big and r.content == b"x" * engine.big


def test_fy_rg_005_a_client_that_goes_away_closes_the_engines_connection(guarded):
    """FY-RG-005: the client reads one event of an endless stream and hangs up; the engine's connection
    is closed right away (its next write fails), so the engine stops generating."""
    engine, g = guarded
    s = socket.create_connection(("127.0.0.1", g.port), timeout=5)
    s.sendall(f"POST /forever HTTP/1.1\r\nHost: r\r\nAuthorization: Bearer {KEY}\r\nContent-Length: 0\r\n\r\n".encode())
    got = b""
    while b"data: 0" not in got:  # the head and the first event may come in separate reads
        chunk = s.recv(65536)
        assert chunk, got
        got += chunk
    t0 = time.monotonic()
    s.close()
    assert engine.gone.wait(5)
    assert time.monotonic() - t0 < 3
    time.sleep(0.2)
    assert g.call(_conns(g.guard)) == 0


async def _conns(guard):
    return len(guard._conns)


def test_fy_rg_006_engine_down_keep_alive_timeouts():
    """FY-RG-006: with the engine down the guard closes the connection without an answer (the gateway
    then tries another replica, as when it reached a dead engine directly); a kept-alive connection
    carries several authorised requests; a refused one is closed; Expect: 100-continue is answered by
    the guard; a request head sent too slowly is cut off; a closed guard no longer listens."""
    port = free_port()
    g = GuardThread(port, head_timeout=0.5)
    try:
        out = _raw(g.port, f"POST /invocations HTTP/1.1\r\nHost: r\r\nAuthorization: Bearer {KEY}\r\nContent-Length: 0\r\n\r\n".encode())
        assert out == b""
        with pytest.raises(httpx.RemoteProtocolError):
            httpx.post(g.url + "/v1/chat/completions", headers=AUTH, json={}, trust_env=False)
        refused = _raw(g.port, b"GET /invocations HTTP/1.1\r\nHost: r\r\n\r\nGET /invocations HTTP/1.1\r\nHost: r\r\n\r\n")
        assert refused.count(b"HTTP/1.1 401") == 1  # answered once, then closed
        with socket.create_connection(("127.0.0.1", g.port), timeout=5) as s:
            s.sendall(b"POST /x HTTP/1.1\r\nHost: r\r\n")
            time.sleep(1.0)
            assert s.recv(100) == b""  # the head did not finish within 0.5 s
    finally:
        g.close()
    engine = OpenEngine()
    g = GuardThread(engine.port)
    try:
        with httpx.Client(base_url=g.url, trust_env=False) as c:
            for _ in range(3):
                assert c.post("/score", headers=AUTH, json={}).status_code == 200
        assert len(engine.seen) == 3
        with socket.create_connection(("127.0.0.1", g.port), timeout=5) as s:
            s.sendall(f"POST /classify HTTP/1.1\r\nHost: r\r\nAuthorization: Bearer {KEY}\r\nExpect: 100-continue\r\n"
                      f"Content-Length: 4\r\n\r\n".encode())
            assert s.recv(100).startswith(b"HTTP/1.1 100")
            s.sendall(b"abcd")
            answer = s.recv(65536)
            assert answer.startswith(b"HTTP/1.1 200") and "expect" not in engine.seen[-1]["headers"]
        port = g.port
    finally:
        g.close()
        engine.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_fy_rg_007_the_engine_is_bound_to_loopback_only():
    """FY-RG-007: a guarded container publishes its port on 127.0.0.1:<internal> (never on the agent's
    address), a guarded process gets PORT=<internal> and ${BIND_ADDRESS}=127.0.0.1; an unguarded one
    is unchanged. beyond_loopback() finds a listener on any other address."""
    ports = [{"name": "http", "port": 8000, "host_port": 31000, "internal": 31001, "address": "10.0.0.9"}]
    argv = plan.container_run_argv("docker", name="n", image="img", command=None, args=[], env_file="/e", labels={},
                                   ports=ports, bind="10.0.0.9", cpu=None, memory=None, gpus=[], gpu_mode=None,
                                   render_devices=[], render_gids=[], log_max_bytes=1 << 20)
    assert argv[argv.index("-p") + 1] == "127.0.0.1:31001:8000/tcp" and "10.0.0.9" not in " ".join(argv)
    plain = [{k: v for k, v in ports[0].items() if k != "internal"}]
    argv = plan.container_run_argv("docker", name="n", image="img", command=None, args=[], env_file="/e", labels={},
                                   ports=plain, bind="10.0.0.9", cpu=None, memory=None, gpus=[], gpu_mode=None,
                                   render_devices=[], render_gids=[], log_max_bytes=1 << 20)
    assert argv[argv.index("-p") + 1] == "10.0.0.9:31000:8000/tcp"
    env = plan.process_env({}, [], ports, home="/h", user="u", replica_id="r", workload="w", ordinal=0)
    assert env["PORT"] == env["PORT_HTTP"] == "31001"
    assert plan.process_env({}, [], plain, home="/h", user="u", replica_id="r", workload="w", ordinal=0)["PORT"] == "31000"
    with socket.socket() as wide, socket.socket() as narrow:
        wide.bind(("0.0.0.0", 0))
        wide.listen()
        narrow.bind(("127.0.0.1", 0))
        narrow.listen()
        a, b = wide.getsockname()[1], narrow.getsockname()[1]
        assert beyond_loopback([a, b]) == [f"0.0.0.0:{a}"]
        assert beyond_loopback([b]) == []


# ------------------------------------------------------------------ in the agent
ENGINE = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({"pid": os.getpid(), "path": self.path}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    do_GET = do_POST = _go
HTTPServer((sys.argv[1], int(os.environ["PORT"])), H).serve_forever()
'''


def _process_spec(tmp: Path, bind: str = "127.0.0.1") -> dict:
    script = tmp / "engine.py"
    script.write_text(ENGINE)
    script.chmod(0o644)
    return {"name": "stand-in", "kind": "service",
            "runtime": {"type": "process", "command": "python3", "args": ["-I", str(script), bind]},
            "ports": [{"name": "http"}],
            "health": {"type": "http", "port": "http", "path": "/health", "interval": 1, "timeout": 2}}


def keyed(rep: dict, env=None, value: str = KEY) -> dict:
    return {**rep, "replica_key": {"env": env, "value": value}}


def _get(port: int, path: str, key=None) -> int:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.post(f"http://127.0.0.1:{port}{path}", headers=headers, json={}, trust_env=False, timeout=5).status_code


@pytest.mark.skipif(os.geteuid() != 0, reason="runs the process as the workload user, which needs a root agent")
def test_fy_rg_008_a_process_replica_behind_its_guard_and_after_an_agent_restart(tmp_path):
    """FY-RG-008: a real process replica with a key (env None: the engine reads none, like a custom
    recipe): the guard holds the published port (401 without the key, also on /invocations; 200 with
    it), the engine listens on 127.0.0.1:<internal> only, the internal port is in no status report. The
    agent "dies" (its guard with it: refused connections, the engine keeps running), a new agent adopts
    the same pid, keeps the port closed until the server's key arrives, then guards it again."""
    work = world_dir()
    try:
        spec = _process_spec(work)
        state = tmp_path / "state"

        def runtime():
            return ProcessRuntime(RecordStore(state), work / "work", workload_user="nobody", systemd=lambda: False)

        async def first():
            sup = Supervisor(state, {"process": runtime()}, tick=0.05, port_range=(32100, 32130))
            sup.set_desired(desired(keyed(replica("r1", spec))))
            await converge(sup, lambda s: s.records["r1"].state == "healthy")
            rec = sup.records["r1"]
            [p] = rec.ports
            report = json.dumps(sup.take_report())
            codes = {path: await asyncio.to_thread(_get, p["host_port"], path) for path in ("/invocations", "/rerank", "/pooling")}
            good = await asyncio.to_thread(_get, p["host_port"], "/invocations", KEY)
            wide = beyond_loopback([p["internal"], p["host_port"]])
            await sup.close()  # the agent goes away; the replica keeps running
            return rec.pid, p, report, codes, good, wide

        pid, port, report, codes, good, wide = asyncio.run(first())
        assert codes == {"/invocations": 401, "/rerank": 401, "/pooling": 401} and good == 200
        assert wide == [] and port["address"] == "127.0.0.1" and port["internal"] != port["host_port"]
        assert '"internal"' not in report and str(port["internal"]) not in report
        assert "PORT=" not in (state / "replicas" / "r1.json").read_text() and KEY not in (state / "replicas" / "r1.json").read_text()
        with pytest.raises(httpx.ConnectError):
            _get(port["host_port"], "/invocations")
        assert _get(port["internal"], "/invocations") == 200  # the engine lives on, on loopback

        async def second():
            sup = Supervisor(state, {"process": runtime()}, tick=0.05, port_range=(32100, 32130))
            await sup.adopt()
            for _ in range(5):
                await sup.reconcile_once()
                await sup.settle()
            closed = not sup.guards
            sup.set_desired(desired(keyed(replica("r1", spec))))
            await converge(sup, lambda s: "r1" in s.guards and s.guards["r1"][0].listening)
            rec = sup.records["r1"]
            codes = (await asyncio.to_thread(_get, port["host_port"], "/invocations"),
                     await asyncio.to_thread(_get, port["host_port"], "/invocations", KEY))
            started = sup.started
            sup.set_desired(desired())
            await converge(sup, lambda s: "r1" not in s.records)
            await sup.close()
            return closed, rec.pid, codes, started

        closed, pid2, codes, started = asyncio.run(second())
        assert closed and pid2 == pid and codes == (401, 200) and started == 0
    finally:
        cleanup_world(work)


@pytest.mark.skipif(os.geteuid() != 0, reason="runs the process as the workload user, which needs a root agent")
def test_fy_rg_009_an_engine_listening_beyond_loopback_is_stopped(tmp_path):
    """FY-RG-009: a guarded process that ignores the bind address and listens on every address could be
    used around the guard: once it answers its health check the agent stops it and fails it for good."""
    work = world_dir()
    try:
        spec = _process_spec(work, bind="0.0.0.0")

        async def scenario():
            rt = ProcessRuntime(RecordStore(tmp_path / "state"), work / "work", workload_user="nobody", systemd=lambda: False)
            sup = Supervisor(tmp_path / "state", {"process": rt}, tick=0.05, port_range=(32140, 32160))
            sup.set_desired(desired(keyed(replica("r1", spec), env="LLAMA_API_KEY")))
            await converge(sup, lambda s: s.records["r1"].final, timeout=20)
            rec = sup.records["r1"]
            listening = beyond_loopback([rec.ports[0]["internal"]])
            guards = dict(sup.guards)
            sup.set_desired(desired())
            await converge(sup, lambda s: "r1" not in s.records)
            await sup.close()
            return rec.state, rec.reason, listening, guards

        state, reason, listening, guards = asyncio.run(scenario())
        assert state == "failed" and "not only on loopback" in reason and "{host}" in reason
        assert listening == [] and guards == {}
    finally:
        cleanup_world(work)


def test_fy_rg_010_containers_publish_on_loopback_and_old_replicas_are_put_behind_a_guard(tmp_path):
    """FY-RG-010: a keyed container is published on 127.0.0.1:<internal> while the guard holds the
    published port (its label keeps the internal port, so a restarted agent knows it). A container an
    agent without the guard started (published straight on the address, no internal port) is adopted,
    then, once its key is known, stopped and started again behind a guard; the published port stays."""
    fake = FakeDocker(tmp_path / "bin")
    probes = []

    def prober(kind, host, port, path, timeout, headers=None):
        probes.append((host, port, headers))
        return None

    spec = container(health={"type": "http", "path": "/health", "interval": 1})

    async def first():
        rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32200, 32230), prober=prober)
        sup.set_desired(desired(keyed(replica("abcdef0123456789", spec), env="VLLM_API_KEY")))
        await converge(sup, lambda s: s.records["abcdef0123456789"].state == "healthy")
        rec = sup.records["abcdef0123456789"]
        listening = [g.listening for g in sup.guards[rec.id]]
        code = await asyncio.to_thread(_get, rec.ports[0]["host_port"], "/invocations")
        await sup.close()
        return dict(rec.ports[0]), listening, code

    port, listening, code = asyncio.run(first())
    [run] = fake.runs()
    assert run[run.index("-p") + 1] == f"127.0.0.1:{port['internal']}:8000/tcp"
    assert listening == [True] and code == 401  # the guard; the fake engine never even started a server
    [c] = fake.state().values()
    assert json.loads(c["labels"]["forgeyard.ports"])[0]["internal"] == port["internal"]
    assert probes[-1] == ("127.0.0.1", port["internal"], {"Authorization": f"Bearer {KEY}"})

    # An older agent's container: published on the address itself, record without an internal port.
    rec_file = tmp_path / "state" / "replicas" / "abcdef0123456789.json"
    data = json.loads(rec_file.read_text())
    data["ports"] = [{k: v for k, v in data["ports"][0].items() if k != "internal"}]
    data["guarded"] = False
    rec_file.write_text(json.dumps(data))

    async def upgraded():
        rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32200, 32230), prober=prober)
        await sup.adopt()
        for _ in range(3):  # no key yet: left running as it is
            await sup.reconcile_once()
            await sup.settle()
        before = len(fake.runs())
        sup.set_desired(desired(keyed(replica("abcdef0123456789", spec), env="VLLM_API_KEY")))
        await converge(sup, lambda s: s.records["abcdef0123456789"].state == "healthy"
                       and plan.guarded(s.records["abcdef0123456789"].ports))
        rec = sup.records["abcdef0123456789"]
        out = before, dict(rec.ports[0]), rec.restarts
        await sup.close()
        return out

    before, port2, restarts = asyncio.run(upgraded())
    assert before == 1 and len(fake.runs()) == 2
    assert port2["host_port"] == port["host_port"] and port2.get("internal")
    run = fake.runs()[-1]
    assert run[run.index("-p") + 1] == f"127.0.0.1:{port2['internal']}:8000/tcp"


def test_fy_rg_011_no_key_no_guard_and_the_key_follows_desired(tmp_path):
    """FY-RG-011: an unkeyed replica gets no guard and no internal port (plain M2 workloads are not
    guarded); a keyed one only listens once its key is known; a new key from the server replaces the old
    one in the running guard."""
    fake = FakeDocker(tmp_path / "bin")

    async def scenario():
        rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32240, 32260))
        sup.set_desired(desired(replica("plain0123456789a", container(name="plain")),
                                keyed(replica("keyed0123456789a", container(name="keyed")))))
        await converge(sup, lambda s: all(r.state == "running" for r in s.records.values()))
        plain, rec = sup.records["plain0123456789a"], sup.records["keyed0123456789a"]
        out = {"plain_ports": dict(plain.ports[0]), "plain_guard": plain.id in sup.guards}
        port = rec.ports[0]["host_port"]
        out["old"] = await asyncio.to_thread(_try, port, KEY)
        sup.set_desired(desired(replica("plain0123456789a", container(name="plain")),
                                keyed(replica("keyed0123456789a", container(name="keyed")), value="new-key-0123456789")))
        await converge(sup)
        out["old_after"] = await asyncio.to_thread(_try, port, KEY)
        out["new_after"] = await asyncio.to_thread(_try, port, "new-key-0123456789")
        await sup.close()
        return out

    out = asyncio.run(scenario())
    assert "internal" not in out["plain_ports"] and not out["plain_guard"]
    # Nothing listens behind the fake container: an authorised request is closed unanswered (the
    # gateway then tries another replica); a refused one is a 401.
    assert out["old"] == "RemoteProtocolError" and out["old_after"] == 401 and out["new_after"] == "RemoteProtocolError"


def _try(port: int, key):
    try:
        return _get(port, "/invocations", key)
    except httpx.HTTPError as e:
        return type(e).__name__


def test_fy_rg_012_guard_bind_failure_is_reported_not_ignored(tmp_path):
    """FY-RG-012: when the published port is taken by someone else, the engine is not started and the
    replica says why (and retries like any failed start)."""
    fake = FakeDocker(tmp_path / "bin")
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def probe(port):  # every port looks free, so the agent picks 32270 although it is taken
        return True

    blocker.bind(("127.0.0.1", 32270))
    blocker.listen()
    try:
        async def scenario():
            rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))
            sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32270, 32280), port_probe=probe)
            sup.set_desired(desired(keyed(replica("abcdef0123456789", container()))))
            await converge(sup, lambda s: s.records["abcdef0123456789"].state == "backoff")
            rec = sup.records["abcdef0123456789"]
            await sup.close()
            return rec.reason

        reason = asyncio.run(scenario())
        assert "cannot listen on 127.0.0.1:32270" in reason and "key guard" in reason
        assert fake.runs() == []
    finally:
        blocker.close()


def test_fy_rg_013_close_cuts_open_streams():
    """FY-RG-013: closing a guard (the agent stops, the replica is removed) cuts the connections in
    flight, and the engine's with them."""
    engine = OpenEngine()

    async def scenario():
        port = free_port()
        g = Guard(listen=("127.0.0.1", port), upstream=("127.0.0.1", engine.port), key=KEY)
        await g.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"POST /forever HTTP/1.1\r\nHost: r\r\nAuthorization: Bearer {KEY}\r\nContent-Length: 0\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"data: 0")
        await g.close()
        rest = await asyncio.wait_for(reader.read(), 5)  # EOF soon after the close
        writer.close()
        return g.listening, rest

    try:
        listening, _ = asyncio.run(scenario())
        assert not listening and engine.gone.wait(5)
    finally:
        engine.close()


def test_fy_rg_014_threads_do_not_leak():
    """FY-RG-014: nothing of a guard outlives its close (no thread, no listening socket)."""
    before = threading.active_count()
    engine = OpenEngine()
    g = GuardThread(engine.port)
    port = g.port
    g.close()
    engine.close()
    time.sleep(0.2)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    assert threading.active_count() <= before + 1


# ------------------------------------------------------------------ the server's side
def test_fy_rg_015_the_server_keys_every_replica_on_a_guarding_agent(owner):
    """FY-RG-015: on a machine whose agent guards replicas, every model replica gets a key, also with a
    recipe whose engine reads none (env null: the guard alone requires it); an agent without the guard
    gets keys only for engines that read one, as before. The deployment page says which replicas are
    guarded on every path."""
    from sqlalchemy import select

    from forgeyard.db import Workload, new_session
    from forgeyard.models.recipes import builtin_recipes
    from forgeyard.workloads import state as wstate
    from tests.test_scheduler import MODELS, deploy, seed_catalog
    from tests.workload_helpers import make_node

    seed_catalog()
    text = builtin_recipes()["llama-cpp"][1]
    custom = text.replace("name: llama-cpp", "name: keyless").replace("api_key_env: LLAMA_API_KEY\n", "")
    assert "api_key_env" not in custom
    assert owner.post("/api/recipes", json={"yaml": custom}).status_code == 201
    guard = make_node("guard", features=MODELS + ("replica_keys", "replica_guard"))
    old = make_node("old", features=MODELS + ("replica_keys",))
    params = {"gpus": 0, "threads": 2}
    d1 = deploy(owner, "on-guard", "qwen3-1.7b-gguf", "keyless", machine_id=guard, params=params)["deployment"]
    deploy(owner, "on-old", "qwen3-1.7b-gguf", "keyless", machine_id=old, params=params)
    deploy(owner, "keyed-old", "qwen3-1.7b-gguf", "llama-cpp", machine_id=old, params=params)
    db = new_session()
    try:
        secrets_ = {w.name: w.replicas[0].secret for w in db.scalars(select(Workload)).all()}
        on_guard = wstate.desired_message(db, guard, 1)["replicas"]
        on_old = {r["workload"]: r for r in wstate.desired_message(db, old, 1)["replicas"]}
    finally:
        db.close()
    assert secrets_["on-guard"] and secrets_["on-old"] is None and secrets_["keyed-old"]
    assert on_guard[0]["replica_key"] == {"env": None, "value": secrets_["on-guard"]}
    assert "replica_key" not in on_old["on-old"]
    assert on_old["keyed-old"]["replica_key"]["env"] == "LLAMA_API_KEY"
    from tests.gateway_helpers import make_ready

    make_ready("on-guard", [(guard, 31000, secrets_["on-guard"])])
    [e] = owner.get(f"/api/deployments/{d1['id']}").json()["deployment"]["endpoints"]
    assert e["protected"] is True and e["guarded"] is True


def test_fy_rg_016_an_agent_lists_the_guard_and_takes_a_key_without_a_variable(tmp_path):
    """FY-RG-016: a key with env null is kept (memory only) and guards the replica, while the engine's
    environment gets no key variable. (That the agent's hello lists replica_guard: FY-AGT-030.)"""
    fake = FakeDocker(tmp_path / "bin")

    async def scenario():
        rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32290, 32300))
        sup.set_desired(desired(keyed(replica("abcdef0123456789", container()), env=None)))
        await converge(sup, lambda s: s.records["abcdef0123456789"].state == "running")
        rec = sup.records["abcdef0123456789"]
        out = rec.guarded, rec.key_env, rec.id in sup.guards, sup.keys[rec.id]
        await sup.close()
        return out

    guarded_, key_env, has_guard, key = asyncio.run(scenario())
    assert guarded_ and key_env is None and has_guard and key == (None, KEY)
    [c] = fake.state().values()
    assert KEY not in json.dumps(c["env"])
    assert KEY not in (tmp_path / "state" / "replicas" / "abcdef0123456789.json").read_text()


def test_fy_rg_017_the_gateway_through_a_guard(owner):
    """FY-RG-017: the gateway reaches a replica through its guard with the internal key (200, SSE intact),
    a direct call on the replica's port without it is 401, and a guard whose engine is down makes the
    gateway fail over to the next replica instead of answering 502."""
    from tests.gateway_helpers import FakeEngine, api_client, make_ready, new_key
    from tests.test_gateway import KEYS, chat, events
    from tests.test_scheduler import deploy, seed_catalog
    from tests.workload_helpers import make_node

    seed_catalog()
    a = make_node("cpu-a", features=KEYS + ("replica_guard",))
    b = make_node("cpu-b", features=KEYS + ("replica_guard",))
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", replicas=2, params={"gpus": 0, "threads": 2}, wait=True)
    engine = FakeEngine("guarded", "secret-g", chunks=4)
    g0 = GuardThread(engine.port, key="secret-g")
    g1 = GuardThread(free_port(), key="secret-x")  # replica 1: its engine is gone
    try:
        make_ready("chat", [(a, g0.port, "secret-g"), (b, g1.port, "secret-x")])
        api = api_client(owner, new_key(owner, "k")["key"])
        for _ in range(3):
            r = chat(api, stream=True)
            assert r.status_code == 200 and r.headers["x-forgeyard-replica"] == "chat#0"
            evs = events(r.text)
            assert evs[-1] == "[DONE]" and [e["choices"][0]["delta"].get("content") for e in evs[:4]] == ["t0 ", "t1 ", "t2 ", "t3 "]
        assert httpx.post(f"http://127.0.0.1:{g0.port}/v1/chat/completions", json={}, trust_env=False).status_code == 401
        assert httpx.post(f"http://127.0.0.1:{g0.port}/invocations", json={}, trust_env=False).status_code == 401
        assert len(engine.posts()) == 3 and all(p["auth"] == "Bearer secret-g" for p in engine.posts())
    finally:
        g0.close()
        g1.close()
        engine.close()


def test_fy_rg_018_a_client_gone_before_the_first_byte_closes_the_engines_connection():
    """FY-RG-018: the engine has not sent a byte yet (a long prompt being read) when the client hangs
    up: the guard notices on its own (it watches the client while waiting) and closes the engine's
    connection, so the engine does not generate for nobody."""
    engine = socket.socket()
    engine.bind(("127.0.0.1", 0))
    engine.listen()
    closed = threading.Event()

    def serve():
        conn, _ = engine.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(65536)
        conn.settimeout(10)
        try:
            while conn.recv(65536):
                pass
            closed.set()  # EOF: the guard closed it
        except OSError:
            closed.set()
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    g = GuardThread(engine.getsockname()[1])
    try:
        s = socket.create_connection(("127.0.0.1", g.port), timeout=5)
        s.sendall(f"POST /invocations HTTP/1.1\r\nHost: r\r\nAuthorization: Bearer {KEY}\r\nContent-Length: 0\r\n\r\n".encode())
        time.sleep(0.3)
        assert not closed.is_set()
        s.close()
        assert closed.wait(3)
    finally:
        g.close()
        engine.close()


@pytest.mark.skipif(os.geteuid() != 0, reason="runs the process as the workload user, which needs a root agent")
def test_fy_rg_019_a_guarded_model_process_gets_loopback_and_the_internal_port(tmp_path):
    """FY-RG-019: a model replica run as a process gets ${BIND_ADDRESS}=127.0.0.1 and ${PORT}=<internal>
    when guarded, whatever the agent's address; unguarded it gets the agent's address and the
    published port (as before)."""
    from forgeyard.agent.workloads.records import Record

    work = world_dir()
    try:
        (work / "m").mkdir()
        os.chmod(work / "m", 0o755)
        rt = ProcessRuntime(RecordStore(tmp_path / "state"), work / "work", workload_user="nobody", systemd=lambda: False)
        spec = {"name": "m", "kind": "service", "runtime": {"type": "process", "command": "sh",
                "args": ["-c", "echo bind=${BIND_ADDRESS} port=${PORT}"]}, "ports": [{"name": "http"}]}
        from forgeyard.workloads.spec import WorkloadSpec

        spec = WorkloadSpec.model_validate(spec).model_dump(mode="json")
        out = {}
        for name, ports in (("guarded", [{"name": "http", "port": None, "host_port": 31000, "internal": 31001,
                                          "address": "10.9.8.7"}]),
                            ("plain", [{"name": "http", "port": None, "host_port": 31000, "address": "10.9.8.7"}])):
            rec = Record(id=f"rec-{name}", generation=1, workload="m", ordinal=0, kind="service", runtime="process",
                         spec=spec, ports=ports, model={"kind": "local", "path": str(work / "m")},
                         paths={"path": str(work / "m")})
            rt.start(rec, "10.9.8.7")
            deadline = time.monotonic() + 10
            while rt.observe(rec).alive and time.monotonic() < deadline:
                time.sleep(0.05)
            out[name] = rt.tail(rec, 10)[0].strip()
        assert out == {"guarded": "bind=127.0.0.1 port=31001", "plain": "bind=10.9.8.7 port=31000"}
    finally:
        cleanup_world(work)
