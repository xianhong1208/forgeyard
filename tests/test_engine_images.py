"""A machine's engine images on its page, and removing one it no longer uses (1.4.2, issue #123; FY-EIMG-001…009):
the listing (only images a recipe names, with sizes, the recipes and the deployments there that use them), who may
remove one, the refusals before the agent is asked (an id the machine does not list, an image no recipe names, the
agent's own image, an image a replica there uses), the agent's own checks (its own image, its replicas, any container
of the engine, never `rmi -f`), the audit, and the whole loop with a real agent on the tests' fake Docker CLI."""

import asyncio
import json
import subprocess
import threading

import pytest
from sqlalchemy import select

from forgeyard.db import AuditEvent, Deployment, Replica, Workload, new_session
from forgeyard.workloads import images
from tests.conftest import add_user, sign_in
from tests.image_helpers import build_image, docker_save
from tests.machines_helpers import connect, hello, joined
from tests.test_prefetch import DOCKER, FEATURES, cache, gguf_id
from tests.test_workload_stream import recv, recv_type
from tests.waits import wait_until
from tests.workload_helpers import FakeDocker

LISTS = FEATURES + ["image_ids", "image_load", "image_save"]
REMOVES = LISTS + ["image_remove"]
TAGGED = "registry.example/acme/tts:2.0"          # a custom recipe names it by tag only
OTHER = "docker.io/someone/else:latest"           # no recipe names it: someone else's image on a shared host
AGENT = "docker.io/tonyhuang1101/forgeyard-agent:1.4.1"


def online(ws, features=REMOVES):
    ws.send_json(hello("nvidia-rtx4090x2", features=list(features), runtimes=["process", "container"],
                       runtime_info=DOCKER, model_cache=cache()))
    assert recv(ws)["type"] == "welcome"
    return recv_type(ws, "desired")


def audits() -> list[dict]:
    db = new_session()
    try:
        return [e.detail | {"_target": e.target} for e in db.scalars(
            select(AuditEvent).where(AuditEvent.action == "node.image_removed").order_by(AuditEvent.id))]
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


def recipe(owner, name: str, image: str) -> None:
    """llama-cpp's recipe with one CPU container variant: this image."""
    text = open("forgeyard/recipes/llama-cpp.yaml").read().split("\nvariants:")[0].replace("name: llama-cpp",
                                                                                           f"name: {name}")
    text += f"\nvariants:\n  - name: cpu\n    when: {{vendor: cpu}}\n    runtime: container\n    image: {image}\n"
    r = owner.post("/api/recipes", json={"yaml": text})
    assert r.status_code == 201, r.text


@pytest.fixture()
def img(monkeypatch):
    image = build_image("ghcr.io/acme/engine:1.0", arches=("amd64",))
    lock = {**images.load_lock(), image.ref: image.lock_entry()}
    monkeypatch.setattr(images, "load_lock", lambda path=None: lock)
    return image


def listed(img) -> list[dict]:
    """What a 1.4.2 agent lists: the pinned image (under its tag, recognised by its ids), a tag-only recipe image,
    someone else's image, and the agent's own image."""
    p = img.platforms["amd64"]
    return [{"id": p.config_digest, "tags": [img.name], "content": p.content, "size": 2000, "os": "linux",
             "architecture": "amd64", "created": None},
            {"id": "sha256:" + "a" * 64, "tags": [TAGGED.removeprefix("docker.io/")], "content": None, "size": 900,
             "os": "linux", "architecture": "amd64", "created": None},
            {"id": "sha256:" + "b" * 64, "tags": [OTHER.removeprefix("docker.io/")], "content": None, "size": 50,
             "os": "linux", "architecture": "amd64", "created": None},
            {"id": "sha256:" + "c" * 64, "tags": [AGENT.removeprefix("docker.io/")], "content": None, "size": 70,
             "os": "linux", "architecture": "amd64", "created": None}]


def answer_list(ws, items, **extra) -> None:
    ask = recv_type(ws, "image_list")
    ws.send_json({"v": 1, "type": "image_listed", "request_id": ask["request_id"], "ok": True, "images": items,
                  **extra})


def using(node_id: int, image: str, name: str = "tts-1", *, state: str = "running", final: bool = False) -> int:
    """A deployment whose replica on the machine runs this container image, straight in the database."""
    model = gguf_id()
    db = new_session()
    try:
        w = Workload(name=name, kind="service", runtime="model", spec={"name": name}, desired_state="running")
        db.add(w)
        db.flush()
        d = Deployment(name=name, model_id=model, recipe="tts", served_name=name, workload_id=w.id)
        db.add(d)
        db.add(Replica(workload_id=w.id, node_id=node_id, state=state, final=final,
                       spec={"runtime": {"type": "container", "image": image}}))
        db.commit()
        return d.id
    finally:
        db.close()


# ------------------------------------------------------------------ the console's side, with the agent played here
def test_fy_eimg_001_the_listing_shows_the_images_a_recipe_names(owner, img):
    """FY-EIMG-001: GET /api/machines/{id}/engine-images (everyone signed in) asks the agent and lists only the images a
    recipe names (a pinned one by its recorded ids, a tag-only one by its tag; Docker Hub names compared in full),
    largest first, each with the recipes, the deployments there that use it and why it cannot be removed; the other
    images are only counted; the agent's own image is marked and never removable; the engine's free space is passed
    on. A plain user sees the list and no Remove; an offline machine or an agent that cannot list says why."""
    recipe(owner, "pinned-engine", img.ref)
    recipe(owner, "tts", TAGGED)
    recipe(owner, "agent-named", AGENT)  # a custom recipe could even name the agent's image
    j = joined(owner)
    add_user(owner, "viewer")
    user = sign_in(owner, "viewer", "quiet-meadow-4821")
    r = user.get(f"/api/machines/{j['node_id']}/engine-images")
    assert r.status_code == 200 and r.json()["listed"] is False and "not connected" in r.json()["note"]
    with connect(owner, j["credential"]) as ws:
        online(ws)
        using(j["node_id"], TAGGED)
        call = InThread(lambda: user.get(f"/api/machines/{j['node_id']}/engine-images"))
        answer_list(ws, listed(img), agent_image="sha256:" + "c" * 64, free_bytes=10 * 1024**3,
                    total_bytes=100 * 1024**3)
        r = call.result()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["listed"] and body["others"] == 1 and body["free_bytes"] == 10 * 1024**3
        assert body["can_remove"] is False and body["remove_note"] == "Only admins can remove engine images."
        got = {i["tags"][0]: i for i in body["images"]}
        assert list(got) == [img.name, TAGGED.removeprefix("docker.io/"), AGENT.removeprefix("docker.io/")]
        assert got[img.name]["matches"][0]["how"] == "checked" and got[img.name]["used_by"] == []
        tts = got[TAGGED.removeprefix("docker.io/")]
        assert tts["matches"][0]["how"] == "tag" and [u["name"] for u in tts["used_by"]] == ["tts-1"]
        assert got[AGENT.removeprefix("docker.io/")]["agent"] is True
        call = InThread(lambda: owner.get(f"/api/machines/{j['node_id']}/engine-images"))
        answer_list(ws, listed(img), agent_image="sha256:" + "c" * 64)
        body = call.result().json()
        got = {i["tags"][0]: i for i in body["images"]}
        assert body["can_remove"] is True and body["remove_note"] is None
        assert got[img.name]["remove_note"] is None
        assert got[TAGGED.removeprefix("docker.io/")]["remove_note"] == "In use by tts-1 on gpu-box. Stop it first."
        assert got[AGENT.removeprefix("docker.io/")]["remove_note"] == "The agent runs from this image."
        call = InThread(lambda: owner.get(f"/api/machines/{j['node_id']}/engine-images"))
        ask = recv_type(ws, "image_list")
        ws.send_json({"v": 1, "type": "image_listed", "request_id": ask["request_id"], "ok": False,
                      "reason": "docker image ls failed: permission denied"})
        body = call.result().json()
        assert body["listed"] is False and "permission denied" in body["note"]
    old = joined(owner, "old")
    with connect(owner, old["credential"]) as ws:
        online(ws, FEATURES)
        body = owner.get(f"/api/machines/{old['node_id']}/engine-images").json()
        assert body["listed"] is False and "cannot list" in body["note"]


def test_fy_eimg_002_removals_are_refused_before_the_agent_is_asked(owner, img):
    """FY-EIMG-002: only admins remove; an image not named by an id, an agent that cannot remove (update it), a
    machine offline, an id the machine does not list, an image no recipe names, the agent's own image and an image a
    replica there uses (named, 409) are refused before the agent is asked; none of them is audited."""
    recipe(owner, "pinned-engine", img.ref)
    recipe(owner, "tts", TAGGED)
    recipe(owner, "agent-named", AGENT)
    j, old = joined(owner), joined(owner, "old")
    add_user(owner, "op", role="operator")
    op = sign_in(owner, "op", "quiet-meadow-4821")
    p = img.platforms["amd64"]

    def remove(image, client=owner, node=j["node_id"]):
        return client.post(f"/api/machines/{node}/engine-images/remove", json={"image": image})

    assert remove(p.config_digest, op).status_code == 403
    r = remove(p.config_digest)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "machine_offline"
    with connect(owner, j["credential"]) as ws, connect(owner, old["credential"]) as ows:
        online(ws)
        online(ows, LISTS)
        r = remove(p.config_digest, node=old["node_id"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "agent_too_old"
        assert "Update the agent" in r.json()["detail"]["message"]
        assert remove(img.name).json()["detail"]["code"] == "bad_image"
        using(j["node_id"], TAGGED)
        for image, status, code in ((("sha256:" + "d" * 64), 404, "image_not_found"),
                                    ("sha256:" + "b" * 64, 409, "not_an_engine_image"),
                                    ("sha256:" + "c" * 64, 409, "agent_image"),
                                    ("sha256:" + "a" * 64, 409, "image_in_use")):
            call = InThread(lambda: remove(image))
            answer_list(ws, listed(img), agent_image="sha256:" + "c" * 64)
            r = call.result()
            assert (r.status_code, r.json()["detail"]["code"]) == (status, code), r.text
        assert r.json()["detail"]["in_use"][0]["name"] == "tts-1" and "Stop it first" in r.json()["detail"]["message"]
        # Nothing but image_list reached the agent.
        with pytest.raises(Exception):
            recv_type(ws, "image_remove", timeout=0.5)
    assert audits() == []


def test_fy_eimg_003_the_agent_is_asked_by_id_and_its_answer_is_audited(owner, img):
    """FY-EIMG-003: an image a recipe names and nothing here uses: the agent is asked to remove exactly that id;
    removed: the console gets the bytes freed and the space after, audited ok with the recipes; refused by the agent
    (a container it found uses it): 409 in the agent's words, audited not ok. A stopped deployment no longer holds it."""
    recipe(owner, "pinned-engine", img.ref)
    recipe(owner, "tts", TAGGED)
    j = joined(owner)
    p = img.platforms["amd64"]
    using(j["node_id"], img.ref, "old-engine", state="stopped", final=True)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        call = InThread(lambda: owner.post(f"/api/machines/{j['node_id']}/engine-images/remove",
                                           json={"image": p.config_digest}))
        answer_list(ws, listed(img))
        ask = recv_type(ws, "image_remove")
        assert ask["image"] == p.config_digest and set(ask) == {"v", "type", "request_id", "image"}
        ws.send_json({"v": 1, "type": "image_removed", "request_id": ask["request_id"], "ok": True,
                      "image": listed(img)[0], "freed_bytes": 2000, "free_bytes": 12 * 1024**3,
                      "total_bytes": 100 * 1024**3})
        r = call.result()
        assert r.status_code == 200, r.text
        assert (r.json()["freed_bytes"], r.json()["free_bytes"]) == (2000, 12 * 1024**3)
        call = InThread(lambda: owner.post(f"/api/machines/{j['node_id']}/engine-images/remove",
                                           json={"image": "sha256:" + "a" * 64}))
        answer_list(ws, listed(img))
        ask = recv_type(ws, "image_remove")
        ws.send_json({"v": 1, "type": "image_removed", "request_id": ask["request_id"], "ok": False, "code": "in_use",
                      "reason": "the engine keeps an image a container uses: someones-tts uses it",
                      "in_use": ["someones-tts"]})
        r = call.result()
        assert r.status_code == 409 and r.json()["detail"]["code"] == "image_in_use"
        assert r.json()["detail"]["message"] == "gpu-box: the engine keeps an image a container uses: someones-tts uses it"
    ok, refused = audits()
    assert ok["ok"] is True and ok["image"] == p.config_digest and ok["tags"] == [img.name]
    assert ok["recipes"] == ["pinned-engine"] and ok["freed_bytes"] == 2000 and ok["_target"] == "gpu-box"
    assert refused["ok"] is False and refused["code"] == "in_use" and refused["recipes"] == ["tts"]


# ------------------------------------------------------------------ the agent
def runtime(tmp_path, fake: FakeDocker, **kw):
    from forgeyard.agent.workloads.container import ContainerRuntime
    from forgeyard.agent.workloads.records import RecordStore

    rt = ContainerRuntime(RecordStore(tmp_path / "agent"), agent_id="a1", engine=str(fake.path), **kw)
    rt.mountinfo = tmp_path / "no-mountinfo"
    return rt


def load_into(fake: FakeDocker, data: bytes) -> None:
    subprocess.run([str(fake.path), "load"], input=data, check=True, capture_output=True)


def test_fy_eimg_004_the_agent_removes_only_an_image_of_its_listing_never_forced(tmp_path, img):
    """FY-EIMG-004: the agent removes an image by an id from its own listing (`rmi <id>`, or `rmi <its tags>` when it
    has several: the engine refuses an id several names share), never with -f; a name or an id it does not list is
    not_found; one a replica here uses (users) or any container of the engine uses is in_use and `rmi` is never run;
    an engine that refuses says why, and which tags went."""
    from forgeyard.agent.workloads.container import RemoveRefused

    fake = FakeDocker(tmp_path / "bin")
    load_into(fake, docker_save(img, tags=[img.name, "ghcr.io/acme/engine:latest"]))
    other = build_image("registry.example/acme/tts:2.0", arches=("amd64",))
    load_into(fake, docker_save(other))
    rt = runtime(tmp_path, fake)
    p, q = img.platforms["amd64"], other.platforms["amd64"]
    for bad in (img.name, "sha256:" + "0" * 64, "../x", ""):
        with pytest.raises(RemoveRefused) as e:
            rt.remove_image(bad)
        assert e.value.code == "not_found"
    with pytest.raises(RemoveRefused) as e:
        rt.remove_image(q.config_digest, lambda entry: ["tts-1"] if entry["id"] == q.config_digest else [])
    assert e.value.code == "in_use" and e.value.users == ["tts-1"] and "stop it first" in str(e.value)
    subprocess.run([str(fake.path), "run", "-d", "--name", "someones-tts", other.name], check=True, capture_output=True)
    with pytest.raises(RemoveRefused) as e:
        rt.remove_image(q.config_digest)
    assert e.value.code == "in_use" and e.value.users == ["someones-tts"] and "someones-tts uses it" in str(e.value)
    assert not [c for c in fake.calls() if c[0] == "rmi"]
    out = rt.remove_image(p.config_digest)
    assert out["ok"] and out["image"]["id"] == p.config_digest and "free_bytes" not in out
    assert out["freed_bytes"] == out["image"]["size"] > 0
    assert ["rmi", img.name, "ghcr.io/acme/engine:latest"] in fake.calls()
    assert [i["Id"] for i in fake.images()] == [q.config_digest]
    fake._update(lambda d: d["containers"].clear())
    fake.rmi_fails("conflict: unable to delete (cannot be forced) - image has dependent child images")
    with pytest.raises(RemoveRefused) as e:
        rt.remove_image(q.config_digest)
    assert e.value.code == "failed" and "dependent child images" in str(e.value)
    assert all("-f" not in c and "--force" not in c for c in fake.calls() if c[0] == "rmi")
    fake._update(lambda d: d.update(rmi_fail=None))
    out = rt.remove_image(q.config_digest)
    assert out["ok"] and ["rmi", other.name] in fake.calls() and fake.images() == []


def test_fy_eimg_005_the_agent_never_removes_its_own_image_and_measures_the_space(tmp_path, img):
    """FY-EIMG-005: the agent finds its own container from the engine's mounts (Docker's and Podman's paths) and
    never removes the image it runs from; what was freed is said as the image's size (at most that came back), and
    the space where the engine keeps its images after, when the agent can see that directory."""
    from forgeyard.agent.workloads.container import RemoveRefused, own_container_id

    cid = "f" * 64
    docker_line = (f"812 790 259:2 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw,relatime - ext4 "
                   "/dev/nvme0n1p2 rw")
    podman_line = (f"91 80 0:44 /containers/storage/overlay-containers/{cid}/userdata/hosts /etc/hosts rw - tmpfs "
                   "tmpfs rw")
    other_line = f"700 690 259:2 /srv/data/containers/{cid}/notes /data rw - ext4 /dev/nvme0n1p2 rw"
    for text, want in ((docker_line, cid), (podman_line, cid), (other_line, None), ("", None)):
        f = tmp_path / "mi"
        f.write_text(text + "\n")
        assert own_container_id(f) == want
    assert own_container_id(tmp_path / "missing") is None

    fake = FakeDocker(tmp_path / "bin")
    root = tmp_path / "docker-root"
    root.mkdir()
    fake.info({"ServerVersion": "27.0.0-fake", "Runtimes": {"runc": {}}, "DefaultRuntime": "runc",
               "DockerRootDir": str(root)})
    load_into(fake, docker_save(img))
    rt = runtime(tmp_path, fake)
    assert "root" not in rt.info()  # a path of the host is not reported to the server
    p = img.platforms["amd64"]
    fake._update(lambda d: d["containers"].update({cid: {"name": "forgeyard-agent", "labels": {}, "status": "running",
                                                          "exit": 0, "pid": 1, "image": p.config_digest, "args": [],
                                                          "opts": {}, "env": {}, "logs": []}}))
    rt.mountinfo = tmp_path / "mi"
    rt.mountinfo.write_text(docker_line + "\n")
    assert rt.agent_image() == p.config_digest
    with pytest.raises(RemoveRefused) as e:
        rt.remove_image(p.config_digest)
    assert e.value.code == "agent_image" and not [c for c in fake.calls() if c[0] == "rmi"]
    rt2 = runtime(tmp_path, fake)
    fake._update(lambda d: d["containers"].clear())
    import shutil as sh

    real = sh.disk_usage
    try:
        sh.disk_usage = lambda path: type("du", (), {"total": 100, "free": 45, "used": 55})() if path == str(root) \
            else real(path)
        out = rt2.remove_image(p.config_digest)
    finally:
        sh.disk_usage = real
    assert (out["freed_bytes"], out["free_bytes"], out["total_bytes"]) == (out["image"]["size"], 45, 100)
    assert out["freed_bytes"] > 0


def test_fy_eimg_006_the_supervisor_names_the_replicas_that_use_an_image(tmp_path, img):
    """FY-EIMG-006: a replica here uses a listed image when its spec names it by the tag, by recorded ids that match
    it, or by a digest the engine resolves to it; finished replicas do not count; the desired list does (a replica
    about to start)."""
    from forgeyard.agent.workloads.records import Record
    from forgeyard.agent.workloads.supervisor import Supervisor

    fake = FakeDocker(tmp_path / "bin")
    load_into(fake, docker_save(img))
    rt = runtime(tmp_path, fake)
    sup = Supervisor(tmp_path / "agent", {"container": rt}, tick=0.1, port_range=(31990, 31994))
    entry = rt.list_images()[0]

    def rec(rid, image, final=False, image_ids=None):
        r = Record(id=rid, generation=1, workload=f"w-{rid}", ordinal=0, kind="service", runtime="container",
                   spec={"runtime": {"type": "container", "image": image}}, final=final)
        r.image_ids = image_ids
        sup.records[rid] = r

    rec("a", "ghcr.io/acme/engine:9.9")                     # another tag: not this image
    rec("b", "ghcr.io/acme/engine:1.0", final=True)         # finished
    assert sup.image_users(entry) == []
    rec("c", "registry.example/other@sha256:" + "0" * 64, image_ids=img.check())  # recorded ids match
    rec("d", "ghcr.io/acme/engine:1.0")                     # its tag
    assert sup.image_users(entry) == ["w-c", "w-d"]
    sup.records.clear()
    sup.desired = {"e": {"id": "e", "workload": "w-e", "spec": {"runtime": {"type": "container",
                                                                             "image": entry["id"]}}}}
    assert sup.image_users(entry) == ["w-e"]  # resolved by the engine (the id)


def test_fy_eimg_007_the_agent_says_it_can_remove_and_answers_only_a_server_that_asks(tmp_path, monkeypatch, img):
    """FY-EIMG-007: an agent with a container engine lists "image_remove", one without does not; it answers
    image_remove only when its server lists the feature, and only then adds agent_image and the space to its listing;
    the answer carries the agent's refusal code."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.container import ContainerRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor

    monkeypatch.setenv(SIMULATION_ENV, "1")
    fake = FakeDocker(tmp_path / "bin")
    load_into(fake, docker_save(img))
    rt = runtime(tmp_path, fake)
    sup = Supervisor(tmp_path / "agent", {"container": rt}, models=ModelCache(tmp_path / "cache"), tick=0.1,
                     port_range=(31995, 31999))
    agent = Agent("http://127.0.0.1:1", "cred-0123456789abcdef", SimulatedInventory("nvidia-rtx4090x2"), supervisor=sup)
    feats = agent.hello({"accelerators": [], "collectors": {}})["features"]
    assert "image_remove" in feats and len(feats) <= 16
    none = ContainerRuntime(RecordStore(tmp_path / "b"), agent_id="a2", engine=str(tmp_path / "missing-docker"))
    plain = Agent("http://127.0.0.1:1", "cred-0123456789abcdef", SimulatedInventory("nvidia-rtx4090x2"),
                  supervisor=Supervisor(tmp_path / "b", {"container": none}, tick=0.1, port_range=(31970, 31979)))
    assert "image_remove" not in plain.hello({"accelerators": [], "collectors": {}})["features"]

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

    async def run(features, *msgs):
        agent.server_features = set(features)
        ws = WS(list(msgs))
        await agent._receive_loop(ws)
        await asyncio.gather(*list(agent._answers))
        return ws.sent

    p = img.platforms["amd64"]
    lst = {"v": 1, "type": "image_list", "request_id": "r1"}
    rm = {"v": 1, "type": "image_remove", "request_id": "r2", "image": p.config_digest}
    [old] = asyncio.run(run({"workloads", "image_save"}, lst, rm))
    assert old["type"] == "image_listed" and "agent_image" not in old and fake.images()
    [new] = asyncio.run(run({"workloads", "image_save", "image_remove"}, lst))
    assert new["agent_image"] is None and new["images"][0]["id"] == p.config_digest
    [gone] = asyncio.run(run({"workloads", "image_remove"}, {**rm, "image": img.name}))
    assert gone["type"] == "image_removed" and gone["ok"] is False and gone["code"] == "not_found"
    [done] = asyncio.run(run({"workloads", "image_remove"}, rm))
    assert done["ok"] is True and done["image"]["id"] == p.config_digest and fake.images() == []


# ------------------------------------------------------------------ the whole loop
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
        for name, image in (("pinned-engine", img.ref), ("tts", TAGGED)):
            text = open("forgeyard/recipes/llama-cpp.yaml").read().split("\nvariants:")[0].replace(
                "name: llama-cpp", f"name: {name}")
            text += f"\nvariants:\n  - name: cpu\n    when: {{vendor: cpu}}\n    runtime: container\n    image: {image}\n"
            _http(port, "POST", "/api/recipes", {"yaml": text}, cookie)
        w = type("Live", (), {})()
        w.port, w.server, w.cookie, w.tmp = port, server, cookie, tmp_path
        w.machine = join(server, _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"], name="box")
        w.fake = FakeDocker(tmp_path / "bin")
        load_into(w.fake, docker_save(img))
        w.tts = build_image(TAGGED, arches=("amd64",))
        load_into(w.fake, docker_save(w.tts))
        w.other = build_image(OTHER, arches=("amd64",))
        load_into(w.fake, docker_save(w.other, tags=[OTHER.removeprefix("docker.io/")]))

        def call(method, path, body=None):
            import http.client

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


class AgentThread:
    def __init__(self, w):
        from forgeyard.agent.backoff import Backoff
        from forgeyard.agent.client import Agent
        from forgeyard.agent.inventory import SimulatedInventory
        from forgeyard.agent.models import ModelCache
        from forgeyard.agent.workloads.supervisor import Supervisor

        rt = runtime(w.tmp, w.fake)
        sup = Supervisor(w.tmp / "agent", {"container": rt}, models=ModelCache(w.tmp / "cache"), tick=0.1,
                         port_range=(31950, 31954))
        self.agent = Agent(w.server, w.machine["credential"], SimulatedInventory("nvidia-rtx4090x2"),
                           backoff=Backoff(base=0.05, cap=0.2), supervisor=sup)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=lambda: self.loop.run_until_complete(self.agent.run()), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.agent.stop)
        self.thread.join(15)


def test_fy_eimg_008_a_real_agent_lists_and_removes_an_engine_image(live, img):
    """FY-EIMG-008 (a real agent, real HTTP and WebSocket, the fake Docker CLI): the machine page lists the two
    recipe images (the third, someone else's, only counted); someone else's image cannot be removed; a recipe image
    a container of the engine uses is refused by the agent, naming the container, and stays; the unused pinned image
    is removed (`rmi`, never -f) and gone from the listing; both asks are audited."""
    w = live
    p = img.platforms["amd64"]
    mid = w.machine["node_id"]
    with AgentThread(w):
        wait_until(lambda: "image_remove" in next(m for m in w.call("GET", "/api/machines")[1]["machines"])["features"],
                   "the agent connected", 15)
        status, body = w.call("GET", f"/api/machines/{mid}/engine-images")
        assert status == 200 and body["listed"], body
        assert sorted(i["tags"][0] for i in body["images"]) == [img.name, TAGGED]
        assert body["others"] == 1 and body["can_remove"] and body["remove_note"] is None
        status, body = w.call("POST", f"/api/machines/{mid}/engine-images/remove",
                              {"image": w.other.platforms["amd64"].config_digest})
        assert status == 409 and body["detail"]["code"] == "not_an_engine_image"
        subprocess.run([str(w.fake.path), "run", "-d", "--name", "someones-tts", TAGGED], check=True,
                       capture_output=True)
        status, body = w.call("POST", f"/api/machines/{mid}/engine-images/remove",
                              {"image": w.tts.platforms["amd64"].config_digest})
        assert status == 409 and body["detail"]["code"] == "image_in_use", body
        assert "someones-tts" in body["detail"]["message"] and body["detail"]["in_use"] == ["someones-tts"]
        status, body = w.call("POST", f"/api/machines/{mid}/engine-images/remove", {"image": p.config_digest})
        assert status == 200, body
        assert body["image"]["id"] == p.config_digest and body["freed_bytes"] > 0
        status, body = w.call("GET", f"/api/machines/{mid}/engine-images")
        assert [i["tags"][0] for i in body["images"]] == [TAGGED]
    assert ["rmi", img.name] in w.fake.calls()
    assert all(c[0] != "rmi" or ("-f" not in c and TAGGED not in c) for c in w.fake.calls())
    assert sorted(i["Id"] for i in w.fake.images()) == sorted([w.tts.platforms["amd64"].config_digest,
                                                                w.other.platforms["amd64"].config_digest])
    recs = audits()
    assert [r["ok"] for r in recs] == [False, True] and recs[1]["recipes"] == ["pinned-engine"]
    assert recs[0]["code"] == "in_use" and recs[0]["recipes"] == ["tts"]


def test_fy_eimg_009_an_agent_without_the_feature_is_never_asked(owner, img):
    """FY-EIMG-009: the machine page of an agent from 1.4.1 lists its images but says to update the agent for
    Remove, and a removal is refused before any image_list or image_remove reaches it."""
    recipe(owner, "pinned-engine", img.ref)
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws, LISTS)
        call = InThread(lambda: owner.get(f"/api/machines/{j['node_id']}/engine-images"))
        answer_list(ws, listed(img)[:1])
        body = call.result().json()
        assert body["listed"] and body["remove_note"] == ("This machine's agent is too old to remove engine images. "
                                                          "Update the agent.")
        assert body["images"][0]["remove_note"] == body["remove_note"]
        r = owner.post(f"/api/machines/{j['node_id']}/engine-images/remove",
                       json={"image": img.platforms["amd64"].config_digest})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "agent_too_old"
        with pytest.raises(Exception):
            recv_type(ws, "image_list", timeout=0.5)
    assert audits() == []
