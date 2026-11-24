"""Forgeyard's transformers server (1.5.4, #141 and #144): the vision server (images/vision/app.py) started with --task
embed or --task classify, on tiny random models of each kind it runs (images/vision/tinymodels.py: real transformers
classes and files, made offline): a model directory checked before anything loads, the replica key, the task's own
paths, embeddings of texts and images in order and normalised, text classification with fixed labels and zero-shot
with an NLI model (transformers' own zero-shot pipeline is the reference), and every refusal (FY-TFM-010 to 018).

It needs torch, transformers and Pillow (the image's own environment): skipped where they are not installed
(Forgeyard's own test environment); run it with the vision server's dependencies (images/vision/requirements-cpu.txt
plus pytest and httpx), as CI's vision-image job does."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import struct
import threading
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")
pytest.importorskip("PIL")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
import torch  # noqa: E402

HERE = Path(__file__).resolve().parent.parent / "images" / "vision"
KEY = {"Authorization": "Bearer k3y"}


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"vision_{name}", HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def app():
    return load("app")


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    """The tiny models, made once: {name: directory}."""
    tm = load("tinymodels")
    root = tmp_path_factory.mktemp("tiny")
    return {name: tm.make(name, root / name) for name in tm.MODELS}


def b64png(size=(64, 48), colour=(200, 30, 30), data_uri=False) -> str:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="PNG")
    raw = base64.b64encode(out.getvalue()).decode()
    return f"data:image/png;base64,{raw}" if data_uri else raw


def serve(app, task: str, model_dir: Path, name: str = "m"):
    app.check_model_dir(task, model_dir)
    worker = app.TASKS[task](model_dir, "cpu")
    return TestClient(app.create_app(task, Path("/nonexistent"), "cpu", model_dir=model_dir, served_name=name,
                                     api_key="k3y", worker=worker)), worker


def err(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


def test_fy_tfm_010_a_model_directory_is_checked_before_loading(app, tiny, tmp_path):
    """FY-TFM-010: embed takes a CLIP-like model (by architecture, or by model_type alone) or DINOv2, and refuses a text
    embedder (pointing at vllm-pooling) or an image classifier; classify takes a sequence classifier with two labels or
    more and refuses a reranker (one output), a regression head and any other model; both refuse a config asking for
    the repository's own code (auto_map) before anything is loaded. The vision tasks are checked as before."""
    for name in ("clip", "siglip", "dinov2"):
        app.check_model_dir("embed", tiny[name])
    for name in ("bert-fixed", "bert-multi-label", "bert-nli-2", "bert-nli-3"):
        app.check_model_dir("classify", tiny[name])

    def refused(task, config, match):
        (tmp_path / "config.json").write_text(json.dumps(config))
        with pytest.raises(SystemExit, match=match):
            app.check_model_dir(task, tmp_path)

    refused("embed", {"architectures": ["XLMRobertaModel"], "model_type": "xlm-roberta"}, "vllm-pooling")
    refused("embed", {"architectures": ["Dinov2ForImageClassification"], "model_type": "dinov2"}, "not an image embedding")
    refused("embed", {"architectures": ["CLIPModel"], "auto_map": {"AutoModel": "x.Y"}}, "auto_map")
    refused("classify", {"architectures": ["BertForSequenceClassification"], "num_labels": 1}, "reranker")
    refused("classify", {"architectures": ["BertForSequenceClassification"], "id2label": {"0": "a", "1": "b"},
                         "problem_type": "regression"}, "regression")
    refused("classify", {"architectures": ["BertModel"]}, "not a text classification model")
    refused("classify", {"architectures": ["BertForSequenceClassification"], "id2label": {"0": "a", "1": "b"},
                         "auto_map": {"AutoModel": "x.Y"}}, "auto_map")
    with pytest.raises(SystemExit, match="config.json"):
        app.check_model_dir("classify", tmp_path / "missing")
    app.check_model_dir("image-classify", tiny["clip"])                 # 1.4.4's zero-shot image classifier, unchanged
    with pytest.raises(SystemExit, match="not a detection"):
        app.check_model_dir("detect", tiny["bert-fixed"])
    assert app.embed_family({"model_type": "siglip"}) == "text+image"
    assert app.embed_family({"model_type": "dinov2_with_registers"}) == "image"


def test_fy_tfm_011_embeddings_of_texts_and_images_in_order(app, tiny):
    """FY-TFM-011: /v1/embeddings (the task's only path) answers OpenAI's shape for a mix of plain strings, {"text"} and
    {"image"} items (base64 or a data: URI) in the order sent: each vector the model's own for that item, L2-normalised,
    base64 = little-endian float32 of the same; usage counts the text tokens, the images and their kilopixels; GET /
    says the modalities, dimensions and text length; the replica key guards it; dimensions equal to the model's pass."""
    c, w = serve(app, "embed", tiny["clip"], "clip")
    assert c.post("/v1/embeddings", json={"input": "a cat"}).status_code == 401
    about = c.get("/", headers=KEY).json()
    assert about["endpoints"] == ["/v1/embeddings"] and about["task"] == "embed"
    assert (about["modalities"], about["dimensions"], about["text_max_tokens"]) == (["text", "image"], 12, 16)
    for path in ("/v1/classify", "/classify", "/v1/vision/classify", "/v1/vision/embed"):
        assert c.post(path, headers=KEY, json={"input": "x"}).status_code == 404, path
    red, blue = b64png(), b64png((40, 40), (20, 40, 200), data_uri=True)
    r = c.post("/v1/embeddings", headers=KEY, json={"model": "x", "input": ["a cat", {"image": red}, {"text": "a dog"},
                                                                           {"image": blue}]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["object"] == "list" and out["model"] == "clip" and [d["index"] for d in out["data"]] == [0, 1, 2, 3]
    assert all(d["object"] == "embedding" and len(d["embedding"]) == 12 for d in out["data"])
    vec = [torch.tensor(d["embedding"]) for d in out["data"]]
    assert all(abs(float(v.norm()) - 1) < 1e-5 for v in vec)
    texts, _, _ = w._texts(["a cat", "a dog"])
    images = w._images([Image.new("RGB", (64, 48), (200, 30, 30)), Image.new("RGB", (40, 40), (20, 40, 200))])
    for got, want in zip(vec, [texts[0], images[0], texts[1], images[1]]):
        assert torch.allclose(got, want, atol=1e-5)
    assert out["usage"] == {"prompt_tokens": 8, "total_tokens": 8, "images": 2, "kilopixels": 3 + 2, "truncated_inputs": 0}
    r = c.post("/v1/embeddings", headers=KEY, json={"input": ["a cat"], "encoding_format": "base64", "dimensions": 12})
    raw = base64.b64decode(r.json()["data"][0]["embedding"])
    assert torch.allclose(torch.tensor(struct.unpack("<12f", raw)), texts[0], atol=1e-6)
    one = c.post("/v1/embeddings", headers=KEY, json={"input": {"image": red}}).json()
    assert torch.allclose(torch.tensor(one["data"][0]["embedding"]), images[0], atol=1e-5)


def test_fy_tfm_012_what_embeddings_refuse(app, tiny, monkeypatch):
    """FY-TFM-012: an image-only model (DINOv2) refuses texts (text_not_supported) but embeds images; a URL is refused
    (the gateway fetches URLs, never the engine); more images than VISION_MAX_IMAGES, other dimensions, another
    encoding_format, token ids, an empty text, an item with other keys, a broken image and a body larger than the
    images allowed are each refused with their code, before the model runs."""
    c, _ = serve(app, "embed", tiny["dinov2"], "dino")
    assert c.get("/", headers=KEY).json()["modalities"] == ["image"]
    assert err(c.post("/v1/embeddings", headers=KEY, json={"input": [{"image": b64png()}, "a cat"]})) == \
        (400, "text_not_supported")
    ok = c.post("/v1/embeddings", headers=KEY, json={"input": [{"image": b64png()}]})
    assert ok.status_code == 200 and len(ok.json()["data"][0]["embedding"]) == 16
    assert ok.json()["usage"]["prompt_tokens"] == 0
    c, _ = serve(app, "embed", tiny["clip"], "clip")
    monkeypatch.setenv("VISION_MAX_IMAGES", "2")
    cases = [({"input": [{"image": "https://example.com/cat.jpg"}]}, "image_url_not_supported"),
             ({"input": [{"image": b64png()}] * 3}, "too_many_images"),
             ({"input": "a cat", "dimensions": 8}, "invalid_option"),
             ({"input": "a cat", "encoding_format": "float16"}, "invalid_option"),
             ({"input": [[1, 2, 3]]}, "invalid_input"), ({"input": [1, 2]}, "invalid_input"),
             ({"input": ["a cat", " "]}, "invalid_input"), ({"input": []}, "invalid_input"), ({}, "invalid_input"),
             ({"input": [{"text": "a", "image": b64png()}]}, "invalid_input"),
             ({"input": [{"image": "not base64!"}]}, "invalid_image"),
             ({"input": [{"image": base64.b64encode(b"not an image").decode()}]}, "invalid_image"),
             ({"input": ["x"] * 2049}, "invalid_input")]
    for body, code in cases:
        assert err(c.post("/v1/embeddings", headers=KEY, json=body)) == (400, code), body
    assert c.post("/v1/embeddings", headers=KEY, json={"input": [{"image": b64png()}] * 2}).status_code == 200
    r = c.post("/v1/embeddings", headers={**KEY, "Content-Type": "application/json"}, content=b"[1]")
    assert err(r) == (400, "invalid_json")
    monkeypatch.setattr(app, "max_body", lambda: 100)
    assert err(c.post("/v1/embeddings", headers=KEY, json={"input": "x" * 200})) == (413, "request_too_large")
    # A declared Content-Length above the cap is refused before the body is read at all.
    from starlette.requests import Request

    async def never(self):
        raise AssertionError("the body was read")

    with monkeypatch.context() as m:
        m.setattr(Request, "body", never)
        assert err(c.post("/v1/embeddings", headers=KEY, json={"input": "x" * 200})) == (413, "request_too_large")
    # Sent in chunks, without a Content-Length: counted as it is read.
    chunked = iter([b'{"input": "', b"x" * 200, b'"}'])
    assert err(c.post("/v1/embeddings", headers={**KEY, "Content-Type": "application/json"}, content=chunked)) == \
        (413, "request_too_large")


def test_fy_tfm_013_texts_are_cut_to_the_models_length_and_counted(app, tiny):
    """FY-TFM-013: a text longer than the model reads (the tiny models: 16 tokens) is cut, counted in
    truncated_inputs, and its tokens counted up to that length; SigLIP's texts are padded to its length (as it was
    trained), CLIP's to the longest of the batch, without changing a text's vector."""
    for name in ("clip", "siglip"):
        c, w = serve(app, "embed", tiny[name])
        long = " ".join(["cat"] * 40)
        out = c.post("/v1/embeddings", headers=KEY, json={"input": [long, "a cat"]}).json()
        assert out["usage"]["truncated_inputs"] == 1 and out["usage"]["prompt_tokens"] == 16 + 4, name
        alone = c.post("/v1/embeddings", headers=KEY, json={"input": "a cat"}).json()["data"][0]["embedding"]
        assert torch.allclose(torch.tensor(out["data"][1]["embedding"]), torch.tensor(alone), atol=1e-5), name
        assert w.pad_to_max is (name == "siglip")


def _pipeline(model_dir: Path):
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline

    return pipeline("zero-shot-classification", device="cpu", tokenizer=AutoTokenizer.from_pretrained(model_dir),
                    model=AutoModelForSequenceClassification.from_pretrained(model_dir, dtype=torch.float32))


def test_fy_tfm_014_fixed_labels_in_vllms_shape(app, tiny):
    """FY-TFM-014: a classifier with fixed labels answers /classify and /v1/classify in vLLM's shape (id, object,
    created, model, data[index, label, probs, num_classes], usage): probs in its id2label order, a softmax (a sigmoid
    each for a multi-label head), the label its best one, the model's own logits with use_activation false; texts
    keep their order whatever their length; truncate_prompt_tokens keeps the last k tokens (vLLM's rule) and counts the
    text as cut; labels, multi_label and hypothesis_template are refused."""
    c, w = serve(app, "classify", tiny["bert-fixed"], "sent")
    about = c.get("/", headers=KEY).json()
    assert about["endpoints"] == ["/classify", "/v1/classify"] and about["zero_shot"] is False
    assert about["labels"] == ["negative", "neutral", "positive"] and about["max_tokens"] == 64
    texts = ["i love it it is good good good", "bad", "the dog"]
    for path in ("/classify", "/v1/classify"):
        out = c.post(path, headers=KEY, json={"model": "x", "input": texts}).json()
        assert out["object"] == "list" and out["model"] == "sent" and out["id"].startswith("classify-")
        assert isinstance(out["created"], int) and [d["index"] for d in out["data"]] == [0, 1, 2]
    enc = w.tokenizer(texts, padding=True, return_tensors="pt")
    with torch.inference_mode():
        logits = w.model(**enc).logits
    for d, row in zip(out["data"], logits):
        assert d["num_classes"] == 3 and abs(sum(d["probs"]) - 1) < 1e-5
        assert torch.allclose(torch.tensor(d["probs"]), row.softmax(-1), atol=1e-5)
        assert d["label"] == about["labels"][int(row.argmax())]
    assert out["usage"] == {"prompt_tokens": int(enc["attention_mask"].sum()), "total_tokens": int(enc["attention_mask"].sum()),
                            "truncated_inputs": 0}
    raw = c.post("/classify", headers=KEY, json={"input": texts[1], "use_activation": False}).json()["data"][0]
    assert torch.allclose(torch.tensor(raw["probs"]), logits[1], atol=1e-5)
    sent = []
    hook = w.model.register_forward_pre_hook(lambda _m, _a, kw: sent.append(kw["input_ids"].tolist()), with_kwargs=True)
    cut = c.post("/classify", headers=KEY, json={"input": "good bad dog cat", "truncate_prompt_tokens": 3}).json()
    hook.remove()
    assert sent == [[[w.tokenizer.convert_tokens_to_ids(t) for t in ("[CLS]", "cat", "[SEP]")]]]   # the last tokens kept
    w.tokenizer.truncation_side = "left"
    want = w.tokenizer(["good bad dog cat"], truncation=True, max_length=3, return_tensors="pt")
    w.tokenizer.truncation_side = "right"
    with torch.inference_mode():
        assert torch.allclose(torch.tensor(cut["data"][0]["probs"]), w.model(**want).logits[0].softmax(-1), atol=1e-5)
    assert cut["usage"]["prompt_tokens"] == 3 and cut["usage"]["truncated_inputs"] == 1
    long = c.post("/classify", headers=KEY, json={"input": " ".join(["good"] * 100)}).json()
    assert long["usage"] == {"prompt_tokens": 64, "total_tokens": 64, "truncated_inputs": 1}
    for body, code in (({"input": "x", "labels": ["a"]}, "labels_not_supported"),
                       ({"input": "x", "multi_label": True}, "invalid_option"),
                       ({"input": "x", "hypothesis_template": "{}"}, "invalid_option"),
                       ({"input": "x", "truncate_prompt_tokens": 0}, "invalid_option"),
                       ({"input": "x", "truncate_prompt_tokens": 65}, "invalid_option"),
                       ({"input": "x", "truncate_prompt_tokens": True}, "invalid_option"),
                       ({"input": "x", "use_activation": "no"}, "invalid_option"),
                       ({"input": [1, 2]}, "invalid_input"), ({"input": ""}, "invalid_input"), ({}, "invalid_input")):
        assert err(c.post("/classify", headers=KEY, json=body)) == (400, code), body
    assert c.post("/classify", headers=KEY, json={"input": "x", "truncate_prompt_tokens": -1}).status_code == 200
    c, w = serve(app, "classify", tiny["bert-multi-label"])
    assert c.get("/", headers=KEY).json()["multi_label"] is True
    d = c.post("/classify", headers=KEY, json={"input": "bad dog"}).json()["data"][0]
    with torch.inference_mode():
        row = w.model(**w.tokenizer(["bad dog"], return_tensors="pt")).logits[0]
    assert torch.allclose(torch.tensor(d["probs"]), row.sigmoid(), atol=1e-5)


def test_fy_tfm_015_zero_shot_is_transformers_own_pipeline(app, tiny):
    """FY-TFM-015: an NLI model (entailment / not_entailment, or entailment / neutral / contradiction) scores the
    request's labels exactly as transformers' ZeroShotClassificationPipeline does with the same template: a softmax of
    the entailment logits across the labels, or (multi_label, or one label alone) entailment against contradiction for
    each; probs follow the request's order, which the answer echoes with multi_label; usage counts every pair's
    tokens. The default template is "This text is about {}."."""
    texts = ["i love it", "the dog is bad", "politics is about cooking"]
    labels = ["sports", "politics", "cooking"]
    for name in ("bert-nli-2", "bert-nli-3"):
        c, w = serve(app, "classify", tiny[name], "zs")
        assert c.get("/", headers=KEY).json()["zero_shot"] is True
        pipe = _pipeline(tiny[name])
        for multi, labs in ((False, labels), (True, labels), (False, ["sports"])):
            out = c.post("/classify", headers=KEY, json={"input": texts, "labels": labs, "multi_label": multi}).json()
            assert out["labels"] == labs and out["multi_label"] is (multi or len(labs) == 1)
            for d, text in zip(out["data"], texts):
                ref = pipe(text, labs, multi_label=multi, hypothesis_template="This text is about {}.")
                want = dict(zip(ref["labels"], ref["scores"]))
                assert max(abs(want[lab] - p) for lab, p in zip(labs, d["probs"])) < 1e-5, (name, multi, labs)
                # The best label (a tiny random model's scores can tie to within rounding: any of the best then).
                assert want[d["label"]] > max(want.values()) - 1e-5 and d["num_classes"] == len(labs)
            if not multi and len(labs) > 1:
                assert all(abs(sum(d["probs"]) - 1) < 1e-5 for d in out["data"])
        tmpl = c.post("/classify", headers=KEY, json={"input": texts[0], "labels": labels,
                                                    "hypothesis_template": "this is about {} ."}).json()
        ref = pipe(texts[0], labels, hypothesis_template="this is about {} .")
        assert dict(zip(ref["labels"], ref["scores"]))[tmpl["data"][0]["label"]] > ref["scores"][0] - 1e-5
        enc = w.tokenizer([texts[0]] * 3, [f"This text is about {lab}." for lab in labels], padding=True)
        one = c.post("/classify", headers=KEY, json={"input": texts[0], "labels": labels}).json()
        assert one["usage"]["prompt_tokens"] == sum(sum(m) for m in enc["attention_mask"])


def test_fy_tfm_016_what_zero_shot_refuses(app, tiny, monkeypatch):
    """FY-TFM-016: a zero-shot model without labels is 400 labels_required; labels must be distinct names (a repeat in
    any case is refused, since the probabilities follow their order), at most 32 of at most 64 characters; the
    template has one {}; multi_label and use_activation are booleans (use_activation false is refused: it always answers
    probabilities); texts x labels above VISION_MAX_PAIRS is 400 too_many_pairs, before the model runs."""
    c, _ = serve(app, "classify", tiny["bert-nli-3"])
    monkeypatch.setenv("VISION_MAX_PAIRS", "6")
    assert c.post("/classify", headers=KEY, json={"input": ["a", "b"], "labels": ["x", "y", "z"]}).status_code == 200
    for body, code in (({"input": "x"}, "labels_required"),
                       ({"input": ["a", "b", "c"], "labels": ["x", "y", "z"]}, "too_many_pairs"),
                       ({"input": "x", "labels": ["cat", "Cat"]}, "invalid_option"),
                       ({"input": "x", "labels": "cat, dog"}, "invalid_option"),
                       ({"input": "x", "labels": []}, "invalid_option"),
                       ({"input": "x", "labels": ["a" * 65]}, "invalid_option"),
                       ({"input": "x", "labels": [f"l{i}" for i in range(33)]}, "invalid_option"),
                       ({"input": "x", "labels": ["a\x00"]}, "invalid_option"),
                       ({"input": "x", "labels": ["a"], "hypothesis_template": "no slot"}, "invalid_option"),
                       ({"input": "x", "labels": ["a"], "hypothesis_template": "{} {}"}, "invalid_option"),
                       ({"input": "x", "labels": ["a"], "multi_label": "yes"}, "invalid_option"),
                       ({"input": "x", "labels": ["a"], "use_activation": False}, "invalid_option")):
        r = c.post("/classify", headers=KEY, json=body)
        assert err(r) == (400, code), (body, r.text)
    assert app.strict_labels({"labels": [" two   words ", "b"]}) == ["two words", "b"]


def test_fy_tfm_017_nli_heads_and_float32_on_a_cpu(app, tiny, tmp_path):
    """FY-TFM-017: the entailment and contradiction columns come from the label names (not_entailment counts as the
    contradiction, any case), else transformers' pipeline rule; a classifier with other names is not zero-shot. On a
    CPU the weights are held in float32 whatever the checkpoint stores (half precision is far slower there); on a GPU
    in the checkpoint's own type."""
    assert app.nli_ids(["entailment", "not_entailment"]) == (0, 1)
    assert app.nli_ids(["entailment", "neutral", "contradiction"]) == (0, 2)
    assert app.nli_ids(["CONTRADICTION", "NEUTRAL", "ENTAILMENT"]) == (2, 0)
    assert app.nli_ids(["entailment", "neutral", "other"]) == (0, 2)
    assert app.nli_ids(["neutral", "entailment"]) == (1, 0)
    assert app.nli_ids(["positive", "negative"]) is None and app.nli_ids([]) is None
    assert app.load_dtype("cpu") is torch.float32 and app.load_dtype("cuda") == "auto"
    from transformers import AutoModelForSequenceClassification

    half = tmp_path / "half"
    AutoModelForSequenceClassification.from_pretrained(tiny["bert-nli-2"]).half().save_pretrained(half)
    for f in tiny["bert-nli-2"].glob("tokenizer*"):
        (half / f.name).write_bytes(f.read_bytes())
    assert json.loads((half / "config.json").read_text()).get("dtype", json.loads((half / "config.json").read_text()).get(
        "torch_dtype")) == "float16"
    w = app.TextClassify(half, "cpu")
    assert {p.dtype for p in w.model.parameters()} == {torch.float32}


def test_fy_tfm_018_one_request_at_a_time_and_no_baked_model(app, tiny, monkeypatch):
    """FY-TFM-018: a replica works on one request at a time (its lock is held while the model runs); --task embed or
    classify without --model stops the server (no model is baked into the image for them)."""
    c, w = serve(app, "classify", tiny["bert-fixed"])
    seen = []
    real = w.run

    def run(fields):
        seen.append(w.lock.locked())
        return real(fields)

    w.run = run
    assert c.post("/classify", headers=KEY, json={"input": "x"}).status_code == 200 and seen == [True]
    for task in ("embed", "classify"):
        monkeypatch.setattr("sys.argv", ["app.py", "--task", task, "--device", "cpu"])
        with pytest.raises(SystemExit, match="no model baked"):
            app.main()
    assert isinstance(w.lock, type(threading.Lock()))
