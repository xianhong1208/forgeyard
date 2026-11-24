"""Can new processes still open the GPUs (1.4.5, #202; FY-GACC-*): the agent's probe, run as the workload account the
way a process workload starts; what it reports; the setup advisor's error; and placement, which keeps process
workloads (not containers) off GPUs a new process cannot open, and places them again once it can."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeyard.agent import gpu_check as g
from forgeyard.agent.workloads.process import ProcessRuntime
from forgeyard.agent.workloads.records import RecordStore
from forgeyard.db import Node, new_session
from forgeyard.machines import gpu_access
from forgeyard.machines.messages import GpuCheckIn, HelloIn
from forgeyard.machines.protocol import FEATURE_GPU_CHECK, SERVER_FEATURES
from tests.machines_helpers import connect, hello, joined, machine
from tests.test_advisor import edit, findings, node
from tests.workload_helpers import PROCESS_OK, container, make_node, task

FAIL = {"ok": False, "at": "2026-11-22T01:00:00+00:00", "user": "forgeyard-run",
        "devices": [{"path": "/dev/nvidiactl", "vendor": "nvidia", "ok": True},
                    {"path": "/dev/nvidia0", "vendor": "nvidia", "ok": False, "error": "Operation not permitted"}],
        "reason": "/dev/nvidia0: Operation not permitted"}
OK = {**FAIL, "ok": True, "devices": [{**d, "ok": True} for d in FAIL["devices"]]}
OK["devices"][1].pop("error")
OK.pop("reason")


def runtime(tmp_path: Path) -> ProcessRuntime:
    """The process runtime as the agent builds it: as root, processes run as an unprivileged account (nobody here),
    otherwise as this user."""
    user = "nobody" if os.geteuid() == 0 else None
    rt = ProcessRuntime(RecordStore(tmp_path / "state"), tmp_path / "work", workload_user=user, limits="rlimit")
    assert rt.info()["ok"], rt.info()
    return rt


@pytest.fixture
def reachable():
    """A directory the workload account can reach (pytest's own are root's alone when the tests run as root)."""
    import shutil
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="fy-gacc-"))
    path.chmod(0o755)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def nvidia_cards(n: int = 2) -> list[dict]:
    return [{"vendor": "nvidia", "index": i, "key": f"nvidia:GPU-{i}"} for i in range(n)] + [{"vendor": "cpu"}]


# ------------------------------------------------------------------ the agent's side
def test_fy_gacc_001_which_device_nodes_a_new_process_opens(tmp_path):
    """FY-GACC-001: NVIDIA: nvidiactl always (a missing one fails the check), nvidia-uvm when there, every nvidiaN by
    number (not nvidia-caps, nvidia-modeset or nvidia-uvm-tools); AMD: kfd and every render node. None for a CPU-only
    machine."""
    dev = tmp_path / "dev"
    (dev / "dri").mkdir(parents=True)
    for name in ("nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset", "nvidia10", "nvidia2", "nvidia-caps",
                 "kfd", "dri/renderD129", "dri/renderD128", "dri/card0"):
        (dev / name).write_text("")
    nv = [("nvidia", str(dev / n)) for n in ("nvidiactl", "nvidia-uvm", "nvidia2", "nvidia10")]
    amd = [("amd", str(dev / n)) for n in ("kfd", "dri/renderD128", "dri/renderD129")]
    assert g.device_nodes({"nvidia"}, dev) == nv
    assert g.device_nodes({"amd"}, dev) == amd
    assert g.device_nodes({"nvidia", "amd"}, dev) == nv + amd
    assert g.device_nodes(set(), dev) == []
    bare = tmp_path / "bare"
    bare.mkdir()
    assert g.device_nodes({"nvidia"}, bare) == [("nvidia", str(bare / "nvidiactl"))]
    check = g.GpuCheck(None, dev=dev)
    assert check.nodes([{"vendor": "cpu"}]) == [] and check.nodes(nvidia_cards()) == nv


def test_fy_gacc_002_the_probes_lines_become_one_result_per_device():
    """FY-GACC-002: `ok` / `fail <message>` per path in order; the shell's prefix is cut from the message; a line
    that never came (the probe ended early) is a failure with what it said; the summary names the first failure."""
    nodes = [("nvidia", "/dev/nvidiactl"), ("nvidia", "/dev/nvidia0"), ("nvidia", "/dev/nvidia1")]
    out = g.parse("ok\nfail sh: 1: cannot open /dev/nvidia0: Operation not permitted \n", nodes, returncode=1,
                  stderr="killed")
    assert out == [{"path": "/dev/nvidiactl", "vendor": "nvidia", "ok": True},
                   {"path": "/dev/nvidia0", "vendor": "nvidia", "ok": False, "error": "Operation not permitted"},
                   {"path": "/dev/nvidia1", "vendor": "nvidia", "ok": False, "error": "killed"}]
    assert g.summary(out) == "/dev/nvidia0 and 1 more: Operation not permitted"
    assert g.parse("", nodes[:1], returncode=125)[0]["error"] == "the probe ended (exit 125)"
    assert g.summary(out[:1]) is None
    assert g.parse("fail /bin/sh: /dev/x: No such file or directory", [("amd", "/dev/x")])[0]["error"] == \
        "No such file or directory"


def test_fy_gacc_003_the_probe_opens_the_devices_as_the_workload_account(tmp_path, reachable, monkeypatch):
    """FY-GACC-003: the real probe (/bin/sh, through the process runtime: the workload account, a new session, an
    empty environment): a node that account may open read-write is fine; one only root (or nobody) may open, a missing
    one and a directory fail with the shell's reason. As root it really runs as the unprivileged account."""
    monkeypatch.setenv("FORGEYARD_SECRET", "the agent's own")  # never passed on
    rt = runtime(tmp_path)
    ok, locked = reachable / "ok-node", reachable / "locked-node"
    ok.write_text("")
    ok.chmod(0o666)
    locked.write_text("")
    locked.chmod(0o600 if os.geteuid() == 0 else 0o000)  # root's alone, or no one's
    nodes = [("nvidia", str(ok)), ("nvidia", str(locked)), ("nvidia", str(reachable / "gone")),
             ("amd", str(reachable))]
    done = rt.probe(["/bin/sh", "-c", g.PROBE_SCRIPT, "probe", *[p for _, p in nodes]], vendors={"nvidia"}, timeout=10)
    out = g.parse(done.stdout, nodes, returncode=done.returncode, stderr=done.stderr)
    assert [d["ok"] for d in out] == [True, False, False, False], out
    assert "Permission denied" in out[1]["error"] and "No such file" in out[2]["error"]
    assert out[3]["error"]  # a directory cannot be opened for writing
    who = rt.probe(["/bin/sh", "-c", "id -u; echo \"$PATH|$HOME|${FORGEYARD_SECRET:-}\""], vendors=set(), timeout=10)
    assert who.stdout.split() == [str(rt.uid), "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin|/|"]
    if os.geteuid() == 0:
        assert rt.uid != 0


def test_fy_gacc_004_as_root_with_systemd_the_probe_runs_in_a_scope(tmp_path, monkeypatch):
    """FY-GACC-004: with systemd limits the probe starts like a replica: systemd-run --scope, then setpriv to the
    workload account with the device groups (AMD's) or none; without, the account and groups are switched by Popen."""
    rt = ProcessRuntime(RecordStore(tmp_path / "s"), tmp_path / "w", workload_user="nobody", limits="systemd",
                        euid=lambda: 0, systemd=lambda: True)
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (seen.append((argv, kw)),
                                                                subprocess.CompletedProcess(argv, 0, "ok\n", ""))[1])
    monkeypatch.setattr(ProcessRuntime, "_device_gids", staticmethod(lambda gpus: [44, 110] if any(x["vendor"] == "amd" for x in gpus) else []))
    rt.probe(["/bin/sh", "-c", "x"], vendors={"amd"}, timeout=5)
    argv, kw = seen[-1]
    assert argv[:4] == ["systemd-run", "--scope", "--quiet", "--collect"] and argv[4].startswith("--unit=forgeyard-gpu")
    assert argv[5:] == ["--", "setpriv", f"--reuid={rt.uid}", f"--regid={rt.gid}", "--groups=44,110", "--",
                        "/bin/sh", "-c", "x"]
    assert "user" not in kw and kw["start_new_session"] is True and kw["timeout"] == 5
    assert set(kw["env"]) == {"PATH", "LANG", "USER", "LOGNAME", "HOME"}
    rt.limits = "rlimit"
    rt.probe(["/bin/sh", "-c", "x"], vendors={"nvidia"}, timeout=5)
    argv, kw = seen[-1]
    assert argv == ["/bin/sh", "-c", "x"] and (kw["user"], kw["group"], kw["extra_groups"]) == (rt.uid, rt.gid, [])
    off = ProcessRuntime(RecordStore(tmp_path / "s2"), tmp_path / "w2", workload_user=None, euid=lambda: 0)
    with pytest.raises(OSError):
        off.probe(["true"], vendors=set(), timeout=5)


def test_fy_gacc_005_the_check_result_and_when_nothing_is_checked(tmp_path, reachable, caplog):
    """FY-GACC-005: a real run over files standing in for device nodes: ok, then (the access gone) a failure with the
    reason, logged once as an error with the fix; back again, logged once. Nothing to check (None): no GPUs, the
    process runtime off, a simulated inventory without SIMULATION_DEVICES_ENV. A probe that hangs or cannot start
    fails every device."""
    rt = runtime(tmp_path)
    dev = reachable / "dev"
    dev.mkdir()
    dev.chmod(0o755)
    for name in ("nvidiactl", "nvidia0", "nvidia1"):
        (dev / name).write_text("")
        (dev / name).chmod(0o666)
    check = g.GpuCheck(rt, dev=dev)
    first = check.run(nvidia_cards())
    assert first["ok"] is True and "reason" not in first and first["user"] == rt.info()["user"]
    assert [d["path"] for d in first["devices"]] == [str(dev / n) for n in ("nvidiactl", "nvidia0", "nvidia1")]
    (dev / "nvidia1").chmod(0o000 if os.geteuid() else 0o600)
    with caplog.at_level("INFO", logger="forgeyard.agent"):
        lost = check.run(nvidia_cards())
        check.run(nvidia_cards())
    assert lost["ok"] is False and lost["reason"].startswith(f"{dev / 'nvidia1'}: ") and check.last["ok"] is False
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "systemctl daemon-reload" in errors[0].getMessage()
    (dev / "nvidia1").chmod(0o666)
    caplog.clear()
    with caplog.at_level("INFO", logger="forgeyard.agent"):
        assert check.run(nvidia_cards())["ok"] is True
    assert [r.getMessage() for r in caplog.records] == ["New processes can open the GPUs again."]
    assert check.run([{"vendor": "cpu"}]) is None and check.last is None
    off = ProcessRuntime(RecordStore(tmp_path / "s2"), tmp_path / "w2", workload_user=None, euid=lambda: 0)
    assert g.GpuCheck(off, dev=dev).run(nvidia_cards()) is None
    assert g.GpuCheck(rt, simulated=True, env={}, dev=dev).run(nvidia_cards()) is None
    sim_env = {"FORGEYARD_AGENT_SIMULATION": "1", g.SIMULATION_DEVICES_ENV: f"{dev / 'nvidiactl'} amd={reachable / 'x'}"}
    sim = g.GpuCheck(rt, simulated=True, env=sim_env, dev=dev).run(nvidia_cards())
    assert [(d["vendor"], d["ok"]) for d in sim["devices"]] == [("nvidia", True), ("amd", False)]
    assert g.GpuCheck(rt, simulated=True, env={g.SIMULATION_DEVICES_ENV: str(dev / "nvidiactl")},
                      dev=dev).run(nvidia_cards()) is None  # only with simulation allowed

    class Stuck:
        def info(self):
            return {"ok": True, "user": "u"}

        def probe(self, argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])

    hung = g.GpuCheck(Stuck(), dev=dev).run(nvidia_cards())
    assert hung["ok"] is False and all("no answer within" in d["error"] for d in hung["devices"])

    class Broken(Stuck):
        def probe(self, argv, **kw):
            raise OSError(8, "Exec format error")

    assert "did not start: Exec format error" in g.GpuCheck(Broken(), dev=dev).run(nvidia_cards())["reason"]


def test_fy_gacc_006_changed_ignores_only_the_time():
    """FY-GACC-006: the server hears again when the verdict or a failing device changed, not for a newer time."""
    assert not g.changed(FAIL, {**FAIL, "at": "later"})
    assert g.changed(FAIL, OK) and g.changed(None, FAIL) and g.changed(FAIL, None) and not g.changed(None, None)
    other = json.loads(json.dumps(FAIL))
    other["devices"][1]["error"] = "No such file or directory"
    assert g.changed(FAIL, other)


def test_fy_gacc_007_the_agent_reports_it_in_hello_and_sends_changes_to_servers_that_list_it(tmp_path, monkeypatch):
    """FY-GACC-007: hello's runtime_info carries the last result (older servers drop the key); the loop probes every
    gpu_check_every seconds and sends `gpu_check` only when the result changed and only to a server that lists
    "gpu_check"; `{"ok": null}` when there is nothing to check any more."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.workloads.supervisor import Supervisor

    monkeypatch.setenv(SIMULATION_ENV, "1")
    inv = SimulatedInventory("nvidia-rtx4090x2")
    sup = Supervisor(tmp_path / "state", {"process": runtime(tmp_path)}, tick=0.05, port_range=(32180, 32189))
    agent = Agent("http://127.0.0.1:9", "fym_x", inv, supervisor=sup)
    assert agent.gpu_check is not None and agent.gpu_check.simulated is True
    assert "gpu_check" not in agent.hello(inv.full())["runtime_info"]
    agent.gpu_check.last = FAIL
    assert agent.hello(inv.full())["runtime_info"]["gpu_check"] == FAIL
    assert "gpu_check" not in Agent("http://127.0.0.1:9", "fym_x", inv).hello(inv.full()).get("runtime_info", {})
    results: list = []

    def next_result(acc):
        return results.pop(0) if len(results) > 1 else results[0]

    monkeypatch.setattr(agent.gpu_check, "run", next_result)
    sent = []

    class Ws:
        async def send(self, text):
            sent.append(json.loads(text))

    async def loop(features):
        agent.server_features = features
        agent.gpu_check.last = FAIL  # what hello carried
        agent.gpu_check_every = 0.01
        task_ = asyncio.create_task(agent._gpu_check_loop(Ws()))
        await asyncio.sleep(0.3)
        task_.cancel()
        await asyncio.gather(task_, return_exceptions=True)

    results[:] = [FAIL, {**FAIL, "at": "later"}, OK, OK, None]
    asyncio.run(loop({FEATURE_GPU_CHECK}))
    assert [m["gpu_check"] for m in sent] == [OK, {"ok": None}] and {m["type"] for m in sent} == {"gpu_check"}
    sent.clear()
    results[:] = [OK, FAIL]
    asyncio.run(loop(set()))  # an older server
    assert sent == []
    assert FEATURE_GPU_CHECK in SERVER_FEATURES


# ------------------------------------------------------------------ the server's side
def test_fy_gacc_008_hello_and_message_are_validated_and_kept(owner):
    """FY-GACC-008: the server keeps the check from hello and from `gpu_check` messages, with only the known fields
    (control characters and unknown vendors or paths dropped; an agent saying ok with a failing device is not ok);
    `ok` null removes it; an agent that sends nothing (before 1.4.5) has none."""
    junk = {**FAIL, "extra": "x", "reason": "a\nb", "devices": FAIL["devices"] + [
        {"path": "/dev/x; reboot", "vendor": "nvidia", "ok": False}, {"path": "/dev/kfd", "vendor": "intel", "ok": False}]}
    kept = HelloIn.model_validate(hello(features=["workloads"], runtime_info={"gpu_check": junk})).runtime_info
    assert kept["gpu_check"] == {**FAIL, "reason": "a b"}
    liar = {**FAIL, "ok": True}
    assert GpuCheckIn.model_validate({"v": 1, "type": "gpu_check", "gpu_check": liar}).gpu_check["ok"] is False
    assert GpuCheckIn.model_validate({"v": 1, "type": "gpu_check", "gpu_check": {"ok": None}}).gpu_check is None
    assert "gpu_check" not in HelloIn.model_validate(hello(runtime_info={"gpu_check": {"ok": "no"}})).runtime_info
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello(features=["workloads"], runtimes=["process"],
                           runtime_info={"process": {"ok": True}, "gpu_check": FAIL}))
        assert ws.receive_json()["type"] == "welcome"
        assert machine(owner, j["node_id"])["runtime_info"]["gpu_check"] == FAIL
        ws.send_json({"v": 1, "type": "gpu_check", "gpu_check": OK})
        ws.send_json({"v": 1, "type": "heartbeat", "seq": 1, "metrics": []})
        for _ in range(100):
            if machine(owner, j["node_id"])["runtime_info"]["gpu_check"]["ok"]:
                break
            time.sleep(0.05)
        assert machine(owner, j["node_id"])["runtime_info"]["gpu_check"] == OK
        ws.send_json({"v": 1, "type": "gpu_check", "gpu_check": {"ok": None}})
        ws.send_json({"v": 1, "type": "heartbeat", "seq": 2, "metrics": []})
        for _ in range(100):
            if "gpu_check" not in machine(owner, j["node_id"])["runtime_info"]:
                break
            time.sleep(0.05)
        assert "gpu_check" not in machine(owner, j["node_id"])["runtime_info"]
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello(features=["workloads"], runtimes=["process"], runtime_info={"process": {"ok": True}}))
        assert ws.receive_json()["type"] == "welcome"
    assert "gpu_check" not in machine(owner, j["node_id"])["runtime_info"]


def test_fy_gacc_009_setup_health_shows_an_error_with_the_fix(owner):
    """FY-GACC-009: an online machine whose check fails: an error (not snoozable) naming the machine, the device and
    the fix; for an agent container, restart it and update it with the machine page's command (the link opens it);
    otherwise check the permissions. None when the check passes, is unknown, or the machine is offline."""
    box = node("gpu-01", runtime_info={**PROCESS_OK, "gpu_check": FAIL, "install": {"method": "container", "known": True}})
    [f] = findings()["machine-gpu-access-lost"]
    assert (f.severity, f.snoozable, f.scope, f.pages) == ("error", False, f"machine:{box}", ("machines",))
    assert f.title == "New processes on gpu-01 cannot open its GPUs"
    assert f.fix_url == f"/machines/{box}#update" and f.fix_label == "Update command"
    for part in ("(/dev/nvidia0: Operation not permitted)", "systemctl daemon-reload", "Restart the agent container",
                 "update it with the command on the machine's page, which keeps the access", "--device",
                 "no process workload or process engine is placed on its NVIDIA GPUs; containers are not affected"):
        assert part in f.detail, part
    edit(Node, box, runtime_info={**PROCESS_OK, "gpu_check": FAIL, "install": {"method": "install.sh", "known": True}})
    [f] = findings()["machine-gpu-access-lost"]
    assert "running as forgeyard-run" in f.detail and "device permissions" in f.detail and "container" not in f.title
    edit(Node, box, status="offline")
    assert "machine-gpu-access-lost" not in findings()
    edit(Node, box, status="online", runtime_info={**PROCESS_OK, "gpu_check": OK})
    assert "machine-gpu-access-lost" not in findings()
    edit(Node, box, runtime_info=PROCESS_OK)
    assert "machine-gpu-access-lost" not in findings()


def _pending(c, spec) -> tuple[int, dict]:
    from tests.workload_helpers import create_workload

    w = create_workload(c, spec)["workload"]
    return w["id"], w["replica_list"][0]


def _replica(c, wid) -> dict:
    return c.get(f"/api/workloads/{wid}").json()["workload"]["replica_list"][0]


def test_fy_gacc_010_process_workloads_stay_off_gpus_new_processes_cannot_open(owner, monkeypatch):
    """FY-GACC-010: while the check fails, a process workload wanting a GPU there waits, with the reason (containers
    still run there; a CPU-only process too); pinned there it is still created (not refused). Once the agent says the
    GPUs open again, both are placed at once (not only by the next liveness sweep, switched off here)."""
    from forgeyard.machines import liveness

    monkeypatch.setattr(liveness, "_sweep_once", lambda: None)
    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello(features=["workloads"], runtimes=["process", "container"],
                           runtime_info={**PROCESS_OK, "gpu_check": FAIL}))
        assert ws.receive_json()["type"] == "welcome"
        nid = j["node_id"]
        job, rep = _pending(owner, task("gpu-job", resources={"gpus": {"count": 1}}))
        assert rep["machine"] is None
        assert ("new processes there cannot open its NVIDIA GPUs (see Setup health): process workloads are not "
                "placed on them meanwhile; containers still run there") in rep["reason"]
        pinned, rep = _pending(owner, task("pinned", machine_id=nid, resources={"gpus": {"vendor": "nvidia", "count": 1}}))
        assert rep["machine"] is None and "cannot open its NVIDIA GPUs" in rep["reason"]
        _, boxed = _pending(owner, container("boxed", resources={"gpus": {"count": 1}}))
        assert boxed["machine"]["id"] == nid and len(boxed["gpus"]) == 1
        _, cpu = _pending(owner, task("cpu-job"))
        assert cpu["machine"]["id"] == nid
        ws.send_json({"v": 1, "type": "gpu_check", "gpu_check": OK})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (_replica(owner, job)["machine"] and _replica(owner, pinned)["machine"]):
            time.sleep(0.05)
        for wid in (job, pinned):
            rep = _replica(owner, wid)
            assert rep["machine"]["id"] == nid and len(rep["gpus"]) == 1, rep


def test_fy_gacc_011_vendors(owner, monkeypatch):
    """FY-GACC-011: the failing vendors are those of the devices that failed; a failure that names no device holds
    back every GPU vendor of the machine; a check that failed for NVIDIA only leaves a vendor-less process workload
    the working AMD card of a machine with both, and nothing when both fail."""
    db = new_session()
    try:
        nid = make_node("rtx", "nvidia-rtx4090x2", runtime_info={**PROCESS_OK, "gpu_check": FAIL})
        n = db.get(Node, nid)
        assert gpu_access.failing_vendors(n) == {"nvidia"}
        n.runtime_info = {**PROCESS_OK, "gpu_check": {"ok": False, "devices": []}}
        db.commit()
        assert gpu_access.failing_vendors(db.get(Node, nid)) == {"nvidia"}  # every GPU vendor it has
        n.runtime_info = {**PROCESS_OK, "gpu_check": OK}
        db.commit()
        assert gpu_access.failing_vendors(db.get(Node, nid)) == set()
    finally:
        db.close()
    from forgeyard.workloads import placement
    from forgeyard.workloads.spec import WorkloadSpec

    cards = [SimpleNamespace(vendor="nvidia", index=0, key="n0"), SimpleNamespace(vendor="amd", index=0, key="a0"),
             SimpleNamespace(vendor="cpu", index=0, key="c")]
    fake = SimpleNamespace(accelerators=cards, gpu_allowlist=None, runtime_info={"gpu_check": FAIL})
    spec = WorkloadSpec.model_validate(task("any", resources={"gpus": {"count": 1}}))
    monkeypatch.setattr(placement, "is_allowed", lambda node, a: True)
    chosen, why = placement.choose_gpus(fake, spec, {})
    assert why is None and [a.key for a in chosen] == ["a0"]  # AMD sorts first anyway: skip it to see the rule
    chosen, why = placement.choose_gpus(fake, spec, {}, skip_vendors=frozenset({"amd"}))
    assert why is None and [a.key for a in chosen] == ["n0"]
    chosen, why = placement.choose_gpus(fake, spec, {}, skip_vendors=frozenset({"nvidia"}))
    assert why is None and [a.key for a in chosen] == ["a0"]
    chosen, why = placement.choose_gpus(fake, spec, {}, skip_vendors=frozenset({"nvidia", "amd"}))
    assert chosen is None


def test_fy_gacc_012_process_engines_of_deployments_too():
    """FY-GACC-012: the model scheduler holds back a process engine on GPUs that fail (a custom recipe's), never a
    container engine or a CPU variant, and says why."""
    from forgeyard.models.scheduler import _gpu_access_problem

    n = SimpleNamespace(accelerators=[SimpleNamespace(vendor="nvidia")], runtime_info={"gpu_check": FAIL})
    v = SimpleNamespace(runtime="process", gpu=True, when=SimpleNamespace(vendor="nvidia"))
    assert "cannot open its NVIDIA GPUs" in _gpu_access_problem(n, v)
    assert _gpu_access_problem(n, SimpleNamespace(runtime="container", gpu=True, when=v.when)) is None
    assert _gpu_access_problem(n, SimpleNamespace(runtime="process", gpu=False, when=SimpleNamespace(vendor="cpu"))) is None
    assert _gpu_access_problem(n, SimpleNamespace(runtime="process", gpu=True, when=SimpleNamespace(vendor="amd"))) is None
    n.runtime_info = {"gpu_check": OK}
    assert _gpu_access_problem(n, v) is None
    n.runtime_info = {"gpu_check": FAIL, "install": {"method": "container"}}
    assert "its agent container lost its device access" in _gpu_access_problem(n, v)


def test_fy_gacc_013_the_shell_lists_only_the_hosts_nodes(tmp_path):
    """FY-GACC-013: the device list the Add-a-machine command and an older agent's update command use, run by sh and
    by bash over a stand-in /dev: one --device per NVIDIA node there, the control nodes first, a missing one (here
    nvidia-modeset) skipped, nothing for a machine without NVIDIA nodes."""
    from forgeyard.install.commands import NVIDIA_DEVICES_SHELL

    dev = tmp_path / "dev"
    dev.mkdir()
    script = "printf '%s\\n' " + NVIDIA_DEVICES_SHELL.replace("/dev/", f"{dev}/")
    for shell in ("/bin/sh", "/bin/bash"):
        if not Path(shell).exists():
            continue
        assert subprocess.run([shell, "-c", script], capture_output=True, text=True).stdout == "\n", shell
        for name in ("nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia0", "nvidia1", "nvidia-caps"):
            (dev / name).touch()
        out = subprocess.run([shell, "-c", script], capture_output=True, text=True).stdout.split()
        want = [f"{dev}/{n}" for n in ("nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia0", "nvidia1")]
        assert out == [x for d in want for x in ("--device", d)], (shell, out)
        for f in dev.iterdir():
            f.unlink()


def test_fy_gacc_014_the_console_and_the_server_list_the_same_devices():
    """FY-GACC-014: the Add-a-machine form (web/src/lib/dockerAgent.ts) and the server's commands use the same device
    list, word for word."""
    import re

    from forgeyard.install.commands import NVIDIA_DEVICES_SHELL

    ts = (Path(__file__).resolve().parent.parent / "web" / "src" / "lib" / "dockerAgent.ts").read_text()
    block = re.search(r"export const NVIDIA_DEVICES =\s*((?:\"(?:[^\"\\]|\\.)*\"\s*\+?\s*)+);", ts).group(1)
    parts = re.findall(r"\"((?:[^\"\\]|\\.)*)\"", block)
    assert "".join(p.replace('\\"', '"') for p in parts) == NVIDIA_DEVICES_SHELL


def test_fy_gacc_015_a_deployment_with_a_process_engine_on_gpus_waits(owner):
    """FY-GACC-015: a custom recipe whose NVIDIA variant runs llama-server as a process: while new processes on the
    machine cannot open its GPUs the deployment does not fit there, with the reason; once they can, it fits."""
    from tests.test_scheduler import MODELS, seed_catalog

    seed_catalog()
    nid = make_node("rtx", "nvidia-rtx4090x2", features=MODELS, runtime_info={**PROCESS_OK, "gpu_check": FAIL})
    text = (Path(__file__).resolve().parent.parent / "forgeyard" / "recipes" / "llama-cpp.yaml").read_text()
    cuda = text.index("  - name: cuda\n")
    end = text.index("  - name: rocm\n")
    engine = text[text.index("    engine:\n", text.index("  - name: cpu-process\n")):]
    engine = engine[:engine.index("binary:")] + engine[engine.index("binary:"):].split("\n", 1)[0] + "\n"
    variant = ("  - name: cuda-process\n    when: {vendor: nvidia}\n"
               "    runtime: process\n" + engine)
    custom = (text[:cuda] + variant + text[end:]).replace("\nname: llama-cpp\n", "\nname: llama-gpu-process\n", 1)
    r = owner.post("/api/recipes", json={"yaml": custom})
    assert r.status_code == 201, r.text

    def estimate():
        r = owner.post("/api/deployments/estimate", json={"name": "g", "model": "qwen3-1.7b-gguf",
                                                           "recipe": "llama-gpu-process", "params": {"gpus": 1}})
        assert r.status_code == 200, r.text
        return r.json()["placement"]

    held = estimate()
    assert held["fits"] is False
    assert "cuda-process: new processes there cannot open its NVIDIA GPUs" in held["machines"][0]["reason"]
    edit(Node, nid, runtime_info={**PROCESS_OK, "gpu_check": OK})
    assert estimate()["fits"] is True
