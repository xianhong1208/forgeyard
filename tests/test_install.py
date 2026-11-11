"""Installing an agent from the server (FY-INST-*): the agent's wheel, /install.sh, the commands on
the Machines page, the install script itself run for real against a real HTTPS server, and the
agent image's Dockerfile (FY-IMG-*)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from forgeyard import __version__
from forgeyard.agent import state
from forgeyard.agent.inventory import SIMULATION_ENV
from forgeyard.config import get_settings
from forgeyard.install import commands, wheel
from forgeyard.security import tls
from tests.conftest import OWNER, SETUP_TOKEN, make_client
from tests.tls_helpers import LiveTLS, ca_of, free_port, https

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "forgeyard" / "install" / "install.sh"


# ------------------------------------------------------------------ the wheel
def test_fy_inst_001_the_wheel_is_the_running_version_and_reproducible():
    """FY-INST-001: the wheel holds the forgeyard package (not the built UI, no bytecode), is named
    and versioned after the running server, declares the agent's requirements, has a RECORD that
    matches every file, and is byte-for-byte the same when built again."""
    data = wheel.build()
    assert data == wheel.build()
    assert wheel.filename() == f"forgeyard-{__version__}-py3-none-any.whl"
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    assert "forgeyard/cli.py" in names and "forgeyard/agent/config.py" in names and "forgeyard/install/install.sh" in names
    assert not any(n.startswith("forgeyard/static/") or n.endswith(".pyc") or "__pycache__" in n for n in names)
    info = f"forgeyard-{__version__}.dist-info"
    meta = zf.read(f"{info}/METADATA").decode()
    assert f"Version: {__version__}" in meta and "Requires-Python: >=3.13" in meta
    for req in wheel.REQUIRES:
        assert f"Requires-Dist: {req}\n" in meta
    assert 'Requires-Dist: nvidia-ml-py>=12.535; extra == "nvidia"' in meta
    assert "forgeyard = forgeyard.cli:main" in zf.read(f"{info}/entry_points.txt").decode()
    record = zf.read(f"{info}/RECORD").decode().splitlines()
    assert len(record) == len(names)
    for line in record:
        name, digest, size = line.rsplit(",", 2)
        if name.endswith("RECORD"):
            continue
        body = zf.read(name)
        assert int(size) == len(body)
        assert digest == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()


def test_fy_inst_002_the_wheel_declares_everything_the_agent_imports():
    """FY-INST-002: the agent's modules, imported in a fresh interpreter, use no third-party package
    beyond the wheel's requirements (and theirs): an agent installed from it starts."""
    code = """
import sys
before = set(sys.modules)
import forgeyard.cli, forgeyard.agent.config, forgeyard.agent.setup, forgeyard.agent.client, forgeyard.agent.models
import forgeyard.agent.workloads.supervisor, forgeyard.agent.workloads.container, forgeyard.agent.workloads.process
import forgeyard.agent.workloads.guard, forgeyard.agent.inventory.nvidia, forgeyard.agent.inventory.amd
forgeyard.cli.build_parser()
mods = {m.split('.')[0] for m in set(sys.modules) - before}
print(sorted(m for m in mods if m not in sys.stdlib_module_names and not m.startswith('_')))
"""
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    used = set(json.loads(out.stdout.replace("'", '"')))
    allowed = {"forgeyard", "websockets", "h11", "pydantic", "pydantic_core", "annotated_types", "typing_extensions",
               "typing_inspection"}
    assert used <= allowed, used - allowed
    assert {r.split(">=")[0] for r in wheel.REQUIRES} == {"websockets", "h11", "pydantic"}


# ------------------------------------------------------------------ the endpoints
def _client(base_url: str):
    return make_client(base_url=base_url)


def test_fy_inst_003_install_sh_and_the_wheel_only_over_https():
    """FY-INST-003: over HTTPS, /install.sh is the script with this server's address, version and the
    wheel's SHA-256 filled in, and /install/<wheel> serves exactly that wheel (no sign-in needed).
    Over plain HTTP both are refused, with a body that fails even when piped into sh. Another name
    is 404; an unknown host is still 421."""
    with _client("https://localhost") as c:
        r = c.get("/install.sh")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-shellscript")
        script = r.text
        name, data, sha = wheel.cached()
        assert 'SERVER="https://localhost"' in script and f'VERSION="{__version__}"' in script
        assert f'WHEEL="{name}"' in script and f'WHEEL_SHA256="{sha}"' in script and "@@" not in script
        w = c.get(f"/install/{name}")
        assert w.status_code == 200 and w.content == data and hashlib.sha256(w.content).hexdigest() == sha
        assert c.get("/install/forgeyard-0.0.1-py3-none-any.whl").status_code == 404
        # only the one file, by its exact name: nothing else of the package or the repository
        assert c.get("/install/pyproject.toml").status_code == 404 and c.get("/install/cli.py").status_code == 404
        assert "[project]" not in c.get("/install/..%2F..%2Fpyproject.toml").text
        assert c.get("/install.sh", headers={"Host": "evil.example"}).status_code == 421
    with _client("http://localhost") as c:
        for path in ("/install.sh", f"/install/{wheel.filename()}"):
            r = c.get(path)
            assert r.status_code == 403
            out = subprocess.run(["sh", "-c", r.text], capture_output=True, text=True)
            assert out.returncode == 1 and "only over HTTPS" in out.stderr


def test_fy_inst_004_a_proxy_that_terminates_tls_counts_and_odd_hosts_are_refused(monkeypatch):
    """FY-INST-004: behind a trusted TLS proxy (TLS=off, X-Forwarded-Proto: https) the script is
    served for the proxy's https address; an address a shell would expand is never put into it."""
    monkeypatch.setenv("TLS", "off")
    monkeypatch.setenv("TRUSTED_PROXIES", "testclient")
    get_settings.cache_clear()
    with _client("http://localhost") as c:
        r = c.get("/install.sh", headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200 and 'SERVER="https://localhost"' in r.text
    assert commands.safe_server("https://192.168.1.20:7860") and commands.safe_server("https://[fd00::1]:7860")
    assert commands.safe_server("https://forge.example.com/forgeyard")
    for bad in ("http://x", 'https://x"; rm -rf /; "', "https://x$(id)", "https://x`id`", "https://x y"):
        assert not commands.safe_server(bad)
        with pytest.raises(ValueError):
            commands.install_script(bad)


def test_fy_inst_005_the_machines_page_gets_three_ways_that_pin_the_ca(owner):
    """FY-INST-005: a new join token comes with the one-line install (CA check first, then the
    script, with the token and the pin), the Docker build and run commands for NVIDIA, AMD and
    CPU (host network, the socket, the state volume, the cache at the same path, the pin), a config
    file and the manual commands; all with the same fingerprint."""
    tls.ensure()  # what the first start does (the TestClient does not start a TLS listener)
    owner.base_url = "https://localhost"
    r = owner.post("/api/machines/join-tokens")
    assert r.status_code == 201
    j = r.json()
    fp, token = j["ca_fingerprint"], j["token"]
    assert fp and fp == tls.fingerprint(tls.trust_anchor_pem())
    hexfp = fp.removeprefix("sha256:")
    inst = j["install_command"]
    assert inst.index("curl -fsSk https://localhost/api/tls/ca.pem") < inst.index("sha256sum -c") < inst.index(
        "--cacert forgeyard-ca.pem https://localhost/install.sh")
    assert f'echo "{hexfp}  forgeyard-ca.der"' in inst and inst.endswith(f"sudo sh -s -- --join {token} --ca-fingerprint {fp}")
    assert j["docker_build_command"] == f"docker build -f Dockerfile.agent -t forgeyard-agent:{__version__} ."
    for kind, extra in (("nvidia", "--gpus all"), ("amd", "--device /dev/kfd"), ("cpu", None)):
        cmd = j["docker_commands"][kind]
        for part in ("--network host", "--restart always", "-v /var/run/docker.sock:/var/run/docker.sock",
                     "-v forgeyard-agent:/var/lib/forgeyard-agent", "-v /srv/forgeyard-models:/srv/forgeyard-models",
                     f"-e FORGEYARD_CA_FINGERPRINT={fp}", f"-e FORGEYARD_JOIN_TOKEN={token}",
                     "-e FORGEYARD_SERVER=https://localhost", f"forgeyard-agent:{__version__}"):
            assert part in cmd, (kind, part)
        assert (extra in cmd) if extra else ("--gpus" not in cmd and "/dev/kfd" not in cmd)
    # Group names are looked up in the image, which has no "render": the host's numeric ids instead.
    amd = j["docker_commands"]["amd"]
    assert ('--group-add "$(getent group video | cut -d: -f3)" --group-add "$(getent group render | cut -d: -f3)"'
            in amd) and "--group-add video" not in amd and "--group-add render" not in amd
    import tomllib

    cfg = tomllib.loads(j["config_file"])
    assert cfg["server"] == "https://localhost" and cfg["ca_fingerprint"] == fp and cfg["models"]["cache"]
    assert j["manual_commands"][0].endswith(f"agent join --join-token {token}")
    # plain HTTP (TLS=off without a proxy): no one-line install, the other ways stay
    owner.base_url = "http://localhost"
    assert commands.install_command("http://localhost", token, None) is None
    assert commands.install_command("https://forge.example.com", token, None) == (
        f"curl -fsS https://forge.example.com/install.sh | sudo sh -s -- --join {token}")


# ------------------------------------------------------------------ install.sh, for real
FAKE_SYSTEMCTL = r'''#!/bin/sh
# systemctl for the tests: records calls; restart runs the unit's ExecStart the way systemd does
# (fresh environment, its own session) and stop ends only the main process (KillMode=process).
D="$FAKE_SYSTEMD"
echo "$*" >> "$D/calls"
while [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) break ;; esac; done
UNIT="$FORGEYARD_UNIT_DIR/forgeyard-agent.service"
stop_main() {
    [ -f "$D/pid" ] || return 0
    pid="$(cat "$D/pid")"; kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    rm -f "$D/pid"
}
case "$1" in
    daemon-reload|enable) ;;
    restart|start)
        stop_main
        cmd="$(sed -n 's/^ExecStart=//p' "$UNIT")"
        env -i PATH=/usr/bin:/bin FORGEYARD_AGENT_SIMULATION="$FORGEYARD_AGENT_SIMULATION" setsid $cmd >> "$D/agent.log" 2>&1 < /dev/null &
        echo $! > "$D/pid" ;;
    stop) stop_main ;;
    disable) [ "${2:-}" = "--now" ] && stop_main ;;
    status) [ -f "$D/pid" ] && echo "active (running), main PID $(cat "$D/pid")" ;;
esac
exit 0
'''

# An account that does not exist here, so the script calls (the fake) useradd for it.
ABSENT_USER = "fy-test-no-such-user"
FAKE_USERADD = '#!/bin/sh\necho "$*" >> "$FAKE_SYSTEMD/useradd.log"\n'


def _python_313() -> bool:
    return sys.version_info >= (3, 13)


@pytest.fixture()
def live(monkeypatch, tmp_path):
    monkeypatch.setenv(SIMULATION_ENV, "1")
    monkeypatch.setenv("AGENT_HEARTBEAT_SECONDS", "1")
    port = free_port()
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", str(port))
    get_settings.cache_clear()
    with LiveTLS(port) as srv:
        ca = ca_of(get_settings().data_dir)
        _, headers = https(port, "POST", "/api/auth/setup", ca,
                           {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        srv.cookie = headers.get("set-cookie", "").split(";")[0]
        srv.ca = ca
        yield srv


def _token(srv) -> dict:
    return https(srv.port, "POST", "/api/machines/join-tokens", srv.ca, {}, srv.cookie)[0]


def _machines(srv) -> list[dict]:
    return https(srv.port, "GET", "/api/machines", srv.ca, None, srv.cookie)[0]["machines"]


class Box:
    """A sandbox the install script writes into instead of /etc, /opt and /var/lib."""

    def __init__(self, root: Path, *, with_uv: bool = True):
        self.root = root
        self.fake = root / "fake"
        self.fake.mkdir(parents=True)
        (self.fake / "systemctl").write_text(FAKE_SYSTEMCTL)
        (self.fake / "useradd").write_text(FAKE_USERADD)
        for f in ("systemctl", "useradd"):
            (self.fake / f).chmod(0o755)
        (self.fake / "python3.13").symlink_to(sys.executable)
        path = [str(self.fake)]
        if with_uv and shutil.which("uv"):
            path.append(str(Path(shutil.which("uv")).parent))
        path += ["/usr/bin", "/bin"]
        self.env = {
            "PATH": ":".join(path), "HOME": str(root / "home"), "TMPDIR": str(root),
            "FORGEYARD_HOME": str(root / "opt"), "FORGEYARD_AGENT_CONFIG": str(root / "etc" / "agent.toml"),
            "FORGEYARD_AGENT_STATE_DIR": str(root / "state"), "FORGEYARD_UNIT_DIR": str(root / "systemd"),
            "FORGEYARD_BIN_DIR": str(root / "bin"), "FORGEYARD_SYSTEMD": "yes", "FAKE_SYSTEMD": str(self.fake),
            "FORGEYARD_AGENT_SIMULATION": "1", "FORGEYARD_PORT_RANGE": "47300-47309",
            "FORGEYARD_INSTALL_ANY_USER": "1",  # CI runs the tests as a normal user
        }
        for key in ("UV_CACHE_DIR", "XDG_CACHE_HOME", "UV_PYTHON_DOWNLOADS", "UV_PYTHON_PREFERENCE", "UV_INDEX_URL",
                    "PIP_INDEX_URL"):
            if os.environ.get(key):
                self.env[key] = os.environ[key]
        if "UV_CACHE_DIR" not in self.env:
            self.env["UV_CACHE_DIR"] = str(Path.home() / ".cache" / "uv")
        (root / "home").mkdir()

    def run(self, script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", "-s", "--", *args], input=script, capture_output=True, text=True, timeout=600,
                              env={**self.env, **(env or {})}, cwd=self.root)

    def calls(self) -> list[str]:
        f = self.fake / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def stop(self):
        subprocess.run([str(self.fake / "systemctl"), "stop", "forgeyard-agent.service"], env=self.env, timeout=30)


def _wait(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


@pytest.mark.skipif(not shutil.which("curl") or not _python_313(), reason="needs curl and Python 3.13")
def test_fy_inst_006_install_sh_end_to_end(live, tmp_path):
    """FY-INST-006: the real install script against a real HTTPS server, in a sandbox with a fake
    systemctl and useradd:
    - a wrong fingerprint is refused before anything is installed, and so is a wheel whose SHA-256
      is not the one in the script;
    - with the right one: the wheel (SHA-256 checked) in a venv, the forgeyard command, the workload
      account created, agent.toml written by `agent init`, joined (credential 0600, CA pinned), the
      unit (KillMode=process, Restart=always, no restart after a refused credential), enabled and
      started; the agent, started the way the unit says, comes online;
    - run again without a token it upgrades: same credential and machine, the config kept, restarted;
    - --uninstall removes the service and the program and keeps the config and the credential;
      --purge deletes those too."""
    box = Box(tmp_path / "box")
    tok = _token(live)
    script = _get_text(live, "/install.sh")
    wrong = "sha256:" + "00" * 32
    r = box.run(script, "--join", tok["token"], "--ca-fingerprint", wrong)
    assert r.returncode == 1 and "Refused: the CA" in r.stderr and wrong.removeprefix("sha256:") in r.stderr
    assert not (box.root / "opt").exists() and not (box.root / "state").exists()
    # a wheel that is not the one the script names (changed on the way) is refused too
    tampered = re.sub(r'WHEEL_SHA256="[0-9a-f]{64}"', 'WHEEL_SHA256="' + "0" * 64 + '"', script)
    r = box.run(tampered, "--join", tok["token"], "--ca-fingerprint", tok["ca_fingerprint"])
    assert r.returncode == 1 and "is damaged" in r.stderr and not (box.root / "opt" / "venv").exists()

    r = box.run(script, "--join", tok["token"], "--ca-fingerprint", tok["ca_fingerprint"], "--name", "box-1",
                "--label", "rack=r9", "--model-cache", str(box.root / "models"), "--workload-user", ABSENT_USER)
    try:  # the agent the fake systemd started is stopped whatever fails below
        assert r.returncode == 0, r.stdout + r.stderr
        out = r.stdout
        assert f"The CA matches {tok['ca_fingerprint']}." in out and "SHA-256 checked" in out
        assert f"forgeyard {__version__}, as {box.root}/bin/forgeyard" in out
        assert (box.root / "bin" / "forgeyard").resolve() == (box.root / "opt" / "venv" / "bin" / "forgeyard").resolve()
        assert (box.fake / "useradd.log").read_text().split()[-1] == ABSENT_USER
        cfg = (box.root / "etc" / "agent.toml").read_text()
        assert 'name = "box-1"' in cfg and 'labels = { rack = "r9" }' in cfg and f'ca_fingerprint = "{tok["ca_fingerprint"]}"' in cfg
        assert f'state_dir = "{box.root}/state"' in cfg and f'user = "{ABSENT_USER}"' in cfg and "fyj_" not in cfg
        assert (box.root / "models").is_dir()
        saved = state.load(box.root / "state")
        assert saved["name"] == "box-1" and saved["ca_fingerprint"] == tok["ca_fingerprint"]
        assert oct((box.root / "state" / "credential.json").stat().st_mode & 0o777) == "0o600"
        assert (box.root / "state" / "server-ca.pem").read_text().strip() == live.ca.strip()
        unit = (box.root / "systemd" / "forgeyard-agent.service").read_text()
        assert "\nKillMode=process\n" in unit and "\nRestart=always\n" in unit and "\nRestartPreventExitStatus=3\n" in unit
        assert f"\nExecStart={box.root}/opt/venv/bin/python -m forgeyard agent --config {box.root}/etc/agent.toml\n" in unit
        assert box.calls()[:3] == ["daemon-reload", "enable forgeyard-agent.service", "restart forgeyard-agent.service"]
        assert _wait(lambda: any(m["name"] == "box-1" and m["status"] == "online" for m in _machines(live))), \
            (box.fake / "agent.log").read_text()
        # run again, as an upgrade: no token; the machine and its credential stay
        r = box.run(script, "--ca-fingerprint", tok["ca_fingerprint"])  # the settings name the account
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Keeping the existing file" in r.stdout and "keeping its credential" in r.stdout + r.stderr
        # the account the settings name, never the default one
        calls = (box.fake / "useradd.log").read_text().splitlines()
        assert len(calls) == 2 and all(c.split()[-1] == ABSENT_USER for c in calls)
        again = state.load(box.root / "state")
        assert again["credential"] == saved["credential"] and again["node_id"] == saved["node_id"]
        assert box.calls().count("restart forgeyard-agent.service") == 2
        assert _wait(lambda: [m["status"] for m in _machines(live)] == ["online"])
        assert len(_machines(live)) == 1
    finally:
        box.stop()
    r = box.run(script, "--uninstall")
    assert r.returncode == 0, r.stderr
    assert not (box.root / "systemd" / "forgeyard-agent.service").exists() and not (box.root / "opt").exists()
    assert not (box.root / "bin" / "forgeyard").exists()
    assert (box.root / "etc" / "agent.toml").exists() and (box.root / "state" / "credential.json").exists()
    assert "disable --now forgeyard-agent.service" in box.calls()
    r = box.run(script, "--uninstall", "--purge")
    assert r.returncode == 0 and not (box.root / "etc" / "agent.toml").exists() and not (box.root / "state").exists()
    assert (box.root / "models").is_dir()  # the model cache is never deleted


def _get_text(srv, path: str) -> str:
    import ssl
    import urllib.request

    ctx = ssl.create_default_context(cadata=srv.ca)
    with urllib.request.urlopen(f"https://127.0.0.1:{srv.port}{path}", context=ctx, timeout=10) as r:
        return r.read().decode()


@pytest.mark.skipif(not shutil.which("curl") or not _python_313(), reason="needs curl and Python 3.13")
def test_fy_inst_007_without_uv_or_systemd_and_the_page_command(live, tmp_path):
    """FY-INST-007: the command from the Machines page, run as it is (minus sudo): its first lines
    check the CA (a wrong fingerprint stops it before install.sh is fetched). Then, without uv on
    the machine, the script uses the Python 3.13 that is there; without systemd it installs, joins,
    writes no unit and says how to start the agent; the agent started that way comes online."""
    box = Box(tmp_path / "box", with_uv=False)
    tok = _token(live)
    cmd = tok["install_command"].replace("sudo sh -s --", "sh -s --") + " --workload-user nobody"
    bad = cmd.replace(tok["ca_fingerprint"].removeprefix("sha256:") + "  forgeyard-ca.der", "00" * 32 + "  forgeyard-ca.der")
    r = subprocess.run(["sh", "-c", bad], capture_output=True, text=True, timeout=60, cwd=box.root,
                       env={**box.env, "FORGEYARD_SYSTEMD": "no"})
    assert r.returncode != 0 and "FAILED" in r.stdout + r.stderr and "==> Checking this machine" not in r.stdout
    r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=600, cwd=box.root,
                       env={**box.env, "FORGEYARD_SYSTEMD": "no"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "forgeyard-ca.der: OK" in r.stdout and "Installed with Python 3.1" in r.stdout
    assert "no systemd here" in r.stdout and not (box.root / "systemd").exists() and box.calls() == []
    start = next(line.strip() for line in r.stdout.splitlines() if line.strip().endswith("agent.toml")
                 and line.strip().startswith(str(box.root)))
    proc = subprocess.Popen(start.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            env={"PATH": "/usr/bin:/bin", SIMULATION_ENV: "1"})
    try:
        assert _wait(lambda: any(m["status"] == "online" for m in _machines(live)))
    finally:
        proc.terminate()
        proc.wait(15)


def test_fy_inst_008_install_sh_is_posix_and_shellcheck_clean():
    """FY-INST-008: install.sh is POSIX sh (it runs under dash and busybox): shellcheck finds nothing
    (CI always runs it; locally when shellcheck is installed). `sh -n` parses it either way."""
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0
    text = SCRIPT.read_text()
    assert text.startswith("#!/bin/sh\n") and "set -eu" in text
    for bashism in ("[[", "function ", "local ", "<(", "$'", "pipefail"):
        assert bashism not in text, bashism
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck is not installed (CI runs it)")
    r = subprocess.run(["shellcheck", "-s", "sh", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# ------------------------------------------------------------------ the agent image
def test_fy_img_001_the_dockerfile_builds_the_same_wheel_and_pins_its_bases():
    """FY-IMG-001: Dockerfile.agent builds the agent from the wheel the server serves (the same
    builder), with nvidia-ml-py; every base image is pinned by digest; the entry point is the agent
    behind tini; the image has the workload account and rlimits; the build context is forgeyard/."""
    text = (ROOT / "Dockerfile.agent").read_text()
    froms = re.findall(r"^FROM (\S+)", text, re.M)
    pinned = [f for f in froms if "@sha256:" in f]
    assert pinned and all(re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", f) for f in pinned)
    assert all("@sha256:" in f or f in ("base",) for f in froms), froms
    assert "python -m forgeyard.install.wheel /dist" in text and "[nvidia]" in text
    assert 'ENTRYPOINT ["tini", "--", "forgeyard", "agent"]' in text
    assert "FORGEYARD_LIMITS=rlimit" in text and "FORGEYARD_WORKLOAD_USER=forgeyard-run" in text
    assert "useradd --system --uid 990" in text and "VOLUME /var/lib/forgeyard-agent" in text
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert ignore[ignore.index("*") + 1] == "!forgeyard" and "forgeyard/static" in ignore
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "docker build -f Dockerfile.agent" in ci and "scripts/agent_image_smoke.sh" in ci and "shellcheck" in ci


def test_fy_img_002_the_image_has_what_the_hosts_libamd_smi_loads():
    """FY-IMG-002: libamd_smi.so from the mounted ROCm dlopen()s libdrm.so.2 and the unversioned
    libdrm_amdgpu.so; without them amdsmi read no ASIC information on the real Strix Halo. The smoke
    test loads both inside the image."""
    text = (ROOT / "Dockerfile.agent").read_text()
    install = re.search(r"apt-get install -y --no-install-recommends ([^\\\n]+)", text).group(1).split()
    assert {"libdrm2", "libdrm-amdgpu1"} <= set(install)
    assert 'ln -s libdrm_amdgpu.so.1 "$(dirname "$amdgpu")/libdrm_amdgpu.so"' in text and "ldconfig" in text
    smoke = (ROOT / "scripts" / "agent_image_smoke.sh").read_text()
    assert 'ctypes.CDLL("libdrm.so.2"); ctypes.CDLL("libdrm_amdgpu.so")' in smoke
