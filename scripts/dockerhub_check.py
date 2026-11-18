"""Can the Docker Hub secrets push the release images? Run by .github/workflows/dockerhub-login.yml.

    DOCKERHUB_USERNAME=… DOCKERHUB_TOKEN=… python3 scripts/dockerhub_check.py

Asks Docker Hub's token service (https://auth.docker.io/token, the first step of every `docker push`) for
pull and push on the three published repositories (server, agent, vision, under DOCKERHUB_NAMESPACE in
forgeyard/install/images.py), with the secrets as Basic credentials. The answer is a JWT whose `access` claim
lists the actions granted on each repository (https://distribution.github.io/distribution/spec/auth/jwt/);
a read-only access token, or one for another account, is granted `pull` only. Nothing is pushed and no
repository is touched.

Exit 0 when every repository is granted push, 1 when the credentials are refused or a repository lacks push.
Without the secrets or the namespace it says why and exits 0: the publish workflow then pushes nothing
either. The secrets are only ever sent to auth.docker.io, never printed. Standard library only: it runs
before any dependency is installed.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forgeyard.install import images  # noqa: E402

TOKEN_URL = "https://auth.docker.io/token"
SERVICE = "registry.docker.io"
FIX = ("re-enter both repository secrets (Settings → Secrets and variables → Actions): DOCKERHUB_USERNAME is "
       "the Docker Hub account name (not the email), DOCKERHUB_TOKEN a personal access token with Read & Write")


def repositories(namespace: str) -> list[str]:
    ns = namespace.strip()
    return [f"{ns}/{r}" for r in (images.SERVER_REPOSITORY, images.AGENT_REPOSITORY, images.VISION_REPOSITORY)]


def token_url(repos: list[str]) -> str:
    query = [("service", SERVICE)] + [("scope", f"repository:{r}:pull,push") for r in repos]
    return f"{TOKEN_URL}?{urllib.parse.urlencode(query)}"


def granted(token: str) -> dict[str, set[str]] | None:
    """The actions the token grants per repository, from its JWT payload; None when it is not readable."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (binascii.Error, ValueError):
        return None
    access = payload.get("access") if isinstance(payload, dict) else None
    if not isinstance(access, list):
        return None
    out: dict[str, set[str]] = {}
    for entry in access:
        if isinstance(entry, dict) and entry.get("type") == "repository" and isinstance(entry.get("name"), str):
            out.setdefault(entry["name"], set()).update(a for a in entry.get("actions") or [] if isinstance(a, str))
    return out


def without_push(grants: dict[str, set[str]], repos: list[str]) -> list[str]:
    return [r for r in repos if "push" not in grants.get(r, set())]


def summary(title: str, text: str) -> None:
    """A section in the run's summary page (GITHUB_STEP_SUMMARY), when there is one."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"### {title}\n\n{text}\n\n")


class Refused(Exception):
    pass


def fetch_token(url: str, username: str, password: str, attempts: int = 3) -> str:
    """The registry token for these credentials. Refused on 401/403; retries network errors and 5xx."""
    basic = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {basic}", "User-Agent": "forgeyard-ci"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.load(resp)
            return body.get("token") or body.get("access_token") or ""
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                try:
                    details = str(json.loads(e.read() or b"{}").get("details", ""))[:200]
                except ValueError:
                    details = ""
                raise Refused(f"HTTP {e.code}" + (f" ({details})" if details else "")) from None
            if e.code < 500 or attempt == attempts:
                raise
        except urllib.error.URLError:
            if attempt == attempts:
                raise
        time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def main() -> int:
    username = os.environ.get("DOCKERHUB_USERNAME", "")
    password = os.environ.get("DOCKERHUB_TOKEN", "")
    if not username or not password:
        print("::warning title=Docker Hub secrets not set::Docker Hub secrets not set: a release would not "
              "push images (set DOCKERHUB_USERNAME and DOCKERHUB_TOKEN)")
        summary("Docker Hub secrets not set", "A release would not push images: set the repository secrets "
                "DOCKERHUB_USERNAME and DOCKERHUB_TOKEN.")
        return 0
    if not images.DOCKERHUB_NAMESPACE.strip():
        print("::warning title=No Docker Hub namespace::DOCKERHUB_NAMESPACE is empty in "
              "forgeyard/install/images.py: a release would not push images")
        return 0
    repos = repositories(images.DOCKERHUB_NAMESPACE)
    try:
        token = fetch_token(token_url(repos), username, password)
    except Refused as e:
        print(f"::error title=Docker Hub refused the secrets::Docker Hub refused DOCKERHUB_USERNAME / "
              f"DOCKERHUB_TOKEN: {e}. A release would fail to push: {FIX}")
        summary("Docker Hub refused the secrets", f"{e}. A release would fail to push: {FIX}.")
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"::error title=Docker Hub not reached::Could not ask Docker Hub's token service: "
              f"{type(e).__name__}. Run the workflow again later.")
        return 1
    grants = granted(token)
    if grants is None:
        print("::warning title=Push access not checked::Docker Hub accepted the secrets, but its token did not "
              "say which actions it grants; push access was not checked")
        return 0
    for r in repos:
        print(f"{r}: {', '.join(sorted(grants.get(r, set()))) or 'nothing'}")
    missing = without_push(grants, repos)
    if missing:
        print(f"::error title=Docker Hub token cannot push::The secrets log in but cannot push to "
              f"{', '.join(missing)}. A release would fail to push: {FIX}")
        summary("Docker Hub token cannot push", f"No push on {', '.join(missing)}. {FIX}.")
        return 1
    print("Docker Hub accepts the secrets, with push on every published repository.")
    summary("Docker Hub login", f"The secrets log in and can push to {', '.join(repos)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
