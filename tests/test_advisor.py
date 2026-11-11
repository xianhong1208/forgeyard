"""The setup advisor: each check, snoozing, the secret-key backup reminder and the admin-only API
(FY-ADV-*)."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from forgeyard import __version__, advisor
from forgeyard.config import get_settings
from forgeyard.db import (
    Accelerator, ApiKey, AuditEvent, CatalogModel, Deployment, GatewayRoute, Node, Provider, ProviderModel, Replica,
    RouteTarget, ServerSetting, User, Workload, new_session, utcnow,
)
from tests.conftest import add_user, sign_in
from tests.workload_helpers import make_node

ROOT = Path(__file__).resolve().parent.parent
PW = "quiet-meadow-4821"
GIB = 1024**3


@pytest.fixture(autouse=True)
def fresh_resolver(monkeypatch):
    advisor.reset()
    monkeypatch.setattr(advisor, "resolver", lambda host: {"api.together.example": ["104.18.1.1"],
                                                           "llm.lan": ["192.168.1.21"]}.get(host, []))
    yield
    advisor.reset()


def findings(**kw) -> dict[str, list[advisor.Finding]]:
    db = new_session()
    try:
        out: dict[str, list[advisor.Finding]] = {}
        for f in advisor.run(db, get_settings(), strict=True, **kw):
            out.setdefault(f.id, []).append(f)
        return out
    finally:
        db.close()


def edit(model, ident, **fields):
    db = new_session()
    try:
        row = db.get(model, ident)
        for k, v in fields.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()


def add(*rows):
    db = new_session()
    try:
        for r in rows:
            db.add(r)
            db.flush()
        ids = [r.id for r in rows]
        db.commit()
        return ids[0] if len(ids) == 1 else ids
    finally:
        db.close()


def owner_id() -> int:
    db = new_session()
    try:
        return db.scalar(select(User.id).where(User.username == "owner"))
    finally:
        db.close()


def key(name="laptop", user_id=None, **fields) -> int:
    raw = secrets.token_hex(8)
    return add(ApiKey(user_id=user_id or owner_id(), name=name, prefix="fyk_" + raw[:8],
                      hash=hashlib.sha256(raw.encode()).hexdigest(), **fields))


def provider(name="cloudy", kind="openai", external=True, key_last4="abcd", base_url="https://api.openai.com/v1",
             **fields) -> int:
    return add(Provider(name=name, kind=kind, base_url=base_url, external=external, key_last4=key_last4, **fields))


def pmodel(pid, upstream="gpt-mini", api="openai-chat", price_input=100, price_output=400, **fields) -> int:
    return add(ProviderModel(provider_id=pid, upstream_model=upstream, api=api, price_input=price_input,
                             price_output=price_output, **fields))


def deployment(name="qwen", recipe="llama-cpp", params=None, desired="running", node_id=None, replica=None) -> int:
    db = new_session()
    try:
        m = db.scalar(select(CatalogModel).where(CatalogModel.name == "m"))
        if m is None:
            m = CatalogModel(name="m", source="hf://org/m", kind="hf", format="gguf", task="chat")
            db.add(m)
            db.flush()
        w = Workload(name=name, kind="service", runtime="model", spec={"name": name, "kind": "service"},
                     desired_state=desired)
        db.add(w)
        db.flush()
        d = Deployment(name=name, model_id=m.id, recipe=recipe, params=params or {}, served_name=name, workload_id=w.id)
        db.add(d)
        db.flush()
        if replica is not None:
            db.add(Replica(workload_id=w.id, node_id=node_id, **replica))
        db.commit()
        return d.id
    finally:
        db.close()


def route(name, targets, **fields) -> int:
    """targets: (kind, id, tier[, enabled])"""
    db = new_session()
    try:
        r = GatewayRoute(served_name=name, api="openai-chat", **fields)
        db.add(r)
        db.flush()
        for t in targets:
            kind, ref, tier = t[:3]
            enabled = t[3] if len(t) > 3 else True
            db.add(RouteTarget(route_id=r.id, tier=tier, kind=kind, enabled=enabled,
                               deployment_id=ref if kind == "local" else None,
                               provider_model_id=ref if kind == "provider" else None))
        db.commit()
        return r.id
    finally:
        db.close()


def node(name="box", inv="nvidia-a100x8", **fields) -> int:
    nid = make_node(name, inv, features=fields.pop("features", ("workloads", "models")))
    edit(Node, nid, **{"agent_version": __version__, **fields})
    return nid


def gpu(nid, index) -> Accelerator:
    db = new_session()
    try:
        return db.scalar(select(Accelerator).where(Accelerator.node_id == nid, Accelerator.index == index,
                                                   Accelerator.vendor != "cpu"))
    finally:
        db.close()


# ------------------------------------------------------------------ the API
def test_fy_adv_001_admins_only_and_the_shape_of_a_finding(owner):
    """FY-ADV-001: GET /api/advisor answers admins only (an operator and a user get 403); each finding
    has id, key (id@scope), severity, title, detail, fix_url, fix_label, scope, pages and snoozable;
    the badge counts errors and warnings, not info."""
    add_user(owner, "olga", role="operator")
    add_user(owner, "ursula")
    for name in ("olga", "ursula"):
        assert sign_in(owner, name, PW).get("/api/advisor").status_code == 403
    node("gpu-01", agent_version="0.9.0", protocol=0)
    body = owner.get("/api/advisor").json()
    assert set(body) >= {"findings", "snoozed", "counts", "badge", "snooze_days", "checked_at"}
    f = next(f for f in body["findings"] if f["id"] == "agent-outdated")
    assert set(f) == {"id", "key", "severity", "title", "detail", "fix_url", "fix_label", "scope", "pages", "snoozable"}
    assert f["key"] == f"agent-outdated@{f['scope']}" and f["severity"] == "error" and f["snoozable"] is False
    assert body["badge"] == body["counts"]["error"] + body["counts"]["warning"]
    assert body["counts"]["info"] >= 1  # setup-token-left: the tests pin a setup token
    severities = [f["severity"] for f in body["findings"]]
    assert severities == sorted(severities, key=advisor.SEVERITIES.index)


def test_fy_adv_002_snoozing_is_per_admin_for_30_days_and_never_for_errors(owner):
    """FY-ADV-002: an admin snoozes a warning: it moves to `snoozed` with a wake-up time 30 days out
    and leaves the badge, for that admin only; snooze false brings it back; an error answers 422
    cannot_snooze; an unknown key 404; an expired snooze counts as none."""
    node("gpu-01", agent_version="0.9.0")                       # warning
    node("gpu-02", agent_version="0.9.0", protocol=0)           # error
    add_user(owner, "ada", role="admin")
    ada = sign_in(owner, "ada", PW)
    before = owner.get("/api/advisor").json()
    warn = next(f for f in before["findings"] if f["id"] == "agent-outdated" and f["severity"] == "warning")
    err = next(f for f in before["findings"] if f["id"] == "agent-outdated" and f["severity"] == "error")
    r = owner.post("/api/advisor/snooze", json={"key": warn["key"]})
    assert r.status_code == 200
    body = r.json()
    assert warn["key"] not in {f["key"] for f in body["findings"]} and body["badge"] == before["badge"] - 1
    until = next(f for f in body["snoozed"] if f["key"] == warn["key"])["snoozed_until"]
    from datetime import datetime

    assert timedelta(days=29, hours=23) < datetime.fromisoformat(until) - utcnow() <= timedelta(days=30)
    assert warn["key"] in {f["key"] for f in ada.get("/api/advisor").json()["findings"]}  # not ada's snooze
    r = owner.post("/api/advisor/snooze", json={"key": err["key"]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "cannot_snooze"
    assert owner.post("/api/advisor/snooze", json={"key": "nothing@here"}).json()["detail"]["code"] == "no_such_finding"
    body = owner.post("/api/advisor/snooze", json={"key": warn["key"], "snooze": False}).json()
    assert warn["key"] in {f["key"] for f in body["findings"]} and body["snoozed"] == []
    # An expired snooze is ignored (and dropped on the next change).
    db = new_session()
    try:
        u = db.scalar(select(User).where(User.username == "owner"))
        u.preferences = {**(u.preferences or {}), advisor.SNOOZE_PREF: {warn["key"]: (utcnow() - timedelta(minutes=1)).isoformat()}}
        db.commit()
    finally:
        db.close()
    assert warn["key"] in {f["key"] for f in owner.get("/api/advisor").json()["findings"]}
    assert ada.post("/api/advisor/snooze", json={"key": warn["key"]}).status_code == 200


def test_fy_adv_003_secret_key_backup_reminder_until_confirmed_for_this_key(owner):
    """FY-ADV-003 (D5): a warning names the key id until an admin confirms a backup of that id; a stale
    id is refused (409 key_changed); the confirmation is audited and stored as the key id only; a
    different key brings the reminder back. The key itself is never in an answer."""
    from forgeyard.security import crypto

    kid = crypto.box().kid
    raw = crypto.key_path().read_text().strip()
    body = owner.get("/api/advisor").json()
    f = next(f for f in body["findings"] if f["id"] == "secret-key-backup")
    assert f["severity"] == "warning" and kid in f["title"] and f["scope"] == f"secret-key:{kid}"
    assert str(crypto.key_path()) in f["detail"]
    r = owner.post("/api/advisor/secret-key-backup", json={"kid": "00000000"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "key_changed"
    r = owner.post("/api/advisor/secret-key-backup", json={"kid": kid})
    assert r.status_code == 200 and "secret-key-backup" not in {f["id"] for f in r.json()["findings"]}
    assert raw not in r.text and raw not in owner.get("/api/advisor").text
    db = new_session()
    try:
        row = db.get(ServerSetting, advisor.BACKUP_SETTING)
        assert row.value["kid"] == kid and row.value["by"] == "owner" and set(row.value) == {"kid", "by", "at"}
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "secret_key.backup_confirmed")).detail["kid"] == kid
        row.value = {**row.value, "kid": "ffffffff"}   # as if the key had been replaced since
        db.commit()
    finally:
        db.close()
    assert "secret-key-backup" in findings()
    add_user(owner, "ursula")
    assert sign_in(owner, "ursula", PW).post("/api/advisor/secret-key-backup", json={"kid": kid}).status_code == 403


def test_fy_adv_004_settings_problems_are_findings(owner, monkeypatch):
    """FY-ADV-004: the settings checks (FY-CFG-*) show up with their own ids and scopes."""
    monkeypatch.setenv("GATEWAY_CLOUD_DEFUALT", "on")
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    found = findings()
    assert found["setting-unknown"][0].scope == "setting:GATEWAY_CLOUD_DEFUALT"
    assert found["private-hosts-allowed"][0].severity == "warning"
    assert found["setup-token-left"][0].severity == "info"


# ------------------------------------------------------------------ server and network
def test_fy_adv_005_host_on_loopback_while_machines_joined_from_elsewhere(owner, monkeypatch):
    """FY-ADV-005 (V2): HOST=127.0.0.1 and a machine that joined from another address: an error.
    Not when every machine is local (an SSH tunnel), with HOST=0.0.0.0, with a trusted proxy, or
    with a PUBLIC_URL elsewhere (a proxy on this machine)."""
    nid = node("tunnelled", ip="127.0.0.1")
    assert "host-loopback" not in findings()
    edit(Node, nid, ip="192.168.1.21")
    f = findings()["host-loopback"][0]
    assert f.severity == "error" and "tunnelled" in f.detail and "HOST=0.0.0.0" in f.detail
    for env, value in (("HOST", "0.0.0.0"), ("TRUSTED_PROXIES", "127.0.0.1"), ("PUBLIC_URL", "https://gpu.example.org")):
        monkeypatch.setenv(env, value)
        get_settings.cache_clear()
        assert "host-loopback" not in findings(), env
        monkeypatch.delenv(env)
        get_settings.cache_clear()


def test_fy_adv_006_public_url_unset_on_a_server_others_reach(owner, monkeypatch):
    """FY-ADV-006 (V3): no PUBLIC_URL with HOST=0.0.0.0: info; a warning when the admin opened the
    page as a loopback address (the links made there say 127.0.0.1). Nothing on a loopback-only
    server, or once PUBLIC_URL is set."""
    assert "public-url-unset" not in findings()
    monkeypatch.setenv("HOST", "0.0.0.0")
    get_settings.cache_clear()
    assert findings(request_host="192.168.1.20")["public-url-unset"][0].severity == "info"
    f = findings(request_host="127.0.0.1")["public-url-unset"][0]
    assert f.severity == "warning" and "127.0.0.1" in f.detail
    assert owner.get("/api/advisor").json()["findings"]  # localhost from the test client
    assert next(f for f in owner.get("/api/advisor").json()["findings"] if f["id"] == "public-url-unset")["severity"] == "warning"
    monkeypatch.setenv("PUBLIC_URL", "https://192.168.1.20:7860")
    get_settings.cache_clear()
    assert "public-url-unset" not in findings()


def test_fy_adv_007_certificates_that_do_not_fit_or_expire(owner, monkeypatch, tmp_path):
    """FY-ADV-007 (V5, V8): an own certificate that does not name PUBLIC_URL's host is an error
    (a wildcard covers one label); an own certificate expiring within 30 days is a warning, within 14
    an error; Forgeyard's CA within 90 days a warning. A fresh CA is fine."""
    from forgeyard.security import tls

    ca = tmp_path / "ca"
    tls.make_ca(ca, days=3650)
    tls.make_server_cert(ca, ["*.example.org"], ["192.168.1.20"], days=20)
    monkeypatch.setenv("TLS_CERT_FILE", str(ca / "server.crt"))
    monkeypatch.setenv("TLS_KEY_FILE", str(ca / "server.key"))
    monkeypatch.setenv("PUBLIC_URL", "https://gpu.example.org:7860")
    get_settings.cache_clear()
    found = findings()
    assert "certificate-name-missing" not in found
    assert found["certificate-expiring"][0].severity == "warning" and "Your server certificate" in found["certificate-expiring"][0].title
    for url in ("https://a.b.example.org", "https://192.168.1.21:7860", "https://example.org"):
        monkeypatch.setenv("PUBLIC_URL", url)
        get_settings.cache_clear()
        assert findings()["certificate-name-missing"][0].severity == "error", url
    monkeypatch.setenv("PUBLIC_URL", "https://192.168.1.20:7860")
    tls.make_server_cert(ca, ["gpu.lan"], ["192.168.1.20"], days=10)
    get_settings.cache_clear()
    found = findings()
    assert "certificate-name-missing" not in found and found["certificate-expiring"][0].severity == "error"
    for env in ("TLS_CERT_FILE", "TLS_KEY_FILE", "PUBLIC_URL"):
        monkeypatch.delenv(env)
    get_settings.cache_clear()
    data = get_settings().tls_dir
    tls.make_ca(data, days=3650)
    assert "certificate-expiring" not in findings()
    for f in ("ca.crt", "ca.key"):
        (data / f).unlink()
    tls.make_ca(data, days=60)
    f = findings()["certificate-expiring"][0]
    assert f.severity == "warning" and "certificate authority" in f.title and f.fix_url == "/trust"


def test_fy_adv_008_people_waiting_for_approval(owner, monkeypatch):
    """FY-ADV-008 (V19): pending accounts are an info finding on Users."""
    assert "registrations-pending" not in findings()
    add(User(username="newbie", role="user", status="pending"))
    f = findings()["registrations-pending"][0]
    assert f.severity == "info" and "newbie" in f.detail and f.pages == ("users",)


# ------------------------------------------------------------------ machines
def test_fy_adv_009_agents_older_than_the_server(owner):
    """FY-ADV-009 (V21): an older agent version is a warning, an older protocol an error; the same
    version (or a newer one, or none reported) is fine."""
    a = node("same")
    b = node("newer", agent_version="99.0.0")
    c = node("older", agent_version="1.0.9")
    d = node("old-protocol", protocol=0)
    found = {f.scope: f.severity for f in findings().get("agent-outdated", [])}
    assert found == {f"machine:{c}": "warning", f"machine:{d}": "error"}
    assert f"machine:{a}" not in found and f"machine:{b}" not in found
    assert advisor._version("v1.2.10rc1") == (1, 2, 10) and advisor._version("dev") == ()


def test_fy_adv_010_gpus_used_by_others_and_no_choice_made(owner):
    """FY-ADV-010 (V22): a machine whose cards hold memory outside Forgeyard (at least 1 GiB more than
    its reservations) and whose GPU access nobody chose: a warning. Not with an allowlist, not once
    an admin saved a choice (even "all"), not when the use is Forgeyard's own."""
    nid = node("server-a")
    for a_index in range(8):
        edit(Accelerator, gpu(nid, a_index).id, vram_used=300 * 1024**2)
    assert "gpus-shared-no-allowlist" not in findings()
    edit(Accelerator, gpu(nid, 4).id, vram_used=34 * GIB)
    f = findings()["gpus-shared-no-allowlist"][0]
    assert f.scope == f"machine:{nid}" and "#4 (34.0 GiB)" in f.detail
    # Forgeyard's own replica holds 33.5 GiB there: no longer "others".
    deployment("mine", replica={"gpus": [{"key": gpu(nid, 4).key}], "vram": int(33.5 * GIB), "state": "healthy",
                                "ready_at": utcnow() - timedelta(minutes=10)}, node_id=nid)
    assert "gpus-shared-no-allowlist" not in findings()
    edit(Accelerator, gpu(nid, 5).id, vram_used=40 * GIB)
    assert "gpus-shared-no-allowlist" in findings()
    r = owner.put(f"/api/machines/{nid}/gpu-allowlist", json={"keys": None})
    assert r.status_code == 200, r.text
    assert "gpus-shared-no-allowlist" not in findings()


def test_fy_adv_011_machine_cache_runtimes_and_simulation(owner):
    """FY-ADV-011 (V23, V24, V26, V27): an online agent without a model cache (warning with GPUs, info
    without); process workloads refused (info, with the agent's reason); a model cache under 10% free
    (warning); simulated hardware (warning). Offline machines are only checked for simulation."""
    nid = node("gpu-01", features=("workloads",), runtime_info={"process": {"ok": False, "reason": "no --workload-user"}},
               model_cache={"ok": True, "free_bytes": 9 * GIB, "total_bytes": 100 * GIB, "cached": []}, simulated=True)
    found = findings()
    assert found["machine-no-models"][0].severity == "warning"
    assert "no --workload-user" in found["process-workloads-off"][0].detail
    assert found["model-cache-full"][0].severity == "warning" and "9.0 GiB of 100.0 GiB" in found["model-cache-full"][0].detail
    assert found["machine-simulated"][0].severity == "warning"
    edit(Node, nid, features=["workloads", "models"], runtime_info={"process": {"ok": True}},
         model_cache={"ok": True, "free_bytes": 11 * GIB, "total_bytes": 100 * GIB}, simulated=False)
    found = findings()
    assert not {"machine-no-models", "process-workloads-off", "model-cache-full", "machine-simulated"} & set(found)
    edit(Node, nid, features=["workloads"], status="offline")
    assert "machine-no-models" not in findings()
    db = new_session()
    try:
        for a in db.scalars(select(Accelerator).where(Accelerator.node_id == nid, Accelerator.vendor != "cpu")):
            db.delete(a)
        db.commit()
    finally:
        db.close()
    edit(Node, nid, status="online")
    assert findings()["machine-no-models"][0].severity == "info"


def test_fy_adv_012_offline_machine_still_holding_replicas(owner):
    """FY-ADV-012 (V25): offline for over 5 minutes with replicas placed there: a warning naming them.
    Not within the first 5 minutes, not without replicas."""
    nid = node("gpu-03", status="offline", last_heartbeat_at=utcnow() - timedelta(hours=2))
    assert "machine-offline-with-replicas" not in findings()
    deployment("bge", replica={"state": "healthy", "gpus": []}, node_id=nid)
    f = findings()["machine-offline-with-replicas"][0]
    assert "bge" in f.detail and f.scope == f"machine:{nid}"
    edit(Node, nid, last_heartbeat_at=utcnow() - timedelta(minutes=2))
    assert "machine-offline-with-replicas" not in findings()


# ------------------------------------------------------------------ deployments
def test_fy_adv_013_a_card_with_a_running_replica_is_almost_full(owner):
    """FY-ADV-013 (V28): a card where a settled replica holds memory and less than the headroom
    (512 MiB) is free: a warning naming the deployment. Not while the replica is starting (its
    numbers are not in yet), not with room left."""
    nid = node("server-b")
    a = gpu(nid, 2)
    edit(Accelerator, a.id, vram_free=300 * 1024**2, vram_used=79 * GIB, updated_at=utcnow())
    deployment("qwen3-4b", replica={"gpus": [{"key": a.key}], "vram": 14 * GIB, "state": "starting"}, node_id=nid)
    assert "replica-margin-low" not in findings()
    db = new_session()
    try:
        r = db.scalar(select(Replica))
        r.state, r.ready_at = "healthy", utcnow() - timedelta(minutes=5)
        db.commit()
    finally:
        db.close()
    f = findings()["replica-margin-low"][0]
    assert "qwen3-4b" in f.detail and f.scope == f"gpu:{nid}:2" and "0.3 GiB is free" in f.detail
    edit(Accelerator, a.id, vram_free=2 * GIB)
    assert "replica-margin-low" not in findings()


def test_fy_adv_014_a_deployment_waiting_for_room(owner):
    """FY-ADV-014 (V29): a replica with no machine for over 15 minutes: a warning with the reason. Not
    before, and not for a stopped deployment."""
    did = deployment("big", replica={"state": "pending", "reason": "needs 70 GiB per card",
                                     "updated_at": utcnow() - timedelta(minutes=20)})
    f = findings()["deployment-waiting"][0]
    assert f.scope == f"deployment:{did}" and "needs 70 GiB per card" in f.detail and f.fix_url == f"/models/deployments/{did}"
    deployment("fresh", replica={"state": "pending", "updated_at": utcnow() - timedelta(minutes=5)})
    deployment("halted", desired="stopped", replica={"state": "pending", "updated_at": utcnow() - timedelta(hours=1)})
    assert [f.scope for f in findings()["deployment-waiting"]] == [f"deployment:{did}"]


def test_fy_adv_015_extra_engine_arguments_that_override_managed_flags(owner):
    """FY-ADV-015 (V31): extra arguments repeating a flag the recipe sets (any spelling: --flag=value,
    llama.cpp's long names for -c / -ngl) or --api-key: a warning. Other flags and values are fine."""
    from forgeyard.models import catalog

    db = new_session()
    try:
        llama = advisor.managed_flags(catalog.get_recipe(db, "llama-cpp"))
        vllm = advisor.managed_flags(catalog.get_recipe(db, "vllm"))
    finally:
        db.close()
    assert {"--ctx-size", "--n-gpu-layers", "--host", "--port", "--api-key", "--parallel"} <= llama
    assert {"--gpu-memory-utilization", "--max-model-len", "--host", "--port", "--api-key"} <= vllm
    ok = deployment("fine", params={"extra_args": ["--flash-attn", "on", "-1"]})
    bad = deployment("clash", params={"extra_args": ["--ctx-size=32768", "--api-key", "x", "-ngl", "10"]})
    v = deployment("v", recipe="vllm", params={"extra_args": ["--gpu-memory-utilization", "0.9"]})
    found = {f.scope: f for f in findings()["extra-args-override"]}
    assert set(found) == {f"deployment:{bad}", f"deployment:{v}"} and f"deployment:{ok}" not in found
    assert "--api-key, --ctx-size, --n-gpu-layers are set by Forgeyard" in found[f"deployment:{bad}"].detail


# ------------------------------------------------------------------ gateway, cloud and money
def _cloud_world():
    """A route chat-m: a local deployment (tier 0), then OpenAI (tier 1)."""
    pid = provider()
    pm = pmodel(pid)
    did = deployment("chat-m")
    rid = route("chat-m", [("local", did, 0), ("provider", pm, 1)], deployment_id=did)
    return pid, pm, did, rid


def test_fy_adv_016_a_key_that_may_spend_with_no_budget(owner):
    """FY-ADV-016 (V35): a key whose effective cloud setting is not Never, that reaches an unbudgeted
    provider: a warning per key. Not with a key budget, a person budget, a provider budget, a key
    limited to other models, or a key set to Never (or capped to it)."""
    pid, *_ = _cloud_world()
    spill = key("spill", cloud="fallback")
    key("never", cloud="off")
    key("other-models", cloud="on", models=["something-else"])
    key("budgeted", cloud="on", budget_micros=5_000_000)
    add_user(owner, "ursula")
    db = new_session()
    try:
        uid = db.scalar(select(User.id).where(User.username == "ursula"))
    finally:
        db.close()
    capped = key("capped", user_id=uid, cloud="on")   # a user's own key, cap off: effectively Never
    f = findings()["key-cloud-without-budget"]
    assert [x.scope for x in f] == [f"key:{spill}"] and "Only to spill" in f[0].detail and "cloudy" in f[0].detail
    owner.put("/api/keys/cloud", json={"max": "on"})
    assert {x.scope for x in findings()["key-cloud-without-budget"]} == {f"key:{spill}", f"key:{capped}"}
    edit(User, uid, budget_micros=1_000_000)
    assert [x.scope for x in findings()["key-cloud-without-budget"]] == [f"key:{spill}"]
    edit(Provider, pid, budget_micros=10_000_000)
    assert "key-cloud-without-budget" not in findings()


def test_fy_adv_017_cloud_models_without_a_price(owner):
    """FY-ADV-017 (V36, V37): a route's cloud model missing its input or output price is an error
    once any budget exists (402 budget_unpriced), and info without budgets. Embeddings need only the
    input price; a model no route uses, or a provider inside your network, is not checked."""
    pid, pm, *_ = _cloud_world()
    assert "cloud-model-unpriced" not in findings() and "cloud-model-cost-unknown" not in findings()
    edit(ProviderModel, pm, price_output=None)
    assert findings()["cloud-model-cost-unknown"][0].severity == "info"
    key("k", budget_micros=1_000_000)
    f = findings()["cloud-model-unpriced"][0]
    assert f.severity == "error" and f.scope == f"provider-model:{pm}" and f.pages == ("providers", f"provider:{pid}")
    emb = pmodel(pid, "text-embed", api="openai-embeddings", price_output=None)
    pmodel(pid, "unused", price_input=None)
    route("emb", [("provider", emb, 0)])
    assert [x.scope for x in findings()["cloud-model-unpriced"]] == [f"provider-model:{pm}"]
    edit(Provider, pid, external=False, kind="openai_compatible", base_url="https://llm.lan/v1")
    assert "cloud-model-unpriced" not in findings()


def test_fy_adv_018_providers_without_a_key_blocked_or_wrongly_inside(owner, monkeypatch):
    """FY-ADV-018 (V38, V39, V46): a kind that needs a key and has none: error; a host left out of
    EGRESS_ALLOWED_HOSTS: error; a provider marked inside your network whose host resolves only to
    public addresses: warning (not for a LAN address, an unresolvable name, or a disabled provider)."""
    a = provider("openai-nokey", key_last4=None)
    provider("own-vllm", kind="openai_compatible", external=False, key_last4=None, base_url="https://llm.lan/v1")
    t = provider("together", kind="openai_compatible", external=False, key_last4=None,
                 base_url="https://api.together.example/v1")
    provider("nowhere", kind="openai_compatible", external=False, key_last4=None, base_url="https://gone.example/v1")
    provider("off", key_last4=None, enabled=False)
    found = findings()
    assert [f.scope for f in found["provider-no-key"]] == [f"provider:{a}"] and found["provider-no-key"][0].severity == "error"
    assert [f.scope for f in found["provider-inside-public-address"]] == [f"provider:{t}"]
    assert "104.18.1.1" in found["provider-inside-public-address"][0].detail
    assert "provider-blocked-by-egress" not in found
    monkeypatch.setenv("EGRESS_ALLOWED_HOSTS", "*.together.example,llm.lan,gone.example")
    get_settings.cache_clear()
    blocked = findings()["provider-blocked-by-egress"]
    assert [f.scope for f in blocked] == [f"provider:{a}"] and "api.openai.com" in blocked[0].detail


def test_fy_adv_019_cloud_for_keys_open_by_default_or_unreachable(owner):
    """FY-ADV-019 (V40, V47): with a cloud route, a default above Never or a maximum of Allowed is info
    (a warning when some key also has no budget); with everything at Never and no key allowed, the
    cloud routes are unreachable (info). Nothing without a cloud route."""
    assert "cloud-default-open" not in findings() and "cloud-unreachable" not in findings()
    owner.put("/api/keys/cloud", json={"default": "fallback", "max": "on"})
    assert "cloud-default-open" not in findings()            # no cloud route yet
    _cloud_world()
    assert findings()["cloud-default-open"][0].severity == "info"
    key("k", cloud="fallback")
    assert findings()["cloud-default-open"][0].severity == "warning"
    owner.put("/api/keys/cloud", json={"default": "off", "max": "off"})
    db = new_session()
    try:
        db.query(ApiKey).delete()
        db.commit()
    finally:
        db.close()
    f = findings()
    assert "cloud-default-open" not in f and f["cloud-unreachable"][0].severity == "info"
    key("k2", cloud="on")    # the owner's own key: an admin's key is never capped
    assert "cloud-unreachable" not in findings()


def test_fy_adv_020_routes_with_tiers_that_cannot_work_as_meant(owner):
    """FY-ADV-020 (V41-V44): a cloud tier before the first local one (warning); a Local only route
    with cloud targets, or a spill rule with nothing after the local tier (info); no usable target
    (error); a mixed route whose local deployment is stopped (warning)."""
    pid, pm, did, rid = _cloud_world()
    assert not {"route-cloud-before-local", "route-cloud-never-used", "route-no-target", "route-local-stopped"} & set(findings())
    d2 = deployment("first-cloud")
    r2 = route("first-cloud", [("provider", pm, 0), ("local", d2, 1)])
    d3 = deployment("lonely")
    r3 = route("lonely", [("local", d3, 0)], spill_on_long_prompt=True)
    r4 = route("dead", [("provider", pm, 0, False)])
    edit(GatewayRoute, rid, local_only=True)
    found = findings()
    assert [f.scope for f in found["route-cloud-before-local"]] == [f"route:{r2}"]
    assert sorted(f.scope for f in found["route-cloud-never-used"]) == sorted([f"route:{rid}", f"route:{r3}"])
    assert [f.scope for f in found["route-no-target"]] == [f"route:{r4}"] and found["route-no-target"][0].severity == "error"
    edit(Provider, pid, enabled=False)
    assert {f.scope for f in findings()["route-no-target"]} == {f"route:{r4}"}  # chat-m still has its local target
    edit(Provider, pid, enabled=True)
    edit(GatewayRoute, rid, local_only=False)
    db = new_session()
    try:
        db.get(Deployment, did).workload.desired_state = "stopped"
        db.commit()
    finally:
        db.close()
    f = findings()["route-local-stopped"][0]
    assert f.scope == f"route:{rid}" and "chat-m is stopped" in f.detail


def test_fy_adv_021_a_budget_of_zero(owner):
    """FY-ADV-021 (V45): a budget of $0 on a key, a person or a provider is info (empty is no budget)."""
    k = key("ci", budget_micros=0)
    key("none", budget_micros=None)
    p = provider("zero", budget_micros=0)
    edit(User, owner_id(), budget_micros=0)
    assert sorted(f.scope for f in findings()["budget-zero"]) == sorted([f"key:{k}", f"provider:{p}", f"person:{owner_id()}"])


def test_fy_adv_022_old_openrouter_prices(owner):
    """FY-ADV-022 (V48): prices refreshed from OpenRouter over 60 days ago, on a model a route uses."""
    _, pm, *_ = _cloud_world()
    edit(ProviderModel, pm, price_source="openrouter", price_checked_at=utcnow() - timedelta(days=30))
    assert "prices-stale" not in findings()
    edit(ProviderModel, pm, price_checked_at=utcnow() - timedelta(days=61))
    assert "gpt-mini" in findings()["prices-stale"][0].detail


def test_fy_adv_023_keys_that_never_expire_and_sit_unused(owner):
    """FY-ADV-023 (V50): active keys without expiry, unused (or never used) for 90 days: one info
    finding listing them. Not a key used recently, one that expires, or a revoked one."""
    old = utcnow() - timedelta(days=120)
    key("dusty", created_at=old)
    key("used", created_at=old, last_used_at=utcnow() - timedelta(days=3))
    key("expiring", created_at=old, expires_at=utcnow() + timedelta(days=5))
    key("revoked", created_at=old, revoked_at=utcnow())
    f = findings()["keys-idle-never-expire"]
    assert len(f) == 1 and "dusty (owner)" in f[0].detail and "used" not in f[0].detail.replace("unused", "")
    assert f[0].title.startswith("1 key never expires")


# ------------------------------------------------------------------ docs
def _ids_in_code() -> set[str]:
    ids = set()
    for f in (ROOT / "forgeyard" / "advisor.py", ROOT / "forgeyard" / "settings_check.py"):
        ids |= set(re.findall(r'(?:Finding|Problem)\(\s*"([a-z-]+)"', f.read_text()))
    return ids


def test_fy_adv_024_every_check_is_documented_and_every_link_lands(owner):
    """FY-ADV-024: each finding id is in the reference's Setup health table in both languages, and every
    fix link into the guide names a page and a section that exist."""
    from forgeyard import guide

    ids = _ids_in_code()
    assert len(ids) >= 35
    for lang in ("en", "zh-TW"):
        text = (guide.directory(lang) / "reference.md").read_text()
        for i in ids:
            assert f"`{i}`" in text, (lang, i)
    links = set(re.findall(r'"(/guide/en/[a-z-]+#[a-z0-9-]+)"', (ROOT / "forgeyard" / "advisor.py").read_text()))
    links |= set(re.findall(r'"/guide/en/reference#[a-z0-9-]+', (ROOT / "forgeyard" / "settings_check.py").read_text()))
    links |= {f"/guide/en/reference#{a}" for a in re.findall(r'REFERENCE\}#([a-z0-9-]+)', (ROOT / "forgeyard" / "advisor.py").read_text()
                                                             + (ROOT / "forgeyard" / "settings_check.py").read_text())}
    assert len(links) >= 8
    for link in links:
        page, anchor = re.fullmatch(r"/guide/en/([a-z-]+)#(.+)", link.strip('"')).groups()
        for lang in ("en", "zh-TW"):
            html = guide.page(lang, page)["html"]
            assert f'id="{anchor}"' in html, (lang, link)


def test_fy_adv_025_a_failing_check_does_not_take_the_panel_down(owner, monkeypatch, caplog):
    """FY-ADV-025: a check that raises is logged and skipped; the others still answer."""
    def broken(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(advisor, "CHECKS", [broken, *advisor.CHECKS])
    r = owner.get("/api/advisor")
    assert r.status_code == 200 and any(f["id"] == "secret-key-backup" for f in r.json()["findings"])
    assert any("the check broken failed" in rec.getMessage() for rec in caplog.records)
    with pytest.raises(RuntimeError):
        db = new_session()
        try:
            advisor.run(db, get_settings(), strict=True)
        finally:
            db.close()
