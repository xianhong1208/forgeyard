"""The recorded ids of a pinned image, read from its registry: what scripts/pin_images.py writes into
forgeyard/recipes/images.lock.json for the built-in recipes, and what "Try this version" (1.5.2, #95) records in the
`image_pins` table for a copy's images (forgeyard/workloads/pins.py). One computation for both, so a copy's image is
checked, relayed and matched after `docker load` exactly as a built-in's is (forgeyard/workloads/images.py).

An entry: {"index": digest | None, "platforms": {"os/arch[/variant]": {"manifest", "config", "content"}}}, read by the
image's digest only, every manifest and config hashed against the digest it was asked for. Attestation manifests
(platform unknown/unknown) are left out: nothing runs them.

The reader is anything with
    manifest(registry, repo, ref) -> (digest, body: dict with "mediaType")
    blob(registry, repo, digest) -> dict (the JSON of the blob, its bytes checked against the digest)
scripts/pin_images.py brings its own (httpx, run by a maintainer); the server uses RegistryReader, over the image
relay's client (forgeyard/workloads/registry.py): the server's egress rules, anonymous pull tokens.
"""

from __future__ import annotations

import json
from typing import Protocol

from forgeyard.workloads import images as image_ids
from forgeyard.workloads import registry as oci

INDEX_TYPES = oci.INDEX_TYPES
CONFIG_MAX = 1024 * 1024


class Reader(Protocol):
    def manifest(self, registry: str, repo: str, ref: str) -> tuple[str, dict]: ...

    def blob(self, registry: str, repo: str, digest: str) -> dict: ...


def describe(reg: Reader, image: str) -> dict:
    """The lock entry of a pinned image (`repo:tag@sha256:…`), read from the registry by its digest."""
    registry, repo, _, digest = oci.split_image(image)
    if not digest:
        raise ValueError(f"{image} is not pinned by digest")
    _, body = reg.manifest(registry, repo, digest)
    if body["mediaType"] in INDEX_TYPES:
        index, members = digest, [(m["digest"], m.get("platform") or {}) for m in body.get("manifests", [])]
    else:
        index, members = None, [(digest, None)]
    platforms: dict[str, dict] = {}
    for mdigest, plat in members:
        if plat is not None and (not plat.get("os") or plat.get("os") == "unknown"):
            continue
        _, manifest = (reg.manifest(registry, repo, mdigest) if mdigest != digest or index else (digest, body))
        config_digest = manifest["config"]["digest"]
        config = reg.blob(registry, repo, config_digest)
        name = "/".join(x for x in (config.get("os"), config.get("architecture"), config.get("variant")) if x)
        platforms[name] = {"manifest": mdigest, "config": config_digest,
                           "content": image_ids.content_of_config(config)}
    return {"index": index, "platforms": dict(sorted(platforms.items()))}


def missing_platforms(entry: dict, variant_arch: list[str] | None) -> list[str]:
    """The platforms a variant needs that the entry lacks: linux/amd64 always, linux/arm64 when the variant claims
    aarch64 (as scripts/pin_images.py checks the built-in recipes)."""
    wanted = {"linux/amd64"}
    if variant_arch and "aarch64" in variant_arch:
        wanted.add("linux/arm64")
    return sorted(wanted - set(entry.get("platforms") or {}))


class RegistryReader:
    """The Reader over the server's registry client (forgeyard/workloads/registry.py): its egress rules, its tokens,
    its digest checks."""

    def __init__(self, client: oci.Registry):
        self.client = client

    def manifest(self, registry: str, repo: str, ref: str) -> tuple[str, dict]:
        data, media = self.client.manifest(registry, repo, ref)   # by digest only: the bytes hash to it
        body = json.loads(data)
        if not isinstance(body, dict):
            raise oci.RegistryError(f"{registry}: the manifest {ref[:19]}… is not an object")
        if not body.get("mediaType"):
            body["mediaType"] = media
        return ref, body

    def blob(self, registry: str, repo: str, digest: str) -> dict:
        with self.client.open_blob(registry, repo, digest) as resp:
            if resp.status >= 400:
                raise oci.RegistryError(f"{registry} answered HTTP {resp.status} for the blob {digest[:19]}…",
                                        resp.status)
            raw = resp.read(CONFIG_MAX + 1)
        if len(raw) > CONFIG_MAX:
            raise oci.RegistryError(f"{registry}: the image config {digest[:19]}… is larger than 1 MiB")
        got = oci.sha256_digest(raw)
        if got != digest:
            raise oci.RegistryError(f"{registry}: the blob asked for as {digest[:19]}… hashes to {got[:19]}…")
        try:
            body = json.loads(raw)
        except ValueError:
            raise oci.RegistryError(f"{registry}: the image config {digest[:19]}… is not JSON") from None
        if not isinstance(body, dict):
            raise oci.RegistryError(f"{registry}: the image config {digest[:19]}… is not an object")
        return body
