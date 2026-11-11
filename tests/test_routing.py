"""Tiered routes (1.2): a local deployment first, a cloud model when local is unavailable, busy or
refusing, and never otherwise; the provider tier's retries and cooldown; the request header that
keeps a request local; streaming and usage from a provider; and the provider quirks on the way out
(FY-RT-*). Every provider here is a local stand-in (tests/cloud_helpers.py)."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from forgeyard.config import get_settings
from forgeyard.gateway import proxy
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.usage import recorder
from tests.cloud_helpers import (CLOUD_KEY, FakeCloud, add_provider, cloud_key, deployment_id, model_id, route_id,
                                 set_tiers)
from tests.gateway_helpers import FakeEngine, api_client, free_port, make_ready, set_state
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

KEYS = MODELS + ("replica_keys",)


@pytest.fixture()
def mixed(owner, monkeypatch):
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")   # the stand-ins listen on 127.0.0.1
    get_settings.cache_clear()
    seed_catalog()
    node = make_node("cpu-a", features=KEYS)
    engine = FakeEngine("a", "secret-a")
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2, "concurrency": 1},
           wait=True)
    make_ready("chat", [(node, engine.port, engine.secret)])
    cloud = FakeCloud("stub")
    p = add_provider(owner, cloud)
    pm = model_id(p, "gpt-mini")
    dep = deployment_id(owner, "chat")
    set_tiers(owner, "chat", [[("local", dep)], [("provider", pm)]])
    key = cloud_key(owner, "fallback-key", "fallback")
    ns = SimpleNamespace(owner=owner, node=node, engine=engine, cloud=cloud, provider=p, pm=pm, dep=dep, key=key,
                         api=api_client(owner, key))
    yield ns
    engine.close()
    cloud.close()


def chat(c, headers=None, **extra):
    body = {"model": "chat", "messages": [{"role": "user", "content": "hi"}], **extra}
    return c.post("/v1/chat/completions", json=body, headers=headers or {})


def events(text: str) -> list:
    out = []
    for block in text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            data = block[6:]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


def in_flight_soon(n: int = 1) -> None:
    deadline = time.monotonic() + 5
    while sum(balancer.inflight.values()) < n and time.monotonic() < deadline:
        time.sleep(0.01)


def background(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()))
    t.start()
    return t, out


def test_fy_rt_001_requests_stay_local_while_local_has_room(mixed):
    """FY-RT-001: one request at a time goes to the local replica and names it; the provider never
    hears of it."""
    for _ in range(3):
        r = chat(mixed.api)
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello from a"
        assert r.headers["x-forgeyard-target"] == "local:chat#0" and r.headers["x-forgeyard-replica"] == "chat#0"
        assert "x-forgeyard-spilled" not in r.headers
    assert mixed.cloud.posts() == []


def test_fy_rt_002_busy_spills_the_second_request(mixed):
    """FY-RT-002: concurrency 1 and one request in flight: the second goes to the cloud tier at once
    (wait_ms 0), named in X-Forgeyard-Target, with X-Forgeyard-Spilled: busy."""
    mixed.engine.hold = 1.0
    t, first = background(lambda: chat(mixed.api))
    in_flight_soon()
    r = chat(mixed.api)
    t.join()
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello from stub"
    assert r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini" and r.headers["x-forgeyard-spilled"] == "busy"
    assert first["r"].headers["x-forgeyard-target"] == "local:chat#0"
    assert len(mixed.cloud.posts()) == 1 and len(mixed.engine.posts()) == 1


def test_fy_rt_003_wait_ms_waits_for_a_local_slot_first(mixed):
    """FY-RT-003: with wait_ms, a request that finds local busy waits for a slot and stays local when
    one frees up in time; when none does, it spills after the wait."""
    set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", mixed.pm)]], wait_ms=3000)
    mixed.engine.hold = 0.5
    t, _ = background(lambda: chat(mixed.api))
    in_flight_soon()
    started = time.monotonic()
    r = chat(mixed.api)
    t.join()
    assert r.headers["x-forgeyard-target"] == "local:chat#0" and time.monotonic() - started >= 0.2
    assert mixed.cloud.posts() == []
    set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", mixed.pm)]], wait_ms=300)
    mixed.engine.hold = 2.0
    t, _ = background(lambda: chat(mixed.api))
    in_flight_soon()
    started = time.monotonic()
    r = chat(mixed.api)
    waited = time.monotonic() - started
    t.join()
    assert r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini" and r.headers["x-forgeyard-spilled"] == "busy"
    assert 0.25 <= waited < 1.5


def test_fy_rt_004_unavailable_spills(mixed):
    """FY-RT-004: no ready replica, or no replica that can be connected to: spill, "unavailable"."""
    set_state("chat", 0, "starting", "loading")
    r = chat(mixed.api)
    assert r.status_code == 200 and r.headers["x-forgeyard-spilled"] == "unavailable"
    make_ready("chat", [(mixed.node, free_port(), "secret-a")])
    r = chat(mixed.api)
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"
    assert r.headers["x-forgeyard-spilled"] == "unavailable" and len(mixed.cloud.posts()) == 2


@pytest.mark.parametrize("status", [429, 503])
def test_fy_rt_005_an_overloaded_engine_spills(mixed, status):
    """FY-RT-005: the local engine answers 429 or 503 before any byte went to the client: spill,
    "refused"."""
    mixed.engine.status = status
    r = chat(mixed.api)
    assert r.status_code == 200 and r.headers["x-forgeyard-spilled"] == "refused"
    assert r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"


def test_fy_rt_006_what_never_spills(mixed, monkeypatch):
    """FY-RT-006: another 4xx from the engine is the answer; a replica refusing the gateway's key is
    502; a timeout after the request was sent is 504; a stream that breaks off ends with an error
    event. None of them reaches the cloud."""
    mixed.engine.status = 400
    r = chat(mixed.api)
    assert r.status_code == 400 and r.json()["error"]["message"] == "engine says no"
    mixed.engine.status = None
    make_ready("chat", [(mixed.node, mixed.engine.port, "not-the-engines-key")])
    r = chat(mixed.api)
    assert r.status_code == 502 and r.json()["error"]["code"] == "replica_refused_key"
    make_ready("chat", [(mixed.node, mixed.engine.port, mixed.engine.secret)])
    mixed.engine.cut_after = 1
    r = chat(mixed.api, stream=True)
    evs = events(r.text)
    assert r.status_code == 200 and evs[0]["choices"][0]["delta"]["content"] == "t0 "
    assert evs[-1]["error"]["code"] == "upstream_stream_broken"
    mixed.engine.cut_after = None
    monkeypatch.setenv("GATEWAY_READ_TIMEOUT", "0.3")
    get_settings.cache_clear()
    proxy._clients.clear()                 # a client with the shorter timeout
    mixed.engine.hold = 1.0
    r = chat(mixed.api)
    assert r.status_code == 504 and r.json()["error"]["code"] == "upstream_timeout"
    assert mixed.cloud.posts() == []


def test_fy_rt_007_a_long_prompt_spills_only_when_the_route_opts_in(mixed):
    """FY-RT-007: a prompt that (estimated) does not fit the deployment's context goes local unless
    the route says spill_on_long_prompt."""
    long = "x" * 20000                       # ~6,700 tokens estimated; the deployment takes 4,096
    r = chat(mixed.api, messages=[{"role": "user", "content": long}])
    assert r.headers["x-forgeyard-target"] == "local:chat#0"
    set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", mixed.pm)]], spill_on_long_prompt=True)
    r = chat(mixed.api, messages=[{"role": "user", "content": long}])
    assert r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini" and r.headers["x-forgeyard-spilled"] == "too_long"
    r = chat(mixed.api)                      # a short one still fits
    assert r.headers["x-forgeyard-target"] == "local:chat#0"


class _Fixed:
    """Equal weights tie, so targets are tried in the route's order."""

    def random(self):
        return 0.5


def test_fy_rt_008_the_cloud_tier_moves_on_and_cools_a_target_down(mixed, monkeypatch):
    """FY-RT-008: within the cloud tier a 429 (long Retry-After), a 5xx or a refused connection moves
    to the next target; the failed one is tried last for the cooldown."""
    monkeypatch.setattr(proxy, "rng", _Fixed())
    other = FakeCloud("second")
    try:
        p2 = add_provider(mixed.owner, other, name="second")
        pm2 = model_id(p2, "gpt-mini")
        set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", mixed.pm), ("provider", pm2)]])
        set_state("chat", 0, "starting", "loading")           # always spill
        mixed.cloud.status = 429
        mixed.cloud.headers_out = {"Retry-After": "30"}
        r = chat(mixed.api)
        assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:second/gpt-mini"
        assert len(mixed.cloud.posts()) == 1              # cloudy was first, answered 429
        for _ in range(3):
            assert chat(mixed.api).headers["x-forgeyard-target"] == "provider:second/gpt-mini"
        assert len(mixed.cloud.posts()) == 1              # cooling down: second goes first now
        assert balancer.cooldown_left(f"pm:{mixed.pm}") > 20
        other.status = 500                                # both fail: the last one tried is the answer
        r = chat(mixed.api)
        assert r.status_code == 429 and r.json()["error"]["code"] == "provider_rate_limited"
        assert len(mixed.cloud.posts()) == 2 and len(other.posts()) == 5
        balancer.reset()
        other.status = 503
        mixed.cloud.status = None
        r = chat(mixed.api)
        assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"
        balancer.reset()
        other.close()                                     # nothing listens there any more
        r = chat(mixed.api)
        assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"
    finally:
        other.close()


def test_fy_rt_009_a_short_retry_after_is_waited_for_once(mixed):
    """FY-RT-009: the only cloud target answers 429 with Retry-After 1 (within the cap): one retry
    after the wait. With Retry-After 30 the client gets the 429 and its Retry-After."""
    set_state("chat", 0, "starting", "loading")
    mixed.cloud.statuses = [429]
    mixed.cloud.headers_out = {"Retry-After": "1"}
    started = time.monotonic()
    r = chat(mixed.api)
    assert r.status_code == 200 and time.monotonic() - started >= 0.9 and len(mixed.cloud.posts()) == 2
    balancer.reset()
    mixed.cloud.status = 429
    mixed.cloud.headers_out = {"Retry-After": "30"}
    r = chat(mixed.api)
    assert r.status_code == 429 and r.json()["error"]["code"] == "provider_rate_limited"
    assert r.headers["retry-after"] == "30" and len(mixed.cloud.posts()) == 3


def test_fy_rt_010_local_only_header(mixed):
    """FY-RT-010: X-Forgeyard-Local-Only: 1 never spills: a busy local tier makes it wait, an
    unavailable one gives the local error, and a cloud-only model is refused."""
    only = {"X-Forgeyard-Local-Only": "1"}
    mixed.engine.hold = 0.5
    t, _ = background(lambda: chat(mixed.api))
    in_flight_soon()
    r = chat(mixed.api, headers=only)
    t.join()
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:chat#0"
    mixed.engine.hold = 0
    set_state("chat", 0, "starting", "loading")
    r = chat(mixed.api, headers=only)
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_ready_replica"
    r = mixed.owner.post("/api/routes", json={"served_name": "gpt", "api": "openai-chat",
                                              "targets": [{"kind": "provider", "provider_model_id": mixed.pm}]})
    assert r.status_code == 201, r.text
    on = api_client(mixed.owner, cloud_key(mixed.owner, "on-key", "on"))
    r = chat(on, model="gpt", headers=only)
    assert r.status_code == 400 and r.json()["error"]["code"] == "cloud_only_model"
    assert mixed.cloud.posts() == []
    assert chat(on, model="gpt").status_code == 200


def test_fy_rt_011_streaming_from_a_provider_and_its_usage(mixed):
    """FY-RT-011: a streamed answer from the provider passes through; the gateway asked for the usage
    chunk and strips it; usage is counted for the provider model, with cached and reasoning tokens,
    and as spilled."""
    set_state("chat", 0, "starting", "loading")
    r = chat(mixed.api, stream=True)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"
    evs = events(r.text)
    assert [e["choices"][0]["delta"].get("content") for e in evs[:3]] == ["c0 ", "c1 ", "c2 "] and evs[-1] == "[DONE]"
    assert not any(isinstance(e, dict) and "usage" in e for e in evs)
    assert mixed.cloud.posts()[-1]["body"]["stream_options"] == {"include_usage": True}
    r = chat(mixed.api, stream=True, stream_options={"include_usage": True})
    assert events(r.text)[-2]["usage"]["total_tokens"] == 18
    recorder.flush()
    rows = {x["id"]: x for x in mixed.owner.get("/api/usage?group=target").json()["rows"]}
    row = rows[f"pm:{mixed.pm}"]
    assert row["name"] == "cloudy/gpt-mini" and row["requests"] == 2 and row["spilled"] == 2
    assert (row["prompt_tokens"], row["completion_tokens"], row["cached_tokens"], row["reasoning_tokens"]) == (22, 14, 8, 4)


def test_fy_rt_012_what_leaves_for_the_provider(mixed):
    """FY-RT-012: the provider gets its own key and model name, the OpenAI kind's quirks (engine
    extras dropped, max_tokens as max_completion_tokens), and nothing of the client's: not its
    Forgeyard key, not its cookies."""
    set_state("chat", 0, "starting", "loading")
    r = chat(mixed.api, max_tokens=16, top_k=20, chat_template_kwargs={"enable_thinking": False}, temperature=0.2)
    assert r.status_code == 200
    seen = mixed.cloud.posts("/chat/completions")[-1]
    assert seen["path"] == "/v1/chat/completions"
    assert seen["headers"]["Authorization"] == f"Bearer {CLOUD_KEY}"
    assert seen["body"] == {"model": "gpt-mini", "messages": [{"role": "user", "content": "hi"}],
                            "max_completion_tokens": 16, "temperature": 0.2}
    assert mixed.key not in json.dumps(seen["headers"]) and "cookie" not in {k.lower() for k in seen["headers"]}
    assert CLOUD_KEY not in json.dumps(dict(r.headers)) and CLOUD_KEY not in r.text


def test_fy_rt_013_anthropic_style_provider(mixed):
    """FY-RT-013: an Anthropic-kind provider gets x-api-key and anthropic-version, max_tokens stays,
    and its always-empty usage details count as no cached and no reasoning tokens."""
    anth = FakeCloud("claude", key="sk-ant-test-abcdef0123456789", style="anthropic")
    try:
        p = add_provider(mixed.owner, anth, name="anthropic", kind="anthropic", models=(("claude-x", "openai-chat"),))
        set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", model_id(p, "claude-x"))]])
        set_state("chat", 0, "starting", "loading")
        r = chat(mixed.api, max_tokens=8, response_format={"type": "text"}, repetition_penalty=1.1)
        assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:anthropic/claude-x"
        seen = anth.posts()[-1]
        assert seen["headers"]["x-api-key"] == anth.key and seen["headers"]["anthropic-version"] == "2023-06-01"
        assert seen["body"] == {"model": "claude-x", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
        r = mixed.owner.post(f"/api/providers/{p['id']}/models",
                             json={"models": [{"upstream_model": "x-embed", "api": "openai-embeddings"}]})
        assert r.status_code == 422                     # Anthropic has no embeddings API
        recorder.flush()
        rows = {x["id"]: x for x in mixed.owner.get("/api/usage?group=target").json()["rows"]}
        row = rows[f"pm:{model_id(p, 'claude-x')}"]
        assert (row["prompt_tokens"], row["completion_tokens"], row["cached_tokens"], row["reasoning_tokens"]) == (13, 5, 0, 0)
    finally:
        anth.close()


def test_fy_rt_014_provider_errors_are_mapped(mixed):
    """FY-RT-014: a provider's 400 keeps its status and message (minus anything like a key); a
    provider refusing Forgeyard's key is a 502, never a 401 the client would blame on its key; a
    redirect is not followed."""
    set_state("chat", 0, "starting", "loading")
    mixed.cloud.status = 400
    mixed.cloud.error_message = "bad messages for key sk-live-abcdefghijklmnop and " + CLOUD_KEY
    r = chat(mixed.api)
    e = r.json()["error"]
    assert r.status_code == 400 and e["code"] == "e400" and e["param"] == "messages"
    assert e["message"].startswith("cloudy: bad messages") and "sk-live" not in e["message"] and CLOUD_KEY not in r.text
    mixed.cloud.status = None
    r = mixed.owner.patch(f"/api/providers/{mixed.provider['id']}", json={"api_key": "sk-wrong-key-000"})
    assert r.status_code == 200
    r = chat(mixed.api)
    assert r.status_code == 502 and r.json()["error"]["code"] == "provider_auth_failed"
    elsewhere = FakeCloud("elsewhere")
    try:
        mixed.cloud.redirect_to = f"http://127.0.0.1:{elsewhere.port}/v1/chat/completions"
        r = chat(mixed.api)
        assert r.status_code == 502 and r.json()["error"]["code"] == "provider_redirect"
        assert elsewhere.seen == []
    finally:
        elsewhere.close()


def test_fy_rt_015_busy_and_the_cloud_failing_waits_for_local_after_all(mixed):
    """FY-RT-015: local is busy and the cloud tier cannot take the request either: it waits for the
    local replica instead of failing."""
    mixed.engine.hold = 0.5
    mixed.cloud.status = 500
    t, _ = background(lambda: chat(mixed.api))
    in_flight_soon()
    r = chat(mixed.api)
    t.join()
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:chat#0"
    assert "x-forgeyard-spilled" not in r.headers and len(mixed.cloud.posts()) == 1
    # Answered locally after the cloud failed: free, not an unpriced cloud request (1.2 cost).
    total = mixed.owner.get("/api/usage?group=target").json()["total"]
    assert (total["unpriced"], total["split"]["local"]["requests"]) == (0, 2)


def test_fy_rt_016_embeddings_spill_and_rerank_stays_local(mixed):
    """FY-RT-016: embeddings routes take provider targets; rerank and audio routes cannot."""
    emb = FakeEngine("emb", "secret-e")
    try:
        gpu = make_node("a100", allowlist=[4], features=KEYS)
        deploy(mixed.owner, "embed", "qwen3-embedding-0.6b", "vllm-pooling", wait=True)
        deploy(mixed.owner, "rerank", "qwen3-reranker-0.6b", "vllm-pooling", wait=True)
        make_ready("embed", [(gpu, emb.port, emb.secret)])
        p = mixed.owner.post(f"/api/providers/{mixed.provider['id']}/models",
                             json={"models": [{"upstream_model": "text-embed-small", "api": "openai-embeddings"}]})
        pm = model_id(p.json()["provider"], "text-embed-small")
        set_tiers(mixed.owner, "embed", [[("local", deployment_id(mixed.owner, "embed"))], [("provider", pm)]])
        set_state("embed", 0, "starting", "loading")
        r = mixed.api.post("/v1/embeddings", json={"model": "embed", "input": "x"})
        assert r.status_code == 200 and r.json()["data"][0]["embedding"] == [0.5, 0.25]
        assert r.headers["x-forgeyard-target"] == "provider:cloudy/text-embed-small"
        r = mixed.owner.put(f"/api/routes/{route_id(mixed.owner, 'rerank')}", json={"targets": [
            {"kind": "local", "deployment_id": deployment_id(mixed.owner, "rerank")},
            {"tier": 1, "kind": "provider", "provider_model_id": pm}]})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "wrong_api"
        r = mixed.owner.put(f"/api/routes/{route_id(mixed.owner, 'chat')}", json={"targets": [
            {"kind": "local", "deployment_id": mixed.dep}, {"tier": 1, "kind": "provider", "provider_model_id": pm}]})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "wrong_api"   # an embeddings model
        r = mixed.owner.put(f"/api/routes/{route_id(mixed.owner, 'chat')}", json={"targets": [
            {"kind": "provider", "provider_model_id": mixed.pm}]})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "owner_required"
        assert mixed.owner.delete(f"/api/routes/{route_id(mixed.owner, 'chat')}").status_code == 409
    finally:
        emb.close()


def test_fy_rt_017_a_rerank_route_refuses_any_provider_model(mixed):
    """FY-RT-017: provider targets serve chat and embeddings only. The API never creates a provider
    model of another kind, so this guard is the only thing keeping one that got into the database
    (an older row, a manual edit) off a rerank route: even an exact api match is refused."""
    from forgeyard.db import ProviderModel, new_session

    deploy(mixed.owner, "rerank", "qwen3-reranker-0.6b", "vllm-pooling", wait=True)
    db = new_session()
    try:
        row = ProviderModel(provider_id=mixed.provider["id"], upstream_model="cloud-reranker", api="rerank",
                            drop_params=[])
        db.add(row)
        db.commit()
        pm = row.id
    finally:
        db.close()
    r = mixed.owner.put(f"/api/routes/{route_id(mixed.owner, 'rerank')}", json={"targets": [
        {"kind": "local", "deployment_id": deployment_id(mixed.owner, "rerank")},
        {"tier": 1, "kind": "provider", "provider_model_id": pm}]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "wrong_api"
    assert "chat and embeddings only" in r.json()["detail"]["message"]
