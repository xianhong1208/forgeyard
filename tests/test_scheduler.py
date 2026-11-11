"""The model scheduler: filter, score, binpack, VRAM sharing, free (not total) memory, pending
reservations, cached-model preference and "does not fit, by how much" (FY-SCH-*)."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard.db import Accelerator, Node, Replica, new_session, utcnow
from forgeyard.models import recipes, scheduler
from forgeyard.models.scheduler import Option
from tests.model_helpers import FIX, GGUF_REV, QWEN4B_REV, RERANK_REV, WHISPER_REV, add_catalog
from tests.workload_helpers import PROCESS_OK, make_node, task

GIB = 1024**3
MODELS = ("workloads", "models")


def _st_meta(repo, rev, config_file, params, weights_bytes, linear):
    from forgeyard.models.inspect import config_summary

    cfg = config_summary(json.loads((FIX / config_file).read_text()))
    return {"kind": "hf", "repo": repo, "ref": "main", "revision": rev, "format": "safetensors", "config": cfg,
            "weights": {"params": {"BF16": params}, "bytes": weights_bytes, "linear_params": linear,
                        "other_params": params - linear},
            "files": [{"path": "model.safetensors", "size": weights_bytes, "sha256": "a" * 64}],
            "size_bytes": weights_bytes}


def seed_catalog():
    """The five acceptance models, with the numbers their real files give."""
    from forgeyard.models import gguf

    add_catalog("qwen3-4b", _st_meta("Qwen/Qwen3-4B", QWEN4B_REV, "qwen3-4b.config.json", 4022468096, 8044936192,
                                     3633315840), task="chat", fmt="safetensors", source="hf://Qwen/Qwen3-4B")
    add_catalog("qwen3-embedding-0.6b", _st_meta("Qwen/Qwen3-Embedding-0.6B", "e" * 40, "qwen3-embedding-0.6b.config.json",
                                                 595776512, 1191553024, 440000000),
                task="embed", fmt="safetensors", source="hf://Qwen/Qwen3-Embedding-0.6B")
    add_catalog("qwen3-reranker-0.6b", _st_meta("Qwen/Qwen3-Reranker-0.6B", RERANK_REV, "qwen3-reranker-0.6b.config.json",
                                                595776512, 1191553024, 440000000),
                task="rerank", fmt="safetensors", source="hf://Qwen/Qwen3-Reranker-0.6B",
                engine_args={"vllm-pooling": ["--hf-overrides", '{"architectures": ["Qwen3ForSequenceClassification"]}']})
    add_catalog("faster-whisper-small", {"kind": "hf", "repo": "Systran/faster-whisper-small", "ref": "main",
                                         "revision": WHISPER_REV, "format": "ctranslate2", "weights_bytes": 483546902,
                                         "files": [{"path": "model.bin", "size": 483546902, "sha256": "b" * 64}],
                                         "size_bytes": 486214370},
                task="asr", fmt="ctranslate2", source="hf://Systran/faster-whisper-small")
    g = gguf.parse((FIX / "qwen3-1.7b-q4_k_m.gguf-header").read_bytes()).summary()
    g["vocab"] = 151936
    add_catalog("qwen3-1.7b-gguf", {"kind": "hf", "repo": "ggml-org/Qwen3-1.7B-GGUF", "ref": "main", "revision": GGUF_REV,
                                    "format": "gguf", "file": "Qwen3-1.7B-Q4_K_M.gguf", "gguf": g,
                                    "files": [{"path": "Qwen3-1.7B-Q4_K_M.gguf", "size": 1282439264, "sha256": "c" * 64}],
                                    "size_bytes": 1282439264},
                task="chat", fmt="gguf", source="hf://ggml-org/Qwen3-1.7B-GGUF")


@pytest.fixture()
def fleet(owner):
    """An 8 x A100 host with one card allowed (#4, partly used), a 2 x 4090 box
    with an older driver, and catalog entries for the acceptance models."""
    seed_catalog()
    a100 = make_node("a100", allowlist=[4], features=MODELS)
    rtx = make_node("rtx", "nvidia-rtx4090x2", features=MODELS)
    return owner, a100, rtx


def deploy(c, name, model, recipe, status=201, **kw):
    body = {"name": name, "model": model, "recipe": recipe, **kw}
    r = c.post("/api/deployments", json=body)
    assert r.status_code == status, r.text
    return r.json()


def reps(c, dep):
    return c.get(f"/api/deployments/{dep['deployment']['id']}").json()["deployment"]["workload"]["replica_list"]


def replica_row(workload_name):
    db = new_session()
    try:
        from forgeyard.db import Workload
        return db.scalar(select(Replica).join(Workload).where(Workload.name == workload_name))
    finally:
        db.close()


def test_fy_sch_001_four_gpu_models_share_card_4(fleet):
    """FY-SCH-001: chat (shared), embeddings and rerank (shared by default) and speech all land on
    #4, the only allowed card; vLLM gets --gpu-memory-utilization from each reservation."""
    c, a100, rtx = fleet
    chat = deploy(c, "chat", "qwen3-4b", "vllm", params={"share": "vram"})
    embed = deploy(c, "embed", "qwen3-embedding-0.6b", "vllm-pooling")
    rerank = deploy(c, "rerank", "qwen3-reranker-0.6b", "vllm-pooling")
    for d in (chat, embed, rerank):
        r = reps(c, d)[0]
        assert r["machine"]["name"] == "a100" and [g["index"] for g in r["gpus"]] == [4]
    row = replica_row("chat")
    args = row.spec["runtime"]["args"]
    image = row.spec["runtime"]["image"]
    assert image == recipes.builtin_recipes()["vllm"][0].variant("cuda").image  # the agent is sent the digest
    assert image.startswith("vllm/vllm-openai:v0.32.0@sha256:") and row.spec["runtime"]["command"] == ["vllm", "serve"]
    assert args[args.index("--gpu-memory-utilization") + 1] == "0.174"
    assert args[0] == "${MODEL_PATH}" and args[args.index("--served-model-name") + 1] == "chat"
    assert row.vram == 15443688448 and row.spec["resources"]["gpus"]["share"] == "vram"
    assert row.model["repo"] == "Qwen/Qwen3-4B" and row.model["revision"] == QWEN4B_REV and "token" not in row.model
    rr = replica_row("rerank")
    assert '{"architectures": ["Qwen3ForSequenceClassification"]}' in rr.spec["runtime"]["args"]
    assert rr.spec["env"]["HF_HUB_CACHE"] == "${MODEL_HUB}" and "--runner" in rr.spec["runtime"]["args"]
    # Speech goes to the tightest card that fits: the 4090 with 5 GB free (binpack), not #4.
    asr = deploy(c, "asr", "faster-whisper-small", "faster-whisper")
    r = reps(c, asr)[0]
    assert r["machine"]["name"] == "rtx" and [g["index"] for g in r["gpus"]] == [1]
    events = c.get(f"/api/deployments/{chat['deployment']['id']}").json()["events"]
    assert any("gpu_memory_utilization 0.174" in e["message"] and "reserves 14.38 GiB" in e["message"] for e in events)


def test_fy_sch_002_free_memory_not_total_and_by_how_much(fleet):
    """FY-SCH-002: #4 has 80 GB but only ~45 GB free (other people's work): a 61 GiB deployment is
    refused, and every machine says why, with the shortfall."""
    c, a100, rtx = fleet
    body = {"name": "big", "model": "qwen3-4b", "recipe": "vllm", "params": {"context": 32768, "concurrency": 10}}
    est = c.post("/api/deployments/estimate", json=body).json()
    need = est["estimate"]["per_gpu"]
    assert need > 50 * GIB and est["placement"]["fits"] is False
    r = c.post("/api/deployments", json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "does_not_fit"
    machines = {m["name"]: m for m in r.json()["detail"]["machines"]}
    free4 = 48294330368 - 512 * 1024**2
    assert machines["a100"]["short_by"] == need - free4
    assert f"#4 {free4 / GIB:.2f} GiB" in machines["a100"]["reason"] and "short" in machines["a100"]["reason"]
    assert "driver 550.54.14 is not >=580" in machines["rtx"]["reason"]
    msg = r.json()["detail"]["message"]
    assert msg.startswith("Nothing can take a replica that needs") and "a100:" in msg and "rtx:" in msg
    # Nothing was created.
    assert c.get("/api/deployments").json()["deployments"] == []


def test_fy_sch_003_reservations_count_until_the_agent_reports_them(fleet):
    """FY-SCH-003: a placed replica's reservation is subtracted from the card's free memory until it
    is ready AND a later inventory shows its memory in use; then the reported number alone counts."""
    c, a100, rtx = fleet
    deploy(c, "chat", "qwen3-4b", "vllm", params={"share": "vram", "context": 16384, "concurrency": 4})
    first = replica_row("chat").vram
    body = {"name": "chat2", "model": "qwen3-4b", "recipe": "vllm", "params": {"share": "vram", "context": 16384,
                                                                                "concurrency": 4}}
    est = c.post("/api/deployments/estimate", json=body).json()
    card = next(x for x in est["placement"]["machines"] if x["name"] == "a100")["cards"][0]
    assert card["available"] == 48294330368 - 512 * 1024**2 - first
    db = new_session()
    try:
        r = db.scalar(select(Replica).where(Replica.id == replica_row("chat").id))
        r.state, r.ready_at = "healthy", utcnow() - timedelta(seconds=30)
        acc = db.scalar(select(Accelerator).where(Accelerator.node_id == a100, Accelerator.index == 4,
                                                  Accelerator.vendor == "nvidia"))
        acc.updated_at = utcnow() - timedelta(seconds=60)  # inventory older than the load: still counts
        db.commit()
        est = c.post("/api/deployments/estimate", json=body).json()
        assert next(x for x in est["placement"]["machines"] if x["name"] == "a100")["cards"][0]["available"] == card["available"]
        acc.updated_at = utcnow()
        acc.vram_free = 48294330368 - first  # the agent now reports the memory in use
        db.commit()
    finally:
        db.close()
    est = c.post("/api/deployments/estimate", json=body).json()
    assert next(x for x in est["placement"]["machines"] if x["name"] == "a100")["cards"][0]["available"] == \
        48294330368 - first - 512 * 1024**2


def test_fy_sch_004_exclusive_and_shared_never_mix(fleet):
    """FY-SCH-004: an exclusive replica keeps #4 to itself; shared ones then cannot go there, and a
    card shared by others is never taken exclusively."""
    c, a100, rtx = fleet
    deploy(c, "chat", "qwen3-4b", "vllm")  # exclusive (the recipe's default)
    r = c.post("/api/deployments", json={"name": "embed", "model": "qwen3-embedding-0.6b", "recipe": "vllm-pooling"})
    assert r.status_code == 409
    a = next(m for m in r.json()["detail"]["machines"] if m["name"] == "a100")
    assert "in use: #4 (chat)" in a["reason"]
    c.delete(f"/api/deployments/{c.get('/api/deployments').json()['deployments'][0]['id']}")
    deploy(c, "embed", "qwen3-embedding-0.6b", "vllm-pooling")
    r = c.post("/api/deployments", json={"name": "chat", "model": "qwen3-4b", "recipe": "vllm"})
    assert r.status_code == 409 and "does not share cards" in next(
        m for m in r.json()["detail"]["machines"] if m["name"] == "a100")["reason"]


def test_fy_sch_005_binpack_then_cached_then_name():
    """FY-SCH-005: the tightest fit wins; on the same whole GiB left, the machine with the model in
    its cache; then the name. Spread picks the emptiest."""

    class N:
        def __init__(self, name):
            self.name, self.status, self.id = name, "online", ord(name[0])

    def opt(name, left, cached):
        return Option(N(name), None, [], left, cached)

    v = lambda o: scheduler.Verdict(1, o.node.name, "online", True, option=o)  # noqa: E731
    tight, roomy = opt("b", 2 * GIB, False), opt("a", 30 * GIB, True)
    assert scheduler.choose([v(roomy), v(tight)], None) is tight
    same1, same2 = opt("a", int(5.2 * GIB), False), opt("z", int(5.7 * GIB), True)
    assert scheduler.choose([v(same1), v(same2)], None) is same2
    assert scheduler.choose([v(opt("b", GIB, False)), v(opt("a", GIB, False))], None).node.name == "a"
    assert scheduler.choose([v(roomy), v(tight)], None, strategy="spread") is roomy
    # A machine that already has a replica of the same deployment comes last (M4: spread).
    assert scheduler.choose([v(roomy), v(tight)], None, siblings={tight.node.id}) is roomy


def test_fy_sch_006_prefers_the_machine_with_the_model_cached(owner):
    """FY-SCH-006: two identical 4090 boxes; the one that already has faster-whisper wins."""
    seed_catalog()
    make_node("rtx-a", "nvidia-rtx4090x2", features=MODELS)
    b = make_node("rtx-b", "nvidia-rtx4090x2", features=MODELS)
    db = new_session()
    try:
        node = db.get(Node, b)
        node.model_cache = {"ok": True, "cached": [f"hub/models--Systran--faster-whisper-small@{WHISPER_REV}"]}
        db.commit()
    finally:
        db.close()
    d = deploy(owner, "asr", "faster-whisper-small", "faster-whisper")
    assert reps(owner, d)[0]["machine"]["name"] == "rtx-b"
    assert d["placement"]["placed"][0]["cached"] is True


def test_fy_sch_007_driver_and_runtime_filters(owner):
    """FY-SCH-007: the 4090 box gets vLLM once its driver is new enough; without NVIDIA support in
    its container engine it gets nothing that needs it."""
    seed_catalog()
    rtx = make_node("rtx", "nvidia-rtx4090x2", features=MODELS)
    r = owner.post("/api/deployments/estimate", json={"name": "c", "model": "qwen3-4b", "recipe": "vllm"}).json()
    assert r["placement"]["fits"] is False
    db = new_session()
    try:
        for a in db.scalars(select(Accelerator).where(Accelerator.node_id == rtx, Accelerator.vendor == "nvidia")):
            a.driver = "580.95.05"
        db.commit()
        r = owner.post("/api/deployments/estimate", json={"name": "c", "model": "qwen3-4b", "recipe": "vllm"}).json()
        assert r["placement"]["fits"] is True and r["placement"]["placed"][0]["gpus"] == [0]
        node = db.get(Node, rtx)
        node.runtime_info = {**PROCESS_OK, "container": {"ok": True, "engine": "docker", "nvidia": False}}
        db.commit()
    finally:
        db.close()
    r = owner.post("/api/deployments/estimate", json={"name": "c", "model": "qwen3-4b", "recipe": "vllm"}).json()
    assert "cannot pass NVIDIA GPUs" in r["placement"]["machines"][0]["reason"]


def test_fy_sch_008_cpu_gguf_container_or_process(owner):
    """FY-SCH-008: llama.cpp on the CPU: the container variant when Docker works, else the process
    variant with its pinned engine download; RAM is checked like VRAM."""
    seed_catalog()
    make_node("cpu-box", "nvidia-rtx4090x2", features=MODELS,
              runtime_info={**PROCESS_OK, "container": {"ok": False, "reason": "docker not found"}})
    d = deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    row = replica_row("small")
    assert row.spec["runtime"]["type"] == "process" and row.spec["runtime"]["command"] == "llama-server"
    assert row.engine["sha256"] == "3b0c3d7d7f7c492f59bb5625bd077727f5322fff72b964dfa32ceca45bbe5053"
    args = row.spec["runtime"]["args"]
    assert args[:2] == ["-m", "${MODEL_FILE}"] and args[args.index("--host") + 1] == "${BIND_ADDRESS}"
    assert args[args.index("--port") + 1] == "${PORT}" and args[args.index("-ngl") + 1] == "0"
    assert args[args.index("-c") + 1] == "8192" and row.gpus == [] and row.vram == d["deployment"]["estimate"]["ram"]
    db = new_session()
    try:
        node = db.scalar(select(Node).where(Node.name == "cpu-box"))
        node.runtime_info = PROCESS_OK
        db.commit()
    finally:
        db.close()
    deploy(owner, "small2", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    row2 = replica_row("small2")
    assert row2.spec["runtime"]["image"].startswith("ghcr.io/ggml-org/llama.cpp:server-b11977@sha256:") and row2.engine is None
    many = owner.post("/api/deployments/estimate", json={"name": "t", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp",
                                                         "params": {"gpus": 0, "threads": 24}}).json()["placement"]
    assert many["fits"] is False and "needs 24 CPU threads, 16 free of 32" in many["machines"][0]["reason"]
    big = owner.post("/api/deployments/estimate", json={"name": "huge", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp",
                                                        "params": {"gpus": 0, "vram": "500Gi"}}).json()
    assert big["placement"]["fits"] is False and "of RAM" in big["placement"]["machines"][0]["reason"]


def test_fy_sch_009_offline_machines_and_agents_without_models(owner):
    """FY-SCH-009: an offline machine is only used when chosen (the replica waits for it); an agent
    without a model cache never gets a model."""
    seed_catalog()
    off = make_node("a100", allowlist=[4], features=MODELS, status="offline")
    make_node("old", features=("workloads",))
    r = owner.post("/api/deployments", json={"name": "e", "model": "qwen3-embedding-0.6b", "recipe": "vllm-pooling"})
    assert r.status_code == 409
    ms = {m["name"]: m for m in r.json()["detail"]["machines"]}
    assert ms["a100"]["fits"] is True and ms["a100"]["reason"] == "offline"
    assert "start it with --model-cache" in ms["old"]["reason"]
    d = deploy(owner, "e", "qwen3-embedding-0.6b", "vllm-pooling", machine_id=off)
    rep = reps(owner, d)[0]
    assert rep["machine"]["name"] == "a100" and rep["reason"] == "waiting for a100 to come online"


def test_fy_sch_010_m2_workloads_and_model_replicas_respect_each_other(fleet):
    """FY-SCH-010: an M2 GPU task (exclusive, no VRAM declared) blocks #4 for model replicas; an M2
    workload that declares VRAM and sharing shares #4 with them, inside the free memory."""
    c, a100, rtx = fleet
    w = c.post("/api/workloads", json=task("probe", machine_id=a100, resources={"gpus": {"count": 1}})).json()["workload"]
    assert w["replica_list"][0]["gpus"][0]["index"] == 4
    r = c.post("/api/deployments", json={"name": "embed", "model": "qwen3-embedding-0.6b", "recipe": "vllm-pooling"})
    assert r.status_code == 409 and "probe" in r.text
    c.delete(f"/api/workloads/{w['id']}")
    shared = task("shared", machine_id=a100, resources={"gpus": {"count": 1, "share": "vram", "vram": "40Gi"}})
    w = c.post("/api/workloads", json=shared).json()["workload"]
    assert w["replica_list"][0]["gpus"][0]["index"] == 4
    deploy(c, "embed", "qwen3-embedding-0.6b", "vllm-pooling")  # 3.4 GiB still fits beside it
    r = c.post("/api/deployments", json={"name": "chat", "model": "qwen3-4b", "recipe": "vllm",
                                         "params": {"share": "vram"}})
    assert r.status_code == 409 and "short" in r.text
    w2 = c.post("/api/workloads", json=task("more", machine_id=a100,
                                             resources={"gpus": {"count": 1, "share": "vram", "vram": "10Gi"}}))
    assert w2.status_code == 201
    rep = w2.json()["workload"]["replica_list"][0]
    assert rep["machine"] is None and "short" in rep["reason"]


def test_fy_sch_011_several_replicas_are_placed_one_after_the_other(fleet):
    """FY-SCH-011: the preview places replicas in turn, each seeing the ones before; it says which
    replica no longer fits."""
    c, a100, rtx = fleet
    ok = c.post("/api/deployments/estimate", json={"name": "e", "model": "qwen3-embedding-0.6b", "recipe": "vllm-pooling",
                                                   "replicas": 3}).json()["placement"]
    assert ok["fits"] and [p["gpus"] for p in ok["placed"]] == [[4], [4], [4]]  # the 4090 driver is too old for vLLM
    many = c.post("/api/deployments/estimate", json={"name": "e", "model": "qwen3-embedding-0.6b",
                                                     "recipe": "vllm-pooling", "replicas": 16}).json()["placement"]
    assert many["fits"] is False and many["failed_replica"] == 13  # 13 x 3.38 GiB fit in 44.5 GiB


def test_fy_sch_012_replicas_of_one_deployment_spread_over_machines(owner):
    """FY-SCH-012: two replicas go to two machines when both can take one (the gateway balances and
    fails over between them); with one machine both go there."""
    seed_catalog()
    a = make_node("cpu-a", features=MODELS)
    b = make_node("cpu-b", features=MODELS)
    body = {"name": "g", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp", "replicas": 2, "params": {"gpus": 0, "threads": 2}}
    plan = owner.post("/api/deployments/estimate", json=body).json()["placement"]
    assert [p["machine"] for p in plan["placed"]] == ["cpu-a", "cpu-b"]
    deploy(owner, "g", "qwen3-1.7b-gguf", "llama-cpp", replicas=2, params={"gpus": 0, "threads": 2})
    db = new_session()
    try:
        from forgeyard.db import Workload
        w = db.scalar(select(Workload).where(Workload.name == "g"))
        assert sorted(r.node_id for r in w.replicas) == sorted([a, b])
    finally:
        db.close()
    one = owner.post("/api/deployments/estimate", json={**body, "machine_id": a}).json()["placement"]
    assert [p["machine"] for p in one["placed"]] == ["cpu-a", "cpu-a"]
