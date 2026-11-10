"""Join tokens, joining and the machines API (FY-MCH-*)."""

from datetime import timedelta

import pytest

from forgeyard.config import get_settings
from forgeyard.db import JoinToken, Node, get_db, utcnow
from forgeyard.machines import store
from forgeyard.security.sessions import token_hash
from tests.conftest import add_user, make_client, sign_in
from tests.machines_helpers import agent_client, join, joined, new_token


def _db():
    return next(get_db())


def test_fy_mch_001_admin_creates_a_join_token_shown_once(owner):
    """FY-MCH-001: the token comes back once, with the command; only its hash is stored."""
    t = new_token(owner)
    assert t["token"].startswith("fyj_") and len(t["token"]) > 40
    assert t["valid_minutes"] == 15
    assert t["command"] == f"uv run --no-sync python -m forgeyard agent --server http://localhost --join-token {t['token']}"
    db = _db()
    row = db.query(JoinToken).one()
    assert row.id == token_hash(t["token"]) and t["token"] not in (row.id, row.public_id)
    listed = owner.get("/api/machines/join-tokens").json()["tokens"]
    assert listed == [{"id": t["id"], "created_at": listed[0]["created_at"], "expires_at": listed[0]["expires_at"],
                       "created_by": "owner"}]
    assert "token" not in listed[0]


def test_fy_mch_002_join_token_is_single_use(owner):
    """FY-MCH-002"""
    t = new_token(owner)["token"]
    first = join(owner, t, "box-a")
    assert first.status_code == 200
    second = join(owner, t, "box-b")
    assert second.status_code == 403 and second.json()["detail"]["code"] == "join_token_invalid"
    assert _db().query(Node).count() == 1


def test_fy_mch_003_join_token_expires_after_15_minutes(owner):
    """FY-MCH-003"""
    t = new_token(owner)["token"]
    db = _db()
    row = db.get(JoinToken, token_hash(t))
    assert timedelta(minutes=14, seconds=50) < row.expires_at.replace(tzinfo=None) - row.created_at.replace(tzinfo=None) <= timedelta(minutes=15)
    assert store.redeem_join_token(db, t, now=utcnow() + timedelta(minutes=15, seconds=1)) is None
    db.rollback()
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    r = join(owner, t)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "join_token_invalid"


def test_fy_mch_004_credential_is_returned_once_and_stored_hashed(owner):
    """FY-MCH-004"""
    j = joined(owner)
    assert j["credential"].startswith("fym_") and j["name"] == "gpu-box"
    assert j["heartbeat_interval"] == 10 and j["offline_after"] == 30
    node = _db().get(Node, j["node_id"])
    assert node.credential_hash == token_hash(j["credential"]) and j["credential"] not in str(node.credential_hash)
    assert "credential" not in str(owner.get("/api/machines").json())


def test_fy_mch_005_unknown_tokens_are_refused_and_rate_limited(owner, monkeypatch):
    """FY-MCH-005"""
    monkeypatch.setenv("LOGIN_IP_FAILURES", "3")
    get_settings.cache_clear()
    for guess in ("fyj_nope", "", "x" * 150):
        assert join(owner, guess).status_code == 403
    r = join(owner, new_token(owner)["token"])
    assert r.status_code == 429  # even a good token waits once the IP has guessed too often
    events = owner.get("/api/audit?action=machine.join_refused").json()["events"]
    assert len(events) == 3


def test_fy_mch_006_old_protocol_does_not_burn_the_token(owner):
    """FY-MCH-006"""
    t = new_token(owner)["token"]
    r = join(owner, t, protocol=99)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "protocol_unsupported"
    assert join(owner, t).status_code == 200


def test_fy_mch_007_names_are_cleaned_and_unique(owner):
    """FY-MCH-007"""
    assert joined(owner, "gpu box!")["name"] == "gpu-box"
    assert joined(owner, "gpu-box")["name"] == "gpu-box-2"
    assert joined(owner, "")["name"] == "machine"


def test_fy_mch_008_cancelled_token_cannot_join(owner):
    """FY-MCH-008"""
    t = new_token(owner)
    assert owner.delete(f"/api/machines/join-tokens/{t['id']}").status_code == 200
    assert join(owner, t["token"]).status_code == 403
    assert owner.get("/api/machines/join-tokens").json()["tokens"] == []
    assert owner.delete(f"/api/machines/join-tokens/{t['id']}").status_code == 404


def test_fy_mch_009_audit_trail(owner):
    """FY-MCH-009: token created, machine joined, revoked, removed."""
    j = joined(owner, "box-1")
    owner.post(f"/api/machines/{j['node_id']}/revoke")
    owner.delete(f"/api/machines/{j['node_id']}")
    events = owner.get("/api/audit?action=machine.").json()["events"]
    actions = [e["action"] for e in reversed(events)]
    assert actions == ["machine.join_token_created", "machine.joined", "machine.revoked", "machine.removed"]
    joined_event = events[2]
    assert joined_event["target"] == "box-1" and joined_event["actor"] == "owner"
    assert joined_event["detail"]["machine_id"] == j["node_id"]


MANAGE = [("post", "/api/machines/join-tokens"), ("get", "/api/machines/join-tokens"),
          ("delete", "/api/machines/join-tokens/x"), ("post", "/api/machines/1/revoke"), ("delete", "/api/machines/1")]


@pytest.mark.parametrize("role", ["operator", "user"])
def test_fy_mch_010_everyone_sees_machines_only_admins_manage(owner, role):
    """FY-MCH-010"""
    j = joined(owner)
    add_user(owner, "bob", role=role)
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    r = bob.get("/api/machines")
    assert r.status_code == 200 and r.json()["can_manage"] is False
    assert [m["name"] for m in r.json()["machines"]] == ["gpu-box"]
    for method, path in MANAGE:
        r = getattr(bob, method)(path.replace("/1/", f"/{j['node_id']}/"))
        assert r.status_code == 403, (method, path)
    assert owner.get("/api/machines").json()["can_manage"] is True


def test_fy_mch_011_anonymous_gets_401(owner):
    """FY-MCH-011"""
    c = make_client()
    assert c.get("/api/machines").status_code == 401
    for method, path in MANAGE:
        assert getattr(c, method)(path).status_code == 401


def test_fy_mch_012_a_session_cookie_does_not_join(owner):
    """FY-MCH-012: the admin's cookie and CSRF header are no substitute for a join token."""
    r = owner.post("/agent/v1/join", json={"join_token": "", "protocol": 1, "name": "x"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "join_token_invalid"


def test_fy_mch_013_join_needs_no_csrf_header(owner):
    """FY-MCH-013: the agent is not a browser; console CSRF rules must not get in its way."""
    t = new_token(owner)["token"]
    r = agent_client(owner).post("/agent/v1/join", json={"join_token": t, "protocol": 1, "name": "a"})
    assert r.status_code == 200


@pytest.mark.parametrize("host, status", [("192.168.1.20:7860", 200), ("10.0.0.5", 200), ("forge.lan", 200),
                                          ("gpu-hub.tailnet.example:7860", 200), ("evil.example", 421)])
def test_fy_mch_014_host_guard_on_join(owner, monkeypatch, host, status):
    """FY-MCH-014: an agent reaching the server by LAN IP or an ALLOWED_HOSTS name works; others get 421."""
    monkeypatch.setenv("ALLOWED_HOSTS", "forge.lan,gpu-hub.tailnet.example")
    get_settings.cache_clear()
    t = new_token(owner)["token"]
    r = agent_client(owner, host).post("/agent/v1/join", json={"join_token": t, "protocol": 1, "name": "a"})
    assert r.status_code == status
    if status == 421:
        assert join(owner, t).status_code == 200  # the refused request did not spend the token


def test_fy_mch_015_revoke_and_remove(owner):
    """FY-MCH-015"""
    j = joined(owner)
    r = owner.post(f"/api/machines/{j['node_id']}/revoke")
    assert r.status_code == 200 and r.json()["machine"]["status"] == "revoked"
    assert r.json()["machine"]["revoked_by"] == "owner"
    assert _db().get(Node, j["node_id"]).credential_hash is None
    assert owner.post(f"/api/machines/{j['node_id']}/revoke").status_code == 409
    assert owner.delete(f"/api/machines/{j['node_id']}").status_code == 200
    assert owner.get("/api/machines").json()["machines"] == []
    assert owner.delete(f"/api/machines/{j['node_id']}").status_code == 404


def test_fy_mch_016_command_uses_public_url(owner, monkeypatch):
    """FY-MCH-016"""
    monkeypatch.setenv("PUBLIC_URL", "https://192.168.1.20:7860/")
    get_settings.cache_clear()
    t = new_token(owner)
    assert t["server_url"] == "https://192.168.1.20:7860"
    assert "--server https://192.168.1.20:7860 --join-token fyj_" in t["command"]


def test_fy_mch_017_two_redeems_racing_admit_one_machine(owner):
    """FY-MCH-017: a request that read the token as unused just before another spent it still loses."""
    from forgeyard.db import new_session

    t = new_token(owner)["token"]
    slow, fast = new_session(), new_session()
    try:
        stale = slow.get(JoinToken, token_hash(t))  # loaded while unused (kept: the identity map is weak)...
        assert stale.used_at is None
        slow.commit()
        assert store.redeem_join_token(fast, t) is not None  # ...then spent by someone else
        fast.commit()
        assert store.redeem_join_token(slow, t) is None
    finally:
        slow.close()
        fast.close()
