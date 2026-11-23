"""A fake upstream for the engine-update check (1.5.2, #95): one HTTP server that answers as an image registry (an
anonymous Bearer token, tag listings page by page with `Link: rel="next"` as GHCR does, manifests by tag (HEAD with
Docker-Content-Digest) and by digest, config blobs) and as GitHub's REST API (release lists and releases by tag).
Its data are the real tag lists and releases of 2026-11-23 (tests/fixtures/engine_updates), so the golden answer is
what a check found that day."""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "engine_updates"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"

# What sd.cpp's rolling tags pointed at on 2026-11-23, and the pins of the built-in recipe (with their labels).
SD_REPO = "leejet/stable-diffusion.cpp"
SD_NOW = {"master-cuda": ("0d71f963af9c94941f4a67e8ea145935651dd756", "2026-11-22T18:45:07.091Z"),
          "master-vulkan": ("0d71f963af9c94941f4a67e8ea145935651dd756", "2026-11-22T18:40:17.152Z")}
SD_PINNED = {"sha256:34f70088fe3902d985d5fffa9a947d94ab67c62b8afbcc7bd0448bea3c9dd83b":
             ("3bad892000000000000000000000000000000000", "2026-11-13T18:16:52.473Z"),
             "sha256:7911e3788c6c7570e1ec4d89c535cfe6f2e990a21474449e71e8ac291b8b6be0":
             ("32d516c0197ed5ca31305b8575e14eb1451fad33", "2026-11-10T17:25:06.301Z")}


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def tag_digest(repo: str, tag: str) -> str:
    """A stable made-up digest for a tag that has no image built here."""
    return sha(f"{repo}:{tag}".encode())


def real_tags() -> dict[str, list[str]]:
    tags = json.loads((FIXTURES / "tags.json").read_text())
    with gzip.open(FIXTURES / "llama-cpp-tags.json.gz", "rt") as f:
        tags["ggml-org/llama.cpp"] = json.load(f)
    return tags


def real_releases() -> dict[str, list[dict]]:
    raw = json.loads((FIXTURES / "releases.json").read_text())
    out: dict[str, list[dict]] = {}
    for key, value in raw.items():
        repo, _, tag = key.partition("@")
        out.setdefault(repo, [])
        out[repo].extend(value if isinstance(value, list) else [value])
    return out


class Image:
    """An index with one linux/amd64 manifest whose config carries OCI labels; registered by its digest."""

    def __init__(self, revision: str, created: str):
        config = json.dumps({"architecture": "amd64", "os": "linux", "created": created, "config": {"Labels": {
            "org.opencontainers.image.revision": revision, "org.opencontainers.image.created": created}}}).encode()
        manifest = json.dumps({"schemaVersion": 2, "mediaType": OCI_MANIFEST,
                               "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                          "digest": sha(config), "size": len(config)},
                               "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                           "digest": sha(b"layer" + revision.encode()), "size": 5}]}).encode()
        index = json.dumps({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [
            {"mediaType": OCI_MANIFEST, "digest": sha(manifest), "size": len(manifest),
             "platform": {"os": "linux", "architecture": "amd64"}},
            {"mediaType": OCI_MANIFEST, "digest": sha(b"attestation" + revision.encode()), "size": 10,
             "platform": {"os": "unknown", "architecture": "unknown"},
             "annotations": {"vnd.docker.reference.type": "attestation-manifest"}}]}).encode()
        self.config, self.manifest, self.index = config, manifest, index
        self.digest = sha(index)


class FakeUpstream:
    """http://127.0.0.1:<port>, serving every repository of `tags` and GitHub's releases of `releases`."""

    def __init__(self, tags: Optional[dict] = None, releases: Optional[dict] = None, page_cap: int = 1000):
        self.tags = tags if tags is not None else real_tags()
        self.releases = releases if releases is not None else real_releases()
        self.page_cap = page_cap
        self.digests: dict[tuple[str, str], str] = {}     # (repo, tag) -> digest, else tag_digest()
        self.blobs: dict[str, bytes] = {}                 # digest -> manifest / index / config bytes
        self.requests: list[dict] = []
        self.tokens = 0
        self.fail: dict[str, int] = {}                    # path prefix -> HTTP status
        self.delay = 0.0
        self.no_digest_header = False
        self.next_link: Optional[str] = None              # replaces the Link of every tag page (a test)
        self.lock = threading.Lock()
        for tag, (rev, created) in SD_NOW.items():
            self.add_image(SD_REPO, tag, Image(rev, created))
        # The recipe's pins, as images of their own (their real bytes are not here): recipes() re-pins sd-cpp to them.
        self.sd_pins = {}
        for digest, (rev, created) in SD_PINNED.items():
            img = Image(rev, created)
            self.add_blobs(img)
            self.sd_pins[digest] = img.digest
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
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                parts = urlsplit(self.path)
                path, query = unquote(parts.path), parse_qs(parts.query)
                with reg.lock:
                    reg.requests.append({"method": self.command, "path": path, "query": parts.query,
                                         "auth": self.headers.get("Authorization")})
                if reg.delay:
                    time.sleep(reg.delay)
                for prefix, status in reg.fail.items():
                    if path.startswith(prefix):
                        return self._send(status, b'{"message":"failing on purpose"}')
                if path.startswith("/repos/"):
                    return self._github(path, query)
                if path == "/token":
                    reg.tokens += 1
                    return self._send(200, json.dumps({"token": f"tok-{reg.tokens}", "expires_in": 300}).encode())
                if not (self.headers.get("Authorization") or "").startswith("Bearer tok-"):
                    host = self.headers.get("Host")
                    return self._send(401, b"{}", {"WWW-Authenticate":
                                                   f'Bearer realm="http://{host}/token",service="fake"'})
                if path == "/v2/":
                    return self._send(200, b"{}")
                rest = path.removeprefix("/v2/")
                if rest.endswith("/tags/list"):
                    return self._tags(rest.removesuffix("/tags/list"), query)
                repo, kind, ref = rest.rsplit("/", 2)
                if repo not in reg.tags:
                    return self._send(404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}')
                if kind == "manifests":
                    if ref.startswith("sha256:"):
                        data = reg.blobs.get(ref)
                        if data is None:
                            return self._send(404, b"{}")
                        media = json.loads(data).get("mediaType") or OCI_MANIFEST
                        return self._send(200, data, {"Content-Type": media})
                    if ref not in reg.tags[repo]:
                        return self._send(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
                    digest = reg.digests.get((repo, ref)) or tag_digest(repo, ref)
                    body = reg.blobs.get(digest, b'{"schemaVersion":2}')
                    headers = {"Content-Type": OCI_INDEX}
                    if not reg.no_digest_header:
                        headers["Docker-Content-Digest"] = digest
                    return self._send(200, body, headers)
                if kind == "blobs" and ref in reg.blobs:
                    return self._send(200, reg.blobs[ref])
                return self._send(404, b"{}")

            def _tags(self, repo, query):
                tags = reg.tags.get(repo)
                if tags is None:
                    return self._send(404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}')
                n = min(int((query.get("n") or ["100"])[0]), reg.page_cap)
                last = (query.get("last") or [None])[0]
                start = tags.index(last) + 1 if last in tags else 0
                page = tags[start:start + n]
                headers = {"Content-Type": "application/json"}
                if start + n < len(tags):
                    headers["Link"] = reg.next_link or f'</v2/{repo}/tags/list?last={page[-1]}&n={n}>; rel="next"'
                return self._send(200, json.dumps({"name": repo, "tags": page}).encode(), headers)

            def _github(self, path, query):
                bits = path.split("/")       # ['', 'repos', owner, repo, 'releases', ('tags', tag)]
                repo = "/".join(bits[2:4])
                listed = reg.releases.get(repo)
                if listed is None or len(bits) < 5 or bits[4] != "releases":
                    return self._send(404, b'{"message":"Not Found"}')
                if len(bits) == 5:
                    n = int((query.get("per_page") or ["30"])[0])
                    return self._send(200, json.dumps(listed[:n]).encode(), {"Content-Type": "application/json"})
                if len(bits) == 7 and bits[5] == "tags":
                    hit = next((r for r in listed if r["tag_name"] == bits[6]), None)
                    if hit is None:
                        return self._send(404, b'{"message":"Not Found"}')
                    return self._send(200, json.dumps(hit).encode(), {"Content-Type": "application/json"})
                return self._send(404, b'{"message":"Not Found"}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add_blobs(self, img: Image) -> None:
        self.blobs[img.digest] = img.index
        self.blobs[sha(img.manifest)] = img.manifest
        self.blobs[sha(img.config)] = img.config

    def add_image(self, repo: str, tag: str, img: Image) -> None:
        self.digests[(repo, tag)] = img.digest
        self.blobs[img.digest] = img.index
        self.blobs[sha(img.manifest)] = img.manifest
        self.blobs[sha(img.config)] = img.config

    def recipes(self, **replace: str) -> dict:
        """The built-in recipes as the check sees them, with sd-cpp pinned to this upstream's copies of its pins;
        `replace` = {recipe name: its YAML} for a test's own version of one."""
        from forgeyard.models import recipes

        out = {}
        for name, (_, text) in recipes.builtin_recipes().items():
            if name == "sd-cpp":
                for real, fake in self.sd_pins.items():
                    text = text.replace(real, fake)
            out[name] = recipes.parse_recipe(replace.get(name, text), builtin=True)
        return out

    def count(self, prefix: str, method: str = "GET") -> int:
        return sum(1 for r in self.requests if r["path"].startswith(prefix) and r["method"] == method)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
