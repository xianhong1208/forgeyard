"""A page per catalog model and per engine (1.4.3, issue #140; FY-PAGE-001…009): GET /api/models/{name} (the model, its
files and sizes, each machine's cache entry and engine, where it fits with each engine's defaults, its deployments),
GET /api/recipes/{name} (each machine's variant and whether the image or program is there, the deployments and models
that use it), the image listing the server keeps per machine (nodes.runtime_info["images"]), who may look and who may
change, and the model names the console's routes reserve."""

import pytest
from sqlalchemy import select

from forgeyard.db import Node, new_session
from forgeyard.machines import engineimages
from forgeyard.models import reach
from forgeyard.workloads import images
from tests.conftest import add_user, sign_in
from tests.image_helpers import build_image
from tests.machines_helpers import connect, hello, joined, machine
from tests.model_helpers import add_catalog
from tests.test_cache_api import deployment
from tests.test_engine_images import InThread, answer_list, recipe
from tests.test_prefetch import DOCKER, ENGINE_SHA, FEATURES, MODEL_FILE, MODEL_KEY, PROCESS, cache, gguf_id
from tests.test_workload_stream import recv, recv_type

GGUF_REVISION_KEY = MODEL_KEY.rsplit("/", 1)[0]
LISTS = FEATURES + ["image_ids", "image_load", "image_save", "image_remove"]
TAGGED = "registry.example/acme/tts:2.0"


def online(ws, *, features=FEATURES, runtime_info=PROCESS, model_cache=None, fixture="nvidia-rtx4090x2"):
    ws.send_json(hello(fixture, features=list(features), runtimes=[k for k, v in runtime_info.items() if v.get("ok")],
                       runtime_info=runtime_info, model_cache=cache() if model_cache is None else model_cache))
    assert recv(ws)["type"] == "welcome"
    return recv_type(ws, "desired")


def sized_cache() -> dict:
    """A 1.4 agent's report with the GGUF model's revision, the model file as a part, and the llama.cpp program."""
    return cache(cached=[GGUF_REVISION_KEY, MODEL_KEY], engines=[ENGINE_SHA[:16]],
                 models=[{"key": GGUF_REVISION_KEY, "kind": "hf", "repo": "ggml-org/Qwen3-1.7B-GGUF", "bytes": 1300000000,
                          "files": 1, "completed_at": 1.0,
                          "parts": [{"key": MODEL_KEY, "file": MODEL_FILE, "bytes": 1282439264}]}])


def viewer(owner):
    add_user(owner, "viewer")
    return sign_in(owner, "viewer", "quiet-meadow-4821")


def stored_listing(node_id: int):
    db = new_session()
    try:
        return (db.get(Node, node_id).runtime_info or {}).get("images")
    finally:
        db.close()


# ------------------------------------------------------------------ a model's page
def test_fy_page_001_a_models_page_for_everyone_signed_in(owner):
    """FY-PAGE-001: GET /api/models/{name} (everyone signed in; also by id): the model, its files with sizes, each
    machine (not revoked) with its cache entry (size there, what uses it), the engine variant it would run and
    whether that program is there, where the model fits with each engine's defaults on each device (the estimate
    and every machine's verdict), and its deployments. A plain user may look and gets can_manage/can_deploy false;
    an unknown name is 404, a signed-out caller 401."""
    mid = gguf_id()
    a = joined(owner, "box-a")
    joined(owner, "box-b")                       # joined, never connected: no cache report
    gone = joined(owner, "box-gone")
    assert owner.post(f"/api/machines/{gone['node_id']}/revoke").status_code == 200   # a revoked machine is not listed
    user = viewer(owner)
    with connect(owner, a["credential"]) as ws:
        online(ws, features=FEATURES + ["cache_admin"], model_cache=sized_cache())
        deployment(a["node_id"], mid, "chat-gguf")
        r = user.get("/api/models/qwen3-1.7b-gguf")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["model"]["id"] == mid and body["model"]["deployments"] == 1
        assert body["can_manage"] is False and body["can_deploy"] is False
        assert [x["name"] for x in body["recipes"]] == ["llama-cpp"]
        assert body["files"] == {"recorded": True, "downloaded_count": 1, "downloaded_bytes": 1282439264,
                                 "files": [{"path": MODEL_FILE, "size": 1282439264, "role": "model file",
                                            "downloaded": True}]}
        rows = {m["name"]: m for m in body["machines"]}
        assert set(rows) == {"box-a", "box-b"}
        here = rows["box-a"]
        assert here["connected"] is True and here["cache"]["cached"] is True
        assert here["cache"]["bytes"] == 1282439264 and here["cache"]["files"] == 1
        assert [u["name"] for u in here["cache"]["used_by"]] == ["chat-gguf"]
        eng = here["engines"]["llama-cpp"]
        assert eng["variant"] == "cpu-process" and eng["kind"] == "program" and eng["present"] is True
        assert eng["engine_name"] == "llama.cpp-1.0.0"
        away = rows["box-b"]
        assert away["cache"] == {"reported": False, "cached": False, "bytes": None, "files": None, "used_by": []}
        assert away["engines"]["llama-cpp"]["present"] is None
        fit = body["fits"]["llama-cpp"]
        assert set(fit) == {"gpu", "cpu"}
        cpu = {v["name"]: v for v in fit["cpu"]["machines"]}
        assert fit["cpu"]["estimate"]["device"] == "cpu" and fit["cpu"]["estimate"]["ram"] > 0
        assert cpu["box-a"]["fits"] is True and cpu["box-a"]["variant"] == "cpu-process"
        assert cpu["box-b"]["fits"] is False and cpu["box-b"]["reason"]
        assert fit["gpu"]["estimate"]["per_gpu"] > 0
        assert body["deployments"] == [{"id": body["deployments"][0]["id"], "name": "chat-gguf", "recipe": "llama-cpp"}]
        assert user.get(f"/api/models/{mid}").json()["model"]["name"] == "qwen3-1.7b-gguf"
    assert user.get("/api/models/no-such-model").status_code == 404
    owner.post("/api/auth/logout")
    assert owner.get("/api/models/qwen3-1.7b-gguf").status_code == 401


def test_fy_page_002_the_files_a_machine_downloads_and_the_others(owner):
    """FY-PAGE-002: the files come from the catalog row: the ones a machine downloads (recorded when the model was
    added: the model file, its projector, image components) first, then the repository's other GGUF quantisations
    and projectors, marked as not downloaded, with their sizes. A model with no recorded files (a local directory)
    says so."""
    add_catalog("vl-gguf", {"kind": "hf", "repo": "acme/vl-GGUF", "ref": "main", "revision": "a" * 40,
                            "file": "vl-Q4_K_M.gguf", "mmproj": {"path": "mmproj-F16.gguf", "size": 600},
                            "files": [{"path": "vl-Q4_K_M.gguf", "size": 4000, "sha256": "1" * 64},
                                      {"path": "mmproj-F16.gguf", "size": 600, "sha256": "2" * 64}],
                            "gguf_files": [{"path": "vl-Q4_K_M.gguf", "size": 4000, "parts": 1},
                                           {"path": "vl-Q8_0.gguf", "size": 7000, "parts": 1}],
                            "mmproj_files": [{"path": "mmproj-F16.gguf", "size": 600},
                                             {"path": "mmproj-BF16.gguf", "size": 610}],
                            "size_bytes": 4600}, task="chat", fmt="gguf", source="hf://acme/vl-GGUF")
    add_catalog("on-disk", {"kind": "local", "path": "/models/x", "format": "safetensors", "size_bytes": 10},
                task="chat", fmt="safetensors", source="local:///models/x", kind="local")
    user = viewer(owner)
    files = user.get("/api/models/vl-gguf").json()["files"]
    assert [(f["path"], f["size"], f["role"], f["downloaded"]) for f in files["files"]] == [
        ("vl-Q4_K_M.gguf", 4000, "model file", True), ("mmproj-F16.gguf", 600, "projector", True),
        ("vl-Q8_0.gguf", 7000, "other quantisation", False), ("mmproj-BF16.gguf", 610, "other projector", False)]
    assert files["downloaded_count"] == 2 and files["downloaded_bytes"] == 4600 and files["recorded"] is True
    local = user.get("/api/models/on-disk").json()["files"]
    assert local == {"recorded": False, "files": [], "downloaded_count": 0, "downloaded_bytes": 0}


def test_fy_page_003_the_console_routes_reserve_some_model_names(owner):
    """FY-PAGE-003: the console opens a model at /models/<name> beside /models/deploy, /models/deployments/…,
    /models/routes/… and /models/engines/…, and the API serves /api/models/recommended and /search beside
    /api/models/<name>: those words are refused as model names (422 reserved_name, before anything is inspected),
    and the API's own paths stay theirs (a plain user gets 403 from /models/recommended, not a model page)."""
    for word in ("deploy", "deployments", "routes", "engines", "catalog", "sources", "recommended", "search", "inspect"):
        r = owner.post("/api/models", json={"name": word, "source": "hf://acme/tiny-chat", "task": "chat"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "reserved_name", (word, r.text)
    user = viewer(owner)
    assert user.get("/api/models/recommended").status_code == 403
    assert user.get("/api/models/search?q=x").status_code == 403
    assert owner.get("/api/models/recommended").json()["models"]


def test_fy_page_004_title_edit_and_remove_are_for_admins(owner):
    """FY-PAGE-004: the page's display name is the catalog title: an admin changes it (PATCH title) and the page shows
    it, an empty title goes back to the name; the name itself never changes. A plain user may look but not rename or
    remove (403)."""
    mid = gguf_id()
    user = viewer(owner)
    assert user.patch(f"/api/models/{mid}", json={"title": "Mine"}).status_code == 403
    assert user.delete(f"/api/models/{mid}").status_code == 403
    r = owner.patch(f"/api/models/{mid}", json={"title": "  Qwen3 1.7B  "})
    assert r.status_code == 200 and r.json()["model"]["title"] == "Qwen3 1.7B"
    page = user.get("/api/models/qwen3-1.7b-gguf").json()["model"]
    assert page["title"] == "Qwen3 1.7B" and page["name"] == "qwen3-1.7b-gguf"
    assert owner.patch(f"/api/models/{mid}", json={"title": ""}).json()["model"]["title"] is None


# ------------------------------------------------------------------ the image listing the server keeps
def test_fy_page_005_the_server_keeps_each_machines_image_listing(owner):
    """FY-PAGE-005: every image_listed answer is kept in nodes.runtime_info["images"] (id, tags, content id, size only;
    when it was listed), so the engine page can say which machines have an image while they are offline too; it
    survives the agent's next hello, never shows in GET /api/machines, a removal the agent confirms takes the image
    out, and an upload it loaded adds it. A failed listing changes nothing."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws, features=LISTS, runtime_info=DOCKER)
        assert stored_listing(j["node_id"]) is None
        call = InThread(lambda: owner.get(f"/api/machines/{j['node_id']}/engine-images"))
        answer_list(ws, [{"id": "sha256:" + "a" * 64, "tags": ["acme/tts:2.0"], "content": None, "size": 900,
                          "os": "linux", "architecture": "amd64", "created": "2026-11-01"}])
        assert call.result().status_code == 200
        kept = stored_listing(j["node_id"])
        assert kept["items"] == [{"id": "sha256:" + "a" * 64, "tags": ["acme/tts:2.0"], "content": None, "size": 900}]
        assert kept["at"]
        listed = machine(owner, j["node_id"])["runtime_info"]
        assert listed["container"]["ok"] is True and "images" not in listed
        # A failed listing keeps the last one.
        call = InThread(lambda: owner.get(f"/api/machines/{j['node_id']}/engine-images"))
        ask = recv_type(ws, "image_list")
        ws.send_json({"v": 1, "type": "image_listed", "request_id": ask["request_id"], "ok": False, "reason": "busy"})
        call.result()
        assert stored_listing(j["node_id"])["items"][0]["id"] == "sha256:" + "a" * 64
    # The next hello keeps it.
    with connect(owner, j["credential"]) as ws:
        online(ws, features=LISTS, runtime_info=DOCKER)
        assert len(stored_listing(j["node_id"])["items"]) == 1
    db = new_session()
    try:
        node = db.get(Node, j["node_id"])
        engineimages.add_loaded(node, [{"id": "sha256:" + "b" * 64, "tags": ["acme/other:1"]}])
        engineimages.forget(node, "sha256:" + "a" * 64)
        db.commit()
    finally:
        db.close()
    assert [i["id"] for i in stored_listing(j["node_id"])["items"]] == ["sha256:" + "b" * 64]


def test_fy_page_006_a_confirmed_removal_leaves_the_kept_listing(owner):
    """FY-PAGE-006: through the agent stream: an image_removed answer with ok takes that image out of the kept
    listing; one that failed leaves it."""
    recipe(owner, "tts", TAGGED)
    j = joined(owner)
    item = {"id": "sha256:" + "a" * 64, "tags": [TAGGED], "content": None, "size": 900, "os": "linux",
            "architecture": "amd64", "created": None}
    with connect(owner, j["credential"]) as ws:
        online(ws, features=LISTS, runtime_info=DOCKER)
        call = InThread(lambda: owner.post(f"/api/machines/{j['node_id']}/engine-images/remove",
                                           json={"image": item["id"]}))
        answer_list(ws, [item])
        ask = recv_type(ws, "image_remove")
        ws.send_json({"v": 1, "type": "image_removed", "request_id": ask["request_id"], "ok": False, "code": "failed",
                      "reason": "disk busy", "image": item})
        assert call.result().status_code == 409
        assert len(stored_listing(j["node_id"])["items"]) == 1
        call = InThread(lambda: owner.post(f"/api/machines/{j['node_id']}/engine-images/remove",
                                           json={"image": item["id"]}))
        answer_list(ws, [item])
        ask = recv_type(ws, "image_remove")
        ws.send_json({"v": 1, "type": "image_removed", "request_id": ask["request_id"], "ok": True, "image": item,
                      "freed_bytes": 900})
        assert call.result().status_code == 200
        assert stored_listing(j["node_id"])["items"] == []


# ------------------------------------------------------------------ an engine's page
def test_fy_page_007_an_engines_page_says_where_its_image_is(owner):
    """FY-PAGE-007: GET /api/recipes/{name} (everyone signed in) adds, per machine, the variant it would run and whether
    that image is there by the machine's last listing: a tag-only image by its tag; True / False once listed, None
    (with a note) before; still answered from the kept listing once the machine is offline. A pulled image the
    server saw counts as there. The deployments and catalog models that use it are listed; can_manage only for
    admins; an unknown engine is 404."""
    recipe(owner, "tts", TAGGED)
    a = joined(owner, "box-a")
    b = joined(owner, "box-b")
    user = viewer(owner)
    with connect(owner, a["credential"]) as wa, connect(owner, b["credential"]) as wb:
        online(wa, features=LISTS, runtime_info=DOCKER)
        online(wb, features=LISTS, runtime_info=DOCKER)
        page = user.get("/api/recipes/tts").json()
        rows = {m["name"]: m for m in page["machines"]}
        assert rows["box-a"]["variant"] == "cpu" and rows["box-a"]["kind"] == "image"
        assert rows["box-a"]["present"] is None and rows["box-a"]["note"] and rows["box-a"]["can_list"] is True
        assert page["can_manage"] is False and page["recipe"]["name"] == "tts" and "yaml" in page["recipe"]
        call = InThread(lambda: owner.get(f"/api/machines/{a['node_id']}/engine-images"))
        answer_list(wa, [{"id": "sha256:" + "a" * 64, "tags": ["registry.example/acme/tts:2.0"], "size": 900}])
        call.result()
        call = InThread(lambda: owner.get(f"/api/machines/{b['node_id']}/engine-images"))
        answer_list(wb, [{"id": "sha256:" + "f" * 64, "tags": ["registry.example/acme/tts:1.0"], "size": 800}])
        call.result()
    page = user.get("/api/recipes/tts").json()
    rows = {m["name"]: m for m in page["machines"]}
    assert rows["box-a"]["present"] is True and rows["box-a"]["listed_at"] and rows["box-a"]["connected"] is False
    assert rows["box-b"]["present"] is False
    reach.seen_image(b["node_id"], TAGGED)       # a replica pulled it there since the server started
    assert {m["name"]: m for m in user.get("/api/recipes/tts").json()["machines"]}["box-b"]["present"] is True
    assert user.get("/api/recipes/no-such-engine").status_code == 404
    assert owner.get("/api/recipes/tts").json()["can_manage"] is True


def test_fy_page_008_refresh_lists_the_connected_machines_again(owner, monkeypatch):
    """FY-PAGE-008: GET /api/recipes/{name}?refresh=true asks every connected machine that can list its images to list
    them again first, keeps each answer and answers with them; a machine that does not answer in time keeps its last
    listing and the page still answers."""
    from forgeyard.api import models as models_api

    monkeypatch.setattr(models_api, "LIST_TIMEOUT", 1.0)
    recipe(owner, "tts", TAGGED)
    a = joined(owner, "box-a")
    with connect(owner, a["credential"]) as wa:
        online(wa, features=LISTS, runtime_info=DOCKER)
        call = InThread(lambda: owner.get("/api/recipes/tts?refresh=true"))
        answer_list(wa, [{"id": "sha256:" + "a" * 64, "tags": [TAGGED], "size": 900}])
        rows = {m["name"]: m for m in call.result().json()["machines"]}
        assert rows["box-a"]["present"] is True
        call = InThread(lambda: owner.get("/api/recipes/tts?refresh=true"))
        recv_type(wa, "image_list")          # asked, and never answered
        r = call.result()
        assert r.status_code == 200 and {m["name"]: m for m in r.json()["machines"]}["box-a"]["present"] is True


def test_fy_page_009_an_engines_program_deployments_and_models(owner):
    """FY-PAGE-009: a process variant's program is there when the machine's cache report lists its archive, not there
    when the report does not, unknown without a report; the engine page lists the deployments that use it and the
    catalog models it runs."""
    mid = gguf_id()
    a = joined(owner, "box-a")
    b = joined(owner, "box-b")
    with connect(owner, a["credential"]) as wa, connect(owner, b["credential"]) as wb:
        online(wa, model_cache=cache(engines=[ENGINE_SHA[:16]]))
        online(wb, model_cache=cache())
        deployment(a["node_id"], mid, "chat-gguf")
        page = owner.get("/api/recipes/llama-cpp").json()
    rows = {m["name"]: m for m in page["machines"]}
    assert rows["box-a"]["variant"] == "cpu-process" and rows["box-a"]["kind"] == "program"
    assert rows["box-a"]["present"] is True and rows["box-b"]["present"] is False
    assert [d["name"] for d in page["deployments"]] == ["chat-gguf"]
    assert "qwen3-1.7b-gguf" in [m["name"] for m in page["models"]]
    c = joined(owner, "box-c")
    assert {m["name"]: m for m in owner.get("/api/recipes/llama-cpp").json()["machines"]}["box-c"]["present"] is None
    assert c
    db = new_session()
    try:
        assert db.scalar(select(Node.id).where(Node.name == "box-c")) == c["node_id"]
    finally:
        db.close()


# ------------------------------------------------------------------ 1.5.6 (#282): an engine image as current state
RELAYS = LISTS + ["relay", "relay_images"]


@pytest.fixture()
def img(monkeypatch):
    """A pinned engine image recorded in the lock (the server can relay it)."""
    image = build_image("ghcr.io/acme/engine:1.0", arches=("amd64",))
    lock = {**images.load_lock(), image.ref: image.lock_entry()}
    monkeypatch.setattr(images, "load_lock", lambda path=None: lock)
    return image


def test_fy_page_010_how_a_missing_image_comes_and_its_size(owner, img):
    """FY-PAGE-010: per machine, the engine page and the model page say how a missing engine image comes (`via`):
    "server" for a machine that fetches through the server and whose agent relays images (a pinned, recorded image),
    "auto" when it tries directly first, "direct" for a machine without the relay feature, set to direct, or for an
    image the server cannot relay (tag-only); and its size when a machine's listing shows it: this machine's, else
    another machine's that has the same image (None when no listing has it)."""
    recipe(owner, "tts", img.ref)
    recipe(owner, "tagged", TAGGED)
    a = joined(owner, "box-a")
    b = joined(owner, "box-b")
    owner.put(f"/api/machines/{b['node_id']}/fetch-via", json={"fetch_via": "server"})
    with connect(owner, a["credential"]) as wa, connect(owner, b["credential"]) as wb:
        online(wa, features=LISTS, runtime_info=DOCKER)
        online(wb, features=RELAYS, runtime_info=DOCKER)
        page = owner.get("/api/recipes/tts").json()
        rows = {m["name"]: m for m in page["machines"]}
        assert rows["box-b"]["via"] == "server" and rows["box-b"]["size"] is None   # no listing has it yet
        call = InThread(lambda: owner.get(f"/api/machines/{a['node_id']}/engine-images"))
        answer_list(wa, [{"id": "sha256:" + "c" * 64, "tags": [img.name], "content": None, "size": 9_350_000}])
        call.result()
        call = InThread(lambda: owner.get(f"/api/machines/{b['node_id']}/engine-images"))
        answer_list(wb, [])
        call.result()
        rows = {m["name"]: m for m in owner.get("/api/recipes/tts").json()["machines"]}
        assert rows["box-a"]["present"] is True and rows["box-a"]["size"] == 9_350_000 and rows["box-a"]["via"] == "direct"
        assert rows["box-b"]["present"] is False and rows["box-b"]["size"] == 9_350_000 and rows["box-b"]["via"] == "server"
        assert {m["name"]: m for m in owner.get("/api/recipes/tagged").json()["machines"]}["box-b"]["via"] == "direct"
        owner.put(f"/api/machines/{b['node_id']}/fetch-via", json={"fetch_via": "auto"})
        assert {m["name"]: m for m in owner.get("/api/recipes/tts").json()["machines"]}["box-b"]["via"] == "auto"
        owner.put(f"/api/machines/{b['node_id']}/fetch-via", json={"fetch_via": "direct"})
        assert {m["name"]: m for m in owner.get("/api/recipes/tts").json()["machines"]}["box-b"]["via"] == "direct"
        owner.put(f"/api/machines/{b['node_id']}/fetch-via", json={"fetch_via": "server"})
        mid = gguf_id()
        model = owner.get(f"/api/models/{mid}").json()
        on_b = {m["name"]: m for m in model["machines"]}["box-b"]["engines"]
        assert on_b["tts"]["via"] == "server" and on_b["tts"]["size"] == 9_350_000 and on_b["tts"]["present"] is False


def test_fy_page_011_the_estimate_says_whether_the_chosen_machine_has_the_engine(owner, img):
    """FY-PAGE-011: POST /api/deployments/estimate: each placed replica carries `engine` for the variant it was placed
    with: present (by the machine's last listing), via and size, as on the engine page. The keys the answer had before
    stay as they were."""
    # A GPU variant first (another image), then the CPU one these machines run.
    text = open("forgeyard/recipes/llama-cpp.yaml").read().split("\nvariants:")[0].replace("name: llama-cpp", "name: tts")
    text += ("\nvariants:\n  - name: cuda\n    when: {vendor: nvidia}\n    runtime: container\n    image: ghcr.io/acme/engine-cuda:1.0\n"
             f"  - name: cpu\n    when: {{vendor: cpu}}\n    runtime: container\n    image: {img.ref}\n")
    assert owner.post("/api/recipes", json={"yaml": text}).status_code == 201
    gguf_id()
    a = joined(owner, "box-a")
    b = joined(owner, "box-b")
    owner.put(f"/api/machines/{b['node_id']}/fetch-via", json={"fetch_via": "server"})
    with connect(owner, a["credential"]) as wa, connect(owner, b["credential"]) as wb:
        online(wa, features=LISTS, runtime_info=DOCKER)
        online(wb, features=RELAYS, runtime_info=DOCKER)
        call = InThread(lambda: owner.get(f"/api/machines/{a['node_id']}/engine-images"))
        answer_list(wa, [{"id": "sha256:" + "c" * 64, "tags": [img.name], "content": None, "size": 4_000}])
        call.result()
        call = InThread(lambda: owner.get(f"/api/machines/{b['node_id']}/engine-images"))
        answer_list(wb, [])
        call.result()
        body = {"name": "t", "model": "qwen3-1.7b-gguf", "recipe": "tts", "params": {"gpus": 0}}
        here = owner.post("/api/deployments/estimate", json={**body, "machine_id": a["node_id"]})
        assert here.status_code == 200, here.text
        placed = here.json()["placement"]["placed"][0]
        assert placed["machine_id"] == a["node_id"] and placed["variant"] == "cpu"
        assert {"replica", "machine", "variant", "runtime", "image", "gpus", "cached", "leftover", "online"} <= set(placed)
        assert placed["engine"]["kind"] == "image" and placed["engine"]["present"] is True
        there = owner.post("/api/deployments/estimate", json={**body, "machine_id": b["node_id"]}).json()
        eng = there["placement"]["placed"][0]["engine"]
        assert eng["present"] is False and eng["via"] == "server" and eng["size"] == 4_000
        assert eng["engine_name"] == img.ref
