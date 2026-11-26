"""1.5.2 (#95, PR 4): trying an engine before switching to it (FY-TRIAL-*): a temporary, isolated deployment of one
model with the engine on one machine, checks against its replica (it starts, its memory against the reservation, it
answers, latency, the built-in engine's answer and numbers), then the deployment is removed: after passing, failing,
running out of time, being cancelled, and when the server starts again. The `engine_trials` rows, the API and its
permissions, the deployment lists and loops that leave the trial out, the switch form's mention and the policy seam."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from forgeyard.db import (Deployment, EngineTrial, GatewayRoute, Replica, UsageDaily, Workload, WorkloadEvent,
                          new_session, utcnow)
from forgeyard.models import trials
from forgeyard.workloads import autoscale, placement, sleep
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import api_client, new_key
from tests.test_deploy_edit import GIB, PW, audit, card, cpu_row
from tests.test_engine_switch import COPY, add_copy, llama_copy
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

MODEL = "qwen3-1.7b-gguf"
CPU = {"gpus": 0, "threads": 2}


@pytest.fixture(autouse=True)
def manual_runner(isolated, monkeypatch):
    """The trials move on only when a test says so (the app's loop is off); no agent to ask for output."""
    monkeypatch.setattr(trials, "live_tail", lambda r: None)
    trials.runner.enabled = False
    placement._previous.clear()
    yield
    trials.runner.enabled = False
    placement._previous.clear()


@pytest.fixture()
def lab(owner):
    """A machine with a lot of free RAM, the acceptance models, and a copy of llama-cpp (`llama-cpp-1-1-0`)."""
    seed_catalog()
    node = make_node("cpu-a", features=MODELS)
    cpu_row(node, vram_free=100 * GIB, vram_used=20 * GIB, updated_at=utcnow() - timedelta(seconds=120))
    add_copy(owner, llama_copy())
    return owner, node


class Replies:
    """A stand-in for the replica (trials.send): answers by URL; records every request."""

    def __init__(self, chat: str = "A GPU is a processor for parallel work.", status: int = 200) -> None:
        self.calls: list[dict] = []
        self.chat, self.status = chat, status
        self.by_port: dict[int, tuple[int, object]] = {}

    def __call__(self, url, payload, ctype, secret, tls_name):
        import json

        self.calls.append({"url": url, "body": json.loads(payload) if ctype.startswith("application/json") else None,
                           "secret": secret})
        port = int(url.split(":")[2].split("/")[0])
        if port in self.by_port:
            status, answer = self.by_port[port]
            return status, answer, 7
        if self.status != 200:
            return self.status, "engine error", 5
        if url.endswith("/v1/chat/completions"):
            return 200, {"choices": [{"message": {"content": self.chat}}]}, 40 + len(self.calls)
        if url.endswith("/v1/embeddings"):
            return 200, {"data": [{"embedding": [0.1, 0.2, 0.3]}]}, 9
        if url.endswith("/v1/rerank"):
            return 200, {"results": [{"index": 0, "relevance_score": 0.98}, {"index": 2, "relevance_score": 0.1},
                                     {"index": 1, "relevance_score": 0.05}]}, 11
        return 200, {}, 5


@pytest.fixture()
def replies(monkeypatch):
    r = Replies()
    monkeypatch.setattr(trials, "send", r)
    return r


def start(c, node, status=201, recipe=COPY, **kw):
    body = {"model": MODEL, "machine_id": node, "params": CPU, **kw}
    r = c.post(f"/api/recipes/{recipe}/trials", json=body)
    assert r.status_code == status, r.text
    return r.json()


def row(trial_id: int) -> EngineTrial:
    db = new_session()
    try:
        t = db.get(EngineTrial, trial_id)
        db.expunge(t)
        return t
    finally:
        db.close()


def wname(trial_id: int) -> str:
    return row(trial_id).measurements["workload"]


def set_rep(trial_id: int, **fields) -> None:
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == wname(trial_id)))
        r = next(r for r in w.replicas if r.ordinal == 0)
        for k, v in fields.items():
            setattr(r, k, v)
        db.commit()
    finally:
        db.close()


def ready_event(trial_id: int, seconds: float = 12.0) -> None:
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == wname(trial_id)))
        db.add(WorkloadEvent(workload_id=w.id, kind="ready", replica=0, at=utcnow(),
                             message=f"Replica 0 on cpu-a: ready in {seconds:.1f} s (model 2.0 s, engine {seconds - 2:.1f} s)"))
        db.commit()
    finally:
        db.close()


def replica(trial_id: int) -> Replica:
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == wname(trial_id)))
        r = next(r for r in w.replicas if r.ordinal == 0)
        db.expunge(r)
        return r
    finally:
        db.close()


def bring_up(trial_id: int, node: int, *, before: int = 20 * GIB, after: int = 22 * GIB, port: int = 41001) -> None:
    """The replica's life as its agent would report it, a minute ago: placed, a reading before the engine starts,
    launched, healthy (with its start event), and three readings after it is ready; the trial moves on after each."""
    t0 = utcnow() - timedelta(seconds=60)
    assert trials.advance(trial_id) == "running"
    cpu_row(node, vram_used=before, updated_at=t0)
    assert trials.advance(trial_id) == "running"
    set_rep(trial_id, state="starting", reason="waiting for the health check", started_at=t0 + timedelta(seconds=5),
            ports=[{"name": "http", "port": 8080, "host_port": port, "address": "127.0.0.1"}])
    assert trials.advance(trial_id) == "running"
    ready = t0 + timedelta(seconds=10)
    set_rep(trial_id, state="healthy", reason=None, ready_at=ready)
    ready_event(trial_id)
    assert trials.advance(trial_id) == "running"
    assert row(trial_id).phase == "measuring"
    for i in range(trials.MEMORY_SAMPLES):
        cpu_row(node, vram_used=after, updated_at=ready + timedelta(seconds=i + 1))
        trials.advance(trial_id)
    assert row(trial_id).phase == "asking"


def finish(trial_id: int, node: int, *, after: int = 22 * GIB) -> str:
    """Ask it (the fake replica answers), then one reading after the answers: the trial ends."""
    assert trials.advance(trial_id) == "running"
    assert row(trial_id).phase == "settling"
    cpu_row(node, vram_used=after, updated_at=utcnow() + timedelta(seconds=1))
    return trials.advance(trial_id)


def check(t: EngineTrial, id_: str) -> dict:
    return next(c for c in t.checks if c["id"] == id_)


def gone(name: str) -> bool:
    db = new_session()
    try:
        return db.scalar(select(Workload.id).where(Workload.name == name)) is None and \
            db.scalar(select(Deployment.id).where(Deployment.name == name)) is None
    finally:
        db.close()


# ------------------------------------------------------------------ starting one
def test_fy_trial_001_a_trial_makes_a_pinned_temporary_deployment_without_a_route(lab):
    """FY-TRIAL-001"""
    c, node = lab
    got = start(c, node)["trial"]
    assert got["state"] == "running" and got["phase"] == "starting" and got["recipe"] == COPY
    assert got["builtin"] == "llama-cpp" and got["engine_version"] == "1.1.0" and got["model"]["name"] == MODEL
    assert got["machine"] == {"id": node, "name": "cpu-a"} and got["requested_by"] == "owner"
    t = row(got["id"])
    db = new_session()
    try:
        d = db.get(Deployment, t.deployment_id)
        assert d is not None and d.recipe == COPY and d.name.startswith("trial-")
        assert d.workload.spec["machine_id"] == node and d.workload.spec["replicas"] == 1
        assert db.scalar(select(GatewayRoute.id).where(GatewayRoute.deployment_id == d.id)) is None
        assert d.workload.replicas[0].node_id == node     # placed at once
    finally:
        db.close()
    a = audit("engine_trial.started")
    assert len(a) == 1 and a[0]["target"] == COPY and a[0]["model"] == MODEL and a[0]["machine"] == "cpu-a"


def test_fy_trial_002_parameters_default_to_the_built_in_deployment_of_the_model_without_gateway_settings(lab):
    """FY-TRIAL-002"""
    c, node = lab
    deploy(c, "chatty", MODEL, "llama-cpp", params={"gpus": 0, "threads": 3, "context": 2048,
                                                     "sleep_when_idle": True, "first_answer_timeout": 60})
    dry = c.post(f"/api/recipes/{COPY}/trials", json={"model": MODEL, "machine_id": node, "dry_run": True}).json()
    assert dry["params_from"] == "chatty" and dry["params"]["threads"] == 3 and dry["params"]["context"] == 2048
    assert "sleep_when_idle" not in dry["params"] and "first_answer_timeout" not in dry["params"]
    assert dry["placement"]["fits"] is True and dry["estimate"]["device"] == "cpu"
    got = start(c, node, params=None)["trial"]
    assert got["params"]["threads"] == 3 and got["params_from"] == "chatty"
    # Sent by a person, sleep and automatic replicas are dropped too.
    c.post(f"/api/engine-trials/{got['id']}/cancel")
    got = start(c, node, params={**CPU, "sleep_when_idle": True, "autoscale": True, "max_replicas": 3})["trial"]
    assert not {"sleep_when_idle", "autoscale", "max_replicas"} & set(got["params"])


def test_fy_trial_003_without_parameters_or_a_deployment_the_engine_defaults_and_the_machine_s_device(lab):
    """FY-TRIAL-003: the A100 machine takes the GPU variant (gpus 1); its RAM row alone would take gpus 0."""
    c, node = lab
    dry = c.post(f"/api/recipes/{COPY}/trials", json={"model": MODEL, "machine_id": node, "dry_run": True}).json()
    assert dry["params_from"] is None and dry["params"]["gpus"] == 1 and dry["estimate"]["device"] == "gpu"
    assert dry["engine_on"]["variant"] and dry["limit"] == trials.MAX_RUNNING


def test_fy_trial_004_refusals(lab):
    """FY-TRIAL-004: a model the engine does not serve, an unknown machine, an offline one, one that cannot take it, a
    second trial of the same engine, the server's limit."""
    c, node = lab
    assert start(c, node, status=422, model="qwen3-embedding-0.6b")["detail"]["code"] == "incompatible"
    assert start(c, 999, status=404)["detail"]["code"] == "machine_not_found"
    off = make_node("cpu-off", features=MODELS, status="offline")
    assert start(c, off, status=409)["detail"]["code"] == "machine_offline"
    small = make_node("cpu-small", features=MODELS)
    cpu_row(small, vram_free=GIB // 4)
    got = start(c, small, status=409)["detail"]
    assert got["code"] == "does_not_fit" and got["machines"]
    start(c, node)
    assert start(c, node, status=409)["detail"]["code"] == "trial_running"
    db = new_session()
    try:
        assert db.scalar(select(EngineTrial.id).where(EngineTrial.state == "running").limit(5)) is not None
    finally:
        db.close()


def test_fy_trial_005_the_server_s_limit_of_running_trials(lab, monkeypatch):
    """FY-TRIAL-005"""
    c, node = lab
    monkeypatch.setattr(trials, "MAX_RUNNING", 1)
    start(c, node)
    add_copy(c, llama_copy(name="llama-cpp-1-2-0", version="1.2.0"))
    got = start(c, node, status=409, recipe="llama-cpp-1-2-0")["detail"]
    assert got["code"] == "too_many_trials"


def test_fy_trial_006_who_may_start_cancel_and_read(lab):
    """FY-TRIAL-006: operators and admins start and cancel; everyone signed in reads the history."""
    c, node = lab
    add_user(c, "olive", role="operator")
    add_user(c, "ursa")
    t = start(c, node)["trial"]
    u = sign_in(c, "ursa", PW)
    assert u.post(f"/api/recipes/{COPY}/trials", json={"model": MODEL, "machine_id": node}).status_code == 403
    assert u.post(f"/api/engine-trials/{t['id']}/cancel").status_code == 403
    listed = u.get(f"/api/recipes/{COPY}/trials").json()
    assert [x["id"] for x in listed["trials"]] == [t["id"]] and listed["can_run"] is False
    assert u.get(f"/api/engine-trials/{t['id']}").json()["trial"]["state"] == "running"
    o = sign_in(c, "olive", PW)
    got = o.post(f"/api/engine-trials/{t['id']}/cancel")
    assert got.status_code == 200 and got.json()["trial"]["state"] == "cancelled"
    assert o.post(f"/api/recipes/{COPY}/trials", json={"model": MODEL, "machine_id": node, "params": CPU}).status_code == 201


# ------------------------------------------------------------------ isolation
def test_fy_trial_007_isolated_from_lists_keys_the_playground_routes_and_the_deployment_api(lab, replies):
    """FY-TRIAL-007"""
    c, node = lab
    t = start(c, node)["trial"]
    name = wname(t["id"])
    dep_id = row(t["id"]).deployment_id
    listed = c.get("/api/deployments").json()
    assert all(d["id"] != dep_id for d in listed["deployments"])
    assert [x["id"] for x in listed["trials"]] == [t["id"]] and listed["trials"][0]["workload"] == name
    for method, path, body in (("get", f"/api/deployments/{dep_id}", None), ("post", f"/api/deployments/{dep_id}/stop", None),
                               ("post", f"/api/deployments/{dep_id}/start", None),
                               ("post", f"/api/deployments/{dep_id}/try", {}),
                               ("patch", f"/api/deployments/{dep_id}", {"params": {"threads": 4}}),
                               ("delete", f"/api/deployments/{dep_id}", None)):
        r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (path, r.text)
    assert all(d["id"] != dep_id for d in c.get("/api/route-options").json()["deployments"])
    page = c.get(f"/api/recipes/{COPY}").json()
    assert page["deployments"] == [] and page["trial_running"]["id"] == t["id"]
    assert c.get(f"/api/models/{MODEL}").json()["deployments"] == []
    # Not a model of the gateway: /v1/models, a key and the Playground's console route.
    key = new_key(c)["key"]
    v1 = api_client(c, key)
    assert all(m["id"] != name for m in v1.get("/v1/models").json()["data"])
    r = v1.post("/v1/chat/completions", json={"model": name, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    r = c.post("/api/gateway/v1/chat/completions", json={"model": name, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code in (403, 404)
    # It answers the trial, and none of that is usage.
    bring_up(t["id"], node)
    assert finish(t["id"], node) == "passed"
    db = new_session()
    try:
        assert db.scalars(select(UsageDaily)).all() == []
    finally:
        db.close()


def test_fy_trial_008_never_put_to_sleep_or_scaled(lab):
    """FY-TRIAL-008: even with the settings forced onto its row, sleep and the autoscaler leave it alone."""
    c, node = lab
    t = start(c, node)["trial"]
    db = new_session()
    try:
        d = db.get(Deployment, row(t["id"]).deployment_id)
        d.params = {**d.params, "sleep_when_idle": True, "idle_minutes": 1, "autoscale": True, "min_replicas": 2,
                    "max_replicas": 4}
        db.commit()
    finally:
        db.close()
    long_ago = utcnow() - timedelta(hours=2)
    set_rep(t["id"], state="healthy", reason=None, ready_at=long_ago, started_at=long_ago,
            ports=[{"name": "http", "port": 8080, "host_port": 41030, "address": "127.0.0.1"}])
    db = new_session()
    try:
        for e in db.scalars(select(WorkloadEvent)).all():
            e.at = long_ago
        for w in db.scalars(select(Workload)).all():
            w.updated_at = w.created_at = long_ago     # (the autoscaler waits 2 minutes after a start)
        db.commit()
    finally:
        db.close()
    sleep.idle.server_started(long_ago)
    assert sleep.sweep_once(utcnow() + timedelta(hours=1)) == []
    autoscale.autoscaler.tick()
    db = new_session()
    try:
        d = db.get(Deployment, row(t["id"]).deployment_id)
        assert d.workload.desired_state == "running" and len(d.workload.replicas) == 1
    finally:
        db.close()


# ------------------------------------------------------------------ a run
def test_fy_trial_009_a_trial_that_passes_measures_asks_compares_and_is_removed(lab, replies):
    """FY-TRIAL-009"""
    c, node = lab
    t = start(c, node)["trial"]
    name = wname(t["id"])
    bring_up(t["id"], node, before=20 * GIB, after=21 * GIB + GIB // 2)
    assert finish(t["id"], node, after=21 * GIB + GIB // 2) == "passed"
    got = c.get(f"/api/engine-trials/{t['id']}").json()["trial"]
    assert got["state"] == "passed" and got["phase"] is None and got["finished_at"]
    assert check(row(t["id"]), "started")["ok"] is True and got["start"]["seconds"] == 12.0
    assert got["start"]["phases"] == {"model": 2.0, "engine": 10.0}
    mem = check(row(t["id"]), "memory")
    assert mem["ok"] is True and mem["value"] == int(1.5 * GIB) and got["measured"] == int(1.5 * GIB)
    assert got["reserved"] and "reserved" in mem["expected"]
    ans = check(row(t["id"]), "answers")
    assert ans["ok"] is True and ans["value"] == f"{trials.REQUESTS + 1}/{trials.REQUESTS + 1}"
    assert got["first_answer"] == "A GPU is a processor for parallel work."
    lat = check(row(t["id"]), "latency")["value"]
    assert lat["count"] == trials.REQUESTS and lat["p50_ms"] is not None and lat["warmup_ms"] is not None
    # The fixed chat request, straight to the replica with its internal key.
    assert len(replies.calls) == trials.REQUESTS + 1
    first = replies.calls[0]
    assert first["url"] == "http://127.0.0.1:41001/v1/chat/completions"
    assert first["body"]["temperature"] == 0 and first["body"]["max_tokens"] <= 128
    assert first["secret"] == replica_secret_seen(replies)
    assert gone(name) and row(t["id"]).deployment_id is None
    assert got["summary"].startswith("Passed: ready in 12 s")
    fin = audit("engine_trial.finished")
    assert len(fin) == 1 and fin[0]["state"] == "passed" and fin[0]["trial_id"] == t["id"]
    # The engine page says how its last trial went; the history lists it.
    assert c.get(f"/api/recipes/{COPY}").json()["last_trial"]["state"] == "passed"
    assert c.get(f"/api/recipes/{COPY}/trials").json()["trials"][0]["state"] == "passed"


def replica_secret_seen(replies: Replies):
    return replies.calls[-1]["secret"]


def test_fy_trial_010_memory_over_the_reservation_fails_and_a_shared_card_is_approximate(lab, replies):
    """FY-TRIAL-010"""
    c, node = lab
    t = start(c, node)["trial"]
    reserved = c.get(f"/api/engine-trials/{t['id']}").json()["trial"]["estimate"]
    bring_up(t["id"], node, before=20 * GIB, after=20 * GIB + int(reserved * 1.3))
    assert finish(t["id"], node, after=20 * GIB + int(reserved * 1.3)) == "failed"
    mem = check(row(t["id"]), "memory")
    assert mem["ok"] is False and "more than it reserves" in mem["note"]
    assert "other programs use the machine's RAM" in mem["note"]          # 20 GiB used by other work before it
    assert row(t["id"]).summary.startswith("Failed (Memory)")


def test_fy_trial_011_memory_within_the_tolerance_passes_and_missing_readings_are_not_measured(lab, replies):
    """FY-TRIAL-011"""
    c, node = lab
    t = start(c, node)["trial"]
    reserved = row(t["id"]).estimate["amount"]
    bring_up(t["id"], node, before=20 * GIB, after=20 * GIB + int(reserved * 1.04))
    assert finish(t["id"], node, after=20 * GIB + int(reserved * 1.04)) == "passed"
    assert check(row(t["id"]), "memory")["ok"] is True
    # A machine that sends no reading after the engine is ready: not measured (and not a failure).
    got = trials.measure({"cpu:0": [[1, "2026-01-01T00:00:00+00:00"]]}, {}, ["cpu:0"], None)
    assert got["cpu:0"]["delta"] is None
    c2 = start(c, node)["trial"]
    assert trials.advance(c2["id"]) == "running"
    ready = utcnow()
    set_rep(c2["id"], state="healthy", reason=None, ready_at=ready, started_at=ready,
            ports=[{"name": "http", "port": 8080, "host_port": 41002, "address": "127.0.0.1"}])
    trials.advance(c2["id"])                                      # measuring: no fresh reading
    trials.advance(c2["id"], now=utcnow() + timedelta(seconds=trials.measure_wait() + 1))   # waited long enough
    assert row(c2["id"]).phase == "asking"
    trials.advance(c2["id"])
    assert trials.advance(c2["id"], now=utcnow() + timedelta(seconds=trials.measure_wait(1) + 1)) == "passed" \
        or row(c2["id"]).state == "passed"
    mem = check(row(c2["id"]), "memory")
    assert mem["ok"] is None and mem["note"].startswith("Not measured")
    got = c.get(f"/api/engine-trials/{c2['id']}").json()["trial"]
    assert got["measured"] is None and got["memory_unmeasured"] == "no_readings" and mem["value"] is None
    assert "memory not measured: the machine sent no readings in time" in got["summary"]


def test_fy_trial_012_a_start_that_fails_ends_the_trial_and_removes_the_deployment(lab, replies):
    """FY-TRIAL-012"""
    c, node = lab
    t = start(c, node)["trial"]
    name = wname(t["id"])
    trials.advance(t["id"])
    set_rep(t["id"], state="backoff", reason="exited with code 1; restarting in 2 s", exit_code=1,
            log_tail="loading model\nerror: unknown argument: --video-fps\n")
    assert trials.advance(t["id"]) == "failed"
    got = c.get(f"/api/engine-trials/{t['id']}").json()["trial"]
    started = next(x for x in got["checks"] if x["id"] == "started")
    assert started["ok"] is False and "unknown argument" in started["note"]
    assert got["summary"] == "Failed: it did not start: exited with code 1"
    assert {x["id"]: x["ok"] for x in got["checks"]}["answers"] is None       # never asked, said as such
    assert gone(name) and got["log_tail"].rstrip().endswith("--video-fps")
    assert any(e["kind"] == "created" for e in got["events"])
    assert replies.calls == []


def test_fy_trial_013_a_replica_that_cannot_be_placed_fails_at_once(lab):
    """FY-TRIAL-013"""
    c, node = lab
    t = start(c, node)["trial"]
    set_rep(t["id"], node_id=None, reason="Cannot be placed: cpu-a: not enough free RAM")
    assert trials.advance(t["id"]) == "failed"
    assert row(t["id"]).summary == "Failed: it could not be placed: cpu-a: not enough free RAM"


def test_fy_trial_014_wrong_answers_fail_the_answers_check(lab, replies):
    """FY-TRIAL-014: an empty chat answer; an engine error."""
    c, node = lab
    replies.chat = "   "
    t = start(c, node)["trial"]
    bring_up(t["id"], node)
    assert finish(t["id"], node) == "failed"
    ans = check(row(t["id"]), "answers")
    assert ans["ok"] is False and ans["value"] == f"0/{trials.REQUESTS + 1}"
    replies.chat, replies.status = "fine", 500
    t = start(c, node)["trial"]
    bring_up(t["id"], node, port=41003)
    assert finish(t["id"], node) == "failed"
    ans = check(row(t["id"]), "answers")
    assert ans["ok"] is False and ans["note"].startswith("HTTP 500")


def test_fy_trial_015_what_each_kind_must_answer():
    """FY-TRIAL-015: the fixed requests' expected answers (rerank: the Paris sentence first; speech to text: the clip's
    words; embeddings: finite numbers; text to speech: a WAV of some length; images: one image)."""
    assert trials.judge("rerank", 200, {"results": [{"index": 0, "relevance_score": 0.9},
                                                    {"index": 1, "relevance_score": 0.1}]})[0] is True
    assert trials.judge("rerank", 200, {"results": [{"index": 2, "relevance_score": 0.9},
                                                    {"index": 0, "relevance_score": 0.1}]})[0] is False
    assert trials.judge("openai-embeddings", 200, {"data": [{"embedding": [0.1, 0.2]}]}) == (True, "2 dimensions")
    assert trials.judge("openai-embeddings", 200, {"data": [{"embedding": [0.1, float("nan")]}]})[0] is False
    assert trials.judge("openai-embeddings", 200, {"data": [{"embedding": []}]})[0] is False
    assert trials.judge("openai-audio", 200, {"text": "And so, my fellow Americans, ask not what your country…"})[0]
    assert trials.judge("openai-audio", 200, {"text": "Hello there."})[0] is False
    assert trials.judge("openai-speech", 200, {"riff": True, "audio_ms": 1800, "audio_bytes": 9000})[0] is True
    assert trials.judge("openai-speech", 200, {"riff": True, "audio_ms": 0, "audio_bytes": 44})[0] is False
    assert trials.judge("openai-images", 200, {"images": 1, "width": 256, "height": 256, "format": "png"})[0]
    assert trials.judge("openai-images", 200, {"images": 0})[0] is False
    assert trials.judge("classify", 200, {"data": [{"label": "en", "probs": [0.9, 0.1], "num_classes": 2}]})[0]
    assert trials.judge("openai-chat", 502, "The replica did not answer") == (False, "The replica did not answer")
    assert trials.judge("openai-chat", 200, {"choices": [{"message": {"content": "", "reasoning_content": "hm"}}]})[0]
    assert trials.percentile([10, 20, 30, 40, 1000], 50) == 30 and trials.percentile([10, 20, 30, 40, 1000], 95) == 1000
    assert trials.percentile([], 50) is None
    assert trials.judge("vision-detect", 500, {"detail": "boom"})[0] is False      # an error, even as JSON
    assert trials.judge("vision-detect", 200, {"boxes": []})[0] is True
    assert trials.same_answer("openai-embeddings", {"data": [{"embedding": [1, 0]}]},
                              {"data": [{"embedding": [1, 0.001]}]})[0] is True
    assert trials.same_answer("openai-embeddings", {"data": [{"embedding": [1, 0]}]},
                              {"data": [{"embedding": [0, 1]}]})[0] is False
    assert trials.same_answer("openai-videos", {}, {}) is None


def test_fy_trial_016_video_is_not_asked_and_says_why(lab, replies, monkeypatch):
    """FY-TRIAL-016"""
    monkeypatch.setitem(trials.NOT_ASKED, "openai-chat", "Not for this test.")
    c, node = lab
    t = start(c, node)["trial"]
    bring_up(t["id"], node)
    assert finish(t["id"], node) == "passed"
    ans = check(row(t["id"]), "answers")
    assert ans["ok"] is None and ans["note"] == "Not checked: Not for this test." and replies.calls == []


def test_fy_trial_017_out_of_time_while_starting(lab):
    """FY-TRIAL-017"""
    c, node = lab
    t = start(c, node, timeout_minutes=5)["trial"]
    name = wname(t["id"])
    trials.advance(t["id"])
    set_rep(t["id"], state="starting", reason="pulling the image: 2.1 of 9.4 GB")
    assert trials.advance(t["id"], now=utcnow() + timedelta(minutes=4)) == "running"
    assert trials.advance(t["id"], now=utcnow() + timedelta(minutes=6)) == "failed"
    got = row(t["id"])
    assert got.summary == "Failed: out of time while it started (the limit was 5 min)."
    assert check(got, "started")["note"] == "pulling the image: 2.1 of 9.4 GB" and gone(name)
    assert start(c, node, status=422, timeout_minutes=2)["detail"]


def test_fy_trial_018_out_of_time_after_it_was_ready(lab, replies):
    """FY-TRIAL-018: measuring, asking and settling have a bound of their own."""
    c, node = lab
    t = start(c, node)["trial"]
    bring_up(t["id"], node)
    late = utcnow() + timedelta(seconds=trials.AFTER_READY_SECONDS + 5)
    assert trials.advance(t["id"], now=late) == "failed"
    assert "while it answered" in row(t["id"]).summary


def test_fy_trial_019_cancel_removes_the_deployment_keeps_the_record_and_is_audited(lab, replies):
    """FY-TRIAL-019"""
    c, node = lab
    t = start(c, node)["trial"]
    name = wname(t["id"])
    bring_up(t["id"], node)
    got = c.post(f"/api/engine-trials/{t['id']}/cancel").json()["trial"]
    assert got["state"] == "cancelled" and got["summary"] == "Cancelled by owner." and gone(name)
    assert check(row(t["id"]), "started")["ok"] is True                   # what was checked stays
    assert c.post(f"/api/engine-trials/{t['id']}/cancel").json()["detail"]["code"] == "not_running"
    assert trials.advance(t["id"]) == "cancelled"
    a = audit("engine_trial.cancelled")
    assert len(a) == 1 and a[0]["target"] == COPY and a[0]["state"] == "cancelled"


def test_fy_trial_020_a_cancel_while_it_is_being_asked_stands(lab, replies, monkeypatch):
    """FY-TRIAL-020: the step that asks takes a while; a person's cancel in the meantime is not overwritten."""
    c, node = lab
    t = start(c, node)["trial"]
    bring_up(t["id"], node)
    original = replies.__call__

    def cancel_during(url, *a):
        if len(replies.calls) == 1:
            r = c.post(f"/api/engine-trials/{t['id']}/cancel")
            assert r.status_code == 200
        return original(url, *a)

    monkeypatch.setattr(trials, "send", cancel_during)
    assert trials.advance(t["id"]) == "cancelled"
    assert row(t["id"]).state == "cancelled" and row(t["id"]).deployment_id is None


def test_fy_trial_021_after_a_server_restart_the_trial_is_ended_and_its_deployment_removed(lab):
    """FY-TRIAL-021"""
    c, node = lab
    t = start(c, node)["trial"]
    name = wname(t["id"])
    assert trials.sweep_at_start() == 1
    got = row(t["id"])
    assert got.state == "failed" and "server restarted" in got.summary and gone(name)
    assert check(got, "answers")["ok"] is None
    # Also on a real start of the app.
    t2 = start(c, node)["trial"]
    name2 = wname(t2["id"])
    from tests.conftest import make_client

    with make_client():
        pass
    assert row(t2["id"]).state == "failed" and gone(name2)


def test_fy_trial_022_a_bug_in_a_step_ends_the_trial_instead_of_leaving_its_deployment(lab, monkeypatch):
    """FY-TRIAL-022"""
    c, node = lab
    t = start(c, node)["trial"]
    name = wname(t["id"])

    def boom(*a, **k):
        raise RuntimeError("bad")

    monkeypatch.setattr(trials, "_launched", boom)
    assert trials.advance(t["id"]) == "failed"
    assert row(t["id"]).summary == "Failed: an error in the server (RuntimeError)." and gone(name)


def test_fy_trial_023_its_deployment_removed_some_other_way_ends_it(lab):
    """FY-TRIAL-023"""
    c, node = lab
    t = start(c, node)["trial"]
    db = new_session()
    try:
        d = db.get(Deployment, row(t["id"]).deployment_id)
        db.delete(d.workload)
        db.delete(d)
        db.commit()
    finally:
        db.close()
    assert trials.advance(t["id"]) == "failed"
    assert "temporary deployment is gone" in row(t["id"]).summary


# ------------------------------------------------------------------ the built-in engine, compared
def test_fy_trial_024_compared_with_a_ready_built_in_deployment_and_its_last_trial(lab, replies):
    """FY-TRIAL-024"""
    c, node = lab
    dep = deploy(c, "chatty", MODEL, "llama-cpp", params=CPU)
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == "chatty"))
        r = w.replicas[0]
        r.state, r.ready_at, r.secret = "healthy", utcnow(), "builtin-key"
        r.ports = [{"name": "http", "port": 8080, "host_port": 42000, "address": "127.0.0.1"}]
        db.add(WorkloadEvent(workload_id=w.id, kind="ready", replica=0, at=utcnow(),
                             message="Replica 0 on cpu-a: ready in 30.0 s (engine 30.0 s)"))
        db.commit()
    finally:
        db.close()
    replies.by_port[42000] = (200, {"choices": [{"message": {"content": "A GPU is a processor for parallel work."}}]})
    # A trial of the built-in engine itself first: the baseline numbers.
    base = start(c, node, recipe="llama-cpp")["trial"]
    assert base["builtin"] is None
    bring_up(base["id"], node, before=20 * GIB, after=21 * GIB, port=41010)
    assert finish(base["id"], node, after=21 * GIB) == "passed"
    t = start(c, node)["trial"]
    bring_up(t["id"], node, port=41011)
    assert finish(t["id"], node) == "passed"
    got = c.get(f"/api/engine-trials/{t['id']}").json()["trial"]
    comp = got["comparison"]
    assert comp["builtin"] == "llama-cpp"
    assert comp["deployment"]["name"] == "chatty" and comp["deployment"]["start_seconds"] == 30.0
    assert comp["deployment"]["same_machine"] is True and comp["deployment"]["id"] == dep["deployment"]["id"]
    assert comp["trial"]["id"] == base["id"] and comp["trial"]["measured"] == GIB
    same = check(row(t["id"]), "same_answer")
    assert same["ok"] is None and same["note"] == "The same." and same["expected"] == "chatty (llama-cpp)"
    to_builtin = [x for x in replies.calls if ":42000/" in x["url"]]
    assert len(to_builtin) == 1 and to_builtin[0]["secret"] == "builtin-key"   # the copy compared once; the built-in did not
    assert to_builtin[-1]["body"]["model"] == "chatty"
    started = check(row(t["id"]), "started")
    assert started["expected"] == "the built-in engine: 30 s"


def test_fy_trial_025_a_different_answer_is_said_not_failed(lab, replies):
    """FY-TRIAL-025"""
    c, node = lab
    deploy(c, "chatty", MODEL, "llama-cpp", params=CPU)
    db = new_session()
    try:
        r = db.scalar(select(Workload).where(Workload.name == "chatty")).replicas[0]
        r.state, r.ready_at = "healthy", utcnow()
        r.ports = [{"name": "http", "port": 8080, "host_port": 42001, "address": "127.0.0.1"}]
        db.commit()
    finally:
        db.close()
    replies.by_port[42001] = (200, {"choices": [{"message": {"content": "Graphics hardware."}}]})
    t = start(c, node)["trial"]
    bring_up(t["id"], node)
    assert finish(t["id"], node) == "passed"
    same = check(row(t["id"]), "same_answer")
    assert same["ok"] is None and same["note"].startswith("Different") and same["value"] == "Graphics hardware."


# ------------------------------------------------------------------ switching, and what else refuses while it runs
def test_fy_trial_026_the_switch_form_names_each_engine_s_last_trial_and_the_policy_allows(lab, replies):
    """FY-TRIAL-026"""
    c, node = lab
    dep = deploy(c, "chatty", MODEL, "llama-cpp", params=CPU)["deployment"]
    engines = {e["name"]: e for e in c.get(f"/api/deployments/{dep['id']}").json()["deployment"]["engines"]}
    assert engines[COPY]["last_trial"] is None
    t = start(c, node)["trial"]
    bring_up(t["id"], node)
    finish(t["id"], node)
    engines = {e["name"]: e for e in c.get(f"/api/deployments/{dep['id']}").json()["deployment"]["engines"]}
    lt = engines[COPY]["last_trial"]
    assert lt["state"] == "passed" and lt["machine"] == "cpu-a" and lt["model"] == MODEL and lt["id"] == t["id"]
    # Never a gate: switching to a copy never tried works too (the community edition's choice).
    add_copy(c, llama_copy(name="llama-cpp-1-2-0", version="1.2.0"))
    r = c.patch(f"/api/deployments/{dep['id']}", json={"recipe": "llama-cpp-1-2-0", "dry_run": True})
    assert r.status_code == 200, r.text


def test_fy_trial_027_the_policy_seam_can_refuse_a_switch(lab):
    """FY-TRIAL-027: an extension's switch policy decides (forgeyard/extensions.py, 1.6.1); without one every switch
    is allowed."""
    from forgeyard import extensions

    c, node = lab
    dep = deploy(c, "chatty", MODEL, "llama-cpp", params=CPU)["deployment"]
    extensions.registry.switch_policies.clear()   # none, whatever this environment has installed
    r = c.patch(f"/api/deployments/{dep['id']}", json={"recipe": COPY, "dry_run": True})
    assert r.status_code == 200, r.text
    extensions.registry.switch_policy(lambda db, d, r: ("not_tried", f"{r.name} has not passed a trial."))
    r = c.patch(f"/api/deployments/{dep['id']}", json={"recipe": COPY, "dry_run": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_tried"


def test_fy_trial_036_an_extension_refusing_the_placement_refuses_the_trial(lab):
    """FY-TRIAL-036: a trial takes GPUs or memory like a deployment: when an extension's placement check refuses it
    (a quota, 1.6.1), the trial is refused with that code and text, and nothing is made."""
    from forgeyard import extensions

    c, node = lab
    extensions.registry.placement_check(lambda db, ctx, ask: ("sample_quota", "No more replicas for this team."))
    r = start(c, node, status=409)
    assert r["detail"]["code"] == "sample_quota" and r["detail"]["message"] == "No more replicas for this team."
    assert c.get(f"/api/recipes/{COPY}/trials").json()["trials"] == []


def test_fy_trial_028_the_engine_and_the_model_cannot_be_deleted_while_a_trial_runs(lab):
    """FY-TRIAL-028"""
    c, node = lab
    t = start(c, node)["trial"]
    r = c.delete(f"/api/recipes/{COPY}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "trial_running"
    mid = row(t["id"]).model_id
    r = c.delete(f"/api/models/{mid}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "trial_running"
    c.post(f"/api/engine-trials/{t['id']}/cancel")
    assert c.delete(f"/api/recipes/{COPY}").status_code == 200
    # The history outlives the copy.
    assert c.get(f"/api/recipes/{COPY}/trials").json()["trials"][0]["id"] == t["id"]
    assert c.get("/api/recipes/nothing-here/trials").status_code == 404


def test_fy_trial_029_a_cached_model_names_the_trial_and_metrics_leave_it_out(lab):
    """FY-TRIAL-029"""
    c, node = lab
    t = start(c, node)["trial"]
    from forgeyard import metrics
    from forgeyard.machines import cache

    db = new_session()
    try:
        r = db.scalar(select(Replica).join(Workload).where(Workload.name == wname(t["id"])))
        who = cache._who(r, db.get(Deployment, row(t["id"]).deployment_id))
        assert who == {"name": f"trial of {COPY}", "kind": "trial", "id": t["id"], "recipe": COPY}
        labels = [s.labels.get("deployment") for fam in (*metrics._replicas(db), *metrics._deployment_replicas(db))
                  for s in fam.samples]
    finally:
        db.close()
    assert wname(t["id"]) not in labels


def test_fy_trial_030_gpu_trial_measures_its_card(owner, replies):
    """FY-TRIAL-030: on the A100 machine (card #4 only), the readings of the card it was given."""
    seed_catalog()
    node = make_node("a100", allowlist=[4], features=MODELS)
    add_copy(owner, llama_copy())
    card(node, 4, vram_used=40 * GIB, updated_at=utcnow() - timedelta(seconds=120))
    t = start(owner, node, params={"gpus": 1})["trial"]
    trials.advance(t["id"])
    got = row(t["id"])
    assert got.gpus and got.gpus[0]["index"] == 4 and got.reserved
    t0 = utcnow() - timedelta(seconds=60)
    card(node, 4, vram_used=40 * GIB, updated_at=t0)
    trials.advance(t["id"])
    set_rep(t["id"], state="starting", reason="waiting for the health check", started_at=t0 + timedelta(seconds=5),
            ports=[{"name": "http", "port": 8080, "host_port": 41020, "address": "127.0.0.1"}])
    trials.advance(t["id"])
    ready = t0 + timedelta(seconds=10)
    set_rep(t["id"], state="healthy", reason=None, ready_at=ready)
    trials.advance(t["id"])
    for i in range(trials.MEMORY_SAMPLES):
        card(node, 4, vram_used=40 * GIB + GIB, updated_at=ready + timedelta(seconds=i + 1))
        card(node, 5, vram_used=70 * GIB, updated_at=ready + timedelta(seconds=i + 1))   # not its card
        trials.advance(t["id"])
    trials.advance(t["id"])
    card(node, 4, vram_used=40 * GIB + GIB, updated_at=utcnow() + timedelta(seconds=1))
    assert trials.advance(t["id"]) == "passed"
    got = row(t["id"])
    assert got.measurements["measured"] == GIB and list(got.measurements["memory"]) == [got.gpus[0]["key"]]
    assert "Approximate" in check(got, "memory")["note"]


def test_fy_trial_031_the_engine_trials_table_goes_down_and_up(client):
    """FY-TRIAL-031: migration 0018 is additive and reversible."""
    from alembic import command
    from sqlalchemy import inspect

    from forgeyard.db import get_engine
    from forgeyard.migrate import alembic_config, upgrade

    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0017_engine_downloads")
        assert "engine_trials" not in inspect(conn).get_table_names()
        assert "deployments" in inspect(conn).get_table_names()
    upgrade()
    with get_engine().connect() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("engine_trials")}
    assert {"recipe", "builtin", "model_name", "machine_name", "checks", "comparison", "state", "deployment_id"} <= cols


def test_fy_trial_032_the_first_run_checklist_the_catalog_counts_and_start_metrics_leave_it_out(lab, monkeypatch):
    """FY-TRIAL-032"""
    c, node = lab
    t = start(c, node)["trial"]
    set_rep(t["id"], state="healthy", ready_at=utcnow(), started_at=utcnow())
    from forgeyard.api import onboarding

    db = new_session()
    try:
        step = onboarding.deploy_step(db, True, True)
        assert step["done"] is False and wname(t["id"]) not in step["detail"]
    finally:
        db.close()
    listed = {m["name"]: m for m in c.get("/api/models").json()["models"]}
    assert listed[MODEL]["deployments"] == 0
    seen = []
    from forgeyard import metrics
    from forgeyard.db import Node
    from forgeyard.workloads import starts

    monkeypatch.setattr(metrics, "replica_started", lambda *a: seen.append(a))
    db = new_session()
    try:
        r = db.scalar(select(Replica).join(Workload).where(Workload.name == wname(t["id"])))
        assert starts.ready(db, r, db.get(Node, node), {"seconds": 9.0, "phases": {}}, utcnow())["seconds"] == 9.0
        db.commit()
    finally:
        db.close()
    assert seen == []


def test_fy_trial_033_readings_are_its_own_cards_after_a_moment_and_the_codes_say_why(lab, replies):
    """FY-TRIAL-033: a card's readings are kept only for the replica's cards and only when newer than the moment asked;
    the checks and the trial carry codes for the console's translation; the engines list shows the last trial."""
    c, node = lab
    db = new_session()
    try:
        from forgeyard.db import Accelerator

        rows = db.scalars(select(Accelerator).where(Accelerator.node_id == node)).all()
        gpu = next(a for a in rows if a.vendor != "cpu")
        now = utcnow()
        for a in rows:
            a.updated_at, a.vram_used = now, 3 * GIB
        db.commit()
        got = trials._readings(db, node, [gpu.key], {})
        assert list(got) == [gpu.key]
        assert trials._readings(db, node, [gpu.key], {}, after=now) == {}
        assert list(trials._readings(db, node, [], {})) == ["cpu:0"]
        for a in rows:
            a.updated_at, a.vram_used = now - timedelta(minutes=5), 20 * GIB
        db.commit()
    finally:
        db.close()
    t = start(c, node)["trial"]
    trials.advance(t["id"])
    set_rep(t["id"], state="backoff", reason="exited with code 1", log_tail="error: bad flag\n")
    trials.advance(t["id"])
    got = c.get(f"/api/engine-trials/{t['id']}").json()["trial"]
    assert got["why"] == {"code": "start_failed", "reason": "exited with code 1"}
    codes = {x["id"]: x["codes"] for x in got["checks"]}
    assert codes["started"] == [{"code": "log", "lines": "error: bad flag"}] and codes["memory"] == [{"code": "not_run"}]
    t2 = start(c, node)["trial"]
    bring_up(t2["id"], node, before=20 * GIB, after=20 * GIB + 10 * GIB)
    finish(t2["id"], node, after=20 * GIB + 10 * GIB)
    got = c.get(f"/api/engine-trials/{t2['id']}").json()["trial"]
    assert got["why"] == {"code": "checks", "checks": ["memory"]}
    mem = next(x for x in got["checks"] if x["id"] == "memory")
    assert {"code": "over"} in mem["codes"] and {"code": "approximate_ram"} in mem["codes"]
    ans = next(x for x in got["checks"] if x["id"] == "answers")
    assert ans["codes"] == [{"code": "first_answer", "text": "A GPU is a processor for parallel work."}]
    cards = {r["name"]: r for r in c.get("/api/models").json()["recipes"]}
    assert cards[COPY]["last_trial"]["id"] == t2["id"] and cards[COPY]["trial_running"] is False
    assert cards["llama-cpp"]["last_trial"] is None
    t3 = start(c, node)["trial"]
    assert {r["name"]: r for r in c.get("/api/models").json()["recipes"]}[COPY]["trial_running"] is True
    c.post(f"/api/engine-trials/{t3['id']}/cancel")
    assert c.get(f"/api/engine-trials/{t3['id']}").json()["trial"]["why"] == {"code": "cancelled", "who": "owner"}


def test_fy_trial_034_a_failed_start_asks_the_agent_for_its_output_when_none_was_sent(lab, monkeypatch):
    """FY-TRIAL-034: an agent sends a replica's output by itself only once it has ended for good, not while it backs
    off: the trial asks for it, so the failed start says what the engine printed."""
    c, node = lab
    monkeypatch.setattr(trials, "live_tail", lambda r: "main: loading\nerror: invalid argument: --no-such-flag\n")
    t = start(c, node)["trial"]
    trials.advance(t["id"])
    set_rep(t["id"], state="backoff", reason="exited with code 1; restarting in 2 s", exit_code=1)
    assert trials.advance(t["id"]) == "failed"
    got = c.get(f"/api/engine-trials/{t['id']}").json()["trial"]
    assert got["log_tail"].endswith("--no-such-flag\n")
    started = next(x for x in got["checks"] if x["id"] == "started")
    assert started["note"] == "main: loading / error: invalid argument: --no-such-flag"
    assert got["summary"] == "Failed: it did not start: exited with code 1"


def test_fy_trial_035_memory_that_went_down_is_said_in_words_never_as_a_negative_number(lab, replies):
    """FY-TRIAL-035 (lab, 2026-11-23): other work on a shared card released memory during the trial, so the difference
    was negative: the check has no value, nothing is called measured, and the summary says why instead of "+-0.03 GiB"."""
    c, node = lab
    cpu_row(node, vram_used=22 * GIB, updated_at=utcnow() - timedelta(seconds=120))
    t = start(c, node)["trial"]
    bring_up(t["id"], node, before=22 * GIB, after=22 * GIB - 26 * 1024**2)
    assert finish(t["id"], node, after=22 * GIB - 26 * 1024**2) == "passed"
    got = c.get(f"/api/engine-trials/{t['id']}").json()["trial"]
    mem = next(x for x in got["checks"] if x["id"] == "memory")
    assert mem["ok"] is None and mem["value"] is None and mem["codes"] == [{"code": "went_down_ram"}]
    assert mem["note"].startswith("Not measured: other programs' use of the machine's RAM changed during the trial")
    assert got["measured"] is None and got["measured_peak"] is None and got["memory_unmeasured"] == "went_down_ram"
    assert "+-" not in got["summary"] and "GiB" not in got["summary"]
    assert got["summary"].endswith("memory not measured: other programs' use of the machine's RAM changed during the "
                                   "trial.")
    assert got["memory"]["cpu:0"]["delta"] == -26 * 1024**2      # the raw difference stays for whoever digs
    # On a GPU the same is said of the card.
    db = new_session()
    try:
        row_ = db.get(EngineTrial, t["id"])
        row_.measurements = {**row_.measurements, "memory_unmeasured": "went_down", "measured": None}
        assert trials.summary_of(row_, "passed").endswith("memory not measured: other work on the card changed during "
                                                          "the trial.")
    finally:
        db.close()
