"""Ready-made workload templates (1.3): vision services that run Forgeyard's own vision server image
(images/vision) and are exposed at /x/<name> straight away. Choosing one on the Workloads page fills the
new-workload form: the image (the CPU or the CUDA build), the GPU, the port, the health check and the
gateway setting; everything stays editable.

Only permissively licensed models and code (recorded per template, shown like the catalog shows a
licence): RT-DETRv2 for detection rather than Ultralytics YOLOv8/11 (AGPL-3.0); SAM 2.1; Depth Anything
V2 *Small* only (Base and Large are CC-BY-NC-4.0). The models are baked into the image at the revisions
in images/vision/models.json, so a template runs on a machine without the network once its image is
there (`docker load`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from forgeyard import __version__
from forgeyard.install import images
from forgeyard.workloads.spec import WorkloadSpec

MODELS_FILE = Path(__file__).resolve().parents[2] / "images" / "vision" / "models.json"
PORT = 8000
GPU_VRAM = "3Gi"       # what one replica reserves on a shared card (the models need well under 2 GiB)
PERMISSIVE = ("apache-2.0", "mit", "bsd-3-clause", "bsd-2-clause")


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    task: str                 # the vision server's --task
    name: str                 # the default workload name
    summary: str
    model: str                # hf repository
    revision: str
    licence: str              # the model's licence (Hugging Face id)
    code_licence: str         # the serving code's: transformers, the vision server
    endpoint: str
    example: str              # a request body, for the docs and the workload page
    answer: str


# The model facts are in models.json (the image's own pins); read here so the two never drift. Kept in
# code too, so the server works without the images/ folder (a wheel install): the test FY-X-029 checks
# that both say the same.
_MODELS = {
    "detect": ("PekingU/rtdetr_v2_r18vd", "5650961749fa93567c0d46fc7f43ea4f9e914107", "apache-2.0"),
    "segment": ("facebook/sam2.1-hiera-tiny", "de431c4043854a71d8101e17995dfe596bf101a5", "apache-2.0"),
    "depth": ("depth-anything/Depth-Anything-V2-Small-hf", "5426e4f0f36572d16453bbda7a8389317b1bef99", "apache-2.0"),
}

TEMPLATES: tuple[Template, ...] = (
    Template("object-detection", "Object detection", "detect", "detector",
             "RT-DETRv2 (ResNet-18): boxes and labels for the 80 COCO classes in an image.",
             *_MODELS["detect"], "Apache-2.0", "/v1/detect", "-F image=@street.jpg -F threshold=0.5",
             '{"detections": [{"label": "cat", "score": 0.97, "box": [341, 25, 640, 373]}], …}'),
    Template("segmentation", "Segmentation", "segment", "segmenter",
             "SAM 2.1 (Hiera tiny): the mask of what a point or a box shows.",
             *_MODELS["segment"], "Apache-2.0", "/v1/segment", "-F image=@street.jpg -F 'points=[[320, 240]]'",
             '{"masks": [{"score": 0.98, "area": 47887, "box": [13, 59, 315, 472], "png": "<base64>"}], …}'),
    Template("depth", "Depth estimation", "depth", "depth",
             "Depth Anything V2 Small: relative depth for every pixel, as a 16-bit PNG.",
             *_MODELS["depth"], "Apache-2.0", "/v1/depth", "-F image=@street.jpg",
             '{"depth_png": "<base64>", "min": 0.81, "max": 5.16, …}'),
)
BY_ID = {t.id: t for t in TEMPLATES}


def image(variant: str, version: str = __version__) -> str:
    return images.vision_image(version, variant)


def spec(t: Template, *, gpu: bool, name: Optional[str] = None, machine_id: Optional[int] = None) -> dict:
    """The workload a template makes (with its gateway setting): the CUDA build on one NVIDIA card it
    shares by memory, or the CPU build."""
    body: dict = {
        "name": name or t.name, "kind": "service",
        "runtime": {"type": "container", "image": image("cuda" if gpu else "cpu"), "args": ["--task", t.task]},
        "ports": [{"name": "http", "port": PORT}],
        "health": {"type": "http", "port": "http", "path": "/health", "interval": 5, "start_period": 300},
        "restart": "always",
        "resources": {"gpus": {"count": 1, "vendor": "nvidia", "share": "vram", "vram": GPU_VRAM}} if gpu else {},
    }
    if machine_id is not None:
        body["machine_id"] = machine_id
    WorkloadSpec.model_validate(body)  # a template is always a valid spec
    return {**body, "gateway": {"expose": True}}


def listing() -> list[dict]:
    out = []
    for t in TEMPLATES:
        out.append({"id": t.id, "title": t.title, "task": t.task, "name": t.name, "summary": t.summary,
                    "model": t.model, "revision": t.revision, "licence": t.licence, "code_licence": t.code_licence,
                    "model_url": f"https://huggingface.co/{t.model}", "endpoint": t.endpoint, "example": t.example,
                    "answer": t.answer, "images": {"cpu": image("cpu"), "cuda": image("cuda")},
                    "specs": {"cpu": spec(t, gpu=False), "cuda": spec(t, gpu=True)}})
    return out


def models_file() -> Optional[dict]:
    """images/vision/models.json, when the source tree is there (a checkout, not a wheel)."""
    try:
        return json.loads(MODELS_FILE.read_text())
    except OSError:
        return None
