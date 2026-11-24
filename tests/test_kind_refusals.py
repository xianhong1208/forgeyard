"""Kinds of models that look like others (1.4.3, #147): zero-shot (open-vocabulary) object detectors, whose
architectures end in ForObjectDetection like the detectors the vision server loads (refused in 1.4.3, supported
since 1.4.4 as detect models marked zero_shot, #142), and text classifiers, tagged text-classification like many
cross-encoder rerankers (served as text classification models since 1.4.4, #144: tests/test_text_classify.py). The
configs and Hugging Face answers are the real ones, saved from the repositories in tests/fixtures/models/kinds
(FY-KIND-*)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from forgeyard.models import fetch, inspect as inspector, sources, vision
from tests.model_helpers import FIX, FakeHF, Repo, add_catalog, safetensors_bytes
from tests.test_scheduler import deploy
from tests.workload_helpers import make_node

KINDS = FIX / "kinds"
ROOT = Path(__file__).resolve().parent.parent
ZERO_SHOT = ("grounding-dino-tiny", "owlv2-base-patch16-ensemble", "owlvit-base-patch32", "omdet-turbo-swin-tiny-hf",
             "mm-grounding-dino-tiny", "llmdet-tiny")
MIB = 1024**2


def config(name: str) -> dict:
    return json.loads((KINDS / f"{name}.config.json").read_text())


def hf_info(name: str) -> dict:
    return json.loads((KINDS / f"{name}.hf-info.json").read_text())


def repo(name: str, sha: str, cfg: dict | None = None) -> Repo:
    """A repository with the real config.json and Hugging Face's real pipeline tag, tags and licence for it, and
    a small safetensors file for weights."""
    info = hf_info(name)
    st = safetensors_bytes({"w": {"dtype": "F32", "shape": [64], "data_offsets": [0, 256]}}, b"\0" * 256)
    r = Repo(info["id"], sha, {"config.json": json.dumps(cfg or config(name)).encode(), "model.safetensors": st})
    r.info.update(pipeline_tag=info["pipeline_tag"], library_name=info["library_name"], tags=info["tags"],
                  cardData=info["cardData"])
    return r


def qwen3_reranker() -> Repo:
    """Qwen3-Reranker-0.6B: a causal-LM reranker (Qwen3ForCausalLM, text-ranking), not a sequence classifier."""
    saved = json.loads((FIX / "qwen3-reranker-0.6b.hf-revision.json").read_text())
    st = safetensors_bytes({"w": {"dtype": "BF16", "shape": [64], "data_offsets": [0, 128]}}, b"\0" * 128)
    r = Repo("Qwen/Qwen3-Reranker-0.6B", "9" * 40,
             {"config.json": (FIX / "qwen3-reranker-0.6b.config.json").read_bytes(), "model.safetensors": st})
    r.info.update(pipeline_tag=saved.get("pipeline_tag") or "text-ranking", tags=saved.get("tags") or [],
                  cardData={"license": "apache-2.0"})
    return r


@pytest.fixture()
def hf(monkeypatch):
    from forgeyard.config import get_settings

    shas = iter(f"{i:x}" * 40 for i in range(1, 16))
    names = ZERO_SHOT + ("rtdetr-v2-r18vd", "bge-reranker-v2-m3", "ms-marco-minilm-l6-v2", "distilbert-sst-2",
                         "nli-deberta-v3-base")
    server = FakeHF(*[repo(n, next(shas)) for n in names], qwen3_reranker())
    monkeypatch.setenv("HF_ENDPOINT", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    yield server
    server.close()
    get_settings.cache_clear()


def inspect(server: FakeHF, repo_id: str, task: str | None = None) -> dict:
    return inspector.inspect_hf(repo_id, None, None, endpoint=server.url, policy=fetch.Policy(allow_private=True),
                                task=task)


# ------------------------------------------------------------------ zero-shot detectors
def test_fy_kind_001_zero_shot_detectors_are_known_by_model_type_or_architecture():
    """FY-KIND-001 (1.4.4, #142): the real configs of Grounding DINO, OWLv2, OWL-ViT, OmDet-Turbo, MM-Grounding-DINO
    and LLMDet are zero-shot detectors: detect models (their architectures end in ForObjectDetection) marked
    zero_shot, with no classes (id2label means nothing for them), so the catalog takes them; OmDet-Turbo, whose
    Swin backbone comes from timm (use_timm_backbone), has the timm problem and is refused like DETR with timm. A
    model_type alone, an architecture alone (any case) or a model_type spelled with underscores is enough;
    RT-DETRv2 and other detectors are not zero-shot."""
    for name in ZERO_SHOT:
        cfg = config(name)
        assert vision.zero_shot(cfg), name
        s = vision.summary(cfg)
        assert s["task"] == "detect" and s["zero_shot"] is True, name
        assert "labels" not in s and "label_examples" not in s, name
        why = vision.refusal("detect", {"kind": "hf", "license": "apache-2.0", "vision": s})
        if name == "omdet-turbo-swin-tiny-hf":
            assert s["problem"] == vision.TIMM_PROBLEM and "timm backbone" in why, name
            continue
        assert "problem" not in s and why is None, name
        assert vision.refusal("detect", {"kind": "hf", "license": "cc-by-nc-4.0", "vision": s}).startswith(
            "Vision models are added with a permissive licence only"), name
    assert vision.zero_shot({"model_type": "owlv2"})                                       # no architectures
    assert vision.zero_shot({"model_type": "grounding_dino", "architectures": ["Whatever"]})
    assert vision.zero_shot({"architectures": ["groundingdinoforobjectdetection"]})         # no model_type
    assert vision.zero_shot({"model_type": "custom", "architectures": ["X", "OmDetTurboForObjectDetection"]})
    rt = config("rtdetr-v2-r18vd")
    assert not vision.zero_shot(rt)
    s = vision.summary(rt)
    assert s["task"] == "detect" and "problem" not in s and "zero_shot" not in s and s["labels"] == 80
    assert vision.refusal("detect", {"kind": "hf", "license": "apache-2.0", "vision": s}) is None
    for cfg in ({"architectures": ["DFineForObjectDetection"], "model_type": "d_fine"},
                {"architectures": ["DetrForObjectDetection"], "model_type": "detr"},
                {"architectures": ["YolosForObjectDetection"], "model_type": "yolos"}, {}, {"model_type": 7}):
        assert not vision.zero_shot(cfg), cfg


def test_fy_kind_002_the_list_follows_the_vision_images_transformers():
    """FY-KIND-002: the zero-shot list is transformers' MODEL_FOR_ZERO_SHOT_OBJECT_DETECTION_MAPPING_NAMES of the
    version both vision images pin; when the pin moves, this fails until the list is checked again (and
    ZERO_SHOT_CHECKED_WITH moved)."""
    assert vision.ZERO_SHOT_DETECT == {
        "grounding-dino": "GroundingDinoForObjectDetection", "mm-grounding-dino": "MMGroundingDinoForObjectDetection",
        "omdet-turbo": "OmDetTurboForObjectDetection", "owlv2": "Owlv2ForObjectDetection",
        "owlvit": "OwlViTForObjectDetection"}
    for build in ("cpu", "cuda"):
        text = (ROOT / "images" / "vision" / f"requirements-{build}.txt").read_text()
        pins = re.findall(r"^transformers==([0-9][^ \\\n]*)", text, re.M)
        assert pins == [vision.ZERO_SHOT_CHECKED_WITH], (build, pins)


def test_fy_kind_003_a_zero_shot_detector_is_added_as_a_detection_model(owner, hf):
    """FY-KIND-003 (1.4.4): the inspector reads Grounding DINO and OWLv2 as detection models marked zero_shot (the
    preview says so); they are added with the task from Hugging Face (zero-shot-object-detection) or detect asked
    for, served by the vision recipe, and the catalog says zero_shot instead of classes. RT-DETRv2 is added as
    before, with its 80 classes."""
    m = inspect(hf, "IDEA-Research/grounding-dino-tiny")
    assert m["task_hint"] == "detect" and m["vision"]["zero_shot"] is True and "problem" not in m["vision"]
    prev = owner.post("/api/models/inspect", json={"source": "hf://google/owlv2-base-patch16-ensemble"})
    assert prev.status_code == 200 and prev.json()["meta"]["vision"]["zero_shot"] is True
    for name, source, task in [("gdino", "hf://IDEA-Research/grounding-dino-tiny", None),
                               ("owl", "hf://google/owlv2-base-patch16-ensemble", "detect"),
                               ("llmdet", "hf://iSEE-Laboratory/llmdet_tiny", None),
                               ("owlvit", "hf://google/owlvit-base-patch32", None)]:
        r = owner.post("/api/models", json={"name": name, "source": source, **({"task": task} if task else {})})
        assert r.status_code == 201, (name, r.text)
        out = r.json()["model"]
        assert out["task"] == "detect" and out["recipes"] == ["vision"], name
        assert out["vision"]["zero_shot"] is True and "labels" not in out["vision"], name
    r = owner.post("/api/models", json={"name": "omdet", "source": "hf://omlab/omdet-turbo-swin-tiny-hf"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "not_a_vision_model"
    assert "timm backbone" in r.json()["detail"]["message"]
    ok = owner.post("/api/models", json={"name": "rtdetr", "source": "hf://PekingU/rtdetr_v2_r18vd"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["model"]["task"] == "detect" and ok.json()["model"]["recipes"] == ["vision"]
    assert ok.json()["model"]["vision"]["labels"] == 80 and "zero_shot" not in ok.json()["model"]["vision"]


def _old_row(name: str, source: str, arch: str, model_type: str, archs: list | None = None) -> dict:
    """A detection model as 1.4.0–1.4.2 kept it: no zero_shot flag, no problem (`archs`: config.json's
    architectures, when not just `arch`)."""
    return {"kind": "hf", "repo": source[5:], "ref": "main", "revision": "c" * 40, "format": "safetensors",
            "pipeline_tag": "zero-shot-object-detection", "license": "apache-2.0", "modalities": [],
            "task_hint": "detect", "vision": {"task": "detect", "architecture": arch, "labels": 2,
                                              "label_examples": ["LABEL_0", "LABEL_1"]},
            "config": {"architectures": [arch] if archs is None else archs, "model_type": model_type},
            "weights": {"bytes": 600 * MIB, "params": {"F32": 150 * MIB}},
            "files": [{"path": "config.json", "size": 2000, "oid": "a" * 40},
                      {"path": "model.safetensors", "size": 600 * MIB, "sha256": "b" * 64}],
            "size_bytes": 600 * MIB + 2000}


NEW_CPU = "docker.io/tonyhuang1101/forgeyard-vision:1.4.4@sha256:" + "4" * 64
NEW_CUDA = "docker.io/tonyhuang1101/forgeyard-vision:1.4.4-cuda@sha256:" + "5" * 64
# The image the built-in recipe pinned before 1.4.4rc1.
OLD_CPU = "docker.io/tonyhuang1101/forgeyard-vision:1.4.0@sha256:ab60eb2164671b9158573e6afe39068dcf407cc4286d053b118e25de29c244d6"
OLD_CUDA = "docker.io/tonyhuang1101/forgeyard-vision:1.4.0-cuda@sha256:773449716b20e961df38b1518422b0120b3e919239063ac128ef5b4affca47b3"


def _vision_copy(name: str, cpu: str, cuda: str) -> str:
    """The built-in vision recipe as a custom one named `name`, with these images."""
    from forgeyard.models import recipes

    text = recipes.builtin_recipes()["vision"][1].replace("name: vision\n", f"name: {name}\n", 1)
    old = [v.image for v in recipes.parse_recipe(recipes.builtin_recipes()["vision"][1], builtin=True).variants]
    return text.replace(f"image: {old[0]}", f"image: {cuda}").replace(f"image: {old[1]}", f"image: {cpu}")


def test_fy_kind_004_a_zero_shot_detector_runs_on_a_vision_image_from_1_4_4_on(owner):
    """FY-KIND-004 (1.4.4): a zero-shot detector added before 1.4.3 (no flag) is known from the model_type or the
    architecture the catalog kept, and listed as zero_shot without the classes it was given then (no migration).
    On a recipe whose Forgeyard vision image is older than 1.4.4 (the built-in one until 1.4.4rc1), deploying
    or estimating it answers 422 vision_image_too_old naming both versions; on an image from 1.4.4 on, or another
    image (a local build), it deploys. A detector that is not zero-shot deploys on the old image too; OmDet-Turbo,
    which needs timm, is refused on any image (422 not_a_vision_model)."""
    add_catalog("gdino", _old_row("gdino", "hf://IDEA-Research/grounding-dino-tiny", "GroundingDinoForObjectDetection",
                                  "grounding-dino"), task="detect", fmt="safetensors",
                source="hf://IDEA-Research/grounding-dino-tiny")
    add_catalog("owl", _old_row("owl", "hf://google/owlv2-base-patch16-ensemble", "SomethingRenamed", "owlv2"),
                task="detect", fmt="safetensors", source="hf://google/owlv2-base-patch16-ensemble")
    # Known only by an architecture config.json lists second, or only by the one meta["vision"] kept.
    add_catalog("owl2", _old_row("owl2", "hf://acme/owl", "CustomHead", "custom", ["CustomHead", "Owlv2ForObjectDetection"]),
                task="detect", fmt="safetensors", source="hf://acme/owl")
    add_catalog("mm", _old_row("mm", "hf://acme/mm", "MMGroundingDinoForObjectDetection", "custom", []),
                task="detect", fmt="safetensors", source="hf://acme/mm")
    add_catalog("rtdetr", _old_row("rtdetr", "hf://PekingU/rtdetr_v2_r18vd", "RtDetrV2ForObjectDetection",
                                   "rt_detr_v2"), task="detect", fmt="safetensors", source="hf://PekingU/rtdetr_v2_r18vd")
    add_catalog("omdet", _old_row("omdet", "hf://omlab/omdet-turbo-swin-tiny-hf", "OmDetTurboForObjectDetection",
                                  "omdet-turbo"), task="detect", fmt="safetensors", source="hf://omlab/omdet-turbo-swin-tiny-hf")
    listed = {m["name"]: m for m in owner.get("/api/models").json()["models"]}
    for model in ("gdino", "owl", "owl2", "mm"):
        assert listed[model]["vision"]["zero_shot"] is True and "labels" not in listed[model]["vision"], model
    assert listed["rtdetr"]["vision"]["labels"] == 2 and "zero_shot" not in listed["rtdetr"]["vision"]
    assert owner.post("/api/recipes", json={"yaml": _vision_copy("vision-new", NEW_CPU, NEW_CUDA)}).status_code == 201
    assert owner.post("/api/recipes", json={"yaml": _vision_copy("vision-old", OLD_CPU, OLD_CUDA)}).status_code == 201
    assert owner.post("/api/recipes", json={"yaml": _vision_copy(
        "vision-local", "forgeyard-local/forgeyard-vision:zs-test", "forgeyard-local/forgeyard-vision:zs-test-cuda")
    }).status_code == 201
    cpu = make_node("cpu-a", features=("workloads", "models"))
    for model in ("gdino", "owl", "owl2", "mm"):
        body = {"name": f"{model}-dep", "model": model, "recipe": "vision-old", "params": {"gpus": 0}, "machine_id": cpu,
                "wait": True}
        for path in ("/api/deployments", "/api/deployments/estimate"):
            r = owner.post(path, json=body)
            assert r.status_code == 422, (model, path, r.text)
            d = r.json()["detail"]
            assert d["code"] == "vision_image_too_old", (model, d)
            assert "needs Forgeyard's vision image 1.4.4 or newer; the vision-old recipe runs 1.4.0" in d["message"]
    assert owner.get("/api/models").status_code == 200
    deploy(owner, "rtdetr-dep", "rtdetr", "vision-old", params={"gpus": 0}, machine_id=cpu, wait=True)
    deploy(owner, "gdino-new", "gdino", "vision-new", params={"gpus": 0}, machine_id=cpu, wait=True)
    deploy(owner, "gdino-builtin", "gdino", "vision", params={"gpus": 0}, machine_id=cpu, wait=True)
    deploy(owner, "owl-local", "owl", "vision-local", params={"gpus": 0}, machine_id=cpu, wait=True)
    # OmDet-Turbo (kept from before 1.4.3) needs timm, whatever the image.
    for recipe in ("vision", "vision-new"):
        r = owner.post("/api/deployments", json={"name": "omdet-dep", "model": "omdet", "recipe": recipe,
                                                 "params": {"gpus": 0}, "machine_id": cpu})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "not_a_vision_model", (recipe, r.text)
        assert "timm backbone" in r.json()["detail"]["message"]


def test_fy_kind_007_which_vision_images_run_zero_shot_detectors():
    """FY-KIND-007 (1.4.4): the version is read from a Forgeyard vision image's tag (CPU or -cuda, pinned or not);
    an image of another name has none and is taken as it is; the refusal is for zero-shot detectors only, and
    names the oldest image of the recipe's variants."""
    from types import SimpleNamespace as NS

    assert vision.image_version("docker.io/tonyhuang1101/forgeyard-vision:1.4.0@sha256:" + "a" * 64) == (1, 4, 0)
    assert vision.image_version("docker.io/tonyhuang1101/forgeyard-vision:1.4.0-cuda@sha256:" + "a" * 64) == (1, 4, 0)
    assert vision.image_version("docker.io/x/forgeyard-vision:1.10.2") == (1, 10, 2)
    for other in ("forgeyard-local/vision:test", "docker.io/x/forgeyard-vision:latest", "docker.io/x/forgeyard-vision",
                  "docker.io/x/my-forgeyard-vision:1.4.0", None, ""):
        assert vision.image_version(other) is None, other
    zs = {"kind": "hf", "vision": {"task": "detect", "architecture": "Owlv2ForObjectDetection", "zero_shot": True}}
    rt = {"kind": "hf", "vision": {"task": "detect", "architecture": "RtDetrV2ForObjectDetection"}}

    def recipe(*images):
        return NS(name="r", variants=[NS(image=i) for i in images])

    old = recipe("docker.io/x/forgeyard-vision:1.4.3-cuda@sha256:" + "a" * 64, "docker.io/x/forgeyard-vision:1.4.4")
    code, why = vision.deploy_refusal("detect", zs, old)
    assert code == "vision_image_too_old"
    assert why.startswith("This zero-shot detector needs Forgeyard's vision image 1.4.4 or newer; the r recipe runs 1.4.3.")
    assert vision.deploy_refusal("detect", zs, recipe("docker.io/x/forgeyard-vision:1.4.4", "docker.io/x/forgeyard-vision:2.0.0-cuda")) is None
    assert vision.deploy_refusal("detect", zs, recipe("local/vision:dev", None)) is None
    assert vision.deploy_refusal("detect", rt, old) is None
    assert vision.deploy_refusal("depth", zs, old) is None
    assert vision.deploy_refusal("detect", zs, None) is None
    assert vision.deploy_refusal("detect", {**zs, "kind": "local"}, old) is None


def test_fy_kind_008_a_pre_release_vision_image_counts_as_its_release():
    """FY-KIND-008 (1.4.4): a pre-release tag of the vision image (PEP 440 aN, bN or rcN, CPU or -cuda, pinned or
    not) is read as the release it leads to, so 1.4.4rc1 runs zero-shot detectors and image classifiers and
    1.4.3rc1 is refused as older than 1.4.4; the built-in recipe's own images (1.4.4rc1) are read as 1.4.4, not
    passed as images of another name. A tag that is no PEP 440 pre-release stays unread."""
    from types import SimpleNamespace as NS

    from forgeyard.models import recipes

    digest = "@sha256:" + "b" * 64
    for tag, want in (("1.4.4rc1", (1, 4, 4)), ("1.4.4rc1-cuda", (1, 4, 4)), ("1.4.4a2", (1, 4, 4)),
                      ("1.4.4b10-cuda", (1, 4, 4)), ("1.4.3rc1", (1, 4, 3)), ("1.4.3rc1-cuda", (1, 4, 3)),
                      ("1.10.0rc12", (1, 10, 0))):
        for suffix in ("", digest):
            assert vision.image_version(f"docker.io/x/forgeyard-vision:{tag}{suffix}") == want, (tag, suffix)
    for other in ("docker.io/x/forgeyard-vision:1.4.4rc", "docker.io/x/forgeyard-vision:1.4.4c1",
                  "docker.io/x/forgeyard-vision:1.4.4-rc1x!", "docker.io/x/forgeyard-vision:1.4.4.rc1",
                  "docker.io/x/forgeyard-vision:1.4.4rc1rc2"):
        assert vision.image_version(other) is None, other

    recipe, _ = recipes.builtin_recipes()["vision"]
    images = [v.image for v in recipe.variants if v.image]
    assert images and all("/forgeyard-vision:1.4.4rc1" in i for i in images), images
    assert {vision.image_version(i) for i in images} == {(1, 4, 4)}

    zs = {"kind": "hf", "vision": {"task": "detect", "architecture": "Owlv2ForObjectDetection", "zero_shot": True}}
    assert vision.deploy_refusal("detect", zs, recipe) is None
    assert vision.deploy_refusal("image-classify", {"kind": "hf"}, recipe) is None

    def r(*tags):
        return NS(name="r", variants=[NS(image=f"docker.io/x/forgeyard-vision:{t}{digest}") for t in tags])

    assert vision.deploy_refusal("detect", zs, r("1.4.4rc1", "1.4.4rc1-cuda")) is None
    for what, task, meta in (("This zero-shot detector", "detect", zs), ("Image classification", "image-classify",
                                                                         {"kind": "hf"})):
        code, why = vision.deploy_refusal(task, meta, r("1.4.4rc1", "1.4.3rc1-cuda"))
        assert code == "vision_image_too_old"
        assert why.startswith(f"{what} needs Forgeyard's vision image 1.4.4 or newer; the r recipe runs 1.4.3."), why


# ------------------------------------------------------------------ text classifiers and rerankers
def test_fy_kind_005_one_label_is_a_reranker_more_is_a_classifier():
    """FY-KIND-005: vLLM's rule on the real configs: bge-reranker-v2-m3 and ms-marco-MiniLM (one label) are
    rerankers; DistilBERT SST-2 (2 labels) and an NLI cross-encoder (3) are text classifiers, refused as rerankers
    with their labels (1.4.4: "add it as a text classification model"); Qwen3-Reranker (a causal LM) is no sequence
    classifier at all. The count is read as transformers reads it: id2label first, then num_labels, then its
    default of 2."""
    assert sources.sequence_labels(config("bge-reranker-v2-m3")) == 1
    assert sources.sequence_labels(config("ms-marco-minilm-l6-v2")) == 1
    assert sources.reranker_refusal(config("bge-reranker-v2-m3")) is None
    assert sources.reranker_refusal(config("ms-marco-minilm-l6-v2")) is None
    qwen = json.loads((FIX / "qwen3-reranker-0.6b.config.json").read_text())
    assert sources.sequence_labels(qwen) is None and sources.reranker_refusal(qwen) is None
    assert sources.sequence_labels(config("distilbert-sst-2")) == 2
    assert sources.reranker_refusal(config("distilbert-sst-2")) == (
        "This is a text classifier with 2 labels (NEGATIVE, POSITIVE), not a reranker: a reranker has one output "
        "(num_labels 1 in its config.json). Add it as a text classification model.")
    nli = sources.reranker_refusal(config("nli-deberta-v3-base"))
    assert nli.startswith("This is a text classifier with 3 labels (contradiction, entailment, neutral)")
    assert nli.endswith("Add it as a text classification model.")
    seq = {"architectures": ["BertForSequenceClassification"]}
    assert sources.sequence_labels(seq) == 2                                     # transformers' default
    assert sources.sequence_labels({**seq, "num_labels": 1}) == 1
    assert sources.sequence_labels({**seq, "num_labels": 1, "id2label": {"0": "a", "1": "b"}}) == 2
    assert sources.sequence_labels({**seq, "id2label": {}, "num_labels": 1}) == 1
    assert sources.sequence_labels({**seq, "num_labels": True}) == 2
    assert "28 labels (" in sources.reranker_refusal({**seq, "id2label": {str(i): f"e{i}" for i in range(28)}})
    assert sources.reranker_refusal({**seq, "id2label": {str(i): f"e{i}" for i in range(28)}}).count(", …") == 1
    assert sources.reranker_refusal({**seq, "num_labels": 4}).startswith("This is a text classifier with 4 labels, ")


def test_fy_kind_006_adding_a_text_classifier_as_a_reranker_is_refused(owner, hf):
    """FY-KIND-006: rerankers tagged text-classification (bge-reranker-v2-m3) or text-ranking (ms-marco-MiniLM, and
    the causal-LM Qwen3-Reranker) are inspected and added as rerank models, as before. 1.4.4 (#144): without a task a
    sentiment model and an NLI model (tagged text-classification too) are read as text classifiers; asked as
    rerankers, they are refused with their labels (422 cannot_inspect). 1.5.4: as text classifiers they are added,
    served by the transformers server only (vLLM runs neither DistilBERT nor an NLI model, which needs labels in the
    request). Asked for another task, the classifier's hint stays text classification."""
    for repo_id in ("BAAI/bge-reranker-v2-m3", "cross-encoder/ms-marco-MiniLM-L6-v2", "Qwen/Qwen3-Reranker-0.6B"):
        assert inspect(hf, repo_id)["task_hint"] == "rerank", repo_id
    for name, source in [("bge-rr", "hf://BAAI/bge-reranker-v2-m3"), ("msmarco", "hf://cross-encoder/ms-marco-MiniLM-L6-v2"),
                         ("qwen-rr", "hf://Qwen/Qwen3-Reranker-0.6B")]:
        r = owner.post("/api/models", json={"name": name, "source": source})
        assert r.status_code == 201, (name, r.text)
        assert r.json()["model"]["task"] == "rerank" and "vllm-pooling" in r.json()["model"]["recipes"], name
    sst2, nli = "distilbert/distilbert-base-uncased-finetuned-sst-2-english", "cross-encoder/nli-deberta-v3-base"
    for repo_id, task, why in [(sst2, "rerank", "text classifier with 2 labels"),
                               (nli, "rerank", "text classifier with 3 labels")]:
        with pytest.raises(inspector.InspectError, match=why):
            inspect(hf, repo_id, task)
    assert inspect(hf, sst2)["classify"]["architecture"] == "DistilBertForSequenceClassification"
    assert inspect(hf, nli)["classify"]["zero_shot"] is True
    r = owner.post("/api/models", json={"name": "sst2-rr", "source": f"hf://{sst2}", "task": "rerank"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "cannot_inspect"
    assert "text classifier with 2 labels" in r.json()["detail"]["message"]
    for name, source in [("sst2", f"hf://{sst2}"), ("nli", f"hf://{nli}")]:
        r = owner.post("/api/models", json={"name": name, "source": source})
        assert r.status_code == 201, (name, r.text)
        assert r.json()["model"]["task"] == "classify" and r.json()["model"]["recipes"] == ["transformers"], name
    prev = owner.post("/api/models/inspect", json={"source": f"hf://{sst2}", "task": "rerank"})
    assert prev.status_code == 422 and "NEGATIVE, POSITIVE" in prev.json()["detail"]["message"]
    other = inspect(hf, sst2, "embed")
    assert other["task_hint"] == "classify" and other["format"] == "safetensors" and "classify" not in other
    names = {m["name"] for m in owner.get("/api/models").json()["models"]}
    assert {"bge-rr", "msmarco", "qwen-rr", "sst2", "nli"} <= names and "sst2-rr" not in names
