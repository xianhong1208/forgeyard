"""Roles and the protected owner (FY-ROLE-*)."""

import pytest

from tests.conftest import add_user, make_client, sign_in

ADMIN_CALLS = [("get", "/api/users"), ("post", "/api/users"), ("get", "/api/audit"),
               ("patch", "/api/users/1"), ("delete", "/api/users/1"), ("post", "/api/users/1/sign-out"),
               ("post", "/api/users/1/password-link")]


@pytest.mark.parametrize("role", ["user", "operator"])
@pytest.mark.parametrize("method, path", ADMIN_CALLS)
def test_fy_role_001_non_admins_get_403(owner, role, method, path):
    """FY-ROLE-001"""
    add_user(owner, "bob", role=role)
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    kwargs = {"json": {"username": "x1", "role": "admin"}} if method in ("post", "patch") else {}
    r = getattr(bob, method)(path, **kwargs)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"


@pytest.mark.parametrize("method, path", ADMIN_CALLS + [("get", "/api/auth/me"), ("get", "/api/auth/sessions")])
def test_fy_role_002_anonymous_gets_401(owner, method, path):
    """FY-ROLE-002"""
    c = make_client()
    kwargs = {"json": {}} if method in ("post", "patch") else {}
    assert getattr(c, method)(path, **kwargs).status_code == 401


def test_fy_role_003_owner_cannot_be_demoted_disabled_or_deleted(owner):
    """FY-ROLE-003: not even by another admin."""
    add_user(owner, "ada", role="admin")
    ada = sign_in(owner, "ada", "quiet-meadow-4821")
    owner_id = owner.get("/api/auth/me").json()["user"]["id"]
    for body in ({"role": "user"}, {"status": "disabled"}):
        r = ada.patch(f"/api/users/{owner_id}", json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "owner_protected"
    assert ada.delete(f"/api/users/{owner_id}").status_code == 409
    me = owner.get("/api/auth/me").json()["user"]
    assert me["role"] == "admin" and me["status"] == "active"


def test_fy_role_004_admin_cannot_disable_or_delete_self(owner):
    """FY-ROLE-004"""
    add_user(owner, "ada", role="admin")
    ada = sign_in(owner, "ada", "quiet-meadow-4821")
    ada_id = ada.get("/api/auth/me").json()["user"]["id"]
    assert ada.patch(f"/api/users/{ada_id}", json={"status": "disabled"}).status_code == 409
    assert ada.delete(f"/api/users/{ada_id}").status_code == 409


def test_fy_role_005_role_change_applies_on_the_next_request(owner):
    """FY-ROLE-005: roles are read from the database per request, not baked into the session."""
    add_user(owner, "bob", role="admin")
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    assert bob.get("/api/users").status_code == 200
    bob_id = bob.get("/api/auth/me").json()["user"]["id"]
    owner.patch(f"/api/users/{bob_id}", json={"role": "user"})
    assert bob.get("/api/users").status_code == 403


def test_fy_role_006_delete_user(owner):
    """FY-ROLE-006"""
    add_user(owner, "bob")
    bob = sign_in(owner, "bob", "quiet-meadow-4821")
    bob_id = bob.get("/api/auth/me").json()["user"]["id"]
    assert owner.delete(f"/api/users/{bob_id}").status_code == 200
    assert bob.get("/api/auth/me").status_code == 401
    assert "bob" not in [u["username"] for u in owner.get("/api/users").json()["users"]]
