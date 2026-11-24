"""GET /metrics in the Prometheus text format (1.4.5, #165; FY-MET-*): who may read it (as Folio: without
METRICS_TOKEN only a direct request from this host, with it a Bearer token on every scrape), what the gateway counts
(requests, latency, first byte, tokens, spills, the waiting line), the state read at scrape time (replicas, machines,
GPU memory, budgets), and that no label carries a key, a user or a prompt."""

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from forgeyard import metrics
from forgeyard.config import get_settings
from forgeyard.db import new_session
from forgeyard.gateway import budgets, waiting
from tests.test_routing import background, chat, in_flight_soon, mixed  # noqa: F401 (a fixture)
from tests.test_scheduler import deploy, fleet, reps  # noqa: F401 (a fixture)
from tests.test_placement_fetch import set_node

TOKEN = "metrics-token-0123456789abcdef"


def scraper(app_client, host="127.0.0.1", **headers) -> TestClient:
    """A scrape from `host`, without the console's cookies or headers."""
    c = TestClient(app_client.app, base_url="http://localhost", client=(host, 51000))
    c.portal = getattr(app_client, "portal", None)
    c.headers.update(headers)
    return c


def scrape(app_client) -> str:
    r = scraper(app_client).get("/metrics")
    assert r.status_code == 200, r.text
    return r.text


def value(text: str, name: str, **labels) -> float:
    """The sample `name` with exactly these labels (0 when absent)."""
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            if s.name == name and s.labels == {k: str(v) for k, v in labels.items()}:
                return s.value
    return 0.0


def samples(text: str, name: str) -> list:
    return [s for f in text_string_to_metric_families(text) for s in f.samples if s.name == name]


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", TOKEN)
    get_settings.cache_clear()
    yield TOKEN
    monkeypatch.delenv("METRICS_TOKEN")
    get_settings.cache_clear()


def test_fy_met_001_without_a_token_only_a_direct_request_from_this_host(owner):
    """FY-MET-001: no METRICS_TOKEN: 127.0.0.1, ::1 and ::ffff:127.0.0.1 get the metrics (Prometheus' content type);
    another address gets 403, and so does a loopback request that came through a proxy (X-Forwarded-For, Forwarded,
    X-Real-IP), whatever address that header names. The console's session is no way in either."""
    for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        r = scraper(owner, host).get("/metrics")
        assert r.status_code == 200, (host, r.text)
        assert r.headers["content-type"].startswith("text/plain; version=") and "# TYPE forgeyard_" in r.text
        assert r.headers["cache-control"] == "no-store"
    for host in ("10.0.0.5", "192.168.1.20", "testclient"):
        r = scraper(owner, host).get("/metrics")
        assert r.status_code == 403 and r.json()["detail"]["code"] == "metrics_local_only", host
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
        r = scraper(owner, "127.0.0.1", **{header: "127.0.0.1"}).get("/metrics")
        assert r.status_code == 403, header
    assert owner.get("/metrics").status_code == 403  # signed in, from TestClient's own address


def test_fy_met_002_with_a_token_every_scrape_needs_it(owner, token):
    """FY-MET-002: METRICS_TOKEN set: even loopback needs Authorization: Bearer; a missing, wrong or non-Bearer one
    gets 401 with the same answer and WWW-Authenticate; the right one works from any address and through a proxy."""
    answers = []
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"},
                    {"Authorization": f"Bearer {TOKEN}x"}, {"Authorization": "Bearer "}):
        r = scraper(owner, "127.0.0.1", **headers).get("/metrics")
        assert r.status_code == 401 and r.headers["www-authenticate"] == 'Bearer realm="metrics"', headers
        answers.append(r.text)
    assert len(set(answers)) == 1
    for host, extra in (("10.0.0.5", {}), ("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}), ("::1", {})):
        r = scraper(owner, host, Authorization=f"Bearer {token}", **extra).get("/metrics")
        assert r.status_code == 200 and "forgeyard_gateway_requests" in r.text, host


def test_fy_met_003_requests_latency_first_byte_and_tokens(mixed):  # noqa: F811
    """FY-MET-003: a request through the gateway counts once under its endpoint, model, status, source and target,
    with its total time, its time to first byte and its tokens; a stream too. A model nobody serves counts as
    _unknown, never under the name the client sent. Nothing names the key, its id or its person."""
    labels = dict(endpoint="chat/completions", model="chat", status="200", source="api", target="local")
    before = scrape(mixed.owner)
    assert chat(mixed.api).status_code == 200
    r = chat(mixed.api, stream=True, stream_options={"include_usage": True})
    assert r.status_code == 200
    assert mixed.api.post("/v1/chat/completions", json={"model": "no-such-model-x9", "messages": []}).status_code == 404
    after = scrape(mixed.owner)
    delta = lambda name, **lab: value(after, name, **lab) - value(before, name, **lab)  # noqa: E731
    assert delta("forgeyard_gateway_requests_total", **labels) == 2
    assert delta("forgeyard_gateway_request_duration_seconds_count", endpoint="chat/completions", model="chat") == 2
    assert delta("forgeyard_gateway_time_to_first_byte_seconds_count", endpoint="chat/completions", model="chat") == 2
    assert delta("forgeyard_gateway_tokens_total", model="chat", direction="in") == 20
    assert delta("forgeyard_gateway_tokens_total", model="chat", direction="out") == 40
    assert delta("forgeyard_gateway_requests_total", endpoint="chat/completions", model="_unknown", status="404",
                 source="api", target="local") == 1
    assert "no-such-model-x9" not in after
    for secret in ("fallback-key", mixed.key, mixed.key[:12], "owner", "tony", "hi"):
        assert f'"{secret}"' not in after and f"={secret}" not in after, secret
    # The first byte comes before the end.
    ttfb = value(after, "forgeyard_gateway_time_to_first_byte_seconds_sum", endpoint="chat/completions", model="chat")
    total = value(after, "forgeyard_gateway_request_duration_seconds_sum", endpoint="chat/completions", model="chat")
    assert 0 < ttfb <= total
    # A stream's first byte is its first chunk, not its headers (which the engine sends at once).
    mixed.engine.first_delay = 0.4
    assert chat(mixed.api, stream=True).status_code == 200
    later = scrape(mixed.owner)
    took = value(later, "forgeyard_gateway_time_to_first_byte_seconds_sum", endpoint="chat/completions", model="chat")
    assert took - ttfb >= 0.4


def test_fy_met_004_a_spill_counts_its_reason_and_the_provider_target(mixed):  # noqa: F811
    """FY-MET-004: a request that finds local busy and spills to the cloud counts as target provider, and as a spill
    with reason busy; the local one as local."""
    before = scrape(mixed.owner)
    # The engine keeps the first request until the test lets it go, not for a fixed second: on a busy runner the
    # second request could take longer than that to reach the slots and find them free (#226, as FY-RT-002).
    mixed.engine.gate = threading.Event()
    try:
        t, first = background(lambda: chat(mixed.api))
        in_flight_soon()
        r = chat(mixed.api)
    finally:
        mixed.engine.gate.set()
    t.join()
    assert first["r"].status_code == 200 and first["r"].headers["x-forgeyard-target"] == "local:chat#0"
    assert r.headers["x-forgeyard-spilled"] == "busy"
    after = scrape(mixed.owner)
    assert value(after, "forgeyard_gateway_spills_total", model="chat", reason="busy") - \
        value(before, "forgeyard_gateway_spills_total", model="chat", reason="busy") == 1
    for target in ("local", "provider"):
        lab = dict(endpoint="chat/completions", model="chat", status="200", source="api", target=target)
        assert value(after, "forgeyard_gateway_requests_total", **lab) - \
            value(before, "forgeyard_gateway_requests_total", **lab) == 1, target
    assert "gpt-mini" not in after and "cloudy" in after  # the provider's name in its spend, never its model id


def test_fy_met_005_replicas_machines_and_gpu_memory(fleet):  # noqa: F811
    """FY-MET-005: replicas per deployment and state; machines online/offline and each one's up; per GPU its total
    and free memory as the agent reported them and what Forgeyard's replicas reserve there (the replica's amount)."""
    c, a100, rtx = fleet
    deploy(c, "chat", "qwen3-4b", "vllm", params={"share": "vram"})
    rep = reps(c, {"deployment": {"id": c.get("/api/deployments").json()["deployments"][0]["id"]}})[0]
    set_node(rtx, status="offline")
    text = scrape(c)
    assert value(text, "forgeyard_replicas", deployment="chat", state="pending") == 1
    assert value(text, "forgeyard_machines", status="online") == 1 and value(text, "forgeyard_machines",
                                                                             status="offline") == 1
    assert value(text, "forgeyard_machine_up", machine="a100") == 1 and value(text, "forgeyard_machine_up",
                                                                              machine="rtx") == 0
    db = new_session()
    try:
        from forgeyard.db import Node

        cards = {a.index: a for a in db.get(Node, a100).accelerators if a.vendor != "cpu"}
        cpu_row = next(a for a in db.get(Node, a100).accelerators if a.vendor == "cpu")
    finally:
        db.close()
    assert [g["index"] for g in rep["gpus"]] == [4]
    assert value(text, "forgeyard_gpu_memory_total_bytes", machine="a100", gpu="4") == cards[4].vram_total
    assert value(text, "forgeyard_gpu_memory_free_bytes", machine="a100", gpu="4") == cards[4].vram_free
    assert value(text, "forgeyard_gpu_memory_reserved_bytes", machine="a100", gpu="4") == 15443688448
    assert value(text, "forgeyard_gpu_memory_reserved_bytes", machine="a100", gpu="3") == 0
    assert value(text, "forgeyard_machine_memory_total_bytes", machine="a100") == cpu_row.vram_total
    assert len(samples(text, "forgeyard_gpu_memory_total_bytes")) == 8 + 2  # every card of both machines
    assert value(text, "forgeyard_build_info", version=__import__("forgeyard").__version__) == 1


def test_fy_met_006_budget_spend_per_provider_and_budgets_reached(mixed):  # noqa: F811
    """FY-MET-006: each provider's spend this month (USD) and its budget; how many key, person and provider budgets
    reached 80 and 100 percent, never whose."""
    db = new_session()
    try:
        from forgeyard.db import ApiKey, Provider

        p = db.get(Provider, mixed.provider["id"])
        p.budget_micros = 2_000_000
        key = db.query(ApiKey).filter(ApiKey.name == "fallback-key").one()
        key.budget_micros = 1_000_000
        db.commit()
        pid, kid = p.id, key.id
    finally:
        db.close()
    led = budgets.ledger
    led.ensure()
    with led._lock:
        led._spent[("provider", pid)] = 1_700_000 * budgets.PICOS   # 85% of its budget
        led._spent[("key", kid)] = 1_000_000 * budgets.PICOS        # all of it
    text = scrape(mixed.owner)
    assert value(text, "forgeyard_cloud_spend_usd", provider="cloudy") == pytest.approx(1.7)
    assert value(text, "forgeyard_provider_budget_usd", provider="cloudy") == pytest.approx(2.0)
    assert value(text, "forgeyard_budgets_reached", scope="provider", threshold="80") == 1
    assert value(text, "forgeyard_budgets_reached", scope="provider", threshold="100") == 0
    assert value(text, "forgeyard_budgets_reached", scope="key", threshold="100") == 1
    assert value(text, "forgeyard_budgets_reached", scope="person", threshold="80") == 0
    assert "fallback-key" not in text


def test_fy_met_007_the_waiting_line(owner):
    """FY-MET-007: requests waiting in a route's line now, per model (its tiers added up), and how long each waited
    once it left the line."""
    waiting.forget()
    before = scrape(owner)

    async def scenario():
        a, b = waiting.line("chat#0"), waiting.line("chat#1")
        w1, w2, w3 = a.join(), a.join(), b.join()
        now = scrape(owner)
        assert value(now, "forgeyard_gateway_waiting", model="chat") == 3
        await asyncio.sleep(0.06)
        for line, w in ((a, w1), (a, w2), (b, w3)):
            line.leave(w)
        line_left = scrape(owner)
        assert value(line_left, "forgeyard_gateway_waiting", model="chat") == 0
        return line_left

    after = asyncio.run(scenario())
    waiting.forget()
    count = "forgeyard_gateway_wait_seconds_count"
    assert value(after, count, model="chat") - value(before, count, model="chat") == 3
    assert value(after, "forgeyard_gateway_wait_seconds_bucket", model="chat", le="0.05") - \
        value(before, "forgeyard_gateway_wait_seconds_bucket", model="chat", le="0.05") == 0
    a = waiting.line("chat#0")
    a.leave(None)  # not in line: nothing counted
    assert value(scrape(owner), count, model="chat") == value(after, count, model="chat")


def test_fy_met_008_labels_stay_bounded(monkeypatch):
    """FY-MET-008: a label takes at most MAX_VALUES different values, the rest count as _other; fixed vocabularies
    (spill reason, source) take only their own values; an empty value is _unknown."""
    metrics.reset()
    monkeypatch.setattr(metrics, "MAX_VALUES", 3)
    assert [metrics.bounded("model", f"m{i}") for i in range(5)] == ["m0", "m1", "m2", "_other", "_other"]
    assert metrics.bounded("model", "m1") == "m1"           # one already seen keeps its name
    assert metrics.bounded("model", "") == "_unknown" and metrics.bounded("model", None) == "_unknown"
    assert metrics.bounded("reason", "busy", metrics.SPILL_REASONS) == "busy"
    assert metrics.bounded("reason", "x" * 50, metrics.SPILL_REASONS) == "_other"
    assert metrics.bounded("source", "api", metrics.SOURCES) == "api"
    assert metrics.target_of("pm:17") == "provider" and metrics.target_of("local") == "local"
    metrics.reset()


def test_fy_met_009_a_broken_metric_never_fails_a_request(mixed, monkeypatch):  # noqa: F811
    """FY-MET-009: recording a request for /metrics that raises is logged, and the request is answered and its usage
    counted as usual."""
    def broken(*a, **k):
        raise RuntimeError("metrics broke")

    monkeypatch.setattr(metrics.REQUESTS, "labels", broken)
    r = chat(mixed.api)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello from a"
    from forgeyard.gateway.usage import recorder

    assert any(c.requests for c in recorder._pending.values())


def test_fy_met_010_video_jobs_count_too(owner):
    """FY-MET-010: a finished video job counts under videos. (1.4.5 also counted /x/ requests here; /x/ was removed in
    1.5.3, #89.)"""
    before = scrape(owner)
    from forgeyard.gateway import videojobs

    videojobs._record({"source": "api", "key_id": 7, "user_id": 1, "username": "someone", "model": "wan",
                       "deployment_id": None, "status": 200, "latency_ms": 90_000.0, "units": None})
    after = scrape(owner)
    for ep, model in (("videos", "wan"),):
        lab = dict(endpoint=ep, model=model, status="200", source="api", target="local")
        assert value(after, "forgeyard_gateway_requests_total", **lab) - \
            value(before, "forgeyard_gateway_requests_total", **lab) == 1, ep
    assert value(after, "forgeyard_gateway_request_duration_seconds_sum", endpoint="videos", model="wan") - \
        value(before, "forgeyard_gateway_request_duration_seconds_sum", endpoint="videos", model="wan") == 90
    assert "someone" not in after
