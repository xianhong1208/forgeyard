"""A backup of the secret key (and Forgeyard's CA) that an admin downloads from the console (1.4.2,
issue #127), sealed with a passphrase the admin chooses, and `python -m forgeyard secrets restore`.

The file (`.fykey`) is binary:

    offset  size  what
    0       8     magic  b"FYKEYBK\\0"
    8       2     N, the header's length in bytes (big-endian)
    10      N     header: UTF-8 JSON, for example
                    {"format": "forgeyard-key-backup", "version": 1,
                     "kdf": {"name": "argon2id", "memory_kib": 65536, "iterations": 3, "lanes": 4,
                             "salt": "<base64, 16 bytes>"},
                     "cipher": {"name": "aes-256-gcm", "nonce": "<base64, 12 bytes>"},
                     "kid": "ab12cd34", "created_at": "2026-11-17T08:00:00+00:00"}
    10+N    rest  AES-256-GCM ciphertext followed by its 16-byte tag

- The key that seals it is Argon2id (RFC 9106) of the passphrase (Unicode NFC, UTF-8) with the
  header's salt and parameters: 64 MiB, 3 passes, 4 lanes (RFC 9106's second recommended setting),
  32 bytes out. Salt (16 bytes) and nonce (12 bytes) are random for every file.
- The associated data is everything before the ciphertext (magic, length and header), so a changed
  header (the key id, the KDF parameters) fails like a changed ciphertext: a wrong passphrase, a
  changed byte and a cut-off file are all refused, and nothing is written.
- The plain text is JSON: {"kid", "secret_key" (base64url of the 32 key bytes, as in secret.key),
  "tls": {"ca.key": PEM, "ca.crt": PEM} or null, "created_at", "server_version"}.

What is in it, and why: the secret key (without it the stored Hugging Face tokens, provider keys,
replica keys and app environment values cannot be read) and the CA's key and certificate (without
them every browser, program and agent has to trust a new CA). Not the server certificate and its
key: the server makes them again from the CA on its first start, with the names of the machine it
runs on, and nothing pins them (agents and browsers trust the CA). Not the database: back it up as
usual, apart from this file.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import os
import secrets
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAGIC = b"FYKEYBK\0"
FORMAT = "forgeyard-key-backup"
VERSION = 1
EXTENSION = ".fykey"

KDF_NAME = "argon2id"
MEMORY_KIB = 64 * 1024
ITERATIONS = 3
LANES = 4
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
CIPHER_NAME = "aes-256-gcm"

# What restore accepts: the parameters this version writes may grow in a later one, but never past
# these (a crafted header must not make restore use gigabytes) nor below them (never weaker).
MIN_MEMORY_KIB, MAX_MEMORY_KIB = MEMORY_KIB, 1024 * 1024
MIN_ITERATIONS, MAX_ITERATIONS = ITERATIONS, 16
MIN_LANES, MAX_LANES = 1, 16
MAX_HEADER = 4096
MAX_FILE = 256 * 1024

PASSPHRASE_MIN = 12
PASSPHRASE_MAX = 1024

CA_FILES = ("ca.key", "ca.crt")


class BackupError(Exception):
    """The file cannot be opened: not a backup, an unknown version, cut off, changed, or the wrong
    passphrase. Nothing was written."""


@dataclass
class Contents:
    kid: str
    key: bytes
    tls: Optional[dict[str, str]]   # {"ca.key": PEM, "ca.crt": PEM} or None
    created_at: str


def passphrase_problem(passphrase: str) -> Optional[str]:
    if len(passphrase) < PASSPHRASE_MIN:
        return f"Use a passphrase of at least {PASSPHRASE_MIN} characters."
    if len(passphrase) > PASSPHRASE_MAX:
        return f"Use a passphrase of at most {PASSPHRASE_MAX} characters."
    if len(set(passphrase)) <= 2:
        return "Use more than two different characters in the passphrase."
    return None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: object, what: str, size: int) -> bytes:
    if not isinstance(text, str):
        raise BackupError(f"The header's {what} is missing.")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise BackupError(f"The header's {what} is not base64.") from None
    if len(raw) != size:
        raise BackupError(f"The header's {what} must be {size} bytes, not {len(raw)}.")
    return raw


def _derive(passphrase: str, salt: bytes, memory_kib: int, iterations: int, lanes: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

    secret = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    return Argon2id(salt=salt, length=32, iterations=iterations, lanes=lanes, memory_cost=memory_kib).derive(secret)


def seal(contents: Contents, passphrase: str, *, now: Optional[dt.datetime] = None) -> bytes:
    """The backup file's bytes. The passphrase must already have passed passphrase_problem()."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    problem = passphrase_problem(passphrase)
    if problem:
        raise ValueError(problem)
    salt, nonce = secrets.token_bytes(SALT_BYTES), secrets.token_bytes(NONCE_BYTES)
    created = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    header = json.dumps({
        "format": FORMAT, "version": VERSION,
        "kdf": {"name": KDF_NAME, "memory_kib": MEMORY_KIB, "iterations": ITERATIONS, "lanes": LANES, "salt": _b64(salt)},
        "cipher": {"name": CIPHER_NAME, "nonce": _b64(nonce)},
        "kid": contents.kid, "created_at": created,
    }, separators=(",", ":")).encode("utf-8")
    prefix = MAGIC + struct.pack(">H", len(header)) + header
    from forgeyard import __version__

    body = json.dumps({"kid": contents.kid, "secret_key": base64.urlsafe_b64encode(contents.key).decode("ascii"),
                       "tls": contents.tls, "created_at": created, "server_version": __version__}).encode("utf-8")
    key = _derive(passphrase, salt, MEMORY_KIB, ITERATIONS, LANES)
    return prefix + AESGCM(key).encrypt(nonce, body, prefix)


def read_header(data: bytes) -> tuple[dict, bytes, bytes]:
    """(header, prefix, ciphertext) after checking everything that can be checked without the
    passphrase."""
    if len(data) > MAX_FILE:
        raise BackupError("This file is far larger than a key backup: it is not one.")
    if len(data) < len(MAGIC) + 2 or data[:len(MAGIC)] != MAGIC:
        raise BackupError("This is not a Forgeyard key backup (it does not start with the backup's mark).")
    (size,) = struct.unpack(">H", data[len(MAGIC):len(MAGIC) + 2])
    if size == 0 or size > MAX_HEADER:
        raise BackupError("The backup's header has an impossible length: the file is damaged.")
    end = len(MAGIC) + 2 + size
    if len(data) < end + TAG_BYTES + 1:
        raise BackupError("The backup file is cut off: it ends before its contents.")
    try:
        header = json.loads(data[len(MAGIC) + 2:end].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BackupError("The backup's header cannot be read: the file is damaged.") from None
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise BackupError("This is not a Forgeyard key backup (its header names another format).")
    if header.get("version") != VERSION:
        raise BackupError(f"This backup has format version {header.get('version')!r}; this Forgeyard reads version "
                          f"{VERSION}. Restore it with the Forgeyard version that made it, or a newer one.")
    kdf, cipher = header.get("kdf"), header.get("cipher")
    if not isinstance(kdf, dict) or kdf.get("name") != KDF_NAME:
        raise BackupError(f"This backup names a key derivation this Forgeyard does not use ({kdf!r}).")
    if not isinstance(cipher, dict) or cipher.get("name") != CIPHER_NAME:
        raise BackupError(f"This backup names a cipher this Forgeyard does not use ({cipher!r}).")
    for name, low, high in (("memory_kib", MIN_MEMORY_KIB, MAX_MEMORY_KIB), ("iterations", MIN_ITERATIONS, MAX_ITERATIONS),
                            ("lanes", MIN_LANES, MAX_LANES)):
        value = kdf.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise BackupError(f"The backup's {name} ({value!r}) is outside {low}–{high}: the file is damaged or was not "
                              "made by Forgeyard.")
    if kdf["memory_kib"] < 8 * kdf["lanes"]:
        raise BackupError("The backup's key derivation parameters do not fit together: the file is damaged.")
    _unb64(kdf.get("salt"), "salt", SALT_BYTES)
    _unb64(cipher.get("nonce"), "nonce", NONCE_BYTES)
    return header, data[:end], data[end:]


def open_backup(data: bytes, passphrase: str) -> Contents:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from forgeyard.security import crypto

    header, prefix, ciphertext = read_header(data)
    kdf = header["kdf"]
    key = _derive(passphrase, _unb64(kdf["salt"], "salt", SALT_BYTES), kdf["memory_kib"], kdf["iterations"], kdf["lanes"])
    try:
        body = AESGCM(key).decrypt(_unb64(header["cipher"]["nonce"], "nonce", NONCE_BYTES), ciphertext, prefix)
    except InvalidTag:
        raise BackupError("Wrong passphrase, or the file was changed or damaged: it cannot be opened.") from None
    try:
        inner = json.loads(body.decode("utf-8"))
        raw = inner["secret_key"]
        secret_key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, binascii.Error):
        raise BackupError("The backup opened, but its contents cannot be read.") from None
    if len(secret_key) != crypto.KEY_BYTES or crypto.key_id(secret_key) != inner.get("kid") or inner.get("kid") != header.get("kid"):
        raise BackupError("The backup opened, but its key does not match its key id.")
    tls = inner.get("tls")
    if tls is not None:
        if not isinstance(tls, dict) or set(tls) != set(CA_FILES) or not all(isinstance(v, str) for v in tls.values()):
            raise BackupError("The backup opened, but its certificate authority part cannot be read.")
        _check_ca(tls["ca.key"].encode(), tls["ca.crt"].encode())
    return Contents(kid=inner["kid"], key=secret_key, tls=tls, created_at=str(inner.get("created_at", "")))


def _check_ca(key_pem: bytes, cert_pem: bytes) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
        cert = x509.load_pem_x509_certificate(cert_pem)
    except (ValueError, TypeError):
        raise BackupError("The certificate authority in the backup cannot be read.") from None
    der = serialization.Encoding.DER
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(der, spki) != cert.public_key().public_bytes(der, spki):
        raise BackupError("The certificate authority's key and certificate in the backup do not belong together.")


# ------------------------------------------------------------------ what the server backs up
def current(settings=None) -> Contents:
    """This server's key (from the key file or FORGEYARD_SECRET_KEY) and, when it uses its own CA,
    the CA's key and certificate."""
    from forgeyard.config import get_settings
    from forgeyard.security import crypto, tls as tls_mod

    s = settings or get_settings()
    key = crypto.load_key(create=False)
    if key is None:
        raise crypto.SecretKeyError("The server has no secret key to back up.")
    if crypto.key_id(key) != crypto.box().kid:
        raise crypto.SecretKeyError("The secret key on disk is not the one the server runs with: restart the server.")
    ca = None
    if tls_mod.mode(s) == "forgeyard-ca" and all((s.tls_dir / f).is_file() for f in CA_FILES):
        ca = {f: (s.tls_dir / f).read_text(encoding="ascii") for f in CA_FILES}
    return Contents(kid=crypto.key_id(key), key=key, tls=ca, created_at="")


def filename(kid: str, now: dt.datetime) -> str:
    return f"forgeyard-key-backup-{kid}-{now.strftime('%Y%m%d')}{EXTENSION}"


# ------------------------------------------------------------------ restore
class RestoreConflict(Exception):
    """A different key or CA is already where the backup would go."""


@dataclass
class Plan:
    key_path: Path
    tls_dir: Optional[Path]
    write_key: bool          # False: the same key is already there
    write_ca: bool           # False: no CA in the backup, or the same CA is already there
    replaced: list[Path]     # files a --force restore moves aside


def _same_key_file(path: Path, key: bytes) -> Optional[bool]:
    """True/False for an existing key file holding this key or another; None when there is none."""
    from forgeyard.security import crypto

    if not path.exists():
        return None
    try:
        return crypto._decode_key(path.read_text(encoding="ascii"), str(path)) == key
    except (crypto.SecretKeyError, OSError, UnicodeDecodeError):
        return False


def plan(contents: Contents, key_path: Path, tls_dir: Path, *, force: bool) -> Plan:
    """What restore writes, after checking that it overwrites nothing different (unless `force`)."""
    conflicts: list[str] = []
    replaced: list[Path] = []
    same = _same_key_file(key_path, contents.key)
    if same is False:
        conflicts.append(f"{key_path} holds a different secret key")
        replaced.append(key_path)
    write_ca = False
    if contents.tls is not None:
        existing = [tls_dir / f for f in CA_FILES if (tls_dir / f).exists()]
        differs = [p for p in existing if p.read_bytes() != contents.tls[p.name].encode("ascii")]
        write_ca = len(existing) < len(CA_FILES) or bool(differs)
        if differs:
            conflicts.append(f"{tls_dir} holds a different certificate authority")
            replaced.extend(existing)
    if conflicts and not force:
        raise RestoreConflict("; ".join(conflicts))
    return Plan(key_path, tls_dir if contents.tls is not None else None, same is not True, write_ca, replaced)


def _write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".restoring")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def apply(contents: Contents, p: Plan, *, now: Optional[dt.datetime] = None) -> list[str]:
    """Write the files of a plan. Files it replaces (only with --force) are kept as
    <name>.replaced-<time>. Returns what was done, one line each."""
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%d%H%M%S")
    done: list[str] = []
    for old in p.replaced:
        aside = old.with_name(f"{old.name}.replaced-{stamp}")
        old.rename(aside)
        done.append(f"kept the old {old.name} as {aside}")
    if p.write_key:
        _write(p.key_path, base64.urlsafe_b64encode(contents.key) + b"\n", 0o600)
        done.append(f"wrote the secret key {contents.kid} to {p.key_path} (mode 600)")
    else:
        done.append(f"{p.key_path} already holds the secret key {contents.kid}")
    if p.tls_dir is not None and contents.tls is not None:
        if p.write_ca:
            p.tls_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(p.tls_dir, 0o700)
            _write(p.tls_dir / "ca.key", contents.tls["ca.key"].encode("ascii"), 0o600)
            _write(p.tls_dir / "ca.crt", contents.tls["ca.crt"].encode("ascii"), 0o644)
            done.append(f"wrote the certificate authority to {p.tls_dir} (ca.key mode 600, ca.crt 644)")
        else:
            done.append(f"{p.tls_dir} already holds this certificate authority")
    return done
