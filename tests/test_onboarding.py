"""The Get started checklist: each step's state comes from what is really there, admins and operators
get the setup steps, users the three about using the API, and hiding it is a per-person setting
(FY-ONB-*)."""

import pytest
from sqlalchemy import select

from forgeyard.db import Accelerator, Node, User, new_session
from forgeyard.gateway.usage import recorder
from tests.conftest import add_user, make_client, sign_in
from tests.gateway_helpers import FakeEngine, api_client, make_ready, new_key
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

PW = "quiet-meadow-4821"
KEYS = MODELS + ("replica_keys",)


def steps(c) -> dict:
    r = c.get("/api/onboarding")
    assert r.status_code == 200, r.text
    body = r.json()
    return {"body": body, **{s["id"]: s for s in body["steps"]}}


def done(c) -> list[str]:
    return [s["id"] for s in c.get("/api/onboarding").json()["steps"] if s["done"]]


def cpu_only(node_id: int) -> None:
    db = new_session()
    try:
        for a in db.scalars(select(Accelerator).where(Accelerator.node_id == node_id, Accelerator.vendor != "cpu")):
            db.delete(a)
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def engine():
    e = FakeEngine("chat", "secret-a")
    yield e
    e.close()


def test_fy_onb_001_a_fresh_install_shows_six_open_steps(owner):
    """FY-ONB-001: an admin on a fresh server gets the six setup steps in order, none done, each with
    a reason and a button to the right page; later steps say what they wait for."""
    s = steps(owner)
    body = s["body"]
    assert body["kind"] == "setup" and body["total"] == 6 and body["done"] == 0 and body["complete"] is False
    assert [x["id"] for x in body["steps"]] == ["machine", "gpus", "model", "deploy", "key", "call"]
    assert all(x["why"] and x["detail"] for x in body["steps"])
    assert s["machine"]["action"] == {"to": "/machines", "label": "Add a machine"} and s["machine"]["who"] is None
    assert s["gpus"]["blocked_by"] == "machine" and s["deploy"]["blocked_by"] == "model"
    assert s["call"]["blocked_by"] == "deploy" and s["call"]["action"] is None
    assert body["hidden"] is False and body["example"] is None
    assert body["gateway_url"] == "http://localhost/v1"


def test_fy_onb_002_each_step_follows_real_state(owner, engine):
    """FY-ONB-002: a machine online, its GPUs chosen (also "all of them", which leaves no allowlist),
    a model, a ready deployment, an active key, a request through /v1: each turns its step done."""
    off = make_node("asleep", status="offline", features=KEYS)
    s = steps(owner)
    assert not s["machine"]["done"] and "asleep joined but is offline" in s["machine"]["detail"]
    box = make_node("box", features=KEYS)
    s = steps(owner)
    assert s["machine"]["done"] and s["machine"]["detail"] == "1 machine online, 1 offline."
    assert not s["gpus"]["done"] and "asleep, box" in s["gpus"]["detail"]

    # Choosing "every GPU" stores no allowlist: the choice is read from the audit log.
    for node_id in (off, box):
        r = owner.put(f"/api/machines/{node_id}/gpu-allowlist", json={"keys": None})
        assert r.status_code == 200, r.text
    assert steps(owner)["gpus"]["done"]

    seed_catalog()
    assert done(owner) == ["machine", "gpus", "model"]
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    s = steps(owner)
    assert not s["deploy"]["done"] and "chat (" in s["deploy"]["detail"]
    assert s["body"]["example"] == {"model": "chat", "api": "openai-chat", "ready": False}
    make_ready("chat", [(box, engine.port, engine.secret)])
    s = steps(owner)
    assert s["deploy"]["done"] and s["deploy"]["detail"] == "Ready: chat."
    assert s["body"]["example"]["ready"] is True and s["call"]["blocked_by"] is None

    key = new_key(owner, "laptop")["key"]
    assert "key" in done(owner) and "call" not in done(owner)
    # The playground counts as the console, not as a call with a key.
    assert owner.post("/api/playground/chat/completions",
                      json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]}).status_code == 200
    recorder.flush()
    assert "call" not in done(owner)
    r = api_client(owner, key).post("/v1/chat/completions",
                                    json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    recorder.flush()
    s = steps(owner)
    assert s["call"]["done"] and s["body"]["complete"] is True and s["body"]["done"] == 6


def test_fy_onb_003_gpu_choices_per_machine(owner):
    """FY-ONB-003: an allowlist counts as a choice; a machine without GPUs needs none; one GPU machine
    still open keeps the step open."""
    a = make_node("a", allowlist=[4], features=KEYS)
    assert steps(owner)["gpus"]["done"]
    b = make_node("b", features=KEYS)
    s = steps(owner)
    assert not s["gpus"]["done"] and "Not chosen yet on b" in s["gpus"]["detail"]
    cpu_only(b)
    assert steps(owner)["gpus"]["done"]
    cpu_only(a)
    s = steps(owner)
    assert s["gpus"]["done"] and "no GPU" in s["gpus"]["detail"]
    # A revoked machine is not waiting for anything.
    c = make_node("c", features=KEYS)
    assert not steps(owner)["gpus"]["done"]
    assert owner.post(f"/api/machines/{c}/revoke").status_code == 200
    assert steps(owner)["gpus"]["done"]


def test_fy_onb_004_keys_and_calls_are_the_persons_own(owner, engine):
    """FY-ONB-004: someone else's key or requests, or a revoked key, do not tick my steps."""
    box = make_node("box", allowlist=[4], features=KEYS)
    seed_catalog()
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    make_ready("chat", [(box, engine.port, engine.secret)])
    add_user(owner, "olga", role="operator")
    op = sign_in(owner, "olga", PW)
    other = new_key(op, "theirs")["key"]
    assert api_client(owner, other).post("/v1/chat/completions",
                                         json={"model": "chat", "messages": [{"role": "user", "content": "x"}]}).status_code == 200
    recorder.flush()
    s = steps(owner)
    assert not s["key"]["done"] and not s["call"]["done"]
    assert steps(op)["key"]["done"] and steps(op)["call"]["done"]
    mine = new_key(owner, "mine")
    assert steps(owner)["key"]["done"]
    assert owner.delete(f"/api/keys/{mine['api_key']['id']}").status_code == 200
    s = steps(owner)
    assert not s["key"]["done"] and s["key"]["detail"] == "You have no active key."


def test_fy_onb_005_roles_get_their_own_list(owner):
    """FY-ONB-005: operators see the admin-only steps with who does them; users get create a key →
    call /v1 → see usage, and the last one needs a call and a visit to Usage."""
    add_user(owner, "olga", role="operator")
    add_user(owner, "ursula", role="user")
    op, user = sign_in(owner, "olga", PW), sign_in(owner, "ursula", PW)
    s = steps(op)
    assert s["body"]["kind"] == "setup" and s["body"]["total"] == 6
    assert s["machine"]["who"] == "An admin adds machines." and s["machine"]["action"]["label"] == "Open Machines"
    assert s["model"]["who"] == "An admin adds models." and s["deploy"]["who"] is None
    s = steps(user)
    assert s["body"]["kind"] == "use" and [x["id"] for x in s["body"]["steps"]] == ["key", "call", "usage"]
    assert s["usage"]["blocked_by"] == "call"
    new_key(user, "laptop")
    assert user.post("/api/onboarding/seen", json={"step": "usage"}).status_code == 200
    s = steps(user)
    assert s["key"]["done"] and not s["usage"]["done"]  # opened Usage, but nothing was called yet
    assert user.post("/api/onboarding/seen", json={"step": "machines"}).status_code == 422


def test_fy_onb_006_hiding_is_per_person_and_needs_a_session(owner):
    """FY-ONB-006: hiding the checklist is stored for that person only, survives a new session, can be
    undone; without a session or the console header nothing is written."""
    add_user(owner, "ursula", role="user")
    user = sign_in(owner, "ursula", PW)
    r = owner.put("/api/onboarding", json={"hidden": True})
    assert r.status_code == 200 and r.json()["hidden"] is True
    assert steps(user)["body"]["hidden"] is False
    again = sign_in(owner, "owner", "harbour-lantern-71")
    assert steps(again)["body"]["hidden"] is True
    assert again.put("/api/onboarding", json={"hidden": False}).json()["hidden"] is False
    assert steps(owner)["body"]["hidden"] is False
    with make_client() as anon:
        assert anon.get("/api/onboarding").status_code == 401
        assert anon.put("/api/onboarding", json={"hidden": True}).status_code == 401
    no_header = sign_in(owner, "ursula", PW)
    del no_header.headers["X-Forgeyard"]
    assert no_header.put("/api/onboarding", json={"hidden": True}).status_code == 403
    db = new_session()
    try:
        u = db.scalar(select(User).where(User.username == "ursula"))
        assert (u.preferences or {}).get("onboarding_hidden") is None
    finally:
        db.close()
    assert owner.put("/api/onboarding", json={"hidden": "yes please"}).status_code == 422


def test_fy_onb_007_a_users_call_and_usage_visit_finish_their_list(owner, engine):
    """FY-ONB-007: for a user, the example points at the ready deployment and all three steps end done."""
    box = make_node("box", allowlist=[4], features=KEYS)
    seed_catalog()
    deploy(owner, "embed", "qwen3-embedding-0.6b", "vllm-pooling", wait=True)
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    make_ready("chat", [(box, engine.port, engine.secret)])
    add_user(owner, "ursula", role="user")
    user = sign_in(owner, "ursula", PW)
    s = steps(user)
    assert s["body"]["example"] == {"model": "chat", "api": "openai-chat", "ready": True}
    key = new_key(user, "laptop")["key"]
    assert api_client(owner, key).post("/v1/chat/completions",
                                       json={"model": "chat", "messages": [{"role": "user", "content": "x"}]}).status_code == 200
    recorder.flush()
    assert done(user) == ["key", "call"]
    user.post("/api/onboarding/seen", json={"step": "usage"})
    assert steps(user)["body"]["complete"] is True


def test_fy_onb_008_offline_and_unready_explain_themselves(owner):
    """FY-ONB-008: no machine at all and an empty catalog say so plainly."""
    s = steps(owner)
    assert s["machine"]["detail"] == "No machine has joined yet."
    assert s["gpus"]["detail"] == "Add a machine first."
    assert s["model"]["detail"] == "The catalog is empty."
    assert s["deploy"]["detail"] == "Add a model first."
    assert s["key"]["detail"] == "You have no active key."
    db = new_session()
    try:
        assert db.scalar(select(Node)) is None
    finally:
        db.close()
