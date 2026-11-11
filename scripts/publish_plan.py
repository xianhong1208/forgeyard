"""What .github/workflows/publish-images.yml publishes, and whether it pushes at all.

    python3 scripts/publish_plan.py >> "$GITHUB_OUTPUT"

Reads the version from forgeyard/__init__.py and the Docker Hub namespace from
forgeyard/install/images.py (standard library only: it runs before any dependency is installed),
and the run from GitHub's variables (GITHUB_REF_TYPE, GITHUB_REF_NAME) plus HAVE_SECRETS ("true"
when both DOCKERHUB_USERNAME and DOCKERHUB_TOKEN are set). It pushes only for a release tag vX.Y.Z
that matches the version, with the secrets and the namespace set; otherwise the workflow builds both
images without pushing and says why. A tag that does not match the version is an error.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forgeyard import __version__  # noqa: E402
from forgeyard.install import images  # noqa: E402

# Placeholder repositories for a build that is not pushed, while no namespace is set.
DRY_RUN_NAMESPACE = "example"
_RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class PlanError(ValueError):
    pass


def image_tags(version: str) -> list[str]:
    """X.Y.Z, X.Y and latest for a release; only the exact version for a pre-release (1.3.0rc1)."""
    m = _RELEASE.match(version)
    if not m:
        return [version]
    return [version, f"{m.group(1)}.{m.group(2)}", "latest"]


def plan(version: str, namespace: str, ref_type: str, ref_name: str, have_secrets: bool) -> dict[str, str]:
    is_tag = ref_type == "tag"
    if is_tag and ref_name != f"v{version}":
        raise PlanError(f"tag {ref_name} does not match the version in forgeyard/__init__.py ({version}): "
                        f"expected v{version}")
    if not have_secrets:
        reason = ("secrets not configured, skipped push: set DOCKERHUB_USERNAME and DOCKERHUB_TOKEN "
                  "(Settings → Secrets and variables → Actions)")
    elif not namespace.strip():
        reason = "DOCKERHUB_NAMESPACE is empty in forgeyard/install/images.py, skipped push"
    elif not is_tag:
        reason = f"{ref_name} is not a release tag, skipped push (build only); run it on v{version} to publish"
    else:
        reason = ""
    ns = namespace.strip() or DRY_RUN_NAMESPACE
    agent = images.published_repository(images.AGENT_REPOSITORY, ns)
    server = images.published_repository(images.SERVER_REPOSITORY, ns)
    tags = image_tags(version)
    return {
        "version": version,
        "push": "false" if reason else "true",
        "reason": reason,
        "agent_repository": agent,
        "server_repository": server,
        "agent_tags": ",".join(f"{agent}:{t}" for t in tags),
        "server_tags": ",".join(f"{server}:{t}" for t in tags),
    }


def main() -> int:
    try:
        out = plan(__version__, images.DOCKERHUB_NAMESPACE, os.environ.get("GITHUB_REF_TYPE", ""),
                   os.environ.get("GITHUB_REF_NAME", ""), os.environ.get("HAVE_SECRETS") == "true")
    except PlanError as e:
        print(f"::error title=Wrong tag::{e}", file=sys.stderr)
        return 1
    for key, value in out.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
