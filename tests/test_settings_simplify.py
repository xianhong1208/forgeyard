"""1.3.1 settings-simplify (issue #30): deprecated settings (D7), values from .env (D1), where a
provider is (D4), the server's Hugging Face token (D2), the agent's machine defaults (D6) and the
deploy form's context hint (D9) (FY-SIMP-*)."""

from __future__ import annotations

import json
import logging
import types

import pytest
from sqlalchemy import select, text

from forgeyard import advisor, settings_check
from forgeyard.agent import config as agent_config
from forgeyard.config import DEPRECATED, Settings, get_settings
from forgeyard.db import AuditEvent, Deployment, Replica, ServerSetting, get_engine, new_session
from forgeyard.gateway import where
from forgeyard.models import hf_token
from forgeyard.security import crypto
from forgeyard.workloads import state as wstate
from tests.conftest import add_user, make_client, sign_in
from tests.model_helpers import FakeHF, add_catalog, tiny_repo
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

PW = "quiet-meadow-4821"
TOKEN = "hf_server_token_for_tests_0123456789"


def _ids(problems) -> dict[str, list]:
    out: dict[str, list] = {}
    for p in problems:
        out.setdefault(p.id, []).append(p)
    return out


# ------------------------------------------------------------------ D7: deprecated settings
@pytest.mark.parametrize("name, value", [
    ("OPENROUTER_MODELS_URL", "https://prices.example.org/models"), ("USAGE_FLUSH_SECONDS", "7"),
    ("PROVIDER_COOLDOWN_SECONDS", "45"), ("PROVIDER_RETRY_AFTER_CAP", "1"),
    ("FORGEYARD_SETUP_TOKEN", "pinned-setup-token-0123456789"), ("AGENT_HEARTBEAT_SECONDS", "7"),
    ("SECRET_KEY_FILE", "/etc/forgeyard/secret.key"),
])
def test_fy_simp_001_a_deprecated_setting_still_works_and_is_named_with_its_replacement(name, value):
    """FY-SIMP-001: each deprecated setting is still read (its value is used), and a warning names
    it, what replaces it and that 1.4 removes it. An empty value is not reported."""
    s = Settings(_env_file=None, **{name.lower(): value})
    assert str(getattr(s, name.lower())) in (value, f"{value}.0")
    found = [p for p in settings_check.static_problems(s, {}, []) if p.id == "setting-deprecated"]
    assert [p.scope for p in found] == [f"setting:{name}"]
    assert found[0].severity == "warning" and "removed in 1.4" in found[0].detail
    instead, how = DEPRECATED[name.lower()]
    assert how in found[0].detail and found[0].fix_url.endswith("#deprecated-settings")
    if name in ("USAGE_FLUSH_SECONDS", "PROVIDER_COOLDOWN_SECONDS", "PROVIDER_RETRY_AFTER_CAP", "OPENROUTER_MODELS_URL"):
        assert instead is None and "Nothing replaces it" in found[0].detail
    else:
        assert f"Use {instead} instead" in found[0].detail
    empty = Settings(_env_file=None, **{name.lower(): ""}) if name not in (
        "USAGE_FLUSH_SECONDS", "PROVIDER_COOLDOWN_SECONDS", "PROVIDER_RETRY_AFTER_CAP", "AGENT_HEARTBEAT_SECONDS") else None
    if empty is not None:
        assert not [p for p in settings_check.static_problems(empty, {}, []) if p.id == "setting-deprecated"]
    assert not [p for p in settings_check.static_problems(Settings(_env_file=None), {}, []) if p.id == "setting-deprecated"]


def test_fy_simp_002_the_server_logs_the_deprecation_at_start(monkeypatch, caplog):
    """FY-SIMP-002: started with a deprecated variable, the server logs which one and what to use, and
    keeps using its value."""
    monkeypatch.setenv("USAGE_FLUSH_SECONDS", "3")
    monkeypatch.setenv("AGENT_HEARTBEAT_SECONDS", "6")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="forgeyard"), make_client() as c:
        assert c.get("/api/health").status_code == 200
    assert "USAGE_FLUSH_SECONDS is deprecated" in caplog.text and "AGENT_HEARTBEAT_SECONDS is deprecated" in caplog.text
    assert "Use AGENT_OFFLINE_SECONDS instead (agents send a heartbeat every third of it)" in caplog.text
    assert get_settings().heartbeat_seconds == 6


def test_fy_simp_003_secret_key_file_and_its_new_name(tmp_path):
    """FY-SIMP-003: FORGEYARD_SECRET_KEY_FILE names the key file; SECRET_KEY_FILE still does when it is
    alone; with both, the new name wins and a different old one is reported; FORGEYARD_SECRET_KEY next to
    either file is reported as before."""
    new, old = str(tmp_path / "new.key"), str(tmp_path / "old.key")
    assert str(Settings(_env_file=None, forgeyard_secret_key_file=new).secret_key_path) == new
    assert str(Settings(_env_file=None, secret_key_file=old).secret_key_path) == old
    both = Settings(_env_file=None, forgeyard_secret_key_file=new, secret_key_file=old)
    assert str(both.secret_key_path) == new
    found = _ids(settings_check.static_problems(both, {}, []))
    assert "SECRET_KEY_FILE (its old name) is ignored" in found["secret-key-both"][0].detail
    same = Settings(_env_file=None, forgeyard_secret_key_file=new, secret_key_file=new)
    assert "secret-key-both" not in _ids(settings_check.static_problems(same, {}, []))
    for field in ("forgeyard_secret_key_file", "secret_key_file"):
        s = Settings(_env_file=None, forgeyard_secret_key="a" * 44, **{field: new})
        p = _ids(settings_check.static_problems(s, {}, []))["secret-key-both"][0]
        assert field.upper() in p.title
    assert str(Settings(_env_file=None, data_dir=tmp_path).secret_key_path) == str(tmp_path / "secret.key")


def test_fy_simp_004_the_heartbeat_is_a_third_of_the_offline_time(owner, monkeypatch):
    """FY-SIMP-004: without AGENT_HEARTBEAT_SECONDS, agents are told to beat every third of
    AGENT_OFFLINE_SECONDS (at least 2 s); the deprecated value still wins when set. The Machines API
    and the join answer carry it."""
    for offline, beat in ((30, 10), (45, 15), (4, 2), (7, 2)):
        assert Settings(_env_file=None, agent_offline_seconds=offline).heartbeat_seconds == beat
    assert Settings(_env_file=None, agent_offline_seconds=30, agent_heartbeat_seconds=4).heartbeat_seconds == 4
    assert "heartbeat-flapping" not in _ids(settings_check.static_problems(Settings(_env_file=None), {}, []))
    assert "heartbeat-flapping" in _ids(settings_check.static_problems(
        Settings(_env_file=None, agent_offline_seconds=4), {}, []))
    monkeypatch.setenv("AGENT_OFFLINE_SECONDS", "60")
    get_settings.cache_clear()
    body = owner.get("/api/machines").json()
    assert body["heartbeat_interval"] == 20 and body["offline_after"] == 60


# ------------------------------------------------------------------ D1: values that come from .env
def test_fy_simp_005_the_pages_say_which_values_still_come_from_env(owner, monkeypatch):
    """FY-SIMP-005: until an admin saves a panel, the API names the variables its values come from;
    after saving, none. Unset variables are not named; people who are not admins get nothing."""
    monkeypatch.setenv("GATEWAY_MAX_RPM", "30")
    monkeypatch.setenv("GATEWAY_CLOUD_MAX", "fallback")
    monkeypatch.setenv("MODEL_URL_HOSTS", "mirror.example.org")
    get_settings.cache_clear()
    body = owner.get("/api/keys").json()
    assert body["env_seeds"] == {"limits": ["GATEWAY_MAX_RPM"], "cloud": ["GATEWAY_CLOUD_MAX"]}
    assert body["cloud"] == {"default": "off", "max": "fallback"}
    assert owner.get("/api/model-sources").json()["sources"]["env_seeds"] == ["MODEL_URL_HOSTS"]
    assert owner.put("/api/keys/cloud", json={"default": "off", "max": "fallback"}).status_code == 200
    assert owner.get("/api/keys").json()["env_seeds"] == {"limits": ["GATEWAY_MAX_RPM"], "cloud": []}
    assert owner.put("/api/model-sources", json={"url_hosts": ["mirror.example.org"]}).json()["sources"]["env_seeds"] == []
    add_user(owner, "ursula")
    assert "env_seeds" not in sign_in(owner, "ursula", PW).get("/api/keys").json()


# ------------------------------------------------------------------ D4: outside or inside your network
@pytest.mark.parametrize("addr, kind", [
    ("10.1.2.3", "private"), ("172.16.0.1", "private"), ("172.31.255.254", "private"), ("192.168.1.20", "private"),
    ("100.64.0.1", "private"), ("100.127.255.1", "private"), ("127.0.0.1", "private"), ("169.254.10.1", "private"),
    ("::1", "private"), ("fe80::1", "private"), ("fd12:3456:789a::1", "private"), ("fc00::1", "private"),
    ("::ffff:192.168.1.20", "private"), ("[fd00::5]", "private"),
    ("172.32.0.1", "public"), ("100.128.0.1", "public"), ("203.0.113.10", "public"), ("198.51.100.7", "public"),
    ("192.0.2.1", "public"), ("2001:db8::1", "public"), ("::ffff:203.0.113.10", "public"),
    ("224.0.0.1", None), ("0.0.0.0", None), ("::", None), ("240.0.0.1", None), ("llm.example.org", None),
])
def test_fy_simp_006_address_classes(addr, kind):
    """FY-SIMP-006: private means RFC 1918, 100.64.0.0/10 (VPNs), loopback, link-local and IPv6 ULA;
    any other unicast address is public (documentation addresses stand for public ones); multicast,
    unspecified and reserved are neither, and a name is not an address."""
    assert where.address_kind(addr) == kind


def test_fy_simp_007_where_a_provider_is_from_its_kind_and_address():
    """FY-SIMP-007: cloud kinds are outside whatever the URL; an OpenAI-compatible server is judged by
    its host: an address by its class, a local name without a lookup, any other name by what it
    resolves to on the server; unknown keeps the kind's default (inside) and says it cannot tell."""
    asked: list[str] = []

    def resolve(host):
        asked.append(host)
        return {"lan-only.example.org": ["192.168.1.40", "fd00::40"], "together.example.org": ["203.0.113.50"],
                "split.example.org": ["192.168.1.41", "203.0.113.51"]}.get(host, [])

    def c(kind, url):
        return where.classify(kind, url, resolve=resolve)

    for kind in ("openai", "anthropic", "gemini", "openrouter"):
        w = c(kind, None)
        assert (w.external, w.basis) == (True, "kind")
        assert c(kind, "https://192.168.1.20/v1").external is True  # the kind wins
    assert (c("azure", "https://res.openai.azure.com/openai/v1").external, c("azure", None).basis) == (True, "kind")
    oc = "openai_compatible"
    assert (c(oc, "https://203.0.113.10/v1").external, c(oc, "https://203.0.113.10/v1").basis) == (True, "address")
    w = c(oc, "http://192.168.1.30:8000/v1")
    assert (w.external, w.basis) == (False, "address") and "private address" in w.reason
    assert c(oc, "https://[fd00::7]:8443/v1").external is False
    for host in ("localhost", "gpu-box", "llm.lan", "box.local", "api.internal", "nas.home.arpa", "x.localhost"):
        w = c(oc, f"https://{host}/v1")
        assert (w.external, w.basis) == (False, "name"), host
    assert asked == []  # none of those needed a lookup
    w = c(oc, "https://lan-only.example.org/v1")
    assert (w.external, w.basis) == (False, "resolved") and "192.168.1.40" in w.reason
    w = c(oc, "https://together.example.org/v1")
    assert (w.external, w.basis) == (True, "resolved") and "third party" in w.reason
    for host, why in (("split.example.org", "both private and public"), ("nowhere.example.org", "did not resolve")):
        w = c(oc, f"https://{host}/v1")
        assert (w.external, w.basis) == (False, "unknown") and why in w.reason and "Tick it" in w.reason
    w = c(oc, "https://224.0.0.1/v1")
    assert (w.external, w.basis) == (False, "unknown")
    assert (c(oc, "").basis, c(oc, None).external) == ("unknown", False)


def test_fy_simp_008_the_api_suggests_and_a_new_provider_takes_the_suggestion(owner, monkeypatch):
    """FY-SIMP-008: POST /api/providers/where (admins) gives the suggestion with its reason; a provider
    created without `external` gets it; an explicit value wins; changing an existing provider's URL
    never changes its tick."""
    advisor.reset()  # nothing cached from another test
    monkeypatch.setattr(advisor, "resolver", lambda host: {"llm.example.org": ["203.0.113.60"]}.get(host, []))
    try:
        r = owner.post("/api/providers/where", json={"kind": "openai_compatible", "base_url": "https://llm.example.org/v1"})
        assert r.status_code == 200 and r.json() == {"external": True, "basis": "resolved",
                                                     "reason": r.json()["reason"]} and "203.0.113.60" in r.json()["reason"]
        assert owner.post("/api/providers/where", json={"kind": "nope"}).status_code == 422
        add_user(owner, "olga", role="operator")
        assert sign_in(owner, "olga", PW).post("/api/providers/where", json={"kind": "openai"}).status_code == 403

        def make(name, url, **kw):
            r = owner.post("/api/providers", json={"name": name, "kind": "openai_compatible", "base_url": url, **kw})
            assert r.status_code == 201, r.text
            return r.json()["provider"]

        assert make("together", "https://203.0.113.10/v1")["external"] is True
        assert make("own-vllm", "https://192.168.1.30/v1")["external"] is False
        assert make("by-name", "https://llm.example.org/v1")["external"] is True
        assert make("unknown", "https://nowhere.example.org/v1")["external"] is False
        assert make("forced", "https://203.0.113.11/v1", external=False)["external"] is False
        own = make("moves", "https://192.168.1.31/v1")
        r = owner.patch(f"/api/providers/{own['id']}", json={"base_url": "https://203.0.113.12/v1"})
        assert r.status_code == 200 and r.json()["provider"]["external"] is False
    finally:
        advisor.reset()


# ------------------------------------------------------------------ D2: the server's Hugging Face token
def test_fy_simp_009_the_server_token_is_stored_encrypted_and_never_returned(owner):
    """FY-SIMP-009: admins set and remove the token; the database row holds ciphertext only; the API
    returns whether it is set, its last four characters, who and when, never the token; the audit log
    names the action without it; operators may not touch it; a bad token is refused."""
    assert owner.get("/api/model-sources").json()["sources"]["hf_token"]["set"] is False
    r = owner.put("/api/hf-token", json={"token": f"  {TOKEN}  "})
    assert r.status_code == 200 and r.json()["hf_token"]["set"] is True and r.json()["hf_token"]["last4"] == TOKEN[-4:]
    assert r.json()["hf_token"]["updated_by"] == "owner"
    with get_engine().connect() as conn:
        raw = conn.execute(text("SELECT value FROM server_settings WHERE key = 'hf_token'")).scalar_one()
    raw = json.loads(raw) if isinstance(raw, str) else raw
    assert raw["token"].startswith(f"fy1:{crypto.box().kid}:") and TOKEN not in json.dumps(raw)
    db = new_session()
    try:
        assert hf_token.get(db) == TOKEN
        assert [e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.action.like("hf_token.%")))] == ["hf_token.set"]
        assert TOKEN not in json.dumps([e.detail for e in db.scalars(select(AuditEvent))])
    finally:
        db.close()
    for body in (owner.get("/api/model-sources").text, owner.get("/api/models").text, owner.get("/api/audit").text):
        assert TOKEN not in body
    assert owner.get("/api/models").json()["hf_token_set"] is True
    for bad in ("", "two words", "x" * 513, "tök"):
        assert owner.put("/api/hf-token", json={"token": bad}).status_code == 422, bad
    add_user(owner, "olga", role="operator")
    op = sign_in(owner, "olga", PW)
    assert op.put("/api/hf-token", json={"token": "hf_x"}).status_code == 403
    assert op.delete("/api/hf-token").status_code == 403
    assert op.get("/api/models").json()["hf_token_set"] is True  # the deploy form may say it is there
    assert owner.delete("/api/hf-token").json()["hf_token"]["set"] is False
    db = new_session()
    try:
        assert db.get(ServerSetting, "hf_token") is None and hf_token.get(db) is None
    finally:
        db.close()


@pytest.fixture()
def gated_hf(monkeypatch):
    server = FakeHF(tiny_repo("acme/gated", gated=True))
    monkeypatch.setenv("HF_ENDPOINT", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    yield server
    server.close()


def test_fy_simp_010_inspection_uses_the_server_token_unless_it_brings_its_own(owner, gated_hf):
    """FY-SIMP-010: a gated model cannot be inspected without a token; with the server's token it can
    (Hugging Face gets it); a token typed for the inspection wins over the server's."""
    r = owner.post("/api/models/inspect", json={"source": "hf://acme/gated"})
    assert r.status_code == 422 and "gated or private" in r.json()["detail"]["message"]
    assert owner.put("/api/hf-token", json={"token": FakeHF.TOKEN}).status_code == 200
    gated_hf.requests.clear()
    assert owner.post("/api/models/inspect", json={"source": "hf://acme/gated"}).status_code == 200
    assert gated_hf.requests and all(q["auth"] == f"Bearer {FakeHF.TOKEN}" for q in gated_hf.requests)
    gated_hf.requests.clear()
    r = owner.post("/api/models/inspect", json={"source": "hf://acme/gated", "token": "hf_some_other_token"})
    assert r.status_code == 422  # its own token, which the fake refuses: the server's was not used
    assert {q["auth"] for q in gated_hf.requests} == {"Bearer hf_some_other_token"}
    assert owner.post("/api/models", json={"name": "gated", "source": "hf://acme/gated"}).status_code == 201


def test_fy_simp_011_downloads_get_the_deployments_token_else_the_servers(owner):
    """FY-SIMP-011: the desired state carries a deployment's own token; without one, the server's, for a
    Hugging Face model only (an https:// GGUF host never gets a token); with neither, none. Changing the
    server's token reaches the next desired state."""
    seed_catalog()
    add_catalog("tiny-url", {"kind": "url", "url": "https://files.example.org/tiny.gguf", "format": "gguf",
                             "gguf": {"layers": 2, "tensor_bytes": 1000, "kv_heads": 1, "key_length": 8,
                                      "value_length": 8, "context_length": 4096, "vocab": 16},
                             "files": [{"path": "tiny.gguf", "size": 1000, "sha256": "d" * 64}], "size_bytes": 1000},
                task="chat", fmt="gguf", source="https://files.example.org/tiny.gguf", kind="url")
    a100 = make_node("a100", allowlist=[4], features=MODELS)
    own = deploy(owner, "own", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0}, machine_id=a100,
                 hf_token="hf_deployment_own_token")["deployment"]
    plain = deploy(owner, "plain", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0}, machine_id=a100)["deployment"]
    url = deploy(owner, "by-url", "tiny-url", "llama-cpp", params={"gpus": 0}, machine_id=a100)["deployment"]

    def tokens():
        db = new_session()
        try:
            names = {d.workload_id: d.name for d in db.scalars(select(Deployment))}
            msg = wstate.desired_message(db, a100, 1)
            return {names[r.workload_id]: next(x["model"].get("token") for x in msg["replicas"] if x["id"] == r.uid)
                    for r in db.scalars(select(Replica)) if r.workload_id in names}
        finally:
            db.close()

    assert tokens() == {"own": "hf_deployment_own_token", "plain": None, "by-url": None}
    assert owner.put("/api/hf-token", json={"token": TOKEN}).status_code == 200
    assert tokens() == {"own": "hf_deployment_own_token", "plain": TOKEN, "by-url": None}
    assert owner.put("/api/hf-token", json={"token": TOKEN + "b"}).status_code == 200
    assert tokens()["plain"] == TOKEN + "b"
    assert own["has_token"] is True and plain["has_token"] is False and url["has_token"] is False


def test_fy_simp_012_secrets_reset_drops_a_server_token_it_cannot_read(owner, caplog):
    """FY-SIMP-012: after a lost key, `secrets reset` removes the server's token too and counts it."""
    from forgeyard.cli import main as cli_main
    from forgeyard.db import reset_engine

    assert owner.put("/api/hf-token", json={"token": TOKEN}).status_code == 200
    get_settings().secret_key_path.unlink()
    crypto.reset()
    caplog.set_level(logging.INFO)
    assert cli_main(["secrets", "reset", "--yes"]) == 0
    assert "Dropped 1 Hugging Face token(s)" in caplog.text
    crypto.reset()
    reset_engine()
    db = new_session()
    try:
        assert db.get(ServerSetting, "hf_token") is None
    finally:
        db.close()


def test_fy_simp_013_setup_health_flags_a_gated_model_without_a_token(owner):
    """FY-SIMP-013 (V33): a deployment of a gated Hugging Face model with no token of its own while the
    server has none is a note, an error once a download was refused; a server token or the
    deployment's own clears it."""
    seed_catalog()
    db = new_session()
    try:
        from forgeyard.db import CatalogModel

        m = db.scalar(select(CatalogModel).where(CatalogModel.name == "qwen3-1.7b-gguf"))
        m.meta = {**m.meta, "gated": True}
        db.commit()
    finally:
        db.close()
    a100 = make_node("a100", allowlist=[4], features=MODELS)
    dep = deploy(owner, "gated", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0}, machine_id=a100)["deployment"]

    def found():
        db = new_session()
        try:
            return [f for f in advisor.run(db, get_settings(), strict=True) if f.id == "gated-model-no-token"]
        finally:
            db.close()

    f = found()
    assert len(f) == 1 and f[0].severity == "info" and f[0].scope == f"deployment:{dep['id']}"
    assert "neither the server nor this deployment has a token" in f[0].detail
    db = new_session()
    try:
        r = db.scalar(select(Replica))
        r.state, r.reason = "backoff", ("model Qwen3-1.7B-Q4_K_M.gguf: access refused (HTTP 403); a gated model needs a "
                                        "Hugging Face token")
        db.commit()
    finally:
        db.close()
    assert found()[0].severity == "error"
    assert owner.put("/api/hf-token", json={"token": TOKEN}).status_code == 200
    assert found() == []
    owner.delete("/api/hf-token")
    db = new_session()
    try:
        db.scalar(select(Deployment)).hf_token = "hf_its_own"
        db.commit()
    finally:
        db.close()
    assert found() == []


# ------------------------------------------------------------------ D6: the agent's machine defaults
@pytest.fixture()
def machine_defaults(monkeypatch):
    """The real machine defaults (the suite turns them off), on a fake machine."""
    monkeypatch.setattr(agent_config, "MACHINE_DEFAULTS", {
        "models.cache": lambda: agent_config.default_model_cache(),
        "workloads.user": lambda: agent_config.default_workload_user(),
    })
    state = types.SimpleNamespace(euid=0, users={"forgeyard-run"})
    monkeypatch.setattr(agent_config.os, "geteuid", lambda: state.euid)
    monkeypatch.setattr(agent_config, "user_exists", lambda name: name in state.users)
    return state


def _args(**kw):
    import argparse

    ns = argparse.Namespace(config=None)
    for s in agent_config.SETTINGS:
        setattr(ns, s.dest, None)
    ns.no_workloads = None
    ns.verbose = None
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_fy_simp_014_the_agent_defaults_match_the_installer(machine_defaults, monkeypatch, tmp_path):
    """FY-SIMP-014: as root, the model cache defaults to /srv/forgeyard-models and the workload user to
    forgeyard-run when that account exists (never otherwise: it is not created); not as root, the cache
    is ~/.local/share/forgeyard-models and there is no workload user. Both show as defaults."""
    monkeypatch.setenv("HOME", str(tmp_path))
    res = agent_config.resolve(_args(), env={})
    assert res.values["models.cache"] == "/srv/forgeyard-models" and res.source("models.cache") == "default"
    assert res.values["workloads.user"] == "forgeyard-run" and res.source("workloads.user") == "default"
    machine_defaults.users = set()
    res = agent_config.resolve(_args(), env={})
    assert res.values["workloads.user"] is None and res.values["models.cache"] == "/srv/forgeyard-models"
    machine_defaults.euid, machine_defaults.users = 1000, {"forgeyard-run"}
    res = agent_config.resolve(_args(), env={})
    assert res.values["models.cache"] == str(tmp_path / ".local/share/forgeyard-models")
    assert res.values["workloads.user"] is None
    assert agent_config.default_model_cache(euid=0) == agent_config.ROOT_MODEL_CACHE
    assert agent_config.default_workload_user(euid=1000) is None


def test_fy_simp_015_given_values_win_and_empty_means_no_models(machine_defaults, tmp_path):
    """FY-SIMP-015: a flag, a variable or the file still wins over a machine default; an empty model
    cache from any of them means "serve no models" (not the default), and `agent config` says so."""
    res = agent_config.resolve(_args(model_cache="/data/models", workload_user="svc"), env={})
    assert (res.values["models.cache"], res.source("models.cache")) == ("/data/models", "flag")
    assert res.values["workloads.user"] == "svc"
    res = agent_config.resolve(_args(model_cache=""), env={})
    assert (res.values["models.cache"], res.source("models.cache")) == ("", "flag")
    res = agent_config.resolve(_args(), env={"FORGEYARD_MODEL_CACHE": ""})
    assert (res.values["models.cache"], res.source("models.cache")) == ("", "env FORGEYARD_MODEL_CACHE")
    agent_config.DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    agent_config.DEFAULT_PATH.write_text('[models]\ncache = ""\n')
    res = agent_config.resolve(_args(), env={})
    assert (res.values["models.cache"], res.source("models.cache")) == ("", "file")
    out = agent_config.describe(res)
    assert any(line.startswith("models.cache") and "serves no models" in line and "(file)" in line for line in out.splitlines())
    agent_config.DEFAULT_PATH.write_text('[models]\ncache = "relative"\n')
    with pytest.raises(agent_config.ConfigError, match="absolute path"):
        agent_config.resolve(_args(), env={})
    agent_config.DEFAULT_PATH.unlink()
    out = agent_config.describe(agent_config.resolve(_args(), env={}))
    assert any(line.startswith("models.cache") and "/srv/forgeyard-models" in line and "(default)" in line
               for line in out.splitlines())


def test_fy_simp_016_an_empty_cache_serves_no_models_and_the_default_one_does(machine_defaults, tmp_path):
    """FY-SIMP-016: the supervisor gets no model cache for an empty value and one at the default path
    otherwise (what makes the agent report the models feature)."""
    from forgeyard.cli import _make_supervisor

    args = _args(model_cache="", port_range="30000-30010", container_engine="none", limits="rlimit", log_max_mb=10)
    agent_config.resolve(args, env={})
    assert _make_supervisor(args, tmp_path / "state").models is None
    monkeypatch_cache = str(tmp_path / "cache")
    agent_config.ROOT_MODEL_CACHE, saved = monkeypatch_cache, agent_config.ROOT_MODEL_CACHE
    try:
        args = _args(port_range="30000-30010", container_engine="none", limits="rlimit", log_max_mb=10)
        agent_config.resolve(args, env={})
        sup = _make_supervisor(args, tmp_path / "state2")
        assert sup.models is not None and str(sup.models.root) == monkeypatch_cache
    finally:
        agent_config.ROOT_MODEL_CACHE = saved


# ------------------------------------------------------------------ D9: the context hint
def test_fy_simp_017_the_estimate_offers_the_model_max_and_the_largest_context_that_fits(owner):
    """FY-SIMP-017: for a recipe with a context, the estimate names the model's maximum and the largest
    context (a multiple of 1024) that fits now: one step more does not. The default context is not
    changed. A memory override, or a recipe without a context, gives no `fits_max` / no hint."""
    seed_catalog()
    a100 = make_node("a100", allowlist=[4], features=MODELS)
    body = {"name": "chat", "model": "qwen3-4b", "recipe": "vllm", "params": {"concurrency": 16}, "machine_id": a100}
    r = owner.post("/api/deployments/estimate", json=body).json()
    hint = r["context_hint"]
    assert r["params"]["context"] == 8192 and hint["param"] == "context" and hint["model_max"] == 40960
    m = hint["fits_max"]
    assert m and m % 1024 == 0 and m < 40960

    def fits(n):
        out = owner.post("/api/deployments/estimate", json={**body, "params": {"concurrency": 16, "context": n}})
        return out.status_code == 200 and out.json()["placement"]["fits"]

    assert fits(m) and not fits(m + 1024)
    small = owner.post("/api/deployments/estimate", json={**body, "params": {"concurrency": 1}}).json()["context_hint"]
    assert small["fits_max"] == 40960  # everything up to the model's maximum fits
    over = owner.post("/api/deployments/estimate", json={**body, "params": {"concurrency": 16, "vram": "20Gi"}}).json()
    assert over["context_hint"] == {"param": "context", "model_max": 40960, "fits_max": None}
    asr = owner.post("/api/deployments/estimate", json={"name": "asr", "model": "faster-whisper-small",
                                                         "recipe": "faster-whisper", "machine_id": a100}).json()
    assert asr["context_hint"] is None
