"""The server's Hugging Face token (1.3.1, decision D2): one token for the whole server, set by an
admin, used to inspect gated models and sent to the agents that download them.

Stored in one `server_settings` row ("hf_token"), encrypted with the server's secret key like every
other stored secret (AES-GCM, bound to "server_settings.hf_token"):

    {"token": "fy1:<key id>:…", "last4": "abcd"}

The token itself is never returned by the API, logged or audited; only whether one is set, its last
four characters, who set it and when.

Which token a request uses, first that is set:

1. the one typed for this one inspection (Add a model), or a deployment's own token (its *More*
   options), which stays an override for that deployment;
2. the server's token.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session as DbSession

from forgeyard.db import ServerSetting, utcnow
from forgeyard.security import crypto

log = logging.getLogger("forgeyard")

KEY = "hf_token"
PURPOSE = "server_settings.hf_token"
MAX_LENGTH = 512


class TokenError(ValueError):
    pass


def clean(token: Optional[str]) -> str:
    """The token as it is stored: one word of printable ASCII."""
    token = (token or "").strip()
    if not token:
        raise TokenError("The token is empty.")
    if len(token) > MAX_LENGTH or any(c.isspace() for c in token) or not token.isascii() or not token.isprintable():
        raise TokenError(f"The token must be one word of at most {MAX_LENGTH} printable characters.")
    return token


def get(db: DbSession) -> Optional[str]:
    """The server's token, or None (none set, or it cannot be decrypted with this server's key)."""
    row = db.get(ServerSetting, KEY)
    stored = (row.value or {}).get("token") if row is not None else None
    if not stored:
        return None
    try:
        return crypto.unseal(stored, PURPOSE)
    except crypto.SecretError as e:
        log.warning("The server's Hugging Face token cannot be read (%s); set it again under Models -> Model "
                    "sources.", e)
        return None


def info(db: DbSession) -> dict:
    """What the web UI may show: whether a token is set, its last four characters, who set it, when."""
    row = db.get(ServerSetting, KEY)
    value = (row.value or {}) if row is not None else {}
    if not value.get("token"):
        return {"set": False, "last4": None, "updated_by": None, "updated_at": None}
    return {"set": True, "last4": value.get("last4"), "updated_by": row.updated_by, "updated_at": row.updated_at}


def put(db: DbSession, token: str, by: str) -> None:
    token = clean(token)
    row = db.get(ServerSetting, KEY)
    if row is None:
        row = ServerSetting(key=KEY)
        db.add(row)
    row.value = {"token": crypto.seal(token, PURPOSE), "last4": token[-4:]}
    row.updated_by = by
    row.updated_at = utcnow()


def clear(db: DbSession) -> bool:
    row = db.get(ServerSetting, KEY)
    if row is None:
        return False
    db.delete(row)
    return True


def choose(own: Optional[str], server: Optional[str]) -> Optional[str]:
    """The token a request uses: its own (an inspection's, a deployment's), else the server's."""
    own = (own or "").strip()
    return own or server or None
