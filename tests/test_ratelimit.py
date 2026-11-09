"""Sign-in backoff, IP limits, the global budget, X-Forwarded-For (FY-RL-*)."""

from types import SimpleNamespace

import pytest

from forgeyard.config import get_settings
from forgeyard.security.client import client_ip
from forgeyard.security.ratelimit import RateLimiter, limiter
from tests.conftest import OWNER, make_client


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(limiter, "_clock", c)
    return c


def _bad(c, user=OWNER[0]):
    return c.post("/api/auth/login", json={"username": user, "password": "wrong-wrong-wrong-1"})


def test_fy_rl_001_backoff_after_free_failures(owner, clock):
    """FY-RL-001"""
    c = make_client()
    assert [_bad(c).status_code for _ in range(5)] == [401] * 5
    r = _bad(c)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_fy_rl_002_no_permanent_lockout(owner, clock):
    """FY-RL-002: after the wait, the right password works again."""
    c = make_client()
    for _ in range(5):
        _bad(c)
    r = c.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]})
    assert r.status_code == 429  # even the right password waits during the backoff
    clock.t += 3
    assert c.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]}).status_code == 200


def test_fy_rl_003_backoff_doubles_and_caps(clock):
    """FY-RL-003"""
    rl = RateLimiter(clock=clock)
    waits = []
    for _ in range(14):
        rl.login_failed("a", "1.1.1.1")
        waits.append(rl.login_wait("a", "1.1.1.1", free=5, ip_limit=1000))
    assert waits[:4] == [0, 0, 0, 0]
    assert waits[4:8] == [2, 4, 8, 16]
    assert max(waits) == 300


def test_fy_rl_004_rate_limit_does_not_sign_you_out(owner, clock):
    """FY-RL-004: someone hammering your account does not end your session."""
    attacker = make_client()
    for _ in range(8):
        _bad(attacker)
    assert _bad(attacker).status_code == 429
    assert owner.get("/api/auth/me").status_code == 200


def test_fy_rl_005_ip_limit_across_accounts(owner, clock, monkeypatch):
    """FY-RL-005"""
    monkeypatch.setenv("LOGIN_IP_FAILURES", "4")
    get_settings.cache_clear()
    c = make_client()
    assert [_bad(c, f"u{i}").status_code for i in range(4)] == [401] * 4
    assert _bad(c, "u9").status_code == 429
    clock.t += 15 * 60
    assert _bad(c, "u9").status_code == 401


def test_fy_rl_006_global_budget_is_429_not_401(owner, monkeypatch):
    """FY-RL-006"""
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "5")
    get_settings.cache_clear()
    limiter.reset()
    codes = [owner.get("/api/auth/me").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    r = owner.get("/api/auth/me")
    assert r.json()["detail"]["code"] == "rate_limited" and "retry-after" in r.headers


def _req(peer, xff=None):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": xff} if xff else {})


def test_fy_rl_007_forwarded_for_only_from_trusted_proxies(monkeypatch):
    """FY-RL-007"""
    assert client_ip(_req("10.0.0.5", "6.6.6.6")) == "10.0.0.5"
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.5")
    get_settings.cache_clear()
    assert client_ip(_req("10.0.0.5", "6.6.6.6")) == "6.6.6.6"
    # A client-supplied first hop cannot pick the address: the right-most untrusted hop wins.
    assert client_ip(_req("10.0.0.5", "1.2.3.4, 6.6.6.6")) == "6.6.6.6"
    assert client_ip(_req("10.0.0.9", "6.6.6.6")) == "10.0.0.9"


def test_fy_rl_008_setup_token_guessing_is_limited(client, clock):
    """FY-RL-008"""
    codes = [client.post("/api/auth/setup", json={"setup_token": f"guess{i}", "username": "m",
                                                  "password": OWNER[1]}).status_code for i in range(7)]
    assert codes[:5] == [403] * 5 and 429 in codes[5:]
