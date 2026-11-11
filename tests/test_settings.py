"""Server settings: parity between the code, .env.example and the reference, validation at start,
and the checks for misspelled, ignored and risky settings (FY-CFG-*)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from forgeyard import settings_check
from forgeyard.config import Settings, get_settings
from forgeyard.db import new_session
from forgeyard.guide import directory

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
LINE = re.compile(r"^(?:# )?([A-Z][A-Z0-9_]+)=(.*)$")
# The settings people set most: active lines at the top of .env.example, and the reference's
# "Common settings" table.
COMMON = {"HOST", "PORT", "PUBLIC_URL", "DATA_DIR", "DATABASE_URL", "TLS", "TLS_CERT_FILE", "TLS_KEY_FILE",
          "TLS_CA_FILE", "REGISTRATION", "HF_ENDPOINT", "EGRESS_PROXY"}


def _example() -> dict[str, tuple[str, bool]]:
    """NAME -> (value, active) for each setting line of .env.example."""
    out = {}
    for line in EXAMPLE.read_text().splitlines():
        m = LINE.match(line)
        if m:
            assert m.group(1) not in out, f"{m.group(1)} is in .env.example twice"
            out[m.group(1)] = (m.group(2), not line.startswith("#"))
    return out


def test_fy_cfg_001_env_example_lists_every_setting_with_its_default():
    """FY-CFG-001: every server setting is in .env.example, nothing else is, and the value shown is
    the default (read the way the server reads it). Common settings are active lines; the advanced
    ones are commented out, so copying the file changes nothing."""
    example = _example()
    fields = Settings.model_fields
    assert set(example) == {f.upper() for f in fields}, set(example) ^ {f.upper() for f in fields}
    for name, (value, active) in example.items():
        field = name.lower()
        parsed = Settings(_env_file=None, **{field: value}) if value else None
        default = fields[field].default
        if parsed is not None:
            assert getattr(parsed, field) == default, (name, value, default)
        else:
            assert default in ("", None), (name, default)
        assert active == (name in COMMON), name


def test_fy_cfg_002_the_reference_has_every_setting_and_only_real_ones():
    """FY-CFG-002: in both languages, the server settings part of the reference names every setting
    (FY-GUIDE-007 checks that direction too), names no variable that is not one, and its Common
    settings table lists exactly the common ones."""
    known = {f.upper() for f in Settings.model_fields}
    for lang in ("en", "zh-TW"):
        text = (directory(lang) / "reference.md").read_text()
        server = text.split("\n## ", 2)[1]  # from "## Server settings" to the next "## "
        named = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)`", server, re.M))
        named |= set(re.findall(r"^\| `[A-Z0-9_]+`, `([A-Z][A-Z0-9_]+)`", server, re.M))
        assert named <= known, (lang, named - known)
        for name in known:
            assert f"`{name}`" in server, (lang, name)
        common = server.split("\n### ", 2)[1]
        names = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)`", common, re.M))
        names |= set(re.findall(r"^\| `[A-Z0-9_]+`, `([A-Z][A-Z0-9_]+)`", common, re.M))
        assert names == COMMON, (lang, names ^ COMMON)


def test_fy_cfg_003_unknown_names_get_did_you_mean():
    """FY-CFG-003: a name in .env that is not a setting is reported, with the closest setting; in the
    environment only names with a prefix only Forgeyard uses (GATEWAY_, PROVIDER_…) or very close to a
    setting are (HOME, PATH, HOSTNAME, a CI runner's AGENT_TOOLSDIRECTORY are not); settings in another case, and the agent's FORGEYARD_* names, are fine."""
    env = {"HOME": "/root", "PATH": "/bin", "HOSTNAME": "x", "public_url": "https://a", "GATEWAY_CLOUD_DEFUALT": "on",
           "PUBLC_URL": "https://b", "FORGEYARD_SERVER": "https://s", "FORGEYARD_MODEL_CACHE": "/srv",
           "FORGEYARD_SECRET_KEY_FILE": "/k", "HF_HOME": "/hf", "TLS": "on",
           "AGENT_TOOLSDIRECTORY": "/opt/hostedtoolcache", "MODEL_NAME": "x", "TLS_VERSION": "1.3",
           "AGENT_OFFLINE_SECOND": "40"}
    found = {name: (where, hint) for name, where, hint in
             settings_check.unknown_names(env, ["DATA_DIRS", "PORT", "TOTALLY_UNRELATED"])}
    assert found == {"DATA_DIRS": (".env", "DATA_DIR"), "TOTALLY_UNRELATED": (".env", None),
                     "GATEWAY_CLOUD_DEFUALT": ("the environment", "GATEWAY_CLOUD_DEFAULT"),
                     "PUBLC_URL": ("the environment", "PUBLIC_URL"),
                     "AGENT_OFFLINE_SECOND": ("the environment", "AGENT_OFFLINE_SECONDS"),
                     "FORGEYARD_SECRET_KEY_FILE": ("the environment", "FORGEYARD_SECRET_KEY")}
    probs = settings_check.static_problems(Settings(_env_file=None), env, ["DATA_DIRS"])
    p = next(p for p in probs if p.scope == "setting:DATA_DIRS")
    assert p.id == "setting-unknown" and p.severity == "warning" and "Did you mean DATA_DIR?" in p.detail


@pytest.mark.parametrize("value,stored", [("auto", "auto"), ("TRUE", "true"), ("false", "false"), ("1", "true"),
                                          ("no", "false"), (" Auto ", "auto")])
def test_fy_cfg_004_cookie_secure_accepts_three_values(value, stored):
    """FY-CFG-004: COOKIE_SECURE is auto, true or false (yes/no/1/0 read as such); anything else stops
    the start instead of acting as auto."""
    assert Settings(_env_file=None, cookie_secure=value).cookie_secure == stored
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        Settings(_env_file=None, cookie_secure="sometimes")


def _ids(probs) -> dict[str, list]:
    out: dict[str, list] = {}
    for p in probs:
        out.setdefault(p.id, []).append(p)
    return out


def test_fy_cfg_005_tls_settings_that_do_nothing_are_reported():
    """FY-CFG-005: TLS=off with TLS_* files or names set, TLS_CA_FILE without an own certificate, and
    TLS_EXTRA_NAMES with an own certificate are each reported as ignored; a correct own certificate
    with its CA is not. (Only one of TLS_CERT_FILE/TLS_KEY_FILE stops the start: FY-TLS-005.)"""
    def tls(**kw):
        return _ids(settings_check.static_problems(Settings(_env_file=None, **kw), {}, [])).get("tls-setting-ignored", [])

    assert tls() == []
    off = tls(tls="off", tls_cert_file="/c", tls_key_file="/k")
    assert len(off) == 1 and "TLS_CERT_FILE, TLS_KEY_FILE are ignored" == off[0].title
    assert tls(tls="off", tls_extra_names="a.lan")[0].title == "TLS_EXTRA_NAMES is ignored"
    assert tls(tls_ca_file="/ca.pem")[0].title == "TLS_CA_FILE is ignored"
    assert tls(tls_cert_file="/c", tls_key_file="/k", tls_extra_names="x")[0].title == "TLS_EXTRA_NAMES is ignored"
    assert tls(tls_cert_file="/c", tls_key_file="/k", tls_ca_file="/ca.pem") == []
    assert tls(tls_extra_names="gpu.lan") == []


def test_fy_cfg_006_risky_combinations():
    """FY-CFG-006: both secret-key settings; an offline time under 2.5 heartbeats (exactly 2.5 is
    fine); either private-hosts switch; COOKIE_SECURE=false over HTTPS (not with TLS=off)."""
    def ids(**kw):
        return _ids(settings_check.static_problems(Settings(_env_file=None, **kw), {}, []))

    assert ids() == {}
    assert "secret-key-both" in ids(forgeyard_secret_key="a" * 44, secret_key_file="/k")
    assert "secret-key-both" not in ids(secret_key_file="/k")
    assert "heartbeat-flapping" in ids(agent_heartbeat_seconds=10, agent_offline_seconds=24)
    assert "heartbeat-flapping" not in ids(agent_heartbeat_seconds=10, agent_offline_seconds=25)
    assert len(ids(model_allow_private_hosts=True, provider_allow_private_hosts=True)["private-hosts-allowed"]) == 2
    assert "cookie-not-secure" in ids(cookie_secure="false")
    assert "cookie-not-secure" not in ids(cookie_secure="false", tls="off")
    assert "cookie-not-secure" not in ids(cookie_secure="true")


def test_fy_cfg_007_setup_values_left_behind_and_ignored_seeds(owner, monkeypatch):
    """FY-CFG-007: once an owner exists, ADMIN_PASSWORD (warning) and FORGEYARD_SETUP_TOKEN (info)
    are reported. An env seed is reported only when it is set and the database holds another value:
    not while the seed is still used, nor when they agree, nor for a seed that is not set (MODEL_URL_HOSTS
    here). Directories compare case-sensitively."""
    monkeypatch.setenv("ADMIN_PASSWORD", "some-old-password-1")
    monkeypatch.setenv("GATEWAY_MAX_RPM", "30")
    monkeypatch.setenv("GATEWAY_CLOUD_DEFAULT", "fallback")
    monkeypatch.setenv("GATEWAY_CLOUD_MAX", "fallback")
    monkeypatch.setenv("MODEL_LOCAL_DIRS", "/srv/Models")
    get_settings.cache_clear()
    db = new_session()
    try:
        found = _ids(settings_check.database_problems(get_settings(), db))
        assert found["admin-password-left"][0].severity == "warning"
        assert found["setup-token-left"][0].severity == "info"
        assert "env-seed-ignored" not in found  # no row saved yet: the seeds are what is used
    finally:
        db.close()
    assert owner.put("/api/keys/limits", json={"max_rpm": 30}).status_code == 200      # agrees
    assert owner.put("/api/keys/cloud", json={"default": "off"}).status_code == 200    # differs
    assert owner.put("/api/model-sources", json={"url_hosts": ["mirror.example.org"], "local_dirs": ["/srv/models"]}).status_code == 200
    db = new_session()
    try:
        seeds = _ids(settings_check.database_problems(get_settings(), db))["env-seed-ignored"]
        assert sorted(p.scope for p in seeds) == ["setting:GATEWAY_CLOUD_DEFAULT", "setting:MODEL_LOCAL_DIRS"]
        assert all(p.severity == "info" and "(owner)" in p.detail for p in seeds)
        cloud = next(p for p in seeds if p.scope == "setting:GATEWAY_CLOUD_DEFAULT")
        assert cloud.fix_url == "/keys" and "API keys → Cloud for keys" in cloud.detail
    finally:
        db.close()


def test_fy_cfg_008_the_seed_table_names_the_real_rows():
    """FY-CFG-008: the seeds the check compares are the server_settings rows the code reads."""
    from forgeyard.gateway import keys as gkeys
    from forgeyard.models import catalog

    assert {key for key, *_ in settings_check.SEEDS} == {catalog.SOURCES_KEY, gkeys.LIMITS_KEY, gkeys.CLOUD_KEY}
    for _key, _field, env_field, _kind in settings_check.SEEDS:
        assert env_field in Settings.model_fields


def test_fy_cfg_009_the_server_logs_settings_problems_at_start(monkeypatch, caplog):
    """FY-CFG-009: starting the server logs each problem once, as a warning."""
    from tests.conftest import make_client

    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    monkeypatch.setenv("GATEWAY_CLOUD_DEFUALT", "on")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="forgeyard"):
        with make_client():
            pass
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Settings: ")]
    assert any("Cloud providers may use private addresses" in m for m in lines)
    assert any("GATEWAY_CLOUD_DEFUALT is not a setting" in m and "Did you mean GATEWAY_CLOUD_DEFAULT?" in m
               for m in lines)


def test_fy_cfg_010_a_bad_setting_stops_serve_with_a_plain_message(monkeypatch, caplog):
    """FY-CFG-010: `forgeyard serve` with an invalid value exits 2 and names the setting, before it
    touches the database or listens."""
    from forgeyard import cli

    monkeypatch.setenv("COOKIE_SECURE", "sometimes")
    get_settings.cache_clear()
    with caplog.at_level(logging.ERROR, logger="forgeyard"):
        assert cli._serve() == 2
    assert any("Setting COOKIE_SECURE: COOKIE_SECURE must be 'auto', 'true' or 'false'" in r.getMessage()
               for r in caplog.records)
    monkeypatch.setenv("COOKIE_SECURE", "auto")
    get_settings.cache_clear()


def test_fy_cfg_011_dotenv_names_are_read_from_the_file(tmp_path):
    """FY-CFG-011: the names of a .env file (comments, blanks and export lines handled), and nothing
    when there is no file."""
    f = tmp_path / ".env"
    f.write_text("# a comment\nHOST=0.0.0.0\n\nexport PUBLIC_URl=https://x\n# PORT=1\n")
    assert settings_check.dotenv_names(f) == ["HOST", "PUBLIC_URl"]
    assert settings_check.dotenv_names(tmp_path / "missing") == []

