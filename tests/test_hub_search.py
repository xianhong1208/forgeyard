"""Searching Hugging Face through the server: what is asked, what comes back, the cache, the rate
limit, who may search, and what happens without internet (FY-HUB-*)."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from forgeyard.config import get_settings
from forgeyard.models import fetch, hub as hub_mod
from forgeyard.models.hub import Hub, make_query
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import free_port

PW = "quiet-meadow-4821"

QWEN = {"id": "Qwen/Qwen3-0.6B", "downloads": 20511631, "likes": 1888, "lastModified": "2025-07-26T03:46:27.000Z",
        "gated": False, "private": False, "pipeline_tag": "text-generation", "library_name": "transformers",
        "tags": ["transformers", "safetensors", "qwen3", "text-generation"],
        "safetensors": {"parameters": {"BF16": 751632384}, "total": 751632384}}
GGUF = {"id": "ggml-org/Qwen3-1.7B-GGUF", "downloads": 50000, "likes": 30, "lastModified": "2025-05-01T00:00:00.000Z",
        "gated": False, "pipeline_tag": None, "tags": ["gguf"],
        "gguf": {"total": 2031739904, "architecture": "qwen3", "chat_template": "{{ x }}" * 1000}}
GEMMA = {"id": "google/gemma-3-4b-it", "downloads": 1012306, "likes": 900, "gated": "manual",
         "pipeline_tag": "image-text-to-text", "tags": ["safetensors"], "safetensors": {"total": 4300079472,
                                                                                         "parameters": {"BF16": 4300079472}}}
EMB_A = {"id": "Qwen/Qwen3-Embedding-0.6B", "downloads": 9042916, "pipeline_tag": "feature-extraction",
         "tags": ["safetensors"], "safetensors": {"total": 595776512, "parameters": {"BF16": 595776512}}}
EMB_B = {"id": "BAAI/bge-m3", "downloads": 29334229, "pipeline_tag": "sentence-similarity", "tags": ["pytorch"]}
WHISPER = {"id": "Systran/faster-whisper-small", "downloads": 5792974, "pipeline_tag": "automatic-speech-recognition",
           "library_name": "ctranslate2", "tags": ["ctranslate2", "audio"]}
RERANK = {"id": "BAAI/bge-reranker-v2-m3", "downloads": 14786395, "pipeline_tag": "text-classification",
          "tags": ["safetensors"], "safetensors": {"total": 567755777, "parameters": {"F32": 567755777}}}
SENTIMENT = {"id": "cardiffnlp/twitter-roberta-base-sentiment", "downloads": 90000000, "pipeline_tag": "text-classification",
             "tags": ["safetensors"]}
QWEN_RR = {"id": "Qwen/Qwen3-ReRanker-0.6B", "downloads": 1334528, "pipeline_tag": "text-ranking", "tags": ["safetensors"]}
BAD = [{"id": "../etc/passwd"}, {"id": "a/b/c"}, {"modelId": "ok-org/ok-name", "downloads": 1}, "not a model"]


class FakeHub:
    """http://127.0.0.1:<port>/api/models: answers by pipeline_tag; records every query."""

    def __init__(self):
        self.queries: list[dict] = []
        self.mode = "ok"         # ok | error | junk | slow
        self.by_tag = {"text-generation": [QWEN, GEMMA], "feature-extraction": [EMB_A, QWEN],
                       "sentence-similarity": [EMB_B], "automatic-speech-recognition": [WHISPER], None: [QWEN, GGUF, GEMMA],
                       "text-ranking": [QWEN_RR], "text-classification": [SENTIMENT, RERANK]}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                q = parse_qs(parts.query)
                fake.queries.append({"path": parts.path, **q})
                if fake.mode == "slow":
                    time.sleep(1.5)
                if fake.mode == "error":
                    body, status = b'{"error": "boom"}', 500
                elif fake.mode == "junk":
                    body, status = b"<html>maintenance</html>", 200
                elif parts.path != "/api/models":
                    body, status = b"{}", 404
                else:
                    tag = (q.get("pipeline_tag") or [None])[0]
                    items = list(fake.by_tag.get(tag, []))
                    if q.get("filter") == ["gguf"]:
                        items = [m for m in items if "gguf" in m.get("tags", [])]
                    body, status = json.dumps(items + (BAD if fake.mode == "bad" else [])).encode(), 200
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake(monkeypatch):
    f = FakeHub()
    monkeypatch.setenv("HF_ENDPOINT", f.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    yield f
    f.close()


def search(c, **params):
    return c.get("/api/models/search", params=params)


def test_fy_hub_001_the_server_asks_hugging_face_and_summarises(owner, fake):
    """FY-HUB-001: one request to <hf_endpoint>/api/models with the search, the task's pipeline tag,
    the format filter, sorted by downloads, with the fields to show; the answer is cut to what the page
    needs (no chat templates), with format, task, size and gated worked out."""
    r = search(owner, q="qwen3", task="chat", format="safetensors", limit=5)
    assert r.status_code == 200, r.text
    [q] = fake.queries
    assert q["path"] == "/api/models" and q["search"] == ["qwen3"] and q["pipeline_tag"] == ["text-generation"]
    assert q["filter"] == ["safetensors"] and q["sort"] == ["downloads"] and q["limit"] == ["5"]
    assert {"downloads", "likes", "lastModified", "gated", "safetensors", "gguf"} <= set(q["expand[]"])
    body = r.json()
    assert body["cached"] is False and [x["id"] for x in body["results"]] == ["Qwen/Qwen3-0.6B", "google/gemma-3-4b-it"]
    qwen, gemma = body["results"]
    assert qwen == {"id": "Qwen/Qwen3-0.6B", "source": "hf://Qwen/Qwen3-0.6B", "downloads": 20511631, "likes": 1888,
                    "last_modified": "2025-07-26T03:46:27.000Z", "gated": False, "gated_mode": None, "private": False,
                    "pipeline_tag": "text-generation", "library": "transformers", "task": "chat",
                    "format": "safetensors", "params": 751632384, "weights_bytes": 751632384 * 2,
                    "in_catalog": None, "recipes": ["vllm"]}
    assert gemma["gated"] is True and gemma["gated_mode"] == "manual" and gemma["task"] == "chat"
    r = search(owner, q="qwen3", format="gguf")
    [g] = r.json()["results"]
    assert g["format"] == "gguf" and g["task"] == "chat" and g["params"] == 2031739904 and g["weights_bytes"] is None
    assert "chat_template" not in json.dumps(r.json()) and g["recipes"] == ["llama-cpp"]


def test_fy_hub_002_embeddings_search_both_tags_merged_by_downloads(owner, fake):
    """FY-HUB-002: embedders are tagged two ways: both are asked, the lists merged without duplicates,
    most downloaded first, cut to the limit."""
    body = search(owner, q="embed", task="embed", limit=2).json()
    assert sorted(q["pipeline_tag"][0] for q in fake.queries) == ["feature-extraction", "sentence-similarity"]
    assert [x["id"] for x in body["results"]] == ["BAAI/bge-m3", "Qwen/Qwen3-0.6B"]
    assert body["results"][0]["task"] == "embed" and body["results"][0]["format"] == "other"
    assert body["results"][0]["recipes"] == []
    body = search(owner, task="asr").json()
    assert body["results"][0]["format"] == "ctranslate2" and body["results"][0]["recipes"] == ["faster-whisper"]


def test_fy_hub_010_rerank_search_puts_rerankers_first(owner, fake):
    """FY-HUB-010: a rerank search also asks for text classification (where many rerankers are filed),
    so names with "rerank" (any case) come first, each part by downloads; other tasks keep plain
    download order."""
    body = search(owner, q="m3", task="rerank").json()
    assert sorted(q["pipeline_tag"][0] for q in fake.queries) == ["text-classification", "text-ranking"]
    assert [x["id"] for x in body["results"]] == ["BAAI/bge-reranker-v2-m3", "Qwen/Qwen3-ReRanker-0.6B",
                                                   "cardiffnlp/twitter-roberta-base-sentiment"]
    assert search(owner, q="m3", task="rerank", limit=2).json()["results"][1]["id"] == "Qwen/Qwen3-ReRanker-0.6B"
    fake.by_tag[None] = [SENTIMENT, RERANK]
    assert [x["id"] for x in search(owner, q="m3").json()["results"]] == [SENTIMENT["id"], RERANK["id"]]


def test_fy_hub_003_answers_are_cached_failures_are_not(owner, fake):
    """FY-HUB-003: the same search within the cache time asks Hugging Face once; another search, or
    after a failure, asks again."""
    search(owner, q="qwen3")
    assert search(owner, q="qwen3").json()["cached"] is True and len(fake.queries) == 1
    search(owner, q="  qwen3  ")  # the same after trimming
    assert len(fake.queries) == 1
    search(owner, q="qwen3", task="chat")
    assert len(fake.queries) == 2
    fake.mode = "error"
    assert search(owner, q="llama").status_code == 503
    fake.mode = "ok"
    assert search(owner, q="llama").status_code == 200 and len(fake.queries) == 4


def test_fy_hub_004_the_cache_expires():
    """FY-HUB-004: an entry is used until it expires, then Hugging Face is asked again."""
    now = [1000.0]
    calls = []
    h = Hub(clock=lambda: now[0], getter=lambda url, policy: calls.append(url) or json.dumps([QWEN]).encode())
    q = make_query("qwen3", None, None, 10)
    pol = fetch.Policy()
    assert h.search("https://hf.example", q, pol)[1] is False
    now[0] += hub_mod.CACHE_SECONDS - 1
    assert h.search("https://hf.example", q, pol)[1] is True and len(calls) == 1
    now[0] += 2
    assert h.search("https://hf.example", q, pol)[1] is False and len(calls) == 2


@pytest.mark.parametrize("mode", ["error", "junk", "down", "slow"])
def test_fy_hub_005_no_internet_is_a_clear_503(owner, fake, monkeypatch, mode):
    """FY-HUB-005: Hugging Face down, answering an error or junk, or too slow: 503 saying the
    recommended list still works, never a 500."""
    if mode == "down":
        monkeypatch.setenv("HF_ENDPOINT", f"http://127.0.0.1:{free_port()}")
        get_settings.cache_clear()
    elif mode == "slow":
        monkeypatch.setattr(hub_mod, "TIMEOUT", 0.3)
    fake.mode = "ok" if mode == "down" else mode
    r = search(owner, q="qwen3")
    assert r.status_code == 503, r.text
    d = r.json()["detail"]
    assert d["code"] == "hub_unreachable" and "Cannot reach Hugging Face" in d["message"]
    assert "recommended models still work" in d["message"]


def test_fy_hub_006_what_may_be_searched(owner, fake):
    """FY-HUB-006: the text, task, format and limit are checked before anything is asked; bad entries
    in an answer (odd ids) are dropped."""
    for params in ({"q": "x" * 101}, {"q": "qwen\x01"}, {"task": "vision"}, {"format": "onnx"}, {"limit": 0},
                   {"limit": 31}):
        r = search(owner, **params)
        assert r.status_code == 422, (params, r.text)
    assert fake.queries == []
    fake.mode = "bad"
    ids = [x["id"] for x in search(owner).json()["results"]]
    assert "ok-org/ok-name" in ids and not any(".." in i or i.count("/") != 1 for i in ids)


def test_fy_hub_007_admins_only_and_rate_limited(owner, fake):
    """FY-HUB-007: operators and users cannot search (adding models is for admins); an admin gets 429
    with Retry-After past the searches allowed per minute."""
    add_user(owner, "olga", role="operator")
    add_user(owner, "ursula", role="user")
    for c in (sign_in(owner, "olga", PW), sign_in(owner, "ursula", PW)):
        assert search(c, q="qwen3").status_code == 403
    from forgeyard.api.models import SEARCH_PER_MINUTE

    codes = [search(owner, q=f"q{i % 3}").status_code for i in range(SEARCH_PER_MINUTE + 1)]
    assert codes[:-1] == [200] * SEARCH_PER_MINUTE and codes[-1] == 429
    r = search(owner, q="q1")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_fy_hub_008_only_the_configured_endpoint_and_only_public_addresses(owner, fake, monkeypatch):
    """FY-HUB-008: the search goes to the configured Hugging Face endpoint through the guarded fetcher:
    a private address is refused unless private hosts are allowed (a LAN mirror)."""
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "false")
    get_settings.cache_clear()
    r = search(owner, q="qwen3")
    assert r.status_code == 503 and "plain http is refused" in r.json()["detail"]["message"]
    monkeypatch.setenv("HF_ENDPOINT", fake.url.replace("http://", "https://"))
    get_settings.cache_clear()
    r = search(owner, q="qwen3")
    assert r.status_code == 503 and "loopback address; refused" in r.json()["detail"]["message"]
    assert fake.queries == []
    urls = hub_mod.search_urls("https://huggingface.co/", make_query("a&filter=x", "rerank", None, 3))
    assert all(u.startswith("https://huggingface.co/api/models?") for u in urls) and len(urls) == 2
    assert "search=a%26filter%3Dx" in urls[0]


def test_fy_hub_009_results_say_whats_already_in_the_catalog(owner, fake):
    """FY-HUB-009: a result already in the catalog carries its catalog name."""
    from tests.model_helpers import add_catalog

    add_catalog("tiny-qwen", {"kind": "hf", "repo": "Qwen/Qwen3-0.6B"}, task="chat", fmt="safetensors",
                source="hf://Qwen/Qwen3-0.6B")
    res = {x["id"]: x["in_catalog"] for x in search(owner, q="qwen3").json()["results"]}
    assert res["Qwen/Qwen3-0.6B"] == "tiny-qwen" and res["google/gemma-3-4b-it"] is None
