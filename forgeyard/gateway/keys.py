"""API keys for the gateway.

A key is `fyk_` + 43 random URL-safe characters (256 bits), shown once when it is created. The
server keeps its SHA-256 (a key has far too much entropy to guess, so a slow hash adds nothing) and
its first 12 characters, the prefix people see to tell keys apart. Every request looks the key up
again, so a revoke, an expiry or a disabled account stops it on the very next request.

Limits per key are requests (rpm) and tokens (tpm) per minute. A key without its own value gets
the server's default; admins set the defaults and the caps (the most a user may give their own
key), and may give any key any value. 0 or None: no limit.

The `cloud` setting of a key (1.2) says whether its requests may reach a provider outside your
network: off (never; every key from before 1.2), fallback (only when a route spills from a local
tier) or on (cloud-only models too). Admins set the value a new key gets and the most a user may
give their own key; as with limits, a value an admin sets on someone else's key is locked for its
owner. The console's playground uses the most a user may give a key (admins: on).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.config import get_settings
from forgeyard.db import ApiKey, ServerSetting, User, as_utc, utcnow
from forgeyard.gateway.errors import GatewayError

KEY_PREFIX = "fyk_"
PREFIX_CHARS = 12          # "fyk_" + 8: what lists show
LIMITS_KEY = "gateway_limits"
CLOUD_KEY = "gateway_cloud"
CLOUD_LEVELS = ("off", "fallback", "on")
LAST_USED_EVERY = timedelta(seconds=60)  # last_used_at is written at most this often per key


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> tuple[str, str, str]:
    """(the key, its visible prefix, its hash)."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:PREFIX_CHARS], hash_key(key)


# ------------------------------------------------------------------ limits
def _positive(value) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def get_limits(db: DbSession) -> dict:
    """{default_rpm, default_tpm, max_rpm, max_tpm}: None means no limit / no cap."""
    row = db.get(ServerSetting, LIMITS_KEY)
    s = get_settings()
    value = row.value if row is not None else {"default_rpm": s.gateway_default_rpm, "default_tpm": s.gateway_default_tpm,
                                               "max_rpm": s.gateway_max_rpm, "max_tpm": s.gateway_max_tpm}
    return {k: _positive(value.get(k)) for k in ("default_rpm", "default_tpm", "max_rpm", "max_tpm")}


def set_limits(db: DbSession, limits: dict, by: str) -> None:
    row = db.get(ServerSetting, LIMITS_KEY)
    if row is None:
        row = ServerSetting(key=LIMITS_KEY)
        db.add(row)
    row.value = {k: _positive(limits.get(k)) for k in ("default_rpm", "default_tpm", "max_rpm", "max_tpm")}
    row.updated_by = by
    row.updated_at = utcnow()


def effective(own: Optional[int], default: Optional[int], cap: Optional[int]) -> Optional[int]:
    """The limit a key works with. Its own value wins (a user could only set it within the cap; an
    admin may set anything). Without one: the default, held to the cap."""
    if own:
        return own
    if default and cap:
        return min(default, cap)
    return default or cap


def check_own_limit(name: str, value: Optional[int], cap: Optional[int]) -> Optional[str]:
    """Why a user may not give their own key this limit (None: fine). `value` None means "the
    default", which is always fine."""
    if value is None:
        return None
    if value < 1:
        return f"{name}: at least 1 (leave it empty for the server's default)"
    if cap and value > cap:
        return f"{name}: at most {cap}, the most an admin allows for a key"
    return None


# ------------------------------------------------------------------ cloud
def cloud_level(value) -> str:
    return value if value in CLOUD_LEVELS else "off"


def get_cloud(db: DbSession) -> dict:
    """{default, max}: what a new key gets, and the most a user may give their own key."""
    row = db.get(ServerSetting, CLOUD_KEY)
    s = get_settings()
    value = row.value if row is not None else {"default": s.gateway_cloud_default, "max": s.gateway_cloud_max}
    out = {"default": cloud_level(value.get("default")), "max": cloud_level(value.get("max"))}
    if CLOUD_LEVELS.index(out["default"]) > CLOUD_LEVELS.index(out["max"]):
        out["default"] = out["max"]
    return out


def set_cloud(db: DbSession, value: dict, by: str) -> None:
    row = db.get(ServerSetting, CLOUD_KEY)
    if row is None:
        row = ServerSetting(key=CLOUD_KEY)
        db.add(row)
    row.value = {"default": cloud_level(value.get("default")), "max": cloud_level(value.get("max"))}
    row.updated_by = by
    row.updated_at = utcnow()


def cloud_above(value: str, cap: str) -> bool:
    return CLOUD_LEVELS.index(cloud_level(value)) > CLOUD_LEVELS.index(cloud_level(cap))


def console_cloud(db: DbSession, user: User) -> str:
    """The playground's cloud setting: admins on; others what they could give their own key."""
    return "on" if user.role == "admin" else get_cloud(db)["max"]


# ------------------------------------------------------------------ authentication
@dataclass
class KeyCaller:
    """Who a gateway request is for."""

    source: str                       # api | console
    user_id: int
    username: str
    key_id: int = 0                   # 0: the console
    key_name: Optional[str] = None
    models: Optional[list] = None     # None: every deployment
    rpm: Optional[int] = None
    tpm: Optional[int] = None
    cloud: str = "off"                # off | fallback | on


def bearer(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def authenticate(db: DbSession, authorization: Optional[str], *, now: Optional[datetime] = None) -> KeyCaller:
    """The key's owner, allowlist and limits, or GatewayError(401). Writes last_used_at (at most once
    a minute per key); the caller commits."""
    now = now or utcnow()
    raw = bearer(authorization)
    if raw is None:
        raise GatewayError(401, "missing_api_key", "Send your Forgeyard API key as 'Authorization: Bearer fyk_…'.")
    if not raw.startswith(KEY_PREFIX) or len(raw) > 128:
        raise GatewayError(401, "invalid_api_key", "This is not a valid Forgeyard API key.")
    digest = hash_key(raw)
    key = db.scalar(select(ApiKey).where(ApiKey.hash == digest))
    if key is None or not hmac.compare_digest(key.hash, digest):
        raise GatewayError(401, "invalid_api_key", "This is not a valid Forgeyard API key.")
    if key.revoked_at is not None:
        raise GatewayError(401, "revoked_api_key", "This API key was revoked.")
    expires = as_utc(key.expires_at)
    if expires is not None and expires <= now:
        raise GatewayError(401, "expired_api_key", "This API key has expired.")
    user: Optional[User] = key.user
    if user is None or user.status != "active":
        raise GatewayError(401, "account_disabled", "The account this API key belongs to is not active.")
    last = as_utc(key.last_used_at)
    if last is None or now - last >= LAST_USED_EVERY:
        key.last_used_at = now
    limits = get_limits(db)
    return KeyCaller(source="api", user_id=user.id, username=user.username, key_id=key.id, key_name=key.name,
                     models=list(key.models) if key.models is not None else None,
                     rpm=effective(key.rpm, limits["default_rpm"], limits["max_rpm"]),
                     tpm=effective(key.tpm, limits["default_tpm"], limits["max_tpm"]), cloud=cloud_level(key.cloud))
