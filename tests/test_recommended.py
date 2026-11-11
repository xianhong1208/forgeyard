"""The recommended models file, the list on Add a model, and the suggestion an inspection comes with:
recipe, estimate and where it would fit (FY-REC-*)."""

import copy

import pytest
import yaml
from sqlalchemy import select

from forgeyard.config import get_settings
from forgeyard.db import CatalogModel, new_session
from forgeyard.models import recommended as rec
from tests.conftest import add_user, sign_in
from tests.model_helpers import FakeHF, Repo, gguf_repo, qwen4b_repo, tiny_repo
from tests.test_scheduler import MODELS
from tests.workload_helpers import make_node

PW = "quiet-meadow-4821"


@pytest.fixture()
def hf(monkeypatch):
    whisper = Repo("Systran/faster-whisper-small", "5" * 40,
                   {"model.bin": b"w" * 2048, "vocabulary.txt": b"a\nb\n", "config.json": b"{}"})
    whisper.info.update(pipeline_tag="automatic-speech-recognition", library_name="ctranslate2")
    server = FakeHF(qwen4b_repo(), gguf_repo(), tiny_repo(), whisper, tiny_repo("google/gemma-3-4b-it", gated=True))
    monkeypatch.setenv("HF_ENDPOINT", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    yield server
    server.close()


def raw() -> dict:
    return yaml.safe_load(rec.FILE.read_text())


def test_fy_rec_001_the_shipped_list(owner):
    """FY-REC-001: the file loads and covers what was asked for: Qwen3 chat in six sizes plus a CPU
    GGUF, Qwen3 embedding and reranker 0.6B/4B, gpt-oss, Gemma 3 and Llama marked gated, Qwen3-VL
    (1.3), Whisper small and large-v3-turbo, BGE-M3 and its reranker."""
    data = rec.load()
    assert [g.id for g in data.groups] == ["qwen3", "llm", "vision", "speech", "retrieval"]
    by = {e.name: e for e in data.models}
    for size in ("0.6b", "1.7b", "4b", "8b", "14b", "32b"):
        assert by[f"qwen3-{size}"].recipe == "vllm" and by[f"qwen3-{size}"].task == "chat"
    assert by["qwen3-1.7b-gguf"].device == "cpu" and by["qwen3-1.7b-gguf"].file.endswith(".gguf")
    for n in ("qwen3-embedding-0.6b", "qwen3-embedding-4b", "qwen3-reranker-0.6b", "qwen3-reranker-4b"):
        assert by[n].recipe == "vllm-pooling"
    assert "--hf-overrides" in by["qwen3-reranker-4b"].engine_args["vllm-pooling"]
    gated = {e.name for e in data.models if e.gated}
    assert gated == {"gemma-3-4b-it", "gemma-3-12b-it", "gemma-3-27b-it", "llama-3.2-3b-instruct", "llama-3.1-8b-instruct"}
    assert not by["gpt-oss-20b"].gated
    gemma4 = {"gemma-4-e2b-it", "gemma-4-e4b-it", "gemma-4-12b-it", "gemma-4-26b-a4b-it", "gemma-4-31b-it"}
    assert gemma4 <= set(by) and not any(by[n].gated for n in gemma4)
    assert all(by[n].recipe == "vllm" and by[n].source.startswith("hf://google/gemma-4-") for n in gemma4)
    assert {by["faster-whisper-small"].recipe, by["faster-whisper-large-v3-turbo"].recipe} == {"faster-whisper"}
    assert by["bge-m3"].revision == "refs/pr/130" and by["bge-reranker-v2-m3"].task == "rerank"


def _break(path, value):
    def apply(d):
        d = copy.deepcopy(d)
        target = d["models"][0]
        if value is KeyError:
            target.pop(path)
        else:
            target[path] = value
        return d
    return apply


BROKEN = [
    ("bad name", _break("name", "Qwen3 4B"), "catalog name"),
    ("unknown group", _break("group", "nope"), "no group"),
    ("recipe cannot serve it", _break("recipe", "faster-whisper"), "does not serve"),
    ("unknown recipe", _break("recipe", "sglang"), "no built-in recipe"),
    ("GGUF without its file", _break("format", "gguf"), "names its file"),
    ("a file on a safetensors entry", _break("file", "x.gguf"), "names its file"),
    ("no CPU variant", _break("device", "cpu"), "no CPU variant"),
    ("not a Hugging Face source", _break("source", "https://example.org/x.gguf"), "only hf://"),
    ("bad source", _break("source", "hf://../x"), "hf://"),
    ("bad revision", _break("revision", "main; rm -rf"), "revision"),
    ("engine args for an unknown recipe", _break("engine_args", {"nope": ["--x"]}), "unknown recipe"),
    ("multi-line engine arg", _break("engine_args", {"vllm": ["--x\n--y"]}), "one-line"),
    ("unknown field", _break("licence", "mit"), "Extra inputs"),
    ("a licence that is not allowed", _break("license", "cc-by-nc-4.0"), "not one the recommended list"),
    ("no licence", _break("license", KeyError), "license"),
    ("a projector on a safetensors entry", _break("mmproj", "mmproj-x.gguf"), "only a GGUF entry"),
    ("an unknown input", _break("inputs", ["smell"]), "inputs"),
    ("missing note", _break("note", KeyError), "note"),
    ("no size", _break("download_gib", 0), "download_gib"),
    ("bad task", _break("task", "vision"), "task"),
]


@pytest.mark.parametrize("label,mutate,needle", BROKEN, ids=[b[0] for b in BROKEN])
def test_fy_rec_002_a_broken_entry_is_refused(label, mutate, needle):
    """FY-REC-002: every rule of the file has a check that names the entry."""
    with pytest.raises(rec.RecommendedError) as e:
        rec.check(mutate(raw()))
    assert needle in str(e.value), str(e.value)


def test_fy_rec_003_duplicates_are_refused():
    """FY-REC-003: the same name or the same source twice, or two groups with one id."""
    d = raw()
    d["models"].append(dict(d["models"][0]))
    with pytest.raises(rec.RecommendedError, match="used twice"):
        rec.check(d)
    d = raw()
    d["models"].append(dict(d["models"][0], name="another-name"))
    with pytest.raises(rec.RecommendedError, match="listed twice"):
        rec.check(d)
    d = raw()
    d["groups"].append(dict(d["groups"][0]))
    with pytest.raises(rec.RecommendedError, match="same id"):
        rec.check(d)


def test_fy_rec_004_admins_get_the_list_with_whats_in_the_catalog(owner, hf):
    """FY-REC-004: only admins (who add models) get the list; an entry already added says under
    which name."""
    r = owner.get("/api/models/recommended")
    assert r.status_code == 200
    body = r.json()
    assert len(body["models"]) == len(rec.load().models) and body["groups"][0]["title"] == "Qwen3"
    assert all(m["in_catalog"] is None for m in body["models"])
    r = owner.post("/api/models", json={"name": "my-qwen", "source": "hf://Qwen/Qwen3-4B"})
    assert r.status_code == 201, r.text
    entry = next(m for m in owner.get("/api/models/recommended").json()["models"] if m["name"] == "qwen3-4b")
    assert entry["in_catalog"] == "my-qwen"
    add_user(owner, "olga", role="operator")
    add_user(owner, "ursula", role="user")
    for c in (sign_in(owner, "olga", PW), sign_in(owner, "ursula", PW)):
        assert c.get("/api/models/recommended").status_code == 403


def test_fy_rec_005_inspecting_suggests_a_recipe_and_where_it_fits(owner, hf):
    """FY-REC-005: an inspection comes with the recipe, the estimate with its defaults and the machines
    it would fit on now (the card on the allowlist), before anything is added."""
    r = owner.post("/api/models/inspect", json={"source": "hf://Qwen/Qwen3-4B"})
    s = r.json()["suggestion"]
    assert s["recipe"] == "vllm" and s["device"] == "gpu" and s["fits"] is False
    assert s["summary"] == "No machine has joined."
    make_node("a100", allowlist=[4], features=MODELS)
    s = owner.post("/api/models/inspect", json={"source": "hf://Qwen/Qwen3-4B"}).json()["suggestion"]
    assert s["fits"] is True and s["params"]["gpus"] == 1
    assert [(m["name"], m["fits"], m["gpus"]) for m in s["machines"]] == [("a100", True, [4])]
    assert 13 * 1024**3 < s["estimate"]["per_gpu"] < 16 * 1024**3  # 14.38 GiB with the vllm defaults
    # Nothing was added.
    db = new_session()
    try:
        assert db.scalar(select(CatalogModel)) is None
    finally:
        db.close()


def test_fy_rec_006_device_and_recipe_follow_the_hardware(owner, hf):
    """FY-REC-006: a GGUF model is suggested for the CPU when no GPU is free (and on request), the
    recipe asked for is used when it fits the model, and a model nothing serves says so."""
    make_node("a100", allowlist=[], features=MODELS)  # no GPU allowed for Forgeyard
    s = owner.post("/api/models/inspect", json={"source": "hf://ggml-org/Qwen3-1.7B-GGUF"}).json()["suggestion"]
    assert s["recipe"] == "llama-cpp" and s["device"] == "cpu" and s["devices"] == ["gpu", "cpu"]
    assert s["params"]["gpus"] == 0 and s["estimate"]["device"] == "cpu" and s["fits"] is True
    s = owner.post("/api/models/inspect", json={"source": "hf://ggml-org/Qwen3-1.7B-GGUF", "device": "gpu"}).json()["suggestion"]
    assert s["device"] == "gpu" and s["fits"] is False
    s = owner.post("/api/models/inspect", json={"source": "hf://acme/tiny-chat", "task": "embed",
                                                "recipe": "vllm-pooling"}).json()["suggestion"]
    assert s["recipe"] == "vllm-pooling" and s["recipes"] == ["vllm-pooling"]
    s = owner.post("/api/models/inspect", json={"source": "hf://acme/tiny-chat", "recipe": "llama-cpp"}).json()["suggestion"]
    assert s["recipe"] == "vllm"  # llama-cpp does not read safetensors: the first that fits instead
    s = owner.post("/api/models/inspect", json={"source": "hf://Systran/faster-whisper-small", "task": "chat"}).json()["suggestion"]
    assert s["recipe"] is None and s["error"] == "No recipe serves ctranslate2 chat models."


def test_fy_rec_007_adding_from_the_list_keeps_engine_args_and_pins(owner, hf):
    """FY-REC-007: what the page sends for a recommended entry (its name, title, task and engine
    arguments) is added pinned to the commit, like any other model; a gated one needs a token."""
    entry = next(e for e in rec.load().models if e.name == "qwen3-reranker-0.6b")
    body = {"name": "qwen3-4b", "title": "Qwen3 4B", "source": "hf://Qwen/Qwen3-4B", "task": "chat",
            "engine_args": {"vllm": ["--enable-prefix-caching"]}}
    r = owner.post("/api/models", json=body)
    assert r.status_code == 201, r.text
    m = r.json()["model"]
    assert m["revision"] == qwen4b_repo().sha and m["engine_args"] == {"vllm": ["--enable-prefix-caching"]}
    assert entry.engine_args["vllm-pooling"][0] == "--hf-overrides"
    r = owner.post("/api/models/inspect", json={"source": "hf://google/gemma-3-4b-it"})
    assert r.status_code == 422 and "gated" in r.json()["detail"]["message"]
    r = owner.post("/api/models/inspect", json={"source": "hf://google/gemma-3-4b-it", "token": FakeHF.TOKEN})
    assert r.status_code == 200 and r.json()["suggestion"]["recipe"] == "vllm"
