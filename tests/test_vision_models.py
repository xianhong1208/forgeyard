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
# The vision recipe's pinned images (1.4.1, #113): moved by scripts/pin_images.py --write after a release that
# changed images/vision.
VISION_CPU = ("docker.io/tonyhuang1101/forgeyard-vision:1.4.0"
              "@sha256:ab60eb2164671b9158573e6afe39068dcf407cc4286d053b118e25de29c244d6")
VISION_CUDA = ("docker.io/tonyhuang1101/forgeyard-vision:1.4.0-cuda"
               "@sha256:773449716b20e961df38b1518422b0120b3e919239063ac128ef5b4affca47b3")


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
        self.refuse: Optional[tuple] = None    # (status, body): an error the vision server itself gives
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
                if engine.refuse:
                    return self._send(*engine.refuse)
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
                elif engine.task == "classify":
                    out.update(labels=[{"label": "tabby, tabby cat", "score": 0.86}, {"label": "Egyptian cat", "score": 0.07}],
                               multi_label=False)
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
    """FY-VIS-001: the built-in `vision` recipe serves detect, segment, depth and (1.4.4) image-classify as
    vision-detect, -segment, -depth and -classify with Forgeyard's
    own vision image, pinned by digest (1.4.1: the CUDA build on NVIDIA 7.5+ with driver 580, the CPU build for
    x86_64 and aarch64), the model from the cache (--model {model_path}) and the replica's key in
    VISION_API_KEY; a vision task served under another protocol, forgeyard_image beside image, an unknown
    forgeyard_image, or forgeyard_image on a process variant are refused."""
    r = recipes.builtin_recipes()["vision"][0]
    assert r.tasks == ["detect", "segment", "depth", "image-classify"] and r.formats == ["safetensors"]
    assert r.api == {"detect": "vision-detect", "segment": "vision-segment", "depth": "vision-depth",
                     "image-classify": "vision-classify"}
    assert r.api_key_env == "VISION_API_KEY" and r.share == "vram" and r.estimate.method == "files"
    assert r.args[:4] == ["--task", "{task}", "--model", "{model_path}"] and "{served_name}" in r.args
    assert [(v.name, v.image) for v in r.variants] == [("cuda", VISION_CUDA), ("cpu", VISION_CPU)]
    assert all(v.forgeyard_image is None for v in r.variants)
    assert variant_matches(r.variants[0], A100) is None
    assert variant_matches(r.variants[0], {**A100, "arch": "7.0"}) is not None       # CUDA 13: Turing and newer
    assert variant_matches(r.variants[0], {**A100, "driver": "575.51"}) is not None
    assert variant_matches(r.variants[1], {"vendor": "cpu", "arch": "aarch64"}) is None
    assert variant_matches(r.variants[0], {"vendor": "amd", "arch": "gfx1151"}) is not None  # AMD: the CPU build
    text = recipes.builtin_recipes()["vision"][1]
    cuda_line, cpu_line = f"    image: {VISION_CUDA}\n", f"    image: {VISION_CPU}\n"
    assert cuda_line in text and cpu_line in text
    for bad, why in [(text.replace("detect: vision-detect", "detect: openai-chat"), "vision-detect"),
                     (text.replace("image-classify: vision-classify", "image-classify: vision-detect"),
                      "served as vision-classify"),
                     (text.replace(cuda_line, cuda_line + "    forgeyard_image: vision-cuda\n"), "instead of image"),
                     (text.replace(cpu_line, "    forgeyard_image: rocm\n"), "forgeyard_image"),
                     (text.replace("    runtime: container\n" + cpu_line,
                                   "    runtime: process\n    forgeyard_image: vision\n"), "forgeyard_image")]:
        assert bad != text
        with pytest.raises(recipes.RecipeError, match=why):
            recipes.parse_recipe(bad)


def test_fy_vis_019_the_vision_image_is_pinned_recorded_and_relayable(monkeypatch):
    """FY-VIS-019 (#113): the vision recipe's images are pinned by digest and recorded in images.lock.json (the
    CPU build for linux/amd64 and linux/arm64, the CUDA build for linux/amd64), so a loaded copy is checked by
    its ids and the server relays them (#102): to an aarch64 machine the arm64 build, from Docker Hub's API
    (registry-1.docker.io, though the recipe names docker.io). A server of another version runs the same images:
    only a PR moving the pin changes them."""
    from forgeyard.machines import relay
    from forgeyard.workloads import images as image_ids

    lock = image_ids.load_lock()
    assert VISION_CPU.startswith("docker.io/tonyhuang1101/forgeyard-vision:1.4.0@sha256:")
    assert VISION_CUDA.startswith("docker.io/tonyhuang1101/forgeyard-vision:1.4.0-cuda@sha256:")
    for image, platforms in [(VISION_CPU, {"linux/amd64", "linux/arm64"}), (VISION_CUDA, {"linux/amd64"})]:
        entry = lock[image]
        assert entry["index"] == image.partition("@")[2] and set(entry["platforms"]) == platforms
        assert relay.relayable_image(image)
    arm = relay.image_target(SimpleNamespace(name="arm", platform={"arch": "aarch64"}), VISION_CPU)
    assert (arm.registry, arm.repo, arm.platform) == ("registry-1.docker.io", "tonyhuang1101/forgeyard-vision",
                                                      "linux/arm64")
    assert arm.manifest == lock[VISION_CPU]["platforms"]["linux/arm64"]["manifest"]
    # Docker Hub named in full is still Docker Hub's API (https://docker.io/v2/ is the website's redirect).
    from forgeyard.workloads.registry import split_image

    assert split_image("docker.io/acme/x:1") == split_image("acme/x:1") == ("registry-1.docker.io", "acme/x", "1", None)
    assert split_image("docker.io/python:3.13") == split_image("python:3.13") == split_image(
        "index.docker.io/library/python:3.13") == ("registry-1.docker.io", "library/python", "3.13", None)
    assert split_image("ghcr.io/a/b:t@sha256:" + "0" * 64) == ("ghcr.io", "a/b", "t", "sha256:" + "0" * 64)
    assert split_image("localhost:5000/x") == ("localhost:5000", "x", "latest", None)
    # A server upgrade does not move them.
    text = recipes.builtin_recipes()["vision"][1]
    monkeypatch.setattr("forgeyard.__version__", "9.9.9")
    again = recipes.parse_recipe(text, builtin=True)
    assert [v.image for v in again.variants] == [VISION_CUDA, VISION_CPU]


def test_fy_vis_020_forgeyard_image_still_names_the_servers_vision_image_in_a_custom_recipe(monkeypatch):
    """FY-VIS-020: a custom recipe saved from 1.4.0's vision recipe (forgeyard_image: vision / vision-cuda) still
    loads, and still names the published image of the server's version, tag only: not recorded, so not
    relayed (the built-in recipe names the pinned one)."""
    from forgeyard.machines import relay

    text = recipes.builtin_recipes()["vision"][1].replace("name: vision\n", "name: my-vision\n", 1)
    text = text.replace(f"image: {VISION_CUDA}", "forgeyard_image: vision-cuda").replace(
        f"image: {VISION_CPU}", "forgeyard_image: vision")
    r = recipes.parse_recipe(text)
    assert [v.image for v in r.variants] == [vision_image(__version__, "cuda"), vision_image(__version__, "cpu")]
    assert r.variants[1].image == f"docker.io/tonyhuang1101/forgeyard-vision:{__version__}"
    assert not any(relay.relayable_image(v.image) for v in r.variants)
    monkeypatch.setattr("forgeyard.__version__", "9.9.9")
    assert recipes.parse_recipe(text).variants[1].image == "docker.io/tonyhuang1101/forgeyard-vision:9.9.9"


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
    assert det["runtime"]["image"] == VISION_CPU
    args = det["runtime"]["args"]
    assert args[:4] == ["--task", "detect", "--model", "${MODEL_PATH}"]
    assert args[args.index("--served-name") + 1] == "det" and args[args.index("--port") + 1] == "8000"
    assert det["env"]["VISION_THREADS"] == "4" and det["env"]["HF_HUB_OFFLINE"] == "1"
    assert "VISION_API_KEY" not in det["env"]          # set per replica by the agent, never in the spec
    assert det["resources"]["cpu"] == 4.0 and det["resources"]["gpus"]["count"] == 0
    seg = spec_of(vis.owner, "seg")
    assert seg["runtime"]["image"] == VISION_CUDA and seg["runtime"]["args"][1] == "segment"
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
    licence and ready replicas (no apps since 1.4, #88); the console reaches a model at
    /api/playground/vision/<task> as the signed-in person (its session and CSRF rule, counted as console),
    and not with an API key."""
    o = vis.owner.get("/api/playground/vision/options").json()
    models = {m["name"]: m for m in o["models"]}
    assert set(models) == {"det", "seg", "depth"} and "workloads" not in o  # 1.4 (#88): no apps
    assert models["det"] | {"deployment_id": None} == {
        "name": "det", "task": "detect", "title": "Object detection", "endpoint": "/v1/vision/detect",
        "deployment_id": None, "model": "PekingU/rtdetr_v2_r18vd", "licence": "apache-2.0", "zero_shot": False,
        "ready": 1, "running": True, "reason": None}
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
    vision image's models.json, with the vision recipe on the CPU and Apache-2.0; so are three zero-shot detectors
    (1.4.4, #142: Grounding DINO tiny, MM-Grounding-DINO tiny, OWLv2 base), pinned to a commit; a vision entry with a
    licence that is not permissive (even one the list allows for others) or without a pinned commit is refused.
    1.4.4 (#141): five image classifiers in a group of their own (SigLIP 2 and LAION's CLIP zero-shot, MIT for
    CLIP; ViT, ConvNeXt V2, DINOv2 with ImageNet classes)."""
    classify = {e.name: e for e in rec.recommended().models if e.task == "image-classify"}
    assert set(classify) == {"siglip2-base", "clip-vit-b-32-laion", "vit-base-patch16-224", "convnextv2-tiny-22k-224",
                             "dinov2-small-imagenet1k"}
    for e in classify.values():
        assert e.recipe == "vision" and e.device == "cpu" and e.group == "image-classification" and len(e.revision) == 40
        assert e.license == ("mit" if e.name == "clip-vit-b-32-laion" else "apache-2.0"), e.name
    entries = [e for e in rec.recommended().models if e.task in ("detect", "segment", "depth")]
    baked = templates.models_file()
    fixed = {e.task: e for e in entries if e.source == f"hf://{baked[e.task]['repo']}"}
    assert set(fixed) == {"detect", "segment", "depth"}
    for task, e in fixed.items():
        assert (e.source, e.revision) == (f"hf://{baked[task]['repo']}", baked[task]["revision"])
        assert e.recipe == "vision" and e.device == "cpu" and e.license == "apache-2.0" and e.group == "detection"
    zero = {e.source: e for e in entries if e not in fixed.values()}
    assert set(zero) == {"hf://IDEA-Research/grounding-dino-tiny", "hf://google/owlv2-base-patch16-ensemble",
                         "hf://openmmlab-community/mm_grounding_dino_tiny_o365v1_goldg_v3det"}
    for e in zero.values():
        assert e.task == "detect" and e.recipe == "vision" and e.license == "apache-2.0" and e.group == "detection"
        assert len(e.revision) == 40 and "name" in e.note.lower(), e.source
    data = rec.load().model_dump(exclude_defaults=False)
    i = next(n for n, m in enumerate(data["models"]) if m["task"] == "detect")
    for change, why in [({"license": "creativeml-openrail-m"}, "permissive"), ({"revision": "main"}, "pinned")]:
        broken = json.loads(json.dumps(data))
        broken["models"][i].update(change)
        with pytest.raises(rec.RecommendedError, match=why):
            rec.check(broken)


def test_fy_vis_013_searching_hugging_face_for_vision_models():
    """FY-VIS-013: the search's detect, segment and depth ask Hugging Face for object-detection (and, 1.4.4,
    zero-shot-object-detection), mask-generation and depth-estimation; a result says its task and the licence its
    tags state."""
    q = hub.make_query("", "detect", None, 10)
    assert any("pipeline_tag=object-detection" in u for u in hub.search_urls("https://hf.example", q))
    # 1.4.4 (#142): zero-shot detectors are detection models too
    assert any("pipeline_tag=zero-shot-object-detection" in u for u in hub.search_urls("https://hf.example", q))
    zs = hub.summarise({"id": "IDEA-Research/grounding-dino-tiny", "pipeline_tag": "zero-shot-object-detection",
                        "library_name": "transformers", "tags": ["safetensors", "license:apache-2.0"]})
    assert zs["task"] == "detect" and zs["license"] == "apache-2.0"
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
    funcs, names = {"task_of", "zero_shot_classifier", "classifier_type"}, {"SEGMENT_TYPES", "ZERO_SHOT_CLASSIFY"}
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in funcs
            or isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names for t in n.targets)]
    assert len(keep) == 5
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "app.py", "exec"), ns)
    configs = [{"architectures": ["RtDetrV2ForObjectDetection"]}, {"architectures": ["Sam2VideoModel"], "model_type": "sam2_video"},
               {"model_type": "sam2"}, {"architectures": ["DPTForDepthEstimation"]}, {"architectures": ["SamModel"]},
               {"architectures": ["Qwen3ForCausalLM"]}, {}, {"architectures": [None, "YolosForObjectDetection"]},
               # 1.4.4 (#141): classifiers with fixed classes, CLIP-like zero-shot ones (by architecture, or by
               # model_type alone), a fine-tuned SigLIP head (fixed), BLIP (not taken)
               {"architectures": ["ViTForImageClassification"]}, {"architectures": ["Siglip2Model"]},
               {"model_type": "clip"}, {"model_type": "chinese-clip"}, {"architectures": ["SiglipForImageClassification"]},
               {"architectures": ["BlipModel"], "model_type": "blip"}, {"model_type": "siglip2", "architectures": ["Other"]}]
    assert [ns["task_of"](c) for c in configs] == [vision.task_of(c) for c in configs]
    assert [vision.task_of(c) for c in configs] == ["detect", "segment", "segment", "depth", None, None, None, "detect",
                                                    "image-classify", "image-classify", "image-classify",
                                                    "image-classify", "image-classify", None, None]


# ------------------------------------------------------------------ zero-shot detectors (1.4.4, #142)
def test_fy_vis_021_the_server_and_the_catalog_agree_on_zero_shot_detectors():
    """FY-VIS-021: the vision server's own list of zero-shot detectors and its zero_shot rule (images/vision/app.py,
    read from the file) are the catalog's, and its label limits are those Try it keeps to."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "images" / "vision" / "app.py").read_text()
    tree = ast.parse(src)
    names = {"ZERO_SHOT_DETECT", "MAX_LABELS", "MAX_LABEL_CHARS"}
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "zero_shot"
            or isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names for t in n.targets)]
    assert len(keep) == 4
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "app.py", "exec"), ns)
    assert ns["ZERO_SHOT_DETECT"] == vision.ZERO_SHOT_DETECT
    assert (ns["MAX_LABELS"], ns["MAX_LABEL_CHARS"]) == (vision.MAX_LABELS, vision.MAX_LABEL_CHARS)
    configs = [{"model_type": "grounding-dino"}, {"model_type": "mm_grounding_dino"}, {"architectures": ["Owlv2ForObjectDetection"]},
               {"architectures": ["owlvitforobjectdetection"]}, {"model_type": "x", "architectures": ["OmDetTurboForObjectDetection"]},
               {"architectures": ["RtDetrV2ForObjectDetection"]}, {}, {"model_type": None, "architectures": [None]}]
    assert [ns["zero_shot"](c) for c in configs] == [vision.zero_shot(c) for c in configs] == [True] * 5 + [False] * 3
    assert vision.try_labels(None) == vision.TEST_LABELS and vision.try_labels(" , ") == vision.TEST_LABELS
    assert vision.try_labels("cat, remote control ,") == ["cat", "remote control"]
    assert len(vision.try_labels(",".join(f"l{i}" for i in range(40)))) == vision.MAX_LABELS
    assert vision.try_labels("x" * 100) == ["x" * vision.MAX_LABEL_CHARS]


NEW_CPU = "docker.io/tonyhuang1101/forgeyard-vision:1.4.4@sha256:" + "4" * 64
NEW_CUDA = "docker.io/tonyhuang1101/forgeyard-vision:1.4.4-cuda@sha256:" + "5" * 64


@pytest.fixture()
def zsd(vis):
    """A zero-shot detector (Grounding DINO tiny) deployed as `gd` with a copy of the vision recipe on a 1.4.4 image,
    on a fake vision server."""
    text = recipes.builtin_recipes()["vision"][1].replace("name: vision\n", "name: vision-144\n", 1)
    text = text.replace(f"image: {VISION_CUDA}", f"image: {NEW_CUDA}").replace(f"image: {VISION_CPU}", f"image: {NEW_CPU}")
    assert vis.owner.post("/api/recipes", json={"yaml": text}).status_code == 201
    meta = vision_meta("detect", "IDEA-Research/grounding-dino-tiny", "a" * 40, "GroundingDinoForObjectDetection",
                       660 * MIB, pipeline_tag="zero-shot-object-detection",
                       config={"architectures": ["GroundingDinoForObjectDetection"], "model_type": "grounding-dino"})
    meta["vision"]["zero_shot"] = True
    add_catalog("gdino", meta, task="detect", fmt="safetensors", source="hf://IDEA-Research/grounding-dino-tiny")
    engine = FakeVision("detect", "s-gd")
    deploy(vis.owner, "gd", "gdino", "vision-144", params={"gpus": 0}, machine_id=vis.cpu, wait=True)
    make_ready("gd", [(vis.cpu, engine.port, engine.secret)])
    yield SimpleNamespace(vis=vis, engine=engine)
    engine.close()


def test_fy_vis_022_labels_reach_a_zero_shot_detector(zsd):
    """FY-VIS-022: `labels` reach the zero-shot detector's replica as the client sent them (a multipart field, or a
    JSON list), with only `model` changed; the vision server's refusal (400 labels_required) comes back unchanged and
    is counted as no image; the Playground lists it as zero_shot (the others not); Try it sends it the test image
    with the names of its two shapes, or the words typed (comma-separated), and a detector with fixed classes gets
    no labels."""
    vis, engine = zsd.vis, zsd.engine
    r = detect(vis.api, "gd", labels='["cat", "remote control"]')
    assert r.status_code == 200, r.text
    assert b'name="labels"\r\n\r\n["cat", "remote control"]' in engine.seen[-1]["body"]
    image = base64.b64encode(b"\x89PNG fake").decode()
    r = vis.api.post("/v1/vision/detect", json={"model": "gd", "image": image, "labels": ["cat", "dog"], "threshold": 0.3})
    assert r.status_code == 200
    assert json.loads(engine.seen[-1]["body"]) == {"model": "gd", "image": image, "labels": ["cat", "dog"], "threshold": 0.3}
    before = sum(row.get("images") or 0 for row in usage_rows(vis.owner) if row.get("name") == "gd")
    engine.refuse = (400, b'{"error": {"message": "This model finds what you name: send labels.", "code": "labels_required"}}')
    r = vis.api.post("/v1/vision/detect", json={"model": "gd", "image": image})
    engine.refuse = None
    assert r.status_code == 400 and err(r)["code"] == "labels_required"
    after = sum(row.get("images") or 0 for row in usage_rows(vis.owner) if row.get("name") == "gd")
    assert before == 2 and after == 2
    models = {m["name"]: m for m in vis.owner.get("/api/playground/vision/options").json()["models"]}
    assert models["gd"]["zero_shot"] is True and models["det"]["zero_shot"] is False and models["seg"]["zero_shot"] is False
    deps = {d["name"]: d["id"] for d in vis.owner.get("/api/deployments").json()["deployments"]}
    out = vis.owner.post(f"/api/deployments/{deps['gd']}/try", json={}).json()
    assert out["ok"] and out["request"]["labels"] == ["red square", "blue rectangle"]
    assert json.loads(engine.seen[-1]["body"])["labels"] == vision.TEST_LABELS
    vis.owner.post(f"/api/deployments/{deps['gd']}/try", json={"input": "square, circle"})
    assert json.loads(engine.seen[-1]["body"])["labels"] == ["square", "circle"]
    vis.owner.post(f"/api/deployments/{deps['det']}/try", json={"input": "square"})
    assert "labels" not in json.loads(vis.engines["det"].seen[-1]["body"])


def test_fy_vis_028_grounding_dino_is_estimated_with_its_text_encoder():
    """FY-VIS-028: Grounding DINO and MM-Grounding-DINO (measured 2.24 GiB peak on a CPU with 0.64 GiB of files) get
    512 MiB beyond the files and the vision recipe's overhead, on a CPU and on a GPU, known by model_type or (a row
    from before 1.4.3) the architecture alone; OWLv2 and a detector with fixed classes get nothing more."""
    r = recipes.builtin_recipes()["vision"][0]
    gd = vision_meta("detect", "IDEA-Research/grounding-dino-tiny", "a" * 40, "GroundingDinoForObjectDetection", 660 * MIB,
                     config={"architectures": ["GroundingDinoForObjectDetection"], "model_type": "grounding-dino"})
    gd["vision"]["zero_shot"] = True
    cpu = estimate(gd, r, {"gpus": 0}, device="cpu", gpu_context=500 * MIB)
    assert cpu.ram == 660 * MIB + 2000 + 1280 * MIB + 512 * MIB
    assert cpu.parts[-1].label == "Zero-shot detector" and cpu.parts[-1].bytes == 512 * MIB
    gpu = estimate(gd, r, {"gpus": 1}, device="gpu", gpu_context=500 * MIB)
    assert gpu.per_gpu == 660 * MIB + 2000 + GIB + 500 * MIB + 512 * MIB
    old = vision_meta("detect", "acme/mm", "a" * 40, "MMGroundingDinoForObjectDetection", 660 * MIB)
    assert vision.estimate_extra(old) == 512 * MIB
    owl = vision_meta("detect", "google/owlv2-base-patch16-ensemble", "a" * 40, "Owlv2ForObjectDetection", 590 * MIB,
                      config={"architectures": ["Owlv2ForObjectDetection"], "model_type": "owlv2"})
    assert vision.estimate_extra(owl) == 0
    assert estimate(owl, r, {"gpus": 0}, device="cpu", gpu_context=0).ram == 590 * MIB + 2000 + 1280 * MIB
    rt = vision_meta("detect", "PekingU/rtdetr_v2_r18vd", RTDETR_REV, "RtDetrV2ForObjectDetection", 80 * MIB)
    assert vision.estimate_extra(rt) == 0


# ------------------------------------------------------------------ image classification (1.4.4, #141)
def test_fy_vis_029_the_server_and_the_catalog_agree_on_image_classifiers():
    """FY-VIS-029: the vision server's list of zero-shot image classifiers is the catalog's, and both read the same
    configs the same way (FY-VIS-015 covers task_of); image classification is its own kind, `image-classify`
    ("Image classification"), served as vision-classify at /v1/vision/classify, apart from text classification."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "images" / "vision" / "app.py").read_text()
    tree = ast.parse(src)
    keep = [n for n in tree.body if isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) in ("ZERO_SHOT_CLASSIFY", "PATHS") for t in n.targets)]
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "app.py", "exec"), ns)
    assert ns["ZERO_SHOT_CLASSIFY"] == vision.ZERO_SHOT_CLASSIFY
    assert set(vision.ZERO_SHOT_CLASSIFY) == {"align", "altclip", "chinese_clip", "clip", "metaclip_2", "siglip", "siglip2"}
    assert ns["PATHS"] == {"image-classify": "classify"} and vision.ENDPOINT["image-classify"] == "vision/classify"
    assert vision.API["image-classify"] == "vision-classify" and vision.LABEL["image-classify"] == "Image classification"
    assert proxy.ENDPOINTS["vision/classify"] == ("vision-classify", "vision")
    assert proxy.endpoint_for("vision-classify") == "/v1/vision/classify"
    assert "classify" not in recipes.TASKS          # text classification (#144) is another kind
    for cfg, zs in (({"architectures": ["CLIPModel"]}, True), ({"model_type": "siglip"}, True),
                    ({"architectures": ["CLIPForImageClassification"], "model_type": "clip"}, False),
                    ({"architectures": ["ViTForImageClassification"]}, False)):
        s = vision.summary(cfg)
        assert s["task"] == "image-classify" and bool(s.get("zero_shot")) is zs, cfg
    timm = vision.summary({"architectures": ["TimmWrapperForImageClassification"], "model_type": "timm_wrapper"})
    assert "timm" in timm["problem"]


def _repo(name: str, sha: str, extra: dict | None = None):
    """A real repository's config.json and Hugging Face answer (tests/fixtures/models/kinds), with small weights."""
    from tests.model_helpers import FIX

    info = json.loads((FIX / "kinds" / f"{name}.hf-info.json").read_text())
    st = safetensors_bytes({"w": {"dtype": "F32", "shape": [64], "data_offsets": [0, 256]}}, b"\0" * 256)
    files = {"config.json": (FIX / "kinds" / f"{name}.config.json").read_bytes(), "model.safetensors": st, **(extra or {})}
    r = Repo(info["id"], sha, files)
    r.info.update(pipeline_tag=info["pipeline_tag"], library_name=info["library_name"], tags=info["tags"],
                  cardData=info["cardData"])
    return r


def test_fy_vis_030_image_classifiers_are_added_from_hugging_face(owner, monkeypatch):
    """FY-VIS-030: SigLIP 2 (its config names no architecture, only model_type siglip) and LAION's CLIP are added as
    zero-shot image classifiers, ViT with its 1,000 ImageNet classes, all with the task Hugging Face's pipeline tag
    gives and the vision recipe; OpenCLIP's own copy beside LAION's transformers weights is neither read nor
    downloaded; another task asked for is refused, as is a licence that is not permissive."""
    from forgeyard.config import get_settings

    big = safetensors_bytes({"w": {"dtype": "F32", "shape": [4096], "data_offsets": [0, 16384]}}, b"\0" * 16384)
    server = FakeHF(_repo("siglip2-base-patch16-224", "1" * 40), _repo("vit-base-patch16-224", "2" * 40),
                    _repo("clip-vit-b-32-laion2b", "3" * 40, {"open_clip_model.safetensors": big,
                                                              "open_clip_config.json": b"{}"}))
    monkeypatch.setenv("HF_ENDPOINT", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    try:
        added = {}
        for name, source in (("siglip2", "hf://google/siglip2-base-patch16-224"), ("vit", "hf://google/vit-base-patch16-224"),
                             ("clip", "hf://laion/CLIP-ViT-B-32-laion2B-s34B-b79K")):
            r = owner.post("/api/models", json={"name": name, "source": source})
            assert r.status_code == 201, (name, r.text)
            added[name] = r.json()["model"]
        for name in added:
            assert added[name]["task"] == "image-classify" and added[name]["recipes"] == ["vision"], name
            assert added[name]["task_label"] == "Image classification"
        assert added["siglip2"]["vision"] == {"task": "image-classify", "architecture": "siglip", "zero_shot": True}
        assert added["clip"]["vision"]["zero_shot"] is True and added["clip"]["license"] == "mit"
        assert added["vit"]["vision"]["labels"] == 1000 and "zero_shot" not in added["vit"]["vision"]
        assert added["vit"]["vision"]["label_examples"][0] == "tench, Tinca tinca"
        assert added["clip"]["size_bytes"] < 16384                      # the OpenCLIP copy is not counted
        assert added["clip"]["summary"]["params"] == 64                  # nor read as weights
        prev = owner.post("/api/models/inspect", json={"source": "hf://laion/CLIP-ViT-B-32-laion2B-s34B-b79K"}).json()
        assert prev["meta"]["file_count"] == 2                          # config.json and model.safetensors
        r = owner.post("/api/models", json={"name": "vit-det", "source": "hf://google/vit-base-patch16-224", "task": "detect"})
        assert r.status_code == 422 and "image classification model" in r.json()["detail"]["message"]
        meta = {"kind": "hf", "license": "cc-by-nc-4.0", "vision": {"task": "image-classify", "architecture": "CLIPModel"}}
        assert "permissive licence only" in vision.refusal("image-classify", meta)
    finally:
        server.close()
        get_settings.cache_clear()


@pytest.fixture()
def cls(vis):
    """Two image classifiers on fake vision servers, on a copy of the vision recipe with a 1.4.4 image: `vit` (fixed
    ImageNet classes) and `siglip` (zero-shot)."""
    text = recipes.builtin_recipes()["vision"][1].replace("name: vision\n", "name: vision-144\n", 1)
    text = text.replace(f"image: {VISION_CUDA}", f"image: {NEW_CUDA}").replace(f"image: {VISION_CPU}", f"image: {NEW_CPU}")
    assert vis.owner.post("/api/recipes", json={"yaml": text}).status_code == 201
    vit = vision_meta("detect", "google/vit-base-patch16-224", "b" * 40, "ViTForImageClassification", 330 * MIB,
                      pipeline_tag="image-classification")
    vit["vision"] = {"task": "image-classify", "architecture": "ViTForImageClassification", "labels": 1000}
    sig = vision_meta("detect", "google/siglip2-base-patch16-224", "c" * 40, "siglip", 1430 * MIB,
                      pipeline_tag="zero-shot-image-classification", config={"architectures": [], "model_type": "siglip"})
    sig["vision"] = {"task": "image-classify", "architecture": "siglip", "zero_shot": True}
    add_catalog("vit-b", vit, task="image-classify", fmt="safetensors", source="hf://google/vit-base-patch16-224")
    add_catalog("siglip2", sig, task="image-classify", fmt="safetensors", source="hf://google/siglip2-base-patch16-224")
    engines = {"vit": FakeVision("classify", "s-vit"), "siglip": FakeVision("classify", "s-sig")}
    deploy(vis.owner, "vit", "vit-b", "vision-144", params={"gpus": 0}, machine_id=vis.cpu, wait=True)
    deploy(vis.owner, "siglip", "siglip2", "vision-144", params={"gpus": 0}, machine_id=vis.cpu, wait=True)
    for name in engines:
        make_ready(name, [(vis.cpu, engines[name].port, engines[name].secret)])
    yield SimpleNamespace(vis=vis, engines=engines)
    for e in engines.values():
        e.close()


def test_fy_vis_031_classify_through_the_gateway(cls):
    """FY-VIS-031: /v1/vision/classify reaches a classifier's replica at /v1/vision/classify (multipart and JSON, labels
    and top_k as sent, only `model` changed) and its answer comes back; a classifier at /v1/vision/detect, or a
    detector at /v1/vision/classify, is 400 wrong_endpoint; each answer counts one image; the Playground lists both
    (the zero-shot one marked) at /v1/vision/classify; Try it sends the zero-shot one the test image with three
    classes (or the words typed), the other none, and sums the answer up; /v1/models lists them."""
    vis, engines = cls.vis, cls.engines
    files = {"image": ("cats.jpg", b"\xff\xd8\xff fake jpeg", "image/jpeg")}
    r = vis.api.post("/v1/vision/classify", data={"model": "vit", "top_k": "3"}, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["labels"][0]["label"] == "tabby, tabby cat" and r.headers["x-forgeyard-target"] == "local:vit#0"
    seen = engines["vit"].seen[-1]
    assert seen["path"] == "/v1/vision/classify" and seen["auth"] == "Bearer s-vit" and b'name="top_k"\r\n\r\n3' in seen["body"]
    image = base64.b64encode(b"\x89PNG fake").decode()
    r = vis.api.post("/v1/vision/classify", json={"model": "siglip", "image": image, "labels": ["a cat", "a dog"]})
    assert r.status_code == 200
    assert json.loads(engines["siglip"].seen[-1]["body"]) == {"model": "siglip", "image": image, "labels": ["a cat", "a dog"]}
    r = vis.api.post("/v1/vision/detect", json={"model": "vit", "image": image})
    assert r.status_code == 400 and err(r)["code"] == "wrong_endpoint" and "/v1/vision/classify" in err(r)["message"]
    r = vis.api.post("/v1/vision/classify", json={"model": "det", "image": image})
    assert r.status_code == 400 and err(r)["code"] == "wrong_endpoint"
    rows = {row["name"]: row for row in usage_rows(vis.owner)}
    assert rows["vit"]["images"] == 1 and rows["siglip"]["images"] == 1 and rows["vit"]["kilopixels"] == 307
    models = {m["name"]: m for m in vis.owner.get("/api/playground/vision/options").json()["models"]}
    assert models["siglip"] | {"deployment_id": None} == {
        "name": "siglip", "task": "image-classify", "title": "Image classification", "endpoint": "/v1/vision/classify",
        "deployment_id": None, "model": "google/siglip2-base-patch16-224", "licence": "apache-2.0", "zero_shot": True,
        "ready": 1, "running": True, "reason": None}
    assert models["vit"]["zero_shot"] is False
    r = vis.owner.post("/api/playground/vision/classify", data={"model": "vit"}, files=files)
    assert r.status_code == 200 and r.json()["labels"][0]["label"] == "tabby, tabby cat"
    deps = {d["name"]: d["id"] for d in vis.owner.get("/api/deployments").json()["deployments"]}
    out = vis.owner.post(f"/api/deployments/{deps['siglip']}/try", json={}).json()
    assert out["ok"] and out["api"] == "vision-classify" and out["endpoint"].endswith("/v1/vision/classify")
    assert out["request"]["labels"] == vision.TEST_CLASSES and out["summary"] == "Best: tabby, tabby cat 0.86, Egyptian cat 0.07"
    vis.owner.post(f"/api/deployments/{deps['siglip']}/try", json={"input": "squares, circles"})
    assert json.loads(engines["siglip"].seen[-1]["body"])["labels"] == ["squares", "circles"]
    vis.owner.post(f"/api/deployments/{deps['vit']}/try", json={})
    assert "labels" not in json.loads(engines["vit"].seen[-1]["body"])
    names = {m["id"] for m in vis.api.get("/v1/models").json()["data"]}
    assert {"vit", "siglip"} <= names


def test_fy_vis_032_image_classifiers_need_the_1_4_4_vision_image(vis):
    """FY-VIS-032: on the built-in recipe while it pins a vision image older than 1.4.4, deploying or estimating an image
    classifier answers 422 vision_image_too_old (the old server has no image-classify task); its estimate on a newer
    image is its files plus the recipe's overhead."""
    vit = vision_meta("detect", "google/vit-base-patch16-224", "b" * 40, "ViTForImageClassification", 330 * MIB)
    vit["vision"] = {"task": "image-classify", "architecture": "ViTForImageClassification", "labels": 1000}
    add_catalog("vit-old", vit, task="image-classify", fmt="safetensors", source="hf://google/vit-base-patch16-224")
    body = {"name": "vit-old", "model": "vit-old", "recipe": "vision", "params": {"gpus": 0}, "machine_id": vis.cpu}
    for path in ("/api/deployments", "/api/deployments/estimate"):
        r = vis.owner.post(path, json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "vision_image_too_old", (path, r.text)
        assert r.json()["detail"]["message"].startswith("Image classification needs Forgeyard's vision image 1.4.4")
    est = estimate(vit, recipes.builtin_recipes()["vision"][0], {"gpus": 0}, device="cpu", gpu_context=0)
    assert est.ram == 330 * MIB + 2000 + 1280 * MIB
    q = hub.make_query("", "image-classify", None, 10)
    urls = hub.search_urls("https://hf.example", q)
    assert any("pipeline_tag=image-classification" in u for u in urls)
    assert any("pipeline_tag=zero-shot-image-classification" in u for u in urls)
