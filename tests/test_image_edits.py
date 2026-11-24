"""Image edits (1.5.5, #143): /v1/images/edits through the gateway to fake vLLM-Omni and stable-diffusion.cpp engines:
what a model does with images (catalog meta.image.edit / generate, from its pipeline class or its names), the
edit-only model that used to be sent text-to-image requests, both request shapes (multipart and JSON with image_url,
URLs fetched by the gateway), the input checks (count, bytes, type, header size), the engine's request rebuilt with
an explicit size and the checked files only, usage, variations closed, the estimate and Try it (FY-IEDT-*)."""

from __future__ import annotations

import base64
import json
import logging
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Optional

import pytest

from forgeyard.config import get_settings
from forgeyard.gateway import imagegen, multipart
from forgeyard.gateway.usage import recorder
from forgeyard.models import editing, recipes
from forgeyard.models.estimate import estimate
from tests.gateway_helpers import api_client, make_ready, new_key
from tests.model_helpers import add_catalog
from tests.test_image_generation import FEATURES, png, sd15_meta
from tests.test_multimodal import MediaServer
from tests.test_scheduler import deploy
from tests.workload_helpers import make_node

MIB = 1024**2


def jpeg_header(w: int, h: int) -> bytes:
    """The start of a JPEG (SOI, an APP0, a SOF0 naming w x h): enough for its size to be read, nothing to decode."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\x00" * 16


def claims(w: int, h: int) -> bytes:
    """A small PNG whose header claims w x h (a decompression bomb's shape): 33 bytes, never decoded here."""
    head = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + head + b"\0\0\0\0"


class FakeEditEngine:
    """An image engine at http://127.0.0.1:<port>: POST /v1/images/generations (JSON) and /v1/images/edits (multipart)
    answer `n` PNGs of the size asked. Records what it was sent: the edit's plain fields and its files. `gate`
    (a threading.Event) holds every request until set; counts how many it works on at once."""

    def __init__(self, secret: str):
        self.secret = secret
        self.seen: list[dict] = []
        self.gate: Optional[threading.Event] = None
        self.inflight = self.most = 0
        self._lock = threading.Lock()
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body: bytes):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, b'{"data": []}')

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Authorization") != f"Bearer {engine.secret}":
                    return self._send(401, b'{"error":"Unauthorized"}')
                ctype = self.headers.get("Content-Type") or ""
                if ctype.startswith("multipart/form-data"):
                    _, parts = multipart.parse(raw, ctype)
                    fields = {p.name: p.content.decode() for p in parts if not p.is_file}
                    files = [(p.name, p.head.decode(), p.content) for p in parts if p.is_file]
                    seen = {"path": self.path, "fields": fields, "files": files, "raw": raw}
                else:
                    fields = json.loads(raw or b"{}")
                    seen = {"path": self.path, "fields": fields, "files": [], "raw": raw}
                engine.seen.append(seen)
                with engine._lock:
                    engine.inflight += 1
                    engine.most = max(engine.most, engine.inflight)
                try:
                    if engine.gate is not None:
                        engine.gate.wait(30)
                    w, h = (int(x) for x in str(fields.get("size") or "512x512").split("x"))
                    items = [{"b64_json": base64.b64encode(png(w, h, 60 + i)).decode()}
                             for i in range(int(fields.get("n") or 1))]
                    self._send(200, json.dumps({"created": 1700000000, "data": items, "output_format": "png"}).encode())
                finally:
                    with engine._lock:
                        engine.inflight -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def edits(self) -> list[dict]:
        return [s for s in self.seen if s["path"] == "/v1/images/edits"]

    def close(self):
        if self.gate is not None:
            self.gate.set()
        self.server.shutdown()
        self.server.server_close()


def diffusers_meta(repo: str, pipeline: str, **image) -> dict:
    comps = {"transformer": {"class": "T", "params": 1_000_000_000, "bytes": 2_000_000_000, "dtype": "BF16"},
             "text_encoder": {"class": "Qwen3Model", "params": 500_000_000, "bytes": 1_000_000_000, "dtype": "BF16"},
             "vae": {"class": "AutoencoderKL", "params": 80_000_000, "bytes": 160_000_000, "dtype": "BF16"}}
    return {"kind": "hf", "repo": repo, "ref": "main", "revision": "a" * 40, "format": "diffusers",
            "license": "apache-2.0", "modalities": [], "task_hint": "image",
            "diffusers": {"pipeline": pipeline, "components": comps},
            "image": {"size": 1024, "multiple": 16, **image},
            "files": [{"path": "model_index.json", "size": 400, "oid": "a" * 40}], "size_bytes": 3_160_000_000}


def klein_gguf_meta() -> dict:
    comps = {"llm": "qwen_3_4b-Q8_0.gguf", "vae": "flux2-vae.safetensors"}
    return {"kind": "hf", "repo": "acme/FLUX.2-klein-4B-GGUF", "ref": "main", "revision": "b" * 40, "format": "gguf",
            "file": "flux-2-klein-4b-Q8_0.gguf", "license": "apache-2.0", "modalities": [], "task_hint": "image",
            "image": {"size": 1024, "multiple": 16, "components": comps, "single": False, "edit": 4, "generate": True,
                      "bytes": {"model": 4_300_000_000, "llm": 4_270_000_000, "vae": 330_000_000}},
            "files": [{"path": "flux-2-klein-4b-Q8_0.gguf", "size": 4_300_000_000, "sha256": "d" * 64}],
            "size_bytes": 8_900_000_000}


@pytest.fixture()
def ed(owner, monkeypatch):
    """`klein` (vllm-omni-image, FLUX.2 [klein]: edits up to 4 and makes images from text; the deployment takes 2 input
    images up to 2048 px), `qedit` (vllm-omni-image, Qwen-Image-Edit as a 1.3 catalog entry: no edit / generate keys,
    read from its pipeline class), `kgguf` (sd-cpp on a CPU, FLUX.2 [klein] GGUF, inputs up to 1024 px) and `sd`
    (sd-cpp, Stable Diffusion 1.5: edits nothing); a key; a media server on 127.0.0.1 (MEDIA_ALLOW_PRIVATE_HOSTS on)."""
    monkeypatch.setenv("MEDIA_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    add_catalog("klein", diffusers_meta("black-forest-labs/FLUX.2-klein-4B", "Flux2KleinPipeline", edit=4, generate=True),
                task="image", fmt="diffusers", source="hf://black-forest-labs/FLUX.2-klein-4B")
    add_catalog("qedit", diffusers_meta("Qwen/Qwen-Image-Edit", "QwenImageEditPipeline"), task="image",
                fmt="diffusers", source="hf://Qwen/Qwen-Image-Edit")
    add_catalog("kgguf", klein_gguf_meta(), task="image", fmt="gguf", source="hf://acme/FLUX.2-klein-4B-GGUF")
    add_catalog("sd15", sd15_meta(), task="image", fmt="gguf", source="hf://second-state/stable-diffusion-v1-5-GGUF")
    cpu = make_node("cpu-a", features=FEATURES)
    gpu = make_node("a100", allowlist=[4], features=FEATURES)
    e = {n: FakeEditEngine(f"s-{n}") for n in ("klein", "qedit", "kgguf", "sd")}
    deploy(owner, "klein", "klein", "vllm-omni-image", params={"max_input_images": 2, "max_images": 2})
    deploy(owner, "qedit", "qedit", "vllm-omni-image")
    deploy(owner, "kgguf", "kgguf", "sd-cpp", params={"gpus": 0, "threads": 2, "max_input_images": 4})
    deploy(owner, "sd", "sd15", "sd-cpp", params={"gpus": 0, "threads": 2})
    for n in ("klein", "qedit"):
        make_ready(n, [(gpu, e[n].port, e[n].secret)])
    for n in ("kgguf", "sd"):
        make_ready(n, [(cpu, e[n].port, e[n].secret)])
    key = new_key(owner, "laptop")["key"]
    imagegen.forget()
    media = MediaServer()
    yield SimpleNamespace(owner=owner, e=e, key=key, api=api_client(owner, key), media=media)
    for x in e.values():
        x.close()
    media.close()
    imagegen.forget()
    get_settings.cache_clear()


def err(r) -> dict:
    body = r.json()
    assert set(body) == {"error"}, body
    return body["error"]


def edit(c, model: str, images: list[bytes], prompt: str = "Make the sky purple.", field: str = "image[]", **fields):
    files = [(field, (f"in-{i}.png", data, "image/png")) for i, data in enumerate(images)]
    return c.post("/v1/images/edits", data={"model": model, "prompt": prompt, **fields}, files=files)


def data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def usage_by_model(owner) -> dict:
    recorder.flush()
    return {x["id"]: x for x in owner.get("/api/usage?group=model").json()["rows"]}


# ------------------------------------------------------------------ what a model does with images
def test_fy_iedt_001_what_a_model_does_with_images():
    """FY-IEDT-001: a diffusers pipeline class says it (FLUX.2 [klein] edits 4 and generates, Qwen-Image-Edit edits 1
    only, Qwen-Image-Edit-Plus 3, an inpainting pipeline neither, a text-to-image pipeline generates only); a
    stable-diffusion.cpp file by its names; a catalog entry without the keys (1.3) is read from its pipeline class, one
    with them as they say; an admin's word counts only where no pipeline class says it."""
    assert editing.from_pipeline("Flux2KleinPipeline") == (4, True)
    assert editing.from_pipeline("QwenImageEditPipeline") == (1, False)
    assert editing.from_pipeline("QwenImageEditPlusPipeline") == (3, False)
    assert editing.from_pipeline("StableDiffusionXLInpaintPipeline") == (0, False)
    assert editing.from_pipeline("StableDiffusionImg2ImgPipeline") == (1, False)
    assert editing.from_pipeline("ZImagePipeline") == (0, True)
    assert editing.from_names("flux-2-klein-4b-Q8_0.gguf", "leejet/FLUX.2-klein-4B-GGUF") == (4, True)
    assert editing.from_names("Qwen-Image-Edit-2511-Q4_K_M.gguf", "unsloth/Qwen-Image-Edit-2511-GGUF") == (3, False)
    assert editing.from_names("qwen-image-edit-Q4_0.gguf", None) == (1, False)
    assert editing.from_names("flux1-kontext-dev-Q8_0.gguf", None) == (1, True)
    assert editing.from_names("sd-v1-5-Q8_0.gguf", "second-state/stable-diffusion-v1-5-GGUF") == (0, True)
    assert editing.of(diffusers_meta("Qwen/Qwen-Image-Edit", "QwenImageEditPipeline")) == (1, False)
    assert editing.of(sd15_meta()) == (0, True)
    assert editing.of(klein_gguf_meta()) == (4, True)
    meta = diffusers_meta("Qwen/Qwen-Image-Edit", "QwenImageEditPipeline")
    editing.describe(meta, edit=4, generate=True)
    assert (meta["image"]["edit"], meta["image"]["generate"]) == (1, False)
    gg = sd15_meta()
    editing.describe(gg, edit=2, generate=False)
    assert (gg["image"]["edit"], gg["image"]["generate"]) == (2, False)


def test_fy_iedt_002_the_inspector_and_adding_a_model(owner, monkeypatch):
    """FY-IEDT-002: the inspector writes meta.image.edit / generate (a Qwen-Image-Edit diffusers repository: edits 1,
    makes nothing from text); the catalog shows them; adding a stable-diffusion.cpp file takes the admin's word
    (image_edit, image_generate) over its names; an inpainting pipeline is refused as an image model
    (not_an_image_model): it needs a mask."""
    from tests.model_helpers import FakeHF, Repo, safetensors_bytes

    st = safetensors_bytes({"w": {"dtype": "BF16", "shape": [8], "data_offsets": [0, 16]}}, b"\0" * 16)

    def pipeline_repo(name: str, cls: str) -> Repo:
        index = {"_class_name": cls, "transformer": ["diffusers", "T"], "vae": ["diffusers", "AutoencoderKL"]}
        r = Repo(name, "1" * 40, {"model_index.json": json.dumps(index).encode(),
                                  "transformer/diffusion_pytorch_model.safetensors": st,
                                  "vae/diffusion_pytorch_model.safetensors": st})
        r.info.update(pipeline_tag="image-to-image", library_name="diffusers", cardData={"license": "apache-2.0"})
        return r

    gg = Repo("acme/sd-files", "2" * 40, {"model-Q8_0.gguf": b"g" * 40, "README.md": b"x"})
    gg.info.update(pipeline_tag="text-to-image", library_name=None, cardData={"license": "apache-2.0"})
    hf = FakeHF(pipeline_repo("acme/qedit", "QwenImageEditPipeline"),
                pipeline_repo("acme/inpaint", "StableDiffusionXLInpaintPipeline"), gg)
    try:
        monkeypatch.setenv("HF_ENDPOINT", hf.url)
        monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
        get_settings.cache_clear()
        r = owner.post("/api/models", json={"name": "qedit", "source": "hf://acme/qedit", "task": "image"})
        assert r.status_code == 201, r.text
        m = r.json()["model"]
        assert (m["image"]["edit"], m["image"]["generate"]) == (1, False) and m["recipes"] == ["vllm-omni-image"]
        r = owner.post("/api/models", json={"name": "files", "source": "hf://acme/sd-files", "task": "image",
                                            "image_edit": 2, "image_generate": False})
        assert r.status_code == 201, r.text
        assert (r.json()["model"]["image"]["edit"], r.json()["model"]["image"]["generate"]) == (2, False)
        r = owner.post("/api/models", json={"name": "inpaint", "source": "hf://acme/inpaint", "task": "image"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "not_an_image_model"
        r = owner.post("/api/models", json={"name": "bad", "source": "hf://acme/sd-files", "task": "image",
                                            "image_edit": 17})
        assert r.status_code == 422
    finally:
        hf.close()
        get_settings.cache_clear()


def test_fy_iedt_003_an_edit_only_model_gets_no_text_to_image_request(ed):
    """FY-IEDT-003 (the latent 1.3 bug): Qwen-Image-Edit added as an image model used to be sent every
    /v1/images/generations request, which its engine cannot make. Now it is 400 generation_not_supported, pointing to
    /v1/images/edits, and nothing reaches the engine; a model that does both still makes images from text."""
    r = ed.api.post("/v1/images/generations", json={"model": "qedit", "prompt": "A cat."})
    assert r.status_code == 400 and err(r)["code"] == "generation_not_supported"
    assert "/v1/images/edits" in err(r)["message"] and ed.e["qedit"].seen == []
    r = ed.api.post("/v1/images/generations", json={"model": "klein", "prompt": "A cat."})
    assert r.status_code == 200 and len(r.json()["data"]) == 1
    assert ed.e["klein"].seen[-1]["path"] == "/v1/images/generations"


# ------------------------------------------------------------------ the request and what the engine gets
def test_fy_iedt_004_a_multipart_edit_reaches_the_engine_rebuilt(ed):
    """FY-IEDT-004: an OpenAI SDK-shaped multipart edit (two image[] files) is sent to the engine as a new multipart
    body: the fields Forgeyard builds (model, prompt, n, an explicit size, output_format, response_format) and the
    two files byte for byte as image[]; nothing else the client sent reaches it (url, url[], mask_image,
    reference_image, lora, layers). The answer is OpenAI's shape; usage counts the image made, its kilopixels, the 2
    input images and their bytes."""
    a, b = png(800, 600, 10), png(400, 400, 20)
    r = edit(ed.api, "klein", [a, b], n="1", url="http://169.254.169.254/latest/meta-data/",
             **{"url[]": "http://10.0.0.1/x.png", "mask_image": "http://10.0.0.1/m.png",
                "reference_image": "http://10.0.0.1/r.png", "lora": '{"path": "/etc/shadow"}', "layers": "4"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"created", "data", "output_format", "size"} and len(out["data"]) == 1
    sent = ed.e["klein"].edits()[-1]
    assert set(sent["fields"]) == {"model", "prompt", "n", "size", "output_format", "response_format"}
    assert sent["fields"]["prompt"] == "Make the sky purple." and sent["fields"]["response_format"] == "b64_json"
    assert [(name, data) for name, _, data in sent["files"]] == [("image[]", a), ("image[]", b)]
    assert b"169.254.169.254" not in sent["raw"] and b"10.0.0.1" not in sent["raw"] and b"/etc/shadow" not in sent["raw"]
    w, h = (int(x) for x in sent["fields"]["size"].split("x"))
    assert out["size"] == f"{w}x{h}"
    rows = usage_by_model(ed.owner)
    k = rows["klein"]
    assert (k["images"], k["kilopixels"], k["media_items"], k["bytes_in"]) == (1, round(w * h / 1000), 2, len(a) + len(b))


def test_fy_iedt_005_json_edits_and_urls_fetched_by_the_gateway(ed, monkeypatch):
    """FY-IEDT-005: OpenAI's JSON form: images[].image_url as a data: URL, or an http(s) URL the gateway fetches under
    the media rules and sends as a file (the engine never gets the URL); with private hosts off the URL is refused
    before it is fetched (400 media_url_refused); a file_id is 400 file_id_not_supported; a mask (JSON or a multipart
    file) is 400 mask_not_supported; a file under another name is refused; another scheme is 400 media_url_refused; a
    key that may not use the model makes the gateway fetch nothing (403). None of these reaches the engine."""
    a = png(512, 512, 30)
    url = f"{ed.media.url}/img.png"
    r = ed.api.post("/v1/images/edits", json={"model": "klein", "prompt": "Add a hat.",
                                              "images": [{"image_url": data_url(a)}, {"image_url": url}]})
    assert r.status_code == 200, r.text
    sent = ed.e["klein"].edits()[-1]
    from tests.test_multimodal import PNG
    assert [data for _, _, data in sent["files"]] == [a, PNG] and ed.media.hits == ["/img.png"]
    assert url.encode() not in sent["raw"]
    before = len(ed.e["klein"].seen)
    for body, code in [({"images": [{"file_id": "file-abc"}]}, "file_id_not_supported"),
                       ({"images": [{"image_url": data_url(a)}], "mask": {"image_url": data_url(a)}}, "mask_not_supported"),
                       ({"images": [{"image_url": "file:///etc/passwd"}]}, "media_url_refused"),
                       ({"images": [{"image_url": "data:image/png;base64,@@@"}]}, "invalid_image"),
                       ({}, "missing_image")]:
        r = ed.api.post("/v1/images/edits", json={"model": "klein", "prompt": "x", **body})
        assert r.status_code == 400 and err(r)["code"] == code, (body, r.text)
    r = ed.api.post("/v1/images/edits", data={"model": "klein", "prompt": "x"},
                    files=[("image", ("a.png", a, "image/png")), ("mask", ("m.png", a, "image/png"))])
    assert r.status_code == 400 and err(r)["code"] == "mask_not_supported"
    r = ed.api.post("/v1/images/edits", data={"model": "klein", "prompt": "x"},
                    files=[("image", ("a.png", a, "image/png")), ("reference_image", ("r.png", a, "image/png"))])
    assert r.status_code == 400 and err(r)["param"] == "reference_image"
    # A key that may not use the model makes the gateway fetch nothing.
    narrow = api_client(ed.owner, new_key(ed.owner, "narrow", models=["sd"])["key"])
    hits = len(ed.media.hits)
    r = narrow.post("/v1/images/edits", json={"model": "klein", "prompt": "x", "images": [{"image_url": url}]})
    assert r.status_code == 403 and err(r)["code"] == "model_not_allowed" and len(ed.media.hits) == hits
    monkeypatch.setenv("MEDIA_ALLOW_PRIVATE_HOSTS", "false")
    get_settings.cache_clear()
    hits = len(ed.media.hits)
    r = ed.api.post("/v1/images/edits", json={"model": "klein", "prompt": "x", "images": [{"image_url": url}]})
    assert r.status_code == 400 and err(r)["code"] == "media_url_refused" and len(ed.media.hits) == hits
    assert len(ed.e["klein"].seen) == before


def test_fy_iedt_006_the_input_checks(ed, monkeypatch):
    """FY-IEDT-006: no image (JSON or a form) is 400 missing_image; more than 16 is too_many_input_images before any URL
    is fetched, and so is more than the
    deployment's Input images; an image whose header claims more than its Largest input (a 33-byte PNG claiming
    60000x60000, never decoded) is input_image_too_large; a GIF or HEIC is unsupported_image_format; a PNG whose header
    is cut, or claims no pixels, is invalid_image; one over MEDIA_FETCH_MAX_MB is 413 media_too_large. None reaches an engine."""
    one = png(64, 64)
    urls = [{"image_url": f"{ed.media.url}/img.png"}] * 17
    cases = [(ed.api.post("/v1/images/edits", json={"model": "klein", "prompt": "x"}), 400, "missing_image"),
             (ed.api.post("/v1/images/edits", data={"model": "klein", "prompt": "x"}, files={"note": (None, "a form")}),
              400, "missing_image"),
             (ed.api.post("/v1/images/edits", json={"model": "klein", "prompt": "x", "images": urls}), 400,
              "too_many_input_images"),
             (edit(ed.api, "klein", [one] * 17), 400, "too_many_input_images"),
             (edit(ed.api, "klein", [one] * 3), 400, "too_many_input_images"),
             (edit(ed.api, "qedit", [one] * 2), 400, "too_many_input_images"),
             (edit(ed.api, "klein", [claims(60000, 60000)]), 400, "input_image_too_large"),
             (edit(ed.api, "kgguf", [png(1200, 64)]), 400, "input_image_too_large"),
             (edit(ed.api, "klein", [b"GIF89a" + b"\0" * 40]), 400, "unsupported_image_format"),
             (edit(ed.api, "klein", [b"\0\0\0\x18ftypheic" + b"\0" * 40]), 400, "unsupported_image_format"),
             (edit(ed.api, "klein", [b"\x89PNG\r\n\x1a\n\0\0"]), 400, "invalid_image"),
             (edit(ed.api, "klein", [claims(0, 0)]), 400, "invalid_image")]
    for r, status, code in cases:
        assert r.status_code == status and err(r)["code"] == code, (code, r.text)
    assert "at most 2" in err(cases[4][0])["message"]   # what the deployment takes
    assert ed.media.hits == []                          # 17 URLs: refused before any is fetched
    monkeypatch.setenv("MEDIA_FETCH_MAX_MB", "1")
    get_settings.cache_clear()
    big = png(64, 64) + b"\0" * (MIB + 10)
    r = edit(ed.api, "klein", [big])
    assert r.status_code == 413 and err(r)["code"] == "media_too_large"
    assert all(not x.seen for x in ed.e.values())


def test_fy_iedt_007_the_size_is_always_sent(ed):
    """FY-IEDT-007: a request without a size gets the first input's shape at the deployment's default pixels, within
    its largest side, sides a multiple of 16 (an 800x600 photo: 1024x768 for a 1024x1024 default and side); a size asked is
    sent as asked and checked against the caps (image_too_large, invalid_size); an edit-only model edits too."""
    assert edit(ed.api, "klein", [png(800, 600)]).status_code == 200
    w, h = (int(x) for x in ed.e["klein"].edits()[-1]["fields"]["size"].split("x"))
    assert (w, h) == (1024, 768)
    assert edit(ed.api, "klein", [png(64, 2000)]).status_code == 200
    w, h = (int(x) for x in ed.e["klein"].edits()[-1]["fields"]["size"].split("x"))
    assert h <= 1024 and w >= 16 and w % 16 == 0
    assert edit(ed.api, "klein", [png(800, 600)], size="512x768").status_code == 200
    assert ed.e["klein"].edits()[-1]["fields"]["size"] == "512x768"
    r = edit(ed.api, "klein", [png(800, 600)], size="2048x2048")
    assert r.status_code == 400 and err(r)["code"] == "image_too_large"
    r = edit(ed.api, "klein", [png(800, 600)], size="500x500")
    assert r.status_code == 400 and err(r)["code"] == "invalid_size"
    r = edit(ed.api, "qedit", [png(1024, 1024)], field="image")
    assert r.status_code == 200 and ed.e["qedit"].edits()[-1]["fields"]["size"] == "1024x1024"


def test_fy_iedt_008_stable_diffusion_cpp_edits(ed):
    """FY-IEDT-008: sd-server gets its extras inside the prompt (<sd_cpp_extra_args>), the files as image[] and png or
    jpeg only (its edits encode nothing else: webp is 400 unsupported_output_format for an edit, still made for a
    generation); a client's own extra-args block is refused (400); a model that edits nothing is 400
    edits_not_supported, pointing to /v1/images/generations."""
    r = edit(ed.api, "kgguf", [png(512, 512)], seed="7", num_inference_steps="4", negative_prompt="blur",
             output_format="jpeg")
    assert r.status_code == 200, r.text
    f = ed.e["kgguf"].edits()[-1]["fields"]
    assert f["prompt"].startswith("Make the sky purple. <sd_cpp_extra_args>") and '"seed":7' in f["prompt"]
    assert f["output_format"] == "jpeg" and "response_format" not in f
    r = edit(ed.api, "kgguf", [png(512, 512)], output_format="webp")
    assert r.status_code == 400 and err(r)["code"] == "unsupported_output_format"
    gen = ed.api.post("/v1/images/generations", json={"model": "kgguf", "prompt": "x", "output_format": "webp"})
    assert gen.status_code == 200
    r = edit(ed.api, "kgguf", [png(512, 512)], prompt="x <sd_cpp_extra_args>{}</sd_cpp_extra_args>")
    assert r.status_code == 400 and err(r)["param"] == "prompt"
    r = edit(ed.api, "sd", [png(512, 512)])
    assert r.status_code == 400 and err(r)["code"] == "edits_not_supported"
    assert "/v1/images/generations" in err(r)["message"] and not ed.e["sd"].seen


def test_fy_iedt_009_edits_wait_in_the_image_queue(ed):
    """FY-IEDT-009: an edit shares the deployment's queue with generations: while one request holds the replica the
    next waits in the gateway (never two at once at concurrency 1) and is answered once the first is done."""
    eng = ed.e["kgguf"]
    eng.gate = threading.Event()
    results = []
    t1 = threading.Thread(target=lambda: results.append(
        ed.api.post("/v1/images/generations", json={"model": "kgguf", "prompt": "first"}).status_code))
    t1.start()
    from tests.waits import wait_until
    wait_until(lambda: eng.inflight == 1, "the first request at the engine")
    t2 = threading.Thread(target=lambda: results.append(edit(ed.api, "kgguf", [png(256, 256)]).status_code))
    t2.start()
    wait_until(lambda: imagegen.lengths().get("kgguf#0") == 1, "the edit waiting in the queue")
    assert eng.most == 1 and len(eng.seen) == 1
    eng.gate.set()
    t1.join(15)
    t2.join(15)
    assert results == [200, 200] and eng.most == 1 and eng.edits()


def test_fy_iedt_010_variations_point_to_edits(ed):
    """FY-IEDT-010: /v1/images/variations stays closed (404 endpoint_not_supported) and says to use /v1/images/edits;
    an edit without a key is 401, to a model that does not exist 404 model_not_found."""
    r = ed.api.post("/v1/images/variations", json={"model": "klein"})
    assert r.status_code == 404 and err(r)["code"] == "endpoint_not_supported" and "/v1/images/edits" in err(r)["message"]
    assert api_client(ed.owner).post("/v1/images/edits", json={"model": "klein"}).status_code == 401
    r = ed.api.post("/v1/images/edits", json={"model": "nope", "prompt": "x", "images": [{"image_url": data_url(png(8, 8))}]})
    assert r.status_code == 404 and err(r)["code"] == "model_not_found"


def test_fy_iedt_011_nothing_of_an_edit_is_kept(ed, caplog):
    """FY-IEDT-011: an edit's prompt and image bytes appear in no log line and no usage row (only counts are kept)."""
    secret_prompt = "Paint the secret-prompt-7731 on the wall."
    a = png(300, 200, 77)
    with caplog.at_level(logging.DEBUG):
        assert edit(ed.api, "klein", [a], prompt=secret_prompt).status_code == 200
        recorder.flush()
    logs = caplog.text
    assert "secret-prompt-7731" not in logs and base64.b64encode(a)[:40].decode() not in logs
    rows = ed.owner.get("/api/usage?group=model").json()
    assert "secret-prompt-7731" not in json.dumps(rows)


# ------------------------------------------------------------------ recipes and the estimate
def test_fy_iedt_012_the_estimate_counts_input_images():
    """FY-IEDT-012: a model that edits adds its input images (activations for each input per image made) and one VAE
    encode; vLLM-Omni's pipelines resize inputs to about a megapixel (input_mp), so Largest input does not raise it,
    stable-diffusion.cpp's grows with it; more Input images raise it, up to what the model edits; a model that edits
    nothing is estimated as before (no input parts)."""
    b = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
    omni, sd = b["vllm-omni-image"], b["sd-cpp"]
    klein = diffusers_meta("black-forest-labs/FLUX.2-klein-4B", "Flux2KleinPipeline", edit=4, generate=True)
    base = {"gpus": 1, "max_images": 1, "max_side": 0, "max_input_images": 1, "max_input_side": 2048}
    e = estimate(klein, omni, base, device="gpu", gpu_context=0)
    parts = {p.label: p.bytes for p in e.parts}
    assert parts["Input images"] == int(omni.estimate.activation_per_mp * 1.0)
    assert parts["VAE encode"] == int(omni.estimate.vae_per_mp * 1.0)
    assert estimate(klein, omni, {**base, "max_input_side": 4096}, device="gpu", gpu_context=0).per_gpu == e.per_gpu
    two = estimate(klein, omni, {**base, "max_input_images": 2}, device="gpu", gpu_context=0)
    assert two.per_gpu - e.per_gpu == int(omni.estimate.activation_per_mp * 1.0)
    many = estimate(klein, omni, {**base, "max_input_images": 16}, device="gpu", gpu_context=0)
    four = estimate(klein, omni, {**base, "max_input_images": 4}, device="gpu", gpu_context=0)
    assert many.per_gpu == four.per_gpu
    plain = estimate(diffusers_meta("acme/z", "ZImagePipeline"), omni, base, device="gpu", gpu_context=0)
    assert not {"Input images", "VAE encode"} & {p.label for p in plain.parts}
    gg = {**base, "gpus": 0, "max_input_side": 1024}
    small = estimate(klein_gguf_meta(), sd, gg, device="cpu", gpu_context=0)
    large = estimate(klein_gguf_meta(), sd, {**gg, "max_input_side": 2048}, device="cpu", gpu_context=0)
    mp = 1024 * 1024 / 1e6
    assert {p.label: p.bytes for p in small.parts}["Input images"] == int(sd.estimate.activation_per_mp * mp)
    assert large.ram > small.ram
    sd_plain = estimate(sd15_meta(), sd, {"gpus": 0, "max_images": 1, "max_side": 0}, device="cpu", gpu_context=0)
    assert "Input images" not in {p.label for p in sd_plain.parts}


def test_fy_iedt_013_try_it_edits_with_an_edit_only_model(ed):
    """FY-IEDT-013: Try it on an edit-only deployment sends the test image to the replica's /v1/images/edits (a
    multipart image[] file) and answers the image's size; on a model that makes images from text it still sends a
    generation."""
    deps = {d["name"]: d["id"] for d in ed.owner.get("/api/deployments").json()["deployments"]}
    r = ed.owner.post(f"/api/deployments/{deps['qedit']}/try", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["endpoint"].endswith("v1/images/edits") and out["summary"].startswith("1 image")
    sent = ed.e["qedit"].edits()[-1]
    assert [name for name, _, _ in sent["files"]] == ["image[]"] and sent["files"][0][2][:8] == b"\x89PNG\r\n\x1a\n"
    r = ed.owner.post(f"/api/deployments/{deps['klein']}/try", json={})
    assert r.status_code == 200 and r.json()["endpoint"].endswith("v1/images/generations")


def test_fy_iedt_014_the_openai_sdk_edits(ed):
    """FY-IEDT-014: the official OpenAI SDK's images.edit (one image, then two) is answered in its own types; the
    engine gets the files as sent; images.create_variation gets the SDK's NotFoundError."""
    openai = pytest.importorskip("openai")
    c = openai.OpenAI(base_url="http://localhost/v1", api_key=ed.key, http_client=ed.api, max_retries=0)
    a, b = png(640, 480, 5), png(320, 320, 6)
    one = c.images.edit(model="klein", image=("a.png", a, "image/png"), prompt="Add a moon.")
    assert len(one.data) == 1 and base64.b64decode(one.data[0].b64_json)[:8] == b"\x89PNG\r\n\x1a\n"
    assert [data for _, _, data in ed.e["klein"].edits()[-1]["files"]] == [a]
    two = c.images.edit(model="klein", image=[("a.png", a, "image/png"), ("b.png", b, "image/png")],
                        prompt="Put the cat from the second image in the first.", n=2)
    assert len(two.data) == 2 and [data for _, _, data in ed.e["klein"].edits()[-1]["files"]] == [a, b]
    with pytest.raises(openai.NotFoundError):
        c.images.create_variation(model="klein", image=("a.png", a, "image/png"))
