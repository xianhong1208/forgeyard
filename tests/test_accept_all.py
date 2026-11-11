"""scripts/accept_all.py, the full regression acceptance run: its pure parts (FY-ACC-*). The run itself
needs a server and real machines; these cover what decides what runs, what it reports and what it
removes."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "accept_all.py"
ORDER = ["server", "machines", "workloads", "models", "gateway", "cloud", "console"]


@pytest.fixture(scope="module")
def acc():
    spec = importlib.util.spec_from_file_location("accept_all", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["accept_all"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("accept_all", None)


def test_fy_acc_001_groups_in_order_and_their_needs(acc):
    """FY-ACC-001: the seven groups run in this order; gateway and cloud need the models group."""
    assert list(acc.GROUPS) == ORDER
    assert acc.GROUPS["gateway"].needs == ("models",) and acc.GROUPS["cloud"].needs == ("models",)
    assert all(not acc.GROUPS[g].needs for g in ("server", "machines", "workloads", "models", "console"))


def test_fy_acc_002_arguments(acc):
    """FY-ACC-002: by default every group runs; --only and --skip pick groups, and a group's needs come
    along, saying why; wrong combinations are refused before anything runs."""
    a = acc.parse_args(["--server", "https://h:7860/"])
    assert a.plan == [(g, "") for g in ORDER] and a.server == "https://h:7860"
    assert a.ca is None and a.user == "owner" and not a.kill_local
    assert acc.parse_args(["--cacert", "ca.pem"]).ca == "ca.pem" == acc.parse_args(["--ca", "ca.pem"]).ca
    assert acc.parse_args(["--only", "gateway"]).plan == [("models", "needed by gateway"), ("gateway", "")]
    assert acc.parse_args(["--only", " cloud, server "]).plan == [("server", ""), ("models", "needed by cloud"),
                                                                 ("cloud", "")]
    assert acc.parse_args(["--skip", "console,cloud"]).plan == [(g, "") for g in ORDER[:5]]
    both = acc.parse_args(["--gpu-machine", "a", "--cpu-machine", "a", "--second-cpu-machine", "b"])
    assert (both.gpu_machine, both.cpu_machine, both.second_cpu_machine) == ("a", "a", "b")  # one machine, two roles
    for bad in (["--only", "server", "--skip", "console"], ["--only", "nonsense"], ["--skip", ",".join(ORDER)],
                ["--second-cpu-machine", "b"],
                ["--cpu-machine", "a", "--second-cpu-machine", "a"], ["--gpu-machine", "a", "--cpu-machine", "b",
                 "--second-cpu-machine", "a"], ["--cloud-model", "m"], ["--timeout", "0"]):
        with pytest.raises(SystemExit) as e:
            acc.parse_args(bad)
        assert e.value.code == 2, bad


def test_fy_acc_003_plan_keeps_what_a_chosen_group_needs(acc):
    """FY-ACC-003: skipping models while gateway runs still deploys them (the gateway has nothing to call
    otherwise); the plan stays in the groups' order."""
    needs = {"gateway": ("models",), "cloud": ("models",)}
    plan = acc.plan_groups([], ["models", "console"], ORDER, needs)
    assert plan == [("server", ""), ("machines", ""), ("workloads", ""), ("models", "needed by gateway"),
                    ("gateway", ""), ("cloud", "")]
    assert acc.plan_groups(["cloud", "gateway"], [], ORDER, needs) == [
        ("models", "needed by gateway"), ("gateway", ""), ("cloud", "")]
    with pytest.raises(ValueError, match="unknown group 'x'"):
        acc.plan_groups(["x"], [], ORDER, needs)


def test_fy_acc_004_report_tables_totals_and_reasons(acc):
    """FY-ACC-004: a table per group with its counts and time, every skip with its reason, every failure,
    totals with the time taken; JSON with the same; the exit code is 1 on any FAIL."""
    clock = iter([100.0, 165.0]).__next__
    r = acc.Report(clock=clock)
    r.group("server", "HTTPS and guards").seconds = 4.2
    r.add("server", "421 for a foreign Host", acc.PASS, "421 host_not_allowed")
    r.add("server", "sign-in back-off", acc.SKIP, "no back-off   was\nreached")
    r.group("gateway", "/v1", "needed by cloud").seconds = 75
    r.add("gateway", "chat", acc.FAIL, "502 upstream_unreachable")
    with pytest.raises(ValueError):
        r.add("server", "x", "INFO")
    r.finish()
    assert r.totals() == {"PASS": 1, "FAIL": 1, "SKIP": 1} and r.totals("server") == {"PASS": 1, "FAIL": 0, "SKIP": 1}
    assert not r.ok and r.exit_code() == 1
    text = r.render()
    assert "== server: HTTPS and guards  [1 pass, 0 fail, 1 skip; 4 s]" in text
    assert "== gateway: /v1 (needed by cloud)  [0 pass, 1 fail, 0 skip; 1 min 15 s]" in text
    assert "Skipped, and why:\n  server: sign-in back-off: no back-off was reached" in text
    assert "Failed:\n  gateway: chat: 502 upstream_unreachable" in text
    assert text.rstrip().endswith("Total: 1 pass, 1 fail, 1 skip in 1 min 05 s: FAILED")
    j = json.loads(json.dumps(r.to_json()))
    assert j["ok"] is False and j["totals"] == {"PASS": 1, "FAIL": 1, "SKIP": 1} and j["seconds"] == 65.0
    assert [g["name"] for g in j["groups"]] == ["server", "gateway"]
    assert j["groups"][0]["checks"][1] == {"check": "sign-in back-off", "status": "SKIP", "detail": "no back-off was reached"}
    ok = acc.Report()
    ok.group("server", "s")
    ok.add("server", "a", acc.PASS)
    ok.add("server", "b", acc.SKIP, "why")
    assert ok.ok and ok.exit_code() == 0 and ok.render().rstrip().endswith(": OK")
    assert len(ok.add("server", "long", acc.PASS, "x" * 2000).detail) == 600


def test_fy_acc_005_cleanup_runs_everything_once_in_reverse(acc):
    """FY-ACC-005: what the run made is undone last-made first; an undo that fails is recorded and the
    others still run; nothing is undone twice (an item undone early is skipped at the end); a failed
    cleanup makes the run fail."""
    done, said = [], []
    c = acc.Cleanup(say=said.append)
    c.add("catalog entry", done.append, "catalog")
    c.add("deployment", done.append, "deployment")

    def boom():
        raise RuntimeError("409 model_in_use")

    c.add("route", boom)
    early = c.add("budget", lambda: done.append("budget"))
    c.add("key", done.append, "key")
    assert c.pending == 5
    assert c.now(early) is True and c.now(early) is True and done == ["budget"]
    results = c.run()
    assert done == ["budget", "key", "deployment", "catalog"]
    assert [x["label"] for x in results] == ["budget", "key", "route", "deployment", "catalog entry"]
    assert results[2] == {"label": "route", "ok": False, "error": "RuntimeError: 409 model_in_use"}
    assert said and "route" in said[0] and c.pending == 0
    assert c.run() == results and done == ["budget", "key", "deployment", "catalog"]
    r = acc.Report()
    r.cleanup = results
    assert not r.ok and "NOT removed: route (RuntimeError: 409 model_in_use)" in r.render()


def test_fy_acc_006_budgets_that_alert_without_touching_anyone_else(acc):
    """FY-ACC-006: the key budget for the alert check lies between 80% and 100% of what the key spent,
    whatever the rounding of that spend (half up to micro-USD); a provider's test budget always keeps
    its month's spend below 80%, so setting it alerts nobody."""
    for shown in range(0, 400):
        b = acc.choose_budget(shown)
        if shown < 7:
            assert b is None, shown
            continue
        assert b is not None, shown
        for real in (shown - 0.5, shown - 0.2, shown, shown + 0.49):
            assert real * 100 >= 80 * b and real < b, (shown, real, b)
    for spent in (0, 1, 7, 123, 10**7):
        for reserved in (0, 3):
            b = acc.provider_test_budget(spent, reserved)
            assert (spent + reserved + 0.5) * 100 < 80 * b
    assert acc.tokens_for(10, 2_000_000) == 64                       # 10 micro-USD at $2/M: 10 tokens, at least 64
    assert acc.tokens_for(1000, 2_000_000) == 1001                   # twice 1000 micro-USD
    assert acc.tokens_for(5, None) == 64 and acc.tokens_for(10**9, 1) is None


def test_fy_acc_007_ca_fingerprint_as_the_server_prints_it(acc, tmp_path):
    """FY-ACC-007: the fingerprint of the CA given with --ca is computed like the server's own."""
    from forgeyard.security import tls

    tls.make_ca(tmp_path)
    pem = (tmp_path / "ca.crt").read_text()
    assert acc.ca_fingerprint(pem) == tls.fingerprint(pem)
    assert acc.ca_fingerprint("junk\n" + pem).startswith("sha256:")


def test_fy_acc_008_gpu_probe_travels_as_one_argument(acc):
    """FY-ACC-008: scripts/gpu_probe.py goes to the machine inside one `python3 -c` argument (a workload
    argument holds at most 4096 characters), unchanged."""
    from forgeyard.workloads.spec import WorkloadSpec

    src = acc.probe_source()
    assert len(src) <= 4096
    blob = src.split("b64decode('")[1].split("')")[0]
    assert zlib.decompress(base64.b64decode(blob)) == (ROOT / "scripts" / "gpu_probe.py").read_bytes()
    compile(src, "<probe>", "exec")
    WorkloadSpec.model_validate({"name": "accept-x-probe", "kind": "task", "machine_id": 1,
                                 "runtime": {"type": "process", "command": "python3", "args": ["-c", src, "--expect", "1"]}})


def test_fy_acc_009_error_codes_and_headers(acc):
    """FY-ACC-009: an answer's error code is read from Forgeyard's shape and from the gateway's
    (OpenAI's); who served a request comes from X-Forgeyard-Target."""
    assert acc.Resp(403, {}, b'{"detail": {"code": "csrf", "message": "x"}}').code() == "csrf"
    assert acc.Resp(402, {}, b'{"error": {"code": "budget_exhausted"}}').code() == "budget_exhausted"
    assert acc.Resp(500, {}, b"<html>").code() == "" and acc.Resp(0, {}, b"", "ConnectionRefusedError").text
    assert acc.target_of({"x-forgeyard-target": "provider:gemini/flash"}) == "provider:gemini/flash"
    assert acc.target_of(None) == ""


def test_fy_acc_010_a_group_that_breaks_is_a_failure_not_the_end(acc):
    """FY-ACC-010: checks land in the group that runs; a group that raises is recorded as FAIL with
    the reason and the run goes on; Ctrl-C is recorded and stops the run (the cleanup still runs)."""
    args = acc.parse_args(["--server", "https://127.0.0.1:9"])
    run = acc.Run(args, None)
    assert run.name("svc-c") == f"accept-{run.tag}-svc-c" and len(run.tag) == 4

    def fine(r):
        r.check("one", True, "detail")
        r.skip("two", "no GPU machine")

    def fails(r):
        raise acc.Fail("POST /api/workloads -> 422")

    def crashes(r):
        {}["missing"]

    def interrupted(r):
        raise KeyboardInterrupt

    for name, fn in (("a", fine), ("b", fails), ("c", crashes)):
        run.run_group(acc.Group(name, name.upper(), fn), "")
    with pytest.raises(KeyboardInterrupt):
        run.run_group(acc.Group("d", "D", interrupted), "")
    got = [(c.group, c.name, c.status) for c in run.report.checks]
    assert got == [("a", "one", "PASS"), ("a", "two", "SKIP"), ("b", "b: the group stopped", "FAIL"),
                   ("c", "c: the group stopped", "FAIL"), ("d", "d: interrupted", "FAIL")]
    assert "422" in run.report.checks[2].detail and "KeyError" in run.report.checks[3].detail
    assert set(run.report.groups) == {"a", "b", "c", "d"}


def test_fy_acc_011_the_models_it_deploys(acc):
    """FY-ACC-011: MODELS uses built-in recipes and the catalog names of accept_m3.py for the same
    sources (so a machine's model cache and catalog entries are reused); one CPU model, the rest GPU."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import accept_m3
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    from forgeyard.models.recipes import builtin_recipes

    builtin = set(builtin_recipes())
    m3 = {s["source"]: s["model"] for s in accept_m3.MODELS}
    assert len({m["key"] for m in acc.MODELS}) == len(acc.MODELS)
    assert [m["device"] for m in acc.MODELS].count("cpu") == 1 and acc.MODELS[0]["device"] == "cpu"
    for m in acc.MODELS:
        assert m["recipe"] in builtin and m["model"].startswith("accept-") and m3.get(m["source"]) == m["model"], m
        assert (m["params"].get("gpus") == 0) == (m["device"] == "cpu")
    assert acc.CLOUD_PROMPT and len(acc.CLOUD_PROMPT) < 100


def test_fy_acc_012_the_guide_explains_it_in_both_languages():
    """FY-ACC-012: the how-to has "Check a server end to end" in English and Traditional Chinese, with
    the same command."""
    en = (ROOT / "docs" / "guide" / "how-to.md").read_text()
    zh = (ROOT / "docs" / "guide" / "zh-TW" / "how-to.md").read_text()
    assert "## Check a server end to end" in en and "scripts/accept_all.py" in en
    assert "scripts/accept_all.py" in zh
