"""Updating an agent (1.4.2, #121 and #122): whether a machine's agent is behind the server, and the complete
command that brings it to the server's version, built from what the agent reported about how it runs
(agent/how_it_runs.py, stored as `nodes.runtime_info["install"]`).

    container   pull the server's agent image (or carry it: `docker save | ssh … docker load` for a machine that
                gets its files through the server), then `docker rm -f` and the same `docker run` with the new image
    install.sh  the one-line install again, without a join token: it upgrades in place and keeps the settings
    manual      a checkout: check out the server's version and sync; then restart the agent
    otherwise   (an agent before 1.4.2, a container that is not the agent image, nothing could be read) say so

Every value the agent reported is checked again here and shell-quoted: the command is pasted into a root shell.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Optional

from forgeyard import __version__
from forgeyard.db import Node
from forgeyard.install import images
from forgeyard.install.commands import NVIDIA_DEVICES_SHELL, nvidia_loop_finds
from forgeyard.machines import protocol as p

GUIDE = "/guide/en/how-to#update-an-agent"
ENGINES = ("docker", "podman")
DEFAULT_NAME = "forgeyard-agent"
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
HOST_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
GPUS = re.compile(r"^(all|\d{1,3}|device=[A-Za-z0-9,:_-]{1,200})$")
RESTART = re.compile(r"^(always|unless-stopped|on-failure(:\d{1,4})?)$")
IMAGE_FILE = "forgeyard-agent.tar.gz"
MAX_LIST = 64
# 1.4.5 (#202): an NVIDIA agent container also gets its device nodes with --device, next to --gpus. The NVIDIA runtime
# hook behind --gpus sets up the container's device access without Docker or systemd knowing, and the host's next
# `systemctl daemon-reload` takes it away from new processes (agent/gpu_check.py); a --device Docker records, so it
# stays. 1.5.4: an agent that has every GPU (`--gpus all`) gets the host's nodes listed by the shell when the command
# runs (commands.NVIDIA_DEVICES_SHELL, as Add a machine does): a GPU installed after the agent started is passed too,
# and the command is the same on a machine with 1 GPU or 8. The loop expands to plain --device words before
# `docker run` starts, so Docker records each node and the #202 property holds. An agent limited to some GPUs
# (`--gpus "device=0,2"`, a count, NVIDIA_VISIBLE_DEVICES) keeps the nodes it reported, by name: the loop would give it
# every card. A reported node the loop would not find (another /dev/nvidia… name) is kept by name either way.
NVIDIA_NODE = re.compile(r"^/dev/nvidia[A-Za-z0-9_-]{1,32}$")


def version_tuple(v: Optional[str]) -> tuple[int, ...]:
    m = re.match(r"\d+(?:\.\d+)*", (v or "").strip().lstrip("v"))
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


# ------------------------------------------------------------------ behind (#122)
def expected_features(node: Node) -> set[str]:
    """The features an agent of the server's version declares on a machine like this one (agent/client.py
    hello): every agent with workloads, more with a model cache, more with a container engine."""
    have = set(node.features or [])
    want: set[str] = set()
    if p.FEATURE_WORKLOADS not in have:
        return want  # an inventory-only agent (no workloads): nothing to compare
    want |= {p.FEATURE_WORKLOADS, p.FEATURE_REPLICA_KEYS, p.FEATURE_REPLICA_GUARD, p.FEATURE_IMAGE_IDS,
             p.FEATURE_ENGINE_ZIP, p.FEATURE_WORKLOAD_GUARD, p.FEATURE_GUARD_TLS}
    models = p.FEATURE_MODELS in have
    if models:
        want |= {p.FEATURE_MODELS, p.FEATURE_CACHE_ADMIN, p.FEATURE_PREFETCH, p.FEATURE_RELAY, p.FEATURE_RELAY_SOURCE}
    if ((node.runtime_info or {}).get("container") or {}).get("ok"):
        want |= {p.FEATURE_IMAGE_LOAD, p.FEATURE_IMAGE_SAVE, p.FEATURE_IMAGE_REMOVE}
        if models:
            want.add(p.FEATURE_RELAY_IMAGES)
    return want


def behind(node: Node, server_version: str = __version__) -> Optional[dict]:
    """Why this machine's agent should be updated, or None: older than the server, an older protocol, or the
    server's version but without a feature such an agent declares (a build from older code)."""
    if node.status == "revoked":
        return None
    theirs, mine = version_tuple(node.agent_version), version_tuple(server_version)
    if theirs and mine and theirs > mine:
        return None  # newer than the server: nothing to update to
    older = bool(theirs and mine and theirs < mine)
    old_protocol = node.protocol is not None and node.protocol < p.PROTOCOL_VERSION
    missing = sorted(expected_features(node) - set(node.features or [])) if node.agent_version else []
    if not (older or old_protocol or missing):
        return None
    return {"agent": node.agent_version, "server": server_version, "older": older, "old_protocol": old_protocol,
            "missing": missing}


# ------------------------------------------------------------------ rendering
def _clean(value: Any, limit: int = 1024) -> Optional[str]:
    """A reported string fit to quote into a command, or None (not a string, too long, control characters)."""
    if not isinstance(value, str) or not value or len(value) > limit or CONTROL.search(value):
        return None
    return value


def _q(value: str) -> str:
    return shlex.quote(value)


def _list(value: Any) -> list:
    return value[:MAX_LIST] if isinstance(value, list) else []


def _host(node: Node) -> str:
    for candidate in (node.hostname, node.name):
        if isinstance(candidate, str) and HOST_NAME.match(candidate):
            return candidate
    return "HOST"


def run_flags(c: dict, notes: list[str]) -> list[str]:
    """The options of the reported `docker run`, quoted, without the image and its arguments. Anything that
    does not check out is left out and named in `notes`."""
    skipped: list[str] = []
    flags = ["-d"]
    name = c.get("name")
    flags.append(f"--name {name if isinstance(name, str) and CONTAINER_NAME.match(name) else DEFAULT_NAME}")
    restart = c.get("restart")
    if isinstance(restart, str) and RESTART.match(restart):
        flags.append(f"--restart {restart}")
    network = _clean(c.get("network"), 128)
    if network:
        flags.append(f"--network {_q(network)}")
    gpus = c.get("gpus")
    if isinstance(gpus, str) and GPUS.match(gpus):
        flags.append(f"--gpus {_q(chr(34) + gpus + chr(34)) if gpus.startswith('device=') else gpus}")
    runtime = _clean(c.get("runtime"), 64)
    if runtime:
        flags.append(f"--runtime {_q(runtime)}")
    nvidia = (isinstance(gpus, str) and GPUS.match(gpus) is not None) or runtime == "nvidia"
    loop = nvidia and every_gpu(c)
    if nvidia:
        flags += nvidia_device_flags(c, notes, loop)
    for d in _list(c.get("devices")):
        host = _clean((d or {}).get("host"), 256) if isinstance(d, dict) else None
        if not host:
            skipped.append("a device")
            continue
        spec = host
        inside = _clean(d.get("container"), 256)
        perms = d.get("permissions")
        if loop and nvidia_loop_finds(host) and not inside and not perms:
            continue  # given by an earlier update command (or by hand): the shell line passes it, with any new GPU
        if inside or (isinstance(perms, str) and re.fullmatch(r"[rwm]{1,3}", perms)):
            spec += f":{inside or host}"
            if isinstance(perms, str) and re.fullmatch(r"[rwm]{1,3}", perms):
                spec += f":{perms}"
        flags.append(f"--device {_q(spec)}")
    for g in _list(c.get("group_add")):
        g = _clean(str(g) if isinstance(g, int) else g, 64)
        if g:
            flags.append(f"--group-add {_q(g)}")
    if c.get("privileged") is True:
        flags.append("--privileged")
    for key, flag in (("cap_add", "--cap-add"), ("security_opt", "--security-opt"), ("add_hosts", "--add-host"),
                      ("ports", "-p")):
        for v in _list(c.get(key)):
            v = _clean(v, 256)
            if v:
                flags.append(f"{flag} {_q(v)}")
    for key in ("pid", "ipc"):
        if c.get(key) == "host":
            flags.append(f"--{key} host")
    user = _clean(c.get("user"), 64)
    if user:
        flags.append(f"--user {_q(user)}")
    for m in _list(c.get("mounts")):
        if not isinstance(m, dict):
            continue
        target = _clean(m.get("target"), 1024)
        if m.get("type") == "tmpfs" and target:
            flags.append(f"--tmpfs {_q(target)}")
            continue
        source = _clean(m.get("source"), 1024)
        if m.get("type") not in ("bind", "volume") or not source or not target or ":" in source + target:
            skipped.append(f"a mount on {target}" if target else "a mount")
            continue
        options = _clean(m.get("options"), 64)
        flags.append(f"-v {_q(':'.join([source, target] + ([options] if options else [])))}")
    exports: list[str] = []
    for e in _list(c.get("env")):
        name = (e or {}).get("name") if isinstance(e, dict) else None
        if not isinstance(name, str) or not ENV_NAME.match(name):
            skipped.append("an environment variable")
            continue
        if name == "FORGEYARD_JOIN_TOKEN":
            continue  # used once to join; the machine's credential is in its state volume
        if e.get("hidden"):
            exports.append(name)
            flags.append(f'-e {name}="${name}"')
            continue
        if "value" not in e or e["value"] is None:
            flags.append(f"-e {name}")
            continue
        value = e["value"] if isinstance(e["value"], str) and not CONTROL.search(e["value"]) else None
        if value is None or len(value) > 1024:
            skipped.append(f"the value of {name}")
            continue
        flags.append(f"-e {_q(f'{name}={value}')}")
    if exports:
        notes.append("The agent does not report secrets. Before you run it, set "
                     + ", ".join(exports) + " in your shell (export NAME=…), or the new container starts without "
                     + ("it." if len(exports) == 1 else "them."))
    if skipped:
        notes.append("Left out, as the agent reported them in a form that cannot be repeated safely: "
                     + ", ".join(skipped) + ". Add them by hand if the agent needs them.")
    return flags


def every_gpu(c: dict) -> bool:
    """Whether the agent container has every GPU of the host: `--gpus all`, or `--runtime nvidia` without --gpus and
    without an NVIDIA_VISIBLE_DEVICES that names some (one the image sets the same way is not reported: all)."""
    gpus = c.get("gpus")
    if gpus == "all":
        return True
    if gpus or c.get("runtime") != "nvidia":
        return False
    visible = [e.get("value") for e in _list(c.get("env"))
               if isinstance(e, dict) and e.get("name") == "NVIDIA_VISIBLE_DEVICES"]
    return not visible or visible[-1] in (None, "all")


def nvidia_device_flags(c: dict, notes: list[str], loop: bool) -> list[str]:
    """`--device` for the NVIDIA device nodes (1.4.5, #202). Every GPU: the host's, listed by the shell when the
    command runs (1.5.4), plus any node the agent reported that the shell would not find. Some GPUs: the nodes the
    agent reported, by name, or a note when it reported none."""
    given = {d.get("host") for d in _list(c.get("devices")) if isinstance(d, dict)}
    nodes = list(dict.fromkeys(d for d in _list(c.get("nvidia_devices")) if isinstance(d, str) and NVIDIA_NODE.match(d)))
    if loop:
        return [NVIDIA_DEVICES_SHELL] + [f"--device {_q(d)}" for d in nodes if not nvidia_loop_finds(d) and d not in given]
    if nodes:
        return [f"--device {_q(d)}" for d in nodes if d not in given]
    if any(isinstance(h, str) and NVIDIA_NODE.match(h) for h in given):
        return []  # already passed by hand
    notes.append("Add --device for /dev/nvidiactl, /dev/nvidia-uvm and each /dev/nvidiaN of the GPUs it uses (ls "
                 "/dev/nvidia*), so that the agent keeps its GPUs when the host runs `systemctl daemon-reload`.")
    return []


def container_plan(node: Node, report: dict, image: images.AgentImage, version: str, offline: bool) -> dict:
    c = report.get("container") if isinstance(report.get("container"), dict) else {}
    engine = report.get("engine") if report.get("engine") in ENGINES else "docker"
    cli = f"sudo {engine}"
    notes: list[str] = []
    flags = run_flags(c, notes)
    name = flags[1].split(" ", 1)[1]
    devices = "host" if NVIDIA_DEVICES_SHELL in flags else ("listed" if any(
        f.startswith("--device /dev/nvidia") for f in flags) else None)
    args = [_q(a) for a in (_clean(x, 1024) for x in _list(c.get("args"))) if a]
    hidden = [h for h in _list(c.get("hidden_args")) if isinstance(h, str)]
    if hidden:
        notes.append("Arguments with secrets were not reported (" + ", ".join(sorted(set(hidden)))
                     + "); add them back after the image name if the agent still needs them.")
    if c.get("state_kept") is False:
        notes.append("The agent's state directory (its machine credential) is not on a volume or a host directory: a "
                     "new container would have to join again with a new token. Keep the old container until the new "
                     "one shows online, or add -v forgeyard-agent:/var/lib/forgeyard-agent and join again.")

    def replace(ref: str) -> str:
        run = " \\\n  ".join([f"{cli} run {flags[0]}", *flags[1:], " ".join([ref, *args])])
        return f"{cli} rm -f {name} && \\\n{run}"

    carry = (f"{cli} pull {image.reference} && \\\n"
             + (f"{cli} tag {image.reference} {image.local_tag} && \\\n" if image.reference != image.local_tag else "")
             + f"{cli} save {image.local_tag} | gzip | ssh USER@{_host(node)} 'gunzip -c | {cli} load'")
    steps: list[dict] = []
    alternative = None
    if not image.published:
        steps.append({"title": f"Build the agent image {version} from a Forgeyard checkout of v{version} "
                               "(nothing is published for this server)",
                      "command": f"git checkout v{version} && \\\n{cli} build -f Dockerfile.agent -t {image.local_tag} ."})
        steps.append({"title": f"On {node.name}: replace the container", "command": replace(image.local_tag)})
    elif offline:
        steps.append({"title": f"On a machine that can reach the registry: copy the image to {node.name} (replace USER, "
                               "and the host if you reach it by another name; or save the image to a file and "
                               "carry it)", "command": carry})
        steps.append({"title": f"On {node.name}: replace the container", "command": replace(image.local_tag)})
    else:
        steps.append({"title": f"On {node.name}", "command": f"{cli} pull {image.reference} && \\\n{replace(image.reference)}"})
        alternative = {"title": f"{node.name} cannot reach the registry?", "steps": [
            {"title": "On a machine that can reach it (replace USER, and the host if you reach it by another name; or "
                      "save the image to a file and carry it)",
             "command": carry},
            {"title": f"On {node.name}", "command": replace(image.local_tag)},
        ]}
    notes.append("Its models keep running while the agent is replaced"
                 + (f"; the volume keeps the machine ({node.name})." if c.get("state_kept") is not False else "."))
    return {"steps": steps, "alternative": alternative, "notes": notes, "nvidia_devices": devices}


def install_plan(node: Node, report: dict, server: str, fingerprint: Optional[str]) -> dict:
    from forgeyard.install.commands import safe_server, update_command

    reported = report.get("server")
    target = reported if isinstance(reported, str) and safe_server(reported) else server
    command = update_command(target, fingerprint)
    if command is None:
        return {"steps": [], "alternative": None,
                "notes": ["install.sh is served over HTTPS only, and this server answers over plain HTTP: update "
                          "the agent by hand (see the guide)."]}
    notes = ["It installs the server's version and keeps the settings and the machine's credential."]
    if report.get("service"):
        notes.append(f"It restarts the service {report['service']}; its models keep running.")
    return {"steps": [{"title": f"On {node.name}", "command": command}], "alternative": None, "notes": notes}


def manual_plan(node: Node, report: dict, version: str) -> dict:
    checkout = _clean(report.get("checkout"), 512)
    if checkout:
        return {"steps": [{"title": f"On {node.name}, in the Forgeyard checkout the agent runs from",
                           "command": f"cd {_q(checkout)} && \\\ngit fetch --tags && git checkout v{version} && \\\nuv sync"}],
                "alternative": None,
                "notes": ["Then restart the agent the way you started it; its models keep running."]}
    return {"steps": [], "alternative": None,
            "notes": ["This agent was installed by hand, not with install.sh or the agent image. Install the server's "
                      "version the same way, or switch to install.sh or Docker (see the guide)."]}


def plan(node: Node, *, server: str, fingerprint: Optional[str], image: images.AgentImage,
         version: str = __version__) -> dict:
    """What the machine page shows admins: the agent's and the server's versions, why it is behind, and the
    command that updates it."""
    report = (node.runtime_info or {}).get("install")
    report = report if isinstance(report, dict) else None
    offline = (node.fetch_via or "auto") == "server"
    out = {"machine_id": node.id, "name": node.name, "agent_version": node.agent_version, "server_version": version,
           "behind": behind(node, version), "method": report.get("method") if report else None,
           "known": bool(report and report.get("known")), "reason": None, "image": image.reference,
           "offline": offline, "steps": [], "alternative": None, "notes": [], "guide": GUIDE,
           # 1.5.4: how an NVIDIA agent's device nodes are passed: "host" (the shell lists the host's when the command
           # runs; the console explains it under the command), "listed" (by name: an agent limited to some GPUs), None
           "nvidia_devices": None}
    if report is None:
        out["reason"] = ("This agent does not say how it runs (agents before 1.4.2 do not). Update it by hand once, "
                         "as the guide shows; from then on this page shows the exact command.")
        return out
    if not report.get("known"):
        reason = _clean(report.get("reason"), 300) or "it could not tell how it runs"
        out["reason"] = f"The agent could not describe how it runs: {reason}. Update it as the guide shows."
        return out
    method = report.get("method")
    if method == "container":
        out.update(container_plan(node, report, image, version, offline))
    elif method == "install.sh":
        out.update(install_plan(node, report, server, fingerprint))
    elif method == "manual":
        out.update(manual_plan(node, report, version))
    else:
        out["known"] = False
        out["reason"] = "The agent could not describe how it runs. Update it as the guide shows."
    return out
