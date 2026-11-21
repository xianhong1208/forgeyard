"""No request runs twice, and a shorter wait for the first answer (1.4.5, #168): a request moves to
another replica only when it provably never reached the first one; a replica that took it and then
broke off or went silent is given up (502 / 504) without trying it elsewhere; the deployment's
`first_answer_timeout` (120 s by default) bounds the wait for the status line, and for a stream for
its first bytes too (FY-FA-*)."""

import socket
import threading
import time

import pytest

from forgeyard.gateway import proxy
from forgeyard.gateway.balancer import balancer
from tests.gateway_helpers import free_port, make_ready
from tests.test_gateway import chat, gw  # noqa: F401  (the fixture)
from tests.test_routing import mixed  # noqa: F401  (the fixture)
from tests.test_routing import events


class Breaker:
    """A replica that reads a request and then hangs up without answering (an engine that crashed while
    generating), or (`reset`) resets the connection."""

    def __init__(self, reset: bool = False):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self.reset = reset
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            data = b""
            conn.settimeout(5)
            try:
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                head, _, rest = data.partition(b"\r\n\r\n")
                n = int(next((ln.split(b":")[1] for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")),
                             b"0"))
                while len(rest) < n:
                    rest += conn.recv(65536)
                self.requests += 1
                if self.reset:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
            conn.close()

    def close(self):
        self.sock.close()


def test_fy_fa_001_what_moves_a_request_to_another_replica():
    """FY-FA-001: only a connection that could not be made, or no free connection in the pool, is
    retried elsewhere; a connection that broke while the request was being written or its answer
    awaited is not."""
    import httpx

    assert set(proxy.RETRYABLE) == {httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout}
    assert set(proxy.SENT_BROKE) == {httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError}
    assert not set(proxy.RETRYABLE) & set(proxy.SENT_BROKE)


@pytest.mark.parametrize("reset", [False, True])
def test_fy_fa_002_a_replica_that_breaks_off_is_not_retried_elsewhere(gw, reset):  # noqa: F811
    """FY-FA-002: replica 0 reads the request and hangs up (or resets) without an answer: 502
    upstream_error saying it was not sent elsewhere; replica 1 never hears of it. Then replica 0 is
    tried last for a while, so the next request goes to replica 1."""
    broken = Breaker(reset=reset)
    try:
        make_ready("chat", [(gw.a, broken.port, None), (gw.b, gw.e2.port, gw.e2.secret)])
        balancer.reset()
        r = chat(gw.api)
        if r.status_code == 200:      # the balancer started at replica 1: once more, now replica 0 is first
            r = chat(gw.api)
        assert r.status_code == 502, r.text
        err = r.json()["error"]
        assert err["code"] == "upstream_error" and "not sent to another replica" in err["message"]
        assert broken.requests >= 1
        sent = len(gw.e2.posts())
        r = chat(gw.api)
        assert r.status_code == 200 and r.headers["x-forgeyard-replica"] == "chat#1"
        assert len(gw.e2.posts()) == sent + 1
    finally:
        broken.close()


def test_fy_fa_003_a_replica_that_cannot_be_reached_still_fails_over(gw):  # noqa: F811
    """FY-FA-003: nothing listens on replica 0's port: the request goes to replica 1 (connect errors are
    retried), every time."""
    make_ready("chat", [(gw.a, free_port(), "secret-a"), (gw.b, gw.e2.port, gw.e2.secret)])
    for _ in range(3):
        r = chat(gw.api)
        assert r.status_code == 200 and r.headers["x-forgeyard-replica"] == "chat#1"


def test_fy_fa_004_a_silent_replica_gets_504_after_the_first_answer_wait(gw, monkeypatch):  # noqa: F811
    """FY-FA-004: a replica that takes the request and says nothing: 504 first_answer_timeout after the
    wait (not GATEWAY_READ_TIMEOUT's 600 s), never sent to the other replica; the engine's connection is
    closed."""
    monkeypatch.setattr(proxy, "FIRST_ANSWER_DEFAULT", 0.5)
    gw.e1.gate = threading.Event()
    gw.e2.gate = threading.Event()
    try:
        started = time.monotonic()
        r = chat(gw.api)
        took = time.monotonic() - started
    finally:
        gw.e1.gate.set()
        gw.e2.gate.set()
    assert r.status_code == 504 and r.json()["error"]["code"] == "first_answer_timeout", r.text
    assert "within 0.5 s" in r.json()["error"]["message"] and "not sent to another replica" in r.json()["error"]["message"]
    assert 0.45 <= took < 3.0
    assert len(gw.e1.posts()) + len(gw.e2.posts()) == 1
    assert sum(balancer.inflight.values()) == 0


def test_fy_fa_005_the_deployments_own_wait(gw):  # noqa: F811
    """FY-FA-005: first_answer_timeout from the deployment's parameters, else 120 s."""
    from forgeyard.db import new_session

    db = new_session()
    try:
        route = proxy.load_route(db, "chat")
        assert [g.first_answer for t in route.tiers for g in t.locals] == [120.0]
    finally:
        db.close()
    deps = {d["name"]: d["id"] for d in gw.owner.get("/api/deployments").json()["deployments"]}
    r = gw.owner.patch(f"/api/deployments/{deps['chat']}", json={"params": {"first_answer_timeout": 900}})
    assert r.status_code == 200, r.text
    assert r.json()["restart"] == "none" and r.json()["changes"] == {"first_answer_timeout": {"before": None, "after": 900}}
    db = new_session()
    try:
        route = proxy.load_route(db, "chat")
        assert [g.first_answer for t in route.tiers for g in t.locals] == [900.0]
    finally:
        db.close()


def test_fy_fa_006_streams_wait_for_their_first_bytes(gw, monkeypatch):  # noqa: F811
    """FY-FA-006: a stream whose headers come at once but whose first chunk does not come within the wait
    is 504 first_answer_timeout (not a 200 that breaks off); one whose first chunk comes in time streams
    whole even when later chunks are further apart than the wait."""
    monkeypatch.setattr(proxy, "FIRST_ANSWER_DEFAULT", 0.5)
    for e in (gw.e1, gw.e2):
        e.first_delay = 1.5
    r = chat(gw.api, stream=True)
    assert r.status_code == 504 and r.json()["error"]["code"] == "first_answer_timeout"
    assert len(gw.e1.posts()) + len(gw.e2.posts()) == 1
    for e in (gw.e1, gw.e2):
        e.first_delay, e.delay = 0.0, 0.7
    r = chat(gw.api, stream=True)
    evs = events(r.text)
    assert r.status_code == 200 and evs[-1] == "[DONE]"
    assert [e["choices"][0]["delta"].get("content") for e in evs[:3]] == ["t0 ", "t1 ", "t2 "]


def test_fy_fa_007_a_long_wait_also_lifts_the_read_timeout(gw, monkeypatch):  # noqa: F811
    """FY-FA-007: a first-answer wait above GATEWAY_READ_TIMEOUT is honoured: the answer that comes after
    the read timeout but within the wait is passed on."""
    from forgeyard.config import get_settings

    monkeypatch.setenv("GATEWAY_READ_TIMEOUT", "0.3")
    get_settings.cache_clear()
    proxy._clients.clear()
    monkeypatch.setattr(proxy, "FIRST_ANSWER_DEFAULT", 3.0)
    for e in (gw.e1, gw.e2):
        e.hold = 1.0
    r = chat(gw.api)
    assert r.status_code == 200, r.text
    proxy._clients.clear()


def test_fy_fa_008_deploy_checks_the_parameter(gw):  # noqa: F811
    """FY-FA-008: first_answer_timeout is 5 to 3600 whole seconds, refused for image and video
    deployments (they have their own timeout), and kept only when it is given."""
    deps = {d["name"]: d for d in gw.owner.get("/api/deployments").json()["deployments"]}
    assert "first_answer_timeout" not in deps["chat"]["params"]
    for bad in (4, 3601, "soon", True, 1.5, 60.5):
        r = gw.owner.patch(f"/api/deployments/{deps['chat']['id']}", json={"params": {"first_answer_timeout": bad}})
        assert r.status_code == 422 and "first_answer_timeout" in r.text, (bad, r.text)
    r = gw.owner.patch(f"/api/deployments/{deps['chat']['id']}", json={"params": {"first_answer_timeout": "60"}})
    assert r.status_code == 200 and r.json()["params"]["first_answer_timeout"] == 60
    r = gw.owner.patch(f"/api/deployments/{deps['chat']['id']}", json={"params": {"first_answer_timeout": None}})
    assert r.status_code == 200 and "first_answer_timeout" not in r.json()["params"]
    from fastapi import HTTPException

    from forgeyard.api.deployments import normalise_params
    from forgeyard.db import new_session
    from forgeyard.models.catalog import get_recipe

    db = new_session()
    try:
        for name in ("sd-cpp", "vllm-omni-video"):
            with pytest.raises(HTTPException) as e:
                normalise_params(get_recipe(db, name), {"gpus": 1, "first_answer_timeout": 60})
            assert "image and video deployments" in e.value.detail["message"]
        assert normalise_params(get_recipe(db, "faster-whisper"), {"gpus": 0, "first_answer_timeout": 600})[
            "first_answer_timeout"] == 600
    finally:
        db.close()


def test_fy_fa_009_a_provider_that_breaks_off_is_not_tried_on_the_next_target(mixed):  # noqa: F811
    """FY-FA-009: the local tier is unavailable and the first cloud target reads the request and hangs
    up: 502 provider_error, and the next tier's provider never gets it (it may have been served, and
    billed, by the first)."""
    from types import SimpleNamespace

    from tests.cloud_helpers import add_provider, model_id, set_tiers
    from tests.gateway_helpers import set_state

    broken = Breaker()
    try:
        p = add_provider(mixed.owner, SimpleNamespace(base_url=f"http://127.0.0.1:{broken.port}/v1", key="sk-test-x"),
                         name="flaky", kind="openai_compatible", models=(("m1", "openai-chat"),))
        set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", model_id(p, "m1"))],
                                        [("provider", mixed.pm)]])
        set_state("chat", 0, "starting", "loading")
        r = chat(mixed.api)
        assert r.status_code == 502, r.text
        assert r.json()["error"]["code"] == "provider_error" and "not sent to another target" in r.json()["error"]["message"]
        assert broken.requests == 1 and mixed.cloud.posts() == []
    finally:
        broken.close()
