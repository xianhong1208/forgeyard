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
- **Embeddings and text classification (1.5.4, #141 and #144, the built-in `transformers` recipe):** `--task embed`
  with a CLIP-like model (CLIP, SigLIP, SigLIP 2, …: texts and images in one space) or DINOv2 (images only), and
  `--task classify` with any `AutoModelForSequenceClassification` model: fixed labels (DistilBERT, DeBERTa, BERT,
  RoBERTa, XLM-R…), or zero-shot with an NLI model (bge-m3-zeroshot, roberta-zeroshot…), whose labels come with each
  request. No code from a model's repository is run (auto_map is refused).
- **A model baked into the image (1.3, the workload templates exposed at /x/<name>):** without `--model`,
  the task's model of models.json, fetched at a pinned revision when the image was built, so it starts
  without the network.

    python app.py --task detect|segment|depth|image-classify|embed|classify [--model DIR --served-name NAME]
                  [--host 0.0.0.0] [--port 8000]
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

The transformers recipe's tasks (1.5.4) take JSON only, in the OpenAI / vLLM shapes the gateway serves:

    POST /v1/embeddings        embed: {"input": "a text" | ["a text", {"text": "…"}, {"image": "<base64 or data: URI>"}, …],
                               "encoding_format": "float" | "base64", "dimensions": <the model's own, or left out>}
                               -> {"object": "list", "data": [{"object": "embedding", "index", "embedding"}, …],
                               "usage": {"prompt_tokens", "total_tokens", "images", "kilopixels", "truncated_inputs"}}.
                               Every vector is L2-normalised, in the order of `input`. Texts are cut to the model's length
                               (CLIP 77 tokens, SigLIP 64) and counted in truncated_inputs; an image-only model (DINOv2)
                               refuses texts (text_not_supported); at most VISION_MAX_IMAGES images (32); a URL is
                               refused (image_url_not_supported: the gateway fetches URLs, this server never does)
    POST /classify             classify (vLLM's shape): {"input": "a text" | [texts], "truncate_prompt_tokens": k | -1,
    POST /v1/classify          "use_activation": true} -> {"id", "object": "list", "created", "data": [{"index", "label",
                               "probs", "num_classes"}], "usage": {"prompt_tokens", "total_tokens", "truncated_inputs"}};
                               probs in the model's label order. Zero-shot (an NLI model) also takes "labels": [names]
                               (required, at most 32, no repeats), "multi_label" (false) and "hypothesis_template"
                               ("This text is about {}."); its probs follow the request's labels, which the answer echoes
                               ("labels", "multi_label"); texts x labels is at most VISION_MAX_PAIRS (256), too_many_pairs
                               otherwise. A fixed-label model given labels is 400 labels_not_supported, a zero-shot one
                               without them labels_required.

GET / also says what the model takes: for embed its modalities, dimensions and text length; for classify whether it is
zero-shot, its labels and its length. Only the started task's paths answer (404 for the others). Every answer carries "model" (the served
name, or the baked model's repository), "ms", and for a baked model "revision". Errors:
{"error": {"message", "code"}} with 400 (a bad image, input or option; labels_required: a zero-shot detector was not
told what to find; labels_not_supported: a detector with fixed classes was given labels), 401 (a missing or wrong
key), 413 (an image larger than MAX_IMAGE_MB, or a text task's request too large for its images) or 500.

**The key (1.4):** with VISION_API_KEY set (Forgeyard sets a random one per replica), every path but
/health needs `Authorization: Bearer <that key>`, compared in constant time; without it, nothing is asked
(the 1.3 templates, behind the agent's key guard). VISION_THREADS caps torch's CPU threads; on a CPU the
transformers recipe's models run in float32 (16-bit checkpoints are widened: half-precision is far slower there).
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


def max_body() -> int:
    """The largest JSON body a text task reads: its images (base64 is 4/3 of the bytes) and some text."""
    return env_int("VISION_MAX_IMAGES", 32, 1, 1024) * MAX_IMAGE_MB * 1024**2 * 4 // 3 + 64 * 1024**2


async def read_json(request: Request) -> dict:
    """A text task's JSON object (1.5.4), refused above max_body() before it is read."""
    size = request.headers.get("content-length", "")
    if size.isdigit() and int(size) > max_body():
        raise Refused(413, "request_too_large", "The request is too large: send fewer or smaller images.")
    raw = await request.body()
    if len(raw) > max_body():
        raise Refused(413, "request_too_large", "The request is too large: send fewer or smaller images.")
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        raise Refused(400, "invalid_json", "Send a JSON object.") from None
    if not isinstance(body, dict):
        raise Refused(400, "invalid_json", "Send a JSON object.")
    return body


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


# ------------------------------------------------------------------ the transformers recipe's tasks (1.5.4, #141, #144)
# Image (and text) embeddings and text classification, the built-in `transformers` recipe: same image, other tasks.
# Each request is JSON (no multipart), and these limits are the recipe's parameters (VISION_MAX_IMAGES,
# VISION_MAX_PAIRS) or OpenAI's (MAX_INPUTS).
MAX_INPUTS = 2048                  # items (texts and images) per request, OpenAI's limit for embeddings
MAX_TEXT_CHARS = 100_000           # one text; it is cut to the model's length in tokens anyway
TEXT_BATCH = 32                    # texts per forward pass
IMAGE_BATCH = 8                    # images per forward pass
PAIR_BATCH = 16                    # (text, hypothesis) pairs per forward pass, NLI zero-shot
DEFAULT_HYPOTHESIS = "This text is about {}."
# Image-only embedders: DINOv2 (and its registers variant), model_type -> architecture. Their vector is the
# backbone's pooled output (the class token after the final layer norm).
IMAGE_ONLY_EMBED = {"dinov2": "Dinov2Model", "dinov2_with_registers": "Dinov2WithRegistersModel"}
NLI_WORDS = ("entail", "contradict")


def env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw) if raw.strip() else default
    except ValueError:
        value = default
    return min(max(value, low), high)


def load_dtype(device: str):
    """The weights' type: float32 on a CPU, where half-precision matrix products are several times slower (an
    NLI pass of bge-m3-zeroshot took 1.4 s in float16 against 0.2 s in float32, four threads); the checkpoint's own
    on a GPU."""
    return torch.float32 if device == "cpu" else "auto"


def remote_code(config: dict) -> bool:
    """config.json asks for code from the repository (auto_map): never run here (trust_remote_code stays off)."""
    return bool(config.get("auto_map"))


def embed_family(config: dict) -> str | None:
    """"text+image" for a CLIP-like model (ZERO_SHOT_CLASSIFY: CLIP, SigLIP, SigLIP 2, …: a text and an image in
    one space), "image" for an image-only backbone (DINOv2), else None."""
    if zero_shot_classifier(config):
        return "text+image"
    archs = {a for a in config.get("architectures") or [] if isinstance(a, str)}
    if archs & set(IMAGE_ONLY_EMBED.values()) or (not archs and classifier_type(config) in IMAGE_ONLY_EMBED):
        return "image"
    return None


def sequence_labels(config: dict) -> list[str]:
    """A sequence classifier's label names in index order (id2label), or LABEL_i for num_labels without names."""
    names = config.get("id2label")
    if isinstance(names, dict) and names:
        def index(k) -> int:
            try:
                return int(k)
            except (TypeError, ValueError):
                return 1 << 30
        return [str(v) for _, v in sorted(names.items(), key=lambda kv: index(kv[0]))]
    n = config.get("num_labels")
    return [f"LABEL_{i}" for i in range(n)] if isinstance(n, int) and not isinstance(n, bool) and n > 0 else []


def nli_ids(names: list[str]) -> tuple[int, int] | None:
    """(entailment, contradiction) columns of an NLI head (entailment / neutral / contradiction, entailment /
    not_entailment), or None for a classifier with fixed labels. The contradiction column is the one named so (or
    not_entailment); with neither, transformers' zero-shot pipeline rule (the first column, or the last when
    entailment is the first)."""
    low = [n.lower() for n in names]
    if not any(w in n for n in low for w in NLI_WORDS):
        return None
    entail = next((i for i, n in enumerate(low) if n.startswith("entail")), None)
    if entail is None:
        return None
    contra = next((i for i, n in enumerate(low) if n.startswith("contradict") or n.startswith("not_entail")
                   or n.startswith("not entail")), None)
    if contra is None:
        contra = len(names) - 1 if entail == 0 else 0
    return entail, contra


def text_classifier_problem(config: dict) -> str | None:
    """Why a config.json is not a text classifier this server runs (None when it is): an architecture ending in
    ForSequenceClassification with two labels or more, not a regression head."""
    archs = [a for a in config.get("architectures") or [] if isinstance(a, str)]
    if not any(a.endswith("ForSequenceClassification") for a in archs):
        return "not a text classification model (no …ForSequenceClassification architecture)"
    if len(sequence_labels(config)) < 2:
        return "a model with one output (num_labels 1) scores pairs: it is a reranker, not a classifier"
    if config.get("problem_type") == "regression":
        return "a regression head predicts numbers, not labels"
    return None


def strict_labels(fields: dict) -> list[str] | None:
    """A zero-shot text classifier's `labels`: a list of names in the caller's order, which the answer's
    probabilities follow, so a repeat (in any case) is refused rather than dropped; at most MAX_LABELS of at most
    MAX_LABEL_CHARS characters of plain text. None when none were sent."""
    raw = fields.get("labels")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise Refused(400, "invalid_option", "labels must be a list of names (strings).")
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        x = " ".join(x.split())
        if not x or len(x) > MAX_LABEL_CHARS or any(ord(c) < 32 or ord(c) == 127 for c in x):
            raise Refused(400, "invalid_option", f"Each label is 1 to {MAX_LABEL_CHARS} characters of plain text.")
        if x.lower() in seen:
            raise Refused(400, "invalid_option", f"labels has {x!r} twice: the probabilities follow the labels' order, "
                                                 "so each label comes once.")
        seen.add(x.lower())
        out.append(x)
    if not out:
        raise Refused(400, "invalid_option", "labels is empty: send the labels to choose from.")
    if len(out) > MAX_LABELS:
        raise Refused(400, "invalid_option", f"At most {MAX_LABELS} labels per request.")
    return out


def texts_of(fields: dict) -> list[str]:
    """`input`: a text or a non-empty list of texts (token ids are not taken)."""
    inp = fields.get("input")
    texts = [inp] if isinstance(inp, str) else inp
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
        raise Refused(400, "invalid_input", "'input' is a text or a non-empty list of texts (token ids are not taken).")
    if len(texts) > MAX_INPUTS:
        raise Refused(400, "invalid_input", f"'input' has {len(texts)} texts; at most {MAX_INPUTS} per request.")
    for i, t in enumerate(texts):
        if not t.strip():
            raise Refused(400, "invalid_input", f"input[{i}] is empty.")
        if len(t) > MAX_TEXT_CHARS:
            raise Refused(400, "invalid_input", f"input[{i}] has more than {MAX_TEXT_CHARS} characters.")
    return texts


def image_of(data: str, where: str) -> Image.Image:
    """An image item's data: base64 or a data: URI. A URL is the gateway's to fetch (through its egress guard): this
    server never fetches anything."""
    if data.startswith(("http://", "https://")):
        raise Refused(400, "image_url_not_supported", f"{where}: send the image's bytes (base64 or a data: URI); "
                                                      "this engine does not fetch URLs.")
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise Refused(400, "invalid_image", f"{where} is not base64 or a data: URI.") from None
    img = decode_image(raw)
    if img.width * img.height > MAX_PIXELS:
        raise Refused(400, "image_too_large", f"{where} has more than 40 megapixels.")
    return img


def chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def features(out):
    """get_text_features / get_image_features answer a tensor (older transformers) or an output whose pooled value
    is the projected vector (5.x)."""
    return out if isinstance(out, torch.Tensor) else out.pooler_output


def max_length_of(tokenizer, config, cap: int = 1 << 20) -> int:
    """The longest input the model reads, in tokens: the tokenizer's model_max_length when it is set, else the loaded
    model's position embeddings (its text model's for a CLIP-like one; RoBERTa-like models start their positions
    after the padding index)."""
    n = getattr(tokenizer, "model_max_length", None)
    if isinstance(n, int) and 0 < n < cap:
        return n
    text = getattr(config, "text_config", None) or config
    pos = int(getattr(text, "max_position_embeddings", 0) or 512)
    pad = getattr(text, "pad_token_id", None)
    if getattr(text, "model_type", "") in ("roberta", "xlm-roberta", "camembert") and isinstance(pad, int):
        pos -= pad + 1
    return max(pos, 1)


class Embed(Task):
    """Embeddings (1.5.4, #141): a CLIP-like model (CLIP, SigLIP, SigLIP 2, …) puts texts and images in one space
    (text -> image search); DINOv2 embeds images only (image -> image similarity, duplicates). Every vector is
    L2-normalised (cosine similarity = dot product), the order of `input` kept."""

    name = "embed"

    def __init__(self, path: Path, device: str):
        super().__init__(path, device)
        config = json.loads((path / "config.json").read_text())
        self.family = embed_family(config)
        dtype = load_dtype(device)
        if self.family == "text+image":
            from transformers import AutoModel, AutoProcessor

            self.processor = AutoProcessor.from_pretrained(path)
            self.tokenizer = self.processor.tokenizer
            self.image_processor = self.processor.image_processor
            self.model = AutoModel.from_pretrained(path, dtype=dtype).to(device).eval()
            # SigLIP was trained on texts padded to 64 tokens (transformers' pipeline asks it so): its texts are padded
            # to its length; CLIP's to the longest of the batch.
            self.pad_to_max = classifier_type(config) in SIGMOID_TYPES
            self.text_max = max_length_of(self.tokenizer, self.model.config, cap=4096)
        else:
            from transformers import AutoImageProcessor, AutoModel

            self.tokenizer = None
            self.image_processor = AutoImageProcessor.from_pretrained(path)
            self.model = AutoModel.from_pretrained(path, dtype=dtype).to(device).eval()
            self.pad_to_max, self.text_max = False, 0
        # One small image tells the vector's size (and warms the model up).
        self.dim = int(self._images([Image.new("RGB", (32, 32), (128, 128, 128))]).shape[-1])

    def describe(self) -> dict:
        return {"modalities": ["text", "image"] if self.tokenizer is not None else ["image"], "dimensions": self.dim,
                "text_max_tokens": self.text_max or None}

    def _images(self, imgs: list[Image.Image]):
        inputs = self.image_processor(images=imgs, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            if self.family == "text+image":
                vec = features(self.model.get_image_features(**inputs))
            else:
                vec = self.model(**inputs).pooler_output
        return torch.nn.functional.normalize(vec.float(), dim=-1).cpu()

    def _texts(self, texts: list[str]) -> tuple[torch.Tensor, int, int]:
        """(the vectors, the tokens read, how many texts were cut to the model's length)."""
        full = self.tokenizer(texts, truncation=False)["input_ids"]
        cut = sum(len(ids) > self.text_max for ids in full)
        tokens = sum(min(len(ids), self.text_max) for ids in full)
        kw = {"padding": "max_length"} if self.pad_to_max else {"padding": True}
        inputs = self.tokenizer(texts, truncation=True, max_length=self.text_max, return_tensors="pt", **kw).to(self.device)
        with torch.inference_mode():
            vec = features(self.model.get_text_features(**inputs))
        return torch.nn.functional.normalize(vec.float(), dim=-1).cpu(), tokens, cut

    def items(self, fields: dict) -> list[tuple[str, object]]:
        """`input` as ("text", str) and ("image", Image) items: a text, or a list of texts, {"text": …} and
        {"image": "<base64 or data: URI>"} (the Jina shape) in any mix."""
        inp = fields.get("input")
        raw = [inp] if isinstance(inp, (str, dict)) else inp
        if not isinstance(raw, list) or not raw:
            raise Refused(400, "invalid_input", "'input' is a text, an item ({\"text\": …} or {\"image\": …}) or a "
                                                "non-empty list of them (token ids are not taken).")
        if len(raw) > MAX_INPUTS:
            raise Refused(400, "invalid_input", f"'input' has {len(raw)} items; at most {MAX_INPUTS} per request.")
        most = env_int("VISION_MAX_IMAGES", 32, 1, 1024)
        out: list[tuple[str, object]] = []
        for i, item in enumerate(raw):
            where = f"input[{i}]" if isinstance(inp, list) else "input"
            if isinstance(item, dict) and set(item) == {"image"} and isinstance(item["image"], str):
                out.append(("image", item["image"]))
                continue
            text = item if isinstance(item, str) else None
            if isinstance(item, dict) and set(item) == {"text"}:
                text = item["text"]
            if not isinstance(text, str):
                raise Refused(400, "invalid_input", f"{where}: a text, {{\"text\": \"…\"}} or {{\"image\": \"<base64 or "
                                                    "data: URI>\"}.")
            if not text.strip() or len(text) > MAX_TEXT_CHARS:
                raise Refused(400, "invalid_input", f"{where} is empty or longer than {MAX_TEXT_CHARS} characters.")
            if self.tokenizer is None:
                raise Refused(400, "text_not_supported", f"{where} is a text, but this model embeds images only "
                                                         "(DINOv2): send images, or use a CLIP-like model for texts.")
            out.append(("text", text))
        n = sum(kind == "image" for kind, _ in out)
        if n > most:
            raise Refused(400, "too_many_images", f"{n} images in one request; this deployment takes at most {most}.")
        return [(kind, image_of(v, f"input[{i}]") if kind == "image" else v) for i, (kind, v) in enumerate(out)]

    def run(self, fields: dict) -> dict:
        fmt = fields.get("encoding_format", "float")
        if fmt not in ("float", "base64"):
            raise Refused(400, "invalid_option", "encoding_format is float or base64.")
        dims = fields.get("dimensions")
        if dims is not None and dims != self.dim:
            raise Refused(400, "invalid_option", f"dimensions: this model's vectors have {self.dim} (it cannot shorten "
                                                 "them); leave dimensions out or send that number.")
        items = self.items(fields)
        vectors: list = [None] * len(items)
        texts = [(i, v) for i, (kind, v) in enumerate(items) if kind == "text"]
        imgs = [(i, v) for i, (kind, v) in enumerate(items) if kind == "image"]
        tokens = cut = 0
        for batch in chunks(texts, TEXT_BATCH):
            vec, n, c = self._texts([t for _, t in batch])
            tokens, cut = tokens + n, cut + c
            for (i, _), v in zip(batch, vec):
                vectors[i] = v
        for batch in chunks(imgs, IMAGE_BATCH):
            for (i, _), v in zip(batch, self._images([im for _, im in batch])):
                vectors[i] = v

        def encode(v):
            if fmt == "base64":
                return base64.b64encode(v.numpy().astype("<f4").tobytes()).decode("ascii")
            return v.tolist()

        kpx = sum(round(im.width * im.height / 1000) for _, im in imgs)
        return {"object": "list",
                "data": [{"object": "embedding", "index": i, "embedding": encode(v)} for i, v in enumerate(vectors)],
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens, "images": len(imgs), "kilopixels": kpx,
                          "truncated_inputs": cut}}


class TextClassify(Task):
    """Text classification (1.5.4, #144) with any sequence classifier (DistilBERT, DeBERTa, BERT, RoBERTa,
    XLM-RoBERTa…): fixed labels (its id2label), or zero-shot with an NLI model (labels entailment / neutral /
    contradiction or entailment / not_entailment), which scores each label sent as a hypothesis ("This text is
    about {}.") against each text: texts x labels forward passes. Answered in vLLM's /classify shape."""

    name = "classify"

    def __init__(self, path: Path, device: str):
        super().__init__(path, device)
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        config = json.loads((path / "config.json").read_text())
        self.names = sequence_labels(config)
        self.nli = nli_ids(self.names)
        self.multi = config.get("problem_type") == "multi_label_classification"
        self.tokenizer = AutoTokenizer.from_pretrained(path)
        self.model = AutoModelForSequenceClassification.from_pretrained(path, dtype=load_dtype(device)).to(device).eval()
        self.max_len = max_length_of(self.tokenizer, self.model.config)

    def describe(self) -> dict:
        out = {"zero_shot": self.nli is not None, "max_tokens": self.max_len}
        if self.nli is None:
            out.update(labels=self.names, multi_label=self.multi)
        return out

    def _logits(self, first: list[str], second: list[str] | None, max_length: int, left: bool):
        """One batch: its logits, the tokens read, and which inputs were longer than max_length (and cut: the
        text's end, or with `left` its start; a pair cuts its text, never the hypothesis)."""
        self.tokenizer.truncation_side = "left" if left else "right"
        try:
            kw = {"text_pair": second, "truncation": "only_first"} if second is not None else {"truncation": True}
            full = self.tokenizer(first, text_pair=second)["input_ids"]
            inputs = self.tokenizer(first, max_length=max_length, padding=True, return_tensors="pt", **kw)
        finally:
            self.tokenizer.truncation_side = "right"
        tokens = int(inputs["attention_mask"].sum())
        with torch.inference_mode():
            logits = self.model(**inputs.to(self.device)).logits.float().cpu()
        return logits, tokens, [len(ids) > max_length for ids in full]

    def run(self, fields: dict) -> dict:
        texts = texts_of(fields)
        labels = strict_labels(fields)
        zero_shot = self.nli is not None
        if zero_shot and labels is None:
            raise Refused(400, "labels_required", "This is a zero-shot model: send the labels to choose from, e.g. "
                                                  "\"labels\": [\"billing\", \"technical problem\", \"praise\"].")
        if not zero_shot:
            if labels is not None:
                raise Refused(400, "labels_not_supported", "This classifier has fixed labels: leave labels out (a "
                                                           "zero-shot NLI model takes them).")
            for k in ("multi_label", "hypothesis_template"):
                if k in fields:
                    raise Refused(400, "invalid_option", f"{k} is for zero-shot models only.")
        use_act = fields.get("use_activation", True)
        if not isinstance(use_act, bool) or (zero_shot and use_act is not True):
            raise Refused(400, "invalid_option", "use_activation is true or false (a zero-shot model always answers "
                                                 "probabilities).")
        k = fields.get("truncate_prompt_tokens")
        if k is not None:
            if not isinstance(k, int) or isinstance(k, bool) or k == 0 or k < -1 or k > self.max_len:
                raise Refused(400, "invalid_option", f"truncate_prompt_tokens is 1 to {self.max_len} (this model's "
                                                     "length), or -1 for that length.")
        max_length = self.max_len if k in (None, -1) else k
        if zero_shot:
            return self._zero_shot(texts, labels, fields, max_length)
        # vLLM's rule: truncate_prompt_tokens keeps the last k tokens; without it, a text longer than the model is cut
        # at its end (and counted).
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        rows: list = [None] * len(texts)
        tokens = cut = 0
        for batch in chunks(order, TEXT_BATCH):
            logits, n, over = self._logits([texts[i] for i in batch], None, max_length, left=k not in (None, -1))
            tokens, cut = tokens + n, cut + sum(over)
            for i, row in zip(batch, logits):
                rows[i] = row
        data = []
        for i, row in enumerate(rows):
            probs = (row.sigmoid() if self.multi else row.softmax(-1)) if use_act else row
            best = int(row.argmax())
            data.append({"index": i, "label": self.names[best] if best < len(self.names) else str(best),
                         "probs": [float(p) for p in probs], "num_classes": len(row)})
        return {"data": data, "usage": {"prompt_tokens": tokens, "total_tokens": tokens, "truncated_inputs": cut}}

    def _zero_shot(self, texts: list[str], labels: list[str], fields: dict, max_length: int) -> dict:
        multi = fields.get("multi_label", False)
        if not isinstance(multi, bool):
            raise Refused(400, "invalid_option", "multi_label is true or false.")
        template = fields.get("hypothesis_template", DEFAULT_HYPOTHESIS)
        if not isinstance(template, str) or template.count("{}") != 1 or len(template) > 200 or \
                any(ord(c) < 32 or ord(c) == 127 for c in template):
            raise Refused(400, "invalid_option", "hypothesis_template is one line of at most 200 characters with one {} "
                                                 "where the label goes, e.g. \"This text is about {}.\"")
        most = env_int("VISION_MAX_PAIRS", 256, 1, 100_000)
        pairs = [(t, template.replace("{}", label)) for t in texts for label in labels]
        if len(pairs) > most:
            raise Refused(400, "too_many_pairs", f"{len(texts)} texts x {len(labels)} labels = {len(pairs)} pairs, each "
                                                 f"one pass of the model; this deployment takes at most {most} per "
                                                 "request: send fewer texts or labels.")
        entail, contra = self.nli
        out = []
        tokens = 0
        cut_pairs = set()
        for start in range(0, len(pairs), PAIR_BATCH):
            batch = pairs[start:start + PAIR_BATCH]
            logits, n, over = self._logits([p for p, _ in batch], [h for _, h in batch], max_length, left=False)
            tokens += n
            out.append(logits)
            cut_pairs.update((start + j) // len(labels) for j, o in enumerate(over) if o)
        logits = torch.cat(out).reshape(len(texts), len(labels), -1)
        # transformers' ZeroShotClassificationPipeline: one label alone, or multi_label, scores each label on its own
        # (entailment against contradiction); otherwise the entailment logits are a softmax across the labels.
        if multi or len(labels) == 1:
            probs = logits[..., [contra, entail]].softmax(-1)[..., 1]
        else:
            probs = logits[..., entail].softmax(-1)
        data = [{"index": i, "label": labels[int(row.argmax())], "probs": [float(p) for p in row],
                 "num_classes": len(labels)} for i, row in enumerate(probs)]
        return {"data": data, "labels": labels, "multi_label": bool(multi or len(labels) == 1),
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens, "truncated_inputs": len(cut_pairs)}}


TEXT_TASKS = {"embed": Embed, "classify": TextClassify}
# A text task's paths: the gateway calls /v1/embeddings and /classify (vLLM's path, without /v1).
TEXT_PATHS = {"embed": ("/v1/embeddings",), "classify": ("/classify", "/v1/classify")}
TASKS = {"detect": Detect, "segment": Segment, "depth": Depth, "image-classify": Classify, **TEXT_TASKS}


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
    config = config if isinstance(config, dict) else {}
    if task in TEXT_TASKS:
        # 1.5.4: the transformers recipe's tasks. No code from the model's repository is ever run.
        if remote_code(config):
            raise SystemExit(f"{model_dir} needs code from its repository (auto_map in config.json), which this server "
                             "never runs.")
        if task == "embed" and embed_family(config) is None:
            raise SystemExit(f"{model_dir} (architectures {config.get('architectures')}) is not an image embedding model "
                             "this server runs: a CLIP-like model (CLIP, SigLIP, SigLIP 2, …) or DINOv2. Text embedding "
                             "models run on vLLM (the vllm-pooling recipe).")
        if task == "classify" and (why := text_classifier_problem(config)):
            raise SystemExit(f"{model_dir} (architectures {config.get('architectures')}) is {why}.")
        return
    found = task_of(config)
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
    endpoints = list(TEXT_PATHS[task]) if task in TEXT_PATHS else [f"/v1/vision/{path}", f"/v1/{path}"]
    about.update(device=device, endpoints=endpoints, loaded_s=loaded_s)
    if hasattr(worker, "describe"):
        about.update(worker.describe())
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

    from starlette.concurrency import run_in_threadpool

    if task in TEXT_TASKS:
        async def run_text(request: Request):
            fields = await read_json(request)
            start = time.monotonic()

            def call():
                with worker.lock:
                    return worker.run(fields)

            out = await run_in_threadpool(call)
            extra = {"id": f"classify-{os.urandom(12).hex()}", "created": int(time.time())} if task == "classify" else {}
            return {**extra, "object": "list", **named, **out, "ms": round((time.monotonic() - start) * 1000, 1)}

        for p in TEXT_PATHS[task]:
            app.add_api_route(p, run_text, methods=["POST"])
        return app

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
