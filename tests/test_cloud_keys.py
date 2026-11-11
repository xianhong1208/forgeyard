"""A key's `cloud` setting (1.2): off / fallback / on against local-only, mixed and cloud-only
models (listing, 404, spilling), the route's local-only flag, the server's default and cap, the
admin's lock, and the playground (FY-CK-*)."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forgeyard.config import get_settings
from forgeyard.db import ApiKey, AuditEvent, new_session
from tests.cloud_helpers import FakeCloud, add_provider, cloud_key, deployment_id, model_id, set_tiers
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import FakeEngine, api_client, make_ready, new_key, set_state
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

KEYS = MODELS + ("replica_keys",)
PW = "quiet-meadow-4821"


@pytest.fixture()
def world(owner, monkeypatch):
    """local-m: one deployment; mixed-m: a deployment, then the cloud; cloud-m: the cloud only;
    own-m: a provider inside your network (not cloud)."""
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    seed_catalog()
    node = make_node("cpu-a", features=KEYS)
    e1, e2 = FakeEngine("local", "s1"), FakeEngine("mixed", "s2")
    deploy(owner, "local-m", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    deploy(owner, "mixed-m", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    make_ready("local-m", [(node, e1.port, e1.secret)])
    make_ready("mixed-m", [(node, e2.port, e2.secret)])
    cloud, own = FakeCloud("cloud"), FakeCloud("own", key=None)
    pm = model_id(add_provider(owner, cloud), "gpt-mini")
    opm = model_id(add_provider(owner, own, name="vpn-vllm", kind="openai_compatible", external=False, api_key=None,
                                models=(("qwen-big", "openai-chat"),)), "qwen-big")
    set_tiers(owner, "mixed-m", [[("local", deployment_id(owner, "mixed-m"))], [("provider", pm)]])
    for name, ref in (("cloud-m", pm), ("own-m", opm)):
        r = owner.post("/api/routes", json={"served_name": name, "api": "openai-chat",
                                            "targets": [{"kind": "provider", "provider_model_id": ref}]})
        assert r.status_code == 201, r.text
    keys = {level: api_client(owner, cloud_key(owner, f"k-{level}", level)) for level in ("off", "fallback", "on")}
    ns = SimpleNamespace(owner=owner, node=node, cloud=cloud, own=own, pm=pm, opm=opm, keys=keys, e1=e1, e2=e2)
    yield ns
    for x in (e1, e2, cloud, own):
        x.close()


def chat(c, model, headers=None):
    return c.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
                  headers=headers or {})


def listed(c):
    return [m["id"] for m in c.get("/v1/models").json()["data"]]


def test_fy_ck_001_listing_and_not_found(world):
    """FY-CK-001: a cloud-only model exists only for `on` keys: listed, GET-able and callable; for off
    and fallback it is "not found", exactly like a name that does not exist. A provider inside your
    network is not cloud."""
    k = world.keys
    assert listed(k["off"]) == listed(k["fallback"]) == ["local-m", "mixed-m", "own-m"]
    assert listed(k["on"]) == ["cloud-m", "local-m", "mixed-m", "own-m"]
    for level in ("off", "fallback"):
        r = chat(k[level], "cloud-m")
        nope = chat(k[level], "no-such-model")
        assert r.status_code == nope.status_code == 404
        assert r.json()["error"]["code"] == nope.json()["error"]["code"] == "model_not_found"
        assert k[level].get("/v1/models/cloud-m").status_code == 404
        assert chat(k[level], "own-m").status_code == 200
    assert world.cloud.posts() == []
    assert k["on"].get("/v1/models/cloud-m").json()["id"] == "cloud-m"
    r = chat(k["on"], "cloud-m")
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"
    assert len(world.own.posts()) == 2


def test_fy_ck_002_mixed_routes_spill_only_for_fallback_and_on(world):
    """FY-CK-002: the mixed model's local tier is down: off gets the local error and the provider
    sees nothing; fallback and on spill. With local up, everyone stays local."""
    k = world.keys
    for level in ("off", "fallback", "on"):
        r = chat(k[level], "mixed-m")
        assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:mixed-m#0"
    set_state("mixed-m", 0, "starting", "loading")
    r = chat(k["off"], "mixed-m")
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_ready_replica"
    assert world.cloud.posts() == []
    for level in ("fallback", "on"):
        r = chat(k[level], "mixed-m")
        assert r.status_code == 200 and r.headers["x-forgeyard-spilled"] == "unavailable"
    assert len(world.cloud.posts()) == 2


def test_fy_ck_003_a_local_only_route_never_leaves(world):
    """FY-CK-003: the route's local-only flag beats every key: no spill, and a cloud-only route
    with the flag answers nothing but its refusal."""
    set_tiers(world.owner, "mixed-m", [[("local", deployment_id(world.owner, "mixed-m"))], [("provider", world.pm)]],
              local_only=True)
    set_state("mixed-m", 0, "starting", "loading")
    r = chat(world.keys["on"], "mixed-m")
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_ready_replica"
    routes = {x["served_name"]: x for x in world.owner.get("/api/routes").json()["routes"]}
    rid = routes["cloud-m"]["id"]
    r = world.owner.put(f"/api/routes/{rid}", json={"local_only": True,
                                                    "targets": [{"kind": "provider", "provider_model_id": world.pm}]})
    assert r.status_code == 200
    r = chat(world.keys["on"], "cloud-m")
    assert r.status_code == 400 and r.json()["error"]["code"] == "cloud_only_model"
    assert world.cloud.posts() == []


def test_fy_ck_004_keys_default_off_and_the_cap(world):
    """FY-CK-004: new keys get the server default (off); a user may raise their own key only up to
    the cap (off by default); admins set default and cap (default <= cap), audited."""
    owner = world.owner
    assert new_key(owner, "plain")["api_key"]["cloud"] == "off"
    add_user(owner, "ursula")
    u = sign_in(owner, "ursula", PW)
    assert new_key(u, "mine")["api_key"]["cloud"] == "off"
    r = u.post("/api/keys", json={"name": "x", "cloud": "fallback"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "over_cap"
    assert owner.put("/api/keys/cloud", json={"default": "on", "max": "fallback"}).status_code == 422
    r = owner.put("/api/keys/cloud", json={"default": "off", "max": "fallback"})
    assert r.status_code == 200 and r.json()["cloud"] == {"default": "off", "max": "fallback"}
    k = new_key(u, "spill", cloud="fallback")["api_key"]
    assert k["cloud"] == "fallback"
    r = u.patch(f"/api/keys/{k['id']}", json={"cloud": "on"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "over_cap"
    assert u.get("/api/keys").json()["cloud"] == {"default": "off", "max": "fallback"}
    owner.put("/api/keys/cloud", json={"default": "fallback", "max": "on"})
    db = new_session()
    try:
        # Raising the default changes no existing key.
        assert db.scalar(select(ApiKey.cloud).where(ApiKey.name == "mine")) == "off"
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "api_key_cloud.changed")) is not None
    finally:
        db.close()
    assert new_key(u, "later")["api_key"]["cloud"] == "fallback"


def test_fy_ck_005_an_admins_value_is_locked_for_the_owner(world):
    """FY-CK-005: an admin setting the cloud setting on someone else's key locks it; the owner cannot
    change it (unchanged values sent back are fine); null unlocks it and sets the default."""
    owner = world.owner
    owner.put("/api/keys/cloud", json={"default": "off", "max": "on"})
    add_user(owner, "ursula")
    u = sign_in(owner, "ursula", PW)
    k = new_key(u, "mine")["api_key"]
    r = owner.patch(f"/api/keys/{k['id']}", json={"cloud": "fallback"})
    assert r.status_code == 200 and r.json()["api_key"]["cloud_locked_by"] == "owner"
    r = u.patch(f"/api/keys/{k['id']}", json={"cloud": "on"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "limit_locked"
    assert u.patch(f"/api/keys/{k['id']}", json={"cloud": "fallback", "name": "renamed"}).status_code == 200
    r = owner.patch(f"/api/keys/{k['id']}", json={"cloud": None})
    assert r.json()["api_key"]["cloud"] == "off" and r.json()["api_key"]["cloud_locked_by"] is None
    assert u.patch(f"/api/keys/{k['id']}", json={"cloud": "on"}).json()["api_key"]["cloud"] == "on"
    events = [e for e in owner.get("/api/audit").json()["events"] if e["action"] == "api_key.changed"]
    assert any(e["detail"].get("locked") == ["cloud"] for e in events)
    assert any(e["detail"].get("unlocked") == ["cloud"] for e in events)


def test_fy_ck_006_the_playground_uses_what_the_person_may_give_a_key(world):
    """FY-CK-006: in the playground an admin is `on`; a user gets the cap (off by default), so a
    cloud-only model is not found for them until the cap allows it. /api/routes says which ones are
    usable."""
    owner = world.owner
    r = owner.post("/api/playground/chat/completions", json={"model": "cloud-m", "messages": []})
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"
    add_user(owner, "ursula")
    u = sign_in(owner, "ursula", PW)
    r = u.post("/api/playground/chat/completions", json={"model": "cloud-m", "messages": []})
    assert r.status_code == 404
    usable = {x["served_name"]: x["usable"] for x in u.get("/api/routes").json()["routes"]}
    assert usable == {"cloud-m": False, "local-m": True, "mixed-m": True, "own-m": True}
    owner.put("/api/keys/cloud", json={"default": "off", "max": "on"})
    assert u.post("/api/playground/chat/completions", json={"model": "cloud-m", "messages": []}).status_code == 200
    kinds = {x["name"]: x["kind"] for x in owner.get("/api/keys").json()["models"]}
    assert kinds == {"cloud-m": "cloud", "local-m": "local", "mixed-m": "mixed", "own-m": "local"}


def test_fy_ck_007_existing_keys_are_off_after_the_upgrade(owner):
    """FY-CK-007: migration 0009 gives every key from before it `cloud = off`."""
    from alembic import command

    from forgeyard.db import get_engine
    from forgeyard.migrate import alembic_config, upgrade

    new_key(owner, "old-key")
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0008_encrypt_secrets")
    upgrade()
    db = new_session()
    try:
        assert db.scalar(select(ApiKey.cloud).where(ApiKey.name == "old-key")) == "off"
    finally:
        db.close()
    assert json.dumps(owner.get("/api/keys").json()["keys"][0]["cloud"]) == '"off"'


def test_fy_ck_008_fallback_uses_the_cloud_only_after_a_local_tier(world):
    """FY-CK-008: on a route whose first tier is the cloud and second is local, a `fallback` key
    skips the cloud (it is not a spill target there) and an `on` key uses it."""
    set_tiers(world.owner, "mixed-m", [[("provider", world.pm)], [("local", deployment_id(world.owner, "mixed-m"))]])
    r = chat(world.keys["fallback"], "mixed-m")
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:mixed-m#0"
    assert world.cloud.posts() == []
    r = chat(world.keys["on"], "mixed-m")
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"


def test_fy_ck_009_a_stopped_deployment_with_a_cloud_tier_stays_listed_for_cloud_keys(world):
    """FY-CK-009: with its deployment stopped, the mixed model leaves /v1/models for `off` keys (and
    for everyone once the route is local-only), but stays listed, and spills, for keys that may use
    the cloud. A provider inside your network keeps a route listed whatever the key."""
    o = world.owner
    assert o.post(f"/api/deployments/{deployment_id(o, 'mixed-m')}/stop").status_code == 200
    k = world.keys
    assert listed(k["off"]) == ["local-m", "own-m"]
    assert k["off"].get("/v1/models/mixed-m").status_code == 404
    assert listed(k["fallback"]) == ["local-m", "mixed-m", "own-m"]
    r = chat(k["fallback"], "mixed-m")
    assert r.status_code == 200 and r.headers["x-forgeyard-spilled"] == "unavailable"
    set_tiers(o, "mixed-m", [[("local", deployment_id(o, "mixed-m"))], [("provider", world.pm)]], local_only=True)
    assert "mixed-m" not in listed(k["on"])
    set_tiers(o, "local-m", [[("local", deployment_id(o, "local-m"))], [("provider", world.opm)]])
    assert o.post(f"/api/deployments/{deployment_id(o, 'local-m')}/stop").status_code == 200
    assert "local-m" in listed(k["off"])


def usage_rows(model):
    from forgeyard.db import UsageDaily
    from forgeyard.gateway.usage import recorder
    recorder.flush()
    db = new_session()
    try:
        return {(r.target, r.requests, r.errors) for r in db.scalars(select(UsageDaily).where(UsageDaily.model == model))}
    finally:
        db.close()


def test_fy_ck_010_v1_models_follows_the_tiers_a_key_may_use(world):
    """FY-CK-010: a `fallback` key reaches the cloud only after a local tier. A route whose cloud tier
    comes first and whose only deployment is stopped can answer nothing for it, so it is not listed;
    an `on` key still gets it, and both agree with what a call answers."""
    o, k = world.owner, world.keys
    set_tiers(o, "mixed-m", [[("provider", world.pm)], [("local", deployment_id(o, "mixed-m"))]])
    assert "mixed-m" in listed(k["fallback"])                # the deployment still runs
    assert o.post(f"/api/deployments/{deployment_id(o, 'mixed-m')}/stop").status_code == 200
    assert "mixed-m" not in listed(k["fallback"]) and k["fallback"].get("/v1/models/mixed-m").status_code == 404
    assert chat(k["fallback"], "mixed-m").status_code == 503
    assert "mixed-m" in listed(k["on"])
    assert chat(k["on"], "mixed-m").status_code == 200


def test_fy_ck_011_an_upstream_retry_after_reaches_the_client(world):
    """FY-CK-011: when a replica answers 429 with its own Retry-After and nothing else may take the
    request, the client gets that Retry-After with the 429, so it backs off as long as the engine
    asked."""
    world.e2.status, world.e2.retry_after = 429, "7"
    r = chat(world.keys["off"], "mixed-m")
    assert r.status_code == 429 and r.headers.get("retry-after") == "7"
    assert r.headers["x-forgeyard-target"] == "local:mixed-m#0"
    world.e2.status = None
    r = chat(world.keys["off"], "mixed-m")
    assert r.status_code == 200 and "retry-after" not in r.headers


def test_fy_ck_012_budgets_count_refusals_for_the_provider_and_ignore_providers_inside_the_network(world):
    """FY-CK-012: a cloud-only request a budget refused before anything was sent is counted for that
    provider (at no cost), not as local. A provider inside your network costs nothing, so it takes
    no budget (422); clearing one is still allowed."""
    o, k = world.owner, world.keys
    providers = {p["name"]: p for p in o.get("/api/providers").json()["providers"]}
    assert o.put(f"/api/budgets/providers/{providers['cloudy']['id']}", json={"budget_micros": 0}).status_code == 200
    r = chat(k["on"], "cloud-m")
    assert r.status_code == 402 and r.json()["error"]["code"] in ("budget_exhausted", "budget_unpriced")
    assert world.cloud.posts() == []
    assert usage_rows("cloud-m") == {(f"pm:{world.pm}", 1, 1)}
    r = o.put(f"/api/budgets/providers/{providers['vpn-vllm']['id']}", json={"budget_micros": 5_000_000})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "not_cloud"
    assert o.put(f"/api/budgets/providers/{providers['vpn-vllm']['id']}", json={"budget_micros": None}).status_code == 200


def test_fy_ck_013_lowering_the_cap_applies_at_once_to_peoples_own_keys(world):
    """FY-CK-013: a person's own key set to "allowed" works as "never" from the moment an admin lowers
    the most people may give their keys: no cloud-only model, no spill. The key keeps its own setting
    (shown with its effective one) and gets it back when the cap is raised. A value an admin set, and
    an admin's own key, are not capped."""
    o, k = world.owner, world.keys
    assert o.put("/api/keys/cloud", json={"default": "off", "max": "on"}).status_code == 200
    add_user(o, "ursula")
    u = sign_in(o, "ursula", PW)
    mine = api_client(o, new_key(u, "mine", cloud="on")["key"])
    by_admin = new_key(u, "by-admin")["api_key"]
    assert o.patch(f"/api/keys/{by_admin['id']}", json={"cloud": "on"}).status_code == 200
    assert "cloud-m" in listed(mine)
    assert o.put("/api/keys/cloud", json={"default": "off", "max": "off"}).status_code == 200
    assert "cloud-m" not in listed(mine) and chat(mine, "cloud-m").status_code == 404
    set_state("mixed-m", 0, "starting", "loading")
    r = chat(mine, "mixed-m")
    assert r.status_code == 503 and world.cloud.posts() == []
    rows = {x["name"]: x for x in u.get("/api/keys").json()["keys"]}
    assert rows["mine"]["cloud"] == "on" and rows["mine"]["effective_cloud"] == "off"
    assert rows["by-admin"]["effective_cloud"] == "on"           # an admin's value stays
    assert "cloud-m" in listed(k["on"])                           # an admin's own key stays
    assert o.put("/api/keys/cloud", json={"default": "off", "max": "on"}).status_code == 200
    assert "cloud-m" in listed(mine)
