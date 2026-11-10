import os
import re

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import close_all_sessions
from fastapi.testclient import TestClient

from forgeyard.config import get_settings
from forgeyard.db import reset_engine
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.limits import key_limiter
from forgeyard.gateway.usage import recorder
from forgeyard.models.hub import hub
from forgeyard.security.ratelimit import limiter

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
def isolated(tmp_path, monkeypatch):
    """Each test gets its own data dir (SQLite + secrets file) and fresh settings."""
    for key in ("ADMIN_USERNAME", "ADMIN_PASSWORD", "FORGEYARD_SETUP_TOKEN", "DATABASE_URL", "PUBLIC_URL",
                "ALLOWED_HOSTS", "TRUSTED_PROXIES", "REGISTRATION", "RATE_LIMIT_PER_MINUTE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FORGEYARD_SETUP_TOKEN", SETUP_TOKEN)
    if PG_URL:
        _wipe(PG_URL)
        monkeypatch.setenv("DATABASE_URL", PG_URL)
    get_settings.cache_clear()
    reset_engine()
    for state in (limiter, balancer, key_limiter, recorder, hub):  # in-memory state of one server
        state.reset()
    yield
    close_all_sessions()  # tests open sessions with next(get_db()); PostgreSQL would keep their locks
    reset_engine()
    get_settings.cache_clear()
    for state in (limiter, balancer, key_limiter, recorder, hub):
        state.reset()


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
