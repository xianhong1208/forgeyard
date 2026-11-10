"""Model replicas on the agent: fetch the model (and a pinned engine), report progress, start
with the model's paths, mount it read-only into containers, launch replicas that share a GPU one
at a time, cancel unwanted downloads, keep tokens off disk (FY-MSUP-*)."""

import asyncio
import hashlib
import io
import json
import tarfile
import time
import urllib.request

import pytest

from forgeyard.agent.models import ModelCache
from forgeyard.agent.workloads.container import ContainerRuntime
from forgeyard.agent.workloads.process import ProcessRuntime
from forgeyard.agent.workloads.records import RecordStore
from forgeyard.agent.workloads.supervisor import Supervisor
from tests.model_helpers import TINY_REV, FakeHF, Repo, tiny_repo
from tests.workload_helpers import FakeDocker, converge, desired, gpus_of

A100 = gpus_of("nvidia-a100x8")
GGUF_REV = "9" * 40

FAKE_ENGINE = b"""#!/usr/bin/python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
args = sys.argv[1:]
opt = lambda n: args[args.index(n) + 1]
model, port, host = opt("-m"), int(opt("--port")), opt("--host")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        head = open(model, "rb").read(4).decode()
        body = json.dumps({"model": model, "head": head, "writable": os.access(model, os.W_OK),
                           "cache": os.environ.get("HF_HUB_CACHE")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
ThreadingHTTPServer((host, port), H).serve_forever()
"""


def engine_tgz():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("fake-b1/fake-engine")
        info.size, info.mode = len(FAKE_ENGINE), 0o755
        tar.addfile(info, io.BytesIO(FAKE_ENGINE))
    return buf.getvalue()


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


@pytest.fixture()
def hf():
    tgz = engine_tgz()
    server = FakeHF(tiny_repo(), Repo("acme/tiny-gguf", GGUF_REV, {"tiny.gguf": b"GGUF" + bytes(4096)}),
                    Repo("engines/fake", "e" * 40, {"fake.tgz": tgz}))
    server.tgz = tgz
    yield server
    server.close()


def hf_model(hf, repo, rev, file=None, token=None):
    info = hf.repos[repo].info
    files = [{"path": s["rfilename"], "size": s["size"], "sha256": (s.get("lfs") or {}).get("sha256"),
              "oid": s["blobId"]} for s in info["siblings"] if not s["rfilename"].endswith(".bin")]
    m = {"kind": "hf", "repo": repo, "revision": rev, "ref": "main", "endpoint": hf.url, "files": files, "file": file}
    if token:
        m["token"] = token
    return m


def model_replica(rid, spec, *, model, engine=None, gpus=None, generation=1):
    out = {"id": rid, "workload_id": 1, "workload": spec["name"], "ordinal": 0, "generation": generation,
           "kind": "service", "spec": spec, "gpus": gpus or [], "model": model}
    if engine:
        out["engine"] = engine
    return out


def process_spec(name="small"):
    return {"name": name, "kind": "service", "env": {"HF_HUB_CACHE": "${MODEL_HUB}"},
            "runtime": {"type": "process", "command": "fake-engine", "args": ["-m", "${MODEL_FILE}", "--port", "${PORT}",
                                                                           "--host", "${BIND_ADDRESS}"]},
            "ports": [{"name": "http"}], "health": {"type": "http", "path": "/health", "interval": 1,
                                                    "start_period": 60},
            "resources": {"gpus": {"count": 0}}}


def test_fy_msup_001_process_engine_and_model_are_fetched_then_started(tmp_path, hf):
    """FY-MSUP-001: the agent downloads the pinned engine and the model (reporting progress),
    starts the engine with ${MODEL_FILE} etc. filled with cache paths, on its own address; the
    model files are read-only; a token stays in memory only."""
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    rt = ProcessRuntime(RecordStore(tmp_path / "state"), tmp_path / "work", euid=lambda: 4242)
    sup = Supervisor(tmp_path / "state", {"process": rt}, models=cache, tick=0.05, bind_address="127.0.0.1",
                     port_range=(31500, 31599))
    engine = {"name": "fake-b1", "url": f"{hf.url}/engines/fake/resolve/{'e' * 40}/fake.tgz",
              "sha256": hashlib.sha256(hf.tgz).hexdigest(), "binary": "fake-b1/fake-engine"}
    reports, phases = [], set()
    real_progress = sup._progress

    def progress(rec_id, phase, done, total, file):
        phases.add(phase)
        real_progress(rec_id, phase, done, total, file)

    sup._progress = progress

    def collect(s):
        r = s.take_report()
        if r:
            reports.append(r)
        return "r1" in s.records and s.records["r1"].state == "healthy"

    async def scenario():
        sup.set_desired(desired(model_replica("r1", process_spec(), engine=engine,
                                              model=hf_model(hf, "acme/tiny-gguf", GGUF_REV, "tiny.gguf",
                                                             token="hf_secret_xyz"))))
        await converge(sup, collect, timeout=30)
        rec = sup.records["r1"]
        port = rec.ports[0]["host_port"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            seen = json.loads(resp.read())
        return rec, seen

    try:
        rec, seen = asyncio.run(scenario())
    finally:
        for p in list(rt.children.values()):
            p.kill()
            p.wait(5)
    snap = tmp_path / "cache" / "hub" / "models--acme--tiny-gguf" / "snapshots" / GGUF_REV
    assert seen["model"] == str(snap / "tiny.gguf") and seen["head"] == "GGUF"
    assert seen["cache"] == str(tmp_path / "cache" / "hub")
    assert rec.engine_path.endswith("fake-b1/fake-engine") and "/engines/fake-b1-" in rec.engine_path
    assert phases == {"engine", "model"}
    assert any(rep.get("model_cache", {}).get("cached") == [
        f"hub/models--acme--tiny-gguf@{GGUF_REV}", f"hub/models--acme--tiny-gguf@{GGUF_REV}/tiny.gguf"] for rep in reports)
    on_disk = "".join(p.read_text() for p in (tmp_path / "state" / "replicas").glob("*.json"))
    assert "hf_secret_xyz" not in on_disk and "hf_secret_xyz" not in json.dumps(reports)
    assert sup.tokens == {"r1": "hf_secret_xyz"}
    assert "hf_secret_xyz" not in json.dumps(sup.desired)


def container_spec(name="chat", share="vram"):
    return {"name": name, "kind": "service", "env": {"HF_HUB_CACHE": "${MODEL_HUB}", "HF_HUB_OFFLINE": "1"},
            "runtime": {"type": "container", "image": "vllm/vllm-openai:v0.32.0", "command": ["vllm", "serve"],
                        "args": ["${MODEL_PATH}", "--port", "8000", "--gpu-memory-utilization", "0.174"]},
            "ports": [{"name": "http", "port": 8000}],
            "health": {"type": "http", "path": "/health", "interval": 10, "start_period": 600},
            "resources": {"gpus": {"count": 1, "vendor": "nvidia", "vram": 8 * 1024**3, "share": share}}}


def _ctr(tmp_path, hf, prober=None, clock=None):
    fake = FakeDocker(tmp_path / "bin")
    rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="agent-1", engine=str(fake.path))
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    kw = {"prober": prober} if prober else {}
    sup = Supervisor(tmp_path / "state", {"container": rt}, models=cache, tick=0.05, stop_grace=1,
                     bind_address="127.0.0.1", clock=clock or Clock(), gate_poll=0.05, **kw)
    return sup, fake


def test_fy_msup_002_containers_get_the_model_mounted_read_only_and_nothing_else(tmp_path, hf):
    """FY-MSUP-002: one bind mount, the model's cache entry, read-only, at /models/hub/...; the
    arguments and environment point inside it; only the assigned GPU."""
    sup, fake = _ctr(tmp_path, hf)

    async def scenario():
        sup.set_desired(desired(model_replica("c1", container_spec(), gpus=[A100[4]],
                                              model=hf_model(hf, "acme/tiny-chat", TINY_REV))))
        await converge(sup, lambda s: "c1" in s.records and s.records["c1"].state == "starting"
                       and s.records["c1"].container)

    asyncio.run(scenario())
    run = fake.runs()[0]
    mounts = [run[i + 1] for i, a in enumerate(run) if a == "--mount"]
    host = tmp_path / "cache" / "hub" / "models--acme--tiny-chat"
    assert mounts == [f"type=bind,source={host},target=/models/hub/models--acme--tiny-chat,ro=true"]
    assert "-v" not in run and "--volume" not in run and "--privileged" not in run
    assert run[run.index("vllm/vllm-openai:v0.32.0") + 1:] == [
        "serve", f"/models/hub/models--acme--tiny-chat/snapshots/{TINY_REV}", "--port", "8000",
        "--gpu-memory-utilization", "0.174"]
    assert f'"device={A100[4]["uuid"]}"' in run
    env = next(iter(fake.state().values()))["env"]
    assert env["HF_HUB_CACHE"] == "/models/hub" and env["NVIDIA_VISIBLE_DEVICES"] == A100[4]["uuid"]


def test_fy_msup_003_replicas_sharing_a_gpu_load_one_at_a_time(tmp_path, hf):
    """FY-MSUP-003: the second replica on GPU #4 waits until the first has loaded (healthy), so
    two engines never measure the same free memory at once; failures within the start period
    are "loading", not "unhealthy"."""
    healthy = {"c1": False}
    clock = Clock()

    def prober(kind, host, port, path, timeout):
        return None if healthy["c1"] else "GET /health: connection refused"

    sup, fake = _ctr(tmp_path, hf, prober=prober, clock=clock)
    model = hf_model(hf, "acme/tiny-chat", TINY_REV)

    async def rounds(n=10):
        for _ in range(n):
            await sup.reconcile_once()
            await asyncio.sleep(0.05)

    async def scenario():
        sup.set_desired(desired(model_replica("c1", container_spec("one"), gpus=[A100[4]], model=model),
                                model_replica("c2", container_spec("two"), gpus=[A100[4]], model=model)))
        await rounds(20)
        first = [r for r in sup.records.values() if r.container]
        assert len(first) == 1 and first[0].loading
        other = next(r for r in sup.records.values() if r is not first[0])
        assert other.reason.startswith("waiting for") and "to finish loading on the shared GPU" in other.reason
        clock.t += 11
        await rounds(5)
        assert first[0].state == "starting" and first[0].reason == "loading; waiting for the health check to pass"
        assert len(fake.runs()) == 1
        healthy["c1"] = True
        clock.t += 11
        await rounds(20)
        assert first[0].state == "healthy" and not first[0].loading
        assert len(fake.runs()) == 2
        await sup.settle()

    asyncio.run(scenario())


def test_fy_msup_004_an_unwanted_download_is_cancelled(tmp_path):
    """FY-MSUP-004: the deployment is deleted while its model downloads: the download stops, the
    replica is gone, nothing is cached."""
    big = Repo("acme/big", "b" * 40, {"w.safetensors": bytes(600_000), "config.json": b"{}"})
    hf = FakeHF(big, rate=8192)
    try:
        sup, fake = _ctr(tmp_path, hf)
        m = hf_model(hf, "acme/big", "b" * 40)

        async def scenario():
            sup.set_desired(desired(model_replica("c1", container_spec(), gpus=[A100[4]], model=m)))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                await sup.reconcile_once()
                await asyncio.sleep(0.05)
                rec = sup.records.get("c1")
                if rec and rec.download and rec.download["done"] > 20_000:
                    break
            assert sup.records["c1"].reason.startswith("downloading the model")
            sup.set_desired(desired(revision=5))
            await converge(sup, lambda s: not s.records, timeout=10)

        asyncio.run(scenario())
        assert fake.runs() == [] and not sup.models.cached_keys()
    finally:
        hf.close()


def test_fy_msup_005_without_a_model_cache_model_replicas_fail_clearly(tmp_path, hf):
    """FY-MSUP-005"""
    fake = FakeDocker(tmp_path / "bin")
    rt = ContainerRuntime(RecordStore(tmp_path / "state"), agent_id="a", engine=str(fake.path))
    sup = Supervisor(tmp_path / "state", {"container": rt}, tick=0.05, bind_address="127.0.0.1")

    async def scenario():
        sup.set_desired(desired(model_replica("c1", container_spec(), gpus=[A100[4]],
                                              model=hf_model(hf, "acme/tiny-chat", TINY_REV))))
        await converge(sup, lambda s: "c1" in s.records and s.records["c1"].final)

    asyncio.run(scenario())
    assert "no model cache" in sup.records["c1"].reason and fake.runs() == []
    assert sup.model_cache_info() is None
