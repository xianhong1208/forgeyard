"""Shared pieces for the model tests: a fake Hugging Face (API, file downloads with Range and a
CDN redirect, gated repositories), built from real answers and headers saved in
tests/fixtures/models, and catalog entries straight in the database."""

from __future__ import annotations

import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from forgeyard.db import CatalogModel, new_session

FIX = Path(__file__).resolve().parent / "fixtures" / "models"
QWEN4B_REV = "1cfa9a7208912126459214e8b04321603b3df60c"
RERANK_REV = "e61197ed45024b0ed8a2d74b80b4d909f1255473"
WHISPER_REV = "536b0662742c02347bc0e980a01041f333bce120"
GGUF_REV = "daeb8e2d528a760970442092f6bf1e55c3b659eb"
TINY_REV = "0123456789abcdef0123456789abcdef01234567"
GIB = 1024**3


def fixture_json(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def git_oid(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def safetensors_bytes(header: dict, data: bytes = b"") -> bytes:
    raw = json.dumps(header).encode()
    return struct.pack("<Q", len(raw)) + raw + data


class Repo:
    """A repository: {path: bytes} (served), or a real revision answer with partial content."""

    def __init__(self, repo_id: str, sha: str, files: dict[str, bytes], *, info: Optional[dict] = None,
                 gated: bool = False, lfs: tuple = (".safetensors", ".gguf", ".bin")):
        self.id, self.sha, self.files, self.gated = repo_id, sha, files, gated
        if info is None:
            siblings = []
            for path, data in files.items():
                s = {"rfilename": path, "size": len(data), "blobId": git_oid(data)}
                if path.endswith(lfs):
                    s["lfs"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                siblings.append(s)
            info = {"id": repo_id, "sha": sha, "pipeline_tag": "text-generation", "library_name": "transformers",
                    "tags": [], "gated": gated, "private": False, "siblings": siblings}
        self.info = info


def tiny_repo(repo_id: str = "acme/tiny-chat", *, gated: bool = False) -> Repo:
    """A small, complete safetensors repository (a few KB) for real download tests."""
    config = json.loads((FIX / "qwen3-embedding-0.6b.config.json").read_text())
    header = {"model.embed_tokens.weight": {"dtype": "BF16", "shape": [16, 8], "data_offsets": [0, 256]},
              "model.layers.0.mlp.up_proj.weight": {"dtype": "BF16", "shape": [8, 8], "data_offsets": [256, 384]}}
    files = {"config.json": json.dumps(config).encode(), "README.md": b"---\nlibrary_name: transformers\n---\n",
             "model.safetensors": safetensors_bytes(header, bytes(range(256)) * 1 + bytes(128)),
             "tokenizer.json": b'{"version": "1.0"}', "pytorch_model.bin": b"x" * 64,
             "sub/dir/extra.txt": b"nested"}
    return Repo(repo_id, TINY_REV, files, gated=gated)


def qwen4b_repo() -> Repo:
    """Qwen/Qwen3-4B as Hugging Face describes it, with its real config and safetensors headers
    (the shards hold only their header: enough for inspection, never downloaded)."""
    info = fixture_json("qwen3-4b.hf-revision.json")
    heads = fixture_json("qwen3-4b.safetensors-headers.json")
    files = {"config.json": (FIX / "qwen3-4b.config.json").read_bytes()}
    for name, h in heads.items():
        files[name] = safetensors_bytes(h)
    return Repo(info["id"], info["sha"], files, info=info)


def gguf_repo() -> Repo:
    info = fixture_json("qwen3-1.7b-gguf.hf-revision.json")
    files = {"Qwen3-1.7B-Q4_K_M.gguf": (FIX / "qwen3-1.7b-q4_k_m.gguf-header").read_bytes()}
    return Repo(info["id"], info["sha"], files, info=info)


class FakeHF:
    """http://127.0.0.1:<port>: /api/models/<repo>/revision/<ref> and /<repo>/resolve/<rev>/<path>
    (with Range, through a /cdn/ redirect like the real one). Counts requests and remembers the
    Authorization header each one carried."""

    TOKEN = "hf_test_token_value"

    def __init__(self, *repos: Repo, redirect: bool = True, rate: Optional[int] = None):
        self.repos = {r.id: r for r in repos}
        self.redirect = redirect
        self.rate = rate  # bytes per response, to make downloads slow (cancel tests)
        self.requests: list[dict] = []
        self.fail_after: Optional[int] = None  # cut a body after this many bytes once
        hf = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", headers=None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):
                hf.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                    "range": self.headers.get("Range")})
                parts = self.path.split("?")[0].strip("/").split("/")
                if parts[:2] == ["api", "models"] and "revision" in parts:
                    i = parts.index("revision")
                    repo = hf.repos.get("/".join(parts[2:i]))
                    if repo is None:
                        return self._send(404, b'{"error": "Repository not found"}')
                    return self._send(200, json.dumps(repo.info).encode(), {"Content-Type": "application/json"})
                cdn = parts[0] == "cdn"
                if cdn:
                    parts = parts[1:]
                if "resolve" not in parts:
                    return self._send(404)
                i = parts.index("resolve")
                repo = hf.repos.get("/".join(parts[:i]))
                if repo is None:
                    return self._send(404)
                if repo.gated and not cdn and self.headers.get("Authorization") != f"Bearer {hf.TOKEN}":
                    return self._send(401, b"gated")
                from urllib.parse import unquote
                path = unquote("/".join(parts[i + 2:]))
                data = repo.files.get(path)
                if data is None:
                    return self._send(404)
                if hf.redirect and not cdn:
                    return self._send(302, b"", {"Location": "/cdn" + self.path})
                rng = self.headers.get("Range")
                if rng:
                    a, _, b = rng.removeprefix("bytes=").partition("-")
                    start = int(a)
                    end = int(b) if b else len(data) - 1
                    if start >= len(data):
                        return self._send(416, b"", {"Content-Range": f"bytes */{len(data)}"})
                    body = data[start:end + 1]
                    headers = {"Content-Range": f"bytes {start}-{start + len(body) - 1}/{len(data)}"}
                    status = 206
                else:
                    body, headers, status = data, {}, 200
                if hf.fail_after is not None and len(body) > hf.fail_after:
                    cut, hf.fail_after = hf.fail_after, None
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body[:cut])
                    self.close_connection = True
                    return
                if hf.rate:
                    import time
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    for off in range(0, len(body), hf.rate):
                        try:
                            self.wfile.write(body[off:off + hf.rate])
                            self.wfile.flush()
                        except OSError:
                            return
                        time.sleep(0.05)
                    return
                self._send(status, body, headers)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def downloads(self, path_part: str) -> list[dict]:
        return [r for r in self.requests if "/cdn/" in r["path"] and path_part in r["path"]]


def add_catalog(name: str, meta: dict, *, task: str, fmt: str, source: str, kind: str = "hf",
                engine_args: Optional[dict] = None) -> int:
    db = new_session()
    try:
        m = CatalogModel(name=name, source=source, kind=kind, ref=meta.get("ref"), revision=meta.get("revision"),
                         file=meta.get("file"), format=fmt, task=task, meta=meta, engine_args=engine_args or {},
                         size_bytes=int(meta.get("size_bytes") or 0))
        db.add(m)
        db.commit()
        return m.id
    finally:
        db.close()
