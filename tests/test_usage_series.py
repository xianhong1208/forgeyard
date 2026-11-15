"""Usage over time (1.4): the Usage page's line charts, one value per day, week or month summed from the
daily rows, in total or per key, model, provider or person, with the smaller groups summed into "Other";
who sees what (FY-USE-006 to FY-USE-011)."""

from datetime import date, datetime, timezone

from forgeyard.db import new_session
from forgeyard.gateway import pricing
from forgeyard.gateway.usage import Recorder, period_list, period_start, recorder, series
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import api_client, new_key, set_state
from tests.test_cost import GPT
from tests.test_gateway import chat, gw  # noqa: F401  (the fixture)
from tests.test_routing import chat as routed_chat, mixed  # noqa: F401  (the fixture)

PW = "quiet-meadow-4821"


def at(day: str) -> float:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() + 3600


def seeded(days: dict[str, list[dict]]) -> None:
    """Writes requests as the gateway does, each on its UTC day: {"2025-11-03": [{"model": ...}, ...]}."""
    t = [0.0]
    rec = Recorder(clock=lambda: t[0])
    for day, requests in days.items():
        t[0] = at(day)
        for r in requests:
            r = {"source": "api", "key_id": 1, "user_id": 1, "username": "owner", "model": "chat", "deployment_id": None,
                 "status": 200, "latency_ms": 10, **r}
            rec.record(**r)
    rec.flush()


def read(**kw) -> dict:
    db = new_session()
    try:
        return series(db, **kw)
    finally:
        db.close()


def test_fy_use_006_periods_are_days_monday_weeks_and_calendar_months():
    """FY-USE-006: weeks start on Monday and months on the 1st (UTC); the last period runs to today and is
    the only one still going; months count back across a new year."""
    today = date(2026, 1, 14)   # a Wednesday
    assert period_start(today, "week") == date(2026, 1, 12) and period_start(today, "month") == date(2026, 1, 1)
    days = period_list(today, "day", 3)
    assert days == [(date(2026, 1, 12), date(2026, 1, 12)), (date(2026, 1, 13), date(2026, 1, 13)), (today, today)]
    weeks = period_list(today, "week", 3)
    assert weeks == [(date(2025, 12, 29), date(2026, 1, 4)), (date(2026, 1, 5), date(2026, 1, 11)),
                     (date(2026, 1, 12), today)]
    months = period_list(today, "month", 4)
    assert [s for s, _ in months] == [date(2025, 10, 1), date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1)]
    assert months[1][1] == date(2025, 11, 30) and months[2][1] == date(2025, 12, 31) and months[-1][1] == today
    assert period_list(date(2024, 3, 1), "month", 2) == [(date(2024, 2, 1), date(2024, 2, 29)),
                                                         (date(2024, 3, 1), date(2024, 3, 1))]
    assert len(period_list(today, "day", 1)) == 1


def test_fy_use_007_daily_rows_add_up_per_day_week_and_month(client):
    """FY-USE-007: every view sums the same daily rows: a day with nothing is 0, a week holds its Monday to
    Sunday, a month its calendar days; days before the first period are left out; `totals` has every
    metric over the range (tokens = in + out) so the page shows only the units in use."""
    seeded({
        "2025-10-26": [{}],                                                   # a Sunday, before the 3 weeks
        "2025-10-27": [{"prompt_tokens": 10, "completion_tokens": 4}] * 2,   # Monday of week 1
        "2025-11-02": [{"status": 500}],                                      # Sunday of week 1
        "2025-11-05": [{"units": {"images": 2, "busy_ms": 900}}],            # week 2
        "2025-11-12": [{}] * 3,                                               # today, week 3
    })
    days = read(step="day", periods=17, today="2025-11-12")
    assert days["from"] == "2025-10-27" and days["to"] == "2025-11-12" and len(days["periods"]) == 17
    assert days["total"][0] == 2 and days["total"][6] == 1 and days["total"][9] == 1 and days["total"][-1] == 3
    assert sum(days["total"]) == 7 and days["total"][1] == 0
    assert [p["partial"] for p in days["periods"]] == [False] * 16 + [True]
    weeks = read(step="week", periods=3, today="2025-11-12")
    assert [p["start"] for p in weeks["periods"]] == ["2025-10-27", "2025-11-03", "2025-11-10"]
    assert weeks["total"] == [3, 1, 3] and weeks["series"] == [
        {"id": "total", "name": "Total", "values": [3, 1, 3], "total": 7}]
    assert weeks["totals"]["tokens"] == 28 and weeks["totals"]["prompt_tokens"] == 20
    assert weeks["totals"]["errors"] == 1 and weeks["totals"]["images"] == 2 and weeks["totals"]["busy_ms"] == 900
    assert weeks["totals"]["characters"] == 0
    tokens = read(step="week", periods=3, metric="tokens", today="2025-11-12")
    assert tokens["total"] == [28, 0, 0]
    months = read(step="month", periods=2, today="2025-11-12")
    assert [p["start"] for p in months["periods"]] == ["2025-10-01", "2025-11-01"]
    assert months["total"] == [3, 5]  # the week of 27 October ends in November and months["periods"][0]["end"] == "2025-10-31"
    empty = read(step="month", periods=2, today="2026-06-01")
    assert empty["series"] == [] and empty["total"] == [0, 0] and empty["omitted"] == 0


def test_fy_use_008_split_keeps_the_largest_and_sums_the_rest_into_other(client):
    """FY-USE-008: split per model, the largest groups by the chosen metric get a line each, the rest one
    "Other" line holding their sum; groups at 0 over the range are left out and counted; ranking follows
    the metric (by cost, a free local model is left out); per person, by name."""
    reqs = {}
    for i, n in enumerate((6, 5, 4, 3, 2, 1, 1)):
        reqs.setdefault("2025-11-10", []).extend([{"model": f"m{i}"}] * n)
    reqs["2025-11-11"] = [{"model": "cloud-a", "target": "pm:7", "cost_picos": 3_000_000, "cost_method": pricing.COMPUTED,
                           "user_id": 2, "username": "ursula", "key_id": 2}]
    seeded(reqs)
    out = read(step="day", periods=2, split="model", today="2025-11-11", top=4)
    assert [s["name"] for s in out["series"]] == ["m0", "m1", "m2", "m3", "Other (4)"]
    other = out["series"][-1]
    assert other["id"] == "other" and other["other"] is True and other["values"] == [4, 1] and other["total"] == 5
    assert sum(s["total"] for s in out["series"]) == sum(out["total"]) == 23 and out["omitted"] == 0
    cost = read(step="day", periods=2, split="model", metric="cost_micros", today="2025-11-11", top=4)
    assert [(s["name"], s["values"]) for s in cost["series"]] == [("cloud-a", [0, 3])] and cost["omitted"] == 7
    people = read(step="day", periods=2, split="user", today="2025-11-11")
    assert {s["name"]: s["total"] for s in people["series"]} == {"owner": 22, "ursula": 1}
    keys = read(step="day", periods=2, split="key", today="2025-11-11")
    assert {s["name"] for s in keys["series"]} == {"deleted key #1", "deleted key #2"}
    one = read(step="day", periods=2, split="model", today="2025-11-11", top=8)
    assert len(one["series"]) == 8 and all(s["id"] != "other" for s in one["series"])
    mine = read(step="day", periods=2, split="model", user_id=2, today="2025-11-11")
    assert [s["name"] for s in mine["series"]] == ["cloud-a"] and mine["totals"]["requests"] == 1


def test_fy_use_009_people_see_their_own_series_admins_everyones(gw):  # noqa: F811
    """FY-USE-009: GET /api/usage/series counts a user's own keys only, also when asking for everyone's;
    an admin sees everyone's with ?everyone=true, split by person or key; the view's limits are checked."""
    add_user(gw.owner, "ursula")
    u = sign_in(gw.owner, "ursula", PW)
    uc = api_client(gw.owner, new_key(u, "u-key")["key"])
    for _ in range(2):
        assert chat(uc).status_code == 200
    assert chat(gw.api).status_code == 200
    mine = u.get("/api/usage/series?everyone=true&split=user&periods=7").json()
    assert mine["everyone"] is False and mine["is_admin"] is False
    assert [(s["name"], s["total"]) for s in mine["series"]] == [("ursula", 2)] and mine["total"][-1] == 2
    everyone = gw.owner.get("/api/usage/series?everyone=true&split=user&step=week&periods=4").json()
    assert everyone["everyone"] is True and {s["name"]: s["total"] for s in everyone["series"]} == {"ursula": 2, "owner": 1}
    assert len(everyone["periods"]) == 4 and everyone["periods"][-1]["partial"] is True
    own = gw.owner.get("/api/usage/series?split=key&metric=tokens").json()
    assert own["everyone"] is False and [s["name"].split(" (")[0] for s in own["series"]] == ["laptop"]
    assert own["series"][0]["total"] == 30 and own["totals"]["requests"] == 1
    for bad, code in (("step=hour", "bad_step"), ("split=source", "bad_split"), ("metric=latency", "bad_metric"),
                      ("step=day&periods=367", "too_many_periods"), ("step=week&periods=105", "too_many_periods"),
                      ("step=month&periods=37", "too_many_periods"), ("periods=0", None)):
        r = gw.owner.get(f"/api/usage/series?{bad}")
        assert r.status_code == 422, bad
        if code:
            assert r.json()["detail"]["code"] == code, bad
    assert gw.owner.get("/api/usage/series?step=month&periods=36").status_code == 200


def test_fy_use_010_cost_per_provider_through_the_gateway(mixed):  # noqa: F811
    """FY-USE-010: a request that spilled to a priced provider shows on the cost chart under that provider,
    marked cloud; the local replicas cost nothing, so their line is left out; by requests both are there."""
    m = mixed
    m.owner.patch(f"/api/provider-models/{m.pm}", json=GPT)
    assert routed_chat(m.api).headers["x-forgeyard-target"] == "local:chat#0"
    set_state("chat", 0, "starting", "loading")
    assert routed_chat(m.api).headers["x-forgeyard-target"] == "provider:cloudy/gpt-mini"
    recorder.flush()
    cost = m.owner.get("/api/usage/series?split=provider&metric=cost_micros&periods=7").json()
    assert [(s["name"], s["cloud"], s["total"]) for s in cost["series"]] == [("cloudy", True, 93)]
    assert cost["omitted"] == 1 and cost["total"][-1] == 93 and cost["totals"]["cost_micros"] == 93
    reqs = {s["id"]: s for s in m.owner.get("/api/usage/series?split=provider&periods=7").json()["series"]}
    assert reqs["local"]["cloud"] is False and reqs["local"]["total"] == 1 and reqs[f"p:{m.provider['id']}"]["total"] == 1


def test_fy_use_011_the_tables_and_the_charts_agree(client):
    """FY-USE-011: over the same days, the chart's per-day totals equal the Usage table's rows grouped by
    day, for requests, tokens and cost."""
    from forgeyard.gateway.usage import summary

    seeded({
        "2025-11-08": [{"prompt_tokens": 3, "completion_tokens": 1}] * 3,
        "2025-11-10": [{"target": "pm:2", "cost_picos": 2_000_000, "cost_method": pricing.COMPUTED}] * 2,
        "2025-11-11": [{"model": "other", "prompt_tokens": 5}],
    })
    db = new_session()
    try:
        table = {r["id"]: r for r in summary(db, days=7, group_by="day", today="2025-11-11")["rows"]}
    finally:
        db.close()
    for metric in ("requests", "tokens", "cost_micros"):
        chart = read(step="day", periods=7, metric=metric, today="2025-11-11")
        by_day = {p["start"]: v for p, v in zip(chart["periods"], chart["total"]) if v}
        assert by_day == {d: r[metric] for d, r in table.items() if r[metric]}, metric
