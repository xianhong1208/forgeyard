"""The agent's model cache: downloads, verification, resume, layout, read-only files, tokens,
SSRF, disk space, cancellation and engines (FY-MC-*)."""

import hashlib
import io
import os
import stat
import tarfile
import threading
from collections import namedtuple

import pytest

from forgeyard.agent.models import CacheError, Cancelled, ModelCache, validate_model
from forgeyard.models import fetch, inspect
from tests.model_helpers import TINY_REV, FakeHF, Repo, tiny_repo

LOCAL = fetch.Policy(allow_private=True)


@pytest.fixture()
def hf():
    server = FakeHF(tiny_repo(), tiny_repo("acme/gated", gated=True))
    yield server
    server.close()


def block(hf, repo="acme/tiny-chat", token=None):
    meta = inspect.inspect_hf(repo, "main", None, endpoint=hf.url, policy=LOCAL, token=token)
    return {"kind": "hf", "repo": repo, "revision": meta["revision"], "ref": "main", "endpoint": hf.url,
            "files": meta["files"], "file": None}


def test_fy_mc_001_download_layout_and_read_only(tmp_path, hf):
    """FY-MC-001: the Hugging Face cache layout (blobs, relative symlinks, refs/main), verified
    files mode 0444, progress up to the total, reported as cached, never fetched twice."""
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    model = validate_model(block(hf), [])
    seen = []
    paths = cache.ensure_model(model, progress=lambda *a: seen.append(a))
    base = tmp_path / "cache" / "hub" / "models--acme--tiny-chat"
    snap = base / "snapshots" / TINY_REV
    assert paths["path"] == str(snap) and paths["mount"] == str(base)
    assert (base / "refs" / "main").read_text() == TINY_REV
    for name in ("config.json", "model.safetensors", "README.md", "tokenizer.json", "sub/dir/extra.txt"):
        link = snap / name
        assert link.is_symlink() and not os.readlink(link).startswith("/")
        assert stat.S_IMODE(link.resolve().stat().st_mode) == 0o444
        assert link.resolve().parent == base / "blobs"
    assert not (snap / "pytorch_model.bin").exists()  # other weight formats are not fetched
    assert (snap / "sub/dir/extra.txt").read_bytes() == b"nested"
    total = sum(f["size"] for f in model["files"])
    assert seen[-1][:3] == ("model", total, total)
    assert cache.cached_keys() == [f"hub/models--acme--tiny-chat@{TINY_REV}"] and cache.is_cached(model)
    n = len(hf.requests)
    cache.ensure_model(model)
    assert len(hf.requests) == n
    info = cache.info()
    assert info["ok"] and info["cached"] == cache.cached_keys() and info["free_bytes"] > 0


def test_fy_mc_002_resume_after_a_cut_and_verify(tmp_path, hf):
    """FY-MC-002: a cut connection resumes with a Range request; a file that does not match its
    SHA-256 is thrown away and nothing broken is cached."""
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    model = validate_model(block(hf), [])
    start = len(hf.requests)
    hf.fail_after = 100
    import forgeyard.agent.models as am
    orig = am.time.sleep
    am.time.sleep = lambda s: None
    try:
        cache.ensure_model(model)
    finally:
        am.time.sleep = orig
    resumed = [r for r in hf.requests[start:] if r["range"] and "/cdn/" in r["path"]]
    assert len(resumed) == 1 and resumed[0]["range"] == "bytes=100-" and resumed[0]["path"].endswith("/config.json")
    bad = dict(model, files=[dict(f) for f in model["files"]])
    bad["revision"] = "f" * 40
    bad["files"][2]["sha256"] = "0" * 64  # model.safetensors
    hf.repos["acme/tiny-chat"].sha = "f" * 40
    with pytest.raises(CacheError, match="SHA-256 mismatch"):
        cache.ensure_model(validate_model(bad, []))
    assert not cache.is_cached(validate_model(bad, []))
    assert not (tmp_path / "cache" / "hub" / "models--acme--tiny-chat" / "blobs" / ("0" * 64)).exists()


def test_fy_mc_003_gated_models_need_the_token_which_never_touches_disk(tmp_path, hf):
    """FY-MC-003"""
    cache = ModelCache(tmp_path / "cache", allow_private=True)
    model = validate_model(block(hf, "acme/gated", token=FakeHF.TOKEN), [])
    with pytest.raises(CacheError, match="needs a Hugging Face token"):
        cache.ensure_model(model)
    cache.ensure_model(model, token=FakeHF.TOKEN)
    assert any(r["auth"] == f"Bearer {FakeHF.TOKEN}" for r in hf.requests if "/resolve/" in r["path"])
    for root, _, files in os.walk(tmp_path):
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                assert FakeHF.TOKEN.encode() not in open(p, "rb").read(), p


def test_fy_mc_004_private_hosts_need_an_explicit_switch(tmp_path, hf):
    """FY-MC-004: without --allow-private-model-hosts a loopback (or plain http) source is refused
    before any request."""
    cache = ModelCache(tmp_path / "cache")
    model = validate_model(block(hf), [])
    n = len(hf.requests)
    with pytest.raises(CacheError, match="http|private|loopback"):
        cache.ensure_model(model)
    assert len(hf.requests) == n


def test_fy_mc_005_disk_space_is_checked_first(tmp_path, hf):
    """FY-MC-005"""
    Usage = namedtuple("Usage", "total used free")
    cache = ModelCache(tmp_path / "cache", allow_private=True, disk_usage=lambda p: Usage(10**12, 0, 10**6))
    model = validate_model(block(hf), [])
    n = len(hf.requests)
    with pytest.raises(CacheError, match="not enough disk space"):
        cache.ensure_model(model)
    assert len(hf.requests) == n


def test_fy_mc_006_a_download_can_be_cancelled(tmp_path):
    """FY-MC-006: cancelled mid-file; the part stays for a later resume, nothing is cached."""
    big = Repo("acme/big", "b" * 40, {"weights.gguf": os.urandom(400_000), "config.json": b"{}"})
    hf = FakeHF(big, rate=4096)
    try:
        cache = ModelCache(tmp_path / "cache", allow_private=True)
        model = validate_model({"kind": "hf", "repo": "acme/big", "revision": "b" * 40, "endpoint": hf.url,
                                "files": [{"path": s["rfilename"], "size": s["size"], "sha256": (s.get("lfs") or {}).get("sha256"),
                                           "oid": s["blobId"]} for s in big.info["siblings"]]}, [])
        cancel = threading.Event()

        def progress(phase, done, total, f):
            if done > 20_000:
                cancel.set()

        with pytest.raises(Cancelled):
            cache.ensure_model(model, progress=progress, cancel=cancel)
        assert not cache.is_cached(model) and list((tmp_path / "cache" / ".partial").glob("*.part"))
    finally:
        hf.close()


def test_fy_mc_007_the_server_cannot_point_the_agent_anywhere(tmp_path):
    """FY-MC-007: paths are recomputed by the agent; traversal, unpinned revisions and local
    directories outside --model-local-dir are refused."""
    good = {"kind": "hf", "repo": "acme/x", "revision": "a" * 40, "files": [{"path": "config.json", "size": 2}]}
    assert validate_model(good, [])["repo"] == "acme/x"
    assert "token" not in validate_model(dict(good, token="hf_x"), [])  # what is kept (and saved) never holds it
    for bad in [dict(good, repo="../../etc"), dict(good, revision="main"),
                dict(good, files=[{"path": "../../../root/.ssh/authorized_keys", "size": 1}]),
                dict(good, files=[{"path": "/etc/passwd", "size": 1}]), dict(good, files=[]),
                {"kind": "local", "path": "/etc"}, {"kind": "nfs", "path": "/x"},
                {"kind": "url", "url": "file:///etc/passwd"}]:
        with pytest.raises((CacheError, ValueError)):
            validate_model(bad, ["/srv/models"])
    (tmp_path / "models" / "q").mkdir(parents=True)
    (tmp_path / "models" / "q" / "q.gguf").write_bytes(b"GGUF")
    local = validate_model({"kind": "local", "path": str(tmp_path / "models" / "q"), "file": "q.gguf"},
                           [str(tmp_path / "models")])
    cache = ModelCache(tmp_path / "cache")
    assert cache.ensure_model(local)["file"] == str(tmp_path / "models" / "q" / "q.gguf")


def _tarball(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, mode, link in members:
            info = tarfile.TarInfo(name)
            if link:
                info.type, info.linkname = tarfile.SYMTYPE, link
                tar.addfile(info)
            else:
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_fy_mc_008_engines_are_pinned_and_unpacked_safely(tmp_path):
    """FY-MC-008: an engine archive is checked against its SHA-256, unpacked without escaping its
    directory, made read-only, and its program found."""
    good = _tarball([("e/bin/server", b"#!/bin/sh\necho ok\n", 0o755, None), ("e/lib.so", b"x", 0o644, None),
                     ("e/lib.so.1", b"", 0o777, "lib.so")])
    evil = _tarball([("../escape", b"x", 0o644, None)])
    repo = Repo("engines/llama", "e" * 40, {"good.tgz": good, "evil.tgz": evil})
    hf = FakeHF(repo, redirect=False)
    try:
        cache = ModelCache(tmp_path / "cache", allow_private=True)
        url = f"{hf.url}/engines/llama/resolve/{'e' * 40}/"
        sha = hashlib.sha256(good).hexdigest()
        exe = cache.ensure_engine({"name": "llama.cpp-b1", "url": url + "good.tgz", "sha256": sha, "binary": "e/bin/server"})
        assert exe.name == "server" and os.access(exe, os.X_OK)
        assert stat.S_IMODE(exe.stat().st_mode) == 0o555 and stat.S_IMODE((exe.parent.parent / "lib.so").stat().st_mode) == 0o444
        assert cache.ensure_engine({"name": "llama.cpp-b1", "url": url + "good.tgz", "sha256": sha,
                                    "binary": "e/bin/server"}) == exe
        with pytest.raises(CacheError, match="SHA-256 mismatch"):
            cache.ensure_engine({"name": "x", "url": url + "good.tgz", "sha256": "0" * 64, "binary": "e/bin/server"})
        with pytest.raises(CacheError, match="unpack"):
            cache.ensure_engine({"name": "x", "url": url + "evil.tgz", "sha256": hashlib.sha256(evil).hexdigest(),
                                 "binary": "escape"})
        assert not (tmp_path / "cache" / "engines" / "escape").exists() and not (tmp_path / "escape").exists()
        with pytest.raises(CacheError, match="no program"):
            cache.ensure_engine({"name": "y", "url": url + "good.tgz", "sha256": sha, "binary": "e/bin/missing"})
        with pytest.raises(CacheError, match="https"):
            ModelCache(tmp_path / "c2").ensure_engine({"name": "z", "url": url + "good.tgz", "sha256": sha, "binary": "e"})
    finally:
        hf.close()


def test_fy_mc_009_two_files_of_one_commit_are_cached_separately(tmp_path):
    """FY-MC-009: a repository with several GGUF files: fetching one does not make the other look
    cached (found by the real run: the second deployment started without its file)."""
    repo = Repo("acme/ggufs", "c" * 40, {"m-Q4_K_M.gguf": b"GGUF" + bytes(100), "m-Q8_0.gguf": b"GGUF" + bytes(200)})
    hf = FakeHF(repo)
    try:
        cache = ModelCache(tmp_path / "cache", allow_private=True)

        def model(file):
            s = next(x for x in repo.info["siblings"] if x["rfilename"] == file)
            return validate_model({"kind": "hf", "repo": "acme/ggufs", "revision": "c" * 40, "endpoint": hf.url, "file": file,
                                   "files": [{"path": file, "size": s["size"], "sha256": s["lfs"]["sha256"]}]}, [])

        q4, q8 = model("m-Q4_K_M.gguf"), model("m-Q8_0.gguf")
        cache.ensure_model(q4)
        assert cache.is_cached(q4) and not cache.is_cached(q8)
        base = f"hub/models--acme--ggufs@{'c' * 40}"
        assert cache.cached_keys() == [base, f"{base}/m-Q4_K_M.gguf"]
        paths = cache.ensure_model(q8)
        assert open(paths["file"], "rb").read(4) == b"GGUF" and cache.is_cached(q4) and cache.is_cached(q8)
        assert cache.cached_keys() == [base, f"{base}/m-Q4_K_M.gguf", f"{base}/m-Q8_0.gguf"]
    finally:
        hf.close()
