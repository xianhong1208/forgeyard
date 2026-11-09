"""Generated secrets, kept in data_dir/secrets.json (mode 0600).

The one-time setup token lives here while no owner exists: it is printed in the
startup log and required by /api/auth/setup, so whoever reaches a fresh server first
cannot claim it (MCP Gate 1.3.0 fixed exactly that). FORGEYARD_SETUP_TOKEN pins it.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Optional

from forgeyard.config import get_settings

SETUP_TOKEN = "SETUP_TOKEN"


def _path() -> Path:
    return get_settings().secrets_path


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def setup_token() -> str:
    """The setup token, generated and stored on first use."""
    pinned = get_settings().forgeyard_setup_token.strip()
    if pinned:
        return pinned
    data = _load()
    if not data.get(SETUP_TOKEN):
        data[SETUP_TOKEN] = secrets.token_urlsafe(24)
        _save(data)
    return data[SETUP_TOKEN]


def setup_token_matches(candidate: Optional[str]) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate.strip().encode(), setup_token().encode())


def discard_setup_token() -> None:
    data = _load()
    if SETUP_TOKEN in data:
        del data[SETUP_TOKEN]
        _save(data)
