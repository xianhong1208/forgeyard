"""Updating an agent (1.4.2, #121 / #122): how the agent says it runs (agent/how_it_runs.py), what it leaves out,
the update command its machine's page shows (install/update.py), and when an agent is behind (FY-UPD-*)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeyard.agent import how_it_runs as h
from forgeyard.install import images, update
from forgeyard.machines.messages import HelloIn
from tests.conftest import add_user, sign_in
from tests.machines_helpers import connect, hello, joined, machine

FIX = Path(__file__).resolve().parent / "fixtures" / "agent_containers"
CRED = "fym_EXAMPLEcredentialEXAMPLEcredential00"
SECRET_BITS = ("fyj_", "fym_", "hf_", "ghp_", "EXAMPLE", "s3cret", CRED)


def inspect(kind: str) -> dict:
    return json.loads((FIX / f"{kind}.inspect.json").read_text())[0]


def image_inspect() -> dict:
    return json.loads((FIX / "agent.image-inspect.json").read_text())[0]


def report(kind: str, **over) -> dict:
    out = h.describe_container(inspect(kind), image_inspect(), credential=CRED)
    out["engine"] = "docker"
    out.update(over)
    return out


def node(rep=None, *, name="gpu-box", fetch_via="auto", version="1.4.0", features=(), runtime_info=None,
         status="online", protocol=1):
    info = dict(runtime_info or {})
    if rep is not None:
        info["install"] = rep
    return SimpleNamespace(id=7, name=name, hostname=name, runtime_info=info, fetch_via=fetch_via, status=status,
                           agent_version=version, protocol=protocol, features=list(features))


PUBLISHED = images.agent_image("", "1.4.2", namespace="example")
FP = "sha256:" + "3f9a" * 16


def plan(n, image=PUBLISHED, server="https://192.168.1.20:7860", fingerprint=FP):
    return update.plan(n, server=server, fingerprint=fingerprint, image=image, version="1.4.2")


# ------------------------------------------------------------------ the agent's side
def test_fy_upd_001_an_nvidia_agent_container_is_read_from_its_inspect():
    """FY-UPD-001: `docker inspect` of an NVIDIA agent container: name, network, restart, `--gpus all` from the
    DeviceRequests, the three mounts, and only the variables given with -e (the image's own, GPG_KEY among them,
    are left out); the join token and HF_TOKEN by name only."""
    r = report("nvidia")
    c = r["container"]
    assert (r["method"], r["known"]) == ("container", True)
    assert (c["name"], c["network"], c["restart"], c["gpus"], c["runtime"]) == (
        "forgeyard-agent", "host", "always", "all", None)
    assert c["mounts"] == [
        {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
        {"type": "volume", "source": "forgeyard-agent", "target": "/var/lib/forgeyard-agent"},
        {"type": "bind", "source": "/srv/forgeyard-models", "target": "/srv/forgeyard-models"}]
    assert [e["name"] for e in c["env"]] == ["FORGEYARD_SERVER", "FORGEYARD_CA_FINGERPRINT", "FORGEYARD_JOIN_TOKEN",
                                             "FORGEYARD_MODEL_CACHE", "FORGEYARD_NAME", "HF_TOKEN"]
    hidden = {e["name"] for e in c["env"] if e.get("hidden")}
    assert hidden == {"FORGEYARD_JOIN_TOKEN", "HF_TOKEN"}
    assert c["state_kept"] is True and c["devices"] == [] and c["group_add"] == [] and c["args"] == []
    text = json.dumps(r)
    for bit in SECRET_BITS:
        assert bit not in text, bit


def test_fy_upd_002_an_amd_agent_container_keeps_its_devices_and_group_ids():
    """FY-UPD-002: an AMD agent: /dev/kfd and /dev/dri, the numeric group ids, /opt/rocm read-only, the port range."""
    c = report("amd")["container"]
    assert c["devices"] == [{"host": "/dev/kfd"}, {"host": "/dev/dri"}]
    assert c["group_add"] == ["44", "110"]
    assert {"type": "bind", "source": "/opt/rocm", "target": "/opt/rocm", "options": "ro"} in c["mounts"]
    assert {"name": "FORGEYARD_PORT_RANGE", "value": "33000-33002"} in c["env"]
    assert c["gpus"] is None


@pytest.mark.parametrize("requests,gpus,cdi", [
    ([{"Driver": "", "Count": -1, "DeviceIDs": None, "Capabilities": [["gpu"]]}], "all", []),
    ([{"Driver": "nvidia", "Count": 0, "DeviceIDs": ["0", "2"], "Capabilities": [["gpu"]]}], "device=0,2", []),
    ([{"Driver": "", "Count": 2, "DeviceIDs": None, "Capabilities": [["gpu", "utility"]]}], "2", []),
    ([{"Driver": "cdi", "DeviceIDs": ["nvidia.com/gpu=all"]}], None, ["nvidia.com/gpu=all"]),
    ([{"Driver": "", "Count": -1, "Capabilities": [["tpu"]]}], None, []),
    (None, None, []),
])
def test_fy_upd_003_gpu_requests(requests, gpus, cdi):
    """FY-UPD-003: DeviceRequests as `--gpus all`, `"device=0,2"`, a count, or CDI devices; a non-GPU request is not a GPU."""
    assert h.gpus(requests) == (gpus, cdi)


def test_fy_upd_004_secrets_never_leave_the_agent():
    """FY-UPD-004: by name, by value (a Forgeyard token, a GitHub token, a URL with a password, the machine's own
    credential under an innocent name) and as arguments (`--join-token X`, `--hf-token=X`): named, never sent."""
    env = ["API_KEY=abc", "DB_PASSWORD=s3cret", "AWS_SECRET_ACCESS_KEY=s3cret", "MY_SESSION=x", "PLAIN=ok",
           "INNOCENT=prefix-" + CRED, "UPSTREAM=https://bob:s3cret@proxy.example:3128", "GH=ghp_EXAMPLE",
           "OTHER=fyk_EXAMPLE", "NO_VALUE", "1BAD=x", "LONG=" + "x" * 2000]
    out = h.environment(env, [], credential=CRED)
    assert out == [{"name": "API_KEY", "hidden": "secret"}, {"name": "DB_PASSWORD", "hidden": "secret"},
                   {"name": "AWS_SECRET_ACCESS_KEY", "hidden": "secret"}, {"name": "MY_SESSION", "hidden": "secret"},
                   {"name": "PLAIN", "value": "ok"}, {"name": "INNOCENT", "hidden": "secret"},
                   {"name": "UPSTREAM", "hidden": "secret"}, {"name": "GH", "hidden": "secret"},
                   {"name": "OTHER", "hidden": "secret"}, {"name": "NO_VALUE", "value": None},
                   {"name": "LONG", "hidden": "too long"}]
    args, hidden = h.arguments(["--name", "box", "--join-token", "fyj_EXAMPLE", "--hf-token=hf_EXAMPLE", "-v",
                                "--note", "has " + CRED], [], credential=CRED)
    assert args == ["--name", "box", "-v", "--note"]
    assert hidden == ["--join-token", "--hf-token", "(an argument)"]
    assert h.arguments(["agent"], ["agent"]) == ([], [])  # the image's own arguments are not repeated


def test_fy_upd_005_which_container_and_whether_it_is_the_agent_image():
    """FY-UPD-005: the container id from mountinfo (Docker, Podman) or cgroup v1, none on a host; a container that
    is not the agent image (someone's own) is reported as such, without its settings."""
    cid = "a1" * 32
    assert h.own_container_id((FIX / "mountinfo.docker.txt").read_text()) == cid
    podman = f"77 1 0:5 /containers/storage/overlay-containers/{'f0' * 32}/userdata/hostname /etc/hostname rw - tmpfs"
    assert h.own_container_id(podman) == "f0" * 32
    assert h.own_container_id("", f"12:pids:/docker/{'b7' * 32}\n") == "b7" * 32
    assert h.own_container_id("25 1 8:1 / / rw - ext4 /dev/sda1 rw", "0::/user.slice/session-3.scope\n") is None
    other = inspect("nvidia")
    other["Config"].update(Labels={}, Entrypoint=["python3"], Cmd=["-m", "http.server"], Image="python:3.13")
    r = h.describe_container(other, None)
    assert (r["known"], r.get("foreign"), r["image"]) == (False, True, "python:3.13") and "container" not in r


def test_fy_upd_006_the_state_volume_is_kept_even_when_unnamed():
    """FY-UPD-006: an agent started without -v for its state has the image's anonymous volume: reported by its
    name so the new container mounts the same one; with no volume at all the report says so."""
    c = report("cpu")["container"]
    assert {"type": "volume", "source": "e5" * 32, "target": "/var/lib/forgeyard-agent"} in c["mounts"]
    assert c["state_kept"] is True and c["restart"] == "unless-stopped" and c["name"] == "fy-agent"
    bare = inspect("cpu")
    bare["Mounts"] = [m for m in bare["Mounts"] if m["Destination"] != "/var/lib/forgeyard-agent"]
    assert h.describe_container(bare, image_inspect())["container"]["state_kept"] is False
    # a state directory inside a mounted directory is kept too; one next to it is not
    nested = h.describe_container(inspect("cpu"), image_inspect(), state_dir="/srv/forgeyard-models/agent-state")
    assert nested["container"]["state_kept"] is True
    beside = h.describe_container(bare, image_inspect(), state_dir="/srv/forgeyard-models-state")
    assert beside["container"]["state_kept"] is False


def _proc(root: Path, mountinfo: str, cgroup: str = "0::/\n", dockerenv: bool = True) -> Path:
    (root / "proc" / "self").mkdir(parents=True)
    (root / "proc" / "self" / "mountinfo").write_text(mountinfo)
    (root / "proc" / "self" / "cgroup").write_text(cgroup)
    if dockerenv:
        (root / ".dockerenv").write_text("")
    return root


def test_fy_upd_007_collect_reads_its_own_container_through_the_engine(tmp_path):
    """FY-UPD-007: in a container, collect() inspects its own container and image with the engine CLI; a failing
    or missing engine, or anything raising, gives a reason instead of an exception."""
    root = _proc(tmp_path / "root", (FIX / "mountinfo.docker.txt").read_text())
    calls = []

    def run(argv):
        calls.append(argv)
        data = inspect("nvidia") if argv[1] == "inspect" else image_inspect()
        return subprocess.CompletedProcess(argv, 0, json.dumps([data]), "")

    out = h.collect(engine_info={"ok": True, "engine": "docker"}, engine="/usr/local/bin/docker", run=run,
                    credential=CRED, server="https://x", config_path=None, root=root)
    assert calls == [["/usr/local/bin/docker", "inspect", "--type", "container", "a1" * 32],
                     ["/usr/local/bin/docker", "image", "inspect", "sha256:" + "b2" * 32]]
    assert out["known"] is True and out["engine"] == "docker" and out["container"]["gpus"] == "all"
    no_engine = h.collect(engine_info={"ok": False}, engine=None, run=None, credential=CRED, server=None,
                          config_path=None, root=root)
    assert no_engine["known"] is False and "socket" in no_engine["reason"]
    failed = h.collect(engine_info={"ok": True}, engine="docker", run=lambda a: subprocess.CompletedProcess(a, 1, "", "no"),
                       credential=CRED, server=None, config_path=None, root=root)
    assert failed["known"] is False and "inspect" in failed["reason"]

    def boom(argv):
        raise RuntimeError("engine gone")

    broken = h.collect(engine_info={"ok": True}, engine="docker", run=boom, credential=CRED, server=None,
                       config_path=None, root=root)
    assert broken == {"method": "unknown", "known": False, "reason": "RuntimeError: engine gone"}


def test_fy_upd_008_outside_a_container_install_sh_or_manual(tmp_path):
    """FY-UPD-008: the systemd unit install.sh wrote (its first line says so), or its program directory, is
    "install.sh"; anything else is "manual", with its checkout when there is one."""
    units = tmp_path / "units"
    units.mkdir()
    (units / "forgeyard-agent.service").write_text("# Installed by https://srv:7860/install.sh. Running install.sh again rewrites it.\n")
    cg = "0::/system.slice/forgeyard-agent.service\n"
    out = h.describe_host(cgroup=cg, prefix="/usr/lib/fy", config_path="/etc/forgeyard/agent.toml",
                          server="https://srv:7860", unit_dir=units)
    assert out == {"method": "install.sh", "known": True, "service": "forgeyard-agent.service",
                   "unit": str(units / "forgeyard-agent.service"), "program": "/usr/lib/fy",
                   "config": "/etc/forgeyard/agent.toml", "server": "https://srv:7860"}
    venv = h.describe_host(cgroup="0::/\n", prefix="/opt/forgeyard/venv", config_path=None, server=None, unit_dir=units)
    assert venv["method"] == "install.sh" and venv["service"] is None
    checkout = tmp_path / "forgeyard"
    (checkout / ".git").mkdir(parents=True)
    manual = h.describe_host(cgroup="0::/user.slice\n", prefix=str(checkout / ".venv"), config_path=None, server=None,
                             unit_dir=units, package_dir=checkout)
    assert (manual["method"], manual["checkout"]) == ("manual", str(checkout))
    own_unit = units / "my-agent.service"
    own_unit.write_text("[Unit]\nDescription=mine\n")
    mine = h.describe_host(cgroup="0::/system.slice/my-agent.service\n", prefix="/srv/env", config_path=None,
                           server=None, unit_dir=units, package_dir=tmp_path / "nowhere")
    assert (mine["method"], mine["service"], mine["checkout"]) == ("manual", "my-agent.service", None)
    root = _proc(tmp_path / "host", "25 1 8:1 / / rw - ext4 /dev/sda1 rw", cg, dockerenv=False)
    assert h.collect(engine_info=None, engine=None, run=None, credential=CRED, server=None, config_path=None,
                     root=root)["method"] in ("install.sh", "manual")


def test_fy_upd_009_the_agent_sends_it_in_hello_and_older_servers_ignore_it(tmp_path, monkeypatch):
    """FY-UPD-009: hello carries `install` when the agent has one, and nothing otherwise; the server keeps a
    valid one, drops one with an unknown method and shortens one that is too large."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory

    monkeypatch.setenv(SIMULATION_ENV, "1")
    inv = SimulatedInventory("nvidia-rtx4090x2")
    rep = report("nvidia")
    msg = Agent("http://127.0.0.1:1", CRED, inv, install=rep).hello(inv.full())
    assert msg["install"] == rep
    assert "install" not in Agent("http://127.0.0.1:1", CRED, inv).hello(inv.full())
    assert HelloIn.model_validate(msg).install == rep
    assert HelloIn.model_validate({**msg, "install": {"method": "rootkit"}}).install is None
    big = {"method": "container", "known": True, "container": {"env": [{"name": "X", "value": "y" * 40_000}]}}
    assert HelloIn.model_validate({**msg, "install": big}).install == {
        "method": "container", "known": False, "reason": "the description was too large"}


# ------------------------------------------------------------------ the command
NVIDIA_COMMAND = """sudo docker pull docker.io/example/forgeyard-agent:1.4.2 && \\
sudo docker rm -f forgeyard-agent && \\
sudo docker run -d \\
  --name forgeyard-agent \\
  --restart always \\
  --network host \\
  --gpus all \\
  -v /var/run/docker.sock:/var/run/docker.sock \\
  -v forgeyard-agent:/var/lib/forgeyard-agent \\
  -v /srv/forgeyard-models:/srv/forgeyard-models \\
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \\
  -e FORGEYARD_CA_FINGERPRINT=sha256:""" + "3f9a" * 16 + """ \\
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \\
  -e FORGEYARD_NAME=gpu-box \\
  -e HF_TOKEN="$HF_TOKEN" \\
  docker.io/example/forgeyard-agent:1.4.2"""


def test_fy_upd_010_nvidia_command_pulls_then_runs_the_same_flags():
    """FY-UPD-010: pull the server's image, remove the old container, run it again with exactly the reported
    flags and the new image; no join token; HF_TOKEN from the shell, with a note saying so."""
    out = plan(node(report("nvidia")))
    assert out["known"] is True and out["method"] == "container" and out["offline"] is False
    assert [s["command"] for s in out["steps"]] == [NVIDIA_COMMAND]
    assert "FORGEYARD_JOIN_TOKEN" not in NVIDIA_COMMAND
    assert any("HF_TOKEN" in n and "export" in n for n in out["notes"])
    carry, there = out["alternative"]["steps"]
    assert carry["command"] == ("sudo docker pull docker.io/example/forgeyard-agent:1.4.2 && \\\n"
                                "sudo docker save docker.io/example/forgeyard-agent:1.4.2 | gzip | "
                                "ssh USER@gpu-box 'gunzip -c | sudo docker load'")
    assert there["command"] == NVIDIA_COMMAND.split(" && \\\n", 1)[1]


def test_fy_upd_011_amd_machine_without_registry_access_gets_the_image_carried():
    """FY-UPD-011: a machine whose files come through the server: save | ssh | load first, then the run with the
    loaded tag; devices, group ids and /opt/rocm as reported. A digest-pinned image is tagged before the save."""
    pinned = images.agent_image("docker.io/example/forgeyard-agent@sha256:" + "c" * 64, "1.4.2")
    out = plan(node(report("amd"), name="apu-box", fetch_via="server"), image=pinned)
    assert out["offline"] is True and out["alternative"] is None
    carry, run = (s["command"] for s in out["steps"])
    assert carry == ("sudo docker pull docker.io/example/forgeyard-agent@sha256:" + "c" * 64 + " && \\\n"
                     "sudo docker tag docker.io/example/forgeyard-agent@sha256:" + "c" * 64
                     + " docker.io/example/forgeyard-agent:1.4.2 && \\\n"
                     "sudo docker save docker.io/example/forgeyard-agent:1.4.2 | gzip | "
                     "ssh USER@apu-box 'gunzip -c | sudo docker load'")
    lines = run.split(" \\\n")
    assert lines[:2] == ["sudo docker rm -f forgeyard-agent &&", "sudo docker run -d"]
    for flag in ("--device /dev/kfd", "--device /dev/dri", "--group-add 44", "--group-add 110",
                 "-v /opt/rocm:/opt/rocm:ro", "-e FORGEYARD_PORT_RANGE=33000-33002", "--network host"):
        assert f"  {flag}" in lines, flag
    assert lines[-1] == "  docker.io/example/forgeyard-agent:1.4.2" and "--gpus" not in run


def test_fy_upd_012_cpu_agent_and_an_unpublished_image():
    """FY-UPD-012: a CPU agent with its state on the image's unnamed volume keeps that volume by name; with no
    published image the first step builds it from a checkout of the server's version. Podman stays Podman."""
    local = images.agent_image("", "1.4.2", namespace="")
    out = plan(node(report("cpu", engine="podman"), name="cpu-box"), image=local)
    build, run = (s["command"] for s in out["steps"])
    assert build == "git checkout v1.4.2 && \\\nsudo podman build -f Dockerfile.agent -t forgeyard-agent:1.4.2 ."
    assert run.startswith("sudo podman rm -f fy-agent && \\\nsudo podman run -d \\\n  --name fy-agent \\\n"
                          "  --restart unless-stopped")
    assert f"  -v {'e5' * 32}:/var/lib/forgeyard-agent \\\n" in run and run.endswith("forgeyard-agent:1.4.2")
    assert any("the volume keeps the machine" in n for n in out["notes"])
    gone = report("cpu")
    gone["container"]["state_kept"] = False
    notes = plan(node(gone))["notes"]
    assert any("join again" in n for n in notes) and not any("volume keeps" in n for n in notes)


def test_fy_upd_013_reported_values_are_checked_and_quoted():
    """FY-UPD-013: whatever an agent reports, the command is safe to paste: values are shell-quoted, values with
    control characters, colons in mounts, bad names, odd --gpus / --restart values are left out (and named)."""
    rep = report("nvidia")
    c = rep["container"]
    c.update(name="x; reboot", restart="always; reboot", gpus="all; reboot", network="host$(id)", runtime="nvidia",
             group_add=["44", "7\n8"], cap_add=["SYS_ADMIN"], privileged=True, args=["--label", "a b"],
             hidden_args=["--join-token"])
    c["mounts"].append({"type": "bind", "source": "/srv/$(reboot)", "target": "/data"})
    c["mounts"].append({"type": "bind", "source": "/a:b", "target": "/c"})
    c["env"] += [{"name": "BAD NAME", "value": "x"}, {"name": "GREETING", "value": "it's a $HOME"},
                 {"name": "NL", "value": "a\nb"}]
    out = plan(node(rep))
    cmd = out["steps"][0]["command"]
    assert "--name forgeyard-agent " in cmd and "reboot \\" not in cmd and "--restart" not in cmd
    assert "--gpus" not in cmd and "--network 'host$(id)'" in cmd and "--runtime nvidia" in cmd
    assert "--group-add 44 " in cmd and "7\n8" not in cmd and "--cap-add SYS_ADMIN" in cmd and "--privileged" in cmd
    assert "-v '/srv/$(reboot):/data'" in cmd and "/a:b" not in cmd
    assert "-e 'GREETING=it'\"'\"'s a $HOME'" in cmd and "BAD NAME" not in cmd and "NL=" not in cmd
    assert cmd.endswith("docker.io/example/forgeyard-agent:1.4.2 --label 'a b'")
    notes = " ".join(out["notes"])
    assert "a mount on /c" in notes and "an environment variable" in notes and "the value of NL" in notes
    assert "--join-token" in notes


def test_fy_upd_014_install_sh_and_manual_agents():
    """FY-UPD-014: an install.sh agent gets the one-line install without a join token, for the address it uses
    (not the browser's), with the CA check; plain HTTP cannot serve install.sh; a checkout gets git and uv."""
    rep = {"method": "install.sh", "known": True, "service": "forgeyard-agent.service", "server": "https://10.0.0.5:7860"}
    out = plan(node(rep), server="https://localhost:7860")
    (step,) = out["steps"]
    assert step["command"] == ("curl -fsSk https://10.0.0.5:7860/api/tls/ca.pem -o forgeyard-ca.pem && \\\n"
                               "  sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \\\n"
                               f"  echo \"{'3f9a' * 16}  forgeyard-ca.der\" | sha256sum -c && \\\n"
                               "  curl -fsS --cacert forgeyard-ca.pem https://10.0.0.5:7860/install.sh | \\\n"
                               f"  sudo sh -s -- --ca-fingerprint {FP}")
    assert "--join" not in step["command"] and any("forgeyard-agent.service" in n for n in out["notes"])
    hostile = plan(node({**rep, "server": "https://x; reboot"}), server="https://srv:7860", fingerprint=None)
    assert hostile["steps"][0]["command"] == "curl -fsS https://srv:7860/install.sh | sudo sh"
    plain = plan(node({**rep, "server": None}), server="http://srv:7860", fingerprint=None)
    assert plain["steps"] == [] and "HTTPS" in plain["notes"][0]
    manual = plan(node({"method": "manual", "known": True, "checkout": "/home/me/forge yard"}))
    assert manual["steps"][0]["command"] == ("cd '/home/me/forge yard' && \\\ngit fetch --tags && git checkout v1.4.2 && "
                                             "\\\nuv sync")
    assert plan(node({"method": "manual", "known": True}))["steps"] == []


def test_fy_upd_015_nothing_known_says_so():
    """FY-UPD-015: an agent before 1.4.2 (no report), one that could not tell, a foreign container: no command,
    a reason and the guide."""
    old = plan(node(None))
    assert (old["known"], old["steps"], old["method"]) == (False, [], None) and "before 1.4.2" in old["reason"]
    assert old["guide"] == "/guide/en/how-to#update-an-agent"
    lost = plan(node({"method": "container", "known": False, "reason": "no engine\x00"}))
    assert lost["steps"] == [] and "could not describe" in lost["reason"]
    foreign = plan(node({"method": "container", "known": False, "foreign": True,
                         "reason": "the agent runs inside a container that is not the Forgeyard agent image"}))
    assert "not the Forgeyard agent image" in foreign["reason"]


# ------------------------------------------------------------------ behind (#122)
FULL = ("workloads", "replica_keys", "replica_guard", "image_ids", "engine_zip", "workload_guard", "models",
        "cache_admin", "prefetch", "relay", "relay_source", "relay_images", "image_load", "image_save")
CONTAINER_OK = {"container": {"ok": True}}


def test_fy_upd_016_behind_by_version_protocol_or_a_missing_feature():
    """FY-UPD-016: older than the server, an older protocol, or the server's version without a feature such an
    agent declares; not when current, newer, revoked or of an unknown version."""
    current = node(version="1.4.2", features=FULL, runtime_info=CONTAINER_OK)
    assert update.behind(current, "1.4.2") is None
    older = update.behind(node(version="1.4.0", features=FULL, runtime_info=CONTAINER_OK), "1.4.2")
    assert older == {"agent": "1.4.0", "server": "1.4.2", "older": True, "old_protocol": False, "missing": []}
    assert update.behind(node(version="1.4.2", protocol=0, features=FULL, runtime_info=CONTAINER_OK), "1.4.2")["old_protocol"]
    stale = update.behind(node(version="1.4.2", features=[f for f in FULL if f != "image_save"],
                               runtime_info=CONTAINER_OK), "1.4.2")
    assert stale["older"] is False and stale["missing"] == ["image_save"]
    assert update.behind(node(version="1.5.0", features=("workloads",)), "1.4.2") is None  # lacks features, but newer
    assert update.behind(node(version="1.0.0", status="revoked"), "1.4.2") is None
    assert update.behind(node(version=None), "1.4.2") is None
    # what an agent declares depends on what it has: no container engine, no image features
    no_engine = node(version="1.4.2", features=[f for f in FULL if f not in ("image_load", "image_save", "relay_images")])
    assert update.behind(no_engine, "1.4.2") is None
    assert update.expected_features(node(features=())) == set()


def test_fy_upd_017_expected_features_match_what_an_agent_declares(tmp_path, monkeypatch):
    """FY-UPD-017: drift check: a fully equipped agent of this version declares exactly the features the server
    expects of it (agent/client.py and install/update.py change together)."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.container import ContainerRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.workload_helpers import FakeDocker

    monkeypatch.setenv(SIMULATION_ENV, "1")
    rt = ContainerRuntime(RecordStore(tmp_path / "s"), agent_id="a", engine=str(FakeDocker(tmp_path / "bin").path))
    inv = SimulatedInventory("nvidia-rtx4090x2")
    sup = Supervisor(tmp_path / "state", {"container": rt}, models=ModelCache(tmp_path / "c"), tick=0.05,
                     port_range=(32190, 32199))
    msg = Agent("http://127.0.0.1:9", CRED, inv, supervisor=sup).hello(inv.full())
    n = node(version="1.4.2", features=msg["features"], runtime_info=msg["runtime_info"])
    assert set(msg["features"]) == update.expected_features(n) == set(FULL)


# ------------------------------------------------------------------ through the API
def test_fy_upd_018_admins_get_the_command_everyone_sees_behind(owner):
    """FY-UPD-018: an agent's hello with `install`: the machine list never shows it (runtime_info has no
    "install"), says the agent is behind and names the server's version; admins get the command, others 403;
    an older agent's machine says what is not known."""
    j = joined(owner)
    rep = report("nvidia")
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello(agent_version="1.0.0", install=rep, features=["workloads"]))
        assert ws.receive_json()["type"] == "welcome"
        m = machine(owner, j["node_id"])
    assert "install" not in m["runtime_info"]
    assert m["agent_behind"]["older"] is True and m["agent_behind"]["agent"] == "1.0.0"
    assert m["server_version"] == m["agent_behind"]["server"]
    r = owner.get(f"/api/machines/{j['node_id']}/agent-update")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["known"] is True and body["method"] == "container" and body["behind"]["older"] is True
    cmd = body["steps"][0]["command"]
    assert "--gpus all" in cmd and f":{m['server_version']}" in cmd and "FORGEYARD_JOIN_TOKEN" not in cmd
    for bit in ("fyj_", "hf_", j["credential"]):
        assert bit not in r.text
    add_user(owner, "olga", role="operator")
    olga = sign_in(owner, "olga", "quiet-meadow-4821")
    assert olga.get(f"/api/machines/{j['node_id']}/agent-update").status_code == 403
    assert "install" not in machine(olga, j["node_id"])["runtime_info"]
    old = joined(owner, "old-box")
    with connect(owner, old["credential"]) as ws:
        ws.send_json(hello())
        assert ws.receive_json()["type"] == "welcome"
    body = owner.get(f"/api/machines/{old['node_id']}/agent-update").json()
    assert body["known"] is False and body["steps"] == [] and "before 1.4.2" in body["reason"]
    assert owner.get("/api/machines/99999/agent-update").status_code == 404
