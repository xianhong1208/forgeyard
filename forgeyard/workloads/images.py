"""Recorded ids of pinned engine images, so a copy loaded with `docker load` can be checked offline.

A recipe pins its image as `repo:tag@sha256:<index digest>`. Docker and Podman find such a reference
by the digest only, and a loaded copy often does not carry it:

- Docker's classic store: `docker load` keeps the tag, drops the registry digest, and the image Id
  is the platform's *config* digest.
- Docker's containerd store: the Id is the digest of whatever the archive's top entry was. A pull,
  or a `docker save` from another containerd store, keeps the index digest; a classic `docker save`
  is turned into a new manifest on load, whose digest depends on the archive format and on blobs
  already in that machine's store (seen for real: a loaded llama.cpp image showed an Id that is in no
  registry, see tests/fixtures/images/).
- Podman: the Id is the config digest; `RepoDigests` may also list the manifest digest.

So scripts/pin_images.py records, per pinned image, every registry digest a correct copy can show
(the index, each platform's manifest and config) plus a *content id* per platform: a hash over what
a container gets from the image (its layers' diff ids, platform, entrypoint, command, environment,
user, working directory and stop signal). Every store reports those in `image inspect`, whatever
its Id, and Docker checks each layer against its diff id when it loads it. The result is the
committed lock file forgeyard/recipes/images.lock.json, keyed by the full pinned reference: a
custom recipe that names the same pinned image gets the same check.

The server sends the ids with each desired replica (to agents that list the `image_ids` feature);
the agent uses them only when the pinned digest is not on the machine but the tag is
(agent/workloads/container.py).
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

LOCK_PATH = Path(__file__).resolve().parent.parent / "recipes" / "images.lock.json"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
# What one desired replica may carry: a handful of platforms, three digests and a content id each.
MAX_IDS = 64


def normalize(value: Any) -> Optional[str]:
    """'sha256:<hex>' as it is, a bare 64-hex id (Podman) with the prefix; anything else None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if HEX_RE.match(v):
        v = "sha256:" + v
    return v if DIGEST_RE.match(v) else None


def content_id(os_name: Any, architecture: Any, diff_ids: Any, config: Any) -> Optional[str]:
    """The content id of one platform's image: from the registry config, or from `image inspect`.

    `config` is the config's `config` object (registry) or the inspect `Config` (Docker, Podman): the
    keys are the same. Missing and empty values count as the same (null vs [] vs "")."""
    if not isinstance(diff_ids, list) or not diff_ids or not all(normalize(d) for d in diff_ids):
        return None
    config = config if isinstance(config, dict) else {}

    def strings(key: str) -> list[str]:
        value = config.get(key)
        return [str(v) for v in value] if isinstance(value, list) else []

    body = {"os": str(os_name or ""), "architecture": str(architecture or ""),
            "layers": [normalize(d) for d in diff_ids],
            "entrypoint": strings("Entrypoint"), "cmd": strings("Cmd"), "env": strings("Env"),
            "user": str(config.get("User") or ""), "working_dir": str(config.get("WorkingDir") or ""),
            "stop_signal": str(config.get("StopSignal") or "")}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def content_of_config(blob: dict) -> Optional[str]:
    """The content id of a registry config blob (application/vnd.oci.image.config.v1+json)."""
    return content_id(blob.get("os"), blob.get("architecture"), (blob.get("rootfs") or {}).get("diff_ids"),
                      blob.get("config"))


def content_of_inspect(item: dict) -> Optional[str]:
    """The content id of one `docker image inspect` / `podman image inspect` element."""
    return content_id(item.get("Os"), item.get("Architecture"), (item.get("RootFS") or {}).get("Layers"),
                      item.get("Config"))


def local_ids(item: dict) -> set[str]:
    """Every digest a local image shows: its Id, its repo digests, and Podman's `Digest`."""
    found = {normalize(item.get("Id")), normalize(item.get("Digest"))}
    for ref in item.get("RepoDigests") or []:
        if isinstance(ref, str) and "@" in ref:
            found.add(normalize(ref.rsplit("@", 1)[1]))
    found.discard(None)
    return found  # type: ignore[return-value]


def matches(item: dict, check: dict) -> Optional[str]:
    """How a local image matches the recorded ids: "id" (one of its digests is recorded), "content"
    (its layers and run configuration are those of a recorded platform), or None (a different image)."""
    if local_ids(item) & set(check.get("ids") or []):
        return "id"
    content = content_of_inspect(item)
    if content is not None and content in set(check.get("content") or []):
        return "content"
    return None


def entry_check(entry: dict) -> dict:
    """The lock entry of one image as the agent gets it: {"ids": [...], "content": [...]}."""
    ids, content = set(), set()
    if entry.get("index"):
        ids.add(entry["index"])
    for p in (entry.get("platforms") or {}).values():
        ids.update(d for d in (p.get("manifest"), p.get("config")) if d)
        if p.get("content"):
            content.add(p["content"])
    return {"ids": sorted(ids), "content": sorted(content)}


@lru_cache(maxsize=1)
def load_lock(path: Path = LOCK_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    images = data.get("images") if isinstance(data, dict) else None
    return images if isinstance(images, dict) else {}


def check_for(image: Optional[str]) -> Optional[dict]:
    """The recorded ids of a pinned image (exact reference, digest included), or None."""
    if not image or "@" not in image:
        return None
    entry = load_lock().get(image)
    return entry_check(entry) if isinstance(entry, dict) else None


def parse_check(raw: Any) -> Optional[dict]:
    """What the agent accepts from the wire: digests only, at most MAX_IDS in all; else None."""
    if not isinstance(raw, dict):
        return None
    ids = raw.get("ids") or []
    content = raw.get("content") or []
    if not isinstance(ids, list) or not isinstance(content, list) or len(ids) + len(content) > MAX_IDS:
        return None
    ids_n = [normalize(v) for v in ids]
    content_n = [normalize(v) for v in content]
    if None in ids_n or None in content_n or not (ids_n or content_n):
        return None
    return {"ids": sorted(set(ids_n)), "content": sorted(set(content_n))}  # type: ignore[arg-type]
