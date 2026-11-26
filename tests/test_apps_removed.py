"""Apps removed in 1.5.3 (#89): 410 at the removed addresses, keys without /x/ access, migration 0019 (audit entries,
apps deleted, models untouched, columns dropped, down and up), the agent stopping a removed app, Setup health and
`forgeyard check-apps` (FY-APPRM-*)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import inspect, select, text

from forgeyard import removed
from forgeyard.db import ApiKey, AuditEvent, Deployment, Replica, Workload, WorkloadEvent, get_engine, new_session, utcnow
from forgeyard.migrate import alembic_config, upgrade as upgrade_to_head
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import api_client, new_key
from tests.machines_helpers import connect, joined
from tests.test_scheduler import deploy, fleet  # noqa: F401 (the fixture)
from tests.workload_helpers import container, converge, create_workload, task

BEFORE = "0018_engine_trials"
GONE_API = [("POST", "/api/workloads"), ("GET", "/api/workloads/templates"), ("PUT", "/api/workloads/1/gateway"),
            ("POST", "/api/workloads/1/stop"), ("POST", "/api/workloads/1/start"), ("DELETE", "/api/workloads/1"),
            ("POST", "/api/playground/vision/detector/detect")]


# Columns of later migrations that this code reads on every signed-in request (1.6.0, 0020: sessions.mfa_at; 0021:
# sessions.auth_method and fresh_until). These
# tests drive the 1.5.2 database through today's API, so the column stays while the database is at 0018, and goes again
# just before the upgrade adds it.
LATER_COLUMNS = (("sessions", "mfa_at", "TIMESTAMP"), ("sessions", "auth_method", "VARCHAR(16) NOT NULL DEFAULT 'password'"),
                 ("sessions", "fresh_until", "TIMESTAMP"))


def down_to_1_5_2() -> None:
    """The database as 1.5.2 had it (migration 0018): the app columns are back."""
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, BEFORE)
        for table, column, kind in LATER_COLUMNS:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {kind}"))


def upgrade() -> None:
    with get_engine().begin() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if version == BEFORE:
            for table, column, _ in LATER_COLUMNS:
                if column in {c["name"] for c in inspect(conn).get_columns(table)}:
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    upgrade_to_head()


def columns(table: str) -> set[str]:
    with get_engine().connect() as conn:
        return {c["name"] for c in inspect(conn).get_columns(table)}


def audit(action: str) -> list[dict]:
    db = new_session()
    try:
        return [{"target": e.target, "actor": e.actor_name, **e.detail}
                for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id))]
    finally:
        db.close()


def sql(statement: str, **params) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(statement), params)


def assert_gone(r, kind: str) -> dict:
    assert r.status_code == 410, (r.status_code, r.text)
    assert r.headers["deprecation"] == removed.DEPRECATION_HEADER == "@1794787200"
    assert r.headers["link"] == ('</guide/en/reference#apps-removed>; rel="deprecation"; type="text/html", '
                                 '<https://github.com/xianhong1208/forgeyard/releases/tag/v1.5.3>; rel="help"')
    return r.json()


def test_fy_apprm_001_the_removed_console_api_answers_410_with_what_to_use_instead(owner):
    """FY-APPRM-001: creating, exposing, stopping, starting and deleting an app, the templates and the console's route
    to exposed vision apps: 410 apps_removed, saying what replaces them, where to read more, and when it went."""
    for method, path in GONE_API:
        body = assert_gone(owner.request(method, path, json={"name": "web"} if method in ("POST", "PUT") else None),
                           "apps")["detail"]
        assert body["code"] == "apps_removed" and body["removed_in"] == "1.5.3", path
        assert body["see"] == "/guide/en/reference#apps-removed" and body["release_notes"].endswith("/v1.5.3"), path
        assert "removed in Forgeyard 1.5.3" in body["message"], path
    msgs = {p: owner.request(m, p).json()["detail"]["message"] for m, p in GONE_API}
    assert "deploy a model instead" in msgs["/api/workloads"]
    assert "vision model" in msgs["/api/workloads/templates"]
    assert "/v1/vision/<task>" in msgs["/api/playground/vision/detector/detect"]
    # A user's session gets the same answer (nothing to authorise any more), and so does nobody at all.
    add_user(owner, "pat")
    with sign_in(owner, "pat", "quiet-meadow-4821") as pat:
        assert_gone(pat.post("/api/workloads", json={}), "apps")
    assert_gone(api_client(owner).get("/api/workloads/templates"), "templates")


def test_fy_apprm_002_x_answers_410_in_the_gateways_shape_for_every_method_without_a_key(owner):
    """FY-APPRM-002: /x and /x/… (any method, with or without a key, no console header): 410 in OpenAI's error shape."""
    prog = api_client(owner)
    key = new_key(owner)["key"]
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        for path in ("/x", "/x/", "/x/notebook/", "/x/detector/v1/detect?threshold=0.5"):
            r = prog.request(method, path, headers={"Authorization": f"Bearer {key}"} if method == "POST" else {})
            err = assert_gone(r, "x")["error"]
            assert err["code"] == "apps_removed" and err["type"] == "invalid_request_error" and err["param"] is None
            assert "call a model deployment at /v1 instead" in err["message"] and err["removed_in"] == "1.5.3"
    assert prog.head("/x/notebook/").status_code == 410


def test_fy_apprm_003_a_deployments_workload_is_stopped_through_its_deployment_and_the_410_names_it(fleet):  # noqa: F811
    """FY-APPRM-003: stop, start and delete on a model deployment's workload: 410 naming the deployment; nothing changes."""
    c, a100, rtx = fleet
    dep = deploy(c, "embed", "qwen3-embedding-0.6b", "vllm-pooling")["deployment"]
    wid = dep["workload"]["id"]
    for method, verb in (("POST", "stop"), ("POST", "start"), ("DELETE", None)):
        r = c.request(method, f"/api/workloads/{wid}" + (f"/{verb}" if verb else ""))
        body = assert_gone(r, "apps")["detail"]
        assert body["deployment"] == {"id": dep["id"], "name": "embed"}
        assert "belongs to the deployment embed" in body["message"] and f"/api/deployments/{dep['id']}" in body["message"]
    got = c.get(f"/api/workloads/{wid}").json()["workload"]
    assert got["desired_state"] == "running" and got["deployment"]["name"] == "embed" and "gateway" not in got
    listed = c.get("/api/workloads").json()
    assert [w["name"] for w in listed["workloads"]] == [got["name"]] and "gateway" not in listed["workloads"][0]
    assert c.get(f"/api/workloads/{wid}/replicas/0/logs").status_code == 200   # still served: the console shows it


def test_fy_apprm_004_keys_no_longer_have_a_workloads_setting(owner):
    """FY-APPRM-004: none / null / [] are taken (an old form sending its default) and change nothing; asking for any
    /x/ access is 422 apps_removed; answers carry no workloads fields; an admin may still change someone's limits."""
    for extra in ({}, {"workloads": "none"}, {"workloads": None}, {"workloads": "none", "workload_list": []},
                  {"workload_list": None}):
        k = new_key(owner, f"k{len(extra)}{list(extra.values())}"[:60], **extra)["api_key"]
        assert not {"workloads", "workload_list", "workloads_locked_by"} & set(k)
    for extra in ({"workloads": "all"}, {"workloads": "listed", "workload_list": ["web"]}, {"workload_list": ["web"]}):
        r = owner.post("/api/keys", json={"name": "x", **extra})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "apps_removed", extra
        assert r.json()["detail"]["see"] == removed.GUIDE and "no longer have a workloads setting" in r.json()["detail"]["message"]
    kid = new_key(owner, "mine")["api_key"]["id"]
    assert owner.patch(f"/api/keys/{kid}", json={"workloads": "all"}).status_code == 422
    r = owner.patch(f"/api/keys/{kid}", json={"workloads": "none", "rpm": 7})
    assert r.status_code == 200 and r.json()["api_key"]["rpm"] == 7
    add_user(owner, "pat")
    with sign_in(owner, "pat", "quiet-meadow-4821") as pat:
        theirs = new_key(pat, "theirs")["api_key"]["id"]
    r = owner.patch(f"/api/keys/{theirs}", json={"rpm": 5, "workloads": None})
    assert r.status_code == 200 and r.json()["api_key"]["rpm_locked_by"] == "owner"
    assert owner.patch(f"/api/keys/{theirs}", json={"name": "renamed", "workloads": None}).status_code == 403
    for k in owner.get("/api/keys?all=true").json()["keys"]:
        assert "workloads" not in k


def _seed_1_5_2(c, a100: int, rtx: int) -> dict:
    """On a 1.5.2 database: two apps (an exposed container with a secret, a process task), a model deployment, keys
    with and without /x/ access. Returns their ids."""
    from forgeyard.workloads import placement

    down_to_1_5_2()
    assert "workloads" in columns("api_keys") and "gateway" in columns("workloads")
    model = deploy(c, "embed", "qwen3-embedding-0.6b", "vllm-pooling")["deployment"]
    web = create_workload(c, container("notebook", env={"TOKEN": "s3cret-value", "MODE": "lab"},
                                       resources={"gpus": {"count": 1}}, machine_id=rtx))["workload"]
    job = create_workload(c, task("nightly", machine_id=a100))["workload"]
    sql("UPDATE workloads SET gateway = :g WHERE id = :i", g=json.dumps({"expose": True, "port": None}), i=web["id"])
    listed = new_key(c, "listed-key")["api_key"]
    every = new_key(c, "all-key")["api_key"]
    plain = new_key(c, "plain-key")["api_key"]
    sql("UPDATE api_keys SET workloads = 'listed', workload_list = :l, workloads_locked_by = 'owner' WHERE public_id = :k",
        l=json.dumps(["notebook"]), k=listed["id"])
    sql("UPDATE api_keys SET workloads = 'all' WHERE public_id = :k", k=every["id"])
    # What 1.5.2 stored for a key without a list: a JSON null, not an SQL NULL.
    sql("UPDATE api_keys SET workload_list = :l WHERE public_id = :k", l="null", k=plain["id"])
    # A model workload no deployment points at (left behind by a crash): not an app, so not removed.
    orphan = create_workload(c, task("orphan"))["workload"]
    sql("UPDATE workloads SET runtime = 'model' WHERE id = :i", i=orphan["id"])
    db = new_session()
    try:
        placement.place_and_poke(db)
        reps = {w: [r.uid for r in db.scalars(select(Replica).where(Replica.workload_id == w))]
                for w in (web["id"], job["id"], model["workload"]["id"])}
    finally:
        db.close()
    return {"web": web["id"], "job": job["id"], "model": model, "reps": reps, "listed": listed, "every": every,
            "plain": plain, "orphan": orphan["id"]}


def test_fy_apprm_005_the_migration_records_and_removes_apps_and_keys_access_and_leaves_models_alone(fleet):  # noqa: F811
    """FY-APPRM-005: migration 0019 on a database with apps: one audit entry per app (what it was, where it ran,
    environment variables by name only) and per key with /x/ access, the apps with their replicas and events gone,
    the model deployment untouched, the columns dropped."""
    c, a100, rtx = fleet
    s = _seed_1_5_2(c, a100, rtx)
    # Never a deployment's workload, whatever its runtime says (here: made to look like an app).
    mid = s["model"]["workload"]["id"]
    sql("UPDATE workloads SET runtime = 'container' WHERE id = :i", i=mid)
    upgrade()
    sql("UPDATE workloads SET runtime = 'model' WHERE id = :i", i=mid)
    assert "workloads" not in columns("api_keys") and "workload_list" not in columns("api_keys")
    assert "workloads_locked_by" not in columns("api_keys") and "gateway" not in columns("workloads")
    db = new_session()
    try:
        assert db.get(Workload, s["web"]) is None and db.get(Workload, s["job"]) is None
        assert not db.scalars(select(Replica).where(Replica.uid.in_(s["reps"][s["web"]] + s["reps"][s["job"]]))).all()
        assert not db.scalars(select(WorkloadEvent).where(WorkloadEvent.workload_id.in_([s["web"], s["job"]]))).all()
        model_w = db.get(Workload, s["model"]["workload"]["id"])
        assert model_w is not None and model_w.desired_state == "running"
        assert [r.uid for r in model_w.replicas] == s["reps"][s["model"]["workload"]["id"]]
        assert db.get(Deployment, s["model"]["id"]) is not None
        assert db.get(Workload, s["orphan"]) is not None
        assert len(db.scalars(select(ApiKey)).all()) == 3
    finally:
        db.close()
    apps = {e["target"]: e for e in audit("workload.removed_in_upgrade")}
    assert set(apps) == {"notebook", "nightly"}
    nb, job = apps["notebook"], apps["nightly"]
    assert nb["actor"] == "forgeyard" and "removed in Forgeyard 1.5.3" in nb["reason"]
    assert nb["runtime"] == "container" and nb["image"] == "python:3.13-slim" and nb["kind"] == "service"
    assert nb["exposed_at"] == "/x/notebook" and nb["ports"] == "http" and nb["gpus"] == "1 GPU"
    assert nb["env_names"] == "MODE, TOKEN" and "s3cret" not in json.dumps(nb) and nb["machine"] == "rtx"
    assert nb["created_by"] == "owner" and nb["owner"] == "owner" and nb["placed"].startswith("0 on rtx (pending, GPU #0")
    assert job["runtime"] == "process" and job["command"] == "sh -c echo hi" and "exposed_at" not in job
    keys = {e["target"]: e for e in audit("api_key.x_access_removed")}
    assert set(keys) == {"listed-key", "all-key"}          # never a key that had none
    assert keys["listed-key"]["workloads"] == "listed" and keys["listed-key"]["workload_list"] == "notebook"
    assert keys["listed-key"]["locked_by"] == "owner" and keys["listed-key"]["prefix"] == s["listed"]["prefix"]
    assert keys["all-key"]["workloads"] == "all" and keys["all-key"]["owner"] == "owner"
    # The keys keep working for models, and the API still lists them.
    assert {k["name"] for k in c.get("/api/keys").json()["keys"]} == {"listed-key", "all-key", "plain-key"}
    assert {w["name"] for w in c.get("/api/workloads").json()["workloads"]} == {s["model"]["workload"]["name"], "orphan"}


def test_fy_apprm_006_down_brings_the_columns_back_not_the_apps_and_up_again_records_nothing_twice(fleet):  # noqa: F811
    """FY-APPRM-006: going down adds the four columns back (every key "none"); the apps stay gone (the way back is the
    backup); up again runs clean and writes no second audit entry."""
    c, a100, rtx = fleet
    s = _seed_1_5_2(c, a100, rtx)
    upgrade()
    down_to_1_5_2()
    assert {"workloads", "workload_list", "workloads_locked_by"} <= columns("api_keys") and "gateway" in columns("workloads")
    with get_engine().connect() as conn:
        assert {r[0] for r in conn.execute(text("SELECT workloads FROM api_keys"))} == {"none"}
        assert conn.execute(text("SELECT count(*) FROM workloads WHERE runtime <> 'model'")).scalar() == 0
    upgrade()
    assert len(audit("workload.removed_in_upgrade")) == 2 and len(audit("api_key.x_access_removed")) == 2
    assert c.get(f"/api/deployments/{s['model']['id']}").status_code == 200


def test_fy_apprm_007_the_agent_stops_a_removed_app_when_it_is_next_sent_its_work_list(owner, tmp_path):
    """FY-APPRM-007: an app running on a machine (a real process under the agent's supervisor), then the upgrade: on
    reconnecting, the machine's work list no longer has it, and the agent stops and forgets it (no forced kill)."""
    from tests.test_supervisor import make, reap_all, sleeper
    from tests.test_workload_stream import online, recv_type
    from tests.workload_helpers import alive

    j = joined(owner)
    down_to_1_5_2()
    sup, rt = make(tmp_path)

    async def feed(msg):
        sup.set_desired(msg)

    try:
        with connect(owner, j["credential"]) as ws:
            online(ws)
            recv_type(ws, "desired")
            create_workload(owner, sleeper("notebook", machine_id=j["node_id"]))
            msg = recv_type(ws, "desired")
        [rep] = msg["replicas"]

        async def running():
            await feed(msg)
            await converge(sup, lambda s: rep["id"] in s.records and s.records[rep["id"]].state == "running")
        asyncio.run(running())
        pid = sup.records[rep["id"]].pid
        assert alive(pid)

        upgrade()   # the server is upgraded (in a real upgrade it restarts, and agents reconnect)
        with connect(owner, j["credential"]) as ws:
            online(ws)
            again = recv_type(ws, "desired")
        assert again["replicas"] == [] and again["revision"] > 0

        async def gone():
            await feed(again)
            await converge(sup, lambda s: rep["id"] not in s.records, timeout=15)
        asyncio.run(gone())
        assert not alive(pid)
    finally:
        reap_all(rt)
    assert [e["target"] for e in audit("workload.removed_in_upgrade")] == ["notebook"]


def test_fy_apprm_008_setup_health_says_what_the_upgrade_removed_for_a_month(owner):
    """FY-APPRM-008: no finding without audit entries; an info finding naming the apps and keys while the entries are
    younger than 30 days (snoozable, on Machines, linking to the audit log; a revoked key is left to the audit log);
    none after; never apps-deprecated."""
    from forgeyard import advisor
    from forgeyard.config import get_settings

    def findings():
        db = new_session()
        try:
            return {f.id: f for f in advisor.run(db, get_settings(), strict=True)}
        finally:
            db.close()

    assert "apps-removed" not in findings() and "apps-deprecated" not in findings()
    db = new_session()
    try:
        for action, target in (("workload.removed_in_upgrade", "notebook"), ("workload.removed_in_upgrade", "nightly"),
                               ("api_key.x_access_removed", "ci-key"), ("workload.deleted", "other")):
            db.add(AuditEvent(action=action, actor_name="forgeyard", target=target, detail={}))
        db.add(AuditEvent(action="api_key.x_access_removed", actor_name="forgeyard", target="old-key",
                          detail={"revoked": True}))
        db.commit()
    finally:
        db.close()
    f = findings()["apps-removed"]
    assert f.severity == "info" and f.snoozable and f.pages == ("machines",) and f.fix_url == "/audit"
    assert f.title == "The 1.5.3 upgrade removed 2 apps" and f.fix_label == "Open the audit log"
    assert "2 apps (notebook, nightly)" in f.detail and "1 API key (ci-key)" in f.detail
    assert "workload.removed_in_upgrade" in f.detail and "other" not in f.detail and "old-key" not in f.detail
    sql("UPDATE audit_events SET at = :t WHERE action LIKE '%removed%'", t=utcnow() - timedelta(days=31))
    assert "apps-removed" not in findings()
    sql("UPDATE audit_events SET at = :t WHERE target = 'ci-key'", t=utcnow())
    only_keys = findings()["apps-removed"]
    assert only_keys.title == "The 1.5.3 upgrade removed /x/ access from 1 API key"


def test_fy_apprm_009_check_apps_lists_what_the_upgrade_will_remove_and_then_what_it_removed(fleet, capsys):  # noqa: F811
    """FY-APPRM-009: `forgeyard check-apps` on a 1.5.2 database lists the apps and keys (exit 1; --json too), changes
    nothing; on 1.5.3 it reads the audit log (exit 0); with nothing to remove it says so (exit 0)."""
    from forgeyard import apps_check
    from forgeyard.cli import main

    c, a100, rtx = fleet
    down_to_1_5_2()
    assert main(["check-apps"]) == 0
    assert "Nothing to remove" in capsys.readouterr().out
    upgrade()
    s = _seed_1_5_2(c, a100, rtx)
    assert main(["check-apps"]) == 1
    out = capsys.readouterr().out
    assert "Upgrading to Forgeyard 1.5.3 will remove" in out
    assert "app notebook (id" in out and "exposed at /x/notebook" in out and "container python:3.13-slim" in out
    assert "app nightly" in out and "process sh -c echo hi" in out and "embed" not in out
    assert "API key listed-key" in out and "/x/ access listed: notebook" in out and "plain-key" not in out
    assert main(["check-apps", "--json"]) == 1
    found = json.loads(capsys.readouterr().out)
    assert found["state"] == "pending" and [a["name"] for a in found["apps"]] == ["notebook", "nightly"]
    assert "s3cret" not in json.dumps(found)
    assert "workloads" in columns("api_keys")                     # it changed nothing
    db = new_session()                                            # closed before the upgrade: an open read holds a
    try:                                                          # lock that PostgreSQL's DROP COLUMN waits on forever
        assert db.get(Workload, s["web"]) is not None
    finally:
        db.close()
    upgrade()
    assert main(["check-apps"]) == 0
    out = capsys.readouterr().out
    assert "already on 1.5.3" in out and "app notebook: container python:3.13-slim" in out
    assert apps_check.inspect(get_engine())["state"] == "done"


def test_fy_apprm_010_check_apps_never_makes_a_database_where_there_is_none(tmp_path, monkeypatch, capsys):
    """FY-APPRM-010: pointed at a SQLite file that does not exist, it says where it looked and creates nothing."""
    from forgeyard.cli import main
    from forgeyard.config import get_settings
    from forgeyard.db import reset_engine

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "nowhere"))
    get_settings.cache_clear()
    reset_engine()
    assert main(["check-apps"]) == 0
    assert "No Forgeyard database here yet (looked at" in capsys.readouterr().out
    assert not list((tmp_path / "nowhere").glob("*.db"))


def test_fy_apprm_011_the_vision_options_no_longer_list_template_tasks(owner):
    """FY-APPRM-011: GET /api/playground/vision/options lists vision models only (the 1.3 templates' tasks are gone)."""
    assert owner.get("/api/playground/vision/options").json() == {"models": []}


@pytest.mark.parametrize("path", ["/api/workloads/templates", "/x/notebook/"])
def test_fy_apprm_012_removed_paths_are_not_in_the_openapi_schema(owner, path):
    """FY-APPRM-012: the 410 routes are not advertised."""
    paths = owner.get("/api/openapi.json").json()["paths"]
    assert path.rstrip("/") not in paths and "/x/{rest}" not in paths
    assert "post" not in paths.get("/api/workloads", {}) and "get" in paths["/api/workloads"]
