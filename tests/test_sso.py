"""Single sign-on through an OpenID Connect provider (1.6.0, #39, PR 3): the provider settings, the sign-in round trip
(PKCE, state, nonce, the ID token checks), new and existing accounts, linking, the two-factor exemption and the fresh
sign-in that stands in for a password (FY-SSO-*).

The provider is played by the test (FakeIdP, an httpx.MockTransport): its keys are made here, nothing goes to the
network."""

import base64
import hashlib
import json
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import ECKey, KeySet, OctKey, RSAKey
from sqlalchemy import select, text, update

from forgeyard.db import AuditEvent, IdentityProvider, Session, User, UserIdentity, new_session, utcnow
from forgeyard.security import oidc, sso, totp
from tests.conftest import OWNER, add_user, sign_in

ISSUER = "https://idp.example.test/realms/forge"
CLIENT_ID = "forgeyard-console"
CLIENT_SECRET = "idp-client-secret-made-by-the-test-5d1f"


class FakeIdP:
    """An OpenID provider: discovery, JWKS, and a token endpoint that checks the code, the redirect URI, the client
    secret and the PKCE verifier, the way Keycloak does."""

    def __init__(self):
        self.key = RSAKey.generate_key(2048, parameters={"kid": "rsa-1", "use": "sig", "alg": "RS256"})
        self.keys = [self.key]
        self.codes: dict[str, dict] = {}
        self.token_calls: list[dict] = []
        self.meta_overrides: dict = {}
        self.token_error: str | None = None
        self.n = 0

    def meta(self) -> dict:
        return {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
                "token_endpoint": ISSUER + "/protocol/openid-connect/token", "jwks_uri": ISSUER + "/certs",
                "response_types_supported": ["code"], "id_token_signing_alg_values_supported": ["RS256", "ES256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                **self.meta_overrides}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == ISSUER + "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.meta())
        if url == ISSUER + "/certs":
            return httpx.Response(200, json=KeySet(self.keys).as_dict(private=False))
        if url == ISSUER + "/protocol/openid-connect/token" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_calls.append({"form": form, "auth": request.headers.get("authorization")})
            if self.token_error:
                return httpx.Response(400, json={"error": self.token_error})
            grant = self.codes.pop(form.get("code", ""), None)
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if grant is None or form.get("grant_type") != "authorization_code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, json={"error": "invalid_client"})
            if form.get("redirect_uri") != grant["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if challenge != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "expires_in": 300,
                                             "id_token": grant["id_token"]})
        return httpx.Response(404)

    def sign(self, claims: dict, *, key=None, alg="RS256", kid="rsa-1") -> str:
        header = {"alg": alg, "typ": "JWT"}
        if kid is not None:
            header["kid"] = kid
        return jwt.encode(header, claims, key or self.key)

    def authorize(self, url: str, sub: str = "sub-alice", *, token: str | None = None, claims=None, drop=(),
                  **extra) -> tuple[str, str]:
        """The person signs in at the provider: returns (state, code) as the redirect would carry them."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert url.startswith(ISSUER + "/protocol/openid-connect/auth?")
        now = int(time.time())
        c = {"iss": ISSUER, "aud": CLIENT_ID, "sub": sub, "iat": now, "exp": now + 300, "nonce": q["nonce"],
             "auth_time": now, "email": f"{sub}@example.com", "email_verified": True, **extra}
        if claims:
            c.update(claims)
        for k in drop:
            c.pop(k, None)
        self.n += 1
        code = f"code-{self.n}"
        self.codes[code] = {"redirect_uri": q["redirect_uri"], "challenge": q["code_challenge"],
                            "id_token": token if token is not None else self.sign(c), "query": q}
        return q["state"], code


@pytest.fixture()
def idp(monkeypatch):
    fake = FakeIdP()
    monkeypatch.setattr(oidc, "transport", httpx.MockTransport(fake.handler))
    return fake


def browser(app_client) -> TestClient:
    c = TestClient(app_client.app, base_url="http://localhost")
    c.headers["X-Forgeyard"] = "1"
    return c


def configure(owner, **overrides):
    body = {"name": "Keycloak", "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "new_accounts": "approve", **overrides}
    r = owner.put("/api/identity-provider", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def start(c, purpose="login", **body):
    r = c.post("/api/auth/sso/start", json={"purpose": purpose, **body})
    assert r.status_code == 200, r.text
    return r.json()["url"]


def finish(c, state, code):
    return c.post("/api/auth/sso/finish", json={"state": state, "code": code})


def sso_sign_in(app_client, idp, sub="sub-alice", **claims):
    """A new browser signs in through the provider. Returns (browser, finish response)."""
    b = browser(app_client)
    state, code = idp.authorize(start(b), sub, **claims)
    return b, finish(b, state, code)


def code_of(r) -> str:
    return r.json()["detail"]["code"]


def audit(action):
    db = new_session()
    try:
        return [(e.actor_name, e.target, e.detail) for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action)
                                                                       .order_by(AuditEvent.id))]
    finally:
        db.close()


def one(model, **where):
    db = new_session()
    try:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return db.scalars(q).first()
    finally:
        db.close()


def approve(owner, username):
    uid = one(User, username=username).id
    r = owner.patch(f"/api/users/{uid}", json={"status": "active"})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ nothing set up
def test_fy_sso_001_without_a_provider_nothing_changes(owner):
    """FY-SSO-001: with no provider, the sign-in page offers no single sign-on, starting one answers 404
    sso_not_configured, and password sign-in, its sessions and /me are as before (auth_method password)."""
    b = browser(owner)
    assert b.get("/api/auth/state").json()["sso"] is None
    r = b.post("/api/auth/sso/start", json={"purpose": "login"})
    assert r.status_code == 404 and code_of(r) == "sso_not_configured"
    assert b.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]}).status_code == 200
    me = b.get("/api/auth/me").json()
    assert me["session"]["auth_method"] == "password" and me["session"]["fresh_until"] is None
    assert b.get("/api/auth/state").json()["session"]["auth_method"] == "password"
    assert owner.get("/api/identity-provider").json()["provider"] is None


# ------------------------------------------------------------------ the provider (admins)
def test_fy_sso_002_admins_set_the_provider_and_its_secret_is_write_only_and_encrypted(owner, idp):
    """FY-SSO-002: PUT makes the one provider: the answer and GET never carry the client secret (only
    has_client_secret); the database holds it encrypted and bound to its column; idp.created is audited without it;
    the sign-in page then offers it by name; non-admins get 403."""
    out = configure(owner)
    p = out["provider"]
    assert p["has_client_secret"] is True and CLIENT_SECRET not in json.dumps(out)
    assert out["redirect_uri"] == "http://localhost/sso/callback"
    assert CLIENT_SECRET not in owner.get("/api/identity-provider").text
    db = new_session()
    try:
        raw = db.execute(text("SELECT client_secret FROM identity_providers")).scalar()
        assert raw.startswith("fy1:") and CLIENT_SECRET not in raw
        # Bound to its column: the same value does not open as another column's.
        from forgeyard.security import crypto
        with pytest.raises(crypto.SecretError):
            crypto.box().open(raw, "providers.api_key")
        assert crypto.box().open(raw, "identity_providers.client_secret") == CLIENT_SECRET
    finally:
        db.close()
    [(actor, target, detail)] = audit("idp.created")
    assert actor == OWNER[0] and target == "Keycloak" and detail["client_secret"] is True
    assert CLIENT_SECRET not in json.dumps(detail)
    assert browser(owner).get("/api/auth/state").json()["sso"] == {"name": "Keycloak"}
    add_user(owner, "uma", role="user")
    u = sign_in(owner, "uma", "quiet-meadow-4821")
    assert u.get("/api/identity-provider").status_code == 403
    assert u.put("/api/identity-provider", json={"name": "x", "issuer": ISSUER, "client_id": "y"}).status_code == 403


def test_fy_sso_003_saving_checks_what_would_let_anyone_in(owner, idp):
    """FY-SSO-003: automatic accounts need allowed domains (422 domains_required); a role mapping needs its claim;
    the issuer must be https (plain http only on this host); turning it on reads the discovery document (an issuer
    that does not match it: 422 sso_discovery_failed); a secret left out is kept, "" removes it, and idp.changed
    names the change without the value."""
    def put(**kw):
        return owner.put("/api/identity-provider", json={"name": "Keycloak", "issuer": ISSUER, "client_id": CLIENT_ID,
                                                         **kw})
    assert code_of(put(new_accounts="automatic")) == "domains_required"
    assert code_of(put(role_mapping={"fy-admins": "admin"})) == "role_claim_required"
    assert code_of(put(issuer="http://idp.example.test/realms/forge")) == "sso_bad_issuer"
    assert code_of(put(allowed_domains=["not a domain"])) == "bad_domain"
    idp.meta_overrides = {"issuer": ISSUER + "/other"}
    r = put()
    assert r.status_code == 422 and code_of(r) == "sso_discovery_failed"
    idp.meta_overrides = {}
    # Off, it is saved without asking the provider.
    assert put(enabled=False, client_secret=CLIENT_SECRET).json()["provider"]["enabled"] is False
    assert put(name="Company sign-in").json()["provider"]["has_client_secret"] is True  # left out: kept
    [(_, _, changed)] = [a for a in audit("idp.changed") if "name" in a[2]]
    assert changed["name"] == ["Keycloak", "Company sign-in"]
    assert put(client_secret="").json()["provider"]["has_client_secret"] is False
    assert audit("idp.changed")[-1][2]["client_secret"] == "removed"
    assert put(client_secret="another-secret-0000").json()["provider"]["has_client_secret"] is True
    last = audit("idp.changed")[-1][2]
    assert last["client_secret"] == "changed" and "another-secret-0000" not in json.dumps(last)
    # Domains are normalised; automatic with one is accepted.
    p = put(new_accounts="automatic", allowed_domains=[" Example.COM ", "@example.com"]).json()["provider"]
    assert p["allowed_domains"] == ["example.com"] and p["new_accounts"] == "automatic"
    assert p["scopes"] == "openid email profile"


def test_fy_sso_004_check_reads_the_discovery_document(owner, idp):
    """FY-SSO-004: POST /check names the provider's endpoints and algorithms, or says what is wrong (with a trailing
    slash typed, the issuer is still found)."""
    r = owner.post("/api/identity-provider/check", json={"issuer": ISSUER + "/"}).json()
    assert r["ok"] is True and r["issuer"] == ISSUER and r["jwks_uri"] == ISSUER + "/certs"
    assert r["algorithms"] == ["RS256", "ES256"]
    idp.meta_overrides = {"id_token_signing_alg_values_supported": ["HS256"]}
    r = owner.post("/api/identity-provider/check", json={"issuer": ISSUER}).json()
    assert r["ok"] is False and r["reason"] == "discovery_algorithms"
    r = owner.post("/api/identity-provider/check", json={"issuer": "https://nothing.example.test"}).json()
    assert r["ok"] is False and r["code"] == "sso_provider_unreachable"


# ------------------------------------------------------------------ signing in
def test_fy_sso_005_first_sign_in_makes_a_pending_account_then_signs_in_after_approval(owner, idp):
    """FY-SSO-005: someone new (approve, the default) gets a pending account and 403 pending_approval, with the link to
    the provider made; once an admin approves it, the same sign-in starts a session marked sso; audits
    user.registered (via sso) and auth.sso_login."""
    configure(owner)
    b, r = sso_sign_in(owner, idp, "sub-alice", preferred_username="Alice.W")
    assert r.status_code == 403 and code_of(r) == "pending_approval" and r.json()["detail"]["username"] == "alice.w"
    u = one(User, username="alice.w")
    assert u.status == "pending" and u.password_hash is None and u.role == "user" and u.email == "sub-alice@example.com"
    assert one(UserIdentity, user_id=u.id).subject == "sub-alice"
    assert b.get("/api/auth/state").json()["user"] is None
    assert [d["via"] for _, _, d in audit("user.registered")] == ["sso"]
    # Again before approval: refused, and audited as a failed sign-in.
    _, r = sso_sign_in(owner, idp, "sub-alice")
    assert code_of(r) == "pending_approval"
    assert audit("auth.sso_login_failed")[-1][2]["reason"] == "pending_approval"
    approve(owner, "alice.w")
    b, r = sso_sign_in(owner, idp, "sub-alice")
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == "alice.w" and r.json()["next"] == "/"
    state = b.get("/api/auth/state").json()
    assert state["user"]["username"] == "alice.w" and state["session"]["auth_method"] == "sso"
    assert audit("auth.sso_login")[-1][0] == "alice.w"
    assert b.get("/api/keys").status_code == 200
    listed = {u["username"]: u["sso_linked"] for u in owner.get("/api/users").json()["users"]}
    assert listed == {"alice.w": True, OWNER[0]: False}


def test_fy_sso_006_automatic_accounts_only_for_allowed_domains(owner, idp):
    """FY-SSO-006: automatic accounts are active at once for an allowed e-mail domain; another domain (or no e-mail)
    gets 403 sso_domain_not_allowed and no account; an address the provider marks unverified gets 403
    sso_email_unverified."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    b, r = sso_sign_in(owner, idp, "sub-bo", email="bo@example.com")
    assert r.status_code == 200 and one(User, username="bo").status == "active"
    _, r = sso_sign_in(owner, idp, "sub-eve", email="eve@elsewhere.test")
    assert r.status_code == 403 and code_of(r) == "sso_domain_not_allowed" and one(User, username="eve") is None
    _, r = sso_sign_in(owner, idp, "sub-nomail", drop=("email",))
    assert code_of(r) == "sso_domain_not_allowed"
    _, r = sso_sign_in(owner, idp, "sub-unv", email="unv@example.com", email_verified=False)
    assert code_of(r) == "sso_email_unverified" and one(User, username="unv") is None
    # The allowed domains also limit "approve": nobody outside them can queue an account.
    configure(owner, new_accounts="approve", allowed_domains=["example.com"])
    _, r = sso_sign_in(owner, idp, "sub-out", email="out@elsewhere.test")
    assert code_of(r) == "sso_domain_not_allowed" and one(User, username="out") is None


def test_fy_sso_007_accounts_are_found_by_issuer_and_subject_never_by_email(owner, idp):
    """FY-SSO-007: a provider account whose e-mail is an existing person's does not reach that person's account (a new,
    separate pending account); the same subject with another e-mail later still reaches its own account."""
    owner.patch(f"/api/users/{one(User, username=OWNER[0]).id}", json={"email": "owner@example.com"})
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    b, r = sso_sign_in(owner, idp, "sub-mallory", email="owner@example.com", preferred_username="owner")
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "owner-2" and r.json()["user"]["role"] == "user"
    _, r = sso_sign_in(owner, idp, "sub-mallory", email="new@example.com")
    assert r.json()["user"]["username"] == "owner-2"
    assert one(UserIdentity, subject="sub-mallory").user_id == one(User, username="owner-2").id


def test_fy_sso_008_state_is_single_use_short_lived_and_bound_to_the_browser(owner, idp, monkeypatch):
    """FY-SSO-008: a state is used once (the second finish: 400 sso_state_invalid), an unknown or expired one is
    refused, and finishing in a browser that did not start (no sso cookie) is refused; each is audited with its
    reason and starts no session."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    b = browser(owner)
    state, code = idp.authorize(start(b), "sub-a")
    other = browser(owner)
    r = finish(other, state, code)
    assert r.status_code == 400 and code_of(r) == "sso_state_invalid"
    assert audit("auth.sso_login_failed")[-1][2]["reason"] == "browser_mismatch"
    # The try above used the state up, even in the wrong browser.
    assert code_of(finish(b, state, code)) == "sso_state_invalid"
    assert code_of(finish(b, "made-up-state", "code-x")) == "sso_state_invalid"
    state, code = idp.authorize(start(b), "sub-a")
    real = time.monotonic
    monkeypatch.setattr(sso.time, "monotonic", lambda: real() + sso.PENDING_SECONDS + 1)
    assert code_of(finish(b, state, code)) == "sso_state_invalid"
    monkeypatch.setattr(sso.time, "monotonic", real)
    assert b.get("/api/auth/state").json()["user"] is None and other.get("/api/auth/state").json()["user"] is None
    state, code = idp.authorize(start(b), "sub-a")
    assert finish(b, state, code).status_code == 200
    assert code_of(finish(b, state, code)) == "sso_state_invalid"


def test_fy_sso_009_pkce_s256_and_the_secret_go_to_the_token_endpoint_only(owner, idp):
    """FY-SSO-009: the authorization URL carries an S256 code challenge, state and nonce, never the verifier or the
    secret; the token request carries the verifier that hashes to that challenge, the redirect URI and the client
    secret (HTTP Basic)."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    b = browser(owner)
    url = start(b)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["code_challenge_method"] == "S256" and q["response_type"] == "code" and q["client_id"] == CLIENT_ID
    assert q["scope"] == "openid email profile" and len(q["state"]) >= 40 and len(q["nonce"]) >= 40
    assert "max_age" not in q and "prompt" not in q
    assert CLIENT_SECRET not in url
    state, code = idp.authorize(url, "sub-a")
    assert finish(b, state, code).status_code == 200
    [call] = idp.token_calls
    v = call["form"]["code_verifier"]
    assert 43 <= len(v) <= 128 and v not in url
    assert base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    assert call["form"]["redirect_uri"] == "http://localhost/sso/callback"
    assert CLIENT_SECRET not in call["form"].get("client_secret", "") and call["auth"].startswith("Basic ")


@pytest.mark.parametrize("case, reason", [
    ("iss", "iss"), ("aud", "aud"), ("azp", "aud"), ("exp", "exp"), ("iat", "iat"), ("nonce", "nonce"),
    ("hs256", "alg"), ("none", "alg"), ("other_key", "signature"), ("no_sub", "sub"),
])
def test_fy_sso_010_the_id_token_is_checked(owner, idp, case, reason):
    """FY-SSO-010: an ID token from another issuer, for another client (or several audiences with another azp),
    expired, dated in the future, with another nonce, signed with HS256 or alg none, signed by a key the provider
    does not publish, or naming no subject: 400 sso_token_invalid with the reason, audited, no session, no account."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    b = browser(owner)
    url = start(b)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "sub-t", "iat": now, "exp": now + 300, "nonce": q["nonce"],
              "email": "t@example.com"}
    key, alg = None, "RS256"
    if case == "iss":
        claims["iss"] = ISSUER + "-evil"
    elif case == "aud":
        claims["aud"] = "another-client"
    elif case == "azp":
        claims.update(aud=[CLIENT_ID, "another-client"], azp="another-client")
    elif case == "exp":
        claims.update(exp=now - oidc.LEEWAY - 5, iat=now - 600)
    elif case == "iat":
        claims["iat"] = now + oidc.LEEWAY + 60
    elif case == "nonce":
        claims["nonce"] = "another-nonce"
    elif case == "no_sub":
        del claims["sub"]
    if case == "hs256":
        token = idp.sign(claims, key=OctKey.import_key(b"x" * 32), alg="HS256")
    elif case == "none":
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
        token = enc({"alg": "none", "kid": "rsa-1"}) + "." + enc(claims) + "."
    elif case == "other_key":
        token = idp.sign(claims, key=RSAKey.generate_key(2048, parameters={"kid": "rsa-1"}))
    else:
        token = idp.sign(claims, key=key, alg=alg)
    state, code = idp.authorize(url, token=token)
    r = finish(b, state, code)
    assert r.status_code == 400 and code_of(r) == "sso_token_invalid", r.text
    assert r.json()["detail"]["reason"] == reason
    assert audit("auth.sso_login_failed")[-1][2]["reason"] == f"token_invalid:{reason}"
    assert b.get("/api/auth/state").json()["user"] is None and one(UserIdentity, subject="sub-t") is None


def test_fy_sso_011_es256_tokens_keys_without_kid_and_rotated_keys_are_accepted(owner, idp):
    """FY-SSO-011: an ES256 ID token is accepted; so is one without a kid (each published key tried); and a key the
    provider added after the key set was read (the set is read again once for an unknown kid)."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    ec = ECKey.generate_key("P-256", parameters={"kid": "ec-1"})
    idp.keys = [idp.key, ec]
    b = browser(owner)
    url = start(b)
    nonce = parse_qs(urlsplit(url).query)["nonce"][0]
    now = int(time.time())
    base = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, "nonce": nonce, "email": "e@example.com"}
    state, code = idp.authorize(url, token=idp.sign({**base, "sub": "sub-ec"}, key=ec, alg="ES256", kid="ec-1"))
    assert finish(b, state, code).status_code == 200
    b = browser(owner)
    url = start(b)
    base["nonce"] = parse_qs(urlsplit(url).query)["nonce"][0]
    state, code = idp.authorize(url, token=idp.sign({**base, "sub": "sub-nokid"}, kid=None))
    assert finish(b, state, code).status_code == 200
    new = RSAKey.generate_key(2048, parameters={"kid": "rsa-2"})
    idp.keys = [idp.key, ec, new]  # rotated after the server cached the set
    b = browser(owner)
    url = start(b)
    base["nonce"] = parse_qs(urlsplit(url).query)["nonce"][0]
    state, code = idp.authorize(url, token=idp.sign({**base, "sub": "sub-rot"}, key=new, kid="rsa-2"))
    assert finish(b, state, code).status_code == 200


def test_fy_sso_012_a_disabled_account_cannot_sign_in_and_provider_errors_are_said(owner, idp):
    """FY-SSO-012: a disabled account gets 403 account_disabled; the provider's own error (the person cancelled) is
    400 sso_denied; a refused code exchange is 502 sso_token_failed; each audited."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    _, r = sso_sign_in(owner, idp, "sub-d")
    uid = one(User, username="sub-d").id
    assert owner.patch(f"/api/users/{uid}", json={"status": "disabled"}).status_code == 200
    _, r = sso_sign_in(owner, idp, "sub-d")
    assert r.status_code == 403 and code_of(r) == "account_disabled"
    assert audit("auth.sso_login_failed")[-1][2]["reason"] == "account_disabled"
    b = browser(owner)
    state = parse_qs(urlsplit(start(b)).query)["state"][0]
    r = b.post("/api/auth/sso/finish", json={"state": state, "error": "access_denied"})
    assert r.status_code == 400 and code_of(r) == "sso_denied"
    idp.token_error = "invalid_grant"
    b = browser(owner)
    state, code = idp.authorize(start(b), "sub-x")
    r = finish(b, state, code)
    assert r.status_code == 502 and code_of(r) == "sso_token_failed"
    assert audit("auth.sso_login_failed")[-1][2]["reason"] == "token_refused"


def test_fy_sso_013_the_role_claim_sets_the_role_on_each_sign_in_but_never_the_owners(owner, idp):
    """FY-SSO-013: with a role claim and mapping, each sign-in sets the role from the claim (the highest match; no
    match: user; claim missing: unchanged); the owner, signing in through the provider, stays admin."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"], role_claim="groups",
              role_mapping={"fy-admins": "admin", "fy-ops": "operator"})
    _, r = sso_sign_in(owner, idp, "sub-r", groups=["staff", "fy-ops", "fy-admins"])
    assert r.json()["user"]["role"] == "admin"
    _, r = sso_sign_in(owner, idp, "sub-r", groups=["fy-ops"])
    assert r.json()["user"]["role"] == "operator"
    assert audit("auth.sso_login")[-1][2]["role"] == ["admin", "operator"]
    _, r = sso_sign_in(owner, idp, "sub-r")  # no groups claim at all
    assert r.json()["user"]["role"] == "operator"
    _, r = sso_sign_in(owner, idp, "sub-r", groups="staff")
    assert r.json()["user"]["role"] == "user"
    # The owner links their account, then signs in with a token that maps to "user".
    url = start(owner, "link", password=OWNER[1])
    state, code = idp.authorize(url, "sub-owner")
    assert finish(owner, state, code).status_code == 200
    _, r = sso_sign_in(owner, idp, "sub-owner", groups=["staff"])
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin" and r.json()["user"]["is_owner"] is True


# ------------------------------------------------------------------ linking, fresh sign-in
def test_fy_sso_014_linking_needs_the_password_and_a_fresh_sign_in_at_the_provider(owner, idp):
    """FY-SSO-014: Link starts only with the password (or a code), sends max_age=0 and prompt=login, and needs an
    auth_time within 5 minutes; then the account is linked (user.identity_linked) and signs in through the provider.
    A provider account already linked to someone else: 409 identity_in_use; linking twice: 409."""
    configure(owner)
    r = owner.post("/api/auth/sso/start", json={"purpose": "link"})
    assert r.status_code == 400 and code_of(r) == "reauth_required"
    r = owner.post("/api/auth/sso/start", json={"purpose": "link", "password": "wrong-password-0000"})
    assert code_of(r) == "wrong_password"
    url = start(owner, "link", password=OWNER[1])
    q = parse_qs(urlsplit(url).query)
    assert q["max_age"] == ["0"] and q["prompt"] == ["login"]
    state, code = idp.authorize(url, "sub-own", auth_time=int(time.time()) - 600)  # an old sign-in at the provider
    r = finish(owner, state, code)
    assert r.status_code == 400 and code_of(r) == "sso_not_fresh" and one(UserIdentity, subject="sub-own") is None
    state, code = idp.authorize(start(owner, "link", password=OWNER[1]), "sub-own")
    r = finish(owner, state, code)
    assert r.status_code == 200 and r.json()["purpose"] == "link"
    assert one(UserIdentity, subject="sub-own").user_id == one(User, username=OWNER[0]).id
    assert audit("user.identity_linked")[-1][:2] == (OWNER[0], OWNER[0])
    r = owner.post("/api/auth/sso/start", json={"purpose": "link", "password": OWNER[1]})
    assert r.status_code == 409 and code_of(r) == "identity_already_linked"
    _, r = sso_sign_in(owner, idp, "sub-own")
    assert r.status_code == 200 and r.json()["user"]["username"] == OWNER[0]
    # Someone else tries to link the owner's provider account to theirs.
    add_user(owner, "uma")
    u = sign_in(owner, "uma", "quiet-meadow-4821")
    state, code = idp.authorize(start(u, "link", password="quiet-meadow-4821"), "sub-own")
    r = finish(u, state, code)
    assert r.status_code == 409 and code_of(r) == "identity_in_use"
    assert one(UserIdentity, subject="sub-own").user_id == one(User, username=OWNER[0]).id
    # Finishing a link in a session other than the one that started it is refused.
    state, code = idp.authorize(start(u, "link", password="quiet-meadow-4821"), "sub-uma")
    u.cookies.delete("forgeyard_session")
    r = finish(u, state, code)
    assert r.status_code == 401 and one(UserIdentity, subject="sub-uma") is None


def test_fy_sso_015_unlinking_keeps_a_way_in(owner, idp):
    """FY-SSO-015: an account with a password unlinks (user.identity_unlinked); an account whose only way in is the
    provider gets 409 last_way_in."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    state, code = idp.authorize(start(owner, "link", password=OWNER[1]), "sub-own")
    assert finish(owner, state, code).status_code == 200
    [ident] = owner.get("/api/auth/sso/identities").json()["identities"]
    assert ident["provider"] == "Keycloak" and ident["email"] == "sub-own@example.com"
    b, _ = sso_sign_in(owner, idp, "sub-solo")
    [solo] = b.get("/api/auth/sso/identities").json()["identities"]
    r = b.post("/api/auth/sso/unlink", json={"id": solo["id"]})
    assert r.status_code == 409 and code_of(r) == "last_way_in"
    assert b.post("/api/auth/sso/unlink", json={"id": ident["id"]}).status_code == 404  # not theirs
    assert owner.post("/api/auth/sso/unlink", json={"id": ident["id"]}).status_code == 200
    assert owner.get("/api/auth/sso/identities").json()["identities"] == []
    assert audit("user.identity_unlinked")[-1][:2] == (OWNER[0], OWNER[0])


def test_fy_sso_016_provider_sessions_are_exempt_from_required_two_factor(owner, idp, monkeypatch):
    """FY-SSO-016: with two-factor required of everyone and the grace period over, a person's password session reaches
    only the setup (403 mfa_enrolment_required) while their session through the provider works and shows no
    enrolment; an account without a password is not counted as one still to add it."""
    from forgeyard.security import mfa_policy

    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    add_user(owner, "pat")
    p = sign_in(owner, "pat", "quiet-meadow-4821")
    state, code = idp.authorize(start(p, "link", password="quiet-meadow-4821"), "sub-pat")
    assert finish(p, state, code).status_code == 200
    _, r = sso_sign_in(owner, idp, "sub-solo")  # an account with no password
    assert owner.put("/api/users/mfa-policy", json={"mfa_required": "everyone", "mfa_grace_days": 0}).status_code == 200
    monkeypatch.setattr(mfa_policy, "now", lambda: utcnow() + timedelta(seconds=5))
    pw = sign_in(owner, "pat", "quiet-meadow-4821")
    r = pw.get("/api/keys")
    assert r.status_code == 403 and code_of(r) == "mfa_enrolment_required"
    b, r = sso_sign_in(owner, idp, "sub-pat")
    assert r.status_code == 200 and r.json()["user"]["mfa_enrolment"] is None
    assert b.get("/api/keys").status_code == 200
    assert b.get("/api/auth/state").json()["user"]["mfa_enrolment"] is None
    db = new_session()
    try:  # (the owner, signed in with a password and no authenticator, is now overdue too)
        solo = db.scalar(select(User).where(User.username == "sub-solo"))
        assert solo.password_hash is None and mfa_policy.enrolment(db, solo) is None
        assert mfa_policy.enrolment(db, db.scalar(select(User).where(User.username == "pat")))["due"] is True
    finally:
        db.close()


def test_fy_sso_017_an_admin_without_a_password_confirms_the_backup_with_a_fresh_sign_in_or_a_code(owner, idp, monkeypatch):
    """FY-SSO-017: an admin who signs in only through the provider downloads the secret-key backup after signing in
    again at the provider (reauth: max_age=0, within 5 minutes) or with an authenticator code; without either:
    400 fresh_sign_in_required; 5 minutes later the fresh sign-in no longer counts. Re-authenticating as another
    provider account is 403 sso_wrong_account."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"], role_claim="groups",
              role_mapping={"fy-admins": "admin"})
    b, r = sso_sign_in(owner, idp, "sub-adm", groups=["fy-admins"], auth_time=int(time.time()) - 3600)
    assert r.json()["user"]["role"] == "admin" and r.json()["user"]["has_password"] is False
    pp = {"passphrase": "a long backup passphrase", "passphrase_confirm": "a long backup passphrase"}
    info = b.get("/api/secret-key").json()
    assert info["has_password"] is False and info["sso_linked"] is True and info["fresh_until"] is None
    r = b.post("/api/secret-key/backup", json=pp)
    assert r.status_code == 400 and code_of(r) == "fresh_sign_in_required"
    url = start(b, "reauth")
    assert parse_qs(urlsplit(url).query)["max_age"] == ["0"]
    state, code = idp.authorize(url, "sub-other")
    r = finish(b, state, code)
    assert r.status_code == 403 and code_of(r) == "sso_wrong_account"
    sso_sign_in(owner, idp, "sub-someone", groups=[])  # another person's linked account
    state, code = idp.authorize(start(b, "reauth"), "sub-someone")
    r = finish(b, state, code)
    assert r.status_code == 403 and code_of(r) == "sso_wrong_account"
    state, code = idp.authorize(start(b, "reauth"), "sub-adm")
    r = finish(b, state, code)
    assert r.status_code == 200 and r.json()["fresh_until"]
    r = b.post("/api/secret-key/backup", json=pp)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    real = oidc.clock
    db = new_session()
    try:
        db.execute(update(Session).where(Session.auth_method == "sso").values(fresh_until=utcnow() - timedelta(seconds=1)))
        db.commit()
    finally:
        db.close()
    assert code_of(b.post("/api/secret-key/backup", json=pp)) == "fresh_sign_in_required"
    # An authenticator code works too. Adding one needs a fresh sign-in (no password to type).
    r = b.post("/api/auth/mfa/totp/start").json()
    key = totp.secret_bytes(r["secret"])
    body = {"enrolment": r["enrolment"], "new_code": totp.code_at(key, time.time())}
    assert code_of(b.post("/api/auth/mfa/totp", json=body)) == "reauth_required"
    state, code = idp.authorize(start(b, "reauth"), "sub-adm")
    assert finish(b, state, code).status_code == 200
    assert b.post("/api/auth/mfa/totp", json=body).status_code == 200
    assert audit("auth.mfa_enrolled")[-1][2]["confirmed_with"] == "sso"
    db = new_session()
    try:
        db.execute(update(Session).values(fresh_until=None))
        db.commit()
    finally:
        db.close()
    from forgeyard.security import mfa
    monkeypatch.setattr(mfa, "clock", lambda: time.time() + 30)  # the next step: the enrolment code is used up
    r = b.post("/api/secret-key/backup", json={**pp, "code": totp.code_at(key, time.time() + 30)})
    assert r.status_code == 200
    assert code_of(b.post("/api/secret-key/backup", json={**pp, "code": "000000"})) == "mfa_code_invalid"
    assert real is oidc.clock


# ------------------------------------------------------------------ the provider's lifecycle
def test_fy_sso_018_turning_off_or_deleting_the_provider(owner, idp):
    """FY-SSO-018: turned off, the sign-in page offers nothing and a sign-in started before it cannot finish
    (sso_not_configured); deleting it removes every link (idp.deleted counts them) and keeps the accounts."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    sso_sign_in(owner, idp, "sub-k")
    b = browser(owner)
    state, code = idp.authorize(start(b), "sub-k")
    configure(owner, enabled=False)
    assert b.get("/api/auth/state").json()["sso"] is None
    assert code_of(finish(b, state, code)) == "sso_not_configured"
    assert code_of(b.post("/api/auth/sso/start", json={})) == "sso_not_configured"
    # Removed and made again (a new provider) between start and finish: that sign-in cannot finish either.
    configure(owner, enabled=True, new_accounts="automatic", allowed_domains=["example.com"])
    b = browser(owner)
    state, code = idp.authorize(start(b), "sub-k")
    assert owner.delete("/api/identity-provider").status_code == 200
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"], client_id="another-client")
    assert code_of(finish(b, state, code)) == "sso_not_configured"
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    sso_sign_in(owner, idp, "sub-k")
    r = owner.delete("/api/identity-provider")
    assert r.status_code == 200 and r.json()["provider"] is None
    assert one(UserIdentity) is None and one(User, username="sub-k") is not None
    assert audit("idp.deleted")[-1][2]["links_removed"] == 1


def test_fy_sso_019_secrets_reset_drops_the_client_secret_and_turns_the_provider_off(owner, idp, tmp_path):
    """FY-SSO-019: after the secret key is lost (`secrets reset`), the client secret the new key cannot read is dropped
    and the provider turned off; the rest of its settings stay."""
    from forgeyard.security import crypto, secrets_reset

    configure(owner)
    crypto.key_path().unlink()
    crypto.reset()
    out = secrets_reset.reset()
    assert out["sso_secrets"] == 1
    p = one(IdentityProvider)
    assert p.client_secret is None and p.enabled is False and p.issuer == ISSUER


def test_fy_sso_020_usernames_from_the_provider(owner, idp):
    """FY-SSO-020: a new account's username comes from preferred_username, else the e-mail's local part, lower case and
    [a-z0-9._-] only, with -2, -3 when taken."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    assert sso_sign_in(owner, idp, "s1", preferred_username="Mary Ann")[1].json()["user"]["username"] == "mary-ann"
    assert sso_sign_in(owner, idp, "s2", preferred_username="mary ann")[1].json()["user"]["username"] == "mary-ann-2"
    assert sso_sign_in(owner, idp, "s3", email="j.doe@example.com")[1].json()["user"]["username"] == "j.doe"
    assert sso_sign_in(owner, idp, "s4", preferred_username="x@example.com")[1].json()["user"]["username"] == "x-user"


def test_fy_sso_021_the_redirect_address_follows_public_url(owner, idp, monkeypatch):
    """FY-SSO-021: with PUBLIC_URL set, the provider is told https://<PUBLIC_URL>/sso/callback, and the settings say
    PUBLIC_URL is set; the browser cookie for the half-done sign-in is HttpOnly, SameSite=Strict, only for
    /api/auth/sso."""
    from forgeyard.config import get_settings

    monkeypatch.setenv("PUBLIC_URL", "https://forge.example.test:7860")
    monkeypatch.setenv("ALLOWED_HOSTS", "localhost")
    get_settings.cache_clear()
    out = configure(owner)
    assert out["redirect_uri"] == "https://forge.example.test:7860/sso/callback" and out["public_url_set"] is True
    b = browser(owner)
    r = b.post("/api/auth/sso/start", json={"purpose": "login"})
    q = parse_qs(urlsplit(r.json()["url"]).query)
    assert q["redirect_uri"] == ["https://forge.example.test:7860/sso/callback"]
    cookie = r.headers["set-cookie"].lower()
    assert "forgeyard_sso=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert "path=/api/auth/sso" in cookie


def test_fy_sso_022_next_is_kept_only_as_a_same_site_path(owner, idp):
    """FY-SSO-022: the page to return to comes back from finish; anything that is not a same-site path becomes /."""
    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    for asked, kept in (("/serving?x=1", "/serving?x=1"), ("//evil.example", "/"), ("https://evil.example", "/")):
        b = browser(owner)
        state, code = idp.authorize(start(b, next=asked), "sub-n")
        assert finish(b, state, code).json()["next"] == kept


def test_fy_sso_023_issuer_and_endpoint_addresses(monkeypatch):
    """FY-SSO-023: provider addresses are https, or plain http only on this host; no query, fragment or user."""
    assert oidc.url_problem("https://idp.example.test/realms/a") is None
    assert oidc.url_problem("http://localhost:8080/realms/a") is None
    assert oidc.url_problem("http://127.0.0.1:9000") is None
    assert oidc.url_problem("http://[::1]:9000") is None
    assert oidc.url_problem("http://10.0.0.5:8080") is not None
    assert oidc.url_problem("https://idp.example.test/?a=1") is not None
    assert oidc.url_problem("https://user:pw@idp.example.test") is not None
    assert oidc.url_problem("ftp://idp.example.test") is not None
    assert oidc.normalise_issuer(" https://idp.example.test/realms/a/ ") == "https://idp.example.test/realms/a/"


def test_fy_sso_024_google_is_not_sent_prompt_login(owner, idp, monkeypatch):
    """FY-SSO-024: a fresh sign-in asks Google with max_age=0 only (Google refuses prompt=login); other providers get
    both."""
    meta = {"issuer": oidc.GOOGLE, "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth"}
    url = oidc.authorization_url(meta, client_id="c", redirect_uri="https://x/sso/callback", scopes="openid",
                                 state="s", nonce="n", verifier="v" * 50, fresh=True)
    q = parse_qs(urlsplit(url).query)
    assert q["max_age"] == ["0"] and "prompt" not in q
    url = oidc.authorization_url({**meta, "issuer": ISSUER}, client_id="c", redirect_uri="https://x/sso/callback",
                                 scopes="openid", state="s", nonce="n", verifier="v" * 50, fresh=True)
    assert parse_qs(urlsplit(url).query)["prompt"] == ["login"]


def test_fy_sso_025_the_migration_adds_two_tables_and_two_session_columns_and_goes_down(owner, idp):
    """FY-SSO-025: 0021 adds identity_providers and user_identities, and sessions.auth_method (existing sessions:
    password) and fresh_until; going down to 0020 drops exactly those, keeps every account and session (the owner's
    browser stays signed in), and coming back up works."""
    from alembic import command
    from sqlalchemy import inspect

    from forgeyard.db import get_engine
    from forgeyard.migrate import alembic_config, upgrade

    configure(owner, new_accounts="automatic", allowed_domains=["example.com"])
    sso_sign_in(owner, idp, "sub-m")

    def shape():
        i = inspect(get_engine())
        return set(i.get_table_names()), {c["name"] for c in i.get_columns("sessions")}

    tables, cols = shape()
    assert {"identity_providers", "user_identities"} <= tables and {"auth_method", "fresh_until"} <= cols
    with get_engine().connect() as conn:
        users, sessions_n = (conn.execute(text("SELECT count(*) FROM users")).scalar(),
                             conn.execute(text("SELECT count(*) FROM sessions")).scalar())
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0020_two_factor")
    down_tables, down_cols = shape()
    assert down_tables == tables - {"identity_providers", "user_identities"}
    assert down_cols == cols - {"auth_method", "fresh_until"}
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == users
        assert conn.execute(text("SELECT count(*) FROM sessions")).scalar() == sessions_n
    upgrade()
    assert shape() == (tables, cols)
    with get_engine().connect() as conn:
        assert {r[0] for r in conn.execute(text("SELECT auth_method FROM sessions"))} == {"password"}
    assert owner.get("/api/auth/me").status_code == 200


def test_fy_sso_026_an_issuer_with_a_trailing_slash_is_kept_as_the_provider_spells_it(owner, idp, monkeypatch):
    """FY-SSO-026: a provider that publishes its issuer with a trailing slash (Authentik) is found whether or not the
    admin typed the slash; the stored issuer, the ID token check and the linked accounts use the provider's spelling."""
    slash = ISSUER + "/"
    monkeypatch.setattr(idp, "meta_overrides", {"issuer": slash})
    out = configure(owner, issuer=ISSUER, new_accounts="automatic", allowed_domains=["example.com"])
    assert out["provider"]["issuer"] == slash
    b = browser(owner)
    state, code = idp.authorize(start(b), "sub-slash", iss=slash)
    assert finish(b, state, code).status_code == 200
    assert one(UserIdentity, subject="sub-slash").issuer == slash
