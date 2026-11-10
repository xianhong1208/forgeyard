"""The agent's settings: a config file, environment variables and command-line flags.

    /etc/forgeyard/agent.toml      the file (or --config PATH, or FORGEYARD_AGENT_CONFIG)
    FORGEYARD_*                    environment variables (the Docker image uses these)
    --flags                        the command line

Each setting comes from the first of: a flag, an environment variable, the file, the built-in
default. Labels are merged key by key in the same order. `forgeyard agent init` writes a file from
what it detects on this machine; `forgeyard agent config` prints the settings in effect and where
each one came from.

`server`, `ca_fingerprint`, `ca_file` and `name` are used to **join**. Once a machine has joined,
its state directory (credential.json and the pinned server-ca.pem) says which server it talks to
and which CA it trusts; `forgeyard agent trust` changes that, never the file.
"""

from __future__ import annotations

import difflib
import ipaddress
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

DEFAULT_PATH = Path("/etc/forgeyard/agent.toml")
CONFIG_ENV = "FORGEYARD_AGENT_CONFIG"
JOIN_TOKEN_ENV = "FORGEYARD_JOIN_TOKEN"
ENGINES = ("auto", "docker", "podman", "none")
LIMITS = ("auto", "systemd", "rlimit")
LOG_LEVELS = ("info", "debug")


class ConfigError(Exception):
    """One or more settings are wrong; the message lists every problem, one per line."""


# ------------------------------------------------------------------ checks
def _server(v: str) -> str:
    from forgeyard.agent.client import normalise_server

    if "://" not in v:
        raise ValueError(f"wants an address with https://, e.g. https://192.168.1.20:7860, got {v!r}")
    return normalise_server(v)


def _fingerprint(v: str) -> str:
    from forgeyard.agent.trust import TrustError, normalise_fingerprint

    try:
        return normalise_fingerprint(v)
    except TrustError as e:
        raise ValueError(str(e)) from None


def _name(v: str) -> str:
    v = v.strip()
    if not v or len(v) > 255:
        raise ValueError("wants 1 to 255 characters")
    return v


def _abs_path(v: str) -> str:
    if not v.strip():
        raise ValueError("is empty")
    if not Path(v).is_absolute():
        raise ValueError(f"wants an absolute path (starting with /), got {v!r}")
    return v


def _port_range(v: str) -> str:
    from forgeyard.agent.workloads.ports import parse_range

    lo, hi = parse_range(v)  # raises ValueError with its own message
    return f"{lo}-{hi}"


def _bind(v: str) -> str:
    try:
        ipaddress.ip_address(v)
    except ValueError:
        raise ValueError(f"wants an IP address of this machine, e.g. 192.168.1.21, got {v!r}") from None
    return v


def _engine(v: str) -> str:
    if v in ENGINES or v.startswith("/"):
        return v
    raise ValueError(f"wants one of {', '.join(ENGINES)} or the path of the engine's CLI, got {v!r}")


def _choice(options: tuple[str, ...]) -> Callable[[str], str]:
    def check(v: str) -> str:
        if v not in options:
            raise ValueError(f"wants one of {', '.join(options)}, got {v!r}")
        return v
    return check


def _log_mb(v: int) -> int:
    if not 1 <= v <= 10240:
        raise ValueError(f"wants a number of MB from 1 to 10240, got {v}")
    return v


@dataclass(frozen=True)
class Setting:
    key: str                  # in the file: "server", "workloads.user"
    dest: str                 # the argparse destination
    env: str
    kind: str                 # str, int, bool, list, labels
    default: Any
    flag: str
    help: str
    check: Optional[Callable] = None
    example: Optional[str] = None  # shown commented out in a new file when there is no value

    @property
    def section(self) -> str:
        return self.key.rpartition(".")[0]

    @property
    def name(self) -> str:
        return self.key.rpartition(".")[2]


SETTINGS: tuple[Setting, ...] = (
    Setting("server", "server", "FORGEYARD_SERVER", "str", None, "--server",
            "The server's address, as the Machines page shows it.", _server, '"https://192.168.1.20:7860"'),
    Setting("ca_fingerprint", "ca_fingerprint", "FORGEYARD_CA_FINGERPRINT", "str", None, "--ca-fingerprint",
            "Pin the server's certificate authority by its SHA-256 fingerprint (from the join command). "
            "Only certificates it signed are accepted.", _fingerprint, '"sha256:3f9a…"'),
    Setting("ca_file", "ca_file", "FORGEYARD_CA_FILE", "str", None, "--ca-file",
            "Or pin this CA file instead (a company CA, a TLS proxy's). Not both.", _abs_path,
            '"/etc/forgeyard/company-ca.pem"'),
    Setting("name", "name", "FORGEYARD_NAME", "str", None, "--name",
            "The machine's name on the server. Default: the host name.", _name, '"gpu-01"'),
    Setting("state_dir", "state_dir", "FORGEYARD_AGENT_STATE_DIR", "str", None, "--state-dir",
            "Where the machine's credential, the pinned CA and the replicas' records are kept (mode 0700). "
            "Default: /var/lib/forgeyard-agent as root, else ~/.local/state/forgeyard-agent.", None),
    Setting("log_level", "log_level", "FORGEYARD_LOG_LEVEL", "str", "info", "-v",
            "info, or debug for every message (-v).", _choice(LOG_LEVELS)),
    Setting("labels", "label", "FORGEYARD_LABELS", "labels", {}, "--label",
            "Labels shown on the Machines page, e.g. { rack = \"a\" }. Environment: rack=a,room=2. "
            "Merged key by key: a flag wins over the environment, the environment over the file."),
    Setting("workloads.enabled", "workloads_enabled", "FORGEYARD_WORKLOADS", "bool", True, "--no-workloads",
            "false: only report hardware, run nothing."),
    Setting("workloads.user", "workload_user", "FORGEYARD_WORKLOAD_USER", "str", None, "--workload-user",
            "The unprivileged account process workloads run as when the agent runs as root (required then).",
            None, '"forgeyard-run"'),
    Setting("workloads.work_dir", "work_dir", "FORGEYARD_WORK_DIR", "str", None, "--work-dir",
            "One directory per process replica. Default: next to the state directory (<state_dir>-work).", None),
    Setting("workloads.port_range", "port_range", "FORGEYARD_PORT_RANGE", "str", "30000-32767", "--port-range",
            "Host ports for services and model replicas.", _port_range),
    Setting("workloads.bind_address", "bind_address", "FORGEYARD_BIND_ADDRESS", "str", None, "--bind-address",
            "The address published ports listen on. Default: this machine's address towards the server.", _bind,
            '"192.168.1.21"'),
    Setting("workloads.container_engine", "container_engine", "FORGEYARD_CONTAINER_ENGINE", "str", "auto",
            "--container-engine", "auto (docker, then podman), docker, podman, none, or the engine CLI's path.",
            _engine),
    Setting("workloads.limits", "limits", "FORGEYARD_LIMITS", "str", "auto", "--limits",
            "How process workloads are limited: systemd (a scope with CPU and memory caps; root on a systemd "
            "host), rlimit (no systemd, e.g. in a container), or auto.", _choice(LIMITS)),
    Setting("workloads.log_max_mb", "log_max_mb", "FORGEYARD_LOG_MAX_MB", "int", 10, "--log-max-mb",
            "Output kept per replica, in MB.", _log_mb),
    Setting("models.cache", "model_cache", "FORGEYARD_MODEL_CACHE", "str", None, "--model-cache",
            "Where models and engines are downloaded and kept. Without it this machine serves no models. "
            "In the Docker image it must be the same path inside and outside the container.", None,
            '"/srv/forgeyard-models"'),
    Setting("models.local_dirs", "model_local_dir", "FORGEYARD_MODEL_LOCAL_DIRS", "list", [], "--model-local-dir",
            "Directories local:// models may come from on this machine. Environment: comma-separated.", None),
    Setting("models.allow_private_hosts", "allow_private_model_hosts", "FORGEYARD_ALLOW_PRIVATE_MODEL_HOSTS", "bool",
            False, "--allow-private-model-hosts",
            "Allow model downloads from private addresses and plain http (a LAN mirror only)."),
)

BY_KEY = {s.key: s for s in SETTINGS}
SECTIONS = {"": "", "workloads": "Workloads: services, tasks and model replicas on this machine",
            "models": "Models"}
PATH_KEYS = {"state_dir", "workloads.work_dir", "models.cache"}


# ------------------------------------------------------------------ parsing
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _from_env(s: Setting, raw: str) -> Any:
    raw = raw.strip()
    if s.kind == "bool":
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        raise ValueError(f"wants true or false, got {raw!r}")
    if s.kind == "int":
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"wants a whole number, got {raw!r}") from None
    if s.kind == "list":
        return [p.strip() for p in raw.split(",") if p.strip()]
    if s.kind == "labels":
        return parse_labels([p for p in raw.split(",") if p.strip()])
    return raw


def parse_labels(items: list[str]) -> dict:
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"wants key=value, got {item!r}")
        out[key.strip()] = value.strip()
    return out


def _typed(s: Setting, value: Any) -> Any:
    """A value from the file, checked for its type (TOML already typed it)."""
    if s.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"wants true or false (without quotes), got {value!r}")
    elif s.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"wants a whole number (without quotes), got {value!r}")
    elif s.kind == "list":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f'wants a list of strings, e.g. ["/data/models"], got {value!r}')
    elif s.kind == "labels":
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise ValueError(f'wants a table of strings, e.g. {{ rack = "a" }}, got {value!r}')
    elif not isinstance(value, str):
        raise ValueError(f"wants a string in quotes, got {value!r}")
    return value


def _check(s: Setting, value: Any, *, in_file: bool) -> Any:
    if value is None:
        return None
    if s.kind == "list" and in_file:
        return [_abs_path(v) for v in value]
    if s.check is not None and not (s.kind == "list"):
        value = s.check(value)
    if in_file and s.key in PATH_KEYS:
        value = _abs_path(value)
    return value


def _where(s: Setting) -> str:
    return f"[{s.section}] {s.name}" if s.section else s.name


def read_file(path: Path) -> dict:
    """The file's settings by key ("workloads.user"), type-checked and validated. Every problem is
    reported at once."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path}: no such file") from None
    except OSError as e:
        raise ConfigError(f"{path}: cannot read it: {e.strerror or e}") from None
    return read_text_check(text, path)


def read_text_check(text: str, path: Path) -> dict:
    """The settings in `text` (the content of `path`), checked like a file the agent reads."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from None
    problems: list[str] = []
    values: dict = {}

    def unknown(key: str, where: str, options: list[str]) -> None:
        hint = difflib.get_close_matches(key, options, n=1)
        problems.append(f"unknown setting {key!r}{where}" + (f" (did you mean {hint[0]!r}?)" if hint else ""))

    top = [s.name for s in SETTINGS if not s.section] + [k for k in SECTIONS if k]
    for key, value in data.items():
        if key in SECTIONS and key:
            if not isinstance(value, dict):
                problems.append(f"{key} must be a section, [{key}]")
                continue
            names = [s.name for s in SETTINGS if s.section == key]
            for sub, sv in value.items():
                s = BY_KEY.get(f"{key}.{sub}")
                if s is None:
                    unknown(sub, f" in [{key}]", names)
                    continue
                try:
                    values[s.key] = _check(s, _typed(s, sv), in_file=True)
                except ValueError as e:
                    problems.append(f"{_where(s)} {e}")
            continue
        s = BY_KEY.get(key)
        if s is None or s.section:
            unknown(key, "", top)
            continue
        try:
            values[s.key] = _check(s, _typed(s, value), in_file=True)
        except ValueError as e:
            problems.append(f"{_where(s)} {e}")
    if values.get("ca_fingerprint") and values.get("ca_file"):
        problems.append("ca_fingerprint and ca_file are both set: pin one or the other")
    if problems:
        raise ConfigError(f"{path} has {len(problems)} problem{'s' if len(problems) > 1 else ''}:\n"
                          + "\n".join(f"  - {p}" for p in problems))
    return values


# ------------------------------------------------------------------ resolving
@dataclass
class Resolved:
    path: Optional[Path]
    values: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)

    def source(self, key: str) -> str:
        return self.sources.get(key, "default")


def config_path(flag: Optional[str], env: Mapping[str, str]) -> tuple[Optional[Path], bool]:
    """The file to read and whether it was asked for (then it must exist)."""
    if flag:
        return Path(flag), True
    if env.get(CONFIG_ENV):
        return Path(env[CONFIG_ENV]), True
    return (DEFAULT_PATH, False) if DEFAULT_PATH.exists() else (None, False)


def _flag_value(s: Setting, args) -> Any:
    v = getattr(args, s.dest, None)
    if s.key == "workloads.enabled":
        return False if getattr(args, "no_workloads", None) else None
    if s.key == "log_level":
        return "debug" if getattr(args, "verbose", None) else None
    if s.kind == "labels":
        return parse_labels(v) if v else None
    if s.kind == "list":
        return list(v) if v else None
    return v


def resolve(args, env: Mapping[str, str] = os.environ, *, read: bool = True) -> Resolved:
    """Fill `args` with every setting (flag, else environment, else file, else default) and say where
    each came from. Raises ConfigError listing every problem. `read=False` ignores the file (init)."""
    path, required = config_path(getattr(args, "config", None), env) if read else (None, False)
    file_values: dict = {}
    if path is not None and (required or path.exists()):
        file_values = read_file(path)
    out = Resolved(path=path if path is not None and (required or path.exists()) else None)
    problems: list[str] = []
    for s in SETTINGS:
        try:
            flag = _flag_value(s, args)
        except ValueError as e:
            problems.append(f"{s.flag} {e}")
            continue
        env_raw = env.get(s.env)
        env_value = None
        if env_raw is not None and (env_raw.strip() or s.kind == "bool"):
            try:
                env_value = _check(s, _from_env(s, env_raw), in_file=False)
            except ValueError as e:
                problems.append(f"{s.env} {e}")
                continue
        if s.kind == "labels":
            merged = {**s.default, **file_values.get(s.key, {}), **(env_value or {}), **(flag or {})}
            value = merged
            src = "flag" if flag else f"env {s.env}" if env_value else "file" if s.key in file_values else "default"
        elif flag is not None:
            try:
                value, src = _check(s, flag, in_file=False), "flag"
            except ValueError as e:
                problems.append(f"{s.flag} {e}")
                continue
        elif env_value is not None:
            value, src = env_value, f"env {s.env}"
        elif s.key in file_values:
            value, src = file_values[s.key], "file"
        else:
            value, src = (list(s.default) if isinstance(s.default, list) else s.default), "default"
        out.values[s.key] = value
        out.sources[s.key] = src
    if out.values.get("ca_fingerprint") and out.values.get("ca_file"):
        problems.append("both a CA fingerprint and a CA file are set "
                        f"({out.source('ca_fingerprint')}, {out.source('ca_file')}): pin one or the other")
    if problems:
        raise ConfigError("The agent's settings have problems:\n" + "\n".join(f"  - {p}" for p in problems))
    for s in SETTINGS:
        value = out.values[s.key]
        if s.key == "workloads.enabled":
            args.no_workloads = not value
        elif s.key == "log_level":
            args.verbose = value == "debug"
        elif s.kind == "labels":
            args.label = [f"{k}={v}" for k, v in value.items()]
        else:
            setattr(args, s.dest, value)
    if getattr(args, "join_token", None) is None and env.get(JOIN_TOKEN_ENV, "").strip():
        args.join_token = env[JOIN_TOKEN_ENV].strip()
        out.sources["join_token"] = f"env {JOIN_TOKEN_ENV}"
    return out


def plain(v: Any) -> str:
    """A value as one line of text ("" when unset)."""
    if v in (None, [], {}):
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ",".join(v)
    if isinstance(v, dict):
        return ",".join(f"{k}={x}" for k, x in v.items())
    return str(v)


def describe(res: Resolved) -> str:
    """`forgeyard agent config`: every setting in effect and where it came from."""
    lines = [f"Config file: {res.path or f'none ({DEFAULT_PATH} does not exist)'}", ""]
    width = max(len(s.key) for s in SETTINGS)
    for s in SETTINGS:
        shown = plain(res.values.get(s.key)) or "-"
        lines.append(f"{s.key:<{width}}  {shown}   ({res.source(s.key)})")
    return "\n".join(lines)


# ------------------------------------------------------------------ writing (agent init)
def toml_value(v: Any) -> str:
    import json

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_bare(k)} = {json.dumps(x)}" for k, x in v.items()) + " }" if v else "{}"
    return json.dumps(str(v))  # JSON string escapes are valid TOML basic-string escapes


def _bare(k: str) -> str:
    import json
    import re

    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def render(values: dict, notes: Optional[dict] = None, header: Optional[list[str]] = None) -> str:
    """A complete, commented agent.toml: every setting with what it does; settings without a value
    are written commented out (with an example), so the file is its own reference."""
    import textwrap

    notes = notes or {}
    out = [f"# {line}" if line else "#" for line in (header or [])]
    if out:
        out.append("")
    for section, title in SECTIONS.items():
        if section:
            out += ["", f"# {title}", f"[{section}]"]
        for s in SETTINGS:
            if s.section != section:
                continue
            for line in textwrap.wrap(s.help, 96):
                out.append(f"# {line}")
            if s.key in notes:
                out.append(f"# {notes[s.key]}")
            v = values.get(s.key, s.default)
            if v is None:
                example = s.example or '""'
                out.append(f"# {s.name} = {example}")
            else:
                out.append(f"{s.name} = {toml_value(v)}")
            out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"
