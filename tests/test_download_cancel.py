"""Model downloads found on real machines (FY-DLC-*): progress that went over 100% on a resume
("14.54 of 12.93 GiB (112%)" while 11.66 GiB were on disk), and a removed deployment whose download
went on writing and held the model's lock, so a new deployment of the same model waited."""

import asyncio
import os
import threading
import time

import pytest

from forgeyard.agent.models import Cancelled, ModelCache, validate_model
from forgeyard.agent.workloads.records import Record
from forgeyard.agent.workloads.supervisor import Supervisor
from forgeyard.models.sources import cache_key
from tests.model_helpers import FakeHF, Repo
from tests.test_model_supervisor import A100, _ctr, container_spec, hf_model, model_replica
from tests.workload_helpers import converge, desired

REV = "b" * 40


def _model(hf, repo):
    return validate_model({"kind": "hf", "repo": repo.id, "revision": repo.sha, "endpoint": hf.url,
                           "files": [{"path": s["rfilename"], "size": s["size"],
                                      "sha256": (s.get("lfs") or {}).get("sha256"), "oid": s["blobId"]}
                                     for s in repo.info["siblings"]]}, [])


def _parts(cache_dir):
    return {p.name: p.stat().st_size for p in (cache_dir / ".partial").glob("*.part")}


def test_fy_dlc_001_a_resumed_download_counts_each_byte_once(tmp_path):
    """FY-DLC-001: the real case. A model of three files is resumed with the first file complete and
    the second half there. Before, the complete file was counted twice (once in the starting total,
    again in the loop), so progress went past 100%. Now every report is the bytes of that model on
    disk, never above the total, and it ends at exactly the total."""
    files = {"a.safetensors": os.urandom(150_000), "b.safetensors": os.urandom(200_000), "config.json": b"{}"}
    repo = Repo("acme/three", REV, files)
    hf = FakeHF(repo)
    try:
        cache = ModelCache(tmp_path / "cache", allow_private=True)
        model = _model(hf, repo)
        total = sum(len(v) for v in files.values())
        # The first download: cut in the middle of b, after a is complete.
        cancel = threading.Event()

        def stop_in_b(phase, done, tot, f):
            if done > 150_000 + 50_000:
                cancel.set()

        hf.rate = 8192
        with pytest.raises(Cancelled):
            cache.ensure_model(model, progress=stop_in_b, cancel=cancel)
        on_disk = sum(_parts(tmp_path / "cache").values()) + 150_000
        assert on_disk < total
        seen = []
        cache.ensure_model(model, progress=lambda phase, done, tot, f: seen.append((done, tot)))
    finally:
        hf.close()
    assert seen[0][0] == on_disk  # where it resumes: what is on disk, not more
    assert all(done <= tot == total for done, tot in seen)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen) and seen[-1] == (total, total)


def test_fy_dlc_002_the_cancel_is_set_when_the_desired_list_arrives(tmp_path):
    """FY-DLC-002: the cancel no longer waits for a reconcile round. A round first looks at every
    running replica (one `docker inspect` each: seconds on a busy machine), and only then cancelled
    downloads; meanwhile the download kept writing and held the lock. Now set_desired cancels at once,
    says why, and the download stops within a moment."""
    big = Repo("acme/big", REV, {"w.safetensors": bytes(3_000_000), "config.json": b"{}"})
    hf = FakeHF(big, rate=16384)
    try:
        sup, fake = _ctr(tmp_path, hf)
        m = hf_model(hf, "acme/big", REV)

        async def scenario():
            sup.set_desired(desired(model_replica("c1", container_spec(), gpus=[A100[4]], model=m)))
            for _ in range(400):
                await sup.reconcile_once()
                await asyncio.sleep(0.02)
                rec = sup.records.get("c1")
                if rec and rec.download and rec.download["done"] > 100_000:
                    break
            event = sup.cancels["c1"]
            key = cache_key(sup.records["c1"].model)
            sup.set_desired(desired(revision=5))  # no reconcile_once from here on until it has stopped
            assert event.is_set() and sup.cancelled["c1"] == "cancelled: the deployment was removed"
            t0 = time.monotonic()
            while sup.models.busy(key) and time.monotonic() - t0 < 5:
                await asyncio.sleep(0.02)
            took = time.monotonic() - t0
            sizes = _parts(tmp_path / "cache")
            await asyncio.sleep(0.5)
            assert _parts(tmp_path / "cache") == sizes  # the .part stopped growing
            assert took < 1.5 and not sup.models.busy(key)
            assert sup.records["c1"].reason == "cancelled: the deployment was removed"
            await converge(sup, lambda s: not s.records, timeout=10)
            return sup.take_report()

        msg = asyncio.run(scenario())
        [gone] = [e for e in msg["replicas"] if e["id"] == "c1"]
        assert gone["state"] == "stopped" and gone["reason"] == "cancelled: the deployment was removed"
    finally:
        hf.close()


def test_fy_dlc_003_a_cancel_ends_a_read_on_a_stalled_connection(tmp_path):
    """FY-DLC-003: the CDN stops sending in the middle of a file (seen: "The read operation timed
    out"). The read used to wait for the socket timeout (60 s) before the cancel was looked at, holding
    the lock. Now the cancel ends the read at once, and the lock is free."""
    repo = Repo("acme/stall", REV, {"w.safetensors": os.urandom(300_000), "config.json": b"{}"})
    hf = FakeHF(repo)
    hf.stall_after = 40_000
    try:
        cache = ModelCache(tmp_path / "cache", allow_private=True)
        model = _model(hf, repo)
        cancel = threading.Event()
        threading.Thread(target=lambda: (hf.stalled.wait(10), time.sleep(0.2), cancel.set()), daemon=True).start()
        t0 = time.monotonic()
        with pytest.raises(Cancelled):
            cache.ensure_model(model, cancel=cancel)
        assert time.monotonic() - t0 < 3
        assert not cache.busy(cache_key(model)) and _parts(tmp_path / "cache")
    finally:
        hf.close()


def test_fy_dlc_004_a_cancel_ends_a_retry_pause_and_a_checksum(tmp_path, monkeypatch):
    """FY-DLC-004: a failed read is retried after a pause of up to 30 s, and a finished file of many
    GB takes seconds to hash: neither looked at the cancel. Now both end at once."""
    repo = Repo("acme/retry", REV, {"w.safetensors": os.urandom(100_000), "config.json": b"{}"})
    hf = FakeHF(repo)
    hf.fail_after = 10_000
    try:
        cache = ModelCache(tmp_path / "cache", allow_private=True)
        model = _model(hf, repo)
        cancel = threading.Event()
        pauses = []
        real_wait = cancel.wait

        def wait(seconds):
            if seconds >= 1:  # the retry's pause (the watchdog polls the same event briefly)
                pauses.append(seconds)
                threading.Timer(0.1, cancel.set).start()
            return real_wait(seconds)

        cancel.wait = wait
        t0 = time.monotonic()
        with pytest.raises(Cancelled):
            cache.ensure_model(model, cancel=cancel)
        assert pauses == [2] and time.monotonic() - t0 < 1.5
    finally:
        hf.close()
    part = tmp_path / "big.bin"
    part.write_bytes(bytes(8 * 1024 * 1024))
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        ModelCache._sha256(part, stop)
    from forgeyard.agent.models import git_blob_id
    with pytest.raises(Cancelled):
        git_blob_id(part, stop)


def test_fy_dlc_005_a_replica_waiting_for_the_lock_can_be_cancelled(tmp_path):
    """FY-DLC-005: a replica queued behind another one's download of the same model could not stop
    waiting (a plain lock). Now its cancel ends the wait; the download that holds the lock goes on."""
    repo = Repo("acme/shared", REV, {"w.safetensors": os.urandom(400_000), "config.json": b"{}"})
    hf = FakeHF(repo, rate=8192)
    try:
        cache = ModelCache(tmp_path / "cache", allow_private=True)
        model = _model(hf, repo)
        first_cancel, second_cancel = threading.Event(), threading.Event()
        first = threading.Thread(target=lambda: _quiet(cache.ensure_model, model, cancel=first_cancel))
        first.start()
        for _ in range(200):
            if cache.busy(cache_key(model)):
                break
            time.sleep(0.02)
        threading.Timer(0.3, second_cancel.set).start()
        t0 = time.monotonic()
        with pytest.raises(Cancelled):
            cache.ensure_model(model, cancel=second_cancel)
        assert time.monotonic() - t0 < 1.5 and cache.busy(cache_key(model))
        first_cancel.set()
        first.join(5)
        assert not first.is_alive() and not cache.busy(cache_key(model))
    finally:
        hf.close()


def _quiet(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Cancelled:
        pass


def test_fy_dlc_006_an_agent_that_stops_cancels_its_downloads(tmp_path):
    """FY-DLC-006: when the agent stops, the asyncio task of a download is cancelled, but its thread
    went on (its cancel switch was dropped with the task) and kept the lock until the download ended.
    close() now cancels the downloads first."""
    big = Repo("acme/big", REV, {"w.safetensors": bytes(3_000_000), "config.json": b"{}"})
    hf = FakeHF(big, rate=16384)
    try:
        sup, fake = _ctr(tmp_path, hf)
        m = hf_model(hf, "acme/big", REV)

        async def scenario():
            sup.set_desired(desired(model_replica("c1", container_spec(), gpus=[A100[4]], model=m)))
            for _ in range(400):
                await sup.reconcile_once()
                await asyncio.sleep(0.02)
                rec = sup.records.get("c1")
                if rec and rec.download and rec.download["done"] > 50_000:
                    break
            key = cache_key(sup.records["c1"].model)
            event = sup.cancels["c1"]
            await sup.close()
            assert event.is_set()
            t0 = time.monotonic()
            while sup.models.busy(key) and time.monotonic() - t0 < 5:
                await asyncio.sleep(0.02)
            assert not sup.models.busy(key)

        asyncio.run(scenario())
    finally:
        hf.close()


def test_fy_dlc_007_reported_progress_never_passes_the_total(tmp_path):
    """FY-DLC-007: the safety net: whatever the cache reports, the replica says at most 100% (and the
    server clamps it again, FY-PULL-017)."""
    sup = Supervisor(tmp_path / "state", {}, tick=0.05)
    rec = Record(id="r1", generation=1, workload="w", ordinal=0, kind="service", runtime="container", spec={})
    sup.records["r1"] = rec
    sup.cancels["r1"] = threading.Event()
    gib = 1024**3
    sup._progress("r1", "model", int(14.54 * gib), int(12.93 * gib), "unet.safetensors")
    assert rec.download["done"] == rec.download["total"] == int(12.93 * gib)
    assert rec.reason == "downloading the model: 12.93 of 12.93 GiB (100%)"
    sup.cancels.pop("r1")
    sup._progress("r1", "model", 1, 2, None)  # a late report after the download ended: ignored
    assert rec.download["done"] == int(12.93 * gib)
