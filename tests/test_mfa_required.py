"""Required two-factor sign-in (1.6.0, #39, PR 2): the `mfa_required` setting and its grace period, the session that
can only add an authenticator, Setup health, and `forgeyard users reset-mfa` (FY-MFA-024…).

Expected values come from the requirement: off changes nothing; admins / everyone; the grace period counts from the
later of the setting and the account; overdue → 403 `mfa_enrolment_required` everywhere but the setup, who-am-I and
sign-out; API keys never affected; nobody (the owner included) left without a way in."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard import advisor, cli
from forgeyard.config import get_settings
from forgeyard.db import MfaFactor, RecoveryCode, User, new_session, reset_engine, utcnow
from forgeyard.security import mfa, mfa_policy
from tests.conftest import OWNER, add_user, sign_in
from tests.gateway_helpers import api_client, new_key
from tests.test_mfa import T0, audit, browser, enrol, rows, user_id

PW = "quiet-meadow-4821"
DUE = "mfa_enrolment_required"
DAY = timedelta(days=1)


@pytest.fixture()
def clock(monkeypatch):
    """The time authenticator codes are checked at (as in test_mfa.py): fixed, so the codes the test makes match."""
    t = [T0]
    monkeypatch.setattr(mfa, "clock", lambda: t[0])
    return t


@pytest.fixture()
def now(monkeypatch):
    """The time the grace period is measured at: the real time to start with, moved by the test (now[0] += …)."""
    t = [utcnow()]
    monkeypatch.setattr(mfa_policy, "now", lambda: t[0])
    return t


def require(c, level, days=7, status=200):
    r = c.put("/api/users/mfa-policy", json={"mfa_required": level, "mfa_grace_days": days})
    assert r.status_code == status, r.text
    return r.json()


def me(c):
    r = c.get("/api/auth/state")
    assert r.status_code == 200, r.text
    return r.json()["user"]


def code_of(r):
    return r.json()["detail"]["code"]


def findings(fid):
    db = new_session()
    try:
        return [f for f in advisor.run(db, get_settings()) if f.id == fid]
    finally:
        db.close()


# ------------------------------------------------------------------ off: nothing changes
def test_fy_mfa_024_off_by_default_and_nothing_changes(owner, now):
    """FY-MFA-024: the setting is off with 7 days of grace by default; with it off, a person without an authenticator
    gets no deadline and every page answers, however much time passes."""
    p = owner.get("/api/users/mfa-policy").json()
    assert (p["mfa_required"], p["mfa_grace_days"], p["in_grace_period"], p["setup_overdue"]) == ("off", 7, 0, 0)
    add_user(owner, "uma", password=PW)
    uma = sign_in(owner, "uma", PW)
    now[0] += 400 * DAY
    for c in (owner, uma):
        assert me(c)["mfa_enrolment"] is None
        assert c.get("/api/machines").status_code == 200
        assert c.get("/api/keys").status_code == 200
    assert all(u["mfa_enrolment"] is None for u in owner.get("/api/users").json()["users"])
    assert findings("mfa-setup-pending") == []


# ------------------------------------------------------------------ the setting
def test_fy_mfa_025_only_admins_set_it_within_bounds_and_each_change_is_audited(owner, now):
    """FY-MFA-025: only admins read or change it (403 forbidden otherwise); the level is off | admins | everyone and
    the grace period 0–90 days (422 outside); each real change is audited once with before and after."""
    add_user(owner, "uma", password=PW)
    uma = sign_in(owner, "uma", PW)
    assert code_of(uma.get("/api/users/mfa-policy")) == "forbidden"
    assert code_of(uma.put("/api/users/mfa-policy", json={"mfa_required": "everyone"})) == "forbidden"
    for bad in ({"mfa_required": "all"}, {"mfa_required": "admins", "mfa_grace_days": 91},
                {"mfa_required": "admins", "mfa_grace_days": -1}):
        assert owner.put("/api/users/mfa-policy", json=bad).status_code == 422
    assert require(owner, "admins", 90)["mfa_grace_days"] == 90
    require(owner, "admins", 90)  # the same again: no new audit entry
    require(owner, "everyone", 0)
    assert [(a, d["before"], d["after"]) for a, _, d in audit("mfa_policy.changed")] == [
        ("owner", {"mfa_required": "off", "mfa_grace_days": 7}, {"mfa_required": "admins", "mfa_grace_days": 90}),
        ("owner", {"mfa_required": "admins", "mfa_grace_days": 90}, {"mfa_required": "everyone", "mfa_grace_days": 0}),
    ]


# ------------------------------------------------------------------ the grace period and the overdue session
def test_fy_mfa_026_grace_period_boundary_then_only_the_setup(owner, now, clock):
    """FY-MFA-026: required of admins with 7 days: the owner (no authenticator) has until the moment the setting
    was switched on + 7 days; one second before it everything answers, at it every console API answers 403
    mfa_enrolment_required (the Playground too, and the admin part of /api/tls goes) while who-am-I, the
    two-factor setup and sign-out still work; adding an authenticator ends it at once."""
    start = now[0]
    require(owner, "admins", 7)
    e = me(owner)["mfa_enrolment"]
    assert e["due"] is False and e["by"].startswith((start + 7 * DAY).isoformat()[:19])
    now[0] = start + 7 * DAY - timedelta(seconds=1)
    assert owner.get("/api/machines").status_code == 200
    assert "admin" in owner.get("/api/tls").json()
    now[0] = start + 7 * DAY
    assert me(owner)["mfa_enrolment"]["due"] is True
    for method, path, body in (("get", "/api/machines", None), ("get", "/api/keys", None), ("get", "/api/users", None),
                               ("put", "/api/users/mfa-policy", {"mfa_required": "off"}),
                               ("get", "/api/auth/sessions", None), ("post", "/api/auth/password",
                                                                      {"current_password": OWNER[1], "new_password": "x" * 20}),
                               ("get", "/api/engine-updates", None),  # a route that calls current() itself
                               ("get", "/api/playground/vision/options", None),
                               ("post", "/api/playground/chat/completions", {"model": "m", "messages": []})):
        r = getattr(owner, method)(path, **({"json": body} if body is not None else {}))
        assert (r.status_code, code_of(r)) == (403, DUE), path
    assert "admin" not in owner.get("/api/tls").json()
    assert owner.get("/api/auth/me").json()["user"]["mfa_enrolment"]["due"] is True
    assert owner.get("/api/auth/mfa/factors").status_code == 200
    _, r = enrol(owner, clock)
    assert r.status_code == 200, r.text
    assert me(owner)["mfa_enrolment"] is None
    assert owner.get("/api/machines").status_code == 200
    assert owner.get("/api/auth/sessions").json()["sessions"][0]["two_factor"] is True
    assert owner.post("/api/auth/logout").status_code == 200


def test_fy_mfa_027_admins_or_everyone_and_who_keeps_which_deadline(owner, now):
    """FY-MFA-027: `admins` covers admins only; `everyone` covers all; widening from admins to everyone gives the
    others a new grace period and keeps the admins' deadline; narrowing back or off removes the deadlines."""
    add_user(owner, "uma", password=PW)
    add_user(owner, "otto", role="operator", password=PW)
    uma = sign_in(owner, "uma", PW)
    otto = sign_in(owner, "otto", PW)
    t0 = now[0]
    require(owner, "admins", 3)
    assert me(uma)["mfa_enrolment"] is None and me(otto)["mfa_enrolment"] is None
    now[0] = t0 + 10 * DAY
    assert uma.get("/api/machines").status_code == 200
    assert code_of(owner.get("/api/machines")) == DUE
    set_level("everyone")  # (the overdue owner cannot change it through the API any more)
    for c in (uma, otto):
        e = me(c)["mfa_enrolment"]
        assert e["due"] is False and e["by"].startswith((t0 + 13 * DAY).isoformat()[:19])
    assert me(owner)["mfa_enrolment"]["by"].startswith((t0 + 3 * DAY).isoformat()[:19])
    now[0] = t0 + 13 * DAY
    assert code_of(uma.get("/api/machines")) == DUE and code_of(otto.get("/api/machines")) == DUE
    set_level("admins")
    assert uma.get("/api/machines").status_code == 200 and me(otto)["mfa_enrolment"] is None
    assert code_of(owner.get("/api/machines")) == DUE
    set_level("off")
    assert owner.get("/api/machines").status_code == 200 and me(owner)["mfa_enrolment"] is None


def set_level(level, days=3):
    db = new_session()
    try:
        mfa_policy.save(db, level, days, "test")
        db.commit()
    finally:
        db.close()


def test_fy_mfa_028_an_account_made_later_gets_its_own_grace_period(owner, now):
    """FY-MFA-028: the grace period counts from the later of the setting and the account's creation: someone added
    5 days after `everyone` was switched on still has the full 7 days from their own start."""
    t0 = now[0]
    require(owner, "everyone", 7)
    now[0] = t0 + 5 * DAY
    db = new_session()
    try:
        add_user(owner, "late", password=PW)
        late_user = db.scalar(select(User).where(User.username == "late"))
        late_user.created_at = now[0]
        db.commit()
    finally:
        db.close()
    late = sign_in(owner, "late", PW)
    assert me(late)["mfa_enrolment"]["by"].startswith((t0 + 12 * DAY).isoformat()[:19])
    now[0] = t0 + 12 * DAY - timedelta(seconds=1)
    assert late.get("/api/keys").status_code == 200
    now[0] = t0 + 12 * DAY
    assert code_of(late.get("/api/keys")) == DUE


def test_fy_mfa_029_api_keys_keep_working_when_the_owner_is_overdue(owner, now):
    """FY-MFA-029: an API key of a person whose setup is overdue keeps answering on /v1 (the gateway never looks at
    the setting), while the same person's console calls answer 403."""
    add_user(owner, "uma", password=PW)
    uma = sign_in(owner, "uma", PW)
    key = new_key(uma, "laptop")["key"]
    t0 = now[0]
    require(owner, "everyone", 1)
    now[0] = t0 + 2 * DAY
    assert code_of(uma.get("/api/keys")) == DUE
    api = api_client(owner, key)
    assert api.get("/v1/models").status_code == 200
    assert api_client(owner, "fyk_" + "x" * 40).get("/v1/models").status_code == 401


def test_fy_mfa_030_overdue_people_still_sign_in_with_their_password_and_can_add_one(owner, now, clock):
    """FY-MFA-030: nobody is locked out by the setting, the owner included: with 0 days of grace, a fresh password
    sign-in works (one step, a session), says the setup is due, and the setup can be finished from it."""
    require(owner, "everyone", 0)
    b = browser(owner)
    r = b.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]})
    assert r.status_code == 200 and r.json()["user"]["mfa_enrolment"]["due"] is True
    assert code_of(b.get("/api/machines")) == DUE
    _, r = enrol(b, clock)
    assert r.status_code == 200, r.text
    assert b.get("/api/machines").status_code == 200


def test_fy_mfa_031_users_list_and_counts_show_who_still_has_to_add_it(owner, now, clock):
    """FY-MFA-031: Users shows each person's deadline (none for those with an authenticator or not covered), and the
    setting's answer counts the people in the grace period and past it."""
    add_user(owner, "uma", password=PW)
    add_user(owner, "erin", role="admin", password=PW)
    enrol(owner, clock)
    t0 = now[0]
    require(owner, "admins", 2)
    by = {u["username"]: u["mfa_enrolment"] for u in owner.get("/api/users").json()["users"]}
    assert by["owner"] is None and by["uma"] is None and by["erin"]["due"] is False
    p = owner.get("/api/users/mfa-policy").json()
    assert (p["in_grace_period"], p["setup_overdue"]) == (1, 0)
    now[0] = t0 + DAY
    p = require(owner, "everyone", 2)
    assert (p["in_grace_period"], p["setup_overdue"]) == (2, 0)
    now[0] = t0 + 2.5 * DAY
    p = owner.get("/api/users/mfa-policy").json()
    assert (p["in_grace_period"], p["setup_overdue"]) == (1, 1)  # uma's period began at the switch to everyone


def test_fy_mfa_032_setup_health_warns_about_admins_and_counts_the_people_left(owner, now, clock):
    """FY-MFA-032: admins without an authenticator are info while nothing is required and a warning once the setting
    covers admins; a second note counts the people still in the grace period and past it; both go once everyone
    has an authenticator."""
    add_user(owner, "uma", password=PW)
    (f,) = findings("admins-without-mfa")
    assert f.severity == "info"
    t0 = now[0]
    require(owner, "admins", 7)
    (f,) = findings("admins-without-mfa")
    assert f.severity == "warning"
    (p,) = findings("mfa-setup-pending")
    assert p.severity == "info" and p.title == "1 person still to add two-factor sign-in" and "owner" in p.detail
    assert "uma" not in p.detail and p.pages == ("users",)
    set_level("everyone", 7)
    now[0] = t0 + 7 * DAY
    (p,) = findings("mfa-setup-pending")
    assert p.title == "2 people still to add two-factor sign-in"
    assert "1 person still in the grace period (uma" in p.detail and "1 person past it (owner)" in p.detail
    set_level("everyone", 30)
    enrol(owner, clock)
    assert findings("admins-without-mfa") == []
    uma = sign_in(owner, "uma", PW)
    enrol(uma, clock, password=PW)
    assert findings("mfa-setup-pending") == []


# ------------------------------------------------------------------ break-glass
def test_fy_mfa_033_cli_reset_mfa_is_the_owners_way_back_in(owner, now, clock):
    """FY-MFA-033: `forgeyard users reset-mfa NAME` on the server host removes the account's authenticators and
    recovery codes, ends its sessions, and records user.mfa_reset with the actor `cli`; the owner then signs in with
    the password alone. An unknown name exits 2 and changes nothing; an account without a factor exits 0, no entry."""
    enrol(owner, clock)
    assert rows(MfaFactor) == 1 and rows(RecoveryCode) == 10
    assert cli.main(["users", "reset-mfa", "nobody"]) == 2
    assert rows(MfaFactor) == 1 and audit("user.mfa_reset") == []
    assert cli.main(["users", "reset-mfa", "OWNER"]) == 0
    assert rows(MfaFactor) == 0 and rows(RecoveryCode) == 0
    assert owner.get("/api/auth/me").status_code == 401
    ((actor, target, detail),) = audit("user.mfa_reset")
    assert (actor, target, detail["factors"]) == ("cli", "owner", 1) and detail["sessions_ended"] >= 1
    b = browser(owner)
    r = b.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]})
    assert r.status_code == 200 and "user" in r.json()
    assert cli.main(["users", "reset-mfa", "owner"]) == 0
    assert len(audit("user.mfa_reset")) == 1
    assert user_id("owner") is not None


def test_fy_mfa_034_cli_reset_mfa_refuses_a_missing_database(tmp_path, monkeypatch):
    """FY-MFA-034: pointed at a data directory without a database, it exits 2 and creates none."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "elsewhere"))
    get_settings.cache_clear()
    reset_engine()
    assert cli.main(["users", "reset-mfa", "owner"]) == 2
    assert not (tmp_path / "elsewhere" / "forgeyard.db").exists()
