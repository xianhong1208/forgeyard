"""The agent's model cache, listed with sizes and removed from (1.4, issue #35, "cache_admin"):
sizes per revision, GGUF file, URL model and engine; removal like `hf cache rm` (shared blobs kept);
refusal while a replica uses the files or a download runs; keys from the server never used as
paths; partial downloads; the listing within its share of a message (FY-CACHE-*)."""

import asyncio
import hashlib
import json
import os
import threading

import pytest

from forgeyard.agent import models as am
from forgeyard.agent.models import CacheRefused, ModelCache, tree_bytes, validate_model
from forgeyard.agent.workloads.process import ProcessRuntime
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.agent.workloads.supervisor import Supervisor
from forgeyard.models import cache_keys, sources
from tests.model_helpers import FakeHF, Repo, git_oid
from tests.waits import wait_until

REV_A = "a" * 40
REV_B = "b" * 40
GGUF_REV = "c" * 40
SHARED = b'{"shared": "config"}'
WEIGHTS = b"W" * 3000
GGUF_FILES = {"m-Q4_K_M.gguf": b"GGUF" + b"4" * 2000, "m-Q8_0.gguf": b"GGUF" + b"8" * 4000,
              "big-00001-of-00002.gguf": b"GGUF" + b"x" * 1500, "big-00002-of-00002.gguf": b"y" * 1500,
              "mmproj-m-f16.gguf": b"GGUF" + b"p" * 700}


@pytest.fixture()
def hf():
    files = {"config.json": SHARED, "model.safetensors": WEIGHTS, "README.md": b"# readme for revision a",
             "tokenizer.json": b'{"tok": "only in a"}'}
    server = FakeHF(Repo("acme/chat", REV_A, files), Repo("acme/gguf", GGUF_REV, GGUF_FILES))
    yield server
    server.close()


def entry_for(hf, repo, rev, paths, file=None):
    """A validated model block for these files of the (fake) repository at this revision (the fake
    serves the same bytes for any revision, so two revisions can share blobs)."""
    data = hf.repos[repo].files
    files = []
    for p in paths:
        lfs = p.endswith((".safetensors", ".gguf"))
        files.append({"path": p, "size": len(data[p]), "sha256": hashlib.sha256(data[p]).hexdigest() if lfs else None,
                      "oid": git_oid(data[p])})
    return validate_model({"kind": "hf", "repo": repo, "revision": rev, "ref": "main", "endpoint": hf.url,
                           "files": files, "file": file}, [])


def by_key(info, key):
    return next(e for e in info["models"] if e["key"] == key)


def test_fy_cache_001_info_lists_each_model_with_its_size(tmp_path, hf):
    """FY-CACHE-001: a revision with its files' total (from the files on disk), each GGUF model file of a
    revision on its own (a split model once, with all its parts), a URL model, an engine; what the cache
    takes on disk with shared blobs once; and the partial downloads."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    cache.ensure_model(entry_for(hf, "acme/chat", REV_A, ["config.json", "model.safetensors", "README.md"]))
    cache.ensure_model(entry_for(hf, "acme/chat", REV_B, ["config.json", "model.safetensors"]))
    cache.ensure_model(entry_for(hf, "acme/gguf", GGUF_REV, ["m-Q4_K_M.gguf", "mmproj-m-f16.gguf"], "m-Q4_K_M.gguf"))
    cache.ensure_model(entry_for(hf, "acme/gguf", GGUF_REV, ["big-00001-of-00002.gguf", "big-00002-of-00002.gguf"],
                                 "big-00001-of-00002.gguf"))
    url = tmp_path / "c" / "url" / ("d" * 24)
    url.mkdir()
    (url / "tiny.gguf").write_bytes(b"GGUF" + bytes(96))
    (url / ".forgeyard.json").write_text(json.dumps({"url": "https://x/tiny.gguf", "file": "tiny.gguf",
                                                     "completed_at": 1.0}))
    eng = tmp_path / "c" / "engines" / f"llama-cpp-{'e' * 16}"
    (eng / "bin").mkdir(parents=True)
    (eng / "bin" / "llama-server").write_bytes(b"E" * 512)
    (tmp_path / "c" / ".partial" / "x.part").write_bytes(b"p" * 333)

    info = cache.info()
    a = by_key(info, f"hub/models--acme--chat@{REV_A}")
    assert a["kind"] == "hf" and a["repo"] == "acme/chat" and a["revision"] == REV_A and a["files"] == 3
    assert a["bytes"] == len(SHARED) + len(WEIGHTS) + len(b"# readme for revision a") and a["completed_at"] > 0
    assert "parts" not in a and not any(k.startswith("_") for k in a)
    b = by_key(info, f"hub/models--acme--chat@{REV_B}")
    assert b["bytes"] == len(SHARED) + len(WEIGHTS)  # logical: the shared blobs count in both revisions
    g = by_key(info, f"hub/models--acme--gguf@{GGUF_REV}")
    assert g["files"] == 4
    assert [(p["file"], p["bytes"]) for p in g["parts"]] == [
        ("big-00001-of-00002.gguf", 3004), ("m-Q4_K_M.gguf", 2004)]  # no projector, split parts once
    assert all(p["key"] == f"{g['key']}/{p['file']}" and "paths" not in p for p in g["parts"])
    assert by_key(info, f"url/{'d' * 24}") == {"key": f"url/{'d' * 24}", "kind": "url", "file": "tiny.gguf",
                                               "bytes": 100, "files": 1, "completed_at": 1.0}
    assert info["engines_detail"] == [{"key": f"engine/{'e' * 16}", "id": "e" * 16, "name": "llama-cpp", "bytes": 512}]
    assert info["partial_bytes"] == 333 and info["partial_files"] == 1
    on_disk = sum(tree_bytes(tmp_path / "c" / d) for d in ("hub", "url", "engines")) + 333
    assert info["used_bytes"] == on_disk and info["used_bytes"] < sum(e["bytes"] for e in info["models"]) + 512 + 333
    assert "truncated" not in info


def test_fy_cache_002_removing_a_revision_deletes_its_files_and_frees_the_space(tmp_path, hf):
    """FY-CACHE-002: the snapshot, the marker, the ref and the blobs go, then the empty repository
    directory; the bytes freed are the blobs' (what du sees going); it is no longer cached."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    model = entry_for(hf, "acme/chat", REV_A, ["config.json", "model.safetensors", "README.md"])
    cache.ensure_model(model)
    base = tmp_path / "c" / "hub" / "models--acme--chat"
    before = tree_bytes(base)
    blobs = sum(p.stat().st_size for p in (base / "blobs").iterdir())
    freed = cache.remove(f"hub/models--acme--chat@{REV_A}")
    assert freed == blobs and before - blobs < 1024  # what is left besides blobs: links, the marker, the ref
    assert not base.exists() and cache.cached_keys() == [] and not cache.is_cached(model)
    assert cache.info()["models"] == []
    cache.ensure_model(model)  # downloads again
    assert cache.is_cached(model)


def test_fy_cache_003_blobs_another_revision_uses_are_kept(tmp_path, hf):
    """FY-CACHE-003: two revisions share config.json and the weights. Removing one deletes only the blobs
    nothing else links to; the other revision still reads every file."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    cache.ensure_model(entry_for(hf, "acme/chat", REV_A, ["config.json", "model.safetensors", "README.md",
                                                          "tokenizer.json"]))
    b = entry_for(hf, "acme/chat", REV_B, ["config.json", "model.safetensors"])
    cache.ensure_model(b)
    base = tmp_path / "c" / "hub" / "models--acme--chat"
    assert len(list((base / "blobs").iterdir())) == 4
    freed = cache.remove(f"hub/models--acme--chat@{REV_A}")
    assert freed == len(b"# readme for revision a") + len(b'{"tok": "only in a"}')
    assert len(list((base / "blobs").iterdir())) == 2
    snap = base / "snapshots" / REV_B
    assert (snap / "config.json").read_bytes() == SHARED and (snap / "model.safetensors").read_bytes() == WEIGHTS
    assert not (base / "snapshots" / REV_A).exists() and not (base / ".forgeyard" / f"{REV_A}.json").exists()
    assert cache.is_cached(b) and cache.cached_keys() == [f"hub/models--acme--chat@{REV_B}"]
    assert (base / "refs" / "main").read_text() == REV_B  # a ref naming the other revision stays


def test_fy_cache_004_one_gguf_file_of_a_revision_and_then_the_last(tmp_path, hf):
    """FY-CACHE-004: removing one GGUF file keeps the revision's other model files (and the projector they
    share); removing the last model file takes the revision with it."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    q4 = entry_for(hf, "acme/gguf", GGUF_REV, ["m-Q4_K_M.gguf", "mmproj-m-f16.gguf"], "m-Q4_K_M.gguf")
    big = entry_for(hf, "acme/gguf", GGUF_REV, ["big-00001-of-00002.gguf", "big-00002-of-00002.gguf"],
                    "big-00001-of-00002.gguf")
    cache.ensure_model(q4)
    cache.ensure_model(big)
    base = tmp_path / "c" / "hub" / "models--acme--gguf"
    key = f"hub/models--acme--gguf@{GGUF_REV}"
    assert cache.remove(f"{key}/big-00001-of-00002.gguf") == 3004
    snap = base / "snapshots" / GGUF_REV
    assert not (snap / "big-00001-of-00002.gguf").exists() and not (snap / "big-00002-of-00002.gguf").exists()
    assert cache.is_cached(q4) and not cache.is_cached(big)
    assert sorted(f["path"] for f in json.loads((base / ".forgeyard" / f"{GGUF_REV}.json").read_text())["files"]) == [
        "m-Q4_K_M.gguf", "mmproj-m-f16.gguf"]
    assert [p["file"] for p in by_key(cache.info(), key)["parts"]] == ["m-Q4_K_M.gguf"]
    assert cache.remove(f"{key}/m-Q4_K_M.gguf") == 2004 + 704  # the projector goes with the last model file
    assert not base.exists() and cache.info()["models"] == []


def test_fy_cache_005_refused_while_a_replica_uses_it(tmp_path, hf):
    """FY-CACHE-005: the supervisor is asked (with what would be deleted) while the lock is held; any user
    refuses, naming it, and nothing is deleted."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    q4 = entry_for(hf, "acme/gguf", GGUF_REV, ["m-Q4_K_M.gguf", "mmproj-m-f16.gguf"], "m-Q4_K_M.gguf")
    big = entry_for(hf, "acme/gguf", GGUF_REV, ["big-00001-of-00002.gguf", "big-00002-of-00002.gguf"],
                    "big-00001-of-00002.gguf")
    cache.ensure_model(q4)
    cache.ensure_model(big)
    asked = []

    def in_use(target):
        asked.append(target)
        return ["qwen-chat"] if cache_keys.model_uses(q4, target) else []

    key = f"hub/models--acme--gguf@{GGUF_REV}"
    with pytest.raises(CacheRefused) as e:
        cache.remove(f"{key}/m-Q4_K_M.gguf", in_use)
    assert e.value.code == "in_use" and e.value.users == ["qwen-chat"] and "qwen-chat" in str(e.value)
    assert asked[-1] == {"kind": "hf", "dir": "hub/models--acme--gguf", "revision": GGUF_REV, "paths": {"m-Q4_K_M.gguf"}}
    with pytest.raises(CacheRefused):
        cache.remove(key, in_use)  # the whole revision includes the file in use
    assert asked[-1]["paths"] is None
    assert cache.is_cached(q4) and cache.is_cached(big)
    assert cache.remove(f"{key}/big-00001-of-00002.gguf", in_use) == 3004  # the other file is not in use
    assert cache.is_cached(q4)


def test_fy_cache_006_never_while_a_download_of_those_files_runs(tmp_path, hf):
    """FY-CACHE-006: a download holds its repository's lock and the partial-downloads lock (shared); a
    removal of the same repository, or of the partial downloads, is refused at once instead of
    deleting what the download is writing or is about to link. Other repositories are not blocked."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    cache.ensure_model(entry_for(hf, "acme/chat", REV_A, ["config.json", "README.md"]))
    cache.ensure_model(entry_for(hf, "acme/gguf", GGUF_REV, ["m-Q4_K_M.gguf"], "m-Q4_K_M.gguf"))
    hf.rate = 50  # slow (3 s): the download of revision B stays in progress
    b = entry_for(hf, "acme/chat", REV_B, ["config.json", "model.safetensors"])
    stop = threading.Event()
    t = threading.Thread(target=lambda: _quiet(cache.ensure_model, b, cancel=stop))
    t.start()
    try:
        wait_until(lambda: list((tmp_path / "c" / ".partial").glob("*.part")), "the download to start")
        with pytest.raises(CacheRefused) as e:
            cache.remove(f"hub/models--acme--chat@{REV_A}")
        assert e.value.code == "busy" and "in progress" in str(e.value)
        with pytest.raises(CacheRefused) as e:
            cache.prune_partial()
        assert e.value.code == "busy"
        assert cache.remove(f"hub/models--acme--gguf@{GGUF_REV}") > 0  # another repository: not blocked
        assert cache.is_cached(entry_for(hf, "acme/chat", REV_A, ["config.json", "README.md"]))
    finally:
        stop.set()
        t.join(10)
    assert cache.remove(f"hub/models--acme--chat@{REV_A}") > 0  # once the download is over
    assert cache.prune_partial() > 0  # the cancelled download's partial file


def _quiet(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except am.CacheError:
        pass


def test_fy_cache_007_keys_are_looked_up_never_used_as_paths(tmp_path, hf):
    """FY-CACHE-007: a key is only matched against what the agent found on its own disk: traversal, odd
    keys and keys of things not there are refused as not found, and a link in a snapshot that points
    outside the repository's blobs is unlinked without its target being touched."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    cache.ensure_model(entry_for(hf, "acme/chat", REV_A, ["config.json", "README.md"]))
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (tmp_path / "c" / "url" / ("f" * 24)).mkdir()
    for key in (f"hub/models--acme--chat@{REV_A}/../../../../victim.txt", f"hub/models--acme--chat@{REV_A}/x.gguf",
                f"hub/models--acme--chat@{REV_B}", "hub/models--acme--chat", "url/../../victim.txt", f"url/{'f' * 24}",
                "/etc/passwd", "engine/../../x", f"engine/{'0' * 16}", "hub/models--..--x@" + "a" * 40, "", None):
        with pytest.raises(CacheRefused) as e:
            cache.remove(key)
        assert e.value.code == "not_found", key
    assert victim.read_text() == "keep me" and (tmp_path / "c" / "url" / ("f" * 24)).is_dir()
    snap = tmp_path / "c" / "hub" / "models--acme--chat" / "snapshots" / REV_A
    os.chmod(snap, 0o755)
    os.symlink(victim, snap / "planted.txt")
    cache.remove(f"hub/models--acme--chat@{REV_A}")
    assert victim.read_text() == "keep me" and not snap.exists()
    # A directory of a snapshot that is a link to somewhere else: its files are not the cache's.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "m.gguf").write_bytes(b"not yours")
    cache.ensure_model(entry_for(hf, "acme/gguf", GGUF_REV, ["m-Q4_K_M.gguf"], "m-Q4_K_M.gguf"))
    base = tmp_path / "c" / "hub" / "models--acme--gguf"
    os.symlink(outside, base / "snapshots" / GGUF_REV / "evil")
    marker = base / ".forgeyard" / f"{GGUF_REV}.json"
    data = json.loads(marker.read_text())
    data["files"].append({"path": "evil/m.gguf", "size": 9})
    marker.write_text(json.dumps(data))
    assert cache.remove(f"hub/models--acme--gguf@{GGUF_REV}/evil/m.gguf") == 0
    assert (outside / "m.gguf").read_bytes() == b"not yours"


def test_fy_cache_008_partial_downloads_are_listed_and_removed(tmp_path, hf):
    """FY-CACHE-008: a cancelled download's partial file and a half-unpacked engine are counted, and
    removed; finished models are untouched."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    done = entry_for(hf, "acme/chat", REV_A, ["config.json", "README.md"])
    cache.ensure_model(done)
    hf.stall_after = 1000
    stop = threading.Event()
    t = threading.Thread(target=lambda: _quiet(cache.ensure_model, entry_for(hf, "acme/chat", REV_B, ["model.safetensors"]),
                                               cancel=stop))
    t.start()
    assert hf.stalled.wait(10)
    stop.set()
    t.join(10)
    [part] = (tmp_path / "c" / ".partial").glob("*.part")
    left = part.stat().st_size  # what had arrived (written out when the download closed it)
    tmp = tmp_path / "c" / "engines" / ".tmp-0123456789abcdef-77"
    (tmp / "bin").mkdir(parents=True)
    (tmp / "bin" / "x").write_bytes(b"z" * 200)
    info = cache.info()
    assert info["partial_files"] == 2 and info["partial_bytes"] == left + 200 and left <= 1000
    assert cache.prune_partial() == left + 200
    assert not tmp.exists() and list((tmp_path / "c" / ".partial").iterdir()) == []
    assert cache.info()["partial_bytes"] == 0 and cache.is_cached(done)


def test_fy_cache_009_the_listing_fits_its_share_of_a_message(tmp_path, monkeypatch):
    """FY-CACHE-009: hundreds of models: the biggest are kept, the listing stays within its budget and
    says it was cut short."""
    cache = ModelCache(tmp_path / "c")
    hub = tmp_path / "c" / "hub"
    for i in range(300):
        base = hub / f"models--org--model-{i:03d}"
        snap = base / "snapshots" / REV_A
        snap.mkdir(parents=True)
        (base / ".forgeyard").mkdir()
        files = [{"path": f"weights-{j:02d}-{'q' * 60}.gguf", "size": 10} for j in range(80)]
        for f in files:
            (snap / f["path"]).write_bytes(b"x" * (i + 1))
        (base / ".forgeyard" / f"{REV_A}.json").write_text(json.dumps({"repo": f"org/model-{i:03d}", "revision": REV_A,
                                                                        "files": files}))
    info = cache.info()
    assert len(json.dumps({k: info[k] for k in ("models", "engines_detail", "used_bytes", "partial_bytes",
                                                 "partial_files", "truncated")})) <= am.DETAIL_BUDGET
    assert info["truncated"] is True and 0 < len(info["models"]) < 256
    assert info["models"][0]["repo"] == "org/model-299"  # the biggest first
    assert all(len(e["parts"]) <= am.MAX_PARTS for e in info["models"])
    monkeypatch.setattr(am, "DETAIL_BUDGET", 10**9)
    assert len(cache.info()["models"]) == am.MAX_ENTRIES


def test_fy_cache_010_url_models_and_engines(tmp_path, hf):
    """FY-CACHE-010: a URL model's directory and an engine's directory are removed whole; an engine a
    replica uses is refused."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    url = tmp_path / "c" / "url" / ("d" * 24)
    url.mkdir()
    (url / "tiny.gguf").write_bytes(b"GGUF" + bytes(96))
    (url / ".forgeyard.json").write_text(json.dumps({"url": "https://x/tiny.gguf", "file": "tiny.gguf"}))
    eng = tmp_path / "c" / "engines" / f"llama-cpp-{'e' * 16}"
    (eng / "bin").mkdir(parents=True)
    (eng / "bin" / "llama-server").write_bytes(b"E" * 512)
    used = lambda t: ["chat"] if cache_keys.engine_uses({"sha256": "e" * 64}, t) else []  # noqa: E731
    with pytest.raises(CacheRefused) as e:
        cache.remove(f"engine/{'e' * 16}", used)
    assert e.value.code == "in_use" and eng.is_dir()
    assert cache.remove(f"engine/{'e' * 16}") == 512 and not eng.exists() and cache.cached_engines() == []
    on_disk = tree_bytes(url)  # the file and the marker
    assert cache.remove(f"url/{'d' * 24}") == on_disk and on_disk > 100 and not url.exists()
    assert cache.info()["models"] == []


# ------------------------------------------------------------------ the supervisor
def _supervisor(tmp_path, cache):
    rt = ProcessRuntime(RecordStore(tmp_path / "state"), tmp_path / "work", euid=lambda: 4242)
    return Supervisor(tmp_path / "state", {"process": rt}, models=cache, tick=0.05, bind_address="127.0.0.1",
                      port_range=(31600, 31699))


def test_fy_cache_011_the_supervisor_names_the_replicas_that_use_it(tmp_path, hf):
    """FY-CACHE-011: every replica the agent has not finished with, and every one the desired list wants,
    counts; a finished one does not. The answer carries the cache as it is afterwards."""
    cache = ModelCache(tmp_path / "c", allow_private=True)
    q4 = entry_for(hf, "acme/gguf", GGUF_REV, ["m-Q4_K_M.gguf"], "m-Q4_K_M.gguf")
    cache.ensure_model(q4)
    sup = _supervisor(tmp_path, cache)
    rec = Record(id="r1", generation=1, workload="qwen-chat", ordinal=0, kind="service", runtime="process", spec={},
                 state="backoff", model=q4)
    sup.records["r1"] = rec
    key = f"hub/models--acme--gguf@{GGUF_REV}"
    answer = asyncio.run(sup.cache_remove(key))
    assert answer["ok"] is False and answer["code"] == "in_use" and answer["in_use"] == ["qwen-chat"]
    assert by_key(answer["model_cache"], key)["bytes"] == 2004
    rec.final, rec.state = True, "failed"
    sup.desired = {"r2": {"id": "r2", "workload": "other-chat", "generation": 1,
                          "model": {"kind": "hf", "repo": "acme/gguf", "revision": GGUF_REV,
                                    "files": [{"path": "m-Q4_K_M.gguf"}], "file": "m-Q4_K_M.gguf"}}}
    assert asyncio.run(sup.cache_remove(key))["in_use"] == ["other-chat"]
    sup.desired = {}
    answer = asyncio.run(sup.cache_remove(key))
    assert answer["ok"] is True and answer["freed_bytes"] == 2004 and answer["model_cache"]["models"] == []
    assert asyncio.run(sup.cache_remove(key))["code"] == "not_found"
    answer = asyncio.run(sup.cache_remove(None, partial=True))
    assert answer["ok"] is True and answer["freed_bytes"] == 0
    assert asyncio.run(_supervisor(tmp_path / "x", None).cache_remove(key))["code"] == "no_cache"


def test_fy_cache_012_targets_and_keys():
    """FY-CACHE-012: what a key names, and which model blocks a target touches (both sides use it)."""
    rev = "1" * 40
    assert cache_keys.parse(f"hub/models--a--b@{rev}") == {"kind": "hf", "dir": "hub/models--a--b", "revision": rev,
                                                           "paths": None}
    assert cache_keys.parse(f"hub/models--a--b@{rev}/sub/x-00001-of-00002.gguf")["file"] == "sub/x-00001-of-00002.gguf"
    for bad in (f"hub/models--a--b@{rev}/../x.gguf", f"hub/models--a--b@{rev}/x.safetensors", f"hub/models--a--b@{rev[:-1]}",
                "url/abc", f"url/{'0' * 24}/x", "engine/xyz", "hub/models--a/b@" + rev, "x" * 900, 5, None):
        assert cache_keys.parse(bad) is None, bad
    block = {"kind": "hf", "repo": "a/b", "revision": rev, "files": [{"path": "x-00002-of-00002.gguf"}],
             "file": "x-00001-of-00002.gguf"}
    t = cache_keys.parse(f"hub/models--a--b@{rev}/x-00001-of-00002.gguf")
    assert cache_keys.model_uses(block, t)
    assert not cache_keys.model_uses(block, cache_keys.parse(f"hub/models--a--b@{rev}/y.gguf"))
    assert cache_keys.model_uses(block, {**t, "paths": {"x-00002-of-00002.gguf"}})
    assert cache_keys.model_uses(block, cache_keys.parse(f"hub/models--a--b@{rev}"))
    assert not cache_keys.model_uses(block, cache_keys.parse(f"hub/models--a--b@{'2' * 40}"))
    assert not cache_keys.model_uses(block, cache_keys.parse(f"hub/models--a--c@{rev}"))
    url = {"kind": "url", "url": "https://h/x.gguf"}
    assert cache_keys.model_uses(url, cache_keys.parse(sources.url_dir("https://h/x.gguf")))
    assert not cache_keys.model_uses(url, cache_keys.parse(f"url/{'0' * 24}"))
    assert not cache_keys.model_uses({"kind": "local", "path": "/srv/m"}, t) and not cache_keys.model_uses(None, t)
    assert cache_keys.engine_uses({"sha256": "ab" * 32}, cache_keys.parse("engine/" + "ab" * 8))
    assert not cache_keys.engine_uses({"sha256": "cd" * 32}, cache_keys.parse("engine/" + "ab" * 8))


def test_fy_cache_013_an_engine_being_downloaded_is_not_removed(tmp_path):
    """FY-CACHE-013: an engine archive's download holds its directory's lock: removing that engine meanwhile
    is refused as busy (not "not found"), and works once it is unpacked."""
    import io
    import tarfile

    payload = os.urandom(3000)  # does not compress: the slow download takes a few seconds
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("eng/run")
        info.size, info.mode = len(payload), 0o755
        tar.addfile(info, io.BytesIO(payload))
    tgz = buf.getvalue()
    server = FakeHF(Repo("engines/fake", "e" * 40, {"fake.tgz": tgz}), rate=50)
    try:
        cache = ModelCache(tmp_path / "c", allow_private=True)
        sha = hashlib.sha256(tgz).hexdigest()
        engine = {"name": "fake", "url": f"{server.url}/engines/fake/resolve/{'e' * 40}/fake.tgz", "sha256": sha,
                  "binary": "eng/run"}
        stop = threading.Event()
        t = threading.Thread(target=lambda: _quiet(cache.ensure_engine, engine, cancel=stop))
        t.start()
        try:
            wait_until(lambda: list((tmp_path / "c" / ".partial").glob("engine-*.part")), "the engine download to start")
            with pytest.raises(CacheRefused) as e:
                cache.remove(f"engine/{sha[:16]}")
            assert e.value.code == "busy"
        finally:
            t.join(30)
        assert (tmp_path / "c" / "engines" / f"fake-{sha[:16]}" / "eng" / "run").is_file()
        assert cache.remove(f"engine/{sha[:16]}") == len(payload)
    finally:
        stop.set()
        server.close()
