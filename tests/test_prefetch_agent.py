""""Download to…" on the agent (1.4, issue #35, protocol feature "prefetch"; FY-PF-001…009): downloads from
`desired.downloads` go into the model cache with a replica's code (locks, resume, checksums, cancel), start
nothing, and are reported; a download no longer listed stops at once and resumes when asked again; at most
two run at a time; tokens stay in memory; engine images are pulled by the container runtime; URL models
get their SHA-256 recorded and checked (D8)."""

import asyncio
import hashlib
import io
import json
import os
import tarfile
import threading

import pytest

from forgeyard.agent.models import CacheError, ModelCache, validate_model
from forgeyard.agent.workloads import prefetch as agent_prefetch
from forgeyard.agent.workloads.process import ProcessRuntime
from forgeyard.agent.workloads.records import RecordStore
from forgeyard.agent.workloads.runtime import StartError
from forgeyard.agent.workloads.supervisor import Supervisor
from forgeyard.models.sources import cache_key
from tests.model_helpers import FakeHF, Repo, git_oid
from tests.waits import until

REV = "a" * 40
BIG = os.urandom(240_000)


def block(hf, repo: Repo, **extra) -> dict:
    """The model block the server sends (catalog.model_block), for every file of the fake repository."""
    files = [{"path": p, "size": len(d), "oid": git_oid(d), **({"sha256": hashlib.sha256(d).hexdigest()}
                                                               if p.endswith(".gguf") else {})}
             for p, d in repo.files.items()]
    return {"kind": "hf", "format": "gguf", "file": None, "repo": repo.id, "revision": repo.sha, "ref": "main",
            "endpoint": hf.url, "files": files, **extra}


def engine_archive() -> bytes:
    buf = io.BytesIO()
    data = b"#!/bin/sh\necho engine\n"
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("eng-b1/server")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def supervisor(tmp_path, cache, runtimes=None) -> Supervisor:
    rt = ProcessRuntime(RecordStore(tmp_path / "state"), tmp_path / "work", euid=lambda: 4242)
    sup = Supervisor(tmp_path / "state", runtimes or {"process": rt}, models=cache, tick=0.05,
                     bind_address="127.0.0.1", port_range=(31800, 31899))
    sup.server_features = {"workloads", "models", "prefetch"}
    return sup


def want(sup: Supervisor, *items):
    """A desired list from the server with these downloads (and no replicas)."""
    sup.set_desired({"revision": 0, "replicas": [], "downloads": list(items)})


def job(sup, jid):
    return sup.prefetch.jobs.get(jid)


@pytest.fixture()
def hf():
    server = FakeHF(Repo("acme/gguf", REV, {"m-Q4_K_M.gguf": b"GGUF" + BIG, "README.md": b"# readme"}),
                    Repo("acme/two", REV, {"two.gguf": b"GGUF" + BIG}),
                    Repo("acme/three", REV, {"three.gguf": b"GGUF" + BIG}),
                    Repo("engines/eng", "e" * 40, {"eng.tgz": engine_archive()}))
    yield server
    server.close()


def test_fy_pf_001_a_download_fills_the_cache_and_starts_nothing(tmp_path, hf):
    """FY-PF-001: the model and its engine archive land in the cache, progress is reported while it runs
    (never past the total), the agent says "done" and that its cache changed; no replica is created."""
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    repo = hf.repos["acme/gguf"]
    data = repo.files["m-Q4_K_M.gguf"]
    engine = {"name": "llama-cpp-b1", "url": f"{hf.url}/engines/eng/resolve/{'e' * 40}/eng.tgz",
              "sha256": hashlib.sha256(engine_archive()).hexdigest(), "binary": "eng-b1/server"}
    hf.rate = 16384

    async def scenario():
        sup = supervisor(tmp_path, cache)
        sup.cache_dirty = False
        want(sup, {"id": "d1", "model": block(hf, repo), "engine": engine})
        seen = await until(lambda: (job(sup, "d1").download or {}).get("done"), "progress")
        d = job(sup, "d1").download
        assert d["phase"] == "model" and 0 < seen <= d["total"] == len(data) + len(b"# readme")
        assert job(sup, "d1").state == "downloading" and job(sup, "d1").reason.startswith("downloading the model:")
        await until(lambda: job(sup, "d1").state == "done", "the download to finish", timeout=30)
        report = sup.take_report()
        assert report["downloads"] == [{"id": "d1", "state": "done", "reason": None}]
        assert report["model_cache"]["cached"][0] == cache_key(validate_model(block(hf, repo), []))
        assert report["model_cache"]["engines"] == [engine["sha256"][:16]]
        assert sup.records == {} and sup.started == 0 and sup.ops == {}
        await sup.close()

    asyncio.run(scenario())
    assert cache.is_cached(validate_model(block(hf, repo), []))
    assert (tmp_path / "cache" / "engines" / f"llama-cpp-b1-{engine['sha256'][:16]}" / "eng-b1" / "server").is_file()


def test_fy_pf_002_no_longer_wanted_stops_at_once_and_resumes_later(tmp_path, hf):
    """FY-PF-002: a download missing from the next list is cancelled while it waits on the network, its lock
    is released and its partial file kept; asked again (a new id, as the server asks again), it resumes from
    that file with a Range request and finishes."""
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    repo = hf.repos["acme/two"]
    model = validate_model(block(hf, repo), [])
    hf.rate = 4096

    async def scenario():
        sup = supervisor(tmp_path, cache)
        want(sup, {"id": "d1", "model": block(hf, repo)})
        await until(lambda: (job(sup, "d1").download or {}).get("done", 0) > 20_000, "some bytes")
        want(sup)
        await until(lambda: "d1" not in sup.prefetch.jobs, "the cancel", timeout=5)
        assert not cache.busy(cache_key(model))
        part = next((tmp_path / "cache" / ".partial").glob("*.part"))
        have = part.stat().st_size
        assert 0 < have < len(repo.files["two.gguf"])
        hf.rate = None
        want(sup, {"id": "d2", "model": block(hf, repo)})
        await until(lambda: job(sup, "d2").state == "done", "the resumed download", timeout=30)
        await sup.close()
        return have

    have = asyncio.run(scenario())
    assert cache.is_cached(model)
    assert hf.downloads("two.gguf")[-1]["range"] == f"bytes={have}-"


def test_fy_pf_003_it_shares_the_models_lock_with_replica_downloads(tmp_path, hf):
    """FY-PF-003: while a replica downloads the same model, the download waits and says so; then it finds
    the model there and is done without fetching a byte again."""
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    repo = hf.repos["acme/two"]
    model = validate_model(block(hf, repo), [])
    hf.rate = 8192
    replica = threading.Thread(target=lambda: cache.ensure_model(model))
    replica.start()

    async def scenario():
        await until(lambda: cache.busy(cache_key(model)), "the replica's download")
        sup = supervisor(tmp_path, cache)
        want(sup, {"id": "d1", "model": block(hf, repo)})
        await until(lambda: job(sup, "d1").reason == "waiting for another download of the same model on this machine",
                    "the wait")
        await until(lambda: job(sup, "d1").state == "done", "the download", timeout=30)
        await sup.close()

    asyncio.run(scenario())
    replica.join(10)
    assert len(hf.downloads("two.gguf")) == 1


def test_fy_pf_004_two_at_a_time_the_others_wait(tmp_path, hf):
    """FY-PF-004: a third download waits ("queued", saying why) until one of the first two ends."""
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    hf.rate = 2048

    async def scenario():
        sup = supervisor(tmp_path, cache)
        items = [{"id": f"d{i}", "model": block(hf, hf.repos[r])} for i, r in enumerate(("acme/gguf", "acme/two", "acme/three"))]
        want(sup, *items)
        await until(lambda: job(sup, "d0").state == "downloading" and job(sup, "d1").state == "downloading", "two running")
        third = job(sup, "d2")
        assert third.state == "queued" and third.task is None
        assert third.reason == "waiting for another download on this machine to finish"
        assert {e["id"]: e["state"] for e in sup.take_report()["downloads"]} == {
            "d0": "downloading", "d1": "downloading", "d2": "queued"}
        want(sup, items[1], items[2])  # d0 is cancelled: d2 may start
        await until(lambda: job(sup, "d2").state == "downloading", "the third to start")
        assert agent_prefetch.MAX_RUNNING == 2
        await sup.close()

    asyncio.run(scenario())


def test_fy_pf_005_refused_blocks_and_tokens_in_memory_only(tmp_path):
    """FY-PF-005: a model block with a path outside the repository is refused (failed, saying why, nothing
    fetched); a gated model's token is used for its requests, never reported, and forgotten when it ends."""
    gated = Repo("acme/gated", REV, {"g.gguf": b"GGUF" + b"g" * 1000}, gated=True)
    hf = FakeHF(gated)
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    try:
        async def scenario():
            sup = supervisor(tmp_path, cache)
            bad = block(hf, gated)
            bad["files"] = [{"path": "../../etc/passwd", "size": 10, "oid": "1" * 40}]
            want(sup, {"id": "bad", "model": bad}, {"id": "g", "model": block(hf, gated, token=FakeHF.TOKEN)})
            assert job(sup, "bad").state == "failed" and job(sup, "bad").reason.startswith("refused the download:")
            await until(lambda: job(sup, "g").state == "done", "the gated download")
            report = json.dumps(sup.take_report())
            assert FakeHF.TOKEN not in report and '"bad"' in report
            assert sup.prefetch.tokens == {}
            assert "token" not in (job(sup, "g").model or {})
            await sup.close()

        asyncio.run(scenario())
        assert {r["auth"] for r in hf.requests if "/resolve/" in r["path"] and "/cdn/" not in r["path"]} == {
            f"Bearer {FakeHF.TOKEN}"}
    finally:
        hf.close()


class ImageRuntime:
    """A container runtime that only pulls (what a download of an engine image needs)."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def info(self):
        return {"ok": True}

    def ensure_image(self, image, check, progress=None, cancel=None, *, who=""):
        self.calls.append((image, check))
        if self.fail:
            raise StartError(self.fail)
        progress({"done": 50, "total": 100, "layers": 2, "layers_done": 1})
        return image, "pinned"


def test_fy_pf_006_engine_images_are_pulled_by_the_container_runtime(tmp_path):
    """FY-PF-006: an image (with its recorded ids) goes to the container runtime's pull, its progress is
    reported per layer, and a failed pull or a machine without a container engine fails with the reason."""
    from forgeyard.workloads import images

    cache = ModelCache(tmp_path / "cache", allow_private=True)
    image = "ghcr.io/acme/engine:1@sha256:" + "b" * 64
    ids = {"ids": ["sha256:" + "c" * 64], "content": ["d" * 64]}

    async def scenario():
        rt = ImageRuntime()
        sup = supervisor(tmp_path, cache, {"container": rt})
        seen = []
        orig = sup.prefetch._pull_progress
        sup.prefetch._pull_progress = lambda j, snap: (orig(j, snap), seen.append(dict(j.download or {})))
        want(sup, {"id": "i1", "image": image, "image_ids": ids})
        await until(lambda: job(sup, "i1").state == "done", "the pull")
        assert rt.calls == [(image, images.parse_check(ids))]
        assert seen and seen[0]["phase"] == "image" and seen[0]["layers"] == 2 and seen[0]["layers_done"] == 1
        bad = supervisor(tmp_path, cache, {"container": ImageRuntime(fail="the registry could not be reached")})
        want(bad, {"id": "i2", "image": image})
        await until(lambda: job(bad, "i2").state == "failed", "the failure")
        assert job(bad, "i2").reason == "the registry could not be reached"
        none = supervisor(tmp_path, cache)
        want(none, {"id": "i3", "image": image})
        await until(lambda: job(none, "i3").state == "failed", "no engine")
        assert "no container engine" in job(none, "i3").reason
        for s in (sup, bad, none):
            await s.close()

    asyncio.run(scenario())


def test_fy_pf_007_reports_go_only_to_a_server_that_asks_for_downloads(tmp_path, hf):
    """FY-PF-007: a server without "prefetch" hears nothing of downloads (and sends none); a full report
    lists every download the agent has."""
    cache = ModelCache(tmp_path / "cache", allow_private=True)

    async def scenario():
        sup = supervisor(tmp_path, cache)
        want(sup, {"id": "d1", "model": block(hf, hf.repos["acme/two"])})
        await until(lambda: job(sup, "d1").state == "done", "the download")
        sup.server_features = {"workloads", "models"}
        quiet = sup.take_report()
        assert quiet is None or "downloads" not in quiet
        sup.server_features = {"workloads", "models", "prefetch"}
        sup.full_ready = True
        assert [d["id"] for d in sup.take_report()["downloads"]] == ["d1"]
        sup.set_desired({"revision": 0, "replicas": []})  # an older server's list: no downloads key
        assert sup.prefetch.jobs == {}
        await sup.close()

    asyncio.run(scenario())


def test_fy_pf_008_a_url_models_sha256_is_recorded_then_required(tmp_path):
    """FY-PF-008 (D8): a URL model has no published checksum. After its first complete download its SHA-256
    is kept in the marker and listed with the cache; a later download that must match a different one is
    refused, and nothing of it is left in the cache."""
    data = b"GGUF" + os.urandom(5000)
    hf = FakeHF(Repo("files/u", REV, {"tiny.gguf": data}))
    try:
        url = f"{hf.url}/files/u/resolve/{REV}/tiny.gguf"
        first = ModelCache(tmp_path / "one", allow_private=True)
        model = validate_model({"kind": "url", "url": url, "files": [{"path": "tiny.gguf", "size": len(data)}]}, [])
        assert model["sha256"] is None
        first.ensure_model(model)
        sha = hashlib.sha256(data).hexdigest()
        [entry] = first.info()["models"]
        assert entry["kind"] == "url" and entry["sha256"] == sha
        marker = json.loads(next((tmp_path / "one" / "url").glob("*/.forgeyard.json")).read_text())
        assert marker["sha256"] == sha
        other = ModelCache(tmp_path / "two", allow_private=True)
        wrong = validate_model({"kind": "url", "url": url, "files": [{"path": "tiny.gguf", "size": len(data),
                                                                      "sha256": "0" * 64}]}, [])
        with pytest.raises(CacheError, match="SHA-256 mismatch"):
            other.ensure_model(wrong)
        assert not other.is_cached(wrong)
        assert list((tmp_path / "two" / ".partial").glob("*")) == []
        right = validate_model({"kind": "url", "url": url, "files": [{"path": "tiny.gguf", "size": len(data),
                                                                      "sha256": sha}]}, [])
        other.ensure_model(right)
        assert other.is_cached(right)
    finally:
        hf.close()


def test_fy_pf_009_the_agent_lists_prefetch_when_it_has_a_model_cache(tmp_path, monkeypatch):
    """FY-PF-009: the hello of an agent with a model cache lists "prefetch" (and one without does not)."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory

    monkeypatch.setenv(SIMULATION_ENV, "1")
    inv = SimulatedInventory("nvidia-rtx4090x2")
    with_cache = Agent("http://127.0.0.1:1", "c", inv, supervisor=supervisor(tmp_path, ModelCache(tmp_path / "c")))
    assert "prefetch" in with_cache.hello(inv.full())["features"]
    rt = ProcessRuntime(RecordStore(tmp_path / "s2"), tmp_path / "w2", euid=lambda: 4242)
    without = Agent("http://127.0.0.1:1", "c", inv, supervisor=Supervisor(tmp_path / "s2", {"process": rt}))
    assert "prefetch" not in without.hello(inv.full())["features"]
