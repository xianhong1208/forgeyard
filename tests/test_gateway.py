"""The gateway at /v1: keys only (never the cookie), no CSRF but the host check, routing by model,
least in-flight balancing, failover before the first byte, streaming with usage, error shapes, the
playground, and the official OpenAI SDK against it (FY-GW-*)."""

import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from forgeyard.db import ApiKey, ApiKey as _K, User, Workload, new_session  # noqa: F401
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.usage import recorder
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import (FakeEngine, LiveServer, api_client, free_port, make_ready, new_key, set_state)
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

PW = "quiet-meadow-4821"
KEYS = MODELS + ("replica_keys",)


@pytest.fixture()
def gw(owner):
    seed_catalog()
    a = make_node("cpu-a", features=KEYS)
    b = make_node("cpu-b", features=KEYS)
    gpu = make_node("a100", allowlist=[4], features=KEYS)
    e1, e2, emb = FakeEngine("a", "secret-a"), FakeEngine("b", "secret-b"), FakeEngine("emb", "secret-e")
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", replicas=2, params={"gpus": 0, "threads": 2}, wait=True)
    deploy(owner, "embed", "qwen3-embedding-0.6b", "vllm-pooling", wait=True)
    deploy(owner, "rerank", "qwen3-reranker-0.6b", "vllm-pooling", wait=True)
    deploy(owner, "asr", "faster-whisper-small", "faster-whisper", wait=True)
    make_ready("chat", [(a, e1.port, e1.secret), (b, e2.port, e2.secret)])
    for name in ("embed", "rerank", "asr"):
        make_ready(name, [(gpu, emb.port, emb.secret)])
    key = new_key(owner, "laptop")["key"]
    ns = SimpleNamespace(owner=owner, a=a, b=b, gpu=gpu, e1=e1, e2=e2, emb=emb, key=key, api=api_client(owner, key))
    yield ns
    for e in (e1, e2, emb):
        e.close()


def chat(c, **extra):
    return c.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "hi"}], **extra})


def events(text: str) -> list:
    out = []
    for block in text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            data = block[6:]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


def err(r):
    body = r.json()
    assert set(body) == {"error"} and {"message", "type", "code"} <= set(body["error"]), body
    return body["error"]


def test_fy_gw_001_chat_goes_to_a_replica_with_its_internal_key(gw):
    """FY-GW-001: the engine gets the replica's own key, the upstream model name and the body; never
    the client's API key or cookies."""
    r = chat(gw.api, max_tokens=16)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"].startswith("hello from")
    replica = int(r.headers["x-forgeyard-replica"].split("#")[1])
    engine = (gw.e1, gw.e2)[replica]
    [seen] = engine.posts("/v1/chat/completions")
    assert seen["auth"] == f"Bearer {engine.secret}" and gw.key not in json.dumps(seen["headers"])
    assert "cookie" not in {k.lower() for k in seen["headers"]}
    assert seen["body"] == {"model": "chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    assert r.headers["cache-control"] == "no-store"


def test_fy_gw_002_requests_alternate_between_replicas(gw):
    """FY-GW-002: one at a time, every replica has 0 in flight, so they take turns."""
    seen = [chat(gw.api).headers["x-forgeyard-replica"] for _ in range(6)]
    assert seen in (["chat#0", "chat#1"] * 3, ["chat#1", "chat#0"] * 3)
    assert len(gw.e1.posts()) == 3 and len(gw.e2.posts()) == 3
    dep = gw.owner.get("/api/deployments").json()["deployments"]
    eps = next(d for d in dep if d["name"] == "chat")["endpoints"]
    assert [(e["served"], e["in_flight"], e["protected"]) for e in eps] == [(3, 0, True), (3, 0, True)]


def test_fy_gw_003_least_in_flight_wins(gw):
    """FY-GW-003: while replica 0 holds a slow request, the next ones go to replica 1."""
    gw.e1.hold = 1.5
    first = chat(gw.api)  # replica 0 or 1 first; make sure the slow one is busy
    slow_replica = first.headers["x-forgeyard-replica"]
    slow = gw.e1 if slow_replica == "chat#0" else gw.e2
    fast_name = "chat#1" if slow is gw.e1 else "chat#0"
    slow.hold, (gw.e2 if slow is gw.e1 else gw.e1).hold = 1.5, 0.0
    # Point the next turn at the slow replica: one quick request goes to the other one first.
    while chat(gw.api).headers["x-forgeyard-replica"] != fast_name:
        pass
    results = {}
    t = threading.Thread(target=lambda: results.setdefault("slow", chat(gw.api)))
    t.start()
    deadline = time.monotonic() + 5
    while sum(balancer.inflight.values()) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    during = [chat(gw.api).headers["x-forgeyard-replica"] for _ in range(4)]
    t.join()
    assert results["slow"].headers["x-forgeyard-replica"] == slow_replica
    assert during == [fast_name] * 4


def test_fy_gw_004_failover_before_the_first_byte(gw):
    """FY-GW-004: replica 0's engine is gone (nothing listens): every request is answered by replica 1,
    and replica 0 is tried last for a while."""
    make_ready("chat", [(gw.a, free_port(), "secret-a"), (gw.b, gw.e2.port, gw.e2.secret)])
    answers = [chat(gw.api) for _ in range(4)]
    assert all(r.status_code == 200 and r.headers["x-forgeyard-replica"] == "chat#1" for r in answers)
    assert len(gw.e2.posts()) == 4
    assert balancer.snapshot(balancer_uid("chat", 0))["served"] == 0  # it never got one


def balancer_uid(workload, ordinal):
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == workload))
        return next(r.uid for r in w.replicas if r.ordinal == ordinal)
    finally:
        db.close()


def test_fy_gw_005_unreachable_and_not_ready(gw):
    """FY-GW-005: 502 when no replica can be reached, 503 with the reason when none is ready."""
    make_ready("chat", [(gw.a, free_port(), "x"), (gw.b, free_port(), "y")])
    r = chat(gw.api)
    assert r.status_code == 502 and err(r)["code"] == "upstream_unreachable" and err(r)["type"] == "upstream_error"
    set_state("chat", 0, "starting", "loading; waiting for the health check to pass")
    set_state("chat", 1, "backoff", "exited with code 1; restarting in 4 s")
    r = chat(gw.api)
    e = err(r)
    assert r.status_code == 503 and e["code"] == "no_ready_replica" and e["type"] == "service_unavailable"
    assert "replica 0 on cpu-a: loading" in e["message"] and "replica 1 on cpu-b: exited with code 1" in e["message"]
    dep = next(d for d in gw.owner.get("/api/deployments").json()["deployments"] if d["name"] == "embed")
    assert gw.owner.post(f"/api/deployments/{dep['id']}/stop").status_code == 200
    r = gw.api.post("/v1/embeddings", json={"model": "embed", "input": "x"})
    assert r.status_code == 503 and "stopped" in err(r)["message"]


def test_fy_gw_006_streaming_passes_through_and_counts_usage(gw):
    """FY-GW-006: SSE passes through; the gateway asks for the usage chunk and strips it again when the
    client did not ask; when the client did, it arrives unchanged. Tokens are counted either way."""
    r = chat(gw.api, stream=True)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = events(r.text)
    assert evs[-1] == "[DONE]" and [e["choices"][0]["delta"].get("content") for e in evs[:3]] == ["t0 ", "t1 ", "t2 "]
    assert not any(isinstance(e, dict) and "usage" in e for e in evs)
    engine = gw.e1 if r.headers["x-forgeyard-replica"] == "chat#0" else gw.e2
    assert engine.posts()[-1]["body"]["stream_options"] == {"include_usage": True}
    r = chat(gw.api, stream=True, stream_options={"include_usage": True})
    evs = events(r.text)
    assert evs[-2]["choices"] == [] and evs[-2]["usage"]["total_tokens"] == 30
    recorder.flush()
    total = gw.owner.get("/api/usage?group=model").json()["total"]
    assert total["requests"] == 2 and total["prompt_tokens"] == 20 and total["completion_tokens"] == 40


def test_fy_gw_007_only_api_keys_open_v1(gw, owner):
    """FY-GW-007: no key, a wrong key, the console's cookie, a revoked key (at once), an expired key
    and a key of a disabled account all get 401 in OpenAI's shape."""
    assert err(chat(api_client(owner)))["code"] == "missing_api_key"
    assert chat(api_client(owner, "fyk_" + "x" * 43)).status_code == 401
    assert err(chat(api_client(owner, "sk-something")))["code"] == "invalid_api_key"
    cookie_only = owner.post("/v1/chat/completions", json={"model": "chat", "messages": []})
    assert cookie_only.status_code == 401 and err(cookie_only)["type"] == "authentication_error"
    assert owner.get("/v1/models").status_code == 401
    k = new_key(owner, "temp")
    c = api_client(owner, k["key"])
    assert chat(c).status_code == 200
    assert owner.delete(f"/api/keys/{k['api_key']['id']}").status_code == 200
    r = chat(c)
    assert r.status_code == 401 and err(r)["code"] == "revoked_api_key"
    k2 = new_key(owner, "short", expires_in_days=1)
    db = new_session()
    try:
        row = db.scalar(select(ApiKey).where(ApiKey.public_id == k2["api_key"]["id"]))
        from datetime import timedelta

        from forgeyard.db import utcnow
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    assert err(chat(api_client(owner, k2["key"])))["code"] == "expired_api_key"
    add_user(owner, "ursula")
    u = sign_in(owner, "ursula", PW)
    uk = new_key(u, "mine")["key"]
    assert chat(api_client(owner, uk)).status_code == 200
    uid = next(x["id"] for x in owner.get("/api/users").json()["users"] if x["username"] == "ursula")
    assert owner.patch(f"/api/users/{uid}", json={"status": "disabled"}).status_code == 200
    assert err(chat(api_client(owner, uk)))["code"] == "account_disabled"


def test_fy_gw_008_no_csrf_no_cors_but_the_host_check(gw):
    """FY-GW-008: programs need no X-Forgeyard header and any Origin is fine (a key is not a cookie a
    browser sends by itself); a foreign Host still gets 421; no CORS headers are ever sent."""
    from fastapi.testclient import TestClient

    bare = TestClient(gw.owner.app, base_url="http://localhost")
    bare.portal = gw.owner.portal
    r = bare.post("/v1/chat/completions", json={"model": "chat", "messages": []},
                  headers={"Authorization": f"Bearer {gw.key}", "Origin": "https://evil.example"})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
    r = bare.options("/v1/chat/completions", headers={"Origin": "https://evil.example",
                                                      "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
    r = gw.api.post("/v1/chat/completions", json={"model": "chat"}, headers={"Host": "evil.example"})
    assert r.status_code == 421 and err(r)["code"] == "host_not_allowed"
    # The console's per-IP budget is for /api; /v1 is limited per key.
    from forgeyard.config import get_settings
    import os
    os.environ["RATE_LIMIT_PER_MINUTE"] = "2"
    get_settings.cache_clear()
    try:
        assert all(gw.api.get("/v1/models").status_code == 200 for _ in range(4))
    finally:
        del os.environ["RATE_LIMIT_PER_MINUTE"]
        get_settings.cache_clear()


def test_fy_gw_009_models_allowlist_and_error_shapes(gw):
    """FY-GW-009: /v1/models lists what the key may use; 403 outside its list, 404 for an unknown
    model, 400 for the wrong endpoint, 404 for an unknown path."""
    names = [m["id"] for m in gw.api.get("/v1/models").json()["data"]]
    assert names == ["asr", "chat", "embed", "rerank"]
    only = api_client(gw.owner, new_key(gw.owner, "chat-only", models=["chat"])["key"])
    assert [m["id"] for m in only.get("/v1/models").json()["data"]] == ["chat"]
    assert only.get("/v1/models/chat").json()["id"] == "chat"
    assert chat(only).status_code == 200
    r = only.post("/v1/embeddings", json={"model": "embed", "input": "x"})
    assert r.status_code == 403 and err(r) == {"message": "This API key may not use the model 'embed'.",
                                               "type": "permission_error", "param": "model", "code": "model_not_allowed"}
    assert only.get("/v1/models/embed").status_code == 403
    r = gw.api.post("/v1/chat/completions", json={"model": "nope", "messages": []})
    assert r.status_code == 404 and err(r)["code"] == "model_not_found"
    r = gw.api.post("/v1/embeddings", json={"model": "chat", "input": "x"})
    assert r.status_code == 400 and err(r)["code"] == "wrong_endpoint" and "/v1/chat/completions" in err(r)["message"]
    r = gw.api.post("/v1/chat/completions", content=b"{nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and err(r)["code"] == "invalid_json"
    r = gw.api.get("/v1/nothing/here")
    assert r.status_code == 404 and err(r)["code"] == "unknown_endpoint"
    assert len(gw.emb.posts()) == 0  # nothing refused reached an engine


def test_fy_gw_010_embeddings_rerank_score_and_transcriptions(gw):
    """FY-GW-010: each protocol reaches its route; speaches gets the Hugging Face id as `model` and
    the audio untouched; tokens are counted from each answer."""
    r = gw.api.post("/v1/embeddings", json={"model": "embed", "input": ["a", "b"]})
    assert r.status_code == 200 and r.json()["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    r = gw.api.post("/v1/rerank", json={"model": "rerank", "query": "q", "documents": ["a", "b"]})
    assert r.json()["results"][0]["relevance_score"] == 0.98
    assert gw.api.post("/v1/score", json={"model": "rerank", "text_1": "q", "text_2": ["a"]}).status_code == 200
    audio = bytes(range(256)) * 40 + b"\r\n--not-a-boundary\r\n"
    r = gw.api.post("/v1/audio/transcriptions", data={"model": "asr", "response_format": "json"},
                    files={"file": ("clip.mp3", audio, "audio/mpeg")})
    assert r.status_code == 200 and "ask not" in r.json()["text"]
    seen = gw.emb.posts("/v1/audio/transcriptions")[-1]
    assert seen["auth"] == "Bearer secret-e" and b"Systran/faster-whisper-small" in seen["raw"]
    assert b'name="model"\r\n\r\nasr\r\n' not in seen["raw"] and audio in seen["raw"]
    assert seen["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    recorder.flush()
    rows = {r["id"]: r for r in gw.owner.get("/api/usage?group=model").json()["rows"]}
    assert rows["embed"]["prompt_tokens"] == 4 and rows["rerank"]["prompt_tokens"] == 24 and rows["asr"]["requests"] == 1


def test_fy_gw_011_engine_errors_and_a_refused_internal_key(gw):
    """FY-GW-011: an engine's own 4xx answer is passed on as it is; an engine refusing the gateway's
    internal key is the gateway's problem (502), never a 401 the client would blame on its key."""
    gw.emb.status = 400
    r = gw.api.post("/v1/embeddings", json={"model": "embed", "input": "x"})
    assert r.status_code == 400 and r.json()["error"]["message"] == "engine says no"
    gw.emb.status = None
    make_ready("embed", [(gw.gpu, gw.emb.port, "not-the-engines-key")])
    r = gw.api.post("/v1/embeddings", json={"model": "embed", "input": "x"})
    assert r.status_code == 502 and err(r)["code"] == "replica_refused_key"
    recorder.flush()
    total = gw.owner.get("/api/usage").json()["total"]
    assert total["requests"] == 2 and total["errors"] == 2


def test_fy_gw_012_a_client_that_goes_away_stops_the_engine(gw):
    """FY-GW-012: on a real server, reading one chunk of a long stream and hanging up closes the
    upstream connection: the engine never finishes the stream."""
    gw.e1.chunks = gw.e2.chunks = 200
    gw.e1.delay = gw.e2.delay = 0.05
    live = LiveServer()
    try:
        with httpx.Client(base_url=live.url, headers={"Authorization": f"Bearer {gw.key}"}, timeout=10) as c:
            with c.stream("POST", "/v1/chat/completions",
                          json={"model": "chat", "stream": True, "messages": []}) as resp:
                assert resp.status_code == 200
                first = next(resp.iter_raw())
                assert b"t0" in first
        deadline = time.monotonic() + 10
        while gw.e1.disconnects + gw.e2.disconnects < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert gw.e1.disconnects + gw.e2.disconnects == 1
        assert gw.e1.finished_streams + gw.e2.finished_streams == 0
        deadline = time.monotonic() + 5
        while sum(balancer.inflight.values()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sum(balancer.inflight.values()) == 0
    finally:
        live.close()
    recorder.flush()
    total = gw.owner.get("/api/usage").json()["total"]
    assert total["requests"] == 1 and total["errors"] == 0  # the client went away: not an error


def test_fy_gw_013_the_official_openai_sdk(gw):
    """FY-GW-013: the official OpenAI Python SDK against /v1: chat, a streamed chat, models,
    embeddings, a transcription, and its exceptions for our error answers."""
    openai = pytest.importorskip("openai")
    c = openai.OpenAI(base_url="http://localhost/v1", api_key=gw.key, http_client=gw.api, max_retries=0)
    out = c.chat.completions.create(model="chat", messages=[{"role": "user", "content": "hi"}], max_tokens=8)
    assert out.choices[0].message.content.startswith("hello from") and out.usage.total_tokens == 30
    parts = [ch.choices[0].delta.content for ch in c.chat.completions.create(
        model="chat", messages=[{"role": "user", "content": "hi"}], stream=True) if ch.choices]
    assert "".join(p for p in parts if p) == "t0 t1 t2 "
    assert sorted(m.id for m in c.models.list()) == ["asr", "chat", "embed", "rerank"]
    assert c.embeddings.create(model="embed", input="x").data[0].embedding == [0.1, 0.2, 0.3]
    text = c.audio.transcriptions.create(model="asr", file=("clip.mp3", b"\x00" * 64, "audio/mpeg"))
    assert "ask not" in text.text
    with pytest.raises(openai.NotFoundError):
        c.chat.completions.create(model="nope", messages=[])
    bad = openai.OpenAI(base_url="http://localhost/v1", api_key="fyk_wrong", http_client=gw.api, max_retries=0)
    with pytest.raises(openai.AuthenticationError):
        bad.models.list()
    limited = new_key(gw.owner, "slow", rpm=1)["key"]
    lc = openai.OpenAI(base_url="http://localhost/v1", api_key=limited, http_client=gw.api, max_retries=0)
    lc.models.list()  # /v1/models is not counted
    lc.chat.completions.create(model="chat", messages=[])
    with pytest.raises(openai.RateLimitError) as e:
        lc.chat.completions.create(model="chat", messages=[])
    assert int(e.value.response.headers["retry-after"]) >= 1


def test_fy_gw_014_playground_uses_the_session_not_a_key(gw, owner):
    """FY-GW-014: the playground is a console endpoint (cookie + CSRF), works for every role, counts as
    "console", and never creates or shows an API key."""
    from fastapi.testclient import TestClient

    add_user(owner, "ursula")
    u = sign_in(owner, "ursula", PW)
    before = _count(ApiKey)
    r = u.post("/api/playground/chat/completions", json={"model": "chat", "stream": True,
                                                         "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and events(r.text)[-1] == "[DONE]"
    assert _count(ApiKey) == before and "fyk_" not in r.text
    bare = TestClient(owner.app, base_url="http://localhost", cookies=u.cookies)
    assert bare.post("/api/playground/chat/completions", json={"model": "chat"}).json()["detail"]["code"] == "csrf"
    anon = api_client(owner)
    anon.headers["X-Forgeyard"] = "1"
    r = anon.post("/api/playground/chat/completions", json={"model": "chat"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "not_signed_in"
    assert u.post("/api/playground/nothing", json={}).status_code == 404
    r = u.post("/api/playground/embeddings", json={"model": "chat", "input": "x"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "wrong_endpoint"
    recorder.flush()
    rows = {x["id"]: x for x in owner.get("/api/usage?group=source&everyone=true").json()["rows"]}
    assert rows["console"]["requests"] == 2 and "api" not in rows


def _count(model) -> int:
    db = new_session()
    try:
        return db.scalar(select(func.count()).select_from(model))
    finally:
        db.close()


def test_fy_gw_015_a_deployments_page_shows_its_v1_name_and_usage(gw):
    """FY-GW-015"""
    for _ in range(3):
        chat(gw.api)
    dep = next(d for d in gw.owner.get("/api/deployments").json()["deployments"] if d["name"] == "chat")
    detail = gw.owner.get(f"/api/deployments/{dep['id']}").json()["deployment"]
    assert detail["route"]["served_name"] == "chat" and detail["usage"]["total"]["requests"] == 3
    assert detail["usage"]["total"]["tokens"] == 90 and detail["usage"]["by_day"][0]["requests"] == 3
    assert "secret" not in json.dumps(detail) and "secret-a" not in json.dumps(detail)


def test_fy_gw_016_try_it_sends_the_replicas_internal_key(gw):
    """FY-GW-016: "try it" calls one replica directly, so it must carry that replica's key."""
    dep = next(d for d in gw.owner.get("/api/deployments").json()["deployments"] if d["name"] == "embed")
    out = gw.owner.post(f"/api/deployments/{dep['id']}/try", json={}).json()
    assert out["ok"] and out["summary"].startswith("3 dimensions")
    assert gw.emb.posts("/v1/embeddings")[-1]["auth"] == "Bearer secret-e" and gw.emb.refused == 0
