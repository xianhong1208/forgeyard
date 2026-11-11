"""Migration 0009 (1.2) on routes made before it: each becomes one tier-0 target, its deployment, and
behaves as before; the downgrade keeps deployments' routes and drops what 1.1 cannot express
(FY-RM-*)."""

from alembic import command
from sqlalchemy import text

from forgeyard.config import get_settings
from forgeyard.db import get_engine
from forgeyard.migrate import alembic_config, upgrade
from tests.cloud_helpers import FakeCloud, add_provider, cloud_key, deployment_id, model_id, set_tiers
from tests.gateway_helpers import FakeEngine, api_client, make_ready, new_key
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

KEYS = MODELS + ("replica_keys",)


def down_to_1_1():
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0008_encrypt_secrets")


def test_fy_rm_001_old_routes_become_one_local_tier(owner):
    """FY-RM-001: routes as 1.1 stored them (served name, upstream model, deployment, workload) come
    back from the upgrade as one tier-0 local target each, with the same upstream model, and serve
    exactly as before."""
    seed_catalog()
    node = make_node("box", features=KEYS)
    engine = FakeEngine("e", "s-1")
    try:
        deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
        deploy(owner, "asr", "faster-whisper-small", "faster-whisper", wait=True)
        down_to_1_1()
        with get_engine().connect() as conn:
            old = conn.execute(text("SELECT served_name, upstream_model, deployment_id, workload_id FROM gateway_routes "
                                    "ORDER BY served_name")).all()
        assert [(r[0], r[1]) for r in old] == [("asr", "Systran/faster-whisper-small"), ("chat", "chat")]
        assert all(r[2] and r[3] for r in old)
        upgrade()
        with get_engine().connect() as conn:
            rows = conn.execute(text("SELECT g.served_name, t.tier, t.kind, t.upstream_model, t.weight, t.enabled, "
                                     "t.deployment_id = g.deployment_id FROM route_targets t JOIN gateway_routes g "
                                     "ON g.id = t.route_id ORDER BY g.served_name")).all()
        assert [tuple(r) for r in rows] == [("asr", 0, "local", "Systran/faster-whisper-small", 1, True, True),
                                            ("chat", 0, "local", "chat", 1, True, True)]
        make_ready("chat", [(node, engine.port, engine.secret)])
        api = api_client(owner, new_key(owner, "k")["key"])
        r = api.post("/v1/chat/completions", json={"model": "chat", "messages": []})
        assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:chat#0"
        assert engine.posts()[-1]["body"]["model"] == "chat"
        routes = {x["served_name"]: x for x in owner.get("/api/routes").json()["routes"]}
        assert routes["chat"]["kind"] == "local" and routes["chat"]["local_only"] is False
        assert routes["asr"]["upstream_model"] == "Systran/faster-whisper-small"
    finally:
        engine.close()


def test_fy_rm_002_the_downgrade_keeps_deployment_routes(owner, monkeypatch):
    """FY-RM-002: going back to 1.1 keeps every deployment's route (its own upstream model), and
    drops cloud-only routes, provider targets and providers."""
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    seed_catalog()
    make_node("box", features=KEYS)
    cloud = FakeCloud("c")
    try:
        deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
        pm = model_id(add_provider(owner, cloud), "gpt-mini")
        set_tiers(owner, "chat", [[("local", deployment_id(owner, "chat"))], [("provider", pm)]])
        owner.post("/api/routes", json={"served_name": "gpt", "api": "openai-chat",
                                        "targets": [{"kind": "provider", "provider_model_id": pm}]})
        key = cloud_key(owner, "k", "on")
        api_client(owner, key).post("/v1/chat/completions", json={"model": "gpt", "messages": []})
        from forgeyard.gateway.usage import recorder

        recorder.flush()
        down_to_1_1()
        with get_engine().connect() as conn:
            assert conn.execute(text("SELECT served_name, upstream_model FROM gateway_routes")).all() == [("chat", "chat")]
            tables = set(conn.dialect.get_table_names(conn))
            assert not {"providers", "provider_models", "route_targets"} & tables
            assert conn.execute(text("SELECT model, requests FROM usage_daily")).all() == [("gpt", 1)]
        upgrade()
    finally:
        cloud.close()
