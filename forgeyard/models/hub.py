"""Searching Hugging Face for models, on the server (the browser never talks to Hugging Face).

One request to `<hf_endpoint>/api/models?search=…` per Hugging Face task tag (sorted by downloads),
over the guarded fetcher (public addresses only, every redirect checked), merged and trimmed. Only
the configured Hugging Face endpoint is ever asked; nothing in the request chooses the host.

Answers are cached for a few minutes, so typing the same thing again, or several people looking at
the same list, asks Hugging Face once. Failures are not cached: the next search tries again.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlencode

from forgeyard.models import fetch, sources

CACHE_SECONDS = 300.0
CACHE_ENTRIES = 256
TIMEOUT = 10.0
MAX_QUERY = 100
MAX_LIMIT = 30
MAX_ANSWER = 8 * 1024**2

# Our task -> the Hugging Face pipeline tags searched for it (some embedders are tagged one way,
# some the other; many rerankers are tagged text classification).
TASK_TAGS: dict[str, tuple[str, ...]] = {
    "chat": ("text-generation",),
    "embed": ("feature-extraction", "sentence-similarity"),
    "rerank": ("text-ranking", "text-classification"),
    "asr": ("automatic-speech-recognition",),
    "tts": ("text-to-speech",),
    "image": ("text-to-image",),
    "video": ("text-to-video",),
}
FORMATS = ("safetensors", "gguf", "ctranslate2", "onnx", "diffusers")
EXPAND = ("downloads", "likes", "lastModified", "gated", "private", "pipeline_tag", "library_name", "tags",
          "safetensors", "gguf")
_DTYPE_BYTES = {"F64": 8, "I64": 8, "F32": 4, "I32": 4, "BF16": 2, "F16": 2, "I16": 2, "F8_E4M3": 1, "F8_E5M2": 1,
                "I8": 1, "U8": 1, "BOOL": 1}


class HubError(Exception):
    """Hugging Face could not be asked (no internet, an error answer). The message is safe to show."""


@dataclass(frozen=True)
class Query:
    text: str
    task: Optional[str]
    format: Optional[str]
    limit: int


def make_query(text: str, task: Optional[str], fmt: Optional[str], limit: int) -> Query:
    text = " ".join((text or "").split())
    if len(text) > MAX_QUERY:
        raise ValueError(f"search: at most {MAX_QUERY} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError("search: plain text only")
    if task not in (None, "", *TASK_TAGS):
        raise ValueError("task: chat, embed, rerank, asr, tts, image or video")
    if fmt not in (None, "", *FORMATS):
        raise ValueError("format: safetensors, gguf, ctranslate2, onnx or diffusers")
    if not 1 <= int(limit) <= MAX_LIMIT:
        raise ValueError(f"limit: 1 to {MAX_LIMIT}")
    return Query(text, task or None, fmt or None, int(limit))


def search_urls(endpoint: str, q: Query) -> list[str]:
    tags: tuple[Optional[str], ...] = TASK_TAGS[q.task] if q.task else (None,)
    urls = []
    for tag in tags:
        params: list[tuple[str, str]] = [("sort", "downloads"), ("direction", "-1"), ("limit", str(q.limit))]
        if q.text:
            params.append(("search", q.text))
        if tag:
            params.append(("pipeline_tag", tag))
        if q.format:
            params.append(("filter", q.format))
        params += [("expand[]", e) for e in EXPAND]
        urls.append(f"{endpoint.rstrip('/')}/api/models?{urlencode(params)}")
    return urls


def _format(m: dict) -> str:
    tags = set(m.get("tags") or [])
    if "gguf" in tags or m.get("gguf"):
        return "gguf"
    if (m.get("library_name") or "").lower() == "diffusers" or "diffusers" in tags:
        return "diffusers"
    if "safetensors" in tags or m.get("safetensors"):
        return "safetensors"
    if (m.get("library_name") or "").lower() == "ctranslate2" or "ctranslate2" in tags:
        return "ctranslate2"
    if (m.get("library_name") or "").lower() == "onnx" or "onnx" in tags:
        return "onnx"
    return "other"


def _weights_bytes(m: dict) -> Optional[int]:
    """The safetensors weights' size from the parameter counts per dtype (no download)."""
    per = (m.get("safetensors") or {}).get("parameters")
    if not isinstance(per, dict) or not per:
        return None
    total = 0
    for dtype, count in per.items():
        if not isinstance(count, (int, float)):
            return None
        total += int(count) * _DTYPE_BYTES.get(str(dtype).upper(), 2)
    return total


def summarise(m: dict) -> Optional[dict]:
    rid = m.get("id") or m.get("modelId")
    if not isinstance(rid, str) or not sources.HF_REPO_RE.match(rid) or ".." in rid:
        return None
    gated = m.get("gated")
    params = (m.get("safetensors") or {}).get("total") or (m.get("gguf") or {}).get("total")
    fmt = _format(m)
    return {
        "id": rid, "source": f"hf://{rid}",
        "downloads": int(m.get("downloads") or 0), "likes": int(m.get("likes") or 0),
        "last_modified": m.get("lastModified") if isinstance(m.get("lastModified"), str) else None,
        "gated": bool(gated), "gated_mode": gated if isinstance(gated, str) else None,
        "private": bool(m.get("private")),
        "pipeline_tag": m.get("pipeline_tag") if isinstance(m.get("pipeline_tag"), str) else None,
        "library": m.get("library_name") if isinstance(m.get("library_name"), str) else None,
        "task": sources.task_hint(m.get("pipeline_tag"), m.get("tags") or []) or ("chat" if fmt == "gguf" else None),
        "format": fmt,
        "params": int(params) if isinstance(params, (int, float)) and params > 0 else None,
        "weights_bytes": _weights_bytes(m) if fmt == "safetensors" else None,
    }


def rank(q: Query, s: dict) -> tuple:
    """Most downloaded first. A rerank search also asks for text classification (where many
    rerankers are filed), which brings in sentiment and topic classifiers: models with "rerank" in
    their name come first."""
    reranker = q.task == "rerank" and "rerank" in s["id"].lower()
    return (not reranker, -s["downloads"])


class Hub:
    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 getter: Callable[[str, fetch.Policy], bytes] | None = None):
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._get = getter or (lambda url, policy: fetch.get_bytes(url, policy, limit=MAX_ANSWER,
                                                                    headers={"Accept": "application/json"}))
        self.upstream_calls = 0

    def reset(self) -> None:
        with self._lock:
            self._cache.clear()
            self.upstream_calls = 0

    def search(self, endpoint: str, q: Query, policy: fetch.Policy) -> tuple[list[dict], bool]:
        """(results, from the cache?) Raises HubError when Hugging Face cannot be asked."""
        key = (endpoint.rstrip("/"), q)
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1], True
        merged: dict[str, dict] = {}
        for url in search_urls(endpoint, q):
            with self._lock:
                self.upstream_calls += 1
            try:
                raw = self._get(url, policy)
            except fetch.FetchError as e:
                raise HubError(str(e)) from None
            try:
                data = json.loads(raw)
            except ValueError:
                raise HubError("Hugging Face answered with something that is not JSON") from None
            if not isinstance(data, list):
                raise HubError("Hugging Face answered with something unexpected")
            for m in data:
                s = summarise(m) if isinstance(m, dict) else None
                if s and s["id"] not in merged:
                    merged[s["id"]] = s
        results = sorted(merged.values(), key=lambda s: rank(q, s))[: q.limit]
        with self._lock:
            if len(self._cache) >= CACHE_ENTRIES:
                for k in [k for k, (exp, _) in self._cache.items() if exp <= now] or list(self._cache)[:CACHE_ENTRIES // 4]:
                    self._cache.pop(k, None)
            self._cache[key] = (now + CACHE_SECONDS, results)
        return results, False


hub = Hub()


def policy(allow_private: bool) -> fetch.Policy:
    return fetch.Policy(allow_private=allow_private, timeout=TIMEOUT)
