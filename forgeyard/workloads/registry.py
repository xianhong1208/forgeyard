"""Anonymous reads from an image registry (the OCI distribution API), for the server's image relay (1.4,
issue #35 PR 4, D4) and scripts/pin_images.py's naming of references.

Only what a relay of a *pinned* image needs: a pull token per repository (the anonymous Bearer flow that
Docker Hub, GHCR and quay.io all answer: `GET /v2/` -> 401 `WWW-Authenticate: Bearer realm=…,service=…` ->
`GET <realm>?service=…&scope=repository:<repo>:pull`), a manifest by its digest (the bytes must hash to it),
and a blob by its digest, opened for streaming with `Range` passed on. Every request goes through
forgeyard.models.fetch: https and public addresses only (unless the server allows private model hosts, a LAN
mirror), every redirect checked, and the token dropped when a redirect leaves the registry (blob downloads
are redirected to a CDN with a signed URL).

Nothing is stored but the tokens, in memory, until shortly before they expire.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Optional
from urllib.parse import urlencode, urlsplit

from forgeyard.models import fetch

MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
ACCEPT = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
MANIFEST_MAX = 4 * 1024 * 1024
TOKEN_MAX = 64 * 1024
TOKEN_TTL = 60.0          # what a token lives when the registry does not say (the spec's default)
TOKEN_MARGIN = 20.0       # renewed this long before it expires
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')

# Tests (only) point a registry name at a fake one: {"ghcr.io": "http://127.0.0.1:<port>"}.
ENDPOINTS: dict[str, str] = {}


class RegistryError(fetch.FetchError):
    """The registry refused or failed; the message is safe to show."""


def split_image(image: str) -> tuple[str, str, str, Optional[str]]:
    """'ghcr.io/a/b:tag@sha256:x' -> ('ghcr.io', 'a/b', 'tag', 'sha256:x'); Docker Hub names get
    'registry-1.docker.io' and 'library/' for official images."""
    ref, _, digest = image.partition("@")
    name, _, tag = ref.rpartition(":") if ":" in ref.split("/")[-1] else (ref, "", "latest")
    first = name.split("/")[0]
    if "." in first or ":" in first or first == "localhost":
        registry, repo = first, name.split("/", 1)[1]
    else:
        registry, repo = "registry-1.docker.io", name if "/" in name else f"library/{name}"
    return registry, repo, tag, digest or None


def registry_host(image: Optional[str]) -> Optional[str]:
    """The registry an image reference is pulled from, as a host name: ghcr.io/x/y:tag -> ghcr.io;
    python:3.13 -> docker.io. The server and the agent both key "this machine cannot reach it" on it, so it
    lives here, where the agent can import it (no database)."""
    if not image:
        return None
    first, slash, _ = image.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        return first.split(":", 1)[0].lower()
    return "docker.io"


def full_name(image: str) -> str:
    """An image reference in Docker's full form, to compare names: Docker lists Docker Hub images without
    "docker.io/" (and official ones without "library/"), a recipe may name them in full.
    'python:3.13' -> 'docker.io/library/python:3.13'; 'acme/x:1' -> 'docker.io/acme/x:1'; others unchanged."""
    first, slash, rest = image.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        if first.lower() == "docker.io" and "/" not in rest:
            return f"docker.io/library/{rest}"
        return image
    return f"docker.io/{image}" if slash else f"docker.io/library/{image}"


def tag_ref(image: str) -> str:
    """The name a loaded copy is tagged with: 'ghcr.io/a/b:tag@sha256:…' -> 'ghcr.io/a/b:tag' (as written in
    the recipe; Docker shortens 'docker.io/library/x' to 'x' itself)."""
    return image.partition("@")[0]


def base_url(registry: str) -> str:
    return ENDPOINTS.get(registry) or f"https://{registry}"


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Registry:
    """Anonymous pulls from any registry, through the server's egress rules. Thread-safe."""

    def __init__(self, policy_for=None):
        # A function returning the fetch.Policy to use now (the setting may change while the server runs).
        self._policy_for = policy_for or (lambda: fetch.Policy())
        self._lock = threading.Lock()
        self._tokens: dict[tuple[str, str], tuple[Optional[str], float]] = {}

    def policy(self) -> fetch.Policy:
        return self._policy_for()

    # ------------------------------------------------------------------ tokens
    def token(self, registry: str, repo: str, *, renew: bool = False) -> Optional[str]:
        """A pull token for the repository (None: the registry wants none)."""
        key = (registry, repo)
        now = time.monotonic()
        with self._lock:
            held = self._tokens.get(key)
            if held is not None and not renew and held[1] > now:
                return held[0]
        token, ttl = self._new_token(registry, repo)
        with self._lock:
            if len(self._tokens) > 256:
                self._tokens.clear()
            self._tokens[key] = (token, time.monotonic() + max(1.0, ttl - TOKEN_MARGIN))
        return token

    def _new_token(self, registry: str, repo: str) -> tuple[Optional[str], float]:
        policy = self.policy()
        with fetch.open_url(f"{base_url(registry)}/v2/", policy) as probe:
            challenge = probe.headers.get("WWW-Authenticate") or ""
            status = probe.status
            probe.read(TOKEN_MAX)
        if status < 400:
            return None, 3600.0  # an open registry
        if status != 401 or not challenge.lower().startswith("bearer"):
            raise RegistryError(f"{registry} answered HTTP {status} without a way to get a pull token", status)
        params = dict(_CHALLENGE.findall(challenge))
        realm = params.pop("realm", "")
        if not realm:
            raise RegistryError(f"{registry}'s token challenge names no realm")
        params.pop("error", None)
        params["scope"] = f"repository:{repo}:pull"
        url = realm + ("&" if "?" in realm else "?") + urlencode(params)
        if urlsplit(url).scheme not in ("https", "http"):
            raise RegistryError(f"{registry}'s token realm is not a web address")
        data = fetch.get_bytes(url, policy, limit=TOKEN_MAX)
        try:
            body = json.loads(data)
        except ValueError:
            raise RegistryError(f"{registry}'s token answer is not JSON") from None
        token = body.get("token") or body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise RegistryError(f"{registry} gave no pull token for {repo}")
        try:
            ttl = float(body.get("expires_in") or TOKEN_TTL)
        except (TypeError, ValueError):
            ttl = TOKEN_TTL
        return token, ttl

    def _headers(self, registry: str, repo: str, *, renew: bool = False) -> dict:
        token = self.token(registry, repo, renew=renew)
        return {"Authorization": f"Bearer {token}"} if token else {}

    # ------------------------------------------------------------------ manifests and blobs
    def manifest(self, registry: str, repo: str, digest: str) -> tuple[bytes, str]:
        """(the exact bytes, media type) of the manifest or index with this digest. The bytes must hash to
        the digest: whatever the registry sends, nothing else is ever used."""
        if not DIGEST_RE.match(digest):
            raise RegistryError(f"not a sha256 digest: {digest[:80]}")
        url = f"{base_url(registry)}/v2/{repo}/manifests/{digest}"
        for renew in (False, True):
            with fetch.open_url(url, self.policy(), headers={**self._headers(registry, repo, renew=renew),
                                                             "Accept": ACCEPT}) as resp:
                if resp.status == 401 and not renew:
                    resp.read(TOKEN_MAX)
                    continue  # the token expired early: once more with a new one
                if resp.status >= 400:
                    raise RegistryError(f"{registry} answered HTTP {resp.status} for the manifest {digest[:19]}…",
                                        resp.status)
                data = resp.read(MANIFEST_MAX + 1)
                media = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            break
        if len(data) > MANIFEST_MAX:
            raise RegistryError(f"{registry}: the manifest {digest[:19]}… is larger than {MANIFEST_MAX} bytes")
        got = sha256_digest(data)
        if got != digest:
            raise RegistryError(f"{registry}: the manifest asked for as {digest[:19]}… hashes to {got[:19]}…")
        try:
            body = json.loads(data)
        except ValueError:
            raise RegistryError(f"{registry}: the manifest {digest[:19]}… is not JSON") from None
        return data, (body.get("mediaType") if isinstance(body, dict) and body.get("mediaType") else media)

    def blob_url(self, registry: str, repo: str, digest: str) -> str:
        if not DIGEST_RE.match(digest):
            raise RegistryError(f"not a sha256 digest: {digest[:80]}")
        return f"{base_url(registry)}/v2/{repo}/blobs/{digest}"

    def open_blob(self, registry: str, repo: str, digest: str, *, headers: Optional[dict] = None) -> fetch.Response:
        """The blob opened for reading (the caller closes it), `Range` passed on; a 401 is tried once more with
        a new token. Any other status is the caller's to judge."""
        url = self.blob_url(registry, repo, digest)
        extra = {k: v for k, v in (headers or {}).items() if k.lower() == "range"}
        for renew in (False, True):
            resp = fetch.open_url(url, self.policy(), headers={**self._headers(registry, repo, renew=renew), **extra})
            if resp.status == 401 and not renew:
                resp.close()
                continue
            return resp
        raise RegistryError("unreachable")  # pragma: no cover


def parse_manifest(data: bytes) -> dict:
    """A single-platform image manifest: {media_type, config: {digest, size}, layers: [{digest, size,
    mediaType}]}; ValueError for anything else (an index, a schema 1 manifest, a malformed one)."""
    try:
        body = json.loads(data)
    except ValueError:
        raise ValueError("the manifest is not JSON") from None
    if not isinstance(body, dict) or body.get("schemaVersion") != 2:
        raise ValueError("not an image manifest (schema 2)")
    media = body.get("mediaType") or ""
    if media in INDEX_TYPES or "manifests" in body:
        raise ValueError("an index, not one platform's manifest")
    if media and media not in MANIFEST_TYPES:
        raise ValueError(f"an unknown manifest type: {media[:80]}")
    config = body.get("config")
    layers = body.get("layers")
    if not isinstance(config, dict) or not isinstance(layers, list) or not layers:
        raise ValueError("the manifest names no config or no layers")

    def desc(d) -> dict:
        if not isinstance(d, dict) or not DIGEST_RE.match(str(d.get("digest") or "")):
            raise ValueError("a descriptor without a sha256 digest")
        size = d.get("size")
        if not isinstance(size, int) or size < 0:
            raise ValueError("a descriptor without a size")
        return {"digest": d["digest"], "size": size, "mediaType": str(d.get("mediaType") or "")}

    return {"media_type": media or MANIFEST_TYPES[1], "config": desc(config), "layers": [desc(x) for x in layers]}
