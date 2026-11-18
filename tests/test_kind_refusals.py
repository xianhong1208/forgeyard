"""Kinds of models the catalog refuses until they are supported (1.4.3, #147): zero-shot (open-vocabulary)
object detectors, whose architectures end in ForObjectDetection like the detectors the vision server loads
(support comes with #142), and text classifiers, tagged text-classification like many cross-encoder
rerankers (support comes with #144). The configs and Hugging Face answers are the real ones, saved from the
repositories in tests/fixtures/models/kinds (FY-KIND-*)."""

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
    """FY-KIND-001: the real configs of Grounding DINO, OWLv2, OWL-ViT, OmDet-Turbo, MM-Grounding-DINO and LLMDet
    are zero-shot detectors: still read as detect (their architectures end in ForObjectDetection), with the
    problem that names #142, so the catalog refuses them as detection models. A model_type alone, an
    architecture alone (any case) or a model_type spelled with underscores is enough; RT-DETRv2 and other
    detectors are not zero-shot."""
    for name in ZERO_SHOT:
        cfg = config(name)
        assert vision.zero_shot(cfg), name
        s = vision.summary(cfg)
        assert s["task"] == "detect" and s["zero_shot"] is True, name
        assert s["problem"] == vision.ZERO_SHOT_PROBLEM and "#142" in s["problem"], name
        why = vision.refusal("detect", {"kind": "hf", "license": "apache-2.0", "vision": s})
        assert why == f"This model cannot run on Forgeyard's vision server: {vision.ZERO_SHOT_PROBLEM}.", name
        assert "zero-shot (open-vocabulary) detector" in why
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


def test_fy_kind_003_adding_a_zero_shot_detector_is_refused(owner, hf):
    """FY-KIND-003: the inspector reads Grounding DINO and OWLv2 as detection models with the problem (the preview
    shows it); adding either, with the task from Hugging Face or detect asked for, answers 422
    not_a_vision_model naming #142, and re-tasking a model to detect is refused the same way. RT-DETRv2 is
    added as before."""
    m = inspect(hf, "IDEA-Research/grounding-dino-tiny")
    assert m["task_hint"] == "detect" and m["vision"]["zero_shot"] is True and "#142" in m["vision"]["problem"]
    prev = owner.post("/api/models/inspect", json={"source": "hf://google/owlv2-base-patch16-ensemble"})
    assert prev.status_code == 200 and "#142" in prev.json()["meta"]["vision"]["problem"]
    for name, source, task in [("gdino", "hf://IDEA-Research/grounding-dino-tiny", None),
                               ("owl", "hf://google/owlv2-base-patch16-ensemble", "detect"),
                               ("llmdet", "hf://iSEE-Laboratory/llmdet_tiny", None)]:
        r = owner.post("/api/models", json={"name": name, "source": source, **({"task": task} if task else {})})
        assert r.status_code == 422, (name, r.text)
        d = r.json()["detail"]
        assert d["code"] == "not_a_vision_model" and "zero-shot (open-vocabulary) detector" in d["message"], name
        assert "support comes with #142" in d["message"]
    ok = owner.post("/api/models", json={"name": "rtdetr", "source": "hf://PekingU/rtdetr_v2_r18vd"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["model"]["task"] == "detect" and ok.json()["model"]["recipes"] == ["vision"]


def _old_row(name: str, source: str, arch: str, model_type: str, archs: list | None = None) -> dict:
    """A detection model as 1.4.0–1.4.2 kept it: no zero_shot flag, no problem (`archs`: config.json's
    architectures, when not just `arch`)."""
    return {"kind": "hf", "repo": source[5:], "ref": "main", "revision": "c" * 40, "format": "safetensors",
            "pipeline_tag": "zero-shot-object-detection", "license": "apache-2.0", "modalities": [],
            "task_hint": "detect", "vision": {"task": "detect", "architecture": arch},
            "config": {"architectures": [arch] if archs is None else archs, "model_type": model_type},
            "weights": {"bytes": 600 * MIB, "params": {"F32": 150 * MIB}},
            "files": [{"path": "config.json", "size": 2000, "oid": "a" * 40},
                      {"path": "model.safetensors", "size": 600 * MIB, "sha256": "b" * 64}],
            "size_bytes": 600 * MIB + 2000}


def test_fy_kind_004_a_zero_shot_detector_already_in_the_catalog_is_not_deployed(owner):
    """FY-KIND-004: a zero-shot detector added before 1.4.3 stays in the catalog (no migration), but deploying it,
    or estimating a deployment of it, answers 422 not_a_vision_model with the same message, known from the
    model_type or the architecture the catalog kept; a detector that is not zero-shot still deploys."""
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
    cpu = make_node("cpu-a", features=("workloads", "models"))
    for model in ("gdino", "owl", "owl2", "mm"):
        body = {"name": f"{model}-dep", "model": model, "recipe": "vision", "params": {"gpus": 0}, "machine_id": cpu,
                "wait": True}
        for path in ("/api/deployments", "/api/deployments/estimate"):
            r = owner.post(path, json=body)
            assert r.status_code == 422, (model, path, r.text)
            assert r.json()["detail"] == {"code": "not_a_vision_model", "message":
                                          f"This model cannot run on Forgeyard's vision server: {vision.ZERO_SHOT_PROBLEM}."}
    assert owner.get("/api/models").status_code == 200
    deploy(owner, "rtdetr-dep", "rtdetr", "vision", params={"gpus": 0}, machine_id=cpu, wait=True)
    assert vision.deploy_refusal("depth", _old_row("x", "hf://a/b", "OwlViTForObjectDetection", "owlvit")) is None
    assert vision.deploy_refusal("detect", {**_old_row("x", "hf://a/b", "OwlViTForObjectDetection", "owlvit"),
                                            "kind": "local"}) is None


# ------------------------------------------------------------------ text classifiers and rerankers
def test_fy_kind_005_one_label_is_a_reranker_more_is_a_classifier():
    """FY-KIND-005: vLLM's rule on the real configs: bge-reranker-v2-m3 and ms-marco-MiniLM (one label) are
    rerankers; DistilBERT SST-2 (2 labels) and an NLI cross-encoder (3) are text classifiers, refused with
    their labels and #144; Qwen3-Reranker (a causal LM) is no sequence classifier at all. The count is read as
    transformers reads it: id2label first, then num_labels, then its default of 2."""
    assert sources.sequence_labels(config("bge-reranker-v2-m3")) == 1
    assert sources.sequence_labels(config("ms-marco-minilm-l6-v2")) == 1
    assert sources.reranker_refusal(config("bge-reranker-v2-m3")) is None
    assert sources.reranker_refusal(config("ms-marco-minilm-l6-v2")) is None
    qwen = json.loads((FIX / "qwen3-reranker-0.6b.config.json").read_text())
    assert sources.sequence_labels(qwen) is None and sources.reranker_refusal(qwen) is None
    assert sources.sequence_labels(config("distilbert-sst-2")) == 2
    assert sources.reranker_refusal(config("distilbert-sst-2")) == (
        "This is a text classifier with 2 labels (NEGATIVE, POSITIVE), not a reranker: a reranker has one output "
        "(num_labels 1 in its config.json). Text classification comes with #144.")
    nli = sources.reranker_refusal(config("nli-deberta-v3-base"))
    assert nli.startswith("This is a text classifier with 3 labels (contradiction, entailment, neutral)") and "#144" in nli
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
    the causal-LM Qwen3-Reranker) are inspected and added as rerank models, as before; a sentiment model tagged
    text-classification, and an NLI model (its tags say text-classification too), are refused when inspected
    or added (422 cannot_inspect naming #144), whether the task comes from Hugging Face or rerank is asked for.
    Asked for another task, the classifier is not read as a reranker."""
    for repo_id in ("BAAI/bge-reranker-v2-m3", "cross-encoder/ms-marco-MiniLM-L6-v2", "Qwen/Qwen3-Reranker-0.6B"):
        assert inspect(hf, repo_id)["task_hint"] == "rerank", repo_id
    for name, source in [("bge-rr", "hf://BAAI/bge-reranker-v2-m3"), ("msmarco", "hf://cross-encoder/ms-marco-MiniLM-L6-v2"),
                         ("qwen-rr", "hf://Qwen/Qwen3-Reranker-0.6B")]:
        r = owner.post("/api/models", json={"name": name, "source": source})
        assert r.status_code == 201, (name, r.text)
        assert r.json()["model"]["task"] == "rerank" and "vllm-pooling" in r.json()["model"]["recipes"], name
    for repo_id, task in [("distilbert/distilbert-base-uncased-finetuned-sst-2-english", None),
                          ("cross-encoder/nli-deberta-v3-base", None), ("cross-encoder/nli-deberta-v3-base", "rerank")]:
        with pytest.raises(inspector.InspectError, match="text classifier .*#144"):
            inspect(hf, repo_id, task)
    for name, source, task in [("sst2", "hf://distilbert/distilbert-base-uncased-finetuned-sst-2-english", None),
                               ("sst2-rr", "hf://distilbert/distilbert-base-uncased-finetuned-sst-2-english", "rerank"),
                               ("nli", "hf://cross-encoder/nli-deberta-v3-base", None)]:
        r = owner.post("/api/models", json={"name": name, "source": source, **({"task": task} if task else {})})
        assert r.status_code == 422, (name, r.text)
        d = r.json()["detail"]
        assert d["code"] == "cannot_inspect" and "text classifier with" in d["message"], name
        assert "Text classification comes with #144." in d["message"]
    prev = owner.post("/api/models/inspect", json={"source": "hf://distilbert/distilbert-base-uncased-finetuned-sst-2-english"})
    assert prev.status_code == 422 and "NEGATIVE, POSITIVE" in prev.json()["detail"]["message"]
    other = inspect(hf, "distilbert/distilbert-base-uncased-finetuned-sst-2-english", "embed")
    assert other["task_hint"] == "rerank" and other["format"] == "safetensors"
    names = {m["name"] for m in owner.get("/api/models").json()["models"]}
    assert {"bge-rr", "msmarco", "qwen-rr"} <= names and not names & {"sst2", "sst2-rr", "nli"}
