"""Cloud providers (1.2): admins only, the key write-only and encrypted (never in an answer, the
audit log or the logs, never copied by a clone), test connection and model import against a stand-in,
and the egress rules: https, no redirects, no link-local or metadata addresses, private addresses only
inside your network, the host allowlist, the proxy and TLS verification (FY-PRV-*)."""

import json
import logging

import pytest
from sqlalchemy import text

from forgeyard.config import get_settings
from forgeyard.db import get_engine
from forgeyard.gateway import egress
from forgeyard.security import tls
from tests.cloud_helpers import CLOUD_KEY, FakeCloud, add_provider, cloud_key, model_id
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import api_client

PW = "quiet-meadow-4821"


@pytest.fixture()
def cloud(monkeypatch):
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    c = FakeCloud("stub")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def fresh_egress():
    egress.reset()
    yield
    egress.reset()


def cloud_route(owner, name, pm):
    r = owner.post("/api/routes", json={"served_name": name, "api": "openai-chat",
                                        "targets": [{"kind": "provider", "provider_model_id": pm}]})
    assert r.status_code == 201, r.text
    return r.json()["route"]


def test_fy_prv_001_the_key_is_write_only_and_encrypted(owner, cloud, caplog):
    """FY-PRV-001: the key never comes back (last four characters only), is encrypted in the
    database, and appears in no answer, audit event or log line, even when the provider fails."""
    caplog.set_level(logging.DEBUG)
    p = add_provider(owner, cloud)
    assert p["has_key"] and p["key_hint"] == "…" + CLOUD_KEY[-4:] and "api_key" not in p
    with get_engine().connect() as conn:
        stored = conn.execute(text("SELECT api_key FROM providers")).scalar()
    assert stored.startswith("fy1:") and CLOUD_KEY not in stored
    cloud_route(owner, "gpt", model_id(p, "gpt-mini"))
    on = api_client(owner, cloud_key(owner, "on", "on"))
    assert on.post("/v1/chat/completions", json={"model": "gpt", "messages": []}).status_code == 200
    cloud.status = 500
    on.post("/v1/chat/completions", json={"model": "gpt", "messages": []})
    assert owner.post(f"/api/providers/{p['id']}/test").json()["ok"] is True
    owner.patch(f"/api/providers/{p['id']}", json={"api_key": "sk-new-secret-value-7777"})
    seen = json.dumps([owner.get("/api/providers").json(), owner.get(f"/api/providers/{p['id']}").json(),
                       owner.get("/api/audit").json(), owner.get("/api/routes").json()])
    for secret in (CLOUD_KEY, "sk-new-secret-value-7777"):
        assert secret not in seen and secret not in caplog.text
    changed = [e for e in owner.get("/api/audit").json()["events"] if e["action"] == "provider.changed"]
    assert changed[0]["detail"]["changes"]["api_key"] == "replaced"
    assert owner.get(f"/api/providers/{p['id']}").json()["provider"]["key_hint"] == "…7777"


def test_fy_prv_002_admins_only_and_a_clone_has_no_key(owner, cloud):
    """FY-PRV-002: users and operators may list providers (no headers) but not change, test or
    clone them; a clone copies settings and models, never the key, and starts disabled."""
    p = add_provider(owner, cloud, headers={"X-Title": "forgeyard"})
    add_user(owner, "olga", role="operator")
    o = sign_in(owner, "olga", PW)
    listing = o.get("/api/providers").json()
    assert listing["providers"][0]["name"] == "cloudy" and "headers" not in listing["providers"][0]
    for call in (lambda: o.post("/api/providers", json={"name": "x", "kind": "openai"}),
                 lambda: o.patch(f"/api/providers/{p['id']}", json={"enabled": False}),
                 lambda: o.post(f"/api/providers/{p['id']}/test"),
                 lambda: o.post(f"/api/providers/{p['id']}/clone", json={"name": "copy"}),
                 lambda: o.delete(f"/api/providers/{p['id']}")):
        assert call().status_code == 403
    r = owner.post(f"/api/providers/{p['id']}/clone", json={"name": "copy"})
    c = r.json()["provider"]
    assert r.status_code == 201 and c["has_key"] is False and c["key_hint"] is None and c["enabled"] is False
    assert [m["upstream_model"] for m in c["models"]] == ["gpt-mini"] and c["headers"] == {"X-Title": "forgeyard"}
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT api_key FROM providers WHERE name = 'copy'")).scalar() is None


def test_fy_prv_003_validation(owner, monkeypatch):
    """FY-PRV-003: what a provider may look like: https (http only inside your network with the
    box ticked), no credentials in the URL, no secret headers, a known kind, a base URL for Azure."""
    def make(**body):
        return owner.post("/api/providers", json={"name": "p", "kind": "openai", **body})

    assert make(base_url="http://api.example.com/v1").json()["detail"]["code"] == "bad_base_url"
    assert make(base_url="https://user:pw@api.example.com/v1").json()["detail"]["code"] == "bad_base_url"
    assert make(base_url="ftp://api.example.com").json()["detail"]["code"] == "bad_base_url"
    assert make(allow_http=True, base_url="http://10.0.0.5:8000/v1").json()["detail"]["code"] == "bad_base_url"
    assert make(headers={"Authorization": "Bearer x"}).json()["detail"]["code"] == "bad_headers"
    assert make(headers={"X-Ok": "a\nb"}).json()["detail"]["code"] == "bad_headers"
    assert make(kind="bedrock").json()["detail"]["code"] == "bad_kind"
    assert owner.post("/api/providers", json={"name": "az", "kind": "azure"}).json()["detail"]["code"] == "bad_base_url"
    r = owner.post("/api/providers", json={"name": "vpn", "kind": "openai_compatible", "external": False,
                                           "allow_http": True, "base_url": "http://10.0.0.5:8000/v1"})
    assert r.status_code == 201 and r.json()["provider"]["external"] is False
    r = make()
    assert r.status_code == 201 and r.json()["provider"]["base_url"] == "https://api.openai.com/v1"
    assert make().status_code == 409
    monkeypatch.setenv("EGRESS_ALLOWED_HOSTS", "*.openai.azure.com")
    get_settings.cache_clear()
    r = owner.post("/api/providers", json={"name": "q", "kind": "openai"})
    assert r.status_code == 422 and "EGRESS_ALLOWED_HOSTS" in r.json()["detail"]["message"]
    r = owner.post("/api/providers", json={"name": "az", "kind": "azure",
                                           "base_url": "https://res.openai.azure.com/openai/v1"})
    assert r.status_code == 201


def test_fy_prv_004_test_connection_and_import(owner, cloud):
    """FY-PRV-004: Test connection asks the configured base URL for /models with the key; import
    lists them (Gemini's "models/" prefix removed, embeddings recognised, already added marked)."""
    cloud.models = [{"id": "models/gemini-2.5-flash"}, {"id": "models/gemini-embedding-001"}]
    p = add_provider(owner, cloud, name="gem", kind="gemini", models=())
    out = owner.post(f"/api/providers/{p['id']}/test").json()
    assert out["ok"] and out["models"] == 2 and out["sample"] == ["gemini-2.5-flash", "gemini-embedding-001"]
    assert cloud.seen[-1]["path"] == "/v1/models" and cloud.seen[-1]["headers"]["Authorization"] == f"Bearer {CLOUD_KEY}"
    models = owner.get(f"/api/providers/{p['id']}/upstream-models").json()["models"]
    assert [(m["id"], m["api"], m["added"]) for m in models] == [
        ("gemini-2.5-flash", "openai-chat", False), ("gemini-embedding-001", "openai-embeddings", False)]
    r = owner.post(f"/api/providers/{p['id']}/models", json={"models": [{"upstream_model": m["id"], "api": m["api"]}
                                                                       for m in models]})
    assert r.json()["added"] == ["gemini-2.5-flash", "gemini-embedding-001"]
    assert all(m["added"] for m in owner.get(f"/api/providers/{p['id']}/upstream-models").json()["models"])
    owner.patch(f"/api/providers/{p['id']}", json={"api_key": "wrong-key-123"})
    out = owner.post(f"/api/providers/{p['id']}/test").json()
    assert out["ok"] is False and out["status"] == 401 and "refused the key" in out["message"]


def test_fy_prv_005_deleting_what_routes_use(owner, cloud):
    """FY-PRV-005: a provider or model that a route uses cannot be deleted (409, naming the routes);
    once the route is gone it can."""
    p = add_provider(owner, cloud)
    pm = model_id(p, "gpt-mini")
    route = cloud_route(owner, "gpt", pm)
    r = owner.delete(f"/api/providers/{p['id']}")
    assert r.status_code == 409 and r.json()["detail"]["routes"] == ["gpt"]
    assert owner.delete(f"/api/provider-models/{pm}").status_code == 409
    assert owner.delete(f"/api/routes/{route['id']}").status_code == 200
    assert owner.delete(f"/api/provider-models/{pm}").status_code == 200
    assert owner.delete(f"/api/providers/{p['id']}").status_code == 200
    assert owner.get("/api/providers").json()["providers"] == []




def test_fy_prv_006_link_local_metadata_and_private_addresses(owner, monkeypatch):
    """FY-PRV-006: a provider whose name resolves to the metadata service or another link-local
    address is refused before anything is sent; a private address only for a provider inside your
    network; plain http never to a public address."""
    import asyncio

    hosts = {"metadata.test": ["169.254.169.254"], "aws6.test": ["fd00:ec2::254"], "lan.test": ["10.1.2.3"],
             "public.test": ["93.184.216.34"], "mixed.test": ["93.184.216.34", "169.254.10.1"]}

    async def resolve(host, port):
        return hosts[host]

    monkeypatch.setattr(egress, "resolver", resolve)

    def check(url, external=True, allow_http=False):
        dest = egress.check_url(url, external=external, allow_http=allow_http)
        return asyncio.run(egress.checked_address(dest, external=external, allow_http=allow_http))

    for url in ("https://metadata.test/v1", "https://aws6.test/v1", "https://mixed.test/v1",
                "https://169.254.169.254/latest", "https://[fe80::1]/v1"):
        with pytest.raises(egress.EgressError, match="link-local"):
            check(url, external=False)
    with pytest.raises(egress.EgressError, match="private"):
        check("https://lan.test/v1")
    assert check("https://lan.test/v1", external=False) == "10.1.2.3"
    assert check("http://lan.test/v1", external=False, allow_http=True) == "10.1.2.3"
    with pytest.raises(egress.EgressError, match="plain http"):
        check("http://public.test/v1", external=False, allow_http=True)
    assert check("https://public.test/v1") == "93.184.216.34"
    url, headers, ext = asyncio.run(egress.prepare("https://public.test/v1", "chat/completions", external=True,
                                                   allow_http=False))
    # The connection goes to the address that was checked; TLS still verifies the name.
    assert url == "https://93.184.216.34:443/v1/chat/completions" and headers == {"Host": "public.test"}
    assert ext == {"sni_hostname": "public.test"}
    # And through the API: saved, but the request is refused, nothing is sent.
    egress._cache.clear()
    r = owner.post("/api/providers", json={"name": "meta", "kind": "openai_compatible", "external": False,
                                           "base_url": "https://metadata.test/v1", "api_key": "k-123456"})
    pid = r.json()["provider"]["id"]
    r = owner.post(f"/api/providers/{pid}/test")
    assert r.status_code == 422 and "link-local" in r.json()["detail"]["message"]
    owner.post(f"/api/providers/{pid}/models", json={"models": [{"upstream_model": "m"}]})
    pm = owner.get(f"/api/providers/{pid}").json()["provider"]["models"][0]["id"]
    cloud_route(owner, "meta-m", pm)
    r = api_client(owner, cloud_key(owner, "on", "on")).post("/v1/chat/completions", json={"model": "meta-m",
                                                                                           "messages": []})
    assert r.status_code == 502 and r.json()["error"]["code"] == "provider_blocked"


def test_fy_prv_007_requests_go_through_the_egress_proxy(owner, monkeypatch):
    """FY-PRV-007: with EGRESS_PROXY set, provider requests go to the proxy (absolute URL, the
    provider's name resolved by the proxy), never straight to the provider."""
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    proxy_stub = FakeCloud("proxy")
    try:
        monkeypatch.setenv("EGRESS_PROXY", f"http://127.0.0.1:{proxy_stub.port}")
        get_settings.cache_clear()
        r = owner.post("/api/providers", json={"name": "far", "kind": "openai",
                                               "base_url": "http://cloud.example.invalid/v1", "api_key": CLOUD_KEY})
        p = r.json()["provider"]
        owner.post(f"/api/providers/{p['id']}/models", json={"models": [{"upstream_model": "gpt-mini"}]})
        pm = owner.get(f"/api/providers/{p['id']}").json()["provider"]["models"][0]["id"]
        cloud_route(owner, "far-m", pm)
        r = api_client(owner, cloud_key(owner, "on", "on")).post(
            "/v1/chat/completions", json={"model": "far-m", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello from proxy"
        assert proxy_stub.posts()[-1]["path"] == "http://cloud.example.invalid/v1/chat/completions"
    finally:
        proxy_stub.close()


def test_fy_prv_008_tls_is_verified(owner, monkeypatch, tmp_path):
    """FY-PRV-008: a provider with a certificate the server does not trust is not reached; with its
    CA in EGRESS_CA_FILE it is."""
    tls_dir = tmp_path / "stub-tls"
    tls.make_ca(tls_dir)
    tls.make_server_cert(tls_dir, ["localhost"], ["127.0.0.1"])
    stub = FakeCloud("tls", tls_dir=tls_dir)
    try:
        r = owner.post("/api/providers", json={"name": "inside", "kind": "openai_compatible", "external": False,
                                               "base_url": stub.base_url, "api_key": CLOUD_KEY})
        pid = r.json()["provider"]["id"]
        r = owner.post(f"/api/providers/{pid}/test")
        assert r.status_code == 502 and r.json()["detail"]["code"] == "provider_unreachable" and stub.seen == []
        monkeypatch.setenv("EGRESS_CA_FILE", str(tls_dir / "ca.crt"))
        get_settings.cache_clear()
        egress.reset()
        assert owner.post(f"/api/providers/{pid}/test").json()["ok"] is True
    finally:
        stub.close()


def test_fy_prv_009_routes_admin_api(owner, cloud):
    """FY-PRV-009: routes of their own (cloud only) are created, changed and deleted by admins; a
    deployment's own route cannot be deleted or lose its deployment; targets must match the
    protocol; tiers are renumbered."""
    p = add_provider(owner, cloud)
    pm = model_id(p, "gpt-mini")
    r = owner.post("/api/routes", json={"served_name": "gpt", "api": "openai-chat", "targets": [
        {"tier": 5, "kind": "provider", "provider_model_id": pm, "weight": 3}]})
    route = r.json()["route"]
    assert r.status_code == 201 and route["kind"] == "cloud" and route["targets"][0]["tier"] == 0
    assert route["targets"][0]["weight"] == 3 and route["deployment_id"] is None
    assert owner.post("/api/routes", json={"served_name": "gpt", "api": "openai-chat", "targets": [
        {"kind": "provider", "provider_model_id": pm}]}).status_code == 409
    assert owner.post("/api/routes", json={"served_name": "asr2", "api": "openai-audio", "targets": [
        {"kind": "provider", "provider_model_id": pm}]}).json()["detail"]["code"] == "wrong_api"
    assert owner.put(f"/api/routes/{route['id']}", json={"targets": [
        {"kind": "provider", "provider_model_id": pm}, {"kind": "provider", "provider_model_id": pm}]}
                     ).json()["detail"]["code"] == "duplicate_target"
    add_user(owner, "ursula")
    u = sign_in(owner, "ursula", PW)
    assert u.put(f"/api/routes/{route['id']}", json={"targets": [{"kind": "provider", "provider_model_id": pm}]}
                 ).status_code == 403
    assert u.get(f"/api/routes/{route['id']}").json()["route"]["served_name"] == "gpt"
    assert owner.delete(f"/api/routes/{route['id']}").status_code == 200
    events = [e["action"] for e in owner.get("/api/audit").json()["events"]]
    assert "route.created" in events and "route.deleted" in events
