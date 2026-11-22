"""scripts/accept_scale.py, the 1.5.0 acceptance of sleep, waking and automatic replicas (also accept_all.py's `scale`
group): its pure parts (FY-ACS-*). The run itself needs a server and real machines; these cover its options, what
decides which steps run or skip, and how it reads metrics, answers, streams, counts and reservations."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


@pytest.fixture(scope="module")
def mods():
    loaded = {}
    sys.path.insert(0, str(SCRIPTS))
    try:
        for name in ("accept_all", "accept_scale"):
            spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            loaded[name] = mod
        yield loaded["accept_all"], loaded["accept_scale"]
    finally:
        for name in ("accept_all", "accept_scale", "replica_port"):
            sys.modules.pop(name, None)
        sys.path.remove(str(SCRIPTS))


@pytest.fixture
def sc_mod(mods):
    return mods[1]


def test_fy_acs_001_options_defaults_and_refusals(mods):
    """FY-ACS-001: the script's options and their defaults (idle 1 min, 20 at once, wake_wait 10 s, a 2-minute
    scale-down window, 4 clients); out-of-range values and step 6's options given apart are refused before
    anything runs; accept_all.py takes the same options and runs the script as its `scale` group, before console,
    needing no other group and freeing the GPU like the other GPU groups."""
    acc, s = mods
    a = s.parse_args(["--server", "https://h:7860/", "--cpu-machine", "cpu-a"])
    assert a.server == "https://h:7860" and a.plan == [("scale", "")] and a.cpu_machine == "cpu-a"
    assert (a.scale_idle_minutes, a.scale_parallel, a.scale_wake_wait, a.scale_down_minutes, a.scale_clients,
            a.scale_threads) == (1, 20, 10, 2, 4, 4)
    assert a.stoppable_agent is None and a.relay_machine is None and a.tts_asr == "none"
    ok = s.parse_args(["--stoppable-agent", "cpu-a", "--agent-stop", "x", "--agent-start", "y"])
    assert (ok.stoppable_agent, ok.agent_stop, ok.agent_start) == ("cpu-a", "x", "y")
    for bad in (["--scale-idle-minutes", "0"], ["--scale-idle-minutes", "1441"], ["--scale-parallel", "1"],
                ["--scale-wake-wait", "9"], ["--scale-wake-wait", "1801"], ["--scale-down-minutes", "0"],
                ["--scale-down-minutes", "121"], ["--scale-clients", "1"], ["--scale-threads", "0"],
                ["--stoppable-agent", "cpu-a"], ["--stoppable-agent", "cpu-a", "--agent-stop", "x"],
                ["--agent-start", "y"], ["--second-cpu-machine", "b"], ["--cpu-machine", "a", "--second-cpu-machine", "a"],
                ["--cloud-model", "m"], ["--timeout", "0"]):
        with pytest.raises(SystemExit) as e:
            s.parse_args(bad)
        assert e.value.code == 2, bad
    full = acc.parse_args(["--only", "scale", "--scale-idle-minutes", "2", "--scale-wake-wait", "15"])
    assert full.plan == [("scale", "")] and (full.scale_idle_minutes, full.scale_wake_wait) == (2, 15)
    with pytest.raises(SystemExit):
        acc.parse_args(["--scale-parallel", "1"])
    names = list(acc.GROUPS)
    assert names.index("scale") == names.index("console") - 1 and acc.GROUPS["scale"].needs == ()
    assert "scale" in acc.GPU_GROUPS


def test_fy_acs_002_metrics_text_is_read_by_name_and_labels(sc_mod):
    """FY-ACS-002: the Prometheus text is read sample by sample (comments, blank and broken lines left out; escaped
    label values unescaped; timestamps ignored); a metric is the sum of the samples whose labels include those asked
    for, None when there is none, and None when /metrics could not be read at all."""
    text = "\n".join([
        "# HELP forgeyard_deployment_wakes_total Asleep deployments woken",
        "# TYPE forgeyard_deployment_wakes_total counter",
        'forgeyard_deployment_wakes_total{deployment="accept-x-scale-sleepy",by="request"} 2.0',
        'forgeyard_deployment_wakes_total{deployment="accept-x-scale-sleepy",by="person"} 1.0',
        'forgeyard_deployment_wakes_total{deployment="other",by="request"} 7.0 1795366485000',
        'forgeyard_replica_start_seconds_count{recipe="llama-cpp",model="a\\"b\\\\c"} 3.0',
        'forgeyard_gpu_memory_reserved_bytes{machine="gpu-01",gpu="4"} 1.5e+10',
        "forgeyard_build_info 1",
        "this is not a sample",
        'broken{x="1"} not-a-number',
        "",
    ])
    samples = sc_mod.parse_metrics(text)
    assert len(samples) == 6
    m = sc_mod.metric
    assert m(samples, "forgeyard_deployment_wakes_total", deployment="accept-x-scale-sleepy", by="request") == 2.0
    assert m(samples, "forgeyard_deployment_wakes_total", deployment="accept-x-scale-sleepy") == 3.0
    assert m(samples, "forgeyard_deployment_wakes_total", by="request") == 9.0
    assert m(samples, "forgeyard_deployment_wakes_total", deployment="nope") is None
    assert m(samples, "forgeyard_replica_start_seconds_count", model='a"b\\c') == 3.0
    assert m(samples, "forgeyard_gpu_memory_reserved_bytes", machine="gpu-01", gpu=4) == 1.5e10
    assert m(samples, "forgeyard_build_info") == 1.0
    assert m(None, "forgeyard_build_info") is None and sc_mod.parse_metrics("") == []


def test_fy_acs_003_answers_and_streams_are_read(sc_mod):
    """FY-ACS-003: an answer's replica comes from X-Forgeyard-Replica ("<served name>#<ordinal>"); a 200 keeps its
    id, tokens, target and spill reason; an error keeps its code, message and Retry-After; a stream is counted by its
    data chunks, with the finish reason, whether [DONE] came, and the text's end."""
    assert sc_mod.replica_of("accept-x-scale-auto#1") == 1 and sc_mod.replica_of("a#b#12") == 12
    assert sc_mod.replica_of("qwen3-1.7b#0") == 0
    assert sc_mod.replica_of(None) is None and sc_mod.replica_of("no-ordinal") is None
    ok = SimpleNamespace(status=200, headers={"x-forgeyard-replica": "m#0", "x-forgeyard-target": "local:m#0"},
                         data={"id": "chatcmpl-1", "choices": [{"message": {"content": "hi there"}}],
                               "usage": {"completion_tokens": 7}}, code=lambda: "")
    c = sc_mod.Call.of(ok, 1.234)
    assert (c.status, c.replica, c.target, c.id, c.tokens, c.text, c.seconds) == (200, 0, "local:m#0", "chatcmpl-1", 7,
                                                                                   "hi there", 1.23)
    assert c.brief() == "200 in 1.23 s from local:m#0"
    spilled = SimpleNamespace(status=200, headers={"x-forgeyard-target": "provider:p/x", "x-forgeyard-spilled": "waking"},
                              data={"choices": []}, code=lambda: "")
    assert sc_mod.Call.of(spilled, 0.1).spilled == "waking" and "spilled waking" in sc_mod.Call.of(spilled, 0.1).brief()
    err = SimpleNamespace(status=503, headers={"retry-after": "70"}, text="",
                          data={"error": {"code": "model_waking", "message": "still waking"}},
                          code=lambda: "model_waking")
    e = sc_mod.Call.of(err, 10.04)
    assert (e.status, e.code, e.message, e.retry_after, e.replica) == (503, "model_waking", "still waking", "70", None)
    assert e.brief().startswith("503 model_waking in 10.04 s")
    lines = [b": keep-alive", b'data: {"choices":[{"delta":{"content":"1\\n"}}]}', b"",
             'data: {"choices":[{"delta":{"content":"2\\n"},"finish_reason":null}]}',
             b"data: {broken", b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}', b"data: [DONE]"]
    t = sc_mod.sse_tally(lines)
    assert t == {"chunks": 3, "finish": "length", "done": True, "tail": "1\n2\n"}
    assert sc_mod.sse_tally([b'data: {"choices":[{"delta":{"content":"x"}}]}'])["done"] is False


def test_fy_acs_004_none_ran_twice_counts_per_replica(sc_mod):
    """FY-ACS-004: the load's answers are counted per status and, for those answered, per replica, with the ids seen
    twice; the gateway's served counts between two snapshots are per replica, a new replica counting from 0, so the
    run can compare what each replica got with what it answered."""
    C = sc_mod.Call
    calls = [C(200, 1, 0, id="a"), C(200, 1, 1, id="b"), C(200, 1, 0, id="c"), C(503, 1, None, code="x"),
             C(200, 1, 1, id="b")]
    t = sc_mod.tally(calls)
    assert t == {"sent": 5, "status": {200: 4, 503: 1}, "per_replica": {0: 2, 1: 2}, "repeated": ["b"]}
    assert sc_mod.tally([])["sent"] == 0
    before = [{"replica": 0, "served": 10}]
    after = [{"replica": 0, "served": 31}, {"replica": 1, "served": 17}]
    assert sc_mod.served_delta(before, after) == {0: 21, 1: 17}
    assert sc_mod.served_delta([], [{"replica": 0}]) == {0: 0}


def test_fy_acs_005_new_events_by_prefix(sc_mod):
    """FY-ACS-005: the events a step caused are those not there before it whose message starts with the prefix
    (the same event read twice is not new; one at the same time with another message is)."""
    before = [{"at": "2026-11-22T10:00:00", "message": "Woken by a request (key k) (generation 2)."}]
    after = before + [{"at": "2026-11-22T10:05:00", "message": "Woken by a request (key k) (generation 3)."},
                      {"at": "2026-11-22T10:05:00", "message": "Replica 0 on cpu-a: ready in 2.1 s"},
                      {"at": "2026-11-22T10:06:00", "message": "Woken by owner (generation 4)."}]
    got = sc_mod.new_events(before, after, "Woken by a request")
    assert [e["message"] for e in got] == ["Woken by a request (key k) (generation 3)."]
    assert len(sc_mod.new_events(before, after, "Woken")) == 2 and sc_mod.new_events(after, after, "") == []


def _rep(machine, state="healthy", vram=0, gpus=(), final=False):
    return {"machine": {"id": machine} if machine else None, "state": state, "vram": vram, "final": final,
            "gpus": [{"index": g} for g in gpus]}


def test_fy_acs_006_reservation_freed_against_the_other_replicas(sc_mod):
    """FY-ACS-006: what a machine's RAM or one GPU holds is the vram of the replicas placed there that are neither
    ended nor confirmed stopped (CPU replicas for RAM, those on that card for a GPU), for one workload or all but it;
    the reservation counts as freed when /metrics went down by the workload's own amount once the other replicas'
    changes in between are taken out (within 1 MiB), never when it held nothing."""
    G = 1 << 30
    ws = [{"id": 1, "replica_list": [_rep(5, vram=2 * G), _rep(5, "stopped", 9 * G), _rep(6, vram=4 * G)]},
          {"id": 2, "replica_list": [_rep(5, "pending", G), _rep(5, vram=3 * G, final=True),
                                     _rep(5, vram=7 * G, gpus=(4,)), _rep(None, "pending", G)]},
          {"id": 3, "replica_list": [_rep(5, vram=5 * G, gpus=(3, 4))]}]
    held = sc_mod.held
    assert held(ws, 5) == 3 * G and held(ws, 5, only=1) == 2 * G and held(ws, 5, but=1) == G
    assert held(ws, 5, gpu=4) == 12 * G and held(ws, 5, gpu=4, but=3) == 7 * G and held(ws, 5, gpu=3) == 5 * G
    assert held(ws, 6) == 4 * G and held(ws, 7) == 0
    freed = sc_mod.freed
    assert freed((10 * G, 8 * G), (8 * G, 8 * G), 2 * G)
    assert freed((10 * G, 8 * G), (11 * G, 11 * G), 2 * G)          # another replica took 3 GiB meanwhile
    assert freed((10 * G, 8 * G), (8 * G + 1000, 8 * G), 2 * G)     # within 1 MiB
    assert not freed((10 * G, 8 * G), (8 * G + (2 << 20), 8 * G), 2 * G)   # 2 MiB short
    assert not freed((10 * G, 8 * G), (10 * G, 8 * G), 2 * G)       # nothing went
    assert not freed((10 * G, 8 * G), (11 * G, 9 * G), 2 * G)
    assert not freed((10 * G, 10 * G), (10 * G, 10 * G), 0)


def test_fy_acs_007_retry_after_of_model_waking(sc_mod):
    """FY-ACS-007: model_waking's Retry-After must be whole seconds, at least 5, and at most the last start (rounded
    up, plus one) when it is known, else at most the server's default of 30."""
    ok = sc_mod.retry_after_ok
    assert ok("102", 112.4)[0] and ok("113", 112.4)[0] and not ok("115", 112.4)[0]
    assert ok("5", 3.0)[0] and not ok("4", 112.4)[0] and ok("5", None)[0]
    assert ok("30", None)[0] and not ok("31", None)[0]
    assert not ok(None, 100)[0] and not ok("ten", 100)[0] and not ok("1.5", 100)[0]
    assert "last start 112.4 s" in ok("102", 112.4)[1]


def test_fy_acs_008_stream_length_and_the_pause(sc_mod):
    """FY-ACS-008: the drain's stream is sized to last the scale-down window and a minute at the measured speed,
    within the context less 300 tokens and at least 256 (10 tokens/s when no speed was measured); the autoscaler's
    2-minute pause after a start or change is counted on the server's clock."""
    st = sc_mod.stream_tokens
    assert st(15.0, 180, 8192) == 2700 and st(60.0, 180, 8192) == 7892 and st(0.5, 180, 8192) == 256
    assert st(None, 180, 8192) == 1800
    p = sc_mod.pause_left
    assert p("2026-11-22T10:00:00", "2026-11-22T10:01:30") == 30.0
    assert p("2026-11-22T10:00:00Z", "2026-11-22T10:03:00+00:00") == 0.0 and p(None, "2026-11-22T10:00:00") == 0.0


def test_fy_acs_009_which_steps_run_and_why_the_others_skip(sc_mod):
    """FY-ACS-009: the sleepy deployment needs --cpu-machine; spill while waking also a --cloud-provider; automatic
    replicas two CPU machines; the vLLM variant --gpu-machine; cannot-wake only with --stoppable-agent (opt-in: it
    stops nothing it was not told it may); step 5 only on a model whose last start is clearly longer than the short
    wake_wait. Each skip says what to give."""
    a = SimpleNamespace(cloud_provider=None, stoppable_agent=None)
    none = sc_mod.plan_parts(a, {})
    assert all(none.values())
    assert "--cpu-machine" in none["sleep"] and "--cpu-machine" in none["spill"] and "--gpu-machine" in none["vllm"]
    assert "--second-cpu-machine" in none["autoscale"] and "--cpu-machine" in none["cannot_wake"]
    one = sc_mod.plan_parts(a, {"cpu": {"id": 1}})
    assert one["sleep"] is None and one["stop"] is None and "--cloud-provider" in one["spill"]
    assert "--second-cpu-machine" in one["autoscale"] and "--stoppable-agent" in one["cannot_wake"]
    assert "never stops a machine" in one["cannot_wake"]
    b = SimpleNamespace(cloud_provider="gemini", stoppable_agent="cpu-a")
    all_ = sc_mod.plan_parts(b, {"cpu": {"id": 1}, "cpu2": {"id": 2}, "gpu": {"id": 3}})
    assert all(v is None for v in all_.values())
    ws = sc_mod.waking_skip
    assert ws(112.0, 10) is None and ws(15.0, 10) is None
    assert "2.2 s" in ws(2.2, 10) and "--gpu-machine" in ws(14.9, 10) and "not known" in ws(None, 10)


def test_fy_acs_010_without_machines_every_step_is_a_skip_with_its_reason(mods, monkeypatch):
    """FY-ACS-010: with no machine, provider or stoppable agent given, the scale group runs nothing on the server:
    every part is a SKIP that names the option it needs (no FAIL), and the metrics step says why it could not read
    /metrics; nothing is created, so nothing is left to clean up."""
    acc, s = mods
    run = acc.Run(acc.parse_args(["--only", "scale"]), None)
    calls = []
    run.admin = SimpleNamespace(request=lambda *a, **k: calls.append(a), ok=lambda *a, **k: calls.append(a),
                                username="owner")
    run.current = "scale"
    run.report.group("scale", "t")

    def no_metrics(self):
        self.why = "GET /metrics answered 403 metrics_local_only"
        return None

    monkeypatch.setattr(s.Metrics, "read", no_metrics)
    s.exercise(run)
    checks = run.report.checks
    assert checks and all(c.status == acc.SKIP for c in checks), [(c.name, c.status) for c in checks]
    text = " ".join(c.detail for c in checks)
    for need in ("--cpu-machine", "--second-cpu-machine", "--gpu-machine", "metrics_local_only"):
        assert need in text, need
    assert not calls and run.cleanup.pending == 0


def test_fy_acs_011_agent_commands_return_when_the_shell_does(sc_mod, tmp_path):
    """FY-ACS-011: step 6's --agent-stop/--agent-start return as soon as their shell does, even when the command
    leaves a background job that keeps the shell's output open (`cd … && agent &`, which used to hold the call for
    the whole 120 s while the deployment went back to sleep); their output and exit code are still reported."""
    import os
    import time

    t0 = time.monotonic()
    ok, how = sc_mod.Scale.agent_cmd(None, f"cd {tmp_path} && sleep 6 & echo $! > {tmp_path}/pid; echo started")
    took = time.monotonic() - t0
    pid = int((tmp_path / "pid").read_text())
    try:
        assert ok and how == "exit 0: started" and took < 3, (ok, how, took)
        assert sc_mod.Scale.agent_cmd(None, "echo nope >&2; exit 3") == (False, "exit 3: nope")
    finally:  # let the background job end by itself: the leak check would SIGKILL its group, which is pytest's
        deadline = time.monotonic() + 10
        while os.path.exists(f"/proc/{pid}") and time.monotonic() < deadline:
            time.sleep(0.1)
