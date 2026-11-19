"""Forgeyard's vision server itself (images/vision/app.py, 1.4): the replica key on every path but /health, the task's
two paths only, the served name in answers, and a model directory checked against its task before anything is loaded
(FY-VIS-016 to 018); zero-shot detectors (1.4.4, #142): labels checked, required by them and refused by the others,
each family asked its own way, a timm backbone refused (FY-VIS-023 to 027). It needs torch, transformers and Pillow (the image's own
environment): skipped where they are not installed (Forgeyard's own test environment); run it with the vision
server's dependencies (images/vision/requirements-cpu.txt plus pytest and httpx)."""

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
import torch  # noqa: E402

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


# ------------------------------------------------------------------ zero-shot detectors (1.4.4, #142)
def test_fy_vis_023_labels_are_checked():
    """FY-VIS-023: `labels` is a list of names or one comma-separated string (a form field); spaces are folded,
    empty names and repeats (in any case) dropped; not strings, nothing left, more than 32, one longer than 64
    characters or with a control character (after white space is folded) is a 400 invalid_option; no labels
    is None."""
    mod = load()
    assert mod.label_list({}) is None
    assert mod.label_list({"labels": ["cat", " remote   control ", "Cat", "", "dog"]}) == ["cat", "remote control", "dog"]
    assert mod.label_list({"labels": "cat, dog ,,red car"}) == ["cat", "dog", "red car"]
    assert mod.label_list({"labels": ["貓", "x" * 64]}) == ["貓", "x" * 64]
    assert mod.label_list({"labels": [f"l{i}" for i in range(32)]})[-1] == "l31"
    assert mod.label_list({"labels": ["a\nb", "tab\there"]}) == ["a b", "tab here"]     # white space is folded
    for bad in ([], " , ", [1], ["cat", None], {"a": 1}, 7, [f"l{i}" for i in range(33)], ["x" * 65], ["nul\x00"],
                ["del\x7f"]):
        with pytest.raises(mod.Refused) as e:
            mod.label_list({"labels": bad})
        assert e.value.status == 400 and e.value.code == "invalid_option", bad


class _Stub:
    """Stands in for a processor or a model: records the calls it gets."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.calls = []


def _detector(mod, zero_shot: bool, fam: str = "grounding"):
    d = mod.Detect.__new__(mod.Detect)
    d.zero_shot, d.device, d.path = zero_shot, "cpu", Path("/m")
    if zero_shot:
        d.family = fam
    return d


def test_fy_vis_024_a_zero_shot_detector_needs_labels_and_another_refuses_them():
    """FY-VIS-024: a zero-shot detector without labels is 400 labels_required, and a detector with fixed classes
    given labels is 400 labels_not_supported, before the model is asked; over HTTP the code reaches the caller
    in the server's error shape. Each family starts from its own threshold (Grounding DINO 0.3, OWL 0.1,
    a fixed detector 0.5) and the request's threshold wins; boxes are kept within the image."""
    mod = load()
    img = Image.new("RGB", (64, 48))
    with pytest.raises(mod.Refused) as e:
        _detector(mod, True).run(img, {"threshold": 0.4})
    assert (e.value.status, e.value.code) == (400, "labels_required") and "labels" in e.value.message
    with pytest.raises(mod.Refused) as e:
        _detector(mod, False).run(img, {"labels": ["cat"]})
    assert (e.value.status, e.value.code) == (400, "labels_not_supported")
    seen = {}
    for fam, want in (("grounding", 0.3), ("owl", 0.1)):
        d = _detector(mod, True, fam)
        setattr(d, f"_{fam}", lambda img, labels, threshold, fam=fam: seen.setdefault(fam, []).append((labels, threshold)) or [])
        assert d.run(img, {"labels": "cat"}) == {"detections": []}
        d.run(img, {"labels": ["cat"], "threshold": "0.7"})
        assert seen[fam] == [(["cat"], want), (["cat"], 0.7)], fam
    d = _detector(mod, True, "owl")
    d._owl = lambda img, labels, threshold: [("cat", 0.61234, torch.tensor([-3.0, -1.0, 70.0, 50.0]))]
    assert d.run(img, {"labels": ["cat"]}) == {"detections": [{"label": "cat", "score": 0.6123, "box": [0.0, 0.0, 64.0, 48.0]}]}

    class Raising:
        lock = __import__("threading").Lock()

        def run(self, img, fields):
            return _detector(mod, True).run(img, fields)

    c = TestClient(mod.create_app("detect", Path("/x"), "cpu", model_dir=Path("/m"), served_name="gd", worker=Raising()))
    r = c.post("/v1/vision/detect", files={"image": ("a.png", png(), "image/png")})
    assert r.status_code == 400 and r.json() == {"error": {"code": "labels_required", "message": r.json()["error"]["message"]}}


def test_fy_vis_025_grounding_dino_boxes_take_the_label_whose_words_score_them():
    """FY-VIS-025: Grounding DINO's prompt is the labels in lower case, each closed by a dot (a dot inside a label
    becomes a space), with each label's characters known; a prompt token belongs to the label its characters fall
    in (special tokens and dots to none); a box's label is the one whose best token scores it highest, its score
    that token's; relative boxes become pixels within the image."""
    mod = load()
    prompt, spans = mod.grounding_prompt(["Cat", "remote control.", "e.g. red car"])
    assert prompt == "cat. remote control. e g red car."
    assert [prompt[a:b] for a, b in spans] == ["cat", "remote control", "e g red car"]
    # [CLS] cat . remote control . [SEP]
    offsets = [(0, 0), (0, 3), (3, 4), (5, 11), (12, 19), (19, 20), (0, 0)]
    p2, s2 = mod.grounding_prompt(["cat", "remote control"])
    assert mod.token_labels(offsets, s2) == [-1, 0, -1, 1, 1, -1, -1]
    probs = torch.tensor([[0.9, 0.8, 0.9, 0.1, 0.3, 0.9, 0.9],     # the cat (the dot and special tokens do not count)
                          [0.0, 0.1, 0.0, 0.6, 0.7, 0.0, 0.0],     # the remote: its better word
                          [0.0, 0.05, 0.0, 0.02, 0.0, 0.0, 0.0]])  # nothing much
    scores, which = mod.best_label(probs, mod.token_labels(offsets, s2), 2)
    assert which.tolist()[:2] == [0, 1] and [round(float(x), 2) for x in scores] == [0.8, 0.7, 0.05]
    boxes = mod.corners(torch.tensor([[0.5, 0.5, 0.5, 0.5], [0.05, 0.5, 0.3, 1.2]]), 200, 100)
    assert boxes.tolist() == [[50.0, 25.0, 150.0, 75.0], [0.0, 0.0, 40.0, 100.0]]


def test_fy_vis_026_owl_boxes_are_scaled_to_the_square_owlv2_looked_at():
    """FY-VIS-026: OWL-ViT and OWLv2 get one text query per label and their label indices back as names; OWLv2
    pads the image to a square before it looks, so its boxes are scaled to that square (max side x max side),
    OWL-ViT's to the image. Grounding DINO and its re-trainings are the other family; a checkpoint with a timm backbone
    (OmDet-Turbo) is refused before anything is loaded."""
    mod = load()
    img = Image.new("RGB", (640, 480))

    class Proc(_Stub):
        def __call__(self, **kw):
            self.calls.append(kw)
            return _Stub(to=lambda device: {})

        def post_process_grounded_object_detection(self, out, **kw):
            self.calls.append(kw)
            return [{"scores": torch.tensor([0.4, 0.2]), "labels": torch.tensor([1, 0]),
                     "text_labels": ["dog", "cat"], "boxes": torch.tensor([[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]])}]

    for kind, size in (("owlv2", (640, 640)), ("owlvit", (480, 640))):
        d = _detector(mod, True, "owl")
        d.processor = Proc()
        d.model = _Stub(config=_Stub(model_type=kind))
        d.model.__class__.__call__ = lambda self, **kw: "out"
        found = d._owl(img, ["cat", "dog"], 0.1)
        assert [(lbl, round(s, 2)) for lbl, s, _ in found] == [("dog", 0.4), ("cat", 0.2)], kind
        assert d.processor.calls[0]["text"] == [["cat", "dog"]]
        assert d.processor.calls[1] == {"threshold": 0.1, "target_sizes": [size]}, kind
    assert [mod.family(c) for c in ({"model_type": "owlv2"}, {"architectures": ["OwlViTForObjectDetection"]},
                                    {"model_type": "grounding-dino"}, {"model_type": "mm-grounding-dino"})] == [
        "owl", "owl", "grounding", "grounding"]


def test_fy_vis_027_a_timm_backbone_is_refused_before_loading(tmp_path):
    """FY-VIS-027: a detection model directory whose config.json uses a timm backbone (OmDet-Turbo, DETR with timm)
    stops the server at start with why, before anything is loaded; Grounding DINO's passes."""
    mod = load()
    (tmp_path / "config.json").write_text(json.dumps({"architectures": ["OmDetTurboForObjectDetection"], "model_type": "omdet-turbo",
                                                      "use_timm_backbone": True}))
    with pytest.raises(SystemExit, match="timm backbone"):
        mod.check_model_dir("detect", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"architectures": ["GroundingDinoForObjectDetection"],
                                                      "model_type": "grounding-dino", "use_timm_backbone": False}))
    mod.check_model_dir("detect", tmp_path)
