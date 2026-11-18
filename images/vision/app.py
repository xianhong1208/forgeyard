"""Forgeyard's vision server: one small HTTP API per task (object detection, segmentation, depth). Two ways
to run it:

- **A model from the machine's model cache (1.4, the built-in `vision` recipe):** `--model <dir>` is a
  transformers model directory (config.json, safetensors weights, the preprocessor's config), mounted
  read-only by the agent; any `AutoModelForObjectDetection` (RT-DETR, D-FINE, DETR without timm, …),
  `AutoModelForZeroShotObjectDetection` (1.4.4: Grounding DINO, MM-Grounding-DINO / LLMDet, OWLv2, OWL-ViT; not
  OmDet-Turbo, whose backbone needs timm), SAM 2 / 2.1 (`Sam2Model`) or `AutoModelForDepthEstimation` (Depth
  Anything, DPT, …) model; for image classification (1.4.4, `--task image-classify`, answered at
  /v1/vision/classify) any `AutoModelForImageClassification` (ViT, ConvNeXt V2, DINOv2 classifiers, …) or a
  CLIP-like model for zero-shot classification (CLIP, SigLIP, SigLIP 2, …). The gateway reaches it at
  /v1/vision/<path> by the deployment's model name.
- **A model baked into the image (1.3, the workload templates exposed at /x/<name>):** without `--model`,
  the task's model of models.json, fetched at a pinned revision when the image was built, so it starts
  without the network.

    python app.py --task detect|segment|depth|image-classify [--model DIR --served-name NAME] [--host 0.0.0.0] [--port 8000]
                  [--models /models] [--device cpu|cuda]

Endpoints (JSON; an image is sent as a multipart file `image`, or as JSON {"image": "<base64 or data: URI>"}):

    GET  /health               200 once the model is loaded (never needs the key)
    GET  /                     the task, the model, its licence and revision (baked models), the device, the endpoints
    POST /v1/vision/detect     detect: {"detections": [{"label", "score", "box": [x0, y0, x1, y1]}], "width", "height"}
    POST /v1/detect            options: threshold (0.5; zero-shot 0.3, OWL 0.1), max_detections (100), labels
                               (zero-shot detectors only, and required there: what to find, a list of names or a
                               comma-separated string; at most 32 of at most 64 characters)
    POST /v1/vision/segment    segment: a point ("points": [[x, y]], "labels": [1]) or a box ("box": [x0, y0, x1, y1]);
    POST /v1/segment           {"masks": [{"score", "box", "area", "png"}]}: each mask a 1-bit PNG, base64
    POST /v1/vision/depth      depth: {"depth_png": <16-bit PNG, base64, near = bright>, "min", "max", "width", "height"}
    POST /v1/depth             relative depth (Depth Anything V2), not metres
    POST /v1/vision/classify   image-classify (1.4.4, model cache only): {"labels": [{"label", "score"}], "multi_label"},
    POST /v1/classify          best first; options: top_k (5; a zero-shot model: all its labels), labels (zero-shot
                               models only, and required there), template ("This is a photo of {}.", zero-shot)

Only the started task's two paths answer (404 for the others). Every answer carries "model" (the served
name, or the baked model's repository), "ms", and for a baked model "revision". Errors:
{"error": {"message", "code"}} with 400 (a bad image or option; labels_required: a zero-shot detector was not
told what to find; labels_not_supported: a detector with fixed classes was given labels), 401 (a missing or wrong
key), 413 (more than MAX_IMAGE_MB) or 500.

**The key (1.4):** with VISION_API_KEY set (Forgeyard sets a random one per replica), every path but
/health needs `Authorization: Bearer <that key>`, compared in constant time; without it, nothing is asked
(the 1.3 templates, behind the agent's key guard). VISION_THREADS caps torch's CPU threads.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import io
import json
import os
import threading
import time
from pathlib import Path

import torch
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError

HERE = Path(__file__).resolve().parent
MODELS = json.loads((HERE / "models.json").read_text())
MAX_IMAGE_MB = int(os.environ.get("MAX_IMAGE_MB", "20"))
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


TASK_NAMES = ("detect", "segment", "depth", "image-classify")
# A task's paths (/v1/vision/<path>, /v1/<path>): its name, but for image classification (1.4.4).
PATHS = {"image-classify": "classify"}
# Zero-shot image classifiers (1.4.4, #141): the CLIP-like models of transformers'
# MODEL_FOR_ZERO_SHOT_IMAGE_CLASSIFICATION_MAPPING_NAMES (5.23.0) that score an image against each text, model_type ->
# architecture; the same list as forgeyard/models/vision.py (FY-VIS-029). BLIP (image-text matching), CLIPSeg
# (segmentation) and the video models of the mapping are left out. SigLIP's scores are sigmoids (each label on its
# own), the others' a softmax over the labels.
ZERO_SHOT_CLASSIFY = {
    "align": "AlignModel",
    "altclip": "AltCLIPModel",
    "chinese_clip": "ChineseCLIPModel",
    "clip": "CLIPModel",
    "metaclip_2": "MetaClip2Model",
    "siglip": "SiglipModel",
    "siglip2": "Siglip2Model",
}
SIGMOID_TYPES = ("siglip", "siglip2")
DEFAULT_TEMPLATE = "This is a photo of {}."
SEGMENT_TYPES = ("sam2", "sam2_video")
# Zero-shot (open-vocabulary) detectors (1.4.4, #142): transformers' MODEL_FOR_ZERO_SHOT_OBJECT_DETECTION_MAPPING_NAMES
# (5.23.0), model_type -> architecture; the same list as Forgeyard's forgeyard/models/vision.py (FY-VIS-021).
ZERO_SHOT_DETECT = {
    "grounding-dino": "GroundingDinoForObjectDetection",
    "mm-grounding-dino": "MMGroundingDinoForObjectDetection",
    "omdet-turbo": "OmDetTurboForObjectDetection",
    "owlv2": "Owlv2ForObjectDetection",
    "owlvit": "OwlViTForObjectDetection",
}
MAX_LABELS = 32
MAX_LABEL_CHARS = 64


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ------------------------------------------------------------------ the image
def decode_image(raw: bytes) -> Image.Image:
    if len(raw) > MAX_IMAGE_MB * 1024**2:
        raise Refused(413, "image_too_large", f"The image is larger than {MAX_IMAGE_MB} MiB.")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise Refused(400, "invalid_image", "The image could not be read (send a PNG, JPEG or WebP).") from None
    return img.convert("RGB")


async def read_request(request: Request) -> tuple[Image.Image, dict]:
    """(the image, the other fields) from a multipart form or a JSON body."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("image")
        if upload is None or isinstance(upload, str):
            raise Refused(400, "missing_image", "Send the image as a file field named 'image'.")
        fields = {}
        for k, v in form.items():
            if k != "image" and isinstance(v, str):
                try:
                    fields[k] = json.loads(v)
                except ValueError:
                    fields[k] = v
        return decode_image(await upload.read()), fields
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        raise Refused(400, "invalid_json", "Send multipart/form-data with an 'image' file, or a JSON object.") from None
    if not isinstance(body, dict) or not isinstance(body.get("image"), str):
        raise Refused(400, "missing_image", "The JSON body needs 'image': base64 or a data: URI.")
    data = body.pop("image")
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise Refused(400, "invalid_image", "'image' is not base64.") from None
    return decode_image(raw), body


def number(fields: dict, name: str, default: float, low: float, high: float) -> float:
    value = fields.get(name, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise Refused(400, "invalid_option", f"{name} must be a number.") from None
    if not low <= value <= high:
        raise Refused(400, "invalid_option", f"{name} must be between {low:g} and {high:g}.")
    return value


def png_b64(img: Image.Image) -> str:
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return base64.b64encode(out.getvalue()).decode("ascii")


# ------------------------------------------------------------------ the tasks
class Task:
    name = ""

    def __init__(self, path: Path, device: str):
        self.path, self.device = path, device
        self.lock = threading.Lock()  # one image at a time per replica: Forgeyard's concurrency spreads the rest

    def run(self, img: Image.Image, fields: dict) -> dict:
        raise NotImplementedError


def zero_shot(config: dict) -> bool:
    """A zero-shot detector, by its model_type or an architecture (the rule of forgeyard/models/vision.py)."""
    kind = str(config.get("model_type") or "").strip().lower().replace("_", "-")
    archs = {a.lower() for a in config.get("architectures") or [] if isinstance(a, str)}
    return kind in ZERO_SHOT_DETECT or bool(archs & {a.lower() for a in ZERO_SHOT_DETECT.values()})


def family(config: dict) -> str:
    """How a zero-shot detector is asked: "owl" (OWL-ViT, OWLv2: one text query per label) or "grounding" (Grounding
    DINO and its re-trainings, MM-Grounding-DINO and LLMDet: one lower-case prompt, "a cat. a remote control.").
    OmDet-Turbo needs timm (use_timm_backbone), which this image does not have: check_model_dir refuses it."""
    kind = str(config.get("model_type") or "").strip().lower().replace("_", "-")
    archs = " ".join(a.lower() for a in config.get("architectures") or [] if isinstance(a, str))
    return "owl" if kind.startswith("owl") or "owl" in archs else "grounding"


def label_list(fields: dict) -> list[str] | None:
    """The request's `labels` (what a zero-shot detector is to find), checked: None when it sent none; a list of
    names, or one comma-separated string (a form field), at most MAX_LABELS of at most MAX_LABEL_CHARS
    characters, without control characters; repeats (in any case) are dropped."""
    raw = fields.get("labels")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise Refused(400, "invalid_option", "labels must be a list of names (strings), or one comma-separated string.")
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        x = " ".join(x.split())
        if not x:
            continue
        if len(x) > MAX_LABEL_CHARS or any(ord(c) < 32 or ord(c) == 127 for c in x):
            raise Refused(400, "invalid_option", f"Each label is at most {MAX_LABEL_CHARS} characters of plain text.")
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    if not out:
        raise Refused(400, "invalid_option", "labels is empty: name at least one thing to find.")
    if len(out) > MAX_LABELS:
        raise Refused(400, "invalid_option", f"At most {MAX_LABELS} labels per request.")
    return out


def grounding_prompt(labels: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """Grounding DINO's prompt for these labels, "a cat. a remote control." (lower case, a dot after each: its
    training format; a dot inside a label would split it, so it becomes a space), and each label's characters in it."""
    parts, spans, at = [], [], 0
    for label in labels:
        text = " ".join(label.lower().replace(".", " ").split()) or "object"
        spans.append((at, at + len(text)))
        parts.append(text)
        at += len(text) + 2               # ". "
    return ". ".join(parts) + ".", spans


def token_labels(offsets, spans: list[tuple[int, int]]) -> list[int]:
    """The label each prompt token belongs to (its characters fall within that label's), -1 for the others (the
    special tokens, the dots)."""
    out = []
    for a, b in offsets:
        a, b = int(a), int(b)
        out.append(next((i for i, (x, y) in enumerate(spans) if b > a and x <= a and b <= y), -1))
    return out


def best_label(probs, token_label: list[int], n: int):
    """Per box (probs: boxes x text positions, already sigmoid), the score of each label is its best token's; the
    box's score and label are the best of those."""
    per_label = torch.zeros((probs.shape[0], n))
    for t, i in enumerate(token_label):
        if 0 <= i < n and t < probs.shape[1]:
            per_label[:, i] = torch.maximum(per_label[:, i], probs[:, t])
    return per_label.max(dim=1)


def corners(boxes, width: int, height: int):
    """Relative (centre x, centre y, width, height) boxes as pixels [x0, y0, x1, y1], within the image."""
    cx, cy, w, h = boxes.unbind(-1)
    xyxy = torch.stack([(cx - w / 2) * width, (cy - h / 2) * height, (cx + w / 2) * width, (cy + h / 2) * height], dim=-1)
    return torch.stack([xyxy[:, 0].clamp(0, width), xyxy[:, 1].clamp(0, height), xyxy[:, 2].clamp(0, width),
                        xyxy[:, 3].clamp(0, height)], dim=-1)


class Detect(Task):
    name = "detect"

    def __init__(self, path: Path, device: str):
        super().__init__(path, device)
        config = json.loads((path / "config.json").read_text())
        self.zero_shot = zero_shot(config)
        if self.zero_shot:
            from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

            self.family = family(config)
            self.processor = AutoProcessor.from_pretrained(path)
            self.model = AutoModelForZeroShotObjectDetection.from_pretrained(path).to(device).eval()
            # Grounding DINO reads at most this many prompt tokens (its text logits' width).
            self.max_text = int(getattr(self.model.config, "max_text_len", 256) or 256)
        else:
            from transformers import AutoImageProcessor, AutoModelForObjectDetection

            self.processor = AutoImageProcessor.from_pretrained(path)
            self.model = AutoModelForObjectDetection.from_pretrained(path).to(device).eval()

    def run(self, img: Image.Image, fields: dict) -> dict:
        labels = label_list(fields)
        if self.zero_shot and labels is None:
            raise Refused(400, "labels_required", "This model finds what you name: send labels, e.g. "
                                                  "[\"cat\", \"remote control\"].")
        if not self.zero_shot and labels is not None:
            raise Refused(400, "labels_not_supported", "This detector knows a fixed set of classes and cannot be told "
                                                       "what to find: leave labels out (a zero-shot detector takes them).")
        default = {"grounding": 0.3, "owl": 0.1}.get(getattr(self, "family", ""), 0.5)
        threshold = number(fields, "threshold", default, 0.0, 1.0)
        most = int(number(fields, "max_detections", 100, 1, 1000))
        if self.zero_shot:
            # Within the image: OWL's boxes can reach a little past its edges.
            found = [(label, score, [min(max(float(v), 0.0), float(img.width if i % 2 == 0 else img.height))
                                     for i, v in enumerate(box)])
                     for label, score, box in getattr(self, f"_{self.family}")(img, labels, threshold)]
        else:
            inputs = self.processor(images=img, return_tensors="pt").to(self.device)
            with torch.inference_mode():
                out = self.model(**inputs)
            res = self.processor.post_process_object_detection(out, threshold=threshold,
                                                               target_sizes=[(img.height, img.width)])[0]
            names = self.model.config.id2label
            found = [(names[int(lbl)], float(s), box) for s, lbl, box in zip(res["scores"], res["labels"], res["boxes"])]
        found = [{"label": label, "score": round(score, 4), "box": [round(float(v), 1) for v in box]}
                 for label, score, box in found]
        found.sort(key=lambda d: -d["score"])
        return {"detections": found[:most]}

    def _grounding(self, img: Image.Image, labels: list[str], threshold: float) -> list:
        """Grounding DINO: one lower-case prompt, "a cat. a remote control.", whose tokens each box scores; a box is
        the label whose tokens score it highest (not transformers' phrase decoding, which can merge two labels)."""
        prompt, spans = grounding_prompt(labels)
        enc = self.processor.tokenizer(prompt, return_offsets_mapping=True)
        if len(enc["input_ids"]) > self.max_text:
            raise Refused(400, "invalid_option", f"The labels are too long for this model ({len(enc['input_ids'])} "
                                                 f"tokens, it reads {self.max_text}): send fewer or shorter ones.")
        inputs = self.processor(images=img, text=prompt, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            out = self.model(**inputs)
        scores, which = best_label(out.logits[0].sigmoid().float().cpu(), token_labels(enc["offset_mapping"], spans),
                                   len(labels))
        keep = scores > threshold
        boxes = corners(out.pred_boxes[0].float().cpu()[keep], img.width, img.height)
        return [(labels[int(i)], float(sc), b) for sc, i, b in zip(scores[keep], which[keep], boxes)]

    def _owl(self, img: Image.Image, labels: list[str], threshold: float) -> list:
        """OWL-ViT / OWLv2: one text query per label. OWLv2 pads the image to a square before it looks, so its boxes
        are scaled to that square."""
        inputs = self.processor(text=[labels], images=img, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            out = self.model(**inputs)
        side = max(img.width, img.height)
        size = (side, side) if self.model.config.model_type == "owlv2" else (img.height, img.width)
        res = self.processor.post_process_grounded_object_detection(out, threshold=threshold, target_sizes=[size])[0]
        return [(labels[int(i)], float(s), b) for s, i, b in zip(res["scores"], res["labels"], res["boxes"])]


class Segment(Task):
    name = "segment"

    def __init__(self, path: Path, device: str):
        super().__init__(path, device)
        from transformers import Sam2Model, Sam2Processor

        self.processor = Sam2Processor.from_pretrained(path)
        self.model = Sam2Model.from_pretrained(path).to(device).eval()

    def run(self, img: Image.Image, fields: dict) -> dict:
        points, labels, box = fields.get("points"), fields.get("labels"), fields.get("box")
        kw: dict = {}
        if box is not None:
            if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
                raise Refused(400, "invalid_option", "box must be [x0, y0, x1, y1].")
            kw["input_boxes"] = [[[float(v) for v in box]]]
        if points is not None:
            if not (isinstance(points, list) and 0 < len(points) <= 16
                    and all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)
                            for p in points)):
                raise Refused(400, "invalid_option", "points must be [[x, y], …] (at most 16).")
            labels = labels if labels is not None else [1] * len(points)
            if not (isinstance(labels, list) and len(labels) == len(points) and all(v in (0, 1) for v in labels)):
                raise Refused(400, "invalid_option", "labels must be 1 (inside) or 0 (outside), one per point.")
            kw["input_points"] = [[[[float(x), float(y)] for x, y in points]]]
            kw["input_labels"] = [[[int(v) for v in labels]]]
        if not kw:
            raise Refused(400, "missing_prompt", "Say what to segment: 'points' ([[x, y]]) or 'box' ([x0, y0, x1, y1]).")
        multi = str(fields.get("multimask", "true")).lower() not in ("false", "0", "no")
        inputs = self.processor(images=img, return_tensors="pt", **kw).to(self.device)
        with torch.inference_mode():
            out = self.model(**inputs, multimask_output=multi)
        masks = self.processor.post_process_masks(out.pred_masks.cpu(), inputs["original_sizes"])[0][0]
        scores = out.iou_scores.cpu()[0][0]
        result = []
        for m, s in sorted(zip(masks, scores), key=lambda t: -float(t[1])):
            m = m.numpy().astype(bool)
            ys, xs = m.nonzero()
            area = int(m.sum())
            bbox = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1] if area else None
            result.append({"score": round(float(s), 4), "area": area, "box": bbox,
                           "png": png_b64(Image.fromarray(m).convert("1"))})
        return {"masks": result}


class Depth(Task):
    name = "depth"

    def __init__(self, path: Path, device: str):
        super().__init__(path, device)
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation

        self.processor = AutoImageProcessor.from_pretrained(path)
        self.model = AutoModelForDepthEstimation.from_pretrained(path).to(device).eval()

    def run(self, img: Image.Image, fields: dict) -> dict:
        inputs = self.processor(images=img, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            out = self.model(**inputs)
        depth = torch.nn.functional.interpolate(out.predicted_depth.unsqueeze(1), size=(img.height, img.width),
                                                mode="bicubic", align_corners=False)[0, 0].float().cpu()
        lo, hi = float(depth.min()), float(depth.max())
        scaled = ((depth - lo) / (hi - lo or 1.0) * 65535).round().clamp(0, 65535).to(torch.int32).numpy().astype("uint16")
        return {"depth_png": png_b64(Image.fromarray(scaled)), "min": round(lo, 4), "max": round(hi, 4),
                "unit": "relative (larger is nearer)"}


def classifier_type(config: dict) -> str:
    return str(config.get("model_type") or "").strip().lower().replace("-", "_")


def zero_shot_classifier(config: dict) -> bool:
    """A CLIP-like model for zero-shot image classification: one of ZERO_SHOT_CLASSIFY's architectures, or its
    model_type when it names no architecture (a fine-tuned SigLIP classifier, …ForImageClassification, has fixed
    classes)."""
    archs = [a for a in config.get("architectures") or [] if isinstance(a, str)]
    names = {a.lower() for a in ZERO_SHOT_CLASSIFY.values()}
    return any(a.lower() in names for a in archs) or (not archs and classifier_type(config) in ZERO_SHOT_CLASSIFY)


def template_of(fields: dict) -> str:
    t = fields.get("template", DEFAULT_TEMPLATE)
    if not isinstance(t, str) or t.count("{}") != 1 or len(t) > 200 or any(ord(c) < 32 for c in t):
        raise Refused(400, "invalid_option", "template must be one line of at most 200 characters with one {} where "
                                             "the label goes, e.g. \"This is a photo of {}.\"")
    return t


def ranked(names: list[str], probs, top_k: int) -> list[dict]:
    order = sorted(range(len(names)), key=lambda i: -float(probs[i]))[:top_k]
    return [{"label": names[i], "score": round(float(probs[i]), 4)} for i in order]


class Classify(Task):
    """Image classification (1.4.4): a model with fixed classes (its id2label), or a CLIP-like one told the labels."""

    name = "image-classify"

    def __init__(self, path: Path, device: str):
        super().__init__(path, device)
        config = json.loads((path / "config.json").read_text())
        self.zero_shot = zero_shot_classifier(config)
        self.sigmoid = classifier_type(config) in SIGMOID_TYPES if self.zero_shot else \
            config.get("problem_type") == "multi_label_classification"
        if self.zero_shot:
            from transformers import AutoModelForZeroShotImageClassification, AutoProcessor

            self.processor = AutoProcessor.from_pretrained(path)
            self.model = AutoModelForZeroShotImageClassification.from_pretrained(path).to(device).eval()
        else:
            from transformers import AutoImageProcessor, AutoModelForImageClassification

            self.processor = AutoImageProcessor.from_pretrained(path)
            self.model = AutoModelForImageClassification.from_pretrained(path).to(device).eval()

    def run(self, img: Image.Image, fields: dict) -> dict:
        labels = label_list(fields)
        if self.zero_shot and labels is None:
            raise Refused(400, "labels_required", "This model classifies into the labels you send: send labels, e.g. "
                                                  "[\"cat\", \"dog\", \"invoice\"].")
        if not self.zero_shot and labels is not None:
            raise Refused(400, "labels_not_supported", "This classifier knows a fixed set of classes: leave labels out "
                                                       "(a zero-shot classifier such as CLIP or SigLIP takes them).")
        top_k = int(number(fields, "top_k", len(labels) if labels else 5, 1, 1000))
        if self.zero_shot:
            template = template_of(fields)
            texts = [template.replace("{}", label) for label in labels]
            # SigLIP was trained on texts padded to 64 tokens (as transformers' pipeline asks it).
            kw = {"padding": "max_length", "max_length": 64, "truncation": True} if self.sigmoid else {"padding": True}
            inputs = self.processor(text=texts, images=img, return_tensors="pt", **kw).to(self.device)
            with torch.inference_mode():
                logits = self.model(**inputs).logits_per_image[0].float().cpu()
            names = labels
        else:
            inputs = self.processor(images=img, return_tensors="pt").to(self.device)
            with torch.inference_mode():
                logits = self.model(**inputs).logits[0].float().cpu()
            id2label = self.model.config.id2label
            names = [str(id2label.get(i, i)) for i in range(logits.shape[0])]
        probs = logits.sigmoid() if self.sigmoid else logits.softmax(-1)
        return {"labels": ranked(names, probs, top_k), "multi_label": bool(self.sigmoid)}


TASKS = {"detect": Detect, "segment": Segment, "depth": Depth, "image-classify": Classify}


def task_of(config: dict) -> str | None:
    """The task a transformers config.json is for (the same rule as Forgeyard's inspector,
    forgeyard/models/vision.py): an ...ForObjectDetection or ...ForDepthEstimation architecture, SAM 2, or (1.4.4)
    an ...ForImageClassification architecture or a CLIP-like zero-shot classifier."""
    archs = [a for a in config.get("architectures") or [] if isinstance(a, str)]
    if config.get("model_type") in SEGMENT_TYPES or any(a.startswith("Sam2") for a in archs):
        return "segment"
    if any(a.endswith("ForObjectDetection") for a in archs):
        return "detect"
    if any(a.endswith("ForDepthEstimation") for a in archs):
        return "depth"
    if any(a.endswith("ForImageClassification") for a in archs) or zero_shot_classifier(config):
        return "image-classify"
    return None


def check_model_dir(task: str, model_dir: Path) -> None:
    """Refuse to start (SystemExit, with why) on a directory that is not a model for `task`."""
    try:
        config = json.loads((model_dir / "config.json").read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"{model_dir}: no readable config.json ({e})") from None
    found = task_of(config if isinstance(config, dict) else {})
    if found != task:
        what = f"a {found} model" if found else "not a detection, SAM 2, depth or image classification model"
        raise SystemExit(f"{model_dir} is {what} (architectures {config.get('architectures')}), not one for {task}.")
    if config.get("use_timm_backbone"):
        raise SystemExit(f"{model_dir} uses a timm backbone (use_timm_backbone), which this image does not include.")


# ------------------------------------------------------------------ the app
def create_app(task: str, models_dir: Path, device: str, *, model_dir: Path | None = None,
               served_name: str | None = None, api_key: str | None = None, worker: Task | None = None) -> FastAPI:
    """The app for one task: the baked model of models.json, or (`model_dir`) a model of the model cache,
    answered as `served_name`. `worker`: an already loaded model (tests)."""
    if model_dir is None:
        info = MODELS[task]
        model_dir = models_dir / info["dir"]
        named = {"model": info["repo"], "revision": info["revision"]}
        about = {"task": task, **named, "licence": info["licence"]}
    else:
        named = {"model": served_name or model_dir.name}
        about = {"task": task, **named, "source": "model cache"}
    t0 = time.monotonic()
    worker = worker or TASKS[task](model_dir, device)
    loaded_s = round(time.monotonic() - t0, 1)
    app = FastAPI(title=f"Forgeyard vision: {task}", docs_url=None, redoc_url=None, openapi_url=None)
    path = PATHS.get(task, task)
    about.update(device=device, endpoints=[f"/v1/vision/{path}", f"/v1/{path}"], loaded_s=loaded_s)
    want = f"Bearer {api_key}".encode() if api_key else None

    @app.middleware("http")
    async def _key(request: Request, call_next):
        # Every path but /health needs the replica's key when one is set (1.4): its port then answers only
        # the gateway, even without the agent's key guard in front.
        if want is not None and request.url.path != "/health":
            got = (request.headers.get("authorization") or "").encode()
            if not hmac.compare_digest(got, want):
                return JSONResponse({"error": {"message": "A valid API key is needed.", "code": "invalid_api_key"}},
                                    status_code=401)
        return await call_next(request)

    @app.exception_handler(Refused)
    async def _refused(_request, e: Refused):
        return JSONResponse({"error": {"message": e.message, "code": e.code}}, status_code=e.status)

    @app.get("/health")
    def health():
        return {"ok": True, "task": task}

    @app.get("/")
    def root():
        return about

    @app.post(f"/v1/vision/{path}")
    @app.post(f"/v1/{path}")
    async def run(request: Request):
        img, fields = await read_request(request)
        if img.width * img.height > MAX_PIXELS:
            raise Refused(400, "image_too_large", "The image has more than 40 megapixels.")
        start = time.monotonic()

        def call():
            with worker.lock:
                return worker.run(img, fields)

        from starlette.concurrency import run_in_threadpool

        out = await run_in_threadpool(call)
        return {**named, "width": img.width, "height": img.height, **out,
                "ms": round((time.monotonic() - start) * 1000, 1)}

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Forgeyard's vision server")
    ap.add_argument("--task", choices=sorted(TASKS), required=True)
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--models", default=os.environ.get("VISION_MODELS", "/models"), help="where models.json's models are")
    ap.add_argument("--model", default=None, help="a model directory (the model cache) instead of the baked model")
    ap.add_argument("--served-name", default=None, help="the model's name in answers (with --model)")
    ap.add_argument("--device", default=os.environ.get("VISION_DEVICE", ""), help="cpu or cuda (default: cuda when present)")
    args = ap.parse_args()
    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    threads = os.environ.get("VISION_THREADS")
    if threads and threads.isdigit() and int(threads) > 0:
        torch.set_num_threads(int(threads))
    model_dir = Path(args.model) if args.model else None
    if model_dir is not None:
        check_model_dir(args.task, model_dir)
    elif args.task not in MODELS:
        raise SystemExit(f"--task {args.task} has no model baked into the image: give one with --model.")
    app = create_app(args.task, Path(args.models), device, model_dir=model_dir, served_name=args.served_name,
                     api_key=os.environ.get("VISION_API_KEY") or None)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
