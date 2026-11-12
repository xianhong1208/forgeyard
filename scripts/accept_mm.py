#!/usr/bin/env python3
"""1.3 acceptance on real machines: images (and, opt-in, audio and video) in chat through the gateway,
called with the official OpenAI Python SDK.

Through Forgeyard's API only (an admin session), for each model of --only, on --machine:

  1. adds the model to the catalog unless it is there (GGUF models with their projector file), and
     deploys it there as `accept-mm-<model>`, then waits until it is ready. A GPU deployment goes to
     --machine's allowed GPU (or --gpu); the card's memory is read before and after, against the
     estimate;
  2. creates an API key and sends, with the SDK: a generated PNG as a data: URI (a red square: the
     answer should say red); --image-url, fetched by the gateway (default: a JPEG on Hugging Face's
     CDN, behind a redirect); with --audio a generated WAV as input_audio (models that take audio);
     with --video-url a short video (the deployment then has videos 1);
  3. shows the refusals: 127.0.0.1 (the server itself), 169.254.169.254, a name that resolves to a
     private address (10.0.0.1.nip.io), a public redirect to 127.0.0.1 (postman-echo.com), file://,
     a `file` part (PDF), more images than the deployment takes, a body over its max_body_mb, and
     the replica's own port without its internal key;
  4. reads the usage: the media items counted, and the prompt tokens of an image request against
     the same request without it (image tokens are inside prompt_tokens);

then removes what it created (deployments, the catalog entries it added, keys) and prints a report
(also JSON with --out). Everything is named accept-mm-*, and leftovers of an earlier run are removed
first, so it can be run again.

    FORGEYARD_PASSWORD=… python3 scripts/accept_mm.py --server https://<server>:7860 --cacert forgeyard-ca.pem \\
        --user <admin> --machine <machine> --only qwen3-vl-8b,gemma-4-12b-it --audio --out accept-mm.json

Models it knows (--only): qwen3-vl-8b, qwen3-vl-2b (vLLM), gemma-4-12b-it (vLLM, images and audio),
qwen3-vl-8b-gguf, qwen3-vl-2b-gguf (llama.cpp with projector), qwen2.5-omni-3b-gguf (llama.cpp, images and
audio). Every medium is covered by: an image model with --video-url sample, and an audio model with --audio. GGUF models run with --recipe (default
llama-cpp; a custom recipe, such as a Vulkan one, must have `optional_args: [{if_model: mmproj, args:
[--mmproj, "{mmproj_file}"]}]` and `media: {inputs: [image, audio, video]}`) on --device (cpu or gpu).

With Forgeyard's own CA, --cacert is the CA file. The official SDK: as scripts/accept_m4.py, in its own
virtual environment (--venv) when `openai` is not installed.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import math
import os
import struct
import sys
import time
import urllib.parse
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept_m3  # noqa: E402
from accept_m3 import GIB, Client, Fail, log, machine  # noqa: E402
from accept_m4 import ensure_sdk, raw_post, wait_all_ready  # noqa: E402

PREFIX = "accept-mm-"
MODELS = {
    "qwen3-vl-8b": {"source": "hf://Qwen/Qwen3-VL-8B-Instruct", "recipe": "vllm", "device": "gpu",
                    "params": {"context": 16384, "concurrency": 4, "share": "vram"}},
    "qwen3-vl-2b": {"source": "hf://Qwen/Qwen3-VL-2B-Instruct", "recipe": "vllm", "device": "gpu",
                    "params": {"context": 8192, "concurrency": 4, "share": "vram"}},
    "gemma-4-12b-it": {"source": "hf://google/gemma-4-12B-it", "recipe": "vllm", "device": "gpu",
                       "params": {"context": 8192, "concurrency": 4, "share": "vram"}},
    "qwen3-vl-8b-gguf": {"source": "hf://Qwen/Qwen3-VL-8B-Instruct-GGUF", "file": "Qwen3VL-8B-Instruct-Q4_K_M.gguf",
                         "mmproj": "mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf", "recipe": "llama-cpp",
                         "params": {"context": 8192, "concurrency": 1}},
    "qwen3-vl-2b-gguf": {"source": "hf://Qwen/Qwen3-VL-2B-Instruct-GGUF", "file": "Qwen3VL-2B-Instruct-Q4_K_M.gguf",
                         "mmproj": "mmproj-Qwen3VL-2B-Instruct-F16.gguf", "recipe": "llama-cpp",
                         "params": {"context": 4096, "concurrency": 1}},
    # Audio on llama.cpp: a projector with an audio encoder (and vision). Its licence (qwen-research) allows
    # testing, not commercial use, so it is not on the recommended list.
    "qwen2.5-omni-3b-gguf": {"source": "hf://ggml-org/Qwen2.5-Omni-3B-GGUF", "file": "Qwen2.5-Omni-3B-Q4_K_M.gguf",
                             "mmproj": "mmproj-Qwen2.5-Omni-3B-Q8_0.gguf", "recipe": "llama-cpp",
                             "params": {"context": 8192, "concurrency": 1}},
}
DEFAULT_IMAGE_URL = "https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/bee.jpg"
# A 6.5 MB tennis clip (under MEDIA_FETCH_MAX_MB) for --video-url.
SAMPLE_VIDEO_URL = "https://huggingface.co/datasets/hf-internal-testing/fixtures_videos/resolve/main/tennis.mp4"
PRIVATE_REDIRECT = "https://postman-echo.com/redirect-to?url={target}"


# ------------------------------------------------------------------ test media, made here
def png_square(size: int = 256, square: int = 128, rgb=(220, 30, 30)) -> bytes:
    """A white PNG with a coloured square in the middle (no imaging library needed)."""
    lo, hi = (size - square) // 2, (size + square) // 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            row += bytes(rgb) if lo <= x < hi and lo <= y < hi else b"\xff\xff\xff"
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def wav_tone(seconds: float = 1.5, hz: float = 440.0, rate: int = 16000) -> bytes:
    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(n))
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def image_part(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


# ------------------------------------------------------------------ the run
def cleanup(c: Client, added: list[str]) -> None:
    """Remove accept-mm-* deployments and keys (also an earlier run's) and the catalog entries this run added."""
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
                    time.sleep(3)     # its deployment is still being removed
                log(f"cleanup: catalog entry {m['name']} -> {status}")


def ensure(c: Client, name: str, spec: dict) -> bool:
    """Add the model to the catalog unless it is there. True if this run added it."""
    have = {m["name"]: m for m in c.ok("GET", "/api/models")["models"]}
    if name in have:
        m = have[name]
        if spec.get("mmproj") and not m.get("mmproj"):
            raise Fail(f"{name} is in the catalog without a projector file: remove it or add it again with "
                       f"mmproj {spec['mmproj']}")
        return False
    body = {"name": name, "source": spec["source"], "task": "chat", "title": name}
    for k in ("file", "mmproj"):
        if spec.get(k):
            body[k] = spec[k]
    m = c.ok("POST", "/api/models", body, timeout=300)["model"]
    log(f"catalog: added {name} ({m['source']} @ {m['revision'][:12]}, {m['size_bytes'] / GIB:.2f} GiB, takes "
        f"{m.get('modalities')}, licence {m.get('license')})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860")
    ap.add_argument("--cacert", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--machine", required=True, help="the machine to deploy on")
    ap.add_argument("--only", default="qwen3-vl-8b", help=f"comma-separated, of {', '.join(MODELS)}")
    ap.add_argument("--recipe", help="the recipe for GGUF models (default llama-cpp), e.g. a custom Vulkan one")
    ap.add_argument("--device", choices=("cpu", "gpu"), help="for GGUF models (default: gpu when the machine has one)")
    ap.add_argument("--gpu", type=int, help="the GPU index (default: the machine's only allowed GPU)")
    ap.add_argument("--threads", type=int, default=8, help="CPU threads for a GGUF model on the CPU")
    ap.add_argument("--context", type=int, help="the context length instead of the model's default here (a smaller "
                                                "one fits a shared card)")
    ap.add_argument("--concurrency", type=int, help="concurrent requests instead of the model's default here")
    ap.add_argument("--image-url", default=DEFAULT_IMAGE_URL, help="a public image the gateway fetches")
    ap.add_argument("--audio", action="store_true", help="also send audio (models that take it: gemma-4-12b-it)")
    ap.add_argument("--video-url", help="also send this public video (the deployment gets videos 1); "
                    f"'sample' for {SAMPLE_VIDEO_URL}")
    ap.add_argument("--timeout", type=float, default=3600, help="seconds per deployment to become ready")
    ap.add_argument("--venv", default=str(Path.home() / ".cache" / "forgeyard-accept-m4"),
                    help="where to install the openai SDK if it is missing")
    ap.add_argument("--keep", action="store_true", help="leave the deployments running afterwards")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args()
    if args.video_url == "sample":
        args.video_url = SAMPLE_VIDEO_URL
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

    def check(name: str, ok: bool, detail="", info: bool = False) -> None:
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail, "info": info})
        log(("INFO" if info else "PASS" if ok else "FAIL") + f"  {name}" + (f"  ({detail})" if detail else ""))

    def card(index: int) -> dict:
        return next(a for a in machine(c, m["name"])["accelerators"] if a["index"] == index and a["vendor"] != "cpu")

    cleanup(c, [])
    try:
        for name in args.only.split(","):
            spec = dict(MODELS[name])
            gguf = "file" in spec
            recipe = (args.recipe or spec["recipe"]) if gguf else spec["recipe"]
            device = args.device or spec.get("device") or ("gpu" if gpus else "cpu")
            if ensure(c, name, spec):
                added.append(name)
            params = dict(spec["params"])
            params["gpus"] = 1 if device == "gpu" else 0
            if device == "cpu":
                params["threads"] = args.threads
            if args.video_url:
                params["videos"] = 1
                if gguf:
                    # llama.cpp b12242 samples a Qwen3-VL video at the model's own 2 frames a second
                    # (--video-fps no longer applies): the 6-second sample takes ~5,300 tokens.
                    params["context"] = max(int(params.get("context") or 0), 8192)
            if args.context:
                params["context"] = args.context
            if args.concurrency:
                params["concurrency"] = args.concurrency
            dep_name = f"{PREFIX}{name}".replace(".", "-")[:48]   # deployment names take no dots
            body = {"name": dep_name, "model": name, "recipe": recipe, "params": params, "served_name": dep_name,
                    "replicas": 1, "machine_id": m["id"]}
            est = c.ok("POST", "/api/deployments/estimate", body)
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
            info = {"recipe": recipe, "device": device, "ready_seconds": int(time.time() - t0),
                    "estimate_gib": round(dep["estimate"]["amount"] / GIB, 2),
                    "estimate_parts": {p["label"]: round(p["bytes"] / GIB, 3) for p in dep["estimate"]["parts"]}}
            if index is not None:
                after = accept_m3.settle_card(c, m["name"], card(index)["key"])
                used = (after["vram_total"] - after["vram_free"]) - (before["vram_total"] - before["vram_free"])
                info["measured_gib"] = round(used / GIB, 2)
                check(f"{dep_name}: memory on GPU #{index} within the estimate", used <= dep["estimate"]["amount"],
                      f"measured {used / GIB:.2f} GiB (card used before/after; other work on the card adds noise), "
                      f"estimate {dep['estimate']['amount'] / GIB:.2f} GiB", info=used > dep["estimate"]["amount"])
            report["deployments"][dep_name] = info
            exercise(c, d, dep_name, args, check, httpx, openai, report)
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
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 1 if failed else 0


def exercise(c: Client, d: dict, model: str, args, check, httpx, openai, report: dict) -> None:
    k = c.ok("POST", "/api/keys", {"name": f"{PREFIX}key"})
    gw = k["gateway_url"]
    http = httpx.Client(verify=accept_m3.TLS, timeout=600) if accept_m3.TLS else None
    sdk = openai.OpenAI(base_url=gw, api_key=k["key"], max_retries=0, timeout=600, http_client=http)
    no_think = {"chat_template_kwargs": {"enable_thinking": False}}
    question = {"type": "text", "text": "What colour is the square in the middle of this picture? One word."}

    def ask(*parts, max_tokens: int = 24):
        return sdk.chat.completions.create(model=model, max_tokens=max_tokens, temperature=0, extra_body=no_think,
                                           messages=[{"role": "user", "content": [question, *parts]}])

    def refused(label: str, parts: list, status: int, codes: tuple, info: bool = False) -> None:
        body = json.dumps({"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": [question, *parts]}]})
        s, text = raw_post(f"{gw}/chat/completions", body.encode(), "application/json",
                           {"Authorization": f"Bearer {k['key']}"}, timeout=120)
        try:
            code = json.loads(text)["error"]["code"]
        except (ValueError, KeyError, TypeError):
            code = None
        check(f"{model}: {label}", s == status and code in codes, f"{s} {code}: {text[:160]}", info=info)

    # ---- an image as a data: URI
    square = data_uri("image/png", png_square())
    t0 = time.time()
    out = ask(image_part(square))
    text = (out.choices[0].message.content or "").strip()
    check(f"{model}: image as a data: URI (SDK)", "red" in text.lower(),
          f"{time.time() - t0:.1f} s, {out.usage.prompt_tokens}+{out.usage.completion_tokens} tokens: {text[:80]!r}")
    plain = sdk.chat.completions.create(model=model, max_tokens=8, temperature=0, extra_body=no_think,
                                        messages=[{"role": "user", "content": [question]}])
    image_tokens = out.usage.prompt_tokens - plain.usage.prompt_tokens
    check(f"{model}: image tokens are in prompt_tokens", image_tokens > 16,
          f"{out.usage.prompt_tokens} with the image, {plain.usage.prompt_tokens} without: {image_tokens} for the image")
    report["deployments"][model].update(image_seconds=round(time.time() - t0, 1), image_tokens=image_tokens)

    # ---- an image by URL, fetched by the gateway
    t0 = time.time()
    try:
        out = sdk.chat.completions.create(model=model, max_tokens=40, temperature=0, extra_body=no_think, messages=[
            {"role": "user", "content": [{"type": "text", "text": "What is in this picture? One short sentence."},
                                         image_part(args.image_url)]}])
        text = (out.choices[0].message.content or "").strip()
        check(f"{model}: image by URL, fetched by the gateway", bool(text),
              f"{time.time() - t0:.1f} s, {out.usage.prompt_tokens} prompt tokens: {text[:100]!r}")
    except openai.APIStatusError as e:
        check(f"{model}: image by URL, fetched by the gateway", False, f"{e.status_code}: {str(e)[:200]}")

    # ---- audio and video, when asked
    if args.audio:
        try:
            out = sdk.chat.completions.create(model=model, max_tokens=40, temperature=0, extra_body=no_think, messages=[
                {"role": "user", "content": [{"type": "text", "text": "Describe this sound in a few words."},
                                             {"type": "input_audio", "input_audio": {
                                                 "data": base64.b64encode(wav_tone()).decode(), "format": "wav"}}]}])
            check(f"{model}: audio (input_audio)", bool(out.choices[0].message.content),
                  f"{out.usage.prompt_tokens} prompt tokens: {(out.choices[0].message.content or '')[:80]!r}")
        except openai.APIStatusError as e:
            check(f"{model}: audio (input_audio)", False, f"{e.status_code}: {str(e)[:200]}")
    if args.video_url:
        try:
            out = sdk.chat.completions.create(model=model, max_tokens=40, temperature=0, extra_body=no_think, messages=[
                {"role": "user", "content": [{"type": "text", "text": "What happens in this video? One sentence."},
                                             {"type": "video_url", "video_url": {"url": args.video_url}}]}])
            check(f"{model}: video by URL", bool(out.choices[0].message.content),
                  f"{out.usage.prompt_tokens} prompt tokens: {(out.choices[0].message.content or '')[:80]!r}")
        except openai.APIStatusError as e:
            check(f"{model}: video by URL", False, f"{e.status_code}: {str(e)[:200]}")

    # ---- refusals: nothing of these reaches the engine
    server = urllib.parse.urlsplit(args.server)
    own = f"http://127.0.0.1:{server.port or 443}/api/health"
    refused("the server itself (127.0.0.1) is refused", [image_part(own)], 400, ("media_url_refused",))
    refused("the metadata address is refused", [image_part("http://169.254.169.254/latest/meta-data/")], 400,
            ("media_url_refused",))
    refused("a name that resolves to a private address is refused", [image_part("http://10.0.0.1.nip.io/a.png")], 400,
            ("media_url_refused",))
    refused("a public redirect to 127.0.0.1 is refused at the redirect",
            [image_part(PRIVATE_REDIRECT.format(target=urllib.parse.quote(own, safe="")))], 400,
            ("media_url_refused",))
    refused("file:// is refused", [image_part("file:///etc/passwd")], 400, ("media_url_refused",))
    refused("a file part (PDF) is refused", [{"type": "file", "file": {"filename": "a.pdf",
                                                                        "file_data": "data:application/pdf;base64,JVBERi0xLjQK"}}],
            400, ("file_input_not_supported",))
    images = int(d["params"].get("images", 4))
    refused(f"more than {images} images are refused", [image_part(square)] * (images + 1), 400, ("too_many_media_items",))
    limit = int(d["params"].get("max_body_mb", 64))
    big = "data:image/png;base64," + "A" * ((limit + 2) * 1024**2)
    refused(f"a body over {limit} MiB is refused", [image_part(big)], 413, ("request_too_large",))

    # ---- the replica's own port without its internal key
    for e in d["endpoints"]:
        s, text = raw_post(f"{e['url']}/v1/chat/completions", b"{}", "application/json", timeout=20)
        check(f"{model}: replica {e['replica']} on {e['machine']} without the key is refused", s == 401,
              f"{s} {text[:80]}")

    # ---- usage
    c.ok("GET", "/api/usage?everyone=true&group=model")   # writes what is counted in memory first
    time.sleep(1)
    rows = {r["id"]: r for r in c.ok("GET", "/api/usage?everyone=true&group=model")["rows"]}
    counted = rows.get(model, {}).get("media_items", 0)
    sent = 2 + (1 if args.audio else 0) + (1 if args.video_url else 0)
    check(f"{model}: usage counts the media items", counted >= sent, f"{counted} counted, {sent} answered")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        log(f"STOPPED: {e}")
        sys.exit(2)
