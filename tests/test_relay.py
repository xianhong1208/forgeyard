"""The server relays model files and engine archives to machines that cannot reach their source, storing
nothing (1.4, issue #35, D1/D3; FY-RELAY-001…009): the relay in the desired blocks (and the token left out),
the endpoint's authorization from the machine's desired state, Range passed through, the server's own token
and source rules, refusals and limits, a relay closed when nobody wants it any more, and placement that no
longer avoids a machine which relays for a host it could not reach."""

import hashlib
import http.client
import json
import logging
import os
import threading
import time

import pytest
from forgeyard.config import get_settings
from forgeyard.db import ModelDownload, Node, new_session, utcnow
from forgeyard.machines import relay
from forgeyard.machines.messages import _download
from forgeyard.models import hf_token, reach
from tests.machines_helpers import agent_client, bearer, connect, joined
from tests.model_helpers import FakeHF, Repo, add_catalog, git_oid
from tests.test_placement_fetch import cpu, failure, set_node
from tests.test_prefetch import FEATURES, online
from tests.test_scheduler import deploy, reps, seed_catalog
from tests.test_workload_stream import recv_type
from tests.waits import wait_until

REV = "b" * 40
DATA = b"GGUF" + os.urandom(300_000)
GATED = b"GGUF" + os.urandom(5_000)
RELAY = FEATURES + ["relay"]
TOKEN = FakeHF.TOKEN


@pytest.fixture()
def hf(monkeypatch):
    server = FakeHF(Repo("acme/small", REV, {"small.gguf": DATA, "README.md": b"# small"}),
                    Repo("acme/locked", REV, {"locked.gguf": GATED}, gated=True))
    monkeypatch.setenv("HF_ENDPOINT", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    get_settings.cache_clear()
    yield server
    server.close()


def small_model(name="small", repo="acme/small", data=DATA, file="small.gguf") -> int:
    return add_catalog(name, {"kind": "hf", "repo": repo, "ref": "main", "revision": REV, "format": "gguf", "file": file,
                              "size_bytes": len(data), "files": [{"path": file, "size": len(data), "oid": git_oid(data),
                                                                  "sha256": hashlib.sha256(data).hexdigest()}]},
                       task="chat", fmt="gguf", source=f"hf://{repo}")


def deployable(name, repo, data, file) -> int:
    """A catalog entry like the seeded qwen3-1.7b-gguf (whose GGUF metadata the estimate needs), served by the
    fake Hugging Face as `repo` with one file."""
    from forgeyard.db import CatalogModel

    seed_catalog()
    db = new_session()
    try:
        meta = dict(db.query(CatalogModel).filter_by(name="qwen3-1.7b-gguf").one().meta)
    finally:
        db.close()
    meta.update(repo=repo, revision=REV, file=file, size_bytes=len(data),
                files=[{"path": file, "size": len(data), "oid": git_oid(data), "sha256": hashlib.sha256(data).hexdigest()}])
    return add_catalog(name, meta, task="chat", fmt="gguf", source=f"hf://{repo}")


def fetch_via(node_id: int, value: str) -> None:
    set_node(node_id, fetch_via=value)


def start(owner, mid, node_id) -> dict:
    r = owner.post("/api/downloads", json={"model_id": mid, "machine_ids": [node_id], "engine": False})
    assert r.status_code == 201, r.text
    return r.json()["downloads"][0]


def get(owner, path, credential=None, **headers):
    h = {**(bearer(credential) if credential else {}), **headers}
    return agent_client(owner).get(path, headers=h)


# ------------------------------------------------------------------ what the agent is told
def test_fy_relay_001_desired_blocks_say_how_to_fetch_and_drop_the_token_through_the_server(owner, hf):
    """FY-RELAY-001: a machine whose agent relays gets `relay` in its download's model block: "auto" (with the
    server's token, for the direct try), "server" (no token: the server adds its own) when set so or when a
    network failure to the source is remembered; "direct" and an agent without the feature get no relay (and
    the token as before). Engine archives the same."""
    mid = small_model()
    db = new_session()
    hf_token.put(db, "hf_server_token_1234", "owner")
    db.commit()
    db.close()
    a, old = joined(owner, "relayer"), joined(owner, "old")
    with connect(owner, a["credential"]) as ws, connect(owner, old["credential"]) as ows:
        online(ws, features=RELAY)
        online(ows)
        start(owner, mid, a["node_id"])
        start(owner, mid, old["node_id"])
        item = recv_type(ws, "desired")["downloads"][0]
        assert item["model"]["relay"] == {"path": f"/agent/v1/relay/download/{item['id']}/model", "mode": "auto"}
        assert item["model"]["token"] == "hf_server_token_1234"
        plain = recv_type(ows, "desired")["downloads"][0]
        assert "relay" not in plain["model"] and plain["model"]["token"] == "hf_server_token_1234"
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "server"})
        item = recv_type(ws, "desired")["downloads"][0]
        assert item["model"]["relay"]["mode"] == "server" and "token" not in item["model"]
        assert "hf_server_token_1234" not in json.dumps(item)
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "auto"})
        item = recv_type(ws, "desired")["downloads"][0]
        assert item["model"]["relay"]["mode"] == "auto" and item["model"]["token"] == "hf_server_token_1234"
        reach.record(a["node_id"], failure(host="127.0.0.1"))  # the fake Hugging Face's host
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "direct"})
        item = recv_type(ws, "desired")["downloads"][0]
        assert "relay" not in item["model"] and item["model"]["token"] == "hf_server_token_1234"
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "auto"})
        item = recv_type(ws, "desired")["downloads"][0]
        assert item["model"]["relay"]["mode"] == "server" and "token" not in item["model"]
    node = Node(id=1, features=RELAY, fetch_via="server")
    engine = {"name": "llama.cpp-1", "url": "https://github.com/x/y.tar.gz", "sha256": "0" * 64, "binary": "server"}
    out = relay.attach({"id": "r1", "engine": engine}, node, "replica", "r1")
    assert out["engine"]["relay"] == {"path": "/agent/v1/relay/replica/r1/engine", "mode": "server"}
    assert relay.attach({"id": "r1", "engine": engine}, Node(id=1, features=FEATURES, fetch_via="server"),
                        "replica", "r1") == {"id": "r1", "engine": engine}
    local = {"kind": "local", "path": "/models/x"}
    assert "relay" not in relay.attach({"model": local}, node, "replica", "r1")["model"]


# ------------------------------------------------------------------ the endpoint
def test_fy_relay_002_serves_what_the_desired_state_names_with_range_and_the_servers_token(owner, hf):
    """FY-RELAY-002: the file of a download under way is streamed from the source the catalog gives, whole or
    from a byte (Range passed through: 206 and Content-Range); the source gets the server's token, never the
    machine's credential; the body is the file."""
    mid = small_model("locked", "acme/locked", GATED, "locked.gguf")
    db = new_session()
    hf_token.put(db, TOKEN, "owner")
    db.commit()
    db.close()
    a = joined(owner, "relayer")
    with connect(owner, a["credential"]) as ws:
        online(ws, features=RELAY)
        start(owner, mid, a["node_id"])
        path = recv_type(ws, "desired")["downloads"][0]["model"]["relay"]["path"]
        r = get(owner, f"{path}/locked.gguf", a["credential"])
        assert r.status_code == 200, r.text
        assert r.content == GATED and r.headers["content-length"] == str(len(GATED))
        r = get(owner, f"{path}/locked.gguf", a["credential"], Range="bytes=1000-")
        assert r.status_code == 206 and r.content == GATED[1000:]
        assert r.headers["content-range"] == f"bytes 1000-{len(GATED) - 1}/{len(GATED)}"
        asked = [q for q in hf.requests if "locked.gguf" in q["path"] and "/cdn/" not in q["path"]]
        assert [q["auth"] for q in asked] == [f"Bearer {TOKEN}"] * 2 and asked[1]["range"] == "bytes=1000-"
        assert all(a["credential"] not in (q["auth"] or "") for q in hf.requests)


def test_fy_relay_003_anything_the_desired_state_does_not_name_is_refused(owner, hf):
    """FY-RELAY-003: not an open proxy. No or a wrong credential: 401. Another machine's download, a file the
    model does not list, a path made up to point elsewhere, a cancelled download, an unknown kind: 404, and the
    source is never asked. A machine set to "direct", or whose agent does not relay: 403. A browser: 403. Two
    ranges: 416."""
    mid = small_model()
    a, b, old = joined(owner, "relayer"), joined(owner, "other"), joined(owner, "old")
    with connect(owner, a["credential"]) as wa, connect(owner, b["credential"]) as wb, \
            connect(owner, old["credential"]) as wo:
        online(wa, features=RELAY)
        online(wb, features=RELAY)
        online(wo)
        d = start(owner, mid, a["node_id"])
        path = recv_type(wa, "desired")["downloads"][0]["model"]["relay"]["path"]
        start(owner, mid, old["node_id"])
        old_uid = recv_type(wo, "desired")["downloads"][0]["id"]
        before = len(hf.requests)
        assert get(owner, f"{path}/small.gguf").status_code == 401
        assert get(owner, f"{path}/small.gguf", "not-a-credential").status_code == 401
        mine = get(owner, f"{path}/small.gguf", b["credential"])
        assert mine.status_code == 404 and mine.json()["detail"]["code"] == "relay_not_wanted"
        for bad in ("other.gguf", "..%2F..%2Fetc%2Fpasswd", "http://169.254.169.254/latest/meta-data"):
            assert get(owner, f"{path}/{bad}", a["credential"]).status_code == 404, bad
        assert get(owner, path, a["credential"]).status_code == 404  # an HF model has no single-file form
        assert get(owner, path.replace("/download/", "/workload/") + "/small.gguf", a["credential"]).status_code == 404
        assert get(owner, f"/agent/v1/relay/download/{old_uid}/model/small.gguf", old["credential"]).status_code == 403
        assert get(owner, f"{path}/small.gguf", a["credential"], Origin="https://evil.example").status_code == 403
        assert get(owner, f"{path}/small.gguf", a["credential"], Range="bytes=0-1,5-9").status_code == 416
        assert len(hf.requests) == before
        fetch_via(a["node_id"], "direct")
        off = get(owner, f"{path}/small.gguf", a["credential"])
        assert off.status_code == 403 and off.json()["detail"]["code"] == "relay_off"
        fetch_via(a["node_id"], "auto")
        assert get(owner, f"{path}/small.gguf", a["credential"]).status_code == 200
        owner.post(f"/api/downloads/{d['id']}/cancel")
        assert get(owner, f"{path}/small.gguf", a["credential"]).status_code == 404


def test_fy_relay_004_a_replica_is_served_with_its_deployments_token_while_it_is_placed_there(owner, hf):
    """FY-RELAY-004: a deployment's replica on a relaying machine: its blocks carry the relay; the file comes
    with the deployment's own token; once the deployment is removed, the relay for that replica is refused."""
    deployable("locked", "acme/locked", GATED, "locked.gguf")
    a = joined(owner, "relayer")
    with connect(owner, a["credential"]) as ws:
        online(ws, features=RELAY)
        set_node(a["node_id"], fetch_via="server")
        d = deploy(owner, "locked", "locked", "llama-cpp", params={"gpus": 0}, hf_token=TOKEN)
        rep = next(m for m in iter(lambda: recv_type(ws, "desired"), None) if m["replicas"])["replicas"][0]
        assert rep["model"]["relay"] == {"path": f"/agent/v1/relay/replica/{rep['id']}/model", "mode": "server"}
        assert "token" not in rep["model"] and rep["engine"]["relay"]["mode"] == "server"
        r = get(owner, rep["model"]["relay"]["path"] + "/locked.gguf", a["credential"])
        assert r.status_code == 200 and r.content == GATED
        other = joined(owner, "other")
        set_node(other["node_id"], features=RELAY)
        assert get(owner, rep["model"]["relay"]["path"] + "/locked.gguf", other["credential"]).status_code == 404
        assert hf.requests[-2]["auth"] == f"Bearer {TOKEN}"  # before the CDN redirect
        assert owner.delete(f"/api/deployments/{d['deployment']['id']}").status_code in (200, 204)
        wait_until(lambda: get(owner, rep["model"]["relay"]["path"] + "/locked.gguf", a["credential"]).status_code
                   == 404, "the relay refused once the replica is gone")


def test_fy_relay_005_the_sources_answers_and_the_servers_own_rules(owner, hf, monkeypatch):
    """FY-RELAY-005: a gated file without a token: 403 "source_refused" (the agent stops, as directly); a
    source the server cannot reach: 502 "source_unreachable" (the agent retries); a private source when the
    server does not allow private model hosts: 403 "source_blocked" (its SSRF rules apply to the relay)."""
    mid = small_model("locked", "acme/locked", GATED, "locked.gguf")
    a = joined(owner, "relayer")
    with connect(owner, a["credential"]) as ws:
        online(ws, features=RELAY)
        start(owner, mid, a["node_id"])
        path = recv_type(ws, "desired")["downloads"][0]["model"]["relay"]["path"] + "/locked.gguf"
        r = get(owner, path, a["credential"])
        assert r.status_code == 403 and r.json()["detail"]["code"] == "source_refused"
        monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "0")
        get_settings.cache_clear()
        r = get(owner, path, a["credential"])
        assert r.status_code == 403 and r.json()["detail"]["code"] == "source_blocked"
        monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
        get_settings.cache_clear()
        db = new_session()
        hf_token.put(db, TOKEN, "owner")
        db.commit()
        db.close()
        hf.close()
        r = get(owner, path, a["credential"])
        assert r.status_code == 502 and r.json()["detail"]["code"] == "source_unreachable"
        assert relay.slots.by_node == {}  # every refusal gave its slot back


def test_fy_relay_006_two_relays_per_machine_four_in_all(owner, hf, monkeypatch):
    """FY-RELAY-006: a machine with two relays under way gets 429 with Retry-After for a third; so does any
    machine once four run in all; a slot is given back when a relay ends."""
    mid = small_model()
    a = joined(owner, "relayer")
    with connect(owner, a["credential"]) as ws:
        online(ws, features=RELAY)
        start(owner, mid, a["node_id"])
        path = recv_type(ws, "desired")["downloads"][0]["model"]["relay"]["path"] + "/small.gguf"
        assert relay.slots.take(a["node_id"]) and relay.slots.take(a["node_id"])
        busy = get(owner, path, a["credential"])
        assert busy.status_code == 429 and busy.headers["retry-after"] == "5"
        relay.slots.give(a["node_id"])
        assert get(owner, path, a["credential"]).status_code == 200
        assert relay.slots.by_node == {a["node_id"]: 1}
        relay.slots.give(a["node_id"])
        for other in (901, 902, 903, 904):
            assert relay.slots.take(other)
        assert get(owner, path, a["credential"]).status_code == 429
        assert not relay.slots.take(905)


def test_fy_relay_007_cancelling_closes_a_relay_under_way(monkeypatch, tmp_path, caplog):
    """FY-RELAY-007 (over real HTTP): a relay whose source has stalled is closed within the recheck once the
    download is cancelled: the agent's read ends, the slot is given back, and the log says why and how much."""
    from tests.conftest import OWNER, SETUP_TOKEN
    from tests.test_agent import LiveServer, _free_port, _http
    from forgeyard.agent.client import join

    hf = FakeHF(Repo("acme/small", REV, {"small.gguf": DATA}))
    hf.stall_after = 50_000
    monkeypatch.setenv("HF_ENDPOINT", hf.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    monkeypatch.setattr(relay, "RECHECK", 0.2)
    get_settings.cache_clear()
    port = _free_port()
    caplog.set_level(logging.INFO, logger="forgeyard.relay")
    try:
        with LiveServer(port):
            _, cookie = _http(port, "POST", "/api/auth/setup",
                              {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
            j = join(f"http://127.0.0.1:{port}", _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"],
                     name="relayer")
            set_node(j["node_id"], features=RELAY)
            row_id, uid = _download_row(small_model(), j["node_id"])
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request("GET", f"/agent/v1/relay/download/{uid}/model/small.gguf", headers=bearer(j["credential"]))
            resp = conn.getresponse()
            assert resp.status == 200
            got = b""
            while len(got) < 50_000:
                got += resp.read1(65536)
            assert relay.slots.by_node == {j["node_id"]: 1}
            t0 = time.monotonic()
            _http(port, "POST", f"/api/downloads/{row_id}/cancel", {}, cookie)
            ended = threading.Event()

            def drain():
                try:
                    while resp.read1(65536):
                        pass
                except (OSError, http.client.HTTPException):
                    pass
                ended.set()

            threading.Thread(target=drain, daemon=True).start()
            assert ended.wait(10), "the relay did not close"
            assert time.monotonic() - t0 < 5
            assert got == DATA[:len(got)]
            wait_until(lambda: relay.slots.by_node == {}, "the slot given back")
            wait_until(lambda: any("no longer wanted" in r.getMessage() for r in caplog.records), "the log line")
            line = next(r.getMessage() for r in caplog.records if "no longer wanted" in r.getMessage())
            assert line.startswith("Relayed acme/small@bbbbbbbbbbbb/small.gguf to relayer: 0.0 MiB")
    finally:
        hf.close()


def _download_row(model_id: int, node_id: int) -> tuple[int, str]:
    """A download under way, straight in the database (the machine has no agent connected here)."""
    db = new_session()
    try:
        row = ModelDownload(model_id=model_id, node_id=node_id, state="downloading", via="direct", requested_by="owner",
                            created_at=utcnow())
        db.add(row)
        db.commit()
        return row.id, row.uid
    finally:
        db.close()


# ------------------------------------------------------------------ progress and placement
def test_fy_relay_008_progress_through_the_server_is_kept(owner, hf):
    """FY-RELAY-008: the agent's progress may say `via` "server" (or "direct"; anything else is dropped); a
    download's row then says it came through the server; the dialog's machines say how each would get the
    model (an agent without the feature: "direct", whatever its setting)."""
    assert _download({"phase": "model", "done": 1, "total": 2, "via": "server"})["via"] == "server"
    assert _download({"phase": "model", "done": 1, "total": 2, "via": "direct"})["via"] == "direct"
    assert "via" not in _download({"phase": "model", "done": 1, "total": 2, "via": "elsewhere"})
    mid = small_model()
    a = joined(owner, "relayer")
    with connect(owner, a["credential"]) as ws:
        online(ws, features=RELAY)
        d = start(owner, mid, a["node_id"])
        uid = recv_type(ws, "desired")["downloads"][0]["id"]
        ws.send_json({"v": 1, "type": "status", "replicas": [], "downloads": [
            {"id": uid, "state": "downloading", "reason": "downloading the model through the server: 0.00 of 0.00 GiB",
             "download": {"phase": "model", "done": 100, "total": len(DATA), "via": "server"}}]})
        listed = wait_until(lambda: owner.get(f"/api/downloads?machine_id={a['node_id']}").json()["downloads"][0]
                            ["download"], "the progress")
        assert listed["via"] == "server"
        row = owner.get(f"/api/downloads?machine_id={a['node_id']}").json()["downloads"][0]
        assert row["id"] == d["id"] and row["via"] == "server"
    # The dialog says how each machine would get it.
    b, old = joined(owner, "relayer-b"), joined(owner, "old")
    set_node(b["node_id"], features=RELAY, fetch_via="server")
    set_node(old["node_id"], features=FEATURES, fetch_via="server")
    t = {m["name"]: m["via"] for m in owner.get(f"/api/downloads/targets?model_id={mid}&engine=false").json()["machines"]}
    assert t == {"relayer": "auto", "relayer-b": "server", "old": "direct"}


def test_fy_relay_009_placement_no_longer_avoids_a_machine_that_relays(owner):
    """FY-RELAY-009 (#71 + relay): cpu-a could not reach huggingface.co. With an agent that relays and
    fetch_via "auto", the model comes through the server, so cpu-a (first by name) takes the
    replica and the page says so; set to "direct", or with an agent that does not relay, it is avoided as
    before. Set to "server", the page says it downloads through the server."""
    seed_catalog()
    a = cpu("cpu-a")
    cpu("cpu-b")
    set_node(a, features=["workloads", "models", "relay"])
    reach.record(a, failure())
    d = deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    r = reps(owner, d)[0]
    assert r["machine"]["name"] == "cpu-a"
    # The engine archive is on github.com, which cpu-a has not failed to reach: it tries that one directly.
    assert r["placement_note"] == "cpu-a could not reach huggingface.co just now; the model comes through the server"
    machines = {m["name"]: m for m in d["placement"]["machines"]}
    assert machines["cpu-a"]["via_server"] == ["model"] and "unreachable" not in machines["cpu-a"]
    set_node(a, fetch_via="direct")
    d2 = deploy(owner, "small2", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    assert reps(owner, d2)[0]["machine"]["name"] == "cpu-b"
    set_node(a, fetch_via="auto", features=["workloads", "models"])
    d3 = deploy(owner, "small3", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    assert reps(owner, d3)[0]["machine"]["name"] == "cpu-b"
    reach.reset()
    set_node(a, fetch_via="server", features=["workloads", "models", "relay"])
    d4 = deploy(owner, "small4", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0}, machine_id=a)
    r4 = reps(owner, d4)[0]
    assert r4["machine"]["name"] == "cpu-a"
    assert r4["placement_note"] == "cpu-a downloads the model and the engine through the server (its internet access setting)"
    assert {m["name"]: m for m in d4["placement"]["machines"]}["cpu-a"]["via_server"] == ["model", "engine"]
    node = Node(id=a, features=["workloads", "models", "relay"], fetch_via="auto")
    assert relay.relays(node, "model") and relay.relays(node, "engine") and not relay.relays(node, "image")


class Records(logging.Handler):
    """Every record a logger lets through (uvicorn's error logger does not propagate to caplog's handler)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def texts(self) -> list[str]:
        return [r.getMessage() + (f" {r.exc_info[1]!r}" if r.exc_info else "") for r in self.records]


def test_fy_relay_043_a_source_that_breaks_off_reads_as_such_in_the_log(monkeypatch, caplog):
    """FY-RELAY-043 (over real HTTP, issue #130): the internet source breaks off mid-file. The agent still sees a
    short body (it asks for the rest again); the server's log has one line naming the file, the machine, the source
    and the bytes sent, and nothing from uvicorn ("Exception in ASGI application" / "Response content shorter than
    Content-Length"). Other records of uvicorn's error logger still pass."""
    from tests.conftest import OWNER, SETUP_TOKEN
    from tests.test_agent import LiveServer, _free_port, _http
    from forgeyard.agent.client import join

    hf = FakeHF(Repo("acme/small", REV, {"small.gguf": DATA}))
    hf.fail_after = 100_000
    monkeypatch.setenv("HF_ENDPOINT", hf.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    get_settings.cache_clear()
    port = _free_port()
    caplog.set_level(logging.INFO, logger="forgeyard.relay")
    uvicorn_log = logging.getLogger("uvicorn.error")
    seen = Records()
    try:
        with LiveServer(port):
            uvicorn_log.addHandler(seen)  # after the server's logging setup, which replaces the handlers
            _, cookie = _http(port, "POST", "/api/auth/setup",
                              {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
            j = join(f"http://127.0.0.1:{port}", _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"],
                     name="relayer")
            set_node(j["node_id"], features=RELAY)
            _, uid = _download_row(small_model(), j["node_id"])
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request("GET", f"/agent/v1/relay/download/{uid}/model/small.gguf", headers=bearer(j["credential"]))
            resp = conn.getresponse()
            assert resp.status == 200 and resp.headers["Content-Length"] == str(len(DATA))
            got = b""
            try:
                while chunk := resp.read1(65536):
                    got += chunk
            except (http.client.IncompleteRead, OSError):
                pass
            conn.close()
            assert got == DATA[:100_000]  # the connection closed short of its Content-Length: never a whole file
            wait_until(lambda: relay.slots.by_node == {}, "the slot given back")
            wait_until(lambda: any("cut short" in m for m in caplog.messages), "the log line")
            lines = [r for r in caplog.records if r.name == "forgeyard.relay" and "small.gguf to relayer" in r.getMessage()
                     and not r.getMessage().startswith("Relaying")]
            assert len(lines) == 1, [r.getMessage() for r in lines]
            line = lines[0]
            assert line.levelno == logging.WARNING
            assert line.getMessage().startswith(f"Relay of acme/small@bbbbbbbbbbbb/small.gguf to relayer cut short: the "
                                                f"source ended {len(DATA) - 100_000} bytes early; 100000 of {len(DATA)} "
                                                "bytes sent from 127.0.0.1 in ")
            uvicorn_log.error("an unrelated error")  # the filter drops only a relay's early end
            wait_until(lambda: "an unrelated error" in seen.texts(), "the unrelated record")
            assert not any("ASGI" in t or "Content-Length" in t or "EndedEarly" in t for t in seen.texts()), seen.texts()
    finally:
        uvicorn_log.removeHandler(seen)
        hf.close()


def test_fy_relay_044_a_source_that_sends_too_much_is_cut_short_and_only_that_is_kept_from_uvicorn(owner, monkeypatch,
                                                                                                   caplog):
    """FY-RELAY-044 (#130): a source that sends more bytes than the file has is cut short, with the one line; the
    filter on uvicorn's error logger drops only a relay's early end (EndedEarly), and is installed once."""
    from tests.machines_helpers import joined

    a = joined(owner, "dock")
    data = b"x" * 1000

    class Up:
        """What the source's opener returns (a fetch.Response, read in a thread)."""
        status = 200
        headers = {"Content-Length": "1000"}

        def __init__(self):
            self.left = [data]

        def read1(self, n):
            return self.left.pop() if self.left else b""

        def remaining(self):
            return 0

        def abort(self):
            pass

        def close(self):
            pass

    src = relay.Source(url="https://registry.example/v2/acme/x/blobs/sha256:" + "a" * 64, policy=None, label="acme/x blob",
                       digest="sha256:" + hashlib.sha256(data).hexdigest(), size=990, opener=lambda headers: Up())
    monkeypatch.setattr(relay, "_start", lambda credential, kind, uid, what, path: (a["node_id"], "dock", src))
    caplog.set_level(logging.INFO, logger="forgeyard.relay")
    got = b""
    with agent_client(owner).stream("GET", f"/agent/v1/relay/download/{'u' * 32}/image/blobs/sha256:{'a' * 64}",
                                    headers=bearer(a["credential"])) as r:
        assert r.status_code == 200
        try:
            for chunk in r.iter_bytes():
                got += chunk
        except relay.EndedEarly:
            pass  # the in-process client raises what the app raised; uvicorn closes the connection instead
    assert got == b""
    wait_until(lambda: relay.slots.by_node == {}, "the slot given back")
    lines = [m for m in caplog.messages if "acme/x blob to dock" in m and not m.startswith("Relaying")]
    assert len(lines) == 1 and lines[0].startswith(
        "Relay of acme/x blob to dock cut short: the source sent more than the 990 bytes the file has; 0 of 1000 bytes "
        "sent from registry.example in ") and lines[0].endswith(" s."), lines
    quiet = relay._QuietEndedEarly()

    def record(exc):
        return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application\n", None,
                                 (type(exc), exc, None) if exc else None)

    assert quiet.filter(record(relay.EndedEarly("the source broke off"))) is False
    assert quiet.filter(record(RuntimeError("Response content shorter than Content-Length"))) is True
    assert quiet.filter(record(None)) is True
    relay.quiet_uvicorn()
    relay.quiet_uvicorn()
    assert sum(isinstance(f, relay._QuietEndedEarly) for f in logging.getLogger("uvicorn.error").filters) == 1
