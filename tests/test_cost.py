"""Cost tracking (1.2): the cost of each request from its usage and the provider model's prices,
the upstream's own figure first, unknown prices never 0, integer micro-USD; usage rows and the usage
API with cost per group and the local/cloud split; who may set prices; and the refresh from
OpenRouter's model list (a local stand-in: preview only, confirm to apply, manual prices kept)
(FY-CST-*)."""

from datetime import datetime, timezone

import pytest
from alembic import command
from sqlalchemy import text

from forgeyard.config import get_settings
from forgeyard.db import ProviderModel, get_engine, new_session
from forgeyard.gateway import egress, pricing
from forgeyard.gateway.pricing import Prices, cost_of
from forgeyard.gateway.quirks import Usage
from forgeyard.gateway.usage import Recorder, recorder, summary
from forgeyard.migrate import alembic_config, upgrade
from tests.cloud_helpers import FakeCatalog, FakeCloud, add_provider, cloud_key, model_id, or_model, set_tiers
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import api_client, set_state
from tests.test_routing import chat, mixed  # noqa: F401  (the fixture)

PW = "quiet-meadow-4821"
# micro-USD per million tokens: $2.50 in, $10 out, $1.25 cached, $3.125 written to the cache
GPT = {"price_input": 2_500_000, "price_output": 10_000_000, "price_cache_read": 1_250_000}


def usage_of(c, group="model", everyone=True) -> dict:
    recorder.flush()
    return c.get(f"/api/usage?group={group}{'&everyone=true' if everyone else ''}").json()


# ------------------------------------------------------------------ the formula
def test_fy_cst_001_the_formula():
    """FY-CST-001: uncached prompt x input + cached x cache read + cache writes x cache write +
    completion x output, in pico-USD; reasoning tokens are inside the completion (not added again);
    embeddings need only the input price."""
    p = Prices(input=2_500_000, output=10_000_000, cache_read=1_250_000, cache_write=3_125_000)
    c = cost_of(Usage(prompt=1000, completion=500, cached=400, reasoning=300, cache_write=100), p, cloud=True, served=True)
    assert c.method == pricing.COMPUTED
    assert c.picos == 500 * 2_500_000 + 400 * 1_250_000 + 100 * 3_125_000 + 500 * 10_000_000
    assert c.micros == 7063                                   # $0.0070625, half up
    # The same request without reasoning tokens costs the same: they are billed once, as output.
    assert cost_of(Usage(prompt=1000, completion=500, cached=400, cache_write=100), p, cloud=True,
                   served=True).picos == c.picos
    emb = cost_of(Usage(prompt=3), Prices(input=20_000), cloud=True, served=True)
    assert (emb.picos, emb.method, emb.micros) == (60_000, pricing.COMPUTED, 0)


def test_fy_cst_002_unknown_is_not_zero_and_the_upstreams_figure_wins():
    """FY-CST-002: a missing input or output price makes the cost unknown (None, "unpriced"), never 0;
    a missing cache price falls back to the input price as an estimate, and so does a provider that
    does not report cached tokens; the upstream's reported cost wins over the prices; local targets
    cost 0; no tokens: unpriced if it was answered, 0 if it was refused."""
    p = Prices(input=1_000_000, output=2_000_000)
    for u, prices in ((Usage(prompt=10, completion=5), Prices(input=1_000_000)),
                      (Usage(prompt=10, completion=5), Prices(output=2_000_000)),
                      (Usage(prompt=10, completion=5), None)):
        c = cost_of(u, prices, cloud=True, served=True)
        assert c.picos is None and c.micros is None and c.method == pricing.UNPRICED
    c = cost_of(Usage(prompt=10, completion=5, cached=4), p, cloud=True, served=True)
    assert (c.picos, c.method) == (10 * 1_000_000 + 5 * 2_000_000, pricing.ESTIMATED)
    c = cost_of(Usage(prompt=10, completion=5), p, cloud=True, served=True, reports_cached=False)
    assert (c.picos, c.method) == (20_000_000, pricing.ESTIMATED)
    c = cost_of(Usage(prompt=10, completion=5), p, cloud=True, served=True)
    assert (c.picos, c.method) == (20_000_000, pricing.COMPUTED)
    c = cost_of(Usage(prompt=10, completion=5, cost=0.000123), p, cloud=True, served=True)
    assert (c.picos, c.micros, c.method) == (123_000_000, 123, pricing.REPORTED)
    c = cost_of(Usage(prompt=10, completion=5, cost=0.0000425), None, cloud=True, served=True)
    assert (c.micros, c.method) == (43, pricing.REPORTED)       # 42.5 micro-USD, half up
    assert cost_of(Usage(prompt=10, completion=5), None, cloud=False, served=True) == pricing.Cost(0, pricing.LOCAL)
    assert cost_of(Usage(), p, cloud=True, served=True).method == pricing.UNPRICED
    assert cost_of(Usage(), p, cloud=True, served=False) == pricing.Cost(0, pricing.NONE)
    assert [pricing.micros_of(x) for x in (0, 499_999, 500_000, 1_499_999, 1_500_000)] == [0, 0, 1, 1, 2]


def test_fy_cst_003_usage_rows_add_cost_in_micro_usd(client):
    """FY-CST-003: rows sum the cost in micro-USD, rounding what is written to the nearest micro-dollar
    and carrying the rest into the next write (many cheap requests add up exactly); unpriced requests
    are counted apart and add nothing; reported and estimated ones are counted."""
    t = [1_762_732_800.0]  # 2025-11-10 00:00 UTC
    rec = Recorder(clock=lambda: t[0])

    def one(picos, method=pricing.COMPUTED, target="pm:1"):
        rec.record(source="api", key_id=1, user_id=1, username="owner", model="gpt", deployment_id=None, status=200,
                   latency_ms=5, prompt_tokens=10, completion_tokens=2, target=target, cost_picos=picos,
                   cost_method=method)

    one(1_500_000)                       # 1.5 micro-USD: written as 2, -0.5 carried
    rec.flush()
    one(1_500_000)                       # 1.5 - 0.5 = 1
    one(None, pricing.UNPRICED)
    one(400_000, pricing.REPORTED)
    one(400_000, pricing.ESTIMATED)       # 0.8 more: 1.8, written as 2, -0.2 carried
    rec.flush()
    for _ in range(5):                   # 5 x 0.3 = 1.5 - 0.2 = 1.3: 1
        one(300_000)
    one(0, pricing.LOCAL, target="local")
    rec.flush()
    db = new_session()
    try:
        s = summary(db, days=1, group_by="target", today="2025-11-10")
        rows = {r["id"]: r for r in s["rows"]}
        assert rows["pm:1"]["cost_micros"] == 2 + 2 + 1         # 3.5 + 0.8 + 1.5 = 5.8 micro-USD
        assert (rows["pm:1"]["unpriced"], rows["pm:1"]["cost_reported"], rows["pm:1"]["cost_estimated"]) == (1, 1, 1)
        assert rows["local"]["cost_micros"] == 0 and rows["local"]["unpriced"] == 0
        assert s["total"]["cost_micros"] == 5 and s["total"]["requests"] == 11
        raw = db.execute(text("SELECT cost_micros FROM usage_daily WHERE target = 'pm:1'")).scalar()
        assert isinstance(raw, int) and raw == 5
    finally:
        db.close()


# ------------------------------------------------------------------ through the gateway
@pytest.fixture()
def priced(mixed):  # noqa: F811
    r = mixed.owner.patch(f"/api/provider-models/{mixed.pm}", json=GPT)
    assert r.status_code == 200, r.text
    m = r.json()["model"]
    assert m["price_source"] == "manual" and m["price_checked_at"] and m["price_input"] == 2_500_000
    return mixed


def test_fy_cst_004_cloud_requests_are_priced_local_ones_cost_nothing(priced):
    """FY-CST-004: a request that spilled to the provider costs (11 - 4 cached) x $2.50 + 4 x $1.25 +
    7 x $10 per million = 92.5 micro-USD; streamed ones are priced from the final usage chunk the
    same way; a local answer costs 0; the totals split local from cloud."""
    m = priced
    assert chat(m.api).headers["x-forgeyard-target"] == "local:chat#0"
    set_state("chat", 0, "starting", "loading")
    assert chat(m.api).headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"
    r = chat(m.api, stream=True)
    assert r.status_code == 200 and "data: [DONE]" in r.text
    out = usage_of(m.owner, "target")
    rows = {x["id"]: x for x in out["rows"]}
    assert rows[f"pm:{m.pm}"]["requests"] == 2 and rows[f"pm:{m.pm}"]["cost_micros"] == 185
    assert rows["local"]["requests"] == 1 and rows["local"]["cost_micros"] == 0
    split = out["total"]["split"]
    assert (split["cloud"]["requests"], split["cloud"]["cost_micros"]) == (2, 185)
    assert (split["local"]["requests"], split["local"]["cost_micros"]) == (1, 0)
    assert out["total"]["cost_micros"] == 185 and out["total"]["unpriced"] == 0
    for group in ("day", "key", "user", "model", "provider"):
        g = usage_of(m.owner, group)
        assert sum(x["cost_micros"] for x in g["rows"]) == 185, group
    prov = {x["id"]: x for x in usage_of(m.owner, "provider")["rows"]}
    assert prov[f"p:{m.provider['id']}"]["name"] == "cloudy" and prov[f"p:{m.provider['id']}"]["cloud"] is True
    assert prov["local"]["name"] == "Local replicas" and prov["local"]["cloud"] is False


def test_fy_cst_005_unpriced_reported_and_estimated(mixed):  # noqa: F811
    """FY-CST-005: without prices a cloud request is counted as unpriced with no cost (not 0 as if
    known); OpenRouter's reported cost is taken as is; an Anthropic-kind provider (no cached tokens
    reported) is priced as uncached and counted as an estimate; a provider refusing (429) costs 0."""
    m = mixed
    set_state("chat", 0, "starting", "loading")
    chat(m.api)
    out = usage_of(m.owner, "target")
    row = next(x for x in out["rows"] if x["id"] == f"pm:{m.pm}")
    assert (row["unpriced"], row["cost_micros"]) == (1, 0) and out["total"]["unpriced"] == 1
    m.cloud.usage = {"prompt_tokens": 11, "completion_tokens": 7, "cost": 0.00042}
    chat(m.api)
    row = next(x for x in usage_of(m.owner, "target")["rows"] if x["id"] == f"pm:{m.pm}")
    assert (row["unpriced"], row["cost_reported"], row["cost_micros"]) == (1, 1, 420)
    m.cloud.statuses = [429]
    m.owner.patch(f"/api/provider-models/{m.pm}", json=GPT)
    chat(m.api)
    row = next(x for x in usage_of(m.owner, "target")["rows"] if x["id"] == f"pm:{m.pm}")
    assert (row["requests"], row["unpriced"], row["cost_micros"]) == (3, 1, 420)

    anth = FakeCloud("claude", key="sk-ant-test-abcdef0123456789", style="anthropic")
    try:
        p = add_provider(m.owner, anth, name="claude", kind="anthropic", models=(("claude-x", "openai-chat"),))
        pm = model_id(p, "claude-x")
        m.owner.patch(f"/api/provider-models/{pm}", json={"price_input": 3_000_000, "price_output": 15_000_000})
        set_tiers(m.owner, "chat", [[("local", m.dep)], [("provider", pm)]])
        chat(m.api)
        row = next(x for x in usage_of(m.owner, "target")["rows"] if x["id"] == f"pm:{pm}")
        # 13 x $3 + 5 x $15 per million = 114 micro-USD, flagged as an estimate
        assert (row["cost_micros"], row["cost_estimated"], row["unpriced"]) == (114, 1, 0)
    finally:
        anth.close()


def test_fy_cst_006_a_provider_inside_your_network_costs_nothing(owner, monkeypatch):
    """FY-CST-006: a provider marked as inside your network is local: 0, even with prices, and on the
    local side of the split."""
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    own = FakeCloud("own")
    try:
        p = add_provider(owner, own, name="vpn-vllm", kind="openai_compatible", external=False, allow_http=True)
        pm = model_id(p, "gpt-mini")
        owner.patch(f"/api/provider-models/{pm}", json=GPT)
        r = owner.post("/api/routes", json={"served_name": "own", "api": "openai-chat",
                                            "targets": [{"kind": "provider", "provider_model_id": pm}]})
        assert r.status_code == 201, r.text
        api = api_client(owner, cloud_key(owner, "k", "off"))
        assert api.post("/v1/chat/completions", json={"model": "own", "messages": []}).status_code == 200
        out = usage_of(owner, "target")
        assert out["total"]["cost_micros"] == 0 and out["total"]["unpriced"] == 0
        assert out["total"]["split"]["local"]["requests"] == 1 and out["total"]["split"]["cloud"]["requests"] == 0
    finally:
        own.close()


def test_fy_cst_007_the_migration_adds_cost_to_old_rows(owner):
    """FY-CST-007: usage rows and provider models from before 0010 come through the upgrade with cost
    0 and nothing unpriced, and the downgrade drops the new columns again."""
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0009_providers")
    with get_engine().begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(usage_daily)"))} \
            if conn.dialect.name == "sqlite" else \
            {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns "
                                             "WHERE table_name = 'usage_daily'"))}
        assert "cost_micros" not in cols and "cached_tokens" in cols
        conn.execute(text("INSERT INTO usage_daily (day, source, key_id, user_id, username, model, target, requests, "
                          "errors, limited, prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens, spilled, "
                          "latency_ms_sum, latency_buckets) VALUES ('2026-11-01', 'api', 1, 1, 'owner', 'gpt', 'pm:9', "
                          "4, 0, 0, 100, 50, 0, 0, 0, 40, '[]')"))
    upgrade()
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT requests, cost_micros, unpriced, cost_reported, cost_estimated "
                                "FROM usage_daily WHERE model = 'gpt'")).one()
    assert tuple(row) == (4, 0, 0, 0, 0)


# ------------------------------------------------------------------ prices: who may set them
def test_fy_cst_008_only_admins_set_and_refresh_prices(owner, monkeypatch):
    """FY-CST-008: users see prices but cannot change them or refresh them; an admin's edit is
    "manual" with a date; clearing every price clears the source; prices are never negative."""
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    cloud = FakeCloud("stub")
    try:
        p = add_provider(owner, cloud)
        pm = model_id(p, "gpt-mini")
        add_user(owner, "bob")
        bob = sign_in(owner, "bob", PW)
        assert bob.patch(f"/api/provider-models/{pm}", json={"price_input": 1}).status_code == 403
        assert bob.post("/api/prices/openrouter/preview", json={}).status_code == 403
        assert bob.post("/api/prices/openrouter/apply", json={"items": [{"model_id": pm, "ref": "x/y"}]}).status_code == 403
        assert owner.patch(f"/api/provider-models/{pm}", json={"price_input": -1}).status_code == 422
        m = owner.patch(f"/api/provider-models/{pm}", json=GPT).json()["model"]
        assert (m["price_source"], m["price_input"], m["price_cache_write"]) == ("manual", 2_500_000, None)
        listed = bob.get("/api/providers").json()["providers"][0]["models"][0]
        assert listed["price_output"] == 10_000_000 and listed["price_source"] == "manual"
        m = owner.patch(f"/api/provider-models/{pm}", json={k: None for k in pricing.PRICE_FIELDS}).json()["model"]
        assert (m["price_source"], m["price_checked_at"], m["price_input"]) == (None, None, None)
        events = [e for e in owner.get("/api/audit").json()["events"] if e["action"] == "provider.model_changed"]
        assert events and "price_input" in events[0]["detail"]["changes"]
    finally:
        cloud.close()


# ------------------------------------------------------------------ the refresh from OpenRouter
@pytest.fixture()
def catalog(owner, monkeypatch):
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    cat = FakeCatalog([
        or_model("openai/gpt-mini", "0.0000025", "0.00001", read="0.00000125"),
        or_model("anthropic/claude-sonnet-4.5", "0.000003", "0.000015", read="0.0000003", write="0.00000375"),
        or_model("meta/llama-x", "0.0000001", "0.0000002"),
        or_model("openrouter/auto", "-1", "-1"),
    ])
    monkeypatch.setenv("OPENROUTER_MODELS_URL", cat.url)
    get_settings.cache_clear()
    egress.reset()
    cloud = FakeCloud("stub")
    yield cat, cloud
    cat.close()
    cloud.close()
    egress.reset()


def _ids(owner, cloud):
    oa = add_provider(owner, cloud, name="oa", kind="openai", models=(("gpt-mini", "openai-chat"), ("other", "openai-chat")))
    an = add_provider(owner, cloud, name="an", kind="anthropic", models=(("claude-sonnet-4-5-20250929", "openai-chat"),))
    orr = add_provider(owner, cloud, name="or", kind="openrouter", models=(("meta/llama-x", "openai-chat"),))
    return model_id(oa, "gpt-mini"), model_id(oa, "other"), model_id(an, "claude-sonnet-4-5-20250929"), \
        model_id(orr, "meta/llama-x"), oa["id"]


def _db_prices(pm_id):
    db = new_session()
    try:
        pm = db.get(ProviderModel, pm_id)
        return pm.price_input, pm.price_output, pm.price_cache_read, pm.price_cache_write, pm.price_source, pm.price_ref
    finally:
        db.close()


def _item(row, **extra):
    return {"model_id": row["model_id"], "ref": row["ref"], **row["after"], **extra}


def test_fy_cst_009_refresh_matches_previews_and_applies_only_what_is_confirmed(owner, catalog):
    """FY-CST-009: the preview reads OpenRouter's list without a key, matches an OpenRouter
    provider's model exactly and suggests vendor matches ("openai/gpt-mini", the Anthropic name with
    its date and version dashes), shows old -> new, and changes nothing; applying sets the prices
    from per-token strings, the source "openrouter", the date and the confirmed id; a match the admin
    picks by hand is "confirmed"."""
    cat, cloud = catalog
    gpt, other, claude, llama, oa = _ids(owner, cloud)
    r = owner.post("/api/prices/openrouter/preview", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    rows = {x["model_id"]: x for x in out["rows"]}
    assert out["listed"] == 3 and out["matched"] == 3 and out["changed"] == 3
    assert cat.seen and not {"authorization", "x-api-key"} & {k.lower() for k in cat.seen[-1]["headers"]}
    assert (rows[llama]["match"], rows[llama]["ref"]) == ("exact", "meta/llama-x")
    assert (rows[gpt]["match"], rows[gpt]["ref"]) == ("suggested", "openai/gpt-mini")
    assert (rows[claude]["match"], rows[claude]["ref"]) == ("suggested", "anthropic/claude-sonnet-4.5")
    assert rows[other]["match"] == "none" and rows[other]["after"] is None and rows[other]["changed"] is False
    assert rows[gpt]["before"] == {k: None for k in pricing.PRICE_FIELDS}
    assert rows[gpt]["after"] == {"price_input": 2_500_000, "price_output": 10_000_000, "price_cache_read": 1_250_000,
                                  "price_cache_write": None}
    assert rows[claude]["after"]["price_cache_write"] == 3_750_000
    assert _db_prices(gpt) == (None,) * 6                              # the preview changed nothing
    r = owner.post("/api/prices/openrouter/apply", json={"items": [_item(rows[gpt]), _item(rows[llama])]})
    assert r.status_code == 200 and sorted(r.json()["applied"]) == sorted([gpt, llama]) and r.json()["skipped"] == []
    assert _db_prices(gpt) == (2_500_000, 10_000_000, 1_250_000, None, "openrouter", "openai/gpt-mini")
    assert _db_prices(llama)[:2] == (100_000, 200_000) and _db_prices(claude) == (None,) * 6
    m = next(x for x in owner.get(f"/api/providers/{oa}").json()["provider"]["models"] if x["id"] == gpt)
    assert m["price_source"] == "openrouter" and m["price_ref"] == "openai/gpt-mini"
    assert datetime.fromisoformat(m["price_checked_at"].replace("Z", "+00:00")).date() == datetime.now(timezone.utc).date()
    # Once applied the match is confirmed; one provider only; a hand-picked match.
    out = owner.post("/api/prices/openrouter/preview", json={"provider_id": oa, "refs": {str(other): "meta/llama-x"}}).json()
    rows = {x["model_id"]: x for x in out["rows"]}
    assert set(rows) == {gpt, other}
    assert (rows[gpt]["match"], rows[gpt]["changed"]) == ("confirmed", False)
    assert (rows[other]["match"], rows[other]["ref"], rows[other]["after"]["price_input"]) == ("confirmed", "meta/llama-x", 100_000)
    events = [e for e in owner.get("/api/audit").json()["events"] if e["action"] == "provider.prices_refreshed"]
    assert events and events[0]["detail"]["count"] == 2


def test_fy_cst_010_manual_prices_are_kept_and_stale_rows_skipped(owner, catalog):
    """FY-CST-010: a manual price is kept unless the admin ticks it to be overwritten; a row whose
    prices changed on OpenRouter since the preview is skipped; an id OpenRouter does not list is
    skipped."""
    cat, cloud = catalog
    gpt, other, claude, llama, oa = _ids(owner, cloud)
    owner.patch(f"/api/provider-models/{gpt}", json={"price_input": 1, "price_output": 2})
    rows = {x["model_id"]: x for x in owner.post("/api/prices/openrouter/preview", json={}).json()["rows"]}
    assert rows[gpt]["manual"] is True and rows[gpt]["before"]["price_input"] == 1
    r = owner.post("/api/prices/openrouter/apply", json={"items": [_item(rows[gpt])]}).json()
    assert r["applied"] == [] and r["skipped"] == [{"model_id": gpt, "reason": "manual"}]
    assert _db_prices(gpt)[:2] == (1, 2) and _db_prices(gpt)[4] == "manual"
    r = owner.post("/api/prices/openrouter/apply", json={"items": [_item(rows[gpt], overwrite_manual=True)]}).json()
    assert r["applied"] == [gpt] and _db_prices(gpt)[4] == "openrouter"
    cat.models[2] = or_model("meta/llama-x", "0.0000009", "0.0000002")
    r = owner.post("/api/prices/openrouter/apply", json={"items": [_item(rows[llama])]}).json()
    assert r["skipped"] == [{"model_id": llama, "reason": "changed"}] and _db_prices(llama) == (None,) * 6
    r = owner.post("/api/prices/openrouter/apply", json={"items": [{**_item(rows[llama]), "ref": "nope/none"}]}).json()
    assert r["skipped"] == [{"model_id": llama, "reason": "not_listed"}]


def test_fy_cst_011_refresh_failures_change_nothing(owner, catalog, monkeypatch):
    """FY-CST-011: OpenRouter answering an error, something that is not JSON, a list without prices,
    nothing listening, or a URL the egress rules refuse: an error to show, and no price changes."""
    cat, cloud = catalog
    gpt, *_ = _ids(owner, cloud)
    cat.status = 500
    r = owner.post("/api/prices/openrouter/preview", json={})
    assert r.status_code == 502 and r.json()["detail"]["code"] == "catalog_error"
    cat.status, cat.body = 200, b"<html>busy</html>"
    assert owner.post("/api/prices/openrouter/preview", json={}).json()["detail"]["code"] == "catalog_unreadable"
    cat.body = b'{"data": [{"id": "a/b"}]}'
    assert owner.post("/api/prices/openrouter/apply", json={"items": [{"model_id": gpt, "ref": "a/b"}]}) \
        .json()["detail"]["code"] == "catalog_unreadable"
    cat.close()
    egress.reset()                       # no kept-alive connection to the closed stand-in
    assert owner.post("/api/prices/openrouter/preview", json={}).json()["detail"]["code"] == "catalog_unreachable"
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "false")
    get_settings.cache_clear()
    r = owner.post("/api/prices/openrouter/preview", json={})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "egress_refused"
    assert _db_prices(gpt) == (None,) * 6
    assert owner.post("/api/prices/openrouter/preview", json={"provider_id": 999}).status_code == 404


def test_fy_cst_012_reading_openrouters_list():
    """FY-CST-012: USD per token as strings -> micro-USD per million tokens exactly; "-1" (a router's
    variable price) and junk are unknown; models without prices are left out; name spellings."""
    assert pricing.per_token_to_price("0.00000015") == 150_000
    assert pricing.per_token_to_price("0.0000025") == 2_500_000
    assert pricing.per_token_to_price("0") == 0 and pricing.per_token_to_price(0.00003) == 30_000_000
    for junk in ("-1", "abc", None, True, "NaN", "inf", ""):
        assert pricing.per_token_to_price(junk) is None, junk
    known = pricing.catalog({"data": [or_model("a/b", "0.000001", "0.000002", read="0.0000001"),
                                      or_model("r/auto", "-1", "-1"), {"id": "c/d"}, "junk", {"pricing": {}}]})
    assert known == {"a/b": Prices(input=1_000_000, output=2_000_000, cache_read=100_000)}
    assert pricing.catalog("junk") == {} and pricing.catalog({"data": "x"}) == {}
    assert pricing.candidates("claude-sonnet-4-5-20250929") == ["claude-sonnet-4-5-20250929", "claude-sonnet-4-5",
                                                                "claude-sonnet-4.5-20250929", "claude-sonnet-4.5"]
    assert pricing.candidates("gpt-4o-2024-08-06")[:2] == ["gpt-4o-2024-08-06", "gpt-4o"]
    assert pricing.candidates("claude-3-5-haiku-latest")[-1] == "claude-3.5-haiku-latest"
    known = {"openai/gpt-4o": Prices(input=1), "x/y": Prices(input=2)}
    assert pricing.match("openai", "openai", "gpt-4o-2024-08-06", None, known) == ("openai/gpt-4o", "suggested")
    assert pricing.match("openai_compatible", "", "x/y", None, known) == ("x/y", "suggested")
    assert pricing.match("openrouter", "", "openai/gpt-4o", None, known) == ("openai/gpt-4o", "exact")
    assert pricing.match("openrouter", "", "gpt-4o", None, known) == (None, "none")
    assert pricing.match("openai", "openai", "gpt-4o", "x/y", known) == ("x/y", "confirmed")
    assert pricing.match("openai", "openai", "gpt-4o", "gone/away", known) == ("gone/away", "not_listed")
