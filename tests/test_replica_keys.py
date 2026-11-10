"""Each model replica's internal key: made by the server per placement, sent only to agents that
handle it, given to the engine through the environment only, kept in the agent's memory only, sent
with health checks; never shown anywhere (FY-RK-*)."""

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from forgeyard.agent.workloads import plan
from forgeyard.agent.workloads.process import ProcessRuntime
from forgeyard.agent.workloads.records import RecordStore
from forgeyard.agent.workloads.supervisor import Supervisor
from forgeyard.db import Replica, Workload, new_session
from forgeyard.models.recipes import RecipeError, builtin_recipes, parse_recipe
from forgeyard.workloads import state as wstate
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import FakeDocker, container, converge, desired, replica, world_dir
from tests.workload_helpers import make_node, service

SECRET = "k3y-0123456789abcdefghijklmnopqrstuvwxyzABCDEF"


def keyed(rep: dict, env: str = "LLAMA_API_KEY", value: str = SECRET) -> dict:
    return {**rep, "replica_key": {"env": env, "value": value}}


def test_fy_rk_001_builtin_recipes_name_their_engines_key_variable():
    """FY-RK-001: the variables each pinned engine reads (see the recipes for the sources)."""
    got = {name: r.api_key_env for name, (r, _t) in builtin_recipes().items()}
    assert got == {"faster-whisper": "API_KEY", "llama-cpp": "LLAMA_API_KEY", "vllm": "VLLM_API_KEY",
                   "vllm-pooling": "VLLM_API_KEY"}
    base = builtin_recipes()["llama-cpp"][1]
    for bad, msg in [("FORGEYARD_KEY", "cannot be used"), ("PORT", "cannot be used"), ("1BAD", "cannot be used")]:
        with pytest.raises(RecipeError, match=msg):
            parse_recipe(base.replace("name: llama-cpp", "name: x").replace("api_key_env: LLAMA_API_KEY",
                                                                             f"api_key_env: {bad}"))
    dup = base.replace("name: llama-cpp", "name: x").replace("api_key_env: LLAMA_API_KEY",
                                                             "api_key_env: LLAMA_API_KEY\nenv: {LLAMA_API_KEY: fixed}")
    with pytest.raises(RecipeError, match="also set in env"):
        parse_recipe(dup)


def _rows(name):
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == name))
        return [(r.uid, r.secret, r.node_id) for r in sorted(w.replicas, key=lambda r: r.ordinal)]
    finally:
        db.close()


def test_fy_rk_002_server_makes_one_per_placement_and_sends_it_only_to_agents_that_handle_it(owner, caplog):
    """FY-RK-002: a placed model replica gets a random key; the desired state carries it (with the
    recipe's variable) only for an agent with the replica_keys feature; a stop and start places it
    again with a new key; the key is in no API answer, event, audit entry or log line."""
    caplog.set_level(logging.DEBUG)
    seed_catalog()
    new = make_node("new", allowlist=[4], features=MODELS + ("replica_keys",))
    old = make_node("old", allowlist=[4], features=MODELS)
    d1 = deploy(owner, "embed", "qwen3-embedding-0.6b", "vllm-pooling", machine_id=new)["deployment"]
    deploy(owner, "rerank", "qwen3-reranker-0.6b", "vllm-pooling", machine_id=old)
    [(uid, secret, node)] = _rows("embed")
    assert node == new and secret and len(secret) >= 40
    assert _rows("rerank")[0][1]  # made anyway; the old agent is simply not given it
    db = new_session()
    try:
        msg_new = wstate.desired_message(db, new, 1)
        msg_old = wstate.desired_message(db, old, 1)
    finally:
        db.close()
    [entry] = msg_new["replicas"]
    assert entry["replica_key"] == {"env": "VLLM_API_KEY", "value": secret}
    assert secret not in json.dumps(entry["spec"]) and "VLLM_API_KEY" not in json.dumps(entry["spec"])
    assert "replica_key" not in msg_old["replicas"][0]
    outputs = [owner.get(p).text for p in ("/api/deployments", f"/api/deployments/{d1['id']}", "/api/workloads",
                                           "/api/routes", "/api/audit", "/api/machines")]
    workload_id = owner.get("/api/workloads").json()["workloads"][0]["id"]
    outputs.append(owner.get(f"/api/workloads/{workload_id}").text)
    assert all(secret not in text for text in outputs)
    assert secret not in caplog.text
    assert owner.post(f"/api/deployments/{d1['id']}/stop").status_code == 200
    db = new_session()
    try:  # the agent confirms the stop
        for r in db.scalars(select(Replica)).all():
            r.state = "stopped"
        db.commit()
    finally:
        db.close()
    assert owner.post(f"/api/deployments/{d1['id']}/start").status_code == 200
    [(uid2, secret2, _)] = _rows("embed")
    assert secret2 and secret2 != secret


def test_fy_rk_003_env_only_never_argv():
    """FY-RK-003: the key is in the process environment and the container env file, never on a
    command line, and it overrides a declared variable of the same name."""
    env = plan.process_env({"LLAMA_API_KEY": "declared"}, [], [{"name": "http", "port": None, "host_port": 31000}],
                           home="/h", user="nobody", replica_id="r", workload="w", ordinal=0,
                           secret={"LLAMA_API_KEY": SECRET})
    assert env["LLAMA_API_KEY"] == SECRET
    cenv = plan.container_env({}, [], [], replica_id="r", workload="w", ordinal=0, secret={"API_KEY": SECRET})
    assert f"API_KEY={SECRET}\n" in plan.env_file_text(cenv)
    argv = plan.container_run_argv("docker", name="n", image="img", command=None, args=["--port", "8000"],
                                   env_file="/state/env", labels={}, ports=[], bind="127.0.0.1", cpu=None,
                                   memory=None, gpus=[], gpu_mode=None, render_devices=[], render_gids=[],
                                   log_max_bytes=1 << 20)
    assert SECRET not in " ".join(argv) and "--env-file" in argv


@pytest.mark.skipif(os.geteuid() != 0, reason="runs the process as the workload user, which needs a root agent")
def test_fy_rk_004_a_real_process_gets_it_in_its_environment_only(tmp_path):
    """FY-RK-004: a real replica: its environment has the key (it prints it), its command line (what
    `ps` shows) does not, nor the agent's record on disk, nor its status reports."""
    work = world_dir()
    try:
        rt = ProcessRuntime(RecordStore(tmp_path / "state"), work / "work", workload_user="nobody",
                            systemd=lambda: False)

        async def scenario():
            sup = Supervisor(tmp_path / "state", {"process": rt}, tick=0.05)
            spec = service(runtime={"type": "process", "command": "sh", "args": ["-c", "env; exec sleep 30"]},
                           ports=[{"name": "http"}])
            sup.set_desired(desired(keyed(replica("r1", spec))))
            await converge(sup, lambda s: s.records["r1"].pid is not None
                           and "LLAMA_API_KEY" in rt.tail(s.records["r1"], 100)[0])
            rec = sup.records["r1"]
            printed = rt.tail(rec, 100)[0]
            cmdlines = [Path(f"/proc/{p}/cmdline").read_bytes() for p in _group(rec.pgid)]
            report = json.dumps(sup.take_report())
            on_disk = (tmp_path / "state" / "replicas" / "r1.json").read_text()
            sup.set_desired(desired())
            await converge(sup, lambda s: "r1" not in s.records)
            await sup.close()
            return printed, cmdlines, report, on_disk

        printed, cmdlines, report, on_disk = asyncio.run(scenario())
        assert f"LLAMA_API_KEY={SECRET}" in printed.splitlines()
        assert cmdlines and not any(SECRET.encode() in c for c in cmdlines)
        assert SECRET not in report and SECRET not in on_disk
        assert json.loads(on_disk)["key_env"] == "LLAMA_API_KEY"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _group(pgid):
    from forgeyard.agent.workloads.process import proc_stat

    out = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            st = proc_stat(int(entry.name))
            if st and st[1] == pgid:
                out.append(int(entry.name))
    return out


def test_fy_rk_005_containers_get_it_through_the_env_file(tmp_path):
    """FY-RK-005: `docker run` reads it from the 0600 env file (deleted afterwards); it is not in the
    argv anyone can see in `ps`."""
    fake = FakeDocker(tmp_path / "bin")
    from forgeyard.agent.workloads.container import ContainerRuntime

    rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))

    async def scenario():
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32000, 32010))
        sup.set_desired(desired(keyed(replica("abcdef0123456789", container()), env="VLLM_API_KEY")))
        await converge(sup, lambda s: s.records["abcdef0123456789"].state == "running")
        await sup.close()

    asyncio.run(scenario())
    [run] = fake.runs()
    assert SECRET not in json.dumps(fake.calls())
    [c] = fake.state().values()
    assert c["env"]["VLLM_API_KEY"] == SECRET
    assert not (tmp_path / "state" / "logs" / "abcdef0123456789" / "env").exists()


def test_fy_rk_006_health_checks_send_it_and_nothing_starts_without_it(tmp_path):
    """FY-RK-006: probes carry `Authorization: Bearer <key>` (speaches guards /health); an agent that
    restarted knows the replica needs a key (the variable is in its record) but not the key, so it
    waits for the server instead of starting the engine open."""
    fake = FakeDocker(tmp_path / "bin")
    from forgeyard.agent.workloads.container import ContainerRuntime

    seen = []

    def prober(kind, host, port, path, timeout, headers=None):
        seen.append(headers)
        return None

    spec = container(health={"type": "http", "path": "/health", "interval": 1})

    async def first():
        rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32000, 32010), prober=prober)
        sup.set_desired(desired(keyed(replica("abcdef0123456789", spec), env="API_KEY")))
        await converge(sup, lambda s: s.records["abcdef0123456789"].state == "healthy")
        await sup.close()

    asyncio.run(first())
    assert seen and seen[-1] == {"Authorization": f"Bearer {SECRET}"}
    fake.set("fy-box-abcdef012345", status="exited", exit_code=1)

    async def restarted():
        rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32000, 32010), prober=prober)
        await sup.adopt()
        await sup.reconcile_once()  # sees the container exited: a restart is due
        await sup.settle()
        rec = sup.records["abcdef0123456789"]
        rec.next_start_at = 0
        for _ in range(10):  # no desired yet: only restarts of what it adopted
            await sup.reconcile_once()
            await sup.settle()
        state = (rec.state, rec.reason)
        sup.set_desired(desired(keyed(replica("abcdef0123456789", spec), env="API_KEY")))
        rec.next_start_at = 0
        await converge(sup, lambda s: s.records["abcdef0123456789"].state == "healthy")
        await sup.close()
        return state

    runs_before = len(fake.runs())
    state = asyncio.run(restarted())
    assert state[0] == "backoff" and "internal key" in state[1]
    assert len(fake.runs()) == runs_before + 1  # started once, after the key arrived
