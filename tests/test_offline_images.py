"""Engine images loaded with `docker load` on a machine that cannot reach the registry: the recorded
ids (forgeyard/recipes/images.lock.json, scripts/pin_images.py), the agent's choice of what to run,
and the protocol that carries the ids (FY-OFF-*).

The registry data in tests/fixtures/images/ is real: ghcr.io's index, manifests and configs of
ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242, fetched anonymously on 2026-11-12, and the manifest
behind the Id a loaded copy of that image showed on a real offline machine (Docker's containerd image
store): sha256:cb7d70b2…, which is in no registry."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from forgeyard.agent.workloads.container import (ABSENT, LOADED, MISMATCH, PINNED, UNCHECKED, ContainerRuntime,
                                                 choose_image, pull_failed_reason)
from forgeyard.agent.workloads.records import RecordStore
from forgeyard.agent.workloads.supervisor import Supervisor
from forgeyard.workloads import images
from forgeyard.workloads.spec import WorkloadSpec
from tests.workload_helpers import FakeDocker, container, converge, desired, replica

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "images"
REPO = "ghcr.io/ggml-org/llama.cpp"
TAG = f"{REPO}:server-vulkan-b12242"
INDEX = "sha256:28a56d03c5cc4abeb16b2a5422c5286d9e48020a3cb3416434bff5fbf847bfa6"
VULKAN = f"{TAG}@{INDEX}"
MANIFEST_AMD64 = "sha256:6aa95eba499ed44085cb614140243b0965431d83164b10444dec88c677f9f857"
MANIFEST_ARM64 = "sha256:431e8e0a09f4109909927f7e649cdd85099631406711f6d0513f0317858f8011"
CONFIG_AMD64 = "sha256:977c1b42052a5ed559fff6f72b1649091290ed8810d28153c73f051c60532597"
CONFIG_ARM64 = "sha256:95d89570949fa16f3f5e3b270719c7b9fef2d8aa06d2400ad6919b8973667a61"
# What `docker images --digests` showed for the loaded copy on the offline machine.
LOADED_ID = "sha256:cb7d70b298bb580d44cd96d7f229b2ea38185de5a299af51098d52bfc4981aca"


def raw(name: str) -> bytes:
    return (FIX / f"llama-cpp-vulkan-b12242-{name}.json").read_bytes()


def config(arch: str = "amd64") -> dict:
    return json.loads(raw(f"config-{arch}"))


def inspected(image_id: str, *, arch: str = "amd64", tags=(TAG,), digests=(), **config_over) -> dict:
    """An `image inspect` element as Docker or Podman prints it, built from a real config."""
    cfg = config(arch)
    return {"Id": image_id, "RepoTags": list(tags), "RepoDigests": list(digests), "Os": cfg["os"],
            "Architecture": cfg["architecture"], "RootFS": {"Type": "layers", "Layers": cfg["rootfs"]["diff_ids"]},
            "Config": {**cfg["config"], **config_over}}


def check() -> dict:
    found = images.check_for(VULKAN)
    assert found is not None
    return found


# ------------------------------------------------------------------ the recorded ids
def test_fy_off_001_the_committed_ids_are_the_registrys(monkeypatch):
    """FY-OFF-001: pin_images reads the pinned index, each platform's manifest and config from the
    registry, checks each against its digest, and its lock entry for the Vulkan image is the one
    committed in forgeyard/recipes/images.lock.json (index, 2 manifests, 2 configs, 2 content ids)."""
    pin = _pin_module()
    client = httpx.Client(transport=httpx.MockTransport(_registry()))
    entry = pin.describe(pin.Registry(client), VULKAN)
    assert entry == {"index": INDEX, "platforms": {
        "linux/amd64": {"manifest": MANIFEST_AMD64, "config": CONFIG_AMD64,
                        "content": images.content_of_config(config("amd64"))},
        "linux/arm64": {"manifest": MANIFEST_ARM64, "config": CONFIG_ARM64,
                        "content": images.content_of_config(config("arm64"))}}}
    assert images.load_lock()[VULKAN] == entry
    assert check()["ids"] == sorted([INDEX, MANIFEST_AMD64, MANIFEST_ARM64, CONFIG_AMD64, CONFIG_ARM64])
    # A registry that serves other bytes than the digest asked for is refused.
    bad = httpx.Client(transport=httpx.MockTransport(_registry(tamper=CONFIG_AMD64)))
    with pytest.raises(RuntimeError, match="hash to"):
        pin.describe(pin.Registry(bad), VULKAN)


def test_fy_off_002_the_offline_machines_loaded_copy_is_recognised_by_content():
    """FY-OFF-002: the real data point. On an offline machine with Docker's containerd store, the
    image loaded from a `docker save` archive showed Id sha256:cb7d70b2…. It is the digest of the
    Docker schema-2 manifest that containerd's importer wrote on load: the archive's uncompressed
    layers, except the empty layer, swapped for the 32-byte gzip blob already in that store. So no
    registry digest can predict it. Its config is the registry's amd64 config and its layers are that
    config's diff ids, so the agent recognises it by content."""
    body = raw("loaded-manifest")
    assert "sha256:" + hashlib.sha256(body).hexdigest() == LOADED_ID
    manifest = json.loads(body)
    assert manifest["config"]["digest"] == CONFIG_AMD64 and CONFIG_AMD64 in check()["ids"]
    uncompressed = [layer["digest"] for layer in manifest["layers"] if layer["mediaType"].endswith(".tar")]
    assert uncompressed == config("amd64")["rootfs"]["diff_ids"][:5]
    assert manifest["layers"][5]["digest"] == json.loads(raw("manifest-amd64"))["layers"][5]["digest"]
    assert LOADED_ID not in check()["ids"]
    item = inspected(LOADED_ID, digests=[f"{REPO}@{LOADED_ID}"])
    assert images.matches(item, check()) == "content"
    assert choose_image(VULKAN, check(), _store([item])) == (LOADED_ID, LOADED)


def test_fy_off_003_what_counts_as_the_same_image():
    """FY-OFF-003: a copy matches by any recorded digest (classic store: the config digest; containerd
    store after a pull: the index; Podman: a bare config id), or by content: the layers, platform,
    entrypoint, command, environment, user, working directory and stop signal. Another build, another
    entrypoint or extra environment over the same layers, or a missing layer, does not match."""
    c = check()
    assert images.matches(inspected(CONFIG_AMD64), c) == "id"                      # classic docker load
    assert images.matches(inspected(INDEX, digests=[f"{REPO}@{INDEX}"]), c) == "id"  # containerd, pulled
    assert images.matches(inspected(CONFIG_AMD64.removeprefix("sha256:")), c) == "id"  # Podman
    assert images.matches(inspected(CONFIG_ARM64, arch="arm64"), c) == "id"
    # Only a repo digest is recorded (Podman lists the manifest digest there), and the content differs.
    assert images.matches(inspected("sha256:" + "12" * 32, digests=[f"{REPO}@{MANIFEST_AMD64}"], User="x"), c) == "id"
    other = "sha256:" + "ab" * 32
    assert images.matches(inspected(other), c) == "content"
    assert images.matches(inspected(other, Entrypoint=["/bin/sh"]), c) is None
    assert images.matches(inspected(other, Env=config()["config"]["Env"] + ["LD_PRELOAD=/x.so"]), c) is None
    assert images.matches(inspected(other, User="root"), c) is None
    assert images.matches(inspected(other, WorkingDir="/"), c) is None
    assert images.matches(inspected(other, StopSignal="SIGKILL"), c) is None
    assert images.matches(inspected(other, Cmd=["--help"]), c) is None
    fewer = inspected(other)
    fewer["RootFS"]["Layers"] = fewer["RootFS"]["Layers"][:-1]
    assert images.matches(fewer, c) is None
    arm_layers = inspected(other)
    arm_layers["Architecture"] = "arm64"  # amd64 layers claimed as arm64
    assert images.matches(arm_layers, c) is None
    # Null, empty and absent are the same; the registry's Cmd is absent, an inspect may print null.
    assert images.matches(inspected(other, Cmd=None, User=None), c) == "content"
    assert images.matches({"Id": other}, c) is None
    assert images.check_for(TAG) is None and images.check_for("python:3.13-slim") is None


def test_fy_off_004_ids_from_the_wire_are_digests_only():
    """FY-OFF-004: the agent takes digests only (a bare 64-hex id gets its prefix), at most 64; any
    other value drops the whole check, so nothing runs in place of the pinned image."""
    good = {"ids": [INDEX, CONFIG_AMD64.removeprefix("sha256:")], "content": [CONFIG_ARM64]}
    assert images.parse_check(good) == {"ids": sorted([INDEX, CONFIG_AMD64]), "content": [CONFIG_ARM64]}
    for bad in (None, [], "x", {"ids": ["sha256:short"]}, {"ids": [INDEX, 5]}, {"ids": "sha256:" + "a" * 64},
                {"ids": [], "content": []}, {"ids": [INDEX] * 65}):
        assert images.parse_check(bad) is None, bad


# ------------------------------------------------------------------ the agent's choice
def test_fy_off_005_choose_image_each_branch():
    """FY-OFF-005: the decision table. Pinned digest local: run as pinned. Tag local and matching:
    run by its local Id. Tag local, not matching: never run it, go to the registry. Tag local with no
    recorded ids: go to the registry. Nothing local: go to the registry. Not pinned by digest: as
    before, with no lookup at all."""
    c = check()
    pulled = inspected(INDEX, digests=[f"{REPO}@{INDEX}"])
    assert choose_image(VULKAN, c, _store([pulled])) == (VULKAN, PINNED)
    assert choose_image(VULKAN, c, _store([inspected(CONFIG_AMD64)])) == (CONFIG_AMD64, LOADED)
    stranger = inspected("sha256:" + "cd" * 32, Entrypoint=["/bin/sh"])
    assert choose_image(VULKAN, c, _store([stranger])) == (VULKAN, MISMATCH)
    assert choose_image(VULKAN, None, _store([inspected(CONFIG_AMD64)])) == (VULKAN, UNCHECKED)
    assert choose_image(VULKAN, c, _store([])) == (VULKAN, ABSENT)
    asked: list[str] = []
    assert choose_image("python:3.13-slim", c, lambda ref: asked.append(ref)) == ("python:3.13-slim", PINNED)
    assert asked == []
    no_id = inspected(CONFIG_AMD64)
    no_id["Id"] = ""
    assert choose_image(VULKAN, {"ids": [], "content": c["content"]}, _store([no_id])) == (VULKAN, MISMATCH)


def test_fy_off_006_offline_a_loaded_copy_runs_by_its_id(tmp_path):
    """FY-OFF-006: offline, the pinned digest is not local but a loaded copy is (classic store: tag,
    no repo digest, Id = config digest). The replica runs that image by its Id, with no pull."""
    fake = FakeDocker(tmp_path / "bin")
    fake.offline()
    fake.load_image(inspected(CONFIG_AMD64))
    rec = _start(tmp_path, fake, check())
    assert rec.state == "running", rec.reason
    [run] = fake.runs()
    assert CONFIG_AMD64 in run and VULKAN not in run
    assert not [c for c in fake.calls() if c[0] == "pull"]


def test_fy_off_007_offline_nothing_loaded_says_how_to_load_it(tmp_path):
    """FY-OFF-007: offline, nothing local: the pull fails and the reason says the image is not on this
    machine, could not be pulled, and how to load it."""
    fake = FakeDocker(tmp_path / "bin")
    fake.offline()
    rec = _start(tmp_path, fake, check())
    assert rec.state in ("backoff", "failed")
    assert rec.reason.startswith("the image is not on this machine and could not be pulled; on an offline "
                                 "machine, docker load the image (Forgeyard checks it by its recorded ids)")
    assert "no such host" in rec.reason
    assert fake.runs() and all(VULKAN in r for r in fake.runs())


def test_fy_off_008_a_different_image_under_the_tag_is_never_run(tmp_path):
    """FY-OFF-008: offline, another image is loaded under the tag. The agent does not run it: it asks
    for the pinned digest, which fails, and the reason says a different image is loaded there."""
    fake = FakeDocker(tmp_path / "bin")
    fake.offline()
    stranger_id = "sha256:" + "ef" * 32
    fake.load_image(inspected(stranger_id, Entrypoint=["/bin/sh", "-c", "evil"]))
    rec = _start(tmp_path, fake, check())
    assert rec.state in ("backoff", "failed")
    assert rec.reason.startswith(f"a different image is loaded under {TAG} (its ids do not match the recorded "
                                 "ones), so it is not run")
    assert all(stranger_id not in r for r in fake.runs()) and all(VULKAN in r for r in fake.runs())


def test_fy_off_009_online_a_mismatched_tag_still_gets_the_pinned_image(tmp_path):
    """FY-OFF-009: with a registry, a stale or foreign image under the tag changes nothing: `run` gets
    the pinned reference (Docker pulls the digest) and the replica runs."""
    fake = FakeDocker(tmp_path / "bin")
    fake.load_image(inspected("sha256:" + "ef" * 32, Entrypoint=["/bin/sh"]))
    rec = _start(tmp_path, fake, check())
    assert rec.state == "running"
    assert [VULKAN in r for r in fake.runs()] == [True]


def test_fy_off_010_without_recorded_ids_a_loaded_tag_is_not_trusted(tmp_path):
    """FY-OFF-010: a pinned image with no recorded ids (a custom recipe's own image, or a server that
    sends none) is never swapped for a loaded tag; offline, the reason says why and what to do."""
    fake = FakeDocker(tmp_path / "bin")
    fake.offline()
    fake.load_image(inspected(CONFIG_AMD64))
    rec = _start(tmp_path, fake, None)
    assert rec.state in ("backoff", "failed")
    assert rec.reason.startswith(f"{TAG} is loaded but Forgeyard has no recorded ids for this image")
    assert "name the image by tag only in a custom recipe" in rec.reason
    assert all(CONFIG_AMD64 not in r for r in fake.runs())


def test_fy_off_011_podman_and_a_start_failure_after_a_good_pull(tmp_path):
    """FY-OFF-011: Podman prints a bare Id; the agent runs it as printed. A `run` that fails although
    the pinned image is local keeps the engine's own error (no load advice)."""
    fake = FakeDocker(tmp_path / "bin")
    fake.offline()
    bare = CONFIG_AMD64.removeprefix("sha256:")
    fake.load_image(inspected(bare, tags=[TAG], digests=[f"{REPO}@{MANIFEST_AMD64}"]))
    rec = _start(tmp_path, fake, check())
    assert rec.state == "running" and bare in fake.runs()[0]
    assert pull_failed_reason("podman", VULKAN, ABSENT, "x").startswith(
        "the image is not on this machine and could not be pulled; on an offline machine, podman load")
    fake2 = FakeDocker(tmp_path / "bin2")
    fake2.load_image({"Id": "sha256:" + "11" * 32, "RepoTags": ["fail/x:1"], "RepoDigests": ["fail/x@" + INDEX]})
    rec2 = _start(tmp_path / "two", fake2, check(), image="fail/x:1@" + INDEX)
    assert rec2.reason.startswith("docker run failed: Error: pull access denied")


# ------------------------------------------------------------------ the protocol
def test_fy_off_012_only_agents_that_list_image_ids_get_them(owner):
    """FY-OFF-012: the server sends the ids beside the spec, never in it, and only to an agent that
    lists "image_ids". An older agent gets exactly what it got before, and its spec still passes the
    agent's own validation (extra="forbid"); so does the new agent's, which is the same spec."""
    from forgeyard.db import new_session
    from forgeyard.workloads import state as wstate
    from tests.test_scheduler import MODELS, deploy, seed_catalog
    from tests.workload_helpers import make_node

    seed_catalog()
    today = MODELS + ("replica_keys", "replica_guard")
    new = make_node("apu-new", "amd-gfx1151-apu", features=today + ("image_ids",))
    old = make_node("apu-old", "amd-gfx1151-apu", features=today)
    m2 = make_node("apu-m2", "amd-gfx1151-apu", features=("workloads",))
    for name, node in (("on-new", new), ("on-old", old)):
        deploy(owner, name, "qwen3-1.7b-gguf", "llama-cpp", machine_id=node)
    db = new_session()
    try:
        [on_new] = wstate.desired_message(db, new, 1)["replicas"]
        [on_old] = wstate.desired_message(db, old, 1)["replicas"]
        assert wstate.desired_message(db, m2, 1)["replicas"] == []
    finally:
        db.close()
    assert on_new["spec"]["runtime"]["image"] == VULKAN
    assert on_new["image_ids"] == check() and "image_ids" not in json.dumps(on_new["spec"])
    assert "image_ids" not in on_old
    assert set(on_new) - set(on_old) == {"image_ids"}
    for entry in (on_new, on_old):
        WorkloadSpec.model_validate(entry["spec"])
    # The same spec, but for the deployment's own name (its workload name and served alias).
    assert json.dumps(on_new["spec"]).replace("on-new", "X") == json.dumps(on_old["spec"]).replace("on-old", "X")


def test_fy_off_013_the_agent_keeps_the_ids_with_the_replica(tmp_path):
    """FY-OFF-013: the agent keeps the ids in the replica's record (so a restart before the next
    `desired` still checks), drops ids that are not digests, and fills them in on a record made before
    it had them. (That a real agent lists "image_ids" in hello is in test_agent.py.)"""
    fake = FakeDocker(tmp_path / "bin")
    rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))

    async def scenario():
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32100, 32110))
        rep = replica("feedface01234567", container(image=VULKAN))
        sup.set_desired(desired({**rep, "image_ids": {"ids": ["not-a-digest"]}}))
        await converge(sup, lambda s: "feedface01234567" in s.records)
        assert sup.records["feedface01234567"].image_ids is None
        sup.set_desired(desired({**rep, "image_ids": check()}, revision=2))
        await converge(sup, lambda s: s.records["feedface01234567"].image_ids is not None)
        return sup

    asyncio.run(scenario())
    stored = RecordStore(tmp_path / "state").load_all()["feedface01234567"]
    assert stored.image_ids == check()


def test_fy_off_014_every_builtin_image_has_recorded_ids_and_nothing_else_does():
    """FY-OFF-014: the lock covers each built-in container image, under its exact pinned reference,
    with linux/amd64 among its platforms, and has no entry that no recipe uses (pin_images --write
    makes it so; its check mode compares it with the registry)."""
    from forgeyard.models import recipes

    used = {v.image for r, _ in recipes.builtin_recipes().values() for v in r.variants if v.runtime == "container"}
    lock = images.load_lock()
    assert set(lock) == used
    for image, entry in lock.items():
        assert "linux/amd64" in entry["platforms"], image
        assert entry["index"] is None or image.endswith("@" + entry["index"]), image
        for p in entry["platforms"].values():
            assert all(images.DIGEST_RE.match(p[k]) for k in ("manifest", "config", "content")), image


def test_fy_off_015_pin_images_check_mode(tmp_path, capsys):
    """FY-OFF-015: the check mode passes when the lock is what the registry serves, and fails (exit 1,
    one line each) for ids that differ, an image with no ids, and an entry no recipe uses; --write
    rewrites the lock from the registry."""
    pin = _pin_module()
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "x.yaml").write_text(f"variants:\n  - name: vulkan\n    when: {{vendor: amd}}\n    image: {VULKAN}\n")
    lock = tmp_path / "images.lock.json"

    def run(*argv):
        client = httpx.Client(transport=httpx.MockTransport(_registry()))
        return pin.main(list(argv), client=client, recipes=recipes_dir, lock=lock)

    assert run() == 1 and "no ids recorded for" in capsys.readouterr().out
    assert run("--write") == 0
    assert json.loads(lock.read_text())["images"] == {VULKAN: images.load_lock()[VULKAN]}
    assert run() == 0 and "match the registry" in capsys.readouterr().out
    data = json.loads(lock.read_text())
    data["images"][VULKAN]["platforms"]["linux/amd64"]["content"] = "sha256:" + "0" * 64
    data["images"]["old/image:1@" + INDEX] = data["images"][VULKAN]
    lock.write_text(json.dumps(data))
    assert run() == 1
    out = capsys.readouterr().out
    assert f"the ids recorded for {VULKAN} differ from the registry's" in out
    assert f"old/image:1@{INDEX} is in no recipe any more" in out


# ------------------------------------------------------------------ helpers
def _store(items: list[dict]):
    """An `image inspect` stand-in over a list of local images (Id, repo:tag, repo@digest)."""
    def inspect(ref: str):
        for item in items:
            if ref in (item.get("Id"), *item.get("RepoTags", []), *item.get("RepoDigests", [])):
                return item
        return None
    return inspect


def _start(tmp_path, fake: FakeDocker, ids, image: str = VULKAN):
    rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))

    async def scenario():
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32100, 32110))
        rep = replica("feedface01234567", container(image=image))
        if ids is not None:
            rep["image_ids"] = ids
        sup.set_desired(desired(rep))
        await converge(sup, lambda s: s.records["feedface01234567"].state in ("running", "backoff", "failed"))
        return sup.records["feedface01234567"]

    return asyncio.run(scenario())


def _pin_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("pin_images", ROOT / "scripts" / "pin_images.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registry(tamper: str | None = None):
    """ghcr.io as it served the Vulkan b12242 image: the fixtures, by digest and by tag."""
    by_digest = {INDEX: raw("index"), MANIFEST_AMD64: raw("manifest-amd64"), MANIFEST_ARM64: raw("manifest-arm64"),
                 CONFIG_AMD64: raw("config-amd64"), CONFIG_ARM64: raw("config-arm64")}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/":
            return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://ghcr.io/token",'
                                                                    'service="ghcr.io"'})
        if path == "/token":
            return httpx.Response(200, json={"token": "anonymous"})
        ref = path.rsplit("/", 1)[1]
        if ref == "server-vulkan-b12242":
            ref = INDEX
        body = by_digest.get(ref)
        if body is None:
            return httpx.Response(404)
        if ref == tamper:
            body = body + b" "
        media = json.loads(by_digest[ref]).get("mediaType", "application/octet-stream")
        return httpx.Response(200, content=body, headers={"content-type": media})

    return handler


def test_fy_off_016_the_recipe_list_says_which_images_are_checked_offline(owner):
    """FY-OFF-016: /api/recipes marks each container variant whose image has recorded ids. The ids
    belong to the pinned reference, so a custom recipe naming the same image is checked too; one
    with another digest is not."""
    from forgeyard.models.recipes import builtin_recipes

    got = {r["name"]: r for r in owner.get("/api/recipes").json()["recipes"]}
    llama = {v["name"]: v["image_check"] for v in got["llama-cpp"]["variants"]}
    assert llama == {"cuda": True, "rocm": True, "vulkan": True, "cpu": True, "cpu-process": False}
    text = builtin_recipes()["llama-cpp"][1].replace("name: llama-cpp", "name: own-llama")
    other = "sha256:" + "0" * 64
    text = text.replace(INDEX, other)
    assert owner.post("/api/recipes", json={"yaml": text}).status_code == 201
    own = {r["name"]: r for r in owner.get("/api/recipes").json()["recipes"]}["own-llama"]
    assert {v["name"]: v["image_check"] for v in own["variants"]} == {
        "cuda": True, "rocm": True, "vulkan": False, "cpu": True, "cpu-process": False}
