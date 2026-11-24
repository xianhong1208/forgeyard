"""The transformers server behind the catalog and the gateway (1.5.4, #141 and #144, PR 2): which engine is offered
which embedding and text classification models (vLLM's pooling runner only what it runs, the transformers server the
rest), what the inspector keeps (meta.embed, zero-shot, languages), the recommended entries, /v1/embeddings with
typed items (images inlined from data: URIs, base64 or URLs fetched through the media rules; never spilled to a
provider), /v1/classify with the zero-shot fields, usage, and Try it. The configs are the real ones
(tests/fixtures/models/kinds); the engine is a fake of Forgeyard's transformers server (FY-TGW-*)."""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from forgeyard.config import get_settings
from forgeyard.gateway import classify as gclassify, embeddings as gembed
from forgeyard.gateway.usage import recorder
from forgeyard.models import encoders, fetch, inspect as inspector, modalities, recipes, recommended as rec, textcls
from forgeyard.models.inspect import config_summary
from tests.cloud_helpers import FakeCloud, add_provider, cloud_key, deployment_id, model_id, set_tiers
from tests.gateway_helpers import FakeEngine, api_client, make_ready, new_key
from tests.model_helpers import FIX, FakeHF, Repo, add_catalog, safetensors_bytes
from tests.test_multimodal import PNG, MediaServer
from tests.test_scheduler import deploy, seed_catalog
from tests.test_transformers_recipe import FEATURES, NEW_CPU, NEW_CUDA, copy_on
from tests.workload_helpers import make_node

KINDS = FIX / "kinds"
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()
PNG_B64 = base64.b64encode(PNG).decode()


def cfg(name: str) -> dict:
    path = KINDS / f"{name}.config.json"
    return json.loads((path if path.exists() else FIX / f"{name}.config.json").read_text())


def meta_of(name: str, task: str, **over) -> dict:
    """A safetensors catalog entry as the inspector keeps it, from a real config.json."""
    config = cfg(name)
    m = {"kind": "hf", "repo": f"acme/{name}", "format": "safetensors", "revision": "c" * 40,
         "config": config_summary(config),
         "modalities": modalities.from_config(config, []), "license": "apache-2.0",
         "weights": {"bytes": 400_000_000, "params": {"F32": 100_000_000}},
         "files": [{"path": "model.safetensors", "size": 400_000_000, "sha256": "b" * 64}], "size_bytes": 400_000_000}
    if task == "classify":
        m["classify"] = textcls.summary(config)
        m["modalities"] = []
    if task == "embed":
        m["embed"] = encoders.embed_summary(config)
        if encoders.embed_family(m) is not None:
            m["modalities"] = []       # the inspector reads a CLIP-like model as a vision model (no chat media)
    m.update(over)
    return m


def served(meta: dict, task: str) -> list[str]:
    from forgeyard.api.models import serving

    return serving("safetensors", task, meta, recipes.builtin_recipes())


# ------------------------------------------------------------------ the catalog: which engine serves what
def test_fy_tgw_001_each_engine_is_offered_what_it_runs():
    """FY-TGW-001: on the real configs, vLLM's pooling runner keeps the text embedders (Qwen3-Embedding, BGE-M3) and
    the classifiers it runs (BERT, XLM-R) as their default, the transformers server listed after it for the classifiers;
    CLIP, SigLIP 2 and DINOv2 as embed models, NLI models (bge-m3- and roberta-zeroshot, DeBERTa) and DistilBERT go to
    the transformers server alone; a multimodal embedder vLLM would need chat messages for (Qwen3-VL-Embedding) is
    offered nothing, and refused at add with "planned for a later 1.5 patch"."""
    assert served(meta_of("qwen3-embedding-0.6b", "embed"), "embed") == ["vllm-pooling"]
    assert served(meta_of("bge-m3", "embed"), "embed") == ["vllm-pooling"]
    for name in ("clip-vit-b-32-laion2b", "siglip2-base-patch16-224", "dinov2-small"):
        assert served(meta_of(name, "embed"), "embed") == ["transformers"], name
    for name in ("bert-multilingual-sentiment", "xlm-roberta-language-detection", "toxic-bert"):
        assert served(meta_of(name, "classify"), "classify") == ["vllm-pooling", "transformers"], name
    for name in ("bge-m3-zeroshot-v2.0-c", "roberta-base-zeroshot-v2.0-c", "nli-deberta-v3-base",
                 "distilbert-multilingual-sentiment", "distilbert-sst-2"):
        assert served(meta_of(name, "classify"), "classify") == ["transformers"], name
    vl = meta_of("qwen3-vl-embedding-2b", "embed")
    assert vl["modalities"] and served(vl, "embed") == []
    all_r = recipes.builtin_recipes()
    why = encoders.add_refusal("embed", vl, "safetensors", all_r)
    assert "planned for a later 1.5 patch" in why and "transformers" in why
    assert encoders.add_refusal("embed", meta_of("clip-vit-b-32-laion2b", "embed"), "safetensors", all_r) is None
    assert encoders.add_refusal("classify", meta_of("nli-deberta-v3-base", "classify"), "safetensors", all_r) is None
    assert encoders.add_refusal("embed", {"kind": "local"}, "safetensors", all_r) is None
    assert encoders.add_refusal("chat", vl, "safetensors", all_r) is None
    pool = all_r["vllm-pooling"][0]
    assert "transformers server" in encoders.vllm_refusal("embed", meta_of("dinov2-small", "embed"))
    assert encoders.vllm_refusal("embed", {"kind": "local"}) is None
    assert encoders.is_vllm(pool, "classify") and not encoders.is_vllm(pool, "rerank")
    assert not encoders.is_vllm(all_r["vllm"][0], "chat")
    # Rerank on vllm-pooling is untouched: a reranker with a vision tower is not refused.
    assert encoders.offered(pool, "rerank", vl)
    # At the gateway, vLLM embeds texts only, whatever the entry (one from before these rules, deployed on it, takes
    # no images there); the transformers server what its model takes, with the deployment's max_images.
    clip = meta_of("clip-vit-b-32-laion2b", "embed")
    tf = all_r["transformers"][0]
    assert gembed.embeds(pool, {"max_images": 8}, clip) == gembed.Embeds(("text",), 0)
    assert gembed.embeds(tf, {}, clip) == gembed.Embeds(("text", "image"), 32)
    assert gembed.embeds(tf, {"max_images": 3}, meta_of("dinov2-small", "embed")) == gembed.Embeds(("image",), 3)
    # The spill estimate counts an image as so many tokens, not the size of its base64 text.
    from forgeyard.gateway import body as gbody, fit as gfit, media as gmedia
    obj = {"model": "m", "input": ["hi", {"image": "data:image/png;base64," + "A" * 300_000}]}
    parsed = gbody.Parsed(model="m", obj=obj)
    parsed.embed = gembed.check(obj)
    assert gfit.prompt_estimate(parsed) < gmedia.TOKENS_PER_ITEM + 100


RECOMMENDED_CONFIGS = {   # the real config of each recommended embed / classify entry
    "hf://Qwen/Qwen3-Embedding-0.6B": "qwen3-embedding-0.6b", "hf://Qwen/Qwen3-Embedding-4B": "qwen3-embedding-0.6b",
    "hf://BAAI/bge-m3": "bge-m3", "hf://nlptown/bert-base-multilingual-uncased-sentiment": "bert-multilingual-sentiment",
    "hf://papluca/xlm-roberta-base-language-detection": "xlm-roberta-language-detection",
    "hf://unitary/toxic-bert": "toxic-bert", "hf://MoritzLaurer/bge-m3-zeroshot-v2.0-c": "bge-m3-zeroshot-v2.0-c",
    "hf://MoritzLaurer/roberta-base-zeroshot-v2.0-c": "roberta-base-zeroshot-v2.0-c",
    "hf://google/siglip2-base-patch16-224": "siglip2-base-patch16-224",
    "hf://laion/CLIP-ViT-B-32-laion2B-s34B-b79K": "clip-vit-b-32-laion2b", "hf://facebook/dinov2-small": "dinov2-small",
}
# What each recommended embed / classify entry deployed with before 1.5.4 (the recipes the catalog listed, first =
# the default), and the five new entries.
BEFORE = {"qwen3-embedding-0.6b": ["vllm-pooling"], "qwen3-embedding-4b": ["vllm-pooling"], "bge-m3": ["vllm-pooling"],
          "bert-multilingual-sentiment": ["vllm-pooling", "transformers"],
          "xlm-roberta-language-detection": ["vllm-pooling", "transformers"],
          "toxic-bert": ["vllm-pooling", "transformers"]}
NEW = {"bge-m3-zeroshot-v2.0-c": ["transformers"], "roberta-base-zeroshot-v2.0-c": ["transformers"],
       "siglip2-base-embed": ["transformers"], "clip-vit-b-32-laion-embed": ["transformers"],
       "dinov2-small-embed": ["transformers"]}


def test_fy_tgw_002_no_default_engine_changes_for_the_recommended_models():
    """FY-TGW-002: every recommended model's first recipe (its default) is the recipe the list suggests, and the embed
    and classify entries list exactly the recipes they listed before 1.5.4 (vLLM first where it ran them); only the
    five new entries (two zero-shot classifiers, three image embedders) default to the transformers server. Other tasks
    are not filtered by these rules at all."""
    all_r = recipes.builtin_recipes()
    from forgeyard.api.models import serving

    seen = {}
    for e in rec.recommended().models:
        meta = meta_of(RECOMMENDED_CONFIGS[e.source], e.task) if e.task in ("embed", "classify") else {"kind": "hf"}
        got = serving(e.format, e.task, meta, all_r)
        assert got and got[0] == e.recipe, (e.name, got, e.recipe)
        if e.task in ("embed", "classify"):
            seen[e.name] = got
        else:
            assert got == serving(e.format, e.task, None, all_r), e.name   # unfiltered
    assert seen == {**BEFORE, **NEW}


# ------------------------------------------------------------------ the inspector
def _repo(name: str, sha: str, info_name: str | None = None) -> Repo:
    info = json.loads((KINDS / f"{info_name or name}.hf-info.json").read_text())
    st = safetensors_bytes({"w": {"dtype": "F32", "shape": [64], "data_offsets": [0, 256]}}, b"\0" * 256)
    r = Repo(info["id"], sha, {"config.json": (KINDS / f"{name}.config.json").read_bytes(), "model.safetensors": st,
                                "tokenizer.json": b'{"version": "1.0"}'})
    r.info.update(pipeline_tag=info["pipeline_tag"], library_name=info.get("library_name"), tags=info["tags"],
                  cardData=info["cardData"])
    return r


NAMES = ("clip-vit-b-32-laion2b", "siglip2-base-patch16-224", "dinov2-small", "bge-m3-zeroshot-v2.0-c",
         "roberta-base-zeroshot-v2.0-c", "distilbert-multilingual-sentiment", "qwen3-vl-embedding-2b", "bge-m3")


@pytest.fixture()
def hf(monkeypatch):
    server = FakeHF(*[_repo(n, f"{i + 1:x}" * 40) for i, n in enumerate(NAMES)])
    monkeypatch.setenv("HF_ENDPOINT", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    yield server
    server.close()
    get_settings.cache_clear()


def repo_id(name: str) -> str:
    return json.loads((KINDS / f"{name}.hf-info.json").read_text())["id"]


def look(server, name: str, task: str | None = None) -> dict:
    return inspector.inspect_hf(repo_id(name), None, None, endpoint=server.url, policy=fetch.Policy(allow_private=True),
                                task=task)


def test_fy_tgw_003_the_inspector_reads_what_an_embedder_takes_and_the_languages(hf):
    """FY-TGW-003: inspected as embed, CLIP keeps meta.embed {clip, text and image, 512-d, 77 tokens}, SigLIP 2 (whose
    config.json names almost nothing) {siglip, 768-d, 64 tokens}, DINOv2 {image-only, 384-d} (tagged
    image-feature-extraction: read as embed by itself), BGE-M3 {text, 1024-d}; an NLI model is read as a zero-shot
    classifier (no longer refused) with its entailment label; the card's languages are kept (multilingual, en, the
    DistilBERT list), none when the card names none."""
    clip = look(hf, "clip-vit-b-32-laion2b", "embed")
    assert clip["embed"] == {"family": "clip", "modalities": ["text", "image"], "dimensions": 512, "text_max_tokens": 77}
    assert "languages" not in clip
    sig = look(hf, "siglip2-base-patch16-224", "embed")["embed"]
    assert sig == {"family": "siglip", "modalities": ["text", "image"], "dimensions": 768, "text_max_tokens": 64}
    dino = look(hf, "dinov2-small")
    assert dino["task_hint"] == "embed" and dino["embed"] == {"family": "image-only", "modalities": ["image"],
                                                               "dimensions": 384}
    assert look(hf, "bge-m3", "embed")["embed"] == {"family": "text", "modalities": ["text"], "dimensions": 1024}
    assert "embed" not in look(hf, "clip-vit-b-32-laion2b")    # read as an image classifier: no embed block
    nli = look(hf, "bge-m3-zeroshot-v2.0-c")
    assert nli["task_hint"] == "classify" and nli["classify"]["zero_shot"] is True
    assert nli["classify"]["entailment"] == "entailment" and nli["languages"] == ["multilingual"]
    assert look(hf, "roberta-base-zeroshot-v2.0-c")["languages"] == ["en"]
    assert look(hf, "distilbert-multilingual-sentiment")["languages"][:3] == ["en", "ar", "de"]
    assert inspector.languages_of({"cardData": {"language": "zh"}}) == ["zh"]
    assert inspector.languages_of({"cardData": {"language": ["EN", "en", "x" * 40, 3, "zh_TW"]}}) == ["en", "zh-tw"]
    assert inspector.languages_of({"cardData": {}}) is None and inspector.languages_of({}) is None


def test_fy_tgw_004_adding_embedders_classifiers_and_one_repository_twice(owner, hf):
    """FY-TGW-004: through the API CLIP is added as an image classifier and, under another name, as an embedding model
    (one job per entry, Q5), the latter offered the transformers server alone, with meta.embed and the languages the
    recommended list gives it (its card names none); an NLI model is added (zero-shot, transformers only, its card's
    languages); a multimodal embedder is refused (422 no_engine, planned for later), and so is retasking a text
    classifier... as an embedder of nothing (no engine embeds it)."""
    r = owner.post("/api/models", json={"name": "clip-cls", "source": f"hf://{repo_id('clip-vit-b-32-laion2b')}",
                                        "task": "image-classify"})
    assert r.status_code == 201, r.text
    assert r.json()["model"]["recipes"] == ["vision"] and r.json()["model"]["embed"] is None
    r = owner.post("/api/models", json={"name": "clip-emb", "source": f"hf://{repo_id('clip-vit-b-32-laion2b')}",
                                        "task": "embed"})
    assert r.status_code == 201, r.text
    m = r.json()["model"]
    assert m["recipes"] == ["transformers"] and m["languages"] == ["en"]
    assert m["embed"] == {"family": "clip", "modalities": ["text", "image"], "dimensions": 512, "text_max_tokens": 77}
    nli = owner.post("/api/models", json={"name": "topics", "source": f"hf://{repo_id('bge-m3-zeroshot-v2.0-c')}"})
    assert nli.status_code == 201, nli.text
    n = nli.json()["model"]
    assert n["task"] == "classify" and n["recipes"] == ["transformers"] and n["classify"]["zero_shot"] is True
    assert n["languages"] == ["multilingual"]
    r = owner.post("/api/models", json={"name": "vl-emb", "source": f"hf://{repo_id('qwen3-vl-embedding-2b')}",
                                        "task": "embed"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "no_engine"
    assert "planned for a later 1.5 patch" in r.json()["detail"]["message"]
    bge = owner.post("/api/models", json={"name": "bge", "source": f"hf://{repo_id('bge-m3')}", "task": "embed"}).json()
    assert bge["model"]["recipes"] == ["vllm-pooling"] and bge["model"]["embed"]["modalities"] == ["text"]
    assert bge["model"]["languages"] is None
    dino = owner.post("/api/models", json={"name": "dino", "source": f"hf://{repo_id('dinov2-small')}"}).json()["model"]
    assert dino["task"] == "embed" and dino["recipes"] == ["transformers"]
    # The recommended list says which one is in the catalog by task too.
    listed = {e["name"]: e["in_catalog"] for e in owner.get("/api/models/recommended").json()["models"]}
    assert listed["clip-vit-b-32-laion-embed"] == "clip-emb" and listed["clip-vit-b-32-laion"] == "clip-cls"
    assert listed["bge-m3-zeroshot-v2.0-c"] == "topics"


# ------------------------------------------------------------------ deploying
def test_fy_tgw_005_vllm_refuses_at_deploy_what_it_does_not_run(owner):
    """FY-TGW-005: catalog entries added before these rules: a DINOv2 or a CLIP added as embed and an NLI or
    DistilBERT classifier deploy on vllm-pooling no more (422 incompatible, naming the transformers server); a text
    embedder and a BERT classifier still do."""
    seed_catalog()
    for name, conf, task in (("dino", "dinov2-small", "embed"), ("clip", "clip-vit-b-32-laion2b", "embed"),
                             ("nli", "bge-m3-zeroshot-v2.0", "classify"), ("db", "distilbert-sst-2", "classify"),
                             ("stars", "bert-multilingual-sentiment", "classify")):
        meta = meta_of(conf, task)
        meta.pop("embed", None)          # an entry from before 1.5.4: no meta.embed
        add_catalog(name, meta, task=task, fmt="safetensors", source=f"hf://acme/{conf}")
    make_node("a100", allowlist=[4], features=FEATURES)
    for name in ("dino", "clip", "nli", "db"):
        r = owner.post("/api/deployments", json={"name": name, "model": name, "recipe": "vllm-pooling"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "incompatible", (name, r.text)
        assert "transformers" in r.json()["detail"]["message"], name
    deploy(owner, "stars", "stars", "vllm-pooling")
    deploy(owner, "emb", "qwen3-embedding-0.6b", "vllm-pooling")


# ------------------------------------------------------------------ a fake transformers server
class FakeTransformers:
    """Forgeyard's transformers server as the gateway sees it (images/vision/app.py --task embed | classify): the key
    on every path, /v1/embeddings with texts and {"image": <data: URI or base64>} items (a URL is refused, as the real
    one does), usage with images and kilopixels; /classify with fixed labels or (zero-shot) the request's."""

    def __init__(self, secret: str, *, zero_shot: bool = False, image_only: bool = False):
        self.secret, self.zero_shot, self.image_only = secret, zero_shot, image_only
        self.seen: list[dict] = []
        me = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status: int, obj: dict) -> None:
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._json(200, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                time.sleep(0.03)       # the model's work: the replica's busy time is counted
                me.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if self.headers.get("Authorization") != f"Bearer {me.secret}":
                    return self._json(401, {"error": {"message": "key", "code": "unauthorized"}})
                if self.path == "/v1/embeddings":
                    return self._embed(body)
                if self.path == "/classify":
                    return self._classify(body)
                self._json(404, {"error": {"message": "no", "code": "not_found"}})

            def _embed(self, body):
                inp = body.get("input")
                items = [inp] if isinstance(inp, (str, dict)) else inp
                data, images = [], 0
                for i, item in enumerate(items):
                    if isinstance(item, dict):
                        v = item["image"]
                        if v.startswith(("http://", "https://")):
                            return self._json(400, {"error": {"message": "url", "code": "image_url_not_supported"}})
                        raw = base64.b64decode(v.split(",", 1)[-1])
                        assert raw[:8] == b"\x89PNG\r\n\x1a\n", raw[:8]
                        images += 1
                        vec = [0.0, 1.0]
                    else:
                        if me.image_only:
                            return self._json(400, {"error": {"message": "text", "code": "text_not_supported"}})
                        vec = [1.0, 0.0]
                    data.append({"object": "embedding", "index": i, "embedding": vec})
                tokens = 3 * (len(items) - images)
                self._json(200, {"object": "list", "model": body.get("model"), "data": data,
                                 "usage": {"prompt_tokens": tokens, "total_tokens": tokens, "images": images,
                                           "kilopixels": 7 * images, "truncated_inputs": 0}})

            def _classify(self, body):
                texts = [body["input"]] if isinstance(body["input"], str) else body["input"]
                labels = body.get("labels")
                if me.zero_shot and not labels:
                    return self._json(400, {"error": {"message": "labels", "code": "labels_required"}})
                names = labels or ["positive", "negative"]
                data = [{"index": i, "label": names[0], "probs": [1 / len(names)] * len(names),
                         "num_classes": len(names)} for i in range(len(texts))]
                n = 5 * len(texts) * len(names)
                out = {"id": "c", "object": "list", "created": 1, "model": body.get("model"), "data": data,
                       "usage": {"prompt_tokens": n, "total_tokens": n, "truncated_inputs": 0}}
                if labels:
                    out.update(labels=labels, multi_label=bool(body.get("multi_label")))
                self._json(200, out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self) -> list[dict]:
        return [s for s in self.seen if s["auth"] == f"Bearer {self.secret}"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def gw(owner, monkeypatch):
    """CLIP (texts and images, 2 images per request), DINOv2 (images only) and a zero-shot NLI model on a copy of the
    transformers recipe naming a 1.5.4 image, Qwen3-Embedding and a BERT classifier on vllm-pooling, each on a fake
    engine; an API key; a media server on 127.0.0.1 (MEDIA_ALLOW_PRIVATE_HOSTS on)."""
    monkeypatch.setenv("MEDIA_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    seed_catalog()
    for name, conf, task in (("clip", "clip-vit-b-32-laion2b", "embed"), ("dino", "dinov2-small", "embed"),
                             ("nli", "bge-m3-zeroshot-v2.0-c", "classify"),
                             ("stars", "bert-multilingual-sentiment", "classify")):
        add_catalog(name, meta_of(conf, task), task=task, fmt="safetensors", source=f"hf://acme/{conf}")
    cpu = make_node("cpu-a", features=FEATURES)
    gpu = make_node("a100", allowlist=[4], features=FEATURES)
    tf = copy_on(owner, NEW_CPU, NEW_CUDA, "transformers-154")
    engines = {"clip": FakeTransformers("s-clip"), "dino": FakeTransformers("s-dino", image_only=True),
               "nli": FakeTransformers("s-nli", zero_shot=True)}
    vllm = {"emb": FakeEngine("emb", "s-emb"), "stars": FakeEngine("stars", "s-stars")}
    deploy(owner, "clip", "clip", tf, params={"gpus": 0, "max_images": 2}, machine_id=cpu)
    deploy(owner, "dino", "dino", tf, params={"gpus": 0}, machine_id=cpu)
    deploy(owner, "nli", "nli", tf, params={"gpus": 0, "max_pairs": 6}, machine_id=cpu)
    deploy(owner, "emb", "qwen3-embedding-0.6b", "vllm-pooling")
    deploy(owner, "stars", "stars", "vllm-pooling")
    for n, e in engines.items():
        make_ready(n, [(cpu, e.port, e.secret)])
    for n, e in vllm.items():
        make_ready(n, [(gpu, e.port, e.secret)])
    key = new_key(owner, "laptop")["key"]
    media = MediaServer()
    yield SimpleNamespace(owner=owner, cpu=cpu, e=engines, v=vllm, key=key, api=api_client(owner, key), media=media)
    for e in [*engines.values(), *vllm.values()]:
        e.close()
    media.close()
    get_settings.cache_clear()


def err(r) -> dict:
    return r.json()["error"]


def usage_rows(owner) -> dict:
    recorder.flush()
    return {x["id"]: x for x in owner.get("/api/usage?group=model").json()["rows"]}


def test_fy_tgw_006_typed_items_reach_the_transformers_server(gw):
    """FY-TGW-006: plain OpenAI bodies pass as they came; typed items reach the engine as plain strings and
    {"image": …} items (a data: URI or bare base64 as sent), the answer in input order; usage counts the engine's
    images and kilopixels and the replica's busy milliseconds for the request with images, the text tokens for both."""
    r = gw.api.post("/v1/embeddings", json={"model": "clip", "input": ["a cat", "a dog"], "user": "u1"})
    assert r.status_code == 200, r.text
    assert gw.e["clip"].posts()[-1]["body"] == {"model": "clip", "input": ["a cat", "a dog"], "user": "u1"}
    r = gw.api.post("/v1/embeddings", json={"model": "clip", "encoding_format": "float",
                                            "input": [{"text": "two cats"}, {"image": PNG_URI}, "x", {"image": PNG_B64}]})
    assert r.status_code == 200, r.text
    assert [d["embedding"] for d in r.json()["data"]] == [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]]
    sent = gw.e["clip"].posts()[-1]
    assert sent["path"] == "/v1/embeddings" and sent["body"]["input"] == ["two cats", {"image": PNG_URI}, "x",
                                                                          {"image": PNG_B64}]
    assert sent["body"]["encoding_format"] == "float" and r.headers["X-Forgeyard-Target"].startswith("local:clip#")
    one = gw.api.post("/v1/embeddings", json={"model": "dino", "input": {"image": PNG_URI}})
    assert one.status_code == 200 and gw.e["dino"].posts()[-1]["body"]["input"] == [{"image": PNG_URI}]
    rows = usage_rows(gw.owner)
    assert rows["clip"]["requests"] == 2 and rows["clip"]["prompt_tokens"] == 6 + 6
    assert rows["clip"]["images"] == 2 and rows["clip"]["kilopixels"] == 14 and rows["clip"]["busy_ms"] >= 2 * 25
    assert rows["dino"]["images"] == 1 and rows["dino"]["kilopixels"] == 7
    # A text request to vLLM is unchanged: no image units, no busy time.
    assert gw.api.post("/v1/embeddings", json={"model": "emb", "input": [{"text": "hello"}]}).status_code == 200
    assert gw.v["emb"].posts("/v1/embeddings")[-1]["body"]["input"] == ["hello"]
    rows = usage_rows(gw.owner)
    assert rows["emb"]["images"] == 0 and rows["emb"]["busy_ms"] == 0


def test_fy_tgw_007_image_urls_are_fetched_by_the_gateway_under_the_media_rules(gw, monkeypatch):
    """FY-TGW-007: an http(s) image URL is fetched by the gateway (the egress guard, MEDIA_ALLOW_PRIVATE_HOSTS) and sent
    as a data: URI, so the engine never fetches; with private hosts off the URL is refused before anything is fetched
    (400 media_url_refused); a page that is not an image, a 404 and other schemes are refused, a file over
    MEDIA_FETCH_MAX_MB is 413 media_too_large. None of them reaches the engine."""
    r = gw.api.post("/v1/embeddings", json={"model": "clip", "input": ["cats", {"image": f"{gw.media.url}/img.png"}]})
    assert r.status_code == 200, r.text
    got = gw.e["clip"].posts()[-1]["body"]["input"][1]["image"]
    assert got.startswith("data:image/png;base64,") and base64.b64decode(got.split(",", 1)[1]) == PNG
    assert gw.media.hits == ["/img.png"]
    before = len(gw.e["clip"].posts())
    for url, status, code in [(f"{gw.media.url}/page.html", 400, "media_fetch_failed"),
                              (f"{gw.media.url}/missing", 400, "media_fetch_failed"),
                              (f"{gw.media.url}/to-metadata", 400, "media_url_refused"),
                              ("file:///etc/passwd", 400, "media_url_refused"),
                              ("ftp://example.com/x.png", 400, "media_url_refused"),
                              ("not base64 at all!", 400, "invalid_media_data"),
                              ("data:text/plain;base64,aGk=", 400, "invalid_media_data")]:
        r = gw.api.post("/v1/embeddings", json={"model": "clip", "input": [{"image": url}]})
        assert r.status_code == status and err(r)["code"] == code and err(r)["param"] == "input", (url, r.text)
    monkeypatch.setenv("MEDIA_FETCH_MAX_MB", "1")
    get_settings.cache_clear()
    r = gw.api.post("/v1/embeddings", json={"model": "clip", "input": [{"image": f"{gw.media.url}/big"}]})
    assert r.status_code == 413 and err(r)["code"] == "media_too_large"
    monkeypatch.setenv("MEDIA_ALLOW_PRIVATE_HOSTS", "false")
    get_settings.cache_clear()
    hits = len(gw.media.hits)
    r = gw.api.post("/v1/embeddings", json={"model": "clip", "input": [{"image": f"{gw.media.url}/img.png"}]})
    assert r.status_code == 400 and err(r)["code"] == "media_url_refused"
    assert "MEDIA_ALLOW_PRIVATE_HOSTS" in err(r)["message"] and len(gw.media.hits) == hits
    assert len(gw.e["clip"].posts()) == before


def test_fy_tgw_008_what_a_model_does_not_embed_is_refused_at_the_gateway(gw):
    """FY-TGW-008: images to a text embedder (vLLM) 400 images_not_supported; texts (typed or plain) to DINOv2 400
    text_not_supported; more images than the deployment's max_images 400 too_many_images; malformed items 400
    invalid_request naming input; none reaches an engine."""
    def emb(model, inp):
        return gw.api.post("/v1/embeddings", json={"model": model, "input": inp})

    r = emb("emb", ["hi", {"image": PNG_URI}])
    assert r.status_code == 400 and err(r)["code"] == "images_not_supported" and "CLIP" in err(r)["message"]
    for inp in (["a text"], "a text", [{"text": "t"}, {"image": PNG_URI}]):
        r = emb("dino", inp)
        assert r.status_code == 400 and err(r)["code"] == "text_not_supported", inp
    r = emb("clip", [{"image": PNG_URI}] * 3)
    assert r.status_code == 400 and err(r)["code"] == "too_many_images" and "at most 2" in err(r)["message"]
    assert emb("clip", [{"image": PNG_URI}] * 2).status_code == 200
    for inp, words in [([{"image": PNG_URI, "text": "x"}], "one key each"), ([{"img": "x"}], "an item is a text"),
                       ([{"text": "  "}], "an empty text"), ([{"image": ""}], "an image item is"),
                       ([{"image": 5}], "an image item is"), ([{"text": "a"}] * 2049, "at most 2048")]:
        r = emb("clip", inp)
        assert r.status_code == 400 and err(r)["code"] == "invalid_request" and words in err(r)["message"], (inp, r.text)
    r = gw.api.post("/v1/embeddings", json={"model": "clip", "input": [{"image": PNG_URI}],
                                            "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400 and "not both" in err(r)["message"]
    assert len(gw.e["dino"].posts()) == 0 and gw.v["emb"].posts("/v1/embeddings") == []
    assert len(gw.e["clip"].posts()) == 1


def test_fy_tgw_009_a_request_with_images_never_goes_to_a_provider(gw, monkeypatch):
    """FY-TGW-009: CLIP's route with a provider tier after it: with the replica unreachable a request with an image
    answers 502 and is not spilled (the provider never sees it), a text-only one (typed items rewritten as plain
    strings) spills to the provider as an OpenAI body; a provider-only route refuses images (images_not_supported)."""
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    cloud = FakeCloud("stub")
    try:
        p = add_provider(gw.owner, cloud, models=(("text-emb", "openai-embeddings"),))
        pm = model_id(p, "text-emb")
        set_tiers(gw.owner, "clip", [[("local", deployment_id(gw.owner, "clip"))], [("provider", pm)]])
        api = api_client(gw.owner, cloud_key(gw.owner, "on-key", "on"))
        r = api.post("/v1/embeddings", json={"model": "clip", "input": [{"image": PNG_URI}]})
        assert r.status_code == 200 and r.headers["X-Forgeyard-Target"].startswith("local:")
        make_ready("clip", [(gw.cpu, 1, "s-clip")])        # nothing listens there: unreachable
        r = api.post("/v1/embeddings", json={"model": "clip", "input": ["cats", {"image": PNG_URI}]})
        assert r.status_code == 502 and err(r)["code"] == "upstream_unreachable"
        assert cloud.seen == []
        r = api.post("/v1/embeddings", json={"model": "clip", "input": [{"text": "cats"}, "dogs"]})
        assert r.status_code == 200 and r.headers["X-Forgeyard-Target"].startswith("provider:"), r.text
        assert cloud.seen[-1]["body"]["input"] == ["cats", "dogs"]
        r = gw.owner.post("/api/routes", json={"served_name": "cloud-emb", "api": "openai-embeddings",
                                               "targets": [{"kind": "provider", "provider_model_id": pm}]})
        assert r.status_code == 201, r.text
        r = api.post("/v1/embeddings", json={"model": "cloud-emb", "input": [{"image": PNG_URI}]})
        assert r.status_code == 400 and err(r)["code"] == "images_not_supported"
        assert len(cloud.seen) == 1
    finally:
        cloud.close()


def test_fy_tgw_010_zero_shot_fields_on_classify(gw):
    """FY-TGW-010: labels, multi_label and hypothesis_template reach a zero-shot deployment (the answer's labels and
    multi_label come back); sent to a fixed-label model they are 400 labels_not_supported; a zero-shot model without
    them 400 labels_required; malformed ones 400 naming the field; texts x labels above max_pairs 400 too_many_pairs.
    Usage: the engine's prompt tokens (every pair run) and, for the transformers server, its busy time."""
    body = {"model": "nli", "input": ["我的發票金額開錯了"], "labels": ["帳務", "技術問題", "稱讚"],
            "multi_label": True, "hypothesis_template": "這段文字和{}有關。", "user": "dropped"}
    r = gw.api.post("/v1/classify", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["labels"] == ["帳務", "技術問題", "稱讚"] and r.json()["multi_label"] is True
    assert gw.e["nli"].posts()[-1]["body"] == {k: v for k, v in body.items() if k != "user"}
    assert gw.e["nli"].posts()[-1]["path"] == "/classify"
    r = gw.api.post("/v1/classify", json={"model": "stars", "input": "x", "labels": ["a", "b"]})
    assert r.status_code == 400 and err(r)["code"] == "labels_not_supported"
    r = gw.api.post("/v1/classify", json={"model": "nli", "input": "x"})
    assert r.status_code == 400 and err(r)["code"] == "labels_required"
    r = gw.api.post("/v1/classify", json={"model": "nli", "input": ["a", "b", "c"], "labels": ["x", "y", "z"]})
    assert r.status_code == 400 and err(r)["code"] == "too_many_pairs" and "at most 6" in err(r)["message"]
    assert gw.api.post("/v1/classify", json={"model": "nli", "input": ["a", "b"], "labels": ["x", "y", "z"]}).status_code == 200
    for extra, param, words in [
        ({"labels": "a, b"}, "labels", "non-empty list"), ({"labels": []}, "labels", "non-empty list"),
        ({"labels": ["a", 1]}, "labels", "non-empty list"), ({"labels": ["a", "A"]}, "labels", "twice"),
        ({"labels": [f"l{i}" for i in range(33)]}, "labels", "at most 32"), ({"labels": ["x" * 65]}, "labels", "1 to 64"),
        ({"labels": ["a\x07b"]}, "labels", "plain text"), ({"multi_label": True}, "multi_label", "goes with 'labels'"),
        ({"hypothesis_template": "About {}."}, "hypothesis_template", "goes with 'labels'"),
        ({"labels": ["a"], "multi_label": "yes"}, "multi_label", "true or false"),
        ({"labels": ["a"], "hypothesis_template": "no slot"}, "hypothesis_template", "one {}"),
        ({"labels": ["a"], "hypothesis_template": "{} and {}"}, "hypothesis_template", "one {}"),
    ]:
        r = gw.api.post("/v1/classify", json={"model": "nli", "input": "x", **extra})
        assert r.status_code == 400 and err(r)["param"] == param and words in err(r)["message"], (extra, r.text)
    assert len(gw.e["nli"].posts()) == 2 and gw.v["stars"].posts() == []
    assert gw.api.post("/v1/classify", json={"model": "stars", "input": "I love it"}).status_code == 200
    rows = usage_rows(gw.owner)
    assert rows["nli"]["requests"] >= 2 and rows["nli"]["prompt_tokens"] == 5 * 3 + 5 * 6
    assert rows["nli"]["busy_ms"] >= 2 * 25 and rows["stars"]["busy_ms"] == 0
    # The zero-shot flags of the route's deployments come from the catalog.
    from forgeyard.db import new_session
    from forgeyard.gateway import proxy
    db = new_session()
    try:
        groups = {n: proxy.load_route(db, n).tiers[0].locals[0] for n in ("nli", "stars", "clip", "dino", "emb")}
    finally:
        db.close()
    assert groups["nli"].classifies == gclassify.Classifies(zero_shot=True, max_pairs=6) and groups["nli"].busy_usage
    assert groups["stars"].classifies == gclassify.Classifies(zero_shot=False) and not groups["stars"].busy_usage
    assert groups["clip"].embeds == gembed.Embeds(("text", "image"), 2) and groups["dino"].embeds == gembed.Embeds(("image",), 32)
    assert groups["emb"].embeds == gembed.Embeds(("text",), 0)


def test_fy_tgw_011_try_it_sends_the_test_image_and_the_labels(gw):
    """FY-TGW-011: Try it on CLIP sends the text and the test image and sums up their cosine; on DINOv2 the test image
    alone; on the zero-shot model the text and the labels typed (or three default ones); each with the replica's key;
    the request it shows has the image left out; the deployment's model says what it embeds and that it is
    zero-shot."""
    deps = {d["name"]: d for d in gw.owner.get("/api/deployments").json()["deployments"]}
    assert deps["clip"]["model"]["embed"]["modalities"] == ["text", "image"]
    assert deps["nli"]["model"]["classify"]["zero_shot"] is True and "zero_shot" not in deps["stars"]["model"]["classify"]
    out = gw.owner.post(f"/api/deployments/{deps['clip']['id']}/try", json={"input": "two squares"}).json()
    assert out["ok"] and out["summary"].startswith("2 vectors (the text and the test image), 2 dimensions")
    assert "text-image cosine 0.000" in out["summary"]
    sent = gw.e["clip"].posts()[-1]["body"]["input"]
    assert sent[0] == "two squares" and sent[1]["image"].startswith("data:image/png;base64,iVBOR")
    assert out["request"]["input"][1] == {"image": "<the test image: 256x192 PNG, base64>"}
    out = gw.owner.post(f"/api/deployments/{deps['dino']['id']}/try", json={}).json()
    assert out["ok"] and len(gw.e["dino"].posts()[-1]["body"]["input"]) == 1
    out = gw.owner.post(f"/api/deployments/{deps['nli']['id']}/try", json={"input": "hi", "labels": ["a", " b "]}).json()
    assert out["ok"] and gw.e["nli"].posts()[-1]["body"]["labels"] == ["a", "b"] and out["summary"] == "a 0.500 (2 labels)"
    gw.owner.post(f"/api/deployments/{deps['nli']['id']}/try", json={})
    assert gw.e["nli"].posts()[-1]["body"]["labels"] == ["technology", "sports", "cooking"]
    out = gw.owner.post(f"/api/deployments/{deps['stars']['id']}/try", json={"labels": ["x"]}).json()
    assert "labels" not in gw.v["stars"].posts()[-1]["body"]


# ------------------------------------------------------------------ the recommended list
def test_fy_tgw_012_the_recommended_entries():
    """FY-TGW-012: the two zero-shot classifiers (Q7) and SigLIP 2, CLIP and DINOv2 as embedders are listed, each pinned
    to a commit, MIT or Apache-2.0, on the transformers server on a CPU, with their languages (DINOv2: none, it reads
    no text); a repository may be listed once per task, not twice for one; an entry of the transformers server under
    another licence is refused."""
    by = {e.name: e for e in rec.recommended().models}
    want = {"bge-m3-zeroshot-v2.0-c": ("classify", "mit", ["multilingual"]),
            "roberta-base-zeroshot-v2.0-c": ("classify", "mit", ["en"]),
            "siglip2-base-embed": ("embed", "apache-2.0", ["multilingual"]),
            "clip-vit-b-32-laion-embed": ("embed", "mit", ["en"]), "dinov2-small-embed": ("embed", "apache-2.0", [])}
    for name, (task, lic, langs) in want.items():
        e = by[name]
        assert (e.task, e.license, e.languages, e.recipe, e.device) == (task, lic, langs, "transformers", "cpu"), name
        assert e.revision and len(e.revision) == 40
    assert by["siglip2-base-embed"].source == by["siglip2-base"].source and by["siglip2-base"].task == "image-classify"
    raw = json.loads(json.dumps(rec.recommended().model_dump()))
    entry = next(e for e in raw["models"] if e["name"] == "clip-vit-b-32-laion-embed")
    with pytest.raises(rec.RecommendedError, match="listed twice for the same task"):
        rec.check({**raw, "models": raw["models"] + [{**entry, "name": "clip-again"}]})
    with pytest.raises(rec.RecommendedError, match="a model of the transformers server has a permissive licence"):
        rec.check({**raw, "models": [{**entry, "license": "openrail"}]})


def test_fy_tgw_013_deployments_carry_what_the_playground_shows(owner):
    """FY-TGW-013 (1.5.4, PR 3): a deployment's model names the languages its card names (embedding and classification
    models only: the Playground warns before Chinese texts go to an English-only one) next to what it embeds and its
    labels; another task's deployment has no `languages`; a card that names none gives null."""
    seed_catalog()
    for name, conf, task, langs in (("clip", "clip-vit-b-32-laion2b", "embed", ["en"]), ("dino", "dinov2-small", "embed", None),
                                    ("nli", "bge-m3-zeroshot-v2.0-c", "classify", ["multilingual"])):
        add_catalog(name, meta_of(conf, task, **({"languages": langs} if langs else {})), task=task, fmt="safetensors",
                    source=f"hf://acme/{conf}")
    cpu = make_node("cpu-a", features=FEATURES)
    tf = copy_on(owner, NEW_CPU, NEW_CUDA, "transformers-154")
    for name in ("clip", "dino", "nli"):
        deploy(owner, name, name, tf, params={"gpus": 0}, machine_id=cpu)
    deploy(owner, "emb", "qwen3-embedding-0.6b", "vllm-pooling")
    deps = {d["name"]: d["model"] for d in owner.get("/api/deployments").json()["deployments"]}
    assert deps["clip"]["languages"] == ["en"] and deps["clip"]["embed"]["modalities"] == ["text", "image"]
    assert deps["dino"]["languages"] is None and deps["dino"]["embed"]["modalities"] == ["image"]
    assert deps["nli"]["languages"] == ["multilingual"] and deps["nli"]["classify"]["zero_shot"] is True
    assert "languages" in deps["emb"]
    one = owner.get(f"/api/deployments/{owner.get('/api/deployments').json()['deployments'][0]['id']}").json()["deployment"]
    assert "languages" in one["model"]
