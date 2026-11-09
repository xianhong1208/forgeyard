"""Self-registration: off by default, approval when on (FY-REG-*)."""

from forgeyard.config import get_settings
from tests.conftest import make_client, sign_in

NEW = {"username": "dana", "password": "violet-harbor-3317"}


def test_fy_reg_001_off_by_default(owner):
    """FY-REG-001"""
    r = make_client().post("/api/auth/register", json=NEW)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "registration_closed"


def test_fy_reg_002_approval_flow(owner, monkeypatch):
    """FY-REG-002: a new account waits as pending until an admin approves it."""
    monkeypatch.setenv("REGISTRATION", "approval")
    get_settings.cache_clear()
    c = make_client()
    assert c.get("/api/auth/state").json()["registration"] == "approval"
    assert c.post("/api/auth/register", json=NEW).json() == {"status": "pending"}
    r = c.post("/api/auth/login", json=NEW)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "pending_approval"
    users = owner.get("/api/users").json()["users"]
    dana = next(u for u in users if u["username"] == "dana")
    assert dana["status"] == "pending" and dana["role"] == "user" and users[0]["username"] == "dana"
    assert owner.patch(f"/api/users/{dana['id']}", json={"status": "active"}).status_code == 200
    sign_in(owner, "dana", NEW["password"])
    actions = [e["action"] for e in owner.get("/api/audit").json()["events"]]
    assert "user.registered" in actions and "user.approved" in actions


def test_fy_reg_003_not_before_setup(client, monkeypatch):
    """FY-REG-003: registration cannot be used to become the first account."""
    monkeypatch.setenv("REGISTRATION", "approval")
    get_settings.cache_clear()
    assert client.post("/api/auth/register", json=NEW).status_code == 403


def test_fy_reg_004_taken_username(owner, monkeypatch):
    """FY-REG-004"""
    monkeypatch.setenv("REGISTRATION", "approval")
    get_settings.cache_clear()
    r = make_client().post("/api/auth/register", json={"username": "owner", "password": NEW["password"]})
    assert r.status_code == 409
