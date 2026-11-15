"""Placement that knows what a machine can fetch (1.4, #56; FY-FETCH-*): the model, engine archive or
image already on a machine counts ahead of binpack; a machine that could not reach a source is
remembered for an hour and avoided for replicas that would fetch from it; a replica in backoff
because of such a failure is placed again elsewhere (unpinned deployments only); the page says why."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard.db import Node, Replica, Workload, new_session, utcnow
from forgeyard.machines import agent_api
from forgeyard.machines.hub import hub
from forgeyard.machines.messages import StatusIn
from forgeyard.models import reach, scheduler
from tests.model_helpers import GGUF_REV
from tests.test_scheduler import MODELS, deploy, reps, seed_catalog
from tests.workload_helpers import PROCESS_OK, make_node

ENGINE_SHA = "0bc1f091e8916102b6d8f2cf944233a620e90a0f856c72d0ce41150c65c2e72e"
ENGINE_FILE = "llama-b12359-bin-ubuntu-x64.tar.gz"
MODEL_FILE = "Qwen3-1.7B-Q4_K_M.gguf"
MODEL_KEY = f"hub/models--ggml-org--Qwen3-1.7B-GGUF@{GGUF_REV}/{MODEL_FILE}"
NO_DOCKER = {**PROCESS_OK, "container": {"ok": False, "reason": "docker not found"}}
DNS = f"{MODEL_FILE}: download failed: cannot resolve huggingface.co: Temporary failure in name resolution; restarting in 2 s"


def cpu(name, runtime_info=None):
    return make_node(name, "nvidia-rtx4090x2", features=MODELS, runtime_info=runtime_info or NO_DOCKER)


def set_node(node_id, **values):
    db = new_session()
    try:
        node = db.get(Node, node_id)
        for k, v in values.items():
            setattr(node, k, v)
        db.commit()
    finally:
        db.close()


def failure(host="huggingface.co", phase="model", at=None):
    return reach.Failure(host, phase, "cannot resolve", at or utcnow())


def rows(name):
    db = new_session()
    try:
        return list(db.scalars(select(Replica).join(Workload).where(Workload.name == name).order_by(Replica.ordinal)))
    finally:
        db.close()


def report(node_id, r, state, reason):
    """The agent of node_id reports replica r, through the server's own status handler."""
    db = new_session()
    try:
        msg = StatusIn.model_validate({"v": 1, "type": "status", "replicas": [
            {"id": r.uid, "generation": r.generation, "state": state, "reason": reason}]})
        return agent_api._status(db, node_id, msg)
    finally:
        db.close()


def events(c, dep):
    return [e["message"] for e in c.get(f"/api/deployments/{dep['deployment']['id']}").json()["events"]]


@pytest.fixture()
def poked(monkeypatch):
    seen = []
    monkeypatch.setattr(hub, "poke", lambda node_id: seen.append(node_id) or False)
    return seen


def test_fy_fetch_001_network_failures_are_told_from_other_failures():
    """FY-FETCH-001: a download that could not reach its source (DNS, refused, timeout, unreachable;
    an image pull that could not reach the registry) is a network failure of that host and phase; an
    HTTP error, a refused token, a checksum or a full disk is not."""
    r = Replica(engine={"url": f"https://github.com/ggml-org/llama.cpp/releases/download/b1/{ENGINE_FILE}",
                        "sha256": ENGINE_SHA},
                model={"kind": "hf", "repo": "ggml-org/Qwen3-1.7B-GGUF", "endpoint": "https://huggingface.co"},
                spec={"runtime": {"type": "process"}})
    f = reach.classify(r, f"{ENGINE_FILE}: download failed: cannot resolve github.com: Name or service not known; "
                          "restarting in 4 s")
    assert (f.host, f.phase, f.detail) == ("github.com", "engine", "cannot resolve github.com: Name or service not known")
    assert reach.classify(r, DNS).host == "huggingface.co" and reach.classify(r, DNS).phase == "model"
    for why in ("huggingface.co: Connection refused", "cas-bridge.xethub.hf.co: timed out",
                "huggingface.co: Network is unreachable", "huggingface.co: No route to host"):
        assert reach.classify(r, f"{MODEL_FILE}: download failed: {why}; restarting in 8 s").phase == "model", why
    for why in ("HTTP 404", "HTTP 503", "incomplete (5 of 9 bytes)"):
        assert reach.classify(r, f"{MODEL_FILE}: download failed: {why}; restarting in 8 s") is None, why
    for other in (f"{MODEL_FILE}: access refused (HTTP 401); a gated model needs a Hugging Face token",
                  f"{MODEL_FILE}: SHA-256 mismatch (got abc…, expected def…)",
                  "not enough disk space in the model cache: needs 1.2 GiB (+1 GiB margin), 0.1 GiB free",
                  "exited with code 1; restarting in 2 s", None):
        assert reach.classify(r, other) is None, other
    img = Replica(spec={"runtime": {"type": "container", "image": "ghcr.io/ggml-org/llama.cpp:server@sha256:" + "a" * 64}})
    pulled = reach.classify(img, "the image is not on this machine and could not be pulled; on an offline machine, docker "
                                 "load the image. dial tcp: lookup ghcr.io on 127.0.0.53:53: no such host; restarting in 2 s")
    assert (pulled.host, pulled.phase) == ("ghcr.io", "image")
    assert reach.classify(img, "the pull of x stalled: no progress for 300 s; restarting in 2 s") is None
    assert [reach.registry(i) for i in ("python:3.13-slim", "vllm/vllm-openai:v1", "localhost:5000/x:1",
                                        "registry.example.org:8443/a/b")] == ["docker.io", "docker.io", "localhost",
                                                                              "registry.example.org"]
    assert reach.model_host({"kind": "url", "url": "https://files.example.org/m.gguf"}) == "files.example.org"
    assert reach.model_host({"kind": "local", "path": "/models/x"}) is None


def test_fy_fetch_002_a_failure_is_remembered_for_an_hour_per_machine_and_host():
    """FY-FETCH-002: remembered per machine and host, forgotten after TTL (one hour)."""
    now = utcnow()
    reach.record(1, failure(at=now))
    assert reach.failed(1, "huggingface.co", now + timedelta(minutes=59)) is not None
    assert reach.failed(2, "huggingface.co", now) is None and reach.failed(1, "github.com", now) is None
    assert reach.failed(1, "huggingface.co", now + reach.TTL) is None
    assert reach.failed(1, "huggingface.co", now) is None  # gone for good once expired


def test_fy_fetch_003_a_machine_that_could_not_reach_the_source_is_avoided(owner):
    """FY-FETCH-003: cpu-a would win by name, but it could not reach huggingface.co: the replica goes
    to cpu-b and the page says why. Once cpu-a has the model in its cache it needs no download, and
    the failure no longer matters there."""
    seed_catalog()
    a = cpu("cpu-a")
    cpu("cpu-b")
    reach.record(a, failure())
    d = deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    r = reps(owner, d)[0]
    assert r["machine"]["name"] == "cpu-b"
    assert r["placement_note"] == "cpu-a could not reach huggingface.co; placed on cpu-b instead"
    assert any("cpu-a could not reach huggingface.co; placed on cpu-b instead." in m for m in events(owner, d))
    machines = {m["name"]: m for m in d["placement"]["machines"]}
    assert machines["cpu-a"]["unreachable"] == "cpu-a could not reach huggingface.co"
    assert machines["cpu-a"]["needs"] == ["model", "engine"] and "unreachable" not in machines["cpu-b"]
    set_node(a, model_cache={"ok": True, "cached": [MODEL_KEY]})
    d2 = deploy(owner, "small2", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    r2 = reps(owner, d2)[0]
    assert r2["machine"]["name"] == "cpu-a" and r2["placement_note"] is None


def test_fy_fetch_004_with_no_other_machine_it_still_goes_there_and_says_so(owner):
    """FY-FETCH-004: a failure makes a machine the last choice, never no choice."""
    seed_catalog()
    a = cpu("cpu-a")
    reach.record(a, failure())
    d = deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    r = reps(owner, d)[0]
    assert r["machine"]["name"] == "cpu-a"
    assert r["placement_note"] == ("cpu-a could not reach huggingface.co just now; no other machine can take it, so it "
                                   "tries there again")


def test_fy_fetch_005_a_replica_stuck_on_a_failed_download_is_placed_again(owner, poked):
    """FY-FETCH-005 (the issue's case): two replicas spread over cpu-a and cpu-b, cpu-b already has the
    model. The one on cpu-a cannot reach huggingface.co and goes to backoff: it is taken off cpu-a
    (whose agent is poked to drop it) and placed on cpu-b, which says why."""
    seed_catalog()
    a, b = cpu("cpu-a"), cpu("cpu-b")
    set_node(b, model_cache={"ok": True, "cached": [MODEL_KEY]})
    d = deploy(owner, "pair", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0}, replicas=2)
    assert sorted(r["machine"]["name"] for r in reps(owner, d)) == ["cpu-a", "cpu-b"]
    on_a = next(r for r in rows("pair") if r.node_id == a)
    poked.clear()
    assert report(a, on_a, "backoff", DNS) is True
    assert a in poked and b in poked
    moved = next(r for r in reps(owner, d) if r["ordinal"] == on_a.ordinal)
    assert moved["machine"]["name"] == "cpu-b" and moved["id"] == on_a.uid
    assert moved["placement_note"] == "cpu-a could not reach huggingface.co; placed on cpu-b instead"
    assert moved["state"] == "pending" and moved["reason"] is None
    text = events(owner, d)
    assert any(m.startswith(f"Replica {on_a.ordinal}: cpu-a could not reach huggingface.co: cannot resolve") for m in text)
    assert reach.failed(a, "huggingface.co") is not None
    # A late report from cpu-a's agent about the replica it no longer has changes nothing.
    assert report(a, on_a, "backoff", DNS) is False
    assert next(r for r in rows("pair") if r.ordinal == on_a.ordinal).node_id == b


def test_fy_fetch_006_pinned_stays_and_says_why_once(owner, poked):
    """FY-FETCH-006: a pinned deployment's replica stays where it is pinned and keeps retrying; the
    page says why (one event, not one per retry)."""
    seed_catalog()
    a, _ = cpu("cpu-a"), cpu("cpu-b")
    d = deploy(owner, "pinned", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0}, machine_id=a)
    r = rows("pinned")[0]
    assert report(a, r, "backoff", DNS) is False
    assert report(a, r, "backoff", DNS.replace("in 2 s", "in 4 s")) is False
    rep = reps(owner, d)[0]
    assert rep["machine"]["name"] == "cpu-a" and rep["state"] == "backoff"
    note = "cpu-a could not reach huggingface.co; the deployment is pinned to cpu-a, so the replica stays there and tries again"
    assert rep["placement_note"] == note
    assert sum(1 for m in events(owner, d) if note in m) == 1
    # Pinned to a machine already known not to reach the source: placed there anyway, and it says so.
    d2 = deploy(owner, "pinned2", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0}, machine_id=a)
    assert reps(owner, d2)[0]["placement_note"] == ("cpu-a could not reach huggingface.co just now; the deployment is "
                                                    "pinned to cpu-a, so it tries there anyway")


def test_fy_fetch_007_other_failures_and_no_alternative_leave_the_replica_where_it_is(owner, poked):
    """FY-FETCH-007: an HTTP error is not remembered and moves nothing; a network failure with no
    other machine that fits keeps the replica there, saying so."""
    seed_catalog()
    a = cpu("cpu-a")
    d = deploy(owner, "one", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    r = rows("one")[0]
    assert report(a, r, "backoff", f"{MODEL_FILE}: download failed: HTTP 404; restarting in 2 s") is False
    assert reach.failures(a) == [] and reps(owner, d)[0]["placement_note"] is None
    assert report(a, r, "backoff", DNS) is False
    rep = reps(owner, d)[0]
    assert rep["machine"]["name"] == "cpu-a"
    assert rep["placement_note"] == "cpu-a could not reach huggingface.co; no other machine can take it, so it tries again on cpu-a"


def test_fy_fetch_008_a_machine_runs_the_variant_it_can_fetch(owner):
    """FY-FETCH-008: cpu-a has Docker but could not reach ghcr.io: it gets the process variant (its
    archive comes from github.com) instead of the container one, and is not avoided for it. Without
    the failure the container variant comes first, as before."""
    seed_catalog()
    a = cpu("cpu-a", runtime_info=PROCESS_OK)
    est = owner.post("/api/deployments/estimate", json={"name": "x", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp",
                                                        "params": {"gpus": 0}}).json()["placement"]
    assert est["placed"][0]["variant"] == "cpu" and est["placed"][0]["needs"] == ["model", "image"]
    reach.record(a, failure("ghcr.io", "image"))
    est = owner.post("/api/deployments/estimate", json={"name": "x", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp",
                                                        "params": {"gpus": 0}}).json()["placement"]
    assert est["placed"][0]["variant"] == "cpu-process" and "unreachable" not in est["placed"][0]
    reach.record(a, failure("github.com", "engine"))  # neither works: the first variant, marked
    est = owner.post("/api/deployments/estimate", json={"name": "x", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp",
                                                        "params": {"gpus": 0}}).json()["placement"]
    assert est["placed"][0]["variant"] == "cpu" and est["placed"][0]["unreachable"].startswith("cpu-a could not reach ghcr.io")


def test_fy_fetch_009_engines_and_images_on_a_machine_count(owner, tmp_path):
    """FY-FETCH-009: the agent lists the engine archives it unpacked (1.4); the server keeps them, and
    an image counts as there once a replica started from it. A machine with the engine (neither has the
    model) wins over a better binpack fit."""
    from forgeyard.agent.models import ModelCache
    from forgeyard.machines.messages import _model_cache

    cache = ModelCache(tmp_path / "cache")
    (cache.root / "engines" / f"llama.cpp-b12359-{ENGINE_SHA[:16]}").mkdir()
    (cache.root / "engines" / f".tmp-{ENGINE_SHA[:16]}-77").mkdir()
    info = cache.info()
    assert info["engines"] == [ENGINE_SHA[:16]]
    assert _model_cache({**info, "engines": [ENGINE_SHA[:16], "../x", 5]})["engines"] == [ENGINE_SHA[:16]]
    assert "engines" not in _model_cache({"ok": True, "cached": []})
    seed_catalog()
    cpu("cpu-a")
    b = cpu("cpu-b")
    set_node(b, model_cache={"ok": True, "cached": [], "engines": [ENGINE_SHA[:16]]})
    d = deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    assert reps(owner, d)[0]["machine"]["name"] == "cpu-b"
    assert d["placement"]["placed"][0]["needs"] == ["model"]
    # Images: seen when a replica on that machine is ready.
    c = cpu("cpu-c", runtime_info=PROCESS_OK)
    db = new_session()
    try:
        node = db.get(Node, c)
        fake = Replica(spec={"runtime": {"type": "container", "image": "ghcr.io/x/y:1"}})
        assert reach.has_image(node, "ghcr.io/x/y:1") is False
        reach.seen(c, fake)
        assert reach.has_image(node, "ghcr.io/x/y:1") is True
        assert reach.needs(node, cached=True, model=None, engine=None, image="ghcr.io/x/y:1") == []
    finally:
        db.close()


def test_fy_fetch_010_a_ready_replica_marks_its_engine_present(owner, poked):
    """FY-FETCH-010: when a replica reports ready, its engine archive (old agents do not list them) is
    counted as on that machine for later placements; a stopped deployment is never moved."""
    seed_catalog()
    a = cpu("cpu-a")
    deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})
    r = rows("small")[0]
    db = new_session()
    try:
        assert reach.has_engine(db.get(Node, a), r.engine) is False
    finally:
        db.close()
    report(a, r, "running", None)  # not ready yet: it has a health check
    db = new_session()
    try:
        assert reach.has_engine(db.get(Node, a), r.engine) is False
    finally:
        db.close()
    report(a, r, "healthy", None)
    db = new_session()
    try:
        assert reach.has_engine(db.get(Node, a), r.engine) is True
    finally:
        db.close()
    # Stopping: a backoff report of a replica being stopped is not a reason to move it.
    cpu("cpu-b")
    dep_id = owner.get("/api/deployments").json()["deployments"][0]["id"]
    assert owner.post(f"/api/deployments/{dep_id}/stop").status_code == 200
    assert report(a, r, "backoff", DNS) is False
    assert rows("small")[0].node_id == a


def test_fy_fetch_011_cache_counts_ahead_of_binpack_in_the_placement_pass():
    """FY-FETCH-011: the order of choose(): unreachable last, then restart-to-previous, then spread,
    then cached, then fewer fetches, then binpack."""

    class N:
        def __init__(self, name):
            self.name, self.status, self.id = name, "online", ord(name[0])

    def opt(name, left, cached, needs=(), blocked=None):
        return scheduler.Option(N(name), None, [], left, cached, list(needs), blocked)

    v = lambda o: scheduler.Verdict(o.node.id, o.node.name, "online", True, option=o)  # noqa: E731
    bad = opt("a", 0, False, [("model", "huggingface.co")], failure())
    good = opt("z", 60 * 1024**3, False, [("model", "huggingface.co")])
    assert scheduler.choose([v(bad), v(good)], None) is good
    assert scheduler.choose([v(bad), v(good)], None, avoid=False) is bad
    assert scheduler.choose([v(bad), v(good)], None, prefer=bad.node.id) is good  # unreachable beats "go back"
    assert scheduler.choose([v(bad)], None) is bad
    note = scheduler.placement_note([v(bad), v(good)], good, None, set(), None)
    assert note == "a could not reach huggingface.co; placed on z instead"
    assert scheduler.placement_note([v(good)], good, None, set(), None) is None
