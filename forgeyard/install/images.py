"""Where Forgeyard's container images are published, and which agent image a machine should run.

Releases push two images to Docker Hub (.github/workflows/publish-images.yml), for linux/amd64 and
linux/arm64, tagged X.Y.Z, X.Y and latest:

    docker.io/<DOCKERHUB_NAMESPACE>/forgeyard         the server (Dockerfile.server)
    docker.io/<DOCKERHUB_NAMESPACE>/forgeyard-agent   the agent (Dockerfile.agent)
    docker.io/<DOCKERHUB_NAMESPACE>/forgeyard-vision  the workload templates' vision server (images/vision;
                                                      X.Y.Z for the CPU, X.Y.Z-cuda for NVIDIA, amd64)

DOCKERHUB_NAMESPACE is the one place the namespace is written: the publish workflow reads it from
here, and so does the server when it shows the agent's `docker run`. Empty: nothing is published
yet, so the Machines page shows how to build the agent image locally (forgeyard-agent:<version>).

This module imports nothing from the rest of Forgeyard (the settings validate AGENT_IMAGE with it,
and the publish workflow reads it with a bare python3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The Docker Hub user or organisation the release images are pushed to (the maintainer's account).
DOCKERHUB_NAMESPACE = "tonyhuang1101"

REGISTRY = "docker.io"
SERVER_REPOSITORY = "forgeyard"
AGENT_REPOSITORY = "forgeyard-agent"
# The vision server behind the ready-made workload templates (1.3, images/vision): tag X.Y.Z (CPU, amd64 and
# arm64) and X.Y.Z-cuda (NVIDIA, amd64).
VISION_REPOSITORY = "forgeyard-vision"
# A local build of the agent image (Dockerfile.agent) while nothing is published.
LOCAL_AGENT_IMAGE = AGENT_REPOSITORY

# registry[:port]/path[:tag][@sha256:digest], the part of Docker's reference grammar we accept:
# lower-case path components, a tag of at most 128 characters, a sha256 digest. Nothing a shell
# would expand: the reference goes into a command people paste.
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REFERENCE = re.compile(
    rf"^(?P<repository>(?:[a-z0-9.-]+(?::\d{{1,5}})?/)?{_COMPONENT}(?:/{_COMPONENT})*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)


def published_repository(name: str, namespace: str | None = None) -> str:
    """docker.io/<namespace>/<name>, or "" while no namespace is set."""
    ns = (DOCKERHUB_NAMESPACE if namespace is None else namespace).strip()
    return f"{REGISTRY}/{ns}/{name}" if ns else ""


def reference_error(value: str) -> str | None:
    """Why `value` is not an image reference AGENT_IMAGE accepts (None: it is)."""
    if not _REFERENCE.match(value.strip()):
        return ("AGENT_IMAGE must be an image reference such as docker.io/forgeyard/forgeyard-agent, "
                "optionally with :TAG or @sha256:DIGEST (lower-case repository, no spaces).")
    return None


@dataclass(frozen=True)
class AgentImage:
    """The agent image a new machine runs."""

    reference: str    # what `docker run` names: repository:tag, or repository@sha256:… when pinned
    published: bool   # pulled from a registry; False: built locally from a checkout (Dockerfile.agent)
    local_tag: str    # repository:tag without a digest: what a local build or `docker load` is named

    @property
    def pinned_by_digest(self) -> bool:
        return "@sha256:" in self.reference


def agent_image(setting: str, version: str, namespace: str | None = None) -> AgentImage:
    """The agent image for AGENT_IMAGE `setting` (empty: the published one, if any) on a server of
    `version`. Without a tag or digest, the tag is the server's own version, so an agent always
    matches its server."""
    value = setting.strip() or published_repository(AGENT_REPOSITORY, namespace)
    if not value:
        local = f"{LOCAL_AGENT_IMAGE}:{version}"
        return AgentImage(local, False, local)
    m = _REFERENCE.match(value)
    if not m:
        raise ValueError(reference_error(value))
    repository, tag, digest = m.group("repository"), m.group("tag"), m.group("digest")
    local_tag = f"{repository}:{tag or version}"
    reference = f"{repository}@{digest}" if digest else local_tag
    return AgentImage(reference, True, local_tag)


def vision_image(version: str, variant: str = "cpu", namespace: str | None = None) -> str:
    """The vision server's image for a server of `version`: the published one (X.Y.Z, or X.Y.Z-cuda), else
    a local build's tag (forgeyard-vision:X.Y.Z)."""
    repository = published_repository(VISION_REPOSITORY, namespace) or VISION_REPOSITORY
    return f"{repository}:{version}{'-cuda' if variant == 'cuda' else ''}"
