"""The agent's state directory: the machine credential, readable by its owner only (0600)."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

log = logging.getLogger("forgeyard.agent")

STATE_ENV = "FORGEYARD_AGENT_STATE_DIR"
CREDENTIAL_FILE = "credential.json"


def default_state_dir() -> Path:
    if os.environ.get(STATE_ENV):
        return Path(os.environ[STATE_ENV])
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/var/lib/forgeyard-agent")
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "forgeyard-agent"


def load(state_dir: Path) -> Optional[dict]:
    path = state_dir / CREDENTIAL_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        log.warning("%s was readable by others (mode %o); setting it to 0600.", path, mode)
        os.chmod(path, 0o600)
    if not isinstance(data, dict) or not data.get("credential"):
        raise ValueError(f"{path} is not a Forgeyard agent credential file")
    return data


def save(state_dir: Path, data: dict) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    path = state_dir / CREDENTIAL_FILE
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)  # in case the file existed with looser permissions
    os.replace(tmp, path)
    return path
