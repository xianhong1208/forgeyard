#!/usr/bin/env python3
"""1.3 acceptance on real machines: image generation at /v1/images/generations through the gateway, called
with the official OpenAI Python SDK (client.images.generate).

Through Forgeyard's API only (an admin session), for each model of --only, on --machine:

  1. adds the model to the catalog unless it is there (pinned to the commit below; GGUF models with the
     files beside them), deploys it there as `accept-img-<model>` and waits until it is ready. A GPU
     deployment goes to --machine's allowed GPU (or --gpu); the previous model's card is freed first, and
     the card's memory is read before, once ready, and after the largest request the caps allow, against
     the estimate;
  2. creates an API key and, with the SDK: one image at the model's own size (it must decode as a PNG of
     that size: CRCs and pixel data checked here, no Pillow), two images in one request (two different
     ones), and the largest request the deployment allows (max_images at max_side), timing each;
  3. shows the refusals: response_format url, more images than the cap, a side over the cap, more steps
     than the cap, an empty prompt, engine settings in the prompt, and the replica's own port without its
     key;
  4. the queue: two requests at once on the concurrency-1 deployment; both are answered, one after the
     other (the second takes about twice as long);
  5. reads the usage of the run's own key: the images and their kilopixels, and busy time;
  6. what it shows (--vision): each first image goes to a chat model that reads images, through the
     gateway, which is asked what it shows; the answer should name the prompt's subject. Without one it
     is SKIPped.

then removes what it created (deployments, the catalog entries it added, keys) and prints a report (also
JSON with --out; --save-images keeps the images). Everything is named accept-img-*, and leftovers of an
earlier run are removed first, so it can be run again.

    FORGEYARD_PASSWORD=… python3 scripts/accept_img.py --server https://<server>:7860 --cacert forgeyard-ca.pem \\
        --user <admin> --machine <machine> --only z-image-turbo,sdxl-base-1.0 --out accept-img.json

Models it knows (--only):
  z-image-turbo, sdxl-base-1.0, flux.1-schnell   diffusers pipelines on vLLM-Omni (NVIDIA); FLUX.1-schnell is
                                                 gated: HF_TOKEN must be a token that accepted its terms
  z-image-turbo-gguf, flux.1-schnell-gguf        GGUF with their text encoders and VAE on stable-diffusion.cpp
                                                 (Vulkan on AMD; --device cpu for the CPU; no CUDA, #43)
  sd-1.5-gguf                                    Stable Diffusion 1.5 GGUF on stable-diffusion.cpp, on the CPU
GGUF models run with --recipe (default sd-cpp; a custom one, such as one with a locally loaded image,
must keep the sd-cpp recipe's `image:` block) on --device (cpu or gpu).

With Forgeyard's own CA, --cacert is the CA file. The official SDK: as scripts/accept_m4.py, in its own
virtual environment (--venv) when `openai` is not installed.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept_m3  # noqa: E402
from accept_m3 import GIB, Client, Fail, log, machine  # noqa: E402
from accept_m4 import ensure_sdk, raw_post, wait_all_ready  # noqa: E402

PREFIX = "accept-img-"
PROMPT = "A red apple on a wooden table, studio photo"
SUBJECT = ("apple",)
TWO = "A small sailboat on a calm blue lake"
MODELS = {
    "z-image-turbo": {"source": "hf://Tongyi-MAI/Z-Image-Turbo", "revision": "f332072aa78be7aecdf3ee76d5c247082da564a6",
                      "recipe": "vllm-omni-image", "device": "gpu", "size": 1024, "params": {}},
    "sdxl-base-1.0": {"source": "hf://stabilityai/stable-diffusion-xl-base-1.0",
                      "revision": "462165984030d82259a11f4367a4eed129e94a7b", "recipe": "vllm-omni-image",
                      "device": "gpu", "size": 1024, "params": {}},
    "flux.1-schnell": {"source": "hf://black-forest-labs/FLUX.1-schnell", "revision": "741f7c3ce8b383c54771c7003378a50191e9efe9",
                       "recipe": "vllm-omni-image", "device": "gpu", "size": 1024, "gated": True, "params": {}},
    "z-image-turbo-gguf": {"source": "hf://wbruna/Z-Image-Turbo-sdcpp-GGUF", "revision": "ea7669b52d27042b4909e3413f58581c0d52d522",
                           "file": "z_image_turbo-Q8_0.gguf",
                           "components": {"llm": "qwen_3_4b-Q8_0.gguf", "vae": "ae_bf16.safetensors"},
                           "engine_args": {"sd-cpp": ["--cfg-scale", "1", "--steps", "8"]}, "recipe": "sd-cpp",
                           "size": 1024, "params": {}},
    "flux.1-schnell-gguf": {"source": "hf://second-state/FLUX.1-schnell-GGUF",
                            "revision": "8c45a2ba25e2d02bd34230989fb54983f39e44ec", "file": "flux1-schnell-Q4_0.gguf",
                            "components": {"t5xxl": "t5xxl-Q8_0.gguf", "clip_l": "clip_l-Q8_0.gguf", "vae": "ae.safetensors"},
                            "engine_args": {"sd-cpp": ["--cfg-scale", "1", "--steps", "4"]}, "recipe": "sd-cpp",
                            "size": 1024, "params": {}},
    "sd-1.5-gguf": {"source": "hf://second-state/stable-diffusion-v1-5-GGUF",
                    "revision": "031b5f5df991f511b3f5fa8fed6d99048ababb69",
                    "file": "stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf",
                    "engine_args": {"sd-cpp": ["--steps", "20", "--cfg-scale", "7"]}, "recipe": "sd-cpp",
                    "device": "cpu", "size": 512, "params": {}},
}


# ------------------------------------------------------------------ images, decoded here (no Pillow)
def _png(data: bytes) -> tuple[int, int, str]:
    """Decode a PNG enough to know it is whole: every chunk's CRC, IHDR, and the IDAT data inflating to
    exactly the rows its size and colour type need (each row's filter byte checked)."""
    pos, idat, ihdr, ended = 8, [], None, False
    while pos + 12 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        crc = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]
        if len(body) != n or zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise ValueError(f"PNG chunk {kind!r} is cut or its CRC is wrong")
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            ended = True
            break
        pos += 12 + n
    if ihdr is None or not idat or not ended:
        raise ValueError("PNG without IHDR, IDAT or IEND")
    w, h, depth, ctype, _, _, interlace = ihdr
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None or depth not in (1, 2, 4, 8, 16) or interlace != 0:
        raise ValueError(f"PNG colour type {ctype}, depth {depth}, interlace {interlace} not checked here")
    row = (w * channels * depth + 7) // 8
    raw = zlib.decompress(b"".join(idat))
    if len(raw) != h * (row + 1):
        raise ValueError(f"PNG pixel data is {len(raw)} bytes, {h * (row + 1)} expected for {w}x{h}")
    if any(raw[i * (row + 1)] > 4 for i in range(h)):
        raise ValueError("PNG row with an unknown filter")
    return w, h, "png"


def _jpeg(data: bytes) -> tuple[int, int, str]:
    if not data.endswith(b"\xff\xd9"):
        raise ValueError("JPEG without its end marker")
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            raise ValueError("JPEG marker expected")
        marker = data[pos + 1]
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
            return w, h, "jpeg"
        pos += 2 + length
    raise ValueError("JPEG without a frame header")


def decode_image(data: bytes) -> tuple[int, int, str]:
    """(width, height, format) of a PNG (fully checked) or a JPEG (structure checked); ValueError else."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        size = struct.unpack("<I", data[4:8])[0]
        if size + 8 != len(data):
            raise ValueError("WebP size does not match")
        return 0, 0, "webp"
    raise ValueError("not a PNG, JPEG or WebP")


def distinct(a: bytes, b: bytes) -> bool:
    return a != b


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
        if have[name]["task"] != "image":
            raise Fail(f"{name} is in the catalog as a {have[name]['task']} model: remove it first")
        return False
    body = {"name": name, "source": spec["source"], "task": "image", "title": name, "revision": spec["revision"]}
    for k in ("file", "components", "engine_args"):
        if spec.get(k):
            body[k] = spec[k]
    if spec.get("gated"):
        token = os.environ.get("HF_TOKEN")
        if not token:
            raise Fail(f"{name} is gated: set HF_TOKEN to a Hugging Face token that accepted its terms")
        body["token"] = token
    m = c.ok("POST", "/api/models", body, timeout=300)["model"]
    log(f"catalog: added {name} ({m['source']} @ {m['revision'][:12]}, {m['size_bytes'] / GIB:.2f} GiB, "
        f"licence {m.get('license')}, image {json.dumps(m.get('image'))[:160]})")
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
    ap.add_argument("--only", default="z-image-turbo", help=f"comma-separated, of {', '.join(MODELS)}")
    ap.add_argument("--recipe", help="the recipe for GGUF models (default sd-cpp), e.g. a custom one with a local image")
    ap.add_argument("--device", choices=("cpu", "gpu"), help="for GGUF models (default: gpu when the machine has one)")
    ap.add_argument("--gpu", type=int, help="the GPU index (default: the machine's only allowed GPU)")
    ap.add_argument("--threads", type=int, default=16, help="CPU threads for a model on the CPU")
    ap.add_argument("--size", type=int, help="the side of the images (default: each model's own); smaller is faster")
    ap.add_argument("--max-images", type=int, default=2, help="the deployment's max_images (the largest request is this "
                                                              "many at the largest side)")
    ap.add_argument("--vision", default="auto", help="what the image shows: auto (a ready chat model that reads images), "
                                                     "a served model name, or none")
    ap.add_argument("--timeout", type=float, default=5400, help="seconds per deployment to become ready")
    ap.add_argument("--venv", default=str(Path.home() / ".cache" / "forgeyard-accept-m4"),
                    help="where to install the openai SDK if it is missing")
    ap.add_argument("--save-images", help="keep every image in this directory")
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
    gpus = [a for a in m["accelerators"] if a["vendor"] != "cpu" and a.get("allowed", True)]
    report: dict = {"server": args.server, "machine": m["name"], "sdk": f"openai {openai.__version__}", "checks": [],
                    "deployments": {}}
    added: list[str] = []
    save = Path(args.save_images) if args.save_images else None
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
            recipe = (args.recipe or spec["recipe"]) if spec.get("file") else spec["recipe"]
            if recipe not in recipes:
                raise Fail(f"no recipe {recipe!r} on the server")
            fixed = spec.get("device")
            device = args.device if spec.get("file") and args.device else \
                fixed or ("gpu" if gpus and "gpu" in recipes[recipe]["devices"] else "cpu")
            if ensure(c, name, spec):
                added.append(name)
            side = args.size or spec["size"]
            params = dict(spec["params"], max_images=args.max_images, max_side=side,
                          default_size=f"{side}x{side}" if f"{side}x{side}" in
                          (recipes[recipe]["params"]["default_size"].get("choices") or []) else "auto")
            params["gpus"] = 1 if device == "gpu" else 0
            if device == "cpu" and "threads" in recipes[recipe]["params"]:
                params["threads"] = args.threads
            dep_name = f"{PREFIX}{name}".replace(".", "-")[:48]
            body = {"name": dep_name, "model": name, "recipe": recipe, "params": params, "served_name": dep_name,
                    "replicas": 1, "machine_id": m["id"]}
            # The previous model's card is freed once its agent confirms the stop: wait for it a little.
            for _ in range(36):
                est = c.ok("POST", "/api/deployments/estimate", body)
                if est["placement"]["fits"]:
                    break
                time.sleep(5)
            if not est["placement"]["fits"]:
                raise Fail(f"{dep_name} does not fit on {m['name']}: {est['placement'].get('summary')}")
            placed = est["placement"]["placed"][0]
            index = placed["gpus"][0] if placed["gpus"] else None
            if device == "gpu" and args.gpu is not None and index != args.gpu:
                raise Fail(f"{dep_name} would go to GPU {index}, not #{args.gpu}; stopping")
            before = accept_m3.settle_card(c, m["name"], card(index)["key"], 1) if index is not None else None
            dep = c.ok("POST", "/api/deployments", body)["deployment"]
            log(f"{dep_name}: deployed on {placed['machine']} {'GPU #' + str(index) if index is not None else 'CPU'} "
                f"({placed['variant']}); estimate {dep['estimate']['amount'] / GIB:.2f} GiB: "
                + ", ".join(f"{p['label']} {p['bytes'] / GIB:.2f}" for p in dep["estimate"]["parts"]))
            t0 = time.time()
            d = wait_all_ready(c, dep["id"], args.timeout)
            info = {"recipe": recipe, "device": device, "variant": placed["variant"], "side": side,
                    "ready_seconds": int(time.time() - t0), "estimate_gib": round(dep["estimate"]["amount"] / GIB, 2),
                    "estimate_parts": {p["label"]: round(p["bytes"] / GIB, 3) for p in dep["estimate"]["parts"]}}
            report["deployments"][dep_name] = info
            if index is not None:
                ready = accept_m3.settle_card(c, m["name"], card(index)["key"])
                info["loaded_gib"] = round(((ready["vram_total"] - ready["vram_free"])
                                            - (before["vram_total"] - before["vram_free"])) / GIB, 2)
            exercise(c, d, dep_name, spec, side, args, check, httpx, openai, report, save)
            if index is not None:
                # PyTorch keeps what the largest request took: the card now shows the peak of the run.
                after = accept_m3.settle_card(c, m["name"], card(index)["key"])
                used = (after["vram_total"] - after["vram_free"]) - (before["vram_total"] - before["vram_free"])
                info["peak_gib"] = round(used / GIB, 2)
                check(f"{dep_name}: memory on GPU #{index} within the estimate", used <= dep["estimate"]["amount"],
                      f"after the largest request {used / GIB:.2f} GiB (loaded {info['loaded_gib']} GiB; card used "
                      f"before/after, other work on the card adds noise), estimate {dep['estimate']['amount'] / GIB:.2f} GiB",
                      info=used > dep["estimate"]["amount"])
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


def exercise(c: Client, d: dict, model: str, spec: dict, side: int, args, check, httpx, openai, report: dict,
             save, key: dict | None = None) -> None:
    """Every check on one ready deployment `d` served as `model`. `key`: an API key to use (as POST
    /api/keys answers it); without one a key named accept-img-key is created (removed by cleanup)."""
    k = key or c.ok("POST", "/api/keys", {"name": f"{PREFIX}key"})
    gw = k["gateway_url"]
    http = httpx.Client(verify=accept_m3.TLS, timeout=3600) if accept_m3.TLS else httpx.Client(timeout=3600)
    sdk = openai.OpenAI(base_url=gw, api_key=k["key"], max_retries=0, timeout=3600, http_client=http)
    prefix = (k.get("api_key") or k).get("prefix") or k["key"][:12]
    before = usage_row(c, prefix)
    made = {"images": 0, "kilopixels": 0}
    info = report["deployments"][model]
    params = d.get("params") or {}
    most = int(params.get("max_images") or 1)
    size = f"{side}x{side}"
    firsts: list[bytes] = []

    def generate(label: str, prompt: str, n: int, sz: str, **extra) -> list[bytes] | None:
        t0 = time.time()
        try:
            out = sdk.images.generate(model=model, prompt=prompt, n=n, size=sz, extra_body=extra or None)
        except openai.APIStatusError as e:
            check(f"{model}: {label}", False, f"{e.status_code}: {str(e)[:200]}")
            return None
        secs = time.time() - t0
        images, problems = [], []
        w, h = (int(x) for x in sz.split("x"))
        for i, item in enumerate(out.data or []):
            data = base64.b64decode(item.b64_json or "")
            try:
                got = decode_image(data)
                if got[:2] != (w, h):
                    problems.append(f"image {i} is {got[0]}x{got[1]}")
            except ValueError as e:
                problems.append(f"image {i}: {e}")
            images.append(data)
            if save:
                (save / f"{model}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:40]}-{i}.png").write_bytes(data)
        made["images"] += len(images)
        made["kilopixels"] += len(images) * round(w * h / 1000)
        ok = len(images) == n and not problems
        check(f"{model}: {label}", ok, f"{len(images)} of {n} at {sz} decode as PNG in {secs:.1f} s "
                                       f"({secs / max(1, len(images)):.1f} s an image)" + (f"; {problems}" if problems else ""))
        info[f"seconds_{re.sub(r'[^a-z0-9]+', '_', label.lower())[:30]}"] = round(secs, 1)
        return images

    # ---- one image at the model's own size, then two in one request, then the largest allowed
    one = generate(f"one image at {size}", PROMPT, 1, size)
    if one:
        firsts.append(one[0])
        info["seconds_per_image"] = info.get("seconds_one_image_at_" + size.replace("x", "x"), None)
    two = generate("two images in one request", TWO, 2, size)
    if two and len(two) == 2:
        check(f"{model}: the two images differ", distinct(two[0], two[1]), f"{len(two[0])} and {len(two[1])} bytes")
    if most > 2 or side != int(spec["size"]):
        generate(f"the largest request ({most} at {size})", PROMPT, most, size)

    # ---- refusals
    def refused(label: str, body: dict, code: str, path: str = "images/generations") -> None:
        s, text = raw_post(f"{gw}/{path}", json.dumps({"model": model, **body}).encode(), "application/json",
                           {"Authorization": f"Bearer {k['key']}"}, timeout=120)
        check(f"{model}: {label}", s in (400, 404) and f'"{code}"' in text, f"{s} {text[:140]}")

    try:
        sdk.images.generate(model=model, prompt=PROMPT, response_format="url", size=size)
        check(f"{model}: response_format url is refused", False, "it was answered")
    except openai.BadRequestError as e:
        check(f"{model}: response_format url is refused", getattr(e, "code", None) == "response_format_not_supported",
              f"400 {getattr(e, 'code', None)}: {str(e)[:120]}")
    refused(f"more images than the cap ({most}) are refused", {"prompt": PROMPT, "n": most + 1}, "too_many_images")
    refused(f"a side over the cap ({side}) is refused", {"prompt": PROMPT, "size": f"{side * 2}x{side * 2}"},
            "image_too_large")
    refused("more steps than the cap are refused", {"prompt": PROMPT, "num_inference_steps": 1000}, "too_many_steps")
    refused("an empty prompt is refused", {"prompt": "  "}, "invalid_request")
    refused("engine settings in the prompt are refused",
            {"prompt": 'x <sd_cpp_extra_args>{"width": 4096}</sd_cpp_extra_args>'}, "invalid_request")
    refused("image edits are not offered", {"prompt": PROMPT}, "endpoint_not_supported", path="images/edits")

    # ---- the replica's own port without its internal key
    for e in d["endpoints"]:
        s, text = raw_post(f"{e['url']}/v1/images/generations", b'{"prompt":"hi"}', "application/json", timeout=20)
        check(f"{model}: replica {e['replica']} on {e['machine']} without the key is refused", s in (401, 403),
              f"{s} {text[:80]}")

    # ---- the queue: two at once on a deployment that makes one at a time
    results: dict = {}

    def one_more(tag: str) -> None:
        t0 = time.time()
        try:
            out = sdk.images.generate(model=model, prompt=f"{PROMPT}, {tag}", n=1, size=size)
            results[tag] = (len(out.data or []), time.time() - t0)
        except openai.APIStatusError as e:
            results[tag] = (f"{e.status_code}", time.time() - t0)

    threads = [threading.Thread(target=one_more, args=(t,)) for t in ("first", "second")]
    for t in threads:
        t.start()
        time.sleep(0.3)
    for t in threads:
        t.join()
    served = [v for v in results.values() if v[0] == 1]
    made["images"] += len(served)
    made["kilopixels"] += len(served) * round(side * side / 1000)
    slow, fast = max(v[1] for v in results.values()), min(v[1] for v in results.values())
    check(f"{model}: two requests at once wait in the queue and are both answered",
          len(served) == 2 and slow > 1.5 * fast, f"answers {results}; the second took {slow:.1f} s, the first {fast:.1f} s")
    info["queue"] = {k: [v[0], round(v[1], 1)] for k, v in results.items()}

    # ---- usage
    after = usage_row(c, prefix)
    images = after.get("images", 0) - before.get("images", 0)
    kp = after.get("kilopixels", 0) - before.get("kilopixels", 0)
    busy = after.get("busy_ms", 0) - before.get("busy_ms", 0)
    check(f"{model}: usage counts the images", images == made["images"], f"{images} counted, {made['images']} made")
    check(f"{model}: usage counts their kilopixels", kp == made["kilopixels"], f"{kp} counted, {made['kilopixels']} made")
    check(f"{model}: usage records the busy time", busy > 0, f"{busy / 1000:.1f} s busy")
    info.update(images=images, kilopixels=kp, busy_seconds=round(busy / 1000, 1))

    # ---- what it shows
    vision = None
    if args.vision != "none":
        # A chat deployment whose model reads images, with a ready replica.
        models = {x["name"]: x for x in c.ok("GET", "/api/models")["models"]}
        ready = {r["served_name"] for r in c.ok("GET", "/api/routes")["routes"]
                 if r.get("api") == "openai-chat" and r.get("endpoints")}
        chat = [dep["served_name"] for dep in c.ok("GET", "/api/deployments")["deployments"]
                if dep["served_name"] in ready and "image" in (models.get((dep.get("model") or {}).get("name"), {}).get("modalities") or [])]
        vision = chat[0] if args.vision == "auto" and chat else (args.vision if args.vision != "auto" else None)
    if not firsts:
        return
    if vision is None:
        check(f"{model}: the image shows the prompt's subject", True, "no chat model that reads images: --vision",
              skip=True)
        return
    url = "data:image/png;base64," + base64.b64encode(firsts[0]).decode()
    try:
        out = sdk.chat.completions.create(model=vision, max_tokens=60, temperature=0, messages=[
            {"role": "user", "content": [{"type": "text", "text": "In a few words: what is in this picture?"},
                                         {"type": "image_url", "image_url": {"url": url}}]}],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        said = (out.choices[0].message.content or "").strip()
    except openai.APIStatusError as e:
        check(f"{model}: the image shows the prompt's subject ({vision})", False, f"{e.status_code}: {str(e)[:160]}")
        return
    info["vision"] = said[:200]
    check(f"{model}: the image shows the prompt's subject ({vision})", any(w in said.lower() for w in SUBJECT),
          f"{vision} says: {said[:120]!r}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        log(f"STOPPED: {e}")
        sys.exit(2)
