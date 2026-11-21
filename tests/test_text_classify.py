"""Text classification (1.4.4, #144): sequence classifiers with two labels or more as a catalog kind (task classify),
served by vLLM's pooling runner and answered at /v1/classify. The configs and Hugging Face answers are the real ones,
saved in tests/fixtures/models/kinds; the engine is a fake vLLM (tests/gateway_helpers.py) (FY-TCLS-*)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forgeyard.gateway.usage import recorder
from forgeyard.models import fetch, inspect as inspector, recipes, recommended as rec, sources, textcls
from forgeyard.models.inspect import config_summary
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import FakeEngine, api_client, make_ready, new_key
from tests.model_helpers import FIX, FakeHF, Repo, safetensors_bytes
from tests.test_scheduler import MODELS, deploy, replica_row, seed_catalog
from tests.workload_helpers import make_node

KINDS = FIX / "kinds"
PW = "quiet-meadow-4821"
SENTIMENT = "nlptown/bert-base-multilingual-uncased-sentiment"
LANGUAGE = "papluca/xlm-roberta-base-language-detection"
TOXIC = "unitary/toxic-bert"
ZERO_SHOT = "MoritzLaurer/bge-m3-zeroshot-v2.0"
RERANKER = "BAAI/bge-reranker-v2-m3"
SST2 = "distilbert/distilbert-base-uncased-finetuned-sst-2-english"
FILES = {SENTIMENT: "bert-multilingual-sentiment", LANGUAGE: "xlm-roberta-language-detection", TOXIC: "toxic-bert",
         ZERO_SHOT: "bge-m3-zeroshot-v2.0", RERANKER: "bge-reranker-v2-m3", SST2: "distilbert-sst-2",
         "cross-encoder/nli-deberta-v3-base": "nli-deberta-v3-base"}


def config(name: str) -> dict:
    return json.loads((KINDS / f"{name}.config.json").read_text())


def repo(repo_id: str, sha: str) -> Repo:
    """The real config.json, pipeline tag, tags and licence, and a small safetensors file for weights."""
    name = FILES[repo_id]
    info = json.loads((KINDS / f"{name}.hf-info.json").read_text())
    st = safetensors_bytes({"w": {"dtype": "F32", "shape": [64], "data_offsets": [0, 256]}}, b"\0" * 256)
    r = Repo(repo_id, sha, {"config.json": (KINDS / f"{name}.config.json").read_bytes(), "model.safetensors": st,
                            "tokenizer.json": b'{"version": "1.0"}'})
    r.info.update(pipeline_tag=info["pipeline_tag"], library_name=info["library_name"], tags=info["tags"],
                  cardData=info["cardData"])
    return r


@pytest.fixture()
def hf(monkeypatch):
    from forgeyard.config import get_settings

    onnx = Repo("acme/sentiment-onnx", "f" * 40, {"config.json": (KINDS / "bert-multilingual-sentiment.config.json").read_bytes(),
                                                  "model.onnx": b"onnx" * 16})
    onnx.info.update(pipeline_tag="text-classification", tags=["onnx", "text-classification"], cardData={"license": "mit"})
    causal = Repo("acme/causal-cls", "e" * 40, {"config.json": (FIX / "qwen3-reranker-0.6b.config.json").read_bytes(),
                                                "model.safetensors": safetensors_bytes(
                                                    {"w": {"dtype": "F32", "shape": [4], "data_offsets": [0, 16]}}, b"\0" * 16)})
    causal.info.update(pipeline_tag="text-classification", tags=["text-classification"], cardData={"license": "mit"})
    server = FakeHF(*[repo(r, f"{i + 1:x}" * 40) for i, r in enumerate(FILES)], onnx, causal)
    monkeypatch.setenv("HF_ENDPOINT", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    yield server
    server.close()
    get_settings.cache_clear()


def inspect(server: FakeHF, repo_id: str, task: str | None = None) -> dict:
    return inspector.inspect_hf(repo_id, None, None, endpoint=server.url, policy=fetch.Policy(allow_private=True),
                                task=task)


# ------------------------------------------------------------------ what a text classifier is
def test_fy_tcls_001_the_rules_on_real_configs():
    """FY-TCLS-001: vLLM's rule and what it runs, on the real configs: a BERT sentiment model (5 labels), an XLM-R
    language detector (20) and a multi-label BERT toxicity model (6) are text classifiers, with their labels in index
    order and their problem type; a reranker (one output), a regression head, an NLI model (XLM-R or DeBERTa,
    whatever its labels are called), a DistilBERT classifier (vLLM does not run it) and a causal LM are not, each
    with its reason."""
    s = textcls.summary(config("bert-multilingual-sentiment"))
    assert s == {"architecture": "BertForSequenceClassification", "labels": 5,
                 "label_names": ["1 star", "2 stars", "3 stars", "4 stars", "5 stars"], "problem": None}
    assert textcls.refusal(s) is None
    lang = textcls.summary(config("xlm-roberta-language-detection"))
    assert lang["labels"] == 20 and lang["label_names"][:3] == ["ja", "nl", "ar"] and "zh" in lang["label_names"]
    assert lang["architecture"] == "XLMRobertaForSequenceClassification" and textcls.refusal(lang) is None
    tox = textcls.summary(config("toxic-bert"))
    assert tox["problem"] == "multi_label_classification" and tox["label_names"][0] == "toxic" and not textcls.refusal(tox)
    # Index order, not text order ("10" after "9"); names are cut to 64 characters.
    many = {"architectures": ["BertForSequenceClassification"], "id2label": {str(i): f"l{i}" for i in range(12)}}
    assert textcls.label_names(many)[9:] == ["l9", "l10", "l11"]
    assert textcls.label_names({"id2label": {"0": "x" * 100}}) == ["x" * 64] and textcls.label_names({}) == []

    assert "Add it as a rerank model" in textcls.refusal(textcls.summary(config("bge-reranker-v2-m3")))
    regress = textcls.summary({**many, "problem_type": "regression"})
    assert textcls.refusal(regress).startswith("This model predicts numbers")
    for name in ("bge-m3-zeroshot-v2.0", "nli-deberta-v3-base"):
        why = textcls.refusal(textcls.summary(config(name)))
        assert why.startswith("This is a zero-shot (NLI) model") and "1.5 (#144)" in why and "structured outputs" in why
    why = textcls.refusal(textcls.summary(config("distilbert-sst-2")))
    assert why.startswith("vLLM 0.32.0 cannot run DistilBertForSequenceClassification") and "1.5 (#144)" in why
    qwen = json.loads((FIX / "qwen3-reranker-0.6b.config.json").read_text())
    assert textcls.summary(qwen) is None and textcls.refusal(None).startswith("This is not a text classifier")
    assert textcls.summary("not a dict") is None  # type: ignore[arg-type]


def test_fy_tcls_002_the_architectures_follow_the_pinned_vllm():
    """FY-TCLS-002: the architectures accepted are vLLM 0.32.0's native sequence classifiers, the version the
    vllm-pooling recipe pins; when the recipe moves, check the list again (registry.py)."""
    r = recipes.builtin_recipes()["vllm-pooling"][0]
    assert r.version == textcls.VLLM_CHECKED_WITH == "0.32.0"
    assert all(f":v{textcls.VLLM_CHECKED_WITH}" in v.image for v in r.variants)
    assert "DebertaV2ForSequenceClassification" not in textcls.VLLM_ARCHITECTURES
    assert {"BertForSequenceClassification", "XLMRobertaForSequenceClassification",
            "ModernBertForSequenceClassification"} <= set(textcls.VLLM_ARCHITECTURES)


def test_fy_tcls_003_roberta_positions_hold_two_fewer_tokens():
    """FY-TCLS-003: RoBERTa and XLM-R count positions after the padding index, so 514 position embeddings hold 512
    tokens (and BGE-M3's 8194, 8192); BERT's 512 stay 512. A longer --max-model-len would overflow them."""
    assert config_summary(config("xlm-roberta-language-detection"))["max_context"] == 512
    assert config_summary(config("bge-reranker-v2-m3"))["max_context"] == 8192
    assert config_summary(config("bert-multilingual-sentiment"))["max_context"] == 512
    assert config_summary({"model_type": "xlm-roberta", "max_position_embeddings": 514})["max_context"] == 514
    assert config_summary({"model_type": "roberta", "max_position_embeddings": 514, "pad_token_id": True})["max_context"] == 514


# ------------------------------------------------------------------ adding one
def test_fy_tcls_004_inspecting_reads_the_kind_from_the_labels(hf):
    """FY-TCLS-004: tagged text-classification (or zero-shot-classification), a model with two labels or more is read
    as a text classifier, its labels kept; with one output it stays a reranker. Asked for as a text classifier, a
    reranker, a causal-LM reranker, an NLI model and a DistilBERT model are refused with their reasons."""
    out = inspect(hf, SENTIMENT)
    assert out["task_hint"] == "classify" and out["classify"]["labels"] == 5 and out["config"]["max_context"] == 512
    assert inspect(hf, LANGUAGE, "classify")["classify"]["label_names"][-1] == "vi"
    assert inspect(hf, RERANKER)["task_hint"] == "rerank" and "classify" not in inspect(hf, RERANKER)
    with pytest.raises(inspector.InspectError, match="Add it as a rerank model"):
        inspect(hf, RERANKER, "classify")
    with pytest.raises(inspector.InspectError, match="text classifier with 5 labels"):
        inspect(hf, SENTIMENT, "rerank")
    with pytest.raises(inspector.InspectError, match=r"zero-shot \(NLI\) model"):
        inspect(hf, ZERO_SHOT)
    with pytest.raises(inspector.InspectError, match="cannot run DistilBert"):
        inspect(hf, SST2, "classify")
    # Tagged text-classification but no classification head to read (a causal LM): no kind is guessed.
    assert inspect(hf, "acme/causal-cls")["task_hint"] is None
    assert inspect(hf, SENTIMENT, "embed")["task_hint"] == "classify"   # another task: no classify block, no refusal
    assert "classify" not in inspect(hf, SENTIMENT, "embed")


def test_fy_tcls_005_adding_and_retasking_through_the_api(owner, hf):
    """FY-TCLS-005: added from its source, a sentiment model becomes a "Text classification" model served by
    vllm-pooling, with its labels on the catalog entry (and no media); a reranker added as one is refused, and so is
    a GGUF or another repository without a config to read. A classifier cannot become a reranker, and a model added
    as something else cannot become a classifier (Forgeyard read no labels for it)."""
    r = owner.post("/api/models", json={"name": "sentiment", "source": f"hf://{SENTIMENT}"})
    assert r.status_code == 201, r.text
    m = r.json()["model"]
    assert m["task"] == "classify" and m["task_label"] == "Text classification" and m["recipes"] == ["vllm-pooling"]
    assert m["classify"] == {"architecture": "BertForSequenceClassification", "labels": 5,
                             "label_names": ["1 star", "2 stars", "3 stars", "4 stars", "5 stars"]}
    assert m["modalities"] == [] and m["summary"]["max_context"] == 512
    tox = owner.post("/api/models", json={"name": "toxic", "source": f"hf://{TOXIC}", "task": "classify"}).json()["model"]
    assert tox["classify"]["problem"] == "multi_label_classification"
    r = owner.post("/api/models", json={"name": "rr-as-cls", "source": f"hf://{RERANKER}", "task": "classify"})
    assert r.status_code == 422 and "Add it as a rerank model" in r.json()["detail"]["message"]
    rr = owner.post("/api/models", json={"name": "rr", "source": f"hf://{RERANKER}"}).json()["model"]
    assert rr["task"] == "rerank" and rr["classify"] is None

    r = owner.patch(f"/api/models/{m['id']}", json={"task": "rerank"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "not_a_text_classifier"
    assert "text classifier with 5 labels" in r.json()["detail"]["message"]
    r = owner.patch(f"/api/models/{rr['id']}", json={"task": "classify"})
    assert r.status_code == 422 and "Forgeyard read no labels" in r.json()["detail"]["message"]
    assert owner.patch(f"/api/models/{m['id']}", json={"task": "classify", "title": "Stars"}).status_code == 200
    page = owner.get("/api/models/sentiment").json()
    assert page["model"]["classify"]["labels"] == 5 and [x["name"] for x in page["recipes"]] == ["vllm-pooling"]
    # An ONNX export tagged text-classification: no safetensors config is read, so no labels: refused.
    r = owner.post("/api/models", json={"name": "onnx-cls", "source": "hf://acme/sentiment-onnx"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "not_a_text_classifier"
    assert r.json()["detail"]["message"].startswith("This is not a text classifier")


def test_fy_tcls_006_a_classifier_deploys_on_the_pooling_runner_with_its_own_context(owner, hf):
    """FY-TCLS-006: a text classifier deploys with vllm-pooling's runner (--runner pooling: vLLM picks the classify
    task by itself, no task flag), its route speaks the classify protocol, and a context the request does not name
    starts at the model's own maximum (512) instead of the recipe's 8192; a longer one named is refused as before."""
    owner.post("/api/models", json={"name": "lang", "source": f"hf://{LANGUAGE}"})
    make_node("a100", allowlist=[4], features=MODELS)
    est = owner.post("/api/deployments/estimate", json={"name": "x", "model": "lang", "recipe": "vllm-pooling"})
    assert est.status_code == 200, est.text
    r = owner.post("/api/deployments/estimate", json={"name": "x", "model": "lang", "recipe": "vllm-pooling",
                                                       "params": {"context": 8192}})
    assert r.status_code == 422 and "at most 512 tokens" in r.json()["detail"]["message"]
    d = deploy(owner, "lang", "lang", "vllm-pooling")["deployment"]
    assert d["params"]["context"] == 512 and d["route"]["api"] == "classify"
    args = replica_row("lang").spec["runtime"]["args"]
    assert args[args.index("--runner") + 1] == "pooling" and args[args.index("--max-model-len") + 1] == "512"
    assert not {"--convert", "--task", "--pooler-config"} & set(args)
    assert recipes.builtin_recipes()["vllm-pooling"][0].api_for("classify") == "classify"
    preview = owner.post("/api/models/inspect", json={"source": f"hf://{SENTIMENT}"}).json()
    assert preview["recipes"] == ["vllm-pooling"] and preview["suggestion"]["params"]["context"] == 512
    assert "error" not in preview["suggestion"]
    # The model's page says whether a copy fits each machine with the engine's defaults: the same context.
    page = owner.get("/api/models/lang").json()
    assert "at most 512" not in json.dumps(page), page


def test_fy_tcls_007_a_recipe_serves_classify_only_as_classify():
    """FY-TCLS-007: a custom recipe that names the classify task serves it with the classify protocol."""
    text = (recipes.BUILTIN_DIR / "vllm-pooling.yaml").read_text().replace("name: vllm-pooling", "name: my-pooling")
    assert recipes.parse_recipe(text).api["classify"] == "classify"
    with pytest.raises(recipes.RecipeError, match="classify is served as classify"):
        recipes.parse_recipe(text.replace("classify: classify}", "classify: openai-embeddings}"))


# ------------------------------------------------------------------ /v1/classify
@pytest.fixture()
def gw(owner):
    seed_catalog()
    from tests.model_helpers import add_catalog
    meta = {"kind": "hf", "repo": SENTIMENT, "revision": "d" * 40, "format": "safetensors",
            "config": config_summary(config("bert-multilingual-sentiment")),
            "classify": textcls.summary(config("bert-multilingual-sentiment")), "modalities": [],
            "weights": {"params": {"F32": 167360261}, "bytes": 669441044, "linear_params": 85000000,
                        "other_params": 82360261},
            "files": [{"path": "model.safetensors", "size": 669441044, "sha256": "a" * 64}], "size_bytes": 669441044}
    add_catalog("stars", meta, task="classify", fmt="safetensors", source=f"hf://{SENTIMENT}")
    gpu = make_node("a100", allowlist=[4], features=MODELS + ("replica_keys",))
    cpu = make_node("cpu-a", features=MODELS + ("replica_keys",))
    eng, chat_eng = FakeEngine("cls", "secret-c"), FakeEngine("chat", "secret-h")
    deploy(owner, "stars", "stars", "vllm-pooling", served_name="sentiment", wait=True)
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    make_ready("stars", [(gpu, eng.port, eng.secret)])
    make_ready("chat", [(cpu, chat_eng.port, chat_eng.secret)])
    key = new_key(owner, "laptop")["key"]
    yield SimpleNamespace(owner=owner, eng=eng, chat=chat_eng, api=api_client(owner, key))
    eng.close()
    chat_eng.close()


def err(r) -> dict:
    return r.json()["error"]


def test_fy_tcls_008_classify_goes_to_the_engines_own_path(gw):
    """FY-TCLS-008: /v1/classify reaches the replica's /classify (vLLM's, without /v1) with the internal key and the
    engine's model name; the body carries only model, input and the two optional fields (others dropped); the
    answer comes back in vLLM's shape, and its prompt tokens are counted."""
    r = gw.api.post("/v1/classify", json={"model": "sentiment", "input": ["I love it", "meh, broken"],
                                          "truncate_prompt_tokens": 128, "use_activation": True,
                                          "user": "x", "priority": 5, "cache_salt": "s"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["label"] for d in body["data"]] == ["positive", "negative"] and body["data"][0]["num_classes"] == 2
    assert r.headers["X-Forgeyard-Target"].startswith("local:stars#")
    [seen] = gw.eng.posts()
    assert seen["path"] == "/classify" and seen["auth"] == "Bearer secret-c"
    assert seen["body"] == {"model": "sentiment", "input": ["I love it", "meh, broken"],
                            "truncate_prompt_tokens": 128, "use_activation": True}
    r = gw.api.post("/v1/classify", json={"model": "sentiment", "input": "I love one text"})
    assert r.status_code == 200 and len(r.json()["data"]) == 1 and gw.eng.posts()[-1]["body"]["input"] == "I love one text"
    recorder.flush()
    rows = {x["id"]: x for x in gw.owner.get("/api/usage?group=model").json()["rows"]}
    assert rows["sentiment"]["requests"] == 2 and rows["sentiment"]["prompt_tokens"] == (5 + 4) + 6
    assert rows["sentiment"]["completion_tokens"] == 0


def test_fy_tcls_009_refusals_never_reach_the_engine(gw, monkeypatch):
    """FY-TCLS-009: unknown model 404; a key without the model 403; a chat model at /v1/classify 400 wrong_endpoint
    (with how a chat model classifies), the classifier at /v1/embeddings 400 naming /v1/classify; chat messages,
    token ids, an empty list or text, too many texts, a bad truncate_prompt_tokens or use_activation: 400 naming the
    field; a body over GATEWAY_MAX_BODY_MB: 413. None of them reaches an engine."""
    def classify(c=gw.api, **body):
        return c.post("/v1/classify", json={"model": "sentiment", **body})

    r = gw.api.post("/v1/classify", json={"model": "nope", "input": "x"})
    assert r.status_code == 404 and err(r)["code"] == "model_not_found"
    only = api_client(gw.owner, new_key(gw.owner, "chat-only", models=["chat"])["key"])
    r = classify(only, input="x")
    assert r.status_code == 403 and err(r)["code"] == "model_not_allowed"
    r = gw.api.post("/v1/classify", json={"model": "chat", "input": "x"})
    assert r.status_code == 400 and err(r)["code"] == "wrong_endpoint"
    assert "structured_outputs.choice" in err(r)["message"] and "/v1/chat/completions" in err(r)["message"]
    r = gw.api.post("/v1/embeddings", json={"model": "sentiment", "input": "x"})
    assert r.status_code == 400 and "served at /v1/classify, not /v1/embeddings" in err(r)["message"]
    assert "structured_outputs" not in err(r)["message"]
    for body, param, words in [
        ({"messages": [{"role": "user", "content": "x"}]}, "messages", "chat messages are not taken"),
        ({"input": [101, 2023]}, "input", "token ids are not taken"),
        ({"input": [[101, 2023]]}, "input", "a string or a non-empty list"),
        ({"input": []}, "input", "non-empty list"),
        ({}, "input", "the text to classify"),
        ({"input": ["fine", "  "]}, "input", "an empty text (input[1])"),
        ({"input": ""}, "input", "an empty text."),
        ({"input": ["x"] * 2049}, "input", "2049 texts; at most 2048"),
        ({"input": "x", "truncate_prompt_tokens": 0}, "truncate_prompt_tokens", "at least 1"),
        ({"input": "x", "truncate_prompt_tokens": -2}, "truncate_prompt_tokens", "-1 for the model's maximum"),
        ({"input": "x", "truncate_prompt_tokens": True}, "truncate_prompt_tokens", "a number of tokens"),
        ({"input": "x", "use_activation": "yes"}, "use_activation", "true or false"),
    ]:
        r = classify(**body)
        assert r.status_code == 400 and err(r)["param"] == param and words in err(r)["message"], (body, r.text)
    assert classify(input=["x"] * 2048).status_code == 200   # the most it takes
    assert classify(input="x", truncate_prompt_tokens=-1).status_code == 200
    monkeypatch.setenv("GATEWAY_MAX_BODY_MB", "1")
    from forgeyard.config import get_settings
    from forgeyard.gateway import proxy
    get_settings.cache_clear()
    proxy.forget_ceiling()
    try:
        r = classify(input=["word " * 1000] * 300)
        assert r.status_code == 413, r.text
    finally:
        get_settings.cache_clear()
        proxy.forget_ceiling()
    assert len(gw.eng.posts()) == 2 and gw.chat.posts() == []


def test_fy_tcls_010_local_only_no_cloud_target(gw):
    """FY-TCLS-010: text classification never spills to a cloud provider: providers serve chat and embeddings only,
    so a classify route cannot take one, and the gateway offers no provider for /v1/classify."""
    from forgeyard.gateway import proxy

    assert "classify" not in proxy.CLOUD_ENDPOINTS and "classify" not in proxy.CLOUD_APIS
    assert proxy.ENDPOINTS["classify"] == ("classify", "json") and proxy.endpoint_for("classify") == "/v1/classify"
    assert proxy.UPSTREAM_PATHS == {"classify": "/classify"}
    names = [m["id"] for m in gw.api.get("/v1/models").json()["data"]]
    assert "sentiment" in names


def test_fy_tcls_011_playground_try_it_and_the_deployment_page(gw):
    """FY-TCLS-011: the Playground classifies as the signed-in person (no key, counted as console); "try it" sends one
    text straight to the replica's /classify with its internal key and sums up the label; the deployment's model
    carries its labels for the Playground."""
    add_user(gw.owner, "ursula")
    u = sign_in(gw.owner, "ursula", PW)
    r = u.post("/api/playground/classify", json={"model": "sentiment", "input": ["I love it"]})
    assert r.status_code == 200 and r.json()["data"][0]["label"] == "positive"
    deps = {d["name"]: d for d in gw.owner.get("/api/deployments").json()["deployments"]}
    assert deps["stars"]["model"]["classify"]["label_names"][0] == "1 star" and "classify" not in deps["chat"]["model"]
    out = gw.owner.post(f"/api/deployments/{deps['stars']['id']}/try", json={"input": "I love this"}).json()
    assert out["ok"] and out["summary"] == "positive 0.900 (2 labels)" and out["endpoint"].endswith("/classify")
    assert gw.eng.posts()[-1]["path"] == "/classify" and gw.eng.posts()[-1]["auth"] == "Bearer secret-c"
    recorder.flush()
    rows = {x["id"]: x for x in gw.owner.get("/api/usage?group=source&everyone=true").json()["rows"]}
    assert rows["console"]["requests"] == 1


# ------------------------------------------------------------------ search and the recommended list
def test_fy_tcls_012_searching_for_text_classifiers(owner, monkeypatch):
    """FY-TCLS-012: a text classification search asks for text-classification, shows its results as text
    classifiers and puts rerankers (filed there too) last; a rerank search still shows them as rerankers first."""
    from tests.test_hub_search import RERANK, SENTIMENT as SENT, FakeHub
    from forgeyard.config import get_settings

    fake = FakeHub()
    monkeypatch.setenv("HF_ENDPOINT", fake.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    try:
        # The reranker is the most downloaded: a text classification search still puts it last.
        fake.by_tag["text-classification"] = [{**RERANK, "downloads": 10**9}, SENT]
        body = owner.get("/api/models/search", params={"q": "x", "task": "classify"}).json()
        assert [q["pipeline_tag"] for q in fake.queries] == [["text-classification"]]
        assert [(x["id"], x["task"]) for x in body["results"]] == [(SENT["id"], "classify"), (RERANK["id"], "classify")]
        assert body["results"][0]["recipes"] == ["vllm-pooling"]
        body = owner.get("/api/models/search", params={"q": "x", "task": "rerank"}).json()
        assert [(x["id"], x["task"]) for x in body["results"]][0] == (RERANK["id"], "rerank")
        assert owner.get("/api/models/search", params={"task": "nope"}).json()["detail"]["message"].endswith("or classify")
    finally:
        fake.close()
        get_settings.cache_clear()


def test_fy_tcls_013_recommended_text_classifiers():
    """FY-TCLS-013: the recommended list has text classifiers, each pinned to a commit (its labels belong to it),
    under a permissive licence, served by vllm-pooling on a GPU, of an architecture vLLM runs; an unpinned one or one
    under another licence is refused."""
    data = rec.recommended()
    cls = [e for e in data.models if e.task == "classify"]
    assert {e.source for e in cls} == {f"hf://{SENTIMENT}", f"hf://{LANGUAGE}", f"hf://{TOXIC}"}
    assert all(sources.REVISION_RE.match(e.revision or "") and e.license in ("apache-2.0", "mit")
               and e.recipe == "vllm-pooling" and e.device == "gpu" and e.group == "text-classification" for e in cls)
    raw = json.loads(json.dumps(data.model_dump()))
    entry = next(e for e in raw["models"] if e["task"] == "classify")
    for change, words in [({"revision": None}, "a text classification entry is pinned to a commit"),
                          ({"license": "openrail"}, "a text classifier has a permissive licence")]:
        bad = {**raw, "models": [{**entry, **change}]}
        with pytest.raises(rec.RecommendedError, match=words):
            rec.check(bad)


def test_fy_tcls_014_a_chat_model_classifies_with_structured_outputs(gw):
    """FY-TCLS-014: what the guide says a chat model does today: `structured_outputs.choice` (vLLM) and a
    `response_format` JSON schema with an enum reach the engine as they were sent."""
    choice = {"choice": ["billing", "shipping", "technical", "other"]}
    schema = {"type": "json_schema", "json_schema": {"name": "label", "schema": {
        "type": "object", "properties": {"label": {"enum": choice["choice"]}}, "required": ["label"]}}}
    for extra in ({"structured_outputs": choice}, {"response_format": schema}):
        r = gw.api.post("/v1/chat/completions", json={"model": "chat", "temperature": 0, **extra,
                                                      "messages": [{"role": "user", "content": "我的發票金額開錯了"}]})
        assert r.status_code == 200, r.text
        sent = gw.chat.posts("/v1/chat/completions")[-1]["body"]
        assert {k: sent[k] for k in extra} == extra
