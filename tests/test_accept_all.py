"""scripts/accept_all.py, the full regression acceptance run: its pure parts (FY-ACC-*). The run itself
needs a server and real machines; these cover what decides what runs, what it reports and what it
removes."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "accept_all.py"
ORDER = ["server", "machines", "models", "gateway", "cloud", "tts", "img", "video", "vision", "transformers", "relay", "images",
         "scale", "console"]


@pytest.fixture(scope="module")
def acc():
    spec = importlib.util.spec_from_file_location("accept_all", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["accept_all"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("accept_all", None)


def test_fy_acc_001_groups_in_order_and_their_needs(acc):
    """FY-ACC-001: the fourteen groups run in this order (1.3 adds tts, img and video, 1.4 vision, relay and images,
    1.5 scale, 1.5.4 transformers; the workloads and passthrough groups went with apps in 1.5.3, #89); gateway and cloud
    need the models group, the others make their own."""
    assert list(acc.GROUPS) == ORDER
    assert acc.GROUPS["gateway"].needs == ("models",) and acc.GROUPS["cloud"].needs == ("models",)
    assert all(not acc.GROUPS[g].needs for g in ("server", "machines", "models", "tts", "img", "video", "vision", "transformers",
                                                  "relay", "images", "scale", "console"))


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
    assert acc.parse_args(["--skip", "console,cloud"]).plan == [(g, "") for g in ORDER if g not in ("console", "cloud")]
    assert acc.parse_args(["--only", "tts"]).plan == [("tts", "")] and a.tts_asr == "auto"
    assert acc.parse_args(["--only", "img"]).plan == [("img", "")] and a.img_vision == "auto"
    v = acc.parse_args(["--only", "video", "--video-steps", "20", "--video-no-expiry"])
    assert v.plan == [("video", "")] and v.video_steps == 20 and v.video_no_expiry
    assert a.video_steps is None and not a.video_no_expiry
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
    assert plan == [("server", ""), ("machines", ""), ("models", "needed by gateway"),
                    ("gateway", ""), ("cloud", ""), ("tts", ""), ("img", ""), ("video", ""),
                    ("vision", ""), ("transformers", ""), ("relay", ""), ("images", ""), ("scale", "")]
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
    sources (so a machine's model cache and catalog entries are reused; a kind newer than M3, such as the
    1.4.4 text classifier, has no M3 entry); one CPU model, the rest GPU."""
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
        assert m["recipe"] in builtin and m["model"].startswith("accept-") and m3.get(m["source"], m["model"]) == m["model"], m
        assert (m["params"].get("gpus") == 0) == (m["device"] == "cpu")
    assert {m["key"] for m in acc.MODELS if m["source"] not in m3} == {"classify"}
    assert acc.CLOUD_PROMPT and len(acc.CLOUD_PROMPT) < 100


def test_fy_acc_012_the_guide_explains_it_in_both_languages():
    """FY-ACC-012: the how-to has "Check a server end to end" in English and Traditional Chinese, with
    the same command."""
    en = (ROOT / "docs" / "guide" / "how-to.md").read_text()
    zh = (ROOT / "docs" / "guide" / "zh-TW" / "how-to.md").read_text()
    assert "## Check a server end to end" in en and "scripts/accept_all.py" in en
    assert "scripts/accept_all.py" in zh


def test_fy_acc_014_the_scripts_check_with_info_and_skip(acc):
    """FY-ACC-014: the per-feature scripts' exercise() functions call the check they are given with info=True
    or skip=True; Run.check takes both and records them
    as SKIP with the reason, never as a pass or a failure, so a group handed run.check does not stop."""
    args = acc.parse_args(["--server", "https://127.0.0.1:9"])
    run = acc.Run(args, None)

    def g(r):
        assert r.check("ok", True, "fine") is True
        r.check("uid", True, "uid 0", info=True)
        r.check("not here", False, "no container engine", skip=True)
        assert r.check("bad", False, "broken") is False

    run.run_group(acc.Group("x", "X", g), "")
    got = [(c.name, c.status, c.detail) for c in run.report.checks]
    assert got == [("ok", "PASS", "fine"), ("uid", "SKIP", "information: uid 0"),
                   ("not here", "SKIP", "no container engine"), ("bad", "FAIL", "broken")]


def test_fy_acc_015_the_http_library_is_the_one_the_sdk_is_built_on(monkeypatch):
    """FY-ACC-015: openai 3.51 moved from httpx to httpx2, and a fresh --venv installs the newest SDK: the
    scripts take the HTTP library from the SDK's own client class (accept_m3.sdk_httpx), so the client they
    build is one the SDK accepts, with either library."""
    import types

    sys.path.insert(0, str(SCRIPT.parent))
    import accept_m3

    for root in ("httpx2", "httpx"):
        lib = types.ModuleType(root)
        base = type("Client", (), {"__module__": f"{root}._client"})
        lib.Client = base
        sdk = types.ModuleType("openai")
        sdk.DefaultHttpxClient = type("DefaultHttpxClient", (base,), {"__module__": "openai._base_client"})
        monkeypatch.setitem(sys.modules, root, lib)
        monkeypatch.setitem(sys.modules, "openai", sdk)
        assert accept_m3.sdk_httpx() is lib, root



def test_fy_acc_016_the_image_file_it_uploads_is_one_an_engine_loads(acc, tmp_path):
    """FY-ACC-016: the images group's image file is built here, without Docker: a `docker save` archive (manifest.json,
    the config, one layer of one small file) that the tests' Docker CLI loads, checking each layer against the
    config's diff id, as the real engines do; its id is the config's digest, it carries the tag and the run's mark,
    the same input gives the same bytes, and another run gives another image."""
    import hashlib
    import io
    import tarfile

    from tests.workload_helpers import FakeDocker

    data, image_id = acc.image_archive("accept-ab12-image:1", "ab12")
    tar = tarfile.open(fileobj=io.BytesIO(data))
    names = sorted(m.name for m in tar.getmembers())
    manifest = json.loads(tar.extractfile("manifest.json").read())
    assert len(manifest) == 1 and manifest[0]["RepoTags"] == ["accept-ab12-image:1"]
    config = tar.extractfile(manifest[0]["Config"]).read()
    assert image_id == "sha256:" + hashlib.sha256(config).hexdigest()
    layer = tar.extractfile(manifest[0]["Layers"][0]).read()
    assert json.loads(config)["rootfs"]["diff_ids"] == ["sha256:" + hashlib.sha256(layer).hexdigest()]
    assert b"ab12" in tarfile.open(fileobj=io.BytesIO(layer)).extractfile("forgeyard-accept.txt").read()
    assert names == sorted([manifest[0]["Config"], manifest[0]["Layers"][0], "manifest.json"])
    assert json.loads(config)["architecture"] == "amd64" and len(data) <= 20 * 1024  # two tar records
    assert acc.image_archive("accept-ab12-image:1", "ab12") == (data, image_id)
    assert acc.image_archive("accept-cd34-image:1", "cd34")[1] != image_id
    arm = tarfile.open(fileobj=io.BytesIO(acc.image_archive("x:1", "m", "arm64")[0]))
    arm_config = json.loads(arm.extractfile(json.loads(arm.extractfile("manifest.json").read())[0]["Config"]).read())
    assert arm_config["architecture"] == "arm64"
    fake = FakeDocker(tmp_path / "bin")
    import subprocess

    out = subprocess.run([str(fake.path), "load"], input=data, capture_output=True, check=True)
    assert out.stdout.decode().strip() == "Loaded image: accept-ab12-image:1"
    listed = subprocess.run([str(fake.path), "image", "ls", "-q", "--no-trunc"], capture_output=True, text=True)
    assert listed.stdout.split() == [image_id]
    assert acc.engine_arch({"accelerators": [{"vendor": "cpu", "arch": "aarch64"}]}) == "arm64"
    assert acc.engine_arch({"accelerators": [{"vendor": "cpu", "arch": "x86_64"}]}) == "amd64"
    assert acc.engine_arch({"accelerators": [], "platform": {"machine": "arm64"}}) == "arm64"


def test_fy_acc_017_the_relay_and_images_groups_machines(acc):
    """FY-ACC-017: the relay and images groups take their machines from the CPU and GPU machines unless named; a
    second machine must be another one; --only picks each group alone (they need no deployments)."""
    a = acc.parse_args(["--only", "relay,images"])
    assert a.plan == [("relay", ""), ("images", "")]
    assert (a.relay_machine, a.relay_second_machine, a.engine_machine, a.second_engine_machine) == (None,) * 4
    b = acc.parse_args(["--relay-machine", "r1", "--relay-second-machine", "r2", "--engine-machine", "e1",
                        "--second-engine-machine", "e2"])
    assert (b.relay_machine, b.relay_second_machine, b.engine_machine, b.second_engine_machine) == ("r1", "r2", "e1", "e2")
    for bad in (["--relay-machine", "r", "--relay-second-machine", "r"], ["--cpu-machine", "c", "--relay-second-machine", "c"],
                ["--gpu-machine", "g", "--second-engine-machine", "g"], ["--engine-machine", "e", "--second-engine-machine", "e"]):
        with pytest.raises(SystemExit) as e:
            acc.parse_args(bad)
        assert e.value.code == 2, bad
    one, two, three = {"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 3, "name": "c", "ok": True}
    assert acc.pick_second([None, one, two], one) == two
    assert acc.pick_second([one], one) is None
    assert acc.pick_second([two, three], one, lambda m: m.get("ok")) == three
    assert acc.missing_features({"features": ["models", "relay"]}, acc.RELAY_FEATURES) == ["prefetch", "cache_admin"]


def test_fy_acc_018_finding_the_model_and_the_image_it_made(acc):
    """FY-ACC-018: the relay group finds its GGUF file in a machine's cache listing by repository, revision and file
    (not another revision, not another file of the repository); the images group finds its image by its tag (also
    as Podman names it, localhost/…); the recipe that lets it be removed is one the server accepts and names each tag."""
    from forgeyard.models.recipes import parse_recipe

    view = {"models": [
        {"kind": "hf", "repo": "ggml-org/tiny-llamas", "revision": "old", "files": 1,
         "parts": [{"key": "k-old", "file": "stories15M-q8_0.gguf", "bytes": 1}]},
        {"kind": "url", "file": "stories15M-q8_0.gguf", "key": "url/x"},
        {"kind": "hf", "repo": "ggml-org/tiny-llamas", "revision": "rev", "files": 2,
         "parts": [{"key": "k-q4", "file": "stories15M-q4_0.gguf", "bytes": 2},
                   {"key": "k-q8", "file": "stories15M-q8_0.gguf", "bytes": 3}]}]}
    entry, part = acc.cache_part(view, "ggml-org/tiny-llamas", "rev", "stories15M-q8_0.gguf")
    assert entry["revision"] == "rev" and part["key"] == "k-q8"
    assert acc.cache_part(view, "ggml-org/tiny-llamas", "new", "stories15M-q8_0.gguf") == (None, None)
    assert acc.cache_part(view, "ggml-org/tiny-llamas", "rev", "stories260K.gguf")[1] is None
    assert acc.cache_part({}, "a/b", None, None) == (None, None)
    assert acc.RELAY_MODEL["source"] == "hf://ggml-org/tiny-llamas" and acc.RELAY_MODEL["file"].endswith(".gguf")
    items = [{"id": "sha256:1", "tags": ["busybox:1.37"]}, {"id": "sha256:2", "tags": ["localhost/accept-ab12-image:1"]}]
    assert acc.tagged(items, "accept-ab12-image:1")["id"] == "sha256:2"
    assert acc.tagged(items, "image:1") is None and acc.tagged(None, "x:1") is None
    r = parse_recipe(acc.cleanup_recipe("accept-ab12-image-cleanup", ["accept-ab12-image:1", "localhost/accept-ab12-image:1",
                                                                     "accept-ab12-image:1"]))
    assert r.name == "accept-ab12-image-cleanup"
    assert [(v.runtime, v.image) for v in r.variants] == [("container", "accept-ab12-image:1"),
                                                          ("container", "localhost/accept-ab12-image:1")]


class FakeAdmin:
    """The admin's Client, played here: answers by (method, path) from a table, records every call."""

    def __init__(self, acc, answers: dict):
        self.acc, self.answers, self.calls = acc, answers, []

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        got = self.answers.get((method, path.split("?")[0]))
        status, data = got(body) if callable(got) else (got or (404, {"detail": {"code": "not_found"}}))
        return self.acc.Resp(status, {}, json.dumps(data).encode())

    def ok(self, method, path, body=None, **kw):
        r = self.request(method, path, body)
        if not 200 <= r.status < 300:
            raise self.acc.Fail(f"{method} {path} -> {r.status}")
        return r.data


def test_fy_acc_019_what_the_new_groups_skip_and_how_the_image_goes_again(acc):
    """FY-ACC-019: without a machine for them, or with an agent too old, the relay and images groups say so as SKIP
    (never FAIL); the uploaded image's cleanup adds a recipe naming its tags, removes it through Remove engine image,
    deletes the recipe again, and does nothing when the image is gone; on an agent that cannot remove images it
    leaves the tiny image and says so in a note."""
    run = acc.Run(acc.parse_args(["--server", "https://127.0.0.1:9"]), None)
    run.run_group(acc.GROUPS["relay"], "")
    run.run_group(acc.GROUPS["images"], "")
    old = {"id": 7, "name": "old-box", "status": "online", "features": ["models", "workloads"],
           "runtime_info": {"container": {"ok": False, "reason": "docker not found"}}}
    run.admin = FakeAdmin(acc, {("GET", "/api/machines"): (200, {"machines": [old]})})
    run.machines = {"cpu": old, "gpu": old}
    run.run_group(acc.GROUPS["relay"], "")
    run.run_group(acc.GROUPS["images"], "")
    got = [(c.group, c.status, c.detail) for c in run.report.checks]
    assert [s for _, s, _ in got] == ["SKIP"] * 4, got
    assert "no --cpu-machine or --relay-machine" in got[0][2] and "no --gpu-machine or --engine-machine" in got[1][2]
    assert "prefetch, relay, cache_admin" in got[2][2] and "docker not found" in got[3][2]

    box = {"id": 3, "name": "box", "status": "online", "features": list(acc.IMAGE_FEATURES)}
    state = {"images": [{"id": "sha256:aa", "tags": ["accept-ab12-image:1"]}], "recipes": set()}

    def remove(body):
        named = any("accept-ab12-image:1" in r for r in state["recipes"])
        if not named:
            return 409, {"detail": {"code": "not_an_engine_image"}}
        state["images"] = [i for i in state["images"] if i["id"] != body["image"]]
        return 200, {"ok": True, "freed_bytes": 10}

    def add_recipe(body):
        state["recipes"].add(body["yaml"])
        return 201, {}

    def del_recipe(body):
        state["recipes"].clear()
        return 200, {}

    fake = FakeAdmin(acc, {("GET", "/api/machines"): lambda b: (200, {"machines": [box]}),
                           ("GET", "/api/machines/3/images"): lambda b: (200, {"images": state["images"]}),
                           ("POST", "/api/machines/3/engine-images/remove"): remove,
                           ("POST", "/api/recipes"): add_recipe,
                           ("DELETE", f"/api/recipes/{run.name('image-cleanup')}"): del_recipe})
    run.admin = fake
    img = acc.UploadedImage(run, "accept-ab12-image:1")
    img.loaded(box, {"id": "sha256:aa", "tags": ["accept-ab12-image:1"]})
    assert img.remove_answer(box).code() == "not_an_engine_image"
    assert run.cleanup.run()[-1]["ok"] is True
    assert state["images"] == [] and state["recipes"] == set()
    order = [(m, p) for m, p, _ in fake.calls if m != "GET"]
    assert order == [("POST", "/api/machines/3/engine-images/remove"), ("POST", "/api/recipes"),
                     ("POST", "/api/machines/3/engine-images/remove"), ("DELETE", f"/api/recipes/{run.name('image-cleanup')}")]
    img.remove(box)  # gone already: nothing asked
    assert [(m, p) for m, p, _ in fake.calls if m != "GET"] == order

    state["images"] = [{"id": "sha256:bb", "tags": ["accept-ab12-image:1"]}]
    box["features"] = ["image_load", "image_save"]
    img2 = acc.UploadedImage(run, "accept-ab12-image:1")
    img2.loaded(box, state["images"][0])
    img2.remove(box)
    assert state["images"] and "stays on box" in run.report.notes[-1] and "docker rmi accept-ab12-image:1" in \
        run.report.notes[-1]


def test_fy_acc_020_a_broken_off_download_leaves_nothing_but_never_takes_others(acc, monkeypatch):
    """FY-ACC-020: after a download the run broke off, its part file goes with Remove partial downloads, once the
    agent's cache report shows it (it comes with the heartbeat) and once the agent stops refusing (cache_busy); a
    finished download needs nothing; a machine that had partial downloads before the run keeps them all (Remove
    partial downloads would take those too), and the report says so."""
    ft = FakeTime()     # accept_all's own clock only: patching time.sleep/monotonic would reach every thread (#239)
    monkeypatch.setattr(acc, "time", ft)
    run = acc.Run(acc.parse_args(["--server", "https://127.0.0.1:9"]), None)
    box = {"id": 4, "name": "box"}
    reports = iter([0, 0, 1, 1, 1])
    answers = iter([(409, {"detail": {"code": "cache_busy"}}), (200, {"ok": True, "freed_bytes": 5})])
    rows = {"state": "cancelled"}
    fake = FakeAdmin(acc, {("GET", "/api/machines/4/cache"): lambda b: (200, {"partial_files": next(reports)}),
                           ("POST", "/api/machines/4/cache/remove-partial"): lambda b: next(answers),
                           ("GET", "/api/downloads"): lambda b: (200, {"downloads": [{"id": 9, **rows}]})})
    run.admin = fake
    acc._partials_after(run, box, 9, 0)
    posts = [p for m, p, _ in fake.calls if m == "POST"]
    assert posts == ["/api/machines/4/cache/remove-partial"] * 2
    fake.calls.clear()
    rows["state"] = "done"
    acc._partials_after(run, box, 9, 0)
    assert [p.split("?")[0] for m, p, _ in fake.calls] == ["/api/downloads"]
    rows["state"] = "cancelled"
    reports = iter([3])
    fake.calls.clear()
    acc._partials_after(run, box, 9, 2)
    assert all(m == "GET" for m, _, _ in fake.calls) and "box keeps the partial downloads" in run.report.notes[-1]
    reports = iter([0] * 20)    # a wait that never ends runs out of reports and fails
    fake.calls.clear()
    start = ft.t
    acc._remove_partials(run, box, "x", wait=25)    # nothing shows within the wait: nothing asked
    assert all(m == "GET" for m, _, _ in fake.calls) and 25 < ft.t - start <= 27


# ------------------------------------------------------------------ waits with a limit for room (1.4.5, #203)
class FakeTime:
    """time for accept_all: monotonic() moves only when sleep() is called."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def __getattr__(self, name):
        import time
        return getattr(time, name)


def rep(state="pending", machine=None, reason=None, final=False, **kw):
    return {"ordinal": 0, "state": state, "machine": machine, "reason": reason, "final": final, **kw}


SHORT = "Cannot be placed: GPU #4 has 1.8 GiB available, needs 3 GiB"


def test_fy_acc_021_a_replica_with_no_room_is_named(acc):
    """FY-ACC-021: unplaced() gives the server's reason while a replica is on no machine because none has room
    (`Cannot be placed: …`, said once for several replicas), and nothing for a replica that is placed (even still
    pending or downloading there), finished, or waiting for another reason."""
    assert acc.unplaced({"replica_list": [rep(reason=SHORT)]}) == SHORT
    assert acc.unplaced({"replica_list": [rep(reason=SHORT), {**rep(reason=SHORT), "ordinal": 1}]}) == SHORT
    on = {"id": 3, "name": "gpu-01"}
    for r in (rep(machine=on, reason=SHORT), rep(state="downloading", machine=on), rep(reason=SHORT, final=True),
              rep(reason="waiting for gpu-01 to come online"), rep(), rep(state="healthy", machine=on)):
        assert acc.unplaced({"replica_list": [r]}) is None, r
    assert acc.unplaced({"replica_list": []}) is None and acc.unplaced({}) is None
    assert acc.unplaced({"replica_list": [rep(machine=on), rep(reason=SHORT)]}) == SHORT


def test_fy_acc_022_waiting_for_room_has_its_own_limit(acc, monkeypatch):
    """FY-ACC-022: a wait whose object stays unplaced for --place-timeout seconds in a row (300 by default) ends
    with Unplaced naming the shortage, long before --timeout; a replica that is placed in between starts the count
    again; one that is placed and slow still has the whole --timeout and then fails as before (Fail, not
    Unplaced); a poll without the third value is never cut short."""
    ft = FakeTime()
    monkeypatch.setattr(acc, "time", ft)
    run = acc.Run(acc.parse_args([]), None)
    assert run.args.place_timeout == acc.PLACE_TIMEOUT == 300 and run.args.timeout == 2400
    t0 = ft.t
    with pytest.raises(acc.Unplaced) as e:
        run.wait("accept-x-depth", lambda: (False, "pending", SHORT), run.args.timeout, every=5)
    assert 300 <= ft.t - t0 < 310 and SHORT in str(e.value) and "within 300 s" in str(e.value)
    assert isinstance(e.value, acc.Fail)
    # Placed now and then: the count starts again each time, so only --timeout ends it.
    n = {"i": 0}

    def flaky():
        n["i"] += 1
        return False, "pending", (SHORT if n["i"] % 50 else None)

    t0 = ft.t
    with pytest.raises(acc.Fail) as e:
        run.wait("w", flaky, 1000, every=5)
    assert not isinstance(e.value, acc.Unplaced) and ft.t - t0 > 1000
    t0 = ft.t
    with pytest.raises(acc.Fail) as e:
        run.wait("w", lambda: (False, "downloading", None), 600, every=5)
    assert not isinstance(e.value, acc.Unplaced) and ft.t - t0 > 600
    with pytest.raises(acc.Fail) as e:
        run.wait("w", lambda: (False, "pending"), 600, every=5)
    assert not isinstance(e.value, acc.Unplaced)
    # Ready while the reason is still given (the last poll): done wins.
    assert run.wait("w", lambda: (True, "running", SHORT), 10) == "running"
    short = acc.Run(acc.parse_args(["--place-timeout", "20"]), None)
    t0 = ft.t
    with pytest.raises(acc.Unplaced):
        short.wait("w", lambda: (False, "pending", SHORT), 2400, every=2)
    assert 20 <= ft.t - t0 < 23
    with pytest.raises(SystemExit):
        acc.parse_args(["--place-timeout", "0"])


def test_fy_acc_023_no_room_is_a_skip_not_a_failure(acc, monkeypatch):
    """FY-ACC-023: what waited for room records a SKIP with the shortage (not_ready), any other failed wait a FAIL;
    an Unplaced that leaves a group ends it as a SKIP of the rest of the group; a deployment's wait passes the reason
    on (dep_poll)."""
    ft = FakeTime()
    monkeypatch.setattr(acc, "time", ft)
    run = acc.Run(acc.parse_args(["--place-timeout", "10"]), None)
    run.current = "vision"
    run.not_ready("accept-x-depth: ready", acc.Unplaced(f"workload #9: not placed within 10 s, {SHORT}"))
    run.not_ready("accept-x-seg: ready", acc.Fail("workload #8: not done within 2400 s"))

    def group(r):
        r.check("one", True)
        raise acc.Unplaced(SHORT)

    run.run_group(acc.Group("tts", "T", group), "")
    got = [(c.group, c.name, c.status) for c in run.report.checks]
    assert got == [("vision", "accept-x-depth: ready", "SKIP"), ("vision", "accept-x-seg: ready", "FAIL"),
                   ("tts", "one", "PASS"), ("tts", "tts: the rest of the group", "SKIP")]
    assert SHORT in run.report.checks[0].detail and SHORT in run.report.checks[3].detail

    class Admin:
        def ok(self, method, path, body=None, **kw):
            return {"deployment": {"name": "d", "workload": {"replica_list": [rep(reason=SHORT)]}}}

    run.admin = Admin()
    assert acc.dep_poll(run, 7) == (False, f"#0 - pending ({SHORT})", SHORT)


class GpuAdmin:
    """The admin's client for free_gpu: deployments and workloads it knows, their replicas stop when asked."""

    def __init__(self, objects: dict):
        self.objects = objects          # path -> {"kind": "deployment"|"workload", "state": …}
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path))
        base = path.removesuffix("/stop")
        o = self.objects.get(base)
        if o is None:
            return acc_resp(404, {})
        if method == "POST" and path.endswith("/stop"):
            o["state"] = "stopped"
            return acc_resp(200, {"ok": True})
        if method == "DELETE":
            self.objects.pop(base)
            return acc_resp(200, {"ok": True})
        w = {"replica_list": [rep(state=o["state"], machine={"id": 1, "name": "gpu-01"})]}
        return acc_resp(200, {"deployment": {"workload": w}} if o["kind"] == "deployment" else {"workload": w})

    def ok(self, method, path, body=None, **kw):
        return self.request(method, path, body).data


def acc_resp(status, data):
    import json as _json
    from types import SimpleNamespace
    return SimpleNamespace(status=status, data=data, body=_json.dumps(data).encode(), code=lambda: "")


def test_fy_acc_024_before_a_gpu_group_the_run_lets_go_of_its_own_gpu_work(acc, monkeypatch):
    """FY-ACC-024: before each GPU group (tts, img, video, vision, transformers, scale) the run stops what earlier groups
    deployed on a GPU, waits until that is stopped, removes it (the models group's deployments are only stopped:
    the console shows one; they are removed at the end like everything else), and notes it; the models group's
    speech-to-text deployment stays while the tts group may use it (--tts-asr), CPU work and the group's own stay;
    run_groups does this before the GPU groups only, and every object is still removed by the cleanup."""
    ft = FakeTime()
    monkeypatch.setattr(acc, "time", ft)
    assert acc.GPU_GROUPS == ("tts", "img", "video", "vision", "transformers", "scale")
    run = acc.Run(acc.parse_args([]), None)
    objs = {f"/api/deployments/{i}": {"kind": "deployment", "state": "healthy"} for i in (1, 2, 3, 4, 5, 6)}
    run.admin = admin = GpuAdmin(objs)
    deleted = []

    def made(group, label, path, gpu, **kw):
        item = run.cleanup.add(label, lambda: (admin.request("DELETE", path), deleted.append(label)))
        item.update(group=group, gpu=gpu, path=path, **kw)
        return item

    made("models", "deployment chat", "/api/deployments/1", True, dep_id=1)
    made("models", "deployment asr", "/api/deployments/2", True, dep_id=2)
    made("models", "deployment cpu", "/api/deployments/3", False, dep_id=3)
    run.deps = {"chat": {"id": 1}, "asr": {"id": 2}, "cpu": {"id": 3}}
    made("tts", "deployment tts-gpu", "/api/deployments/4", True, dep_id=4)
    made("tts", "deployment tts-cpu", "/api/deployments/5", False, dep_id=5)
    made("img", "deployment img-gpu", "/api/deployments/6", True, dep_id=6)
    # Before tts: chat stops (asr stays: tts may use it); nothing of tts's own yet.
    assert run.free_gpu("tts", ["img", "video", "vision", "console"]) == ["deployment chat"]
    assert ("POST", "/api/deployments/1/stop") in admin.calls and ("POST", "/api/deployments/2/stop") not in admin.calls
    assert objs["/api/deployments/1"]["state"] == "stopped" and "/api/deployments/1" in objs and not deleted
    # Before img: asr (tts is over) and tts's GPU deployment go; tts's CPU one and img's own stay.
    admin.calls.clear()
    assert run.free_gpu("img", ["video", "vision"]) == ["deployment asr", "deployment tts-gpu"]
    assert deleted == ["deployment tts-gpu"] and "/api/deployments/2" in objs and "/api/deployments/5" in objs
    assert ("POST", "/api/deployments/1/stop") not in admin.calls          # stopped once
    assert run.free_gpu("img", ["video", "vision"]) == []
    # Before vision: img's GPU deployment.
    assert run.free_gpu("vision", []) == ["deployment img-gpu"]
    assert any("before img, freed the GPU of: deployment asr, deployment tts-gpu" == n for n in run.report.notes)
    # --tts-asr none: the speech-to-text deployment is not kept for tts.
    r2 = acc.Run(acc.parse_args(["--tts-asr", "none"]), None)
    r2.admin = GpuAdmin({"/api/deployments/2": {"kind": "deployment", "state": "healthy"}})
    r2.cleanup.add("deployment asr", lambda: None).update(group="models", gpu=True, path="/api/deployments/2", dep_id=2)
    r2.deps = {"asr": {"id": 2}}
    assert r2.free_gpu("tts", []) == ["deployment asr"]
    # The cleanup still removes everything at the end, once.
    run.cleanup.run()
    assert sorted(deleted) == sorted(["deployment tts-gpu", "deployment img-gpu", "deployment chat",
                                      "deployment asr", "deployment cpu", "deployment tts-cpu"])
    # run_groups: free_gpu before the GPU groups only, with the groups after it.
    order = []
    monkeypatch.setattr(acc.Run, "free_gpu", lambda self, g, rest: order.append(("free", g, tuple(rest))))
    monkeypatch.setattr(acc.Run, "run_group", lambda self, g, why: order.append(("run", g.name)))
    acc.run_groups(run, [("models", ""), ("gateway", ""), ("tts", ""), ("relay", ""), ("vision", ""), ("console", "")])
    assert order == [("run", "models"), ("run", "gateway"), ("free", "tts", ("relay", "vision", "console")),
                     ("run", "tts"), ("run", "relay"), ("free", "vision", ("console",)), ("run", "vision"),
                     ("run", "console")]


def test_fy_acc_025_what_it_deploys_says_where_it_runs(acc):
    """FY-ACC-025: a deployment the run makes is registered for the cleanup with its group, its path and whether it
    takes a GPU (a GPU model), which is what free_gpu goes by."""
    run = acc.Run(acc.parse_args([]), None)
    run.current = "tts"

    class Admin:
        def ok(self, method, path, body=None, **kw):
            if path == "/api/deployments/estimate":
                return {"placement": {"fits": True, "placed": [{"machine": "gpu-01", "gpus": [4], "variant": "cuda"}]},
                        "estimate": {"amount": 3 * 1024**3}}
            raise AssertionError(path)

        def request(self, method, path, body=None, **kw):
            return acc_resp(201, {"deployment": {"id": 21, "name": body["name"]}})

    run.admin = Admin()
    m = {"id": 1, "name": "gpu-01", "accelerators": [{"index": 4, "vendor": "nvidia", "allowed": True}]}
    spec = {"model": "qwen3-tts-0.6b", "recipe": "vllm-omni-tts", "params": {"gpus": 1}, "device": "gpu"}
    assert acc.deploy(run, "tts-gpu", spec, m)["deployment"]["id"] == 21
    acc.deploy(run, "tts-cpu", {**spec, "params": {"gpus": 0}, "device": "cpu"}, {**m, "accelerators": []})
    got = [(i["label"], i["group"], i["gpu"], i["path"]) for i in run.cleanup.items()]
    assert got == [(f"deployment {run.name('tts-gpu')}", "tts", True, "/api/deployments/21"),
                   (f"deployment {run.name('tts-cpu')}", "tts", False, "/api/deployments/21")]
    assert run.cleanup.items()[0]["dep_id"] == 21
