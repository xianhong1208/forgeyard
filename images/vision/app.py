"""Forgeyard's vision server: one small HTTP API per task, for the ready-made workload templates exposed
at /x/<name> (1.3). Each model is baked into the image at a pinned revision (models.json), so it starts
without the network.

    python app.py --task detect|segment|depth [--host 0.0.0.0] [--port 8000] [--models /models]

Endpoints (JSON; an image is sent as a multipart file `image`, or as JSON {"image": "<base64 or data: URI>"}):

    GET  /health        200 once the model is loaded
    GET  /              the task, the model, its licence and revision, the device, the endpoints
    POST /v1/detect     detect: {"detections": [{"label", "score", "box": [x0, y0, x1, y1]}], "width", "height"}
                        options: threshold (0.5), max_detections (100)
    POST /v1/segment    segment: a point ("points": [[x, y]], "labels": [1]) or a box ("box": [x0, y0, x1, y1]);
                        {"masks": [{"score", "box", "area", "png"}]}: each mask a 1-bit PNG, base64
    POST /v1/depth      depth: {"depth_png": <16-bit PNG, base64, near = bright>, "min", "max", "width", "height"}
                        relative depth (Depth Anything V2 Small), not metres

Every answer carries "model", "revision" and "ms". Errors: {"error": {"message", "code"}} with 400 (a bad
image or option), 413 (more than MAX_IMAGE_MB) or 500.
"""

from __future__ import annotations

import argparse
import base64
import binascii
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


class Detect(Task):
    name = "detect"

    def __init__(self, path: Path, device: str):
        super().__init__(path, device)
        from transformers import AutoImageProcessor, AutoModelForObjectDetection

        self.processor = AutoImageProcessor.from_pretrained(path)
        self.model = AutoModelForObjectDetection.from_pretrained(path).to(device).eval()

    def run(self, img: Image.Image, fields: dict) -> dict:
        threshold = number(fields, "threshold", 0.5, 0.0, 1.0)
        most = int(number(fields, "max_detections", 100, 1, 1000))
        inputs = self.processor(images=img, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            out = self.model(**inputs)
        res = self.processor.post_process_object_detection(out, threshold=threshold,
                                                           target_sizes=[(img.height, img.width)])[0]
        labels = self.model.config.id2label
        found = [{"label": labels[int(lbl)], "score": round(float(s), 4), "box": [round(float(v), 1) for v in box]}
                 for s, lbl, box in zip(res["scores"], res["labels"], res["boxes"])]
        found.sort(key=lambda d: -d["score"])
        return {"detections": found[:most]}


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


TASKS = {"detect": Detect, "segment": Segment, "depth": Depth}


# ------------------------------------------------------------------ the app
def create_app(task: str, models_dir: Path, device: str) -> FastAPI:
    info = MODELS[task]
    model_dir = models_dir / info["dir"]
    t0 = time.monotonic()
    worker = TASKS[task](model_dir, device)
    loaded_s = round(time.monotonic() - t0, 1)
    app = FastAPI(title=f"Forgeyard vision: {task}", docs_url=None, redoc_url=None, openapi_url=None)
    about = {"task": task, "model": info["repo"], "revision": info["revision"], "licence": info["licence"],
             "device": device, "endpoint": f"/v1/{task}", "loaded_s": loaded_s}

    @app.exception_handler(Refused)
    async def _refused(_request, e: Refused):
        return JSONResponse({"error": {"message": e.message, "code": e.code}}, status_code=e.status)

    @app.get("/health")
    def health():
        return {"ok": True, "task": task}

    @app.get("/")
    def root():
        return about

    @app.post(f"/v1/{task}")
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
        return {"model": info["repo"], "revision": info["revision"], "width": img.width, "height": img.height,
                **out, "ms": round((time.monotonic() - start) * 1000, 1)}

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Forgeyard's vision server")
    ap.add_argument("--task", choices=sorted(TASKS), required=True)
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--models", default=os.environ.get("VISION_MODELS", "/models"), help="where models.json's models are")
    ap.add_argument("--device", default=os.environ.get("VISION_DEVICE", ""), help="cpu or cuda (default: cuda when present)")
    args = ap.parse_args()
    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    threads = os.environ.get("VISION_THREADS")
    if threads:
        torch.set_num_threads(int(threads))
    app = create_app(args.task, Path(args.models), device)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
