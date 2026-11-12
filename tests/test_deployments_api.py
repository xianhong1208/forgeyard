"""Catalog, recipes, sources and deployments through the API; who may do what; the token; the
desired state an agent gets; routes and "try it" (FY-DEP-*, FY-MROLE-*)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select

from forgeyard.config import get_settings
from forgeyard.db import AuditEvent, Deployment, GatewayRoute, Node, Replica, new_session
from forgeyard.machines.messages import StatusIn
from forgeyard.models import routes
from forgeyard.workloads import state as wstate
from tests.conftest import add_user, sign_in
from tests.model_helpers import FakeHF, gguf_repo, qwen4b_repo, tiny_repo
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

PW = "quiet-meadow-4821"


@pytest.fixture()
def hf(monkeypatch):
    server = FakeHF(qwen4b_repo(), gguf_repo(), tiny_repo(), tiny_repo("acme/gated", gated=True))
    monkeypatch.setenv("HF_ENDPOINT", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    yield server
    server.close()


def audit(action):
    db = new_session()
    try:
        return [e.detail | {"target": e.target} for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action))]
    finally:
        db.close()


def test_fy_mrole_001_who_may_do_what(owner):
    """FY-MROLE-001: users look (catalog, recipes, deployments, routes); operators deploy but do not
    manage the catalog, recipes or sources; admins do everything."""
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    add_user(owner, "olga", role="operator")
    add_user(owner, "ursula", role="user")
    op, user = sign_in(owner, "olga", PW), sign_in(owner, "ursula", PW)
    body = {"name": "embed", "model": "qwen3-embedding-0.6b", "recipe": "vllm-pooling"}
    for c in (op, user):
        assert c.get("/api/models").status_code == 200 and c.get("/api/recipes").status_code == 200
        for method, path, payload in [("post", "/api/models", {"name": "x", "source": "hf://a/b"}),
                                      ("post", "/api/models/inspect", {"source": "hf://a/b"}),
                                      ("post", "/api/recipes", {"yaml": "name: x"}),
                                      ("put", "/api/model-sources", {"url_hosts": []}),
                                      ("get", "/api/model-sources", None), ("delete", "/api/models/1", None)]:
            r = getattr(c, method)(path, **({"json": payload} if payload is not None else {}))
            assert r.status_code == 403, (path, r.status_code)
    assert user.post("/api/deployments/estimate", json=body).status_code == 403
    assert user.post("/api/deployments", json=body).status_code == 403
    d = op.post("/api/deployments", json=body)
    assert d.status_code == 201
    dep_id = d.json()["deployment"]["id"]
    assert user.get("/api/deployments").json()["can_manage"] is False
    assert user.get(f"/api/deployments/{dep_id}").status_code == 200 and user.get("/api/routes").status_code == 200
    assert user.post(f"/api/deployments/{dep_id}/try", json={}).status_code == 409  # allowed, but nothing is ready
    for path in (f"/api/deployments/{dep_id}/stop", f"/api/deployments/{dep_id}/start"):
        assert user.post(path).status_code == 403
    assert user.delete(f"/api/deployments/{dep_id}").status_code == 403
    assert op.delete(f"/api/deployments/{dep_id}").status_code == 200
    listing = user.get("/api/models").json()
    assert listing["can_manage"] is False and listing["can_deploy"] is False and "sources" not in listing
    assert op.get("/api/models").json()["can_deploy"] is True


def test_fy_dep_001_catalog_add_inspect_remove(owner, hf):
    """FY-DEP-001: an admin inspects and adds models (pinned to the commit), the catalog shows
    which recipes fit, a deployed model cannot be removed; everything is audited without tokens."""
    r = owner.post("/api/models/inspect", json={"source": "hf://Qwen/Qwen3-4B"})
    assert r.status_code == 200 and r.json()["recipes"] == ["vllm"] and r.json()["meta"]["file_count"] == 12
    m = owner.post("/api/models", json={"name": "qwen3-4b", "source": "hf://Qwen/Qwen3-4B"}).json()["model"]
    assert m["revision"] == "1cfa9a7208912126459214e8b04321603b3df60c" and m["task"] == "chat"
    assert m["summary"]["params"] == 4022468096 and m["recipes"] == ["vllm"]
    assert owner.post("/api/models", json={"name": "qwen3-4b", "source": "hf://Qwen/Qwen3-4B"}).status_code == 409
    gg = owner.post("/api/models", json={"name": "q17", "source": "hf://ggml-org/Qwen3-1.7B-GGUF"}).json()["model"]
    assert gg["file"] == "Qwen3-1.7B-Q4_K_M.gguf" and gg["recipes"] == ["llama-cpp"]
    r = owner.post("/api/models", json={"name": "gated", "source": "hf://acme/gated", "task": "chat"})
    assert r.status_code == 422 and "gated" in r.json()["detail"]["message"]
    r = owner.post("/api/models", json={"name": "gated", "source": "hf://acme/gated", "task": "chat",
                                        "token": FakeHF.TOKEN})
    assert r.status_code == 201
    assert FakeHF.TOKEN not in json.dumps(owner.get("/api/models").json())
    assert FakeHF.TOKEN not in json.dumps(audit("model.added"))
    for bad, code in [({"name": "Bad Name!", "source": "hf://a/b"}, "bad_name"),
                      ({"name": "x", "source": "file:///etc/passwd"}, "invalid_source"),
                      ({"name": "x", "source": "hf://acme/tiny-chat", "task": "dance"}, "task_required")]:
        assert owner.post("/api/models", json=bad).json()["detail"]["code"] == code
    make_node("a100", allowlist=[4], features=MODELS)
    deploy(owner, "chat", "qwen3-4b", "vllm", params={"context": 4096})
    r = owner.delete(f"/api/models/{m['id']}")
    assert r.status_code == 409 and "chat" in r.json()["detail"]["message"]
    assert owner.patch(f"/api/models/{m['id']}", json={"title": "Qwen3 4B"}).json()["model"]["title"] == "Qwen3 4B"
    assert [a["target"] for a in audit("model.added")] == ["qwen3-4b", "q17", "gated"]


def test_fy_dep_002_url_sources_only_from_allowed_hosts(owner, hf):
    """FY-DEP-002: a GGUF URL is refused until its host is on the admin allowlist (audited)."""
    url = f"{hf.url}/ggml-org/Qwen3-1.7B-GGUF/resolve/daeb8e2d528a760970442092f6bf1e55c3b659eb/Qwen3-1.7B-Q4_K_M.gguf"
    r = owner.post("/api/models/inspect", json={"source": url})
    assert r.status_code == 422 and "not on the allowlist" in r.json()["detail"]["message"]
    for bad in (["not a host"], ["*"]):
        assert owner.put("/api/model-sources", json={"url_hosts": bad}).status_code == 422
    assert owner.put("/api/model-sources", json={"local_dirs": ["/"]}).status_code == 422
    r = owner.put("/api/model-sources", json={"url_hosts": ["127.0.0.1.nip.io", "models.example.org"],
                                              "local_dirs": ["/srv/models/"]})
    assert r.json()["sources"]["local_dirs"] == ["/srv/models"]
    assert audit("model_sources.changed")[0]["after"]["url_hosts"] == ["127.0.0.1.nip.io", "models.example.org"]
    r = owner.post("/api/models", json={"name": "local", "source": "local:///etc/secrets", "format": "gguf",
                                        "file": "x.gguf", "task": "chat"})
    assert r.status_code == 422 and "not under a directory allowed" in r.json()["detail"]["message"]
    r = owner.post("/api/models", json={"name": "local", "source": "local:///srv/models/q", "format": "gguf",
                                        "file": "q.gguf", "task": "chat"})
    assert r.status_code == 201 and r.json()["model"]["kind"] == "local"


def test_fy_dep_003_deployment_expands_into_workload_and_route(owner):
    """FY-DEP-003: one deployment = a service workload (runtime "model") + a gateway route; the
    workloads API cannot create model workloads; deleting either removes both."""
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    d = deploy(owner, "asr", "faster-whisper-small", "faster-whisper", served_name="whisper-small")["deployment"]
    assert d["route"] == {"served_name": "whisper-small", "api": "openai-audio",
                          "upstream_model": "Systran/faster-whisper-small"}
    w = d["workload"]
    assert w["kind"] == "service" and w["runtime"] == "model" and w["spec"]["runtime"]["type"] == "model"
    runs = w["replica_list"][0]["runs"]
    assert runs["type"] == "container" and runs["command"] is None
    assert runs["image"].startswith("ghcr.io/speaches-ai/speaches:0.8.3-cuda@sha256:")  # what the agent runs
    bad = {"name": "x", "kind": "service", "runtime": {"type": "model", "recipe": "vllm", "model": "qwen3-4b"}}
    assert owner.post("/api/workloads", json=bad).json()["detail"]["message"].startswith("runtime: model workloads")
    assert owner.post("/api/deployments", json={"name": "asr2", "model": "faster-whisper-small", "recipe": "faster-whisper",
                                                "served_name": "whisper-small"}).status_code == 409
    assert owner.post("/api/deployments", json={"name": "asr", "model": "faster-whisper-small",
                                                "recipe": "faster-whisper"}).status_code == 409
    assert owner.delete(f"/api/workloads/{w['id']}").status_code == 200
    db = new_session()
    try:
        assert db.scalars(select(Deployment)).all() == [] and db.scalars(select(GatewayRoute)).all() == []
    finally:
        db.close()
    d2 = deploy(owner, "chat", "qwen3-4b", "vllm", params={"context": 4096})["deployment"]
    assert owner.delete(f"/api/deployments/{d2['id']}").status_code == 200
    assert owner.get("/api/workloads").json()["workloads"] == []
    assert [a["target"] for a in audit("deployment.created")] == ["asr", "chat"] and audit("deployment.deleted")


@pytest.mark.parametrize("body,code,msg", [
    ({"recipe": "vllm", "params": {"gpus": 0}}, "invalid_params", "GPUs only"),
    ({"recipe": "vllm", "params": {"temperature": 1}}, "invalid_params", "no parameter 'temperature'"),
    ({"recipe": "vllm", "params": {"context": 100000}}, "cannot_estimate", "at most 40960"),
    ({"recipe": "vllm", "params": {"share": "half"}}, "invalid_params", "share"),
    ({"recipe": "vllm", "params": {"extra_args": ["--x\n--y"]}}, "invalid_params", "one line"),
    ({"recipe": "llama-cpp"}, "incompatible", "reads gguf"),
    ({"recipe": "nope"}, "recipe_not_found", "No recipe"),
    ({"recipe": "vllm", "served_name": "bad name"}, "bad_served_name", "served_name"),
])
def test_fy_dep_004_parameters_are_checked(owner, body, code, msg):
    """FY-DEP-004"""
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    r = owner.post("/api/deployments", json={"name": "chat", "model": "qwen3-4b", **body})
    assert r.json()["detail"]["code"] == code and msg in r.json()["detail"]["message"]


def test_fy_dep_005_token_reaches_only_the_agent(owner):
    """FY-DEP-005: a deployment's Hugging Face token is never returned or audited; it is in the
    desired state of the machine that downloads, and nowhere else."""
    seed_catalog()
    a100 = make_node("a100", allowlist=[4], features=MODELS)
    other = make_node("other", allowlist=[4], features=MODELS)
    d = deploy(owner, "chat", "qwen3-4b", "vllm", params={"context": 4096}, machine_id=a100,
               hf_token="hf_secret_value_123")["deployment"]
    assert d["has_token"] is True
    for text in (json.dumps(owner.get("/api/deployments").json()), json.dumps(owner.get(f"/api/deployments/{d['id']}").json()),
                 json.dumps(audit("deployment.created")), json.dumps(owner.get("/api/workloads").json())):
        assert "hf_secret_value_123" not in text
    db = new_session()
    try:
        msg = wstate.desired_message(db, a100, 1)
        assert msg["replicas"][0]["model"]["token"] == "hf_secret_value_123"
        assert wstate.desired_message(db, other, 1)["replicas"] == []
        assert "token" not in db.scalar(select(Replica)).model
    finally:
        db.close()


def test_fy_dep_006_desired_state_for_new_and_old_agents(owner):
    """FY-DEP-006: an M3 agent gets the rendered spec, the model and the engine; an M2 agent gets
    specs without the new fields its spec model would refuse."""
    seed_catalog()
    cpu = make_node("cpu", features=MODELS, runtime_info={"process": {"ok": True}, "container": {"ok": False}})
    deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    old = make_node("m2", features=("workloads",))
    owner.post("/api/workloads", json={"name": "web", "kind": "service", "machine_id": old,
                                       "runtime": {"type": "process", "command": "true"}, "ports": [{"name": "http"}],
                                       "health": {"type": "tcp"}})
    db = new_session()
    try:
        rep = wstate.desired_message(db, cpu, 1)["replicas"][0]
        assert rep["spec"]["runtime"]["type"] == "process" and rep["engine"]["binary"] == "llama-b12242/llama-server"
        assert rep["model"]["file"] == "Qwen3-1.7B-Q4_K_M.gguf" and rep["spec"]["health"]["start_period"] == 600
        m2 = wstate.desired_message(db, old, 1)["replicas"][0]["spec"]
        assert "vram" not in m2["resources"]["gpus"] and "share" not in m2["resources"]["gpus"]
        assert "start_period" not in m2["health"]
        from forgeyard.workloads.spec import WorkloadSpec
        WorkloadSpec.model_validate(m2)
    finally:
        db.close()


def test_fy_dep_007_custom_recipes(owner):
    """FY-DEP-007: admins add, change and remove their own recipes; built-in ones are fixed; an
    invalid recipe is refused with the reason; a recipe in use cannot go."""
    text = open("forgeyard/recipes/faster-whisper.yaml").read().replace("name: faster-whisper", "name: my-whisper")
    r = owner.post("/api/recipes", json={"yaml": text})
    assert r.status_code == 201 and r.json()["recipe"]["builtin"] is False
    assert owner.post("/api/recipes", json={"yaml": text}).status_code == 409
    assert owner.post("/api/recipes", json={"yaml": text.replace("name: my-whisper", "name: vllm")}).json()["detail"]["code"] == "builtin_name"
    bad = owner.post("/api/recipes", json={"yaml": text.replace("my-whisper", "w2").replace("{port}", "{prot}")})
    assert bad.status_code == 422 and "unknown placeholder {prot}" in bad.json()["detail"]["message"]
    assert owner.put("/api/recipes/my-whisper", json={"yaml": text.replace("0.8.3-cpu", "0.8.2-cpu")}).status_code == 200
    assert owner.put("/api/recipes/vllm", json={"yaml": text}).status_code == 409
    assert owner.put("/api/recipes/my-whisper", json={"yaml": text.replace("name: my-whisper", "name: other")}).status_code == 422
    assert owner.delete("/api/recipes/vllm").status_code == 409
    names = [x["name"] for x in owner.get("/api/recipes").json()["recipes"]]
    assert names == ["faster-whisper", "llama-cpp", "speaches-tts", "vllm", "vllm-omni-tts", "vllm-pooling", "my-whisper"]
    seed_catalog()
    make_node("a100", allowlist=[4], features=MODELS)
    deploy(owner, "asr", "faster-whisper-small", "my-whisper")
    assert owner.delete("/api/recipes/my-whisper").status_code == 409
    assert [a["target"] for a in audit("recipe.created")] == ["my-whisper"] and audit("recipe.updated")


class _Engine(BaseHTTPRequestHandler):
    """A stand-in for a ready replica: answers the four protocols."""
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Engine.seen.append((self.path, self.headers.get("Content-Type"), body))
        if self.path == "/v1/chat/completions":
            out = {"choices": [{"message": {"role": "assistant", "content": " A GPU is a processor. "}}]}
        elif self.path == "/v1/embeddings":
            out = {"data": [{"embedding": [0.1, -0.2, 0.3, 0.4, 0.5]}]}
        elif self.path == "/v1/rerank":
            out = {"results": [{"index": 1, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.98}]}
        else:
            out = {"text": "And so, my fellow Americans, ask not what your country can do for you."}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_fy_dep_008_try_it_and_routes(owner):
    """FY-DEP-008: "try it" sends one small request in the route's protocol to a ready replica's own
    port; routes list only ready replicas (what the M4 gateway will balance over)."""
    seed_catalog()
    a100 = make_node("a100", allowlist=[4], features=MODELS)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        deps = {}
        for name, model, recipe in [("chat", "qwen3-4b", "vllm"), ("embed", "qwen3-embedding-0.6b", "vllm-pooling"),
                                    ("rerank", "qwen3-reranker-0.6b", "vllm-pooling"),
                                    ("asr", "faster-whisper-small", "faster-whisper")]:
            params = {"share": "vram", "context": 4096} if recipe == "vllm" else {}
            deps[name] = deploy(owner, name, model, recipe, params=params)["deployment"]
        assert routes.all_routes(new_session())[0]["endpoints"] == []
        db = new_session()
        try:
            node = db.get(Node, a100)
            for r in db.scalars(select(Replica)).all():
                msg = StatusIn.model_validate({"v": 1, "type": "status", "replicas": [{
                    "id": r.uid, "generation": 1, "state": "healthy",
                    "ports": [{"name": "http", "port": 8000, "host_port": port, "address": "127.0.0.1"}]}]})
                wstate.apply_status(db, node, msg)
            db.commit()
            resolved = routes.resolve(db, "asr")
            assert resolved["api"] == "openai-audio" and resolved["upstream_model"] == "Systran/faster-whisper-small"
            assert resolved["endpoints"] == [{"replica": 0, "machine": "a100", "state": "healthy", "ready": True,
                                              "url": f"http://127.0.0.1:{port}/"}]
            assert routes.resolve(db, "nothing") is None
        finally:
            db.close()
        out = owner.post(f"/api/deployments/{deps['chat']['id']}/try", json={"input": "Hi"}).json()
        assert out["ok"] and out["summary"] == "A GPU is a processor." and out["request"]["model"] == "chat"
        assert owner.post(f"/api/deployments/{deps['embed']['id']}/try", json={}).json()["summary"].startswith("5 dimensions")
        assert owner.post(f"/api/deployments/{deps['rerank']['id']}/try", json={}).json()["summary"] == "#0 0.980; #1 0.100"
        asr = owner.post(f"/api/deployments/{deps['asr']['id']}/try", json={}).json()
        assert asr["summary"].startswith("And so, my fellow Americans")
        path, ctype, body = _Engine.seen[-1]
        assert path == "/v1/audio/transcriptions" and ctype.startswith("multipart/form-data")
        assert b"Systran/faster-whisper-small" in body and b"jfk-ask-not.mp3" in body
        listing = owner.get("/api/routes").json()["routes"]
        assert [r["served_name"] for r in listing] == ["asr", "chat", "embed", "rerank"]
        assert all(len(r["endpoints"]) == 1 for r in listing)
    finally:
        srv.shutdown()
        srv.server_close()
