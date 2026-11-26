"""Teams (1.6.1, #190): teams and their members, team API keys that outlive the person who made them, deployments a
team's managers manage, usage counted to the key's team, the team cloud budget, deleting a team, the migration, and
an install without teams working exactly as before (FY-TEAM-*)."""

import time
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import inspect, select, text

from forgeyard.db import ApiKey, UsageDaily, User, get_engine, new_session
from forgeyard.gateway import budgets, pricing
from forgeyard.gateway.budgets import Blocked, Ledger, Limit, ledger
from forgeyard.gateway.keys import KeyCaller
from forgeyard.gateway.usage import recorder
from forgeyard.migrate import alembic_config, upgrade
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import FakeEngine, api_client, make_ready, new_key, set_state
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

PW = "quiet-meadow-4821"
P = pricing.PICOS_PER_MICRO
KEYS = MODELS + ("replica_keys",)


def chat(c):
    return c.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]})


def code(r) -> str:
    body = r.json()
    return body["detail"]["code"] if "detail" in body else body["error"]["code"]


def audit(c, prefix: str) -> list[dict]:
    return [e for e in c.get(f"/api/audit?action={prefix}&limit=200").json()["events"]]


def user_id(name: str) -> int:
    db = new_session()
    try:
        return db.scalar(select(User.id).where(User.username == name))
    finally:
        db.close()


def usage_rows() -> list[UsageDaily]:
    recorder.flush()
    db = new_session()
    try:
        return list(db.scalars(select(UsageDaily).order_by(UsageDaily.id)).all())
    finally:
        db.close()


@pytest.fixture()
def team(owner):
    """A deployment `chat` answering on a fake engine; people: mia (manager of team `ml`), max (member), ola
    (operator, in no team), uwe (user, in no team)."""
    seed_catalog()
    node = make_node("cpu-a", features=KEYS)
    engine = FakeEngine("a", "secret-a")
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    make_ready("chat", [(node, engine.port, engine.secret)])
    for name, role in (("mia", "user"), ("max", "user"), ("ola", "operator"), ("uwe", "user")):
        add_user(owner, name, role=role)
    r = owner.post("/api/teams", json={"name": "ml", "title": "Machine learning"})
    assert r.status_code == 201, r.text
    assert owner.put("/api/teams/ml/members/mia", json={"role": "manager"}).status_code == 200
    assert owner.put("/api/teams/ml/members/max", json={"role": "member"}).status_code == 200
    people = {n: sign_in(owner, n, PW) for n in ("mia", "max", "ola", "uwe")}
    dep = next(d["id"] for d in owner.get("/api/deployments").json()["deployments"] if d["name"] == "chat")
    ns = SimpleNamespace(owner=owner, engine=engine, node=node, dep=dep, team_id=r.json()["team"]["id"], **people)
    yield ns
    engine.close()


# ------------------------------------------------------------------ teams and members
def test_fy_team_001_only_admins_make_rename_and_delete_teams(team):
    """FY-TEAM-001: making, renaming (name and title), setting the budget of and deleting a team are for admins only:
    an operator, a manager and a user get 403; names follow the username rule (422 bad_team_name) and are unique
    (409 team_name_taken)."""
    for who in (team.ola, team.mia, team.uwe):
        assert who.post("/api/teams", json={"name": "ops"}).status_code == 403
        assert who.patch("/api/teams/ml", json={"title": "x"}).status_code == 403
        assert who.patch("/api/teams/ml", json={"budget_micros": 5}).status_code == 403
        assert who.delete("/api/teams/ml").status_code == 403
    assert code(team.owner.post("/api/teams", json={"name": "No Spaces"})) == "bad_team_name"
    assert code(team.owner.post("/api/teams", json={"name": "ml"})) == "team_name_taken"
    r = team.owner.patch("/api/teams/ml", json={"name": "ml-research", "title": "ML research", "budget_micros": 900})
    assert r.status_code == 200 and (r.json()["team"]["name"], r.json()["team"]["title"]) == ("ml-research", "ML research")
    assert r.json()["team"]["budget"]["budget_micros"] == 900
    assert team.owner.get("/api/teams/ml").status_code == 404
    [changed] = audit(team.owner, "team.changed")
    assert changed["detail"]["changes"]["name"] == {"before": "ml", "after": "ml-research"}
    assert [e["action"] for e in audit(team.owner, "team.created")] == ["team.created"]


def test_fy_team_002_who_sees_which_team(team):
    """FY-TEAM-002: everyone sees the teams they are in (My teams) with their role; admins see every team; someone
    outside a team gets 404 for it (they cannot learn it exists); members see the members but not the keys."""
    team.owner.post("/api/teams", json={"name": "ops"})
    assert [t["name"] for t in team.uwe.get("/api/teams").json()["teams"]] == []
    mine = team.max.get("/api/teams").json()["teams"]
    assert [(t["name"], t["my_role"], t["can_manage"]) for t in mine] == [("ml", "member", False)]
    assert {t["name"] for t in team.owner.get("/api/teams").json()["teams"]} == {"ml", "ops"}
    ops = team.ola.get("/api/teams").json()["teams"]      # an operator chooses a team when deploying
    assert {(t["name"], t["my_role"], t["can_manage"]) for t in ops} == {("ml", None, False), ("ops", None, False)}
    assert team.ola.get("/api/teams/ml").status_code == 404
    assert code(team.uwe.get("/api/teams/ml")) == "not_found" and team.max.get("/api/teams/ops").status_code == 404
    page = team.max.get("/api/teams/ml").json()["team"]
    assert [(m["username"], m["role"]) for m in page["member_list"]] == [("mia", "manager"), ("max", "member")]
    assert "key_list" not in page
    assert "key_list" in team.mia.get("/api/teams/ml").json()["team"]


def test_fy_team_003_managers_add_and_remove_members_admins_choose_managers(team):
    """FY-TEAM-003: a manager adds people as members and removes members; choosing managers, changing roles and
    removing a manager are for admins (403 admins_only); a member cannot change the team (403 not_team_manager); each
    change is audited (team.member_added / _changed / _removed)."""
    assert team.mia.put("/api/teams/ml/members/uwe", json={"role": "member"}).status_code == 200
    assert code(team.mia.put("/api/teams/ml/members/ola", json={"role": "manager"})) == "admins_only"
    assert code(team.mia.put("/api/teams/ml/members/max", json={"role": "manager"})) == "admins_only"
    assert code(team.mia.put("/api/teams/ml/members/nobody", json={"role": "member"})) == "no_such_user"
    assert code(team.max.put("/api/teams/ml/members/ola", json={"role": "member"})) == "not_team_manager"
    assert code(team.max.delete("/api/teams/ml/members/uwe")) == "not_team_manager"
    assert team.mia.delete("/api/teams/ml/members/uwe").status_code == 200
    assert team.owner.put("/api/teams/ml/members/max", json={"role": "manager"}).status_code == 200
    assert code(team.mia.delete("/api/teams/ml/members/max")) == "admins_only"
    assert team.owner.delete("/api/teams/ml/members/max").status_code == 200
    assert code(team.owner.delete("/api/teams/ml/members/max")) == "not_a_member"
    acts = [(e["action"], e["detail"].get("member")) for e in audit(team.owner, "team.member")]
    assert acts == [("team.member_removed", "max"), ("team.member_changed", "max"), ("team.member_removed", "uwe"),
                    ("team.member_added", "uwe"), ("team.member_added", "max"), ("team.member_added", "mia")]


# ------------------------------------------------------------------ team keys
def test_fy_team_004_team_keys_are_made_by_managers_and_admins(team):
    """FY-TEAM-004: a manager (and an admin) makes keys for the team: no person owns them, the team and the maker are
    recorded; a member gets 403 not_team_manager, someone outside the team 404 team_not_found; the team's managers see
    its keys on the API keys page, its members do not; the key answers /v1."""
    out = new_key(team.mia, "ml-service", team="ml")
    k = out["api_key"]
    assert k["team"]["name"] == "ml" and k["user"]["id"] is None and k["created_by"] == "mia"
    assert code(team.max.post("/api/keys", json={"name": "x", "team": "ml"})) == "not_team_manager"
    assert code(team.uwe.post("/api/keys", json={"name": "x", "team": "ml"})) == "team_not_found"
    assert code(team.ola.post("/api/keys", json={"name": "x", "team": "ml"})) == "team_not_found"
    assert team.owner.post("/api/keys", json={"name": "by-admin", "team": "ml"}).status_code == 201
    listed = team.mia.get("/api/keys").json()
    assert {x["name"] for x in listed["keys"]} == {"ml-service", "by-admin"}
    assert [t["name"] for t in listed["teams"]] == ["ml"]
    assert team.max.get("/api/keys").json()["keys"] == [] and team.max.get("/api/keys").json()["teams"] == []
    assert chat(api_client(team.owner, out["key"])).status_code == 200
    assert team.max.delete(f"/api/keys/{k['id']}").status_code == 404     # not a manager: not even its existence


def test_fy_team_005_a_team_key_outlives_its_maker_and_stops_when_revoked(team):
    """FY-TEAM-005: a team key keeps working when the person who made it is disabled and when their account is deleted
    (a personal key of a disabled person stops with 401 account_disabled, as before); it stops on the next request
    once a manager revokes it (401 revoked_api_key)."""
    team_key = new_key(team.mia, "ml-service", team="ml")
    own_key = new_key(team.mia, "mine")["key"]
    tc, oc = api_client(team.owner, team_key["key"]), api_client(team.owner, own_key)
    assert chat(tc).status_code == 200 and chat(oc).status_code == 200
    mia = user_id("mia")
    assert team.owner.patch(f"/api/users/{mia}", json={"status": "disabled"}).status_code == 200
    assert chat(tc).status_code == 200
    r = chat(oc)
    assert r.status_code == 401 and code(r) == "account_disabled"
    assert team.owner.delete(f"/api/users/{mia}").status_code == 200
    assert chat(tc).status_code == 200
    listed = [k for k in team.owner.get("/api/keys?all=true").json()["keys"] if k["name"] == "ml-service"]
    assert listed and listed[0]["created_by"] == "mia" and listed[0]["status"] == "active"
    assert team.owner.delete(f"/api/keys/{team_key['api_key']['id']}").status_code == 200
    r = chat(tc)
    assert r.status_code == 401 and code(r) == "revoked_api_key"


def test_fy_team_006_managers_act_on_team_keys_as_owners_and_admin_values_are_locked(team):
    """FY-TEAM-006: a manager renames and limits the team's keys like their own; a limit an admin set on a team they
    do not manage is locked for its managers (403 limit_locked)."""
    k = new_key(team.mia, "svc", team="ml")["api_key"]
    r = team.mia.patch(f"/api/keys/{k['id']}", json={"name": "svc-2", "rpm": 50})
    assert r.status_code == 200 and r.json()["api_key"]["name"] == "svc-2"
    assert team.owner.patch(f"/api/keys/{k['id']}", json={"rpm": 7}).json()["api_key"]["rpm_locked_by"] == "owner"
    assert code(team.mia.patch(f"/api/keys/{k['id']}", json={"rpm": 9})) == "limit_locked"
    made = team.owner.post("/api/keys", json={"name": "a", "team": "ml", "rpm": 3}).json()["api_key"]
    assert made["rpm_locked_by"] == "owner"


# ------------------------------------------------------------------ deleting a team
def test_fy_team_007_a_team_with_working_keys_or_deployments_is_not_deleted(team):
    """FY-TEAM-007: deleting a team whose keys still work answers 409 team_has_keys, one with deployments 409
    team_has_deployments; once its keys are revoked and its deployment moved, it goes, its revoked keys with it
    (they answer 401 like any unknown key), and its usage rows keep its id."""
    out = new_key(team.mia, "svc", team="ml")
    assert chat(api_client(team.owner, out["key"])).status_code == 200
    assert team.owner.patch(f"/api/deployments/{team.dep}", json={"team": "ml"}).status_code == 200
    r = team.owner.delete("/api/teams/ml")
    assert r.status_code == 409 and code(r) == "team_has_keys"
    team.mia.delete(f"/api/keys/{out['api_key']['id']}")
    r = team.owner.delete("/api/teams/ml")
    assert r.status_code == 409 and code(r) == "team_has_deployments"
    assert team.owner.patch(f"/api/deployments/{team.dep}", json={"team": None}).status_code == 200
    assert team.owner.delete("/api/teams/ml").status_code == 200
    assert team.mia.get("/api/teams").json()["teams"] == []
    assert code(chat(api_client(team.owner, out["key"]))) == "invalid_api_key"
    assert [r.team_id for r in usage_rows() if r.source == "api"] == [team.team_id]
    [deleted] = audit(team.owner, "team.deleted")
    assert deleted["detail"]["removed_keys"] == 1 and deleted["detail"]["members"] == 2


# ------------------------------------------------------------------ usage
def test_fy_team_008_usage_counts_to_the_keys_team(team):
    """FY-TEAM-008: a team key's requests count to its team and to no person; a personal key's and the Playground's to
    no team; ?team= shows the team's usage to admins and its managers (403 for members, 404 outside); the Team split
    names the team and "No team"."""
    tk = api_client(team.owner, new_key(team.mia, "svc", team="ml")["key"])
    pk = api_client(team.owner, new_key(team.mia, "mine")["key"])
    for c in (tk, tk, pk):
        assert chat(c).status_code == 200
    r = team.mia.post("/api/playground/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    rows = {(r.source, r.user_id, r.team_id): r.requests for r in usage_rows()}
    mia = user_id("mia")
    assert rows == {("api", 0, team.team_id): 2, ("api", mia, 0): 1, ("console", mia, 0): 1}
    u = team.mia.get("/api/usage?team=ml&group=key").json()
    assert u["total"]["requests"] == 2 and u["team"]["name"] == "ml" and [t["name"] for t in u["teams"]] == ["ml"]
    assert code(team.max.get("/api/usage?team=ml")) == "not_team_manager"
    assert code(team.uwe.get("/api/usage?team=ml")) == "team_not_found"
    split = {g["name"]: g["requests"] for g in team.owner.get("/api/usage?everyone=true&group=team").json()["rows"]}
    assert split == {"Machine learning": 2, "No team": 2}
    s = team.owner.get("/api/usage/series?everyone=true&split=team").json()
    assert {g["name"]: g["total"] for g in s["series"]} == {"Machine learning": 2, "No team": 2}
    assert team.mia.get("/api/usage/series?team=ml").json()["total"][-1] == 2
    # Her own usage is still hers only: the team key's requests are not in it.
    assert team.mia.get("/api/usage").json()["total"]["requests"] == 2


# ------------------------------------------------------------------ the team budget
def test_fy_team_009_the_team_budget_is_a_scope_of_the_ledger(owner):
    """FY-TEAM-009: a team key's cloud requests are reserved against its team's budget (and no person's), settled into
    it, blocked by it with the team named, and seeded from usage rows with the team's id."""
    caller = KeyCaller(source="api", user_id=0, username="", key_id=3, key_name="svc", team_id=7, team_name="ml",
                       team_budget_micros=100)
    lims = budgets.limits_for(caller, provider_id=1, provider="cloudy", provider_budget=None)
    assert [(lim.scope, lim.id, lim.micros) for lim in lims] == [("team", 7, 100)]
    assert budgets.limits_for(KeyCaller(source="api", user_id=2, username="bo", key_id=4), 1, "c", None) == []
    lg = Ledger()
    lg._month = budgets.month_of(time.time())
    h = lg.reserve(lims, 90 * P)
    lg.settle(h, key_id=3, user_id=0, provider_id=1, picos=90 * P, team_id=7)
    assert lg.snapshot().spent[("team", 7)] == 90 and ("person", 0) not in lg.snapshot().spent
    b = lg.reserve(lims, 20 * P)
    assert isinstance(b, Blocked) and b.limit.scope == "team"
    err = b.error()
    assert err.headers["X-Forgeyard-Budget-Exhausted"] == "team" and "the team ml" in err.message
    month = budgets.month_of(time.time())
    with get_engine().begin() as conn:
        conn.execute(text("INSERT INTO usage_daily (day, source, key_id, user_id, username, model, target, team_id, "
                          "requests, errors, limited, prompt_tokens, completion_tokens, latency_ms_sum, latency_buckets, "
                          f"cost_micros) VALUES ('{month}-01', 'api', 3, 0, '', 'm', 'pm:1', 7, 1, 0, 0, 1, 1, 1, '[]', 55)"))
    seeded = Ledger()
    assert seeded.snapshot().spent[("team", 7)] == 55


def test_fy_team_010_team_budget_alerts_and_the_budgets_page(team):
    """FY-TEAM-010: 80% and 100% of a team's budget are audited once each (scope team); the budgets page lists the
    teams a manager manages (admins every team) with this month's spend."""
    assert team.owner.patch("/api/teams/ml", json={"budget_micros": 100}).status_code == 200
    lim = Limit("team", team.team_id, 100, "ml")
    h = ledger.reserve([lim], 85 * P)
    ledger.settle(h, key_id=1, user_id=0, provider_id=1, picos=85 * P, team_id=team.team_id)
    assert ledger.write_alerts() == 1
    [e] = audit(team.owner, "budget.")
    assert e["action"] == "budget.warning" and e["detail"]["scope"] == "team" and e["detail"]["name"] == "ml"
    mine = team.mia.get("/api/budgets").json()["teams"]
    assert [(t["name"], t["budget_micros"], t["spent_micros"]) for t in mine] == [("ml", 100, 85)]
    assert team.max.get("/api/budgets").json()["teams"] == []
    assert [t["name"] for t in team.owner.get("/api/budgets?everyone=true").json()["teams"]] == ["ml"]


# ------------------------------------------------------------------ deployments
def test_fy_team_011_a_teams_managers_stop_start_and_change_its_deployments(team):
    """FY-TEAM-011: a deployment made for a team names it; the team's managers stop, start and change it (not its
    engine, its extra arguments or its team: 403 operators_only); its members and people outside get 403; admins and
    operators keep their rights on every deployment, and move it between teams."""
    assert code(team.ola.patch(f"/api/deployments/{team.dep}", json={"team": "nope"})) == "team_not_found"
    r = team.ola.patch(f"/api/deployments/{team.dep}", json={"team": "ml"})
    assert r.status_code == 200 and r.json()["deployment"]["team"]["name"] == "ml"
    assert r.json()["changes"]["team"] == {"before": None, "after": "ml"} and r.json()["restart"] == "none"
    for who in (team.max, team.uwe):
        assert code(who.post(f"/api/deployments/{team.dep}/stop")) == "forbidden"
        assert code(who.patch(f"/api/deployments/{team.dep}", json={"params": {"threads": 3}})) == "forbidden"
        assert who.get(f"/api/deployments/{team.dep}").json()["deployment"]["can_manage"] is False
    assert team.mia.get(f"/api/deployments/{team.dep}").json()["deployment"]["can_manage"] is True
    assert team.mia.post(f"/api/deployments/{team.dep}/stop").status_code == 200
    set_state("chat", 0, "stopped")       # its agent confirmed the stop
    assert team.mia.post(f"/api/deployments/{team.dep}/start").status_code == 200
    r = team.mia.patch(f"/api/deployments/{team.dep}", json={"params": {"threads": 3, "extra_args": None}, "wait": True})
    assert r.status_code == 200 and r.json()["deployment"]["params"]["threads"] == 3
    for body in ({"params": {"extra_args": ["--foo"]}}, {"recipe": "vllm"}, {"team": None}):
        assert code(team.mia.patch(f"/api/deployments/{team.dep}", json=body)) == "operators_only"
    assert team.mia.delete(f"/api/deployments/{team.dep}").status_code == 403
    assert team.ola.post(f"/api/deployments/{team.dep}/stop").status_code == 200
    created = deploy(team.ola, "emb", "qwen3-embedding-0.6b", "vllm-pooling", wait=True, team="ml")["deployment"]
    assert created["team"]["name"] == "ml"
    deploy(team.ola, "emb2", "qwen3-embedding-0.6b", "vllm-pooling", status=404, wait=True, team="nope")
    assert [e["detail"].get("team") for e in audit(team.owner, "deployment.created")][0] == "ml"


# ------------------------------------------------------------------ without teams: as before
def test_fy_team_012_without_teams_nothing_changes(owner):
    """FY-TEAM-012: an install with no team: keys are personal (no team) and answer as before, usage rows count to no
    team, a user's API keys page offers no team, and the team endpoints show nothing to a user."""
    seed_catalog()
    node = make_node("cpu-a", features=KEYS)
    engine = FakeEngine("a", "secret-a")
    try:
        deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
        make_ready("chat", [(node, engine.port, engine.secret)])
        add_user(owner, "uwe")
        u = sign_in(owner, "uwe", PW)
        k = new_key(u, "laptop")
        assert k["api_key"]["team"] is None and k["api_key"]["user"]["id"] == user_id("uwe")
        assert chat(api_client(owner, k["key"])).status_code == 200
        assert [r.team_id for r in usage_rows()] == [0]
        assert u.get("/api/keys").json()["teams"] == [] and u.get("/api/teams").json()["teams"] == []
        assert owner.get("/api/deployments").json()["deployments"][0]["team"] is None
    finally:
        engine.close()


def test_fy_team_013_the_migration_keeps_every_key_and_goes_down_and_up(team):
    """FY-TEAM-013: 0022 adds the tables and columns with every existing row unchanged; going down drops them and the
    team keys (an older server has no owner for them) and keeps every personal key working; up again restores the
    shape."""
    personal = new_key(team.mia, "mine")["key"]
    new_key(team.mia, "svc", team="ml")
    assert team.owner.patch(f"/api/deployments/{team.dep}", json={"team": None}).status_code == 200

    def shape():
        i = inspect(get_engine())
        return (set(i.get_table_names()), {c["name"] for c in i.get_columns("api_keys")},
                {c["name"] for c in i.get_columns("deployments")}, {c["name"] for c in i.get_columns("usage_daily")})

    before = shape()
    assert {"teams", "team_members"} <= before[0] and {"team_id", "created_by"} <= before[1]
    db = new_session()
    try:
        personal_ids = set(db.scalars(select(ApiKey.id).where(ApiKey.team_id.is_(None))).all())
    finally:
        db.close()
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0021_single_sign_on")
    tables, keys, deps, usage = shape()
    assert not {"teams", "team_members"} & tables and not {"team_id", "created_by"} & keys
    assert "team_id" not in deps and "team_id" not in usage
    with get_engine().connect() as conn:
        assert {r[0] for r in conn.execute(text("SELECT id FROM api_keys"))} == personal_ids
        assert conn.execute(text("SELECT count(*) FROM deployments")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM gateway_routes")).scalar() == 1
    upgrade()
    assert shape() == before
    assert chat(api_client(team.owner, personal)).status_code == 200
