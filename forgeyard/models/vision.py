"""Vision models (1.4): object detection, promptable segmentation and depth as model kinds of the
catalog, served by Forgeyard's own vision server (images/vision) through the built-in `vision` recipe and
answered at /v1/vision/<task>.

What counts as one, read from a transformers repository's config.json (the same rule as the vision
server's own `task_of`, so what the catalog accepts is what the server loads):

- **detect**: an architecture ending in `ForObjectDetection` (RT-DETR / RT-DETRv2, D-FINE, DETR,
  Conditional / Deformable DETR, YOLOS, …), loaded with `AutoModelForObjectDetection`. DETR checkpoints
  that use a timm backbone (`use_timm_backbone`) are refused: the vision server has no timm. So are
  zero-shot (open-vocabulary) detectors (Grounding DINO, MM-Grounding-DINO / LLMDet, OWL-ViT, OWLv2,
  OmDet-Turbo, 1.4.3, #147): their architectures end in `ForObjectDetection` too, but transformers loads
  them only with `AutoModelForZeroShotObjectDetection`, and they find what a text prompt names (#142).
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

from typing import Optional

TASKS = ("detect", "segment", "depth")
API = {"detect": "vision-detect", "segment": "vision-segment", "depth": "vision-depth"}
ENDPOINT = {t: f"vision/{t}" for t in TASKS}           # under /v1
LABEL = {"detect": "Object detection", "segment": "Segmentation", "depth": "Depth estimation"}
PIPELINE_TAGS = {"object-detection": "detect", "mask-generation": "segment", "depth-estimation": "depth"}
SEGMENT_TYPES = ("sam2", "sam2_video")
PERMISSIVE = ("apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause")

# Zero-shot (open-vocabulary) detectors: transformers' MODEL_FOR_ZERO_SHOT_OBJECT_DETECTION_MAPPING_NAMES in the
# version the vision image pins (5.23.0, images/vision/requirements-*.txt), model_type -> architecture. None of
# them is in MODEL_FOR_OBJECT_DETECTION_MAPPING_NAMES, so AutoModelForObjectDetection cannot load them. Matched
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
ZERO_SHOT_PROBLEM = ("it is a zero-shot (open-vocabulary) detector, which finds what a text prompt names; "
                     "support comes with #142")


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


def zero_shot(config: dict) -> bool:
    """A zero-shot (open-vocabulary) object detector: its model_type or one of its architectures is one of
    transformers' zero-shot detection models (ZERO_SHOT_DETECT)."""
    kind = str(config.get("model_type") or "").strip().lower().replace("_", "-")
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
    labels = config.get("id2label")
    if task == "detect" and isinstance(labels, dict):
        names = [str(v)[:40] for _, v in sorted(labels.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)]
        out["labels"] = len(names)
        out["label_examples"] = names[:8]
    if task == "detect" and config.get("use_timm_backbone"):
        out["problem"] = "it uses a timm backbone (use_timm_backbone), which Forgeyard's vision server does not include"
    if task == "detect" and zero_shot(config):
        out["zero_shot"] = True
        out["problem"] = ZERO_SHOT_PROBLEM
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


def deploy_refusal(task: str, meta: dict) -> Optional[str]:
    """Why a vision model already in the catalog cannot be deployed (a message), or None: a zero-shot detector
    added before 1.4.3 refused them (#147), read from what the catalog kept of its config.json (its model_type
    and architectures). Nothing is migrated: such a row stays in the catalog, its deployments are refused."""
    if task != "detect" or meta.get("kind") == "local":
        return None
    cfg = meta.get("config") if isinstance(meta.get("config"), dict) else {}
    v = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    archs = [a for a in list(cfg.get("architectures") or []) + [v.get("architecture")] if isinstance(a, str)]
    if v.get("zero_shot") or zero_shot({"model_type": cfg.get("model_type"), "architectures": archs}):
        return f"This model cannot run on Forgeyard's vision server: {ZERO_SHOT_PROBLEM}."
    return None


# ------------------------------------------------------------------ the test image (Try it, the smoke test)
TEST_SIZE = (256, 192)
TEST_BOX = [40, 40, 120, 120]      # the red square: what Try it asks a segmentation model to outline


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
