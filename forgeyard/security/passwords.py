"""Password hashing (argon2id) and the password policy."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 256

_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters


@lru_cache
def _common() -> frozenset[str]:
    lines = (Path(__file__).with_name("common_passwords.txt")).read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip().lower() for line in lines if line.strip() and not line.startswith("#"))


def policy_error(password: str, username: Optional[str] = None) -> Optional[str]:
    """Why a new password is refused, or None when it is acceptable."""
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    low = password.lower()
    stem = re.sub(r"[\d\W_]+$", "", low)
    if low in _common() or stem in _common():
        return "This password is too common. Pick one that is not on public password lists."
    if len(set(low)) <= 2:
        return "Use more than two different characters."
    if username and username.lower() in low:
        return "The password must not contain the username."
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


# Verifying against this when the account does not exist keeps sign-in time the same
# for unknown usernames, so timing does not reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("forgeyard-timing-equaliser")


def verify_password(password_hash: Optional[str], password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
