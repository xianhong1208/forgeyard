"""The agent's side of the image relay (1.4, issue #35 PR 4, D4; FY-RELAY-027…034, 040): the manifest checked against
the recorded digests, each blob fetched through the server (with the credential and nothing else), resumed,
checked against its digest; an archive both Docker stores and Podman load (checked here against the OCI and
docker-archive layouts, a Docker-like loader that checks diff ids, and skopeo when it is installed); the #10
check on what was loaded; "auto" only after a network failure; the supervisor and Download to… taking the
relay from the desired list with progress "via the server"; and an uploaded image file loaded as it streams."""

import asyncio
import gzip
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

import pytest

from forgeyard.agent import imagerelay
from forgeyard.agent.imagerelay import ImageRelay
from forgeyard.agent.models import ModelCache
from forgeyard.agent.workloads import pull as pulling
from forgeyard.agent.workloads.container import LOADED, ContainerRuntime
from forgeyard.agent.workloads.records import RecordStore
from forgeyard.agent.workloads.runtime import StartError
from forgeyard.workloads import images
from tests.image_helpers import build_image, docker_save
from tests.pull_helpers import FakeEngineAPI
from tests.test_relay_agent import CRED, FakeRelay
from tests.waits import wait_until
from tests.workload_helpers import FakeDocker

PATH = "/agent/v1/relay/download/j1/image"


def relay_files(img, arch="amd64", base="/download/j1/image") -> dict:
    p = img.platforms[arch]
    files = {f"{base}/manifest": p.manifest, f"{base}/blobs/{p.config_digest}": p.config}
    files.update({f"{base}/blobs/{d}": img.blobs[d] for d in p.layers})
    return files


class Box:
    """An agent's pieces: a model cache with a client to a fake relay, a container runtime on a fake docker."""

    def __init__(self, tmp_path, img, files=None, mode="server"):
        self.img = img
        self.relay = FakeRelay(files if files is not None else relay_files(img))
        self.cache = ModelCache(tmp_path / "cache", allow_private=True)
        self.cache.relay_client = self.relay.client()
        self.fake = FakeDocker(tmp_path / "bin")
        self.rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(self.fake.path))
        self.through = ImageRelay(PATH, mode, self.cache.relay_client, self.cache)
        self.snaps: list[dict] = []

    def ensure(self, cancel=None, check=None, image=None):
        return self.rt.ensure_image(image or self.img.ref, check or self.img.check(), self.snaps.append, cancel,
                                    relay=self.through)

    def staging(self):
        return sorted((self.cache.root / ".partial").glob("image-*"))

    def close(self):
        self.relay.close()


@pytest.fixture()
def box(tmp_path):
    b = Box(tmp_path, build_image())
    yield b
    b.close()


def part_sizes(cache_root: Path) -> list[int]:
    """The sizes of the downloads in progress. A small blob's `.part` is renamed into place as soon as it is
    complete, so one listed a moment ago may be gone by the time it is looked at: that one is skipped. (#161: the
    FileNotFoundError it raised ended the thread that was to cancel the pull, which then stalled on every retry
    and failed with "timed out".)"""
    sizes = []
    for f in (cache_root / ".partial").rglob("*.part"):
        try:
            sizes.append(f.stat().st_size)
        except FileNotFoundError:
            pass
    return sizes


def members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {m.name: (tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()}


# ------------------------------------------------------------------ through the server
def test_fy_relay_027_server_mode_fetches_checks_loads_tags_and_runs_by_the_recorded_ids(box):
    """FY-RELAY-027: set to "server", nothing is pulled: the manifest, the config and every layer come from the
    agent's own server with its credential and nothing else; the archive is loaded, the recipe's tag is on it,
    the #10 check recognises it (LOADED, by the config digest docker shows), the staging directory is gone, and
    the progress said "via the server", layer by layer, never above the total, then "loading it"."""
    target, found = box.ensure()
    p = box.img.platforms["amd64"]
    assert (target, found) == (p.config_digest, LOADED)
    assert not box.fake.pulls() and len(box.fake.loads()) == 1
    paths = [r["path"] for r in box.relay.requests]
    assert paths == [f"{PATH}/manifest", f"{PATH}/blobs/{p.config_digest}", *[f"{PATH}/blobs/{d}" for d in p.layers]]
    for r in box.relay.requests:
        assert r["authorization"] == f"Bearer {CRED}"
        assert set(r) <= {"path", "host", "authorization", "user-agent", "accept-encoding", "range"}
    [loaded] = box.fake.images()
    assert loaded["Id"] == p.config_digest and loaded["RepoTags"] == ["ghcr.io/acme/engine:1.0"]
    assert images.matches(loaded, box.img.check()) == "id"
    assert box.staging() == []
    total = box.img.total()
    assert all(s["via"] == "server" and s["done"] <= s["total"] == total for s in box.snaps)
    assert box.snaps[-1]["unpacking"] and box.snaps[-1]["layers_done"] == 3
    layers = [s for s in box.snaps if not s.get("unpacking")]
    assert [s["layers_done"] for s in layers] == sorted(s["layers_done"] for s in layers)
    assert pulling.describe({**layers[-1], "layer": 3}).startswith("pulling the image via the server: 0.00 of 0.00 GiB")
    assert pulling.describe({"via": "server", "done": 2 * 1024**3, "total": 4 * 1024**3, "layers": 7, "layers_done": 2,
                             "layer": 3}) == "pulling the image via the server: 2.00 of 4.00 GiB (50%), layer 3 of 7"
    assert pulling.describe({"via": "server", "unpacking": True, "layers": 7}) == \
        "pulling the image via the server: loading it, 7 layers"
    # An engine that loads it untagged (only "Loaded image ID: …"): the agent gives it the recipe's tag.
    b2 = Box(box.cache.root.parent / "untagged", box.img)
    try:
        b2.fake.untagged_loads()
        assert b2.ensure()[1] == LOADED
        assert b2.fake.images()[0]["RepoTags"] == ["ghcr.io/acme/engine:1.0"]
        assert ["tag", p.config_digest, "ghcr.io/acme/engine:1.0"] in b2.fake.calls()
    finally:
        b2.close()
    # Loaded now: the next time nothing is fetched.
    box.relay.requests.clear()
    assert box.ensure()[1] == LOADED and box.relay.requests == []


def test_fy_relay_028_the_archive_is_an_oci_layout_and_a_classic_docker_archive(box, tmp_path):
    """FY-RELAY-028: what is streamed into `load`: `oci-layout`, `index.json` naming the registry's manifest
    (digest, size, media type, the full name and tag), `manifest.json` naming the config and the layers in
    order with the recipe's tag, and each blob once under blobs/sha256/<hex>, hashing to its name, the manifest
    with the registry's exact bytes. The Docker-like loader unpacked each (compressed) layer and checked it
    against its diff id. skopeo (containers/image, what Podman uses) reads both layouts when installed."""
    box.ensure()
    [archive] = box.fake.loads()
    m = members(archive)
    p = box.img.platforms["amd64"]
    blobs = {p.manifest_digest, p.config_digest, *p.layers}
    assert set(m) == {"oci-layout", "blobs", "blobs/sha256", "index.json", "manifest.json",
                      *[f"blobs/sha256/{d[7:]}" for d in blobs]}
    assert json.loads(m["oci-layout"]) == {"imageLayoutVersion": "1.0.0"}
    for d in blobs:
        assert hashlib.sha256(m[f"blobs/sha256/{d[7:]}"]).hexdigest() == d[7:]
    assert m[f"blobs/sha256/{p.manifest_digest[7:]}"] == p.manifest
    index = json.loads(m["index.json"])
    assert index["mediaType"] == "application/vnd.oci.image.index.v1+json" and index["schemaVersion"] == 2
    [desc] = index["manifests"]
    assert desc == {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": p.manifest_digest,
                    "size": len(p.manifest), "annotations": {"io.containerd.image.name": "ghcr.io/acme/engine:1.0",
                                                             "org.opencontainers.image.ref.name": "1.0"}}
    assert json.loads(m["manifest.json"]) == [{"Config": f"blobs/sha256/{p.config_digest[7:]}",
                                               "RepoTags": ["ghcr.io/acme/engine:1.0"],
                                               "Layers": [f"blobs/sha256/{d[7:]}" for d in p.layers]}]
    assert imagerelay.full_name("vllm/vllm-openai:v0.32.0@sha256:" + "a" * 64) == "docker.io/vllm/vllm-openai:v0.32.0"
    assert imagerelay.full_name("python:3.13") == "docker.io/library/python:3.13"
    if shutil.which("skopeo") is None:
        return
    path = tmp_path / "archive.tar"
    path.write_bytes(archive)
    oci = json.loads(subprocess.run(["skopeo", "inspect", f"oci-archive:{path}"], capture_output=True, check=True,
                                    timeout=60).stdout)
    assert oci["Digest"] == p.manifest_digest and oci["Architecture"] == "amd64"
    classic = json.loads(subprocess.run(["skopeo", "inspect", f"docker-archive:{path}"], capture_output=True,
                                        check=True, timeout=60).stdout)
    assert classic["Architecture"] == "amd64" and len(classic["Layers"]) == 3
    out = tmp_path / "copied"
    subprocess.run(["skopeo", "copy", f"oci-archive:{path}", f"dir:{out}"], capture_output=True, check=True, timeout=60)
    assert (out / "manifest.json").read_bytes() == p.manifest


def test_fy_relay_029_auto_relays_only_after_the_registry_could_not_be_reached(tmp_path):
    """FY-RELAY-029: "auto" pulls first; a pull that could not reach the registry (DNS) switches to the server,
    and the registry is remembered, so the next image from it goes through the server at once. A pull that
    failed for another reason (access denied) is not relayed."""
    img = build_image()
    b = Box(tmp_path, img, mode="auto")
    try:
        b.fake.offline()
        assert b.ensure()[1] == LOADED
        assert len(b.fake.pulls()) == 1 and b.relay.requests
        assert b.through.client.unreachable("ghcr.io")
        # The registry is remembered: another image from it goes through the server without a pull first.
        second = build_image("ghcr.io/acme/other:2.0")
        assert ImageRelay(PATH, "auto", b.through.client, b.cache).first(second.ref)
        assert not ImageRelay(PATH, "auto", b.through.client, b.cache).first("quay.io/acme/x:1@sha256:" + "0" * 64)
    finally:
        b.close()
    tagged = Box(tmp_path / "tagged", img, mode="auto")
    try:
        tagged.fake.offline()
        with pytest.raises(StartError) as e:
            tagged.ensure(image="ghcr.io/acme/engine:1.0", check=None)
        assert "not pinned by digest" in str(e.value) and "Upload an image" in str(e.value)
        assert tagged.relay.requests == []
    finally:
        tagged.close()
    denied = build_image("fail/engine:1.0")
    b3 = Box(tmp_path / "three", denied, mode="auto")
    try:
        with pytest.raises(StartError) as e:
            b3.ensure()
        assert "pull access denied" in str(e.value) and b3.relay.requests == []
        assert not b3.through.client.unreachable("docker.io")
    finally:
        b3.close()


def docker29_unreachable(ref: str) -> str:
    """What Docker 29 (containerd image store) answers to a pull of a pinned image when the registry's name does
    not resolve, as a DNS-filtered machine sees it: the reference twice, each with its 64-digit digest, before the
    cause. Well over 300 characters."""
    name, _, digest = ref.partition("@")
    repo = name.rsplit(":", 1)[0]
    host, _, path = repo.partition("/")
    return (f'failed to resolve reference "{repo}@{digest}": failed to do request: Head '
            f'"https://{host}/v2/{path}/manifests/{digest}": dial tcp: lookup {host} on 127.0.0.53:53: no such host')


def test_fy_relay_041_a_long_engine_api_error_still_reads_as_unreachable(tmp_path):
    """FY-RELAY-041: on a real machine (Docker 29, pulls through the engine API) a pinned image whose registry does
    not resolve fails with a message whose cause comes after the reference twice over, past 300 characters.
    "auto" still sees a network failure and switches to the server; and where the image cannot come through the
    server (a tag-only one), the reason the console shows keeps the cause ("no such host") at its end."""
    img = build_image()
    message = docker29_unreachable(img.ref)
    assert len(message) > 300 and message.index("no such host") > 300
    sock = Path(tempfile.mkdtemp(prefix="fyd-", dir="/tmp"))
    api = FakeEngineAPI(sock / "d.sock", [("status", 500, message)])
    b = Box(tmp_path, img, mode="auto")
    try:
        b.rt.socket = str(api.path)
        assert b.ensure()[1] == LOADED
        assert len(api.requests) == 1 and b.relay.requests
        assert b.through.client.unreachable("ghcr.io")
    finally:
        b.close()
        api.close()
    tagged = Box(tmp_path / "tagged", img, mode="auto")
    api2 = FakeEngineAPI(sock / "e.sock", [("status", 500, docker29_unreachable(img.ref))])
    try:
        tagged.rt.socket = str(api2.path)
        with pytest.raises(StartError) as e:
            tagged.ensure(image="ghcr.io/acme/engine:1.0", check=None)
        reason = str(e.value)
        assert len(reason) <= 300 and "no such host" in reason and "Upload an image" in reason
    finally:
        tagged.close()
        api2.close()
        shutil.rmtree(sock, ignore_errors=True)


def test_fy_relay_030_what_does_not_match_is_never_loaded_or_run(tmp_path, monkeypatch):
    """FY-RELAY-030: refused, with nothing loaded: a manifest that is not a recorded digest; a layer whose bytes
    do not hash to its digest (its partial file deleted); a config whose layers and settings are not the
    recorded ones; the image of another architecture. And an image the engine shows differently once loaded
    (its ids do not match) is never run."""
    img = build_image()
    p = img.platforms["amd64"]

    def refused(name, files=None, check=None, needle=""):
        b = Box(tmp_path / name, img, files=files)
        try:
            with pytest.raises(StartError) as e:
                b.ensure(check=check)
            assert needle in str(e.value), str(e.value)
            assert b.fake.images() == [] or name == "altered"
            return b
        finally:
            b.close()

    files = relay_files(img)
    refused("manifest", {**files, "/download/j1/image/manifest": p.manifest + b" "}, needle="not one of the image's "
            "recorded digests")
    bad = bytearray(img.blobs[p.layers[1]])
    bad[100] ^= 0xFF
    b = refused("layer", {**files, f"/download/j1/image/blobs/{p.layers[1]}": bytes(bad)}, needle="SHA-256 mismatch")
    assert not list((b.cache.root / ".partial").rglob("*.part")) and b.fake.loads() == []
    other = {"ids": img.check()["ids"], "content": ["sha256:" + "1" * 64]}
    refused("content", check=other, needle="not the recorded ones")
    b0 = Box(tmp_path / "noids", img)
    try:
        with pytest.raises(imagerelay.ImageRelayError, match="no recorded ids"):
            imagerelay.fetch(b0.through, img.ref, None)
        assert b0.relay.requests == []
    finally:
        b0.close()
    no_config = {"ids": [d for d in img.check()["ids"] if d != p.config_digest], "content": img.check()["content"]}
    refused("config", check=no_config, needle="names a config that is not the recorded one")
    monkeypatch.setattr(imagerelay, "machine_arch", lambda: "arm64")
    refused("arch", needle="this machine is arm64")
    monkeypatch.undo()
    b = Box(tmp_path / "altered", img)
    try:
        b.fake.alter_loads()
        with pytest.raises(StartError) as e:
            b.ensure()
        assert "does not match the recorded ids" in str(e.value)
    finally:
        b.close()


def test_fy_relay_031_a_cut_layer_resumes_from_the_bytes_on_disk(box):
    """FY-RELAY-031: a transfer cut in the middle of the large layer resumes from the bytes on disk (Range), and
    the loaded image is the whole one."""
    p = box.img.platforms["amd64"]
    big = p.layers[1]
    box.relay.cut_after = 100_000
    assert box.ensure()[1] == LOADED
    asked = [r for r in box.relay.requests if r["path"].endswith(big)]
    assert [r.get("range") for r in asked] == [None, "bytes=100000-"]


def test_fy_relay_040_a_cancel_keeps_the_staging_and_the_next_try_resumes(tmp_path):
    """FY-RELAY-040: cancelled while the large layer stalls, the pull ends at once ("cancelled"),
    the config and first layer stay staged and count as partial downloads; the next try asks only for the rest,
    from where the large layer stopped."""
    img = build_image()
    b = Box(tmp_path, img)
    try:
        p = img.platforms["amd64"]
        big = p.layers[1]
        b.relay.stall_after = 50_000
        cancel = threading.Event()
        failed: list[BaseException] = []

        def cancel_once_stalled():  # a condition, not a fixed time: the large layer has started and stalled
            try:
                wait_until(lambda: any(n >= 50_000 for n in part_sizes(b.cache.root)),
                           "the large layer's first bytes on disk", 30)
            except BaseException as e:  # (#161) never leave the pull stalled: cancel it, then fail with this
                failed.append(e)
            finally:
                cancel.set()

        threading.Thread(target=cancel_once_stalled, daemon=True).start()
        with pytest.raises(pulling.PullCancelled):
            b.ensure(cancel=cancel)
        assert not failed, failed
        [staged] = b.staging()
        assert (staged / p.config_digest[7:]).exists() and (staged / p.layers[0][7:]).exists()
        assert b.cache.detail()["partial_bytes"] > 0
        b.relay.stall_after = None
        b.relay.requests.clear()
        assert b.ensure()[1] == LOADED
        asked = [r["path"].rsplit("/", 1)[-1] for r in b.relay.requests]
        assert p.config_digest not in asked and p.layers[0] not in asked and big in asked
        assert next(r for r in b.relay.requests if r["path"].endswith(big)).get("range", "").startswith("bytes=")
        assert b.staging() == []
    finally:
        b.close()


# ------------------------------------------------------------------ the supervisor and Download to…
def test_fy_relay_032_desired_image_relays_are_used_for_downloads_and_replicas(tmp_path):
    """FY-RELAY-032: a download whose image comes with `image_relay` is fetched through the server and reported
    "via" the server; a replica with `image_relay` beside its spec gets its image the same way and runs it by the
    id that matched; the relay never ends up in the replica's record."""
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.test_prefetch_agent import want
    from tests.workload_helpers import container, converge, desired, replica

    img = build_image()
    files = {**relay_files(img), **relay_files(img, base="/replica/feedface01234567/image")}
    b = Box(tmp_path, img, files=files)
    try:
        async def scenario():
            sup = Supervisor(tmp_path / "state", {"container": b.rt}, models=b.cache, tick=0.05,
                             port_range=(32150, 32160))
            sup.server_features = {"workloads", "models", "prefetch", "pull_progress"}
            seen, replica_seen = [], []
            report = sup.prefetch._pull_progress
            sup.prefetch._pull_progress = lambda job, snap: (report(job, snap), seen.append(dict(job.download or {})))[0]
            replica_report = sup._pull_progress
            sup._pull_progress = lambda rid, snap: (replica_report(rid, snap),
                                                    replica_seen.append(dict(sup.records[rid].download or {})))[0]
            want(sup, {"id": "j1", "image": img.ref, "image_ids": img.check(),
                       "image_relay": {"path": PATH, "mode": "server"}})
            await converge(sup, lambda s: s.prefetch.jobs.get("j1") and s.prefetch.jobs["j1"].state in ("done", "failed"))
            job = sup.prefetch.jobs["j1"]
            assert job.state == "done", job.reason
            assert seen and all(x.get("via") == "server" for x in seen), seen
            # The replica's image is not here any more and the registry cannot be reached: only the relay helps.
            b.fake._update(lambda data: data.update(images=[]))
            b.fake.offline()
            b.relay.requests.clear()
            rep = replica("feedface01234567", container(image=img.ref))
            sup.set_desired(desired({**rep, "image_ids": img.check(),
                                     "image_relay": {"path": "/agent/v1/relay/replica/feedface01234567/image",
                                                     "mode": "server"}}))
            await converge(sup, lambda s: "feedface01234567" in s.records
                           and s.records["feedface01234567"].state in ("running", "backoff", "failed"))
            rec = sup.records["feedface01234567"]
            assert rec.state == "running", rec.reason
            assert any(r["path"].startswith("/agent/v1/relay/replica/feedface01234567/image") for r in b.relay.requests)
            assert replica_seen and all(x.get("via") == "server" for x in replica_seen if x), replica_seen
            return sup

        asyncio.run(scenario())
        [run] = b.fake.runs()
        assert img.platforms["amd64"].config_digest in run
        stored = RecordStore(tmp_path / "state").load_all()["feedface01234567"]
        assert "image_relay" not in json.dumps(stored.spec)
    finally:
        b.close()


def test_fy_relay_033_the_agent_says_what_it_can(tmp_path, monkeypatch):
    """FY-RELAY-033: an agent with a model cache and a working container engine lists "relay_images" and
    "image_load"; without a container engine neither; without a model cache only "image_load"."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.workloads.supervisor import Supervisor

    monkeypatch.setenv(SIMULATION_ENV, "1")
    fake = FakeDocker(tmp_path / "bin")
    rt = ContainerRuntime(RecordStore(tmp_path / "s1"), agent_id="a", engine=str(fake.path))
    none = ContainerRuntime(RecordStore(tmp_path / "s2"), agent_id="a", engine="none")
    inv = SimulatedInventory("nvidia-rtx4090x2")

    def features(runtimes, models):
        sup = Supervisor(tmp_path / "state", runtimes, models=models, tick=0.05, port_range=(32170, 32180))
        agent = Agent("http://127.0.0.1:9", CRED, inv, supervisor=sup)
        return agent.hello(inv.full())["features"]

    full = features({"container": rt}, ModelCache(tmp_path / "c1"))
    assert "relay" in full and "relay_images" in full and "image_load" in full
    off = features({"container": none}, ModelCache(tmp_path / "c2"))
    assert "relay" in off and "relay_images" not in off and "image_load" not in off
    nocache = features({"container": rt}, None)
    assert "relay_images" not in nocache and "image_load" in nocache
    assert len(full) <= 16
    # Only relay paths of the expected forms are taken from the server.
    from forgeyard.agent.relay import parse_image

    assert parse_image({"image_relay": {"path": PATH, "mode": "server"}}) == {"path": PATH, "mode": "server"}
    for bad in ("/etc/passwd", "https://evil.example/agent/v1/relay/download/j1/image", PATH + "/../x",
                "/agent/v1/relay/download/j1/model"):
        assert parse_image({"image_relay": {"path": bad, "mode": "server"}}) is None, bad
    assert parse_image({"image_relay": {"path": PATH, "mode": "sideways"}}) is None
    sup = Supervisor(tmp_path / "state3", {"container": rt}, models=ModelCache(tmp_path / "c3"), tick=0.05,
                     port_range=(32170, 32180))
    agent = Agent("http://127.0.0.1:9", CRED, inv, supervisor=sup)
    sent = []

    class WS:
        async def send(self, text):
            sent.append(json.loads(text))

    asyncio.run(agent._answer_image_load(WS(), {"request_id": "r1", "path": "/agent/v1/relay/download/j1/model",
                                                 "size": 10}))
    assert sent == [{"v": 1, "type": "image_loaded", "request_id": "r1", "ok": False,
                     "reason": "this agent cannot load images (no container engine), or the path is wrong"}]


# ------------------------------------------------------------------ an uploaded image file
def test_fy_relay_034_an_uploaded_file_is_loaded_as_it_streams_and_a_short_one_never(tmp_path):
    """FY-RELAY-034: a `docker save` file (gzip) is read from the server's upload path and loaded: the agent says
    each image's id, tags and content id (the recorded one for this image); a stream that ends early is never
    loaded (the engine is stopped before its input ends)."""
    img = build_image()
    saved = docker_save(img, compress=True)
    upload = "/agent/v1/image-upload/" + "t" * 32
    b = Box(tmp_path, img, files={upload: saved})
    try:
        found = b.rt.load_upload(b.cache.relay_client.open(upload), len(saved))
        p = img.platforms["amd64"]
        assert found == [{"id": p.config_digest, "tags": ["ghcr.io/acme/engine:1.0"], "content": p.content,
                          "os": "linux", "architecture": "amd64"}]
        assert b.fake.loads()[0] == saved
        # Podman shows the id as bare hex: reported as a digest all the same (the server compares it).
        shown = b.rt._image
        b.rt._image = lambda ref: (lambda i: {**i, "Id": i["Id"][7:]} if i else i)(shown(ref))
        assert b.rt.load_upload(b.cache.relay_client.open(upload), len(saved))[0]["id"] == p.config_digest
        b.rt._image = shown
        b.fake._update(lambda data: data.update(images=[]))
        with pytest.raises(StartError) as e:
            b.rt.load_upload(b.cache.relay_client.open(upload), len(saved) + 10)
        assert "nothing was loaded" in str(e.value) and b.fake.images() == []
    finally:
        b.close()
    assert gzip.decompress(saved)[:100]  # a real gzip stream
