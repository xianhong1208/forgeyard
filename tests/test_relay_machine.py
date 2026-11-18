"""A model file from another machine's cache, through the server (1.4.1, issue #104; FY-RFM-001…014): which
machines may send it, the source agent's lookup on its own disk and its POST, the server's pipe into the
target's unchanged relay GET (Range, whole-file hashing with the last piece held back, a bad source marked and
the target's file started over), the fallback order machine -> next machine -> internet, the limits, cancel,
the progress that names the source, and two whole loops with real agents over real HTTP and WebSocket."""

import asyncio
import hashlib
import http.client
import json
import logging
import os
import threading
import time

import pytest

from forgeyard.agent import relay as relay_client
from forgeyard.agent import share
from forgeyard.agent.models import ModelCache, validate_model
from forgeyard.config import get_settings
from forgeyard.db import AuditEvent, ModelDownload, Node, new_session, utcnow
from forgeyard.machines import peers, relay
from forgeyard.machines.hub import Connection, hub
from forgeyard.machines.messages import StatusIn
from forgeyard.models import sources
from tests.model_helpers import FakeHF, Repo, add_catalog, git_oid
from tests.test_placement_fetch import set_node
from tests.waits import wait_until

REV = "d" * 40
DATA = b"GGUF" + os.urandom(600_000)
README = b"# shared model\n"
URL_DATA = b"GGUF" + os.urandom(200_000)
RELAY = ["workloads", "models", "cache_admin", "prefetch", "relay"]


def hf_block(endpoint: str) -> dict:
    files = [{"path": "m.gguf", "size": len(DATA), "oid": git_oid(DATA), "sha256": hashlib.sha256(DATA).hexdigest()},
             {"path": "README.md", "size": len(README), "oid": git_oid(README)}]
    return {"kind": "hf", "repo": "acme/shared", "revision": REV, "ref": "main", "files": files, "endpoint": endpoint}


def catalog_meta() -> dict:
    files = [{"path": "m.gguf", "size": len(DATA), "oid": git_oid(DATA), "sha256": hashlib.sha256(DATA).hexdigest()},
             {"path": "README.md", "size": len(README), "oid": git_oid(README)}]
    return {"kind": "hf", "repo": "acme/shared", "ref": "main", "revision": REV, "format": "gguf",
            "size_bytes": len(DATA) + len(README), "files": files}


def add_model() -> int:
    return add_catalog("shared", catalog_meta(), task="chat", fmt="gguf", source="hf://acme/shared")


def filled_cache(root, endpoint: str) -> ModelCache:
    """A model cache with the model downloaded (and checked) as an agent does it."""
    cache = ModelCache(root, allow_private=True)
    cache.ensure_model(validate_model(hf_block(endpoint), []))
    return cache


def blob(cache: ModelCache, data: bytes):
    return cache.root / "hub" / "models--acme--shared" / "blobs" / hashlib.sha256(data).hexdigest()


def unresolvable(host, port):
    from forgeyard.models import fetch

    raise fetch.FetchError(f"cannot resolve {host}: Temporary failure in name resolution")


def download_row(model_id: int, node_id: int) -> tuple[int, str]:
    db = new_session()
    try:
        row = ModelDownload(model_id=model_id, node_id=node_id, state="downloading", via="direct", requested_by="owner",
                            created_at=utcnow())
        db.add(row)
        db.commit()
        return row.id, row.uid
    finally:
        db.close()


class AgentThread:
    """A real agent (supervisor, model cache, relay client) on its own event loop in a thread."""

    def __init__(self, tmp_path, server: str, credential: str, cache: ModelCache, name: str, ports: tuple[int, int]):
        from forgeyard.agent.backoff import Backoff
        from forgeyard.agent.client import Agent
        from forgeyard.agent.inventory import SimulatedInventory
        from forgeyard.agent.workloads.process import ProcessRuntime
        from forgeyard.agent.workloads.records import RecordStore
        from forgeyard.agent.workloads.supervisor import Supervisor

        state = tmp_path / f"{name}-state"
        rt = ProcessRuntime(RecordStore(state), tmp_path / f"{name}-work", euid=lambda: 4242)
        self.sup = Supervisor(state, {"process": rt}, models=cache, tick=0.1, port_range=ports)
        self.agent = Agent(server, credential, SimulatedInventory("nvidia-rtx4090x2"),
                           backoff=Backoff(base=0.05, cap=0.2), supervisor=self.sup)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=lambda: self.loop.run_until_complete(self.agent.run()), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.agent.stop)
        self.thread.join(15)
        assert not self.thread.is_alive(), "the agent did not stop"


@pytest.fixture()
def world(monkeypatch, tmp_path):
    """A live server, a fake Hugging Face, the catalog model, and two machines joined: `src` (whose cache has
    the model, run by a real agent while the test says) and `tgt` (no agent; set to fetch through the server)."""
    from forgeyard.agent.client import join
    from forgeyard.agent.inventory import SIMULATION_ENV
    from tests.conftest import OWNER, SETUP_TOKEN
    from tests.test_agent import LiveServer, _free_port, _http

    hf = FakeHF(Repo("acme/shared", REV, {"m.gguf": DATA, "README.md": README}))
    monkeypatch.setenv(SIMULATION_ENV, "1")
    monkeypatch.setenv("HF_ENDPOINT", hf.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    get_settings.cache_clear()
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    live = LiveServer(port).__enter__()
    try:
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})

        def token():
            return _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"]

        w = type("World", (), {})()
        w.hf, w.port, w.server, w.cookie, w.tmp = hf, port, server, cookie, tmp_path
        w.src = join(server, token(), name="src")
        w.src2 = join(server, token(), name="src2")
        w.tgt = join(server, token(), name="tgt")
        set_node(w.tgt["node_id"], features=RELAY, fetch_via="server")
        w.model_id = add_model()
        w.cache = filled_cache(tmp_path / "src-cache", hf.url)
        hf.requests.clear()
        w.api = lambda method, path, body=None: _http(port, method, path, body, cookie)[0]
        yield w
    finally:
        live.__exit__(None, None, None)
        hf.close()


def source(w, which="src", cache=None, ports=(31940, 31949)) -> AgentThread:
    return AgentThread(w.tmp, w.server, getattr(w, which)["credential"], cache or w.cache, which, ports)


def connected_source(w, node_id: int, what="relay_source"):
    def ready():
        conn = hub.get(node_id)
        db = new_session()
        try:
            cached = (db.get(Node, node_id).model_cache or {}).get("cached") or []
        finally:
            db.close()
        return conn is not None and what in conn.features and cached
    wait_until(ready, "the source agent connected with its cache", 15)


def get(w, path: str, credential: str, rng=None, timeout=30) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", w.port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {credential}", **({"Range": rng} if rng else {})})
        resp = conn.getresponse()
        body = b""
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                body += chunk
        except (http.client.IncompleteRead, OSError):
            pass
        return resp.status, dict(resp.headers), body
    finally:
        conn.close()


def post(w, path: str, credential, body: bytes = b"", length=None, origin=None) -> tuple[int, dict]:
    """A source's POST. The server answers a refusal at once and closes the connection, so writing the rest of the
    body may fail: the answer is read all the same, as the agent would."""
    conn = http.client.HTTPConnection("127.0.0.1", w.port, timeout=20)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Length", str(len(body) if length is None else length))
        if credential:
            conn.putheader("Authorization", f"Bearer {credential}")
        if origin:
            conn.putheader("Origin", origin)
        conn.endheaders()
        try:
            for i in range(0, len(body), 65536):
                conn.send(body[i:i + 65536])
        except (BrokenPipeError, ConnectionResetError):
            pass
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


def cdn(w, part="m.gguf") -> list:
    return [r["range"] for r in w.hf.downloads(part)]


def audit_rows(action: str) -> list[AuditEvent]:
    db = new_session()
    try:
        return list(db.query(AuditEvent).filter_by(action=action).all())
    finally:
        db.close()


# ------------------------------------------------------------------ the server's pipe, over real HTTP
def test_fy_rfm_001_a_file_another_machine_has_comes_from_it_whole_or_from_a_byte(world, caplog):
    """FY-RFM-001: a machine set to fetch through the server asks the relay for a file (its request unchanged): the
    server asks the source machine, whose agent finds the file on its own disk and POSTs it; the target gets the
    file whole (200, the server's Content-Length) or from a byte (206, Content-Range built from the model's
    size), a small file by its git blob id too; Hugging Face is never asked; the log names the source."""
    w = world
    caplog.set_level(logging.INFO, logger="forgeyard.relay")
    _, uid = download_row(w.model_id, w.tgt["node_id"])
    base = f"/agent/v1/relay/download/{uid}/model"
    with source(w):
        connected_source(w, w.src["node_id"])
        status, headers, body = get(w, f"{base}/m.gguf", w.tgt["credential"])
        assert status == 200 and body == DATA and headers["content-length"] == str(len(DATA))
        status, headers, body = get(w, f"{base}/m.gguf", w.tgt["credential"], rng="bytes=1000-")
        assert status == 206 and body == DATA[1000:]
        assert headers["content-range"] == f"bytes 1000-{len(DATA) - 1}/{len(DATA)}"
        status, headers, body = get(w, f"{base}/m.gguf", w.tgt["credential"], rng="bytes=10-19")
        assert status == 206 and body == DATA[10:20] and headers["content-range"] == f"bytes 10-19/{len(DATA)}"
        status, _, body = get(w, f"{base}/README.md", w.tgt["credential"])
        assert status == 200 and body == README
        assert w.hf.requests == []
        status, _, _ = get(w, f"{base}/m.gguf", w.tgt["credential"], rng=f"bytes={len(DATA)}-")
        assert status == 416 and cdn(w) == [f"bytes={len(DATA)}-"]  # past the end: as before, from the internet
    assert any("Relayed acme/shared@dddddddddddd/m.gguf from src to tgt" in m for m in caplog.messages)
    assert sum("comes from src" in m for m in caplog.messages) == 4
    assert relay.slots.by_node == {} and peers._sending == {} and peers._pipes == {}


def test_fy_rfm_002_the_send_endpoint_takes_only_the_chosen_sources_one_post_of_the_right_length(world, monkeypatch):
    """FY-RFM-002: POST /agent/v1/relay-send/<ticket> is refused to a browser (403), an unknown credential (401),
    another machine's credential (404, the target's included), an unknown ticket (404), a length other than the
    one asked for (400, and the next source or the internet is used), and a second POST (409); the chosen source's
    POST of the right length goes through to the target."""
    w = world
    _, uid = download_row(w.model_id, w.tgt["node_id"])
    asked, release = [], threading.Event()

    def held(self, message, cancel=None):
        asked.append(message)
        release.wait(20)
        return {"ok": False, "code": "failed", "reason": "the test sent it"}

    monkeypatch.setattr(share.Sender, "send", held)
    monkeypatch.setattr(peers, "SOURCE_TIMEOUT", 15.0)
    out = {}
    with source(w):
        connected_source(w, w.src["node_id"])
        t = threading.Thread(target=lambda: out.update(r=get(w, f"/agent/v1/relay/download/{uid}/model/m.gguf",
                                                               w.tgt["credential"])))
        t.start()
        wait_until(lambda: asked, "the source asked")
        msg = asked[0]
        path = msg["path"]
        assert msg["item"] == {"kind": "hf", "repo": "acme/shared", "revision": REV, "file": "m.gguf",
                               "blob": hashlib.sha256(DATA).hexdigest(), "size": len(DATA)}
        assert msg["offset"] == 0 and msg["length"] == len(DATA)
        assert post(w, path, w.src["credential"], DATA, origin="http://evil.example")[0] == 403
        assert post(w, path, "not-a-credential", DATA)[0] == 401
        assert post(w, path, w.tgt["credential"], DATA)[0] == 404
        assert post(w, path, w.src2["credential"], DATA)[0] == 404
        assert post(w, path + "x", w.src["credential"], DATA)[0] == 404
        assert post(w, "/agent/v1/relay-send/" + "A" * 32, w.src["credential"], DATA)[0] == 404
        first = http.client.HTTPConnection("127.0.0.1", w.port, timeout=20)
        first.putrequest("POST", path)
        first.putheader("Authorization", f"Bearer {w.src['credential']}")
        first.putheader("Content-Length", str(len(DATA)))
        first.endheaders()
        first.send(DATA[:1000])
        wait_until(lambda: peers._pipes[path.rsplit("/", 1)[1]].claimed, "the first POST under way")
        status, answer = post(w, path, w.src["credential"], DATA)
        assert status == 409 and answer["detail"]["code"] == "send_claimed"
        first.send(DATA[1000:])
        resp = first.getresponse()
        assert resp.status == 200 and json.loads(resp.read()) == {"ok": True, "bytes": len(DATA)}
        first.close()
        t.join(20)
        assert out["r"][0] == 200 and out["r"][2] == DATA
        wait_until(lambda: peers._pipes == {}, "the pipe closed")
        assert post(w, path, w.src["credential"], DATA)[0] == 404  # the ticket is gone with the pipe
        # A wrong length: refused before a byte moves; the target then gets the file from the internet.
        asked.clear()
        t = threading.Thread(target=lambda: out.update(r=get(w, f"/agent/v1/relay/download/{uid}/model/m.gguf",
                                                               w.tgt["credential"])))
        t.start()
        wait_until(lambda: asked, "the source asked again")
        path = asked[0]["path"]
        status, answer = post(w, path, w.src["credential"], DATA[:-1])
        assert status == 400 and answer["detail"]["code"] == "send_length"
        assert post(w, path, w.src["credential"], DATA)[0] in (404, 409)
        t.join(20)
        assert out["r"][0] == 200 and out["r"][2] == DATA and cdn(w) == [None]
        release.set()


def test_fy_rfm_003_not_found_busy_or_silent_sources_are_skipped_then_the_internet(world, monkeypatch, caplog):
    """FY-RFM-003: the first source answering "not_found" (a revision may hold only some files) or "busy" makes
    the server ask the next one, then the internet, before the target gets a byte; a source that does not start
    sending within SOURCE_TIMEOUT is marked slow for that file and not asked for it again; the ranking prefers a
    machine listing the very GGUF file, then fewer sends, then the latest heartbeat."""
    w = world
    caplog.set_level(logging.INFO, logger="forgeyard.relay")
    monkeypatch.setattr(peers, "SOURCE_TIMEOUT", 1.0)
    _, uid = download_row(w.model_id, w.tgt["node_id"])
    url = f"/agent/v1/relay/download/{uid}/model/m.gguf"
    real = share.Sender.send
    plan = {}

    def scripted(self, message, cancel=None):
        name = "src" if self.cache is w.cache else "src2"
        what = plan.get(name, "send")
        if what == "silent":
            time.sleep(2.0)
            return {"ok": False, "code": "failed", "reason": "too late"}
        if what in ("not_found", "busy"):
            return {"ok": False, "code": what, "reason": "scripted"}
        return real(self, message, cancel)

    monkeypatch.setattr(share.Sender, "send", scripted)
    other = filled_cache(w.tmp / "src2-cache", w.hf.url)
    w.hf.requests.clear()
    with source(w), source(w, "src2", other, ports=(31950, 31959)):
        connected_source(w, w.src["node_id"])
        connected_source(w, w.src2["node_id"])
        plan.update(src="not_found", src2="busy")
        status, _, body = get(w, url, w.tgt["credential"])
        assert status == 200 and body == DATA and cdn(w) == [None]
        assert any("trying the next machine" in m for m in caplog.messages)
        assert any("trying the internet" in m for m in caplog.messages)
        plan.update(src="not_found", src2="send")
        status, _, body = get(w, url, w.tgt["credential"])
        assert status == 200 and body == DATA and cdn(w) == [None]
        assert peers.source_of("download", uid) == (w.src2["node_id"], "src2")
        plan.update(src="silent", src2="silent")
        t0 = time.monotonic()
        status, _, body = get(w, url, w.tgt["credential"])
        assert status == 200 and body == DATA and cdn(w) == [None, None]
        assert time.monotonic() - t0 < 6
        key = "hub/models--acme--shared@" + REV + "/m.gguf"
        assert peers.marked(w.src["node_id"], key) and peers.marked(w.src2["node_id"], key)
        plan.clear()
        status, _, body = get(w, url, w.tgt["credential"])  # both marked: straight to the internet
        assert status == 200 and cdn(w) == [None, None, None]
        assert peers.source_of("download", uid) is None
    c = [peers.Candidate(1, "a", False, 100.0), peers.Candidate(2, "b", True, 50.0), peers.Candidate(3, "c", False, 200.0)]
    assert [x.name for x in peers.ranked(c)] == ["b", "c", "a"]
    peers._sending[3] = 1
    assert [x.name for x in peers.ranked(c)] == ["b", "a", "c"]


def test_fy_rfm_004_a_copy_that_does_not_match_is_never_sent_whole_and_its_source_is_marked(world, caplog):
    """FY-RFM-004: the server hashes a whole file from a machine and holds its last piece back: a source whose
    copy does not match its checksum never gets a complete file to the target; it is marked for that file for an
    hour, the audit log names it (`node.relay_copy_refused`), and the target's next request (its resume) is
    answered whole, from the internet, so it starts the file over."""
    w = world
    caplog.set_level(logging.INFO, logger="forgeyard.relay")
    _, uid = download_row(w.model_id, w.tgt["node_id"])
    url = f"/agent/v1/relay/download/{uid}/model/m.gguf"
    b = blob(w.cache, DATA)
    os.chmod(b, 0o644)
    bad = bytearray(DATA)
    bad[len(bad) // 2] ^= 0xFF
    b.write_bytes(bytes(bad))
    with source(w):
        connected_source(w, w.src["node_id"])
        status, _, body = get(w, url, w.tgt["credential"])
        assert status == 200 and len(body) < len(DATA) and body == bytes(bad)[:len(body)]
        key = "hub/models--acme--shared@" + REV + "/m.gguf"
        assert "checksum" in (peers.marked(w.src["node_id"], key) or "")
        rows = audit_rows("node.relay_copy_refused")
        assert len(rows) == 1 and rows[0].target == "src" and rows[0].detail["to"] == "tgt"
        assert any("src's copy of acme/shared@dddddddddddd/m.gguf does not match" in m for m in caplog.messages)
        assert any(m.startswith("Relay of acme/shared@dddddddddddd/m.gguf to tgt cut short: the source's bytes hash to ")
                   and f"of {len(DATA)} bytes sent from machine src" in m for m in caplog.messages), caplog.messages
        status, headers, body = get(w, url, w.tgt["credential"], rng=f"bytes={len(body)}-")
        assert status == 200 and body == DATA and "content-range" not in headers
        assert cdn(w) == [None]
        status, _, body = get(w, url, w.tgt["credential"], rng="bytes=5-")
        assert status == 206 and body == DATA[5:] and cdn(w) == [None, "bytes=5-"]


def test_fy_rfm_005_limits_per_source_and_cancel_during_a_send(world, monkeypatch, caplog):
    """FY-RFM-005: a source already sending MAX_PER_SOURCE files is not asked (the internet is used); a cancelled
    download ends a send under way within the relay's recheck: the target's answer ends, the source's POST is
    ended (the server closes its connection, so the source stops at once instead of sending the rest of the file
    to be thrown away), and every slot and ticket is given back."""
    w = world
    caplog.set_level(logging.INFO, logger="forgeyard.relay")
    monkeypatch.setattr(relay, "RECHECK", 0.2)
    row_id, uid = download_row(w.model_id, w.tgt["node_id"])
    url = f"/agent/v1/relay/download/{uid}/model/m.gguf"
    real_post = relay_client.RelayClient.post
    sends = []

    def slow(self, path, chunks, length):
        record = {"sent": 0, "error": None, "ended": None}
        sends.append(record)

        def trickle():
            for chunk in chunks:
                for i in range(0, len(chunk), 8192):
                    time.sleep(0.05)
                    record["sent"] += len(chunk[i:i + 8192])
                    yield chunk[i:i + 8192]
        try:
            return real_post(self, path, trickle(), length)
        except Exception as e:
            record["error"] = e
            raise
        finally:
            record["ended"] = time.monotonic()

    with source(w):
        connected_source(w, w.src["node_id"])
        peers._sending[w.src["node_id"]] = peers.MAX_PER_SOURCE
        status, _, body = get(w, url, w.tgt["credential"])
        assert status == 200 and body == DATA and cdn(w) == [None]
        peers._sending.clear()
        monkeypatch.setattr(relay_client.RelayClient, "post", slow)
        conn = http.client.HTTPConnection("127.0.0.1", w.port, timeout=20)
        conn.request("GET", url, headers={"Authorization": f"Bearer {w.tgt['credential']}"})
        resp = conn.getresponse()
        assert resp.status == 200
        got = resp.read1(65536)
        while len(got) < 40_000:
            got += resp.read1(65536)
        assert peers._sending == {w.src["node_id"]: 1}
        t0 = time.monotonic()
        w.api("POST", f"/api/downloads/{row_id}/cancel", {})
        try:
            while resp.read1(65536):
                pass
        except (OSError, http.client.HTTPException):
            pass
        assert time.monotonic() - t0 < 5
        conn.close()
        wait_until(lambda: peers._sending == {} and peers._pipes == {} and relay.slots.by_node == {},
                   "every slot and ticket given back")
        wait_until(lambda: sends and sends[-1]["ended"], "the source's send ended", 10)
        assert sends[-1]["error"] is not None and sends[-1]["sent"] < len(DATA) // 2, sends[-1]
        assert sends[-1]["ended"] - t0 < 5
        wait_until(lambda: any("no longer wanted" in m and "from src" in m for m in caplog.messages), "the log line")
    assert cdn(w) == [None]  # the cancelled one never went to the internet


def test_fy_rfm_015_a_source_that_stalls_or_sends_too_much_ends_the_answer_short(world, monkeypatch):
    """FY-RFM-015: a source that sends nothing more for STALL seconds ends the target's answer short (it resumes
    from what it has); a body longer than announced is never passed on whole."""
    w = world
    monkeypatch.setattr(peers, "STALL", 1.0)
    _, uid = download_row(w.model_id, w.tgt["node_id"])
    real_post = relay_client.RelayClient.post

    def stalls(self, path, chunks, length):
        def half():
            sent = 0
            for chunk in chunks:
                if sent > length // 2:
                    time.sleep(4)
                    raise relay_client.SendStopped("the test stopped")
                sent += len(chunk)
                yield chunk
        return real_post(self, path, half(), length)

    monkeypatch.setattr(share, "CHUNK", 65536)
    monkeypatch.setattr(relay_client.RelayClient, "post", stalls)
    with source(w):
        connected_source(w, w.src["node_id"])
        t0 = time.monotonic()
        status, _, body = get(w, f"/agent/v1/relay/download/{uid}/model/m.gguf", w.tgt["credential"])
        # (what arrived, less the last piece, which is held back: a whole file is hashed on its way)
        assert status == 200 and 0 < len(body) < len(DATA) and body == DATA[:len(body)]
        assert time.monotonic() - t0 < 3.5
    wait_until(lambda: peers._pipes == {} and peers._sending == {}, "the pipe closed")
    # Longer than announced (a server below that does not enforce Content-Length): stopped there.
    pipe = peers.Pipe("T" * 32, 7, "seven", 10)
    peers._pipes[pipe.ticket] = pipe
    monkeypatch.setattr(peers, "_node_of", lambda credential: 7)

    class Req:
        headers = {"authorization": "Bearer x", "content-length": "10"}

        async def stream(self):
            yield b"0123456789"
            yield b"more"

    async def run():
        answer = await peers.relay_send(pipe.ticket, Req())
        got = []
        while not pipe.queue.empty():
            got.append(pipe.queue.get_nowait())
        return answer, got

    answer, got = asyncio.run(run())
    assert answer.status_code == 409 and got[0] == b"0123456789" and got[-1] is peers._BROKEN
    assert pipe.ended == "it sent more bytes than it announced"


def test_fy_rfm_006_who_may_send_a_file(owner):
    """FY-RFM-006: candidates are other machines, connected, whose agent lists "relay_source", not revoked, whose
    reported cache has the model's revision (listing the GGUF file itself ranks first) and that are not marked
    for the file; a URL model only with the SHA-256 the server recorded and the same one listed by the machine;
    a file with no digest or size, a local model or a URL model without a recorded SHA-256 never comes from a
    machine."""
    from tests.machines_helpers import joined

    loop = asyncio.new_event_loop()
    try:
        nodes = {n: joined(owner, n)["node_id"] for n in ("t", "a", "b", "old", "off", "gone", "url")}
        base = f"hub/models--acme--shared@{REV}"
        sha = hashlib.sha256(URL_DATA).hexdigest()
        url = "https://example.com/models/x.gguf"
        ukey = sources.url_dir(url)
        for n in ("a", "b", "old", "off", "gone"):
            set_node(nodes[n], model_cache={"ok": True, "cached": [base] + ([f"{base}/m.gguf"] if n == "b" else [])})
        set_node(nodes["url"], model_cache={"ok": True, "cached": [ukey], "models": [{"key": ukey, "kind": "url",
                                                                                     "sha256": sha}]})
        set_node(nodes["gone"], revoked_at=utcnow())
        for n in ("a", "b", "gone", "url", "t"):
            hub.attach(Connection(nodes[n], loop=loop, features=["relay_source"]))
        hub.attach(Connection(nodes["old"], loop=loop, features=["relay"]))
        set_node(nodes["t"], model_cache={"ok": True, "cached": [base]})  # the target never sends to itself
        db = new_session()
        try:
            target = db.get(Node, nodes["t"])
            copy = peers.file_copy(db, target, hf_block("https://huggingface.co"), "m.gguf")
            assert copy.item["blob"] == hashlib.sha256(DATA).hexdigest() and copy.algo == "sha256"
            assert [c.name for c in peers.ranked(copy.candidates)] == ["b", "a"]
            small = peers.file_copy(db, target, hf_block("https://huggingface.co"), "README.md")
            assert small.algo == "oid" and small.expect == git_oid(README) and small.item["blob"] == git_oid(README)
            peers.mark(nodes["b"], copy.key, 60, "test")
            assert [c.name for c in peers.file_copy(db, target, hf_block("x"), "m.gguf").candidates] == ["a"]
            nodigest = hf_block("x")
            nodigest["files"][0] = {"path": "m.gguf", "size": len(DATA)}
            assert peers.file_copy(db, target, nodigest, "m.gguf") is None
            assert peers.file_copy(db, target, hf_block("x"), "other.gguf") is None
            assert peers.file_copy(db, target, {"kind": "local", "path": "/models/x"}, None) is None
            ublock = {"kind": "url", "url": url, "files": [{"size": len(URL_DATA), "sha256": sha}]}
            ucopy = peers.file_copy(db, target, ublock, None)
            assert [c.name for c in ucopy.candidates] == ["url"]
            assert ucopy.item == {"kind": "url", "dir": ukey[4:], "sha256": sha, "size": len(URL_DATA)}
            other = {"kind": "url", "url": url, "files": [{"size": len(URL_DATA), "sha256": "e" * 64}]}
            assert peers.file_copy(db, target, other, None).candidates == []
            assert peers.file_copy(db, target, {"kind": "url", "url": url, "files": [{"size": 5}]}, None) is None
        finally:
            db.close()
    finally:
        hub.reset()
        loop.close()


def test_fy_rfm_007_progress_names_the_source_and_downloads_remember_it(owner):
    """FY-RFM-007: progress through the server says which machine the model file comes from (`source`, added by
    the server: an agent cannot set it); a download's row then says "machine" with that machine, which the engine
    that follows does not change and the API shows; Download to… says which machine would send the model."""
    from tests.machines_helpers import connect, joined
    from tests.test_prefetch import FEATURES, cache, online
    from tests.test_workload_stream import recv_type

    mid = add_model()
    tgt, src = joined(owner, "tgt"), joined(owner, "src")
    set_node(src["node_id"], model_cache={"ok": True, "cached": [f"hub/models--acme--shared@{REV}"]})
    with connect(owner, tgt["credential"]) as ws, connect(owner, src["credential"]) as sws:
        online(ws, features=FEATURES + ["relay"])
        online(sws, features=FEATURES + ["relay", "relay_source"],
               model_cache=cache(cached=[f"hub/models--acme--shared@{REV}"]))
        targets = {m["name"]: m for m in owner.get(f"/api/downloads/targets?model_id={mid}").json()["machines"]}
        assert targets["tgt"]["via"] == "auto" and targets["tgt"]["from_machine"] is None
        owner.put(f"/api/machines/{tgt['node_id']}/fetch-via", json={"fetch_via": "server"})
        targets = {m["name"]: m for m in owner.get(f"/api/downloads/targets?model_id={mid}").json()["machines"]}
        assert targets["tgt"]["via"] == "server" and targets["tgt"]["from_machine"] == "src"
        assert targets["src"]["from_machine"] is None
        r = owner.post("/api/downloads", json={"model_id": mid, "machine_ids": [tgt["node_id"]], "engine": False})
        d = r.json()["downloads"][0]
        desired = recv_type(ws, "desired")
        while not desired.get("downloads"):
            desired = recv_type(ws, "desired")
        uid = desired["downloads"][0]["id"]
        peers.note_source("download", uid, (src["node_id"], "src"))

        def report(phase, via="server", source="spoofed"):
            ws.send_json({"v": 1, "type": "status", "downloads": [{"id": uid, "state": "downloading", "download": {
                "phase": phase, "done": 10, "total": 100, "via": via, "source": source}}]})

        def row():
            return next(x for x in owner.get("/api/downloads").json()["downloads"] if x["id"] == d["id"])

        report("model")
        got = wait_until(lambda: (lambda x: x["download"] and x["download"].get("source") and x)(row()), "the source")
        assert got["download"]["source"] == "src" and got["via"] == "machine"
        assert got["source"] == {"id": src["node_id"], "name": "src"}
        report("engine")
        got = wait_until(lambda: (lambda x: x["download"]["phase"] == "engine" and x)(row()), "the engine phase")
        assert got["via"] == "machine" and "source" not in got["download"]
        peers.note_source("download", uid, None)
        report("model")
        got = wait_until(lambda: (lambda x: x["download"]["phase"] == "model" and x)(row()), "the internet")
        assert got["via"] == "server" and got["source"] is None and "source" not in got["download"]
    st = StatusIn.model_validate({"v": 1, "type": "status", "replicas": []})
    assert st.downloads == []
    assert peers.label({"phase": "model", "via": "direct", "done": 1, "total": 2}, "replica", "x") == \
        {"phase": "model", "via": "direct", "done": 1, "total": 2}
    peers.note_source("replica", "r9", (5, "five"))
    assert peers.label({"phase": "model", "via": "server", "done": 1, "total": 2}, "replica", "r9")["source"] == "five"
    assert "source" not in peers.label({"phase": "image", "via": "server", "done": 1, "total": 2}, "replica", "r9")


def test_fy_rfm_008_a_replicas_progress_names_the_source(owner):
    """FY-RFM-008: a replica downloading its model through the server from another machine shows that machine
    in its progress (`download.source`), and loses it once the file comes from the internet."""
    from forgeyard.db import Replica
    from tests.test_scheduler import deploy, seed_catalog
    from tests.machines_helpers import connect, joined
    from tests.test_prefetch import FEATURES, online
    from tests.test_workload_stream import recv_type

    seed_catalog()
    a = joined(owner, "box")
    with connect(owner, a["credential"]) as ws:
        online(ws, features=FEATURES + ["relay"])
        deploy(owner, "rep", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
        desired = recv_type(ws, "desired")
        rep = desired["replicas"][0]
        peers.note_source("replica", rep["id"], (99, "elsewhere"))

        def send(via):
            ws.send_json({"v": 1, "type": "status", "replicas": [{
                "id": rep["id"], "generation": rep["generation"], "state": "pending", "reason": "downloading",
                "download": {"phase": "model", "done": 5, "total": 50, "via": via}}]})

        def download():
            db = new_session()
            try:
                return db.query(Replica).filter_by(uid=rep["id"]).one().download
            finally:
                db.close()

        send("server")
        assert wait_until(lambda: (download() or {}).get("source"), "the source") == "elsewhere"
        send("direct")
        wait_until(lambda: download() and download()["via"] == "direct", "direct")
        assert "source" not in download()


# ------------------------------------------------------------------ the source agent
def test_fy_rfm_009_the_source_finds_only_verified_files_of_its_own_cache(tmp_path):
    """FY-RFM-009: the source agent turns the server's description into a path only by looking it up among the
    models it found on its own disk: the revision's marker must list the file, its snapshot link must point at
    the named blob, a regular file inside the cache of the size said; a URL model must have its marker with the
    SHA-256 said. Anything else is "not_found"."""
    hf = FakeHF(Repo("acme/shared", REV, {"m.gguf": DATA, "README.md": README}))
    try:
        cache = filled_cache(tmp_path / "c", hf.url)
    finally:
        hf.close()
    sha = hashlib.sha256(DATA).hexdigest()
    item = {"kind": "hf", "repo": "acme/shared", "revision": REV, "file": "m.gguf", "blob": sha, "size": len(DATA)}
    path, size, algo, expect, lock = share.find(cache, item)
    assert path == blob(cache, DATA) and size == len(DATA) and algo == "sha256" and expect == sha
    assert lock == "repo:hub/models--acme--shared"
    small = share.find(cache, {**item, "file": "README.md", "blob": git_oid(README), "size": len(README)})
    assert small[2] == "oid"

    def refused(it):
        with pytest.raises(share.SendRefused) as e:
            share.find(cache, it)
        assert e.value.code == "not_found"

    refused({**item, "size": len(DATA) - 1})
    refused({**item, "blob": "a" * 64})
    refused({**item, "revision": "e" * 40})
    refused({**item, "repo": "acme/other"})
    refused({**item, "repo": "../etc"})
    refused({**item, "file": "../../../../etc/passwd"})
    refused({**item, "file": "README.md"})              # not that blob
    refused({**item, "file": "other.gguf"})             # not listed by the marker
    refused({**item, "blob": sha.upper()[:10]})
    refused({**item, "kind": "local"})
    refused("not a dict")
    # The marker no longer lists README.md (its link and blob are still there): not sent.
    marker = cache.root / "hub" / "models--acme--shared" / ".forgeyard" / f"{REV}.json"
    kept = marker.read_text()
    data = json.loads(kept)
    os.chmod(marker, 0o644)
    marker.write_text(json.dumps({**data, "files": [f for f in data["files"] if f["path"] != "README.md"]}))
    refused({**item, "file": "README.md", "blob": git_oid(README), "size": len(README)})
    marker.write_text(kept)
    # A directory where a blob should be, of the size asked: not a regular file.
    rblob = cache.root / "hub" / "models--acme--shared" / "blobs" / git_oid(README)
    os.chmod(rblob.parent, 0o755)
    rblob.unlink()
    rblob.mkdir()
    refused({**item, "file": "README.md", "blob": git_oid(README), "size": os.lstat(rblob).st_size})
    rblob.rmdir()
    rblob.write_bytes(README)
    # The blobs directory replaced by a link to one outside the cache, holding the same file.
    blobs = cache.root / "hub" / "models--acme--shared" / "blobs"
    elsewhere = tmp_path / "elsewhere"
    blobs.rename(elsewhere)
    blobs.symlink_to(elsewhere)
    refused(item)
    blobs.unlink()
    elsewhere.rename(blobs)
    assert share.find(cache, item)[0] == blob(cache, DATA)
    # A blob replaced by a link (to a file outside the cache) is never followed.
    outside = tmp_path / "outside"
    outside.write_bytes(DATA)
    b = blob(cache, DATA)
    os.chmod(b.parent, 0o755)
    b.unlink()
    b.symlink_to(outside)
    refused(item)
    # URL models.
    url = "https://example.com/m/x.gguf"
    d = cache.root / sources.url_dir(url)
    d.mkdir(parents=True)
    (d / "x.gguf").write_bytes(URL_DATA)
    usha = hashlib.sha256(URL_DATA).hexdigest()
    (d / ".forgeyard.json").write_text(json.dumps({"url": url, "file": "x.gguf", "sha256": usha}))
    uitem = {"kind": "url", "dir": d.name, "sha256": usha, "size": len(URL_DATA)}
    assert share.find(cache, uitem)[0] == d / "x.gguf"
    refused({**uitem, "sha256": "f" * 64})
    refused({**uitem, "dir": "../" + d.name})
    refused({**uitem, "size": 3})


def test_fy_rfm_010_the_source_sends_checks_and_limits_itself(tmp_path, monkeypatch):
    """FY-RFM-010: the source POSTs exactly the bytes asked for to its own server's path (never another form of
    path), at most MAX_SEND at a time ("busy" beyond), says "corrupt" when a whole file it sends does not hash to
    its digest (and deletes nothing), never hashes a part, and holds the model's lock: Remove is "busy" while a
    send runs."""
    hf = FakeHF(Repo("acme/shared", REV, {"m.gguf": DATA, "README.md": README}))
    try:
        cache = filled_cache(tmp_path / "c", hf.url)
    finally:
        hf.close()
    sha = hashlib.sha256(DATA).hexdigest()
    item = {"kind": "hf", "repo": "acme/shared", "revision": REV, "file": "m.gguf", "blob": sha, "size": len(DATA)}
    sent, gate = [], threading.Event()
    removal = {}

    class Client:
        def post(self, path, chunks, length):
            if gate.is_set():
                try:
                    cache.remove(f"hub/models--acme--shared@{REV}")
                except Exception as e:
                    removal["code"] = getattr(e, "code", str(e))
            sent.append((path, b"".join(chunks), length))
            return {"ok": True}

    sender = share.Sender(cache, Client())
    path = "/agent/v1/relay-send/" + "T" * 32
    assert sender.send({"path": path, "item": item, "offset": 0, "length": len(DATA)}) == {"ok": True, "bytes": len(DATA)}
    assert sent[-1] == (path, DATA, len(DATA))
    assert sender.send({"path": path, "item": item, "offset": 100, "length": 50})["ok"]
    assert sent[-1][1] == DATA[100:150]
    assert sender.send({"path": path, "item": item, "offset": len(DATA) - 5, "length": 10})["code"] == "not_found"
    for bad in ("https://elsewhere.example/x", "/agent/v1/relay/download/x/model", "/agent/v1/relay-send/short"):
        assert sender.send({"path": bad, "item": item, "offset": 0, "length": 5})["code"] == "failed"
    assert sender.send({"path": path, "item": {**item, "blob": "a" * 64}, "offset": 0, "length": 5})["code"] == "not_found"
    sender.running = share.MAX_SEND
    assert sender.send({"path": path, "item": item, "offset": 0, "length": 5})["code"] == "busy"
    sender.running = 0
    gate.set()
    assert sender.send({"path": path, "item": item, "offset": 0, "length": 5})["ok"]
    assert removal["code"] == "busy"
    gate.clear()
    b = blob(cache, DATA)
    os.chmod(b, 0o644)
    bad = bytearray(DATA)
    bad[7] ^= 1
    b.write_bytes(bytes(bad))
    answer = sender.send({"path": path, "item": item, "offset": 0, "length": len(DATA)})
    assert answer["code"] == "corrupt" and b.is_file()
    assert sender.send({"path": path, "item": item, "offset": 1000, "length": 10})["ok"]  # a part: not hashed


def test_fy_rfm_011_the_agent_says_it_can_send_and_answers_only_a_server_that_asks(tmp_path, monkeypatch):
    """FY-RFM-011: an agent with a model cache lists "relay_source" (one without does not); it answers
    relay_send with relay_sent only when its server lists the feature; the answer carries the send's outcome."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from tests.test_prefetch_agent import supervisor

    monkeypatch.setenv(SIMULATION_ENV, "1")
    sup = supervisor(tmp_path, ModelCache(tmp_path / "c"))
    agent = Agent("http://127.0.0.1:1", "cred-0123456789abcdef", SimulatedInventory("nvidia-rtx4090x2"), supervisor=sup)
    feats = agent.hello({"accelerators": [], "collectors": {}})["features"]
    assert "relay_source" in feats and len(feats) <= 16
    bare = Agent("http://127.0.0.1:1", "cred-0123456789abcdef", SimulatedInventory("nvidia-rtx4090x2"))
    assert "relay_source" not in (bare.hello({"accelerators": [], "collectors": {}}).get("features") or [])

    class WS:
        def __init__(self, msgs):
            self.msgs, self.sent = msgs, []

        def __aiter__(self):
            async def gen():
                for m in self.msgs:
                    yield json.dumps(m)
            return gen()

        async def send(self, text):
            self.sent.append(json.loads(text))

    agent.sender.send = lambda data, cancel=None: {"ok": False, "code": "not_found", "reason": "x"}
    ask = {"v": 1, "type": "relay_send", "request_id": "r1", "path": "/agent/v1/relay-send/" + "A" * 32,
           "item": {}, "offset": 0, "length": 1}

    async def run(features):
        agent.server_features = set(features)
        ws = WS([ask])
        await agent._receive_loop(ws)
        await asyncio.gather(*list(agent._answers))
        return ws.sent

    assert asyncio.run(run({"workloads"})) == []
    assert asyncio.run(run({"workloads", "relay_source"})) == [
        {"v": 1, "type": "relay_sent", "request_id": "r1", "ok": False, "code": "not_found", "reason": "x"}]


def test_fy_rfm_012_a_url_model_comes_from_a_machine_with_its_recorded_sha256(world):
    """FY-RFM-012: a URL model whose SHA-256 the server recorded (D8) comes from a machine that lists it with that
    SHA-256, checked whole on the server; with another SHA-256 listed, from the internet (here: refused, since
    the URL's host is not an allowed one)."""
    w = world
    url = "https://models.example.com/x.gguf"
    sha = hashlib.sha256(URL_DATA).hexdigest()
    mid = add_catalog("urlmodel", {"kind": "url", "url": url, "format": "gguf", "size_bytes": len(URL_DATA),
                                   "files": [{"path": "x.gguf", "size": len(URL_DATA), "sha256": sha}]},
                      task="chat", fmt="gguf", source=url, kind="url")
    d = w.cache.root / sources.url_dir(url)
    d.mkdir(parents=True)
    (d / "x.gguf").write_bytes(URL_DATA)
    (d / ".forgeyard.json").write_text(json.dumps({"url": url, "file": "x.gguf", "sha256": sha}))
    _, uid = download_row(mid, w.tgt["node_id"])
    with source(w):
        connected_source(w, w.src["node_id"])
        wait_until(lambda: any(m.get("sha256") == sha for m in (hub.get(w.src["node_id"]) and _cache(w)).get("models", [])),
                   "the URL model listed")
        status, _, body = get(w, f"/agent/v1/relay/download/{uid}/model", w.tgt["credential"])
        assert status == 200 and body == URL_DATA
        assert peers.source_of("download", uid) == (w.src["node_id"], "src")


def _cache(w) -> dict:
    db = new_session()
    try:
        return db.get(Node, w.src["node_id"]).model_cache or {}
    finally:
        db.close()


# ------------------------------------------------------------------ the whole loop, real agents
def test_fy_rfm_013_a_machine_without_internet_downloads_a_model_another_machine_has(world):
    """FY-RFM-013 (real agents, real HTTP and WebSocket): the target cannot resolve any name and fetches through
    the server; Download to… brings it the model from the source machine's cache: done, "via" machine, from
    "src"; every file identical; Hugging Face never asked for a byte."""
    w = world
    tcache = ModelCache(w.tmp / "tgt-cache", allow_private=True, resolver=unresolvable)
    with source(w), AgentThread(w.tmp, w.server, w.tgt["credential"], tcache, "tgt", (31960, 31969)):
        connected_source(w, w.src["node_id"])
        wait_until(lambda: hub.get(w.tgt["node_id"]) is not None, "the target connected", 15)
        first = w.api("POST", "/api/downloads", {"model_id": w.model_id, "machine_ids": [w.tgt["node_id"]],
                                                 "engine": False})["downloads"][0]

        def done():
            d = next(x for x in w.api("GET", f"/api/downloads?machine_id={w.tgt['node_id']}")["downloads"]
                     if x["id"] == first["id"])
            return d["state"] in ("done", "failed") and d

        d = wait_until(done, "the download from the other machine", 30)
        assert d["state"] == "done" and d["via"] == "machine", d
        assert d["source"] == {"id": w.src["node_id"], "name": "src"}
    assert hashlib.sha256(blob(tcache, DATA).read_bytes()).hexdigest() == hashlib.sha256(DATA).hexdigest()
    assert tcache.is_cached(validate_model(hf_block(w.hf.url), []))
    assert cdn(w) == [] and cdn(w, "README.md") == []


def test_fy_rfm_014_a_bad_copy_is_refused_and_the_target_starts_the_file_over_from_the_internet(world):
    """FY-RFM-014 (real agents): the source's copy of the file was damaged on its disk; the server refuses it
    before the last piece, the target's resume is answered whole from the internet, and the download ends done,
    the file intact; the source is marked and the audit log names it."""
    w = world
    b = blob(w.cache, DATA)
    os.chmod(b, 0o644)
    bad = bytearray(DATA)
    bad[-100] ^= 0x55
    b.write_bytes(bytes(bad))
    tcache = ModelCache(w.tmp / "tgt-cache", allow_private=True, resolver=unresolvable)
    with source(w), AgentThread(w.tmp, w.server, w.tgt["credential"], tcache, "tgt", (31970, 31979)):
        connected_source(w, w.src["node_id"])
        wait_until(lambda: hub.get(w.tgt["node_id"]) is not None, "the target connected", 15)
        first = w.api("POST", "/api/downloads", {"model_id": w.model_id, "machine_ids": [w.tgt["node_id"]],
                                                 "engine": False})["downloads"][0]

        def done():
            d = next(x for x in w.api("GET", f"/api/downloads?machine_id={w.tgt['node_id']}")["downloads"]
                     if x["id"] == first["id"])
            return d["state"] in ("done", "failed") and d

        d = wait_until(done, "the download", 40)
        assert d["state"] == "done", d
    assert blob(tcache, DATA).read_bytes() == DATA
    assert cdn(w) == [None]  # the whole file, once, from the internet
    assert [r.target for r in audit_rows("node.relay_copy_refused")] == ["src"]


def test_fy_rfm_016_a_machine_that_breaks_off_mid_file_reads_as_such_in_the_log(world, monkeypatch, caplog):
    """FY-RFM-016 (issue #130): the source machine's POST ends after part of the file. The target sees a short body;
    the server logs one line naming the file, the target, the source machine and the bytes sent, and uvicorn
    nothing about the cut answer."""
    from tests.test_relay import Records

    w = world
    _, uid = download_row(w.model_id, w.tgt["node_id"])
    asked, release = [], threading.Event()

    def held(self, message, cancel=None):
        asked.append(message)
        release.wait(20)
        return {"ok": False, "code": "failed", "reason": "the test sent it"}

    monkeypatch.setattr(share.Sender, "send", held)
    caplog.set_level(logging.INFO, logger="forgeyard.relay")
    uvicorn_log = logging.getLogger("uvicorn.error")
    seen = Records()
    uvicorn_log.addHandler(seen)
    out = {}
    try:
        with source(w):
            connected_source(w, w.src["node_id"])
            t = threading.Thread(target=lambda: out.update(r=get(w, f"/agent/v1/relay/download/{uid}/model/m.gguf",
                                                                   w.tgt["credential"])))
            t.start()
            wait_until(lambda: asked, "the source asked")
            path = asked[0]["path"]
            cut = http.client.HTTPConnection("127.0.0.1", w.port, timeout=20)
            cut.putrequest("POST", path)
            cut.putheader("Authorization", f"Bearer {w.src['credential']}")
            cut.putheader("Content-Length", str(len(DATA)))
            cut.endheaders()
            cut.send(DATA[:200_000])
            wait_until(lambda: peers._pipes[path.rsplit("/", 1)[1]].received == 200_000, "the first part received")
            cut.close()  # the source machine goes away mid-file
            t.join(20)
            status, headers, body = out["r"]
            assert status == 200 and {k.lower(): v for k, v in headers.items()}["content-length"] == str(len(DATA))
            assert body == DATA[:len(body)] and len(body) <= 200_000  # short: never a whole file
            wait_until(lambda: any("cut short" in m for m in caplog.messages), "the log line")
            lines = [m for m in caplog.messages if "m.gguf" in m and "to tgt" in m and not m.startswith("Relaying")]
            assert len(lines) == 1, lines
            assert lines[0].startswith("Relay of acme/shared@dddddddddddd/m.gguf to tgt cut short: the source broke off (")
            assert f"; {len(body)} of {len(DATA)} bytes sent from machine src in " in lines[0]
            wait_until(lambda: peers._pipes == {}, "the pipe closed")
            assert not any("ASGI" in t or "Content-Length" in t or "EndedEarly" in t for t in seen.texts()), seen.texts()
            release.set()
    finally:
        uvicorn_log.removeHandler(seen)
