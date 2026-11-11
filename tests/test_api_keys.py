"""API keys through the console API: shown once, stored as a hash, who sees and changes which keys,
defaults and caps, the audit log (FY-KEY-*)."""

import hashlib
import json

from sqlalchemy import select, text

from forgeyard.db import ApiKey, AuditEvent, get_engine, new_session
from forgeyard.gateway import keys as gkeys
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import new_key
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

PW = "quiet-meadow-4821"


def _row(public_id):
    db = new_session()
    try:
        return db.scalar(select(ApiKey).where(ApiKey.public_id == public_id))
    finally:
        db.close()


def _audit(action):
    db = new_session()
    try:
        return [(e.actor_name, e.target, e.detail) for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action))]
    finally:
        db.close()


def test_fy_key_001_shown_once_stored_as_a_hash(owner):
    """FY-KEY-001: the key comes back once; the server keeps its SHA-256 and a 12-character prefix,
    and the key itself is nowhere in the database or in a listing."""
    out = new_key(owner, "laptop", expires_in_days=30)
    key = out["key"]
    assert key.startswith("fyk_") and len(key) == 47 and out["gateway_url"] == "http://localhost/v1"
    k = out["api_key"]
    assert k["prefix"] == key[:12] and k["status"] == "active" and k["models"] is None and k["expires_at"]
    row = _row(k["id"])
    assert row.hash == hashlib.sha256(key.encode()).hexdigest() and row.prefix == key[:12]
    listing = owner.get("/api/keys").json()
    assert key not in json.dumps(listing) and listing["keys"][0]["prefix"] == key[:12]
    assert listing["gateway_url"] == "http://localhost/v1"
    with get_engine().connect() as conn:
        for table in ("api_keys", "audit_events"):
            dump = json.dumps([list(map(str, r)) for r in conn.execute(text(f"SELECT * FROM {table}"))])
            assert key not in dump and key[12:] not in dump
    second = new_key(owner, "laptop")["key"]
    assert second != key


def test_fy_key_002_people_manage_their_own_keys_admins_all(owner):
    """FY-KEY-002: a user sees, changes and revokes only their own keys (someone else's is 404); an
    admin lists everyone's with ?all=true, revokes any, and changes only the limits of others' keys."""
    add_user(owner, "ursula")
    add_user(owner, "olga", role="operator")
    u, o = sign_in(owner, "ursula", PW), sign_in(owner, "olga", PW)
    uk = new_key(u, "ursula-key")["api_key"]
    ok = new_key(o, "olga-key")["api_key"]
    mine = new_key(owner, "owner-key")["api_key"]
    assert [k["name"] for k in u.get("/api/keys").json()["keys"]] == ["ursula-key"]
    assert [k["name"] for k in u.get("/api/keys?all=true").json()["keys"]] == ["ursula-key"]  # not an admin
    assert sorted(k["name"] for k in owner.get("/api/keys?all=true").json()["keys"]) == ["olga-key", "owner-key",
                                                                                         "ursula-key"]
    assert [k["name"] for k in owner.get("/api/keys").json()["keys"]] == ["owner-key"]
    for c, other in ((u, ok), (o, uk), (u, mine)):
        assert c.delete(f"/api/keys/{other['id']}").status_code == 404
        assert c.patch(f"/api/keys/{other['id']}", json={"rpm": 5}).status_code == 404
    assert u.patch(f"/api/keys/{uk['id']}", json={"name": "renamed", "rpm": 7}).json()["api_key"]["rpm"] == 7
    r = owner.patch(f"/api/keys/{uk['id']}", json={"rpm": 2, "tpm": 1000})
    assert r.status_code == 200 and (r.json()["api_key"]["rpm"], r.json()["api_key"]["tpm"]) == (2, 1000)
    assert owner.patch(f"/api/keys/{uk['id']}", json={"name": "admin-rename"}).status_code == 403
    assert owner.patch(f"/api/keys/{uk['id']}", json={"models": []}).status_code == 403
    r = owner.delete(f"/api/keys/{ok['id']}")
    assert r.status_code == 200 and r.json()["api_key"]["status"] == "revoked"
    assert r.json()["api_key"]["revoked_by"] == "owner"
    assert o.patch(f"/api/keys/{ok['id']}", json={"rpm": 3}).status_code == 409
    assert u.put("/api/keys/limits", json={"default_rpm": 1}).status_code == 403
    created = _audit("api_key.created")
    assert {t for _a, t, _d in created} == {"ursula-key", "olga-key", "owner-key"}
    assert all("prefix" in d and "hash" not in d for _a, _t, d in created)
    changed = _audit("api_key.changed")
    assert ("owner", "renamed", {"key": uk["id"], "owner": "ursula",
                                 "changes": {"rpm": {"before": 7, "after": 2}, "tpm": {"before": None, "after": 1000},
                                             "rpm_locked_by": {"before": None, "after": "owner"},
                                             "tpm_locked_by": {"before": None, "after": "owner"}},
                                 "locked": ["rpm", "tpm"]}) in changed
    assert [(a, t) for a, t, _d in _audit("api_key.revoked")] == [("owner", "olga-key")]


def test_fy_key_003_defaults_and_caps(owner):
    """FY-KEY-003: admins set defaults and caps (audited); a key without a limit gets the default held
    to the cap; a user may set their own key's limits only up to the cap, an admin anything."""
    assert owner.get("/api/keys/limits").json()["limits"] == {"default_rpm": None, "default_tpm": None,
                                                             "max_rpm": None, "max_tpm": None}
    r = owner.put("/api/keys/limits", json={"default_rpm": 60, "default_tpm": 0, "max_rpm": 30, "max_tpm": 5000})
    assert r.json()["limits"] == {"default_rpm": 60, "default_tpm": None, "max_rpm": 30, "max_tpm": 5000}
    [(_a, _t, d)] = _audit("api_key_limits.changed")
    assert d["before"]["max_rpm"] is None and d["after"]["max_rpm"] == 30
    add_user(owner, "ursula")
    u = sign_in(owner, "ursula", PW)
    k = new_key(u, "plain")["api_key"]
    assert (k["effective_rpm"], k["effective_tpm"]) == (30, 5000)  # default 60 held to the cap 30
    r = u.post("/api/keys", json={"name": "greedy", "rpm": 31})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "over_cap"
    assert u.post("/api/keys", json={"name": "greedy", "tpm": 5001}).status_code == 422
    low = new_key(u, "low", rpm=3, tpm=100)["api_key"]
    assert (low["effective_rpm"], low["effective_tpm"]) == (3, 100)
    assert u.patch(f"/api/keys/{low['id']}", json={"rpm": 40}).status_code == 422
    assert u.patch(f"/api/keys/{low['id']}", json={"rpm": None}).json()["api_key"]["effective_rpm"] == 30
    big = owner.patch(f"/api/keys/{low['id']}", json={"rpm": 500}).json()["api_key"]
    assert big["effective_rpm"] == 500  # an admin may go past the cap
    assert gkeys.effective(None, None, None) is None and gkeys.effective(None, None, 9) == 9
    assert gkeys.effective(None, 5, None) == 5 and gkeys.effective(7, 5, 3) == 7


def test_fy_key_004_model_allowlists_name_real_deployments(owner):
    """FY-KEY-004"""
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    deploy(owner, "embed", "qwen3-embedding-0.6b", "vllm-pooling")
    k = new_key(owner, "e", models=["embed", "embed"])["api_key"]
    assert k["models"] == ["embed"]
    r = owner.post("/api/keys", json={"name": "x", "models": ["embed", "typo"]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_model"
    assert owner.post("/api/keys", json={"name": "x", "models": []}).json()["detail"]["code"] == "no_models"
    assert owner.patch(f"/api/keys/{k['id']}", json={"models": None}).json()["api_key"]["models"] is None
    listing = owner.get("/api/keys").json()
    assert listing["models"] == [{"name": "embed", "api": "openai-embeddings", "kind": "local"}]


def test_fy_key_005_deleting_a_user_deletes_their_keys(owner):
    """FY-KEY-005"""
    add_user(owner, "temp")
    t = sign_in(owner, "temp", PW)
    new_key(t, "k")
    uid = next(x["id"] for x in owner.get("/api/users").json()["users"] if x["username"] == "temp")
    assert owner.delete(f"/api/users/{uid}").status_code == 200
    assert owner.get("/api/keys?all=true").json()["keys"] == []


def test_fy_key_006_limits_an_admin_sets_are_locked_for_the_owner(owner):
    """FY-KEY-006: an admin sets a limit on someone else's key: the owner can no longer change that value
    (403 limit_locked, the value unchanged), may send it back unchanged, may still change the other
    limit and revoke the key. The key says who locked it. An admin changes it further, and unlocks it
    by clearing it (back to the default, the owner's again). All of it is in the audit log."""
    add_user(owner, "ursula")
    u = sign_in(owner, "ursula", PW)
    k = new_key(u, "mine", rpm=10, tpm=1000)["api_key"]
    assert (k["rpm_locked_by"], k["tpm_locked_by"]) == (None, None)
    r = owner.patch(f"/api/keys/{k['id']}", json={"rpm": 3})
    assert r.status_code == 200 and r.json()["api_key"]["rpm_locked_by"] == "owner"
    assert r.json()["api_key"]["tpm_locked_by"] is None
    listed = u.get("/api/keys").json()["keys"][0]
    assert (listed["rpm"], listed["rpm_locked_by"]) == (3, "owner")
    for value in (5, 2, None):
        r = u.patch(f"/api/keys/{k['id']}", json={"rpm": value})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "limit_locked"
        assert "owner" in r.json()["detail"]["message"]
    assert _row(k["id"]).rpm == 3
    r = u.patch(f"/api/keys/{k['id']}", json={"rpm": 3, "tpm": 500, "name": "renamed"})  # rpm unchanged: fine
    assert r.status_code == 200 and (r.json()["api_key"]["rpm"], r.json()["api_key"]["tpm"]) == (3, 500)
    assert r.json()["api_key"]["tpm_locked_by"] is None
    r = owner.patch(f"/api/keys/{k['id']}", json={"rpm": 4})
    assert r.json()["api_key"]["rpm"] == 4 and r.json()["api_key"]["rpm_locked_by"] == "owner"
    r = owner.patch(f"/api/keys/{k['id']}", json={"rpm": None})
    assert (r.json()["api_key"]["rpm"], r.json()["api_key"]["rpm_locked_by"]) == (None, None)
    assert u.patch(f"/api/keys/{k['id']}", json={"rpm": 6}).json()["api_key"]["rpm"] == 6  # unlocked
    owner.patch(f"/api/keys/{k['id']}", json={"tpm": 50})
    assert u.patch(f"/api/keys/{k['id']}", json={"tpm": 60}).status_code == 403
    assert u.delete(f"/api/keys/{k['id']}").json()["api_key"]["status"] == "revoked"  # still theirs to revoke
    changes = [d for _a, t, d in _audit("api_key.changed") if d["key"] == k["id"]]
    assert changes[0]["locked"] == ["rpm"] and changes[0]["changes"]["rpm_locked_by"] == {"before": None, "after": "owner"}
    unlock = next(d for d in changes if d.get("unlocked"))
    assert unlock["unlocked"] == ["rpm"] and unlock["changes"]["rpm"] == {"before": 4, "after": None}
    assert [d.get("locked") for d in changes].count(["tpm"]) == 1


def test_fy_key_007_an_admins_own_keys_are_never_locked(owner):
    """FY-KEY-007: on their own key an admin's limits are their own (no lock), and a locked key's caps
    still do not apply to the admin's value (an admin may go past the cap)."""
    mine = new_key(owner, "mine")["api_key"]
    r = owner.patch(f"/api/keys/{mine['id']}", json={"rpm": 7, "tpm": 70})
    assert (r.json()["api_key"]["rpm_locked_by"], r.json()["api_key"]["tpm_locked_by"]) == (None, None)
    owner.put("/api/keys/limits", json={"max_rpm": 10})
    add_user(owner, "ursula")
    u = sign_in(owner, "ursula", PW)
    k = new_key(u, "k")["api_key"]
    r = owner.patch(f"/api/keys/{k['id']}", json={"rpm": 500})
    assert r.json()["api_key"]["effective_rpm"] == 500 and r.json()["api_key"]["rpm_locked_by"] == "owner"
    # the owner saves the form with the locked value unchanged and a new tpm: allowed, although 500 > cap
    r = u.patch(f"/api/keys/{k['id']}", json={"rpm": 500, "tpm": 99})
    assert r.status_code == 200 and r.json()["api_key"]["tpm"] == 99


def test_fy_key_008_a_partial_change_of_the_key_limits_keeps_the_others(owner):
    """FY-KEY-008: PUT /api/keys/limits changes only the fields it names; null clears one (no limit),
    and a field left out keeps its saved value."""
    owner.put("/api/keys/limits", json={"default_rpm": 60, "default_tpm": 600, "max_rpm": 120, "max_tpm": 6000})
    r = owner.put("/api/keys/limits", json={"max_rpm": 100})
    assert r.status_code == 200
    assert r.json()["limits"] == {"default_rpm": 60, "default_tpm": 600, "max_rpm": 100, "max_tpm": 6000}
    r = owner.put("/api/keys/limits", json={"default_tpm": None})
    assert r.json()["limits"] == {"default_rpm": 60, "default_tpm": None, "max_rpm": 100, "max_tpm": 6000}
