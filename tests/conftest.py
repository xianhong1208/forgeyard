import json
import os
import re
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IllegalStateChangeError
from sqlalchemy.orm import close_all_sessions
from fastapi.testclient import TestClient

from forgeyard.agent import config as agent_config
from forgeyard.config import get_settings
from forgeyard.db import reset_engine
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.budgets import ledger
from forgeyard.gateway.limits import key_limiter
from forgeyard.gateway.passthrough import in_flight
from forgeyard.gateway.proxy import forget_ceiling
from forgeyard.gateway.usage import recorder
from forgeyard.models import reach
from forgeyard.models.hub import hub
from forgeyard.security import crypto, tls
from forgeyard.security.ratelimit import limiter
from tests import leaks

OWNER = ("owner", "harbour-lantern-71")
SETUP_TOKEN = "test-setup-token-0123456789"


# TEST_DATABASE_URL=postgresql+psycopg://… runs the suite on PostgreSQL. It WIPES that database.
PG_URL = os.environ.get("TEST_DATABASE_URL", "")


def _wipe(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture(autouse=True)
def no_real_engine_socket(monkeypatch):
    """No test ever pulls through a real Docker or Podman socket on the machine running the tests:
    the container runtime finds none (it pulls with the fake CLI), unless a test hands it one."""
    from forgeyard.agent.workloads import container

    monkeypatch.setattr(container, "engine_socket", lambda *a, **k: None)


@pytest.fixture(scope="session", autouse=True)
def no_processes_left_behind():
    """Safety net for the whole run: before it, leftovers of runs that are gone are killed and
    removed (with a warning); after it, anything still running from this run's directories is
    killed and the run fails, naming it."""
    stale = leaks.stale_dirs()
    if stale:
        found = [p for d in stale for p in leaks.cleanup(d)]
        if found:
            import warnings

            warnings.warn(f"Killed processes left by an earlier test run: {leaks.describe(found)}", stacklevel=1)
    yield
    found = [p for d in leaks.made for p in leaks.cleanup(d)]
    if found:
        pytest.fail(f"Processes were still running after the tests (killed): {leaks.describe(found)}", pytrace=False)


@pytest.fixture(autouse=True)
def no_process_left_by_the_test(tmp_path):
    """Every test: nothing it started may outlive it (process workloads run in their own session,
    so a test that fails half-way would leave them running). Killed and reported as an error of
    that test."""
    made_before = len(leaks.made)
    yield
    found = leaks.check([tmp_path, *leaks.made[made_before:]])
    if found:
        pytest.fail(f"The test left processes running (killed now): {leaks.describe(found)}", pytrace=False)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Each test gets its own data dir (SQLite + secrets file) and fresh settings."""
    for key in ("ADMIN_USERNAME", "ADMIN_PASSWORD", "FORGEYARD_SETUP_TOKEN", "DATABASE_URL", "PUBLIC_URL",
                "ALLOWED_HOSTS", "TRUSTED_PROXIES", "REGISTRATION", "RATE_LIMIT_PER_MINUTE", "TLS", "TLS_CERT_FILE",
                "TLS_KEY_FILE", "TLS_CA_FILE", "TLS_EXTRA_NAMES", "SECRET_KEY_FILE", "FORGEYARD_SECRET_KEY_FILE",
                "AGENT_HEARTBEAT_SECONDS", crypto.KEY_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # The setup token, where the server keeps the one it generates (FORGEYARD_SETUP_TOKEN, which used
    # to pin it, is deprecated).
    (tmp_path / "data").mkdir(exist_ok=True)
    secrets_file = tmp_path / "data" / "secrets.json"
    secrets_file.write_text(json.dumps({"SETUP_TOKEN": SETUP_TOKEN}))
    secrets_file.chmod(0o600)
    # The agent's settings: never this machine's /etc/forgeyard/agent.toml or FORGEYARD_* variables.
    for setting in agent_config.SETTINGS:
        monkeypatch.delenv(setting.env, raising=False)
    monkeypatch.delenv(agent_config.CONFIG_ENV, raising=False)
    monkeypatch.delenv(agent_config.JOIN_TOKEN_ENV, raising=False)
    monkeypatch.setattr(agent_config, "DEFAULT_PATH", tmp_path / "etc" / "forgeyard" / "agent.toml")
    # Nor this machine's defaults (/srv/forgeyard-models, a forgeyard-run account): tests that are
    # about them put them back (test_agent_defaults.py).
    monkeypatch.setattr(agent_config, "MACHINE_DEFAULTS", {})
    if PG_URL:
        _wipe(PG_URL)
        monkeypatch.setenv("DATABASE_URL", PG_URL)
    get_settings.cache_clear()
    reset_engine()
    for state in (limiter, balancer, key_limiter, in_flight, recorder, ledger, hub, crypto, tls.live, reach):  # in-memory state of one server
        state.reset()
    forget_ceiling()
    yield
    _close_sessions()  # tests open sessions with next(get_db()); PostgreSQL would keep their locks
    reset_engine()
    get_settings.cache_clear()
    for state in (limiter, balancer, key_limiter, in_flight, recorder, ledger, hub, crypto, tls.live, reach):
        state.reset()


def _close_sessions() -> None:
    """close_all_sessions, waiting for a session that another thread is using right now: work the server
    handed to a thread (asyncio.to_thread) can still be connecting when a test ends, and closing it then
    raises IllegalStateChangeError (seen once on PostgreSQL in CI)."""
    deadline = time.monotonic() + 5
    while True:
        try:
            close_all_sessions()
            return
        except IllegalStateChangeError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def make_client(**kwargs) -> TestClient:
    from forgeyard.app import create_app

    kwargs.setdefault("base_url", "http://localhost")
    client = TestClient(create_app(), **kwargs)
    client.headers["X-Forgeyard"] = "1"
    return client


@pytest.fixture()
def client():
    with make_client() as c:
        yield c


@pytest.fixture()
def owner(client):
    """`client` signed in as the owner."""
    r = client.post("/api/auth/setup", json={"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
    assert r.status_code == 200, r.text
    return client


def link_token(url: str) -> str:
    return re.search(r"#token=(.+)$", url).group(1)


def add_user(admin_client, username, role="user", password="quiet-meadow-4821"):
    """Create a user as admin and set their password through the one-time link."""
    r = admin_client.post("/api/users", json={"username": username, "role": role})
    assert r.status_code == 201, r.text
    token = link_token(r.json()["set_password_url"])
    r = admin_client.post("/api/auth/password-link", json={"token": token, "password": password})
    assert r.status_code == 200, r.text
    return r


def sign_in(app_client: TestClient, username, password) -> TestClient:
    """A second browser for the same app."""
    c = TestClient(app_client.app, base_url="http://localhost")
    c.headers["X-Forgeyard"] = "1"
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c
