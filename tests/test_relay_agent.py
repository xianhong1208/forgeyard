"""The agent's side of the server's relay (1.4, issue #35; FY-RELAY-010…018): "server" fetches through the
agent's own server with its credential and nothing else, "auto" switches only after a network failure (not
an HTTP error or a checksum), "direct" never relays; resume (Range), cancel and every checksum stay as they
are, so a relayed file that does not match is refused; the relay's refusals stop, its 429 waits; the
supervisor and Download to… take the relay from the desired list and report progress "through the server";
and the whole loop with a real server and agent."""

import asyncio
import hashlib
import io
import json
import os
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import pytest

import forgeyard.agent.models as am
from forgeyard.agent.models import CacheError, Cancelled, ModelCache, validate_model
from forgeyard.agent.relay import RelayClient, parse
from forgeyard.models import fetch
from tests.model_helpers import FakeHF, Repo, git_oid
from tests.test_prefetch_agent import supervisor, want
from tests.waits import until, wait_until

REV = "c" * 40
BIG = b"GGUF" + os.urandom(400_000)
README = b"# a small model\n"
CRED = "machine-credential-0123456789"
SECRET = "hf_secret_token_should_never_leave"
BASE = "/agent/v1/relay/download/j1/model"


def block(endpoint: str, repo: str = "acme/relayed", files: Optional[dict] = None, **extra) -> dict:
    files = files or {"m.gguf": BIG, "README.md": README}
    out = [{"path": p, "size": len(d), "oid": git_oid(d), **({"sha256": hashlib.sha256(d).hexdigest()}
                                                            if p.endswith(".gguf") else {})}
           for p, d in files.items()]
    return {"kind": "hf", "format": "gguf", "file": None, "repo": repo, "revision": REV, "ref": "main",
            "endpoint": endpoint, "files": out, **extra}


class FakeRelay:
    """The server's relay endpoint as the agent sees it: {path below /agent/v1/relay: bytes}, with Range;
    remembers every request's headers. Can flip a byte, cut a body once, stall, or answer a status first."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.requests: list[dict] = []
        self.tamper_at: Optional[int] = None
        self.cut_after: Optional[int] = None
        self.stall_after: Optional[int] = None
        self.first: list[tuple[int, dict, dict]] = []   # (status, headers, detail) answered before the bytes
        self._closing = threading.Event()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                relay.requests.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
                if relay.first:
                    status, headers, detail = relay.first.pop(0)
                    body = json.dumps({"detail": detail}).encode()
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                data = relay.files.get(self.path.removeprefix("/agent/v1/relay"))
                if data is None:
                    return self.send_error(404)
                if relay.tamper_at is not None:
                    data = data[:relay.tamper_at] + bytes([data[relay.tamper_at] ^ 0xFF]) + data[relay.tamper_at + 1:]
                start = 0
                rng = self.headers.get("Range")
                if rng:
                    start = int(rng.removeprefix("bytes=").rstrip("-"))
                body = data[start:]
                self.send_response(206 if rng else 200)
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for limit, stall in ((relay.cut_after, False), (relay.stall_after, True)):
                    if limit is not None and len(body) > limit:
                        if not stall:
                            relay.cut_after = None
                        self.wfile.write(body[:limit])
                        self.wfile.flush()
                        if stall:
                            relay._closing.wait(30)
                        self.close_connection = True
                        return
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self) -> RelayClient:
        return RelayClient(self.url, None, CRED, timeout=10)

    def close(self):
        self._closing.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def hf():
    server = FakeHF(Repo("acme/relayed", REV, {"m.gguf": BIG, "README.md": README}),
                    Repo("acme/second", REV, {"m.gguf": BIG, "README.md": README}))
    yield server
    server.close()


@pytest.fixture()
def relay():
    r = FakeRelay({"/download/j1/model/m.gguf": BIG, "/download/j1/model/README.md": README,
                   "/download/j2/model/m.gguf": BIG, "/download/j2/model/README.md": README})
    yield r
    r.close()


def cache_with(tmp_path, relay, **kw) -> ModelCache:
    cache = ModelCache(tmp_path / "cache", allow_private=True, **kw)
    cache.relay_client = relay.client()
    return cache


def unresolvable(host, port):
    raise fetch.FetchError(f"cannot resolve {host}: Temporary failure in name resolution")


def blob(cache: ModelCache, data: bytes):
    return cache.root / "hub" / "models--acme--relayed" / "blobs" / hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ the model cache
def test_fy_relay_010_through_the_server_with_the_credential_and_nothing_else(tmp_path, hf, relay):
    """FY-RELAY-010: mode "server": every file comes from the agent's own server (the source is never asked),
    with the machine credential and no Hugging Face token even when one is known; `via` is told "server" for each file;
    the files are verified and cached as a direct download would be. A bad relay block is ignored (direct)."""
    cache = cache_with(tmp_path, relay)
    model = validate_model(block(hf.url), [])
    via, asked = [], []
    opened = cache.relay_client.open
    cache.relay_client.open = lambda path, headers=None: (asked.append(dict(headers or {})), opened(path, headers))[1]
    cache.ensure_model(model, token=SECRET, relay={"path": BASE, "mode": "server"}, via=via.append)
    assert asked and all(set(h) <= {"Range"} for h in asked)  # the download sends the relay no token
    assert cache.is_cached(model) and blob(cache, BIG).read_bytes() == BIG
    assert hf.requests == [] and via == ["server", "server"]
    assert sorted(r["path"] for r in relay.requests) == [f"{BASE}/README.md", f"{BASE}/m.gguf"]
    assert all(r["authorization"] == f"Bearer {CRED}" for r in relay.requests)
    assert SECRET not in json.dumps(relay.requests)
    # And the client itself passes on nothing but Range, whatever it is handed.
    with relay.client().open(f"{BASE}/README.md", {"Authorization": f"Bearer {SECRET}", "X-Hf-Token": SECRET,
                                                     "Range": "bytes=2-"}) as resp:
        assert resp.status == 206 and resp.read() == README[2:]
    assert relay.requests[-1]["authorization"] == f"Bearer {CRED}" and relay.requests[-1]["range"] == "bytes=2-"
    assert SECRET not in json.dumps(relay.requests[-1])
    assert parse({"relay": {"path": BASE, "mode": "server"}}) == {"path": BASE, "mode": "server"}
    for bad in ({"path": "https://evil.example/x", "mode": "server"}, {"path": BASE + "/../../x", "mode": "server"},
                {"path": BASE, "mode": "always"}, {"path": "/agent/v1/relay/download/j1/model?u=x", "mode": "auto"},
                "server", None):
        assert parse({"relay": bad}) is None, bad


def test_fy_relay_011_a_cut_relay_resumes_and_a_changed_byte_is_refused(tmp_path, hf, relay, monkeypatch):
    """FY-RELAY-011: a relay cut after 100 000 bytes resumes from there (Range from the partial file); a relay
    that changes one byte fails the SHA-256 check: nothing is cached, the partial file is deleted."""
    monkeypatch.setattr(am.time, "sleep", lambda s: None)
    cache = cache_with(tmp_path, relay)
    model = validate_model(block(hf.url), [])
    relay.cut_after = 100_000
    cache.ensure_model(model, relay={"path": BASE, "mode": "server"})
    gguf = [r for r in relay.requests if r["path"].endswith("m.gguf")]
    assert [r.get("range") for r in gguf] == [None, "bytes=100000-"]
    assert blob(cache, BIG).read_bytes() == BIG
    other = ModelCache(tmp_path / "other", allow_private=True)
    other.relay_client = relay.client()
    relay.tamper_at = 123_456
    with pytest.raises(CacheError, match="SHA-256 mismatch"):
        other.ensure_model(model, relay={"path": BASE, "mode": "server"})
    assert not blob(other, BIG).exists() and list((other.root / ".partial").iterdir()) == []
    assert not other.is_cached(model)


def test_fy_relay_012_auto_switches_after_a_network_failure_only(tmp_path, hf, relay, monkeypatch):
    """FY-RELAY-012: mode "auto": directly first; a name that does not resolve switches to the server at once
    (no retries against the source), and the host is remembered so the next download goes through the server
    without trying. An HTTP error from the source, or a file that fails its checksum, never switches."""
    calls = []

    def resolver(host, port):
        calls.append(host)
        return unresolvable(host, port)

    cache = cache_with(tmp_path, relay, resolver=resolver)
    via = []
    t0 = time.monotonic()
    cache.ensure_model(validate_model(block(hf.url), []), relay={"path": BASE, "mode": "auto"}, via=via.append)
    assert time.monotonic() - t0 < 5 and len(calls) == 1 and set(via) == {"server"} and hf.requests == []
    second = validate_model(block(hf.url, "acme/second"), [])
    cache.ensure_model(second, relay={"path": "/agent/v1/relay/download/j2/model", "mode": "auto"})
    assert len(calls) == 1 and cache.is_cached(second)  # straight through the server: the host is remembered
    # From the source itself: a 404 and a wrong checksum are answers, not network failures.
    monkeypatch.setattr(am, "RETRIES", 0)
    plain = cache_with(tmp_path / "p", relay)
    before = len(relay.requests)
    with pytest.raises(CacheError, match="HTTP 404"):
        plain.ensure_model(validate_model(block(hf.url, "acme/missing"), []), relay={"path": BASE, "mode": "auto"})
    bad = block(hf.url)
    bad["files"][0]["sha256"] = "0" * 64
    with pytest.raises(CacheError, match="SHA-256 mismatch"):
        plain.ensure_model(validate_model(bad, []), relay={"path": BASE, "mode": "auto"})
    assert len(relay.requests) == before


def test_fy_relay_013_direct_never_relays(tmp_path, hf, relay, monkeypatch):
    """FY-RELAY-013: a block without `relay` (the machine is set to "direct only", or an older server) is
    fetched directly even when the source cannot be reached: the relay is never asked."""
    monkeypatch.setattr(am, "RETRIES", 0)
    cache = cache_with(tmp_path, relay, resolver=unresolvable)
    with pytest.raises(CacheError, match="download failed: cannot resolve"):
        cache.ensure_model(validate_model(block(hf.url), []), relay=None)
    assert relay.requests == []
    assert cache.relay_client.route(None, hf.url) == "direct"


def test_fy_relay_014_the_relays_refusals_stop_its_429_waits_its_5xx_retries(tmp_path, hf, relay, monkeypatch):
    """FY-RELAY-014: a 404 ("not wanted any more") or 403 from the relay ends the download at once with the
    server's words; 429 waits Retry-After without using up a retry; a 502 is retried like a network error."""
    monkeypatch.setattr(am, "RETRIES", 1)
    cache = cache_with(tmp_path, relay)
    model = validate_model(block(hf.url), [])
    relay.first = [(404, {}, {"code": "relay_not_wanted", "message": "No download to this machine with that id."})]
    with pytest.raises(CacheError, match="through the server: No download to this machine with that id"):
        cache.ensure_model(model, relay={"path": BASE, "mode": "server"})
    assert len(relay.requests) == 1
    monkeypatch.setattr(am, "RETRIES", 0)
    relay.first = [(429, {"Retry-After": "1"}, {"code": "relay_busy", "message": "busy"})]
    t0 = time.monotonic()
    cache.ensure_model(model, relay={"path": BASE, "mode": "server"})
    assert cache.is_cached(model) and time.monotonic() - t0 >= 0.9
    monkeypatch.setattr(am, "RETRIES", 1)
    again = cache_with(tmp_path / "b", relay)
    relay.first = [(502, {}, {"code": "source_unreachable", "message": "the server could not reach huggingface.co"})]
    again.ensure_model(model, relay={"path": BASE, "mode": "server"})
    assert again.is_cached(model)


def test_fy_relay_015_engines_and_url_models_come_through_it_checked(tmp_path, relay):
    """FY-RELAY-015: an engine archive through the server is checked against its pinned SHA-256 and unpacked;
    a URL model too, and its recorded SHA-256 (D8) still refuses a different file."""
    buf = io.BytesIO()
    prog = b"#!/bin/sh\necho engine\n"
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("eng/server")
        info.size, info.mode = len(prog), 0o755
        tar.addfile(info, io.BytesIO(prog))
    archive = buf.getvalue()
    relay.files["/replica/r1/engine"] = archive
    relay.files["/replica/r1/model"] = BIG
    cache = cache_with(tmp_path, relay, resolver=unresolvable)
    engine = {"name": "eng-1", "url": "https://github.com/acme/eng/releases/download/b1/eng.tar.gz",
              "sha256": hashlib.sha256(archive).hexdigest(), "binary": "eng/server"}
    exe = cache.ensure_engine(engine, relay={"path": "/agent/v1/relay/replica/r1/engine", "mode": "server"})
    assert exe.read_bytes() == prog
    url = {"kind": "url", "url": "https://models.example/m.gguf", "files": [{"path": "m.gguf", "size": len(BIG)}]}
    model = validate_model(url, [])
    cache.ensure_model(model, relay={"path": "/agent/v1/relay/replica/r1/model", "mode": "server"})
    assert cache.is_cached(model)
    other = cache_with(tmp_path / "o", relay)
    pinned = validate_model({**url, "files": [{"path": "m.gguf", "size": len(BIG), "sha256": "1" * 64}]}, [])
    with pytest.raises(CacheError, match="SHA-256 mismatch"):
        other.ensure_model(pinned, relay={"path": "/agent/v1/relay/replica/r1/model", "mode": "server"})
    with pytest.raises(CacheError, match="engine archive SHA-256 mismatch"):
        other.ensure_engine({**engine, "sha256": "2" * 64}, relay={"path": "/agent/v1/relay/replica/r1/engine",
                                                                  "mode": "server"})


def test_fy_relay_016_a_cancel_ends_a_stalled_relay_at_once(tmp_path, hf, relay):
    """FY-RELAY-016: a relay that stalls half-way is ended by a cancel within a second; the partial file stays
    for the next try."""
    cache = cache_with(tmp_path, relay)
    model = validate_model(block(hf.url), [])
    relay.stall_after = 150_000
    cancel = threading.Event()
    errors = []

    def run():
        try:
            cache.ensure_model(model, relay={"path": BASE, "mode": "server"}, cancel=cancel)
        except Cancelled as e:
            errors.append(e)

    t = threading.Thread(target=run)
    t.start()
    part = cache.root / ".partial" / f"{hashlib.sha256(BIG).hexdigest()}.part"
    wait_until(lambda: part.exists() and part.stat().st_size >= 150_000, "the stalled relay")
    t0 = time.monotonic()
    cancel.set()
    t.join(5)
    assert not t.is_alive() and errors and time.monotonic() - t0 < 2
    assert part.stat().st_size == 150_000


# ------------------------------------------------------------------ the supervisor and Download to…
def test_fy_relay_017_desired_relays_are_used_and_progress_says_through_the_server(tmp_path, hf, relay, monkeypatch):
    """FY-RELAY-017: a download in the desired list with `relay` comes through the server, its progress says
    `via` "server" and "through the server"; the relay is kept beside a replica's record (never in it); the
    agent lists "relay" when it has a model cache."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory

    monkeypatch.setenv(SIMULATION_ENV, "1")
    relay_cache = cache_with(tmp_path, relay)
    sup = supervisor(tmp_path, relay_cache)
    item = {"id": "j1", "model": {**block(hf.url), "relay": {"path": BASE, "mode": "server"}}}
    seen = []
    report = sup.prefetch._progress

    def recorded(job, *a):
        report(job, *a)
        seen.append(dict(job.download or {}, reason=job.reason))

    sup.prefetch._progress = recorded

    async def scenario():
        want(sup, item)
        await until(lambda: sup.prefetch.jobs["j1"].state == "done", "the download", 20)

    asyncio.run(scenario())
    assert any(s.get("via") == "server" and "through the server" in (s.get("reason") or "") for s in seen)
    assert hf.requests == [] and relay_cache.is_cached(validate_model(block(hf.url), []))
    sup.set_desired({"revision": 0, "replicas": [{"id": "r1", "generation": 1, "workload": "w", "kind": "service",
                                                  "spec": {}, "model": {**block(hf.url),
                                                                        "relay": {"path": BASE, "mode": "auto"}}}]})
    assert sup.relays["r1"] == {"model": {"path": BASE, "mode": "auto"}} and "relay" not in sup.desired["r1"]["model"]
    sup.set_desired({"revision": 0, "replicas": []})
    assert sup.relays == {}
    agent = Agent("http://127.0.0.1:1", CRED, SimulatedInventory("nvidia-rtx4090x2"), supervisor=sup)
    assert "relay" in agent.hello({"accelerators": [], "collectors": {}})["features"]
    assert relay_cache.relay_client.host == "127.0.0.1" and relay_cache.relay_client.port == 1


def test_fy_relay_018_a_real_agent_that_cannot_reach_the_source_downloads_through_the_server(monkeypatch, tmp_path):
    """FY-RELAY-018: over real HTTP and WebSocket, an agent whose machine resolves no name, set to "always
    through the server", is asked to Download to… a gated model: the server fetches it with its own token (no
    desired list ever carries it to the agent), a cut in the middle resumes from the partial file, the
    download ends "done" and "via" the server, and the cache lists the model."""
    from forgeyard.agent.backoff import Backoff
    from forgeyard.agent.client import Agent, join
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from forgeyard.db import new_session
    from forgeyard.models import hf_token
    from tests.conftest import OWNER, SETUP_TOKEN
    from tests.model_helpers import add_catalog
    from tests.test_agent import LiveServer, _free_port, _http, _wait

    data = BIG
    hf = FakeHF(Repo("acme/gated-gguf", REV, {"g.gguf": data}, gated=True))
    monkeypatch.setenv(SIMULATION_ENV, "1")
    monkeypatch.setenv("HF_ENDPOINT", hf.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    root = tmp_path / "cache"
    try:
        with LiveServer(port):
            _, cookie = _http(port, "POST", "/api/auth/setup",
                              {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
            db = new_session()
            hf_token.put(db, FakeHF.TOKEN, "owner")
            db.commit()
            db.close()
            joined_ = join(server, _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"], name="off")
            node = joined_["node_id"]
            mid = add_catalog("gated-gguf", {"kind": "hf", "repo": "acme/gated-gguf", "ref": "main", "revision": REV,
                                             "format": "gguf", "file": "g.gguf", "size_bytes": len(data),
                                             "files": [{"path": "g.gguf", "size": len(data),
                                                        "sha256": hashlib.sha256(data).hexdigest()}]},
                              task="chat", fmt="gguf", source="hf://acme/gated-gguf")

            def api(method, path, body=None):
                return _http(port, method, path, body, cookie)[0]

            async def scenario():
                state = tmp_path / "agent"
                rt = ProcessRuntime(RecordStore(state), tmp_path / "work", euid=lambda: 4242)
                models = ModelCache(root, allow_private=True, resolver=unresolvable)
                sup = Supervisor(state, {"process": rt}, models=models, tick=0.1, port_range=(31920, 31930))
                agent = Agent(server, joined_["credential"], SimulatedInventory("nvidia-rtx4090x2"),
                              backoff=Backoff(base=0.05, cap=0.2), supervisor=sup)
                run = asyncio.create_task(agent.run())
                told = []
                desired = sup.set_desired
                sup.set_desired = lambda msg: (told.append(json.dumps(msg)), desired(msg))[1]
                try:
                    assert await _wait(lambda: "relay" in api("GET", "/api/machines")["machines"][0]["features"])
                    await asyncio.to_thread(api, "PUT", f"/api/machines/{node}/fetch-via", {"fetch_via": "server"})
                    hf.fail_after = 100_000
                    first = (await asyncio.to_thread(api, "POST", "/api/downloads",
                                                     {"model_id": mid, "machine_ids": [node], "engine": False}))["downloads"][0]

                    def done():
                        d = next(x for x in api("GET", f"/api/downloads?machine_id={node}")["downloads"]
                                 if x["id"] == first["id"])
                        return d["state"] in ("done", "failed") and d

                    d = await asyncio.to_thread(wait_until, done, "the relayed download", 30)
                    assert d["state"] == "done" and d["via"] == "server", d
                    cdn = hf.downloads("g.gguf")
                    assert [r["range"] for r in cdn] == [None, "bytes=100000-"]
                    asked = [r for r in hf.requests if "/cdn/" not in r["path"]]
                    assert asked and all(r["auth"] == f"Bearer {FakeHF.TOKEN}" for r in asked)
                    assert told and not any(FakeHF.TOKEN in m for m in told)  # the token stayed on the server
                    listing = await asyncio.to_thread(wait_until, lambda: api("GET", f"/api/machines/{node}/cache")["models"],
                                                      "the cache listing", 10)
                    assert listing[0]["repo"] == "acme/gated-gguf"
                finally:
                    agent.stop()
                    await asyncio.wait_for(run, 10)

            asyncio.run(scenario())
    finally:
        hf.close()
