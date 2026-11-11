"""Where a model comes from, and where it lives in an agent's model cache.

Sources (design section 8: nothing else is accepted):

    hf://<org>/<name>          a Hugging Face repository, pinned to a commit
    https://host/path/x.gguf   a single GGUF file, only from hosts on the admin allowlist
    local:///srv/models/x      a directory on the machine, only under admin-configured directories

The cache is keyed by source and revision, shared by every replica on a machine, and laid out
like the Hugging Face cache so engines that look for one (speaches, faster-whisper, vLLM with a
repo id) find it as is:

    <cache>/hub/models--<org>--<name>/blobs/<sha256 or git blob id>
    <cache>/hub/models--<org>--<name>/snapshots/<commit>/<path>  -> ../../blobs/<id>
    <cache>/hub/models--<org>--<name>/refs/<ref>                 (the commit)
    <cache>/url/<sha256(url)[:24]>/<file name>
    <cache>/engines/<name>-<sha256[:16]>/...                     (process engines)

The agent recomputes every path from the repository and revision itself; it never uses a path
string from the server for the file system.
"""

from __future__ import annotations

import fnmatch
import hashlib
import posixpath
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

HF_PART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
HF_REPO_RE = re.compile(rf"^{HF_PART}/{HF_PART}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SPLIT_GGUF_RE = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$")
MAX_FILES = 4000
MAX_FILE_PATH = 512

# Where things are mounted inside a container (read-only).
CONTAINER_ROOT = "/models"

# Weight formats a safetensors deployment does not need (they would double the download).
_OTHER_WEIGHTS = ("*.bin", "*.pt", "*.pth", "*.h5", "*.msgpack", "*.ot", "*.onnx", "*.onnx_data", "*.tflite",
                  "*.mlmodel", "*.gguf", "*.ckpt", "*.npz", "*.pkl")
_OTHER_DIRS = ("onnx/", "openvino/", "coreml/", "original/", "tf_model/", "flax_model/")


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    kind: str                     # hf | url | local
    repo: Optional[str] = None    # hf
    url: Optional[str] = None     # url
    path: Optional[str] = None    # local

    def __str__(self) -> str:
        if self.kind == "hf":
            return f"hf://{self.repo}"
        if self.kind == "url":
            return str(self.url)
        return f"local://{self.path}"


def parse_source(text: str) -> Source:
    text = (text or "").strip()
    if text.startswith("hf://"):
        repo = text[5:].strip("/")
        if not HF_REPO_RE.match(repo) or ".." in repo:
            raise SourceError("hf:// needs <org>/<name>, e.g. hf://Qwen/Qwen3-4B")
        return Source("hf", repo=repo)
    if text.startswith("local://"):
        path = posixpath.normpath(text[8:]) if text[8:] else ""
        if not path.startswith("/") or "\x00" in path or path == "/":
            raise SourceError("local:// needs an absolute directory, e.g. local:///srv/models/my-model")
        return Source("local", path=path)
    if text.startswith(("https://", "http://")):
        parts = urlsplit(text)
        if not parts.hostname:
            raise SourceError("the URL has no host")
        if not parts.path.lower().endswith(".gguf"):
            raise SourceError("a URL source must be a single .gguf file (use hf:// for repositories)")
        if parts.username or parts.password:
            raise SourceError("credentials in the URL are not allowed")
        return Source("url", url=text)
    if text.startswith("modelscope://"):
        raise SourceError("modelscope:// is not supported yet; use hf://")
    raise SourceError("a model source is hf://<org>/<name>, an https:// URL of a .gguf file, or local://<dir>")


def local_allowed(path: str, dirs: list[str]) -> bool:
    """A local:// path must be one of the admin-configured directories or below one."""
    path = posixpath.normpath(path)
    for d in dirs:
        d = posixpath.normpath(d.strip())
        if d.startswith("/") and d != "/" and (path == d or path.startswith(d + "/")):
            return True
    return False


def safe_relpath(path: str) -> str:
    """A path inside a repository: relative, normalised, no '..', no control characters."""
    if not path or len(path) > MAX_FILE_PATH or path.startswith("/") or "\\" in path:
        raise SourceError(f"bad file path {path!r}")
    if any(ord(c) < 32 for c in path):
        raise SourceError(f"bad file path {path!r}")
    norm = posixpath.normpath(path)
    if norm != path or norm.startswith("../") or norm == ".." or "/../" in norm or norm.startswith("./"):
        raise SourceError(f"bad file path {path!r}")
    return norm


# ------------------------------------------------------------------ the cache layout
def hf_dir(repo: str) -> str:
    if not HF_REPO_RE.match(repo) or ".." in repo:
        raise SourceError(f"bad repository {repo!r}")
    org, name = repo.split("/", 1)
    return f"hub/models--{org}--{name}"


def url_dir(url: str) -> str:
    return "url/" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def url_filename(url: str) -> str:
    name = posixpath.basename(urlsplit(url).path)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,200}\.gguf", name, re.IGNORECASE):
        raise SourceError("the URL must end in a plain .gguf file name")
    return name


def cache_key(model: dict) -> str:
    """One string per cached thing (source + revision): what agents report as cached."""
    if model["kind"] == "hf":
        base = f"{hf_dir(model['repo'])}@{model['revision']}"
        # One file of a repository with several GGUF files is cached on its own.
        return f"{base}/{model['file']}" if model.get("file") else base
    if model["kind"] == "url":
        return url_dir(model["url"])
    return f"local:{model['path']}"


def layout(model: dict) -> dict:
    """Paths relative to the cache root (hf, url) or absolute (local):
    {"mount": dir to mount, "path": the model directory, "file": the model file or None}."""
    kind = model["kind"]
    if kind == "hf":
        rev = model["revision"]
        if not REVISION_RE.match(rev or ""):
            raise SourceError("a Hugging Face model must be pinned to a commit")
        base = hf_dir(model["repo"])
        path = f"{base}/snapshots/{rev}"
        f = model.get("file")
        return {"mount": base, "path": path, "file": f"{path}/{safe_relpath(f)}" if f else None, "hub": "hub"}
    if kind == "url":
        base = url_dir(model["url"])
        return {"mount": base, "path": base, "file": f"{base}/{url_filename(model['url'])}", "hub": "hub"}
    if kind == "local":
        p = posixpath.normpath(model["path"])
        f = model.get("file")
        return {"mount": p, "path": p, "file": f"{p}/{safe_relpath(f)}" if f else None, "hub": None}
    raise SourceError(f"unknown model kind {kind!r}")


def container_paths(model: dict) -> dict:
    """The same paths as seen inside a container, where the mount is at /models/<mount> (or
    /models/local for a local directory)."""
    lay = layout(model)
    if model["kind"] == "local":
        base = f"{CONTAINER_ROOT}/local"
        rel = lambda p: base + p[len(lay["mount"]):]  # noqa: E731
        return {"mount": base, "path": base, "file": rel(lay["file"]) if lay["file"] else None,
                "hub": f"{CONTAINER_ROOT}/hub"}
    return {"mount": f"{CONTAINER_ROOT}/{lay['mount']}", "path": f"{CONTAINER_ROOT}/{lay['path']}",
            "file": f"{CONTAINER_ROOT}/{lay['file']}" if lay["file"] else None, "hub": f"{CONTAINER_ROOT}/hub"}


# ------------------------------------------------------------------ which files to fetch
def _matches(path: str, patterns) -> bool:
    name = posixpath.basename(path)
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def detect_format(paths: list[str], library: Optional[str] = None) -> str:
    if any(p.lower().endswith(".gguf") for p in paths):
        return "gguf"
    if any(p.endswith(".safetensors") for p in paths):
        return "safetensors"
    if (library or "").lower() == "ctranslate2" or ("model.bin" in paths and any(p.startswith("vocabulary") for p in paths)):
        return "ctranslate2"
    return "other"


def is_mmproj(path: str) -> bool:
    """A llama.cpp multimodal projector (mmproj-*.gguf), not a model of its own."""
    return posixpath.basename(path).lower().startswith("mmproj") and path.lower().endswith(".gguf")


def mmproj_choices(files: list[dict]) -> list[dict]:
    return sorted(({"path": f["path"], "size": int(f.get("size") or 0)} for f in files if is_mmproj(f["path"])),
                  key=lambda e: e["path"])


def default_mmproj(choices: list[dict]) -> Optional[str]:
    """F16 when there is one (llama.cpp's own default precision for projectors), else BF16, Q8_0,
    or the first."""
    for want in ("f16", "bf16", "q8_0"):
        for c in choices:
            if re.search(rf"[-_.]{want}\.gguf$", c["path"].lower()):
                return c["path"]
    return choices[0]["path"] if choices else None


def gguf_choices(files: list[dict]) -> list[dict]:
    """GGUF files a deployment can pick from; split models appear once (their first part, total size).
    Projector files (mmproj) are not models and are chosen separately."""
    out: dict[str, dict] = {}
    for f in files:
        path = f["path"]
        if not path.lower().endswith(".gguf") or is_mmproj(path):
            continue
        m = SPLIT_GGUF_RE.match(path)
        if m:
            first = f"{m.group(1)}-00001-of-{m.group(3)}.gguf"
            entry = out.setdefault(first, {"path": first, "size": 0, "parts": 0})
            entry["size"] += int(f.get("size") or 0)
            entry["parts"] += 1
        else:
            out[path] = {"path": path, "size": int(f.get("size") or 0), "parts": 1}
    return sorted(out.values(), key=lambda e: e["path"])


def default_gguf(choices: list[dict]) -> Optional[str]:
    """Q4_K_M when there is one (the usual default), else the only file."""
    for c in choices:
        if "q4_k_m" in c["path"].lower():
            return c["path"]
    return choices[0]["path"] if len(choices) == 1 else None


def select_files(files: list[dict], fmt: str, chosen: Optional[str] = None, mmproj: Optional[str] = None) -> list[dict]:
    """The files a deployment downloads, from the repository's file list (and, for a GGUF vision
    model, its projector file)."""
    out = []
    for f in files:
        path = f["path"]
        if path in (".gitattributes",) or path.startswith(".") or "/." in path:
            continue
        if fmt == "gguf":
            if mmproj and path == mmproj:
                out.append(f)
                continue
            if not path.lower().endswith(".gguf") or is_mmproj(path):
                continue
            if chosen is None:
                raise SourceError("choose which GGUF file to use")
            m, c = SPLIT_GGUF_RE.match(path), SPLIT_GGUF_RE.match(chosen)
            if path != chosen and not (m and c and m.group(1) == c.group(1) and m.group(3) == c.group(3)):
                continue
        elif fmt == "safetensors":
            if _matches(path, _OTHER_WEIGHTS) or any(path.startswith(d) for d in _OTHER_DIRS):
                continue
        out.append(f)
    if fmt == "gguf" and not out:
        raise SourceError(f"{chosen} is not a GGUF file of this repository")
    if len(out) > MAX_FILES:
        raise SourceError(f"the repository has more than {MAX_FILES} files to download")
    return out


TASK_FROM_PIPELINE = {
    "text-generation": "chat", "text2text-generation": "chat", "image-text-to-text": "chat",
    "feature-extraction": "embed", "sentence-similarity": "embed",
    "text-ranking": "rerank", "text-classification": "rerank",
    "automatic-speech-recognition": "asr",
}


def task_hint(pipeline_tag: Optional[str], tags: list[str]) -> Optional[str]:
    if pipeline_tag in TASK_FROM_PIPELINE:
        return TASK_FROM_PIPELINE[pipeline_tag]
    for t in tags or []:
        if t in TASK_FROM_PIPELINE:
            return TASK_FROM_PIPELINE[t]
    return None
