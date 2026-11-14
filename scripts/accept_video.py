#!/usr/bin/env python3
"""1.3 acceptance on real machines: video generation at /v1/videos through the gateway, called with the
official OpenAI Python SDK (client.videos.create / retrieve / list / download_content / delete).

Through Forgeyard's API only (an admin session), for each model of --only, on --machine's GPU:

  1. adds the model to the catalog unless it is there (pinned to the commit below), deploys it there as
     `accept-video-<model>` with keep_minutes = --keep-minutes (so the expiry check can run) and waits
     until it is ready; the card's memory is read before, once ready and after the videos, against the
     estimate;
  2. creates an API key and, with the SDK: asks for one video (--seconds at --size, default the model's
     own), polls it until it is completed (timing it, and printing its status as it changes), lists it,
     and downloads it through the gateway: it must be a real MP4 (its boxes read here, no ffmpeg needed:
     ftyp, moov with mvhd, a video track with its size in tkhd and its frames in stts, mdat), of the size
     asked and as long as asked (frames / fps);
  3. shows the refusals: more seconds than the cap, a size over the cap, an image reference
     (image-to-video is not offered), a thumbnail, another key's video (404 for its status, content and
     delete), and the replica's own port without its key;
  4. the per-key cap: with one job of the key at work, two more: the second is 429 too_many_jobs with
     Retry-After; deleting a job at work cancels it (it is gone afterwards);
  5. reads the usage of the run's own key: one request per job, the video's length (media_ms) and the
     replica's time on it (busy_ms);
  6. expiry: waits until the first video's expires_at has passed and the sweeper ran: its content
     answers 410 video_expired (skip with --no-expiry);
  7. a replica that goes takes its jobs: asks for one more video, stops the deployment while it is at
     work, and the job reports failed (replica_gone) (skip with --no-restart);

then removes what it created (deployments, the catalog entries it added, keys) and prints a report (also
JSON with --out; --save-videos keeps the videos). Everything is named accept-video-*, and leftovers of an
earlier run are removed first, so it can be run again.

    FORGEYARD_PASSWORD=… python3 scripts/accept_video.py --server https://<server>:7860 --cacert forgeyard-ca.pem \\
        --user <admin> --machine <machine> --only wan2.1-t2v-1.3b --out accept-video.json --save-videos videos/

Models it knows (--only):
  wan2.1-t2v-1.3b   Wan-AI/Wan2.1-T2V-1.3B-Diffusers: 832x480 at 16 fps (5 s is 81 frames)
  wan2.2-ti2v-5b    Wan-AI/Wan2.2-TI2V-5B-Diffusers: 1280x704 at 24 fps (5 s is 121 frames); about 33 GiB

With Forgeyard's own CA, --cacert is the CA file. The official SDK: as scripts/accept_m4.py, in its own
virtual environment (--venv) when `openai` is not installed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept_m3  # noqa: E402
from accept_m3 import GIB, Client, Fail, log, machine  # noqa: E402
from accept_m4 import ensure_sdk, raw_post, wait_all_ready  # noqa: E402

PREFIX = "accept-video-"
PROMPT = "A red kite flying over a green hill on a sunny day, slow camera pan"
SECOND_PROMPT = "Waves rolling onto a sandy beach at sunset"
MODELS = {
    "wan2.1-t2v-1.3b": {"source": "hf://Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
                        "revision": "0fad780a534b6463e45facd96134c9f345acfa5b", "recipe": "vllm-omni-video",
                        "size": "832x480", "fps": 16, "params": {}},
    "wan2.2-ti2v-5b": {"source": "hf://Wan-AI/Wan2.2-TI2V-5B-Diffusers",
                       "revision": "b8fff7315c768468a5333511427288870b2e9635", "recipe": "vllm-omni-video",
                       "size": "1280x704", "fps": 24, "params": {}},
}


# ------------------------------------------------------------------ MP4, read here
def _boxes(data: bytes, start: int = 0, end: int | None = None):
    """(type, payload start, payload end) of each ISO BMFF box between start and end."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        head = 8
        if size == 1:
            if pos + 16 > end:
                raise ValueError("a truncated 64-bit box size")
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            raise ValueError(f"box {kind!r} at {pos} runs past the end ({size} bytes)")
        yield kind.decode("latin-1"), pos + head, pos + size
        pos += size


def _find(data: bytes, path: list[str], start: int = 0, end: int | None = None):
    for kind, a, b in _boxes(data, start, end):
        if kind == path[0]:
            if len(path) == 1:
                return a, b
            got = _find(data, path[1:], a, b)
            if got:
                return got
    return None


def mp4_info(data: bytes) -> dict:
    """What an MP4 file holds, from its boxes: {brand, duration_s, width, height, frames, fps, codec}.
    Raises ValueError when it is not one (no ftyp first, no moov or mdat, no video track)."""
    top = list(_boxes(data))
    if not top or top[0][0] != "ftyp":
        raise ValueError("not an MP4 file: it does not start with an ftyp box")
    kinds = [k for k, _, _ in top]
    if "moov" not in kinds or "mdat" not in kinds:
        raise ValueError(f"an MP4 file needs moov and mdat; it has {kinds}")
    brand = data[top[0][1]:top[0][1] + 4].decode("latin-1")
    ma, mb = next((a, b) for k, a, b in top if k == "moov")
    mdat = next(b - a for k, a, b in top if k == "mdat")
    mvhd = _find(data, ["mvhd"], ma, mb)
    if not mvhd:
        raise ValueError("no mvhd box")
    a = mvhd[0]
    if data[a] == 1:
        timescale, duration = struct.unpack(">IQ", data[a + 20:a + 32])
    else:
        timescale, duration = struct.unpack(">II", data[a + 12:a + 20])
    out = {"brand": brand, "duration_s": duration / timescale if timescale else 0.0, "mdat_bytes": mdat}
    for kind, ta, tb in _boxes(data, ma, mb):
        if kind != "trak":
            continue
        hdlr = _find(data, ["mdia", "hdlr"], ta, tb)
        if not hdlr or data[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
            continue
        tkhd = _find(data, ["tkhd"], ta, tb)
        t = tkhd[0]
        off = t + (88 if data[t] == 1 else 76)  # after version, flags, times, ids, layer, volume and the matrix
        w, h = struct.unpack(">II", data[off:off + 8])
        out["width"], out["height"] = w >> 16, h >> 16
        stts = _find(data, ["mdia", "minf", "stbl", "stts"], ta, tb)
        if stts:
            n = struct.unpack(">I", data[stts[0] + 4:stts[0] + 8])[0]
            out["frames"] = sum(struct.unpack(">I", data[stts[0] + 8 + 8 * i:stts[0] + 12 + 8 * i])[0] for i in range(n))
        stsd = _find(data, ["mdia", "minf", "stbl", "stsd"], ta, tb)
        if stsd and stsd[1] - stsd[0] >= 16:
            out["codec"] = data[stsd[0] + 12:stsd[0] + 16].decode("latin-1")
        if out.get("frames") and out["duration_s"]:
            out["fps"] = round(out["frames"] / out["duration_s"], 2)
        break
    if "width" not in out:
        raise ValueError("no video track (hdlr 'vide')")
    return out


# ------------------------------------------------------------------ the run
def cleanup(c: Client, added: list[str]) -> None:
    for d in c.ok("GET", "/api/deployments")["deployments"]:
        if d["name"].startswith(PREFIX):
            status, _ = c.call("DELETE", f"/api/deployments/{d['id']}")
            log(f"cleanup: deployment {d['name']} -> {status}")
    for k in c.ok("GET", "/api/keys")["keys"]:
        if k["name"].startswith(PREFIX.rstrip("-")) and not k.get("revoked_at"):
            c.call("DELETE", f"/api/keys/{k['id']}")
    if added:
        deadline = time.time() + 120
        for m in c.ok("GET", "/api/models")["models"]:
            if m["name"] in added:
                while True:
                    status, _ = c.call("DELETE", f"/api/models/{m['id']}")
                    if status != 409 or time.time() > deadline:
                        break
                    time.sleep(3)
                log(f"cleanup: catalog entry {m['name']} -> {status}")


def ensure(c: Client, name: str, spec: dict) -> bool:
    have = {m["name"]: m for m in c.ok("GET", "/api/models")["models"]}
    if name in have:
        if have[name]["task"] != "video":
            raise Fail(f"{name} is in the catalog as a {have[name]['task']} model: remove it first")
        return False
    body = {"name": name, "source": spec["source"], "task": "video", "title": name, "revision": spec["revision"]}
    m = c.ok("POST", "/api/models", body, timeout=300)["model"]
    log(f"catalog: added {name} ({m['source']} @ {m['revision'][:12]}, {m['size_bytes'] / GIB:.2f} GiB, "
        f"licence {m.get('license')}, video {json.dumps(m.get('video'))[:160]})")
    return True


def usage_row(c: Client, prefix: str) -> dict:
    """This run's own key's usage (its row is named "<name> (<prefix>…)")."""
    c.ok("GET", "/api/usage?everyone=true&group=key")
    time.sleep(1)
    rows = c.ok("GET", "/api/usage?everyone=true&group=key")["rows"]
    return next((r for r in rows if f"({prefix}" in str(r.get("name"))), {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860")
    ap.add_argument("--cacert", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--machine", required=True, help="the machine to deploy on")
    ap.add_argument("--only", default="wan2.1-t2v-1.3b", help=f"comma-separated, of {', '.join(MODELS)}")
    ap.add_argument("--gpu", type=int, help="the GPU index (default: the machine's only allowed GPU)")
    ap.add_argument("--size", help="the videos' size (default: each model's own)")
    ap.add_argument("--seconds", type=int, default=5, help="the videos' length (and the deployment's longest)")
    ap.add_argument("--steps", type=int, help="num_inference_steps (default: the model's own); fewer is faster")
    ap.add_argument("--keep-minutes", type=int, default=3, help="the deployment's keep_minutes (the expiry check waits "
                                                                "this long after the first video)")
    ap.add_argument("--no-expiry", action="store_true", help="skip the expiry check")
    ap.add_argument("--no-restart", action="store_true", help="skip stopping the deployment under a job at work")
    ap.add_argument("--timeout", type=float, default=5400, help="seconds per deployment to become ready")
    ap.add_argument("--job-timeout", type=float, default=1800, help="seconds one video may take")
    ap.add_argument("--venv", default=str(Path.home() / ".cache" / "forgeyard-accept-m4"),
                    help="where to install the openai SDK if it is missing")
    ap.add_argument("--save-videos", help="keep every video in this directory")
    ap.add_argument("--keep", action="store_true", help="leave the deployments running afterwards")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args()
    unknown = [n for n in args.only.split(",") if n not in MODELS]
    if unknown:
        raise Fail(f"unknown model(s) {unknown}; known: {', '.join(MODELS)}")
    ensure_sdk(Path(args.venv))
    accept_m3.use_cacert(args.cacert)
    import httpx
    import openai

    password = os.environ.get("FORGEYARD_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    c = Client(args.server)
    c.ok("POST", "/api/auth/login", {"username": args.user, "password": password})
    m = machine(c, args.machine)
    if m["status"] != "online" or "models" not in (m.get("features") or []):
        raise Fail(f"{m['name']} must be online and serve models; status {m['status']}, features {m.get('features')}")
    report: dict = {"server": args.server, "machine": m["name"], "sdk": f"openai {openai.__version__}", "checks": [],
                    "deployments": {}}
    added: list[str] = []
    save = Path(args.save_videos) if args.save_videos else None
    if save:
        save.mkdir(parents=True, exist_ok=True)

    def check(name: str, ok: bool, detail="", info: bool = False, skip: bool = False) -> None:
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail, "info": info or skip, "skip": skip})
        tag = "SKIP" if skip else "INFO" if info else "PASS" if ok else "FAIL"
        log(f"{tag}  {name}" + (f"  ({detail})" if detail else ""))

    def card(index: int) -> dict:
        return next(a for a in machine(c, m["name"])["accelerators"] if a["index"] == index and a["vendor"] != "cpu")

    cleanup(c, [])
    recipes = {r["name"]: r for r in c.ok("GET", "/api/models")["recipes"]}
    try:
        for name in args.only.split(","):
            spec = dict(MODELS[name])
            if spec["recipe"] not in recipes:
                raise Fail(f"no recipe {spec['recipe']!r} on the server (it is older than this script)")
            if ensure(c, name, spec):
                added.append(name)
            size = args.size or spec["size"]
            params = dict(spec["params"], gpus=1, max_seconds=args.seconds, seconds=args.seconds,
                          keep_minutes=args.keep_minutes)
            if args.size:
                params["max_side"] = max(int(x) for x in size.split("x"))
            dep_name = f"{PREFIX}{name}".replace(".", "-")[:48]
            body = {"name": dep_name, "model": name, "recipe": spec["recipe"], "params": params, "served_name": dep_name,
                    "replicas": 1, "machine_id": m["id"]}
            for _ in range(36):
                est = c.ok("POST", "/api/deployments/estimate", body)
                if est["placement"]["fits"]:
                    break
                time.sleep(5)
            if not est["placement"]["fits"]:
                raise Fail(f"{dep_name} does not fit on {m['name']}: {est['placement'].get('summary')}")
            placed = est["placement"]["placed"][0]
            index = placed["gpus"][0] if placed["gpus"] else None
            if args.gpu is not None and index != args.gpu:
                raise Fail(f"{dep_name} would go to GPU {index}, not #{args.gpu}; stopping")
            before = accept_m3.settle_card(c, m["name"], card(index)["key"], 1) if index is not None else None
            dep = c.ok("POST", "/api/deployments", body)["deployment"]
            log(f"{dep_name}: deployed on {placed['machine']} GPU #{index} ({placed['variant']}); estimate "
                f"{dep['estimate']['amount'] / GIB:.2f} GiB: "
                + ", ".join(f"{p['label']} {p['bytes'] / GIB:.2f}" for p in dep["estimate"]["parts"]))
            t0 = time.time()
            d = wait_all_ready(c, dep["id"], args.timeout)
            info = {"recipe": spec["recipe"], "variant": placed["variant"], "size": size, "seconds": args.seconds,
                    "ready_seconds": int(time.time() - t0), "estimate_gib": round(dep["estimate"]["amount"] / GIB, 2),
                    "estimate_parts": {p["label"]: round(p["bytes"] / GIB, 3) for p in dep["estimate"]["parts"]}}
            report["deployments"][dep_name] = info
            if index is not None:
                ready = accept_m3.settle_card(c, m["name"], card(index)["key"])
                info["loaded_gib"] = round(((ready["vram_total"] - ready["vram_free"])
                                            - (before["vram_total"] - before["vram_free"])) / GIB, 2)
            exercise(c, d, dep_name, spec, size, args, check, httpx, openai, report, save)
            if index is not None:
                after = accept_m3.settle_card(c, m["name"], card(index)["key"])
                used = (after["vram_total"] - after["vram_free"]) - (before["vram_total"] - before["vram_free"])
                info["peak_gib"] = round(used / GIB, 2)
                check(f"{dep_name}: memory on GPU #{index} within the estimate", used <= dep["estimate"]["amount"],
                      f"after the videos {used / GIB:.2f} GiB (loaded {info['loaded_gib']} GiB; card used before/after, "
                      f"other work on the card adds noise), estimate {dep['estimate']['amount'] / GIB:.2f} GiB",
                      info=used > dep["estimate"]["amount"])
            if not args.no_restart:
                restart_check(c, d, dep_name, size, args, check, httpx, openai)
            if not args.keep:
                status, _ = c.call("DELETE", f"/api/deployments/{dep['id']}")
                log(f"done with {dep_name}: deployment removed -> {status}")
    finally:
        if args.keep:
            log("--keep: leaving the deployments running; run again without --keep to remove them")
        else:
            cleanup(c, added)
    failed = [x for x in report["checks"] if not x["ok"] and not x["info"]]
    report["passed"] = len([x for x in report["checks"] if x["ok"] and not x["info"]])
    report["failed"] = len(failed)
    report["skipped"] = len([x for x in report["checks"] if x.get("skip")])
    log(f"{report['passed']} passed, {len(failed)} failed, {report['skipped']} skipped")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


def _sdk(k: dict, httpx, openai):
    http = httpx.Client(verify=accept_m3.TLS, timeout=600) if accept_m3.TLS else httpx.Client(timeout=600)
    return openai.OpenAI(base_url=k["gateway_url"], api_key=k["key"], max_retries=0, timeout=600, http_client=http)


def follow(sdk, vid: str, timeout: float, label: str = ""):
    """Poll a video until it is completed or failed; print each status change. Returns (video, seconds)."""
    t0 = time.time()
    last = None
    while True:
        v = sdk.videos.retrieve(vid)
        if v.status != last:
            log(f"  {label or vid}: {v.status} (progress {v.progress}) after {time.time() - t0:.0f} s")
            last = v.status
        if v.status in ("completed", "failed") or time.time() - t0 > timeout:
            return v, time.time() - t0
        time.sleep(3)


def exercise(c: Client, d: dict, model: str, spec: dict, size: str, args, check, httpx, openai, report: dict,
             save, key: dict | None = None) -> None:
    """Every check on one ready video deployment `d` served as `model` (but stopping it: restart_check)."""
    k = key or c.ok("POST", "/api/keys", {"name": f"{PREFIX}key"})
    sdk = _sdk(k, httpx, openai)
    prefix = (k.get("api_key") or k).get("prefix") or k["key"][:12]
    before = usage_row(c, prefix)
    info = report["deployments"].setdefault(model, {})
    params = d.get("params") or {}
    seconds = int(params.get("max_seconds") or args.seconds)
    w, h = (int(x) for x in size.split("x"))
    fps = int(params.get("fps") or 0) or spec["fps"]
    frames = (seconds * fps) // 4 * 4 + 1
    extra = {"num_inference_steps": args.steps} if args.steps else None
    made_ms = 0

    # ---- one video, followed and fetched
    t0 = time.time()
    v = sdk.videos.create(model=model, prompt=PROMPT, seconds=str(seconds), size=size, extra_body=extra)
    check(f"{model}: a video is a job at once", v.status == "queued" and v.id.startswith("video_") and
          time.time() - t0 < 30, f"{v.id} {v.status} in {time.time() - t0:.1f} s")
    v, took = follow(sdk, v.id, args.job_timeout, "first video")
    info["seconds_first_video"] = round(took, 1)
    check(f"{model}: the video is made", v.status == "completed",
          f"{v.status} after {took:.0f} s" + (f": {v.error}" if v.error else ""))
    if v.status != "completed":
        return
    check(f"{model}: the job says when it expires", bool(v.expires_at) and v.expires_at > v.completed_at,
          f"completed_at {v.completed_at}, expires_at {v.expires_at}")
    listed = [x.id for x in sdk.videos.list()]
    check(f"{model}: the key's list holds it", v.id in listed, f"{len(listed)} listed")
    t1 = time.time()
    data = sdk.videos.download_content(v.id).content
    if save:
        (save / f"{model}-first.mp4").write_bytes(data)
    try:
        mp4 = mp4_info(data)
    except ValueError as e:
        check(f"{model}: the content is a real MP4", False, str(e))
        return
    info["mp4"] = mp4
    want = frames / fps
    check(f"{model}: the content is a real MP4", mp4["mdat_bytes"] > 1000,
          f"{len(data) / 1024:.0f} KiB in {time.time() - t1:.1f} s; {json.dumps(mp4)}")
    check(f"{model}: of the size asked", (mp4["width"], mp4["height"]) == (w, h), f"{mp4['width']}x{mp4['height']}")
    check(f"{model}: as long as asked", abs(mp4["duration_s"] - want) <= 0.25,
          f"{mp4['duration_s']:.2f} s ({mp4.get('frames')} frames at {mp4.get('fps')} fps); asked {seconds} s = "
          f"{frames} frames at {fps} fps = {want:.2f} s")
    made_ms += int(round(mp4["duration_s"] * 1000))
    try:
        sdk.videos.download_content(v.id, variant="thumbnail")
        check(f"{model}: a thumbnail is not made", False, "it was answered")
    except openai.BadRequestError as e:
        check(f"{model}: a thumbnail is not made", getattr(e, "code", None) == "variant_not_supported",
              f"400 {getattr(e, 'code', None)}")

    # ---- refusals
    def refused(label: str, body: dict, code: str) -> None:
        s, text = raw_post(f"{k['gateway_url']}/videos", json.dumps({"model": model, **body}).encode(),
                           "application/json", {"Authorization": f"Bearer {k['key']}"}, timeout=120)
        check(f"{model}: {label}", s == 400 and f'"{code}"' in text, f"{s} {text[:160]}")

    refused(f"more than {seconds} s is refused", {"prompt": PROMPT, "seconds": str(seconds + 1)}, "video_too_long")
    refused("a size over the cap is refused", {"prompt": PROMPT, "size": f"{w * 2}x{h * 2}"}, "video_too_large")
    refused("image-to-video is refused", {"prompt": PROMPT, "input_reference": "data:image/png;base64,iVBORw0KGgo="},
            "input_reference_not_supported")
    other = _sdk(c.ok("POST", "/api/keys", {"name": f"{PREFIX}other"}), httpx, openai)
    for label, call in (("its status", lambda: other.videos.retrieve(v.id)),
                        ("its content", lambda: other.videos.download_content(v.id)),
                        ("deleting it", lambda: other.videos.delete(v.id))):
        try:
            call()
            check(f"{model}: another key's video is not found ({label})", False, "it was answered")
        except openai.NotFoundError as e:
            check(f"{model}: another key's video is not found ({label})", getattr(e, "code", None) == "video_not_found",
                  f"404 {getattr(e, 'code', None)}")
    check(f"{model}: another key lists none of it", v.id not in [x.id for x in other.videos.list()])
    for e in d["endpoints"]:
        s, text = raw_post(f"{e['url']}/v1/videos", b"prompt=hi", "application/x-www-form-urlencoded", timeout=20)
        check(f"{model}: replica {e['replica']} on {e['machine']} without the key is refused", s in (401, 403),
              f"{s} {text[:80]}")

    # ---- the per-key cap and deleting a job at work
    per_key = int(params.get("jobs_per_key") or 2)
    held = [sdk.videos.create(model=model, prompt=f"{SECOND_PROMPT}, take {i}", seconds=str(seconds), size=size,
                              extra_body=extra) for i in range(per_key)]
    try:
        sdk.videos.create(model=model, prompt=SECOND_PROMPT, seconds=str(seconds), size=size)
        check(f"{model}: a job over the key's {per_key} at once is refused", False, "it was taken")
    except openai.RateLimitError as e:
        ra = e.response.headers.get("retry-after")
        check(f"{model}: a job over the key's {per_key} at once is refused", getattr(e, "code", None) == "too_many_jobs"
              and bool(ra), f"429 {getattr(e, 'code', None)}, Retry-After {ra}")
    for x in held:
        gone = sdk.videos.delete(x.id)
        try:
            sdk.videos.retrieve(x.id)
            still = True
        except openai.NotFoundError:
            still = False
        check(f"{model}: deleting a job at work cancels it", gone.deleted and not still, f"{x.id}")

    # ---- usage
    after = usage_row(c, prefix)
    reqs = after.get("requests", 0) - before.get("requests", 0)
    media = after.get("media_ms", 0) - before.get("media_ms", 0)
    busy = after.get("busy_ms", 0) - before.get("busy_ms", 0)
    # The first video, the refused ones (3 at the gateway, 1 over the cap) and the two deleted.
    check(f"{model}: usage counts one request per job (and the refused ones)", reqs == 1 + 3 + 1 + per_key,
          f"{reqs} requests")
    check(f"{model}: usage counts the video's length", abs(media - made_ms) <= 50, f"{media} ms counted, {made_ms} ms made")
    check(f"{model}: usage records the replica's time on it", busy > 0, f"{busy / 1000:.1f} s busy")
    info.update(media_ms=media, busy_seconds=round(busy / 1000, 1))

    # ---- expiry
    if args.no_expiry:
        check(f"{model}: an expired video is gone", True, "--no-expiry", skip=True)
        return
    wait = max(0, (v.expires_at or 0) - time.time()) + 8
    log(f"  waiting {wait:.0f} s for the first video to expire (keep_minutes {params.get('keep_minutes')})")
    time.sleep(wait)
    try:
        sdk.videos.download_content(v.id)
        check(f"{model}: an expired video is gone", False, "its content was answered")
    except openai.APIStatusError as e:
        check(f"{model}: an expired video is gone", e.status_code == 410 and getattr(e, "code", None) == "video_expired",
              f"{e.status_code} {getattr(e, 'code', None)}")


def restart_check(c: Client, d: dict, model: str, size: str, args, check, httpx, openai) -> None:
    """A replica that goes takes its jobs: one more video, the deployment stopped while it is at work."""
    k = c.ok("POST", "/api/keys", {"name": f"{PREFIX}restart"})
    sdk = _sdk(k, httpx, openai)
    params = d.get("params") or {}
    v = sdk.videos.create(model=model, prompt=SECOND_PROMPT, seconds=str(int(params.get("max_seconds") or 5)), size=size)
    deadline = time.time() + 300
    while time.time() < deadline and sdk.videos.retrieve(v.id).status == "queued":
        time.sleep(2)
    c.ok("POST", f"/api/deployments/{d['id']}/stop", {})
    log(f"  {model}: stopped while {v.id} is at work")
    got, took = follow(sdk, v.id, 120, "the video under a stop")
    check(f"{model}: a job whose replica is stopped reports failed",
          got.status == "failed" and getattr(got.error, "code", None) == "replica_gone",
          f"{got.status} after {took:.0f} s: {got.error}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        log(f"STOPPED: {e}")
        sys.exit(2)
