"""Look at a model before anyone downloads it: which files, how large, and what the estimator
needs (architecture from config.json, parameter counts from the safetensors headers, the GGUF
metadata). Runs on the server, over the guarded fetcher; a token (for gated repositories) is used
for these requests only and never stored here.
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote

from forgeyard.models import fetch, gguf, safetensors, sources
from forgeyard.models.sources import Source, SourceError

MAX_SHARDS = 256


class InspectError(Exception):
    pass


def _auth(token: Optional[str]) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _hf_error(e: fetch.FetchError, repo: str) -> InspectError:
    if e.status in (401, 403):
        return InspectError(f"{repo} is gated or private: give a Hugging Face token that may read it")
    if e.status == 404:
        return InspectError(f"{repo} (or that revision) does not exist on Hugging Face")
    return InspectError(str(e))


def config_summary(config: dict) -> dict:
    """The fields the KV-cache estimate needs, from a transformers config.json (multimodal
    configs keep them in text_config)."""
    text = config.get("text_config") if isinstance(config.get("text_config"), dict) else config
    heads = int(text.get("num_attention_heads") or 0)
    hidden = int(text.get("hidden_size") or text.get("d_model") or 0)
    head_dim = text.get("head_dim") or (hidden // heads if heads else 0)
    return {
        "architectures": config.get("architectures") or [],
        "model_type": config.get("model_type"),
        "layers": int(text.get("num_hidden_layers") or text.get("n_layer") or 0),
        "heads": heads,
        "kv_heads": int(text.get("num_key_value_heads") or heads or 0),
        "head_dim": int(head_dim or 0),
        "hidden_size": hidden,
        "vocab": int(text.get("vocab_size") or 0),
        "max_context": int(text.get("max_position_embeddings") or 0),
        "dtype": str(config.get("torch_dtype") or config.get("dtype") or text.get("torch_dtype") or "bfloat16"),
        "quantization": (config.get("quantization_config") or {}).get("quant_method"),
    }


def inspect_hf(repo: str, ref: Optional[str], file: Optional[str], *, endpoint: str, policy: fetch.Policy,
               token: Optional[str] = None) -> dict:
    ref = (ref or "main").strip()
    if not (sources.REVISION_RE.match(ref) or sources.REF_RE.match(ref)) or ".." in ref:
        raise InspectError("revision: a branch, tag or commit")
    ep = endpoint.rstrip("/")
    try:
        info = json.loads(fetch.get_bytes(f"{ep}/api/models/{repo}/revision/{quote(ref, safe='')}?blobs=true",
                                          policy, headers=_auth(token), limit=32 * 1024**2))
    except fetch.FetchError as e:
        raise _hf_error(e, repo) from None
    except ValueError:
        raise InspectError("Hugging Face answered with something that is not JSON") from None
    sha = info.get("sha")
    if not isinstance(sha, str) or not sources.REVISION_RE.match(sha):
        raise InspectError("Hugging Face did not say which commit that revision is")
    files = []
    for s in info.get("siblings") or []:
        path = s.get("rfilename")
        try:
            path = sources.safe_relpath(path)
        except SourceError:
            continue
        entry = {"path": path, "size": int(s.get("size") or 0)}
        lfs = s.get("lfs") or {}
        if lfs.get("sha256"):
            entry["sha256"] = lfs["sha256"]
            entry["size"] = int(lfs.get("size") or entry["size"])
        elif s.get("blobId"):
            entry["oid"] = s["blobId"]
        files.append(entry)
    paths = [f["path"] for f in files]
    fmt = sources.detect_format(paths, info.get("library_name"))
    out = {
        "kind": "hf", "repo": repo, "ref": ref, "revision": sha, "format": fmt,
        "pipeline_tag": info.get("pipeline_tag"), "library": info.get("library_name"),
        "task_hint": sources.task_hint(info.get("pipeline_tag"), info.get("tags") or []) or (
            "chat" if fmt == "gguf" else None),
        "gated": bool(info.get("gated")), "private": bool(info.get("private")),
    }

    def url(path: str) -> str:
        return f"{ep}/{repo}/resolve/{sha}/{quote(path)}"

    def ranged(path: str):
        return lambda a, b: fetch.get_range(url(path), policy, a, b, headers=_auth(token))

    try:
        if fmt == "gguf":
            choices = sources.gguf_choices(files)
            out["gguf_files"] = choices
            chosen = file or sources.default_gguf(choices)
            if chosen is None:
                raise InspectError("this repository has several GGUF files: choose one")
            if chosen not in {c["path"] for c in choices}:
                raise InspectError(f"{chosen} is not one of its GGUF files")
            out["file"] = chosen
            out["gguf"] = gguf.read_remote(ranged(chosen)).summary()
        elif fmt == "safetensors":
            config = json.loads(fetch.get_bytes(url("config.json"), policy, headers=_auth(token), limit=4 * 1024**2))
            out["config"] = config_summary(config)
            shards = [p for p in paths if p.endswith(".safetensors") and "/" not in p]
            if not shards:
                shards = [p for p in paths if p.endswith(".safetensors")]
            if len(shards) > MAX_SHARDS:
                raise InspectError(f"more than {MAX_SHARDS} safetensors files")
            out["weights"] = safetensors.merge([safetensors.read_remote(ranged(p)) for p in shards])
        elif fmt == "ctranslate2":
            out["weights_bytes"] = sum(f["size"] for f in files if f["path"] == "model.bin")
        else:
            raise InspectError("no safetensors, GGUF or CTranslate2 weights in this repository")
    except fetch.FetchError as e:
        raise _hf_error(e, repo) from None
    except (gguf.GGUFError, safetensors.SafetensorsError, ValueError) as e:
        raise InspectError(str(e)) from None
    selected = sources.select_files(files, fmt, out.get("file"))
    out["files"] = selected
    out["size_bytes"] = sum(f["size"] for f in selected)
    if fmt == "safetensors" and out["weights"]["bytes"] == 0:
        out["weights"]["bytes"] = sum(f["size"] for f in selected if f["path"].endswith(".safetensors"))
    return out


def inspect_url(url: str, *, policy: fetch.Policy) -> dict:
    name = sources.url_filename(url)
    try:
        header = gguf.read_remote(lambda a, b: fetch.get_range(url, policy, a, b))
        with fetch.open_url(url, policy, headers={"Range": "bytes=0-0"}) as resp:
            total = resp.headers.get("Content-Range", "").rsplit("/", 1)[-1]
        size = int(total) if total.isdigit() else header.data_offset + header.tensor_bytes
    except fetch.FetchError as e:
        raise InspectError(str(e)) from None
    except gguf.GGUFError as e:
        raise InspectError(str(e)) from None
    return {"kind": "url", "url": url, "format": "gguf", "file": name, "gguf": header.summary(),
            "files": [{"path": name, "size": size}], "size_bytes": size, "task_hint": "chat"}


def inspect_source(src: Source, *, ref: Optional[str], file: Optional[str], endpoint: str, policy: fetch.Policy,
                   url_policy: fetch.Policy, local_dirs: list[str], token: Optional[str] = None,
                   fmt: Optional[str] = None, size_bytes: Optional[int] = None) -> dict:
    if src.kind == "hf":
        return inspect_hf(src.repo, ref, file, endpoint=endpoint, policy=policy, token=token)
    if src.kind == "url":
        return inspect_url(src.url, policy=url_policy)
    if not sources.local_allowed(src.path, local_dirs):
        raise InspectError(f"{src.path} is not under a directory allowed for local models "
                           f"({', '.join(local_dirs) or 'none configured'})")
    if fmt not in ("safetensors", "gguf", "ctranslate2"):
        raise InspectError("a local model needs its format (safetensors, gguf or ctranslate2)")
    if fmt == "gguf" and not file:
        raise InspectError("a local GGUF model needs the file name")
    # The server cannot read the machine's disk: deployments of local models declare their memory.
    return {"kind": "local", "path": src.path, "format": fmt, "file": file, "files": [],
            "size_bytes": int(size_bytes or 0), "declared_only": True}
