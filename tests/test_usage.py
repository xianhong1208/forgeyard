"""Usage statistics: counting per day, source, key, user and model; latency percentiles; who sees
what (FY-USE-*)."""

from forgeyard.db import new_session
from forgeyard.gateway import proxy
from forgeyard.gateway.usage import BUCKETS_MS, Recorder, bucket_of, percentile, summary
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import api_client, new_key
from tests.test_gateway import chat, gw  # noqa: F401  (the fixture)

PW = "quiet-meadow-4821"
DAY = 86400.0


def test_fy_use_001_rows_per_day_and_slot_with_percentiles(client):
    """FY-USE-001: the recorder sums per (day, source, key, user, model), writes on flush, adds to
    the same row on the next flush, and starts a new row on a new UTC day."""
    t = [1_762_732_800.0]  # 2025-11-10 00:00 UTC
    rec = Recorder(clock=lambda: t[0])
    for ms in (40, 90, 150, 700, 3000):
        rec.record(source="api", key_id=1, user_id=1, username="owner", model="chat", deployment_id=5, status=200,
                   latency_ms=ms, prompt_tokens=10, completion_tokens=5)
    rec.record(source="api", key_id=1, user_id=1, username="owner", model="chat", deployment_id=5, status=429,
               latency_ms=1, limited=True)
    rec.record(source="console", key_id=0, user_id=1, username="owner", model="chat", deployment_id=5, status=499,
               latency_ms=20)
    assert rec.flush() == 2 and rec.flush() == 0
    rec.record(source="api", key_id=1, user_id=1, username="owner", model="chat", deployment_id=5, status=502,
               latency_ms=10)
    t[0] += DAY
    rec.record(source="api", key_id=1, user_id=1, username="owner", model="chat", deployment_id=5, status=200,
               latency_ms=10, prompt_tokens=1)
    rec.flush()
    db = new_session()
    try:
        s = summary(db, days=7, group_by="day", today="2025-11-11")
        assert [(r["id"], r["requests"]) for r in s["rows"]] == [("2025-11-10", 8), ("2025-11-11", 1)]
        first = s["rows"][0]
        assert first["errors"] == 2 and first["limited"] == 1  # the 429 and the 502; 499 is not an error
        assert first["prompt_tokens"] == 50 and first["completion_tokens"] == 25 and first["tokens"] == 75
        by_source = {r["id"]: r for r in summary(db, days=7, group_by="source", today="2025-11-11")["rows"]}
        assert by_source["console"]["requests"] == 1 and by_source["api"]["requests"] == 8
        assert summary(db, days=1, group_by="model", today="2025-11-11")["total"]["requests"] == 1
        assert summary(db, days=7, deployment_id=6, today="2025-11-11")["total"]["requests"] == 0
    finally:
        db.close()


def test_fy_use_002_percentiles_from_buckets():
    """FY-USE-002"""
    assert bucket_of(50) == 0 and bucket_of(51) == 1 and bucket_of(10**9) == len(BUCKETS_MS)
    buckets = [0] * (len(BUCKETS_MS) + 1)
    assert percentile(buckets, 0.5) is None
    buckets[bucket_of(150)] = 100            # all in (100, 200]
    assert percentile(buckets, 0.5) == 150.0 and percentile(buckets, 0.95) == 195.0
    buckets[bucket_of(3000)] = 100           # half in (1600, 3200]
    assert percentile(buckets, 0.5) == 200.0 and 1600 < percentile(buckets, 0.95) <= 3200


def test_fy_use_003_people_see_their_own_usage_admins_everyones(gw):  # noqa: F811
    """FY-USE-003: requests are counted under the key's owner; a user sees only their own rows, even
    when asking for everyone's; an admin sees all, grouped by user, key or model."""
    add_user(gw.owner, "ursula")
    u = sign_in(gw.owner, "ursula", PW)
    uc = api_client(gw.owner, new_key(u, "u-key")["key"])
    for _ in range(2):
        assert chat(uc).status_code == 200
    assert chat(gw.api).status_code == 200
    mine = u.get("/api/usage?everyone=true&group=user").json()
    assert mine["everyone"] is False and [(r["name"], r["requests"]) for r in mine["rows"]] == [("ursula", 2)]
    allu = gw.owner.get("/api/usage?everyone=true&group=user").json()
    assert {r["name"]: r["requests"] for r in allu["rows"]} == {"ursula": 2, "owner": 1}
    keys = {r["name"] for r in gw.owner.get("/api/usage?everyone=true&group=key").json()["rows"]}
    assert any(n.startswith("u-key (fyk_") for n in keys) and any(n.startswith("laptop (fyk_") for n in keys)
    assert gw.owner.get("/api/usage?group=nope").status_code == 422
    total = allu["total"]
    assert total["tokens"] == 90 and total["latency_p50_ms"] is not None and total["latency_avg_ms"] > 0


def test_fy_use_004_token_counts_from_answers():
    """FY-USE-004: prompt/completion tokens from `usage`; embeddings and rerank report input only."""
    assert proxy.tokens_of({"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}) == (3, 4)
    assert proxy.tokens_of({"prompt_tokens": 4, "total_tokens": 4}) == (4, 0)
    assert proxy.tokens_of({"total_tokens": 12}) == (12, 0)
    assert proxy.tokens_of(None) == (0, 0) and proxy.tokens_of({"prompt_tokens": True}) == (0, 0)


def test_fy_use_005_sse_filter():
    """FY-USE-005: events pass as they complete (also split across reads); the usage-only chunk the
    gateway asked for is dropped, a `usage` on a chunk with choices is removed, and with a client
    that asked for usage nothing changes."""
    usage_chunk = b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'
    body = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n' + usage_chunk + b"data: [DONE]\n\n"
    f = proxy.SSEFilter(strip_usage=True)
    out = b"".join(f.feed(body[i:i + 7]) for i in range(0, len(body), 7)) + f.end()
    assert out == b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n' + b"data: [DONE]\n\n"
    assert f.usage == {"prompt_tokens": 2, "completion_tokens": 3}
    keep = proxy.SSEFilter(strip_usage=False)
    assert keep.feed(body) == body and keep.usage["completion_tokens"] == 3
    mixed = proxy.SSEFilter(strip_usage=True)
    out = mixed.feed(b'data: {"choices":[{"delta":{}}],"usage":{"total_tokens":9}}\r\n\r\n')
    assert out == b'data: {"choices":[{"delta":{}}]}\n\n' and mixed.usage == {"total_tokens": 9}
    partial = proxy.SSEFilter(strip_usage=True)
    assert partial.feed(b'data: {"choices":[') == b"" and partial.end() == b'data: {"choices":['
