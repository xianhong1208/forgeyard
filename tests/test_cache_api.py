"""A machine's model cache on the server (1.4, issue #35): the listing with the catalog models and the
deployments that use each entry, Remove refused while one does (naming it) and asked of the agent
otherwise, partial downloads, older agents, and the whole loop with a real agent (FY-CACHE-*)."""

import asyncio
import json
import threading
import time

from sqlalchemy import select

from forgeyard.db import AuditEvent, Deployment, Replica, Workload, new_session
from forgeyard.machines import cache as server_cache
from forgeyard.machines.messages import CacheRemovedIn, StatusIn
from tests.conftest import add_user, sign_in
from tests.machines_helpers import connect, hello, joined
from tests.model_helpers import add_catalog
from tests.test_workload_stream import recv, recv_type

REV = "c" * 40
KEY = f"hub/models--acme--gguf@{REV}"
Q4 = f"{KEY}/m-Q4_K_M.gguf"
Q8 = f"{KEY}/m-Q8_0.gguf"
URL = "url/" + "d" * 24
ENGINE = "engine/" + "e" * 16


def reported(**over) -> dict:
    """What an agent with cache_admin reports: a GGUF revision with two model files, a URL model, an engine."""
    out = {"ok": True, "free_bytes": 50 * 1024**3, "total_bytes": 100 * 1024**3, "cached": [KEY, Q4, Q8, URL],
           "engines": ["e" * 16],
           "models": [{"key": KEY, "kind": "hf", "repo": "acme/gguf", "revision": REV, "bytes": 7000, "files": 3,
                       "completed_at": 1.0,
                       "parts": [{"key": Q4, "file": "m-Q4_K_M.gguf", "bytes": 2000},
                                 {"key": Q8, "file": "m-Q8_0.gguf", "bytes": 4000}]},
                      {"key": URL, "kind": "url", "file": "tiny.gguf", "bytes": 100, "files": 1, "completed_at": 2.0}],
           "engines_detail": [{"key": ENGINE, "id": "e" * 16, "name": "llama-cpp", "bytes": 512}],
           "used_bytes": 7700, "partial_bytes": 300, "partial_files": 1}
    out.update(over)
    return out


def online(ws, features=("workloads", "models", "cache_admin"), model_cache=None):
    ws.send_json(hello(features=list(features), runtimes=["process"],
                       runtime_info={"process": {"ok": True, "user": "nobody", "uid": 65534, "limits": "rlimit"}},
                       model_cache=reported() if model_cache is None else model_cache))
    welcome = recv(ws)
    assert welcome["type"] == "welcome" and "cache_admin" in welcome["features"]
    recv_type(ws, "desired")


def catalog_q4() -> int:
    meta = {"repo": "acme/gguf", "revision": REV, "ref": "main", "file": "m-Q4_K_M.gguf",
            "files": [{"path": "m-Q4_K_M.gguf", "size": 2000, "sha256": "1" * 64}]}
    return add_catalog("qwen-q4", meta, task="chat", fmt="gguf", source="hf://acme/gguf")


def deployment(node_id: int, model_id: int, name: str = "chat-q4", *, state: str = "running",
               desired: str = "running", final: bool = False) -> int:
    """A deployment with one replica placed on the machine, straight in the database."""
    from forgeyard.db import CatalogModel
    from forgeyard.models.catalog import model_block

    db = new_session()
    try:
        model = db.get(CatalogModel, model_id)
        w = Workload(name=name, kind="service", runtime="model", spec={"name": name}, desired_state=desired)
        db.add(w)
        db.flush()
        db.add(Deployment(name=name, model_id=model_id, recipe="llama-cpp", served_name=name, workload_id=w.id))
        db.add(Replica(workload_id=w.id, node_id=node_id, state=state, final=final, model=model_block(model)))
        db.commit()
        return w.id
    finally:
        db.close()


def stop(workload_id: int) -> None:
    db = new_session()
    try:
        w = db.get(Workload, workload_id)
        w.desired_state = "stopped"
        for r in w.replicas:
            r.state, r.final = "stopped", True
        db.commit()
    finally:
        db.close()


def in_thread(fn):
    out = {}
    t = threading.Thread(target=lambda: out.update(r=fn()))
    t.start()
    return t, out


def test_fy_cache_013_the_listing_with_catalog_models_and_users(owner):
    """FY-CACHE-013: each entry with its size, the catalog models it is, and the deployment there that
    uses it; the machine list carries no per-model detail; a plain user sees the list but no Remove."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        deployment(j["node_id"], catalog_q4())
        r = owner.get(f"/api/machines/{j['node_id']}/cache")
        assert r.status_code == 200, r.text
        c = r.json()
        assert c["sizes"] and c["connected"] and c["remove_note"] is None
        assert (c["free_bytes"], c["used_bytes"], c["partial_bytes"], c["partial_files"]) == (50 * 1024**3, 7700, 300, 1)
        g = next(e for e in c["models"] if e["key"] == KEY)
        assert g["catalog"] == [{"id": 1, "name": "qwen-q4"}] and [u["name"] for u in g["used_by"]] == ["chat-q4"]
        q4, q8 = g["parts"]
        assert q4["catalog"] == [{"id": 1, "name": "qwen-q4"}] and q4["used_by"][0]["kind"] == "deployment"
        assert q8["used_by"] == [] and q8["catalog"] == []
        assert c["engines"] == [{"key": ENGINE, "id": "e" * 16, "name": "llama-cpp", "bytes": 512, "used_by": []}]
        m = owner.get("/api/machines").json()["machines"][0]["model_cache"]
        assert "models" not in m and "engines_detail" not in m and m["cached"] == [KEY, Q4, Q8, URL]
        add_user(owner, "viewer", role="user")
        viewer = sign_in(owner, "viewer", "quiet-meadow-4821")
        seen = viewer.get(f"/api/machines/{j['node_id']}/cache").json()
        assert len(seen["models"]) == 2 and "operators" in seen["remove_note"]
        assert viewer.post(f"/api/machines/{j['node_id']}/cache/remove", json={"key": Q8}).status_code == 403


def test_fy_cache_014_refused_while_a_deployment_uses_it_then_removed_by_the_agent(owner):
    """FY-CACHE-014: 409 naming the deployment, and the agent is not even asked; once the deployment is
    stopped the agent gets cache_remove, its answer frees the space, the cache is stored as it reports
    it, and the removal is audited."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        wid = deployment(j["node_id"], catalog_q4())
        url = f"/api/machines/{j['node_id']}/cache/remove"
        for key in (Q4, KEY):
            r = owner.post(url, json={"key": key})
            assert r.status_code == 409 and r.json()["detail"]["code"] == "model_in_use", r.text
            assert "chat-q4" in r.json()["detail"]["message"] and r.json()["detail"]["in_use"][0]["name"] == "chat-q4"
        stop(wid)
        t, out = in_thread(lambda: owner.post(url, json={"key": Q4}))
        req = recv_type(ws, "cache_remove")
        assert req["key"] == Q4 and req["request_id"]
        after = reported(models=[{"key": KEY, "kind": "hf", "repo": "acme/gguf", "revision": REV, "bytes": 5000,
                                  "files": 2, "parts": [{"key": Q8, "file": "m-Q8_0.gguf", "bytes": 4000}]}],
                         cached=[KEY, Q8])
        ws.send_json({"v": 1, "type": "cache_removed", "request_id": req["request_id"], "ok": True,
                      "freed_bytes": 2000, "model_cache": after})
        t.join(10)
        r = out["r"]
        assert r.status_code == 200, r.text
        assert r.json()["freed_bytes"] == 2000 and [p["key"] for p in r.json()["cache"]["models"][0]["parts"]] == [Q8]
        db = new_session()
        try:
            ev = db.scalars(select(AuditEvent).where(AuditEvent.action == "model.cache_removed")).one()
            assert ev.target == "gpu-box" and ev.detail["key"] == Q4 and ev.detail["freed_bytes"] == 2000
            assert ev.detail["model"] == "acme/gguf · m-Q4_K_M.gguf"
        finally:
            db.close()
        assert owner.get("/api/machines").json()["machines"][0]["model_cache"]["cached"] == [KEY, Q8]


def test_fy_cache_015_refusals_and_failures(owner, monkeypatch):
    """FY-CACHE-015: a key the agent did not report is 404; the agent's own refusals (in use, a download
    in progress) and failures become 409 with its reason; an agent that does not answer, is not connected
    or is too old is 409 with what to do."""
    j = joined(owner)
    url = f"/api/machines/{j['node_id']}/cache/remove"
    with connect(owner, j["credential"]) as ws:
        online(ws)
        r = owner.post(url, json={"key": f"{KEY}/../../etc/passwd"})
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_in_cache"
        for code, reason, status, want in (("in_use", "In use by other-chat on this machine. Stop it first.", 409, "model_in_use"),
                                           ("busy", "A download of this model is in progress.", 409, "cache_busy"),
                                           ("not_found", "gone", 404, "not_in_cache"),
                                           ("failed", "could not remove it: disk on fire", 409, "cache_remove_failed")):
            t, out = in_thread(lambda: owner.post(url, json={"key": URL}))
            req = recv_type(ws, "cache_remove")
            ws.send_json({"v": 1, "type": "cache_removed", "request_id": req["request_id"], "ok": False, "code": code,
                          "reason": reason, "in_use": ["other-chat"] if code == "in_use" else []})
            t.join(10)
            d = out["r"].json()["detail"]
            assert out["r"].status_code == status and d["code"] == want and reason in d["message"], d
        monkeypatch.setattr(server_cache, "TIMEOUT", 0.3)
        r = owner.post(url, json={"key": URL})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "agent_no_answer"
        assert recv_type(ws, "cache_remove")["key"] == URL
    deadline = time.monotonic() + 10
    while owner.get("/api/machines").json()["machines"][0]["connected"] and time.monotonic() < deadline:
        time.sleep(0.05)
    r = owner.post(url, json={"key": URL})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "machine_offline"
    assert "not connected" in owner.get(f"/api/machines/{j['node_id']}/cache").json()["remove_note"]
    db = new_session()
    try:
        assert db.scalars(select(AuditEvent).where(AuditEvent.action == "model.cache_removed")).all() == []
    finally:
        db.close()


def test_fy_cache_016_older_agents_list_keys_without_sizes_and_cannot_remove(owner):
    """FY-CACHE-016: an agent from before 1.4 reports only cache keys: they are listed (with the catalog
    model and its users) without sizes, and Remove says to update the agent. An agent that reports sizes
    but does not list cache_admin is never asked."""
    j = joined(owner)
    old = {"ok": True, "free_bytes": 10, "total_bytes": 20, "cached": [KEY, Q4, URL]}
    with connect(owner, j["credential"]) as ws:
        online(ws, features=("workloads", "models"), model_cache=old)
        catalog_q4()
        c = owner.get(f"/api/machines/{j['node_id']}/cache").json()
        assert not c["sizes"] and "update the agent" in c["remove_note"]
        assert [(e["key"], e["bytes"]) for e in c["models"]] == [(KEY, None), (URL, None)]
        assert c["models"][0]["repo"] == "acme/gguf" and c["models"][0]["catalog"][0]["name"] == "qwen-q4"
        assert owner.post(f"/api/machines/{j['node_id']}/cache/remove", json={"key": KEY}).status_code == 404
    k = joined(owner, "newer")
    with connect(owner, k["credential"]) as ws:
        online(ws, features=("workloads", "models"))
        r = owner.post(f"/api/machines/{k['node_id']}/cache/remove", json={"key": URL})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "agent_too_old"
        assert "too old" in owner.get(f"/api/machines/{k['node_id']}/cache").json()["remove_note"]


def test_fy_cache_017_partial_downloads(owner):
    """FY-CACHE-017: "Remove partial downloads" sends cache_prune; the answer's cache is stored and the
    removal audited."""
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        t, out = in_thread(lambda: owner.post(f"/api/machines/{j['node_id']}/cache/remove-partial"))
        req = recv_type(ws, "cache_prune")
        ws.send_json({"v": 1, "type": "cache_removed", "request_id": req["request_id"], "ok": True, "freed_bytes": 300,
                      "model_cache": reported(partial_bytes=0, partial_files=0)})
        t.join(10)
        assert out["r"].status_code == 200 and out["r"].json()["cache"]["partial_bytes"] == 0
        db = new_session()
        try:
            ev = db.scalars(select(AuditEvent).where(AuditEvent.action == "model.cache_removed")).one()
            assert ev.detail["model"] == "partial downloads" and ev.detail["freed_bytes"] == 300
        finally:
            db.close()


def test_fy_cache_018_what_the_server_keeps_of_a_report():
    """FY-CACHE-018: entries whose key is not a cache key are dropped (never cut short), parts must be files
    of their revision, engines are engine keys; sizes are numbers; older reports keep their shape."""
    rep = reported()
    rep["models"] += [{"key": f"{KEY}/../x.gguf", "kind": "hf"}, {"key": ENGINE, "kind": "url"},
                      {"key": URL, "kind": "hf"}, {"key": "hub/models--a--b@" + "1" * 40, "kind": "hf", "bytes": "lots",
                                                   "parts": [{"key": f"{KEY}/m-Q4_K_M.gguf", "bytes": 1},
                                                             {"key": "hub/models--a--b@" + "1" * 40 + "/../../y.gguf"}]},
                      "junk"]
    rep["engines_detail"].append({"key": URL, "name": "x"})
    msg = StatusIn.model_validate({"v": 1, "type": "status", "model_cache": rep}).model_cache
    assert [e["key"] for e in msg["models"]] == [KEY, URL, "hub/models--a--b@" + "1" * 40]
    odd = msg["models"][2]
    assert odd["bytes"] == 0 and "parts" not in odd
    assert [e["key"] for e in msg["engines_detail"]] == [ENGINE]
    old = StatusIn.model_validate({"v": 1, "type": "status", "model_cache": {"ok": True, "cached": [KEY]}}).model_cache
    assert "models" not in old and "engines_detail" not in old
    ans = CacheRemovedIn.model_validate({"v": 1, "type": "cache_removed", "request_id": "r1", "ok": False,
                                         "reason": "x" * 1000, "in_use": ["a" * 100]})
    assert len(ans.reason) == 400 and len(ans.in_use[0]) == 64


def test_fy_cache_019_a_real_agent_lists_and_removes(monkeypatch, tmp_path):
    """FY-CACHE-019: over real HTTP and WebSocket: the agent says hello with cache_admin and its cache with
    sizes; Remove through the API deletes the files on its disk and the page shows it gone."""
    from forgeyard.agent.backoff import Backoff
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.conftest import OWNER, SETUP_TOKEN
    from tests.test_agent import LiveServer, _free_port, _http, _wait

    monkeypatch.setenv(SIMULATION_ENV, "1")
    root = tmp_path / "cache"
    cache = ModelCache(root)
    base = root / "hub" / "models--acme--gguf"
    (base / "blobs").mkdir(parents=True)
    (base / "snapshots" / REV).mkdir(parents=True)
    (base / ".forgeyard").mkdir()
    (base / "blobs" / ("9" * 64)).write_bytes(b"GGUF" + bytes(4092))
    (base / "snapshots" / REV / "m-Q4_K_M.gguf").symlink_to(f"../../blobs/{'9' * 64}")
    (base / ".forgeyard" / f"{REV}.json").write_text(json.dumps({"repo": "acme/gguf", "revision": REV,
                                                                 "files": [{"path": "m-Q4_K_M.gguf", "size": 4096}]}))
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    state = tmp_path / "agent"
    with LiveServer(port):
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        from forgeyard.agent.client import join

        joined_ = join(server, _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"], name="cpu")
        mid = joined_["node_id"]

        def api(method, path, body=None):
            return _http(port, method, path, body, cookie)[0]

        async def scenario():
            rt = ProcessRuntime(RecordStore(state), tmp_path / "work", euid=lambda: 4242)
            sup = Supervisor(state, {"process": rt}, models=cache, tick=0.1, port_range=(31700, 31710))
            agent = Agent(server, joined_["credential"], SimulatedInventory("nvidia-rtx4090x2"),
                          backoff=Backoff(base=0.05, cap=0.2), supervisor=sup)
            run = asyncio.create_task(agent.run())
            try:
                assert await _wait(lambda: "cache_admin" in api("GET", "/api/machines")["machines"][0]["features"])
                listing = await asyncio.to_thread(api, "GET", f"/api/machines/{mid}/cache")
                [entry] = listing["models"]
                assert entry["key"] == KEY and entry["bytes"] == 4096 and listing["remove_note"] is None
                done = await asyncio.to_thread(api, "POST", f"/api/machines/{mid}/cache/remove", {"key": Q4})
                assert done["freed_bytes"] == 4096 and done["cache"]["models"] == []
                assert not base.exists()
            finally:
                agent.stop()
                await asyncio.wait_for(run, 10)

        asyncio.run(scenario())
