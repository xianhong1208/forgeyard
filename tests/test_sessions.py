"""Server-side sessions: cookie flags, sign-out, timeouts, ending sessions (FY-SES-*)."""

from datetime import timedelta

from fastapi.testclient import TestClient

from forgeyard.db import Session, get_db, utcnow
from forgeyard.security.sessions import COOKIE, token_hash
from tests.conftest import OWNER, add_user, make_client, sign_in


def _db():
    return next(get_db())


def _cookie_header(response) -> str:
    return next(v for k, v in response.headers.multi_items() if k == "set-cookie" and v.startswith(COOKIE))


def test_fy_ses_001_cookie_flags_on_http(owner):
    """FY-SES-001: HttpOnly + SameSite=Strict; not Secure over plain http."""
    r = owner.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]})
    header = _cookie_header(r).lower()
    assert "httponly" in header and "samesite=strict" in header and "secure" not in header.replace("samesite", "")


def test_fy_ses_002_secure_cookie_on_https(owner):
    """FY-SES-002"""
    https = TestClient(owner.app, base_url="https://localhost")
    https.headers["X-Forgeyard"] = "1"
    r = https.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]})
    assert "; secure" in _cookie_header(r).lower()


def test_fy_ses_003_only_the_hash_is_stored(owner):
    """FY-SES-003"""
    token = owner.cookies.get(COOKIE)
    db = _db()
    assert db.get(Session, token) is None and db.get(Session, token_hash(token)) is not None


def test_fy_ses_004_copied_cookie_dies_with_sign_out(owner):
    """FY-SES-004: a cookie copied before sign-out gets 401 on the very next request."""
    copied = owner.cookies.get(COOKIE)
    assert owner.post("/api/auth/logout").status_code == 200
    thief = TestClient(owner.app, base_url="http://localhost", cookies={COOKIE: copied})
    r = thief.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"]["reason"] == "signed_out"


def test_fy_ses_005_idle_timeout_is_enforced_by_the_server(owner):
    """FY-SES-005"""
    db = _db()
    row = db.get(Session, token_hash(owner.cookies.get(COOKIE)))
    row.last_seen_at = utcnow() - timedelta(minutes=121)
    db.commit()
    r = owner.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"]["reason"] == "idle"


def test_fy_ses_006_activity_keeps_a_session_alive(owner):
    """FY-SES-006: last_seen_at moves forward, so an active session does not idle out."""
    db = _db()
    row = db.get(Session, token_hash(owner.cookies.get(COOKIE)))
    row.last_seen_at = utcnow() - timedelta(minutes=110)
    db.commit()
    assert owner.get("/api/auth/me").status_code == 200
    db.expire_all()
    assert utcnow() - db.get(Session, row.id).last_seen_at.replace(tzinfo=utcnow().tzinfo) < timedelta(minutes=1)


def test_fy_ses_007_absolute_timeout_despite_activity(owner):
    """FY-SES-007"""
    db = _db()
    row = db.get(Session, token_hash(owner.cookies.get(COOKIE)))
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    r = owner.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"]["reason"] == "expired"


def test_fy_ses_008_password_change_ends_other_sessions_only(owner):
    """FY-SES-008"""
    other = sign_in(owner, *OWNER)
    r = owner.post("/api/auth/password", json={"current_password": OWNER[1], "new_password": "copper-kettle-5530"})
    assert r.status_code == 200 and r.json()["other_sessions_ended"] == 1
    assert owner.get("/api/auth/me").status_code == 200
    r = other.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"]["reason"] == "password_changed"


def test_fy_ses_009_wrong_current_password_is_400_not_401(owner):
    """FY-SES-009: a typo must not look like an ended session (the UI signs out on 401)."""
    r = owner.post("/api/auth/password", json={"current_password": "nope-nope-nope", "new_password": "copper-kettle-5530"})
    assert r.status_code == 400
    assert owner.get("/api/auth/me").status_code == 200


def test_fy_ses_010_disabling_a_user_ends_their_sessions(owner):
    """FY-SES-010"""
    add_user(owner, "bob")
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    uid = bob.get("/api/auth/me").json()["user"]["id"]
    assert owner.patch(f"/api/users/{uid}", json={"status": "disabled"}).status_code == 200
    r = bob.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"]["reason"] == "account_disabled"
    r = TestClient(owner.app, base_url="http://localhost", headers={"X-Forgeyard": "1"}).post(
        "/api/auth/login", json={"username": "bob", "password": "quiet-meadow-4821"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_disabled"


def test_fy_ses_011_admin_force_sign_out(owner):
    """FY-SES-011"""
    add_user(owner, "bob")
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    uid = bob.get("/api/auth/me").json()["user"]["id"]
    assert owner.post(f"/api/users/{uid}/sign-out").json()["ended"] == 1
    r = bob.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"]["reason"] == "signed_out_by_admin"


def test_fy_ses_012_my_sessions_and_ending_one(owner):
    """FY-SES-012"""
    other = sign_in(owner, *OWNER)
    listed = owner.get("/api/auth/sessions").json()["sessions"]
    assert len(listed) == 2 and sum(s["current"] for s in listed) == 1
    assert all(len(s["id"]) == 36 for s in listed)  # public ids, never the token or its hash
    target = next(s for s in listed if not s["current"])
    assert owner.delete(f"/api/auth/sessions/{target['id']}").status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    assert owner.get("/api/auth/me").status_code == 200


def test_fy_ses_013_cannot_end_someone_elses_session(owner):
    """FY-SES-013"""
    add_user(owner, "bob")
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    owner_session = owner.get("/api/auth/sessions").json()["sessions"][0]["id"]
    assert bob.delete(f"/api/auth/sessions/{owner_session}").status_code == 404
    assert owner.get("/api/auth/me").status_code == 200


def test_fy_ses_014_end_other_sessions(owner):
    """FY-SES-014"""
    a, b = sign_in(owner, *OWNER), sign_in(owner, *OWNER)
    assert owner.post("/api/auth/sessions/end-others").json()["ended"] == 2
    assert a.get("/api/auth/me").status_code == 401 and b.get("/api/auth/me").status_code == 401
    assert owner.get("/api/auth/me").status_code == 200


def test_fy_ses_015_wrong_password_is_401_invalid_credentials(owner):
    """FY-SES-015"""
    c = make_client()
    r = c.post("/api/auth/login", json={"username": OWNER[0], "password": "harbour-lantern-72"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"
    r = c.post("/api/auth/login", json={"username": "nobody", "password": "harbour-lantern-72"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"


def test_fy_ses_016_state_says_why_the_session_ended(owner):
    """FY-SES-016: opening the app with an ended session tells the sign-in page why."""
    copied = owner.cookies.get(COOKIE)
    owner.post("/api/auth/logout")
    thief = TestClient(owner.app, base_url="http://localhost", cookies={COOKIE: copied})
    state = thief.get("/api/auth/state").json()
    assert state["user"] is None and state["ended"] == "signed_out"
    assert make_client().get("/api/auth/state").json()["ended"] is None


def test_fy_ses_017_disabled_status_alone_refuses_the_session(owner):
    """FY-SES-017: even a session that was not revoked stops working once the account is disabled."""
    from forgeyard.db import User

    add_user(owner, "bob")
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    db = _db()
    user = db.query(User).filter_by(username="bob").one()
    user.status = "disabled"  # straight in the database: no revoke_all
    db.commit()
    r = bob.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"]["reason"] == "account_disabled"


def test_fy_ses_018_re_enabling_does_not_revive_old_sessions(owner):
    """FY-SES-018: disabling revokes the sessions, so enabling again needs a fresh sign-in."""
    add_user(owner, "bob")
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    uid = bob.get("/api/auth/me").json()["user"]["id"]
    owner.patch(f"/api/users/{uid}", json={"status": "disabled"})
    owner.patch(f"/api/users/{uid}", json={"status": "active"})
    r = bob.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"]["reason"] == "account_disabled"
    sign_in(owner, "bob", "quiet-meadow-4821")
