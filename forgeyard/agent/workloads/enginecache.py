"""Engine compile caches on this machine (1.5.0, issue #37, "engine_cache"; the server's half and why it is shaped
like this: forgeyard/models/enginecache.py).

A container replica whose desired entry carries `engine_cache: {key, env}` gets the directory
<model cache>/engine-cache/<key> (made here, mode 0700) mounted writable at ENGINE_CACHE_MOUNT, with `env` naming
that path. It lives beside the model cache rather than in the agent's state directory because the container
engine mounts host paths: an agent that runs in a container already sees its model cache at the host's own path
(the model mount needs that), not its state directory.

`prune` removes, among the directories there (only names of the server's key shape, never a link):
- those of a deployment the server no longer lists in `engine_caches` (deleted, or its recipe stopped keeping one);
- the older ones of a replica that is wanted with another key (a new recipe version or image);
never one that a replica on this machine has or is wanted with.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Iterable, Optional

from forgeyard.machines.protocol import ENGINE_CACHE_KEY_RE as KEY_RE, ENGINE_CACHE_PREFIX_PARTS, ENGINE_CACHE_PREFIX_RE as PREFIX_RE
from forgeyard.workloads.spec import ENV_KEY_RE, RESERVED_ENV, RESERVED_ENV_PREFIXES

log = logging.getLogger("forgeyard.agent")

DIR_NAME = "engine-cache"


def parse(block) -> Optional[dict]:
    """A desired replica's `engine_cache`, or None when it is not one this agent accepts."""
    if not isinstance(block, dict):
        return None
    key, env = block.get("key"), block.get("env")
    if not isinstance(key, str) or not KEY_RE.match(key):
        return None
    if not isinstance(env, str) or not ENV_KEY_RE.match(env) or env in RESERVED_ENV \
            or env.startswith(RESERVED_ENV_PREFIXES):
        return None
    return {"key": key, "env": env}


def same(want: Optional[dict], have: Optional[dict]) -> bool:
    """The desired block and the record's name the same cache (the record may also carry its `dir`)."""
    pick = lambda b: (b.get("key"), b.get("env")) if b else None  # noqa: E731
    return pick(want) == pick(have)


def parse_keep(value) -> Optional[set[str]]:
    """`desired.engine_caches`: the deployments whose caches may stay (None: the server did not say)."""
    if not isinstance(value, list):
        return None
    return {p for p in value if isinstance(p, str) and PREFIX_RE.match(p)}


def _replica(key: str) -> str:
    return "-".join(key.split("-")[:ENGINE_CACHE_PREFIX_PARTS + 1])


def _deployment(key: str) -> str:
    return "-".join(key.split("-")[:ENGINE_CACHE_PREFIX_PARTS])


def directory(root: Path, key: str) -> Path:
    """The replica's cache directory, made when missing (0700). Refuses a key of another shape or a link."""
    if not KEY_RE.match(key):
        raise ValueError(f"not an engine cache key: {key!r}")
    base = Path(root) / DIR_NAME
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = base / key
    if path.is_symlink():
        raise ValueError(f"{path} is a link")
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def prune(root: Path, keep: set[str], wanted: Iterable[str]) -> list[str]:
    """Remove what no replica will use again (see the module's docstring). Returns the names removed."""
    base = Path(root) / DIR_NAME
    if not base.is_dir() or base.is_symlink():
        return []
    wanted = set(wanted)
    superseded = {_replica(k) for k in wanted}
    removed = []
    for path in sorted(base.iterdir()):
        name = path.name
        if not KEY_RE.match(name) or path.is_symlink() or not path.is_dir() or name in wanted:
            continue
        if _deployment(name) in keep and _replica(name) not in superseded:
            continue  # a replica of a deployment that still exists, not running here now (asleep, moved)
        try:
            shutil.rmtree(path)
            removed.append(name)
        except OSError as e:
            log.warning("Could not remove the engine cache %s: %s", path, e.strerror or e)
    if removed:
        log.info("Removed %d engine cache(s) nothing uses any more: %s.", len(removed), ", ".join(removed))
    return removed
