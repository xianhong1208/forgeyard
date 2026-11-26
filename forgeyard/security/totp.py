"""Time-based one-time passwords (RFC 6238 over RFC 4226), with the standard library only.

The parameters every authenticator app uses by default: HMAC-SHA-1, 6 digits, a 30-second step. A code is
accepted for the current step and one step either side (clock drift), and each step only once per factor: the
caller records the step a code matched and passes it back as `after` (RFC 6238 section 5.2: a verifier MUST NOT
accept the second attempt of the same code).

Secrets are 160 random bits (RFC 4226 section 4 recommends 160), shown to people as base32 without padding.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote, urlencode

STEP = 30            # seconds per time step
DIGITS = 6
DRIFT = 1            # steps either side of now that still count
SECRET_BYTES = 20    # 160 bits
ISSUER = "Forgeyard"

_CODE_RE = re.compile(r"^\d{6}$")


def new_secret() -> str:
    """A new secret as base32 text (32 characters, no padding)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii")


def secret_bytes(secret: str) -> Optional[bytes]:
    """The key bytes of a base32 secret of exactly 160 bits, or None. Spaces and case are ignored."""
    text = re.sub(r"\s+", "", secret or "").upper()
    if not re.fullmatch(r"[A-Z2-7]+", text):
        return None
    try:
        raw = base64.b32decode(text + "=" * (-len(text) % 8))
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == SECRET_BYTES else None


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    """RFC 4226 section 5.3: HMAC over the 8-byte counter, dynamic truncation, the low `digits` decimal digits."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def step_at(unix_time: float) -> int:
    return int(unix_time // STEP)


def code_at(key: bytes, unix_time: float) -> str:
    return hotp(key, step_at(unix_time))


def clean_code(code: str) -> str:
    """What people type, without spaces (apps show "123 456")."""
    return re.sub(r"\s+", "", code or "")


def looks_like_code(code: str) -> bool:
    return bool(_CODE_RE.match(clean_code(code)))


def match_step(key: bytes, code: str, *, after: Optional[int] = None, now: Optional[float] = None) -> Optional[int]:
    """The time step `code` belongs to (now, or one step either side), or None. Steps at or before `after` (the
    last step this factor accepted) never match, so a code works once and an older code never works again."""
    code = clean_code(code)
    if not _CODE_RE.match(code):
        return None
    current = step_at(time.time() if now is None else now)
    for step in range(current - DRIFT, current + DRIFT + 1):
        if after is not None and step <= after:
            continue
        if hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def otpauth_uri(secret: str, account: str, issuer: str = ISSUER) -> str:
    """The otpauth:// URI authenticator apps read from the QR code (Key Uri Format)."""
    label = quote(f"{issuer}:{account}", safe="@:")
    query = urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{query}"
