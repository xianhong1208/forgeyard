"""Stable ids (1.6.1, #190): the id of a deleted deployment, key, person, team or machine is never given to a new one,
so the rows that keep ids without a foreign key (GPU hours, usage, pinned machines) never mix the old and the new;
migration 0024 makes SQLite's tables work that way and starts each table above every id its history already holds
(FY-SID-*).

The bug: SQLite without AUTOINCREMENT gives a new row the highest id + 1, so deleting the newest row hands its id out
again (a lab server used deployment id 15 three times in one day, and its GPU hours were summed into one row).
PostgreSQL's sequences never reuse an id: the behaviour tests run there too, the migration tests only on SQLite."""

from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import select, text

from forgeyard.db import (ApiKey, AuditEvent, Base, BudgetAlert, Deployment, GpuUsageDaily, JoinToken, Replica,
                          UsageDaily, User, VideoJob, Workload, get_engine, new_session)
from forgeyard.gateway.usage import recorder
from forgeyard.migrate import alembic_config, upgrade
from forgeyard.workloads import placement
from tests.conftest import PG_URL, add_user, sign_in
from tests.gateway_helpers import api_client, new_key
from tests.test_gpu_hours import chat, clock, hold, rows, run  # noqa: F401 (fixtures)
from tests.test_scheduler import MODELS, deploy, fleet  # noqa: F401 (the fixture)
from tests.test_teams import team, usage_rows, user_id  # noqa: F401 (the fixture)
from tests.workload_helpers import make_node

PW = "quiet-meadow-4821"
sqlite_only = pytest.mark.skipif(bool(PG_URL), reason="migration 0024 changes SQLite only (a no-op on PostgreSQL)")

# The tables whose rows can be deleted while their id is kept somewhere without a foreign key.
STABLE = {"users", "teams", "api_keys", "nodes", "workloads", "models", "model_downloads", "deployments", "providers",
          "provider_models", "gateway_routes"}


def ask(c):
    return c.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]})


def key_id(made: dict) -> int:
    """The key's row id (the API answers its public id)."""
    db = new_session()
    try:
        return db.scalar(select(ApiKey.id).where(ApiKey.public_id == made["api_key"]["id"]))
    finally:
        db.close()


def migrate(target: str, how=command.downgrade) -> None:
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        how(cfg, target)


# ------------------------------------------------------------------ a new row never takes a deleted row's id


def test_fy_sid_001_a_new_deployment_never_takes_a_deleted_ones_id_or_gpu_hours(chat):  # noqa: F811
    """FY-SID-001: the newest deployment held a card for 60 s and is deleted; the next deployment (same name) gets
    another id, and its 30 s of GPU time is a row of its own: the deleted one's 60,000 GPU-ms stay apart."""
    c = chat.c
    run(chat.clock, 60)
    old = chat.id
    assert c.delete(f"/api/deployments/{old}").status_code == 200
    recorder.flush()
    new = deploy(c, "chat", "qwen3-4b", "vllm")["deployment"]["id"]
    hold("chat", cards=[4], share="exclusive")
    recorder.flush()
    run(chat.clock, 30)
    assert new != old
    assert sum(r.gpu_ms for r in rows(deployment_id=old)) == 60_000
    assert sum(r.gpu_ms for r in rows(deployment_id=new)) == 30_000
    body = c.get("/api/usage/gpu?group=deployment&days=30").json()
    assert sorted((x["name"], x["gpu_ms"]) for x in body["rows"]) == \
        sorted([("chat", 30_000), (f"deleted deployment #{old}", 60_000)])


def test_fy_sid_002_a_new_person_and_key_never_take_a_deleted_ones_id_or_usage(team):  # noqa: F811
    """FY-SID-002: uwe (the newest person) makes the newest key and sends 2 requests; deleting uwe deletes the key. The
    next person and their key get other ids, and their 1 request is counted apart from uwe's 2."""
    old_user = user_id("uwe")
    old_key = new_key(team.uwe, "uwe-laptop")
    old_kid = key_id(old_key)
    assert ask(api_client(team.owner, old_key["key"])).status_code == 200
    assert ask(api_client(team.owner, old_key["key"])).status_code == 200
    recorder.flush()
    assert team.owner.delete(f"/api/users/{old_user}").status_code == 200
    add_user(team.owner, "vic")
    vic = sign_in(team.owner, "vic", PW)
    new_user = user_id("vic")
    key = new_key(vic, "vic-laptop")
    assert ask(api_client(team.owner, key["key"])).status_code == 200
    assert new_user != old_user and key_id(key) != old_kid
    per_key = {}
    for r in usage_rows():
        per_key[(r.key_id, r.user_id)] = per_key.get((r.key_id, r.user_id), 0) + r.requests
    assert per_key[(old_kid, old_user)] == 2
    assert per_key[(key_id(key), new_user)] == 1


def test_fy_sid_003_a_new_team_never_takes_a_deleted_ones_id_or_usage(team):  # noqa: F811
    """FY-SID-003: the newest team `t2` sends 2 requests with its key and is deleted (its key revoked first); the next
    team `t3` gets another id, and its 1 request counts to it, not to t2."""
    assert team.owner.post("/api/teams", json={"name": "t2", "title": "Two"}).status_code == 201
    k2 = new_key(team.owner, "t2-svc", team="t2")
    k2_id = key_id(k2)
    assert ask(api_client(team.owner, k2["key"])).status_code == 200
    assert ask(api_client(team.owner, k2["key"])).status_code == 200
    old = team.owner.get("/api/teams/t2").json()["team"]["id"]
    assert team.owner.delete(f"/api/keys/{k2['api_key']['id']}").status_code == 200
    assert team.owner.delete("/api/teams/t2").status_code == 200
    r = team.owner.post("/api/teams", json={"name": "t3", "title": "Three"})
    assert r.status_code == 201
    new = r.json()["team"]["id"]
    k3 = new_key(team.owner, "t3-svc", team="t3")
    assert ask(api_client(team.owner, k3["key"])).status_code == 200
    assert new != old and key_id(k3) != k2_id
    per_team = {}
    for row in usage_rows():
        per_team[row.team_id] = per_team.get(row.team_id, 0) + row.requests
    assert per_team[old] == 2 and per_team[new] == 1


def test_fy_sid_004_a_deployment_pinned_to_a_removed_machine_does_not_follow_its_id(fleet):  # noqa: F811
    """FY-SID-004: a deployment pinned to the newest machine waits once that machine is removed; a machine that joins
    afterwards gets another id, and the deployment is not placed on it."""
    c, a100, rtx = fleet
    spare = make_node("spare", features=MODELS)
    deploy(c, "pinned", "qwen3-4b", "vllm", machine_id=spare)
    assert c.delete(f"/api/machines/{spare}").status_code == 200
    late = make_node("late", features=MODELS)
    db = new_session()
    try:
        placement.place_and_poke(db)
        db.commit()
        node_ids = set(db.scalars(select(Replica.node_id).join(Workload).where(Workload.name == "pinned")).all())
    finally:
        db.close()
    assert late != spare
    assert late not in node_ids


# ------------------------------------------------------------------ the migration (SQLite)


def _schema() -> dict:
    with get_engine().connect() as conn:
        tables = [r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'alembic_version' ORDER BY name")]
        out = {}
        for t in tables:
            out[t] = (conn.exec_driver_sql(f'PRAGMA table_xinfo("{t}")').fetchall(),
                      sorted(conn.exec_driver_sql(f'PRAGMA foreign_key_list("{t}")').fetchall()),
                      sorted((i[1], i[2], i[3], tuple(conn.exec_driver_sql(f'PRAGMA index_xinfo("{i[1]}")').fetchall()))
                             for i in conn.exec_driver_sql(f'PRAGMA index_list("{t}")')),
                      conn.exec_driver_sql(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall())
        return out


def _stored_sql() -> dict:
    with get_engine().connect() as conn:
        return {n: s for n, s in conn.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}


@sqlite_only
def test_fy_sid_005_the_migration_changes_only_how_ids_are_given(team):  # noqa: F811
    """FY-SID-005: going down and up keeps every table's columns, foreign keys (with their ON DELETE), indexes and rows
    exactly; after it, foreign keys are still enforced (deleting the deployment deletes its gateway route), the
    tables listed are AUTOINCREMENT on SQLite and no other is, the table text differs only in where the primary key is
    written, and every key keeps answering."""
    personal = new_key(team.mia, "mine")["key"]
    recorder.flush()
    up_sql = _stored_sql()
    assert {t for t, s in up_sql.items() if "AUTOINCREMENT" in (s or "")} == STABLE
    before = _schema()
    migrate("0023_gpu_usage")
    down_sql = _stored_sql()
    assert not any("AUTOINCREMENT" in (s or "") for s in down_sql.values())
    assert _schema() == {t: v for t, v in before.items()}

    def plain(sql: str, name: str) -> str:
        return (sql.replace(f'CREATE TABLE "{name}"', f"CREATE TABLE {name}").replace(" PRIMARY KEY AUTOINCREMENT", "")
                .replace("\n\tPRIMARY KEY (id), ", "").replace(", \n\tPRIMARY KEY (id)\n)", "\n)"))

    assert {t: plain(s, t) for t, s in up_sql.items() if s} == {t: plain(s, t) for t, s in down_sql.items() if s}
    upgrade()
    assert _schema() == before and _stored_sql() == up_sql
    migrate("0023_gpu_usage")
    assert _stored_sql() == down_sql            # down twice: the same text both times
    upgrade()
    with get_engine().connect() as conn:
        # The schema is still exactly what the models say (FY-APP-001's check): every index came back.
        assert compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata) == []
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    assert ask(api_client(team.owner, personal)).status_code == 200
    assert team.owner.delete(f"/api/deployments/{team.dep}").status_code == 200
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM gateway_routes")).scalar() == 0


H = 900  # an id above everything the test made


def _history(db, owner_id: int) -> dict:
    """One case per place that keeps an id without a foreign key: (table, write it with id H + n)."""
    day = "2026-11-01"
    month = "2026-11"
    now = datetime.now(timezone.utc)

    def usage(**kw):
        db.add(UsageDaily(day=day, source="api", model="m", latency_buckets=[], **kw))

    def audit(**detail):
        db.add(AuditEvent(action="test.history", detail=detail))

    def gpu(**kw):
        db.add(GpuUsageDaily(day=day, vendor="nvidia", gpu_model="x", gpu_ms=1, ram_mib_ms=0, busy_ms=0,
                             **{"deployment_id": 1, "team_id": 0, "node_id": 1, **kw}))

    def video(**kw):
        db.add(VideoJob(public_id=f"video_{kw}", source="api", model="v", replica_uid="u", replica_epoch="e",
                        upstream_id="x", status="failed", size="1x1", seconds=1, frames=1, fps=1, keep_s=1,
                        deadline_at=now, **kw))

    def alert(scope, sid):
        db.add(BudgetAlert(scope=scope, scope_id=sid, month=month, threshold=80))

    def snooze(scope):
        u = db.get(User, owner_id)
        prefs = dict(u.preferences or {})
        snoozed = dict(prefs.get("advisor_snoozed") or {})
        snoozed[f"some-finding@{scope}"] = (now + timedelta(days=30)).isoformat()
        prefs["advisor_snoozed"] = snoozed
        u.preferences = prefs

    def pinned(node_id):
        w = db.scalar(select(Workload))
        w.spec = {**w.spec, "machine_id": node_id}

    def token(node_id):
        db.add(JoinToken(id=f"{node_id:064d}", expires_at=now, node_id=node_id))

    return {
        "users: usage_daily.user_id": ("users", lambda i: usage(user_id=i)),
        "users: video_jobs.user_id": ("users", lambda i: video(user_id=i)),
        "users: audit actor_id": ("users", lambda i: db.add(AuditEvent(action="test.history", actor_id=i, detail={}))),
        "users: audit user_id": ("users", lambda i: audit(user_id=i)),
        "users: budget alert person": ("users", lambda i: alert("person", i)),
        "teams: usage_daily.team_id": ("teams", lambda i: usage(team_id=i, key_id=1)),
        "teams: gpu_usage_daily.team_id": ("teams", lambda i: gpu(team_id=i)),
        "teams: budget alert team": ("teams", lambda i: alert("team", i)),
        "teams: audit team_id": ("teams", lambda i: audit(team_id=i)),
        "api_keys: usage_daily.key_id": ("api_keys", lambda i: usage(key_id=i)),
        "api_keys: video_jobs.key_id": ("api_keys", lambda i: video(key_id=i)),
        "api_keys: budget alert key": ("api_keys", lambda i: alert("key", i)),
        "api_keys: snoozed key": ("api_keys", lambda i: snooze(f"key:{i}")),
        "nodes: gpu_usage_daily.node_id": ("nodes", lambda i: gpu(node_id=i)),
        "nodes: join_tokens.node_id": ("nodes", token),
        "nodes: a pinned workload": ("nodes", pinned),
        "nodes: audit machine_id": ("nodes", lambda i: audit(machine_id=i)),
        "nodes: audit from_machine_id": ("nodes", lambda i: audit(from_machine_id=i)),
        "nodes: snoozed machine": ("nodes", lambda i: snooze(f"machine:{i}")),
        "nodes: snoozed gpu": ("nodes", lambda i: snooze(f"gpu:{i}:3")),
        "workloads: audit workload_id": ("workloads", lambda i: audit(workload_id=i)),
        "models: audit model_id": ("models", lambda i: audit(model_id=i)),
        "model_downloads: audit download_id": ("model_downloads", lambda i: audit(download_id=i)),
        "deployments: usage_daily.deployment_id": ("deployments", lambda i: usage(deployment_id=i)),
        "deployments: gpu_usage_daily.deployment_id": ("deployments", lambda i: gpu(deployment_id=i)),
        "deployments: audit deployment_id": ("deployments", lambda i: audit(deployment_id=i)),
        "deployments: snoozed deployment": ("deployments", lambda i: snooze(f"deployment:{i}")),
        "providers: budget alert provider": ("providers", lambda i: alert("provider", i)),
        "providers: audit provider_id": ("providers", lambda i: audit(provider_id=i)),
        "providers: snoozed provider": ("providers", lambda i: snooze(f"provider:{i}")),
        "provider_models: usage_daily.target": ("provider_models", lambda i: usage(target=f"pm:{i}")),
        "provider_models: snoozed provider model": ("provider_models", lambda i: snooze(f"provider-model:{i}")),
        "gateway_routes: audit route_id": ("gateway_routes", lambda i: audit(route_id=i)),
        "gateway_routes: snoozed route": ("gateway_routes", lambda i: snooze(f"route:{i}")),
    }


@sqlite_only
def test_fy_sid_006_the_migration_starts_each_table_above_the_ids_its_history_holds(team):  # noqa: F811
    """FY-SID-006: before 0024, deleted rows' ids live on only in history: one id per place that keeps one (H + n,
    above every row the test made). After the upgrade each table gives out ids above the highest of them: the next
    deployment made through the API, and the next id SQLite hands out in every other table, is above them."""
    migrate("0023_gpu_usage")
    db = new_session()
    try:
        cases = _history(db, user_id("owner"))
        highest: dict[str, int] = {}
        for n, (table, write) in enumerate(cases.values()):
            write(H + n)
            highest[table] = max(highest.get(table, 0), H + n)
        db.commit()
    finally:
        db.close()
    assert set(highest) == STABLE
    upgrade()
    with get_engine().connect() as conn:
        seq = dict(conn.exec_driver_sql("SELECT name, seq FROM sqlite_sequence").fetchall())
    assert {t: seq.get(t, 0) >= highest[t] for t in STABLE} == {t: True for t in STABLE}
    made = deploy(team.owner, "late", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2})
    assert made["deployment"]["id"] > highest["deployments"]


@sqlite_only
@pytest.mark.parametrize("name", [
    "users: usage_daily.user_id", "users: video_jobs.user_id", "users: audit actor_id", "users: audit user_id",
    "users: budget alert person", "teams: usage_daily.team_id", "teams: gpu_usage_daily.team_id",
    "teams: budget alert team", "teams: audit team_id", "api_keys: usage_daily.key_id", "api_keys: video_jobs.key_id",
    "api_keys: budget alert key", "api_keys: snoozed key", "nodes: gpu_usage_daily.node_id",
    "nodes: join_tokens.node_id", "nodes: a pinned workload", "nodes: audit machine_id", "nodes: audit from_machine_id",
    "nodes: snoozed machine", "nodes: snoozed gpu", "workloads: audit workload_id", "models: audit model_id",
    "model_downloads: audit download_id", "deployments: usage_daily.deployment_id",
    "deployments: gpu_usage_daily.deployment_id", "deployments: audit deployment_id",
    "deployments: snoozed deployment", "providers: budget alert provider", "providers: audit provider_id",
    "providers: snoozed provider", "provider_models: usage_daily.target", "provider_models: snoozed provider model",
    "gateway_routes: audit route_id", "gateway_routes: snoozed route"])
def test_fy_sid_007_each_place_that_keeps_an_id_counts(team, name):  # noqa: F811
    """FY-SID-007: each place alone: an id kept only there (H) is not given out again after the upgrade."""
    migrate("0023_gpu_usage")
    db = new_session()
    try:
        table, write = _history(db, user_id("owner"))[name]
        write(H)
        db.commit()
    finally:
        db.close()
    upgrade()
    with get_engine().connect() as conn:
        seq = conn.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).scalar()
    assert seq is not None and seq >= H


def test_fy_sid_008_new_installs_make_the_tables_with_autoincrement():
    """FY-SID-008: the models say the same as the migration: the listed tables are AUTOINCREMENT on SQLite (and only
    they), so a database made from the models gives ids the same way."""
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import sqlite

    made = {t.name for t in Base.metadata.sorted_tables
            if "AUTOINCREMENT" in str(CreateTable(t).compile(dialect=sqlite.dialect()))}
    assert made == STABLE
    assert Deployment.__table__.dialect_options["sqlite"]["autoincrement"] is True


@sqlite_only
def test_fy_sid_009_a_reference_broken_before_the_upgrade_does_not_stop_it(team):  # noqa: F811
    """FY-SID-009: a database with a reference that was already broken (written once without foreign keys: a member
    of a team that is gone) still upgrades, and the row stays as it was."""
    migrate("0023_gpu_usage")
    with get_engine().connect() as conn:
        raw = conn.connection.dbapi_connection
        raw.commit()
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("INSERT INTO team_members (team_id, user_id, role, added_at) VALUES (777, 1, 'member', '2026-11-01')")
        raw.commit()
        raw.execute("PRAGMA foreign_keys=ON")
    upgrade()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM team_members WHERE team_id = 777").scalar() == 1
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0024_stable_ids"


@sqlite_only
def test_fy_sid_010_a_rebuild_that_breaks_a_reference_is_rolled_back(team):  # noqa: F811
    """FY-SID-010: the rebuild's own check: a reference broken inside it (a member of a team that does not exist) makes
    it fail, nothing of it is kept, and foreign keys are on again afterwards."""
    import importlib.util
    from pathlib import Path

    import forgeyard

    path = Path(forgeyard.__file__).parent / "migrations" / "versions" / "0024_stable_ids.py"
    spec = importlib.util.spec_from_file_location("m0024", path)
    m0024 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m0024)
    with get_engine().connect() as conn:
        with pytest.raises(RuntimeError, match="broke references"):
            with m0024._foreign_keys_off(conn) as raw:
                raw.execute("INSERT INTO team_members (team_id, user_id, role, added_at) "
                            "VALUES (778, 1, 'member', '2026-11-01')")
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("SELECT count(*) FROM team_members WHERE team_id = 778").scalar() == 0
