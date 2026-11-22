"""An engine's compile cache kept between starts (1.5.0, issue #37, "engine_cache"; FY-ECA-*): the recipe field (off
in every built-in recipe), what the server sends (only for an enabled recipe and a container replica, beside the
spec), and the agent's side (the directory, its writable mount and variable, and what it removes)."""

import asyncio
import os
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from forgeyard.agent.workloads import enginecache as agent_cache
from forgeyard.db import Deployment, new_session
from forgeyard.machines.protocol import ENGINE_CACHE_MOUNT
from forgeyard.models import enginecache
from forgeyard.models.recipes import RecipeError, builtin_recipes, parse_recipe
from forgeyard.workloads import state as wstate
from tests.test_model_supervisor import TINY_REV, _ctr, container_spec, hf, hf_model, model_replica  # noqa: F401
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import converge, desired, gpus_of, make_node

A100 = gpus_of("nvidia-a100x8")
BOTH = {"process": {"ok": True}, "container": {"ok": True, "nvidia": True}}
KEY = "12-6921abcd-0-0123456789ab"


def vllm_text(name="vllm-cache", enabled=True) -> str:
    text = Path("forgeyard/recipes/vllm.yaml").read_text().replace("name: vllm\n", f"name: {name}\n")
    return text.replace("engine_cache: {env: VLLM_CACHE_ROOT, enabled: false}",
                        f"engine_cache: {{env: VLLM_CACHE_ROOT, enabled: {'true' if enabled else 'false'}}}")


# ------------------------------------------------------------------ the recipe
def test_fy_eca_001_off_in_every_builtin_recipe_and_validated():
    """FY-ECA-001: vllm and vllm-pooling name VLLM_CACHE_ROOT but keep it off (not measured yet); no built-in recipe
    turns it on; a reserved variable, one the recipe sets itself, or a recipe without a container variant is
    refused."""
    builtin = {n: r for n, (r, _) in builtin_recipes().items()}
    assert {n for n, r in builtin.items() if r.engine_cache is not None} == {"vllm", "vllm-pooling"}
    assert all(not r.engine_cache.enabled and r.engine_cache.env == "VLLM_CACHE_ROOT"
               for r in builtin.values() if r.engine_cache is not None)
    assert parse_recipe(vllm_text()).engine_cache.enabled is True
    for env in ("PORT", "FORGEYARD_X", "CUDA_VISIBLE_DEVICES", "VLLM_API_KEY", "HF_HUB_OFFLINE", "1BAD"):
        with pytest.raises(RecipeError):
            parse_recipe(vllm_text().replace("env: VLLM_CACHE_ROOT", f"env: {env}"))
    process_only = yaml.safe_load(Path("forgeyard/recipes/llama-cpp.yaml").read_text())
    process_only.update(name="lcpp-proc", engine_cache={"env": "X_CACHE", "enabled": True},
                        variants=[v for v in process_only["variants"] if v["runtime"] == "process"])
    with pytest.raises(RecipeError, match="container variants"):
        parse_recipe(yaml.safe_dump(process_only))


# ------------------------------------------------------------------ what the server sends
def _desired(node_id):
    db = new_session()
    try:
        return wstate.desired_message(db, node_id, 1)
    finally:
        db.close()


def test_fy_eca_002_sent_only_for_an_enabled_recipe_beside_the_spec(owner):
    """FY-ECA-002: the built-in vllm (off) sends nothing; a copy with it enabled sends {key, env} beside the spec
    (never in it, so an agent older than 1.5.0, which validates the spec strictly, ignores it) and the deployments
    that may keep one, to every machine."""
    seed_catalog()
    assert owner.post("/api/recipes", json={"yaml": vllm_text()}).status_code == 201
    new = make_node("a100", allowlist=[4], features=MODELS, runtime_info=BOTH)
    old = make_node("old", allowlist=[4], features=MODELS, runtime_info=BOTH)
    plain = deploy(owner, "plain", "qwen3-4b", "vllm", params={"context": 4096, "share": "vram"},
                   machine_id=new)["deployment"]
    cached = deploy(owner, "cached", "qwen3-4b", "vllm-cache", params={"context": 4096, "share": "vram"},
                    machine_id=new)["deployment"]
    deploy(owner, "elsewhere", "qwen3-4b", "vllm-cache", params={"context": 4096, "share": "vram"}, machine_id=old)
    msg = _desired(new)
    by = {r["workload"]: r for r in msg["replicas"]}
    assert "engine_cache" not in by["plain"]
    block = by["cached"]["engine_cache"]
    assert block["env"] == "VLLM_CACHE_ROOT" and "engine_cache" not in by["cached"]["spec"]
    db = new_session()
    try:
        dep = db.scalar(select(Deployment).where(Deployment.name == "cached"))
        image = by["cached"]["spec"]["runtime"]["image"]
        assert block["key"] == enginecache.key_for(dep, 0, image)
        assert block["key"].startswith(f"{dep.id}-") and agent_cache.parse(block) == block
        others = sorted(enginecache.deployment_prefix(d) for d in db.scalars(select(Deployment)).all()
                        if d.recipe == "vllm-cache")
    finally:
        db.close()
    assert msg["engine_caches"] == others and len(others) == 2  # every caching deployment, wherever it runs
    old_msg = _desired(old)
    assert old_msg["engine_caches"] == others and "engine_cache" in old_msg["replicas"][0]
    from forgeyard.workloads.spec import WorkloadSpec

    WorkloadSpec.model_validate(old_msg["replicas"][0]["spec"])  # the spec itself is what it was
    assert plain["id"] != cached["id"]


def test_fy_eca_003_the_key_changes_with_the_image_and_the_deployment():
    """FY-ECA-003: another image (a recipe version or variant) or another replica gives another key; a deployment
    made again with the same id (a reset database) gets another prefix from its creation time."""
    from datetime import datetime, timezone

    class Dep:
        id = 12
        created_at = datetime(2026, 11, 22, tzinfo=timezone.utc)

    class Again(Dep):
        created_at = datetime(2026, 11, 23, tzinfo=timezone.utc)

    a = enginecache.key_for(Dep, 0, "vllm/vllm-openai:v0.32.0@sha256:aa")
    assert a == enginecache.key_for(Dep, 0, "vllm/vllm-openai:v0.32.0@sha256:aa")
    assert a != enginecache.key_for(Dep, 0, "vllm/vllm-openai:v0.33.0@sha256:bb")
    assert a != enginecache.key_for(Dep, 1, "vllm/vllm-openai:v0.32.0@sha256:aa")
    assert enginecache.prefix_of(a) == enginecache.deployment_prefix(Dep) != enginecache.deployment_prefix(Again)
    assert agent_cache.parse({"key": a, "env": "VLLM_CACHE_ROOT"}) is not None


# ------------------------------------------------------------------ the agent
def test_fy_eca_004_the_agent_accepts_only_its_own_shapes():
    """FY-ECA-004: a key of another shape (a path, ..) or a reserved variable is refused; so are odd keep lists."""
    ok = {"key": KEY, "env": "VLLM_CACHE_ROOT"}
    assert agent_cache.parse(ok) == ok
    for bad in ({"key": "../etc", "env": "X"}, {"key": KEY + "/x", "env": "X"}, {"key": KEY, "env": "PORT"},
                {"key": KEY, "env": "FORGEYARD_REPLICA"}, {"key": KEY, "env": "A=B"}, {"key": 3, "env": "X"},
                "x", None):
        assert agent_cache.parse(bad) is None, bad
    assert agent_cache.parse_keep(["12-6921abcd", "../x", 3, "12-6921abcd-0"]) == {"12-6921abcd"}
    assert agent_cache.parse_keep(None) is None
    assert agent_cache.same({**ok, "dir": "/x"}, ok) and not agent_cache.same(ok, None)


def test_fy_eca_005_prune_keeps_what_may_be_used_again(tmp_path):
    """FY-ECA-005: a deleted deployment's caches go; a replica wanted with a new key loses its older one; a
    replica's cache kept while its deployment exists (asleep, moved) stays; what a replica uses stays; names of
    another shape and links are never touched."""
    root = tmp_path / "cache"
    asleep, gone, old, new, used = ("1-a-0-" + "a" * 12, "2-b-0-" + "b" * 12, "3-c-0-" + "c" * 12,
                                    "3-c-0-" + "d" * 12, "4-e-1-" + "e" * 12)
    for key in (asleep, gone, old, new, used):
        (agent_cache.directory(root, key) / "torch_compile_cache").mkdir()
    (root / "engine-cache" / "notes").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "engine-cache" / ("9-f-0-" + "f" * 12)).symlink_to(outside)
    removed = agent_cache.prune(root, {"1-a", "3-c", "4-e"}, {new, used})
    assert sorted(removed) == sorted([gone, old])
    left = sorted(p.name for p in (root / "engine-cache").iterdir())
    assert left == sorted([asleep, new, used, "notes", "9-f-0-" + "f" * 12]) and outside.is_dir()
    assert oct(os.stat(root / "engine-cache" / new).st_mode & 0o777) == "0o700"
    with pytest.raises(ValueError):
        agent_cache.directory(root, "9-f-0-" + "f" * 12)  # a link
    with pytest.raises(ValueError):
        agent_cache.directory(root, "../../x")
    assert agent_cache.prune(tmp_path / "nothing", set(), set()) == []


def test_fy_eca_006_the_container_gets_its_cache_writable_and_the_model_read_only(tmp_path, hf):  # noqa: F811
    """FY-ECA-006: the replica's directory under <model cache>/engine-cache is made and mounted writable at
    ENGINE_CACHE_MOUNT, its variable names that path, the model stays the only read-only mount; a desired list that
    no longer lists the deployment removes the directory once the replica is gone."""
    sup, fake = _ctr(tmp_path, hf)
    entry = model_replica("c1", container_spec(), gpus=[A100[4]], model=hf_model(hf, "acme/tiny-chat", TINY_REV))
    entry["engine_cache"] = {"key": KEY, "env": "VLLM_CACHE_ROOT"}
    msg = {**desired(entry), "engine_caches": ["12-6921abcd"]}
    cache_dir = tmp_path / "cache" / "engine-cache" / KEY
    seen = {}

    async def scenario():
        sup.set_desired(msg)
        await converge(sup, lambda s: "c1" in s.records and s.records["c1"].container)
        assert cache_dir.is_dir()
        seen["env"] = next(iter(fake.state().values()))["env"]
        (cache_dir / "torch_compile_cache").mkdir()  # what vLLM writes there
        sup.set_desired({**desired(entry, revision=2), "engine_caches": ["12-6921abcd"]})
        await converge(sup)
        assert cache_dir.is_dir()  # still listed and used
        sup.set_desired({**desired(revision=3), "engine_caches": ["12-6921abcd"]})
        await converge(sup, lambda s: not s.records)
        assert cache_dir.is_dir()  # the deployment exists (asleep, or moved): kept for its next start
        sup.set_desired(desired(revision=4))
        await converge(sup)
        assert cache_dir.is_dir()  # a desired list that does not say which may stay removes nothing
        sup.set_desired({**desired(revision=5), "engine_caches": []})
        await converge(sup)

    asyncio.run(scenario())
    run = fake.runs()[0]
    mounts = [run[i + 1] for i, a in enumerate(run) if a == "--mount"]
    model = tmp_path / "cache" / "hub" / "models--acme--tiny-chat"
    assert mounts == [f"type=bind,source={model},target=/models/hub/models--acme--tiny-chat,ro=true",
                      f"type=bind,source={cache_dir},target={ENGINE_CACHE_MOUNT}"]
    assert "-v" not in run and "--privileged" not in run
    assert seen["env"]["VLLM_CACHE_ROOT"] == ENGINE_CACHE_MOUNT
    assert not cache_dir.exists()  # the deployment was deleted


def test_fy_eca_007_without_the_block_nothing_changes(tmp_path, hf):  # noqa: F811
    """FY-ECA-007: a replica without `engine_cache` (every recipe today) gets no writable mount, no variable and no
    directory, as before 1.5.0; a cache that cannot be made leaves the start as it was, with a warning."""
    sup, fake = _ctr(tmp_path, hf)
    entry = model_replica("c1", container_spec(), gpus=[A100[4]], model=hf_model(hf, "acme/tiny-chat", TINY_REV))

    async def scenario(e):
        sup.set_desired(desired(e))
        await converge(sup, lambda s: "c1" in s.records and s.records["c1"].container)

    asyncio.run(scenario(entry))
    assert sup.records["c1"].engine_cache is None
    sup.set_desired(desired(dict(entry, engine_cache={"key": KEY, "env": "VLLM_CACHE_ROOT"}), revision=2))
    asyncio.run(converge(sup))
    assert sup.records["c1"].engine_cache == {"key": KEY, "env": "VLLM_CACHE_ROOT"}  # used from its next start
    run = fake.runs()[0]
    assert [run[i + 1] for i, a in enumerate(run) if a == "--mount"] == [
        f"type=bind,source={tmp_path / 'cache' / 'hub' / 'models--acme--tiny-chat'},"
        "target=/models/hub/models--acme--tiny-chat,ro=true"]
    assert "VLLM_CACHE_ROOT" not in next(iter(fake.state().values()))["env"]
    assert not (tmp_path / "cache" / "engine-cache").exists()

    sup2, fake2 = _ctr(tmp_path / "b", hf)
    (tmp_path / "b" / "cache" / "engine-cache").write_text("not a directory")
    blocked = dict(entry, engine_cache={"key": KEY, "env": "VLLM_CACHE_ROOT"})

    async def scenario2():
        sup2.set_desired(desired(blocked))
        await converge(sup2, lambda s: "c1" in s.records and s.records["c1"].container)

    asyncio.run(scenario2())
    run = fake2.runs()[0]
    assert sum(1 for a in run if a == "--mount") == 1
    assert "VLLM_CACHE_ROOT" not in next(iter(fake2.state().values()))["env"]


def test_fy_eca_008_never_for_a_process_replica(owner, tmp_path):
    """FY-ECA-008: a caching recipe's process variant gets no engine cache from the server; and an agent given one
    for a process replica anyway makes no directory."""
    seed_catalog()
    lcpp = yaml.safe_load(Path("forgeyard/recipes/llama-cpp.yaml").read_text())
    lcpp.update(name="lcpp-cache", engine_cache={"env": "X_ENGINE_CACHE", "enabled": True})
    assert owner.post("/api/recipes", json={"yaml": yaml.safe_dump(lcpp)}).status_code == 201
    cpu = make_node("cpu", features=MODELS,
                    runtime_info={"process": {"ok": True}, "container": {"ok": False}})
    deploy(owner, "small", "qwen3-1.7b-gguf", "lcpp-cache", params={"gpus": 0})
    msg = _desired(cpu)
    assert msg["replicas"][0]["spec"]["runtime"]["type"] == "process"
    assert "engine_cache" not in msg["replicas"][0] and len(msg["engine_caches"]) == 1

    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.records import Record
    from forgeyard.agent.workloads.supervisor import Supervisor

    sup = Supervisor(tmp_path / "state", {}, models=ModelCache(tmp_path / "c"), tick=0.05)
    rec = Record(id="p1", generation=1, workload="small", ordinal=0, kind="service", runtime="process", spec={},
                 engine_cache={"key": KEY, "env": "X_ENGINE_CACHE"})
    asyncio.run(sup._engine_cache_dir(rec))
    assert "dir" not in rec.engine_cache and not (tmp_path / "c" / "engine-cache").exists()


def test_fy_eca_009_a_new_key_replaces_the_old_and_a_bad_block_is_dropped(tmp_path, hf):  # noqa: F811
    """FY-ECA-009: a replica wanted again with another key (its recipe changed while it was away) loses its older
    directory at once, before it starts; a block of another shape is dropped (no directory, no mount)."""
    sup, fake = _ctr(tmp_path, hf)
    old_key, new_key = "12-6921abcd-0-" + "a" * 12, "12-6921abcd-0-" + "b" * 12
    old_dir = agent_cache.directory(tmp_path / "cache", old_key)
    entry = model_replica("c1", container_spec(), gpus=[A100[4]], model=hf_model(hf, "acme/tiny-chat", TINY_REV))

    async def scenario():
        sup.set_desired({**desired(dict(entry, engine_cache={"key": new_key, "env": "VLLM_CACHE_ROOT"})),
                         "engine_caches": ["12-6921abcd"]})
        await sup.reconcile_once()
        assert not old_dir.exists()
        await converge(sup, lambda s: "c1" in s.records and s.records["c1"].container)

    asyncio.run(scenario())
    assert (tmp_path / "cache" / "engine-cache" / new_key).is_dir()

    sup2, fake2 = _ctr(tmp_path / "b", hf)

    async def bad():
        sup2.set_desired(desired(dict(entry, engine_cache={"key": "../../etc", "env": "VLLM_CACHE_ROOT"})))
        await converge(sup2, lambda s: "c1" in s.records and s.records["c1"].container)

    asyncio.run(bad())
    assert sup2.records["c1"].engine_cache is None
    assert sum(1 for a in fake2.runs()[0] if a == "--mount") == 1
