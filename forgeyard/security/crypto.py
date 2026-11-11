"""Secrets at rest: AES-256-GCM with a server key that is never in the database.

Encrypted in the database: Hugging Face tokens on deployments, model replicas' internal keys, the
values of workload environment variables, and cloud providers' API keys (1.2). Passwords, sessions, API keys and join tokens are not
encrypted but hashed (they are only ever compared, never read back).

    stored value   fy1:<key id>:<base64url(nonce || ciphertext || tag)>
    key            32 random bytes in data_dir/secret.key (SECRET_KEY_FILE), mode 0600, made on
                   first start; or FORGEYARD_SECRET_KEY (base64), never written anywhere
    key id         the first 8 hex digits of sha256("forgeyard-secret-key" || key): stored next to
                   the data (server_settings "secret_key"), so a missing or wrong key is noticed at
                   startup instead of when a secret is first needed

Each value is bound to its column (associated data such as "deployments.hf_token"), so a value
copied into another column does not decrypt. Without the key the stored secrets cannot be read:
back the key up apart from the database (docs/https-and-trust.md says how, and what
`python -m forgeyard secrets reset` does when it is lost).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

from sqlalchemy import JSON, Text, inspect as sa_inspect, text
from sqlalchemy.types import TypeDecorator

from forgeyard.config import get_settings

log = logging.getLogger("forgeyard")

PREFIX = "fy1:"
KEY_ENV = "FORGEYARD_SECRET_KEY"
SETTING_KEY = "secret_key"   # server_settings row: {"kid": "..."}
KEY_BYTES = 32


class SecretKeyError(Exception):
    """The key is missing, wrong or unreadable: the server must not start."""


class SecretError(Exception):
    """A stored value cannot be decrypted (wrong key, tampered, or still plain text)."""


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"forgeyard-secret-key" + key).hexdigest()[:8]


def _decode_key(text_value: str, where: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(text_value.strip() + "=" * (-len(text_value.strip()) % 4))
    except (binascii.Error, ValueError):
        raise SecretKeyError(f"{where} is not a base64 key") from None
    if len(key) != KEY_BYTES:
        raise SecretKeyError(f"{where} must hold {KEY_BYTES} bytes (base64), not {len(key)}")
    return key


def key_path() -> Path:
    return get_settings().secret_key_path


def key_from_env() -> str:
    """FORGEYARD_SECRET_KEY, from the environment or the .env file."""
    return get_settings().forgeyard_secret_key.strip()


def load_key(*, create: bool) -> Optional[bytes]:
    """The key from FORGEYARD_SECRET_KEY or the key file. Missing: a new one when `create`, else None."""
    env = key_from_env()
    if env:
        return _decode_key(env, KEY_ENV)
    path = key_path()
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        if not create:
            return None
        key = secrets.token_bytes(KEY_BYTES)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(base64.urlsafe_b64encode(key).decode() + "\n")
        log.warning("Made a new secret key in %s (key id %s). Back it up apart from the database: without it "
                    "stored secrets cannot be read.", path, key_id(key))
        return key
    except (OSError, UnicodeDecodeError) as e:
        raise SecretKeyError(f"cannot read {path}: {e}") from None
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        log.warning("%s was readable by others (mode %o); setting it to 0600.", path, mode)
        os.chmod(path, 0o600)
    return _decode_key(raw, str(path))


class Box:
    def __init__(self, key: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self.kid = key_id(key)
        self._aead = AESGCM(key)

    def seal(self, plaintext: str, purpose: str) -> str:
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), purpose.encode())
        return f"{PREFIX}{self.kid}:{base64.urlsafe_b64encode(nonce + ct).decode().rstrip('=')}"

    def open(self, stored: str, purpose: str) -> str:
        from cryptography.exceptions import InvalidTag

        if not is_sealed(stored):
            raise SecretError(f"{purpose} holds a value that is not encrypted")
        _, kid, body = stored.split(":", 2)
        if kid != self.kid:
            raise SecretError(f"{purpose} was encrypted with key {kid}, but the server has key {self.kid}")
        try:
            blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            return self._aead.decrypt(blob[:12], blob[12:], purpose.encode()).decode("utf-8")
        except (InvalidTag, ValueError, binascii.Error):
            raise SecretError(f"{purpose} cannot be decrypted (changed, or copied from elsewhere)") from None


def is_sealed(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX) and value.count(":") >= 2


# Set by migration 0008 when it encrypted plain-text values: the database is rewritten afterwards
# (forgeyard.migrate.scrub), so no copy of them stays in old pages.
scrub_after_migration = False

_box: Optional[Box] = None
_lock = threading.Lock()


def box() -> Box:
    """The server's box. Makes the key when there is none: only reached after startup checked that
    the database has no secrets under another key (check_database)."""
    global _box
    with _lock:
        if _box is None:
            _box = Box(load_key(create=True))
        return _box


def reset() -> None:
    """Forget the loaded key (tests switch data directories)."""
    global _box, scrub_after_migration
    with _lock:
        _box = None
        scrub_after_migration = False


def seal(value: Optional[str], purpose: str) -> Optional[str]:
    return None if value is None else box().seal(value, purpose)


def unseal(value: Optional[str], purpose: str) -> Optional[str]:
    return None if value is None else box().open(value, purpose)


# ------------------------------------------------------------------ column types
class EncryptedText(TypeDecorator):
    """A text column stored encrypted. Code reads and writes plain strings."""

    impl = Text
    cache_ok = True

    def __init__(self, purpose: str):
        super().__init__()
        self.purpose = purpose

    def process_bind_param(self, value, dialect):
        return seal(value, self.purpose)

    def process_result_value(self, value, dialect):
        return unseal(value, self.purpose)


def seal_env(spec, purpose: str):
    """A workload spec with its env values encrypted (a copy; names stay readable)."""
    if not isinstance(spec, dict) or not isinstance(spec.get("env"), dict) or not spec["env"]:
        return spec
    return {**spec, "env": {k: box().seal(str(v), f"{purpose}.env.{k}") for k, v in spec["env"].items()}}


def unseal_env(spec, purpose: str):
    if not isinstance(spec, dict) or not isinstance(spec.get("env"), dict) or not spec["env"]:
        return spec
    return {**spec, "env": {k: box().open(v, f"{purpose}.env.{k}") for k, v in spec["env"].items()}}


class SpecJSON(TypeDecorator):
    """A workload spec (JSON) whose environment values are stored encrypted."""

    impl = JSON
    cache_ok = True

    def __init__(self, purpose: str):
        super().__init__()
        self.purpose = purpose

    def process_bind_param(self, value, dialect):
        return seal_env(value, self.purpose)

    def process_result_value(self, value, dialect):
        return unseal_env(value, self.purpose)


# ------------------------------------------------------------------ startup check
def recorded_kid(connection) -> Optional[str]:
    """The key id the database's secrets were written with, or None (none recorded yet)."""
    if not sa_inspect(connection).has_table("server_settings"):
        return None
    row = connection.execute(text("SELECT value FROM server_settings WHERE key = :k"), {"k": SETTING_KEY}).first()
    if row is None:
        return None
    value = row[0]
    if isinstance(value, str):
        import json

        value = json.loads(value)
    return (value or {}).get("kid")


def missing_key_message(kid: str) -> str:
    return (f"The secret key is missing: {key_path()} does not exist and {KEY_ENV} is not set, but the database "
            f"holds secrets encrypted with key {kid} (Hugging Face tokens, replica keys, workload environment "
            f"values, provider keys). Restore the key file from your backup and start again. If it is lost for good, run "
            f"`python -m forgeyard secrets reset --yes` (see docs/https-and-trust.md): Hugging Face tokens and "
            f"provider keys must then be entered again, model replicas restart with new keys, and lost workload environment values "
            f"are emptied.")


def check_database(connection) -> None:
    """Refuse to start with a missing or wrong key while the database holds secrets. Never makes a
    key when the database already names one."""
    kid = recorded_kid(connection)
    if kid is None:
        return
    key = load_key(create=False)
    if key is None:
        raise SecretKeyError(missing_key_message(kid))
    if key_id(key) != kid:
        raise SecretKeyError(f"The secret key in {KEY_ENV if key_from_env() else key_path()} has id "
                             f"{key_id(key)}, but the database's secrets were encrypted with key {kid}. Use the "
                             f"key file that belongs to this database (see docs/https-and-trust.md).")
