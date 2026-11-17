"""An admin uploads an image file to a machine (1.4, issue #35 PR 4, D4; FY-RELAY-035…038): admins only, a size
limit, only to a connected agent that can load images, one at a time per machine; the file is piped to the
agent's GET of its one-time ticket (only that machine, only once) and what it loaded comes back with the recipe
image each one is; audited. And the whole loop over real HTTP with a real agent."""

import asyncio
import gzip
import json
import threading
import time

import pytest
from sqlalchemy import select

from forgeyard.db import AuditEvent, new_session
from forgeyard.machines import upload
from forgeyard.workloads import images
from tests.conftest import add_user, sign_in
from tests.image_helpers import build_image, docker_save
from tests.machines_helpers import agent_client, bearer, connect, joined
from tests.test_prefetch import DOCKER, FEATURES, cache
from tests.test_relay_images import engine_recipe
from tests.test_workload_stream import recv, recv_type
from tests.waits import wait_until

LOADS = FEATURES + ["image_ids", "image_load"]


def online(ws, features=LOADS):
    from tests.machines_helpers import hello

    ws.send_json(hello("nvidia-rtx4090x2", features=list(features), runtimes=["process", "container"],
                       runtime_info=DOCKER, model_cache=cache()))
    assert recv(ws)["type"] == "welcome"
    return recv_type(ws, "desired")


def post(client, node_id, data: bytes, **headers):
    return client.post(f"/api/machines/{node_id}/images", content=data,
                       headers={"Content-Type": "application/octet-stream", "X-Filename": "engine.tar.gz", **headers})


def audits():
    db = new_session()
    try:
        return [e.detail for e in db.scalars(select(AuditEvent).where(AuditEvent.action == "node.image_uploaded")
                                             .order_by(AuditEvent.id))]
    finally:
        db.close()


class Uploading:
    """A POST running in a thread (the browser), while the test plays the agent."""

    def __init__(self, client, node_id, data, **headers):
        self.answer = None
        self.thread = threading.Thread(target=self._run, args=(client, node_id, data, headers), daemon=True)
        self.thread.start()

    def _run(self, client, node_id, data, headers):
        self.answer = post(client, node_id, data, **headers)

    def result(self, timeout=20):
        self.thread.join(timeout)
        assert self.answer is not None, "the upload did not end"
        return self.answer


@pytest.fixture()
def img(monkeypatch):
    image = build_image()
    lock = {**images.load_lock(), image.ref: image.lock_entry()}
    monkeypatch.setattr(images, "load_lock", lambda path=None: lock)
    return image


def test_fy_relay_035_who_may_upload_what_and_where(owner, monkeypatch):
    """FY-RELAY-035: admins only (operators and users get 403); a file over IMAGE_UPLOAD_MAX_GB is refused before
    a byte goes anywhere (413), so is one without a size (411); a machine that is offline, or whose agent cannot
    load images, is refused with the reason; the machine says how large a file may be."""
    a, b = joined(owner, "dock"), joined(owner, "old")
    add_user(owner, "op", role="operator")
    add_user(owner, "viewer")
    op, viewer = sign_in(owner, "op", "quiet-meadow-4821"), sign_in(owner, "viewer", "quiet-meadow-4821")
    assert post(op, a["node_id"], b"x" * 10).status_code == 403
    assert post(viewer, a["node_id"], b"x" * 10).status_code == 403
    r = post(owner, a["node_id"], b"x" * 10)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "machine_offline"
    monkeypatch.setattr(upload, "UNIT", 1)  # IMAGE_UPLOAD_MAX_GB=64 counts bytes now
    with connect(owner, a["credential"]) as ws, connect(owner, b["credential"]) as old:
        online(ws)
        online(old, features=FEATURES + ["image_ids"])
        r = post(owner, a["node_id"], b"x" * 65)
        assert r.status_code == 413 and r.json()["detail"]["code"] == "upload_too_large"
        assert "IMAGE_UPLOAD_MAX_GB" in r.json()["detail"]["message"]
        r = owner.post(f"/api/machines/{a['node_id']}/images", content=(c for c in [b"abc"]),
                       headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 411
        r = post(owner, b["node_id"], b"x" * 10)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "agent_cannot_load"
        m = next(x for x in owner.get("/api/machines").json()["machines"] if x["id"] == a["node_id"])
        assert m["image_upload_max_bytes"] == 64 and "image_load" in m["features"]
    assert post(owner, 999, b"x").status_code == 404
    assert [d["ok"] for d in audits()] == []  # refusals before a byte moved are not uploads


def test_fy_relay_036_the_file_goes_to_that_machines_agent_once_and_what_it_loaded_comes_back(owner, img):
    """FY-RELAY-036: the agent is told where to fetch the file; only its credential gets it, only once (another
    machine 404, a second GET 409, none 401, a browser 403); the bytes are the file's; its answer comes back with
    the recipe images each loaded image is: the pinned one by its recorded ids ("checked"), a tag-only one by
    its tag; the upload is audited with what was loaded."""
    engine_recipe(owner, img.ref)
    engine_recipe(owner, "ghcr.io/acme/engine:1.0", "tag-only")
    data = docker_save(img, compress=True)
    a, b = joined(owner, "dock"), joined(owner, "other")
    with connect(owner, a["credential"]) as ws:
        online(ws)
        up = Uploading(owner, a["node_id"], data)
        msg = recv_type(ws, "image_load")
        assert msg["size"] == len(data) and msg["name"] == "engine.tar.gz"
        path = msg["path"]
        assert path.startswith("/agent/v1/image-upload/") and len(path.rsplit("/", 1)[1]) >= 24
        # The agent's GETs through the same app client as the upload (one event loop, as in a real server); the
        # endpoint reads no cookie, only the machine credential.
        agent = owner
        assert agent.get(path, headers=bearer(b["credential"])).status_code == 404
        assert agent_client(owner).get(path).status_code == 401
        assert agent.get(path, headers={**bearer(a["credential"]), "Origin": "https://x.example"}).status_code == 403
        got = agent.get(path, headers=bearer(a["credential"]))
        assert got.status_code == 200 and got.content == data
        assert int(got.headers["content-length"]) == len(data)
        again = agent.get(path, headers=bearer(a["credential"]))
        assert again.status_code == 409 and again.json()["detail"]["code"] == "upload_claimed"
        p = img.platforms["amd64"]
        ws.send_json({"v": 1, "type": "image_loaded", "request_id": msg["request_id"], "ok": True,
                      "images": [{"id": p.config_digest, "tags": ["ghcr.io/acme/engine:1.0"], "content": p.content,
                                  "os": "linux", "architecture": "amd64"}]})
        r = up.result()
        assert r.status_code == 200, r.text
        [loaded] = r.json()["images"]
        assert loaded["id"] == p.config_digest and loaded["tags"] == ["ghcr.io/acme/engine:1.0"]
        assert {(m["recipe"], m["how"]) for m in loaded["matches"]} == {("relay-engine", "checked"),
                                                                        ("tag-only", "tag")}
        assert r.json()["bytes"] == len(data)
    [event] = audits()
    assert event["ok"] is True and event["file"] == "engine.tar.gz" and event["bytes"] == len(data)
    assert event["images"] == [{"id": p.config_digest, "tags": ["ghcr.io/acme/engine:1.0"]}]
    assert upload._uploads == {}


def test_fy_relay_037_an_agent_that_does_not_fetch_or_cannot_load_ends_the_upload_with_why(owner, img, monkeypatch):
    """FY-RELAY-037: an agent that never fetches the file ends the upload (504, after CONNECT_TIMEOUT); one that
    cannot load it is answered 422 with its reason; a second upload to a machine already taking one is refused;
    each failure is audited."""
    monkeypatch.setattr(upload, "CONNECT_TIMEOUT", 0.5)
    data = docker_save(img)
    a = joined(owner, "dock")
    with connect(owner, a["credential"]) as ws:
        online(ws)
        r = post(owner, a["node_id"], data)
        assert r.status_code == 504 and r.json()["detail"]["code"] == "agent_no_fetch"
        recv_type(ws, "image_load")
        monkeypatch.setattr(upload, "CONNECT_TIMEOUT", 10)
        up = Uploading(owner, a["node_id"], data)
        msg = recv_type(ws, "image_load")
        wait_until(lambda: upload.busy(a["node_id"]), "the upload under way")
        second = post(owner, a["node_id"], data)
        assert second.status_code == 409 and second.json()["detail"]["code"] == "upload_busy"
        owner.get(msg["path"], headers=bearer(a["credential"]))
        ws.send_json({"v": 1, "type": "image_loaded", "request_id": msg["request_id"], "ok": False,
                      "reason": "docker load failed: invalid tar header"})
        r = up.result()
        assert r.status_code == 422 and "invalid tar header" in r.json()["detail"]["message"]
    events = audits()
    assert [e["ok"] for e in events] == [False, False]
    assert events[0]["reason"].startswith("dock's agent did not start fetching the file within 0.5 s")
    assert events[1]["reason"] == "docker load failed: invalid tar header"


def test_fy_relay_038_a_real_agent_loads_an_uploaded_file(monkeypatch, tmp_path, img):
    """FY-RELAY-038: over real HTTP and WebSocket: an admin uploads a gzip `docker save` file of a pinned image;
    the agent streams it into its engine (a Docker-like loader that checks every layer's diff id) and the
    console is told the image it loaded is the pinned image of the recipe, checked by its recorded ids."""
    import urllib.request

    from forgeyard.agent.backoff import Backoff
    from forgeyard.agent.client import Agent, join
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.container import ContainerRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.conftest import OWNER, SETUP_TOKEN
    from tests.test_agent import LiveServer, _free_port, _http, _wait
    from tests.workload_helpers import FakeDocker

    monkeypatch.setenv(SIMULATION_ENV, "1")
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    data = docker_save(img, compress=True)
    fake = FakeDocker(tmp_path / "bin")
    with LiveServer(port):
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        text = open("forgeyard/recipes/llama-cpp.yaml").read().split("\nvariants:")[0].replace(
            "name: llama-cpp", "name: relay-engine")
        text += f"\nvariants:\n  - name: cpu\n    when: {{vendor: cpu}}\n    runtime: container\n    image: {img.ref}\n"
        _http(port, "POST", "/api/recipes", {"yaml": text}, cookie)
        joined_ = join(server, _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"], name="box")
        node = joined_["node_id"]

        def upload_file():
            req = urllib.request.Request(f"{server}/api/machines/{node}/images", data=data, method="POST",
                                         headers={"Content-Type": "application/octet-stream", "X-Forgeyard": "1",
                                                  "X-Filename": "engine.tar.gz", "Cookie": cookie})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())

        async def scenario():
            rt = ContainerRuntime(RecordStore(tmp_path / "agent"), agent_id="agent-1", engine=str(fake.path))
            sup = Supervisor(tmp_path / "agent", {"container": rt}, models=ModelCache(tmp_path / "cache"), tick=0.1,
                             port_range=(31940, 31950))
            agent = Agent(server, joined_["credential"], SimulatedInventory("nvidia-rtx4090x2"),
                          backoff=Backoff(base=0.05, cap=0.2), supervisor=sup)
            run = asyncio.create_task(agent.run())
            try:
                assert await _wait(lambda: "image_load" in _http(port, "GET", "/api/machines", None, cookie)[0]
                                   ["machines"][0]["features"])
                started = time.monotonic()
                answer = await asyncio.to_thread(upload_file)
                return answer, time.monotonic() - started
            finally:
                agent.stop()
                await asyncio.wait_for(run, 10)

        answer, _ = asyncio.run(scenario())
    p = img.platforms["amd64"]
    [loaded] = answer["images"]
    assert loaded["id"] == p.config_digest and loaded["content"] == p.content
    assert loaded["matches"] == [{"recipe": "relay-engine", "variant": "cpu", "image": img.ref, "how": "checked"}]
    assert fake.loads() == [data] and gzip.decompress(data)
    assert [i["Id"] for i in fake.images()] == [p.config_digest]


def test_fy_relay_042_an_uploaded_docker_hub_image_matches_its_recipe_by_the_short_name(owner):
    """FY-RELAY-042: Docker lists a Docker Hub image without "docker.io/" (and an official one without
    "library/"), while a recipe may name it in full: the vision recipe's image is "docker.io/<namespace>/
    forgeyard-vision:<version>". An upload of that image matched no recipe on a real machine; names are compared
    in Docker's full form now. Another registry with the same path is still another image."""
    from forgeyard import __version__
    from forgeyard.install.images import vision_image
    from forgeyard.workloads.registry import full_name

    full = vision_image(__version__, "cpu")
    assert full.startswith("docker.io/")
    short = full.removeprefix("docker.io/")
    db = new_session()
    try:
        got = upload.recipe_matches(db, {"id": "sha256:" + "1" * 64, "tags": [short]})
        assert {"recipe": "vision", "variant": "cpu", "image": full, "how": "tag"} in got
        assert any(m["recipe"] == "vision" for m in upload.recipe_matches(db, {"tags": [full]}))
        assert not upload.recipe_matches(db, {"tags": ["ghcr.io/" + short]})
    finally:
        db.close()
    assert full_name("python:3.13") == full_name("docker.io/library/python:3.13") == full_name("docker.io/python:3.13") \
        == "docker.io/library/python:3.13"
    assert full_name("acme/x:1") == "docker.io/acme/x:1" and full_name("ghcr.io/acme/x:1") == "ghcr.io/acme/x:1"
    assert full_name("localhost:5000/x:1") == "localhost:5000/x:1" and full_name("python") == "docker.io/library/python"
