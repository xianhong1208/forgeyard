"""Forgeyard's vision server itself (images/vision/app.py, 1.4): the replica key on every path but /health,
the task's two paths only, the served name in answers, and a model directory checked against its task before
anything is loaded (FY-VIS-016 to 018). It needs torch, transformers and Pillow (the image's own environment):
skipped where they are not installed (Forgeyard's own test environment); run it with the vision server's
dependencies (images/vision/requirements-cpu.txt plus pytest and httpx)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")
pytest.importorskip("PIL")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "images" / "vision" / "app.py"


def load():
    spec = importlib.util.spec_from_file_location("vision_app", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeWorker:
    """A loaded model, without one: answers what it was given."""

    def __init__(self, mod):
        import threading

        self.lock = threading.Lock()
        self.calls = []

    def run(self, img, fields):
        self.calls.append((img.size, fields))
        return {"detections": [{"label": "cat", "score": 0.9, "box": [0, 0, 1, 1]}]}


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(out, format="PNG")
    return out.getvalue()


def client(mod, **kw):
    worker = FakeWorker(mod)
    app = mod.create_app("detect", Path("/nonexistent"), "cpu", worker=worker, **kw)
    return TestClient(app), worker


def test_fy_vis_016_the_key_guards_every_path_but_health():
    """FY-VIS-016: with a key, /health answers without it, every other path (the task's, /, an unknown one)
    is 401 without it or with a wrong one, and 200 with it; without a key (the 1.3 templates) nothing is asked."""
    mod = load()
    c, worker = client(mod, model_dir=Path("/m/snap"), served_name="det", api_key="s3cret")
    assert c.get("/health").status_code == 200
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "s3cret"}, {"Authorization": "Bearer s3cret "}):
        for method, path in (("post", "/v1/vision/detect"), ("get", "/"), ("post", "/v1/detect"), ("get", "/nope")):
            r = getattr(c, method)(path, headers=headers)
            assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key", (path, headers)
    assert worker.calls == []
    ok = {"Authorization": "Bearer s3cret"}
    r = c.post("/v1/vision/detect", headers=ok, files={"image": ("a.png", png(), "image/png")}, data={"threshold": "0.3"})
    assert r.status_code == 200 and r.json()["model"] == "det" and "revision" not in r.json()
    assert r.json()["width"] == 64 and worker.calls[-1] == ((64, 48), {"threshold": 0.3})
    assert c.get("/", headers=ok).json()["source"] == "model cache"
    open_c, _ = client(mod)                  # the baked model: no key, answers name its repository and revision
    r = open_c.post("/v1/detect", files={"image": ("a.png", png(), "image/png")})
    assert r.status_code == 200 and r.json()["model"] == "PekingU/rtdetr_v2_r18vd" and len(r.json()["revision"]) == 40


def test_fy_vis_017_only_the_tasks_own_paths():
    """FY-VIS-017: a detection server answers /v1/vision/detect and /v1/detect (JSON with base64 too) and
    nothing for the other tasks; a bad image is a 400 in the server's error shape."""
    mod = load()
    c, _ = client(mod, model_dir=Path("/m"), served_name="det")
    import base64

    assert c.post("/v1/vision/detect", json={"image": base64.b64encode(png()).decode()}).status_code == 200
    assert c.post("/v1/vision/segment", json={"image": "eA=="}).status_code == 404
    assert c.post("/v1/depth", json={"image": "eA=="}).status_code == 404
    r = c.post("/v1/vision/detect", json={"image": "eA=="})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_image"


def test_fy_vis_018_a_model_directory_is_checked_against_its_task(tmp_path):
    """FY-VIS-018: the server refuses to start on a directory without config.json, or with a model of
    another task, before loading anything; the right task passes."""
    mod = load()
    with pytest.raises(SystemExit, match="config.json"):
        mod.check_model_dir("detect", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"architectures": ["DepthAnythingForDepthEstimation"]}))
    with pytest.raises(SystemExit, match="a depth model"):
        mod.check_model_dir("detect", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"architectures": ["LlamaForCausalLM"]}))
    with pytest.raises(SystemExit, match="not a detection"):
        mod.check_model_dir("depth", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"architectures": ["Sam2VideoModel"], "model_type": "sam2_video"}))
    mod.check_model_dir("segment", tmp_path)
