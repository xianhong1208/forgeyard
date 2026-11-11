"""Budgets (1.2): monthly cloud spend per API key, person and provider, reserved before a request is
sent and settled with its real cost; a budget that cannot cover a request keeps it local (or another
provider serves it), and when nothing else can, 402 names the budget and when it resets; unpriced
models under a budget are blocked; 80% and 100% alerts once per month; who may set budgets; the
ledger's seeding and month; the migration (FY-BDG-*). Every provider is a local stand-in."""

import threading
import time
from datetime import datetime, timezone

import pytest
from alembic import command
from sqlalchemy import select, text

from forgeyard.db import ApiKey, AuditEvent, BudgetAlert, Provider, User, get_engine, new_session
from forgeyard.gateway import budgets, pricing, proxy
from forgeyard.gateway.budgets import Blocked, Hold, Ledger, Limit, ledger
from forgeyard.gateway.pricing import Prices
from forgeyard.gateway.usage import recorder
from forgeyard.migrate import alembic_config, upgrade
from tests.cloud_helpers import FakeCloud, add_provider, cloud_key, model_id, set_tiers
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import api_client, new_key, set_state
from tests.test_cost import GPT
from tests.test_routing import _Fixed, background, chat, in_flight_soon, mixed  # noqa: F401  (the fixture)

PW = "quiet-meadow-4821"
P = pricing.PICOS_PER_MICRO
NOV = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DEC = datetime(2026, 12, 1, 0, 0, 1, tzinfo=timezone.utc).timestamp()
BODY = {"model": "chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
# The cost of one answer from the stand-in at GPT's prices: (11 - 4) x 2.5 + 4 x 1.25 + 7 x 10 micro-USD.
ANSWER_PICOS = 7 * 2_500_000 + 4 * 1_250_000 + 7 * 10_000_000


def need() -> int:
    """What the gateway reserves for BODY at GPT's prices, in pico-USD."""
    return budgets.estimate(BODY, 10, "chat/completions", Prices(input=2_500_000, output=10_000_000,
                                                                  cache_read=1_250_000), 4096)


def key_id(c, name: str) -> str:
    return next(k["id"] for k in c.get("/api/keys").json()["keys"] if k["name"] == name)


def flushed() -> None:
    recorder.flush()
    ledger.write_alerts()


def audit_actions(c, prefix="budget.") -> list[dict]:
    return [e for e in c.get("/api/audit").json()["events"] if e["action"].startswith(prefix)]


@pytest.fixture()
def priced(mixed):  # noqa: F811
    r = mixed.owner.patch(f"/api/provider-models/{mixed.pm}", json=GPT)
    assert r.status_code == 200, r.text
    mixed.kid = key_id(mixed.owner, "fallback-key")
    return mixed


def send(m, **extra):
    return m.api.post("/v1/chat/completions", json={**BODY, **extra})


# ------------------------------------------------------------------ the ledger
def test_fy_bdg_001_reserve_settle_release():
    """FY-BDG-001: a reservation counts against every budget that applies until it is settled (with
    the real cost) or released; one that any budget cannot cover reserves nothing anywhere."""
    lg = Ledger(clock=lambda: NOV)
    lg._month = "2026-11"                      # seeded, empty
    key, person = Limit("key", 1, 100, "k"), Limit("person", 7, 1000, "bob")
    h = lg.reserve([key, person], 60 * P)
    assert isinstance(h, Hold) and lg._held == {("key", 1): 60 * P, ("person", 7): 60 * P}
    b = lg.reserve([key, person], 50 * P)       # 60 held + 50 > 100
    assert isinstance(b, Blocked) and b.limit == key and (b.spent_picos, b.held_picos, b.need_picos) == (0, 60 * P, 50 * P)
    assert lg._held[("person", 7)] == 60 * P     # nothing reserved for the refused one
    lg.settle(h, key_id=1, user_id=7, provider_id=3, picos=45 * P)
    assert lg._held == {} and lg._spent == {("key", 1): 45 * P, ("person", 7): 45 * P, ("provider", 3): 45 * P}
    h2 = lg.reserve([key], 55 * P)               # 45 + 55 = 100: exactly fits
    assert isinstance(h2, Hold)
    assert isinstance(lg.reserve([key], 1), Blocked)
    lg.release(h2)
    h3 = lg.reserve([key], 30 * P)
    lg.release(h2)                               # twice is harmless: h3 is still held
    assert lg._held == {("key", 1): 30 * P} and isinstance(lg.reserve([key], 25 * P), Hold)
    assert isinstance(lg.reserve([key], 1), Blocked)
    u = lg.reserve([person], None)               # no price: blocked, unpriced
    assert isinstance(u, Blocked) and u.unpriced and u.error().code == "budget_unpriced"
    assert isinstance(lg.reserve([], None), Hold)  # no budget applies: nothing to reserve


def test_fy_bdg_002_the_estimate():
    """FY-BDG-002: the prompt (messages, prompt or input, plus tools) at about 3 bytes a token,
    rounded up, plus max_tokens (or the default cap) times n; embeddings have no completion; a
    missing price that is needed makes it unknown."""
    p = Prices(input=1_000_000, output=2_000_000)
    body = {"messages": [{"role": "user", "content": "x" * 300}]}
    size = len('[{"role": "user", "content": "' + "x" * 300 + '"}]')
    prompt = -(-size // 3)
    assert budgets.prompt_tokens(body) == prompt
    assert budgets.estimate(body, 50, "chat/completions", p, 4096) == prompt * 1_000_000 + 50 * 2_000_000
    assert budgets.estimate(body, None, "chat/completions", p, 4096) == prompt * 1_000_000 + 4096 * 2_000_000
    assert budgets.estimate({**body, "n": 3}, 50, "chat/completions", p, 4096) == prompt * 1_000_000 + 150 * 2_000_000
    tools = {**body, "tools": [{"type": "function", "function": {"name": "f" * 90}}]}
    assert budgets.prompt_tokens(tools) > prompt
    assert budgets.estimate({"input": "abc"}, None, "embeddings", Prices(input=1_000_000), 4096) == 1_000_000 * 2  # "abc": 5 bytes
    assert budgets.estimate(body, 50, "chat/completions", Prices(input=1), 4096) is None
    assert budgets.estimate(body, 50, "chat/completions", Prices(output=1), 4096) is None
    assert budgets.prompt_tokens({}) == 1


def test_fy_bdg_003_concurrent_reservations_never_overspend():
    """FY-BDG-003: 40 threads reserving at once against a budget for 10: exactly 10 holds."""
    lg = Ledger(clock=lambda: NOV)
    lg._month = "2026-11"
    lim = Limit("key", 1, 10 * 7, "k")
    out, gate = [], threading.Barrier(40)

    def one():
        gate.wait()
        out.append(lg.reserve([lim], 7 * P))

    threads = [threading.Thread(target=one) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(isinstance(x, Hold) for x in out) == 10 and lg._held[("key", 1)] == 70 * P


def test_fy_bdg_004_seeded_from_this_months_usage_once(owner):
    """FY-BDG-004: the ledger starts from this month's usage rows (not last month's), per key, person
    and provider; usage still held in memory is written first; a request finishing after the seeding
    is added once (settle and counting happen under one lock)."""
    db = new_session()
    try:
        prov = Provider(name="p1", kind="openai", base_url="https://api.example.org/v1")
        db.add(prov)
        db.flush()
        from forgeyard.db import ProviderModel

        pm = ProviderModel(provider_id=prov.id, upstream_model="m", api="openai-chat", drop_params=[])
        db.add(pm)
        db.commit()
        pm_id, prov_id = pm.id, prov.id
    finally:
        db.close()
    month = budgets.month_of(time.time())
    last = budgets.month_of(time.time() - 40 * 86400)
    with get_engine().begin() as conn:
        for day, cost in ((f"{month}-01", 300), (f"{last}-28", 9999)):
            conn.execute(text("INSERT INTO usage_daily (day, source, key_id, user_id, username, model, target, requests, "
                              "errors, limited, prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens, spilled, "
                              "cost_micros, unpriced, cost_reported, cost_estimated, latency_ms_sum, latency_buckets) "
                              f"VALUES ('{day}', 'api', 5, 9, 'bob', 'm', 'pm:{pm_id}', 1, 0, 0, 1, 1, 0, 0, 0, {cost}, "
                              "0, 0, 0, 1, '[]')"))
    # Counted but not written yet: the console (no key), 40 micro-USD.
    recorder.record(source="console", key_id=0, user_id=9, username="bob", model="m", deployment_id=None, status=200,
                    latency_ms=1, target=f"pm:{pm_id}", cost_picos=40 * P, cost_method=pricing.COMPUTED)
    s = ledger.snapshot()
    assert s.month == month
    assert s.spent == {("key", 5): 300, ("person", 9): 340, ("provider", prov_id): 340}

    def count():
        recorder.record(source="api", key_id=5, user_id=9, username="bob", model="m", deployment_id=None,
                        status=200, latency_ms=1, target=f"pm:{pm_id}", cost_picos=10 * P, cost_method=pricing.COMPUTED)

    ledger.settle(None, key_id=5, user_id=9, provider_id=prov_id, picos=10 * P, then=count)
    assert ledger.snapshot().spent[("key", 5)] == 310
    ledger.reset()
    assert ledger.snapshot().spent[("key", 5)] == 310   # read again from the rows: still once


def test_fy_bdg_005_a_new_month_starts_from_zero(owner):
    """FY-BDG-005: budgets are per UTC calendar month: at the turn of the month the spend is read
    again (nothing yet), and the reset time is the first of the next month, 00:00 UTC."""
    t = [NOV]
    lg = Ledger(clock=lambda: t[0])
    lim = Limit("key", 1, 100, "k")
    h = lg.reserve([lim], 90 * P)
    lg.settle(h, key_id=1, user_id=2, provider_id=3, picos=90 * P)
    assert isinstance(lg.reserve([lim], 20 * P), Blocked)
    t[0] = DEC
    assert isinstance(lg.reserve([lim], 20 * P), Hold) and lg.snapshot().month == "2026-12"
    assert budgets.resets_at("2026-11") == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert budgets.resets_at("2026-12") == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert budgets.month_of(DEC - 2) == "2026-11"


def test_fy_bdg_006_alerts_once_per_scope_month_and_threshold(owner):
    """FY-BDG-006: 80% and 100% of a budget's spend each write one audit event and one row a month,
    also after a restart (a new ledger); a request refused past 80% counts as 100%; one refused far
    below 80% (a single large request) does not alert."""
    lim = Limit("key", 1, 100, "laptop")
    h = ledger.reserve([lim], 50 * P)
    ledger.settle(h, key_id=1, user_id=1, provider_id=1, picos=50 * P)
    assert ledger.write_alerts() == 0
    h = ledger.reserve([lim], 30 * P)
    ledger.settle(h, key_id=1, user_id=1, provider_id=1, picos=30 * P)         # 80%
    h = ledger.reserve([lim], 5 * P)
    ledger.settle(h, key_id=1, user_id=1, provider_id=1, picos=5 * P)          # 85%: no second 80% alert
    assert len(ledger._pending) == 1                                           # not queued again either
    assert ledger.write_alerts() == 1
    assert isinstance(ledger.reserve([lim], 20 * P), Blocked)                  # 85 + 20 > 100, past 80%
    assert ledger.write_alerts() == 1
    events = audit_actions(owner)
    assert [e["action"] for e in events] == ["budget.exhausted", "budget.warning"]
    assert events[0]["detail"]["refused_request"] is True and events[0]["detail"]["threshold"] == 100
    assert events[1]["detail"].items() >= {"scope": "key", "name": "laptop", "spent_micros": 80, "budget_micros": 100,
                                           "month": budgets.month_of(time.time())}.items()
    h = ledger.reserve([lim], 15 * P)
    ledger.settle(h, key_id=1, user_id=1, provider_id=1, picos=15 * P)         # 100% spent: alerted already
    assert ledger.write_alerts() == 0
    # Another server start: the rows say they were written.
    ledger.reset()
    ledger._check(lim)
    assert ledger.write_alerts() == 0
    fresh = Ledger()
    fresh._month = budgets.month_of(time.time())
    fresh._spent = {("key", 1): 100 * P}
    fresh._check(lim)
    assert fresh.write_alerts() == 0                                           # the unique row stops it
    db = new_session()
    try:
        assert db.scalar(select(BudgetAlert).where(BudgetAlert.threshold == 80)).spent_micros == 80
        assert len(db.scalars(select(BudgetAlert)).all()) == 2
    finally:
        db.close()
    big = Limit("person", 2, 1000, "bob")
    assert isinstance(ledger.reserve([big], 2000 * P), Blocked)                # 0% spent: no alert
    assert ledger.write_alerts() == 0


# ------------------------------------------------------------------ through the gateway
def test_fy_bdg_007_a_key_budget_reserves_settles_and_then_keeps_requests_local(priced):
    """FY-BDG-007: a spilled request is reserved and settled at its real cost; once the key's budget
    cannot cover the next one, a busy local tier is waited for (served locally, with
    X-Forgeyard-Budget-Exhausted: key) and an unavailable one gets 402 budget_exhausted naming the
    budget and its reset; the provider never hears of those."""
    m = priced
    budget = (2 * ANSWER_PICOS + need()) // P - 1              # 2 answers, not a third reservation
    r = m.owner.patch(f"/api/keys/{m.kid}", json={"budget_micros": budget})
    assert r.status_code == 200 and r.json()["api_key"]["budget"]["budget_micros"] == budget
    set_state("chat", 0, "starting", "loading")
    for _ in range(2):
        r = send(m)
        assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"
        assert "x-forgeyard-budget-exhausted" not in r.headers
    assert ledger.snapshot().spent[("key", 1)] == pricing.micros_of(2 * ANSWER_PICOS) and ledger._held == {}
    r = send(m)
    assert r.status_code == 402 and r.headers["x-forgeyard-budget-exhausted"] == "key"
    err = r.json()["error"]
    assert err["code"] == "budget_exhausted" and err["type"] == "insufficient_quota"
    assert "this API key (fallback-key)" in err["message"] and "resets on" in err["message"]
    assert err["budget"]["scope"] == "key" and err["budget"]["budget_micros"] == budget
    assert err["budget"]["resets_at"] == budgets.resets_at(budgets.month_of(time.time())).isoformat().replace("+00:00", "Z")
    assert len(m.cloud.posts()) == 2
    # Local busy: the request would spill, but waits for the local replica instead.
    set_state("chat", 0, "healthy")
    m.engine.hold = 0.6
    t, first = background(lambda: send(m))
    in_flight_soon()
    r = send(m)
    t.join()
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:chat#0"
    assert r.headers["x-forgeyard-budget-exhausted"] == "key" and "x-forgeyard-spilled" not in r.headers
    assert len(m.cloud.posts()) == 2
    # Another key of the same person is not limited by this key's budget.
    other = api_client(m.owner, cloud_key(m.owner, "other", "fallback"))
    set_state("chat", 0, "starting", "loading")
    r = other.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"


def test_fy_bdg_008_a_person_budget_covers_all_their_keys_and_the_playground(priced):
    """FY-BDG-008: a person's budget counts every key of theirs and their playground; when it cannot
    cover a request, none of them reaches the cloud."""
    m = priced
    db = new_session()
    try:
        uid = db.scalar(select(User.id).where(User.username == "owner"))
    finally:
        db.close()
    r = m.owner.put(f"/api/budgets/people/{uid}", json={"budget_micros": (ANSWER_PICOS + need()) // P})
    assert r.status_code == 200, r.text
    set_state("chat", 0, "starting", "loading")
    assert send(m).status_code == 200                                     # key 1: 92.5 spent
    other = api_client(m.owner, cloud_key(m.owner, "other", "fallback"))
    r = other.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 402 and r.headers["x-forgeyard-budget-exhausted"] == "person"
    assert "the person owner" in r.json()["error"]["message"]
    r = m.owner.post("/api/playground/chat/completions", json=BODY)
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget_exhausted"
    assert len(m.cloud.posts()) == 1


def test_fy_bdg_009_a_provider_budget_skips_that_provider_only(priced, monkeypatch):
    """FY-BDG-009: a provider whose budget cannot cover a request is skipped; another provider in the
    tier serves it, and the answer names the budget; with no other provider left, 402 names it."""
    m = priced
    monkeypatch.setattr(proxy, "rng", _Fixed())             # the route's order: cloudy first
    second = FakeCloud("second")
    try:
        p2 = add_provider(m.owner, second, name="second")
        pm2 = model_id(p2, "gpt-mini")
        m.owner.patch(f"/api/provider-models/{pm2}", json=GPT)
        set_tiers(m.owner, "chat", [[("local", m.dep)], [("provider", m.pm), ("provider", pm2)]])
        r = m.owner.put(f"/api/budgets/providers/{m.provider['id']}", json={"budget_micros": 0})
        assert r.status_code == 200 and r.json()["provider"]["level"] == "exhausted"
        set_state("chat", 0, "starting", "loading")
        for _ in range(3):
            r = send(m)
            assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "provider:second/gpt-mini"
            assert r.headers["x-forgeyard-budget-exhausted"] == "provider:cloudy"
        assert m.cloud.posts() == [] and len(second.posts()) == 3
        set_tiers(m.owner, "chat", [[("local", m.dep)], [("provider", m.pm)]])
        r = send(m)
        assert r.status_code == 402 and "the provider cloudy" in r.json()["error"]["message"]
        assert r.json()["error"]["budget"]["scope"] == "provider"
        listed = m.owner.get(f"/api/providers/{m.provider['id']}").json()["provider"]["budget"]
        assert listed["budget_micros"] == 0 and listed["spent_micros"] == 0
        p2_budget = m.owner.get(f"/api/providers/{p2['id']}").json()["provider"]["budget"]
        assert p2_budget["budget_micros"] is None and p2_budget["spent_micros"] == pricing.micros_of(3 * ANSWER_PICOS)
    finally:
        second.close()


def test_fy_bdg_010_unpriced_models_are_blocked_only_under_a_budget(mixed):  # noqa: F811
    """FY-BDG-010: a model without prices is used as before when no budget applies; under any budget
    it is blocked with 402 budget_unpriced, telling the admin to set prices."""
    m = mixed
    set_state("chat", 0, "starting", "loading")
    assert send(m).status_code == 200
    m.owner.patch(f"/api/keys/{key_id(m.owner, 'fallback-key')}", json={"budget_micros": 1_000_000})
    r = send(m)
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget_unpriced"
    assert "set its prices" in r.json()["error"]["message"] and r.headers["x-forgeyard-budget-exhausted"] == "key"
    assert len(m.cloud.posts()) == 1
    m.owner.patch(f"/api/provider-models/{m.pm}", json=GPT)
    assert send(m).status_code == 200


def test_fy_bdg_011_failures_release_and_in_flight_requests_hold(priced):
    """FY-BDG-011: a request in flight shows as reserved; a provider failing (5xx, 429) or refusing the
    request (400) releases its reservation and costs nothing."""
    m = priced
    m.owner.patch(f"/api/keys/{m.kid}", json={"budget_micros": 1_000_000})
    set_state("chat", 0, "starting", "loading")
    m.cloud.hold = 0.8
    t, out = background(lambda: send(m))
    deadline = time.monotonic() + 5
    while not ledger._held and time.monotonic() < deadline:
        time.sleep(0.01)
    k = next(x for x in m.owner.get("/api/budgets").json()["keys"] if x["id"] == m.kid)
    assert k["reserved_micros"] == pricing.micros_of(need())
    t.join()
    assert out["r"].status_code == 200 and ledger._held == {}
    m.cloud.hold = 0
    spent = ledger.snapshot().spent[("key", 1)]
    for status in (500, 429, 400):
        m.cloud.statuses = [status]
        r = send(m)
        assert r.status_code >= 400 and ledger._held == {}, status
    assert ledger.snapshot().spent[("key", 1)] == spent


def test_fy_bdg_012_concurrent_requests_do_not_overspend(priced):
    """FY-BDG-012: 12 requests at once against a budget that reserves room for 4: exactly 4 reach the
    provider and the spend stays within the budget."""
    m = priced
    budget = 4 * need() // P + 1
    m.owner.patch(f"/api/keys/{m.kid}", json={"budget_micros": budget})
    set_state("chat", 0, "starting", "loading")
    m.cloud.hold = 0.5
    threads = [background(lambda: send(m)) for _ in range(12)]
    for t, _ in threads:
        t.join()
    codes = sorted(out["r"].status_code for _, out in threads)
    assert codes.count(200) == 4 and codes.count(402) == 8
    assert len(m.cloud.posts()) == 4
    assert ledger.snapshot().spent[("key", 1)] <= budget and ledger._held == {}


def test_fy_bdg_013_an_unknown_cost_counts_at_its_reservation(priced):
    """FY-BDG-013: an answer without usage (its cost unknown) counts against the budget at what was
    reserved for it, not 0."""
    m = priced
    m.owner.patch(f"/api/keys/{m.kid}", json={"budget_micros": 1_000_000})
    set_state("chat", 0, "starting", "loading")
    m.cloud.usage = {"note": "no tokens"}
    assert send(m).status_code == 200
    assert ledger.snapshot().spent[("key", 1)] == pricing.micros_of(need())


def test_fy_bdg_014_who_may_set_budgets(priced):
    """FY-BDG-014: a key's owner sets and removes its budget; an admin's budget on someone else's key
    is locked for the owner (null unlocks it); people and provider budgets are admin-only; everyone
    reads their own spend, admins everyone's; every change is audited."""
    m = priced
    add_user(m.owner, "bob")
    bob = sign_in(m.owner, "bob", PW)
    kb = new_key(bob, "bobs", budget_micros=5_000_000)["api_key"]
    assert kb["budget"] == {"budget_micros": 5_000_000, "spent_micros": 0, "reserved_micros": 0, "used": 0.0,
                            "level": "ok"} and kb["budget_locked_by"] is None
    assert bob.patch(f"/api/keys/{kb['id']}", json={"budget_micros": None}).json()["api_key"]["budget"]["budget_micros"] is None
    assert bob.patch(f"/api/keys/{kb['id']}", json={"budget_micros": -1}).status_code == 422
    r = m.owner.patch(f"/api/keys/{kb['id']}", json={"budget_micros": 2_000_000})
    assert r.status_code == 200 and r.json()["api_key"]["budget_locked_by"] == "owner"
    r = bob.patch(f"/api/keys/{kb['id']}", json={"budget_micros": 9_000_000})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "limit_locked" and "monthly budget" in r.text
    assert bob.patch(f"/api/keys/{kb['id']}", json={"budget_micros": 2_000_000, "name": "same"}).status_code == 200
    r = m.owner.patch(f"/api/keys/{kb['id']}", json={"budget_micros": None})
    assert r.json()["api_key"]["budget_locked_by"] is None and r.json()["api_key"]["budget"]["budget_micros"] is None
    assert bob.patch(f"/api/keys/{kb['id']}", json={"budget_micros": 7}).status_code == 200
    db = new_session()
    try:
        bob_id = db.scalar(select(User.id).where(User.username == "bob"))
    finally:
        db.close()
    assert bob.put(f"/api/budgets/people/{bob_id}", json={"budget_micros": 1}).status_code == 403
    assert bob.put(f"/api/budgets/providers/{m.provider['id']}", json={"budget_micros": 1}).status_code == 403
    assert m.owner.put("/api/budgets/people/999", json={"budget_micros": 1}).status_code == 404
    assert m.owner.put(f"/api/budgets/people/{bob_id}", json={"budget_micros": 3_000_000}).status_code == 200
    mine = bob.get("/api/budgets?everyone=true").json()
    assert mine["everyone"] is False and [p["username"] for p in mine["people"]] == ["bob"]
    assert mine["people"][0]["budget_micros"] == 3_000_000 and mine["providers"] == []
    assert [k["name"] for k in mine["keys"]] == ["same"]
    assert bob.get("/api/keys").json()["person_budget"]["budget_micros"] == 3_000_000
    everyone = m.owner.get("/api/budgets?everyone=true").json()
    assert {p["username"] for p in everyone["people"]} == {"owner", "bob"}
    assert [p["name"] for p in everyone["providers"]] == ["cloudy"] and everyone["month"] == budgets.month_of(time.time())
    assert {k["name"] for k in everyone["keys"]} == {"same"}               # keys with a budget or spend
    acts = [(e["action"], e["detail"].get("locked")) for e in m.owner.get("/api/audit").json()["events"]]
    assert ("api_key.changed", ["budget_micros"]) in acts and ("user.budget_changed", None) in acts


def test_fy_bdg_015_the_migration_adds_no_budget(owner):
    """FY-BDG-015: keys, people and providers from before 0011 come through the upgrade without a
    budget (unlimited), and the downgrade drops the columns and the alerts without losing a key or a
    provider model."""
    cloud = FakeCloud("stub")
    try:
        new_key(owner, "old-key")
        db = new_session()
        try:
            db.add(Provider(name="old-p", kind="openai", base_url="https://api.example.org/v1"))
            db.commit()
        finally:
            db.close()
        cfg = alembic_config()
        with get_engine().begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0010_cost")
        with get_engine().connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM api_keys")).scalar() == 1
            tables = {r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'table'" if conn.dialect.name == "sqlite" else
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}
            assert "budget_alerts" not in tables
        upgrade()
        db = new_session()
        try:
            k = db.scalar(select(ApiKey).where(ApiKey.name == "old-key"))
            assert (k.budget_micros, k.budget_locked_by) == (None, None)
            assert db.scalar(select(Provider.budget_micros).where(Provider.name == "old-p")) is None
            assert all(u.budget_micros is None for u in db.scalars(select(User)).all())
        finally:
            db.close()
    finally:
        cloud.close()


def test_fy_bdg_016_alerts_reach_the_audit_log_through_the_gateway(priced):
    """FY-BDG-016: spend through the gateway passing 80% of a key's budget writes budget.warning, and a
    request refused after that writes budget.exhausted; /api/keys shows the level."""
    m = priced
    m.owner.patch(f"/api/keys/{m.kid}", json={"budget_micros": (ANSWER_PICOS + need()) // P + 1})
    set_state("chat", 0, "starting", "loading")
    assert send(m).status_code == 200                       # 92.5 of 223: 41%
    flushed()
    assert audit_actions(m.owner) == []
    assert send(m).status_code == 200                       # 185 of 223: 83%
    flushed()
    k = next(x for x in m.owner.get("/api/keys").json()["keys"] if x["id"] == m.kid)
    assert k["budget"]["level"] == "warning" and 0.8 <= k["budget"]["used"] < 1
    assert [e["action"] for e in audit_actions(m.owner)] == ["budget.warning"]
    assert send(m).status_code == 402
    flushed()
    assert [e["action"] for e in audit_actions(m.owner)] == ["budget.exhausted", "budget.warning"]
    db = new_session()
    try:
        assert db.scalar(select(AuditEvent.actor_name).where(AuditEvent.action == "budget.warning")) == "forgeyard"
    finally:
        db.close()


def test_fy_bdg_017_a_provider_inside_your_network_is_never_budgeted(owner, monkeypatch):
    """FY-BDG-017: a provider marked as inside your network costs nothing and no budget applies to it,
    even a budget of 0 on the key and the person."""
    from forgeyard.config import get_settings

    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    own = FakeCloud("own")
    try:
        p = add_provider(owner, own, name="vpn-vllm", kind="openai_compatible", external=False, allow_http=True)
        pm = model_id(p, "gpt-mini")
        r = owner.post("/api/routes", json={"served_name": "own", "api": "openai-chat",
                                            "targets": [{"kind": "provider", "provider_model_id": pm}]})
        assert r.status_code == 201, r.text
        api = api_client(owner, cloud_key(owner, "k", "off", budget_micros=0))
        owner.put("/api/budgets/people/1", json={"budget_micros": 0})
        r = api.post("/v1/chat/completions", json={"model": "own", "messages": []})
        assert r.status_code == 200 and "x-forgeyard-budget-exhausted" not in r.headers
        assert ledger._held == {} and "budget" not in owner.get(f"/api/providers/{p['id']}").json()["provider"]
    finally:
        own.close()


def test_fy_bdg_018_a_providers_own_failure_is_said_before_a_budget(priced, monkeypatch):
    """FY-BDG-018: one provider kept out by its budget and the other failing: the client gets the
    provider's error (502), still with the header naming the budget."""
    m = priced
    monkeypatch.setattr(proxy, "rng", _Fixed())
    second = FakeCloud("second")
    try:
        p2 = add_provider(m.owner, second, name="second")
        pm2 = model_id(p2, "gpt-mini")
        m.owner.patch(f"/api/provider-models/{pm2}", json=GPT)
        # second first: it fails, then cloudy is kept out by its budget, last.
        set_tiers(m.owner, "chat", [[("local", m.dep)], [("provider", pm2), ("provider", m.pm)]])
        m.owner.put(f"/api/budgets/providers/{m.provider['id']}", json={"budget_micros": 0})
        second.status = 500
        set_state("chat", 0, "starting", "loading")
        r = send(m)
        assert r.status_code == 502 and r.json()["error"]["code"] == "provider_error"
        assert r.headers["x-forgeyard-budget-exhausted"] == "provider:cloudy" and ledger._held == {}
    finally:
        second.close()


def test_fy_bdg_019_a_failed_cloud_attempt_holds_nothing_while_waiting_for_local(priced):
    """FY-BDG-019: local busy, the cloud tier fails (500): the request waits for the local replica
    after all, and its reservation is released at once, not held while it waits."""
    m = priced
    m.owner.patch(f"/api/keys/{m.kid}", json={"budget_micros": 1_000_000})
    m.engine.hold = 1.2
    t, first = background(lambda: send(m))
    in_flight_soon()
    m.cloud.status = 500
    t2, second = background(lambda: send(m))
    deadline = time.monotonic() + 5
    while len(m.cloud.posts()) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    held_while_waiting = dict(ledger._held)
    t.join()
    t2.join()
    assert held_while_waiting == {}
    assert second["r"].status_code == 200 and second["r"].headers["x-forgeyard-target"] == "local:chat#0"
