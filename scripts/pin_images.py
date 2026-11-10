"""Pin the built-in recipes' engine images by digest.

Every container variant in forgeyard/recipes/*.yaml names its image as `repo:tag@sha256:…`: the
tag says which release it is (and is what the web UI shows), the digest is what the agent pulls,
so a tag moved on the registry can never change what runs.

    uv run --no-sync python scripts/pin_images.py           # check: every digest still matches its tag
    uv run --no-sync python scripts/pin_images.py --write   # resolve each tag again and rewrite the YAML

To bump an engine: change the tag in the recipe (keep or drop the old @sha256:…), run with --write,
review the diff, run the tests. The digest is the multi-architecture index (or the single manifest
when the tag has no index), checked to contain linux/amd64, and linux/arm64 where the variant
claims aarch64. Only anonymous pulls are used: Docker Hub and GHCR both allow them for public images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import httpx

RECIPES = Path(__file__).resolve().parent.parent / "forgeyard" / "recipes"
IMAGE_LINE = re.compile(r"^(?P<lead>\s*image:\s*)(?P<image>\S+)\s*$")
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")


def split_image(image: str) -> tuple[str, str, str, str | None]:
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


def _token(client: httpx.Client, registry: str, repo: str) -> str | None:
    probe = client.get(f"https://{registry}/v2/")
    challenge = probe.headers.get("www-authenticate", "")
    if not challenge.lower().startswith("bearer"):
        return None
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm")
    params["scope"] = f"repository:{repo}:pull"
    r = client.get(realm, params=params)
    r.raise_for_status()
    data = r.json()
    return data.get("token") or data.get("access_token")


def resolve(client: httpx.Client, image: str) -> tuple[str, list[str]]:
    """(digest, platforms) of the image's tag, as the registry serves it now."""
    registry, repo, tag, _ = split_image(image)
    token = _token(client, registry, repo)
    headers = {"Accept": ACCEPT, **({"Authorization": f"Bearer {token}"} if token else {})}
    r = client.get(f"https://{registry}/v2/{repo}/manifests/{tag}", headers=headers)
    r.raise_for_status()
    digest = "sha256:" + hashlib.sha256(r.content).hexdigest()
    served = r.headers.get("docker-content-digest")
    if served and served != digest:
        raise RuntimeError(f"{image}: the registry says {served}, the bytes hash to {digest}")
    body = json.loads(r.content)
    media = body.get("mediaType") or r.headers.get("content-type", "").split(";")[0]
    if media in INDEX_TYPES:
        platforms = sorted({f"{m['platform']['os']}/{m['platform']['architecture']}"
                            for m in body.get("manifests", []) if m.get("platform")
                            and m["platform"].get("os") != "unknown"})
    else:  # a single-platform image: read its config for the platform
        cfg = body["config"]["digest"]
        c = client.get(f"https://{registry}/v2/{repo}/blobs/{cfg}", headers=headers, follow_redirects=True)
        c.raise_for_status()
        conf = c.json()
        platforms = [f"{conf.get('os')}/{conf.get('architecture')}"]
    return digest, platforms


def _wanted_platforms(text: str, line_no: int) -> set[str]:
    """linux/amd64, plus linux/arm64 when the variant this image line belongs to claims aarch64."""
    lines = text.splitlines()
    wanted = {"linux/amd64"}
    for back in range(line_no - 1, max(-1, line_no - 6), -1):
        if "when:" in lines[back]:
            if "aarch64" in lines[back]:
                wanted.add("linux/arm64")
            break
    return wanted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="rewrite the recipes with the digests resolved now")
    args = parser.parse_args(argv)
    problems = 0
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for path in sorted(RECIPES.glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            out = []
            for i, line in enumerate(text.splitlines()):
                m = IMAGE_LINE.match(line)
                if not m:
                    out.append(line)
                    continue
                image = m.group("image")
                ref, _, pinned = image.partition("@")
                digest, platforms = resolve(client, ref)
                missing = _wanted_platforms(text, i) - set(platforms)
                state = "ok" if pinned == digest else ("unpinned" if not pinned else "MOVED")
                print(f"{path.name}: {ref} -> {digest} [{', '.join(platforms)}] {state}")
                if missing:
                    print(f"  missing platform(s): {', '.join(sorted(missing))}")
                    problems += 1
                if state != "ok" and not args.write:
                    problems += 1
                out.append(f"{m.group('lead')}{ref}@{digest}")
            if args.write:
                path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
