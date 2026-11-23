"""1.5.2 (#95, PR 3): Download to… for an engine alone, from its page (FY-EDL-*): migration 0017 (model_downloads.model_id
nullable, reversible), the machines that can take it (a machine no variant of the engine runs on is refused, saying
why), starting it (operators and admins, audited), what the agent is told (the variant's image with its recorded ids,
or its process archive with its SHA-256, and no model), a copy's image through the server by the ids "Try this
version" recorded, progress, cancel, the engine page's list, and a deleted engine ending its downloads."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import select, text

from forgeyard.config import get_settings
from forgeyard.db import ImagePin, ModelDownload, Node, get_engine, new_session, utcnow
from forgeyard.machines import relay
from forgeyard.migrate import alembic_config, upgrade
from forgeyard.models import reach, recipes
from forgeyard.workloads import pins, registry as oci
from tests.conftest import add_user, sign_in
from tests.image_helpers import FakeRegistry, build_image
from tests.machines_helpers import agent_client, bearer, connect, joined
from tests.test_prefetch import DOCKER, ENGINE_SHA, FEATURES, actions, by_name, cache, gguf_id, online, rows, status
from tests.test_workload_stream import recv_type
from tests.waits import wait_until

COPY = "llama-cpp-1-1-0"
IMAGES = FEATURES + ["image_ids", "relay", "relay_images"]
ARCHIVE = "https://github.com/ggml-org/llama.cpp/releases/download/b12359/llama-b12359-bin-ubuntu-x64.tar.gz"


def copy_text(image: str, name: str = COPY) -> str:
    """The built-in llama-cpp as a copy of version 1.1.0 whose CPU container variant is `image` (another repository
    here: the fake registry's), as "Try this version" writes it."""
    text = recipes.builtin_recipes()["llama-cpp"][1].split("\nvariants:")[0]
    text = text.replace("name: llama-cpp\n", f"name: {name}\nbased_on: llama-cpp\n", 1)
    text = text.replace('version: "1.0.0"', 'version: "1.1.0"', 1).replace("title: llama.cpp\n", "title: llama.cpp 1.1.0\n")
    return text + f"\nvariants:\n  - name: cpu\n    when: {{vendor: cpu}}\n    runtime: container\n    image: {image}\n"


@pytest.fixture()
def reg(monkeypatch):
    img = build_image("ghcr.io/acme/llama-copy:1.1.0")
    server = FakeRegistry(img)
    monkeypatch.setitem(oci.ENDPOINTS, "ghcr.io", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    get_settings.cache_clear()
    yield img, server
    server.close()
    pins.reset()
    get_settings.cache_clear()


def pinned_copy(owner, img) -> None:
    """The copy, and its image's recorded ids in image_pins (as Try records them), loaded."""
    r = owner.post("/api/recipes", json={"yaml": copy_text(img.ref)})
    assert r.status_code == 201, r.text
    db = new_session()
    try:
        db.add(ImagePin(ref=img.ref, entry=img.lock_entry(), signature={"status": "unsigned"}, recorded_by="owner",
                        recorded_at=utcnow()))
        db.commit()
        pins.refresh(db)
    finally:
        db.close()


def start(c, recipe: str, *node_ids: int, status_code: int = 201) -> dict:
    r = c.post("/api/downloads", json={"recipe": recipe, "machine_ids": list(node_ids)})
    assert r.status_code == status_code, r.text
    return r.json()


def uid_of(download_id: int) -> str:
    db = new_session()
    try:
        return db.get(ModelDownload, download_id).uid
    finally:
        db.close()


def hello_images(ws, features=IMAGES):
    return online(ws, features=features, runtime_info=DOCKER)


# ------------------------------------------------------------------ the migration
def test_fy_edl_001_migration_0017_makes_model_id_nullable_and_back(owner):
    """FY-EDL-001: 0017 makes model_downloads.model_id nullable and leaves every row as it was; down deletes only the
    engine-only rows (an older server cannot show them) and makes the column NOT NULL again; up again works (SQLite
    and PostgreSQL)."""
    def nullable() -> bool:
        with get_engine().connect() as conn:
            if conn.dialect.name == "sqlite":
                info = {r[1]: r for r in conn.execute(text("PRAGMA table_info(model_downloads)"))}
                return info["model_id"][3] == 0
            return conn.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_name = "
                                     "'model_downloads' AND column_name = 'model_id'")).scalar() == "YES"

    mid = gguf_id()
    a = joined(owner, "cpu-a")
    assert nullable()
    db = new_session()
    db.add(ModelDownload(model_id=mid, node_id=a["node_id"], recipe="llama-cpp", state="done", via="direct",
                         requested_by="owner", created_at=utcnow()))
    db.add(ModelDownload(model_id=None, node_id=a["node_id"], recipe="llama-cpp", state="queued", via="direct",
                         requested_by="owner", created_at=utcnow()))
    db.commit()
    db.close()
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0016_image_pins")
    assert not nullable()
    with get_engine().connect() as conn:
        left = conn.execute(text("SELECT model_id, recipe, state FROM model_downloads")).all()
        assert [tuple(r) for r in left] == [(mid, "llama-cpp", "done")]
        assert {r[0] for r in conn.execute(text("SELECT name FROM nodes"))} == {"cpu-a"}
    upgrade()
    assert nullable()
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM model_downloads")).scalar() == 1


# ------------------------------------------------------------------ the dialog
def test_fy_edl_002_each_machine_says_whether_it_can_take_the_engine(owner):
    """FY-EDL-002: GET /api/downloads/targets?recipe=… without a model: the variant each machine would run, its program
    (a process archive) or image, already there, too old, offline; a machine no variant of the engine runs on is
    refused with why (vLLM on a machine whose cards and driver no variant takes); an unknown engine is 404; neither a model nor an
    engine is 422."""
    gguf_id()
    names = ("proc", "dock", "there", "old")
    nodes = {n: joined(owner, n) for n in names + ("away",)}
    with connect(owner, nodes["away"]["credential"]) as away:
        online(away)
    with connect(owner, nodes["proc"]["credential"]) as proc, connect(owner, nodes["dock"]["credential"]) as dock, \
            connect(owner, nodes["there"]["credential"]) as there, connect(owner, nodes["old"]["credential"]) as old:
        online(proc)
        hello_images(dock)
        online(there, model_cache=cache(engines=[ENGINE_SHA[:16]]))
        online(old, features=["workloads", "models", "cache_admin"])
        r = owner.get("/api/downloads/targets?recipe=llama-cpp")
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["model"] is None and t["recipe"] == "llama-cpp" and t["engine"]["version"] == "1.0.0"
        m = by_name(t)
        assert m["proc"]["state"] == "ok" and m["proc"]["variant"] == "cpu-process"
        assert m["proc"]["engine"] == {"kind": "archive", "name": "llama.cpp-1.0.0", "cached": False}
        assert m["dock"]["state"] == "ok" and m["dock"]["engine"]["kind"] == "image"
        assert m["there"]["state"] == "cached" and m["there"]["note"] == "Already here."
        assert m["old"]["state"] == "too_old" and m["away"]["state"] == "offline"
        v = by_name(owner.get("/api/downloads/targets?recipe=vllm").json())
        assert v["proc"]["state"] == "no_engine" and v["proc"]["variant"] is None
        assert v["proc"]["note"].startswith("No variant of vllm runs on proc: ")
        assert "hardware" in v["proc"]["note"]
    assert owner.get("/api/downloads/targets?recipe=nope").status_code == 404
    r = owner.get("/api/downloads/targets")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "model_or_engine"


# ------------------------------------------------------------------ asking for it
def test_fy_edl_003_operators_start_it_and_the_agent_gets_the_engine_without_a_model(owner):
    """FY-EDL-003: an operator downloads the engine alone to two machines: the process machine is told its archive
    (with its SHA-256), the Docker machine the image with its recorded ids, neither a model; each is audited
    (engine.download_requested, the variant and what is fetched); a user may not; a machine no variant runs on is
    skipped with why; asking again while one runs starts nothing (409); `engine: false` without a model is 422."""
    gguf_id()
    add_user(owner, "op", role="operator")
    add_user(owner, "viewer", role="user")
    op = sign_in(owner, "op", "quiet-meadow-4821")
    viewer = sign_in(owner, "viewer", "quiet-meadow-4821")
    a, b = joined(owner, "proc"), joined(owner, "dock")
    with connect(owner, a["credential"]) as ws, connect(owner, b["credential"]) as dws:
        online(ws)
        hello_images(dws, features=FEATURES + ["image_ids"])
        body = {"recipe": "llama-cpp", "machine_ids": [a["node_id"], b["node_id"]]}
        assert viewer.post("/api/downloads", json=body).status_code == 403
        out = start(op, "llama-cpp", a["node_id"], b["node_id"])
        assert [d["machine"]["name"] for d in out["downloads"]] == ["dock", "proc"] and out["skipped"] == []
        assert all(d["engine_only"] and d["model"] is None and d["recipe"] == "llama-cpp" for d in out["downloads"])
        [item] = recv_type(ws, "desired")["downloads"]
        assert "model" not in item and item["engine"]["url"] == ARCHIVE and item["engine"]["sha256"] == ENGINE_SHA
        [ditem] = recv_type(dws, "desired")["downloads"]
        assert "model" not in ditem and "engine" not in ditem
        assert ditem["image"].startswith("ghcr.io/ggml-org/llama.cpp:server-v1.0.0") and ditem["image_ids"]["ids"]
        again = op.post("/api/downloads", json={"recipe": "llama-cpp", "machine_ids": [a["node_id"]]})
        assert again.status_code == 409 and again.json()["detail"]["skipped"][0]["state"] == "downloading"
        r = op.post("/api/downloads", json={"recipe": "vllm", "machine_ids": [a["node_id"]]})
        assert r.status_code == 409 and r.json()["detail"]["skipped"][0]["state"] == "no_engine"
        r = op.post("/api/downloads", json={"recipe": "llama-cpp", "machine_ids": [a["node_id"]], "engine": False})
        assert r.status_code == 422
    db = new_session()
    from forgeyard.db import AuditEvent

    recs = [e.detail | {"target": e.target} for e in db.scalars(select(AuditEvent).where(
        AuditEvent.action == "engine.download_requested").order_by(AuditEvent.id))]
    db.close()
    assert [(x["target"], x["variant"], x["recipe"], x["version"]) for x in recs] == [
        ("dock", "cpu", "llama-cpp", "1.0.0"), ("proc", "cpu-process", "llama-cpp", "1.0.0")]
    assert recs[1]["engine"] == "llama.cpp-1.0.0" and recs[0]["engine"].startswith("ghcr.io/ggml-org/llama.cpp:server-v1.0.0")


def test_fy_edl_004_a_copys_image_comes_through_the_server_by_its_recorded_ids(owner, reg):
    """FY-EDL-004: a copy's image (recorded in image_pins by Try) downloaded alone to a machine set to fetch through the
    server: the dialog says "server", the agent gets the image with the copy's recorded ids and `image_relay`, and the
    server hands it the manifest of its platform; set to "direct", no relay."""
    img, server = reg
    pinned_copy(owner, img)
    a = joined(owner, "dock")
    owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "server"})
    with connect(owner, a["credential"]) as ws:
        hello_images(ws)
        t = by_name(owner.get(f"/api/downloads/targets?recipe={COPY}").json())
        assert t["dock"]["state"] == "ok" and t["dock"]["via"] == "server" and t["dock"]["engine"]["name"] == img.ref
        d = start(owner, COPY, a["node_id"])["downloads"][0]
        [item] = recv_type(ws, "desired")["downloads"]
        assert item["image"] == img.ref and item["image_ids"] == img.check() and "model" not in item
        assert item["image_relay"] == {"path": f"/agent/v1/relay/download/{item['id']}/image", "mode": "server"}
        r = agent_client(owner).get(f"/agent/v1/relay/download/{uid_of(d['id'])}/image/manifest",
                                    headers=bearer(a["credential"]))
        assert r.status_code == 200, r.text
        assert r.content == img.platforms["amd64"].manifest
        owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "direct"})
        assert "image_relay" not in recv_type(ws, "desired")["downloads"][0]


def test_fy_edl_005_a_process_archive_comes_through_the_server_with_its_sha256(owner):
    """FY-EDL-005: the engine's process archive downloaded alone to a machine set to fetch through the server: the
    engine block carries its SHA-256 and the relay path, and the server resolves that path to the archive's URL; the
    model part of the same path is refused (there is none)."""
    gguf_id()
    a = joined(owner, "proc")
    owner.put(f"/api/machines/{a['node_id']}/fetch-via", json={"fetch_via": "server"})
    with connect(owner, a["credential"]) as ws:
        online(ws, features=FEATURES + ["relay"])
        d = start(owner, "llama-cpp", a["node_id"])["downloads"][0]
        [item] = recv_type(ws, "desired")["downloads"]
        assert item["engine"]["sha256"] == ENGINE_SHA
        assert item["engine"]["relay"] == {"path": f"/agent/v1/relay/download/{item['id']}/engine", "mode": "server"}
        db = new_session()
        try:
            node = db.get(Node, a["node_id"])
            src = relay.resolve(db, node, "download", uid_of(d["id"]), "engine", None)
            assert src.url == ARCHIVE
            with pytest.raises(relay.Refused) as e:
                relay.resolve(db, node, "download", uid_of(d["id"]), "model", None)
            assert e.value.code == "relay_not_wanted"
        finally:
            db.close()


# ------------------------------------------------------------------ progress, cancel, the page
def test_fy_edl_006_progress_done_cancel_and_the_engine_page(owner):
    """FY-EDL-006: the agent's progress is kept and shown on the engine's page (the machine's row and the page's
    list) and in /api/downloads?engine=…; "done" ends it and counts the image as on the machine; an operator cancels
    another (audited engine.download_cancelled; the agent's next list no longer has it); the page says who may
    download."""
    gguf_id()
    add_user(owner, "viewer", role="user")
    viewer = sign_in(owner, "viewer", "quiet-meadow-4821")
    a, b = joined(owner, "proc"), joined(owner, "dock")
    with connect(owner, a["credential"]) as ws, connect(owner, b["credential"]) as dws:
        online(ws)
        hello_images(dws, features=FEATURES + ["image_ids"])
        start(owner, "llama-cpp", a["node_id"], b["node_id"])
        mine = recv_type(ws, "desired")["downloads"][0]["id"]
        dock = recv_type(dws, "desired")["downloads"][0]
        status(dws, {"id": dock["id"], "state": "downloading", "reason": "pulling the image",
                     "download": {"phase": "image", "done": 10, "total": 100, "layers": 2, "layers_done": 1}})
        wait_until(lambda: next(r for r in rows() if r.uid == dock["id"]).download, "the progress")
        page = owner.get("/api/recipes/llama-cpp").json()
        assert page["can_download"] is True and viewer.get("/api/recipes/llama-cpp").json()["can_download"] is False
        row = next(x for x in page["machines"] if x["name"] == "dock")
        assert row["download"]["download"]["phase"] == "image" and row["download"]["engine_only"] is True
        assert [d["machine"]["name"] for d in page["downloads"]] == ["proc", "dock"]   # newest first
        listed = owner.get("/api/downloads?engine=llama-cpp").json()["downloads"]
        assert len(listed) == 2 and owner.get("/api/downloads?engine=vllm").json()["downloads"] == []
        status(dws, {"id": dock["id"], "state": "done"})
        wait_until(lambda: next(r for r in rows() if r.uid == dock["id"]).state == "done", "done")
        assert reach.has_image(Node(id=b["node_id"]), dock["image"])
        proc_row = next(r for r in rows() if r.uid == mine)
        r = owner.post(f"/api/downloads/{proc_row.id}/cancel")
        assert r.status_code == 200 and r.json()["download"]["state"] == "cancelled"
        assert recv_type(ws, "desired")["downloads"] == []
    assert "engine.download_cancelled" in actions() and "model.download_cancelled" not in actions()


def test_fy_edl_007_deleting_the_engine_ends_its_downloads(owner, reg):
    """FY-EDL-007: deleting a copy while a download of it alone runs ends that download (failed, saying why) and the
    agent's next list no longer has it."""
    img, _ = reg
    pinned_copy(owner, img)
    a = joined(owner, "dock")
    with connect(owner, a["credential"]) as ws:
        hello_images(ws)
        start(owner, COPY, a["node_id"])
        assert recv_type(ws, "desired")["downloads"]
        assert owner.delete(f"/api/recipes/{COPY}").status_code == 200
        assert recv_type(ws, "desired")["downloads"] == []
    [row] = rows()
    assert row.state == "failed" and row.reason == f"the engine {COPY} was deleted"


def test_fy_edl_008_nothing_to_fetch_is_never_sent_to_the_agent(owner):
    """FY-EDL-008: an engine-only download whose engine has nothing for that machine any more (its hardware runs no
    variant of it) is left out of the agent's list rather than sent as an empty download."""
    gguf_id()
    a = joined(owner, "proc")
    with connect(owner, a["credential"]) as ws:
        online(ws)
        db = new_session()
        db.add(ModelDownload(model_id=None, node_id=a["node_id"], recipe="vllm", state="queued", via="direct",
                             requested_by="owner", created_at=utcnow()))
        db.commit()
        db.close()
        start(owner, "llama-cpp", a["node_id"])
        [item] = recv_type(ws, "desired")["downloads"]
        assert item["engine"]["url"] == ARCHIVE
