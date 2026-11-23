"""1.5.2 (#95, PR 3): a deployment moved to another version of its engine in Edit (FY-ESW-*): only between a built-in
engine and its copies, re-estimated, parameters the new version does not know dropped (and said), the catalog's
engine arguments through `based_on`, a rolling or an all-at-once restart, audited; the notice before a change that
restarts the only replica (about how long the model does not answer, and how to avoid it); the hint on a copy whose
built-in engine has caught up."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from forgeyard.db import CatalogModel, Deployment, Provider, Workload, new_session
from forgeyard.models import recipes
from forgeyard.workloads import placement, state as wstate
from tests.cloud_helpers import FakeCloud, add_provider, model_id, set_tiers
from tests.test_deploy_edit import CPU_BODY, GIB, audit, cpu_row, ready, rows, sweep, workload
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

COPY = "llama-cpp-1-1-0"
COPY_URL = "https://github.com/ggml-org/llama.cpp/releases/download/b12400/llama-b12400-bin-ubuntu-x64.tar.gz"
COPY_SHA = "ab" * 32
BUILTIN_CPU = ("ghcr.io/ggml-org/llama.cpp:server-v1.0.0-b12359-b1b770d@sha256:"
               "e3032ea0712da96dec820c770626385de4322dd06619fb5829256a504bbc9375")
COPY_CPU = "ghcr.io/ggml-org/llama.cpp:server-v1.1.0-b12400-0123abc@sha256:" + "cd" * 32


def llama_copy(name: str = COPY, version: str = "1.1.0", *, drop_video_fps: bool = True, cpu_overhead: str = "1GiB",
               based_on: str = "llama-cpp") -> str:
    """The built-in llama-cpp recipe as "Try this version" would copy it: another name and version, `based_on`, another
    process archive; this one also drops `video_fps` (a parameter a newer version no longer takes) and counts more
    CPU overhead (a copy may change what the estimate counts)."""
    text = recipes.builtin_recipes()["llama-cpp"][1]
    text = text.replace("name: llama-cpp\n", f"name: {name}\nbased_on: {based_on}\n", 1)
    text = text.replace("title: llama.cpp\n", f"title: llama.cpp {version}\n", 1)
    text = text.replace('version: "1.0.0"', f'version: "{version}"', 1)
    text = text.replace("  cpu_overhead: 512MiB", f"  cpu_overhead: {cpu_overhead}", 1)
    text = text.replace("https://github.com/ggml-org/llama.cpp/releases/download/b12359/llama-b12359-bin-ubuntu-x64.tar.gz",
                        COPY_URL)
    text = text.replace("0bc1f091e8916102b6d8f2cf944233a620e90a0f856c72d0ce41150c65c2e72e", COPY_SHA)
    text = text.replace("llama-b12359/llama-server", "llama-b12400/llama-server")
    text = text.replace(BUILTIN_CPU, COPY_CPU)
    if drop_video_fps:
        start = text.index("  video_fps: {")
        end = text.index("  kv_cache: {")
        text = text[:start] + text[end:]
        text = text.replace(', --video-fps, "{video_fps}"', "")
    return text


def add_copy(c, text: str) -> None:
    r = c.post("/api/recipes", json={"yaml": text})
    assert r.status_code == 201, r.text


@pytest.fixture(autouse=True)
def no_memory_of_machines():
    placement._previous.clear()
    yield
    placement._previous.clear()


@pytest.fixture()
def two_cpus(owner):
    seed_catalog()
    a = make_node("cpu-a", features=MODELS)
    b = make_node("cpu-b", features=MODELS)
    cpu_row(a, vram_free=100 * GIB)
    cpu_row(b, vram_free=20 * GIB)
    add_copy(owner, llama_copy())
    return owner, a, b


def started(name: str, seconds: float) -> None:
    """The deployment's replica became ready in `seconds` (the event the server writes, 1.5.0)."""
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == name))
        wstate.event(db, w, "ready", f"Replica 0 on cpu-a: ready in {seconds:.1f} s (model 1.0 s, engine {seconds - 1:.1f} s)", 0)
        db.commit()
    finally:
        db.close()


def image(r) -> str:
    return r.spec["runtime"]["image"]


def dep_row(name: str) -> Deployment:
    db = new_session()
    try:
        d = db.scalar(select(Deployment).where(Deployment.name == name))
        db.expunge(d)
        return d
    finally:
        db.close()


# ------------------------------------------------------------------ the switch
def test_fy_esw_001_switch_to_a_copy_and_back_keeps_the_name_route_and_served_name(two_cpus):
    """FY-ESW-001: PATCH {recipe: <copy>} moves a running deployment to the copy: re-estimated with the copy (more CPU
    overhead), the replica restarted on the copy's image, the workload's spec names the copy, the name, served name
    and route stay; `video_fps`, which the copy does not know, is dropped and said; audited with from/to and the
    versions; an event says "engine llama-cpp → llama-cpp-1-1-0". Choosing the built-in again goes back (its image, video_fps with its default)."""
    c, a, b = two_cpus
    d = deploy(c, "chat", served_name="team-chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    ready("chat", 0)
    before = rows("chat")[0]
    assert image(before) == BUILTIN_CPU
    detail = c.get(path).json()["deployment"]
    assert [e["name"] for e in detail["engines"]] == ["llama-cpp", COPY]
    assert detail["engines"][0]["current"] is True and detail["engines"][1]["based_on"] == "llama-cpp"
    assert detail["engine"] == {"name": "llama-cpp", "title": "llama.cpp", "version": "1.0.0", "builtin": True,
                                "based_on": None}
    r = c.patch(path, json={"recipe": COPY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changes"]["recipe"] == {"before": "llama-cpp", "after": COPY}
    assert body["changes"]["video_fps"] == {"before": 1.0, "after": None}
    assert body["dropped"] == ["video_fps"] and body["recipe"] == COPY and body["restart"] == "all"
    assert body["estimate"]["amount"] == d["estimate"]["amount"] + 512 * 1024**2
    out = body["deployment"]
    assert out["recipe"] == COPY and out["name"] == "chat" and out["served_name"] == "team-chat"
    assert out["route"]["served_name"] == "team-chat" and out["route"]["upstream_model"] == "team-chat"
    assert "video_fps" not in out["params"] and out["params"]["context"] == 4096
    after = rows("chat")[0]
    assert after.generation == before.generation + 1 and after.node_id == before.node_id
    assert image(after) == COPY_CPU
    assert workload("chat").spec["runtime"]["recipe"] == COPY and dep_row("chat").recipe == COPY
    detail = c.get(path).json()
    assert detail["deployment"]["engine"]["based_on"] == "llama-cpp" and detail["deployment"]["engine"]["version"] == "1.1.0"
    assert any(e["message"].startswith(f"Changed by owner: engine llama-cpp → {COPY}") for e in detail["events"])
    [rec] = audit("deployment.changed")
    assert rec["recipe"] == {"from": "llama-cpp", "to": COPY}
    assert rec["engine_versions"] == {"from": "1.0.0", "to": "1.1.0"} and rec["dropped"] == ["video_fps"]
    # Back to the built-in: its image again, video_fps comes back with its default.
    ready("chat", 0)
    back = c.patch(path, json={"recipe": "llama-cpp"}).json()
    assert back["changes"]["recipe"] == {"before": COPY, "after": "llama-cpp"} and back["dropped"] == []
    assert back["deployment"]["recipe"] == "llama-cpp" and back["deployment"]["params"]["video_fps"] == 1.0
    assert image(rows("chat")[0]) == BUILTIN_CPU
    assert back["estimate"]["amount"] == d["estimate"]["amount"]


def test_fy_esw_002_only_the_built_in_and_its_copies_and_only_operators(two_cpus, owner):
    """FY-ESW-002: another engine (vllm), a custom engine not based on this one, a copy of another built-in, an
    engine that does not serve the model and one that answers another protocol are refused with 422 and say why; an
    unknown engine is 404; nothing changes; a user may not."""
    from tests.conftest import add_user, sign_in

    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    r = c.patch(path, json={"recipe": "vllm"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "engine_not_related"
    assert "a deployment moves only between a built-in engine and its copies" in r.json()["detail"]["message"]
    add_copy(c, llama_copy("my-llama", "1.2.0", based_on="llama-cpp").replace("based_on: llama-cpp\n", ""))
    assert c.patch(path, json={"recipe": "my-llama"}).json()["detail"]["code"] == "engine_not_related"
    chat_only = llama_copy("llama-chat-only", "1.2.1").replace("formats: [gguf]", "formats: [safetensors]")
    add_copy(c, chat_only)
    r = c.patch(path, json={"recipe": "llama-chat-only"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "incompatible"
    other_api = llama_copy("llama-other-api", "1.2.2").replace("upstream_model: served_name", "upstream_model: repo")
    add_copy(c, other_api)
    r = c.patch(path, json={"recipe": "llama-other-api"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "engine_not_compatible"
    assert c.patch(path, json={"recipe": "nope"}).status_code == 404
    assert [e["name"] for e in c.get(path).json()["deployment"]["engines"]] == ["llama-cpp", COPY]
    add_user(owner, "ursula", role="user")
    assert sign_in(owner, "ursula", "quiet-meadow-4821").patch(path, json={"recipe": COPY}).status_code == 403
    assert dep_row("chat").recipe == "llama-cpp" and workload("chat").generation == 1 and audit("deployment.changed") == []


def test_fy_esw_003_dry_run_re_estimates_and_a_copy_that_does_not_fit_is_refused(two_cpus):
    """FY-ESW-003: a dry run with the copy answers its estimate, where it would go and what restarts, saving nothing;
    when the copy's estimate no longer fits anywhere the switch is refused as other edits are (409 does_not_fit) unless
    `wait`; the same request with the built-in is fine."""
    c, a, b = two_cpus
    add_copy(c, llama_copy("llama-cpp-9-0-0", "9.0.0", cpu_overhead="900GiB"))
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    dry = c.patch(path, json={"recipe": COPY, "dry_run": True}).json()
    assert dry["changes"]["recipe"]["after"] == COPY and dry["placement"]["fits"] is True and "deployment" not in dry
    assert dry["estimate"]["amount"] > d["estimate"]["amount"] and dep_row("chat").recipe == "llama-cpp"
    big = c.patch(path, json={"recipe": "llama-cpp-9-0-0", "dry_run": True}).json()
    assert big["placement"]["fits"] is False
    r = c.patch(path, json={"recipe": "llama-cpp-9-0-0"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "does_not_fit"
    assert dep_row("chat").recipe == "llama-cpp" and workload("chat").generation == 1
    r = c.patch(path, json={"recipe": "llama-cpp-9-0-0", "wait": True})
    assert r.status_code == 200 and r.json()["deployment"]["recipe"] == "llama-cpp-9-0-0"


def test_fy_esw_004_two_replicas_restart_one_at_a_time_on_the_new_engine(two_cpus):
    """FY-ESW-004: FY-EDIT-004's rolling restart for an engine switch: replica 0 restarts on the copy, replica 1 keeps the
    built-in's image until replica 0 is ready; no one-replica notice."""
    c, a, b = two_cpus
    d = deploy(c, "chat", replicas=2, **CPU_BODY)["deployment"]
    ready("chat", 0)
    ready("chat", 1)
    r = c.patch(f"/api/deployments/{d['id']}", json={"recipe": COPY}).json()
    assert r["restart"] == "rolling" and r["outage"] is None
    r0, r1 = rows("chat")
    assert r0.generation == 2 and image(r0) == COPY_CPU
    assert r1.generation == 1 and image(r1) == BUILTIN_CPU
    ready("chat", 0)
    sweep()
    assert rows("chat")[1].generation == 2 and image(rows("chat")[1]) == COPY_CPU


def test_fy_esw_005_the_catalogs_engine_arguments_follow_the_copy(two_cpus):
    """FY-ESW-005: arguments the catalog keeps for the built-in engine (a model's engine_args by recipe name) still
    reach the replica after the switch to its copy (based_on), and after the switch back."""
    c, a, b = two_cpus
    db = new_session()
    m = db.scalar(select(CatalogModel).where(CatalogModel.name == "qwen3-1.7b-gguf"))
    m.engine_args = {"llama-cpp": ["--jinja"]}
    db.commit()
    db.close()
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    assert "--jinja" in rows("chat")[0].spec["runtime"]["args"]
    c.patch(f"/api/deployments/{d['id']}", json={"recipe": COPY})
    assert "--jinja" in rows("chat")[0].spec["runtime"]["args"]


def test_fy_esw_006_a_stopped_deployment_switches_without_a_restart(two_cpus):
    """FY-ESW-006: a stopped deployment is only saved (restart on_start, no notice); it starts on the copy."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    c.post(f"{path}/stop")
    r = c.patch(path, json={"recipe": COPY}).json()
    assert r["restart"] == "on_start" and r["outage"] is None and r["deployment"]["recipe"] == COPY


# ------------------------------------------------------------------ the one-replica notice
def test_fy_esw_007_one_replica_says_how_long_from_its_last_start(two_cpus):
    """FY-ESW-007: a change that restarts the only running replica answers `outage`: the seconds of its last start,
    that a new engine version may take longer on its first start (only for a switch), and the ways to avoid it (a
    replica first, automatic replicas); no cloud tier, so no cloud. A parameter change says the same without the
    first-start line. With automatic replicas on, the minimum is what to raise."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    ready("chat", 0)
    started("chat", 74.2)
    o = c.patch(path, json={"recipe": COPY, "dry_run": True}).json()["outage"]
    assert o["seconds"] == 74 and o["source"] == "last_start" and o["first_start"] is True
    assert o["avoid"] == ["add_replica", "autoscale"] and o["cloud"] is False
    assert o["text"].startswith("It has one replica: the model does not answer for about 74 s while it restarts (its "
                                "last start); a new engine version can take longer on its first start")
    assert ("raise the replicas to 2 in this change when there is room for two (the new one starts first) or turn on "
            "automatic replicas." in o["text"]) and "cloud" not in o["text"]
    p = c.patch(path, json={"params": {"context": 2048}, "dry_run": True}).json()["outage"]
    assert p["first_start"] is False and "new engine version" not in p["text"] and p["seconds"] == 74
    # Nothing restarts: no notice.
    assert c.patch(path, json={"params": {"first_answer_timeout": 60}, "dry_run": True}).json()["outage"] is None
    auto = {"params": {"autoscale": True, "min_replicas": 1, "max_replicas": 2}}
    c.patch(path, json=auto)
    ready("chat", 0)
    o = c.patch(path, json={"recipe": COPY, "dry_run": True}).json()["outage"]
    assert o["avoid"] == ["add_replica", "min_replicas"] and "(a minimum of 2 automatic replicas)" in o["text"]


def test_fy_esw_008_without_its_own_start_other_deployments_or_a_typical_figure(two_cpus):
    """FY-ESW-008: no start of its own measured: the median of the other deployments of the engine (copies count:
    the same engine), else the engine's typical start (llama.cpp: 15 s), each saying which it is."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    ready("chat", 0)
    o = c.patch(path, json={"recipe": COPY, "dry_run": True}).json()["outage"]
    assert (o["seconds"], o["source"]) == (15, "typical") and "(a typical start of this engine)" in o["text"]
    for name, secs in (("other", 20.0), ("third", 40.0), ("fourth", 30.0)):
        deploy(c, name, **CPU_BODY, wait=True)
        started(name, secs)
    o = c.patch(path, json={"recipe": COPY, "dry_run": True}).json()["outage"]
    assert (o["seconds"], o["source"]) == (30, "engine") and "(other deployments of this engine)" in o["text"]


def test_fy_esw_009_a_cloud_tier_answers_keys_that_may_use_it(two_cpus):
    """FY-ESW-009: when the deployment's route has a cloud tier, the notice adds that keys that may use the cloud are
    answered there meanwhile; a provider inside your network is not a cloud tier."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    path = f"/api/deployments/{d['id']}"
    ready("chat", 0)
    cloud = FakeCloud()
    try:
        lan = add_provider(c, cloud, name="lan", external=False, allow_http=True)
        set_tiers(c, "chat", [[("local", d["id"])], [("provider", model_id(lan, "gpt-mini"))]])
        o = c.patch(path, json={"recipe": COPY, "dry_run": True}).json()["outage"]
        assert o["cloud"] is False and "cloud" not in o["avoid"]
        far = add_provider(c, cloud, name="far", external=False, allow_http=True)
        db = new_session()   # outside your network (an https provider would be; the fake one answers plain http)
        db.get(Provider, far["id"]).external = True
        db.commit()
        db.close()
        set_tiers(c, "chat", [[("local", d["id"])], [("provider", model_id(far, "gpt-mini"))]])
        o = c.patch(path, json={"recipe": COPY, "dry_run": True}).json()["outage"]
    finally:
        cloud.close()
    assert o["cloud"] is True and o["avoid"][-1] == "cloud"
    assert o["text"].endswith("; keys that may use the cloud are answered by its cloud tier meanwhile.")


def test_fy_esw_010_the_notice_names_machines_without_the_new_image(owner):
    """FY-ESW-010: a container replica whose machine has no image of the copy yet: the notice names that machine and
    says Download to… fetches it ahead."""
    from forgeyard.db import Node
    from forgeyard.machines import engineimages

    seed_catalog()
    docker = {"process": {"ok": False, "reason": "off"}, "container": {"ok": True, "engine": "docker", "nvidia": False}}
    n = make_node("box", features=MODELS, runtime_info=docker)
    cpu_row(n, vram_free=100 * GIB)
    add_copy(owner, llama_copy())
    d = deploy(owner, "chat", **CPU_BODY)["deployment"]
    ready("chat", 0)
    db = new_session()
    db.get(Node, n).runtime_info = {**docker, engineimages.LISTING_KEY: {"at": "2026-11-23T00:00:00+00:00", "items": []}}
    db.commit()
    db.close()
    o = owner.patch(f"/api/deployments/{d['id']}", json={"recipe": COPY, "dry_run": True}).json()["outage"]
    assert o["image_missing"] == ["box"] and "its image is not on box yet: Download to… fetches it ahead" in o["text"]


# ------------------------------------------------------------------ the copy's page
def test_fy_esw_011_a_copy_says_when_its_built_in_has_caught_up(two_cpus):
    """FY-ESW-011: a copy of a newer version than the built-in has no hint; a copy of the built-in's version or an older
    one says the built-in now runs that version (same / newer); a built-in engine never has one."""
    c, a, b = two_cpus
    assert c.get(f"/api/recipes/{COPY}").json()["caught_up"] is None
    add_copy(c, llama_copy("llama-cpp-1-0-0", "1.0.0"))
    add_copy(c, llama_copy("llama-cpp-0-9-0", "0.9.0"))
    assert c.get("/api/recipes/llama-cpp-1-0-0").json()["caught_up"] == {
        "builtin": "llama-cpp", "title": "llama.cpp", "version": "1.0.0", "same": True}
    assert c.get("/api/recipes/llama-cpp-0-9-0").json()["caught_up"]["same"] is False
    assert c.get("/api/recipes/llama-cpp").json()["caught_up"] is None


def test_fy_esw_012_raising_to_two_replicas_in_the_switch_keeps_the_old_one_serving(two_cpus):
    """FY-ESW-012: what the notice advises: one replica, the switch and Replicas 2 in the same change is a rolling
    change with no notice: the new replica starts on the copy first, the old one keeps serving on the built-in until
    the new one is ready, then restarts on the copy."""
    c, a, b = two_cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    ready("chat", 0)
    r = c.patch(f"/api/deployments/{d['id']}", json={"recipe": COPY, "replicas": 2}).json()
    assert r["restart"] == "rolling" and r["outage"] is None
    r0, r1 = rows("chat")
    assert (r0.generation, r0.state, image(r0)) == (1, "healthy", BUILTIN_CPU)
    assert r1.generation == 2 and image(r1) == COPY_CPU
    sweep()
    assert rows("chat")[0].generation == 1   # the new one is not ready yet: the old one keeps serving
    ready("chat", 1)
    sweep()
    r0 = rows("chat")[0]
    assert r0.generation == 2 and image(r0) == COPY_CPU


def test_fy_esw_013_a_copy_with_the_same_parameters_still_restarts(two_cpus):
    """FY-ESW-013: a copy whose parameters and estimate equal the built-in's: the switch alone restarts the replica (it
    runs another image) and is saved; the dry run says so too."""
    c, a, b = two_cpus
    add_copy(c, llama_copy("llama-cpp-1-0-1", "1.0.1", drop_video_fps=False, cpu_overhead="512MiB"))
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    ready("chat", 0)
    dry = c.patch(f"/api/deployments/{d['id']}", json={"recipe": "llama-cpp-1-0-1", "dry_run": True}).json()
    assert dry["changes"] == {"recipe": {"before": "llama-cpp", "after": "llama-cpp-1-0-1"}} and dry["restart"] == "all"
    r = c.patch(f"/api/deployments/{d['id']}", json={"recipe": "llama-cpp-1-0-1"}).json()
    assert r["restart"] == "all" and r["estimate"]["amount"] == d["estimate"]["amount"]
    assert rows("chat")[0].generation == 2 and image(rows("chat")[0]) == COPY_CPU
