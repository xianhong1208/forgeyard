"""First run: the setup token, the owner, ADMIN_USERNAME bootstrap (FY-SETUP-*)."""

import logging
import os
import stat

import pytest

from forgeyard.config import get_settings
from tests.conftest import OWNER, SETUP_TOKEN, make_client


def test_fy_setup_001_fresh_server_needs_setup(client):
    """FY-SETUP-001"""
    assert client.get("/api/auth/state").json()["needs_setup"] is True


@pytest.mark.parametrize("token", ["", "wrong-token", SETUP_TOKEN + "x"])
def test_fy_setup_002_no_owner_without_the_right_token(client, token):
    """FY-SETUP-002: whoever reaches a fresh server first cannot claim it without the token."""
    r = client.post("/api/auth/setup", json={"setup_token": token, "username": "mallory", "password": OWNER[1]})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "bad_setup_token"
    assert client.get("/api/auth/state").json()["needs_setup"] is True


def test_fy_setup_003_setup_creates_signed_in_owner(client):
    """FY-SETUP-003"""
    r = client.post("/api/auth/setup", json={"setup_token": SETUP_TOKEN, "username": "Owner", "password": OWNER[1]})
    assert r.status_code == 200
    user = r.json()["user"]
    assert user["username"] == "owner" and user["role"] == "admin" and user["is_owner"] is True
    assert client.get("/api/auth/me").status_code == 200
    assert client.get("/api/auth/state").json()["needs_setup"] is False


def test_fy_setup_004_setup_only_once(owner):
    """FY-SETUP-004"""
    r = owner.post("/api/auth/setup", json={"setup_token": SETUP_TOKEN, "username": "second", "password": OWNER[1]})
    assert r.status_code == 409


def test_fy_setup_005_setup_enforces_password_policy(client):
    """FY-SETUP-005"""
    r = client.post("/api/auth/setup", json={"setup_token": SETUP_TOKEN, "username": "owner", "password": "short"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "weak_password"
    assert client.get("/api/auth/state").json()["needs_setup"] is True


def test_fy_setup_006_generated_token_is_logged_stored_0600_and_discarded(monkeypatch, caplog):
    """FY-SETUP-006: without FORGEYARD_SETUP_TOKEN one is generated, logged and kept in a 0600 file."""
    monkeypatch.delenv("FORGEYARD_SETUP_TOKEN")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="forgeyard"), make_client() as c:
        path = get_settings().secrets_path
        import json
        token = json.loads(path.read_text())["SETUP_TOKEN"]
        assert token in caplog.text and "/setup" in caplog.text
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        r = c.post("/api/auth/setup", json={"setup_token": token, "username": "owner", "password": OWNER[1]})
        assert r.status_code == 200
        assert "SETUP_TOKEN" not in json.loads(path.read_text())


def test_fy_setup_007_owner_from_environment(monkeypatch):
    """FY-SETUP-007"""
    monkeypatch.setenv("ADMIN_USERNAME", "boss")
    monkeypatch.setenv("ADMIN_PASSWORD", OWNER[1])
    get_settings.cache_clear()
    with make_client() as c:
        assert c.get("/api/auth/state").json()["needs_setup"] is False
        r = c.post("/api/auth/login", json={"username": "boss", "password": OWNER[1]})
        assert r.status_code == 200 and r.json()["user"]["is_owner"] is True


def test_fy_setup_008_weak_admin_password_stops_startup(monkeypatch):
    """FY-SETUP-008: a weak ADMIN_PASSWORD is a configuration error, not a silent owner."""
    monkeypatch.setenv("ADMIN_USERNAME", "boss")
    monkeypatch.setenv("ADMIN_PASSWORD", "password123")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        with make_client():
            pass
