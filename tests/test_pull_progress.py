"""Image pulls before `run`, with progress (FY-PULL-*): summing a Docker Engine API stream per layer,
a pull that fails when it stops making progress (not after a fixed time), a cancel that says so, the
CLI fallback with the anonymous retry for a broken credential helper, #10's offline matching (a loaded
copy is never pulled), and the protocol: pull progress only to a server that lists "pull_progress"."""

import asyncio
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from forgeyard.agent.workloads import container as ctr
from forgeyard.agent.workloads import pull
from forgeyard.agent.workloads.container import ContainerRuntime, engine_socket
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.agent.workloads.runtime import StartError
from forgeyard.agent.workloads.supervisor import Supervisor
from forgeyard.machines.messages import ReplicaStatusIn
from forgeyard.machines.protocol import FEATURE_PULL_PROGRESS, SERVER_FEATURES
from forgeyard.workloads.spec import WorkloadSpec
from tests.pull_helpers import MIB, FakeEngineAPI, layer_events, pull_script
from tests.waits import wait_until
from tests.workload_helpers import FakeDocker, container, converge, desired, replica

ENGINE_IMAGE = "acme/engine:v1"
PINNED = "ghcr.io/acme/engine:v1@sha256:" + "ab" * 32


@pytest.fixture()
def sockdir():
    """A short directory: a Unix socket path must fit in 108 bytes."""
    d = Path(tempfile.mkdtemp(prefix="fyd-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _rt(tmp_path, fake, socket=None, stall=300.0):
    rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path), pull_stall=stall)
    rt.socket = str(socket) if socket else None
    return rt


def _record(image: str, rid: str = "feedface01234567") -> Record:
    spec = WorkloadSpec.model_validate(container(image=image)).model_dump(mode="json")
    return Record(id=rid, generation=1, workload="box", ordinal=0, kind="service", runtime="container", spec=spec)


def _loaded_when_done(fake: FakeDocker, image: str):
    """The fake API's pull leaves the image in the fake CLI's store, as dockerd does."""
    def done(query):
        ref, at, digest = image.partition("@")
        name = ref.rsplit(":", 1)[0]
        fake.load_image({"Id": "sha256:" + "e1" * 32, "RepoTags": [] if at else [image],
                         "RepoDigests": [f"{name}@{digest}"] if at else []})
    return done


# ------------------------------------------------------------------ summing the stream
def test_fy_pull_001_layers_are_summed_once_each():
    """FY-PULL-001: the bytes are each layer's download counted once (repeated events change nothing),
    an "Already exists" layer is done and adds no bytes, a finished download counts its full size, the
    total is final only once every layer's size is known, and done never passes it."""
    L = pull.Layers()
    L.feed({"status": "Pulling from acme/engine", "id": "v1"})
    L.feed({"status": "Already exists", "id": "cccc"})
    L.feed({"status": "Pulling fs layer", "id": "aaaa"})
    L.feed({"status": "Waiting", "id": "bbbb"})
    s = L.snapshot()
    assert s == {"done": 0, "total": 0, "layers": 3, "layers_done": 1, "unpacking": False}
    L.feed({"status": "Downloading", "progressDetail": {"current": 100, "total": 400}, "id": "aaaa"})
    assert L.snapshot()["total"] == 0 and L.snapshot()["done"] == 100  # bbbb's size is not known yet
    L.feed({"status": "Downloading", "progressDetail": {"current": 50, "total": 200}, "id": "bbbb"})
    L.feed({"status": "Downloading", "progressDetail": {"current": 50, "total": 200}, "id": "bbbb"})  # a repeat
    assert L.snapshot() == {"done": 150, "total": 600, "layers": 3, "layers_done": 1, "unpacking": False}
    assert not L.feed({"status": "Downloading", "progressDetail": {"current": 50, "total": 200}, "id": "bbbb"})
    L.feed({"status": "Download complete", "id": "aaaa"})
    assert L.snapshot()["done"] == 450
    L.feed({"status": "Downloading", "progressDetail": {"current": 999, "total": 200}, "id": "bbbb"})  # nonsense
    assert L.snapshot()["done"] <= L.snapshot()["total"] == 600
    L.feed({"status": "Pull complete", "id": "aaaa"})
    L.feed({"status": "Verifying Checksum", "id": "bbbb"})
    s = L.snapshot()
    assert s["done"] == 600 and s["unpacking"] and s["layers_done"] == 2
    L.feed({"status": "Pull complete", "id": "bbbb"})
    assert L.snapshot() == {"done": 600, "total": 600, "layers": 3, "layers_done": 3, "unpacking": False}
    assert "at layer bbbb" in L.where() and "3 of 3 layers done" in L.where()


def test_fy_pull_002_what_is_pulled_and_how_it_reads():
    """FY-PULL-002: a pinned image pulls its digest, a tag its tag, a bare name its latest tag (never
    every tag); a registry with a port is not taken for a tag. The reason reads like a model download."""
    assert pull.pull_ref(PINNED) == ("ghcr.io/acme/engine", "sha256:" + "ab" * 32)
    assert pull.pull_ref("repo@sha256:" + "cd" * 32) == ("repo", "sha256:" + "cd" * 32)
    assert pull.pull_ref("vllm/vllm-openai:v0.32.0") == ("vllm/vllm-openai", "v0.32.0")
    assert pull.pull_ref("localhost:5000/engine") == ("localhost:5000/engine", "latest")
    assert pull.pull_ref("python") == ("python", "latest")
    gib = 1024**3
    assert pull.describe({"done": int(0.8 * gib), "total": int(1.9 * gib), "layers": 7, "layers_done": 2}) == \
        "pulling the image: 0.80 of 1.90 GiB (42%)"
    assert pull.describe({"done": 5 * gib, "total": 4 * gib, "layers": 1, "layers_done": 0}) == \
        "pulling the image: 4.00 of 4.00 GiB (100%)"
    assert pull.describe({"done": gib // 2, "total": 0, "layers": 7, "layers_done": 3}) == \
        "pulling the image: 0.50 GiB, 3 of 7 layers"
    assert pull.describe({"done": 0, "total": 0, "layers": 7, "layers_done": 3}) == "pulling the image: 3 of 7 layers"
    assert pull.describe({"done": 9, "total": 9, "layers": 7, "layers_done": 5, "unpacking": True}) == \
        "pulling the image: unpacking, 5 of 7 layers"
    assert pull.describe({}) == "pulling the image"


# ------------------------------------------------------------------ the engine API
def test_fy_pull_003_an_api_pull_reports_progress_to_the_end(sockdir):
    """FY-PULL-003: the agent asks for the pinned digest (fromImage + tag, as the CLI does) and reports
    the bytes as they arrive, never going back and never above the total, ending at 400 of 400 MiB."""
    api = FakeEngineAPI(sockdir / "d.sock", pull_script(), delay=0.01)
    seen = []
    try:
        pull.api_pull(str(api.path), PINNED, report=seen.append, stall=5)
    finally:
        api.close()
    assert api.requests == [{"path": "/images/create",
                             "query": {"fromImage": "ghcr.io/acme/engine", "tag": "sha256:" + "ab" * 32}}]
    sized = [s for s in seen if s["total"]]
    assert sized and all(s["done"] <= s["total"] for s in sized)
    assert [s["done"] for s in sized] == sorted(s["done"] for s in sized)
    assert seen[-1] == {"done": 400 * MIB, "total": 400 * MIB, "layers": 3, "layers_done": 3, "unpacking": False}


def test_fy_pull_004_a_stalled_pull_fails_and_says_where(sockdir):
    """FY-PULL-004: no new bytes for the limit: the pull fails promptly, naming the layer it was at and
    how far it got, and the setting that sets the limit."""
    script = layer_events("aaaaaaaaaaaa", 100 * MIB, steps=4)[:3] + [("sleep", 30)]
    api = FakeEngineAPI(sockdir / "d.sock", script)
    t0 = time.monotonic()
    try:
        with pytest.raises(pull.PullError) as e:
            pull.api_pull(str(api.path), ENGINE_IMAGE, stall=0.6)
    finally:
        api.close()
    assert time.monotonic() - t0 < 5
    text = str(e.value)
    assert text.startswith("the image pull made no progress for 0.6 s (at layer aaaaaaaaaaaa, 0 of 1 layers done, "
                           "0.05 of 0.10 GiB)")
    assert "FORGEYARD_PULL_STALL_MINUTES" in text
    # An engine that answers and then says nothing at all stalls too.
    api = FakeEngineAPI(sockdir / "e.sock", [("sleep", 30)])
    try:
        with pytest.raises(pull.PullError, match="made no progress for 0.6 s"):
            pull.api_pull(str(api.path), ENGINE_IMAGE, stall=0.6)
    finally:
        api.close()


def test_fy_pull_005_a_slow_pull_that_keeps_moving_is_never_stopped(sockdir):
    """FY-PULL-005: the real case: a large image at under 1 MB/s timed out at a fixed 30 minutes. Now
    only a stall fails it: a pull that takes many times the limit, but keeps receiving bytes, succeeds."""
    events = layer_events("aaaaaaaaaaaa", 1900 * MIB, steps=40)
    api = FakeEngineAPI(sockdir / "d.sock", events, delay=0.05)  # about 2.5 s in all, the limit is 0.5 s
    t0 = time.monotonic()
    try:
        pull.api_pull(str(api.path), ENGINE_IMAGE, stall=0.5)
    finally:
        api.close()
    assert time.monotonic() - t0 > 4 * 0.5


def test_fy_pull_006_errors_say_which_layer_and_credentials_go_to_the_cli(sockdir):
    """FY-PULL-006: an error in the stream names the layer and how far the pull got; a registry that
    wants credentials (an answer 401/403/404 or "unauthorized" in the stream) is handed to the CLI,
    which has the user's; another refusal is an error; no socket at all means the CLI too."""
    cases = [
        (layer_events("aaaaaaaaaaaa", 10 * MIB)[:2] + [{"error": "read tcp: connection reset by peer",
                                                        "errorDetail": {"message": "read tcp: connection reset"}}],
         pull.PullError, "the pull failed at layer aaaaaaaaaaaa, 0 of 1 layers done, 0.00 of 0.01 GiB: read tcp: connection reset by peer"),
        ([{"error": "unauthorized: authentication required"}], pull.NeedsCredentials, "unauthorized"),
        ([("status", 404, "pull access denied for acme/private, repository does not exist or may require "
                          "'docker login'")], pull.NeedsCredentials, "pull access denied"),
        ([("status", 500, 'Get "https://ghcr.io/v2/": dial tcp: lookup ghcr.io: no such host')], pull.PullError,
         "the pull failed (HTTP 500): Get \"https://ghcr.io/v2/\": dial tcp: lookup ghcr.io: no such host"),
    ]
    for n, (script, exc, text) in enumerate(cases):
        api = FakeEngineAPI(sockdir / f"d{n}.sock", script)
        try:
            with pytest.raises(exc) as e:
                pull.api_pull(str(api.path), ENGINE_IMAGE, stall=5)
        finally:
            api.close()
        assert text in str(e.value), (n, str(e.value))
    with pytest.raises(pull.Unreachable):
        pull.api_pull(str(sockdir / "none.sock"), ENGINE_IMAGE)


def test_fy_pull_007_a_cancel_ends_the_pull_at_once(sockdir):
    """FY-PULL-007: the cancel ends a pull waiting on the engine (not at its next event): the
    connection is closed, which is how dockerd learns to stop the pull, and the caller gets
    PullCancelled, never an error that sounds like a missing image."""
    script = layer_events("aaaaaaaaaaaa", 100 * MIB)[:3] + [("sleep", 30), {"status": "never sent", "id": "aaaa"}]
    api = FakeEngineAPI(sockdir / "d.sock", script)
    cancel = threading.Event()
    threading.Timer(0.4, cancel.set).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(pull.PullCancelled):
            pull.api_pull(str(api.path), ENGINE_IMAGE, cancel=cancel, stall=60)
        assert time.monotonic() - t0 < 2
    finally:
        api.close()


# ------------------------------------------------------------------ the runtime
def _supervise(tmp_path, rt, spec, *, features=(FEATURE_PULL_PROGRESS,), during=None):
    """Run one replica until it runs or ends (or `during` says it is done); every reason and
    `download` the agent reported for it, in order."""
    async def scenario():
        sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, port_range=(32100, 32110))
        sup.new_connection(features)
        reasons, downloads = [], []
        notify = sup._notify

        def seen(rec=None):
            if rec is not None and rec.id == "feedface01234567":
                if not reasons or reasons[-1] != rec.reason:
                    reasons.append(rec.reason)
                if rec.download and (not downloads or downloads[-1] != rec.download):
                    downloads.append(dict(rec.download))
            notify(rec)

        sup._notify = seen
        sup.set_desired(desired(replica("feedface01234567", spec)))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await sup.reconcile_once()
            await asyncio.sleep(0.02)
            rec = sup.records.get("feedface01234567")
            if rec is None:
                continue
            if during is not None:
                if await during(sup, rec):
                    break
            elif rec.state in ("running", "backoff", "failed"):
                break
        await sup.settle()
        return sup, reasons, downloads

    return asyncio.run(scenario())


def test_fy_pull_008_the_runtime_pulls_through_the_api_with_progress_then_runs(tmp_path, sockdir):
    """FY-PULL-008: an image that is not local is pulled through the engine API before `run`, the
    replica's reason and `download` show the bytes ("pulling the image: … of 0.39 GiB (…%)", phase
    image, with layers), then "starting the container"; `run` is called once, there is no CLI pull."""
    fake = FakeDocker(tmp_path / "bin")
    api = FakeEngineAPI(sockdir / "d.sock", pull_script(), delay=0.1, on_done=_loaded_when_done(fake, ENGINE_IMAGE))
    try:
        rt = _rt(tmp_path, fake, api.path)
        sup, reasons, downloads = _supervise(tmp_path, rt, container(image=ENGINE_IMAGE))
    finally:
        api.close()
    rec = sup.records["feedface01234567"]
    assert rec.state == "running" and rec.download is None
    pulling = [r for r in reasons if r and r.startswith("pulling the image: ") and "GiB (" in r]
    assert len(pulling) >= 3 and pulling[-1] == "pulling the image: 0.39 of 0.39 GiB (100%)"
    assert reasons[-2:] == ["starting the container", None]
    assert any(d["phase"] == "image" and d["total"] == 400 * MIB and d["layers"] == 3 for d in downloads)
    assert all(d["done"] <= d["total"] for d in downloads if d["total"])
    assert len(fake.runs()) == 1 and fake.pulls() == []
    assert api.requests[0]["query"] == {"fromImage": "acme/engine", "tag": "v1"}


def test_fy_pull_009_an_older_server_gets_the_plain_reason(tmp_path, sockdir):
    """FY-PULL-009: a server without "pull_progress" records every new reason as an event, and knows
    no image phase: the agent says only "pulling the image", with no `download`, then starts."""
    fake = FakeDocker(tmp_path / "bin")
    api = FakeEngineAPI(sockdir / "d.sock", pull_script(), delay=0.02, on_done=_loaded_when_done(fake, ENGINE_IMAGE))
    try:
        rt = _rt(tmp_path, fake, api.path)
        sup, reasons, downloads = _supervise(tmp_path, rt, container(image=ENGINE_IMAGE), features=("workloads",))
    finally:
        api.close()
    assert sup.records["feedface01234567"].state == "running"
    assert [r for r in reasons if r and r.startswith("pulling")] == ["pulling the image"]
    assert downloads == []


def test_fy_pull_010_a_local_or_loaded_image_is_never_pulled(tmp_path, sockdir):
    """FY-PULL-010: #10's offline matching is kept: a pinned image whose digest is local, a loaded copy
    that matches the recorded ids, and a tag-only image that is local are all run without a pull (the
    API is not even asked)."""
    from tests.test_offline_images import CONFIG_AMD64, VULKAN, check, inspected

    fake = FakeDocker(tmp_path / "bin")
    fake.offline()
    fake.load_image(inspected(CONFIG_AMD64))  # loaded with docker load: the tag, no repo digest
    fake.load_image({"Id": "sha256:" + "77" * 32, "RepoTags": ["local/tool:1"], "RepoDigests": []})
    fake.load_image({"Id": "sha256:" + "78" * 32, "RepoTags": [], "RepoDigests": ["acme/pinned@sha256:" + "12" * 32]})
    api = FakeEngineAPI(sockdir / "d.sock", pull_script())
    try:
        rt = _rt(tmp_path, fake, api.path)
        rec = _record(VULKAN)
        rec.image_ids = check()
        rt.pull(rec)
        assert rt._chosen[rec.id] == (CONFIG_AMD64, ctr.LOADED)
        rt.pull(_record("local/tool:1", "feedface00000002"))
        rt.pull(_record("acme/pinned:1@sha256:" + "12" * 32, "feedface00000003"))
    finally:
        api.close()
    assert api.requests == [] and fake.pulls() == []


def test_fy_pull_011_credentials_go_to_the_cli_and_a_broken_helper_is_skipped(tmp_path, sockdir, monkeypatch):
    """FY-PULL-011: the registry wants credentials the API pull did not carry: the CLI pulls (with the
    user's config); its credential helper fails under the agent, so it pulls again anonymously (the
    behaviour kept from FY-ART-022); the replica runs."""
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fake = FakeDocker(tmp_path / "bin")
    api = FakeEngineAPI(sockdir / "d.sock", [{"error": "unauthorized: authentication required"}])
    try:
        rt = _rt(tmp_path, fake, api.path)
        sup, reasons, _ = _supervise(tmp_path, rt, container(image="credfail/engine:1"))
    finally:
        api.close()
    assert sup.records["feedface01234567"].state == "running"
    assert len(api.requests) == 1 and fake.pulls() == [["pull", "credfail/engine:1"]] * 2 and len(fake.runs()) == 1


def test_fy_pull_012_a_cancelled_pull_says_so(tmp_path, sockdir, caplog):
    """FY-PULL-012: the deployment is removed while its image pulls: the pull ends at once, the
    replica's last reason is "cancelled: the workload was removed" (deployment, for a model replica),
    never "the image is not on this machine and could not be pulled", and nothing is run. A changed
    deployment (a new generation) says "changed" for the old one."""
    for gen, want in ((None, "cancelled: the workload was removed"), (2, "cancelled: the workload was changed")):
        fake = FakeDocker(tmp_path / f"bin{gen}")
        api = FakeEngineAPI(sockdir / f"d{gen}.sock", layer_events("aaaaaaaaaaaa", 100 * MIB)[:3] + [("sleep", 30)])
        out: dict = {}

        async def during(sup, rec, gen=gen, out=out):
            if not (rec.reason or "").startswith("pulling the image:"):
                return False
            sup.take_report()
            t0 = time.monotonic()
            again = [] if gen is None else [replica("feedface01234567", container(image="local/other:1"), generation=gen)]
            sup.set_desired(desired(*again, revision=9))
            await converge(sup, lambda s: "feedface01234567" not in s.records
                           or s.records["feedface01234567"].generation == gen, timeout=10)
            out["took"] = time.monotonic() - t0
            out["msg"] = sup.take_report()
            sup.set_desired(desired(revision=10))
            await converge(sup, lambda s: not s.records, timeout=10)
            return True

        try:
            rt = _rt(tmp_path / f"r{gen}", fake, api.path)
            fake.load_image({"Id": "sha256:" + "79" * 32, "RepoTags": ["local/other:1"], "RepoDigests": []})
            _supervise(tmp_path / f"s{gen}", rt, container(image=ENGINE_IMAGE), during=during)
        finally:
            api.close()
        assert out["took"] < 3
        assert not [r for r in caplog.records if r.levelname == "ERROR"]  # a cancel is not a crash
        [gone] = [e for e in out["msg"]["replicas"] if e["id"] == "feedface01234567" and e["generation"] == 1]
        assert gone["state"] == "stopped" and gone["reason"] == want and "download" not in gone
        assert all(ENGINE_IMAGE not in r for r in fake.runs())
        assert not any("could not be pulled" in (e.get("reason") or "") for e in out["msg"]["replicas"])


def test_fy_pull_013_a_stalled_cli_pull_is_ended(tmp_path):
    """FY-PULL-013: without the API the CLI pulls; a pull that stops printing for the limit is ended
    (its process too) and fails with the stall message, not after a fixed time; the layers it saw are
    named. A stall is not an offline machine: no "docker load" advice (seen in the real run)."""
    fake = FakeDocker(tmp_path / "bin")
    fake.pull_script([["a1b2c3d4e5f6: Pulling fs layer", 0], ["a1b2c3d4e5f6: Waiting", 0], ["never", 30]])
    rt = _rt(tmp_path, fake, None, stall=0.6)
    t0 = time.monotonic()
    with pytest.raises(StartError) as e:
        rt.pull(_record("acme/slow:1"))
    assert time.monotonic() - t0 < 6
    assert str(e.value).startswith("the image pull made no progress for 0.6 s (at layer a1b2c3d4e5f6, 0 of 1 layers "
                                   "done)")
    # It was reaching the registry: never the offline advice, also for a pinned image.
    with pytest.raises(StartError) as e:
        rt.pull(_record("acme/slow:1@sha256:" + "ab" * 32))
    assert str(e.value).startswith("the image pull made no progress") and "docker load" not in str(e.value)
    # A CLI that prints nothing at all is ended too, its process with it.
    silent = tmp_path / "silent-docker"
    silent.write_text("#!/bin/sh\n[ \"$1\" = image ] && exit 1\necho $$ > " + str(tmp_path / "silent.pid")
                      + "\nexec sleep 30\n")
    silent.chmod(0o755)
    rt.engine = str(silent)
    with pytest.raises(StartError, match="made no progress for 0.6 s"):
        rt.pull(_record("acme/quiet:1"))
    pid = int((tmp_path / "silent.pid").read_text())

    def ended():
        try:
            return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
        except FileNotFoundError:
            return True
    wait_until(ended, "the silent CLI to be ended", timeout=5)


def test_fy_pull_014_cli_lines_count_layers():
    """FY-PULL-014: without a terminal the CLIs print steps, not bytes: Docker's '<id>: <step>' and
    Podman's 'Copying blob' lines give "n of m layers"; other lines only show it is alive."""
    L = pull.Layers()
    for line in ["v1: Pulling from acme/engine", "aaaaaaaaaaaa: Pulling fs layer", "bbbbbbbbbbbb: Already exists",
                 "aaaaaaaaaaaa: Verifying Checksum", "aaaaaaaaaaaa: Download complete", "aaaaaaaaaaaa: Pull complete",
                 "Digest: sha256:" + "d" * 64]:
        assert pull.cli_layers(line, L)
    assert L.snapshot() == {"done": 0, "total": 0, "layers": 2, "layers_done": 2, "unpacking": False}
    P = pull.Layers()
    for line in ["Trying to pull docker.io/acme/engine:v1...", "Getting image source signatures",
                 "Copying blob sha256:" + "a" * 64, "Copying blob " + "b" * 64 + " skipped: already exists",
                 "Copying blob " + "a" * 64 + " done   |", "Copying config sha256:" + "c" * 64 + " done"]:
        assert pull.cli_layers(line, P)
    assert P.snapshot()["layers"] == 2 and P.snapshot()["layers_done"] == 2
    assert not pull.cli_layers("   ", P)


def test_fy_pull_015_which_socket_the_agent_pulls_through(tmp_path, monkeypatch):
    """FY-PULL-015: Docker: a unix:// DOCKER_HOST, none for tcp:// or ssh:// or another context (the
    CLI knows how to reach those), else /var/run/docker.sock when it exists. Podman: CONTAINER_HOST,
    else the socket `podman info` reports, when it exists."""
    sock = tmp_path / "podman.sock"
    sock.write_text("")
    monkeypatch.setattr(ctr, "DOCKER_SOCKET", str(sock))
    home = {"HOME": str(tmp_path / "nohome")}
    assert engine_socket("docker", {}, {"DOCKER_HOST": "unix:///run/user/1000/docker.sock"}) == "/run/user/1000/docker.sock"
    assert engine_socket("docker", {}, {"DOCKER_HOST": "tcp://10.0.0.5:2376"}) is None
    assert engine_socket("docker", {}, {"DOCKER_HOST": "ssh://me@box"}) is None
    assert engine_socket("docker", {}, {**home, "DOCKER_CONTEXT": "remote"}) is None
    assert engine_socket("docker", {}, home) == str(sock)
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "config.json").write_text('{"currentContext": "remote"}')
    assert engine_socket("docker", {}, {"DOCKER_CONFIG": str(conf)}) is None
    (conf / "config.json").write_text('{"currentContext": "default"}')
    assert engine_socket("docker", {}, {"DOCKER_CONFIG": str(conf)}) == str(sock)
    monkeypatch.setattr(ctr, "DOCKER_SOCKET", str(tmp_path / "missing.sock"))
    assert engine_socket("docker", {}, home) is None
    info = {"host": {"remoteSocket": {"path": f"unix://{sock}", "exists": True}}}
    assert engine_socket("podman", info, {}) == str(sock)
    monkeypatch.setattr(ctr, "PODMAN_SOCKETS", (str(tmp_path / "missing.sock"),))
    assert engine_socket("podman", {"host": {"remoteSocket": {"path": "/nope.sock"}}}, {}) is None
    assert engine_socket("podman", {}, {"CONTAINER_HOST": "unix:///run/podman/podman.sock"}) == "/run/podman/podman.sock"
    assert engine_socket("podman", {}, {"CONTAINER_HOST": "ssh://core@vm/run/podman.sock"}) is None


def test_fy_pull_016_the_stall_limit_is_a_setting(tmp_path):
    """FY-PULL-016: FORGEYARD_PULL_STALL_MINUTES (or the file's [workloads] pull_stall_minutes, or
    --pull-stall-minutes) sets the limit, 5 minutes by default, 1 to 1440."""
    from forgeyard.agent import config
    from forgeyard.cli import _make_supervisor, build_parser

    args = build_parser().parse_args(["agent", "--container-engine", "none"])
    config.resolve(args, env={"FORGEYARD_PULL_STALL_MINUTES": "12"}, read=False)
    assert args.pull_stall_minutes == 12
    sup = _make_supervisor(args, tmp_path / "state")
    assert sup.runtimes["container"].pull_stall == 720
    args = build_parser().parse_args(["agent", "--container-engine", "none"])
    config.resolve(args, env={}, read=False)
    assert _make_supervisor(args, tmp_path / "s2").runtimes["container"].pull_stall == 300
    with pytest.raises(config.ConfigError, match="FORGEYARD_PULL_STALL_MINUTES wants a number of minutes from 1 to 1440"):
        config.resolve(build_parser().parse_args(["agent"]), env={"FORGEYARD_PULL_STALL_MINUTES": "0"}, read=False)


# ------------------------------------------------------------------ the protocol
def test_fy_pull_017_the_server_lists_the_feature_and_keeps_the_progress_small():
    """FY-PULL-017: the server lists "pull_progress"; an image `download` keeps its layers and the
    unpacking flag, a done above the total is clamped (for models too), and what an older agent sends
    (no phase image, no layers) is read as before."""
    assert FEATURE_PULL_PROGRESS in SERVER_FEATURES
    st = ReplicaStatusIn.model_validate({"id": "r1", "generation": 1, "state": "starting", "download": {
        "phase": "image", "done": 5 * MIB, "total": 4 * MIB, "layers": 7, "layers_done": 9, "unpacking": True,
        "extra": "x" * 9999}})
    assert st.download == {"phase": "image", "done": 4 * MIB, "total": 4 * MIB, "layers": 7, "layers_done": 7,
                           "unpacking": True}
    old = ReplicaStatusIn.model_validate({"id": "r1", "generation": 1, "state": "starting",
                                          "download": {"phase": "model", "done": 15, "total": 12, "file": "a"}})
    assert old.download == {"phase": "model", "done": 12, "total": 12, "file": "a"}
    unknown = ReplicaStatusIn.model_validate({"id": "r1", "generation": 1, "state": "starting",
                                              "download": {"phase": "image", "done": 3, "total": 0, "layers": -4}})
    assert unknown.download == {"phase": "image", "done": 3, "total": 0, "layers": 0, "layers_done": 0}


def test_fy_pull_018_the_server_shows_pull_progress_without_an_event_per_step(owner):
    """FY-PULL-018: over the agent stream, a replica's image progress is stored and shown (its
    `download`, phase image), and only the step into pulling is an event, not every progress report.
    A pull cancelled because the workload was stopped (not removed) says "stopped"."""
    from tests.machines_helpers import connect, joined
    from tests.test_workload_stream import online, recv_type, status
    from tests.workload_helpers import container as ctr_spec, create_workload, stop_workload

    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        online(ws)
        recv_type(ws, "desired")
        w = create_workload(owner, ctr_spec(machine_id=j["node_id"]))["workload"]
        rid = recv_type(ws, "desired")["replicas"][0]["id"]
        ws.send_json(status(rid, state="starting", reason="starting the container"))
        for n in range(1, 6):
            ws.send_json(status(rid, state="starting", reason=f"pulling the image: 0.{n}0 of 1.00 GiB ({n * 10}%)",
                                download={"phase": "image", "done": n * 100 * MIB, "total": 1000 * MIB,
                                          "layers": 3, "layers_done": 1}))
        ws.send_json({"v": 1, "type": "heartbeat", "seq": 1, "metrics": []})
        for _ in range(50):
            got = owner.get(f"/api/workloads/{w['id']}").json()
            rep = got["workload"]["replica_list"][0]
            if (rep.get("download") or {}).get("done") == 500 * MIB:
                break
            time.sleep(0.05)
        assert rep["download"] == {"phase": "image", "done": 500 * MIB, "total": 1000 * MIB, "layers": 3,
                                   "layers_done": 1}
        assert rep["reason"] == "pulling the image: 0.50 of 1.00 GiB (50%)"
        pulls = [e for e in got["events"] if "pulling the image" in e["message"]]
        assert len(pulls) == 1, got["events"]
        stop_workload(w["id"])
        recv_type(ws, "desired")
        ws.send_json({"v": 1, "type": "status", "full": False, "replicas": [
            {"id": rid, "generation": 1, "state": "stopped", "reason": "cancelled: the workload was removed"}]})
        ws.send_json({"v": 1, "type": "heartbeat", "seq": 2, "metrics": []})
        for _ in range(50):
            rep = owner.get(f"/api/workloads/{w['id']}").json()["workload"]["replica_list"][0]
            if rep["state"] == "stopped":
                break
            time.sleep(0.05)
        assert rep["state"] == "stopped" and rep["reason"] == "cancelled: the workload was stopped"
        assert rep["download"] is None


def test_fy_pull_019_an_unexpected_error_while_starting_is_the_replicas_reason(tmp_path, caplog):
    """FY-PULL-019: an error nobody planned for while a replica starts (here the image step raising what the agent
    image raised on a real machine, "No module named 'sqlalchemy'") ends that attempt with the error as its
    reason, shown in the console, then the usual backoff; never "ended while the agent was not watching" with
    the cause only in the agent's log. Nothing is run."""
    fake = FakeDocker(tmp_path / "bin")
    rt = _rt(tmp_path, fake)

    def broken_pull(rec, progress, cancel, *through):
        raise ModuleNotFoundError("No module named 'sqlalchemy'")

    rt.pull = broken_pull
    with caplog.at_level("ERROR"):
        sup, reasons, _ = _supervise(tmp_path, rt, container(image=ENGINE_IMAGE))
    rec = sup.records["feedface01234567"]
    assert rec.state == "backoff"
    assert rec.reason.startswith("the agent failed while starting it (ModuleNotFoundError: No module named "
                                 "'sqlalchemy')")
    assert not any(r and "not watching" in r for r in reasons)
    assert fake.runs() == []
    assert any("failed while starting it" in m for m in caplog.messages)
