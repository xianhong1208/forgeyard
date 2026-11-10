"""The agent's runtimes: environment and GPU isolation, the process rules, containers through a
fake docker CLI (FY-RT-*)."""

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from forgeyard.agent.workloads import logs, plan
from forgeyard.agent.workloads.container import ContainerRuntime, detect
from forgeyard.agent.workloads.process import ProcessRuntime
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.agent.workloads.supervisor import Supervisor
from tests.workload_helpers import FakeDocker, container, converge, desired, gpus_of, replica, task, world_dir

A100 = gpus_of("nvidia-a100x8")
APU = gpus_of("amd-gfx1151-apu")


def amd_card(index=1, rocr="GPU-9e3f2a1b4c5d6e7f", minor=129):
    return {"key": f"amd:{index}", "vendor": "amd", "index": index, "uuid": None, "rocr_uuid": rocr,
            "render_minor": minor, "pci_bus_id": "0000:83:00.0"}


# ------------------------------------------------------------------ GPU variables
def test_fy_rt_001_process_gets_only_its_nvidia_card_by_uuid():
    """FY-RT-001"""
    env = plan.gpu_env([A100[4]], "process")
    assert env["CUDA_VISIBLE_DEVICES"] == A100[4]["uuid"] == env["NVIDIA_VISIBLE_DEVICES"]
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert env["ROCR_VISIBLE_DEVICES"] == "" and env["HIP_VISIBLE_DEVICES"] == ""
    two = plan.gpu_env([A100[1], A100[6]], "process")
    assert two["CUDA_VISIBLE_DEVICES"] == f"{A100[1]['uuid']},{A100[6]['uuid']}"
    no_uuid = dict(A100[3], uuid=None)
    assert plan.gpu_env([no_uuid], "process")["CUDA_VISIBLE_DEVICES"] == "3"


def test_fy_rt_002_no_gpus_means_every_list_is_empty():
    """FY-RT-002"""
    env = plan.gpu_env([], "process")
    assert env == {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "", "NVIDIA_VISIBLE_DEVICES": "void",
                   "ROCR_VISIBLE_DEVICES": "", "HIP_VISIBLE_DEVICES": ""}
    assert plan.gpu_env([], "container") == {"NVIDIA_VISIBLE_DEVICES": "void", "CUDA_VISIBLE_DEVICES": "",
                                             "ROCR_VISIBLE_DEVICES": "", "HIP_VISIBLE_DEVICES": ""}


def test_fy_rt_003_amd_uses_rocr_uuids_and_renumbers_hip():
    """FY-RT-003"""
    env = plan.gpu_env([amd_card(1), amd_card(3, rocr="GPU-0000000000000abc", minor=131)], "process")
    assert env["ROCR_VISIBLE_DEVICES"] == "GPU-9e3f2a1b4c5d6e7f,GPU-0000000000000abc"
    assert env["HIP_VISIBLE_DEVICES"] == "0,1" and env["CUDA_VISIBLE_DEVICES"] == ""
    assert plan.gpu_env([amd_card(2, rocr=None)], "process")["ROCR_VISIBLE_DEVICES"] == "2"


def test_fy_rt_004_process_env_starts_empty():
    """FY-RT-004: only the basics, the declared variables, GPUs, ports and identity."""
    env = plan.process_env({"GREETING": "hi", "PATH": "/opt/x:/usr/bin"}, [A100[4]],
                           [{"name": "http", "host_port": 31000, "port": None}], home="/w/r1", user="nobody",
                           replica_id="r1", workload="web", ordinal=0)
    assert set(env) == {"PATH", "HOME", "LANG", "USER", "LOGNAME", "GREETING", "CUDA_DEVICE_ORDER",
                        "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES",
                        "PORT", "PORT_HTTP", "FORGEYARD_REPLICA", "FORGEYARD_WORKLOAD", "FORGEYARD_REPLICA_INDEX"}
    assert env["PATH"] == "/opt/x:/usr/bin" and env["PORT"] == env["PORT_HTTP"] == "31000"
    assert plan.substitute(["--port", "${PORT}", "${PORT_HTTP}", "${HOME}"], env) == ["--port", "31000", "31000", "${HOME}"]


# ------------------------------------------------------------------ the process runtime
def _proc(tmp_path, **kw):
    kw.setdefault("euid", lambda: 4242)  # "not root": runs as the current user, no switching
    return ProcessRuntime(RecordStore(tmp_path / "state"), tmp_path / "work", **kw)


def test_fy_rt_005_root_agent_without_workload_user_refuses_processes(tmp_path):
    """FY-RT-005"""
    rt = _proc(tmp_path, euid=lambda: 0)
    assert rt.info()["ok"] is False and "--workload-user" in rt.info()["reason"]
    assert _proc(tmp_path, euid=lambda: 0, workload_user="root").info()["reason"] == "--workload-user must not be root"
    assert "does not exist" in _proc(tmp_path, euid=lambda: 0, workload_user="no-such-user-x").info()["reason"]
    info = _proc(tmp_path, euid=lambda: 0, workload_user="nobody", systemd=lambda: False).info()
    assert info["ok"] and info["user"] == "nobody" and info["limits"] == "rlimit"
    assert _proc(tmp_path, euid=lambda: 0, workload_user="nobody", systemd=lambda: True).info()["limits"] == "systemd"
    # An agent that is not root cannot switch users, so it refuses a different one.
    assert "can only run processes as" in _proc(tmp_path, workload_user="nobody").info()["reason"]
    assert "needs a root agent" in _proc(tmp_path, limits="systemd").info()["reason"]


def test_fy_rt_006_a_refused_runtime_never_starts_anything(tmp_path):
    """FY-RT-006: the supervisor reports the reason and starts nothing."""
    rt = _proc(tmp_path, euid=lambda: 0)

    async def scenario():
        sup = Supervisor(tmp_path / "state", {"process": rt}, tick=0.05)
        sup.set_desired(desired(replica("r1", task(script="touch /tmp/never"))))
        await converge(sup, lambda s: s.records["r1"].final)
        rec = sup.records["r1"]
        assert rec.state == "failed" and "--workload-user" in rec.reason and rec.pid is None and sup.started == 0

    asyncio.run(scenario())


def test_fy_rt_007_no_agent_env_leaks_into_a_process(tmp_path, monkeypatch):
    """FY-RT-007: a real process prints its environment: nothing of the agent's is there."""
    monkeypatch.setenv("FY_AGENT_SECRET", "s3cr3t")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1,2,3,4,5,6,7")
    rt = _proc(tmp_path)

    async def scenario():
        sup = Supervisor(tmp_path / "state", {"process": rt}, tick=0.05)
        spec = task(env={"DECLARED": "yes"}, runtime={"type": "process", "command": "env"})
        sup.set_desired(desired(replica("r1", spec, gpus=[A100[4]])))
        await converge(sup, lambda s: s.records["r1"].final)
        text, _ = rt.tail(sup.records["r1"], 100)
        return dict(line.split("=", 1) for line in text.splitlines())

    env = asyncio.run(scenario())
    assert "FY_AGENT_SECRET" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["CUDA_VISIBLE_DEVICES"] == A100[4]["uuid"] and env["DECLARED"] == "yes"
    assert set(env) - {"PWD"} == {"PATH", "HOME", "LANG", "USER", "LOGNAME", "DECLARED", "CUDA_DEVICE_ORDER",
                                  "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES",
                                  "HIP_VISIBLE_DEVICES", "FORGEYARD_REPLICA", "FORGEYARD_WORKLOAD",
                                  "FORGEYARD_REPLICA_INDEX"}


@pytest.mark.skipif(os.geteuid() != 0, reason="switching users needs a root agent")
def test_fy_rt_008_as_root_processes_run_as_the_workload_user(tmp_path):
    """FY-RT-008: a real root agent, a real switch to nobody, no supplementary groups."""
    work = world_dir()
    try:
        rt = ProcessRuntime(RecordStore(tmp_path / "state"), work / "work", workload_user="nobody",
                            systemd=lambda: False)

        async def scenario():
            sup = Supervisor(tmp_path / "state", {"process": rt}, tick=0.05)
            sup.set_desired(desired(replica("r1", task(script="id -u; id -G; pwd; echo $HOME"))))
            await converge(sup, lambda s: s.records["r1"].final)
            rec = sup.records["r1"]
            assert rec.state == "exited", rec.reason
            return rt.tail(rec, 10)[0].splitlines(), rec

        lines, rec = asyncio.run(scenario())
        assert lines[0] == "65534" and lines[1] == "65534"
        assert lines[2] == lines[3] == str(work / "work" / "r1")
        assert oct(os.stat(work / "work" / "r1").st_mode & 0o777) == "0o700"
        assert os.stat(work / "work" / "r1").st_uid == 65534
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_fy_rt_009_memory_cap_without_gpus_only():
    """FY-RT-009"""
    assert plan.ulimits(512 * 1024**2, []) == ["-c 0", "-v 524288"]
    assert plan.ulimits(512 * 1024**2, [A100[0]]) == ["-c 0"]
    assert plan.ulimits(None, []) == ["-c 0"]
    argv = plan.process_argv("/usr/bin/python3", ["-c", "x"], "/w/.exit", ["-c 0", "-v 10"])
    assert argv[:2] == ["/bin/sh", "-c"] and "ulimit -v 10 || exit 125" in argv[2]
    assert argv[3:] == ["forgeyard-replica", "/w/.exit", "/usr/bin/python3", "-c", "x"]
    r = subprocess.run(plan.process_argv("/bin/sh", ["-c", "exit 7"], os.devnull, ["-c 0"]))
    assert r.returncode == 7


def test_fy_rt_010_systemd_scope_argv():
    """FY-RT-010: limits in a scope, then setpriv to the workload user."""
    argv = plan.systemd_argv("forgeyard-abc-1-1", cpu=2.5, memory=4 * 1024**3, uid=65534, gid=65534, groups=[],
                             inner=["/bin/sh", "-c", "x"])
    assert argv[:5] == ["systemd-run", "--scope", "--quiet", "--collect", "--unit=forgeyard-abc-1-1"]
    assert "MemoryMax=4294967296" in argv and "CPUQuota=250%" in argv
    i = argv.index("setpriv")
    assert argv[i:i + 4] == ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"]
    assert argv[-3:] == ["/bin/sh", "-c", "x"]


def test_fy_rt_011_log_trim_and_tail(tmp_path):
    """FY-RT-011: the pair of files never holds more than the budget; the tail spans both."""
    d = tmp_path / "logs"
    d.mkdir()
    with open(d / logs.LOG_NAME, "w") as fh:
        fh.write("".join(f"line {i}\n" for i in range(1000)))
    assert logs.trim(d, 4096) is True
    assert (d / logs.LOG_NAME).stat().st_size == 0
    with open(d / logs.LOG_NAME, "a") as fh:
        fh.write("new 1\nnew 2\n")
    text, truncated = logs.tail(d, 3)
    assert text == "line 999\nnew 1\nnew 2" and truncated
    assert logs.trim(d, 10**6) is False
    assert logs.tail(tmp_path / "none", 5) == ("", False)


# ------------------------------------------------------------------ containers (fake docker)
def _ctr(tmp_path, fake: FakeDocker, **kw):
    return ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path), **kw)


def test_fy_rt_012_detect_engine_and_nvidia_support(tmp_path):
    """FY-RT-012"""
    fake = FakeDocker(tmp_path / "bin")
    info = detect(str(fake.path), cdi=lambda: False)
    assert info["ok"] and info["engine"] == "docker" and info["nvidia"] and info["gpu_mode"] == "gpus"
    fake.info({"ServerVersion": "27", "Runtimes": {"runc": {}}})
    assert detect(str(fake.path), which=lambda n: None, cdi=lambda: False)["nvidia"] is False
    assert detect(str(fake.path), which=lambda n: None, cdi=lambda: True)["gpu_mode"] == "cdi"
    assert detect("none")["ok"] is False
    assert detect("auto", which=lambda n: None)["reason"] == "docker not found; podman not found"


def test_fy_rt_013_container_argv_no_gpu(tmp_path):
    """FY-RT-013: no --privileged, no-new-privileges, ports on one address, env from a file that
    is gone afterwards, no GPU and NVIDIA_VISIBLE_DEVICES=void."""
    fake = FakeDocker(tmp_path / "bin")
    rt = _ctr(tmp_path, fake)

    async def scenario():
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, bind_address="10.1.2.3",
                         port_range=(32000, 32010))
        sup.set_desired(desired(replica("abcdef0123456789", container(env={"MODE": "prod"}))))
        await converge(sup, lambda s: s.records["abcdef0123456789"].state == "running")
        return sup.records["abcdef0123456789"]

    rec = asyncio.run(scenario())
    [run] = fake.runs()
    assert "--privileged" not in run and run[run.index("--security-opt") + 1] == "no-new-privileges"
    assert run[run.index("-p") + 1] == "10.1.2.3:32000:8000/tcp"
    assert "--gpus" not in run and "--device" not in run
    assert run[run.index("python:3.13-slim") + 1:] == ["python", "-m", "http.server", "8000"]
    [c] = fake.state().values()
    assert c["env"]["MODE"] == "prod" and c["env"]["NVIDIA_VISIBLE_DEVICES"] == "void"
    assert c["env"]["CUDA_VISIBLE_DEVICES"] == "" and c["env"]["FORGEYARD_REPLICA"] == "abcdef0123456789"
    assert c["labels"]["forgeyard.agent"] == "agent-1" and c["labels"]["forgeyard.generation"] == "1"
    assert c["name"] == rec.container == "fy-box-abcdef012345"
    assert not (tmp_path / "state" / "logs" / "abcdef0123456789" / "env").exists()
    assert rec.ports == [{"name": "http", "port": 8000, "host_port": 32000, "address": "10.1.2.3"}]


def test_fy_rt_014_container_gets_only_its_gpus(tmp_path):
    """FY-RT-014: --gpus with the assigned UUID only (or CDI devices); AMD: /dev/kfd plus only
    the assigned render node."""
    fake = FakeDocker(tmp_path / "bin")
    rt = _ctr(tmp_path, fake)
    gpu_spec = container(resources={"gpus": {"count": 1}})

    async def scenario(rt, gpus, rid):
        sup = Supervisor(tmp_path / rid, {"container": rt}, tick=0.05, bind_address="127.0.0.1")
        sup.set_desired(desired(replica(rid, gpu_spec, gpus=gpus)))
        await converge(sup, lambda s: s.records[rid].state == "running")

    asyncio.run(scenario(rt, [A100[4]], "r-nv-000000001"))
    run = fake.runs()[-1]
    assert run[run.index("--gpus") + 1] == f'"device={A100[4]["uuid"]}"' and run.count("--gpus") == 1
    env = next(c for c in fake.state().values() if c["name"].endswith("r-nv-0000000"))["env"]
    assert env["NVIDIA_VISIBLE_DEVICES"] == A100[4]["uuid"] and "CUDA_VISIBLE_DEVICES" not in env

    cdi = _ctr(tmp_path, fake, info={**rt.info(), "path": str(fake.path), "gpu_mode": "cdi"})
    asyncio.run(scenario(cdi, [A100[2], A100[5]], "r-cdi-00000001"))
    run = fake.runs()[-1]
    devices = [run[i + 1] for i, a in enumerate(run) if a == "--device"]
    assert devices == [f"nvidia.com/gpu={A100[2]['uuid']}", f"nvidia.com/gpu={A100[5]['uuid']}"] and "--gpus" not in run

    asyncio.run(scenario(rt, [amd_card(1, minor=129)], "r-amd-00000001"))
    run = fake.runs()[-1]
    devices = [run[i + 1] for i, a in enumerate(run) if a == "--device"]
    assert devices == ["/dev/kfd", "/dev/dri/renderD129"] and "--gpus" not in run


def test_fy_rt_015_nvidia_gpu_without_support_fails_clearly(tmp_path):
    """FY-RT-015"""
    fake = FakeDocker(tmp_path / "bin")
    fake.info({"ServerVersion": "27", "Runtimes": {"runc": {}}})
    rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="a", engine=str(fake.path),
                          which=lambda n: str(fake.path) if n == str(fake.path) else None, cdi=lambda: False)
    rec = Record(id="r1", generation=1, workload="g", ordinal=0, kind="task", runtime="container",
                 spec=replica("r1", {"name": "g", "kind": "task", "runtime": {"type": "container", "image": "x"}})["spec"],
                 gpus=[A100[0]])
    from forgeyard.agent.workloads.runtime import StartError

    with pytest.raises(StartError, match="cannot pass NVIDIA GPUs"):
        rt.start(rec, "127.0.0.1")
    assert fake.runs() == []


def test_fy_rt_016_failed_pull_is_reported(tmp_path):
    """FY-RT-016"""
    fake = FakeDocker(tmp_path / "bin")
    rt = _ctr(tmp_path, fake)

    async def scenario():
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05)
        sup.set_desired(desired(replica("r1", {"name": "bad", "kind": "task",
                                               "runtime": {"type": "container", "image": "fail/nope"}})))
        await converge(sup, lambda s: s.records["r1"].final)
        return sup.records["r1"]

    rec = asyncio.run(scenario())
    assert rec.state == "failed" and "pull access denied" in rec.reason


def test_fy_rt_017_logs_from_the_engine(tmp_path):
    """FY-RT-017"""
    fake = FakeDocker(tmp_path / "bin")
    rt = _ctr(tmp_path, fake)

    async def scenario():
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05)
        sup.set_desired(desired(replica("r1", container())))
        await converge(sup, lambda s: s.records["r1"].state == "running")
        return await sup.logs("r1", 2), await sup.logs("nope", 2)

    got, missing = asyncio.run(scenario())
    assert got == {"ok": True, "text": "fy-box-r1 line 4\nfy-box-r1 line 5", "truncated": True}
    assert missing["ok"] is False


def test_fy_rt_018_status_report_json_fits_the_protocol(tmp_path):
    """FY-RT-018: what the agent sends is accepted by the server's message model."""
    from forgeyard.machines.messages import StatusIn

    rec = Record(id="r1", generation=2, workload="w", ordinal=0, kind="task", runtime="process", spec={},
                 state="failed", exit_code=3, final=True, attempt=3,
                 ports=[{"name": "http", "port": None, "host_port": 31000, "address": "10.0.0.2"}])
    msg = {"v": 1, "type": "status", "full": True, "replicas": [rec.public(with_tail="  indented\nlast")]}
    parsed = StatusIn.model_validate(json.loads(json.dumps(msg)))
    assert parsed.replicas[0].log_tail == "  indented\nlast" and parsed.replicas[0].ports[0].host_port == 31000


def test_fy_rt_019_health_probes():
    """FY-RT-019: HTTP 2xx/3xx healthy, 5xx and refused not; a TCP self-connect is not a service."""
    import http.server
    import threading

    from forgeyard.agent.workloads.health import probe

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503 if self.path == "/bad" else 200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert probe("http", "127.0.0.1", port, "/") is None
        assert probe("http", "127.0.0.1", port, "/bad") == "GET /bad answered 503"
        assert probe("tcp", "127.0.0.1", port) is None
    finally:
        srv.shutdown()
        srv.server_close()
    assert "GET /" in probe("http", "127.0.0.1", port, "/", timeout=1)
    assert probe("tcp", "127.0.0.1", port, timeout=1).startswith(f"tcp 127.0.0.1:{port}")


def test_fy_rt_020_gpu_probe_script(monkeypatch, capsys):
    """FY-RT-020: scripts/gpu_probe.py formats UUIDs like nvidia-smi and says plainly when there
    is no NVIDIA driver (exit 2). The GPU part runs on Tony's machine (docs/acceptance-m2.md)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "gpu_probe.py"
    spec = importlib.util.spec_from_file_location("gpu_probe", path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.format_uuid(bytes(range(16))) == "GPU-00010203-0405-0607-0809-0a0b0c0d0e0f"
    monkeypatch.setattr(probe, "load_cuda", lambda: None)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-x")
    assert probe.main(["--hold", "0"]) == 2
    out = capsys.readouterr().out
    assert "CUDA_VISIBLE_DEVICES=GPU-x" in out and "libcuda.so.1 not found" in out


def test_fy_rt_021_a_reused_pid_is_never_mistaken_for_a_replica(tmp_path):
    """FY-RT-021: after a restart the recorded pid belongs to a stranger (same pid, other start
    time): it is not adopted as alive and never signalled; another boot counts as gone too."""
    from forgeyard.agent.workloads.process import boot_id, proc_stat

    stranger = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        start = proc_stat(stranger.pid)[2]
        rt = _proc(tmp_path)
        rec = Record(id="r1", generation=1, workload="w", ordinal=0, kind="task", runtime="process", spec={},
                     pid=stranger.pid, pgid=stranger.pid, proc_start=start - 1, boot_id=boot_id())
        obs = rt.observe(rec)
        assert obs.alive is False and obs.missing is True
        rt.stop(rec, 0.2)
        assert stranger.poll() is None  # untouched
        rec.proc_start = start
        assert rt.observe(rec).alive is True
        rec.boot_id = "another-boot"
        assert rt.observe(rec).alive is False
    finally:
        stranger.kill()
        stranger.wait(5)
