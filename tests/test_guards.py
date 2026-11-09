"""Host check (421) and CSRF (FY-GRD-*)."""

import pytest
from fastapi.testclient import TestClient

from forgeyard.config import get_settings
from tests.conftest import OWNER


@pytest.mark.parametrize("host", ["evil.example", "evil.example:7860", "localhost.evil.example"])
def test_fy_grd_001_foreign_host_gets_421(owner, host):
    """FY-GRD-001"""
    r = owner.get("/api/auth/me", headers={"Host": host})
    assert r.status_code == 421 and r.json()["detail"]["code"] == "host_not_allowed"
    assert owner.get("/", headers={"Host": host}).status_code == 421


@pytest.mark.parametrize("host", ["localhost", "localhost:7860", "forge.localhost", "127.0.0.1:7860",
                                  "192.168.1.20", "[::1]:7860"])
def test_fy_grd_002_local_and_ip_hosts_are_fine(owner, host):
    """FY-GRD-002"""
    assert owner.get("/api/auth/me", headers={"Host": host}).status_code == 200


def test_fy_grd_003_public_url_and_allowed_hosts(owner, monkeypatch):
    """FY-GRD-003"""
    monkeypatch.setenv("PUBLIC_URL", "https://forge.lan")
    monkeypatch.setenv("ALLOWED_HOSTS", "gpu-box.home, other.lan")
    get_settings.cache_clear()
    for host in ("forge.lan", "gpu-box.home:7860", "other.lan"):
        assert owner.get("/api/auth/me", headers={"Host": host}).status_code == 200
    assert owner.get("/api/auth/me", headers={"Host": "nope.lan"}).status_code == 421


def test_fy_grd_004_writes_need_the_custom_header(owner):
    """FY-GRD-004"""
    bare = TestClient(owner.app, base_url="http://localhost", cookies=owner.cookies)
    r = bare.post("/api/auth/logout")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf"
    assert owner.get("/api/auth/me").status_code == 200  # the sign-out did not happen


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost.evil.example", "null",
                                    "http://localhost:9999"])
def test_fy_grd_005_cross_origin_writes_are_refused(owner, origin):
    """FY-GRD-005: even with the header (a CORS-enabled attacker cannot get it past us anyway)."""
    r = owner.post("/api/auth/logout", headers={"Origin": origin})
    assert r.status_code == 403
    r = owner.post("/api/auth/logout", headers={"Referer": origin + "/page"} if origin != "null" else {"Origin": "null"})
    assert r.status_code == 403
    assert owner.get("/api/auth/me").status_code == 200


def test_fy_grd_006_same_origin_writes_pass(owner):
    """FY-GRD-006"""
    r = owner.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]},
                   headers={"Origin": "http://localhost"})
    assert r.status_code == 200
    r = owner.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]},
                   headers={"Referer": "http://localhost/login?next=/users"})
    assert r.status_code == 200


def test_fy_grd_007_reads_do_not_need_the_header(owner):
    """FY-GRD-007"""
    bare = TestClient(owner.app, base_url="http://localhost", cookies=owner.cookies)
    assert bare.get("/api/auth/me").status_code == 200


def test_fy_grd_008_security_headers(owner):
    """FY-GRD-008"""
    r = owner.get("/api/auth/me")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
