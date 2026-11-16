"""A small engine image built in memory (an index with linux/amd64 and linux/arm64, gzip layers, the lock entry
scripts/pin_images.py would record for it) and a fake registry that serves it the way GHCR and Docker Hub do:
an anonymous Bearer token from /token, manifests by digest, and blobs through a redirect to a "CDN" that
takes Range and refuses the registry's token (as signed CDN URLs do). For the image relay tests (#35 PR 4)."""

from __future__ import annotations

import errno
import gzip
import hashlib
import io
import json
import socket
import tarfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from forgeyard.workloads import images


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def layer(files: dict[str, bytes]) -> tuple[bytes, bytes]:
    """(the uncompressed tar, its gzip): a layer as a registry serves it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o755, 0
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return raw, gzip.compress(raw, mtime=0)


@dataclass
class Platform:
    arch: str
    manifest: bytes
    manifest_digest: str
    config: bytes
    config_digest: str
    layers: list[str]                 # digests, in order
    content: str


@dataclass
class Image:
    name: str                         # ghcr.io/acme/engine:1.0
    index: bytes
    index_digest: str
    platforms: dict[str, Platform]
    blobs: dict[str, bytes] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """As a recipe pins it: repo:tag@sha256:<index>."""
        return f"{self.name}@{self.index_digest}"

    @property
    def repo(self) -> str:
        return self.name.split("/", 1)[1].rsplit(":", 1)[0]

    def lock_entry(self) -> dict:
        return {"index": self.index_digest,
                "platforms": {f"linux/{a}": {"manifest": p.manifest_digest, "config": p.config_digest,
                                             "content": p.content} for a, p in sorted(self.platforms.items())}}

    def check(self) -> dict:
        return images.entry_check(self.lock_entry())

    def total(self, arch: str = "amd64") -> int:
        p = self.platforms[arch]
        return len(p.config) + sum(len(self.blobs[d]) for d in dict.fromkeys(p.layers))


def build_image(name: str = "ghcr.io/acme/engine:1.0", arches=("amd64", "arm64"), big: int = 300_000) -> Image:
    """Three layers per platform (one large, so a transfer can be cut in its middle), an OCI index over them."""
    import os

    blobs: dict[str, bytes] = {}
    platforms: dict[str, Platform] = {}
    for arch in arches:
        layers_raw = [layer({"bin/engine": f"#!/bin/sh\necho {arch}\n".encode()}),
                      layer({"data/weights.bin": os.urandom(big)}),
                      layer({"etc/engine.conf": b"threads=4\n"})]
        diff_ids = [digest(raw) for raw, _ in layers_raw]
        ldigests = []
        for _, gz in layers_raw:
            blobs[digest(gz)] = gz
            ldigests.append(digest(gz))
        config = {"architecture": arch, "os": "linux",
                  "config": {"Entrypoint": ["/bin/engine"], "Env": ["PATH=/bin"], "WorkingDir": "/"},
                  "rootfs": {"type": "layers", "diff_ids": diff_ids}}
        cbytes = json.dumps(config, sort_keys=True).encode()
        blobs[digest(cbytes)] = cbytes
        manifest = {"schemaVersion": 2, "mediaType": OCI_MANIFEST,
                    "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": digest(cbytes),
                               "size": len(cbytes)},
                    "layers": [{"mediaType": OCI_LAYER, "digest": d, "size": len(blobs[d])} for d in ldigests]}
        mbytes = json.dumps(manifest).encode()
        platforms[arch] = Platform(arch, mbytes, digest(mbytes), cbytes, digest(cbytes), ldigests,
                                   images.content_of_config(config))
    index = {"schemaVersion": 2, "mediaType": OCI_INDEX,
             "manifests": [{"mediaType": OCI_MANIFEST, "digest": p.manifest_digest, "size": len(p.manifest),
                            "platform": {"os": "linux", "architecture": a}} for a, p in platforms.items()]}
    ibytes = json.dumps(index).encode()
    return Image(name, ibytes, digest(ibytes), platforms, blobs)


class FakeRegistry:
    """http://127.0.0.1:<port>: /v2/ (401 with a Bearer challenge), /token, /v2/<repo>/manifests/<digest> and
    /v2/<repo>/blobs/<digest> (a 307 to /cdn/<digest>, which takes Range). Remembers every request."""

    def __init__(self, *imgs: Image):
        self.images = {i.repo: i for i in imgs}
        self.requests: list[dict] = []
        self.tokens = 0
        self.corrupt: set[str] = set()         # digests served with one byte changed
        self.bad_manifest = False              # manifests served with other bytes
        self.expire_once = False               # the next authenticated request gets 401
        reg = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", headers=None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                reg.requests.append({"path": path, "auth": self.headers.get("Authorization"),
                                     "range": self.headers.get("Range"), "query": self.path.partition("?")[2]})
                if path == "/token":
                    reg.tokens += 1
                    return self._send(200, json.dumps({"token": f"tok-{reg.tokens}", "expires_in": 300}).encode(),
                                      {"Content-Type": "application/json"})
                if path.startswith("/cdn/"):
                    if self.headers.get("Authorization"):
                        return self._send(400, b"only one auth mechanism allowed")
                    return self._blob(path.removeprefix("/cdn/"))
                authed = (self.headers.get("Authorization") or "").startswith("Bearer tok-")
                if reg.expire_once and authed:
                    reg.expire_once = False
                    authed = False
                if not authed:
                    host = self.headers.get("Host")
                    return self._send(401, b'{"errors":[{"code":"UNAUTHORIZED"}]}', {
                        "WWW-Authenticate": f'Bearer realm="http://{host}/token",service="fake-registry"'})
                if path == "/v2/":
                    return self._send(200, b"{}")
                parts = path.removeprefix("/v2/").split("/")
                repo, kind, ref = "/".join(parts[:-2]), parts[-2], parts[-1]
                img = reg.images.get(repo)
                if img is None:
                    return self._send(404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}')
                if kind == "manifests":
                    if ref == img.index_digest:
                        return self._send(200, img.index, {"Content-Type": OCI_INDEX})
                    for p in img.platforms.values():
                        if ref == p.manifest_digest:
                            body = p.manifest + (b" " if reg.bad_manifest else b"")
                            return self._send(200, body, {"Content-Type": OCI_MANIFEST})
                    return self._send(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
                if kind == "blobs" and ref in img.blobs:
                    # Another host, as a real CDN is (so the client drops the registry's token).
                    return self._send(307, b"", {"Location": f"http://localhost:{reg.port}/cdn/{ref}"})
                return self._send(404, b'{"errors":[{"code":"BLOB_UNKNOWN"}]}')

            def _blob(self, ref):
                data = next((i.blobs[ref] for i in reg.images.values() if ref in i.blobs), None)
                if data is None:
                    return self._send(404)
                if ref in reg.corrupt:
                    data = data[:-1] + bytes([data[-1] ^ 0xFF])
                rng = self.headers.get("Range")
                if rng:
                    start = int(rng.removeprefix("bytes=").split("-")[0])
                    return self._send(206, data[start:], {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
                return self._send(200, data)

        # The CDN redirect names `localhost`, which resolves to ::1 first on some hosts (GitHub's runners) and
        # to 127.0.0.1 only on others: listen on both loopbacks, on one port, so either answers.
        self.servers = []
        for _ in range(20):
            v4 = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            port = v4.server_address[1]
            try:
                v6 = _V6Server(("::1", port), Handler)
            except OSError as e:
                if e.errno == errno.EADDRINUSE:
                    v4.server_close()
                    continue
                self.servers = [v4]       # no IPv6 loopback here: localhost cannot mean ::1 either
                break
            self.servers = [v4, v6]
            break
        if not self.servers:
            raise RuntimeError("no port free on both 127.0.0.1 and ::1")
        self.server = self.servers[0]
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        self.url = f"http://{self.host}"
        for srv in self.servers:
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    def blob_requests(self, d: Optional[str] = None) -> list[dict]:
        return [r for r in self.requests if r["path"].startswith("/cdn/") and (d is None or r["path"].endswith(d))]

    def close(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()


def docker_save(img: Image, arch: str = "amd64", tags=None, compress: bool = False) -> bytes:
    """What `docker save` (classic) writes for one platform: manifest.json + config + uncompressed layer.tar
    files; gzip-compressed when `compress`. For the upload tests."""
    p = img.platforms[arch]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        def add(name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        names = []
        for i, d in enumerate(p.layers):
            raw = gzip.decompress(img.blobs[d])
            names.append(f"l{i}/layer.tar")
            add(names[-1], raw)
        add(f"{p.config_digest[7:]}.json", p.config)
        add("manifest.json", json.dumps([{"Config": f"{p.config_digest[7:]}.json",
                                          "RepoTags": list(tags if tags is not None else [img.name]),
                                          "Layers": names}]).encode())
    data = buf.getvalue()
    return gzip.compress(data, mtime=0) if compress else data
