"""Engine images through the server (1.4, issue #35 PR 4, D4; FY-RELAY-019…026): `image_relay` in the desired
lists of agents that relay images (pinned and recorded images only), the manifest of the machine's platform
and the blobs that manifest names, by digest, from the registry with an anonymous pull token, Range passed
through, a whole blob checked on its way, refusals (tag-only, not wanted, not in the image, no build for the
machine's architecture, the setting, browsers), and placement that no longer avoids a machine for a registry
it could not reach when its pinned image comes through the server."""

import hashlib
import logging

import pytest
from sqlalchemy import select

from forgeyard.config import get_settings
from forgeyard.db import ModelDownload, Node, new_session
from forgeyard.machines import relay
from forgeyard.models import reach
from forgeyard.workloads import images, registry as oci
from tests.image_helpers import FakeRegistry, build_image, digest
from tests.machines_helpers import agent_client, bearer, connect, joined
from tests.test_placement_fetch import failure, set_node
from tests.test_prefetch import DOCKER, FEATURES, gguf_id
from tests.test_scheduler import deploy, reps
from tests.test_workload_stream import recv_type
from tests.waits import wait_until

IMAGES = FEATURES + ["image_ids", "relay", "relay_images"]
RECIPE = "relay-engine"


@pytest.fixture()
def reg(monkeypatch):
    img = build_image("ghcr.io/acme/engine:1.0")
    server = FakeRegistry(img)
    monkeypatch.setitem(oci.ENDPOINTS, "ghcr.io", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    get_settings.cache_clear()
    lock = {**images.load_lock(), img.ref: img.lock_entry()}
    monkeypatch.setattr(images, "load_lock", lambda path=None: lock)
    yield img, server
    server.close()


def engine_recipe(owner, image: str, name: str = RECIPE) -> None:
    """llama-cpp's recipe with one CPU container variant: this image."""
    text = open("forgeyard/recipes/llama-cpp.yaml").read().split("\nvariants:")[0].replace("name: llama-cpp",
                                                                                           f"name: {name}")
    text += f"\nvariants:\n  - name: cpu\n    when: {{vendor: cpu}}\n    runtime: container\n    image: {image}\n"
    r = owner.post("/api/recipes", json={"yaml": text})
    assert r.status_code == 201, r.text


def model_id() -> int:
    """qwen3-1.7b-gguf (the catalog seeded once)."""
    from forgeyard.db import CatalogModel

    db = new_session()
    try:
        found = db.scalar(select(CatalogModel.id).where(CatalogModel.name == "qwen3-1.7b-gguf"))
    finally:
        db.close()
    return found if found is not None else gguf_id()


def start(owner, node_id: int, recipe: str = RECIPE) -> dict:
    r = owner.post("/api/downloads", json={"model_id": model_id(), "machine_ids": [node_id], "recipe": recipe})
    assert r.status_code == 201, r.text
    out = r.json()["downloads"][0]
    db = new_session()
    try:
        out["uid"] = db.get(ModelDownload, out["id"]).uid  # what the agent and the relay know it by
    finally:
        db.close()
    return out


def get(owner, path, credential=None, **headers):
    return agent_client(owner).get(path, headers={**(bearer(credential) if credential else {}), **headers})


def machine(owner, name="dock", features=IMAGES, arch="x86_64"):
    """A connected CPU machine with Docker whose agent relays images; (joined, ws context)."""
    a = joined(owner, name)
    return a, connect(owner, a["credential"])


def hello_on(ws, features=IMAGES, arch="x86_64"):
    from tests.machines_helpers import hello
    from tests.test_prefetch import cache
    from tests.test_workload_stream import recv

    ws.send_json(hello("nvidia-rtx4090x2", features=list(features), runtimes=["process", "container"],
                       runtime_info=DOCKER, model_cache=cache(), platform={"os": "Linux", "arch": arch}))
    assert recv(ws)["type"] == "welcome"
    return recv_type(ws, "desired")


# ------------------------------------------------------------------ what the agent is told
def test_fy_relay_019_desired_lists_carry_image_relay_for_pinned_recorded_images_only(owner, reg):
    """FY-RELAY-019: a download whose engine is a pinned, recorded image gets `image_relay` beside it ("auto";
    "server" when set so or after a remembered failure to reach the registry); none for "direct", for an agent
    without "relay_images", or for a tag-only image. A replica's comes beside its spec, never in it."""
    img, _ = reg
    engine_recipe(owner, img.ref)
    engine_recipe(owner, "ghcr.io/acme/engine:1.0", "tag-only")
    a, b = joined(owner, "dock"), joined(owner, "old")
    with connect(owner, a["credential"]) as ws, connect(owner, b["credential"]) as old:
        hello_on(ws)
        hello_on(old, features=FEATURES + ["image_ids", "relay"])
        start(owner, a["node_id"])
        start(owner, b["node_id"])
        item = recv_type(ws, "desired")["downloads"][0]
        assert item["image"] == img.ref and item["image_ids"] == img.check()
        assert item["image_relay"] == {"path": f"/agent/v1/relay/download/{item['id']}/image", "mode": "auto"}
        assert "image_relay" not in recv_type(old, "desired")["downloads"][0]
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "server"})
        assert recv_type(ws, "desired")["downloads"][0]["image_relay"]["mode"] == "server"
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "direct"})
        assert "image_relay" not in recv_type(ws, "desired")["downloads"][0]
        reach.record(a["node_id"], failure(host="ghcr.io", phase="image"))
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "auto"})
        assert recv_type(ws, "desired")["downloads"][0]["image_relay"]["mode"] == "server"
    node = Node(id=999, features=IMAGES, fetch_via="auto", platform={"arch": "x86_64"})
    tagged = relay.attach({"id": "d1", "image": "ghcr.io/acme/engine:1.0"}, node, "download", "d1")
    assert "image_relay" not in tagged
    unrecorded = relay.attach({"id": "d1", "image": "ghcr.io/acme/other:1@sha256:" + "0" * 64}, node, "download", "d1")
    assert "image_relay" not in unrecorded
    spec = {"runtime": {"type": "container", "image": img.ref}}
    out = relay.attach({"id": "r1", "spec": spec}, node, "replica", "r1")
    assert out["image_relay"] == {"path": "/agent/v1/relay/replica/r1/image", "mode": "auto"} and out["spec"] == spec
    assert relay.relays(node, "image", img.ref) and not relay.relays(node, "image", "ghcr.io/acme/engine:1.0")
    assert not relay.relays(Node(id=999, features=FEATURES + ["relay"], fetch_via="auto"), "image", img.ref)


# ------------------------------------------------------------------ the manifest
def test_fy_relay_020_the_manifest_of_the_machines_platform_and_who_may_have_it(owner, reg):
    """FY-RELAY-020: the agent gets the recorded manifest of its platform (amd64 here, arm64 for an aarch64
    machine), the registry's exact bytes; only with its credential, only while the download is wanted, never
    another machine's, never to a browser, not when set to "direct", and not to an agent without the
    feature; a machine of an architecture the image has no build for is told so."""
    img, server = reg
    engine_recipe(owner, img.ref)
    a, b, c = joined(owner, "dock"), joined(owner, "arm"), joined(owner, "risc")
    with connect(owner, a["credential"]) as ws, connect(owner, b["credential"]) as arm, \
            connect(owner, c["credential"]) as risc:
        hello_on(ws)
        hello_on(arm, arch="aarch64")
        hello_on(risc, arch="riscv64")
        d = start(owner, a["node_id"])
        darm = start(owner, b["node_id"])
        drisc = start(owner, c["node_id"])
        path = f"/agent/v1/relay/download/{d['uid']}/image/manifest"
        r = get(owner, path, a["credential"])
        assert r.status_code == 200, r.text
        assert r.content == img.platforms["amd64"].manifest
        assert r.headers["docker-content-digest"] == img.platforms["amd64"].manifest_digest
        assert r.headers["forgeyard-platform"] == "linux/amd64"
        assert r.headers["content-type"].startswith("application/vnd.oci.image.manifest.v1+json")
        assert server.tokens >= 1 and all(x["auth"] == "Bearer tok-1" for x in server.requests
                                          if "/manifests/" in x["path"])
        r = get(owner, f"/agent/v1/relay/download/{darm['uid']}/image/manifest", b["credential"])
        assert r.status_code == 200 and r.content == img.platforms["arm64"].manifest
        r = get(owner, f"/agent/v1/relay/download/{drisc['uid']}/image/manifest", c["credential"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "relay_no_platform"
        assert "riscv64" in r.json()["detail"]["message"] and "linux/amd64" in r.json()["detail"]["message"]
        assert get(owner, path).status_code == 401
        assert get(owner, path, b["credential"]).status_code == 404  # another machine's download
        assert get(owner, path, a["credential"], Origin="https://evil.example").status_code == 403
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "direct"})
        r = get(owner, path, a["credential"])
        assert r.status_code == 403 and r.json()["detail"]["code"] == "relay_off"
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "auto"})
        set_node(a["node_id"], features=FEATURES + ["image_ids", "relay"])
        r = get(owner, path, a["credential"])
        assert r.status_code == 403 and r.json()["detail"]["code"] == "relay_off"
        set_node(a["node_id"], features=IMAGES)
        assert get(owner, path, a["credential"]).status_code == 200
        owner.post(f"/api/downloads/{d['id']}/cancel")
        r = get(owner, path, a["credential"])
        assert r.status_code == 404 and r.json()["detail"]["code"] == "relay_not_wanted"


def test_fy_relay_021_a_manifest_that_is_not_the_recorded_one_is_never_served(owner, reg):
    """FY-RELAY-021: a registry that serves other bytes for the recorded manifest digest gets a 502 and nothing
    is passed on; a manifest that names another config than the recorded one neither."""
    img, server = reg
    engine_recipe(owner, img.ref)
    a = joined(owner, "dock")
    with connect(owner, a["credential"]) as ws:
        hello_on(ws)
        d = start(owner, a["node_id"])
        server.bad_manifest = True
        r = get(owner, f"/agent/v1/relay/download/{d['uid']}/image/manifest", a["credential"])
        assert r.status_code == 502 and "hashes to" in r.json()["detail"]["message"]
        server.bad_manifest = False
    target = relay.ImageTarget(img.ref, "ghcr.io", img.repo, "linux/amd64", img.platforms["amd64"].manifest_digest,
                               img.platforms["arm64"].config_digest)
    with pytest.raises(relay.Refused) as e:
        relay.manifest_of(target)
    assert e.value.status == 502 and "another config" in e.value.message


# ------------------------------------------------------------------ the blobs
def test_fy_relay_022_blobs_by_digest_with_range_only_those_the_manifest_names(owner, reg):
    """FY-RELAY-022: the config and each layer are streamed from the registry (its token there, none to the CDN
    it redirects to), whole or from a byte (206, Content-Range); a digest the manifest does not name (another
    platform's layer, the index, a made-up one) is refused, and a malformed one too."""
    img, server = reg
    engine_recipe(owner, img.ref)
    a = joined(owner, "dock")
    p = img.platforms["amd64"]
    with connect(owner, a["credential"]) as ws:
        hello_on(ws)
        d = start(owner, a["node_id"])
        base = f"/agent/v1/relay/download/{d['uid']}/image/blobs"
        r = get(owner, f"{base}/{p.config_digest}", a["credential"])
        assert r.status_code == 200 and r.content == p.config
        big = p.layers[1]
        r = get(owner, f"{base}/{big}", a["credential"], Range="bytes=1000-")
        assert r.status_code == 206 and r.content == img.blobs[big][1000:]
        assert r.headers["content-range"] == f"bytes 1000-{len(img.blobs[big]) - 1}/{len(img.blobs[big])}"
        cdn = server.blob_requests(big)
        assert cdn and cdn[-1]["range"] == "bytes=1000-" and cdn[-1]["auth"] is None
        asked = [x for x in server.requests if f"/blobs/{big}" in x["path"]]
        assert asked and all(x["auth"] and x["auth"].startswith("Bearer tok-") for x in asked)
        for layer in p.layers:
            assert get(owner, f"{base}/{layer}", a["credential"]).content == img.blobs[layer]
        for other in (img.platforms["arm64"].layers[0], img.index_digest, digest(b"nothing")):
            r = get(owner, f"{base}/{other}", a["credential"])
            assert r.status_code == 404 and r.json()["detail"]["code"] == "relay_not_in_image", other
        assert get(owner, f"{base}/sha256:../../etc", a["credential"]).status_code == 404
        assert get(owner, f"{base}/{p.config_digest}", a["credential"], Range="bytes=1-2,5-6").status_code == 416


def test_fy_relay_023_a_blob_whose_bytes_do_not_match_is_never_sent_whole(owner, reg, caplog):
    """FY-RELAY-023: the server hashes a whole blob on its way: when the registry's bytes do not hash to the
    digest, the last piece is held back (the agent gets a short answer, never a complete wrong blob) and the log
    says so. A resumed (ranged) request is passed on as it is: the agent checks the whole file."""
    img, server = reg
    engine_recipe(owner, img.ref)
    a = joined(owner, "dock")
    big = img.platforms["amd64"].layers[1]
    server.corrupt.add(big)
    with connect(owner, a["credential"]) as ws:
        hello_on(ws)
        d = start(owner, a["node_id"])
        url = f"/agent/v1/relay/download/{d['uid']}/image/blobs/{big}"
        caplog.set_level(logging.INFO, logger="forgeyard.relay")
        with agent_client(owner).stream("GET", url, headers=bearer(a["credential"])) as r:
            got = b""
            try:
                for chunk in r.iter_bytes():
                    got += chunk
            except Exception:
                pass  # a client that checks Content-Length says the answer was short
        assert len(got) < len(img.blobs[big])
        assert hashlib.sha256(got).hexdigest() != big[7:]
        assert any("do not match their digest" in m for m in caplog.messages)
        r = get(owner, url, a["credential"], Range="bytes=10-")
        assert r.status_code == 206 and len(r.content) == len(img.blobs[big]) - 10


def test_fy_relay_024_tag_only_images_are_refused_with_a_word_to_upload_them(owner, reg):
    """FY-RELAY-024: a download whose engine image is not pinned (a tag only) gets no `image_relay`, and asking
    for it anyway is refused, saying to upload the image to the machine instead."""
    img, _ = reg
    engine_recipe(owner, "ghcr.io/acme/engine:1.0", "tag-only")
    a = joined(owner, "dock")
    with connect(owner, a["credential"]) as ws:
        hello_on(ws)
        d = start(owner, a["node_id"], "tag-only")
        item = recv_type(ws, "desired")["downloads"][0]
        assert item["image"] == "ghcr.io/acme/engine:1.0" and "image_relay" not in item
        r = get(owner, f"/agent/v1/relay/download/{d['uid']}/image/manifest", a["credential"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "relay_image_not_pinned"
        assert "Upload an image" in r.json()["detail"]["message"]
        r = get(owner, f"/agent/v1/relay/download/{d['uid']}/image/blobs/{img.platforms['amd64'].config_digest}",
                a["credential"])
        assert r.status_code == 409


def test_fy_relay_025_a_replicas_image_and_a_relay_closed_when_the_deployment_goes(owner, reg):
    """FY-RELAY-025: a deployment's replica on a relaying machine gets `image_relay` beside its spec; its
    manifest and blobs are served while it is wanted and refused once the deployment is deleted."""
    img, _ = reg
    model_id()
    engine_recipe(owner, img.ref)
    a = joined(owner, "dock")
    with connect(owner, a["credential"]) as ws:
        hello_on(ws)
        set_node(a["node_id"], fetch_via="server")
        dep = deploy(owner, "relayed", "qwen3-1.7b-gguf", RECIPE, params={"gpus": 0})
        rep = wait_until(lambda: next((r for r in recv_type(ws, "desired")["replicas"]), None), "the replica")
        assert rep["image_relay"] == {"path": f"/agent/v1/relay/replica/{rep['id']}/image", "mode": "server"}
        assert "image_relay" not in rep["spec"]["runtime"]
        path = f"/agent/v1/relay/replica/{rep['id']}/image/manifest"
        assert get(owner, path, a["credential"]).status_code == 200
        assert reps(owner, dep)
        owner.delete(f"/api/deployments/{dep['deployment']['id']}")
        assert get(owner, path, a["credential"]).status_code == 404


# ------------------------------------------------------------------ placement
def test_fy_relay_026_placement_no_longer_avoids_a_machine_whose_pinned_image_comes_through_the_server(owner, reg):
    """FY-RELAY-026 (#71 + relay_images): dock-a could not reach ghcr.io. With an agent that relays images, the
    pinned image comes through the server, so dock-a (first by name) takes the replica and the page says so;
    with an agent that relays only models and engines, or for a tag-only image, it is avoided as before."""
    from tests.test_placement_fetch import cpu

    img, _ = reg
    model_id()
    engine_recipe(owner, img.ref)
    engine_recipe(owner, "ghcr.io/acme/engine:1.0", "tag-only")
    a = cpu("dock-a", runtime_info=DOCKER)
    cpu("dock-b", runtime_info=DOCKER)
    set_node(a, features=["workloads", "models", "image_ids", "relay", "relay_images"])
    reach.record(a, failure(host="ghcr.io", phase="image"))
    d = deploy(owner, "small", "qwen3-1.7b-gguf", RECIPE, params={"gpus": 0})
    r = reps(owner, d)[0]
    assert r["machine"]["name"] == "dock-a"
    assert r["placement_note"] == ("dock-a could not reach ghcr.io (for the engine image) just now; the engine image "
                                   "comes through the server"), r["placement_note"]
    assert {m["name"]: m for m in d["placement"]["machines"]}["dock-a"]["via_server"] == ["image"]
    d2 = deploy(owner, "small2", "qwen3-1.7b-gguf", "tag-only", params={"gpus": 0})
    assert reps(owner, d2)[0]["machine"]["name"] == "dock-b"
    set_node(a, features=["workloads", "models", "image_ids", "relay"])
    d3 = deploy(owner, "small3", "qwen3-1.7b-gguf", RECIPE, params={"gpus": 0})
    assert reps(owner, d3)[0]["machine"]["name"] == "dock-b"


# ------------------------------------------------------------------ the whole loop
def test_fy_relay_039_a_real_agent_that_cannot_reach_the_registry_gets_the_image_through_the_server(
        monkeypatch, tmp_path, reg):
    """FY-RELAY-039: over real HTTP and WebSocket: Download to… a model and its engine, a pinned image, to a
    machine whose engine cannot reach the registry ("auto"): the pull fails on DNS, the agent switches to the
    server, which fetches the manifest and blobs from the registry with its pull token; the agent checks and
    loads them (a Docker-like loader checking every diff id), and the download ends "done" via the server, with
    the image under its tag and recognised by its recorded ids."""
    import asyncio
    import json

    from forgeyard.agent.backoff import Backoff
    from forgeyard.agent.client import Agent, join
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.container import ContainerRuntime
    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.conftest import OWNER, SETUP_TOKEN
    from tests.model_helpers import FakeHF, Repo, add_catalog, git_oid
    from tests.test_agent import LiveServer, _free_port, _http, _wait
    from tests.workload_helpers import FakeDocker

    img, server_reg = reg
    rev = "d" * 40
    data = b"GGUF" + bytes(range(256)) * 400
    hf = FakeHF(Repo("acme/tiny", rev, {"t.gguf": data}))
    monkeypatch.setenv(SIMULATION_ENV, "1")
    monkeypatch.setenv("HF_ENDPOINT", hf.url)
    get_settings.cache_clear()
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    fake = FakeDocker(tmp_path / "bin")
    fake.offline()
    try:
        with LiveServer(port):
            _, cookie = _http(port, "POST", "/api/auth/setup",
                              {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
            text = open("forgeyard/recipes/llama-cpp.yaml").read().split("\nvariants:")[0].replace(
                "name: llama-cpp", f"name: {RECIPE}")
            text += f"\nvariants:\n  - name: cpu\n    when: {{vendor: cpu}}\n    runtime: container\n    image: {img.ref}\n"
            _http(port, "POST", "/api/recipes", {"yaml": text}, cookie)
            joined_ = join(server, _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"], name="off")
            node = joined_["node_id"]
            mid = add_catalog("tiny-gguf", {"kind": "hf", "repo": "acme/tiny", "ref": "main", "revision": rev,
                                            "format": "gguf", "file": "t.gguf", "size_bytes": len(data),
                                            "files": [{"path": "t.gguf", "size": len(data), "oid": git_oid(data),
                                                       "sha256": hashlib.sha256(data).hexdigest()}]},
                              task="chat", fmt="gguf", source="hf://acme/tiny")

            def api(method, path, body=None):
                return _http(port, method, path, body, cookie)[0]

            async def scenario():
                state = tmp_path / "agent"
                rt = ContainerRuntime(RecordStore(state), agent_id="agent-1", engine=str(fake.path))
                prt = ProcessRuntime(RecordStore(state), tmp_path / "work", euid=lambda: 4242)
                models = ModelCache(tmp_path / "cache", allow_private=True)
                sup = Supervisor(state, {"process": prt, "container": rt}, models=models, tick=0.1,
                                 port_range=(31960, 31970))
                agent = Agent(server, joined_["credential"], SimulatedInventory("nvidia-rtx4090x2"),
                              backoff=Backoff(base=0.05, cap=0.2), supervisor=sup)
                run = asyncio.create_task(agent.run())
                told = []
                desired = sup.set_desired
                sup.set_desired = lambda msg: (told.append(msg), desired(msg))[1]
                try:
                    assert await _wait(lambda: "relay_images" in api("GET", "/api/machines")["machines"][0]["features"])
                    first = (await asyncio.to_thread(api, "POST", "/api/downloads",
                                                     {"model_id": mid, "machine_ids": [node], "recipe": RECIPE}))
                    did = first["downloads"][0]["id"]

                    def done():
                        d = next(x for x in api("GET", f"/api/downloads?machine_id={node}")["downloads"] if x["id"] == did)
                        return d["state"] in ("done", "failed") and d

                    d = await asyncio.to_thread(wait_until, done, "the download with its image", 60)
                    assert d["state"] == "done" and d["via"] == "server", d
                    items = [i for m in told for i in m.get("downloads") or []]
                    assert items and items[0]["image_relay"]["mode"] == "auto"
                finally:
                    agent.stop()
                    await asyncio.wait_for(run, 10)

            asyncio.run(scenario())
    finally:
        hf.close()
    p = img.platforms["amd64"]
    assert len(fake.pulls()) == 1 and len(fake.loads()) == 1
    [loaded] = fake.images()
    assert loaded["Id"] == p.config_digest and loaded["RepoTags"] == ["ghcr.io/acme/engine:1.0"]
    fetched = {r["path"].rsplit("/", 1)[1] for r in server_reg.blob_requests()}
    assert fetched == {p.config_digest, *p.layers}
    assert not list((tmp_path / "cache" / ".partial").glob("image-*"))
    assert json.dumps(loaded)  # (inspectable)
