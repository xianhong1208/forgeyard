"""Vision models (1.4): object detection, promptable segmentation and depth as model kinds of the
catalog, served by Forgeyard's own vision server (images/vision) through the built-in `vision` recipe and
answered at /v1/vision/<task>.

What counts as one, read from a transformers repository's config.json (the same rule as the vision
server's own `task_of`, so what the catalog accepts is what the server loads):

- **detect**: an architecture ending in `ForObjectDetection` (RT-DETR / RT-DETRv2, D-FINE, DETR,
  Conditional / Deformable DETR, YOLOS, …), loaded with `AutoModelForObjectDetection`. DETR checkpoints
  that use a timm backbone (`use_timm_backbone`) are refused: the vision server has no timm.
  **Zero-shot (open-vocabulary) detectors** (1.4.4, #142: Grounding DINO, MM-Grounding-DINO / LLMDet,
  OWL-ViT, OWLv2) are detect models too, marked `zero_shot` ("finds what you name"): their
  architectures end in `ForObjectDetection` as well, but transformers loads them only with
  `AutoModelForZeroShotObjectDetection`, and a request names what to find (`labels`). 1.4.3 (#147) refused
  them; a row added before that is known by the model_type and architectures the catalog kept, so it needs
  no migration. They run on a vision image from 1.4.4 on (`deploy_refusal`). OmDet-Turbo is refused: its
  transformers port loads a timm backbone.
- **segment**: SAM 2 / 2.1 (`model_type` sam2 or sam2_video, `Sam2…` architectures), a mask for what a
  point or a box shows, loaded with `Sam2Model`.
- **depth**: an architecture ending in `ForDepthEstimation` (Depth Anything, DPT, GLPN, ZoeDepth, …),
  loaded with `AutoModelForDepthEstimation`.

**Licences:** only permissive ones (Apache-2.0, MIT, BSD), as the 1.3 templates: a vision model from
Hugging Face whose card states another licence, or none, is refused when it is added (Depth Anything V2
Base and Large are CC-BY-NC-4.0; Ultralytics' YOLO checkpoints AGPL-3.0). A local model's licence cannot
be read: whoever adds it answers for it.
"""

from __future__ import annotations

import re
from typing import Optional

TASKS = ("detect", "segment", "depth")
API = {"detect": "vision-detect", "segment": "vision-segment", "depth": "vision-depth"}
ENDPOINT = {t: f"vision/{t}" for t in TASKS}           # under /v1
LABEL = {"detect": "Object detection", "segment": "Segmentation", "depth": "Depth estimation"}
PIPELINE_TAGS = {"object-detection": "detect", "zero-shot-object-detection": "detect", "mask-generation": "segment",
                 "depth-estimation": "depth"}
SEGMENT_TYPES = ("sam2", "sam2_video")
PERMISSIVE = ("apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause")

# Zero-shot (open-vocabulary) detectors: transformers' MODEL_FOR_ZERO_SHOT_OBJECT_DETECTION_MAPPING_NAMES in the
# version the vision image pins (5.23.0, images/vision/requirements-*.txt), model_type -> architecture. None of
# them is in MODEL_FOR_OBJECT_DETECTION_MAPPING_NAMES: the vision server loads them with
# AutoModelForZeroShotObjectDetection, and each request names what to find (1.4.4, #142). Matched
# by model_type and by architecture, so a checkpoint that spells one of the two differently is still caught.
# FY-KIND-002 fails when the image's transformers moves: check the mapping again then.
ZERO_SHOT_CHECKED_WITH = "5.23.0"
ZERO_SHOT_DETECT = {
    "grounding-dino": "GroundingDinoForObjectDetection",
    "mm-grounding-dino": "MMGroundingDinoForObjectDetection",     # also LLMDet's checkpoints
    "omdet-turbo": "OmDetTurboForObjectDetection",
    "owlv2": "Owlv2ForObjectDetection",
    "owlvit": "OwlViTForObjectDetection",
}
_ZERO_SHOT_ARCHS = {a.lower() for a in ZERO_SHOT_DETECT.values()}
# The vision image that first runs zero-shot detection (images/vision/app.py). The built-in recipe pins an image
# by digest, moved by a PR after the release that published it (docs/development.md, Releasing): until then a
# zero-shot detector is not deployed on an older pinned image, which would fail to load it.
ZERO_SHOT_SINCE = (1, 4, 4)
# OmDet-Turbo's transformers port loads its Swin backbone through timm (use_timm_backbone), which the vision image
# does not include: such a model is refused, like DETR with a timm backbone (a row added before 1.4.3 when deployed).
NEEDS_TIMM = ("omdet-turbo",)
TIMM_PROBLEM = "it uses a timm backbone (use_timm_backbone), which Forgeyard's vision server does not include"
VISION_IMAGE_RE = re.compile(r"/forgeyard-vision:(\d+)\.(\d+)\.(\d+)(?:-[a-z0-9]+)?(?:@|$)")


def task_of(config: dict) -> Optional[str]:
    """The vision task a transformers config.json is for, or None."""
    archs = [a for a in config.get("architectures") or [] if isinstance(a, str)]
    if config.get("model_type") in SEGMENT_TYPES or any(a.startswith("Sam2") for a in archs):
        return "segment"
    if any(a.endswith("ForObjectDetection") for a in archs):
        return "detect"
    if any(a.endswith("ForDepthEstimation") for a in archs):
        return "depth"
    return None


def _model_type(config: dict) -> str:
    return str(config.get("model_type") or "").strip().lower().replace("_", "-")


def zero_shot(config: dict) -> bool:
    """A zero-shot (open-vocabulary) object detector: its model_type or one of its architectures is one of
    transformers' zero-shot detection models (ZERO_SHOT_DETECT)."""
    kind = _model_type(config)
    archs = [a.lower() for a in config.get("architectures") or [] if isinstance(a, str)]
    return kind in ZERO_SHOT_DETECT or any(a in _ZERO_SHOT_ARCHS for a in archs)


def summary(config: dict) -> Optional[dict]:
    """What the catalog keeps about a vision model (meta["vision"]): its task, architecture and, for
    detection, how many classes it knows and a few of their names. None: not a vision model."""
    task = task_of(config)
    if task is None:
        return None
    archs = [a for a in config.get("architectures") or [] if isinstance(a, str)]
    out: dict = {"task": task, "architecture": (archs[0] if archs else str(config.get("model_type") or ""))[:64]}
    if task == "detect" and zero_shot(config):
        # Its classes are what each request names: config.json's id2label means nothing for it.
        out["zero_shot"] = True
        if config.get("use_timm_backbone") or _model_type(config) in NEEDS_TIMM:
            out["problem"] = TIMM_PROBLEM
        return out
    labels = config.get("id2label")
    if task == "detect" and isinstance(labels, dict):
        names = [str(v)[:40] for _, v in sorted(labels.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)]
        out["labels"] = len(names)
        out["label_examples"] = names[:8]
    if task == "detect" and config.get("use_timm_backbone"):
        out["problem"] = TIMM_PROBLEM
    return out


def refusal(task: str, meta: dict) -> Optional[str]:
    """Why a model cannot be added as a vision model of `task` (a message), or None. Local models are not
    read by the server, so only their declared task counts."""
    if meta.get("kind") == "local":
        return None
    v = meta.get("vision") if isinstance(meta.get("vision"), dict) else None
    if v is None:
        return (f"This repository is not a {LABEL[task].lower()} model Forgeyard's vision server can run: it needs a "
                "transformers config.json with an …ForObjectDetection or …ForDepthEstimation architecture, or SAM 2.")
    if v.get("task") != task:
        return f"This is a {LABEL[v['task']].lower()} model ({v.get('architecture')}), not one for {LABEL[task].lower()}."
    if v.get("problem"):
        return f"This model cannot run on Forgeyard's vision server: {v['problem']}."
    lic = meta.get("license")
    if lic not in PERMISSIVE:
        return (f"Vision models are added with a permissive licence only ({', '.join(PERMISSIVE)}); this one's card says "
                f"{lic or 'no licence'}.")
    return None


def is_zero_shot(meta: dict) -> bool:
    """A catalog model that is a zero-shot detector: flagged when it was added (1.4.4), or known from the model_type
    and architectures the catalog kept of its config.json (a row added before 1.4.3, which did not flag it)."""
    v = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    if v.get("task") not in (None, "detect"):
        return False
    cfg = meta.get("config") if isinstance(meta.get("config"), dict) else {}
    archs = [a for a in list(cfg.get("architectures") or []) + [v.get("architecture")] if isinstance(a, str)]
    return bool(v.get("zero_shot")) or zero_shot({"model_type": cfg.get("model_type"), "architectures": archs})


# What a zero-shot detector needs beyond its files and the recipe's overhead, by model_type (1.4.4): Grounding DINO
# resizes to 800 px and runs a BERT text encoder; measured on a CPU with transformers 5.23 (peak RSS after three
# 640x480 requests, four threads): Grounding DINO tiny and MM-Grounding-DINO tiny 2.24 GiB with 0.64 GiB of files,
# about 0.35 GiB over the files plus 1.25 GiB. OWLv2 base (1.13 GiB) and OWL-ViT base (0.94) stay within.
ZERO_SHOT_EXTRA = {"grounding-dino": 512 * 1024**2, "mm-grounding-dino": 512 * 1024**2}


def estimate_extra(meta: dict) -> int:
    """Bytes a vision model needs beyond its files and the vision recipe's overhead (ZERO_SHOT_EXTRA), 0 for most."""
    if not is_zero_shot(meta):
        return 0
    cfg = meta.get("config") if isinstance(meta.get("config"), dict) else {}
    v = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    kind = _model_type(cfg)
    if not kind:
        arch = str(v.get("architecture") or "").lower()
        kind = next((t for t, a in ZERO_SHOT_DETECT.items() if a.lower() == arch), "")
    return ZERO_SHOT_EXTRA.get(kind, 0)


def image_version(image: Optional[str]) -> Optional[tuple[int, int, int]]:
    """X.Y.Z of a Forgeyard vision image reference (docker.io/<ns>/forgeyard-vision:1.4.0[-cuda][@sha256:…]), or
    None: another image (a custom recipe's local build), or no version in its tag."""
    m = VISION_IMAGE_RE.search(image or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def deploy_refusal(task: str, meta: dict, recipe) -> Optional[tuple[str, str]]:
    """Why a vision model in the catalog cannot be deployed with `recipe` (an error code and a message), or None:
    a zero-shot detector that needs timm (OmDet-Turbo, kept by a catalog from before 1.4.3), or one on a recipe whose
    Forgeyard vision image is older than the one that runs them (ZERO_SHOT_SINCE). The image of every container variant counts
    (where it lands is decided later); an image of another name (a local build in a custom recipe) is taken as it
    is. A local model is not read: whoever adds it answers for it."""
    if task != "detect" or meta.get("kind") == "local" or not is_zero_shot(meta):
        return None
    cfg = meta.get("config") if isinstance(meta.get("config"), dict) else {}
    v = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    archs = {a.lower() for a in list(cfg.get("architectures") or []) + [v.get("architecture")] if isinstance(a, str)}
    if _model_type(cfg) in NEEDS_TIMM or archs & {ZERO_SHOT_DETECT[t].lower() for t in NEEDS_TIMM}:
        return "not_a_vision_model", f"This model cannot run on Forgeyard's vision server: {TIMM_PROBLEM}."
    if recipe is None:
        return None
    old = sorted({v for v in (image_version(getattr(x, "image", None)) for x in getattr(recipe, "variants", []) or [])
                  if v is not None and v < ZERO_SHOT_SINCE})
    if not old:
        return None
    need = ".".join(map(str, ZERO_SHOT_SINCE))
    return "vision_image_too_old", (
        f"This zero-shot detector needs Forgeyard's vision image {need} or newer; the {recipe.name} recipe runs "
        f"{'.'.join(map(str, old[0]))}. The built-in recipe moves to the new image right after the release that "
        "publishes it; until then, a custom recipe can name a newer image.")


# ------------------------------------------------------------------ the test image (Try it, the smoke test)
TEST_SIZE = (256, 192)
TEST_BOX = [40, 40, 120, 120]      # the red square: what Try it asks a segmentation model to outline
TEST_LABELS = ["red square", "blue rectangle"]   # what Try it asks a zero-shot detector to find (1.4.4)
MAX_LABELS = 32                    # labels per request (the vision server's own limits, images/vision/app.py)
MAX_LABEL_CHARS = 64


def try_labels(text: Optional[str]) -> list[str]:
    """The labels Try it sends a zero-shot detector: the words typed (comma-separated), else TEST_LABELS."""
    words = [w.strip()[:MAX_LABEL_CHARS] for w in (text or "").split(",") if w.strip()]
    return words[:MAX_LABELS] or list(TEST_LABELS)


def test_png() -> bytes:
    """A small PNG without Pillow: a red and a blue square on grey, 256 x 192 (images/vision/testimage.py)."""
    import struct
    import zlib

    w, h = TEST_SIZE

    def pixel(x: int, y: int) -> bytes:
        if 40 <= x < 120 and 40 <= y < 120:
            return b"\xd0\x20\x20"
        if 150 <= x < 220 and 60 <= y < 160:
            return b"\x20\x40\xd0"
        return b"\x80\x80\x80"

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"".join(pixel(x, y) for x in range(w)) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def summarise(task: str, answer: dict) -> str:
    """One line about a vision answer (Try it)."""
    if task == "detect":
        found = answer.get("detections") or []
        if not found:
            return "No objects found above the threshold (the test image is two coloured squares)."
        return f"{len(found)} object{'s' if len(found) != 1 else ''}: " + ", ".join(
            f"{d.get('label')} {float(d.get('score') or 0):.2f}" for d in found[:6])
    if task == "segment":
        masks = answer.get("masks") or []
        if not masks:
            return "No mask."
        best = masks[0]
        return f"{len(masks)} mask{'s' if len(masks) != 1 else ''}; the best covers {best.get('area')} pixels " \
               f"(score {float(best.get('score') or 0):.2f}, box {best.get('box')})"
    return f"Depth map {answer.get('width')}x{answer.get('height')} (relative, {answer.get('min')} to {answer.get('max')})"
