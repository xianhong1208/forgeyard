"""Requests and tokens per minute per key: sliding windows with an injected clock, and through the
gateway (FY-LIM-*)."""

import pytest

from forgeyard.gateway.limits import Admission, KeyLimiter, Refusal
from forgeyard.gateway.usage import recorder
from tests.gateway_helpers import api_client, new_key
from tests.test_gateway import chat, err, gw  # noqa: F401  (the fixture)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_fy_lim_001_rpm_is_a_sliding_window():
    """FY-LIM-001: 3 per minute: the 4th within 60 s is refused until the first leaves the window;
    refusals are not counted."""
    clock = Clock()
    lim = KeyLimiter(clock)
    for i in range(3):
        assert isinstance(lim.admit(1, 3, None), Admission)
        clock.t += 10                       # t = 1000, 1010, 1020
    r = lim.admit(1, 3, None)               # t = 1030
    assert isinstance(r, Refusal) and r.which == "rpm" and r.retry_after == 30 and r.limit == 3
    clock.t += 20                           # t = 1050: still within 60 s of 1000
    assert isinstance(lim.admit(1, 3, None), Refusal)
    clock.t = 1060.5                        # 1000 has left the window
    assert isinstance(lim.admit(1, 3, None), Admission)
    assert isinstance(lim.admit(1, 3, None), Refusal)
    assert isinstance(lim.admit(2, 3, None), Admission)  # per key
    assert lim.used(1) == (3, 0)


def test_fy_lim_002_tpm_counts_answers_and_holds_max_tokens():
    """FY-LIM-002: tokens count when the answer reports them; max_tokens is held while a request runs;
    a full window refuses until enough tokens have left it."""
    clock = Clock()
    lim = KeyLimiter(clock)
    a = lim.admit(1, None, 100, max_tokens=60)
    assert isinstance(a, Admission) and lim.used(1) == (0, 60)
    r = lim.admit(1, None, 100, max_tokens=50)           # 60 held + 50 > 100
    assert isinstance(r, Refusal) and r.which == "tpm"
    assert isinstance(lim.admit(1, None, 100, max_tokens=40), Admission)  # 60 + 40 fits
    clock.t += 5
    lim.settle(a, 30)                                    # the first one used 30, counted from t = 1005
    assert lim.used(1) == (0, 70)
    b = lim.admit(1, None, 100)                          # no max_tokens: allowed while there is room
    lim.settle(b, 40)                                    # 110 now: over
    r = lim.admit(1, None, 100)
    assert isinstance(r, Refusal) and r.retry_after == 55  # the 40 held since t = 1000 leaves at 1060
    clock.t = 1061
    assert isinstance(lim.admit(1, None, 100), Admission)  # 30 + 40 = 70 left in the window: room again
    lim2 = KeyLimiter(Clock())
    big = lim2.admit(9, None, 100, max_tokens=10_000)    # more than the tpm: held as the whole tpm
    assert isinstance(big, Admission) and lim2.used(9) == (0, 100)
    assert isinstance(lim2.admit(9, None, 100), Refusal)


def test_fy_lim_003_no_limits_admit_everything():
    """FY-LIM-003"""
    lim = KeyLimiter(Clock())
    assert all(isinstance(lim.admit(1, None, None, 10**9), Admission) for _ in range(1000))
    assert lim.used(1) == (0, 0)


def test_fy_lim_004_rpm_through_the_gateway(gw):  # noqa: F811
    """FY-LIM-004: a key limited to 3 requests a minute gets 429 with Retry-After on the 4th, in
    OpenAI's shape; other keys are not affected; refusals are counted as limited."""
    c = api_client(gw.owner, new_key(gw.owner, "three", rpm=3)["key"])
    assert [chat(c).status_code for _ in range(3)] == [200, 200, 200]
    r = chat(c)
    e = err(r)
    assert r.status_code == 429 and e["type"] == "rate_limit_error" and e["code"] == "rate_limit_exceeded"
    assert 1 <= int(r.headers["retry-after"]) <= 60 and "3 requests per minute" in e["message"]
    assert chat(gw.api).status_code == 200
    recorder.flush()
    rows = {x["name"]: x for x in gw.owner.get("/api/usage?group=key").json()["rows"]}
    three = next(v for k, v in rows.items() if k.startswith("three"))
    assert three["requests"] == 4 and three["limited"] == 1 and three["errors"] == 1


def test_fy_lim_005_tpm_through_the_gateway(gw):  # noqa: F811
    """FY-LIM-005: tokens come from the answers' usage (30 per chat here): with 50 per minute the
    second request still fits without max_tokens, the third does not; max_tokens past what is left is
    refused at once."""
    c = api_client(gw.owner, new_key(gw.owner, "tokens", tpm=50)["key"])
    assert chat(c).status_code == 200                       # 30 used
    r = chat(c, max_tokens=30)                              # 30 + 30 > 50
    assert r.status_code == 429 and "50 tokens per minute" in err(r)["message"]
    assert chat(c).status_code == 200                       # 30 < 50: room; it uses 30 more
    r = chat(c)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    key = gw.owner.get("/api/keys").json()["keys"]
    assert next(k for k in key if k["name"] == "tokens")["this_minute"]["tokens"] == 60


@pytest.mark.parametrize("stream", [False, True])
def test_fy_lim_006_streamed_tokens_count_too(gw, stream):  # noqa: F811
    """FY-LIM-006: a streamed chat's usage (asked for by the gateway) counts against the tpm."""
    c = api_client(gw.owner, new_key(gw.owner, "s", tpm=40)["key"])
    assert chat(c, stream=stream).status_code == 200
    assert chat(c, stream=stream).status_code == 200        # 30 < 40
    assert chat(c, stream=stream).status_code == 429        # 60 >= 40
