"""Single sign-on through an OpenID Connect provider (1.6.0, #39, PR 3).

People:
- `POST /api/auth/sso/start` {purpose: login | link | reauth, next?, password?, code?}: where to send the browser
  (the provider's sign-in page). `link` (My account: link my account at the provider) needs a signed-in session and
  my password or a current authenticator code; `reauth` (prove it is me again, for the secret-key backup or adding
  an authenticator without a password) needs a signed-in session and a linked account. Both make the provider ask
  for the password again (max_age=0).
- `POST /api/auth/sso/finish` {state, code} (or {state, error}): sent by the web UI's /sso/callback page, from our
  own origin, with the provider's answer. Signs in, links, or marks the session as freshly signed in.
- `GET /api/auth/sso/identities`, `POST /api/auth/sso/unlink`: my linked account; unlinking needs another way in
  (a password).

Admins (`/api/identity-provider`): `GET`, `PUT` (create or change the one provider; the client secret is
write-only), `DELETE`, and `POST /check` {issuer} (read its discovery document).

forgeyard/security/sso.py explains the half-done sign-ins and how accounts are found; forgeyard/security/oidc.py the
protocol checks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from forgeyard import audit
from forgeyard.api.auth import start_session
from forgeyard.api.deps import Caller, cookie_secure, current, fail, public_base, require_admin, signed_in, user_out
from forgeyard.api.mfa import ReauthIn, reauth
from forgeyard.config import get_settings
from forgeyard.db import IdentityProvider, User, UserIdentity, get_db, utcnow
from forgeyard.security import oidc, sessions, sso
from forgeyard.security.client import client_ip
from forgeyard.security.ratelimit import limiter, retry_after

router = APIRouter(prefix="/api/auth/sso", tags=["auth"])
admin = APIRouter(prefix="/api/identity-provider", tags=["users"])

CALLBACK_PATH = "/sso/callback"
LIMIT_KIND = "sso"  # failed finishes back off per client address (ratelimit.attempt_*)


def redirect_uri(request: Request) -> str:
    """The address the provider sends people back to: PUBLIC_URL (or the address this browser used) + /sso/callback."""
    return public_base(request) + CALLBACK_PATH


def _safe_next(raw: Optional[str]) -> str:
    if not raw or not raw.startswith("/") or raw.startswith(("//", "/\\")) or any(ord(c) < 32 for c in raw):
        return "/"
    return raw[:512]


def _oidc_fail(e: oidc.OidcError):
    status = {"sso_token_invalid": 400, "sso_bad_issuer": 422}.get(e.code, 502)
    return fail(status, e.code, e.message, reason=e.reason)


def _need_library() -> None:
    if not oidc.available():
        raise fail(503, "sso_unavailable", "Single sign-on needs the server's extra packages (Authlib): install the "
                                           "server with `uv sync --extra server`, or use the server image.")


# ------------------------------------------------------------------ people
class StartIn(ReauthIn):
    purpose: Literal["login", "link", "reauth"] = "login"
    next: Optional[str] = Field(default=None, max_length=512)


@router.post("/start")
def start(body: StartIn, request: Request, response: Response, db: DbSession = Depends(get_db)):
    _need_library()
    p = sso.enabled_provider(db)
    if p is None:
        raise fail(404, "sso_not_configured", "Single sign-on is not set up on this server.")
    user_id = None
    if body.purpose == "link":
        caller = current(request, db)
        if any(i.provider_id == p.id for i in sso.identities_of(db, caller.user.id)):
            raise fail(409, "identity_already_linked", f"Your account is already linked to {p.name}.")
        reauth(db, caller, body)  # a stolen session must not be able to add a way in
        user_id = caller.user.id
    elif body.purpose == "reauth":
        caller = signed_in(request, db)
        if not any(i.provider_id == p.id for i in sso.identities_of(db, caller.user.id)):
            raise fail(409, "no_linked_identity", f"Your account is not linked to {p.name}.")
        user_id = caller.user.id
    try:
        meta = oidc.discover(p.issuer)
    except oidc.OidcError as e:
        raise _oidc_fail(e) from None
    state, nonce, verifier, browser = sso.new_secret(), sso.new_secret(), sso.new_verifier(), sso.new_secret()
    uri = redirect_uri(request)
    sso.pending.put(state, sso.Pending(purpose=body.purpose, provider_id=p.id, verifier=verifier, nonce=nonce,
                                       browser=sso.digest(browser), redirect_uri=uri, user_id=user_id,
                                       provider_key=(p.issuer, p.client_id),
                                       next=_safe_next(body.next)))
    response.set_cookie(sso.BROWSER_COOKIE, browser, max_age=sso.PENDING_SECONDS, httponly=True, samesite="strict",
                        secure=cookie_secure(request), path=sso.COOKIE_PATH)
    url = oidc.authorization_url(meta, client_id=p.client_id, redirect_uri=uri, scopes=sso.scopes_of(p.scopes),
                                 state=state, nonce=nonce, verifier=verifier, fresh=body.purpose != "login")
    return {"url": url}


class FinishIn(BaseModel):
    state: str = Field(max_length=200)
    code: Optional[str] = Field(default=None, max_length=4096)
    error: Optional[str] = Field(default=None, max_length=200)
    error_description: Optional[str] = Field(default=None, max_length=500)


class _Refused(Exception):
    def __init__(self, status: int, code: str, message: str, reason: str, user: Optional[User] = None,
                 extra: Optional[dict] = None):
        self.status, self.code, self.message, self.reason, self.user, self.extra = \
            status, code, message, reason, user, extra or {}


def _fresh_until(claims: dict) -> Optional[datetime]:
    at = oidc.fresh_until(claims)
    return datetime.fromtimestamp(at, tz=timezone.utc) if at is not None else None


@router.post("/finish")
def finish(body: FinishIn, request: Request, response: Response, db: DbSession = Depends(get_db)):
    settings = get_settings()
    ip = client_ip(request)
    wait = limiter.attempt_wait(LIMIT_KIND, ip, settings.login_free_failures, settings.login_ip_failures)
    if wait:
        raise fail(429, "rate_limited", "Too many failed sign-ins. Wait before trying again.",
                   retry_after=retry_after(wait))
    response.delete_cookie(sso.BROWSER_COOKIE, path=sso.COOKIE_PATH, httponly=True, samesite="strict",
                           secure=cookie_secure(request))
    item = sso.pending.take(body.state)
    try:
        if item is None:
            raise _Refused(400, "sso_state_invalid", "This sign-in took too long or was already used. Start again.",
                           "state_invalid")
        browser = request.cookies.get(sso.BROWSER_COOKIE)
        if not browser or sso.digest(browser) != item.browser:
            raise _Refused(400, "sso_state_invalid", "This sign-in was started in another browser. Start again here.",
                           "browser_mismatch")
        if body.error or not body.code:
            raise _Refused(400, "sso_denied", "The identity provider did not sign you in"
                           + (f": {body.error_description or body.error}." if body.error else "."),
                           f"idp_error:{(body.error or 'no_code')[:40]}")
        p = sso.enabled_provider(db)
        if p is None or p.id != item.provider_id or (p.issuer, p.client_id) != item.provider_key:
            raise _Refused(400, "sso_not_configured", "Single sign-on was turned off or changed. Start again.",
                           "provider_changed")
        _need_library()
        try:
            meta = oidc.discover(p.issuer)
            token = oidc.exchange(meta, client_id=p.client_id, client_secret=p.client_secret, code=body.code,
                                  verifier=item.verifier, redirect_uri=item.redirect_uri)
            claims = oidc.verify_id_token(token["id_token"], meta=meta, client_id=p.client_id, nonce=item.nonce)
        except oidc.OidcError as e:
            status = 400 if e.code == "sso_token_invalid" else 502
            raise _Refused(status, e.code, e.message, e.reason if e.code != "sso_token_invalid"
                           else f"token_invalid:{e.reason}", extra={"reason": e.reason}) from None
        # State, browser and ID token all held: the kinds of failure counted below are behind this address.
        limiter.attempt_succeeded(LIMIT_KIND, ip)
        fresh = _fresh_until(claims)
        if item.purpose == "login":
            return _login(db, request, response, p, claims, fresh, item, ip)
        return _for_session(db, request, p, claims, fresh, item, ip)
    except _Refused as r:
        if r.reason in ("state_invalid", "browser_mismatch") or r.reason.startswith("token_invalid"):
            limiter.attempt_failed(LIMIT_KIND, ip)
        db.rollback()
        audit.record(db, "auth.sso_login_failed", actor=r.user, ip=ip, reason=r.reason,
                     purpose=item.purpose if item else None)
        db.commit()
        raise fail(r.status, r.code, r.message, **r.extra) from None


def _login(db: DbSession, request: Request, response: Response, p: IdentityProvider, claims: dict,
           fresh: Optional[datetime], item: sso.Pending, ip: str) -> dict:
    ident = sso.identity_for(db, p.issuer, claims["sub"])
    role = sso.mapped_role(p, claims)
    if ident is None:
        problem = sso.domain_allowed(p, claims)
        if problem:
            raise _Refused(403, f"sso_{problem}", "This server does not take new accounts for that e-mail address. "
                           "Ask an admin." if problem == "domain_not_allowed" else
                           "The identity provider has not verified your e-mail address.", problem)
        automatic = p.new_accounts == "automatic"
        user = User(username=sso.username_for(db, claims), email=sso.email_of(claims), role=role or "user",
                    status="active" if automatic else "pending")
        db.add(user)
        db.flush()
        db.add(UserIdentity(user_id=user.id, provider_id=p.id, issuer=p.issuer, subject=claims["sub"],
                            email=sso.email_of(claims), created_at=utcnow(), last_login_at=utcnow()))
        audit.record(db, "user.registered", actor_name=user.username, target=user.username, ip=ip, via="sso",
                     provider=p.name, role=user.role, status=user.status)
        if not automatic:
            db.commit()
            raise fail(403, "pending_approval", "Your account is waiting for an admin to approve it.",
                       username=user.username)
    else:
        user = db.get(User, ident.user_id)
        if user.status == "pending":
            raise _Refused(403, "pending_approval", "Your account is waiting for an admin to approve it.",
                           "pending_approval", user=user)
        if user.status != "active":
            raise _Refused(403, "account_disabled", "This account is disabled. Ask an admin.", "account_disabled",
                           user=user)
        ident.last_login_at = utcnow()
    changed = sso.apply_role(user, role) if ident is not None else None
    start_session(request, response, db, user, auth_method="sso", fresh_until=fresh)
    audit.record(db, "auth.sso_login", actor=user, ip=ip, provider=p.name, role=changed)
    db.commit()
    return {"user": user_out(user, via_sso=True), "next": item.next, "purpose": "login"}


def _for_session(db: DbSession, request: Request, p: IdentityProvider, claims: dict, fresh: Optional[datetime],
                 item: sso.Pending, ip: str) -> dict:
    """link and reauth: the browser's own session, the one that started, finishes them."""
    row, _ = sessions.resolve(db, request.cookies.get(sessions.COOKIE))
    if row is None or row.user_id != item.user_id:
        raise _Refused(401, "not_signed_in", "Sign in again, then start again.", "session_changed")
    user = row.user
    if fresh is None:
        raise _Refused(400, "sso_not_fresh", f"{p.name} did not ask for your password again. Sign out of {p.name}, "
                       "or use a code from your authenticator app.", "not_fresh", user=user)
    ident = sso.identity_for(db, p.issuer, claims["sub"])
    if item.purpose == "link":
        if ident is not None and ident.user_id != user.id:
            raise _Refused(409, "identity_in_use", f"That {p.name} account is linked to another Forgeyard account.",
                           "identity_in_use", user=user)
        if ident is None:
            db.add(UserIdentity(user_id=user.id, provider_id=p.id, issuer=p.issuer, subject=claims["sub"],
                                email=sso.email_of(claims), created_at=utcnow()))
            audit.record(db, "user.identity_linked", actor=user, target=user.username, ip=ip, provider=p.name,
                         email=sso.email_of(claims))
    elif ident is None or ident.user_id != user.id:
        raise _Refused(403, "sso_wrong_account", f"That is not the {p.name} account linked to yours.",
                       "wrong_account", user=user)
    else:
        ident.last_login_at = utcnow()
        audit.record(db, "auth.sso_login", actor=user, ip=ip, provider=p.name, purpose="reauth")
    row.fresh_until = fresh
    db.commit()
    return {"user": user_out(user, via_sso=row.auth_method == "sso"), "next": item.next, "purpose": item.purpose,
            "fresh_until": fresh}


def identity_out(i: UserIdentity, providers: dict[int, str]) -> dict:
    return {"id": i.id, "provider": providers.get(i.provider_id), "email": i.email, "created_at": i.created_at,
            "last_login_at": i.last_login_at}


@router.get("/identities")
def my_identities(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    p = sso.enabled_provider(db)
    names = {x.id: x.name for x in db.scalars(select(IdentityProvider))}
    return {"identities": [identity_out(i, names) for i in sso.identities_of(db, caller.user.id)],
            "provider": sso.public_info(db) if p else None,
            "fresh_until": caller.session.fresh_until}


class UnlinkIn(BaseModel):
    id: int


@router.post("/unlink")
def unlink(body: UnlinkIn, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    user = caller.user
    mine = sso.identities_of(db, user.id)
    ident = next((i for i in mine if i.id == body.id), None)
    if ident is None:
        raise fail(404, "not_found", "No such linked account.")
    if user.password_hash is None and len(mine) == 1:
        raise fail(409, "last_way_in", "This is your only way to sign in. Ask an admin for a set-password link "
                                       "first.")
    name = db.scalar(select(IdentityProvider.name).where(IdentityProvider.id == ident.provider_id))
    db.delete(ident)
    audit.record(db, "user.identity_unlinked", actor=user, target=user.username, ip=caller.ip, provider=name)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ admins
class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    issuer: str = Field(min_length=1, max_length=512)
    client_id: str = Field(min_length=1, max_length=255)
    # Write-only. None (left out): keep the stored one; "": a public client, no secret.
    client_secret: Optional[str] = Field(default=None, max_length=1024)
    scopes: str = Field(default=sso.DEFAULT_SCOPES, max_length=255)
    role_claim: Optional[str] = Field(default=None, max_length=64)
    role_mapping: dict[str, Literal["admin", "operator", "user"]] = Field(default_factory=dict, max_length=100)
    new_accounts: Literal["approve", "automatic"] = "approve"
    allowed_domains: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True


class CheckIn(BaseModel):
    issuer: str = Field(min_length=1, max_length=512)


def provider_out(db: DbSession, p: Optional[IdentityProvider], request: Request) -> dict:
    s = get_settings()
    base = {"redirect_uri": redirect_uri(request), "public_url_set": bool(s.public_url.strip()),
            "available": oidc.available(), "ca_file": bool(s.sso_ca_file.strip())}
    if p is None:
        return {"provider": None, **base}
    linked = db.scalar(select(func.count()).select_from(UserIdentity).where(UserIdentity.provider_id == p.id)) or 0
    return {"provider": {
        "id": p.id, "kind": p.kind, "name": p.name, "issuer": p.issuer, "client_id": p.client_id,
        "has_client_secret": bool(p.client_secret), "scopes": p.scopes, "role_claim": p.role_claim,
        "role_mapping": p.role_mapping or {}, "new_accounts": p.new_accounts, "allowed_domains": p.allowed_domains or [],
        "enabled": p.enabled, "created_by": p.created_by, "created_at": p.created_at, "updated_at": p.updated_at,
        "linked_accounts": linked,
    }, **base}


@admin.get("")
def get_provider(request: Request, _: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    return provider_out(db, sso.provider(db), request)


@admin.post("/check")
def check_provider(body: CheckIn, _: Caller = Depends(require_admin)):
    """Read the provider's discovery document, as a sign-in would, and say what it offers."""
    _need_library()
    issuer = oidc.normalise_issuer(body.issuer)
    try:
        meta = oidc.discover(issuer, refresh=True)
    except oidc.OidcError as e:
        return {"ok": False, "issuer": issuer, "code": e.code, "reason": e.reason, "problem": e.message}
    return {"ok": True, "issuer": meta["issuer"], "authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta["token_endpoint"], "jwks_uri": meta["jwks_uri"],
            "algorithms": meta.get("id_token_signing_alg_values_supported"),
            "scopes_supported": meta.get("scopes_supported"), "claims_supported": meta.get("claims_supported")}


@admin.put("")
def put_provider(body: ProviderIn, request: Request, caller: Caller = Depends(require_admin),
                 db: DbSession = Depends(get_db)):
    issuer = oidc.normalise_issuer(body.issuer)
    problem = oidc.url_problem(issuer)
    if problem:
        raise fail(422, "sso_bad_issuer", f"The issuer {problem}.")
    domains = sso.normalise_domains(body.allowed_domains)
    for d in domains:
        bad = sso.domain_problem(d)
        if bad:
            raise fail(422, "bad_domain", bad)
    if body.new_accounts == "automatic" and not domains:
        raise fail(422, "domains_required", "Automatic accounts need at least one allowed e-mail domain: without one, "
                                            "anyone with an account at the provider would get in.")
    claim = (body.role_claim or "").strip() or None
    mapping = {k.strip(): v for k, v in body.role_mapping.items() if k.strip()}
    if mapping and not claim:
        raise fail(422, "role_claim_required", "Name the claim the role mapping reads (for example groups).")
    if body.enabled:
        _need_library()
        try:
            issuer = oidc.discover(issuer, refresh=True)["issuer"]  # the provider's own spelling of it
        except oidc.OidcError as e:
            raise fail(422, "sso_discovery_failed", e.message, reason=e.reason) from None
    p = sso.provider(db)
    values = {"name": body.name.strip(), "issuer": issuer, "client_id": body.client_id.strip(),
              "scopes": sso.scopes_of(body.scopes), "role_claim": claim, "role_mapping": mapping,
              "new_accounts": body.new_accounts, "allowed_domains": domains, "enabled": body.enabled}
    now = utcnow()
    if p is None:
        p = IdentityProvider(kind="oidc", created_by=caller.user.username, created_at=now, updated_at=now, **values)
        p.client_secret = body.client_secret or None
        db.add(p)
        db.flush()
        audit.record(db, "idp.created", actor=caller.user, target=p.name, ip=caller.ip, issuer=issuer,
                     client_secret=bool(p.client_secret), new_accounts=p.new_accounts, enabled=p.enabled)
    else:
        changes = {k: [getattr(p, k), v] for k, v in values.items() if getattr(p, k) != v}
        if body.client_secret is not None and (body.client_secret or None) != p.client_secret:
            changes["client_secret"] = "changed" if body.client_secret else "removed"
            p.client_secret = body.client_secret or None
        for k, v in values.items():
            setattr(p, k, v)
        if changes:
            p.updated_at = now
            if "issuer" in changes:
                oidc.reset()
            audit.record(db, "idp.changed", actor=caller.user, target=p.name, ip=caller.ip, **changes)
    db.commit()
    return provider_out(db, p, request)


@admin.delete("")
def delete_provider(request: Request, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    p = sso.provider(db)
    if p is None:
        raise fail(404, "not_found", "No identity provider is set up.")
    linked = db.scalar(select(func.count()).select_from(UserIdentity).where(UserIdentity.provider_id == p.id)) or 0
    db.execute(UserIdentity.__table__.delete().where(UserIdentity.provider_id == p.id))
    db.delete(p)
    audit.record(db, "idp.deleted", actor=caller.user, target=p.name, ip=caller.ip, issuer=p.issuer,
                 links_removed=linked)
    db.commit()
    oidc.reset()
    return provider_out(db, None, request)
