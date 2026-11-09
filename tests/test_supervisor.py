"""The agent's reconciler with real processes and the fake docker CLI (FY-SUP-*)."""

import asyncio
import json
import os
import signal

from forgeyard.agent.workloads.container import ContainerRuntime
from forgeyard.agent.workloads.process import ProcessRuntime
from forgeyard.agent.workloads.records import RecordStore
from forgeyard.agent.workloads.supervisor import Supervisor
from tests.workload_helpers import FakeDocker, alive, container, converge, desired, gpus_of, replica, service, task

A100 = gpus_of("nvidia-a100x8")
SLEEPER = {"type": "process", "command": "sleep", "args": ["600"]}


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path, clock=None, **kw):
    rt = ProcessRuntime(RecordStore(tmp_path / "state"), tmp_path / "work", euid=lambda: 4242)
    kw.setdefault("tick", 0.05)
    kw.setdefault("stop_grace", 2.0)
    return Supervisor(tmp_path / "state", {"process": rt}, clock=clock or Clock(), **kw), rt


def sleeper(name="svc", **over):
    return service(name, runtime=SLEEPER, ports=[], **over)


def reap_all(*runtimes):
    for rt in runtimes:
        for p in list(rt.children.values()):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait(5)


def test_fy_sup_001_converges_and_is_idempotent(tmp_path):
    """FY-SUP-001: the same desired list again and again starts nothing twice."""
    sup, rt = make(tmp_path)

    async def scenario():
        sup.set_desired(desired(replica("r1", sleeper())))
        await converge(sup, lambda s: s.records["r1"].state == "running")
        pid = sup.records["r1"].pid
        assert sup.take_report()["full"] is True
        for i in range(5):
            sup.set_desired(desired(replica("r1", sleeper()), revision=i + 2))
            await converge(sup)
        assert sup.started == 1 and sup.records["r1"].pid == pid and alive(pid)
        report = sup.take_report()
        assert report["full"] is True and [r["state"] for r in report["replicas"]] == ["running"]
        await converge(sup)
        assert sup.take_report() is None  # nothing changed, nothing to say
        sup.set_desired(desired(revision=10))
        await converge(sup, lambda s: not s.records)
        assert not alive(pid)
        report = sup.take_report()
        assert [(r["id"], r["state"]) for r in report["replicas"]] == [("r1", "stopped")]
        assert not (tmp_path / "work" / "r1").exists() and not (tmp_path / "state" / "replicas" / "r1.json").exists()

    try:
        asyncio.run(scenario())
    finally:
        reap_all(rt)


def test_fy_sup_002_adopts_after_a_restart_instead_of_duplicating(tmp_path):
    """FY-SUP-002: the agent dies (kill -9: nothing stopped, records on disk); a new one adopts the
    same pid, starts nothing, and still stops it when asked."""
    first, rt1 = make(tmp_path)

    async def before():
        first.set_desired(desired(replica("r1", sleeper())))
        await converge(first, lambda s: s.records["r1"].state == "running")
        return first.records["r1"].pid

    pid = asyncio.run(before())
    second, rt2 = make(tmp_path)  # a fresh agent on the same state directory

    async def after():
        await second.adopt()
        assert second.records["r1"].pid == pid and second.records["r1"].state == "running"
        await converge(second)  # no desired yet: watch only
        assert second.started == 0 and alive(pid)
        second.set_desired(desired(replica("r1", sleeper())))
        await converge(second)
        assert second.started == 0 and second.records["r1"].pid == pid
        assert second.take_report()["replicas"][0]["pid"] == pid
        second.set_desired(desired(revision=5))
        await converge(second, lambda s: not s.records)
        rt1.children["r1"].wait(5)  # the test process is still its parent: reap it
        assert not alive(pid)

    try:
        asyncio.run(after())
    finally:
        reap_all(rt1, rt2)


def test_fy_sup_003_an_adopted_process_that_ended_reports_its_exit_code(tmp_path):
    """FY-SUP-003: it exits while no agent watches; the wrapper's file has the code."""
    first, rt1 = make(tmp_path)

    async def before():
        first.set_desired(desired(replica("r1", task(script="sleep 0.3; exit 4"))))
        await converge(first, lambda s: s.records["r1"].state == "running")

    asyncio.run(before())
    rt1.children["r1"].wait(5)
    second, rt2 = make(tmp_path)

    async def after():
        await second.adopt()
        await converge(second, lambda s: s.records["r1"].final)
        rec = second.records["r1"]
        assert (rec.state, rec.exit_code, rec.reason) == ("failed", 4, "exited with code 4")

    asyncio.run(after())


def test_fy_sup_004_a_new_generation_replaces_the_old(tmp_path):
    """FY-SUP-004"""
    sup, rt = make(tmp_path)

    async def scenario():
        sup.set_desired(desired(replica("r1", sleeper())))
        await converge(sup, lambda s: s.records["r1"].state == "running")
        old = sup.records["r1"].pid
        sup.set_desired(desired(replica("r1", sleeper(), generation=2), revision=2))
        await converge(sup, lambda s: "r1" in s.records and s.records["r1"].generation == 2
                       and s.records["r1"].state == "running")
        assert not alive(old) and sup.records["r1"].pid != old and sup.started == 2

    try:
        asyncio.run(scenario())
    finally:
        reap_all(rt)


def test_fy_sup_005_task_retries_with_backoff_then_gives_up(tmp_path):
    """FY-SUP-005: retries=2: three attempts 5 s and 10 s apart, then failed for good."""
    clock = Clock()
    sup, rt = make(tmp_path, clock)

    async def scenario():
        sup.set_desired(desired(replica("r1", task(script="echo try; exit 3", retries=2, retry_backoff=5))))
        await converge(sup, lambda s: s.records["r1"].state == "backoff")
        rec = sup.records["r1"]
        assert rec.attempt == 1 and rec.reason == "exited with code 3; retry 1 of 2 in 5 s"
        assert rec.next_start_at == clock.t + 5
        await converge(sup)
        assert rec.attempt == 1  # not before the backoff
        clock.t += 5
        await converge(sup, lambda s: rec.attempt == 2 and rec.state == "backoff")
        assert rec.reason == "exited with code 3; retry 2 of 2 in 10 s"
        clock.t += 10
        await converge(sup, lambda s: rec.final)
        assert (rec.state, rec.attempt, rec.exit_code) == ("failed", 3, 3)
        assert rec.reason == "exited with code 3 (attempt 3 of 3)"
        clock.t += 1000
        await converge(sup)
        assert rec.attempt == 3 and sup.started == 3
        assert sup._public(rec)["log_tail"] == "try\ntry\ntry"

    asyncio.run(scenario())


def test_fy_sup_006_task_succeeds_on_a_retry(tmp_path):
    """FY-SUP-006"""
    clock = Clock()
    sup, rt = make(tmp_path, clock)
    marker = tmp_path / "work" / "ran-once"

    async def scenario():
        script = f"if [ -e {marker} ]; then exit 0; fi; touch {marker}; exit 1"
        sup.set_desired(desired(replica("r1", task(script=script, retries=3, retry_backoff=1))))
        await converge(sup, lambda s: s.records["r1"].state == "backoff")
        clock.t += 1
        await converge(sup, lambda s: s.records["r1"].final)
        rec = sup.records["r1"]
        assert (rec.state, rec.exit_code, rec.attempt) == ("exited", 0, 2)

    (tmp_path / "work").mkdir()
    asyncio.run(scenario())


def test_fy_sup_007_max_duration_stops_the_task(tmp_path):
    """FY-SUP-007: past max_duration the process group is stopped; the task fails without retry."""
    clock = Clock()
    sup, rt = make(tmp_path, clock)

    async def scenario():
        sup.set_desired(desired(replica("r1", task(script="sleep 600", max_duration=60, retries=5))))
        await converge(sup, lambda s: s.records["r1"].state == "running")
        rec = sup.records["r1"]
        pid = rec.pid
        clock.t += 59
        await converge(sup)
        assert rec.state == "running" and alive(pid)
        clock.t += 1
        await converge(sup, lambda s: rec.final)
        assert rec.state == "failed" and rec.reason == "ran longer than max_duration (60 s) and was stopped"
        assert not alive(pid) and rec.attempt == 1

    try:
        asyncio.run(scenario())
    finally:
        reap_all(rt)


def test_fy_sup_008_service_restart_policies(tmp_path):
    """FY-SUP-008"""
    clock = Clock()
    sup, rt = make(tmp_path, clock)
    crash = {"type": "process", "command": "sh", "args": ["-c", "exit 2"]}
    fine = {"type": "process", "command": "sh", "args": ["-c", "exit 0"]}

    async def scenario():
        sup.set_desired(desired(replica("a", service("a", runtime=crash, ports=[], restart="on-failure")),
                                replica("b", service("b", runtime=crash, ports=[], restart="never")),
                                replica("c", service("c", runtime=fine, ports=[], restart="on-failure")),
                                replica("d", service("d", runtime=fine, ports=[], restart="always"))))
        await converge(sup, lambda s: s.records["a"].state == "backoff" and s.records["b"].final
                       and s.records["c"].final and s.records["d"].state == "backoff")
        a, b, c, d = (sup.records[k] for k in "abcd")
        assert a.reason == "exited with code 2; restarting in 1 s" and not a.final
        assert (b.state, b.exit_code) == ("failed", 2) and (c.state, c.exit_code) == ("exited", 0)
        clock.t += 1
        await converge(sup, lambda s: a.restarts == 1 and a.state == "backoff")
        assert a.reason == "exited with code 2; restarting in 2 s"

    asyncio.run(scenario())


def _free_range() -> tuple[int, int]:
    """Ports below the kernel's ephemeral range (like the agent's default 30000-32767): a probe to
    an ephemeral port nobody listens on can connect to itself."""
    import random

    from forgeyard.agent.workloads.ports import bindable

    while True:
        lo = random.randrange(20000, 29000, 50)
        if all(bindable(p) for p in range(lo, lo + 4)):
            return lo, lo + 49


def test_fy_sup_009_health_checks(tmp_path):
    """FY-SUP-009: a real HTTP server becomes healthy; a port nobody listens on turns unhealthy."""
    sup, rt = make(tmp_path, clock=None, port_range=_free_range())
    sup.clock = __import__("time").time
    web = service("web", health={"type": "http", "path": "/", "interval": 1},
                  runtime={"type": "process", "command": "python3",
                           "args": ["-m", "http.server", "${PORT}", "--bind", "127.0.0.1"]})
    dead = service("dead", runtime=SLEEPER, health={"type": "tcp", "interval": 1, "failure_threshold": 2})

    async def scenario():
        sup.set_desired(desired(replica("w", web), replica("d", dead, ordinal=0)))
        await converge(sup, lambda s: s.records["w"].state == "healthy" and s.records["d"].state == "unhealthy",
                       timeout=20, step=0.3)
        assert "tcp 127.0.0.1" in sup.records["d"].reason
        assert sup.records["w"].ports[0]["host_port"] != sup.records["d"].ports[0]["host_port"]

    try:
        asyncio.run(scenario())
    finally:
        reap_all(rt)


def test_fy_sup_010_stops_finish_before_starts(tmp_path):
    """FY-SUP-010: GPU #4 handed from one replica to another in one desired list: the old
    process is gone before the new one starts, so the card is never shared."""
    sup, rt = make(tmp_path)
    order = []
    real_start, real_stop = rt.start, rt.stop
    rt.start = lambda rec, addr: (order.append(("start", rec.id)), real_start(rec, addr))[1]
    rt.stop = lambda rec, grace: (real_stop(rec, grace), order.append(("stopped", rec.id)))[0]

    async def scenario():
        sup.set_desired(desired(replica("old", sleeper("old"), gpus=[A100[4]])))
        await converge(sup, lambda s: s.records["old"].state == "running")
        sup.set_desired(desired(replica("new", sleeper("new"), gpus=[A100[4]]), revision=2))
        await converge(sup, lambda s: "old" not in s.records and s.records.get("new") and
                       s.records["new"].state == "running")
        assert order == [("start", "old"), ("stopped", "old"), ("start", "new")]

    try:
        asyncio.run(scenario())
    finally:
        reap_all(rt)


def test_fy_sup_011_nothing_new_before_the_first_desired_list(tmp_path):
    """FY-SUP-011: after a restart the agent does not act on stale records until the server speaks."""
    sup, rt = make(tmp_path)
    store = RecordStore(tmp_path / "state")
    from forgeyard.agent.workloads.records import Record

    store.save(Record(id="ghost", generation=1, workload="ghost", ordinal=0, kind="task", runtime="process",
                      spec=replica("ghost", task("ghost"))["spec"], state="pending"))
    sup, rt = make(tmp_path)

    async def scenario():
        await sup.adopt()
        await converge(sup)
        assert sup.started == 0 and sup.records["ghost"].state == "pending"
        sup.set_desired(desired())
        await converge(sup, lambda s: not s.records)
        assert sup.started == 0

    asyncio.run(scenario())


# ------------------------------------------------------------------ containers
def _ctr(tmp_path, fake):
    rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))
    return Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, stop_grace=1, clock=Clock(),
                      bind_address="127.0.0.1"), rt


def test_fy_sup_012_containers_are_adopted_by_label_even_without_a_record(tmp_path):
    """FY-SUP-012: the record file is lost; the labelled container is found and adopted, never run
    twice; a labelled container nobody wants is removed; others' containers are left alone."""
    fake = FakeDocker(tmp_path / "bin")
    first, _ = _ctr(tmp_path, fake)

    async def before():
        first.set_desired(desired(replica("keep", container("keep")), replica("drop", container("drop"))))
        await converge(first, lambda s: all(r.state == "running" for r in s.records.values()) and len(s.records) == 2)

    asyncio.run(before())
    for f in (tmp_path / "state" / "replicas").glob("*.json"):
        f.unlink()
    state = json.loads((tmp_path / "bin" / "state.json").read_text())
    state["containers"]["someone-else"] = {"name": "not-ours", "labels": {"forgeyard.agent": "other"},
                                           "status": "running", "exit": 0, "pid": 1, "logs": []}
    (tmp_path / "bin" / "state.json").write_text(json.dumps(state))
    runs_before = len(fake.runs())
    second, _ = _ctr(tmp_path, fake)

    async def after():
        await second.adopt()
        second.set_desired(desired(replica("keep", container("keep"))))
        await converge(second, lambda s: "keep" in s.records and s.records["keep"].state == "running")
        assert len(fake.runs()) == runs_before
        names = sorted(c["name"] for c in fake.state().values())
        assert names == ["fy-keep-keep", "not-ours"]
        assert second.records["keep"].ports[0]["host_port"] >= 30000

    asyncio.run(after())


def test_fy_sup_013_container_exit_and_stop(tmp_path):
    """FY-SUP-013: an exited container is seen with its code; removing it stops and deletes it."""
    fake = FakeDocker(tmp_path / "bin")
    sup, _ = _ctr(tmp_path, fake)

    async def scenario():
        sup.set_desired(desired(replica("t1", {"name": "t", "kind": "task",
                                               "runtime": {"type": "container", "image": "alpine"}}),
                                replica("s1", container("s"))))
        await converge(sup, lambda s: s.records["t1"].state == "running" and s.records["s1"].state == "running")
        fake.set("fy-t-t1", status="exited", exit=0)
        await converge(sup, lambda s: s.records["t1"].final)
        assert sup.records["t1"].state == "exited" and "fy-t-t1 line 5" in sup.tails["t1"]
        sup.set_desired(desired(revision=3))
        await converge(sup, lambda s: not s.records)
        assert fake.state() == {}
        assert any(c[:2] == ["stop", "-t"] and c[-1] == "fy-s-s1" for c in fake.calls())

    asyncio.run(scenario())


def test_fy_sup_014_replicas_started_together_get_different_ports(tmp_path):
    """FY-SUP-014: starts run concurrently; each must see the ports the others just took."""
    sup, rt = make(tmp_path, port_range=(30000, 30010), port_probe=lambda p: True)

    async def scenario():
        sup.set_desired(desired(*[replica(f"r{i}", service(f"s{i}", runtime=SLEEPER)) for i in range(4)]))
        await converge(sup, lambda s: len(s.records) == 4 and all(r.state == "running" for r in s.records.values()))
        return sorted(r.ports[0]["host_port"] for r in sup.records.values())

    try:
        assert asyncio.run(scenario()) == [30000, 30001, 30002, 30003]
    finally:
        reap_all(rt)
