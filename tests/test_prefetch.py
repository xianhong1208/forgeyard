""""Download to…" on the server (1.4, issue #35; FY-PF-010…021): migration 0015, the dialog's machines
(fits / already there / offline / agent too old / no space), starting and cancelling (operators and admins,
audited), `desired.downloads` with the engine of the variant each machine would run, the agent's reports,
placement preferring the machine that has the model, the URL model's SHA-256 (D8), `fetch_via` (admins),
older agents, history, and the whole loop with a real agent."""

import asyncio
import hashlib
import io
import json
import os
import tarfile
from datetime import timedelta

from alembic import command
from sqlalchemy import select, text

from forgeyard.db import AuditEvent, CatalogModel, ModelDownload, Node, get_engine, new_session, utcnow
from forgeyard.machines import agent_api
from forgeyard.machines.messages import StatusIn
from forgeyard.migrate import alembic_config, upgrade
from forgeyard.models import prefetch, reach
from forgeyard.models.catalog import model_block
from tests.conftest import add_user, sign_in
from tests.machines_helpers import connect, hello, joined
from tests.model_helpers import GGUF_REV, add_catalog
from tests.test_scheduler import deploy, reps, seed_catalog
from tests.test_workload_stream import recv, recv_type
from tests.waits import wait_until

FEATURES = ["workloads", "models", "cache_admin", "prefetch"]
PROCESS = {"process": {"ok": True, "user": "nobody", "uid": 65534, "limits": "rlimit"},
           "container": {"ok": False, "reason": "docker not found"}}
DOCKER = {"process": {"ok": True, "user": "nobody", "uid": 65534, "limits": "rlimit"},
          "container": {"ok": True, "engine": "docker", "nvidia": False}}
ENGINE_SHA = "0bc1f091e8916102b6d8f2cf944233a620e90a0f856c72d0ce41150c65c2e72e"
MODEL_FILE = "Qwen3-1.7B-Q4_K_M.gguf"
MODEL_KEY = f"hub/models--ggml-org--Qwen3-1.7B-GGUF@{GGUF_REV}/{MODEL_FILE}"
GIB = 1024**3


def cache(**over) -> dict:
    out = {"ok": True, "free_bytes": 50 * GIB, "total_bytes": 100 * GIB, "cached": [], "models": [],
           "engines_detail": [], "used_bytes": 0, "partial_bytes": 0, "partial_files": 0}
    out.update(over)
    return out


def online(ws, features=FEATURES, runtime_info=PROCESS, model_cache=None, fixture="nvidia-rtx4090x2") -> dict:
    """Say hello as an agent; the first desired list it gets."""
    ws.send_json(hello(fixture, features=list(features), runtimes=[k for k, v in runtime_info.items() if v.get("ok")],
                       runtime_info=runtime_info, model_cache=cache() if model_cache is None else model_cache))
    assert recv(ws)["type"] == "welcome"
    return recv_type(ws, "desired")


def gguf_id() -> int:
    seed_catalog()
    db = new_session()
    try:
        return db.scalar(select(CatalogModel.id).where(CatalogModel.name == "qwen3-1.7b-gguf"))
    finally:
        db.close()


def by_name(targets: dict) -> dict:
    return {m["name"]: m for m in targets["machines"]}


def rows() -> list[ModelDownload]:
    db = new_session()
    try:
        return list(db.scalars(select(ModelDownload).order_by(ModelDownload.id)).all())
    finally:
        db.close()


def actions() -> list[str]:
    db = new_session()
    try:
        return [e.action for e in db.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()]
    finally:
        db.close()


def status(ws, *downloads, model_cache=None):
    msg = {"v": 1, "type": "status", "replicas": [], "downloads": list(downloads)}
    if model_cache is not None:
        msg["model_cache"] = model_cache
    ws.send_json(msg)


# ------------------------------------------------------------------ the migration
def test_fy_pf_010_the_migration_adds_the_table_and_fetch_via_auto(owner):
    """FY-PF-010: a machine from before 0015 comes through the upgrade with fetch_via "auto"; down drops the
    table and the column without touching the machine; up again adds them back (SQLite and PostgreSQL)."""
    def columns(table) -> set:
        with get_engine().connect() as conn:
            if conn.dialect.name == "sqlite":
                return {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            return {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns "
                                                    f"WHERE table_name = '{table}'"))}

    j = joined(owner, "old-box")
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0014_video_jobs")
    assert "fetch_via" not in columns("nodes") and columns("model_downloads") == set()
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT name FROM nodes WHERE id = :i"), {"i": j["node_id"]}).scalar() == "old-box"
    upgrade()
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT fetch_via FROM nodes WHERE id = :i"), {"i": j["node_id"]}).scalar() == "auto"
    assert {"uid", "model_id", "node_id", "file", "recipe", "state", "via", "source_node_id", "download", "reason",
            "requested_by", "created_at", "finished_at"} <= columns("model_downloads")


# ------------------------------------------------------------------ the dialog
def test_fy_pf_011_each_machine_says_whether_it_can_take_the_download(owner):
    """FY-PF-011: ready (with the engine archive of the variant it would run, or the image on a machine with
    Docker), already there, agent too old, no model cache, offline, not enough disk; the engine left out
    when it is not wanted; a local model has nothing to download."""
    mid = gguf_id()
    names = ("ready", "docker", "there", "old", "nocache", "small")
    nodes = {n: joined(owner, n) for n in names + ("away",)}
    with connect(owner, nodes["away"]["credential"]) as away:
        online(away)  # then gone
    with connect(owner, nodes["ready"]["credential"]) as ready, \
            connect(owner, nodes["docker"]["credential"]) as docker, \
            connect(owner, nodes["there"]["credential"]) as there, \
            connect(owner, nodes["old"]["credential"]) as old, \
            connect(owner, nodes["nocache"]["credential"]) as nocache, \
            connect(owner, nodes["small"]["credential"]) as small:
        online(ready)
        online(docker, runtime_info=DOCKER)
        online(there, model_cache=cache(cached=[MODEL_KEY], engines=[ENGINE_SHA[:16]]))
        online(old, features=["workloads", "models", "cache_admin"])
        online(nocache, features=["workloads"])
        online(small, model_cache=cache(free_bytes=GIB))
        r = owner.get(f"/api/downloads/targets?model_id={mid}")
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["recipe"] == "llama-cpp" and "llama-cpp" in t["recipes"] and t["can_download"]
        m = by_name(t)
        assert m["ready"]["state"] == "ok" and m["ready"]["variant"] == "cpu-process"
        assert m["ready"]["engine"] == {"kind": "archive", "name": "llama.cpp-1.0.0", "cached": False}
        assert m["ready"]["need_bytes"] == 1282439264 and m["ready"]["free_bytes"] == 50 * GIB
        assert m["docker"]["state"] == "ok" and m["docker"]["engine"]["kind"] == "image"
        assert m["docker"]["engine"]["name"].startswith("ghcr.io/ggml-org/llama.cpp:server-v1.0.0")
        assert m["there"]["state"] == "cached" and m["there"]["note"] == "Already here with the engine."
        assert m["old"]["state"] == "too_old" and "update the agent" in m["old"]["note"]
        assert m["nocache"]["state"] == "no_cache"
        assert m["small"]["state"] == "no_space" and "needs 1.2 GiB (+1 GiB margin), 1.0 GiB free" in m["small"]["note"]
        assert m["away"]["state"] == "offline"
        bare = by_name(owner.get(f"/api/downloads/targets?model_id={mid}&engine=false").json())
        assert bare["ready"]["engine"] is None and bare["there"]["note"] == "Already here."
        bad = owner.get(f"/api/downloads/targets?model_id={mid}&recipe=vllm")
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "recipe_not_compatible"
    local = add_catalog("here", {"kind": "local", "path": "/srv/m", "format": "gguf"}, task="chat", fmt="gguf",
                        source="local:///srv/m", kind="local")
    r = owner.get(f"/api/downloads/targets?model_id={local}")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "local_model"


# ------------------------------------------------------------------ asking for it, and the protocol
def test_fy_pf_012_operators_start_it_and_the_agent_is_told(owner):
    """FY-PF-012: an operator starts it for the machines that can take it (the others are listed with why),
    a user may not, nothing to start is 409; each one is audited; the agent's desired list carries the model
    block (with the server's Hugging Face token) and the engine archive; an agent without "prefetch" gets no
    downloads in its list; asking again while one runs starts nothing new."""
    from forgeyard.models import hf_token

    mid = gguf_id()
    db = new_session()
    hf_token.put(db, "hf_server_token_1234", "owner")
    db.commit()
    db.close()
    add_user(owner, "op", role="operator")
    add_user(owner, "viewer", role="user")
    op = sign_in(owner, "op", "quiet-meadow-4821")
    viewer = sign_in(owner, "viewer", "quiet-meadow-4821")
    a, old = joined(owner, "cpu-a"), joined(owner, "cpu-old")
    with connect(owner, a["credential"]) as ws, connect(owner, old["credential"]) as ows:
        assert "downloads" in online(ws) and online(ows, features=["workloads", "models"]).get("downloads") is None
        body = {"model_id": mid, "machine_ids": [a["node_id"], old["node_id"]]}
        assert viewer.post("/api/downloads", json=body).status_code == 403
        r = op.post("/api/downloads", json=body)
        assert r.status_code == 201, r.text
        out = r.json()
        assert [d["machine"]["name"] for d in out["downloads"]] == ["cpu-a"]
        assert out["downloads"][0]["state"] == "queued" and out["downloads"][0]["recipe"] == "llama-cpp"
        assert out["skipped"] == [{"machine_id": old["node_id"], "name": "cpu-old", "state": "too_old",
                                   "reason": "cpu-old's agent cannot download ahead: update the agent."}]
        desired = recv_type(ws, "desired")
        [item] = desired["downloads"]
        row = rows()[0]
        assert item["id"] == row.uid and row.requested_by == "op"
        assert item["model"]["repo"] == "ggml-org/Qwen3-1.7B-GGUF" and item["model"]["file"] == MODEL_FILE
        assert item["model"]["token"] == "hf_server_token_1234"
        assert item["engine"]["sha256"] == ENGINE_SHA and item["engine"]["name"] == "llama.cpp-1.0.0"
        assert "image" not in item
        again = op.post("/api/downloads", json={"model_id": mid, "machine_ids": [a["node_id"]]})
        assert again.status_code == 409 and again.json()["detail"]["code"] == "nothing_to_download"
        assert again.json()["detail"]["skipped"][0]["state"] == "downloading"
        assert len(rows()) == 1
        assert actions().count("model.download_requested") == 1
        token_free = json.dumps(owner.get("/api/downloads").json())
        assert "hf_server_token_1234" not in token_free


def test_fy_pf_013_the_agents_reports_move_it_along(owner):
    """FY-PF-013: progress is kept; "done" ends it (finished, and it leaves the next desired list); another
    machine cannot report on it; a container variant's machine is sent the image with its recorded ids."""
    mid = gguf_id()
    a, b = joined(owner, "cpu-a"), joined(owner, "dock")
    with connect(owner, a["credential"]) as ws, connect(owner, b["credential"]) as other:
        online(ws)
        online(other, runtime_info=DOCKER, features=FEATURES + ["image_ids"])
        owner.post("/api/downloads", json={"model_id": mid, "machine_ids": [a["node_id"], b["node_id"]]})
        mine = recv_type(ws, "desired")["downloads"][0]["id"]
        theirs = recv_type(other, "desired")["downloads"][0]
        assert theirs["image"].startswith("ghcr.io/ggml-org/llama.cpp:server-v1.0.0") and "engine" not in theirs
        assert theirs["image_ids"]["ids"]
        status(other, {"id": mine, "state": "failed", "reason": "not mine"})
        status(ws, {"id": mine, "state": "downloading", "reason": "downloading the model: 0.50 of 1.19 GiB (41%)",
                    "download": {"phase": "model", "done": 5 * 10**8, "total": 1282439264, "file": MODEL_FILE}})
        wait_until(lambda: next(r for r in rows() if r.uid == mine).download, "the progress")
        row = next(r for r in rows() if r.uid == mine)
        assert row.state == "downloading" and row.download["done"] == 5 * 10**8 and row.finished_at is None
        listed = owner.get(f"/api/downloads?machine_id={a['node_id']}").json()["downloads"]
        assert listed[0]["download"]["total"] == 1282439264 and listed[0]["machine"]["connected"]
        status(ws, {"id": mine, "state": "done"}, model_cache=cache(cached=[MODEL_KEY], engines=[ENGINE_SHA[:16]]))
        assert recv_type(ws, "desired")["downloads"] == []
        row = next(r for r in rows() if r.uid == mine)
        assert row.state == "done" and row.finished_at is not None and row.download is None and row.reason is None
        t = by_name(owner.get(f"/api/downloads/targets?model_id={mid}").json())
        assert t["cpu-a"]["state"] == "cached"
        status(other, {"id": theirs["id"], "state": "done"})
        wait_until(lambda: next(r for r in rows() if r.uid == theirs["id"]).state == "done", "the image")
        assert reach.has_image(Node(id=b["node_id"]), theirs["image"])


def test_fy_pf_014_cancel_ends_it_on_the_agent(owner):
    """FY-PF-014: an operator cancels a download: it is recorded and audited, the agent's next list no longer
    has it, a late report from the agent changes nothing, and cancelling it again is 409."""
    mid = gguf_id()
    a = joined(owner, "cpu-a")
    with connect(owner, a["credential"]) as ws:
        online(ws)
        d = owner.post("/api/downloads", json={"model_id": mid, "machine_ids": [a["node_id"]]}).json()["downloads"][0]
        uid = recv_type(ws, "desired")["downloads"][0]["id"]
        r = owner.post(f"/api/downloads/{d['id']}/cancel")
        assert r.status_code == 200 and r.json()["download"]["state"] == "cancelled"
        assert r.json()["download"]["reason"] == "cancelled by owner"
        assert recv_type(ws, "desired")["downloads"] == []
        status(ws, {"id": uid, "state": "done"})
        owner.get("/api/machines")  # a request after the report: it has been handled
        assert rows()[0].state == "cancelled"
        again = owner.post(f"/api/downloads/{d['id']}/cancel")
        assert again.status_code == 409 and again.json()["detail"]["code"] == "download_finished"
        assert owner.post("/api/downloads/999/cancel").status_code == 404
        assert "model.download_cancelled" in actions()
        # Asked again after a cancel: a new download (the agent resumes from what is on disk).
        assert owner.post("/api/downloads", json={"model_id": mid, "machine_ids": [a["node_id"]]}).status_code == 201


def test_fy_pf_015_placement_prefers_the_machine_that_downloaded_it(owner):
    """FY-PF-015: cpu-a would win by name; once a download finished on cpu-b (its agent then lists the model
    and the engine), a deployment goes to cpu-b and needs nothing fetched there."""
    mid = gguf_id()
    a, b = joined(owner, "cpu-a"), joined(owner, "cpu-b")
    with connect(owner, a["credential"]) as wa, connect(owner, b["credential"]) as wb:
        online(wa)
        online(wb)
        owner.post("/api/downloads", json={"model_id": mid, "machine_ids": [b["node_id"]]})
        uid = recv_type(wb, "desired")["downloads"][0]["id"]
        status(wb, {"id": uid, "state": "done"}, model_cache=cache(cached=[MODEL_KEY], engines=[ENGINE_SHA[:16]]))
        recv_type(wb, "desired")
        d = deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
        [r] = reps(owner, d)
        assert r["machine"]["name"] == "cpu-b"
        placed = {m["name"]: m for m in d["placement"]["machines"]}
        assert placed["cpu-b"]["cached"] and placed["cpu-b"]["needs"] == []
        assert placed["cpu-a"]["needs"] == ["model", "engine"]


def test_fy_pf_016_a_url_models_first_sha256_is_recorded(owner):
    """FY-PF-016 (D8): the first SHA-256 an agent reports for a URL model goes into the catalog entry, with
    the machine and when; every later model block carries it; a different one reported later never replaces
    it. Audited."""
    url = "https://files.example.org/tiny.gguf"
    mid = add_catalog("tiny-url", {"kind": "url", "url": url, "format": "gguf",
                                   "files": [{"path": "tiny.gguf", "size": 100}]},
                      task="chat", fmt="gguf", source=url, kind="url")
    from forgeyard.models.sources import url_dir

    key = url_dir(url)
    sha = "ab" * 32
    a, b = joined(owner, "cpu-a"), joined(owner, "cpu-b")
    with connect(owner, a["credential"]) as wa, connect(owner, b["credential"]) as wb:
        online(wa)
        online(wb)
        entry = {"key": key, "kind": "url", "file": "tiny.gguf", "bytes": 100, "files": 1, "sha256": sha}
        status(wa, model_cache=cache(cached=[key], models=[entry]))
        db = new_session()
        try:
            m = wait_until(lambda: (db.expire_all(), db.get(CatalogModel, mid))[1].meta["files"][0].get("sha256"),
                           "the recorded hash")
            meta = db.get(CatalogModel, mid).meta["files"][0]
            assert m == sha and meta["sha256_from"] == "cpu-a" and meta["sha256_at"]
            assert model_block(db.get(CatalogModel, mid))["files"] == [{"path": "tiny.gguf", "size": 100, "sha256": sha}]
        finally:
            db.close()
        status(wb, model_cache=cache(cached=[key], models=[{**entry, "sha256": "cd" * 32}]))
        owner.get("/api/machines")
        db = new_session()
        try:
            assert db.get(CatalogModel, mid).meta["files"][0]["sha256"] == sha
        finally:
            db.close()
    assert actions().count("model.checksum_recorded") == 1
    bad = StatusIn.model_validate({"v": 1, "type": "status", "model_cache": cache(models=[{**entry, "sha256": "x"}])})
    assert "sha256" not in bad.model_cache["models"][0]


def test_fy_pf_017_only_admins_set_how_a_machine_reaches_the_internet(owner):
    """FY-PF-017 (D3, D9): fetch_via is auto for a new machine; an admin sets server or direct (audited);
    an operator may not; anything else is refused."""
    a = joined(owner, "cpu-a")
    add_user(owner, "op", role="operator")
    op = sign_in(owner, "op", "quiet-meadow-4821")
    m = owner.get("/api/machines").json()["machines"][0]
    assert m["fetch_via"] == "auto"
    assert op.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "server"}).status_code == 403
    bad = owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "peer"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_fetch_via"
    r = owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "server"})
    assert r.status_code == 200 and r.json()["machine"]["fetch_via"] == "server"
    assert owner.put("/api/machines/999/fetch-via", json={"fetch_via": "direct"}).status_code == 404
    db = new_session()
    try:
        e = db.scalar(select(AuditEvent).where(AuditEvent.action == "node.fetch_via_changed"))
        assert e.detail == {"machine_id": a["node_id"], "before": "auto", "after": "server"} and e.target == "cpu-a"
    finally:
        db.close()


def test_fy_pf_018_an_older_agent_or_a_revoked_machine_ends_its_downloads(owner):
    """FY-PF-018: a machine whose agent comes back without "prefetch" (an older one) fails what it was asked,
    saying to update the agent; revoking a machine fails its downloads too."""
    mid = gguf_id()
    a, b = joined(owner, "cpu-a"), joined(owner, "cpu-b")
    with connect(owner, a["credential"]) as wa, connect(owner, b["credential"]) as wb:
        online(wa)
        online(wb)
        owner.post("/api/downloads", json={"model_id": mid, "machine_ids": [a["node_id"], b["node_id"]]})
    with connect(owner, a["credential"]) as wa:
        online(wa, features=["workloads", "models"])
    owner.post(f"/api/machines/{b['node_id']}/revoke")
    by = {r.node_id: r for r in rows()}
    assert by[a["node_id"]].state == "failed" and "cannot download ahead: update the agent" in by[a["node_id"]].reason
    assert by[b["node_id"]].state == "failed" and by[b["node_id"]].reason == "cpu-b was revoked"


def test_fy_pf_019_history_is_kept_30_days_and_20_per_machine(owner):
    """FY-PF-019: finished downloads older than 30 days go, and all but each machine's last 20; ones under way
    always stay."""
    mid = gguf_id()
    a = joined(owner, "cpu-a")
    db = new_session()
    try:
        now = utcnow()
        db.add(ModelDownload(model_id=mid, node_id=a["node_id"], state="done", requested_by="x",
                             created_at=now, finished_at=now - timedelta(days=31)))
        for i in range(25):
            db.add(ModelDownload(model_id=mid, node_id=a["node_id"], state="failed", requested_by="x",
                                 created_at=now, finished_at=now - timedelta(minutes=i)))
        db.add(ModelDownload(model_id=mid, node_id=a["node_id"], state="queued", requested_by="x", created_at=now))
        db.commit()
        prefetch.prune(db)
        db.commit()
        left = list(db.scalars(select(ModelDownload)).all())
        assert sum(r.state == "queued" for r in left) == 1
        assert sum(r.state in prefetch.FINAL for r in left) == prefetch.KEEP_PER_MACHINE == 20
        assert all(r.state != "done" for r in left)
    finally:
        db.close()


def test_fy_pf_020_reports_for_another_machine_are_ignored_by_the_handler(owner):
    """FY-PF-020: the server's status handler applies a download report only to the reporting machine's rows
    that are still under way."""
    mid = gguf_id()
    a, b = joined(owner, "cpu-a"), joined(owner, "cpu-b")
    db = new_session()
    try:
        row = ModelDownload(model_id=mid, node_id=a["node_id"], state="queued", requested_by="x", created_at=utcnow())
        db.add(row)
        db.commit()
        uid = row.uid
    finally:
        db.close()
    db = new_session()
    try:
        msg = StatusIn.model_validate({"v": 1, "type": "status", "downloads": [{"id": uid, "state": "done"}]})
        assert agent_api._status(db, b["node_id"], msg) is False
        assert agent_api._status(db, a["node_id"], msg) is True
    finally:
        db.close()
    assert rows()[0].state == "done"


def test_fy_pf_022_removing_the_model_from_the_catalog_ends_its_downloads(owner):
    """FY-PF-022: a catalog model removed while it downloads takes its downloads with it, and the agent's next
    list no longer has them."""
    mid = gguf_id()
    a = joined(owner, "cpu-a")
    with connect(owner, a["credential"]) as ws:
        online(ws)
        owner.post("/api/downloads", json={"model_id": mid, "machine_ids": [a["node_id"]]})
        assert len(recv_type(ws, "desired")["downloads"]) == 1
        assert owner.delete(f"/api/models/{mid}").status_code == 200
        assert recv_type(ws, "desired")["downloads"] == []
    assert rows() == []


# ------------------------------------------------------------------ the whole loop
def _tgz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("eng/server")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_fy_pf_021_a_real_agent_downloads_ahead_cancels_and_resumes(monkeypatch, tmp_path):
    """FY-PF-021: over real HTTP and WebSocket, with a fake Hugging Face: Download to… one machine; progress
    shows on the server; Cancel stops it there and keeps the partial file; asked again it resumes from it and
    finishes; the machine's cache then lists the model."""
    from forgeyard.agent.backoff import Backoff
    from forgeyard.agent.client import Agent, join
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.conftest import OWNER, SETUP_TOKEN
    from tests.model_helpers import FakeHF, Repo
    from tests.test_agent import LiveServer, _free_port, _http, _wait

    rev = "f" * 40
    data = b"GGUF" + os.urandom(400_000)
    hf = FakeHF(Repo("acme/small-gguf", rev, {"small.gguf": data}), rate=8192)
    monkeypatch.setenv(SIMULATION_ENV, "1")
    monkeypatch.setenv("HF_ENDPOINT", hf.url)
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    root = tmp_path / "cache"
    try:
        with LiveServer(port):
            _, cookie = _http(port, "POST", "/api/auth/setup",
                              {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
            joined_ = join(server, _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"], name="cpu")
            node = joined_["node_id"]
            mid = add_catalog("small-gguf", {"kind": "hf", "repo": "acme/small-gguf", "ref": "main", "revision": rev,
                                             "format": "gguf", "file": "small.gguf", "size_bytes": len(data),
                                             "files": [{"path": "small.gguf", "size": len(data),
                                                        "sha256": hashlib.sha256(data).hexdigest()}]},
                              task="chat", fmt="gguf", source="hf://acme/small-gguf")

            def api(method, path, body=None):
                return _http(port, method, path, body, cookie)[0]

            async def scenario():
                state = tmp_path / "agent"
                rt = ProcessRuntime(RecordStore(state), tmp_path / "work", euid=lambda: 4242)
                sup = Supervisor(state, {"process": rt}, models=ModelCache(root, allow_private=True), tick=0.1,
                                 port_range=(31900, 31910))
                agent = Agent(server, joined_["credential"], SimulatedInventory("nvidia-rtx4090x2"),
                              backoff=Backoff(base=0.05, cap=0.2), supervisor=sup)
                run = asyncio.create_task(agent.run())
                try:
                    assert await _wait(lambda: "prefetch" in api("GET", "/api/machines")["machines"][0]["features"])
                    t = await asyncio.to_thread(api, "GET", f"/api/downloads/targets?model_id={mid}&engine=false")
                    assert t["machines"][0]["state"] == "ok"
                    first = (await asyncio.to_thread(api, "POST", "/api/downloads",
                                                     {"model_id": mid, "machine_ids": [node], "engine": False}))["downloads"][0]

                    def progress():
                        d = api("GET", f"/api/downloads?machine_id={node}")["downloads"][0]
                        return d["download"] and d["download"]["done"] > 50_000 and d

                    seen = await asyncio.to_thread(wait_until, progress, "progress on the server", 20)
                    assert seen["state"] == "downloading" and seen["download"]["total"] == len(data)
                    await asyncio.to_thread(api, "POST", f"/api/downloads/{first['id']}/cancel")
                    await asyncio.to_thread(wait_until, lambda: not sup.prefetch.jobs, "the agent to stop it", 10)
                    part = next((root / ".partial").glob("*.part"))
                    have = part.stat().st_size
                    assert 0 < have < len(data)
                    hf.rate = None
                    again = (await asyncio.to_thread(api, "POST", "/api/downloads",
                                                     {"model_id": mid, "machine_ids": [node], "engine": False}))["downloads"][0]

                    def done():
                        d = next(x for x in api("GET", f"/api/downloads?machine_id={node}")["downloads"] if x["id"] == again["id"])
                        return d["state"] == "done" and d

                    await asyncio.to_thread(wait_until, done, "the resumed download", 20)
                    listing = await asyncio.to_thread(wait_until, lambda: api("GET", f"/api/machines/{node}/cache")["models"],
                                                      "the cache listing", 10)
                    assert listing[0]["repo"] == "acme/small-gguf" and listing[0]["catalog"][0]["name"] == "small-gguf"
                    assert hf.downloads("small.gguf")[-1]["range"] == f"bytes={have}-"
                finally:
                    agent.stop()
                    await asyncio.wait_for(run, 10)

            asyncio.run(scenario())
    finally:
        hf.close()
