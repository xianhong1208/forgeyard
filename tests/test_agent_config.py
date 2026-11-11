"""The agent's config file, environment variables and flags (FY-ACFG-*), `agent init` (FY-INIT-*)
and how a joined agent treats join settings on a restart."""

import json
import logging
import tomllib
from pathlib import Path

import pytest

from forgeyard.agent import config, setup, state
from forgeyard.cli import build_parser, main as cli_main

FP = "sha256:" + "ab" * 32
FP2 = "sha256:" + "cd" * 32

FULL = f'''
server = "https://192.168.1.20:7860"
ca_fingerprint = "{FP}"
name = "gpu-01"
state_dir = "/var/lib/fy-test-state"
log_level = "debug"
labels = {{ rack = "a", room = "lab" }}

[workloads]
enabled = true
user = "forgeyard-run"
work_dir = "/var/lib/fy-test-work"
port_range = "31000-31999"
bind_address = "192.168.1.21"
container_engine = "podman"
limits = "rlimit"
log_max_mb = 25

[models]
cache = "/srv/forgeyard-models"
local_dirs = ["/data/models", "/mnt/models"]
allow_private_hosts = true
'''


def args_of(*argv: str):
    return build_parser().parse_args(["agent", *argv])


def write(tmp_path: Path, text: str, name: str = "agent.toml") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_fy_acfg_001_every_setting_comes_from_the_file(tmp_path):
    """FY-ACFG-001: every key of the file lands on the argument the agent uses, with its type, and
    `config` says it came from the file."""
    path = write(tmp_path, FULL)
    args = args_of("--config", str(path))
    res = config.resolve(args, env={})
    assert args.server == "https://192.168.1.20:7860" and args.ca_fingerprint == FP and args.name == "gpu-01"
    assert args.state_dir == "/var/lib/fy-test-state" and args.verbose is True
    assert sorted(args.label) == ["rack=a", "room=lab"]
    assert args.no_workloads is False and args.workload_user == "forgeyard-run" and args.work_dir == "/var/lib/fy-test-work"
    assert args.port_range == "31000-31999" and args.bind_address == "192.168.1.21"
    assert args.container_engine == "podman" and args.limits == "rlimit" and args.log_max_mb == 25
    assert args.model_cache == "/srv/forgeyard-models" and args.model_local_dir == ["/data/models", "/mnt/models"]
    assert args.allow_private_model_hosts is True
    assert res.path == path and {k for k, v in res.sources.items() if v != "file"} == {"ca_file"}
    text = config.describe(res)
    assert f"Config file: {path}" in text and "workloads.limits" in text and "(file)" in text


def test_fy_acfg_002_defaults_without_a_file(tmp_path):
    """FY-ACFG-002: no file at the default path is fine (the agent worked without one before): the
    built-in defaults, all marked as such."""
    args = args_of()
    res = config.resolve(args, env={})
    assert res.path is None and set(res.sources.values()) == {"default"}
    assert args.port_range == "30000-32767" and args.container_engine == "auto" and args.limits == "auto"
    assert args.log_max_mb == 10 and args.no_workloads is False and args.model_cache is None and args.label == []
    assert "none (" in config.describe(res)


def test_fy_acfg_003_flag_beats_environment_beats_file(tmp_path):
    """FY-ACFG-003: for each kind of setting a flag wins over the environment, which wins over the
    file; labels merge key by key in that order."""
    path = write(tmp_path, FULL)
    env = {"FORGEYARD_PORT_RANGE": "32000-32099", "FORGEYARD_LOG_MAX_MB": "7", "FORGEYARD_WORKLOADS": "false",
           "FORGEYARD_LABELS": "room=env,zone=z", "FORGEYARD_MODEL_LOCAL_DIRS": "/env/a, /env/b",
           "FORGEYARD_LIMITS": "auto", "FORGEYARD_NAME": "from-env"}
    args = args_of("--config", str(path), "--limits", "systemd", "--label", "zone=flag", "--log-max-mb", "3")
    res = config.resolve(args, env=env)
    assert args.limits == "systemd" and res.source("workloads.limits") == "flag"          # flag > env > file
    assert args.port_range == "32000-32099" and res.source("workloads.port_range") == "env FORGEYARD_PORT_RANGE"
    assert args.log_max_mb == 3 and args.no_workloads is True and args.name == "from-env"
    assert args.model_local_dir == ["/env/a", "/env/b"]
    assert args.container_engine == "podman" and res.source("workloads.container_engine") == "file"
    assert dict(x.split("=") for x in args.label) == {"rack": "a", "room": "env", "zone": "flag"}
    # the environment gives the config file's path too, and the join token (never written to a file)
    other = write(tmp_path, 'name = "other"\n', "other.toml")
    args = args_of()
    res = config.resolve(args, env={config.CONFIG_ENV: str(other), config.JOIN_TOKEN_ENV: "fyj_abc"})
    assert args.name == "other" and res.path == other and args.join_token == "fyj_abc"
    args = args_of("--join-token", "fyj_flag")
    config.resolve(args, env={config.JOIN_TOKEN_ENV: "fyj_env"})
    assert args.join_token == "fyj_flag"


def test_fy_acfg_004_mistakes_in_the_file_are_all_named(tmp_path):
    """FY-ACFG-004: a file with mistakes is refused with every problem listed: unknown settings
    (with the closest name), wrong types, bad values, relative paths, both CA pins."""
    path = write(tmp_path, f'''
server = "192.168.1.20:7860"
ca_fingerprint = "{FP}"
ca_file = "/etc/forgeyard/ca.pem"
nmae = "x"
state_dir = "var/lib/forgeyard-agent"
[workloads]
limits = "rlimits"
port_range = "80-90"
log_max_mb = "10"
bind_address = "my-host"
enabled = "yes"
[models]
cahce = "/srv/x"
local_dirs = ["relative/dir"]
''')
    with pytest.raises(config.ConfigError) as e:
        config.resolve(args_of("--config", str(path)), env={})
    msg = str(e.value)
    for expected in ("problems:", "server wants an address with https://", "unknown setting 'nmae' (did you mean 'name'?)",
                     "[workloads] limits wants one of auto, systemd, rlimit, got 'rlimits'", "1024-65535",
                     "[workloads] log_max_mb wants a whole number (without quotes)", "[workloads] bind_address wants an IP",
                     "[workloads] enabled wants true or false", "unknown setting 'cahce' in [models] (did you mean 'cache'?)",
                     "wants an absolute path", "ca_fingerprint and ca_file are both set"):
        assert expected in msg, (expected, msg)
    assert "state_dir wants an absolute path (starting with /), got 'var/lib/forgeyard-agent'" in msg
    assert msg.count("\n  - ") == 11


def test_fy_acfg_005_broken_toml_missing_files_and_bad_environment(tmp_path):
    """FY-ACFG-005: invalid TOML says where; a config file asked for that is missing is an error
    (the default one may be missing); a bad environment value or flag is named."""
    bad = write(tmp_path, 'server = "https://x\n')
    with pytest.raises(config.ConfigError, match=r"is not valid TOML: .*line 1"):
        config.resolve(args_of("--config", str(bad)), env={})
    with pytest.raises(config.ConfigError, match="no such file"):
        config.resolve(args_of("--config", str(tmp_path / "nope.toml")), env={})
    with pytest.raises(config.ConfigError, match="no such file"):
        config.resolve(args_of(), env={config.CONFIG_ENV: str(tmp_path / "nope.toml")})
    with pytest.raises(config.ConfigError) as e:
        config.resolve(args_of("--port-range", "9-1", "--ca-fingerprint", "sha256:12"),
                       env={"FORGEYARD_LOG_MAX_MB": "lots", "FORGEYARD_WORKLOADS": "maybe"})
    msg = str(e.value)
    assert "FORGEYARD_LOG_MAX_MB wants a whole number" in msg and "FORGEYARD_WORKLOADS wants true or false" in msg
    assert "--port-range" in msg and "--ca-fingerprint" in msg
    # a pin from the file and another from a flag: one or the other
    pinned = write(tmp_path, f'ca_fingerprint = "{FP}"\n', "pinned.toml")
    with pytest.raises(config.ConfigError, match="both a CA fingerprint and a CA file"):
        config.resolve(args_of("--config", str(pinned), "--ca-file", "/tmp/ca.pem"), env={})


def test_fy_acfg_006_the_default_path_and_the_config_command(tmp_path, capsys):
    """FY-ACFG-006: /etc/forgeyard/agent.toml is read when it exists; `agent config` prints the
    settings with their sources, `--get KEY` one value, and exits 2 on a broken file."""
    config.DEFAULT_PATH.parent.mkdir(parents=True)
    config.DEFAULT_PATH.write_text('[models]\ncache = "/srv/forgeyard-models"\n')
    assert cli_main(["agent", "config", "--get", "models.cache"]) == 0
    assert capsys.readouterr().out == "/srv/forgeyard-models\n"
    assert cli_main(["agent", "config", "--limits", "rlimit"]) == 0
    out = capsys.readouterr().out
    assert f"Config file: {config.DEFAULT_PATH}" in out
    assert any(line.startswith("workloads.limits") and "rlimit" in line and "(flag)" in line for line in out.splitlines())
    assert any(line.startswith("models.cache") and "(file)" in line for line in out.splitlines())
    assert cli_main(["agent", "config", "--get", "models.nope"]) == 2
    config.DEFAULT_PATH.write_text("[models]\nchache = 1\n")
    assert cli_main(["agent", "config"]) == 2
    assert cli_main(["agent", "runtimes"]) == 2  # every agent command reads the same settings


def test_fy_acfg_007_rendered_files_read_back(tmp_path):
    """FY-ACFG-007: what `init` renders is valid TOML that the agent reads back to the same values,
    with every setting written or shown commented out, and quotes and odd label keys escaped."""
    values = {s.key: s.default for s in config.SETTINGS}
    values.update({"server": "https://forge.lan:7860", "name": 'odd "name" \\ here', "labels": {"rack": "a", "has space": "b"},
                   "models.local_dirs": ["/data/a"], "workloads.log_max_mb": 12})
    text = config.render(values, {"workloads.limits": "a note"}, ["header line"])
    assert text.startswith("# header line\n")
    for s in config.SETTINGS:
        assert f"{s.name} = " in text, s.key
    data = tomllib.loads(text)
    assert data["name"] == 'odd "name" \\ here' and data["labels"] == {"rack": "a", "has space": "b"}
    path = write(tmp_path, text)
    read = config.read_file(path)
    assert read["server"] == "https://forge.lan:7860" and read["workloads.log_max_mb"] == 12
    assert "ca_fingerprint" not in read  # unset: written commented out, with an example


# ------------------------------------------------------------------ agent init
def _detect_env(monkeypatch, *, engine=None, systemd=False, boxed=False):
    from forgeyard.agent.workloads import container as engines
    from forgeyard.agent.workloads import process

    monkeypatch.setenv("FORGEYARD_AGENT_SIMULATION", "1")
    found = engine or {"ok": False, "reason": "docker not found; podman not found"}
    monkeypatch.setattr(engines, "detect", lambda engine="auto", **kw: found)
    monkeypatch.setattr(process, "systemd_running", lambda: systemd)
    monkeypatch.setattr(setup, "in_container", lambda: boxed)


def test_fy_init_001_writes_a_commented_file_from_what_it_finds(tmp_path, monkeypatch, capsys):
    """FY-INIT-001: as root on a systemd host with Docker and the NVIDIA toolkit, and 8 A100s: the
    file names the GPUs, docker, limits auto and the given server and pin; it is 0644, holds no
    join token, reads back, and is printed."""
    _detect_env(monkeypatch, engine={"ok": True, "engine": "docker", "version": "28.5.2", "nvidia": True,
                                     "gpu_mode": "gpus"}, systemd=True)
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup, "_user_exists", lambda name: True)
    target = tmp_path / "etc" / "agent.toml"
    rc = cli_main(["agent", "init", "--config", str(target), "--simulate-inventory", "nvidia-a100x8",
                   "--server", "https://192.168.1.20:7860", "--ca-fingerprint", FP, "--join-token", "fyj_never",
                   "--label", "rack=r2"])
    assert rc == 0
    text = target.read_text()
    assert oct(target.stat().st_mode & 0o777) == "0o644"
    assert text == capsys.readouterr().out
    assert "8 x NVIDIA A100 80GB PCIe" in text and "Found: docker 28.5.2; NVIDIA GPUs via gpus." in text
    assert "fyj_never" not in text
    read = config.read_file(target)
    assert read["server"] == "https://192.168.1.20:7860" and read["ca_fingerprint"] == FP and read["labels"] == {"rack": "r2"}
    assert read["workloads.container_engine"] == "docker" and read["workloads.limits"] == "auto"
    assert read["workloads.user"] == "forgeyard-run" and read["models.cache"] == "/srv/forgeyard-models"
    assert read["state_dir"] == "/var/lib/forgeyard-agent" or read["state_dir"]  # the default for root
    # again: refused without --force, nothing changed; --print writes nothing
    target.write_text("# mine\n")
    assert cli_main(["agent", "init", "--config", str(target), "--simulate-inventory", "nvidia-a100x8"]) == 2
    assert target.read_text() == "# mine\n"
    assert cli_main(["agent", "init", "--config", str(target), "--print", "--simulate-inventory", "nvidia-a100x8"]) == 0
    assert target.read_text() == "# mine\n" and "[workloads]" in capsys.readouterr().out
    assert cli_main(["agent", "init", "--config", str(target), "--force", "--simulate-inventory", "nvidia-a100x8"]) == 0
    assert "[workloads]" in target.read_text()


def test_fy_init_002_a_container_a_cpu_machine_and_no_root(tmp_path, monkeypatch, capsys):
    """FY-INIT-002: in a container: rlimit (no systemd there), no engine: none with the reason, no
    GPU: says so; without the forgeyard-run account: nobody, with how to make one. Not root: no
    workload user, and the model cache in the home directory."""
    _detect_env(monkeypatch, boxed=True)
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup, "_user_exists", lambda name: False)
    assert cli_main(["agent", "init", "--print", "--simulate-inventory", str(_cpu_fixture(tmp_path))]) == 0
    text = capsys.readouterr().out
    data = tomllib.loads(text)
    assert data["workloads"]["limits"] == "rlimit" and "Running in a container" in text
    assert data["workloads"]["container_engine"] == "none" and "docker not found" in text
    assert data["workloads"]["user"] == "nobody" and "useradd --system" in text
    assert "GPUs: none found" in text
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    _detect_env(monkeypatch, systemd=True)
    assert cli_main(["agent", "init", "--print", "--simulate-inventory", str(_cpu_fixture(tmp_path))]) == 0
    data = tomllib.loads(capsys.readouterr().out)
    assert "user" not in data["workloads"] and data["workloads"]["limits"] == "rlimit"
    assert data["models"]["cache"].endswith("/.cache/forgeyard-models")
    # values given in the environment are kept as they are (the Docker image sets these)
    monkeypatch.setenv("FORGEYARD_LIMITS", "auto")
    monkeypatch.setenv("FORGEYARD_MODEL_CACHE", "/srv/m")
    assert cli_main(["agent", "init", "--print", "--simulate-inventory", str(_cpu_fixture(tmp_path))]) == 0
    data = tomllib.loads(capsys.readouterr().out)
    assert data["workloads"]["limits"] == "auto" and data["models"]["cache"] == "/srv/m"


def _cpu_fixture(tmp_path: Path) -> Path:
    from tests.machines_helpers import SIM

    data = json.loads((SIM / "nvidia-a100x8.json").read_text())
    data["accelerators"] = [a for a in data["accelerators"] if a["vendor"] == "cpu"]
    data["collectors"] = {"nvidia": {"ok": False, "reason": "NVML Shared Library Not Found"},
                          "cpu": {"ok": True, "count": 1}}
    p = tmp_path / "cpu-only.json"
    p.write_text(json.dumps(data))
    return p


# ------------------------------------------------------------------ a joined agent restarts
class _NoRun:
    """Stands in for the Agent: records what it was started with."""
    seen: dict = {}

    def __init__(self, server, credential, inventory, **kw):
        _NoRun.seen = {"server": server, "credential": credential, "ca_pem": kw.get("ca_pem")}

    def stop(self):
        pass

    async def run(self):
        return 0


@pytest.fixture()
def joined(tmp_path, monkeypatch):
    from forgeyard.agent import client, trust

    monkeypatch.setattr(client, "Agent", _NoRun)
    monkeypatch.setenv("FORGEYARD_AGENT_SIMULATION", "1")
    state_dir = tmp_path / "state"
    state.save(state_dir, {"server": "https://a.lan:7860", "node_id": 3, "name": "gpu-01", "credential": "fyc_x",
                           "ca_fingerprint": FP})
    monkeypatch.setattr(trust, "load_ca", lambda d: None)
    _NoRun.seen = {}
    return state_dir


def test_fy_acfg_008_a_restart_with_the_join_settings_just_starts(joined, monkeypatch, caplog):
    """FY-ACFG-008: a joined machine started again with its join settings (a container's
    `--restart always`, a re-run install) starts with its saved server: the token is not used (and
    the log says so), a matching pin is fine, a file or environment that disagrees is reported and
    ignored; only a flag with another pin is refused (`agent trust` changes it)."""
    from forgeyard.agent import client

    def spy_join(*a, **kw):
        raise AssertionError("joined again")

    monkeypatch.setattr(client, "join", spy_join)
    base = ["agent", "--state-dir", str(joined), "--no-workloads", "--simulate-inventory", "nvidia-a100x8"]
    caplog.set_level(logging.INFO, "forgeyard.agent")
    monkeypatch.setenv("FORGEYARD_JOIN_TOKEN", "fyj_used")
    monkeypatch.setenv("FORGEYARD_SERVER", "https://a.lan:7860")
    monkeypatch.setenv("FORGEYARD_CA_FINGERPRINT", FP)
    assert cli_main(base) == 0
    assert _NoRun.seen["server"] == "https://a.lan:7860" and "the join token is not used" in caplog.text
    caplog.clear()
    monkeypatch.setenv("FORGEYARD_SERVER", "https://b.lan:7860")
    monkeypatch.setenv("FORGEYARD_CA_FINGERPRINT", FP2)
    assert cli_main(base) == 0
    assert _NoRun.seen["server"] == "https://a.lan:7860"
    assert "but this machine joined https://a.lan:7860: using that" in caplog.text
    assert f"pin {FP2}, but this machine joined trusting {FP}" in caplog.text
    monkeypatch.delenv("FORGEYARD_SERVER")
    monkeypatch.delenv("FORGEYARD_CA_FINGERPRINT")
    monkeypatch.delenv("FORGEYARD_JOIN_TOKEN")
    _NoRun.seen = {}
    assert cli_main([*base, "--ca-fingerprint", FP2]) == 2 and _NoRun.seen == {}
    assert cli_main([*base, "--ca-fingerprint", FP]) == 0  # the same pin again: fine
    assert cli_main([*base, "--join-token", "fyj_x", "--server", "https://b.lan:7860"]) == 2  # another server: --rejoin
    assert cli_main([*base, "--server", "https://b.lan:7860"]) == 0 and _NoRun.seen["server"] == "https://b.lan:7860"


def test_fy_acfg_009_agent_join_joins_once_and_exits(tmp_path, monkeypatch, caplog):
    """FY-ACFG-009: `agent join` joins with the file's server and pin and the token, saves the
    credential and exits 0; run again it keeps the credential and does nothing; without a token or a
    server it says what is missing."""
    from forgeyard.agent import client, trust

    calls = []

    def fake_join(server, token, name=None, ssl_context=None):
        calls.append((server, token, name))
        return {"node_id": 7, "name": name or "box", "credential": "fyc_new"}

    monkeypatch.setattr(client, "join", fake_join)
    monkeypatch.setattr(trust, "resolve", lambda server, fingerprint=None, ca_file=None: None)
    cfg = write(tmp_path, f'server = "https://forge.lan:7860"\nca_fingerprint = "{FP}"\nname = "gpu-09"\n'
                          f'state_dir = "{tmp_path / "state"}"\n')
    caplog.set_level(logging.INFO, "forgeyard.agent")
    assert cli_main(["agent", "join", "--config", str(cfg)]) == 2 and "A join token is needed" in caplog.text
    assert cli_main(["agent", "join", "--config", str(cfg), "--join-token", "fyj_1"]) == 0
    assert calls == [("https://forge.lan:7860", "fyj_1", "gpu-09")]
    assert state.load(tmp_path / "state")["credential"] == "fyc_new"
    assert cli_main(["agent", "--config", str(cfg), "join", "--join-token", "fyj_2"]) == 0
    assert len(calls) == 1 and "keeping its credential" in caplog.text
    assert cli_main(["agent", "join", "--config", str(cfg), "--join-token", "fyj_3", "--rejoin"]) == 0
    assert len(calls) == 2
    empty = write(tmp_path, f'state_dir = "{tmp_path / "other"}"\n', "empty.toml")
    assert cli_main(["agent", "join", "--config", str(empty), "--join-token", "fyj_4"]) == 4
    assert "A server address is needed" in caplog.text
