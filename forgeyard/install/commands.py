"""The ways to add a machine, as the Machines page shows them with a fresh join token:

    install   one line: check the CA against the fingerprint, then run the server's install.sh
    docker    one `docker run` of the agent image (NVIDIA, AMD or CPU only)
    manual    a config file and the agent from a Forgeyard checkout

All of them pin the same CA fingerprint, so a machine in between is refused before the token is
sent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from forgeyard import __version__
from forgeyard.agent import config as agent_config
from forgeyard.install import wheel

TEMPLATE = Path(__file__).with_name("install.sh")
IMAGE = "forgeyard-agent"
MODEL_CACHE = "/srv/forgeyard-models"
# What install.sh may be told about the server: a plain https URL, nothing a shell would expand.
_SAFE_URL = re.compile(r"^https://([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?$")


def safe_server(url: str) -> bool:
    return bool(_SAFE_URL.match(url))


def install_script(server: str) -> str:
    """install.sh for this server: its address, version and the wheel's checksum filled in."""
    if not safe_server(server):
        raise ValueError(f"not a plain https:// address: {server!r}")
    name, _, sha = wheel.cached()
    out = TEMPLATE.read_text()
    for key, value in (("SERVER", server), ("VERSION", __version__), ("WHEEL", name), ("WHEEL_SHA256", sha)):
        out = out.replace(f"@@{key}@@", value)
    return out


def _hex(fingerprint: str) -> str:
    return fingerprint.removeprefix("sha256:")


def install_command(server: str, token: str, fingerprint: Optional[str]) -> Optional[str]:
    """The one-line install. With a fingerprint, the first download checks itself: the CA is
    fetched without verification, converted back to DER with sed and base64 (no openssl needed)
    and compared with the fingerprint by sha256sum -c; only then is install.sh fetched, verified
    against that CA."""
    if not server.startswith("https://"):
        return None  # install.sh is only served over HTTPS
    if not fingerprint:
        return f"curl -fsS {server}/install.sh | sudo sh -s -- --join {token}"
    return (f"curl -fsSk {server}/api/tls/ca.pem -o forgeyard-ca.pem && \\\n"
            f"  sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \\\n"
            f"  echo \"{_hex(fingerprint)}  forgeyard-ca.der\" | sha256sum -c && \\\n"
            f"  curl -fsS --cacert forgeyard-ca.pem {server}/install.sh | \\\n"
            f"  sudo sh -s -- --join {token} --ca-fingerprint {fingerprint}")


def docker_build_command() -> str:
    return f"docker build -f Dockerfile.agent -t {IMAGE}:{__version__} ."


def docker_command(server: str, token: str, fingerprint: Optional[str], kind: str = "nvidia") -> str:
    """`docker run` for the agent image. Host network: the agent's key guard listens on the
    replicas' published ports and reaches each engine on the host's 127.0.0.1. The model cache is
    mounted at the same path inside as outside: the agent hands that path to the host's Docker
    when it starts a model container."""
    lines = ["docker run -d --name forgeyard-agent --restart always --network host"]
    if kind == "nvidia":
        lines.append("--gpus all")
    elif kind == "amd":
        lines.append("--device /dev/kfd --device /dev/dri --group-add video --group-add render")
        lines.append("-v /opt/rocm:/opt/rocm:ro")
    lines += ["-v /var/run/docker.sock:/var/run/docker.sock",
              "-v forgeyard-agent:/var/lib/forgeyard-agent",
              f"-v {MODEL_CACHE}:{MODEL_CACHE}",
              f"-e FORGEYARD_SERVER={server}"]
    if fingerprint:
        lines.append(f"-e FORGEYARD_CA_FINGERPRINT={fingerprint}")
    lines += [f"-e FORGEYARD_JOIN_TOKEN={token}", f"-e FORGEYARD_MODEL_CACHE={MODEL_CACHE}",
              f"{IMAGE}:{__version__}"]
    return " \\\n  ".join(lines)


def config_file(server: str, fingerprint: Optional[str]) -> str:
    """A short agent.toml for the manual way; `forgeyard agent init` writes the full one."""
    values = {"server": server, "ca_fingerprint": fingerprint, "workloads.user": "forgeyard-run",
              "models.cache": MODEL_CACHE}
    lines = ["# /etc/forgeyard/agent.toml (`forgeyard agent init` writes a complete one, with every setting)"]
    for key in ("server", "ca_fingerprint"):
        if values[key]:
            lines.append(f"{key} = {agent_config.toml_value(values[key])}")
    lines += ["", "[workloads]", 'user = "forgeyard-run"   # process workloads run as this account',
              "", "[models]", f"cache = {agent_config.toml_value(MODEL_CACHE)}"]
    return "\n".join(lines) + "\n"


def manual_commands(token: str) -> list[str]:
    run = "uv run --no-sync python -m forgeyard agent"
    return [f"{run} join --join-token {token}", run]


def all_commands(server: str, token: str, fingerprint: Optional[str]) -> dict:
    return {
        "install_command": install_command(server, token, fingerprint),
        "docker_build_command": docker_build_command(),
        "docker_commands": {kind: docker_command(server, token, fingerprint, kind) for kind in ("nvidia", "amd", "cpu")},
        "config_file": config_file(server, fingerprint),
        "manual_commands": manual_commands(token),
        "version": __version__,
    }
