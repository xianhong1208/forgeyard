"""Two-factor sign-in (1.6.0, #39, PR 1): authenticator apps (TOTP, RFC 6238), recovery codes, the two-step sign-in,
re-authentication, an admin's reset, Setup health and the secret-key backup (FY-MFA-*).

Every secret here is made by the test or by the server under test (a temporary data dir): no real key material."""

import hashlib
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from forgeyard import advisor
from forgeyard.config import get_settings
from forgeyard.db import (AuditEvent, LoginChallenge, MfaFactor, RecoveryCode, Session, User, new_session, utcnow)
from forgeyard.security import mfa, totp
from tests.conftest import OWNER, add_user, sign_in

T0 = 1_900_000_020.0  # the start of a 30 s step


@pytest.fixture()
def clock(monkeypatch):
    """The time codes and enrolments are checked at: fixed, moved by the test (now[0] += 30)."""
    now = [T0]
    monkeypatch.setattr(mfa, "clock", lambda: now[0])
    return now


def browser(app_client) -> TestClient:
    c = TestClient(app_client.app, base_url="http://localhost")
    c.headers["X-Forgeyard"] = "1"
    return c


def code(key: bytes, at: float) -> str:
    return totp.code_at(key, at)


def enrol(c, clock, password=OWNER[1], name=None, **extra):
    """Add an authenticator for the signed-in account of `c`. Returns (key, response)."""
    s = c.post("/api/auth/mfa/totp/start")
    assert s.status_code == 200, s.text
    s = s.json()
    key = totp.secret_bytes(s["secret"])
    body = {"enrolment": s["enrolment"], "new_code": code(key, clock[0]), "name": name, **extra}
    if password is not None:
        body["password"] = password
    return key, c.post("/api/auth/mfa/totp", json=body)


def rows(model, **where):
    db = new_session()
    try:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return db.scalar(q)
    finally:
        db.close()


def audit(action):
    db = new_session()
    try:
        return [(e.actor_name, e.target, e.detail) for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action)
                                                                       .order_by(AuditEvent.id))]
    finally:
        db.close()


def user_id(name):
    db = new_session()
    try:
        return db.scalar(select(User.id).where(User.username == name))
    finally:
        db.close()


def password_step(app_client, username=OWNER[0], password=OWNER[1]):
    b = browser(app_client)
    r = b.post("/api/auth/login", json={"username": username, "password": password})
    return b, r


def code_step(b, challenge, value):
    return b.post("/api/auth/mfa", json={"challenge": challenge, "code": value})


# ------------------------------------------------------------------ TOTP itself
def test_fy_mfa_001_rfc_6238_appendix_b_and_rfc_4226_appendix_d_vectors():
    """FY-MFA-001: the standard-library implementation gives the RFCs' published values: HOTP (RFC 4226 Appendix D,
    6 digits) and TOTP (RFC 6238 Appendix B, 8 digits, SHA-1, SHA-256 and SHA-512 with their seeds)."""
    seed1 = b"12345678901234567890"
    hotp_d = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]
    assert [totp.hotp(seed1, i) for i in range(10)] == hotp_d
    seed256 = b"12345678901234567890123456789012"
    seed512 = b"1234567890123456789012345678901234567890123456789012345678901234"
    vectors = [  # time, SHA-1, SHA-256, SHA-512
        (59, "94287082", "46119246", "90693936"),
        (1111111109, "07081804", "68084774", "25091201"),
        (1111111111, "14050471", "67062674", "99943326"),
        (1234567890, "89005924", "91819424", "93441116"),
        (2000000000, "69279037", "90698825", "38618901"),
        (20000000000, "65353130", "77737706", "47863826"),
    ]
    for t, s1, s256, s512 in vectors:
        step = totp.step_at(t)
        assert totp.hotp(seed1, step, 8, hashlib.sha1) == s1
        assert totp.hotp(seed256, step, 8, hashlib.sha256) == s256
        assert totp.hotp(seed512, step, 8, hashlib.sha512) == s512
    # The defaults (SHA-1, 6 digits, 30 s) are the low 6 digits of the SHA-1 column.
    assert totp.code_at(seed1, 59) == "287082"


def test_fy_mfa_002_secrets_are_160_random_bits_and_the_uri_names_the_parameters():
    """FY-MFA-002: a new secret is 32 base32 characters (160 bits), different each time; only 160-bit base32 is read
    back; the otpauth:// URI carries the issuer, the account and SHA1/6/30."""
    a, b = totp.new_secret(), totp.new_secret()
    assert a != b and re.fullmatch(r"[A-Z2-7]{32}", a)
    assert len(totp.secret_bytes(a)) == 20 and totp.secret_bytes(a.lower()[:16] + " " + a.lower()[16:]) is not None
    assert totp.secret_bytes(a[:-8]) is None and totp.secret_bytes(a + "AAAAAAAA") is None
    assert totp.secret_bytes("1" * 32) is None and totp.secret_bytes("") is None
    uri = totp.otpauth_uri(a, "owner")
    assert uri.startswith("otpauth://totp/Forgeyard:owner?")
    assert f"secret={a}" in uri and "issuer=Forgeyard" in uri and "algorithm=SHA1" in uri
    assert "digits=6" in uri and "period=30" in uri


def test_fy_mfa_003_one_step_of_drift_each_way_and_never_a_step_at_or_before_the_last():
    """FY-MFA-003: a code matches now and one step either side, not two; a step at or before `after` never
    matches; spaces are ignored; anything but six digits never matches."""
    key = totp.secret_bytes(totp.new_secret())
    now = T0
    assert totp.match_step(key, code(key, now), now=now) == totp.step_at(now)
    assert totp.match_step(key, code(key, now - 30), now=now) == totp.step_at(now) - 1
    assert totp.match_step(key, code(key, now + 30), now=now) == totp.step_at(now) + 1
    assert totp.match_step(key, code(key, now - 60), now=now) is None
    assert totp.match_step(key, code(key, now + 60), now=now) is None
    c = code(key, now)
    assert totp.match_step(key, c, after=totp.step_at(now), now=now) is None
    assert totp.match_step(key, c, after=totp.step_at(now) - 1, now=now) == totp.step_at(now)
    assert totp.match_step(key, code(key, now - 30), after=totp.step_at(now) - 1, now=now) is None
    assert totp.match_step(key, f"{c[:3]} {c[3:]}", now=now) == totp.step_at(now)
    assert totp.match_step(key, c[:5], now=now) is None and totp.match_step(key, c + "0", now=now) is None


# ------------------------------------------------------------------ enrolment
def test_fy_mfa_004_adding_an_authenticator_needs_the_first_code_and_the_password(owner, clock):
    """FY-MFA-004: nothing is saved before the first code and the password are right; then the factor is listed
    (never its secret), its secret is stored encrypted and bound to its column, 10 recovery codes are answered once,
    this session counts as having proved a second factor, and the audit log has it."""
    s = owner.post("/api/auth/mfa/totp/start").json()
    assert set(s) == {"secret", "uri", "enrolment", "expires_in"} and s["secret"] in s["uri"]
    key = totp.secret_bytes(s["secret"])
    good = code(key, clock[0])
    wrong = f"{(int(good) + 1) % 1_000_000:06d}"
    base = {"enrolment": s["enrolment"], "name": "Pixel"}
    r = owner.post("/api/auth/mfa/totp", json={**base, "new_code": wrong, "password": OWNER[1]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "mfa_new_code_invalid"
    r = owner.post("/api/auth/mfa/totp", json={**base, "new_code": good})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "reauth_required"
    r = owner.post("/api/auth/mfa/totp", json={**base, "new_code": good, "password": "not-the-password-1"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "wrong_password"
    assert rows(MfaFactor) == 0 and rows(RecoveryCode) == 0
    r = owner.post("/api/auth/mfa/totp", json={**base, "new_code": good, "password": OWNER[1]})
    assert r.status_code == 200, r.text
    out = r.json()
    codes = out["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(re.fullmatch(r"[a-hjkmnp-z2-9]{4}(-[a-hjkmnp-z2-9]{4}){3}", c) for c in codes)
    assert [f["name"] for f in out["factors"]] == ["Pixel"] and out["recovery_codes_left"] == 10
    listed = owner.get("/api/auth/mfa/factors").json()
    assert s["secret"] not in str(listed) and listed["factors"][0]["kind"] == "totp"
    db = new_session()
    try:
        raw = db.execute(text("SELECT secret FROM mfa_factors")).scalar_one()
        stored = db.scalars(select(RecoveryCode.hash)).all()
    finally:
        db.close()
    assert raw.startswith("fy1:") and s["secret"] not in raw
    from forgeyard.security import crypto

    assert crypto.box().open(raw, "mfa_factors.secret") == s["secret"]
    with pytest.raises(crypto.SecretError):
        crypto.box().open(raw, "providers.api_key")
    assert sorted(stored) == sorted(hashlib.sha256(c.replace("-", "").encode()).hexdigest() for c in codes)
    me = owner.get("/api/auth/sessions").json()["sessions"]
    assert [x["two_factor"] for x in me] == [True]
    assert owner.get("/api/auth/me").json()["user"]["has_mfa"] is True
    (name, _, detail), = audit("auth.mfa_enrolled")
    assert name == "owner" and detail["factor"] == "Pixel" and detail["confirmed_with"] == "password"
    assert s["secret"] not in str(detail)


def test_fy_mfa_005_an_enrolment_is_this_servers_for_this_account_and_ten_minutes(owner, clock):
    """FY-MFA-005: the sealed enrolment cannot be changed, used for another account or used late; a made-up secret
    is never accepted in its place."""
    add_user(owner, "dana", role="operator")
    dana = sign_in(owner, "dana", "quiet-meadow-4821")
    s = owner.post("/api/auth/mfa/totp/start").json()
    key = totp.secret_bytes(s["secret"])

    def confirm(c, enrolment, password=OWNER[1]):
        return c.post("/api/auth/mfa/totp", json={"enrolment": enrolment, "new_code": code(key, clock[0]),
                                                   "password": password})

    for bad in (s["enrolment"][:-2] + ("AA" if not s["enrolment"].endswith("AA") else "BB"), s["secret"], "fy1:x:y"):
        r = confirm(owner, bad)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "mfa_enrolment_expired"
    r = confirm(dana, s["enrolment"], "quiet-meadow-4821")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "mfa_enrolment_expired"
    clock[0] += mfa.ENROL_TTL + 1
    r = confirm(owner, s["enrolment"])
    assert r.status_code == 400 and r.json()["detail"]["code"] == "mfa_enrolment_expired"
    clock[0] -= 2
    assert confirm(owner, s["enrolment"]).status_code == 200
    assert rows(MfaFactor) == 1


def test_fy_mfa_006_a_second_authenticator_takes_a_current_code_and_at_most_five(owner, clock):
    """FY-MFA-006: a second factor is confirmed with the password or a current code of the first one; it makes no
    new recovery codes; a sixth is refused."""
    key1, r = enrol(owner, clock, name="Phone")
    assert r.status_code == 200
    clock[0] += 30
    _, r = enrol(owner, clock, password=None, name="Tablet", code=code(key1, clock[0]))
    assert r.status_code == 200, r.text
    assert r.json()["recovery_codes"] is None and r.json()["recovery_codes_left"] == 10
    assert audit("auth.mfa_enrolled")[-1][2]["confirmed_with"] == "code"
    for i in range(3):
        _, r = enrol(owner, clock, name=f"spare {i}")
        assert r.status_code == 200
    r = owner.post("/api/auth/mfa/totp/start")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "mfa_factor_limit"
    assert rows(MfaFactor) == 5


# ------------------------------------------------------------------ signing in
def test_fy_mfa_007_with_a_factor_sign_in_is_two_steps(owner, clock):
    """FY-MFA-007: the right password answers a challenge and no session; the code then signs in (one session that
    counts as two-factor) and the audit log says how."""
    key, _ = enrol(owner, clock)
    clock[0] += 30
    b, r = password_step(owner)
    assert r.status_code == 200 and r.json()["mfa_required"] is True and r.json()["expires_in"] == 300
    assert "user" not in r.json() and not b.cookies.get("forgeyard_session")
    assert b.get("/api/auth/me").status_code == 401
    assert rows(LoginChallenge) == 1
    db = new_session()
    try:
        assert db.scalar(select(LoginChallenge.id)) == hashlib.sha256(r.json()["challenge"].encode()).hexdigest()
    finally:
        db.close()
    r2 = code_step(b, r.json()["challenge"], code(key, clock[0]))
    assert r2.status_code == 200, r2.text
    assert r2.json()["user"]["username"] == "owner" and r2.json()["user"]["has_mfa"] is True
    assert b.get("/api/auth/me").status_code == 200
    assert rows(LoginChallenge) == 0
    mine = [s for s in b.get("/api/auth/sessions").json()["sessions"] if s["current"]]
    assert mine[0]["two_factor"] is True
    assert audit("auth.login")[-1][2] == {"mfa": "totp"}


def test_fy_mfa_008_each_code_works_once_and_an_older_one_never_again(owner, clock):
    """FY-MFA-008 (RFC 6238 5.2): the code that signed in once does not sign in again (another challenge, same
    step); after a newer step was used, the previous step's code is refused too."""
    key, _ = enrol(owner, clock)
    clock[0] += 30
    b, r = password_step(owner)
    assert code_step(b, r.json()["challenge"], code(key, clock[0])).status_code == 200
    b2, r = password_step(owner)
    again = code_step(b2, r.json()["challenge"], code(key, clock[0]))
    assert again.status_code == 400 and again.json()["detail"]["code"] == "mfa_code_invalid"
    clock[0] += 30
    older = code_step(b2, r.json()["challenge"], code(key, clock[0] - 30))
    assert older.json()["detail"]["code"] == "mfa_code_invalid"
    assert code_step(b2, r.json()["challenge"], code(key, clock[0])).status_code == 200


def test_fy_mfa_009_a_clock_one_step_off_works_two_steps_off_does_not(owner, clock):
    """FY-MFA-009: the phone's clock may be one step ahead or behind; two steps is refused."""
    key, _ = enrol(owner, clock)
    clock[0] += 300
    b, r = password_step(owner)
    ch = r.json()["challenge"]
    assert code_step(b, ch, code(key, clock[0] + 60)).json()["detail"]["code"] == "mfa_code_invalid"
    assert code_step(b, ch, code(key, clock[0] - 60)).json()["detail"]["code"] == "mfa_code_invalid"
    assert code_step(b, ch, code(key, clock[0] - 30)).status_code == 200
    clock[0] += 300
    b, r = password_step(owner)
    assert code_step(b, r.json()["challenge"], code(key, clock[0] + 30)).status_code == 200


def test_fy_mfa_010_five_wrong_codes_end_the_challenge_and_count_as_failed_sign_ins(owner, clock):
    """FY-MFA-010: each wrong code says how many tries are left; after five the challenge is gone (the right code
    no longer helps) and the next password try waits, like after wrong passwords. Audited as failed sign-ins."""
    key, _ = enrol(owner, clock)
    clock[0] += 30
    b, r = password_step(owner)
    ch = r.json()["challenge"]
    left = []
    for i in range(5):
        x = code_step(b, ch, f"{i:06d}" if f"{i:06d}" != code(key, clock[0]) else "999999")
        assert x.status_code == 400 and x.json()["detail"]["code"] == "mfa_code_invalid"
        left.append(x.json()["detail"]["attempts_left"])
    assert left == [4, 3, 2, 1, 0]
    assert rows(LoginChallenge) == 0
    x = code_step(b, ch, code(key, clock[0]))
    assert x.status_code == 400 and x.json()["detail"]["code"] == "mfa_challenge_expired"
    assert len([d for _, _, d in audit("auth.login_failed") if d.get("reason") == "mfa_code"]) == 5
    _, r = password_step(owner)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"


def test_fy_mfa_011_a_challenge_lasts_five_minutes_and_belongs_to_nobody_else(owner, clock):
    """FY-MFA-011: an expired or unknown challenge is refused; a disabled account cannot finish its challenge."""
    key, _ = enrol(owner, clock)
    clock[0] += 30
    b, r = password_step(owner)
    db = new_session()
    try:
        db.execute(update(LoginChallenge).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    finally:
        db.close()
    x = code_step(b, r.json()["challenge"], code(key, clock[0]))
    assert x.status_code == 400 and x.json()["detail"]["code"] == "mfa_challenge_expired"
    x = code_step(b, "made-up", code(key, clock[0]))
    assert x.json()["detail"]["code"] == "mfa_challenge_expired"
    # Disabled between the two steps.
    add_user(owner, "erin", role="admin")
    erin = sign_in(owner, "erin", "quiet-meadow-4821")
    ekey, _ = enrol(erin, clock, password="quiet-meadow-4821")
    clock[0] += 30
    b, r = password_step(owner, "erin", "quiet-meadow-4821")
    assert owner.patch(f"/api/users/{user_id('erin')}", json={"status": "disabled"}).status_code == 200
    x = code_step(b, r.json()["challenge"], code(ekey, clock[0]))
    assert x.status_code == 403 and x.json()["detail"]["code"] == "account_disabled"
    assert rows(LoginChallenge) == 0


def test_fy_mfa_012_two_requests_with_one_code_or_one_challenge_win_once(owner, clock):
    """FY-MFA-012: the step is claimed with a conditional update, and a challenge is used up once: a second database
    session that loaded the factor before the first claim does not get the same code in."""
    key, _ = enrol(owner, clock)
    clock[0] += 30
    uid = user_id("owner")
    a, b = new_session(), new_session()
    try:
        held = (a.scalars(select(MfaFactor)).all(), b.scalars(select(MfaFactor)).all())  # both hold the enrolment's step
        assert held[1][0].last_step == totp.step_at(clock[0] - 30)
        c = code(key, clock[0])
        assert mfa.check_totp(a, uid, c) is not None
        a.commit()
        assert mfa.check_totp(b, uid, c) is None
        b.rollback()
        token = mfa.new_challenge(a, a.get(User, uid))
        a.commit()
        row_a, row_b = mfa.find_challenge(a, token), mfa.find_challenge(b, token)
        assert mfa.finish_challenge(a, row_a) is True
        a.commit()
        assert mfa.finish_challenge(b, row_b) is False
    finally:
        a.close()
        b.close()


def test_fy_mfa_013_accounts_without_a_factor_sign_in_exactly_as_before(owner, clock):
    """FY-MFA-013 (the rule-1 fact): no factor, no challenge: one step, a session, no new rows, the same answer
    keys; the users list says who has two-factor."""
    add_user(owner, "dana")
    b, r = password_step(owner, "dana", "quiet-meadow-4821")
    assert r.status_code == 200 and set(r.json()) == {"user"} and b.cookies.get("forgeyard_session")
    assert rows(LoginChallenge) == 0
    assert [s["two_factor"] for s in b.get("/api/auth/sessions").json()["sessions"]] == [False]
    enrol(owner, clock)
    listed = {u["username"]: u["has_mfa"] for u in owner.get("/api/users").json()["users"]}
    assert listed == {"owner": True, "dana": False}
    assert audit("auth.login")[-1][2] == {}


# ------------------------------------------------------------------ recovery codes
def test_fy_mfa_014_a_recovery_code_signs_in_once_and_a_new_set_voids_the_old(owner, clock):
    """FY-MFA-014: a recovery code (any case, with or without dashes or spaces) stands in for the code once; it is
    audited with how many are left; a new set (password again) voids every old code."""
    _, r = enrol(owner, clock)
    codes = r.json()["recovery_codes"]
    b, r = password_step(owner)
    x = code_step(b, r.json()["challenge"], codes[0].upper().replace("-", " "))
    assert x.status_code == 200 and x.json()["recovery_codes_left"] == 9
    assert audit("auth.recovery_code_used")[-1][2] == {"left": 9}
    assert audit("auth.login")[-1][2] == {"mfa": "recovery"}
    b, r = password_step(owner)
    again = code_step(b, r.json()["challenge"], codes[0])
    assert again.json()["detail"]["code"] == "mfa_code_invalid"
    assert code_step(b, r.json()["challenge"], codes[1].replace("-", "")).status_code == 200
    r = owner.post("/api/auth/mfa/recovery-codes", json={})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "reauth_required"
    r = owner.post("/api/auth/mfa/recovery-codes", json={"password": OWNER[1]})
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10 and r.json()["recovery_codes_left"] == 10
    fresh = r.json()["recovery_codes"]
    assert not set(fresh) & set(codes)
    b, r = password_step(owner)
    assert code_step(b, r.json()["challenge"], codes[2]).json()["detail"]["code"] == "mfa_code_invalid"
    assert code_step(b, r.json()["challenge"], fresh[0]).status_code == 200
    assert audit("auth.recovery_codes_created")[-1][2]["count"] == 10


# ------------------------------------------------------------------ changing it
def test_fy_mfa_015_removing_a_factor_or_making_codes_needs_the_password_or_a_current_code(owner, clock):
    """FY-MFA-015: without, or with a wrong password or code, nothing changes; a current code works; removing the
    last factor removes the recovery codes and sign-in is one step again. A factor of someone else is not found."""
    key, _ = enrol(owner, clock)
    fid = owner.get("/api/auth/mfa/factors").json()["factors"][0]["id"]
    url = f"/api/auth/mfa/factors/{fid}/remove"
    assert owner.post(url, json={}).json()["detail"]["code"] == "reauth_required"
    assert owner.post(url, json={"password": "wrong-password-99"}).json()["detail"]["code"] == "wrong_password"
    assert owner.post(url, json={"code": "000000" if code(key, clock[0] + 30) != "000000" else "111111"}).json()[
        "detail"]["code"] == "mfa_code_invalid"
    r = owner.post("/api/auth/mfa/recovery-codes", json={"code": code(key, clock[0])})  # the enrolment used that step
    assert r.json()["detail"]["code"] == "mfa_code_invalid"
    assert rows(MfaFactor) == 1 and rows(RecoveryCode) == 10
    add_user(owner, "dana")
    dana = sign_in(owner, "dana", "quiet-meadow-4821")
    assert dana.post(url, json={"password": "quiet-meadow-4821"}).status_code == 404
    clock[0] += 30
    r = owner.post(url, json={"code": code(key, clock[0])})
    assert r.status_code == 200 and r.json()["factors"] == [] and r.json()["recovery_codes_left"] == 0
    assert rows(MfaFactor) == 0 and rows(RecoveryCode) == 0
    (_, _, detail), = audit("auth.mfa_removed")
    assert detail["two_factor_off"] is True and detail["confirmed_with"] == "code"
    b, r = password_step(owner)
    assert set(r.json()) == {"user"}
    r = owner.post("/api/auth/mfa/recovery-codes", json={"password": OWNER[1]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "mfa_not_enabled"


def test_fy_mfa_016_wrong_passwords_and_codes_in_my_account_count_like_sign_ins(owner, clock):
    """FY-MFA-016: re-authentication shares the sign-in back-off, so a stolen session cannot guess the password or a
    code there either."""
    enrol(owner, clock)
    url = "/api/auth/mfa/recovery-codes"
    free = get_settings().login_free_failures
    for _ in range(free - 1):
        assert owner.post(url, json={"password": "wrong-password-99"}).json()["detail"]["code"] == "wrong_password"
    assert owner.post(url, json={"code": "12345a"}).json()["detail"]["code"] == "mfa_code_invalid"
    r = owner.post(url, json={"password": OWNER[1]})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"
    _, r = password_step(owner)
    assert r.status_code == 429


# ------------------------------------------------------------------ admins
def test_fy_mfa_017_an_admin_resets_someones_two_factor_and_ends_their_sessions(owner, clock):
    """FY-MFA-017: an admin removes someone's factors and recovery codes; their sessions end (reason mfa_reset) and
    they sign in with the password alone. Only admins; never yourself; the owner's only by the owner; audited."""
    add_user(owner, "erin", role="admin")
    add_user(owner, "dana", role="user")
    erin = sign_in(owner, "erin", "quiet-meadow-4821")
    dana = sign_in(owner, "dana", "quiet-meadow-4821")
    enrol(dana, clock, password="quiet-meadow-4821")
    enrol(owner, clock)
    assert dana.post(f"/api/users/{user_id('owner')}/mfa-reset").status_code == 403
    r = erin.post(f"/api/users/{user_id('owner')}/mfa-reset")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "owner_protected"
    r = owner.post(f"/api/users/{user_id('owner')}/mfa-reset")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "self_protected"
    r = owner.post(f"/api/users/{user_id('erin')}/mfa-reset")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "mfa_not_enabled"
    r = erin.post(f"/api/users/{user_id('dana')}/mfa-reset")
    assert r.status_code == 200 and r.json()["factors_removed"] == 1 and r.json()["sessions_ended"] == 1
    assert rows(MfaFactor, user_id=user_id("dana")) == 0 and rows(RecoveryCode, user_id=user_id("dana")) == 0
    assert rows(MfaFactor, user_id=user_id("owner")) == 1
    gone = dana.get("/api/auth/me")
    assert gone.status_code == 401 and gone.json()["detail"]["reason"] == "mfa_reset"
    _, r = password_step(owner, "dana", "quiet-meadow-4821")
    assert set(r.json()) == {"user"}
    (actor, target, detail), = audit("user.mfa_reset")
    assert (actor, target, detail) == ("erin", "dana", {"factors": 1, "sessions_ended": 1})


def test_fy_mfa_018_deleting_an_account_deletes_its_factors_codes_and_challenges(owner, clock):
    """FY-MFA-018: the three tables go with the account (ON DELETE CASCADE)."""
    add_user(owner, "dana")
    dana = sign_in(owner, "dana", "quiet-meadow-4821")
    enrol(dana, clock, password="quiet-meadow-4821")
    enrol(owner, clock)
    password_step(owner, "dana", "quiet-meadow-4821")
    uid = user_id("dana")
    assert (rows(MfaFactor, user_id=uid), rows(RecoveryCode, user_id=uid), rows(LoginChallenge, user_id=uid)) == (1, 10, 1)
    assert owner.delete(f"/api/users/{uid}").status_code == 200
    assert (rows(MfaFactor, user_id=uid), rows(RecoveryCode, user_id=uid), rows(LoginChallenge, user_id=uid)) == (0, 0, 0)
    assert rows(MfaFactor) == 1 and rows(RecoveryCode) == 10


def test_fy_mfa_019_setup_health_names_the_admins_without_two_factor(owner, clock):
    """FY-MFA-019: an info finding lists active admins who sign in with a password alone (not operators, users or
    disabled admins), shown on Users, pointing at My account; it goes once each has a factor."""
    add_user(owner, "erin", role="admin")
    add_user(owner, "olga", role="operator")
    add_user(owner, "zed", role="admin")
    owner.patch(f"/api/users/{user_id('zed')}", json={"status": "disabled"})

    def found():
        db = new_session()
        try:
            return [f for f in advisor.run(db, get_settings()) if f.id == "admins-without-mfa"]
        finally:
            db.close()

    (f,) = found()
    assert f.severity == "info" and f.title == "2 admins without two-factor sign-in"
    assert "erin" in f.detail and "owner" in f.detail and "olga" not in f.detail and "zed" not in f.detail
    assert f.fix_url == "/account" and f.pages == ("users",)
    enrol(owner, clock)
    (f,) = found()
    assert f.title == "1 admin without two-factor sign-in" and "owner" not in f.detail
    erin = sign_in(owner, "erin", "quiet-meadow-4821")
    enrol(erin, clock, password="quiet-meadow-4821")
    assert found() == []


def test_fy_mfa_020_the_secret_key_backup_also_asks_for_a_code(owner, clock):
    """FY-MFA-020: an admin with a factor downloads the key backup only with the password and a current code; a
    missing or wrong code is refused (a wrong one audited and counted); without a factor nothing changes."""
    from tests.test_key_backup import PASS

    def body(**extra):
        return {"password": OWNER[1], "passphrase": PASS, "passphrase_confirm": PASS, **extra}

    assert owner.get("/api/secret-key").json()["has_mfa"] is False
    assert owner.post("/api/secret-key/backup", json=body()).status_code == 200
    key, _ = enrol(owner, clock)
    assert owner.get("/api/secret-key").json()["has_mfa"] is True
    r = owner.post("/api/secret-key/backup", json=body())
    assert r.status_code == 400 and r.json()["detail"]["code"] == "mfa_code_required"
    r = owner.post("/api/secret-key/backup", json=body(code=code(key, clock[0])))  # step used by the enrolment
    assert r.status_code == 400 and r.json()["detail"]["code"] == "mfa_code_invalid"
    assert audit("secret_key.backup_failed")[-1][2] == {"reason": "wrong_code"}
    r = owner.post("/api/secret-key/backup", json=body(password="wrong-password-99", code=code(key, clock[0] + 30)))
    assert r.status_code == 401 and r.json()["detail"]["code"] == "wrong_password"
    clock[0] += 30
    r = owner.post("/api/secret-key/backup", json=body(code=code(key, clock[0])))
    assert r.status_code == 200 and r.content[:8] == b"FYKEYBK\0"


# ------------------------------------------------------------------ data
def test_fy_mfa_021_migration_0020_goes_down_and_up_and_keeps_sessions(owner, clock):
    """FY-MFA-021: going down drops the three tables and sessions.mfa_at and a signed-in browser stays signed in;
    going up again brings them back empty and the session still works."""
    from alembic import command
    from sqlalchemy import inspect

    from forgeyard.db import get_engine
    from forgeyard.migrate import alembic_config, upgrade

    enrol(owner, clock)
    _close = owner.get("/api/auth/me")
    assert _close.status_code == 200
    from sqlalchemy.orm import close_all_sessions

    close_all_sessions()
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0019_remove_apps")
        names = inspect(conn).get_table_names()
        assert not {"mfa_factors", "recovery_codes", "login_challenges"} & set(names)
        assert "mfa_at" not in {c["name"] for c in inspect(conn).get_columns("sessions")}
        assert conn.exec_driver_sql("SELECT count(*) FROM sessions WHERE revoked_at IS NULL").scalar() == 1
    upgrade()
    with get_engine().connect() as conn:
        assert {"mfa_factors", "recovery_codes", "login_challenges"} <= set(inspect(conn).get_table_names())
        assert "mfa_at" in {c["name"] for c in inspect(conn).get_columns("sessions")}
    assert owner.get("/api/auth/me").status_code == 200
    assert rows(MfaFactor) == 0


def test_fy_mfa_022_secrets_reset_removes_authenticators_it_cannot_read(owner, clock):
    """FY-MFA-022: after a lost secret key, `secrets reset` removes the factors (and the recovery codes of accounts
    left without one), so nobody is locked out by a factor the server can no longer read."""
    from forgeyard.security import crypto, secrets_reset

    enrol(owner, clock)
    add_user(owner, "dana")
    path = crypto.key_path()
    path.unlink()
    crypto.reset()
    out = secrets_reset.reset()
    assert out["authenticators"] == 1 and "1 authenticator app(s)" in out["summary"]
    assert rows(MfaFactor) == 0 and rows(RecoveryCode) == 0
    _, r = password_step(owner)
    assert set(r.json()) == {"user"}


def test_fy_mfa_023_my_sessions_and_challenges_never_hold_a_token_or_code_in_clear(owner, clock):
    """FY-MFA-023: the database has the challenge's hash, never the token; recovery codes' hashes, never a code."""
    _, r = enrol(owner, clock)
    codes = r.json()["recovery_codes"]
    _, r = password_step(owner)
    token = r.json()["challenge"]
    db = new_session()
    try:
        dump = " ".join(str(v) for t in (LoginChallenge, RecoveryCode, Session)
                        for row in db.execute(select(t.__table__)).all() for v in row)
    finally:
        db.close()
    assert token not in dump and not any(c in dump or c.replace("-", "") in dump for c in codes)
