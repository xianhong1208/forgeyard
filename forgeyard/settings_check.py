"""Server settings that are valid one by one but wrong together, ignored, or misspelled.

Bad single values stop the start in `Settings` itself (forgeyard/config.py: TLS, REGISTRATION,
COOKIE_SECURE, the cloud levels), and so does an own certificate with only one of its two files
(forgeyard/security/tls.py). What is left is found here, without refusing anything:

- a name in the environment or in `.env` that is not a setting but looks like one ("did you mean");
- a setting that another one makes useless (TLS_CA_FILE without an own certificate, any TLS_* with
  TLS=off, SECRET_KEY_FILE next to FORGEYARD_SECRET_KEY);
- values that work but are risky (heartbeat against offline time, the private-address switches,
  non-Secure cookies over HTTPS);
- with the database: setup values left behind after setup, and env seeds that the database
  overrides (MODEL_URL_HOSTS, GATEWAY_DEFAULT_RPM, GATEWAY_CLOUD_MAX, ...).

The server logs every problem at start (forgeyard/app.py) and admins see them in the setup advisor
(forgeyard/advisor.py), which uses the same functions.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from forgeyard.config import Settings

REFERENCE = "/guide/en/reference"

# Names in the environment with these prefixes belong to Forgeyard: an unknown one is a typo or a
# removed setting. Only prefixes no other software uses: AGENT_, MODEL_, TLS_ or SESSION_ are common
# elsewhere (a CI runner sets AGENT_TOOLSDIRECTORY), so those names count only when they are close.
PREFIXES = ("GATEWAY_", "PROVIDER_", "EGRESS_", "BUDGET_", "OPENROUTER_", "VRAM_", "FORGEYARD_")
# Unprefixed names in the environment are checked only when they are this close to a setting:
# the environment holds everything else on the machine (HOME, PATH, HOSTNAME...).
CLOSE_ENOUGH = 0.85
# FORGEYARD_* names that are not server settings but are read elsewhere (the agent, install.sh, the
# variables a workload gets), so a server and an agent can share one environment.
OTHER_FORGEYARD = {"FORGEYARD_AGENT_CONFIG", "FORGEYARD_JOIN_TOKEN", "FORGEYARD_AGENT_STATE_DIR",
                   "FORGEYARD_AGENT_SIMULATION", "FORGEYARD_BIN_DIR", "FORGEYARD_HOME", "FORGEYARD_INSTALL_ANY_USER",
                   "FORGEYARD_SYSTEMD", "FORGEYARD_UNIT_DIR", "FORGEYARD_REPLICA", "FORGEYARD_REPLICA_INDEX",
                   "FORGEYARD_WORKLOAD"}
# Machines flap online and offline when a heartbeat can be missed this few times.
HEARTBEATS_PER_OFFLINE = 2.5

# Env values that only seed a database setting: once an admin saved the setting, the env is ignored.
# (setting key in server_settings, field in that row, env field, how the row stores it)
SEEDS = (
    ("model_sources", "url_hosts", "model_url_hosts", "hosts"),
    ("model_sources", "local_dirs", "model_local_dirs", "dirs"),
    ("gateway_limits", "default_rpm", "gateway_default_rpm", "limit"),
    ("gateway_limits", "default_tpm", "gateway_default_tpm", "limit"),
    ("gateway_limits", "max_rpm", "gateway_max_rpm", "limit"),
    ("gateway_limits", "max_tpm", "gateway_max_tpm", "limit"),
    ("gateway_cloud", "default", "gateway_cloud_default", "level"),
    ("gateway_cloud", "max", "gateway_cloud_max", "level"),
)
SEED_PAGE = {"model_sources": ("/models?tab=catalog", "Models → Model sources"),
             "gateway_limits": ("/keys", "API keys → Limits for keys"),
             "gateway_cloud": ("/keys", "API keys → Cloud for keys")}


@dataclass
class Problem:
    id: str
    severity: str            # error | warning | info
    title: str
    detail: str
    fix_url: str = REFERENCE
    fix_label: str = "Open the reference"
    scope: str = "server"


def setting_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def other_forgeyard_names() -> set[str]:
    from forgeyard.agent import config as agent_config

    return OTHER_FORGEYARD | {s.env for s in agent_config.SETTINGS}


def dotenv_names(path: Path | str = ".env") -> list[str]:
    """The names a `.env` file sets (the one the server reads: next to the working directory)."""
    try:
        from dotenv import dotenv_values

        return [k for k in dotenv_values(path) if k]
    except (OSError, UnicodeDecodeError):
        return []


def unknown_names(environ: Mapping[str, str], dotenv: Iterable[str]) -> list[tuple[str, str, Optional[str]]]:
    """(name, where, suggestion) for each name that looks like a setting but is not one. Settings
    are read case-insensitively, so `public_url` is fine."""
    known = setting_names()
    others = other_forgeyard_names()
    choices = sorted(known)
    out: list[tuple[str, str, Optional[str]]] = []
    seen: set[str] = set()
    for where, names in ((".env", list(dotenv)), ("the environment", list(environ))):
        for name in names:
            upper = name.upper()
            if upper in known or upper in others or upper in seen:
                continue
            hint = difflib.get_close_matches(upper, choices, n=1, cutoff=0.6)
            close = difflib.get_close_matches(upper, choices, n=1, cutoff=CLOSE_ENOUGH)
            # .env is Forgeyard's own file: every unknown name in it is reported.
            if where == ".env" or upper.startswith(PREFIXES) or close:
                seen.add(upper)
                out.append((name, where, hint[0] if hint else None))
    return out


def static_problems(s: Settings, environ: Optional[Mapping[str, str]] = None,
                    dotenv: Optional[Iterable[str]] = None) -> list[Problem]:
    """What can be said from the settings alone."""
    environ = os.environ if environ is None else environ
    dotenv = dotenv_names() if dotenv is None else dotenv
    out: list[Problem] = []
    for name, where, hint in unknown_names(environ, dotenv):
        out.append(Problem("setting-unknown", "warning", f"{name} is not a setting",
                           f"{name} is set in {where}, but Forgeyard has no such setting, so it is ignored."
                           + (f" Did you mean {hint}?" if hint else ""), scope=f"setting:{name.upper()}"))
    tls_files = [n for n in ("tls_cert_file", "tls_key_file", "tls_ca_file", "tls_extra_names")
                 if getattr(s, n).strip()]
    if not s.tls_on and tls_files:
        names = ", ".join(n.upper() for n in tls_files)
        out.append(Problem("tls-setting-ignored", "warning", f"{names} {'is' if len(tls_files) == 1 else 'are'} ignored",
                           f"TLS=off: the server speaks plain HTTP, so {names} {'has' if len(tls_files) == 1 else 'have'} "
                           "no effect. Remove TLS=off to use them, or remove them.", fix_url=f"{REFERENCE}#https",
                           scope="setting:TLS"))
    elif s.tls_on:
        if s.tls_ca_file.strip() and not s.own_certificate:
            out.append(Problem("tls-setting-ignored", "warning", "TLS_CA_FILE is ignored",
                               "TLS_CA_FILE only applies with your own certificate (TLS_CERT_FILE and TLS_KEY_FILE). "
                               "Forgeyard's own certificate authority serves, and agents pin that one.",
                               fix_url=f"{REFERENCE}#https", scope="setting:TLS_CA_FILE"))
        if s.tls_extra_names.strip() and s.own_certificate:
            out.append(Problem("tls-setting-ignored", "warning", "TLS_EXTRA_NAMES is ignored",
                               "TLS_EXTRA_NAMES only adds names to Forgeyard's own server certificate. With your own "
                               "certificate, the names are the ones it was issued for.",
                               fix_url=f"{REFERENCE}#https", scope="setting:TLS_EXTRA_NAMES"))
    if s.forgeyard_secret_key.strip() and s.secret_key_file.strip():
        out.append(Problem("secret-key-both", "warning", "FORGEYARD_SECRET_KEY and SECRET_KEY_FILE are both set",
                           "FORGEYARD_SECRET_KEY is used and the file named by SECRET_KEY_FILE is ignored. "
                           "Keep only one of them.", fix_url=f"{REFERENCE}#data-and-secrets"))
    if s.agent_offline_seconds < HEARTBEATS_PER_OFFLINE * s.agent_heartbeat_seconds:
        out.append(Problem("heartbeat-flapping", "warning", "Machines may flap between online and offline",
                           f"AGENT_OFFLINE_SECONDS ({s.agent_offline_seconds}) is less than {HEARTBEATS_PER_OFFLINE} × "
                           f"AGENT_HEARTBEAT_SECONDS ({s.agent_heartbeat_seconds}): one late heartbeat marks a machine "
                           "offline. Keep it at least three heartbeats long.", fix_url=f"{REFERENCE}#machines"))
    if s.model_allow_private_hosts:
        out.append(Problem("private-hosts-allowed", "warning", "Model sources may use private addresses",
                           "MODEL_ALLOW_PRIVATE_HOSTS=true lets model downloads reach addresses on your network and "
                           "plain http. It is meant for a mirror on your LAN; turn it off otherwise.",
                           fix_url=f"{REFERENCE}#models", scope="setting:MODEL_ALLOW_PRIVATE_HOSTS"))
    if s.provider_allow_private_hosts:
        out.append(Problem("private-hosts-allowed", "warning", "Cloud providers may use private addresses",
                           "PROVIDER_ALLOW_PRIVATE_HOSTS=true lets providers marked as outside your network reach "
                           "private and loopback addresses over plain http. It is meant for testing with a stand-in.",
                           fix_url=f"{REFERENCE}#cloud-providers", scope="setting:PROVIDER_ALLOW_PRIVATE_HOSTS"))
    if s.cookie_secure == "false" and s.tls_on:
        out.append(Problem("cookie-not-secure", "warning", "Session cookies are sent without Secure",
                           "COOKIE_SECURE=false while the server speaks HTTPS: a browser would also send the session "
                           "cookie over plain HTTP. Use auto (the default).", fix_url=f"{REFERENCE}#accounts-and-sign-in"))
    return out


def _seed_value(kind: str, raw) -> object:
    """An env seed or a stored value, in one comparable shape."""
    if kind in ("hosts", "dirs"):
        items = raw if isinstance(raw, list) else str(raw or "").split(",")
        items = [str(x).strip() for x in items if str(x).strip()]
        return sorted(x.lower() for x in items) if kind == "hosts" else sorted(items)
    if kind == "limit":
        try:
            n = int(raw or 0)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None
    return str(raw or "off")


def database_problems(s: Settings, db) -> list[Problem]:
    """What needs the database: setup values left behind, and env seeds the database overrides."""
    from sqlalchemy import func, select

    from forgeyard.db import ServerSetting, User

    out: list[Problem] = []
    has_owner = (db.scalar(select(func.count()).select_from(User)) or 0) > 0
    if has_owner and s.admin_password:
        out.append(Problem("admin-password-left", "warning", "ADMIN_PASSWORD is still set",
                           "ADMIN_PASSWORD is only read on the first start, to create the owner, and it stays in the "
                           "environment or .env in plain text. Remove it (and ADMIN_USERNAME).",
                           fix_url=f"{REFERENCE}#accounts-and-sign-in"))
    if has_owner and s.forgeyard_setup_token:
        out.append(Problem("setup-token-left", "info", "FORGEYARD_SETUP_TOKEN has no use any more",
                           "Setup is done, so the setup token is never read again. Remove it.",
                           fix_url=f"{REFERENCE}#accounts-and-sign-in"))
    rows: dict[str, Optional[ServerSetting]] = {}
    for key, field, env_field, kind in SEEDS:
        if env_field not in s.model_fields_set:
            continue  # not set in the environment: nothing to be surprised about
        if key not in rows:
            rows[key] = db.get(ServerSetting, key)
        row = rows[key]
        if row is None:
            continue  # the seed is still what is used
        stored = _seed_value(kind, (row.value or {}).get(field))
        if stored != _seed_value(kind, getattr(s, env_field)):
            url, where = SEED_PAGE[key]
            name = env_field.upper()
            out.append(Problem("env-seed-ignored", "info", f"{name} in the environment is ignored",
                               f"{name} only sets the first value of {where}. An admin saved that setting"
                               f"{' (' + row.updated_by + ')' if row.updated_by else ''}, so the database value is "
                               f"used. Change it there, or remove {name}.", fix_url=url, fix_label=f"Open {where.split(' →')[0]}",
                               scope=f"setting:{name}"))
    return out


def problems(s: Settings, db=None, environ: Optional[Mapping[str, str]] = None,
             dotenv: Optional[Iterable[str]] = None) -> list[Problem]:
    out = static_problems(s, environ, dotenv)
    if db is not None:
        out += database_problems(s, db)
    return out
