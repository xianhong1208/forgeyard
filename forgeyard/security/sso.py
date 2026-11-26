"""Single sign-on (1.6.0, #39, PR 3): the half-done sign-ins and what a provider's ID token means for accounts.

Half-done sign-ins. `POST /api/auth/sso/start` makes a random `state`, a PKCE verifier and a nonce, and keeps them
here, in the server's memory, found by sha256(state), for 10 minutes and once only. Why memory and not a table:
nothing else needs them, they are useless after a restart (the person simply signs in again), and there is no
account yet for login_challenges' user_id on someone's first sign-in. The browser that started gets a random token
in its own cookie (HttpOnly, SameSite=Strict, only for /api/auth/sso); `finish` must come with it, so a sign-in
someone else started (a link with their code and state) cannot sign this browser in as them.

Why the provider's redirect does not finish the sign-in itself: the session cookie is SameSite=Strict, and the
redirect back from the provider is a cross-site navigation, so a cookie set or read during it is not used. The
provider sends the browser to the web UI's page /sso/callback, which POSTs code and state to
/api/auth/sso/finish from our own origin; the session cookie is set on that answer.

Accounts. A provider's account is found by (issuer, subject), never by e-mail (nOAuth, 2023: an e-mail address is
whatever the provider's user typed). Someone the server does not know yet gets an account as the provider says
(`approve`: pending until an admin approves; `automatic`: active, only for an allowed e-mail domain). An existing
account is linked only by its owner, signed in, in My account.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import IdentityProvider, User, UserIdentity

PENDING_SECONDS = 600
MAX_PENDING = 1000
BROWSER_COOKIE = "forgeyard_sso"
COOKIE_PATH = "/api/auth/sso"
PURPOSES = ("login", "link", "reauth")
NEW_ACCOUNTS = ("approve", "automatic")
ROLES = ("admin", "operator", "user")
RANK = {"user": 0, "operator": 1, "admin": 2}
DEFAULT_SCOPES = "openid email profile"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Pending:
    purpose: str                    # login | link | reauth
    provider_id: int
    verifier: str
    nonce: str
    browser: str                    # sha256 of the browser cookie
    redirect_uri: str
    user_id: Optional[int] = None   # link and reauth: who started it
    provider_key: tuple = ()        # (issuer, client id): the provider changed in between, the sign-in cannot finish
    next: str = "/"
    created: float = field(default_factory=time.monotonic)


class PendingStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, Pending] = {}

    def put(self, state: str, item: Pending) -> None:
        with self._lock:
            now = time.monotonic()
            for k in [k for k, v in self._items.items() if now - v.created >= PENDING_SECONDS]:
                del self._items[k]
            while len(self._items) >= MAX_PENDING:  # the oldest go first
                del self._items[next(iter(self._items))]
            self._items[digest(state)] = item

    def take(self, state: Optional[str]) -> Optional[Pending]:
        """The sign-in `state` belongs to, used up; None when unknown, used or too old."""
        if not state:
            return None
        with self._lock:
            item = self._items.pop(digest(state), None)
        if item is None or time.monotonic() - item.created >= PENDING_SECONDS:
            return None
        return item

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


pending = PendingStore()


def reset() -> None:
    pending.reset()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def new_verifier() -> str:
    return secrets.token_urlsafe(64)  # 86 characters (RFC 7636: 43 to 128)


# ------------------------------------------------------------------ the provider
def provider(db: DbSession) -> Optional[IdentityProvider]:
    return db.scalar(select(IdentityProvider).order_by(IdentityProvider.id).limit(1))


def enabled_provider(db: DbSession) -> Optional[IdentityProvider]:
    p = provider(db)
    return p if p is not None and p.enabled else None


def public_info(db: DbSession) -> Optional[dict]:
    """What the sign-in page needs: the enabled provider's name, or None."""
    from forgeyard.security import oidc

    p = enabled_provider(db)
    return {"name": p.name} if p is not None and oidc.available() else None


def normalise_domains(raw: list[str]) -> list[str]:
    out: list[str] = []
    for d in raw:
        d = d.strip().lower().lstrip("@").rstrip(".")
        if d and d not in out:
            out.append(d)
    return out


def domain_problem(domain: str) -> Optional[str]:
    if not re.fullmatch(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}", domain):
        return f"{domain!r} is not a domain name (example.com)"
    return None


def scopes_of(raw: str) -> str:
    """Space-separated scopes, `openid` always first and once."""
    words = [w for w in raw.split() if w]
    return " ".join(["openid", *[w for w in dict.fromkeys(words) if w != "openid"]])


# ------------------------------------------------------------------ claims
def email_of(claims: dict) -> Optional[str]:
    email = claims.get("email")
    return email.strip()[:255] if isinstance(email, str) and "@" in email else None


def domain_allowed(p: IdentityProvider, claims: dict) -> Optional[str]:
    """None when an account may be made for these claims, else why not: `domain_not_allowed` (the list is set and
    the address is not in it, or there is no address) or `email_unverified` (the provider says it did not check it)."""
    domains = p.allowed_domains or []
    if not domains:
        return None
    email = email_of(claims)
    if email is None or email.rsplit("@", 1)[1].lower() not in domains:
        return "domain_not_allowed"
    if claims.get("email_verified") is False or claims.get("email_verified") == "false":
        return "email_unverified"
    return None


def mapped_role(p: IdentityProvider, claims: dict) -> Optional[str]:
    """The role the claim gives: the highest one any of its values maps to, `user` when none maps; None (leave the
    role alone) when no mapping is set or the token does not carry the claim at all."""
    if not p.role_claim or not p.role_mapping:
        return None
    if p.role_claim not in claims:
        return None
    value = claims[p.role_claim]
    values = [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)] \
        if isinstance(value, list) else []
    roles = [p.role_mapping[v] for v in values if p.role_mapping.get(v) in ROLES]
    return max(roles, key=RANK.__getitem__) if roles else "user"


def apply_role(user: User, role: Optional[str]) -> Optional[list[str]]:
    """Set the role from the provider; the owner's is never changed. Returns [before, after] when it changed."""
    if role is None or user.is_owner or user.role == role:
        return None
    before, user.role = user.role, role
    return [before, role]


_NAME_BAD = re.compile(r"[^a-z0-9._-]+")


def username_for(db: DbSession, claims: dict) -> str:
    """A free username from the provider's preferred_username or the e-mail's local part (lower case, [a-z0-9._-],
    2 to 64 characters), with -2, -3, … when it is taken."""
    raw = claims.get("preferred_username")
    if not isinstance(raw, str) or not raw.strip():
        email = email_of(claims)
        raw = email.split("@", 1)[0] if email else "sso-user"
    base = _NAME_BAD.sub("-", raw.strip().lower().split("@", 1)[0]).strip("._-")[:60] or "sso-user"
    if len(base) < 2:
        base = f"{base}-user"
    name, n = base, 1
    while db.scalar(select(User.id).where(User.username == name)) is not None:
        n += 1
        name = f"{base[:60 - len(str(n))]}-{n}"
    return name


def identity_for(db: DbSession, issuer: str, subject: str) -> Optional[UserIdentity]:
    return db.scalar(select(UserIdentity).where(UserIdentity.issuer == issuer, UserIdentity.subject == subject))


def identities_of(db: DbSession, user_id: int) -> list[UserIdentity]:
    return list(db.scalars(select(UserIdentity).where(UserIdentity.user_id == user_id).order_by(UserIdentity.id)))
