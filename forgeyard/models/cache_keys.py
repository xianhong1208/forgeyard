"""What a cache entry is, for removing it from a machine's model cache (1.4, "cache_admin").

The agent lists the models in its cache under the same keys it reports as cached
(forgeyard.models.sources.cache_key): `hub/models--<org>--<name>@<commit>` for a Hugging Face
revision, `hub/…@<commit>/<file>.gguf` for one GGUF file of it, `url/<id>` for a single-file model,
and `engine/<sha256[:16]>` for an unpacked engine archive.

A *target* describes what a removal would delete, so both sides can tell whether a replica uses it:
the server before it asks (409, naming the deployments), the agent again before it deletes (the
server's view may be a moment old). A key is never turned into a path here: the agent looks it up
among the entries it found on its own disk and uses the paths it computed itself.
"""

from __future__ import annotations

import re
from typing import Optional

from forgeyard.models import sources

HF_KEY_RE = re.compile(r"^(hub/models--[A-Za-z0-9._-]{1,200})@([0-9a-f]{40})(?:/(.+))?$")
URL_KEY_RE = re.compile(r"^url/[0-9a-f]{24}$")
ENGINE_KEY_RE = re.compile(r"^engine/([0-9a-f]{16})$")
MAX_KEY = 800


def engine_key(sha16: str) -> str:
    return f"engine/{sha16}"


def parse(key: str) -> Optional[dict]:
    """The target a key names, or None when it is not a cache key. For a GGUF file of a revision the
    target names the file (`file`); whether removing it removes the whole revision (the last model
    file of it) is decided by the caller, which knows the entry."""
    if not isinstance(key, str) or len(key) > MAX_KEY:
        return None
    m = HF_KEY_RE.match(key)
    if m:
        out = {"kind": "hf", "dir": m.group(1), "revision": m.group(2), "paths": None}
        if m.group(3):
            try:
                out["file"] = sources.safe_relpath(m.group(3))
            except sources.SourceError:
                return None
            if not out["file"].lower().endswith(".gguf"):
                return None
        return out
    if URL_KEY_RE.match(key):
        return {"kind": "url", "dir": key}
    m = ENGINE_KEY_RE.match(key)
    if m:
        return {"kind": "engine", "id": m.group(1)}
    return None


def same_choice(path: str, file: str) -> bool:
    """`path` is `file` or another part of the same split GGUF model (…-00002-of-00003.gguf)."""
    if path == file:
        return True
    a, b = sources.SPLIT_GGUF_RE.match(path), sources.SPLIT_GGUF_RE.match(file)
    return bool(a and b and a.group(1) == b.group(1) and a.group(3) == b.group(3))


def model_uses(model: Optional[dict], target: dict) -> bool:
    """Whether a replica's model block (as the server sends it, or as the agent validated it) needs
    anything the target would delete."""
    if not isinstance(model, dict):
        return False
    kind = model.get("kind")
    if target.get("kind") == "hf" and kind == "hf":
        try:
            d = sources.hf_dir(str(model.get("repo") or ""))
        except sources.SourceError:
            return False
        if d != target["dir"] or str(model.get("revision") or "").lower() != target["revision"]:
            return False
        if target.get("paths") is None and not target.get("file"):
            return True  # the whole revision
        want = {str(f.get("path")) for f in (model.get("files") or []) if isinstance(f, dict)}
        if model.get("file"):
            want.add(str(model["file"]))
        if target.get("paths") is not None:
            return bool(want & set(target["paths"]))
        return any(same_choice(p, target["file"]) for p in want)
    if target.get("kind") == "url" and kind == "url":
        return sources.url_dir(str(model.get("url") or "")) == target["dir"]
    return False


def engine_uses(engine: Optional[dict], target: dict) -> bool:
    if target.get("kind") != "engine" or not isinstance(engine, dict):
        return False
    return str(engine.get("sha256") or "").lower()[:16] == target["id"]
