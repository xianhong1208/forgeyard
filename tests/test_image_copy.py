"""An admin copies an engine image from one machine to another, through the server (1.4.1, issue #104, Q4;
FY-ICP-001…011): the listing of a machine's images and who may see it, the refusals before a byte moves, the
source agent saving only one of its own images by its id and its own tags, the send endpoint (one POST by the
chosen machine), a save that breaks off never loading anything, the size limit, the audit, and the whole loop
with real agents over real HTTP and WebSocket; a source that stops part way is reported the same whichever of its
two signs the server hears first (#215)."""

import asyncio
import http.client
import json
import subprocess
import threading
import time

import pytest
from sqlalchemy import select

from forgeyard.db import AuditEvent, new_session
from forgeyard.machines import imagecopy, upload
from forgeyard.workloads import images
from tests.conftest import add_user, sign_in
from tests.image_helpers import build_image, docker_save
from tests.machines_helpers import connect, hello, joined
from tests.test_prefetch import DOCKER, FEATURES, cache
from tests.test_workload_stream import recv, recv_type
from tests.waits import wait_until
from tests.workload_helpers import FakeDocker

SAVES = FEATURES + ["image_ids", "image_load", "image_save"]


def online(ws, features=SAVES):
    ws.send_json(hello("nvidia-rtx4090x2", features=list(features), runtimes=["process", "container"],
                       runtime_info=DOCKER, model_cache=cache()))
    assert recv(ws)["type"] == "welcome"
    return recv_type(ws, "desired")


def audits(action="node.image_copied") -> list[dict]:
    db = new_session()
    try:
        return [e.detail | {"_target": e.target} for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action)
                                                                     .order_by(AuditEvent.id))]
    finally:
        db.close()


class InThread:
    """A console request that waits on an agent, run in a thread while the test plays the agent."""

    def __init__(self, fn):
        self.answer = None
        self.thread = threading.Thread(target=lambda: setattr(self, "answer", fn()), daemon=True)
        self.thread.start()

    def result(self, timeout=20):
        self.thread.join(timeout)
        assert self.answer is not None, "the request did not end"
        return self.answer


@pytest.fixture()
def img(monkeypatch):
    image = build_image("ghcr.io/acme/copyme:1.0", arches=("amd64",))
    lock = {**images.load_lock(), image.ref: image.lock_entry()}
    monkeypatch.setattr(images, "load_lock", lambda path=None: lock)
    return image


def agent_on_the_servers_loop(owner):
    """A client like the agent whose requests run on the test server's one event loop, as with uvicorn: the
    copy's queues are shared by the source's POST, the copy and the target's GET."""
    from tests.machines_helpers import agent_client

    c = agent_client(owner)
    c.portal = owner.portal
    return c


def load_into(fake: FakeDocker, data: bytes) -> None:
    subprocess.run([str(fake.path), "load"], input=data, check=True, capture_output=True)


# ------------------------------------------------------------------ the console's side, with agents played here
def test_fy_icp_001_a_machines_images_are_listed_to_admins_by_its_agent(owner):
    """FY-ICP-001: GET /api/machines/{id}/images asks the machine's agent (image_list) and answers its images, each
    with the recipe image it is; admins only; a machine offline, or whose agent cannot save images, is refused."""
    src, old = joined(owner, "src"), joined(owner, "old")
    add_user(owner, "op", role="operator")
    op = sign_in(owner, "op", "quiet-meadow-4821")
    assert op.get(f"/api/machines/{src['node_id']}/images").status_code == 403
    r = owner.get(f"/api/machines/{src['node_id']}/images")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "source_offline"
    with connect(owner, src["credential"]) as ws, connect(owner, old["credential"]) as ows:
        online(ws)
        online(ows, FEATURES + ["image_load"])
        r = owner.get(f"/api/machines/{old['node_id']}/images")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "source_cannot_save"
        call = InThread(lambda: owner.get(f"/api/machines/{src['node_id']}/images"))
        ask = recv_type(ws, "image_list")
        item = {"id": "sha256:" + "a" * 64, "tags": ["ghcr.io/ggml-org/llama.cpp:server"], "size": 123,
                "os": "linux", "architecture": "amd64", "content": None, "created": "2026-11-01T00:00:00Z"}
        ws.send_json({"v": 1, "type": "image_listed", "request_id": ask["request_id"], "ok": True, "images": [item]})
        r = call.result()
        assert r.status_code == 200
        [got] = r.json()["images"]
        assert got["id"] == item["id"] and got["tags"] == item["tags"] and got["size"] == 123
        assert isinstance(got["matches"], list)
        call = InThread(lambda: owner.get(f"/api/machines/{src['node_id']}/images"))
        ask = recv_type(ws, "image_list")
        ws.send_json({"v": 1, "type": "image_listed", "request_id": ask["request_id"], "ok": False,
                      "reason": "docker image ls failed: permission denied"})
        r = call.result()
        assert r.status_code == 409 and "permission denied" in r.json()["detail"]["message"]


def test_fy_icp_002_copies_are_refused_before_a_byte_moves(owner, monkeypatch):
    """FY-ICP-002: a copy is refused, and not audited, when the image is not named by an id, the source is the
    target, the source is offline or cannot save, the target is offline or cannot load, the source already sends
    an image, or the target already takes an upload; operators may not copy. A source that refuses to save, or
    sends nothing within START_TIMEOUT, ends the copy with why (audited)."""
    monkeypatch.setattr(imagecopy, "START_TIMEOUT", 1.0)
    src, tgt, bare = joined(owner, "src"), joined(owner, "tgt"), joined(owner, "bare")
    add_user(owner, "op", role="operator")
    image = "sha256:" + "b" * 64

    def copy(target, body, client=owner):
        return client.post(f"/api/machines/{target}/images/copy", json=body)

    assert copy(tgt["node_id"], {"source_id": src["node_id"], "image": image}, sign_in(owner, "op", "quiet-meadow-4821")).status_code == 403
    r = copy(tgt["node_id"], {"source_id": src["node_id"], "image": image})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "source_offline"
    with connect(owner, src["credential"]) as ws, connect(owner, tgt["credential"]) as tws, \
            connect(owner, bare["credential"]) as bws:
        online(ws)
        online(tws)
        online(bws, FEATURES)
        for body, code in (({"source_id": src["node_id"], "image": "ghcr.io/acme/x:1"}, "bad_image"),
                           ({"source_id": tgt["node_id"], "image": image}, "same_machine"),
                           ({"source_id": bare["node_id"], "image": image}, "source_cannot_save")):
            r = copy(tgt["node_id"], body)
            assert r.json()["detail"]["code"] == code, r.text
        r = copy(bare["node_id"], {"source_id": src["node_id"], "image": image})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "agent_cannot_load"
        imagecopy._sends["x" * 32] = imagecopy.Send("x" * 32, src["node_id"], "src")
        r = copy(tgt["node_id"], {"source_id": src["node_id"], "image": image})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "source_busy"
        imagecopy._sends.clear()
        upload._uploads["y" * 32] = upload.Upload("y" * 32, tgt["node_id"], "tgt", 10)
        r = copy(tgt["node_id"], {"source_id": src["node_id"], "image": image})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "upload_busy"
        upload._uploads.clear()
        assert audits() == []
        call = InThread(lambda: copy(tgt["node_id"], {"source_id": src["node_id"], "image": image}))
        ask = recv_type(ws, "image_save")
        ws.send_json({"v": 1, "type": "image_saved", "request_id": ask["request_id"], "ok": False,
                      "reason": "that image is not on this machine (any more)"})
        r = call.result()
        assert r.status_code == 409 and r.json()["detail"]["code"] == "image_save_failed"
        assert "not on this machine" in r.json()["detail"]["message"]
        call = InThread(lambda: copy(tgt["node_id"], {"source_id": src["node_id"], "image": image}))
        recv_type(ws, "image_save")
        r = call.result()
        assert r.status_code == 504 and r.json()["detail"]["code"] == "source_no_send"
        assert imagecopy._sends == {}
    assert [a["ok"] for a in audits()] == [False, False]


def test_fy_icp_003_what_the_source_sends_is_piped_to_the_target_and_compared(owner, img):
    """FY-ICP-003 (agents played here): the source is asked to save the image by its id; its POST to the send
    ticket is piped into the target's image upload (no size: chunked); the target's answer comes back with the
    recipe each image is and whether it is the image the source has (by content id); audited with both machines."""
    from tests.test_relay_images import engine_recipe
    from tests.machines_helpers import bearer

    engine_recipe(owner, img.ref)
    src, tgt = joined(owner, "src"), joined(owner, "tgt")
    p = img.platforms["amd64"]
    data = docker_save(img)
    with connect(owner, src["credential"]) as ws, connect(owner, tgt["credential"]) as tws:
        online(ws)
        online(tws)
        call = InThread(lambda: owner.post(f"/api/machines/{tgt['node_id']}/images/copy",
                                           json={"source_id": src["node_id"], "image": p.config_digest}))
        ask = recv_type(ws, "image_save")
        assert ask["image"] == p.config_digest and ask["path"].startswith("/agent/v1/image-send/")
        sent = {}

        def source_posts():
            def body():
                for i in range(0, len(data), 50_000):
                    yield data[i:i + 50_000]
            sent["r"] = agent_on_the_servers_loop(owner).post(ask["path"], content=body(), headers=bearer(src["credential"]))

        t = threading.Thread(target=source_posts, daemon=True)
        t.start()
        load = recv_type(tws, "image_load")
        assert "size" not in load
        got = agent_on_the_servers_loop(owner).get(load["path"], headers=bearer(tgt["credential"]))
        assert got.status_code == 200 and got.content == data and "content-length" not in got.headers
        t.join(10)
        assert sent["r"].status_code == 200 and sent["r"].json() == {"ok": True, "bytes": len(data)}
        loaded = {"id": p.config_digest, "tags": [img.name], "content": p.content, "os": "linux",
                  "architecture": "amd64"}
        tws.send_json({"v": 1, "type": "image_loaded", "request_id": load["request_id"], "ok": True, "images": [loaded]})
        ws.send_json({"v": 1, "type": "image_saved", "request_id": ask["request_id"], "ok": True, "bytes": len(data),
                      "image": {**loaded, "tags": [img.name]}})
        r = call.result()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bytes"] == len(data) and body["source"] == {"id": src["node_id"], "name": "src"}
    [item] = body["images"]
    assert item["same_as_source"] is True and item["matches"][0]["how"] == "checked"
    [a] = audits()
    assert a["_target"] == "tgt" and a["from_machine"] == "src" and a["ok"] is True and a["bytes"] == len(data)
    assert a["images"][0]["content"] == p.content
    assert imagecopy.same_image({"id": "sha256:" + "1" * 64, "content": "c1"}, {"id": "sha256:" + "2" * 64,
                                                                                 "content": "c1"}) is True
    assert imagecopy.same_image({"id": "sha256:" + "1" * 64, "content": "c1"}, {"content": "c2"}) is False
    assert imagecopy.same_image({"id": "sha256:" + "1" * 64}, {"id": "1" * 64}) is True
    assert imagecopy.same_image(None, {"id": "sha256:" + "1" * 64}) is None


def test_fy_icp_004_the_send_endpoint_takes_only_the_chosen_sources_one_post(owner):
    """FY-ICP-004: POST /agent/v1/image-send/<ticket> is refused to a browser (403), an unknown credential (401),
    another machine (404), an unknown ticket (404) and a second POST (409)."""
    from tests.machines_helpers import agent_client, bearer

    src, other = joined(owner, "src"), joined(owner, "other")
    send = imagecopy.Send("T" * 32, src["node_id"], "src")
    send.claimed = True
    imagecopy._sends[send.ticket] = send
    c = agent_client(owner)
    path = f"/agent/v1/image-send/{send.ticket}"
    assert c.post(path, content=b"x", headers={**bearer(src["credential"]), "Origin": "https://evil"}).status_code == 403
    assert c.post(path, content=b"x", headers=bearer("nope-0123456789")).status_code == 401
    assert c.post(path, content=b"x", headers=bearer(other["credential"])).status_code == 404
    assert c.post(path + "x", content=b"x", headers=bearer(src["credential"])).status_code == 404
    r = c.post(path, content=b"x", headers=bearer(src["credential"]))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "send_claimed"


# ------------------------------------------------------------------ the source agent
def test_fy_icp_005_the_agent_lists_its_images_and_saves_only_its_own_by_id(tmp_path, img):
    """FY-ICP-005: the agent lists its engine's images (id, tags, content id, size, platform); it saves one only by
    an id from that listing (a name from the server is never saved), under its own tags (Podman with
    --multi-image-archive for several), by its id when it has none; a save that fails part way raises from the
    stream after what it wrote; a cancel stops it."""
    from forgeyard.agent.workloads.container import ContainerRuntime, SaveFailed
    from forgeyard.agent.workloads.records import RecordStore

    fake = FakeDocker(tmp_path / "bin")
    data = docker_save(img, tags=[img.name, "ghcr.io/acme/copyme:latest"])
    load_into(fake, data)
    rt = ContainerRuntime(RecordStore(tmp_path / "agent"), agent_id="a1", engine=str(fake.path))
    p = img.platforms["amd64"]
    [item] = rt.list_images()
    assert item["id"] == p.config_digest and item["content"] == p.content and item["size"] == len(data)
    assert item["tags"] == [img.name, "ghcr.io/acme/copyme:latest"] and item["architecture"] == "amd64"
    entry, chunks = rt.save_stream(p.config_digest, threading.Event())
    assert entry == item and b"".join(chunks) == data
    assert fake.calls()[-1] == ["save", img.name, "ghcr.io/acme/copyme:latest"]
    for bad in (img.name, "sha256:" + "0" * 64, "../../etc", ""):
        with pytest.raises(SaveFailed):
            rt.save_stream(bad, threading.Event())
    podman = tmp_path / "bin" / "podman"
    podman.write_bytes(fake.path.read_bytes())
    podman.chmod(0o755)
    rt.engine = str(podman)
    _, chunks = rt.save_stream(p.config_digest, threading.Event())
    assert b"".join(chunks) == data
    assert fake.calls()[-1] == ["save", "--multi-image-archive", img.name, "ghcr.io/acme/copyme:latest"]
    rt.engine = str(fake.path)
    fake._update(lambda d: d["images"][0].update(RepoTags=["<none>:<none>"]))
    assert rt.list_images()[0]["tags"] == []
    _, chunks = rt.save_stream(p.config_digest, threading.Event())
    b"".join(chunks)
    assert fake.calls()[-1] == ["save", p.config_digest]
    fake.save_fails_after(100_000)
    _, chunks = rt.save_stream(p.config_digest, threading.Event())
    got = b""
    with pytest.raises(SaveFailed, match="save failed: Error response from daemon: unexpected EOF"):
        for chunk in chunks:
            got += chunk
    assert 0 < len(got) <= 100_000 + 65536 and data.startswith(got)
    fake._update(lambda d: d.update(save_fail_after=None, save_delay=0.5))
    cancel = threading.Event()
    _, chunks = rt.save_stream(p.config_digest, cancel)
    next(chunks)
    cancel.set()
    t0 = time.monotonic()
    with pytest.raises(SaveFailed, match="cancelled"):
        list(chunks)
    assert time.monotonic() - t0 < 5


def test_fy_icp_006_the_agent_says_it_can_save_and_answers_only_a_server_that_asks(tmp_path, monkeypatch, img):
    """FY-ICP-006: an agent with a container engine lists "image_save"; one without does not; it answers image_list
    and image_save only when its server lists the feature; a save goes only to a path of the send form, and one
    at a time."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.container import ContainerRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor

    monkeypatch.setenv(SIMULATION_ENV, "1")
    fake = FakeDocker(tmp_path / "bin")
    load_into(fake, docker_save(img))
    rt = ContainerRuntime(RecordStore(tmp_path / "agent"), agent_id="a1", engine=str(fake.path))
    sup = Supervisor(tmp_path / "agent", {"container": rt}, models=ModelCache(tmp_path / "cache"), tick=0.1,
                     port_range=(31960, 31969))
    agent = Agent("http://127.0.0.1:1", "cred-0123456789abcdef", SimulatedInventory("nvidia-rtx4090x2"), supervisor=sup)
    feats = agent.hello({"accelerators": [], "collectors": {}})["features"]
    assert "image_save" in feats and len(feats) <= 16
    none = ContainerRuntime(RecordStore(tmp_path / "b"), agent_id="a2", engine=str(tmp_path / "missing-docker"))
    plain = Agent("http://127.0.0.1:1", "cred-0123456789abcdef", SimulatedInventory("nvidia-rtx4090x2"),
                  supervisor=Supervisor(tmp_path / "b", {"container": none}, tick=0.1, port_range=(31970, 31979)))
    assert "image_save" not in plain.hello({"accelerators": [], "collectors": {}})["features"]

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

    posted = []
    agent.relay_client.post = lambda path, chunks, length: (posted.append((path, b"".join(chunks), length)),
                                                            {"ok": True})[1]
    p = img.platforms["amd64"]

    async def run(features, *msgs):
        agent.server_features = set(features)
        ws = WS(list(msgs))
        await agent._receive_loop(ws)
        await asyncio.gather(*list(agent._answers))
        return ws.sent

    lst = {"v": 1, "type": "image_list", "request_id": "r1"}
    save = {"v": 1, "type": "image_save", "request_id": "r2", "path": "/agent/v1/image-send/" + "S" * 32,
            "image": p.config_digest}
    assert asyncio.run(run({"workloads"}, lst, save)) == []
    [listed] = asyncio.run(run({"workloads", "image_save"}, lst))
    assert listed["type"] == "image_listed" and listed["ok"] and listed["images"][0]["id"] == p.config_digest
    [saved] = asyncio.run(run({"workloads", "image_save"}, save))
    assert saved["ok"] and saved["image"]["id"] == p.config_digest and posted[-1][0] == save["path"]
    assert posted[-1][2] is None and saved["bytes"] == len(posted[-1][1])
    [bad] = asyncio.run(run({"workloads", "image_save"}, {**save, "path": "https://elsewhere.example/x"}))
    assert not bad["ok"] and "path" in bad["reason"]
    agent._saving = True
    [busy] = asyncio.run(run({"workloads", "image_save"}, save))
    assert not busy["ok"] and "already sending" in busy["reason"]


# ------------------------------------------------------------------ the whole loop
class AgentThread:
    def __init__(self, tmp_path, server, credential, fake: FakeDocker, name, ports):
        from forgeyard.agent.backoff import Backoff
        from forgeyard.agent.client import Agent
        from forgeyard.agent.inventory import SimulatedInventory
        from forgeyard.agent.models import ModelCache
        from forgeyard.agent.workloads.container import ContainerRuntime
        from forgeyard.agent.workloads.records import RecordStore
        from forgeyard.agent.workloads.supervisor import Supervisor

        rt = ContainerRuntime(RecordStore(tmp_path / f"{name}-agent"), agent_id=name, engine=str(fake.path))
        sup = Supervisor(tmp_path / f"{name}-agent", {"container": rt}, models=ModelCache(tmp_path / f"{name}-cache"),
                         tick=0.1, port_range=ports)
        self.agent = Agent(server, credential, SimulatedInventory("nvidia-rtx4090x2"),
                           backoff=Backoff(base=0.05, cap=0.2), supervisor=sup)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=lambda: self.loop.run_until_complete(self.agent.run()), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.agent.stop)
        self.thread.join(15)


@pytest.fixture()
def live(monkeypatch, tmp_path, img):
    from forgeyard.agent.client import join
    from forgeyard.agent.inventory import SIMULATION_ENV
    from tests.conftest import OWNER, SETUP_TOKEN
    from tests.test_agent import LiveServer, _free_port, _http

    monkeypatch.setenv(SIMULATION_ENV, "1")
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    with LiveServer(port):
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        text = open("forgeyard/recipes/llama-cpp.yaml").read().split("\nvariants:")[0].replace(
            "name: llama-cpp", "name: copy-engine")
        text += f"\nvariants:\n  - name: cpu\n    when: {{vendor: cpu}}\n    runtime: container\n    image: {img.ref}\n"
        _http(port, "POST", "/api/recipes", {"yaml": text}, cookie)
        w = type("Live", (), {})()
        w.port, w.server, w.cookie, w.tmp = port, server, cookie, tmp_path
        w.src = join(server, _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"], name="src")
        w.tgt = join(server, _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"], name="tgt")
        w.sfake, w.tfake = FakeDocker(tmp_path / "src-bin"), FakeDocker(tmp_path / "tgt-bin")
        w.data = docker_save(img)
        load_into(w.sfake, w.data)

        def call(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            try:
                conn.request(method, path, body=json.dumps(body) if body is not None else None,
                             headers={"Content-Type": "application/json", "X-Forgeyard": "1", "Cookie": cookie})
                r = conn.getresponse()
                return r.status, json.loads(r.read() or b"{}")
            finally:
                conn.close()

        w.call = call
        yield w


def both(w):
    a = AgentThread(w.tmp, w.server, w.src["credential"], w.sfake, "src", (31980, 31984))
    b = AgentThread(w.tmp, w.server, w.tgt["credential"], w.tfake, "tgt", (31985, 31989))

    def ready():
        ms = {m["name"]: m["features"] for m in w.call("GET", "/api/machines")[1]["machines"]}
        return "image_save" in ms.get("src", []) and "image_load" in ms.get("tgt", [])
    return a, b, ready


def test_fy_icp_007_a_real_agent_copies_an_image_to_another_real_agent(live, img):
    """FY-ICP-007 (real agents, real HTTP and WebSocket): the source lists its image, an admin copies it: the
    source's engine saves it, the server passes it on, the target's engine loads exactly that archive; the answer
    says it is the recipe's pinned image (checked) and the source's image; audited."""
    w = live
    a, b, ready = both(w)
    p = img.platforms["amd64"]
    with a, b:
        wait_until(ready, "both agents connected", 15)
        status, listing = w.call("GET", f"/api/machines/{w.src['node_id']}/images")
        assert status == 200
        [item] = listing["images"]
        assert item["id"] == p.config_digest and item["tags"] == [img.name]
        assert item["matches"] == [{"recipe": "copy-engine", "variant": "cpu", "image": img.ref, "how": "checked"}]
        status, body = w.call("POST", f"/api/machines/{w.tgt['node_id']}/images/copy",
                              {"source_id": w.src["node_id"], "image": item["id"]})
    assert status == 200, body
    [loaded] = body["images"]
    assert loaded["id"] == p.config_digest and loaded["content"] == p.content and loaded["same_as_source"] is True
    assert loaded["matches"][0]["how"] == "checked" and body["bytes"] == len(w.data)
    assert w.tfake.loads() == [w.data] and [i["Id"] for i in w.tfake.images()] == [p.config_digest]
    assert ["save", img.name] in w.sfake.calls()
    [rec] = audits()
    assert rec["ok"] is True and rec["from_machine"] == "src" and rec["_target"] == "tgt"
    assert imagecopy._sends == {} and upload._uploads == {}


def test_fy_icp_008_a_save_that_breaks_off_or_is_too_large_loads_nothing(live, img, monkeypatch):
    """FY-ICP-008 (real agents): the source's save fails part way: its POST never ends cleanly, the target's
    answer breaks off, so its engine is stopped before the archive ends and loads nothing; the console says it broke
    off and what the source says (#215: whichever the server heard first, FY-ICP-009) and the audit log records it.
    An image larger than IMAGE_UPLOAD_MAX_GB is stopped there, nothing loaded."""
    w = live
    a, b, ready = both(w)
    p = img.platforms["amd64"]
    w.sfake.save_fails_after(120_000)
    with a, b:
        wait_until(ready, "both agents connected", 15)
        status, body = w.call("POST", f"/api/machines/{w.tgt['node_id']}/images/copy",
                              {"source_id": w.src["node_id"], "image": p.config_digest})
        assert status == 400 and body["detail"]["code"] == "upload_broken", body
        assert body["detail"]["message"] == BROKE_OFF, body
        assert w.tfake.images() == []
        w.sfake._update(lambda d: d.update(save_fail_after=None))
        monkeypatch.setattr(upload, "UNIT", 1000)  # IMAGE_UPLOAD_MAX_GB (64) * 1000 bytes
        status, body = w.call("POST", f"/api/machines/{w.tgt['node_id']}/images/copy",
                              {"source_id": w.src["node_id"], "image": p.config_digest})
        assert status == 413 and body["detail"]["code"] == "upload_too_large", body
        assert w.tfake.images() == []
        wait_until(lambda: imagecopy._sends == {} and upload._uploads == {}, "the copy forgotten")
        wait_until(lambda: not a.agent._saving, "the source's save stopped", 15)  # one save at a time there
        monkeypatch.setattr(upload, "UNIT", 1024**3)
        monkeypatch.setattr(imagecopy, "STALL", 1.0)  # a source that stops sending part way
        w.sfake.slow_saves(3.0)
        t0 = time.monotonic()
        status, body = w.call("POST", f"/api/machines/{w.tgt['node_id']}/images/copy",
                              {"source_id": w.src["node_id"], "image": p.config_digest})
        assert status == 400 and "sent nothing for 1 s" in body["detail"]["message"], body
        assert time.monotonic() - t0 < 6 and w.tfake.images() == []
        # The server closed the source's connection: its next write fails and its save is stopped, long before
        # the 15 s the whole slow save would take.
        wait_until(lambda: imagecopy._sends == {} and upload._uploads == {} and not a.agent._saving,
                   "the copy forgotten, and the source's save stopped", 8)
    recs = audits()
    assert [r["ok"] for r in recs] == [False, False, False] and all(r["from_machine"] == "src" for r in recs)
    assert recs[0]["reason"] == BROKE_OFF[:200]


# The fake engine writes two 64 KiB pieces of the archive, then fails (save_fails_after(120_000)).
BROKE_OFF = ("The copy broke off after 131072 bytes: src stopped sending, so nothing was loaded on tgt. src says: docker "
             "save failed: Error response from daemon: unexpected EOF. Fix that there, then copy the image again.")


@pytest.mark.parametrize("first", ["the source's answer", "the broken POST"])
def test_fy_icp_009_a_source_that_stops_part_way_is_reported_the_same_whichever_sign_comes_first(live, img,
                                                                                                 monkeypatch, first):
    """FY-ICP-009 (#215, real agents): a source whose save fails part way gives the server two signs: its POST
    breaks off, and its agent answers image_saved (ok false, the bytes it sent, "docker save failed: …") on its
    WebSocket. Either can come first, and the answer can come before the server has read a byte of the POST. Each
    order is held here with events, not sleeps: the answer reaches the server while the POST is held before it is
    read; or the agent's answer is held until the copy has seen the POST break off and waits for it. Both end the
    same: 400 upload_broken, the copy broke off after the bytes the source sent, nothing was loaded on the target,
    what the source says; the audit records those words."""
    from forgeyard.machines import hub as hub_module

    w = live
    a, b, ready = both(w)
    p = img.platforms["amd64"]
    w.sfake.save_fails_after(120_000)
    seen = {}
    if first == "the source's answer":
        answered = threading.Event()
        answer, node_of = hub_module.Connection.answer, imagecopy._node_of

        def heard(self, request_id, message):
            if message.get("type") == "image_saved":
                seen["read then"] = [s.received for s in imagecopy._sends.values()]
            done = answer(self, request_id, message)
            if message.get("type") == "image_saved":
                answered.set()
            return done

        def held(credential):  # the source's POST, before the server reads it
            assert answered.wait(30), "the source never answered"
            return node_of(credential)

        monkeypatch.setattr(hub_module.Connection, "answer", heard)
        monkeypatch.setattr(imagecopy, "_node_of", held)
    else:
        waiting = threading.Event()
        said, send_answer = imagecopy._said, a.agent._send_answer

        async def waits(saved):  # the copy has seen the POST break off and waits for the source's word
            seen["broke then"] = [(s.broke, s.received > 0) for s in imagecopy._sends.values()]
            seen["answered then"] = saved.done()
            waiting.set()
            return await said(saved)

        async def late(ws, kind, data, answer):
            if kind == "image_saved":
                assert await asyncio.to_thread(waiting.wait, 30), "the copy never waited for the source"
            await send_answer(ws, kind, data, answer)

        monkeypatch.setattr(imagecopy, "_said", waits)
        a.agent._send_answer = late
    with a, b:
        wait_until(ready, "both agents connected", 15)
        status, body = w.call("POST", f"/api/machines/{w.tgt['node_id']}/images/copy",
                              {"source_id": w.src["node_id"], "image": p.config_digest})
        assert status == 400 and body["detail"] == {"code": "upload_broken", "message": BROKE_OFF}, body
        assert w.tfake.images() == []
        wait_until(lambda: imagecopy._sends == {} and upload._uploads == {} and not a.agent._saving,
                   "the copy forgotten, and the source's save stopped", 15)
    if first == "the source's answer":
        assert seen["read then"] == [0]  # the server had not read a byte of the POST when the answer came
        assert w.tfake.loads() == []     # and the target was never asked to load
    else:
        assert seen["broke then"] == [(True, True)] and seen["answered then"] is False
    [rec] = audits()
    assert rec["ok"] is False and rec["reason"] == BROKE_OFF[:200] and rec["from_machine"] == "src"


def test_fy_icp_010_a_source_that_sent_nothing_refused_and_one_that_sent_bytes_broke_off(owner):
    """FY-ICP-010 (#215, agents played here): a source that answers ok false having sent no bytes refused the save
    (409 image_save_failed, its words: FY-ICP-002); one that says it sent bytes broke off, even when the server never
    saw a byte of its POST (400 upload_broken, the bytes it says it sent, its words without a doubled full stop)."""
    src, tgt = joined(owner, "src"), joined(owner, "tgt")
    image = "sha256:" + "c" * 64

    def copy():
        return InThread(lambda: owner.post(f"/api/machines/{tgt['node_id']}/images/copy",
                                           json={"source_id": src["node_id"], "image": image}))

    with connect(owner, src["credential"]) as ws, connect(owner, tgt["credential"]) as tws:
        online(ws)
        online(tws)
        call = copy()
        ask = recv_type(ws, "image_save")
        ws.send_json({"v": 1, "type": "image_saved", "request_id": ask["request_id"], "ok": False, "bytes": 0,
                      "reason": "docker save failed: reference does not exist"})
        r = call.result()
        assert r.status_code == 409 and r.json()["detail"] == {"code": "image_save_failed", "message":
                                                               "src: docker save failed: reference does not exist"}
        call = copy()
        ask = recv_type(ws, "image_save")
        ws.send_json({"v": 1, "type": "image_saved", "request_id": ask["request_id"], "ok": False, "bytes": 65536,
                      "reason": "docker save failed: unexpected EOF."})
        r = call.result()
        assert r.status_code == 400 and r.json()["detail"] == {"code": "upload_broken", "message": (
            "The copy broke off after 65536 bytes: src stopped sending, so nothing was loaded on tgt. src says: "
            "docker save failed: unexpected EOF. Fix that there, then copy the image again.")}
        assert imagecopy._sends == {} and upload._uploads == {}
    assert [(a["ok"], a["reason"][:20]) for a in audits()] == [(False, "src: docker save fai"),
                                                               (False, "The copy broke off a")]


def test_fy_icp_011_a_post_that_breaks_off_unread_waits_for_the_sources_word(live, img, monkeypatch):
    """FY-ICP-011 (#215, real agents): the source's save fails before its first byte, so its POST breaks off before
    the server reads anything. When its agent says why (bytes 0), the copy is a refusal with its words (FY-ICP-002);
    when it says nothing within SAID_GRACE, the copy broke off and the source did not say why."""
    w = live
    a, b, ready = both(w)
    p = img.platforms["amd64"]
    w.sfake.save_fails_after(0)
    with a, b:
        wait_until(ready, "both agents connected", 15)
        status, body = w.call("POST", f"/api/machines/{w.tgt['node_id']}/images/copy",
                              {"source_id": w.src["node_id"], "image": p.config_digest})
        assert status == 409 and body["detail"] == {"code": "image_save_failed", "message":
                                                    "src: docker save failed: Error response from daemon: unexpected "
                                                    "EOF"}, body
        wait_until(lambda: imagecopy._sends == {} and not a.agent._saving, "the copy forgotten", 15)
        send_answer = a.agent._send_answer
        dropped = threading.Event()

        async def mute(ws, kind, data, answer):
            if kind == "image_saved":
                dropped.set()
                return
            await send_answer(ws, kind, data, answer)

        a.agent._send_answer = mute
        monkeypatch.setattr(imagecopy, "SAID_GRACE", 0.5)  # a time-out is what this part is about
        status, body = w.call("POST", f"/api/machines/{w.tgt['node_id']}/images/copy",
                              {"source_id": w.src["node_id"], "image": p.config_digest})
        assert status == 400 and body["detail"]["code"] == "upload_broken", body
        assert body["detail"]["message"].startswith(
            "The copy broke off after 0 bytes: src stopped sending, so nothing was loaded on tgt. src did not say why "
            "(it broke off after 0 bytes ("), body
        assert dropped.is_set() and w.tfake.loads() == [] and w.tfake.images() == []
    assert [r["ok"] for r in audits()] == [False, False]
