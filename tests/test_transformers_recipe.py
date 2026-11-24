"""The built-in `transformers` recipe (1.5.4, #141 and #144): Forgeyard's own transformers server (the vision image,
--task embed or classify) for image embeddings and text classification. The recipe and its rendering, which catalog
models it is offered for and in which order, the refusals at deploy (a model it does not run, a vision image from
before 1.5.4), the float32 estimate on a CPU, and the server and the catalog agreeing on what an image embedder is
(FY-TFM-001 to 006; the server itself: tests/test_transformers_server.py, FY-TFM-010 and on)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from forgeyard.models import encoders, recipes, vision
from forgeyard.models.estimate import estimate
from forgeyard.models.recipes import variant_matches
from tests.model_helpers import add_catalog
from tests.test_scheduler import MODELS, deploy, replica_row, seed_catalog
from tests.workload_helpers import make_node

FEATURES = MODELS + ("replica_keys", "replica_guard")
GIB = 1024**3
MIB = 1024**2
A100 = {"vendor": "nvidia", "arch": "8.0", "driver": "580.178.04"}
# Until the pin moves after the 1.5.4 release, the recipe names the vision recipe's own pinned image (1.4.4rc1).
VISION_CPU = ("docker.io/tonyhuang1101/forgeyard-vision:1.4.4rc1"
              "@sha256:f4e5940bdbfe1c88a6712aaf708432149651e25a9a6d7318e9f382ab697c579b")
VISION_CUDA = ("docker.io/tonyhuang1101/forgeyard-vision:1.4.4rc1-cuda"
               "@sha256:9e4ef32b302cf3d82a07d69a1a55e42b28c17e9e34f8e3399bfe8918d1362376")
NEW_CPU = "docker.io/tonyhuang1101/forgeyard-vision:1.5.4@sha256:" + "6" * 64
NEW_CUDA = "docker.io/tonyhuang1101/forgeyard-vision:1.5.4-cuda@sha256:" + "7" * 64


def st_meta(repo: str, config: dict, params: dict, size: int, **over) -> dict:
    """A safetensors model as the inspector reads it: its config summary and its weights' parameters by dtype."""
    meta = {"kind": "hf", "repo": repo, "ref": "main", "revision": "c" * 40, "format": "safetensors",
            "license": "apache-2.0", "modalities": [], "config": config,
            "weights": {"bytes": size, "params": params},
            "files": [{"path": "config.json", "size": 2000, "oid": "a" * 40},
                      {"path": "model.safetensors", "size": size, "sha256": "b" * 64}],
            "size_bytes": size + 2000}
    meta.update(over)
    return meta


CLIP = st_meta("laion/CLIP-ViT-B-32-laion2B-s34B-b79K", {"architectures": ["CLIPModel"], "model_type": "clip"},
               {"F32": 151_277_313}, 605_156_676)
SIGLIP = st_meta("google/siglip2-base-patch16-224", {"architectures": [], "model_type": "siglip"},
                 {"F32": 375_187_970}, 1_500_751_880)
DINO = st_meta("facebook/dinov2-small", {"architectures": ["Dinov2Model"], "model_type": "dinov2"},
               {"F32": 22_056_576}, 88_249_000)
NLI = st_meta("MoritzLaurer/bge-m3-zeroshot-v2.0-c",
              {"architectures": ["XLMRobertaForSequenceClassification"], "model_type": "xlm-roberta"},
              {"F16": 567_756_802}, 1_135_513_604, license="mit",
              classify={"architecture": "XLMRobertaForSequenceClassification", "labels": 2,
                        "label_names": ["entailment", "not_entailment"], "problem": "single_label_classification"})
SENTIMENT = st_meta("lxyuan/distilbert-base-multilingual-cased-sentiments-student",
                    {"architectures": ["DistilBertForSequenceClassification"], "model_type": "distilbert"},
                    {"F32": 135_326_979}, 541_317_884,
                    classify={"architecture": "DistilBertForSequenceClassification", "labels": 3,
                              "label_names": ["positive", "neutral", "negative"]})


@pytest.fixture()
def tfm(owner):
    """The acceptance catalog (Qwen3-Embedding among it), plus CLIP, SigLIP 2 and DINOv2 added as embed models and two
    text classifiers (bge-m3 NLI, DistilBERT sentiment); a CPU machine and the A100."""
    seed_catalog()
    for name, meta, task in (("clip", CLIP, "embed"), ("siglip2", SIGLIP, "embed"), ("dinov2", DINO, "embed"),
                             ("bge-m3-nli", NLI, "classify"), ("sentiment", SENTIMENT, "classify")):
        add_catalog(name, meta, task=task, fmt="safetensors", source=f"hf://{meta['repo']}")
    cpu = make_node("cpu-a", features=FEATURES)
    gpu = make_node("a100", allowlist=[4], features=FEATURES)
    return owner, cpu, gpu


def copy_on(owner, image_cpu: str, image_cuda: str, name: str) -> str:
    """A custom copy of the transformers recipe naming other images (a newer vision image, as the pin will)."""
    text = recipes.builtin_recipes()["transformers"][1].replace("name: transformers\n", f"name: {name}\n", 1)
    text = text.replace(f"image: {VISION_CUDA}", f"image: {image_cuda}").replace(f"image: {VISION_CPU}", f"image: {image_cpu}")
    assert owner.post("/api/recipes", json={"yaml": text}).status_code == 201
    return name


def test_fy_tfm_001_the_transformers_recipe():
    """FY-TFM-001: the built-in `transformers` recipe serves embed as openai-embeddings and classify as classify with the
    vision recipe's own pinned images (the CUDA build on NVIDIA 7.5+ with driver 580, the CPU build for x86_64 and
    aarch64): one image for both; the model from the cache as --model, its task as --task; the replica's key in
    VISION_API_KEY; threads, max_images and max_pairs as advanced parameters passed in VISION_* variables; the
    files estimate with float32 weights on a CPU. The vision recipe is unchanged."""
    r, text = recipes.builtin_recipes()["transformers"]
    v = recipes.builtin_recipes()["vision"][0]
    assert r.tasks == ["embed", "classify"] and r.formats == ["safetensors"] and r.engine == v.engine == "forgeyard-vision"
    assert r.api == {"embed": "openai-embeddings", "classify": "classify"} and r.upstream_model == "served_name"
    assert r.api_key_env == "VISION_API_KEY" and r.share == "vram" and r.health.path == "/health"
    assert r.estimate.method == "files" and r.estimate.cpu_float32 and not v.estimate.cpu_float32
    assert (r.estimate.overhead, r.estimate.cpu_overhead) == (GIB, 1280 * MIB)
    assert [(x.name, x.image) for x in r.variants] == [("cuda", VISION_CUDA), ("cpu", VISION_CPU)] == \
        [(x.name, x.image) for x in v.variants]
    assert variant_matches(r.variants[0], A100) is None and variant_matches(r.variants[0], {**A100, "arch": "7.0"})
    assert variant_matches(r.variants[1], {"vendor": "cpu", "arch": "aarch64"}) is None
    assert r.args[:4] == ["--task", "{task}", "--model", "{model_path}"]
    assert {k: (p.default, p.advanced) for k, p in r.params.items()} == {
        "threads": (4, True), "max_images": (32, True), "max_pairs": (256, True)}
    assert {k: r.env[k] for k in ("VISION_THREADS", "VISION_MAX_IMAGES", "VISION_MAX_PAIRS", "HF_HUB_OFFLINE")} == {
        "VISION_THREADS": "{threads}", "VISION_MAX_IMAGES": "{max_images}", "VISION_MAX_PAIRS": "{max_pairs}",
        "HF_HUB_OFFLINE": "1"}
    assert v.tasks == ["detect", "segment", "depth", "image-classify"]
    with pytest.raises(recipes.RecipeError, match="served as classify"):
        recipes.parse_recipe(text.replace("classify: classify}", "classify: openai-embeddings}"))


def test_fy_tfm_002_which_models_the_transformers_server_runs():
    """FY-TFM-002: as embed, a CLIP-like model (by architecture, or by model_type alone as SigLIP 2's checkpoints) or
    DINOv2 (with registers too), not a text embedder, a fine-tuned image classifier or BLIP; as classify, a text
    classifier of the catalog with two labels or more, not a regression head or a model without labels; a local model
    (its config unread) is taken. Only the transformers server's recipe (by engine and task, so a custom copy too) is
    filtered and put last; every other recipe is offered as before."""
    assert [encoders.embed_family(m) for m in (CLIP, SIGLIP, DINO)] == ["text+image", "text+image", "image"]
    others = [{"config": {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3"}},
              {"config": {"architectures": ["Dinov2ForImageClassification"], "model_type": "dinov2"}},
              {"config": {"architectures": ["BlipModel"], "model_type": "blip"}}, {"config": "nope"}, {}]
    assert [encoders.embed_family(m) for m in others] == [None] * 5
    assert encoders.embed_family({"config": {"model_type": "dinov2-with-registers"}}) == "image"
    assert encoders.runs("embed", CLIP) and encoders.runs("embed", DINO) and not encoders.runs("embed", others[0])
    assert encoders.runs("classify", NLI) and encoders.runs("classify", SENTIMENT)
    assert not encoders.runs("classify", {**SENTIMENT, "classify": {"labels": 1}})
    assert not encoders.runs("classify", {**SENTIMENT, "classify": {"labels": 3, "problem": "regression"}})
    assert not encoders.runs("classify", CLIP) and not encoders.runs("chat", CLIP)
    assert encoders.runs("embed", {"kind": "local"}) and encoders.runs("classify", {"kind": "local"})
    all_r = recipes.builtin_recipes()
    tf, pool, vis = all_r["transformers"][0], all_r["vllm-pooling"][0], all_r["vision"][0]
    assert encoders.is_server(tf, "embed") and encoders.is_server(tf, "classify")
    assert not encoders.is_server(tf, "chat") and not encoders.is_server(vis, "image-classify")
    assert not encoders.is_server(pool, "embed") and not encoders.is_server(None, "embed")
    assert encoders.offered(pool, "embed", others[0]) and not encoders.offered(tf, "embed", others[0])
    assert encoders.order(["transformers", "vision", "vllm-pooling"], all_r, "embed") == ["vision", "vllm-pooling",
                                                                                         "transformers"]
    assert encoders.order(["transformers", "vllm-pooling"], all_r, "rerank") == ["transformers", "vllm-pooling"]


def test_fy_tfm_003_the_catalog_offers_it_after_vllm_and_only_for_its_models(tfm):
    """FY-TFM-003: a text embedding model (Qwen3-Embedding) keeps vllm-pooling alone; CLIP, SigLIP 2 and DINOv2 are
    offered the transformers server alone (1.5.4: vLLM's pooling runner does not embed images with them), and so are an
    NLI model and a DistilBERT classifier (vLLM runs neither); a model's page and Download to… list the same."""
    owner, _, _ = tfm
    listed = {m["name"]: m["recipes"] for m in owner.get("/api/models").json()["models"]}
    assert listed["qwen3-embedding-0.6b"] == ["vllm-pooling"]
    for name in ("clip", "siglip2", "dinov2", "bge-m3-nli", "sentiment"):
        assert listed[name] == ["transformers"], name
    assert listed["qwen3-4b"] == ["vllm"]
    page = owner.get("/api/models/dinov2").json()
    assert [x["name"] for x in page["recipes"]] == ["transformers"]
    page = owner.get("/api/models/qwen3-embedding-0.6b").json()
    assert [x["name"] for x in page["recipes"]] == ["vllm-pooling"]


def test_fy_tfm_004_deploying_on_it(tfm):
    """FY-TFM-004: on the built-in recipe (its vision image is 1.4.4rc1, which has no embed or classify task) deploying
    or estimating answers 422 vision_image_too_old; on a copy naming a 1.5.4 image, CLIP deploys on the CPU with
    --task embed, its cache mount and served name, VISION_THREADS / VISION_MAX_IMAGES / VISION_MAX_PAIRS, and a text
    classifier on the A100's shared card #4 with the CUDA build and --task classify; a text embedder is refused on it
    (422 incompatible, saying vllm-pooling), and still deploys on vllm-pooling."""
    owner, cpu, gpu = tfm
    body = {"name": "clip", "model": "clip", "recipe": "transformers", "params": {"gpus": 0}, "machine_id": cpu}
    for path in ("/api/deployments", "/api/deployments/estimate"):
        r = owner.post(path, json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "vision_image_too_old", (path, r.text)
        msg = r.json()["detail"]["message"]
        assert "needs Forgeyard's vision image 1.5.4 or newer" in msg and "runs 1.4.4" in msg, msg
    copy = copy_on(owner, NEW_CPU, NEW_CUDA, "transformers-154")
    deploy(owner, "clip", "clip", copy, params={"gpus": 0, "max_images": 8}, machine_id=cpu)
    spec = replica_row("clip").spec
    assert spec["runtime"]["image"] == NEW_CPU
    args = spec["runtime"]["args"]
    assert args[:4] == ["--task", "embed", "--model", "${MODEL_PATH}"] and args[args.index("--served-name") + 1] == "clip"
    assert {k: spec["env"][k] for k in ("VISION_THREADS", "VISION_MAX_IMAGES", "VISION_MAX_PAIRS")} == {
        "VISION_THREADS": "4", "VISION_MAX_IMAGES": "8", "VISION_MAX_PAIRS": "256"}
    assert "VISION_API_KEY" not in spec["env"] and spec["resources"]["gpus"]["count"] == 0
    d = deploy(owner, "nli", "bge-m3-nli", copy, machine_id=gpu)
    spec = replica_row("nli").spec
    assert spec["runtime"]["image"] == NEW_CUDA and spec["runtime"]["args"][1] == "classify"
    assert spec["resources"]["gpus"]["count"] == 1 and spec["resources"]["gpus"]["share"] == "vram"
    assert d["deployment"]["route"]["api"] == "classify"
    r = owner.post("/api/deployments", json={"name": "q", "model": "qwen3-embedding-0.6b", "recipe": copy, "params": {"gpus": 0}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "incompatible"
    assert "text embedding model: deploy it on vLLM (vllm-pooling)" in r.json()["detail"]["message"]
    deploy(owner, "q", "qwen3-embedding-0.6b", "vllm-pooling", machine_id=gpu)


def test_fy_tfm_005_the_estimate_holds_16_bit_weights_in_float32_on_a_cpu():
    """FY-TFM-005: on a CPU the transformers recipe counts a 16-bit checkpoint's weights at 4 bytes a parameter (the
    server widens them: float16 matrix products are several times slower there) plus its 1.25 GiB; a float32
    checkpoint, a model without parameter counts (a local one) and any GPU count the files; the vision recipe never
    widens."""
    tf, vis = recipes.builtin_recipes()["transformers"][0], recipes.builtin_recipes()["vision"][0]
    cpu = estimate(NLI, tf, {"gpus": 0}, device="cpu", gpu_context=0)
    assert cpu.ram == 4 * 567_756_802 + 1280 * MIB
    assert cpu.parts[0].label == "Model weights in float32"
    clip = estimate(CLIP, tf, {"gpus": 0}, device="cpu", gpu_context=0)
    assert clip.ram == CLIP["size_bytes"] + 1280 * MIB and clip.parts[0].label == "Model files"
    gpu = estimate(NLI, tf, {"gpus": 1}, device="gpu", gpu_context=500 * MIB)
    assert gpu.per_gpu == NLI["size_bytes"] + GIB + 500 * MIB
    local = {"kind": "local", "size_bytes": 3 * GIB}
    assert estimate(local, tf, {"gpus": 0}, device="cpu", gpu_context=0).ram == 3 * GIB + 1280 * MIB
    assert estimate(NLI, vis, {"gpus": 0}, device="cpu", gpu_context=0).ram == NLI["size_bytes"] + 1280 * MIB


def test_fy_tfm_006_the_server_and_the_catalog_agree_on_image_embedders():
    """FY-TFM-006: the transformers server's own embed_family (images/vision/app.py, read from the file: it needs torch)
    gives the same answer as the catalog's for every kind of config, and its list of image-only models is the
    catalog's; its NLI rule matches textcls.is_nli for the labels the catalog keeps."""
    from forgeyard.models import textcls

    src = (Path(__file__).resolve().parent.parent / "images" / "vision" / "app.py").read_text()
    tree = ast.parse(src)
    funcs = {"embed_family", "zero_shot_classifier", "classifier_type", "nli_ids"}
    names = {"ZERO_SHOT_CLASSIFY", "IMAGE_ONLY_EMBED", "NLI_WORDS"}
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in funcs
            or isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names for t in n.targets)]
    assert len(keep) == 7
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "app.py", "exec"), ns)
    assert ns["IMAGE_ONLY_EMBED"] == encoders.IMAGE_ONLY and ns["ZERO_SHOT_CLASSIFY"] == vision.ZERO_SHOT_CLASSIFY
    configs = [{"architectures": ["CLIPModel"]}, {"model_type": "siglip"}, {"model_type": "siglip2", "architectures": []},
               {"architectures": ["Dinov2Model"]}, {"model_type": "dinov2_with_registers"},
               {"architectures": ["Dinov2ForImageClassification"], "model_type": "dinov2"},
               {"architectures": ["SiglipForImageClassification"], "model_type": "siglip"},
               {"architectures": ["XLMRobertaModel"], "model_type": "xlm-roberta"}, {"architectures": ["BlipModel"]}, {}]
    assert [ns["embed_family"](c) for c in configs] == [encoders.embed_family({"config": c}) for c in configs] == [
        "text+image", "text+image", "text+image", "image", "image", None, None, None, None, None]
    for labels in (["entailment", "not_entailment"], ["entailment", "neutral", "contradiction"], ["positive", "negative"],
                   ["CONTRADICTION", "NEUTRAL", "ENTAILMENT"]):
        assert (ns["nli_ids"](labels) is not None) == textcls.is_nli(labels), labels
