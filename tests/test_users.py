"""Admin-created accounts and one-time set-password links (FY-USR-*)."""

from datetime import timedelta

from forgeyard.db import PasswordLink, get_db, utcnow
from forgeyard.security.sessions import token_hash
from tests.conftest import link_token, make_client, sign_in


def _create(owner, name="carol", role="operator"):
    r = owner.post("/api/users", json={"username": name, "role": role, "email": f"{name}@example.com"})
    assert r.status_code == 201
    return r.json()


def test_fy_usr_001_created_user_gets_a_link_not_a_password(owner):
    """FY-USR-001"""
    body = _create(owner)
    assert body["user"]["has_password"] is False and body["user"]["role"] == "operator"
    assert body["set_password_url"].startswith("http://localhost/set-password#token=")
    r = make_client().post("/api/auth/login", json={"username": "carol", "password": "anything-at-all-1"})
    assert r.status_code == 401


def test_fy_usr_002_link_sets_the_password_once(owner):
    """FY-USR-002"""
    token = link_token(_create(owner)["set_password_url"])
    c = make_client()
    assert c.post("/api/auth/password-link/check", json={"token": token}).json()["username"] == "carol"
    assert c.post("/api/auth/password-link", json={"token": token, "password": "maple-signal-9034"}).status_code == 200
    again = c.post("/api/auth/password-link", json={"token": token, "password": "maple-signal-9035"})
    assert again.status_code == 404 and again.json()["detail"]["code"] == "link_invalid"
    sign_in(owner, "carol", "maple-signal-9034")


def test_fy_usr_003_link_password_follows_the_policy(owner):
    """FY-USR-003"""
    token = link_token(_create(owner)["set_password_url"])
    r = make_client().post("/api/auth/password-link", json={"token": token, "password": "carol-carol-carol"})
    assert r.status_code == 422
    # The link is still usable after a refused password.
    assert make_client().post("/api/auth/password-link/check", json={"token": token}).status_code == 200


def test_fy_usr_004_expired_link(owner):
    """FY-USR-004"""
    token = link_token(_create(owner)["set_password_url"])
    db = next(get_db())
    db.get(PasswordLink, token_hash(token)).expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert make_client().post("/api/auth/password-link/check", json={"token": token}).status_code == 404


def test_fy_usr_005_reset_link_replaces_the_old_one_and_ends_sessions(owner):
    """FY-USR-005"""
    first = link_token(_create(owner)["set_password_url"])
    make_client().post("/api/auth/password-link", json={"token": first, "password": "maple-signal-9034"})
    carol = sign_in(owner, "carol", "maple-signal-9034")
    uid = carol.get("/api/auth/me").json()["user"]["id"]
    r1 = link_token(owner.post(f"/api/users/{uid}/password-link").json()["set_password_url"])
    r2 = link_token(owner.post(f"/api/users/{uid}/password-link").json()["set_password_url"])
    c = make_client()
    assert c.post("/api/auth/password-link/check", json={"token": r1}).status_code == 404
    assert c.post("/api/auth/password-link", json={"token": r2, "password": "river-stone-2210"}).status_code == 200
    assert carol.get("/api/auth/me").status_code == 401
    sign_in(owner, "carol", "river-stone-2210")


def test_fy_usr_006_link_url_uses_public_url(owner, monkeypatch):
    """FY-USR-006"""
    from forgeyard.config import get_settings
    monkeypatch.setenv("PUBLIC_URL", "https://forge.lan/")
    get_settings.cache_clear()
    body = _create(owner)
    assert body["set_password_url"].startswith("https://forge.lan/set-password#token=")


def test_fy_usr_007_usernames(owner):
    """FY-USR-007"""
    assert owner.post("/api/users", json={"username": "Bad Name"}).status_code == 422
    assert owner.post("/api/users", json={"username": "OWNER"}).status_code == 409


def test_fy_usr_008_bad_token(client):
    """FY-USR-008"""
    assert client.post("/api/auth/password-link/check", json={"token": "nope"}).status_code == 404
