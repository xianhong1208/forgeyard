"""Image generation (1.3): /v1/images/generations through the gateway with two engines (vLLM-Omni for
diffusers pipelines, stable-diffusion.cpp's sd-server for GGUF and single files), checked against fake
engines: the request checks (url refused, no streaming), each deployment's caps (images per request,
size, steps, prompt), only known fields reaching the engine, the queue in the gateway, the answer in
OpenAI's shape, images / kilopixels / busy milliseconds in usage, keys, allowlists and rpm, no cloud;
the recipes, their rendering and estimate, the inspector, the recommended list, the agent's .zip
engines and the Playground's options (FY-IGEN-*)."""

from __future__ import annotations

import base64
import json
import os
import stat
import struct
import threading
import time
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Optional

import httpx
import pytest

from forgeyard.db import Deployment, new_session
from forgeyard.gateway import imagegen
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.usage import recorder
from forgeyard.models import recipes, recommended as rec, render, sources
from forgeyard.models.estimate import estimate
from forgeyard.models.recipes import variant_matches
from tests.cloud_helpers import FakeCloud, add_provider, deployment_id, model_id, set_tiers
from tests.gateway_helpers import LiveServer, api_client, make_ready, new_key
from tests.model_helpers import add_catalog
from tests.test_scheduler import MODELS, deploy, replica_row
from tests.workload_helpers import make_node

FEATURES = MODELS + ("replica_keys", "replica_guard", "engine_zip")
# The stable-diffusion.cpp CUDA build accepted on an A100 (3bad892, master-1094, CUDA 12.6; #43).
SD_CPP_CUDA = ("ghcr.io/leejet/stable-diffusion.cpp:master-cuda"
               "@sha256:34f70088fe3902d985d5fffa9a947d94ab67c62b8afbcc7bd0448bea3c9dd83b")
GIB = 1024**3
MIB = 1024**2


# ------------------------------------------------------------------ images, made and read here
def png(w: int, h: int, shade: int = 128) -> bytes:
    """A real grayscale PNG of w x h (zlib and CRCs included)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes([shade]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def png_decodes(data: bytes) -> tuple[int, int]:
    """(width, height) of a PNG that really decodes: every chunk's CRC, the pixel data inflated to its
    size (the same check as scripts/accept_img.py)."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("accept_img", Path(__file__).resolve().parent.parent / "scripts" /
                                                  "accept_img.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    w, h, fmt = mod.decode_image(data)
    assert fmt == "png"
    return w, h


class FakeImages:
    """An image engine at http://127.0.0.1:<port>: POST /v1/images/generations answers `n` PNGs of the
    size asked (its default when none), in vLLM-Omni's shape (`style` fields: url and revised_prompt,
    stage timings) or sd-server's (generation_info per image). `hold` seconds per request; counts how
    many it works on at once. Every path needs the replica key."""

    def __init__(self, secret: str = "img-secret", *, style: str = "fields", default: tuple = (512, 512),
                 hold: float = 0.0):
        self.secret, self.style, self.default, self.hold = secret, style, default, hold
        self.status: Optional[int] = None
        self.error_body: Optional[bytes] = None
        self.empty = False                  # answer 200 with no images
        self.seen: list[dict] = []
        self.inflight = 0
        self.most = 0
        self._lock = threading.Lock()
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body: bytes, ctype: str = "application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                if self.path in ("/health", "/v1/models"):
                    return self._send(200, b'{"data": []}')
                self._send(404, b"{}")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if engine.secret and self.headers.get("Authorization") != f"Bearer {engine.secret}":
                    return self._send(401, b'{"error":"Unauthorized"}')
                body = json.loads(raw or b"{}")
                engine.seen.append({"path": self.path, "body": body})
                with engine._lock:
                    engine.inflight += 1
                    engine.most = max(engine.most, engine.inflight)
                try:
                    if engine.hold:
                        time.sleep(engine.hold)
                    if engine.status:
                        return self._send(engine.status, engine.error_body or b'{"error":"engine says no"}')
                    if engine.empty:
                        return self._send(200, b'{"created": 1, "data": []}')
                    w, h = engine.default
                    if body.get("size"):
                        w, h = (int(x) for x in body["size"].split("x"))
                    items = []
                    for i in range(int(body.get("n") or 1)):
                        b64 = base64.b64encode(png(w, h, 40 + i)).decode()
                        if engine.style == "sd-cpp":
                            items.append({"b64_json": b64, "generation_info": {"prompt": body.get("prompt"),
                                                                               "model": "/models/hub/x/model.gguf"}})
                        else:
                            items.append({"b64_json": b64, "url": None, "revised_prompt": None})
                    out = {"created": 1700000000, "data": items, "output_format": "png"}
                    if engine.style != "sd-cpp":
                        out.update(stage_durations={"denoise": 1.0}, peak_memory_mb=1234)
                    self._send(200, json.dumps(out).encode())
                finally:
                    with engine._lock:
                        engine.inflight -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def bodies(self) -> list[dict]:
        return [s["body"] for s in self.seen if s["path"] == "/v1/images/generations"]


def zimage_meta(**over) -> dict:
    """Tongyi-MAI/Z-Image-Turbo as the inspector reads it (its transformer is stored in float32)."""
    meta = {"kind": "hf", "repo": "Tongyi-MAI/Z-Image-Turbo", "ref": "main", "revision": "f" * 40, "format": "diffusers",
            "license": "apache-2.0", "modalities": [], "task_hint": "image",
            "diffusers": {"pipeline": "ZImagePipeline", "components": {
                "text_encoder": {"class": "Qwen3Model", "params": 4_022_468_096, "bytes": 8_044_936_192, "dtype": "BF16"},
                "transformer": {"class": "ZImageTransformer2DModel", "params": 6_154_908_736, "bytes": 24_619_634_944,
                                "dtype": "F32"},
                "vae": {"class": "AutoencoderKL", "params": 83_819_683, "bytes": 167_639_366, "dtype": "BF16"}}},
            "image": {"size": 1024, "multiple": 16},
            "files": [{"path": "model_index.json", "size": 400, "oid": "a" * 40},
                      {"path": "transformer/diffusion_pytorch_model-00001-of-00003.safetensors", "size": 9_973_000_000,
                       "sha256": "b" * 64}],
            "size_bytes": 32_850_000_000}
    meta.update(over)
    return meta


def sd15_meta() -> dict:
    return {"kind": "hf", "repo": "second-state/stable-diffusion-v1-5-GGUF", "ref": "main", "revision": "0" * 40,
            "format": "gguf", "file": "stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf", "license": "creativeml-openrail-m",
            "modalities": [], "task_hint": "image",
            "image": {"size": 512, "multiple": 64, "components": {}, "single": True, "bytes": {"model": 1_763_578_176}},
            "files": [{"path": "stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf", "size": 1_763_578_176, "sha256": "c" * 64}],
            "size_bytes": 1_763_578_176}


def zgguf_meta() -> dict:
    comps = {"llm": "qwen_3_4b-Q8_0.gguf", "vae": "ae_bf16.safetensors"}
    return {"kind": "hf", "repo": "wbruna/Z-Image-Turbo-sdcpp-GGUF", "ref": "main", "revision": "e" * 40, "format": "gguf",
            "file": "z_image_turbo-Q8_0.gguf", "license": "apache-2.0", "modalities": [], "task_hint": "image",
            "image": {"size": 1024, "multiple": 16, "components": comps, "single": False,
                      "bytes": {"model": 6_541_971_552, "llm": 4_273_904_096, "vae": 167_666_902}},
            "files": [{"path": "z_image_turbo-Q8_0.gguf", "size": 6_541_971_552, "sha256": "d" * 64},
                      {"path": "qwen_3_4b-Q8_0.gguf", "size": 4_273_904_096, "sha256": "e" * 64},
                      {"path": "ae_bf16.safetensors", "size": 167_666_902, "sha256": "f" * 64}],
            "size_bytes": 10_983_542_550}


@pytest.fixture()
def img(owner):
    """Two image deployments on fake engines: `omni` (vllm-omni-image, Z-Image-Turbo, on the A100's card
    #4, at most 4 images up to 1024x1024) and `sd` (sd-cpp, Stable Diffusion 1.5 GGUF, on a CPU machine:
    its own size 512, so at most 512x512, sizes a multiple of 64); a key."""
    add_catalog("z-image-turbo", zimage_meta(), task="image", fmt="diffusers", source="hf://Tongyi-MAI/Z-Image-Turbo")
    add_catalog("sd15", sd15_meta(), task="image", fmt="gguf", source="hf://second-state/stable-diffusion-v1-5-GGUF",
                engine_args={"sd-cpp": ["--steps", "20", "--cfg-scale", "7"]})
    add_catalog("z-gguf", zgguf_meta(), task="image", fmt="gguf", source="hf://wbruna/Z-Image-Turbo-sdcpp-GGUF")
    cpu = make_node("cpu-a", features=FEATURES)
    gpu = make_node("a100", allowlist=[4], features=FEATURES)
    engines = {"omni": FakeImages("s-omni", default=(1024, 1024)), "sd": FakeImages("s-sd", style="sd-cpp")}
    deploy(owner, "omni", "z-image-turbo", "vllm-omni-image", wait=True)
    deploy(owner, "sd", "sd15", "sd-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    make_ready("omni", [(gpu, engines["omni"].port, engines["omni"].secret)])
    make_ready("sd", [(cpu, engines["sd"].port, engines["sd"].secret)])
    key = new_key(owner, "laptop")["key"]
    imagegen.forget()
    ns = SimpleNamespace(owner=owner, cpu=cpu, gpu=gpu, engines=engines, key=key, api=api_client(owner, key))
    yield ns
    for e in engines.values():
        e.close()
    imagegen.forget()


def gen(c, model: str, prompt: str = "A lighthouse at dusk.", **extra):
    return c.post("/v1/images/generations", json={"model": model, "prompt": prompt, **extra})


def err(r) -> dict:
    body = r.json()
    assert set(body) == {"error"}, body
    return body["error"]


def usage_total(owner) -> dict:
    recorder.flush()
    return owner.get("/api/usage?everyone=true").json()["total"]


# ------------------------------------------------------------------ recipes
def test_fy_igen_001_two_engines_serve_images():
    """FY-IGEN-001: vllm-omni-image (NVIDIA, diffusers) and sd-cpp (CUDA for NVIDIA with driver 560+, pinned
    to build 3bad892 since the first CUDA pin crashed on an A100, #43; Vulkan for AMD; a CPU process from
    a .zip release) serve the image task as openai-images, so an NVIDIA card is offered both and an AMD one
    sd-cpp; the image settings are checked when a recipe is read."""
    b = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
    omni, sd = b["vllm-omni-image"], b["sd-cpp"]
    assert omni.api == {"image": "openai-images"} and omni.formats == ["diffusers"] and omni.estimate.method == "diffusers"
    assert omni.image.request == "fields" and omni.estimate.cast_16bit and omni.estimate.parallel
    assert omni.variants[0].image == b["vllm-omni-tts"].variants[0].image and omni.api_key_env == "VLLM_API_KEY"
    assert sd.image.request == "sd-cpp" and set(sd.formats) == {"gguf", "safetensors"} and not sd.estimate.parallel
    assert [v.name for v in sd.variants] == ["cuda", "vulkan", "cpu-process"]
    assert sd.variant("vulkan").amd_kfd is False and sd.variant("vulkan").when.vendor == "amd"
    assert sd.variant("vulkan").image.startswith("ghcr.io/leejet/stable-diffusion.cpp:master-vulkan@sha256:")
    cuda = sd.variant("cuda")
    assert cuda.image == SD_CPP_CUDA and cuda.runtime == "container" and cuda.entrypoint == ["/sd-server"]
    assert cuda.args[-3:] == ["--eager-load", "--max-vram", "{gpu_budget_gib}"]
    a100 = {"vendor": "nvidia", "arch": "8.0", "driver": "580.178.04"}
    apu = {"vendor": "amd", "arch": "gfx1151", "driver": "7.0.2"}
    offered = {name: [v.name for v in r.variants if variant_matches(v, a100) is None]
               for name, r in b.items() if "image" in r.tasks}
    assert offered == {"vllm-omni-image": ["cuda"], "sd-cpp": ["cuda"]}
    assert [v.name for v in sd.variants if variant_matches(v, apu) is None] == ["vulkan"]
    # CUDA 12.6 needs driver 560; Maxwell (5.0) is the oldest card the image is built for.
    assert variant_matches(cuda, {**a100, "driver": "550.54.15"}) is not None
    assert variant_matches(cuda, {**a100, "arch": "3.7"}) is not None
    cpu = sd.variant("cpu-process")
    assert cpu.engine.url.endswith("-bin-Linux-Ubuntu-24.04-x86_64.zip") and cpu.engine.binary == "sd-server"
    assert sd.api_key_env is None and sd.health.path == "/v1/models"
    for name in ("vllm-omni-image", "sd-cpp"):
        assert {"max_images", "default_size", "max_side", "max_steps", "queue", "timeout"} <= set(b[name].params)
        assert not b[name].params["max_images"].advanced and b[name].params["queue"].advanced
    good = recipes.builtin_recipes()["vllm-omni-image"][1]
    for bad, why in [(good.replace("api: {image: openai-images}", "api: {image: openai-chat}"), "openai-images"),
                     (good.replace("tasks: [image]", "tasks: [chat]").replace("api: {image: openai-images}",
                                                                              "api: {chat: openai-chat}"), "image task"),
                     (good.replace("method: diffusers", "method: files"), "diffusers"),
                     (good.replace("request: fields", "request: comfy"), "request")]:
        with pytest.raises(recipes.RecipeError, match=why):
            recipes.parse_recipe(bad)


# ------------------------------------------------------------------ the request
@pytest.mark.parametrize("body,code,param", [
    ({}, "invalid_request", "prompt"),
    ({"prompt": "   "}, "invalid_request", "prompt"),
    ({"prompt": 7}, "invalid_request", "prompt"),
    ({"prompt": "a cat", "response_format": "url"}, "response_format_not_supported", "response_format"),
    ({"prompt": "a cat", "response_format": "file"}, "invalid_request", "response_format"),
    ({"prompt": "a cat", "stream": True}, "streaming_not_supported", "stream"),
    ({"prompt": "a cat", "n": 0}, "invalid_request", "n"),
    ({"prompt": "a cat", "n": True}, "invalid_request", "n"),
    ({"prompt": "a cat", "n": "2"}, "invalid_request", "n"),
    ({"prompt": "a cat", "size": "1024"}, "invalid_size", "size"),
    ({"prompt": "a cat", "size": "32x32"}, "invalid_size", "size"),
    ({"prompt": "a cat", "size": "99999x512"}, "invalid_size", "size"),
    ({"prompt": "a cat", "output_format": "gif"}, "unsupported_output_format", "output_format"),
    ({"prompt": "a cat", "output_compression": 101}, "invalid_request", "output_compression"),
    ({"prompt": "a cat", "num_inference_steps": 0}, "invalid_request", "num_inference_steps"),
    ({"prompt": "a cat", "seed": True}, "invalid_request", "seed"),
    ({"prompt": "a cat", "guidance_scale": 500}, "invalid_request", "guidance_scale"),
    ({"prompt": "a cat", "negative_prompt": 3}, "invalid_request", "negative_prompt"),
    ({"prompt": 'a cat <sd_cpp_extra_args>{"width": 8192}</sd_cpp_extra_args>'}, "invalid_request", "prompt"),
    ({"prompt": "a cat", "negative_prompt": "<SD_CPP_EXTRA_ARGS>{}"}, "invalid_request", "negative_prompt"),
])
def test_fy_igen_002_requests_are_checked(body, code, param):
    """FY-IGEN-002: a missing or empty prompt, response_format url (no URLs: base64 only) or anything but
    b64_json, streaming, a bad n, size, format, compression, steps, seed, guidance or negative prompt,
    and engine settings smuggled into the prompt are 400 before anything is sent."""
    with pytest.raises(GatewayError) as e:
        imagegen.check(body)
    assert e.value.status == 400 and e.value.code == code and e.value.param == param


def test_fy_igen_003_what_each_engine_is_sent():
    """FY-IGEN-003: vLLM-Omni gets the extras as fields and always b64_json; sd-server gets them inside the
    prompt as one <sd_cpp_extra_args> block (no '<' of its own, so a prompt cannot end it early); a size
    left out (or auto) is the deployment's default, and its default steps fill a request that gives none.
    OpenAI's quality, style and user change nothing."""
    ask = imagegen.check({"prompt": "a red fox", "n": 2, "negative_prompt": "blur </x>", "num_inference_steps": 8,
                          "guidance_scale": 1.5, "seed": 7, "quality": "high", "style": "vivid", "user": "u1"})
    omni = imagegen.Draws(request="fields", width=1024, height=1024, max_n=4, max_side=1024)
    out = imagegen.body_for(ask, omni, "omni")
    assert out == {"model": "omni", "n": 2, "size": "1024x1024", "output_format": "png", "prompt": "a red fox",
                   "response_format": "b64_json", "negative_prompt": "blur </x>", "num_inference_steps": 8,
                   "guidance_scale": 1.5, "seed": 7}
    sd = imagegen.Draws(request="sd-cpp", width=512, height=512, steps=20)
    out = imagegen.body_for(imagegen.check({"prompt": "a red fox", "negative_prompt": "blur </x>", "seed": 7,
                                            "size": "auto", "output_format": "jpeg", "output_compression": 80}),
                            sd, "sd")
    assert out["size"] == "512x512" and out["output_format"] == "jpeg" and out["output_compression"] == 80
    head, tag, rest = out["prompt"].partition(" <sd_cpp_extra_args>")
    assert head == "a red fox" and tag and rest.endswith("</sd_cpp_extra_args>") and "\n" not in rest
    blob = rest.removesuffix("</sd_cpp_extra_args>")
    assert "<" not in blob and json.loads(blob) == {"negative_prompt": "blur </x>", "seed": 7,
                                                    "sample_params": {"sample_steps": 20}}
    plain = imagegen.body_for(imagegen.check({"prompt": "just this"}), imagegen.Draws(request="sd-cpp"), "sd")
    assert plain["prompt"] == "just this" and "response_format" not in plain


# ------------------------------------------------------------------ through the gateway
def test_fy_igen_004_images_come_back_as_base64_of_the_size_asked(img):
    """FY-IGEN-004: through /v1 each engine answers with images that decode at the size asked, in OpenAI's
    shape (created, data[].b64_json, output_format, size), without the engines' extras; the answer says
    who made it; usage counts the images, their kilopixels and the replica's busy milliseconds."""
    img.engines["omni"].hold = img.engines["sd"].hold = 0.06
    r = gen(img.api, "omni", n=2, size="768x512")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"created", "data", "output_format", "size"} and body["size"] == "768x512"
    assert [set(d) for d in body["data"]] == [{"b64_json"}, {"b64_json"}]
    assert all(png_decodes(base64.b64decode(d["b64_json"])) == (768, 512) for d in body["data"])
    assert r.headers["X-Forgeyard-Target"] == "local:omni#0"
    r = gen(img.api, "sd", "An apple.")
    assert r.status_code == 200, r.text
    [item] = r.json()["data"]
    assert set(item) == {"b64_json"} and png_decodes(base64.b64decode(item["b64_json"])) == (512, 512)
    [b] = img.engines["sd"].bodies()
    assert b["size"] == "512x512" and b["n"] == 1
    total = usage_total(img.owner)
    assert total["images"] == 3 and total["kilopixels"] == 2 * round(768 * 512 / 1000) + round(512 * 512 / 1000)
    # Busy time: the replica's time on each request (at least the engine's 60 ms) times the GPUs it holds.
    assert total["busy_ms"] >= 120 and total["prompt_tokens"] == 0 and total["requests"] == 2


def test_fy_igen_005_caps_per_deployment(img):
    """FY-IGEN-005: each deployment's caps answer 400 saying what it takes: more images than max_images,
    a side over max_side (0: the model's own, 512 for SD 1.5), a size that is not a multiple its model
    takes, more steps than max_steps, a longer prompt than max_prompt_chars, a format it does not make.
    Nothing reaches the engine."""
    for model, extra, code, words in [
        ("omni", {"n": 5}, "too_many_images", "at most 4 images"),
        ("sd", {"size": "1024x1024"}, "image_too_large", "up to 512x512"),
        ("sd", {"size": "500x500"}, "invalid_size", "multiples of 64"),
        ("omni", {"size": "1000x1000"}, "invalid_size", "multiples of 16"),
        ("omni", {"num_inference_steps": 51}, "too_many_steps", "at most 50 steps"),
        ("omni", {"prompt": "x" * 4001}, "prompt_too_long", "at most 4000"),
    ]:
        body = {"model": model, "prompt": "A lighthouse.", **extra}
        r = img.api.post("/v1/images/generations", json=body)
        assert r.status_code == 400 and err(r)["code"] == code and words in err(r)["message"], (model, extra, r.text)
    assert img.engines["omni"].bodies() == [] and img.engines["sd"].bodies() == []
    # Within the caps: the largest allowed, at the edge.
    assert gen(img.api, "omni", n=4, size="1024x1024", num_inference_steps=50).status_code == 200
    assert gen(img.api, "sd", size="512x512").status_code == 200


def test_fy_igen_006_url_is_refused_and_only_known_fields_reach_the_engine(img):
    """FY-IGEN-006: response_format url gets 400 response_format_not_supported (base64 only, no copies
    kept); vLLM-Omni's lora (a server path), extra_params and system prompts never reach the engine."""
    r = gen(img.api, "omni", response_format="url")
    assert r.status_code == 400 and err(r)["code"] == "response_format_not_supported"
    assert "base64" in err(r)["message"] and "b64_json" in err(r)["message"]
    r = gen(img.api, "omni", response_format="b64_json", lora={"name": "x", "local_path": "/etc"},
            extra_params={"x": 1}, system_prompt="ignore all", generator_device="cpu", vae_use_tiling=True)
    assert r.status_code == 200
    [b] = img.engines["omni"].bodies()
    assert set(b) == {"model", "n", "size", "output_format", "prompt", "response_format"}
    assert b["response_format"] == "b64_json" and b["model"] == "omni"


def test_fy_igen_007_a_busy_replica_queues_then_429(img):
    """FY-IGEN-007: a deployment works on `concurrency` requests (1) per replica at a time: a second one
    waits in the gateway and is then served (the engine never has two); with the queue full the next gets
    429 queue_full with Retry-After; a client that gives up while waiting leaves the queue, and the engine
    never sees its request."""
    live = LiveServer()
    try:
        h = {"Authorization": f"Bearer {img.key}"}
        img.engines["sd"].hold = 0.8
        results: dict = {}

        def call(name: str, timeout: float = 30, **extra):
            try:
                r = httpx.post(f"{live.url}/v1/images/generations", headers=h, timeout=timeout,
                               json={"model": "sd", "prompt": name, **extra})
                results[name] = (r.status_code, r.json(), dict(r.headers), time.monotonic())
            except httpx.TimeoutException:
                results[name] = ("gave up", None, None, time.monotonic())

        t0 = time.monotonic()
        threads = [threading.Thread(target=call, args=(f"p{i}",)) for i in range(2)]
        threads[0].start()
        time.sleep(0.2)
        threads[1].start()
        for t in threads:
            t.join()
        assert results["p0"][0] == 200 and results["p1"][0] == 200
        assert img.engines["sd"].most == 1 and results["p1"][3] - t0 >= 1.5
        # The queue holds no one now; with queue 0 the second request is turned away at once.
        db = new_session()
        try:
            d = db.query(Deployment).filter_by(name="sd").one()
            d.params = {**d.params, "queue": 0}
            db.commit()
        finally:
            db.close()
        threads = [threading.Thread(target=call, args=(f"q{i}",)) for i in range(2)]
        threads[0].start()
        time.sleep(0.2)
        threads[1].start()
        for t in threads:
            t.join()
        assert results["q0"][0] == 200
        status, body, headers, _ = results["q1"]
        assert status == 429 and body["error"]["code"] == "queue_full" and int(headers["retry-after"]) >= 1
        # Waiting and giving up: the request leaves the queue and never reaches the engine.
        db = new_session()
        try:
            d = db.query(Deployment).filter_by(name="sd").one()
            d.params = {**d.params, "queue": 4}
            db.commit()
        finally:
            db.close()
        seen = len(img.engines["sd"].bodies())
        first = threading.Thread(target=call, args=("r0",))
        first.start()
        time.sleep(0.2)
        call("r1", timeout=0.3)
        first.join()
        assert results["r1"][0] == "gave up" and results["r0"][0] == 200
        deadline = time.monotonic() + 5
        while any(q.waiting for q in imagegen._queues.values()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(q.waiting for q in imagegen._queues.values())
        call("r2")
        assert results["r2"][0] == 200
        prompts = [b["prompt"] for b in img.engines["sd"].bodies()[seen:]]
        assert prompts == ["r0", "r2"]
    finally:
        img.engines["sd"].hold = 0
        live.close()


def test_fy_igen_008_the_queue_is_first_come_first_served():
    """FY-IGEN-008: tickets leave in the order they came; a full queue gives none; one that leaves (gave
    up) makes room; Retry-After grows with those ahead."""
    q = imagegen.Queue()
    assert q.may_try(None)                      # nobody waits: a newcomer takes a free slot
    a, b = q.join(2), q.join(2)
    assert not q.may_try(None) and q.may_try(a) and not q.may_try(b)   # no jumping the queue
    assert q.join(2) is None and q.first(a) and not q.first(b)
    q.leave(a)
    assert q.first(b) and q.join(2) is not None
    imagegen.forget()
    imagegen.note_busy("m#0", 10.0)
    assert imagegen.retry_after("m#0", 0, 1) == 10 and imagegen.retry_after("m#0", 3, 1) == 40
    assert imagegen.retry_after("m#0", 3, 2) == 20 and imagegen.retry_after("x#0", 0, 1) == imagegen.BUSY_DEFAULT


def test_fy_igen_009_engine_errors_in_openais_shape(img):
    """FY-IGEN-009: sd-server's {"error": "…"} and vLLM-Omni's {"detail": …} come back as OpenAI errors with
    the engine's status and message; nothing is counted as made."""
    img.engines["sd"].status, img.engines["sd"].error_body = 400, b'{"error":"invalid params"}'
    r = gen(img.api, "sd")
    assert r.status_code == 400 and err(r)["code"] == "engine_error" and "invalid params" in err(r)["message"]
    img.engines["omni"].status, img.engines["omni"].error_body = 500, b'{"detail":"Image generation failed: OOM"}'
    r = gen(img.api, "omni")
    assert r.status_code == 500 and "OOM" in err(r)["message"] and err(r)["type"] == "upstream_error"
    img.engines["omni"].status = None
    img.engines["omni"].empty = True
    r = gen(img.api, "omni")
    assert r.status_code == 502 and err(r)["code"] == "upstream_error" and "no images" in err(r)["message"]
    total = usage_total(img.owner)
    assert total["images"] == 0 and total["errors"] == 3


def test_fy_igen_010_keys_allowlists_rpm_and_no_cloud(img, monkeypatch):
    """FY-IGEN-010: no key is 401; a key without the model is 403; rpm refuses the next request; an image
    route takes no provider target (422), and a key whose cloud setting is on gets 503 when no replica is
    ready, never a provider. /v1/images/edits and variations are not offered (404)."""
    assert api_client(img.owner).post("/v1/images/generations", json={"model": "sd", "prompt": "x"}).status_code == 401
    narrow = new_key(img.owner, "narrow", models=["omni"])["key"]
    assert err(gen(api_client(img.owner, narrow), "sd"))["code"] == "model_not_allowed"
    rpm = new_key(img.owner, "rpm", rpm=1)["key"]
    assert gen(api_client(img.owner, rpm), "sd").status_code == 200
    r = gen(api_client(img.owner, rpm), "sd")
    assert r.status_code == 429 and err(r)["code"] == "rate_limit_exceeded"
    for path in ("/v1/images/edits", "/v1/images/variations"):
        r = img.api.post(path, json={"model": "sd"})
        assert r.status_code == 404 and err(r)["code"] == "endpoint_not_supported"
    from forgeyard.config import get_settings
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    cloud = FakeCloud("stub")
    try:
        p = add_provider(img.owner, cloud, name="cloudy")
        set_tiers(img.owner, "sd", [[("local", deployment_id(img.owner, "sd"))],
                                    [("provider", model_id(p, "gpt-mini"))]], status=422)
        on = new_key(img.owner, "on", cloud="on")["key"]
        from tests.gateway_helpers import set_state
        set_state("sd", 0, "starting")
        r = gen(api_client(img.owner, on), "sd")
        assert r.status_code == 503 and err(r)["code"] == "no_ready_replica"
        assert not cloud.seen
    finally:
        cloud.close()


def test_fy_igen_011_rendering(img):
    """FY-IGEN-011: vLLM-Omni runs `vllm serve <model> --omni` with the pixel cap of the deployment and VAE
    slicing; sd-server a single checkpoint with -m and a split model with --diffusion-model, --llm and
    --vae, its default size, a random seed, the catalog's sampling settings, and on a GPU --max-vram
    within the reservation, on NVIDIA with the pinned CUDA image and on an AMD card with Vulkan (the
    render node only); the options' flags follow the parameters."""
    spec = replica_row("omni").spec
    args = spec["runtime"]["args"]
    assert spec["runtime"]["command"] == ["vllm", "serve"] and args[:2] == ["${MODEL_PATH}", "--omni"]
    assert args[args.index("--max-generated-image-size") + 1] == str(1024 * 1024) and "--vae-use-slicing" in args
    assert "--enable-cpu-offload" not in args and spec["resources"]["gpus"]["share"] == "vram"
    sd = replica_row("sd").spec
    a = sd["runtime"]["args"]
    assert sd["runtime"]["type"] == "process" and sd["runtime"]["command"] == "sd-server"
    assert a[a.index("-m") + 1] == "${MODEL_FILE}" and "--diffusion-model" not in a
    assert a[a.index("-W") + 1] == "512" and a[a.index("-H") + 1] == "512" and a[a.index("-s") + 1] == "-1"
    assert a[a.index("--listen-ip") + 1] == "${BIND_ADDRESS}" and a[a.index("--listen-port") + 1] == "${PORT}"
    assert a[-4:] == ["--steps", "20", "--cfg-scale", "7"] and sd["resources"]["cpu"] == 2.0
    params = {"offload": "model", "vae_tiling": "on", "default_size": "768x768"}
    # Both machines here are 8x A100: the GGUF goes to an NVIDIA card with the pinned CUDA image.
    dep = deploy(img.owner, "zg", "z-gguf", "sd-cpp", params=params, wait=True)
    assert dep["deployment"]["recipe"] == "sd-cpp"
    row = replica_row("zg")
    z = row.spec
    a = z["runtime"]["args"]
    assert row.node_id in (img.cpu, img.gpu) and [g["vendor"] for g in row.gpus] == ["nvidia"]
    assert z["runtime"]["image"] == SD_CPP_CUDA
    assert z["runtime"]["command"] == ["/sd-server"] and "-m" not in a and "--eager-load" in a
    assert a[a.index("--diffusion-model") + 1] == "${MODEL_FILE}"
    assert a[a.index("--llm") + 1] == "${MODEL_PATH}/qwen_3_4b-Q8_0.gguf"
    assert a[a.index("--vae") + 1] == "${MODEL_PATH}/ae_bf16.safetensors" and "--t5xxl" not in a
    assert a[a.index("-W") + 1] == "768" and "--offload-to-cpu" in a and "--vae-tiling" in a
    budget = float(a[a.index("--max-vram") + 1])
    assert 0 < budget * GIB <= z["resources"]["gpus"]["vram"]
    # On an AMD APU the same model runs with Vulkan, given only the card's render node.
    apu = make_node("strix", "amd-gfx1151-apu", features=FEATURES)
    deploy(img.owner, "za", "z-gguf", "sd-cpp", params=params, machine_id=apu, wait=True)
    row = replica_row("za")
    z = row.spec
    a = z["runtime"]["args"]
    assert row.node_id == apu and [(g["vendor"], g["kfd"]) for g in row.gpus] == [("amd", False)]
    assert z["runtime"]["image"] == recipes.builtin_recipes()["sd-cpp"][0].variant("vulkan").image
    assert z["runtime"]["command"] == ["/sd-server"] and a[a.index("--diffusion-model") + 1] == "${MODEL_FILE}"
    budget = float(a[a.index("--max-vram") + 1])
    assert 0 < budget * GIB <= z["resources"]["gpus"]["vram"]


def test_fy_igen_012_estimates():
    """FY-IGEN-012: the diffusers method counts vLLM-Omni's components in 16 bits (a float32 transformer
    takes half its file), activations for every image a request may ask for at the largest side, one VAE
    decode, the overhead and the CUDA context; model offload keeps only the largest component; sd.cpp's
    files count as stored, one image at a time, in RAM on a CPU; raising the caps raises the estimate."""
    b = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
    omni, sd = b["vllm-omni-image"], b["sd-cpp"]
    base = {"gpus": 1, "max_images": 4, "max_side": 0}
    e = estimate(zimage_meta(), omni, base, device="gpu", gpu_context=400 * MIB)
    parts = {p.label: p.bytes for p in e.parts}
    assert parts["transformer (ZImageTransformer2DModel)"] == 6_154_908_736 * 2
    assert parts["text_encoder (Qwen3Model)"] == 8_044_936_192 and parts["vae (AutoencoderKL)"] == 167_639_366
    mp = 1024 * 1024 / 1e6
    assert parts["Image activations"] == int(omni.estimate.activation_per_mp * mp * 4)
    assert parts["VAE decode"] == int(omni.estimate.vae_per_mp * mp) and parts["CUDA context"] == 400 * MIB
    assert e.per_gpu == sum(parts.values()) and e.method == "diffusers"
    bigger = estimate(zimage_meta(), omni, {**base, "max_side": 1536}, device="gpu", gpu_context=400 * MIB)
    fewer = estimate(zimage_meta(), omni, {**base, "max_images": 1}, device="gpu", gpu_context=400 * MIB)
    assert bigger.per_gpu > e.per_gpu > fewer.per_gpu
    off = estimate(zimage_meta(), omni, {**base, "offload": "model"}, device="gpu", gpu_context=400 * MIB)
    weights = [p for p in off.parts if "(" in p.label]
    assert [p.label for p in weights] == ["transformer (ZImageTransformer2DModel)"] and off.per_gpu < e.per_gpu
    assert any("Model offload" in n for n in off.notes)
    cpu = estimate(sd15_meta(), sd, {"gpus": 0, "max_images": 4, "max_side": 0}, device="cpu", gpu_context=0)
    parts = {p.label: p.bytes for p in cpu.parts}
    assert parts["Checkpoint"] == 1_763_578_176 and cpu.ram == sum(parts.values()) and cpu.per_gpu == 0
    one = 512 * 512 / 1e6
    assert parts["Image activations"] == int(sd.estimate.activation_per_mp * one)   # one at a time, not 4
    split = estimate(zgguf_meta(), sd, {"gpus": 1, "max_images": 2, "max_side": 0}, device="gpu", gpu_context=0)
    labels = [p.label for p in split.parts]
    assert labels[:3] == ["Diffusion model", "Text encoder (LLM)", "VAE"]


def test_fy_igen_013_the_inspector_reads_image_models():
    """FY-IGEN-013: a diffusers repository (model_index.json) is the diffusers format and the image task;
    its components' weights are read from their safetensors headers, one set each (the plain files, not
    the fp16 variants), with nothing from the top level (single-file checkpoints), and the UNet's
    sample_size gives the native size; a GGUF repository for stable-diffusion.cpp gives its model file and
    the text encoder and VAE beside it, by name, or the ones named."""
    from forgeyard.models import fetch, inspect as inspector
    from tests.model_helpers import FakeHF, Repo, safetensors_bytes

    def st(n: int, dtype: str = "F32") -> bytes:
        size = n * (4 if dtype == "F32" else 2)
        return safetensors_bytes({"w": {"dtype": dtype, "shape": [n], "data_offsets": [0, size]}}, b"\0" * size)

    index = {"_class_name": "StableDiffusionXLPipeline", "unet": ["diffusers", "UNet2DConditionModel"],
             "vae": ["diffusers", "AutoencoderKL"], "text_encoder": ["transformers", "CLIPTextModel"],
             "scheduler": ["diffusers", "EulerDiscreteScheduler"], "feature_extractor": [None, None]}
    xl = Repo("acme/xl", "1" * 40, {
        "model_index.json": json.dumps(index).encode(), "unet/config.json": b'{"sample_size": 64}',
        "unet/diffusion_pytorch_model.safetensors": st(64), "unet/diffusion_pytorch_model.fp16.safetensors": st(64, "F16"),
        "vae/diffusion_pytorch_model.safetensors": st(8), "text_encoder/model.safetensors": st(16, "F16"),
        "text_encoder/openvino_model.xml": b"<x/>", "scheduler/scheduler_config.json": b"{}",
        "sd_xl_base_1.0.safetensors": st(80), "vae_1_0/diffusion_pytorch_model.safetensors": st(8)})
    xl.info.update(pipeline_tag="text-to-image", library_name="diffusers")
    gg = Repo("acme/z-gguf", "2" * 40, {"z-Q4_0.gguf": b"g" * 40, "z-Q8_0.gguf": b"g" * 80,
                                        "qwen_3_4b-Q8_0.gguf": b"q" * 30, "ae-f16.gguf": b"a" * 5,
                                        "ae_bf16.safetensors": b"a" * 9, "taef1.safetensors": b"t" * 3,
                                        "README.md": b"x"})
    gg.info.update(pipeline_tag="text-to-image", library_name=None)
    hf = FakeHF(xl, gg)
    try:
        policy = fetch.Policy(allow_private=True)
        m = inspector.inspect_hf("acme/xl", None, None, endpoint=hf.url, policy=policy)
        assert m["format"] == "diffusers" and m["task_hint"] == "image" and m["modalities"] == []
        assert m["image"] == {"size": 512, "multiple": 64} and m["diffusers"]["pipeline"] == "StableDiffusionXLPipeline"
        comps = m["diffusers"]["components"]
        assert set(comps) == {"unet", "vae", "text_encoder"} and comps["unet"] == {
            "class": "UNet2DConditionModel", "params": 64, "bytes": 256, "dtype": "F32"}
        paths = {f["path"] for f in m["files"]}
        assert "unet/diffusion_pytorch_model.safetensors" in paths and "model_index.json" in paths
        assert not paths & {"unet/diffusion_pytorch_model.fp16.safetensors", "sd_xl_base_1.0.safetensors",
                            "vae_1_0/diffusion_pytorch_model.safetensors", "text_encoder/openvino_model.xml"}
        z = inspector.inspect_hf("acme/z-gguf", None, "z-Q4_0.gguf", endpoint=hf.url, policy=policy, task="image")
        assert z["file"] == "z-Q4_0.gguf" and z["image"]["single"] is False and z["image"]["size"] == 1024
        assert z["image"]["components"] == {"llm": "qwen_3_4b-Q8_0.gguf", "vae": "ae_bf16.safetensors"}
        assert {f["path"] for f in z["files"]} == {"z-Q4_0.gguf", "qwen_3_4b-Q8_0.gguf", "ae_bf16.safetensors"}
        assert z["image"]["bytes"] == {"model": 40, "llm": 30, "vae": 9}
        assert [c["path"] for c in z["gguf_files"]] == ["z-Q4_0.gguf", "z-Q8_0.gguf"]
        named = inspector.inspect_hf("acme/z-gguf", None, "z-Q8_0.gguf", endpoint=hf.url, policy=policy, task="image",
                                     components={"vae": "ae-f16.gguf", "llm": "qwen_3_4b-Q8_0.gguf"})
        assert named["image"]["components"]["vae"] == "ae-f16.gguf"
        with pytest.raises(inspector.InspectError, match="not a file"):
            inspector.inspect_hf("acme/z-gguf", None, "z-Q8_0.gguf", endpoint=hf.url, policy=policy, task="image",
                                 components={"vae": "../etc/passwd"})
        with pytest.raises(inspector.InspectError, match="not one of"):
            inspector.inspect_hf("acme/z-gguf", None, "z-Q8_0.gguf", endpoint=hf.url, policy=policy, task="image",
                                 components={"lora": "ae-f16.gguf"})
    finally:
        hf.close()
    assert sources.task_hint("text-to-image", []) == "image"
    assert sources.image_size_guess("stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf") == (512, 64)
    assert sources.image_size_guess("sd_xl_base_1.0.safetensors") == (1024, 64)
    assert sources.image_size_guess("flux1-schnell-Q4_0.gguf") == (1024, 16)
    assert sources.component_role("t5xxl-Q8_0.gguf") == "t5xxl" and sources.component_role("clip_l-Q8_0.gguf") == "clip_l"
    assert sources.component_role("flux1-schnell-Q4_0.gguf") is None


def test_fy_igen_014_adding_an_image_model(owner, monkeypatch):
    """FY-IGEN-014: adding a GGUF repository as an image model reads it as one (its files, its native size),
    with the components the recommended list names for that file; a repository that holds no image model
    is refused for the image task."""
    from forgeyard.config import get_settings
    from tests.model_helpers import FakeHF, Repo

    gg = Repo("second-state/FLUX.1-schnell-GGUF", "8c45a2ba25e2d02bd34230989fb54983f39e44ec",
              {"flux1-schnell-Q4_0.gguf": b"f" * 50, "t5xxl-Q8_0.gguf": b"t" * 20, "t5xxl-Q4_0.gguf": b"t" * 10,
               "clip_l-Q8_0.gguf": b"c" * 5, "ae.safetensors": b"a" * 4, "ae-f16.gguf": b"a" * 2})
    gg.info.update(pipeline_tag="text-to-image", library_name=None)
    hf = FakeHF(gg)
    try:
        monkeypatch.setenv("HF_ENDPOINT", hf.url)
        monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
        get_settings.cache_clear()
        r = owner.post("/api/models", json={"name": "flux-gguf", "source": "hf://second-state/FLUX.1-schnell-GGUF",
                                            "revision": "8c45a2ba25e2d02bd34230989fb54983f39e44ec",
                                            "file": "flux1-schnell-Q4_0.gguf", "task": "image"})
        assert r.status_code == 201, r.text
        m = r.json()["model"]
        assert m["task"] == "image" and m["recipes"] == ["sd-cpp"] and m["image"]["size"] == 1024
        assert m["image"]["components"] == {"t5xxl": "t5xxl-Q8_0.gguf", "clip_l": "clip_l-Q8_0.gguf",
                                            "vae": "ae.safetensors"}
        assert m["size_bytes"] == 50 + 20 + 5 + 4
        r = owner.post("/api/models", json={"name": "not-images", "source": "hf://second-state/FLUX.1-schnell-GGUF",
                                            "file": "flux1-schnell-Q4_0.gguf", "task": "image",
                                            "components": {"vae": "nope.safetensors"}})
        assert r.status_code == 422 and "not a file" in r.json()["detail"]["message"]
    finally:
        hf.close()


def test_fy_igen_015_the_recommended_image_models():
    """FY-IGEN-015: the recommended list has Z-Image-Turbo, SDXL and FLUX.1-schnell for vLLM-Omni and GGUFs
    for stable-diffusion.cpp (one small enough for a CPU), each pinned to a commit with an allowed licence
    (no FLUX.1-dev, SD-Turbo or SD3.5); a split GGUF names its components; an unpinned entry or an unknown
    component role is refused."""
    data = rec.load()
    imgs = {e.name: e for e in data.models if e.task == "image"}
    assert {"z-image-turbo", "sdxl-base-1.0", "flux.1-schnell"} <= {n for n, e in imgs.items() if e.recipe == "vllm-omni-image"}
    assert any(e.recipe == "sd-cpp" and e.device == "cpu" for e in imgs.values())
    assert all(sources.REVISION_RE.match(e.revision or "") and e.license in rec.ALLOWED_LICENSES for e in imgs.values())
    assert not any(k in e.source.lower() for e in imgs.values() for k in ("flux.1-dev", "sd-turbo", "sdxl-turbo", "3.5"))
    assert imgs["z-image-turbo-gguf"].components == {"llm": "qwen_3_4b-Q8_0.gguf", "vae": "ae_bf16.safetensors"}
    assert imgs["z-image-turbo-gguf"].engine_args["sd-cpp"] == ["--cfg-scale", "1", "--steps", "8"]
    raw = json.loads(json.dumps({"groups": [g.model_dump() for g in data.groups],
                                 "models": [e.model_dump() for e in data.models]}))
    first = next(m for m in raw["models"] if m["task"] == "image")
    first["revision"] = "main"
    with pytest.raises(rec.RecommendedError, match="pinned to a commit"):
        rec.check(raw)
    raw = json.loads(json.dumps({"groups": [g.model_dump() for g in data.groups],
                                 "models": [e.model_dump() for e in data.models]}))
    z = next(m for m in raw["models"] if m["name"] == "z-image-turbo-gguf")
    z["components"]["lora"] = "x.safetensors"
    with pytest.raises(rec.RecommendedError, match="components"):
        rec.check(raw)


def test_fy_igen_016_zip_engines_unpack_safely(tmp_path):
    """FY-IGEN-016: the agent unpacks a .zip engine (stable-diffusion.cpp's releases) keeping the
    executable bit, and refuses a path that leaves the directory, an absolute path or a link."""
    from forgeyard.agent.models import CacheError, unzip

    def make(entries: list[tuple[str, bytes, int]]) -> str:
        path = tmp_path / f"e{len(list(tmp_path.iterdir()))}.zip"
        with zipfile.ZipFile(path, "w") as z:
            for name, data, mode in entries:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                z.writestr(info, data)
        return path

    good = make([("sd-server", b"#!/bin/sh\necho ok\n", 0o100755), ("libggml.so", b"lib", 0o100644),
                 ("sub/data.txt", b"d", 0o100644)])
    out = tmp_path / "out"
    out.mkdir()
    unzip(good, out)
    assert os.access(out / "sd-server", os.X_OK) and not os.access(out / "libggml.so", os.X_OK)
    assert (out / "sub" / "data.txt").read_bytes() == b"d"
    for bad, why in [([("../evil", b"x", 0o100644)], "unsafe path"), ([("/abs", b"x", 0o100644)], "unsafe path"),
                     ([("link", b"target", stat.S_IFLNK | 0o777)], "link")]:
        dest = tmp_path / f"bad{len(list(tmp_path.iterdir()))}"
        dest.mkdir()
        with pytest.raises(CacheError, match=why):
            unzip(make(bad), dest)
    assert not (tmp_path / "evil").exists()


def test_fy_igen_017_placement_needs_the_guard_and_zip(owner):
    """FY-IGEN-017: sd-server has no key of its own, so it goes only to agents with the key guard; its CPU
    process engine is a .zip, so only to agents that unpack one."""
    add_catalog("sd15", sd15_meta(), task="image", fmt="gguf", source="hf://second-state/stable-diffusion-v1-5-GGUF")
    old = make_node("old", features=MODELS + ("replica_keys",))
    r = owner.post("/api/deployments/estimate", json={"name": "s", "model": "sd15", "recipe": "sd-cpp",
                                                      "params": {"gpus": 0}, "machine_id": old})
    m = r.json()["placement"]["machines"][0]
    assert not m["fits"] and "key guard" in m["reason"]
    nozip = make_node("nozip", features=MODELS + ("replica_keys", "replica_guard"))
    r = owner.post("/api/deployments/estimate", json={"name": "s", "model": "sd15", "recipe": "sd-cpp",
                                                      "params": {"gpus": 0}, "machine_id": nozip})
    m = next(x for x in r.json()["placement"]["machines"] if x["machine_id"] == nozip)
    assert not m["fits"] and ".zip" in m["reason"]
    ok = make_node("ok", features=FEATURES)
    r = owner.post("/api/deployments/estimate", json={"name": "s", "model": "sd15", "recipe": "sd-cpp",
                                                      "params": {"gpus": 0}, "machine_id": ok})
    assert r.json()["placement"]["fits"]


def test_fy_igen_018_try_it_and_the_playground(img):
    """FY-IGEN-018: a deployment's Try it asks a replica for one small image and answers with its size,
    not the image; the Playground reads the model's options (sizes it takes, most images) and makes images
    as the signed-in user, counted as console."""
    deps = {d["name"]: d["id"] for d in img.owner.get("/api/deployments").json()["deployments"]}
    r = img.owner.post(f"/api/deployments/{deps['sd']}/try", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["summary"].startswith("1 image (256x256 PNG") and "b64_json" not in json.dumps(out)
    assert out["request"]["size"] == "256x256"
    o = img.owner.get("/api/playground/images/options?model=sd").json()
    assert o == {"model": "sd", "max_n": 4, "max_side": 512, "default_size": "512x512", "multiple": 64,
                 "formats": ["png", "jpeg", "webp"], "max_steps": 50, "max_prompt_chars": 4000}
    assert img.owner.get("/api/playground/images/options?model=nope").status_code == 404
    r = img.owner.post("/api/playground/images/generations", json={"model": "omni", "prompt": "a fox", "size": "512x512"})
    assert r.status_code == 200 and len(r.json()["data"]) == 1
    recorder.flush()
    rows = img.owner.get("/api/usage?everyone=true&group=key").json()["rows"]
    assert any(x["images"] == 1 and x["kilopixels"] == round(512 * 512 / 1000) for x in rows)


def test_fy_igen_019_answers_are_read_for_their_size():
    """FY-IGEN-019: an image's size is read from its own header (PNG, JPEG after EXIF, WebP's three
    kinds); the answer keeps OpenAI's fields and counts what it holds; an answer without images is
    none."""
    assert imagegen.image_dims(png(33, 17)) == (33, 17, "png")
    exif = b"\xff\xe1" + struct.pack(">H", 2000) + b"\0" * 1998
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 240, 320) + b"\0" * 10
    assert imagegen.image_dims(b"\xff\xd8" + exif + sof + b"\xff\xd9") == (320, 240, "jpeg")
    vp8x = b"RIFF" + b"\0" * 4 + b"WEBPVP8X" + b"\0" * 8 + (639).to_bytes(3, "little") + (479).to_bytes(3, "little")
    assert imagegen.image_dims(vp8x) == (640, 480, "webp")
    vp8 = b"RIFF" + b"\0" * 4 + b"WEBPVP8 " + b"\0" * 7 + b"\x9d\x01\x2a" + struct.pack("<HH", 100, 50)
    assert imagegen.image_dims(vp8) == (100, 50, "webp")
    bits = (99) | (49 << 14)
    vp8l = b"RIFF" + b"\0" * 4 + b"WEBPVP8L" + b"\0" * 4 + b"\x2f" + bits.to_bytes(4, "little") + b"\0" * 5
    assert imagegen.image_dims(vp8l) == (100, 50, "webp")
    assert imagegen.image_dims(b"GIF89a") is None
    ask = imagegen.check({"prompt": "x", "size": "64x64"})
    data = json.dumps({"created": 5, "data": [{"b64_json": base64.b64encode(png(64, 64)).decode(), "url": None,
                                               "generation_info": {"seed": 1}}], "stage_durations": {}}).encode()
    out, made = imagegen.answer(data, ask, None)
    assert json.loads(out) == {"created": 5, "data": [{"b64_json": base64.b64encode(png(64, 64)).decode()}],
                               "output_format": "png", "size": "64x64"}
    assert (made.images, made.kilopixels) == (1, 4)
    # Counted from the image itself when the engine made another size than asked.
    other = json.dumps({"data": [{"b64_json": base64.b64encode(png(32, 16)).decode()}]}).encode()
    out, made = imagegen.answer(other, ask, None)
    assert (made.kilopixels, json.loads(out)["size"]) == (1, "32x16")
    assert imagegen.answer(b'{"data": []}', ask, None)[1].images == 0
    assert imagegen.answer(b"not json", ask, None) == (None, imagegen.Made())


def test_fy_igen_020_rolling_tags_stay_pinned(tmp_path, capsys):
    """FY-IGEN-020: scripts/pin_images.py holds an image pinned on a rolling tag (master-vulkan, master-cuda)
    at its digest when the tag moves on: the check reports it as information and --write keeps the pin, unless
    --move-rolling; a release tag that moved is still a problem."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("pin_images", Path(__file__).resolve().parent.parent / "scripts" /
                                                  "pin_images.py")
    pin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pin)
    assert pin.rolling("ghcr.io/leejet/stable-diffusion.cpp:master-vulkan")
    assert pin.rolling("ghcr.io/leejet/stable-diffusion.cpp:master-cuda")
    assert pin.rolling("ghcr.io/leejet/stable-diffusion.cpp:master-vulkan@sha256:" + "0" * 64)
    sd = recipes.builtin_recipes()["sd-cpp"][0]
    assert {v.name for v in sd.variants if v.runtime == "container" and pin.rolling(v.image)} == {"cuda", "vulkan"}
    assert not pin.rolling("ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242")
    assert not pin.rolling("vllm/vllm-omni:v0.32.0") and pin.rolling("acme/x:latest")

    old, new = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    entry = {"index": None, "platforms": {"linux/amd64": {"manifest": old, "config": old, "content": old}}}

    class Reg:
        def __init__(self, _client):
            pass

    monkey = {"resolve": lambda reg, ref: new, "describe": lambda reg, image: entry, "Registry": Reg}
    saved = {k: getattr(pin, k) for k in monkey}
    try:
        for k, v in monkey.items():
            setattr(pin, k, v)
        recipes_dir = tmp_path / "recipes"
        recipes_dir.mkdir()
        lock = tmp_path / "images.lock.json"
        rolling = f"ghcr.io/leejet/stable-diffusion.cpp:master-vulkan@{old}"
        (recipes_dir / "x.yaml").write_text(f"variants:\n  - name: vulkan\n    when: {{vendor: amd}}\n    image: {rolling}\n")
        pin.write_lock(lock, {rolling: entry})
        assert pin.main([], client=object(), recipes=recipes_dir, lock=lock) == 0
        assert "rolling tag" in capsys.readouterr().out
        assert pin.main(["--write"], client=object(), recipes=recipes_dir, lock=lock) == 0
        assert old in (recipes_dir / "x.yaml").read_text()
        pin.main(["--write", "--move-rolling"], client=object(), recipes=recipes_dir, lock=lock)
        assert new in (recipes_dir / "x.yaml").read_text()
        release = f"ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242@{old}"
        (recipes_dir / "x.yaml").write_text(f"variants:\n  - name: vulkan\n    when: {{vendor: amd}}\n    image: {release}\n")
        pin.write_lock(lock, {release: entry})
        assert pin.main([], client=object(), recipes=recipes_dir, lock=lock) == 1
        assert "MOVED" in capsys.readouterr().out
    finally:
        for k, v in saved.items():
            setattr(pin, k, v)


def test_fy_igen_021_selecting_diffusers_files():
    """FY-IGEN-021: model_index.json's [library, class] entries are the components; a precision variant is
    left out when the plain file is there (with its index), kept when it is the only one; nothing outside
    the components' directories but model_index.json."""
    index = {"_class_name": "P", "_diffusers_version": "0.30", "transformer": ["diffusers", "T"],
             "vae": ["diffusers", "V"], "safety_checker": [None, None], "../x": ["a", "b"]}
    assert sources.diffusers_components(index) == ["transformer", "vae"]
    files = [{"path": p, "size": 1} for p in [
        "model_index.json", "README.md", "flux1-schnell.safetensors",
        "transformer/diffusion_pytorch_model.safetensors.index.json",
        "transformer/diffusion_pytorch_model-00001-of-00002.safetensors",
        "transformer/diffusion_pytorch_model.fp16-00001-of-00002.safetensors",
        "vae/diffusion_pytorch_model.fp16.safetensors", "vae/config.json", "vae/diffusion_pytorch_model.bin"]]
    got = [f["path"] for f in sources.select_diffusers(files, ["transformer", "vae"])]
    assert got == ["model_index.json", "transformer/diffusion_pytorch_model.safetensors.index.json",
                   "transformer/diffusion_pytorch_model-00001-of-00002.safetensors",
                   "vae/diffusion_pytorch_model.fp16.safetensors", "vae/config.json"]
    assert sources.detect_format([f["path"] for f in files]) == "diffusers"
    assert sources.detect_format(["model_index.json", "x.gguf", "unet/a.safetensors"]) == "gguf"
    assert sources.detect_format(["model.safetensors", "config.json"]) == "safetensors"


def test_fy_igen_022_the_acceptance_decoder_finds_broken_images():
    """FY-IGEN-022: scripts/accept_img.py's decoder (no Pillow) accepts a whole PNG and reads its size,
    and refuses one with a wrong CRC, pixel data short of its size, or no end; a JPEG needs its frame
    header and end marker."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("accept_img", Path(__file__).resolve().parent.parent / "scripts" /
                                                  "accept_img.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    good = png(40, 30)
    assert mod.decode_image(good) == (40, 30, "png")
    bad_crc = bytearray(good)
    bad_crc[30] ^= 0xFF                      # inside IHDR's data: its CRC no longer matches
    short = png(40, 29)
    short = short[:16] + struct.pack(">II", 40, 30) + short[24:]   # says 30 rows, holds 29 (CRC fixed below)
    ihdr = short[12:29]
    short = short[:29] + struct.pack(">I", zlib.crc32(ihdr) & 0xFFFFFFFF) + short[33:]
    for broken, why in [(bytes(bad_crc), "CRC"), (short, "pixel data"), (good[:-12], "IEND"), (b"GIF89a", "not a PNG")]:
        with pytest.raises(ValueError, match=why):
            mod.decode_image(broken)
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 240, 320) + b"\0" * 10
    assert mod.decode_image(b"\xff\xd8" + sof + b"\xff\xd9") == (320, 240, "jpeg")
    with pytest.raises(ValueError, match="end marker"):
        mod.decode_image(b"\xff\xd8" + sof)


def test_fy_igen_023_the_deployments_caps_come_from_its_recipe_params_and_model():
    """FY-IGEN-023: the gateway's settings for a deployment: its images per request within the recipe's
    own most (max_n), its largest side (0: the model's), a default size within that side, its steps,
    queue and timeout."""
    from forgeyard.models.recipes import ImageGen

    b = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
    small = b["sd-cpp"].model_copy(update={"image": ImageGen(request="sd-cpp", max_n=2)})
    d = imagegen.draws(small, {"max_images": 4, "max_side": 0, "steps": 8, "queue": 3, "timeout": 99}, sd15_meta())
    assert (d.max_n, d.max_side, d.width, d.height, d.multiple) == (2, 512, 512, 512, 64)
    assert (d.steps, d.queue, d.timeout, d.request) == (8, 3, 99.0, "sd-cpp")
    d = imagegen.draws(b["vllm-omni-image"], {"max_images": 3, "max_side": 768, "default_size": "1024x1024"},
                       zimage_meta())
    assert (d.max_n, d.max_side, d.width, d.height, d.multiple) == (3, 768, 768, 768, 16)
    assert render.default_size(zimage_meta(), {"max_side": 512, "default_size": "1024x768"}) == (512, 512)
    assert render.default_size(zimage_meta(), {"default_size": "1024x768"}) == (1024, 768)
    assert render.default_size(sd15_meta(), {}) == (512, 512)


def test_fy_igen_024_a_newcomer_does_not_jump_the_queue(img):
    """FY-IGEN-024: while a request waits for a deployment, a new one does not take a slot that comes
    free first: it queues behind (here behind a waiter that never moves, so it times out with 503
    queue_timeout and never reaches the engine)."""
    db = new_session()
    try:
        d = db.query(Deployment).filter_by(name="sd").one()
        d.params = {**d.params, "timeout": 1}
        db.commit()
    finally:
        db.close()
    q = imagegen.queue("sd#0")
    ahead = q.join(8)
    try:
        r = gen(img.api, "sd")
        assert r.status_code == 503 and err(r)["code"] == "queue_timeout"
        assert img.engines["sd"].bodies() == [] and q.waiting == [ahead]
    finally:
        q.leave(ahead)
    assert gen(img.api, "sd").status_code == 200
