"""The published container images: which agent image a machine runs (FY-INST-009..013), the server
image's Dockerfile and health check (FY-IMG-003..006), and the release workflow that publishes both
(FY-PUB-*). The images themselves are built and smoke-tested in CI (server-image, agent-image,
images-arm64, publish-images.yml)."""

from __future__ import annotations

import fnmatch
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from forgeyard import __version__
from forgeyard.config import Settings, get_settings
from forgeyard.install import commands, healthcheck, images
from forgeyard.security import tls
from tests.tls_helpers import LiveTLS, free_port

ROOT = Path(__file__).resolve().parent.parent
DIGEST = "sha256:" + "0f" * 32


def _plan_module():
    spec = importlib.util.spec_from_file_location("publish_plan", ROOT / "scripts" / "publish_plan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ which agent image
def test_fy_inst_009_nothing_published_means_a_local_build_of_this_version(monkeypatch):
    """FY-INST-009: with no Docker Hub namespace and no AGENT_IMAGE, the agent image is the local
    build forgeyard-agent:<version>, not a registry name that does not exist."""
    monkeypatch.setattr(images, "DOCKERHUB_NAMESPACE", "")
    img = images.agent_image("", "1.2.0")
    assert img == images.AgentImage("forgeyard-agent:1.2.0", False, "forgeyard-agent:1.2.0")
    assert images.published_repository("forgeyard-agent") == ""
    assert not img.pinned_by_digest


def test_fy_inst_010_the_namespace_publishes_and_the_tag_is_the_servers_version(monkeypatch):
    """FY-INST-010: the namespace (one constant) names both repositories on docker.io; the agent's
    tag is the server's own version unless AGENT_IMAGE gives a tag or a digest; a mirror or a private
    registry (with a port) works the same way."""
    monkeypatch.setattr(images, "DOCKERHUB_NAMESPACE", "forgeyard")
    assert images.published_repository(images.SERVER_REPOSITORY) == "docker.io/forgeyard/forgeyard"
    assert images.agent_image("", "1.2.0") == images.AgentImage(
        "docker.io/forgeyard/forgeyard-agent:1.2.0", True, "docker.io/forgeyard/forgeyard-agent:1.2.0")
    assert images.agent_image("docker.io/acme/forgeyard-agent:1.2.1", "1.2.0").reference == "docker.io/acme/forgeyard-agent:1.2.1"
    pinned = images.agent_image(f"docker.io/acme/forgeyard-agent@{DIGEST}", "1.2.0")
    assert pinned == images.AgentImage(f"docker.io/acme/forgeyard-agent@{DIGEST}", True, "docker.io/acme/forgeyard-agent:1.2.0")
    assert pinned.pinned_by_digest
    both = images.agent_image(f"docker.io/acme/forgeyard-agent:1.2.1@{DIGEST}", "1.2.0")
    assert (both.reference, both.local_tag) == (f"docker.io/acme/forgeyard-agent@{DIGEST}", "docker.io/acme/forgeyard-agent:1.2.1")
    mirror = images.agent_image("registry.example.org:5000/ops/forgeyard-agent", "1.2.0")
    assert mirror.reference == "registry.example.org:5000/ops/forgeyard-agent:1.2.0" and mirror.published
    # AGENT_IMAGE wins over the namespace, also while none is set.
    monkeypatch.setattr(images, "DOCKERHUB_NAMESPACE", "")
    assert images.agent_image("localhost:5000/forgeyard-agent", "1.2.0").reference == "localhost:5000/forgeyard-agent:1.2.0"


@pytest.mark.parametrize("value", [
    "Docker.io/Forgeyard/agent", "docker.io/forgeyard/forgeyard-agent:", "docker.io/forgeyard/agent@sha256:abc",
    "docker.io/forgeyard/forgeyard agent", "docker.io/$(id)/forgeyard-agent", "docker.io/forgeyard/agent;rm",
    "https://docker.io/forgeyard/forgeyard-agent", "docker.io/forgeyard/forgeyard-agent:" + "x" * 129, "/forgeyard-agent",
])
def test_fy_inst_011_agent_image_refuses_what_is_not_an_image_reference(value):
    """FY-INST-011: AGENT_IMAGE goes into a command people paste: anything that is not a plain image
    reference (upper case, spaces, shell characters, a URL, a short digest) stops the server at start
    with a message naming AGENT_IMAGE, and agent_image() refuses it too."""
    with pytest.raises(ValidationError, match="AGENT_IMAGE must be an image reference"):
        Settings(agent_image=value)
    with pytest.raises(ValueError):
        images.agent_image(value, "1.2.0")


def test_fy_inst_012_agent_image_setting_is_read_and_trimmed(monkeypatch):
    """FY-INST-012: AGENT_IMAGE from the environment, surrounding spaces dropped; empty is allowed."""
    monkeypatch.setenv("AGENT_IMAGE", "  registry.example.org/forgeyard/forgeyard-agent  ")
    get_settings.cache_clear()
    assert get_settings().agent_image == "registry.example.org/forgeyard/forgeyard-agent"
    assert Settings(agent_image="").agent_image == ""


def test_fy_inst_013_the_machines_page_uses_the_published_image(owner, monkeypatch):
    """FY-INST-013: a join token's Docker commands end with the agent image; with the namespace set
    it is the published one at the server's version (agent_image.published, so the page shows no
    build step), and AGENT_IMAGE (a digest here) wins; the build command names the image's tag so a
    local build or `docker load` is found by the same `docker run`."""
    tls.ensure()
    owner.base_url = "https://localhost"

    def issued():
        get_settings.cache_clear()
        r = owner.post("/api/machines/join-tokens")
        assert r.status_code == 201
        return r.json()

    monkeypatch.setattr(images, "DOCKERHUB_NAMESPACE", "")
    j = issued()  # nothing published (no namespace): a local build
    assert j["agent_image"] == {"reference": f"forgeyard-agent:{__version__}", "published": False,
                                "local_tag": f"forgeyard-agent:{__version__}"}

    monkeypatch.setattr(images, "DOCKERHUB_NAMESPACE", "forgeyard")
    j = issued()
    ref = f"docker.io/forgeyard/forgeyard-agent:{__version__}"
    assert j["agent_image"] == {"reference": ref, "published": True, "local_tag": ref}
    for kind in ("nvidia", "amd", "cpu"):
        assert j["docker_commands"][kind].endswith(f" \\\n  {ref}"), kind
    assert j["docker_build_command"] == f"docker build -f Dockerfile.agent -t {ref} ."

    monkeypatch.setenv("AGENT_IMAGE", f"docker.io/forgeyard/forgeyard-agent@{DIGEST}")
    j = issued()
    assert j["agent_image"]["reference"] == f"docker.io/forgeyard/forgeyard-agent@{DIGEST}"
    assert j["docker_commands"]["cpu"].endswith(f"forgeyard-agent@{DIGEST}")
    assert j["docker_build_command"] == f"docker build -f Dockerfile.agent -t docker.io/forgeyard/forgeyard-agent:{__version__} ."
    assert commands.agent_image().pinned_by_digest


# ------------------------------------------------------------------ the server image
def _instructions(name: str) -> list[tuple[str, str]]:
    text = (ROOT / name).read_text().replace("\\\n", " ")
    return [(m.group(1).upper(), m.group(2)) for m in re.finditer(r"^(\w+)\s+(.*)$", text, re.M)
            if not m.group(0).startswith("#")]


def _context_allows(path: str) -> bool:
    """.dockerignore as Docker reads it: the last matching pattern wins, ! re-includes."""
    allowed = True
    for raw in (ROOT / ".dockerignore").read_text().splitlines():
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negate = pattern.startswith("!")
        pattern = pattern.lstrip("!").rstrip("/")
        parts = path.split("/")
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        if any(fnmatch.fnmatch(p, pattern) or fnmatch.fnmatch(p, pattern.replace("**/", "")) for p in prefixes):
            allowed = negate
    return allowed


def test_fy_img_003_the_server_image_is_pinned_non_root_and_keeps_its_data_in_a_volume():
    """FY-IMG-003: Dockerfile.server builds the web UI in a node stage on the build platform, installs
    exactly uv.lock (no dev group, with PostgreSQL), runs as a non-root account, keeps the database,
    secret.key and tls/ in the /data volume, listens on 0.0.0.0:7860 and has a HEALTHCHECK; every
    base image is pinned by digest; the OCI labels name the source, the licence, version and
    revision."""
    text = (ROOT / "Dockerfile.server").read_text()
    ins = _instructions("Dockerfile.server")
    froms = [v for k, v in ins if k == "FROM"]
    images_ = [f.split()[-3] if " AS " in f else f.split()[-1] for f in froms]
    external = [i for i in images_ if i not in ("base", "venv", "web")]
    assert external and all(re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", i) for i in external), external
    assert froms[0].startswith("--platform=$BUILDPLATFORM node:"), froms[0]
    assert "npm ci" in text and "npm run build" in text
    assert "uv sync --locked --no-dev --extra postgres" in text and "uv==" in text
    final = text[text.rindex("\nFROM "):]
    users = [v for k, v in _instructions("Dockerfile.server") if k == "USER"]
    assert users == ["forgeyard"] and "useradd --system --uid 10001" in final
    assert "DATA_DIR=/data" in final and "HOST=0.0.0.0" in final and "PORT=7860" in final
    assert "VOLUME /data" in final and "EXPOSE 7860" in final and "chown forgeyard:forgeyard /data" in final
    # The volume's ownership comes from the image only if /data is set up before VOLUME.
    assert final.index("chown forgeyard:forgeyard /data") < final.index("VOLUME /data")
    assert 'CMD ["python", "-m", "forgeyard.install.healthcheck"]' in final and "HEALTHCHECK " in final
    assert 'CMD ["python", "main.py"]' in final
    assert "COPY --from=web /src/forgeyard/static ./forgeyard/static" in final and "COPY docs/guide ./docs/guide" in final
    for label in ('org.opencontainers.image.source="https://github.com/xianhong1208/forgeyard"',
                  'org.opencontainers.image.licenses="Apache-2.0"', 'org.opencontainers.image.version="${VERSION}"',
                  'org.opencontainers.image.revision="${REVISION}"'):
        assert label in final, label
    # A new revision must not invalidate the expensive layers: VERSION and REVISION are declared last.
    assert final.index("ARG REVISION") > final.index("COPY --from=web")


def test_fy_img_004_the_build_context_has_what_both_images_copy_and_no_local_build():
    """FY-IMG-004: everything the two Dockerfiles COPY from the context passes .dockerignore; a local
    web build (forgeyard/static), node_modules and caches never do."""
    for name in ("Dockerfile.server", "Dockerfile.agent"):
        for kind, value in _instructions(name):
            if kind != "COPY" or value.startswith("--from"):
                continue
            for src in value.split()[:-1]:
                path = src.rstrip("/")
                assert (ROOT / path).exists(), (name, path)
                assert _context_allows(path), (name, path)
    for path in ("forgeyard/static/index.html", "web/node_modules/react/index.js", "web/dist/index.html",
                 "forgeyard/__pycache__/cli.cpython-313.pyc", "data/forgeyard.db", ".env", "tests/test_images.py"):
        assert not _context_allows(path), path
    for path in ("forgeyard/cli.py", "web/src/main.tsx", "docs/guide/how-to.md", "uv.lock"):
        assert _context_allows(path), path


def test_fy_img_005_the_agent_image_builds_for_arm64_without_rocm_and_has_labels():
    """FY-IMG-005: the agent image's ROCm helpers (libdrm-amdgpu, the libdrm_amdgpu.so link) are
    installed only on amd64 (ROCm is x86_64 only); tini and the workload account on every platform;
    NVML comes from the wheel's [nvidia] extra on both; OCI labels like the server's."""
    text = (ROOT / "Dockerfile.agent").read_text()
    run = text[text.index('arch="${TARGETARCH'):text.index("COPY --from=dockercli")]
    branch = run[run.index('if [ "$arch" = "amd64" ]'):run.index("fi;")]
    assert "libdrm-amdgpu1" in branch and "ln -s libdrm_amdgpu.so.1" in branch and 'test -n "$amdgpu"' in branch
    outside = run.replace(branch, "")
    assert "libdrm" not in outside and "tini" in outside and "useradd --system --uid 990" in outside
    assert "ARG TARGETARCH" in text and "[nvidia]" in text
    for label in ('org.opencontainers.image.source="https://github.com/xianhong1208/forgeyard"',
                  'org.opencontainers.image.licenses="Apache-2.0"', 'org.opencontainers.image.revision="${REVISION}"'):
        assert label in text, label
    assert text.index("ARG REVISION") > text.index("COPY --from=venv")


def test_fy_img_006_the_health_check_asks_the_containers_own_server(monkeypatch):
    """FY-IMG-006: the HEALTHCHECK command asks /api/health on the loopback with the server's own
    HOST/PORT/TLS settings: 0 while a real HTTPS server answers (certificate not verified: loopback,
    no credential), 1 when nothing listens; plain http with TLS=off; never through a proxy."""
    assert healthcheck.url({}) == "https://127.0.0.1:7860/api/health"
    assert healthcheck.url({"HOST": "0.0.0.0", "PORT": "8443"}) == "https://127.0.0.1:8443/api/health"
    assert healthcheck.url({"HOST": "::", "TLS": "off"}) == "http://[::1]:7860/api/health"
    assert healthcheck.url({"HOST": "10.0.0.5", "TLS": "False"}) == "http://10.0.0.5:7860/api/health"
    port = free_port()
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", str(port))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")  # a proxy that is not there
    get_settings.cache_clear()
    with LiveTLS(port):
        assert healthcheck.check(f"https://127.0.0.1:{port}/api/health")
        assert healthcheck.main() == 0
        assert not healthcheck.check(f"https://127.0.0.1:{port}/api/no-such-thing")
    dead = free_port()
    monkeypatch.setenv("PORT", str(dead))
    assert healthcheck.main() == 1
    # As the image runs it: a module, standard library only.
    r = subprocess.run([sys.executable, "-m", "forgeyard.install.healthcheck"], cwd=ROOT, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "PORT": str(dead), "HOST": "127.0.0.1"}, timeout=30)
    assert r.returncode == 1 and f"https://127.0.0.1:{dead}/api/health" in r.stderr


# ------------------------------------------------------------------ the release workflow
def test_fy_pub_001_tags_and_when_to_push():
    """FY-PUB-001: a release pushes X.Y.Z, X.Y and latest (a pre-release only its own version), and
    only for its own tag with the secrets and the namespace set; otherwise it says why and builds
    without pushing. A tag that is not v<version> is an error."""
    plan = _plan_module()
    assert plan.image_tags("1.2.0") == ["1.2.0", "1.2", "latest"]
    assert plan.image_tags("1.3.0rc1") == ["1.3.0rc1"]
    full = plan.plan("1.2.0", "forgeyard", "tag", "v1.2.0", True)
    assert full["push"] == "true" and full["reason"] == ""
    assert full["agent_tags"] == ("docker.io/forgeyard/forgeyard-agent:1.2.0,docker.io/forgeyard/forgeyard-agent:1.2,"
                                  "docker.io/forgeyard/forgeyard-agent:latest")
    assert full["server_repository"] == "docker.io/forgeyard/forgeyard"
    assert full["server_tags"].split(",")[0] == "docker.io/forgeyard/forgeyard:1.2.0"
    no_secrets = plan.plan("1.2.0", "forgeyard", "tag", "v1.2.0", False)
    assert no_secrets["push"] == "false" and no_secrets["reason"].startswith("secrets not configured, skipped push")
    assert "DOCKERHUB_USERNAME and DOCKERHUB_TOKEN" in no_secrets["reason"]
    no_ns = plan.plan("1.2.0", "", "tag", "v1.2.0", True)
    assert no_ns["push"] == "false" and "DOCKERHUB_NAMESPACE is empty" in no_ns["reason"]
    assert no_ns["agent_repository"] == "docker.io/example/forgeyard-agent"  # placeholder, never pushed
    branch = plan.plan("1.2.0", "forgeyard", "branch", "dev", True)
    assert branch["push"] == "false" and "not a release tag" in branch["reason"]
    # A branch without secrets (workflow_dispatch before the secrets exist) says the secrets first.
    assert plan.plan("1.2.0", "", "branch", "dev", False)["reason"].startswith("secrets not configured")
    with pytest.raises(plan.PlanError, match="expected v1.2.0"):
        plan.plan("1.2.0", "forgeyard", "tag", "v1.1.0", True)


def test_fy_pub_002_the_plan_script_as_the_workflow_runs_it():
    """FY-PUB-002: `python3 scripts/publish_plan.py` with only the standard library (no venv) prints
    key=value lines for $GITHUB_OUTPUT with this version and the namespace from images.py, and fails
    on a wrong tag."""
    env = {"PATH": "/usr/bin:/bin", "GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "dev", "HAVE_SECRETS": "false"}
    r = subprocess.run([sys.executable, "-I", "scripts/publish_plan.py"], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = dict(line.split("=", 1) for line in r.stdout.splitlines())
    assert out["version"] == __version__ and out["push"] == "false"
    assert out["reason"].startswith("secrets not configured, skipped push")
    ns = images.DOCKERHUB_NAMESPACE or "example"
    assert out["agent_repository"] == f"docker.io/{ns}/forgeyard-agent"
    r = subprocess.run([sys.executable, "-I", "scripts/publish_plan.py"], cwd=ROOT, capture_output=True, text=True,
                       env={**env, "GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v0.0.1"}, timeout=30)
    assert r.returncode == 1 and "::error" in r.stderr


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def _uses(wf: dict) -> list[str]:
    return [s["uses"] for job in wf["jobs"].values() for s in job["steps"] if "uses" in s]


def test_fy_pub_003_the_publish_workflow():
    """FY-PUB-003: publish-images.yml runs on v* tags and by hand, with read-only permissions; it
    builds both images for linux/amd64 and linux/arm64 with provenance (max) and an SBOM, pushes only
    when the plan says so, logs in only then, smoke-tests the amd64 images and writes the digests to
    the job summary. Every docker/* action is pinned by a full commit SHA."""
    raw = (ROOT / ".github" / "workflows" / "publish-images.yml").read_text()
    wf = _workflow("publish-images.yml")
    on = wf[True]  # YAML 1.1 reads the key `on` as True
    assert on["push"]["tags"] == ["v*"] and "workflow_dispatch" in on
    assert wf["permissions"] == {"contents": "read"}
    assert wf["env"]["PLATFORMS"] == "linux/amd64,linux/arm64"
    steps = wf["jobs"]["publish"]["steps"]
    by_id = {s["id"]: s for s in steps if "id" in s}
    for sid, file in (("agent", "Dockerfile.agent"), ("server", "Dockerfile.server")):
        w = by_id[sid]["with"]
        assert w["file"] == file and w["platforms"] == "${{ env.PLATFORMS }}"
        assert w["provenance"] == "mode=max" and w["sbom"] is True
        assert w["push"] == "${{ steps.plan.outputs.push == 'true' }}"
    login = next(s for s in steps if s.get("uses", "").startswith("docker/login-action"))
    assert login["if"] == "steps.plan.outputs.push == 'true'"
    assert login["with"] == {"username": "${{ secrets.DOCKERHUB_USERNAME }}", "password": "${{ secrets.DOCKERHUB_TOKEN }}"}
    assert "scripts/publish_plan.py" in raw and "::notice title=Images not pushed::" in raw
    assert "scripts/agent_image_smoke.sh" in raw and "scripts/server_image_smoke.sh" in raw
    assert "GITHUB_STEP_SUMMARY" in raw and "imagetools inspect" in raw
    for wf_name in ("publish-images.yml", "ci.yml"):
        for use in _uses(_workflow(wf_name)):
            if use.startswith("docker/"):
                assert re.fullmatch(r"docker/[\w-]+@[0-9a-f]{40}", use), use
    assert re.search(r"docker/build-push-action@[0-9a-f]{40} # v\d+\.\d+\.\d+", raw)


def test_fy_pub_004_ci_keeps_the_required_checks_and_builds_both_images():
    """FY-PUB-004: the jobs the repository ruleset requires keep their names (backend,
    backend-postgres, frontend, agent-image); server-image builds and smoke-tests the server image
    and lints both Dockerfiles; images-arm64 builds both for linux/arm64."""
    wf = _workflow("ci.yml")
    assert {"backend", "backend-postgres", "frontend", "agent-image", "server-image", "images-arm64"} <= set(wf["jobs"])
    raw = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "docker build -f Dockerfile.server" in raw and "scripts/server_image_smoke.sh forgeyard:ci" in raw
    assert re.search(r"hadolint/hadolint:v[\d.]+@sha256:[0-9a-f]{64}", raw)
    arm = wf["jobs"]["images-arm64"]["steps"]
    files = {s["with"]["file"]: s["with"]["platforms"] for s in arm if s.get("uses", "").startswith("docker/build-push-action")}
    assert files == {"Dockerfile.agent": "linux/arm64", "Dockerfile.server": "linux/arm64"}


def test_fy_pub_005_the_docs_name_the_images_by_the_namespace_in_the_code():
    """FY-PUB-005: the guide, the READMEs and the design notes write the image names as
    docker.io/<namespace>/… while no namespace is set; once DOCKERHUB_NAMESPACE is filled in, this
    fails until every `<namespace>` is replaced by it, so the docs never name a repository that
    does not exist or the wrong one."""
    docs = [ROOT / "README.md", ROOT / "README.zh-TW.md", ROOT / "CONTRIBUTING.md", ROOT / ".env.example",
            ROOT / "docs" / "design.md", ROOT / "docs" / "design.zh-TW.md", *sorted((ROOT / "docs" / "guide").rglob("*.md"))]
    ns = images.DOCKERHUB_NAMESPACE
    named = 0
    for f in docs:
        text = f.read_text()
        if ns:
            assert "<namespace>" not in text, f"{f}: replace <namespace> with {ns}"
            named += f"docker.io/{ns}/forgeyard" in text
        else:
            for ref in re.findall(r"docker\.io/[^/\s`]+/forgeyard", text):
                assert ref == "docker.io/<namespace>/forgeyard", (f, ref)
            named += "docker.io/<namespace>/forgeyard" in text
    assert named >= 10, named


def test_fy_pub_006_the_namespace_is_a_docker_hub_account_name():
    """FY-PUB-006: DOCKERHUB_NAMESPACE goes into every published image name and every `docker run` the
    Machines page writes: when set it is a Docker Hub account or organisation name (4 to 30 lower-case
    letters and digits), so a typo stops here and not at the first release's push."""
    ns = images.DOCKERHUB_NAMESPACE
    if ns:
        assert re.fullmatch(r"[a-z0-9]{4,30}", ns), ns
        assert images.published_repository(images.AGENT_REPOSITORY) == f"docker.io/{ns}/forgeyard-agent"
