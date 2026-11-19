#!/usr/bin/env python3
"""1.4 acceptance on real machines (#86): vision models deployed from Models with the built-in `vision`
recipe and called at /v1/vision/detect, /v1/vision/segment and /v1/vision/depth through the gateway.

Through Forgeyard's API only (an admin session), for each model of --only, on --machine (--device gpu or
cpu, default: gpu when the machine has an allowed GPU):

  1. adds the model to the catalog unless it is there (pinned to the commit below), deploys it there as
     `accept-vis-<model>` and waits until it is ready; on a GPU the card's memory is read before and once
     ready, against the estimate;
  2. with a new API key: detection on the COCO validation photo 000000039769 (two cats on a sofa: both cats
     found above 0.8; --image to use another), as multipart and as JSON; a zero-shot detector (1.4.4, #142) on the
     same photo told to find a cat, a remote control and a giraffe (both cats above 0.5, a remote, no giraffe),
     as multipart (a comma-separated field) and as JSON, and without labels (400 labels_required), while a
     detector with fixed classes given labels is 400 labels_not_supported; segmentation with a box around the
     red square of a generated image (the mask covers the square); depth on the photo (a 16-bit PNG of the
     photo's size, far and near differ). Each call is timed;
  3. the refusals: no key (401), the wrong task's endpoint (400 wrong_endpoint), no image (400), a key whose
     allowlist lacks the model (403), and the replica's own port without its key (401 or closed);
  4. reads the usage of the run's own key: one image per answered request, its kilopixels, busy time;

then removes what it created and prints a report (JSON with --out). Everything is named accept-vis-*, and
leftovers of an earlier run are removed first, so it can be run again.

    FORGEYARD_PASSWORD=… python3 scripts/accept_vision.py --server https://<server>:7860 --cacert forgeyard-ca.pem \\
        --user <admin> --machine <machine> [--device gpu|cpu] [--only rtdetr-v2-r18,sam2.1-hiera-tiny,…] --out r.json

Models it knows (--only; the first three are the default, every one is recommended but rtdetr-v2-r50):
  rtdetr-v2-r18, sam2.1-hiera-tiny, depth-anything-v2-small, rtdetr-v2-r50 (a larger detector, Apache-2.0),
  and the zero-shot detectors (1.4.4, they need a vision image from 1.4.4 on): grounding-dino-tiny,
  mm-grounding-dino-tiny, owlv2-base

The vision image is the one the `vision` recipe pins by digest (1.4.1: docker.io/tonyhuang1101/forgeyard-vision,
X.Y.Z or X.Y.Z-cuda, recorded in images.lock.json): the machine pulls it, or gets it through the server. To try
a change to images/vision before its release, build it on the machine (images/vision/Dockerfile) and pass a
custom recipe that names that local tag with --recipe.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import struct
import sys
import time
import urllib.request
import uuid
import zlib
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept_m3  # noqa: E402
from accept_m3 import GIB, Client, Fail, log, machine  # noqa: E402
from accept_m4 import wait_all_ready  # noqa: E402

PREFIX = "accept-vis-"
COCO_CATS = "http://images.cocodataset.org/val2017/000000039769.jpg"   # CC BY 4.0, two cats and two remotes
MODELS = {
    "rtdetr-v2-r18": {"source": "hf://PekingU/rtdetr_v2_r18vd", "revision": "5650961749fa93567c0d46fc7f43ea4f9e914107",
                      "task": "detect"},
    "sam2.1-hiera-tiny": {"source": "hf://facebook/sam2.1-hiera-tiny", "revision": "de431c4043854a71d8101e17995dfe596bf101a5",
                          "task": "segment"},
    "depth-anything-v2-small": {"source": "hf://depth-anything/Depth-Anything-V2-Small-hf",
                                "revision": "5426e4f0f36572d16453bbda7a8389317b1bef99", "task": "depth"},
    "rtdetr-v2-r50": {"source": "hf://PekingU/rtdetr_v2_r50vd", "revision": "282494075698cab9faa1096ae26856890030c817",
                      "task": "detect"},
    # 1.4.4 (#142): zero-shot detectors, told what to find
    "grounding-dino-tiny": {"source": "hf://IDEA-Research/grounding-dino-tiny",
                            "revision": "a2bb814dd30d776dcf7e30523b00659f4f141c71", "task": "detect", "zero_shot": True},
    "mm-grounding-dino-tiny": {"source": "hf://openmmlab-community/mm_grounding_dino_tiny_o365v1_goldg_v3det",
                               "revision": "ef2053aa1e0cc9d9cee1df31ca919ee9aae4fc8e", "task": "detect", "zero_shot": True},
    "owlv2-base": {"source": "hf://google/owlv2-base-patch16-ensemble",
                   "revision": "cfd3195ba4ea9592eec887ded089f4c08eff231d", "task": "detect", "zero_shot": True},
}
FIND = ["cat", "remote control", "giraffe"]     # what a zero-shot detector is told to find on the cats photo
OTHER = {"detect": "depth", "segment": "detect", "depth": "segment"}


# ------------------------------------------------------------------ images
def square_png(w: int = 256, h: int = 192) -> bytes:
    """Grey, a red square at (40, 40)-(120, 120) and a blue rectangle (forgeyard/models/vision.test_png)."""
    def px(x, y):
        if 40 <= x < 120 and 40 <= y < 120:
            return b"\xd0\x20\x20"
        if 150 <= x < 220 and 60 <= y < 160:
            return b"\x20\x40\xd0"
        return b"\x80\x80\x80"

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"".join(px(x, y) for x in range(w)) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def png_header(data: bytes) -> tuple[int, int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    w, h, depth = struct.unpack(">IIB", data[16:25])
    return w, h, depth


def photo(path: Optional[str]) -> tuple[bytes, str]:
    if path:
        return Path(path).read_bytes(), Path(path).name
    with urllib.request.urlopen(COCO_CATS, timeout=30) as r:
        return r.read(), "coco-000000039769.jpg"


def multipart(fields: dict, image: bytes, filename: str) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = [f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in fields.items()]
    parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n".encode() + image + b"\r\n")
    return b"".join(parts) + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def post(url: str, body: bytes, ctype: str, key: Optional[str], timeout: float = 300) -> tuple[int, dict, float]:
    headers = {"Content-Type": ctype, **({"Authorization": f"Bearer {key}"} if key else {})}
    t0 = time.monotonic()
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=accept_m3.TLS) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": {"message": str(e), "code": "unreachable"}}, time.monotonic() - t0
    try:
        data = json.loads(raw or b"null")
    except ValueError:
        data = {"raw": raw[:300].decode("utf-8", "replace")}
    return status, data if isinstance(data, dict) else {"raw": data}, time.monotonic() - t0


def code(data: dict) -> Optional[str]:
    err = data.get("error") if isinstance(data, dict) else None
    return err.get("code") if isinstance(err, dict) else None


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
        if have[name]["task"] != spec["task"]:
            raise Fail(f"{name} is in the catalog as a {have[name]['task']} model: remove it first")
        return False
    m = c.ok("POST", "/api/models", {"name": name, "source": spec["source"], "revision": spec["revision"],
                                     "title": name}, timeout=300)["model"]
    if m["task"] != spec["task"]:
        raise Fail(f"{name} was added as {m['task']}, not {spec['task']}")
    log(f"catalog: added {name} ({m['source']} @ {m['revision'][:12]}, {m['size_bytes'] / GIB:.3f} GiB, "
        f"{json.dumps(m.get('vision'))[:160]})")
    return True


def other_model(c, name: str) -> Optional[str]:
    """A served model other than `name`, for a key whose allowlist lacks it (None: there is none)."""
    return next((m["name"] for m in c.ok("GET", "/api/keys")["models"] if m["name"] != name), None)


def exercise(server: str, d: dict, name: str, spec: dict, check, key: str, narrow: Optional[str], image: bytes, filename: str,
             report: dict) -> int:
    """The calls and refusals for one ready deployment (also used by scripts/accept_all.py). Returns how many
    requests were answered (each counts one image)."""
    base = server.rstrip("/") + "/v1/vision/"
    task = spec["task"]
    info = report.setdefault(name, {})
    if task == "detect" and spec.get("zero_shot"):
        body, ctype = multipart({"model": name, "labels": ", ".join(FIND)}, image, filename)
        s, data, t = post(base + "detect", body, ctype, key)
        found = data.get("detections", [])
        cats = [o for o in found if o.get("label") == "cat" and o.get("score", 0) > 0.5]
        remotes = [o for o in found if o.get("label") == "remote control"]
        check(f"{name}: find {', '.join(FIND)} ({filename}): two cats, a remote, no giraffe",
              s == 200 and len(cats) == 2 and remotes and not any(o.get("label") == "giraffe" for o in found)
              and data.get("model") == name,
              f"{s} in {t:.2f} s: {[(o.get('label'), o.get('score')) for o in found][:8] or data}")
        info["detect_s"] = round(t, 3)
        jb = json.dumps({"model": name, "image": base64.b64encode(image).decode(), "labels": FIND}).encode()
        s, data2, t = post(base + "detect", jb, "application/json", key)
        check(f"{name}: the same as JSON with a list of labels (the same boxes)",
              s == 200 and data2.get("detections") == data.get("detections"), f"{s} in {t:.2f} s")
        jb = json.dumps({"model": name, "image": base64.b64encode(image).decode()}).encode()
        s, data, _ = post(base + "detect", jb, "application/json", key)
        check(f"{name}: no labels: 400 labels_required", s == 400 and code(data) == "labels_required", f"{s} {code(data)}")
    elif task == "detect":
        body, ctype = multipart({"model": name, "threshold": "0.5"}, image, filename)
        s, data, t = post(base + "detect", body, ctype, key)
        cats = [o for o in data.get("detections", []) if o.get("label") == "cat" and o.get("score", 0) > 0.8]
        check(f"{name}: detect, multipart ({filename}: two cats)", s == 200 and len(cats) == 2 and data.get("model") == name,
              f"{s} in {t:.2f} s: {[(o.get('label'), o.get('score')) for o in data.get('detections', [])][:6] or data}")
        info["detect_s"] = round(t, 3)
        jb = json.dumps({"model": name, "image": base64.b64encode(image).decode(), "threshold": 0.5}).encode()
        s, data2, t = post(base + "detect", jb, "application/json", key)
        check(f"{name}: detect, JSON with base64 (the same boxes)", s == 200 and data2.get("detections") == data.get("detections"),
              f"{s} in {t:.2f} s")
        jb = json.dumps({"model": name, "image": base64.b64encode(image).decode(), "labels": ["cat"]}).encode()
        s, data, _ = post(base + "detect", jb, "application/json", key)
        # A vision image from before 1.4.4 does not know labels and answers as if none were sent.
        check(f"{name}: labels for a detector with fixed classes: 400 labels_not_supported",
              s == 400 and code(data) == "labels_not_supported", f"{s} {code(data)}"
              + (" (a vision image from before 1.4.4 ignores labels)" if s == 200 else ""), info=s == 200)
    elif task == "segment":
        sq = square_png()
        jb = json.dumps({"model": name, "image": base64.b64encode(sq).decode(), "box": [40, 40, 120, 120]}).encode()
        s, data, t = post(base + "segment", jb, "application/json", key)
        best = (data.get("masks") or [{}])[0]
        box = best.get("box") or [0, 0, 0, 0]
        ok = s == 200 and best.get("area", 0) > 5000 and abs(box[0] - 40) <= 6 and abs(box[2] - 120) <= 6
        if ok:
            w, h, _ = png_header(base64.b64decode(best["png"]))
            ok = (w, h) == (256, 192)
        check(f"{name}: segment, a box around the red square (its mask)", ok,
              f"{s} in {t:.2f} s: area {best.get('area')}, box {box}, score {best.get('score')}")
        info["segment_s"] = round(t, 3)
        body, ctype = multipart({"model": name, "points": "[[80, 80]]"}, sq, "square.png")
        s, data, t = post(base + "segment", body, ctype, key)
        check(f"{name}: segment, multipart with a point", s == 200 and (data.get("masks") or [{}])[0].get("area", 0) > 3000,
              f"{s} in {t:.2f} s")
    else:
        body, ctype = multipart({"model": name}, image, filename)
        s, data, t = post(base + "depth", body, ctype, key)
        ok = s == 200 and data.get("max", 0) > data.get("min", 0)
        if ok:
            w, h, depth = png_header(base64.b64decode(data["depth_png"]))
            ok = (w, h, depth) == (data["width"], data["height"], 16)
        check(f"{name}: depth on {filename} (a 16-bit PNG of its size)", ok,
              f"{s} in {t:.2f} s: {data.get('width')}x{data.get('height')}, {data.get('min')} to {data.get('max')}")
        info["depth_s"] = round(t, 3)
    # Refusals.
    tiny = json.dumps({"model": name, "image": base64.b64encode(square_png()).decode()}).encode()
    s, data, _ = post(base + task, tiny, "application/json", None)
    check(f"{name}: no key: 401", s == 401, f"{s} {code(data)}")
    s, data, _ = post(base + OTHER[task], tiny, "application/json", key)
    check(f"{name}: /v1/vision/{OTHER[task]} for a {task} model: 400 wrong_endpoint", s == 400 and code(data) == "wrong_endpoint",
          f"{s} {code(data)}")
    s, data, _ = post(base + task, json.dumps({"model": name}).encode(), "application/json", key)
    check(f"{name}: no image: 400 missing_image", s == 400 and code(data) == "missing_image", f"{s} {code(data)}")
    if narrow is None:
        check(f"{name}: a key without the model: 403", True, "no other model to allow on this server", info=True)
    else:
        s, data, _ = post(base + task, tiny, "application/json", narrow)
        check(f"{name}: a key without the model: 403", s == 403 and code(data) == "model_not_allowed", f"{s} {code(data)}")
    rep = d["workload"]["replica_list"][0]
    port = next(iter(rep.get("ports") or []), {})
    if port.get("host_port") and port.get("address"):
        url = f"http://{port['address']}:{port['host_port']}/v1/vision/{task}"
        s, data, _ = post(url, tiny, "application/json", None, timeout=15)
        check(f"{name}: the replica's own port without its key: refused", s in (0, 401), f"{s} {code(data)}")
    return 1 if task == "depth" else 2   # a refused request (400) is not counted


def usage_of(c: Client, key_name: str) -> dict:
    c.ok("GET", "/api/usage?everyone=true&group=key")
    time.sleep(1)
    rows = c.ok("GET", "/api/usage?everyone=true&group=key")["rows"]
    return next((r for r in rows if key_name in str(r.get("name"))), {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860")
    ap.add_argument("--cacert", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--machine", required=True, help="the machine to deploy on")
    ap.add_argument("--device", choices=("cpu", "gpu"), help="default: gpu when the machine has an allowed GPU")
    ap.add_argument("--only", default="rtdetr-v2-r18,sam2.1-hiera-tiny,depth-anything-v2-small",
                    help=f"comma-separated, of {', '.join(MODELS)}")
    ap.add_argument("--recipe", default="vision", help="the recipe (a custom one, e.g. with a locally built image or a "
                                                         "process variant, must serve the vision tasks)")
    ap.add_argument("--image", help="the detection and depth photo (default: COCO 000000039769, downloaded)")
    ap.add_argument("--threads", type=int, default=8, help="CPU threads for a model on the CPU")
    ap.add_argument("--timeout", type=float, default=1800, help="seconds per deployment to become ready")
    ap.add_argument("--keep", action="store_true", help="leave the deployments running afterwards")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args()
    unknown = [n for n in args.only.split(",") if n not in MODELS]
    if unknown:
        raise Fail(f"unknown model(s) {unknown}; known: {', '.join(MODELS)}")
    accept_m3.use_cacert(args.cacert)
    password = os.environ.get("FORGEYARD_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    c = Client(args.server)
    c.ok("POST", "/api/auth/login", {"username": args.user, "password": password})
    m = machine(c, args.machine)
    if m["status"] != "online" or "models" not in (m.get("features") or []):
        raise Fail(f"{m['name']} must be online and serve models; status {m['status']}, features {m.get('features')}")
    gpus = [a for a in m["accelerators"] if a["vendor"] == "nvidia" and a.get("allowed", True)]
    device = args.device or ("gpu" if gpus else "cpu")
    image, filename = photo(args.image)
    report: dict = {"server": args.server, "machine": m["name"], "device": device, "checks": [], "deployments": {}}
    added: list[str] = []

    def check(label: str, ok, detail="", info: bool = False) -> None:
        report["checks"].append({"check": label, "ok": bool(ok), "detail": detail, "info": info})
        log(f"{'INFO' if info else 'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))

    def card(index: int) -> dict:
        return next(a for a in machine(c, m["name"])["accelerators"] if a["index"] == index and a["vendor"] != "cpu")

    cleanup(c, [])
    key_name = f"{PREFIX}{uuid.uuid4().hex[:6]}"
    key = c.ok("POST", "/api/keys", {"name": key_name})["key"]
    served = 0
    try:
        for name in args.only.split(","):
            spec = MODELS[name]
            if ensure(c, name, spec):
                added.append(name)
            dep_name = f"{PREFIX}{name}".replace(".", "-")[:48]
            params = {"gpus": 1 if device == "gpu" else 0}
            if device == "cpu":
                params["threads"] = args.threads
            body = {"name": dep_name, "model": name, "recipe": args.recipe, "params": params, "served_name": dep_name,
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
            before = accept_m3.settle_card(c, m["name"], card(index)["key"], 1) if index is not None else None
            dep = c.ok("POST", "/api/deployments", body)["deployment"]
            log(f"{dep_name}: deployed on {placed['machine']} {'GPU #' + str(index) if index is not None else 'CPU'} "
                f"({placed['variant']}); estimate {dep['estimate']['amount'] / GIB:.2f} GiB")
            t0 = time.time()
            d = wait_all_ready(c, dep["id"], args.timeout)
            info = {"variant": placed["variant"], "ready_seconds": int(time.time() - t0),
                    "estimate_gib": round(dep["estimate"]["amount"] / GIB, 2)}
            report["deployments"][dep_name] = info
            check(f"{dep_name}: ready on {placed['machine']} ({placed['variant']})", True, f"after {info['ready_seconds']} s")
            other = other_model(c, dep_name)
            narrow = c.ok("POST", "/api/keys", {"name": f"{key_name}-narrow-{len(report['deployments'])}",
                                                "models": [other]})["key"] if other else None
            served += exercise(c.server, d, dep_name, spec, check, key, narrow, image, filename, report["deployments"])
            if index is not None:
                after = accept_m3.settle_card(c, m["name"], card(index)["key"])
                used = (after["vram_total"] - after["vram_free"]) - (before["vram_total"] - before["vram_free"])
                info["used_gib"] = round(used / GIB, 2)
                check(f"{dep_name}: memory on GPU #{index} within the estimate", used <= dep["estimate"]["amount"],
                      f"{used / GIB:.2f} GiB used after the calls (other work on the card adds noise), estimate "
                      f"{dep['estimate']['amount'] / GIB:.2f} GiB", info=used > dep["estimate"]["amount"])
            if not args.keep:
                status, _ = c.call("DELETE", f"/api/deployments/{dep['id']}")
                log(f"done with {dep_name}: deployment removed -> {status}")
        u = usage_of(c, key_name)
        check("usage: one image per answered request, with its kilopixels", u.get("images") == served and u.get("kilopixels", 0) > 0,
              f"images {u.get('images')} (expected {served}), kilopixels {u.get('kilopixels')}, busy {u.get('busy_ms')} ms")
    finally:
        if args.keep:
            log("--keep: leaving the deployments running; run again without --keep to remove them")
        else:
            cleanup(c, added)
    failed = [x for x in report["checks"] if not x["ok"] and not x["info"]]
    report["passed"] = len([x for x in report["checks"] if x["ok"] and not x["info"]])
    report["failed"] = len(failed)
    log(f"{report['passed']} passed, {len(failed)} failed")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        log(f"FAIL  {e}")
        sys.exit(1)
