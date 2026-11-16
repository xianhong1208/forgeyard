"""Vision models (1.4, #86): object detection, segmentation (SAM 2) and depth as model kinds of the catalog,
deployed with the built-in `vision` recipe (Forgeyard's own vision server, the model from the model cache)
and answered at /v1/vision/detect, /segment and /depth by model name: the recipe and its rendering, the
inspector and the licence rule, adding and re-tasking models, the estimate, the gateway (multipart and JSON,
only `model` changed, the answer passed back, one image / kilopixels / busy milliseconds in usage, the
wrong endpoint, keys, allowlists, rpm, no cloud), Try it, the Playground's options and console route, the
recommended entries and the Hugging Face search (FY-VIS-*)."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Optional

import pytest

from forgeyard import __version__
from forgeyard.gateway import proxy
from forgeyard.gateway.usage import recorder
from forgeyard.install.images import vision_image
from forgeyard.models import fetch, hub, inspect as inspector, recipes, recommended as rec, vision
from forgeyard.models.estimate import estimate
from forgeyard.models.recipes import variant_matches
from forgeyard.workloads import templates
from tests.cloud_helpers import FakeCloud, add_provider, deployment_id, model_id, set_tiers
from tests.gateway_helpers import api_client, make_ready, new_key
from tests.model_helpers import FakeHF, Repo, add_catalog, safetensors_bytes
from tests.test_scheduler import MODELS, deploy, replica_row
from tests.workload_helpers import make_node

FEATURES = MODELS + ("replica_keys", "replica_guard")
GIB = 1024**3
MIB = 1024**2
RTDETR_REV = "5650961749fa93567c0d46fc7f43ea4f9e914107"
SAM_REV = "de431c4043854a71d8101e17995dfe596bf101a5"
DEPTH_REV = "5426e4f0f36572d16453bbda7a8389317b1bef99"
A100 = {"vendor": "nvidia", "arch": "8.0", "driver": "580.178.04"}


def vision_meta(task: str, repo: str, rev: str, arch: str, size: int, **over) -> dict:
    """A vision model as the inspector reads it."""
    meta = {"kind": "hf", "repo": repo, "ref": "main", "revision": rev, "format": "safetensors",
            "pipeline_tag": {"detect": "object-detection", "segment": "mask-generation", "depth": "depth-estimation"}[task],
            "license": "apache-2.0", "modalities": [], "task_hint": task, "vision": {"task": task, "architecture": arch},
            "config": {"architectures": [arch]}, "weights": {"bytes": size, "params": {"F32": size // 4}},
            "files": [{"path": "config.json", "size": 2000, "oid": "a" * 40},
                      {"path": "model.safetensors", "size": size, "sha256": "b" * 64}],
            "size_bytes": size + 2000}
    meta.update(over)
    return meta


class FakeVision:
    """The vision server's HTTP API at http://127.0.0.1:<port>: POST /v1/vision/<task> (its task only) records
    the request and answers like images/vision (the size of the image it was given: `size`); every path
    needs the replica key. `status`/`error` make it fail."""

    def __init__(self, task: str, secret: str, size: tuple = (640, 480)):
        self.task, self.secret, self.size = task, secret, size
        self.seen: list[dict] = []
        self.status: Optional[int] = None
        self.answer: Optional[bytes] = None
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status: int, body: bytes):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                engine.seen.append({"path": self.path, "body": raw, "ctype": self.headers.get("Content-Type"),
                                    "auth": self.headers.get("Authorization")})
                if self.headers.get("Authorization") != f"Bearer {engine.secret}":
                    return self._send(401, b'{"error": {"message": "A valid API key is needed.", "code": "invalid_api_key"}}')
                if self.path != f"/v1/vision/{engine.task}":
                    return self._send(404, b'{"detail": "Not Found"}')
                if engine.status:
                    return self._send(engine.status, b'{"error": {"message": "The image could not be read.", '
                                                     b'"code": "invalid_image"}}')
                if engine.answer is not None:
                    return self._send(200, engine.answer)
                w, h = engine.size
                out: dict = {"model": "served", "width": w, "height": h, "ms": 12.5}
                if engine.task == "detect":
                    out["detections"] = [{"label": "cat", "score": 0.97, "box": [341.1, 25.1, 640.0, 372.9]}]
                elif engine.task == "segment":
                    out["masks"] = [{"score": 0.98, "area": 6400, "box": [40, 40, 120, 120], "png": "iVBORw0KGgo="}]
                else:
                    out.update(depth_png="iVBORw0KGgo=", min=0.8, max=5.2, unit="relative (larger is nearer)")
                self._send(200, json.dumps(out).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def vis(owner):
    """Three vision deployments on fake vision servers: `det` (RT-DETRv2 on a CPU machine), `seg` (SAM 2.1 on
    the A100's card #4) and `depth` (Depth Anything V2 Small on the CPU); a key."""
    add_catalog("rtdetr", vision_meta("detect", "PekingU/rtdetr_v2_r18vd", RTDETR_REV, "RtDetrV2ForObjectDetection",
                                      80 * MIB), task="detect", fmt="safetensors", source="hf://PekingU/rtdetr_v2_r18vd")
    add_catalog("sam", vision_meta("segment", "facebook/sam2.1-hiera-tiny", SAM_REV, "Sam2VideoModel", 156 * MIB),
                task="segment", fmt="safetensors", source="hf://facebook/sam2.1-hiera-tiny")
    add_catalog("dav2", vision_meta("depth", "depth-anything/Depth-Anything-V2-Small-hf", DEPTH_REV,
                                    "DepthAnythingForDepthEstimation", 99 * MIB),
                task="depth", fmt="safetensors", source="hf://depth-anything/Depth-Anything-V2-Small-hf")
    cpu = make_node("cpu-a", features=FEATURES)
    gpu = make_node("a100", allowlist=[4], features=FEATURES)
    engines = {"det": FakeVision("detect", "s-det"), "seg": FakeVision("segment", "s-seg"),
               "depth": FakeVision("depth", "s-depth", size=(256, 192))}
    deploy(owner, "det", "rtdetr", "vision", params={"gpus": 0}, machine_id=cpu, wait=True)
    deploy(owner, "seg", "sam", "vision", machine_id=gpu, wait=True)
    deploy(owner, "depth", "dav2", "vision", params={"gpus": 0}, machine_id=cpu, wait=True)
    for name, node in (("det", cpu), ("seg", gpu), ("depth", cpu)):
        make_ready(name, [(node, engines[name].port, engines[name].secret)])
    key = new_key(owner, "laptop")["key"]
    yield SimpleNamespace(owner=owner, cpu=cpu, gpu=gpu, engines=engines, key=key, api=api_client(owner, key))
    for e in engines.values():
        e.close()


def err(r) -> dict:
    body = r.json()
    assert set(body) == {"error"}, body
    return body["error"]


def detect(c, model: str = "det", **extra):
    return c.post("/v1/vision/detect", data={"model": model, "threshold": "0.6", **extra},
                  files={"image": ("cats.jpg", b"\xff\xd8\xff fake jpeg", "image/jpeg")})


def usage_rows(owner) -> list[dict]:
    recorder.flush()
    return owner.get("/api/usage?everyone=true&group=model").json()["rows"]


def spec_of(_owner, name: str) -> dict:
    """What the replica runs (the rendered spec the agent gets)."""
    return replica_row(name).spec


# ------------------------------------------------------------------ the recipe
def test_fy_vis_001_the_vision_recipe():
    """FY-VIS-001: the built-in `vision` recipe serves detect, segment and depth as vision-<task> with Forgeyard's
    own vision image at the server's version (the CUDA build on NVIDIA 7.5+ with driver 580, the CPU build
    for x86_64 and aarch64), the model from the cache (--model {model_path}) and the replica's key in
    VISION_API_KEY; a vision task served under another protocol, forgeyard_image beside image, or on a process
    variant are refused."""
    r = recipes.builtin_recipes()["vision"][0]
    assert r.tasks == ["detect", "segment", "depth"] and r.formats == ["safetensors"]
    assert r.api == {"detect": "vision-detect", "segment": "vision-segment", "depth": "vision-depth"}
    assert r.api_key_env == "VISION_API_KEY" and r.share == "vram" and r.estimate.method == "files"
    assert r.args[:4] == ["--task", "{task}", "--model", "{model_path}"] and "{served_name}" in r.args
    assert [(v.name, v.image) for v in r.variants] == [("cuda", vision_image(__version__, "cuda")),
                                                       ("cpu", vision_image(__version__, "cpu"))]
    assert r.variants[0].image.endswith(f"forgeyard-vision:{__version__}-cuda")
    assert variant_matches(r.variants[0], A100) is None
    assert variant_matches(r.variants[0], {**A100, "arch": "7.0"}) is not None       # CUDA 13: Turing and newer
    assert variant_matches(r.variants[0], {**A100, "driver": "575.51"}) is not None
    assert variant_matches(r.variants[1], {"vendor": "cpu", "arch": "aarch64"}) is None
    assert variant_matches(r.variants[0], {"vendor": "amd", "arch": "gfx1151"}) is not None  # AMD: the CPU build
    # The templates' image is the same one.
    assert templates.image("cuda") == r.variants[0].image and templates.image("cpu") == r.variants[1].image
    text = recipes.builtin_recipes()["vision"][1]
    for bad, why in [(text.replace("detect: vision-detect", "detect: openai-chat"), "vision-detect"),
                     (text.replace("forgeyard_image: vision-cuda", "forgeyard_image: vision-cuda\n    image: x/y:1"),
                      "instead of image"),
                     (text.replace("forgeyard_image: vision\n", "forgeyard_image: rocm\n"), "forgeyard_image"),
                     (text.replace("  - name: cpu\n    when: {vendor: cpu, arch: [x86_64, aarch64]}\n    runtime: container",
                                   "  - name: cpu\n    when: {vendor: cpu, arch: [x86_64, aarch64]}\n    runtime: process"),
                      "forgeyard_image")]:
        assert bad != text
        with pytest.raises(recipes.RecipeError, match=why):
            recipes.parse_recipe(bad)


def test_fy_vis_002_which_configs_are_vision_models():
    """FY-VIS-002: an …ForObjectDetection architecture is detect, SAM 2 (model_type sam2 / sam2_video, Sam2…)
    segment, …ForDepthEstimation depth; anything else is no vision model. A DETR on a timm backbone is a
    detection model with a problem; the classes of a detection model are kept."""
    assert vision.task_of({"architectures": ["RtDetrV2ForObjectDetection"]}) == "detect"
    assert vision.task_of({"architectures": ["DFineForObjectDetection"]}) == "detect"
    assert vision.task_of({"architectures": ["Sam2VideoModel"], "model_type": "sam2_video"}) == "segment"
    assert vision.task_of({"architectures": ["Sam2Model"]}) == "segment"
    assert vision.task_of({"model_type": "sam2"}) == "segment"
    assert vision.task_of({"architectures": ["DepthAnythingForDepthEstimation"]}) == "depth"
    for config in ({"architectures": ["Qwen3ForCausalLM"]}, {"architectures": ["SamModel"], "model_type": "sam"},
                   {"architectures": ["SegformerForSemanticSegmentation"]}, {}, {"architectures": [7]}):
        assert vision.task_of(config) is None and vision.summary(config) is None
    s = vision.summary({"architectures": ["DetrForObjectDetection"], "use_timm_backbone": True,
                        "id2label": {"1": "person", "0": "N/A", "2": "bicycle"}})
    assert s["task"] == "detect" and s["labels"] == 3 and s["label_examples"] == ["N/A", "person", "bicycle"]
    assert "timm" in s["problem"]
    assert "problem" not in vision.summary({"architectures": ["RtDetrV2ForObjectDetection"], "id2label": {"0": "cat"}})


def _vision_repos() -> list[Repo]:
    st = safetensors_bytes({"w": {"dtype": "F32", "shape": [64], "data_offsets": [0, 256]}}, b"\0" * 256)
    det = Repo("acme/rtdetr", "1" * 40, {"config.json": json.dumps({
        "architectures": ["RtDetrV2ForObjectDetection"], "model_type": "rt_detr_v2", "id2label": {"0": "person", "1": "cat"}}).encode(),
        "model.safetensors": st, "preprocessor_config.json": b"{}", "pytorch_model.bin": b"p" * 50})
    det.info.update(pipeline_tag="object-detection", cardData={"license": "apache-2.0"})
    # As SAM 2.1's own files: a vision_config and a video preprocessor, which would read as chat media.
    sam = Repo("acme/sam2", "2" * 40, {"config.json": json.dumps({"architectures": ["Sam2VideoModel"],
                                                                  "model_type": "sam2_video",
                                                                  "vision_config": {"model_type": "sam2_vision_model"}}).encode(),
                                       "model.safetensors": st, "processor_config.json": b"{}",
                                       "video_preprocessor_config.json": b"{}", "sam2.1_hiera_t.pt": b"t" * 70})
    sam.info.update(pipeline_tag="mask-generation", cardData={"license": "apache-2.0"})
    nc = Repo("acme/depth-large", "3" * 40, {"config.json": json.dumps({
        "architectures": ["DepthAnythingForDepthEstimation"]}).encode(), "model.safetensors": st})
    nc.info.update(pipeline_tag="depth-estimation", cardData={"license": "cc-by-nc-4.0"})
    none = Repo("acme/depth-nolicence", "4" * 40, {"config.json": json.dumps({
        "architectures": ["DPTForDepthEstimation"]}).encode(), "model.safetensors": st})
    none.info.update(pipeline_tag="depth-estimation")
    timm = Repo("acme/detr", "5" * 40, {"config.json": json.dumps({
        "architectures": ["DetrForObjectDetection"], "use_timm_backbone": True}).encode(), "model.safetensors": st})
    timm.info.update(pipeline_tag="object-detection", cardData={"license": "apache-2.0"})
    chat = Repo("acme/chatty", "6" * 40, {"config.json": json.dumps({"architectures": ["Qwen3ForCausalLM"],
                                                                     "num_attention_heads": 8}).encode(),
                                          "model.safetensors": st})
    chat.info.update(cardData={"license": "apache-2.0"})
    return [det, sam, nc, none, timm, chat]


def test_fy_vis_003_the_inspector_reads_vision_models():
    """FY-VIS-003: a detection repository is read as one (its architecture and classes, task hint detect, no
    chat media), a SAM 2 repository as segment (its vision_config and video preprocessor are no chat media)
    without its .pt checkpoint, and both download only the
    safetensors weights and the configs."""
    hf = FakeHF(*_vision_repos())
    try:
        policy = fetch.Policy(allow_private=True)
        m = inspector.inspect_hf("acme/rtdetr", None, None, endpoint=hf.url, policy=policy)
        assert m["task_hint"] == "detect" and m["modalities"] == [] and m["license"] == "apache-2.0"
        assert m["vision"] == {"task": "detect", "architecture": "RtDetrV2ForObjectDetection", "labels": 2,
                               "label_examples": ["person", "cat"]}
        assert {f["path"] for f in m["files"]} == {"config.json", "model.safetensors", "preprocessor_config.json"}
        s = inspector.inspect_hf("acme/sam2", None, None, endpoint=hf.url, policy=policy)
        assert s["task_hint"] == "segment" and s["vision"]["architecture"] == "Sam2VideoModel" and s["modalities"] == []
        assert "sam2.1_hiera_t.pt" not in {f["path"] for f in s["files"]}
        c = inspector.inspect_hf("acme/chatty", None, None, endpoint=hf.url, policy=policy)
        assert "vision" not in c and c["task_hint"] != "detect"
    finally:
        hf.close()


def test_fy_vis_004_adding_vision_models_and_the_licence_rule(owner, monkeypatch):
    """FY-VIS-004: a detection model is added with its task from Hugging Face and the vision recipe; one asked
    for another vision task, one with a non-commercial licence or none, one on a timm backbone, and a chat
    model asked to detect are refused (422 not_a_vision_model). A model cannot be re-tasked to a vision task
    it is not."""
    from forgeyard.config import get_settings

    hf = FakeHF(*_vision_repos())
    try:
        monkeypatch.setenv("HF_ENDPOINT", hf.url)
        monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
        get_settings.cache_clear()
        r = owner.post("/api/models", json={"name": "rtdetr", "source": "hf://acme/rtdetr"})
        assert r.status_code == 201, r.text
        m = r.json()["model"]
        assert m["task"] == "detect" and m["task_label"] == "Object detection" and m["recipes"] == ["vision"]
        assert m["vision"]["architecture"] == "RtDetrV2ForObjectDetection" and m["vision"]["labels"] == 2
        for name, source, task, words in [
                ("as-depth", "hf://acme/rtdetr", "depth", "object detection model"),
                ("nc", "hf://acme/depth-large", None, "cc-by-nc-4.0"),
                ("unlicensed", "hf://acme/depth-nolicence", None, "no licence"),
                ("timm", "hf://acme/detr", None, "timm"),
                ("chat-detect", "hf://acme/chatty", "detect", "not a object detection model")]:
            body = {"name": name, "source": source, **({"task": task} if task else {})}
            r = owner.post("/api/models", json=body)
            assert r.status_code == 422, (name, r.text)
            assert r.json()["detail"]["code"] == "not_a_vision_model" and words in r.json()["detail"]["message"], name
        chat = owner.post("/api/models", json={"name": "chatty", "source": "hf://acme/chatty", "task": "chat"})
        assert chat.status_code == 201, chat.text
        r = owner.patch(f"/api/models/{chat.json()['model']['id']}", json={"task": "segment"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "not_a_vision_model"
        r = owner.patch(f"/api/models/{m['id']}", json={"task": "detect", "title": "RT-DETR"})
        assert r.status_code == 200 and r.json()["model"]["title"] == "RT-DETR"
        prev = owner.post("/api/models/inspect", json={"source": "hf://acme/sam2"}).json()
        assert prev["recipes"] == ["vision"] and prev["suggestion"]["recipe"] == "vision"
        assert prev["suggestion"]["devices"] == ["gpu", "cpu"]
    finally:
        hf.close()


def test_fy_vis_005_rendering_and_the_estimate(vis):
    """FY-VIS-005: a replica runs Forgeyard's vision image with --task <the model's task>, the cache mount as
    --model and its served name, the replica's key and VISION_THREADS; on a CPU its threads count against the
    machine; on the A100 the CUDA build on one shared card. The estimate is the files plus the recipe's
    overhead (1 GiB and the CUDA context on a GPU, 1.25 GiB of RAM on a CPU)."""
    det = spec_of(vis.owner, "det")
    assert det["runtime"]["image"] == vision_image(__version__, "cpu")
    args = det["runtime"]["args"]
    assert args[:4] == ["--task", "detect", "--model", "${MODEL_PATH}"]
    assert args[args.index("--served-name") + 1] == "det" and args[args.index("--port") + 1] == "8000"
    assert det["env"]["VISION_THREADS"] == "4" and det["env"]["HF_HUB_OFFLINE"] == "1"
    assert "VISION_API_KEY" not in det["env"]          # set per replica by the agent, never in the spec
    assert det["resources"]["cpu"] == 4.0 and det["resources"]["gpus"]["count"] == 0
    seg = spec_of(vis.owner, "seg")
    assert seg["runtime"]["image"] == vision_image(__version__, "cuda") and seg["runtime"]["args"][1] == "segment"
    assert seg["resources"]["gpus"]["count"] == 1 and seg["resources"]["gpus"]["share"] == "vram"
    r = recipes.builtin_recipes()["vision"][0]
    meta = vision_meta("detect", "a/b", RTDETR_REV, "RtDetrV2ForObjectDetection", 80 * MIB)
    cpu = estimate(meta, r, {"gpus": 0, "threads": 4}, device="cpu", gpu_context=500 * MIB)
    assert cpu.ram == 80 * MIB + 2000 + 1280 * MIB
    gpu = estimate(meta, r, {"gpus": 1}, device="gpu", gpu_context=500 * MIB)
    assert gpu.per_gpu == 80 * MIB + 2000 + GIB + 500 * MIB


# ------------------------------------------------------------------ the gateway
def test_fy_vis_006_multipart_and_json_reach_the_vision_server(vis):
    """FY-VIS-006: /v1/vision/detect with a multipart image goes to the deployment's replica at
    /v1/vision/detect with its internal key and the form as it came (only `model` changed, the client's key
    never); JSON with a base64 image the same; the answer comes back as the vision server gave it, naming the
    replica."""
    r = detect(vis.api)
    assert r.status_code == 200, r.text
    assert r.json()["detections"][0]["label"] == "cat" and r.headers["x-forgeyard-target"] == "local:det#0"
    seen = vis.engines["det"].seen[-1]
    assert seen["path"] == "/v1/vision/detect" and seen["auth"] == "Bearer s-det" and vis.key not in seen["auth"]
    assert seen["ctype"].startswith("multipart/form-data") and b"\xff\xd8\xff fake jpeg" in seen["body"]
    assert b'name="threshold"\r\n\r\n0.6' in seen["body"] and b'name="model"\r\n\r\ndet' in seen["body"]
    image = base64.b64encode(b"\x89PNG fake").decode()
    r = vis.api.post("/v1/vision/segment", json={"model": "seg", "image": image, "box": [40, 40, 120, 120]})
    assert r.status_code == 200 and r.json()["masks"][0]["area"] == 6400
    sent = json.loads(vis.engines["seg"].seen[-1]["body"])
    assert sent == {"model": "seg", "image": image, "box": [40, 40, 120, 120]}
    r = vis.api.post("/v1/vision/depth", json={"model": "depth", "image": "data:image/png;base64," + image})
    assert r.status_code == 200 and r.json()["unit"].startswith("relative")


def test_fy_vis_007_requests_that_never_reach_a_replica(vis):
    """FY-VIS-007: no image (multipart without an `image` file, JSON without `image`), no model, streaming, a
    body that is neither, and a model of another task (400 wrong_endpoint, naming the right path) are
    refused before anything is sent; a model that does not exist is 404."""
    c = vis.api
    cases = [
        (c.post("/v1/vision/detect", data={"model": "det"}, files={"picture": ("a.png", b"x", "image/png")}), 400, "missing_image"),
        (c.post("/v1/vision/detect", data={"model": "det", "image": "not a file"},
                files={"other": ("a.png", b"x", "image/png")}), 400, "missing_image"),
        (c.post("/v1/vision/detect", json={"model": "det"}), 400, "missing_image"),
        (c.post("/v1/vision/detect", json={"model": "det", "image": "  "}), 400, "missing_image"),
        (c.post("/v1/vision/detect", json={"image": "eA=="}), 400, "invalid_request"),
        (c.post("/v1/vision/detect", json={"model": "det", "image": "eA==", "stream": True}), 400, "streaming_not_supported"),
        (c.post("/v1/vision/detect", content=b"\x89PNG", headers={"Content-Type": "image/png"}), 400, "invalid_json"),
        (c.post("/v1/vision/depth", json={"model": "det", "image": "eA=="}), 400, "wrong_endpoint"),
        (c.post("/v1/chat/completions", json={"model": "det", "messages": []}), 400, "wrong_endpoint"),
        (c.post("/v1/vision/detect", json={"model": "nope", "image": "eA=="}), 404, "model_not_found"),
    ]
    for r, status, code in cases:
        assert r.status_code == status and err(r)["code"] == code, r.text
    assert "/v1/vision/detect" in err(cases[7][0])["message"]
    assert all(not e.seen for e in vis.engines.values())


def test_fy_vis_008_usage_counts_one_image_and_its_kilopixels(vis):
    """FY-VIS-008: an answered request counts one image, its kilopixels (from the size the answer says) and the
    replica's busy milliseconds, under the model; a refused image (the engine's 400, passed on) and an
    answer that is no vision answer (502) count no image."""
    assert detect(vis.api).status_code == 200
    assert vis.api.post("/v1/vision/depth", json={"model": "depth", "image": "eA=="}).status_code == 200
    vis.engines["det"].status = 400
    r = detect(vis.api)
    assert r.status_code == 400 and err(r)["code"] == "invalid_image"
    vis.engines["det"].status = None
    vis.engines["det"].answer = b'{"detections": []}'
    r = detect(vis.api)
    assert r.status_code == 502 and err(r)["code"] == "upstream_error"
    rows = {x["id"]: x for x in usage_rows(vis.owner)}
    assert rows["det"]["requests"] == 3 and rows["det"]["images"] == 1
    assert rows["det"]["kilopixels"] == round(640 * 480 / 1000)
    assert rows["depth"]["images"] == 1 and rows["depth"]["kilopixels"] == round(256 * 192 / 1000)
    assert rows["det"]["busy_ms"] >= 0 and rows["det"]["errors"] == 2


def test_fy_vis_009_keys_allowlists_rpm_and_no_cloud(vis, monkeypatch):
    """FY-VIS-009: no key is 401; a key whose allowlist lacks the model is 403 (the allowlist applies as for
    every model); rpm refuses the next request; a vision route takes no provider target (422), and a key
    whose cloud setting is on gets 503 when no replica is ready, never a provider."""
    assert detect(api_client(vis.owner)).status_code == 401
    narrow = new_key(vis.owner, "narrow", models=["seg"])["key"]
    r = detect(api_client(vis.owner, narrow))
    assert r.status_code == 403 and err(r)["code"] == "model_not_allowed"
    rpm = new_key(vis.owner, "rpm", rpm=1)["key"]
    assert detect(api_client(vis.owner, rpm)).status_code == 200
    r = detect(api_client(vis.owner, rpm))
    assert r.status_code == 429 and err(r)["code"] == "rate_limit_exceeded"
    from forgeyard.config import get_settings
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    cloud = FakeCloud("stub")
    try:
        p = add_provider(vis.owner, cloud, name="cloudy")
        set_tiers(vis.owner, "det", [[("local", deployment_id(vis.owner, "det"))],
                                     [("provider", model_id(p, "gpt-mini"))]], status=422)
        on = new_key(vis.owner, "on", cloud="on")["key"]
        from tests.gateway_helpers import set_state
        set_state("det", 0, "starting")
        r = detect(api_client(vis.owner, on))
        assert r.status_code == 503 and err(r)["code"] == "no_ready_replica"
        assert not cloud.seen
    finally:
        cloud.close()
    # Even a route that somehow had a provider target would not offer it for a vision endpoint.
    route = proxy.Route("det", "vision-detect", tiers=[proxy.Tier(0, [], [SimpleNamespace(external=False)])])
    assert proxy.allowed_tiers(route, "on", False, "vision/detect") == []


def test_fy_vis_010_try_it_and_models_list(vis):
    """FY-VIS-010: a deployment's Try it sends the test image (and, for SAM 2, the box around its red square)
    straight to a replica with its key, and answers with a summary, without the image or the masks' PNGs;
    /v1/models lists the vision models."""
    deps = {d["name"]: d["id"] for d in vis.owner.get("/api/deployments").json()["deployments"]}
    r = vis.owner.post(f"/api/deployments/{deps['det']}/try", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["summary"] == "1 object: cat 0.97" and out["api"] == "vision-detect"
    sent = json.loads(vis.engines["det"].seen[-1]["body"])
    assert base64.b64decode(sent["image"]) == vision.test_png() and vision.test_png()[:8] == b"\x89PNG\r\n\x1a\n"
    assert out["request"]["image"].startswith("<the test image")
    r = vis.owner.post(f"/api/deployments/{deps['seg']}/try", json={}).json()
    assert json.loads(vis.engines["seg"].seen[-1]["body"])["box"] == vision.TEST_BOX
    assert r["summary"].startswith("1 mask; the best covers 6400 pixels") and "png" not in json.dumps(r["answer"])
    r = vis.owner.post(f"/api/deployments/{deps['depth']}/try", json={}).json()
    assert r["summary"].startswith("Depth map 256x192") and "depth_png" not in r["answer"]
    names = {m["id"] for m in vis.api.get("/v1/models").json()["data"]}
    assert {"det", "seg", "depth"} <= names


def test_fy_vis_011_the_playground(vis, client):
    """FY-VIS-011: the Playground's options list the vision models with their task, endpoint, catalog model,
    licence and ready replicas (beside the exposed workloads); the console reaches a model at
    /api/playground/vision/<task> as the signed-in person (its session and CSRF rule, counted as console),
    and not with an API key."""
    o = vis.owner.get("/api/playground/vision/options").json()
    models = {m["name"]: m for m in o["models"]}
    assert set(models) == {"det", "seg", "depth"} and o["workloads"] == []
    assert models["det"] | {"deployment_id": None} == {
        "name": "det", "task": "detect", "title": "Object detection", "endpoint": "/v1/vision/detect",
        "deployment_id": None, "model": "PekingU/rtdetr_v2_r18vd", "licence": "apache-2.0", "ready": 1,
        "running": True, "reason": None}
    files = {"image": ("cats.jpg", b"\xff\xd8 jpeg", "image/jpeg")}
    r = vis.owner.post("/api/playground/vision/detect", data={"model": "det"}, files=files)
    assert r.status_code == 200 and r.json()["detections"][0]["label"] == "cat"
    r = vis.owner.post("/api/playground/vision/detect", data={"model": "det"}, files=files, headers={"X-Forgeyard": ""})
    assert r.status_code == 403
    anon = api_client(vis.owner, vis.key)
    assert anon.post("/api/playground/vision/detect", data={"model": "det"}, files=files,
                     headers={"X-Forgeyard": "1"}).status_code == 401
    assert anon.get("/api/playground/vision/options").status_code == 401
    rows = usage_rows(vis.owner)
    assert any(x["id"] == "det" and x["images"] == 1 for x in rows)
    by_source = {x["id"]: x for x in vis.owner.get("/api/usage?everyone=true&group=source").json()["rows"]}
    assert by_source["console"]["images"] == 1
    from tests.gateway_helpers import set_state
    set_state("seg", 0, "starting")
    seg = {m["name"]: m for m in vis.owner.get("/api/playground/vision/options").json()["models"]}["seg"]
    assert seg["ready"] == 0 and seg["reason"] and "replica 0" in seg["reason"]


# ------------------------------------------------------------------ the recommended list, the search
def test_fy_vis_012_the_recommended_vision_models():
    """FY-VIS-012: the three models of the 1.3 templates are recommended, at the same pinned revisions as the
    vision image's models.json, with the vision recipe on the CPU and Apache-2.0; a vision entry with a
    licence that is not permissive (even one the list allows for others) or without a pinned commit is refused."""
    entries = {e.task: e for e in rec.recommended().models if e.task in vision.TASKS}
    assert set(entries) == {"detect", "segment", "depth"}
    baked = templates.models_file()
    for task, e in entries.items():
        assert (e.source, e.revision) == (f"hf://{baked[task]['repo']}", baked[task]["revision"])
        assert e.recipe == "vision" and e.device == "cpu" and e.license == "apache-2.0" and e.group == "detection"
    data = rec.load().model_dump(exclude_defaults=False)
    i = next(n for n, m in enumerate(data["models"]) if m["task"] == "detect")
    for change, why in [({"license": "creativeml-openrail-m"}, "permissive"), ({"revision": "main"}, "pinned")]:
        broken = json.loads(json.dumps(data))
        broken["models"][i].update(change)
        with pytest.raises(rec.RecommendedError, match=why):
            rec.check(broken)


def test_fy_vis_013_searching_hugging_face_for_vision_models():
    """FY-VIS-013: the search's detect, segment and depth ask Hugging Face for object-detection,
    mask-generation and depth-estimation; a result says its task and the licence its tags state."""
    q = hub.make_query("", "detect", None, 10)
    assert any("pipeline_tag=object-detection" in u for u in hub.search_urls("https://hf.example", q))
    assert hub.TASK_TAGS["segment"] == ("mask-generation",) and hub.TASK_TAGS["depth"] == ("depth-estimation",)
    s = hub.summarise({"id": "PekingU/rtdetr_v2_r50vd", "pipeline_tag": "object-detection", "library_name": "transformers",
                       "tags": ["transformers", "safetensors", "license:apache-2.0"], "downloads": 10})
    assert s["task"] == "detect" and s["license"] == "apache-2.0" and s["format"] == "safetensors"
    assert hub.summarise({"id": "a/b", "tags": []})["license"] is None
    with pytest.raises(ValueError):
        hub.make_query("", "segmentation", None, 10)


def test_fy_vis_014_render_values_carry_the_task(vis):
    """FY-VIS-014: {task} is the model's own task, so one recipe starts the server for each of the three."""
    assert spec_of(vis.owner, "depth")["runtime"]["args"][:2] == ["--task", "depth"]
    assert "task" in recipes.BUILTIN_PLACEHOLDERS


def test_fy_vis_015_the_server_and_the_catalog_agree_on_what_a_vision_model_is():
    """FY-VIS-015: the vision server's own task_of (images/vision/app.py, which needs torch, so it is read from
    the file here) gives the same task as the catalog's for every kind of config: what the catalog accepts is
    what the server starts with."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "images" / "vision" / "app.py").read_text()
    tree = ast.parse(src)
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "task_of"
            or isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "SEGMENT_TYPES" for t in n.targets)]
    assert len(keep) == 2
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "app.py", "exec"), ns)
    configs = [{"architectures": ["RtDetrV2ForObjectDetection"]}, {"architectures": ["Sam2VideoModel"], "model_type": "sam2_video"},
               {"model_type": "sam2"}, {"architectures": ["DPTForDepthEstimation"]}, {"architectures": ["SamModel"]},
               {"architectures": ["Qwen3ForCausalLM"]}, {}, {"architectures": [None, "YolosForObjectDetection"]}]
    assert [ns["task_of"](c) for c in configs] == [vision.task_of(c) for c in configs]
    assert [vision.task_of(c) for c in configs] == ["detect", "segment", "segment", "depth", None, None, None, "detect"]
