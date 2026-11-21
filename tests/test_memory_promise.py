"""Memory is never promised twice (1.4.5, #166): llama.cpp's layers left on the CPU reserve the machine's RAM, an
APU's card that is the machine's RAM shares one pool with it, and a recipe's changed overhead reaches the
reservations of existing deployments without restarting what runs (FY-MEM-*)."""

from datetime import timedelta

from sqlalchemy import select

from forgeyard.db import Accelerator, Deployment, Replica, Workload, WorkloadEvent, new_session, utcnow
from forgeyard.models import recipes
from forgeyard.workloads import placement
from tests.test_scheduler import MODELS, deploy, replica_row, seed_catalog
from tests.workload_helpers import make_node

GIB = 1024**3
MIB = 1024**2
HEADROOM = 512 * MIB
OFFLOAD = {"gpus": 1, "gpu_layers": 10, "share": "vram"}


def set_memory(node_id: int, vendor: str, free: int, **detail) -> None:
    db = new_session()
    try:
        a = db.scalar(select(Accelerator).where(Accelerator.node_id == node_id, Accelerator.vendor == vendor))
        a.vram_free, a.updated_at = free, utcnow()
        if detail:
            a.detail = {**(a.detail or {}), **detail}
        db.commit()
    finally:
        db.close()


def plan(c, **body):
    r = c.post("/api/deployments/estimate", json={"name": "t", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp", **body})
    assert r.status_code == 200, r.text
    return r.json()


def machine(p, name):
    return next(m for m in p["placement"]["machines"] if m["name"] == name)


def test_fy_mem_001_layers_left_on_the_cpu_reserve_ram(owner):
    """FY-MEM-001: a GGUF model with 10 of 28 layers on the GPU keeps the other 18, the output layer and their KV
    cache in the machine's RAM: placement needs that much RAM free, and while the replica is not yet in the
    agent's numbers that RAM is not promised to anything else (here a CPU replica)."""
    seed_catalog()
    a100 = make_node("a100", allowlist=[4], features=MODELS)
    est = plan(owner, params=OFFLOAD)["estimate"]
    host = est["ram"]
    assert est["device"] == "gpu" and host > GIB and {p["label"] for p in est["host_parts"]} == {
        "Weights on the CPU", "KV cache on the CPU"}
    set_memory(a100, "cpu", GIB)
    r = owner.post("/api/deployments", json={"name": "part", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp",
                                             "params": OFFLOAD})
    assert r.status_code == 409, r.text
    reason = next(m for m in r.json()["detail"]["machines"] if m["name"] == "a100")["reason"]
    assert f"needs {host / GIB:.2f} GiB of RAM for the layers left on the CPU, 0.50 GiB available" in reason, reason
    set_memory(a100, "cpu", 6 * GIB)
    deploy(owner, "part", "qwen3-1.7b-gguf", "llama-cpp", params=OFFLOAD)
    # A CPU replica sees the RAM the GPU replica holds (pending: not yet in the agent's numbers).
    cpu = plan(owner, params={"gpus": 0})
    assert machine(cpu, "a100")["cards"][0]["available"] == 6 * GIB - HEADROOM - host
    # A model that fits on the GPU entirely reserves no RAM.
    assert plan(owner)["estimate"]["ram"] == 0


def test_fy_mem_002_held_ram_counts_until_the_agent_reports_it(owner):
    """FY-MEM-002: the RAM of a GPU replica's CPU layers counts until the replica is ready and the agent reports the
    machine's RAM after that; then the reported free RAM alone counts. Two replicas planned at once count each
    other's."""
    seed_catalog()
    a100 = make_node("a100", allowlist=[4], features=MODELS)
    host = plan(owner, params=OFFLOAD)["estimate"]["ram"]
    set_memory(a100, "cpu", 2 * host + HEADROOM - MIB)  # room for one replica's CPU layers, not two
    two = plan(owner, params=OFFLOAD, replicas=2)["placement"]
    assert two["fits"] is False and two["failed_replica"] == 1 and len(two["placed"]) == 1
    assert "for the layers left on the CPU" in two["machines"][0]["reason"]
    deploy(owner, "part", "qwen3-1.7b-gguf", "llama-cpp", params=OFFLOAD)
    assert plan(owner, params=OFFLOAD)["placement"]["fits"] is False
    db = new_session()
    try:
        r = db.scalar(select(Replica).where(Replica.id == replica_row("part").id))
        r.state, r.ready_at = "healthy", utcnow() - timedelta(seconds=30)
        cpu = db.scalar(select(Accelerator).where(Accelerator.node_id == a100, Accelerator.vendor == "cpu"))
        cpu.updated_at = utcnow() - timedelta(seconds=60)
        db.flush()
        u = placement.usage(db, a100)
        assert [(h.vram, h.pending) for h in u.ram] == [(host, True)]  # the agent's numbers predate it
        cpu.updated_at = utcnow()
        db.flush()
        assert [(h.vram, h.pending) for h in placement.usage(db, a100).ram] == [(host, False)]
        # A replica that restarts (an edit) holds nothing, and what it uses now is credited back on the RAM.
        assert placement.usage(db, a100, release={r.id}).credit[cpu.key] == host
        db.commit()
    finally:
        db.close()
    assert plan(owner, params=OFFLOAD)["placement"]["fits"] is True


def test_fy_mem_003_an_apu_card_and_the_ram_are_one_pool(owner):
    """FY-MEM-003: on an APU whose card reports its GTT pool (system RAM), a GPU replica's reservation is also one on
    the RAM and a CPU replica's one on the card, and the card never offers more than the RAM has free."""
    seed_catalog()
    apu = make_node("strix", "amd-gfx1151-apu", features=MODELS)
    set_memory(apu, "cpu", 80 * GIB)  # the card's GTT has 104 GiB free; the RAM it comes from 80
    gpu = {"gpus": 1, "share": "vram", "vram": "50Gi"}
    assert machine(plan(owner, params=gpu), "strix")["cards"][0]["available"] == 80 * GIB - HEADROOM
    deploy(owner, "on-gpu", "qwen3-1.7b-gguf", "llama-cpp", params=gpu)
    cpu = plan(owner, params={"gpus": 0, "vram": "40Gi"})
    assert cpu["placement"]["fits"] is False
    assert machine(cpu, "strix")["cards"][0]["available"] == 30 * GIB - HEADROOM
    r = owner.post("/api/deployments", json={"name": "on-cpu", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp",
                                             "params": {"gpus": 0, "vram": "40Gi"}})
    assert r.status_code == 409
    # The other way round: a CPU replica first, then the card is short by what it holds.
    deploy(owner, "small-cpu", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "vram": "20Gi"})
    p = plan(owner, params={"gpus": 1, "share": "vram", "vram": "20Gi"})
    assert machine(p, "strix")["cards"][0]["available"] == 80 * GIB - HEADROOM - 50 * GIB - 20 * GIB


def test_fy_mem_004_a_carve_out_is_not_the_ram(owner):
    """FY-MEM-004: a card that reports its BIOS carve-out (pool vram, as the real Strix Halo with 96 GiB set aside)
    holds memory the operating system does not count as RAM: the two pools stay apart."""
    seed_catalog()
    apu = make_node("strix", "amd-gfx1151-apu", features=MODELS)
    set_memory(apu, "amd", 90 * GIB, pool="vram")
    set_memory(apu, "cpu", 20 * GIB)
    deploy(owner, "on-gpu", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 1, "share": "vram", "vram": "50Gi"})
    cpu = plan(owner, params={"gpus": 0, "vram": "10Gi"})
    assert cpu["placement"]["fits"] is True
    assert machine(cpu, "strix")["cards"][0]["available"] == 20 * GIB - HEADROOM
    db = new_session()
    try:
        card = db.scalar(select(Accelerator).where(Accelerator.node_id == apu, Accelerator.vendor == "amd"))
        assert card.unified_memory and not placement.shares_ram(card)
        card.detail = {k: v for k, v in card.detail.items() if k != "pool"}
        assert placement.shares_ram(card)  # an agent that does not say which pool: assume the RAM
        db.rollback()
    finally:
        db.close()


def test_fy_mem_005_a_changed_recipe_overhead_reaches_new_placements(owner):
    """FY-MEM-005: when a recipe's overhead grows, a replica placed from then on reserves the new estimate (the
    deployment's events say so), while one already running keeps its reservation and is not restarted."""
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    text = recipes.builtin_recipes()["llama-cpp"][1].replace("name: llama-cpp", "name: own-llama")
    assert owner.post("/api/recipes", json={"yaml": text}).status_code == 201
    dep = deploy(owner, "own", "qwen3-1.7b-gguf", "own-llama", params={"share": "vram"})
    before = replica_row("own").vram
    db = new_session()
    try:
        r = db.scalar(select(Replica).where(Replica.id == replica_row("own").id))
        r.state, r.ready_at = "healthy", utcnow()
        db.commit()
    finally:
        db.close()
    assert "  overhead: 512MiB\n" in text
    r = owner.put("/api/recipes/own-llama", json={"yaml": text.replace("  overhead: 512MiB\n", "  overhead: 1536MiB\n")})
    assert r.status_code == 200, r.text
    # One more replica (an edit that restarts nothing): it reserves the new estimate, the first keeps its own.
    r = owner.patch(f"/api/deployments/{dep['deployment']['id']}", json={"replicas": 2})
    assert r.status_code == 200, r.text
    assert r.json()["restart"] == "none"
    db = new_session()
    try:
        rows = sorted(db.scalars(select(Replica).join(Workload).where(Workload.name == "own")).all(),
                      key=lambda x: x.ordinal)
        assert rows[0].vram == before and rows[0].state == "healthy" and rows[0].generation == 1
        assert rows[1].vram == before + GIB
        d = db.scalar(select(Deployment).where(Deployment.name == "own"))
        assert d.estimate["per_gpu"] == before + GIB
        # The recipe grows again; the second replica is placed again (its machine went away, say): the estimate is
        # made from the recipe as it is now, stored, and the change is an event.
        own = db.scalar(select(Workload).where(Workload.name == "own"))
        rows[1].node_id, rows[1].state, rows[1].gpus = None, "pending", []
        db.commit()
    finally:
        db.close()
    owner.put("/api/recipes/own-llama", json={"yaml": text.replace("  overhead: 512MiB\n", "  overhead: 2560MiB\n")})
    db = new_session()
    try:
        placement.place_pending(db)
        rows = sorted(db.scalars(select(Replica).where(Replica.workload_id == own.id)).all(), key=lambda x: x.ordinal)
        assert rows[1].node_id is not None and rows[1].vram == before + 2 * GIB
        assert rows[0].vram == before and rows[0].state == "healthy"
        d = db.scalar(select(Deployment).where(Deployment.name == "own"))
        assert d.estimate["per_gpu"] == before + 2 * GIB
        said = [e.message for e in db.scalars(select(WorkloadEvent).where(WorkloadEvent.workload_id == own.id,
                                                                         WorkloadEvent.kind == "estimate")).all()]
        assert said == [f"The estimate changed from {(before + GIB) / GIB:.2f} GiB to {(before + 2 * GIB) / GIB:.2f} GiB "
                        "per GPU: the engine's recipe or Forgeyard's estimate changed. Replicas that run keep what they "
                        "reserved; replicas placed from now on reserve the new amount."]
    finally:
        db.close()


def test_fy_mem_006_an_estimate_that_cannot_be_made_again_stays(owner):
    """FY-MEM-006: a deployment whose estimate the current recipe cannot make (its model lost what the estimate
    needs) keeps the one it has; a placement with nothing new changes nothing and says nothing."""
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    deploy(owner, "keep", "qwen3-1.7b-gguf", "llama-cpp", params={"share": "vram"})
    db = new_session()
    try:
        from forgeyard.models import catalog

        d = db.scalar(select(Deployment).where(Deployment.name == "keep"))
        stored = dict(d.estimate)
        recipe = catalog.get_recipe(db, d.recipe)
        assert catalog.current_estimate(db, d, recipe).amount == stored["amount"] and d.estimate == stored
        d.model.meta = {**d.model.meta, "gguf": {}}
        assert catalog.current_estimate(db, d, recipe).amount == stored["amount"] and d.estimate == stored
        assert not db.scalars(select(WorkloadEvent).where(WorkloadEvent.kind == "estimate")).all()
        db.rollback()
    finally:
        db.close()
