"""The audit log (FY-AUD-*)."""

from tests.conftest import OWNER, add_user, make_client


def test_fy_aud_001_records_sign_ins_and_admin_actions(owner):
    """FY-AUD-001"""
    make_client().post("/api/auth/login", json={"username": "owner", "password": "wrong-wrong-wrong-1"})
    add_user(owner, "bob")
    events = owner.get("/api/audit").json()["events"]
    actions = [e["action"] for e in events]
    for expected in ("setup.owner_created", "auth.login_failed", "user.created", "auth.password_set_by_link"):
        assert expected in actions
    created = next(e for e in events if e["action"] == "user.created")
    assert created["actor"] == "owner" and created["target"] == "bob" and created["detail"]["role"] == "user"
    assert events == sorted(events, key=lambda e: -e["id"])


def test_fy_aud_002_filter_and_page(owner):
    """FY-AUD-002"""
    for _ in range(4):
        owner.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]})
    page = owner.get("/api/audit", params={"action": "auth.login", "limit": 3}).json()
    assert len(page["events"]) == 3 and all(e["action"].startswith("auth.login") for e in page["events"])
    rest = owner.get("/api/audit", params={"action": "auth.login", "limit": 3, "before": page["next_before"]}).json()
    assert len(rest["events"]) == 1 and rest["next_before"] is None


def test_fy_aud_003_action_prefix_is_literal(owner):
    """FY-AUD-003: '%' and '_' in the filter are not wildcards."""
    assert owner.get("/api/audit", params={"action": "%"}).json()["events"] == []


def test_fy_aud_004_password_never_logged(owner):
    """FY-AUD-004"""
    make_client().post("/api/auth/login", json={"username": "owner", "password": "do-not-log-me-123"})
    assert "do-not-log-me" not in owner.get("/api/audit").text
