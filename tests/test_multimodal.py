"""Images, audio and video in chat (1.3): the content parts the gateway takes and how it rebuilds them,
media URLs fetched by the gateway under the egress rules (never by the engine), which deployments a
request may go to (the model's media, the recipe's, the deployment's counts and body limit), no cloud
for media, the usage columns of migration 0012, recipe rendering (vLLM hardening, llama.cpp's
--mmproj), the inspector (modalities, projector files, licence), the vision term of the estimate and
the recommended list's licences (FY-MM-*)."""

from __future__ import annotations

import base64
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import text

from forgeyard.config import get_settings
from forgeyard.db import get_engine
from forgeyard.gateway import egress, media as gmedia, proxy
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.usage import UNITS, recorder
from forgeyard.migrate import alembic_config, upgrade
from forgeyard.models import gguf, modalities, recipes, recommended as rec, sources
from forgeyard.models.estimate import estimate
from tests.cloud_helpers import FakeCloud, add_provider, cloud_key, deployment_id, model_id, set_tiers
from tests.gateway_helpers import FakeEngine, api_client, make_ready, new_key
from forgeyard.db import Accelerator, CatalogModel, Deployment, new_session
from tests.model_helpers import FIX, QWEN4B_REV, FakeHF, Repo, add_catalog, safetensors_bytes
from tests.test_scheduler import MODELS, _st_meta, deploy, replica_row, seed_catalog
from tests.workload_helpers import make_node

KEYS = MODELS + ("replica_keys",)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()
WAV_B64 = base64.b64encode(b"RIFF\x24\x00\x00\x00WAVEfmt ").decode()
GIB = 1024**3


def gguf_header(meta: dict) -> bytes:
    """A GGUF file with only metadata (no tensors), like a projector's header."""
    def string(v: str) -> bytes:
        b = v.encode()
        return struct.pack("<Q", len(b)) + b
    out = b"GGUF" + struct.pack("<IQQ", 3, 0, len(meta))
    for k, v in meta.items():
        out += string(k)
        if isinstance(v, bool):
            out += struct.pack("<I?", 7, v)
        elif isinstance(v, int):
            out += struct.pack("<II", 4, v)
        else:
            out += struct.pack("<I", 8) + string(v)
    return out + b"\0" * (-len(out) % 32)


def vl_meta(**over) -> dict:
    meta = _st_meta("Qwen/Qwen3-VL-4B-Instruct", QWEN4B_REV, "qwen3-4b.config.json", 4022468096, 8044936192,
                    3633315840)
    meta.update(modalities=["image", "video"], license="apache-2.0", vision_tile_px=32, **over)
    return meta


def gguf_vl_meta(seeded_g: dict) -> dict:
    return {"kind": "hf", "repo": "Qwen/Qwen3-VL-2B-Instruct-GGUF", "ref": "main", "revision": "f" * 40,
            "format": "gguf", "file": "Qwen3VL-2B-Instruct-Q4_K_M.gguf", "gguf": seeded_g,
            "mmproj": {"path": "mmproj-Qwen3VL-2B-Instruct-F16.gguf", "size": 819394848, "projector": "qwen3vl_merger"},
            "modalities": ["image", "video"],
            "files": [{"path": "Qwen3VL-2B-Instruct-Q4_K_M.gguf", "size": 1107409952, "sha256": "d" * 64},
                      {"path": "mmproj-Qwen3VL-2B-Instruct-F16.gguf", "size": 819394848, "sha256": "e" * 64}],
            "size_bytes": 1926804800}


class MediaServer:
    """http://127.0.0.1:<port> serving test media: /img.png, /octet (a PNG as octet-stream), /page.html,
    /big (MEDIA_FETCH_MAX_MB + 1 MiB), /big-stream (the same without Content-Length), /missing (404), /to-metadata (a redirect to 169.254.169.254),
    /to/<url> (a redirect to <url>), /loop (redirects to itself), /clip.wav, /video.mp4."""

    def __init__(self, big_mb: int = 2):
        self.hits: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", ctype="application/octet-stream", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                server.hits.append(self.path)
                p = self.path
                if p == "/img.png":
                    return self._send(200, PNG, "image/png")
                if p == "/octet":
                    return self._send(200, PNG, "application/octet-stream")
                if p == "/page.html":
                    return self._send(200, b"<html></html>", "text/html")
                if p == "/big":
                    return self._send(200, b"\0" * (big_mb * 1024**2 + 1024), "image/png")
                if p == "/big-stream":       # no Content-Length: the size shows only while reading
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    try:
                        for _ in range(big_mb * 16 + 16):
                            self.wfile.write(b"\0" * 65536)
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                if p == "/clip.wav":
                    return self._send(200, b"RIFF....WAVE", "audio/wav")
                if p == "/video.mp4":
                    return self._send(200, b"\0\0\0\x18ftypmp42", "video/mp4")
                if p == "/to-metadata":
                    return self._send(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
                if p.startswith("/to/"):
                    return self._send(302, headers={"Location": p[4:]})
                if p == "/loop":
                    return self._send(302, headers={"Location": "/loop"})
                self._send(404, b"no")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def mm(owner, monkeypatch):
    """A vision deployment on vLLM (`vl`, images and video, video off), a GGUF vision deployment on
    llama.cpp with a projector and 1 image per request (`vlg`), a text-only GGUF one (`txt`), each on a
    fake engine; an API key; a media server on 127.0.0.1 (MEDIA_ALLOW_PRIVATE_HOSTS on)."""
    monkeypatch.setenv("MEDIA_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    seed_catalog()
    db = new_session()
    try:
        seeded_g = dict(db.query(CatalogModel).filter_by(name="qwen3-1.7b-gguf").one().meta["gguf"])
    finally:
        db.close()
    add_catalog("qwen3-vl-4b", vl_meta(), task="chat", fmt="safetensors", source="hf://Qwen/Qwen3-VL-4B-Instruct")
    add_catalog("qwen3-vl-2b-gguf", gguf_vl_meta(seeded_g), task="chat", fmt="gguf",
                source="hf://Qwen/Qwen3-VL-2B-Instruct-GGUF")
    cpu = make_node("cpu-a", features=KEYS)
    gpu = make_node("a100", allowlist=[4], features=KEYS)
    engines = {n: FakeEngine(n, f"secret-{n}") for n in ("vl", "vlg", "txt")}
    deploy(owner, "vl", "qwen3-vl-4b", "vllm", params={"share": "vram", "context": 4096}, wait=True)
    deploy(owner, "vlg", "qwen3-vl-2b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2, "images": 1}, wait=True)
    deploy(owner, "txt", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    make_ready("vl", [(gpu, engines["vl"].port, engines["vl"].secret)])
    for n in ("vlg", "txt"):
        make_ready(n, [(cpu, engines[n].port, engines[n].secret)])
    key = new_key(owner, "laptop")["key"]
    server = MediaServer()
    ns = SimpleNamespace(owner=owner, cpu=cpu, gpu=gpu, engines=engines, key=key, api=api_client(owner, key),
                         media=server)
    yield ns
    for e in engines.values():
        e.close()
    server.close()


def ask(c, model: str, *parts, path="/v1/chat/completions", **extra):
    content = [{"type": "text", "text": "What is in it?"}, *parts]
    return c.post(path, json={"model": model, "messages": [{"role": "user", "content": content}], **extra})


def image(url: str, **extra) -> dict:
    return {"type": "image_url", "image_url": {"url": url, **extra}}


def replica_spec(name: str) -> dict:
    """The spec replica 0 of a deployment runs (rendered for its machine)."""
    return replica_row(name).spec


def err(r) -> dict:
    body = r.json()
    assert set(body) == {"error"}, body
    return body["error"]


def sent(engine: FakeEngine) -> list:
    [seen] = engine.posts("/v1/chat/completions")
    return seen["body"]["messages"][0]["content"]


# ------------------------------------------------------------------ parts
def test_fy_mm_001_parts_are_rebuilt_in_one_shape():
    """FY-MM-001: image_url objects and bare strings, input_image, video_url, input_video's data,
    audio_url (data: URI) and input_audio (base64 or a data: URI) become image_url / video_url /
    input_audio with only the checked fields; text parts pass unchanged; a request with text only is
    left alone (None)."""
    obj = {"messages": [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [
            {"type": "text", "text": "a"},
            {"type": "image_url", "image_url": {"url": PNG_URI, "detail": "low", "extra": "dropped"}},
            {"type": "image_url", "image_url": PNG_URI},
            {"type": "input_image", "image_url": PNG_URI},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}},
            {"type": "input_video", "input_video": {"data": "data:video/mp4;base64,AAAA"}},
            {"type": "audio_url", "audio_url": {"url": "data:audio/mpeg;base64," + WAV_B64}},
            {"type": "input_audio", "input_audio": {"data": WAV_B64, "format": "wav", "url": "http://x/"}},
            {"type": "input_audio", "input_audio": {"data": "data:audio/flac;base64," + WAV_B64}},
        ]}]}
    m = gmedia.scan(obj)
    parts = obj["messages"][1]["content"]
    assert m.counts == {"image": 3, "audio": 3, "video": 2} and m.items == 8 and not m.fetch
    assert parts[0] == {"type": "text", "text": "a"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": PNG_URI, "detail": "low"}}
    assert parts[2] == parts[3] == {"type": "image_url", "image_url": {"url": PNG_URI}}
    assert parts[4] == parts[5] == {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}}
    assert parts[6] == {"type": "input_audio", "input_audio": {"data": WAV_B64, "format": "mp3"}}
    assert parts[7] == {"type": "input_audio", "input_audio": {"data": WAV_B64, "format": "wav"}}
    assert parts[8] == {"type": "input_audio", "input_audio": {"data": WAV_B64, "format": "flac"}}
    assert gmedia.scan({"messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}]}) is None
    assert gmedia.scan({"messages": [{"role": "user", "content": "x"}]}) is None
    http = {"messages": [{"role": "user", "content": [image("https://example.org/a.png")]}]}
    m = gmedia.scan(http)
    assert [(f.kind, f.url) for f in m.fetch] == [("image", "https://example.org/a.png")]


@pytest.mark.parametrize("part,code", [
    (image("file:///etc/passwd"), "media_url_refused"),
    (image("ftp://example.org/a.png"), "media_url_refused"),
    (image(base64.b64encode(PNG).decode()), "media_url_refused"),         # raw base64 is not a URL
    ({"type": "input_audio", "input_audio": {"data": "http://10.0.0.1/a.wav"}}, "media_url_refused"),
    ({"type": "video_url", "video_url": {"url": "gopher://x"}}, "media_url_refused"),
    (image("data:text/plain;base64,aGk="), "invalid_media_data"),
    (image("data:image/png,not-base64"), "invalid_media_data"),
    ({"type": "image_embeds", "image_embeds": "AAAA"}, "invalid_content_part"),
    ({"type": "mystery", "x": 1}, "invalid_content_part"),
    ({"type": "image_url"}, "invalid_content_part"),
    ({"type": "file", "file": {"file_id": "file-123"}}, "invalid_media_data"),
    ("just a string", "invalid_content_part"),
])
def test_fy_mm_002_other_schemes_and_parts_are_refused(part, code):
    """FY-MM-002: file://, other schemes, raw base64 in a URL field, a URL in input_audio.data, a data:
    URI of the wrong kind or not base64, *_embeds, unknown part types, a media part without its URL and
    a file without a data: URI: 400 with a specific code, before anything is sent."""
    with pytest.raises(GatewayError) as e:
        gmedia.scan({"messages": [{"role": "user", "content": [part]}]})
    assert (e.value.status, e.value.code) == (400, code)


# ------------------------------------------------------------------ the gateway
def test_fy_mm_003_an_image_reaches_a_vision_model_and_is_counted(mm):
    """FY-MM-003: a data: URI image goes to the vision deployment in the canonical shape; usage counts
    one media item, and GET /api/usage shows every 1.3 unit."""
    r = ask(mm.api, "vl", {"type": "input_image", "image_url": PNG_URI, "detail": "high"})
    assert r.status_code == 200, r.text
    assert sent(mm.engines["vl"])[1] == {"type": "image_url", "image_url": {"url": PNG_URI, "detail": "high"}}
    recorder.flush()
    total = mm.owner.get("/api/usage?everyone=true").json()["total"]
    assert total["media_items"] == 1 and total["requests"] == 1
    assert all(total[u] == 0 for u in UNITS if u != "media_items")
    # The playground goes the same way, counted as the console.
    r = mm.owner.post("/api/playground/chat/completions", json={
        "model": "vlg", "messages": [{"role": "user", "content": [image(PNG_URI)]}]})
    assert r.status_code == 200, r.text
    recorder.flush()
    by = {g["id"]: g for g in mm.owner.get("/api/usage?everyone=true&group=source").json()["rows"]}
    assert by["console"]["media_items"] == 1 and by["api"]["media_items"] == 1


def test_fy_mm_004_a_model_gets_only_the_media_it_takes(mm):
    """FY-MM-004: an image to a text-only GGUF (no projector) is 400 media_not_supported and never
    reaches it; video while the deployment's videos is 0, and audio to a model without audio, too;
    more images than its `images` is too_many_media_items."""
    r = ask(mm.api, "txt", image(PNG_URI))
    assert r.status_code == 400 and err(r)["code"] == "media_not_supported"
    assert "text only" in err(r)["message"] and not mm.engines["txt"].posts()
    r = ask(mm.api, "vl", {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}})
    assert err(r)["code"] == "media_not_supported" and "videos" in err(r)["message"]
    r = ask(mm.api, "vl", {"type": "input_audio", "input_audio": {"data": WAV_B64, "format": "wav"}})
    assert err(r)["code"] == "media_not_supported" and "audio" in err(r)["message"]
    r = ask(mm.api, "vlg", image(PNG_URI), image(PNG_URI))
    assert r.status_code == 400 and err(r)["code"] == "too_many_media_items" and "at most 1" in err(r)["message"]
    assert not mm.engines["vlg"].posts() and not mm.engines["vl"].posts()
    r = ask(mm.api, "vl", *[image(PNG_URI)] * 4)       # the default: 4
    assert r.status_code == 200
    assert err(ask(mm.api, "vl", *[image(PNG_URI)] * 5))["code"] == "too_many_media_items"


def test_fy_mm_005_video_when_enabled(mm):
    """FY-MM-005: a deployment with videos 1 takes one video; vLLM gets it in --limit-mm-per-prompt."""
    deploy(mm.owner, "vlv", "qwen3-vl-4b", "vllm", params={"share": "vram", "context": 4096, "videos": 1}, wait=True)
    e = FakeEngine("vlv", "secret-v")
    try:
        make_ready("vlv", [(mm.gpu, e.port, e.secret)])
        r = ask(mm.api, "vlv", {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}})
        assert r.status_code == 200, r.text
        args = replica_spec("vlv")["runtime"]["args"]
        assert args[args.index("--limit-mm-per-prompt") + 1] == '{"image":4,"audio":4,"video":1}'
    finally:
        e.close()


def test_fy_mm_006_file_parts_are_refused_unless_the_recipe_takes_them(mm):
    """FY-MM-006: a PDF `file` part to a built-in recipe is 400 file_input_not_supported; the gateway
    does not convert it."""
    r = ask(mm.api, "vl", {"type": "file", "file": {"file_data": "data:application/pdf;base64,JVBERi0=",
                                                    "filename": "a.pdf"}})
    assert r.status_code == 400 and err(r)["code"] == "file_input_not_supported"
    assert not mm.engines["vl"].posts()


def test_fy_mm_007_media_never_go_to_a_provider(mm, monkeypatch):
    """FY-MM-007: on a route with a local tier and a cloud tier, an image request is served locally,
    and when the local replica is down it is 503, not spilled; a cloud-only route answers 400
    media_local_only; text still spills."""
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    cloud = FakeCloud("stub")
    try:
        p = add_provider(mm.owner, cloud)
        pm = model_id(p, "gpt-mini")
        set_tiers(mm.owner, "vl", [[("local", deployment_id(mm.owner, "vl"))], [("provider", pm)]])
        r = mm.owner.post("/api/routes", json={"served_name": "cloudy-only", "api": "openai-chat",
                                               "targets": [{"kind": "provider", "provider_model_id": pm}]})
        assert r.status_code == 201, r.text
        api = api_client(mm.owner, cloud_key(mm.owner, "on-key", "on"))
        assert ask(api, "vl", image(PNG_URI)).headers["x-forgeyard-target"].startswith("local:")
        make_ready("vl", [(mm.gpu, 1, "secret-vl")])            # nothing listens: unreachable
        r = ask(api, "vl", image(PNG_URI))
        assert r.status_code == 502 and err(r)["code"] == "upstream_unreachable"
        r = ask(api, "cloudy-only", image(PNG_URI))
        assert r.status_code == 400 and err(r)["code"] == "media_local_only"
        assert cloud.seen == []
        r = api.post("/v1/chat/completions", json={"model": "vl", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and r.headers["x-forgeyard-target"].startswith("provider:")
    finally:
        cloud.close()


def test_fy_mm_008_the_body_limit_is_per_deployment(mm, monkeypatch):
    """FY-MM-008: GATEWAY_MAX_BODY_MB 1: a 2 MiB image request is taken by a deployment whose max_body_mb
    is 3 and refused (413) by one whose max_body_mb is 1, with its own limit in the message; the gateway
    reads up to the largest max_body_mb of any deployment (64 by default), so a 2 MiB request for an
    unknown model is read and answered 404; a recipe without max_body_mb keeps the global limit."""
    monkeypatch.setenv("GATEWAY_MAX_BODY_MB", "1")
    get_settings.cache_clear()
    big = "data:image/png;base64," + base64.b64encode(PNG + b"\0" * (1536 * 1024)).decode()
    deploy(mm.owner, "small", "qwen3-vl-2b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2, "max_body_mb": 1},
           wait=True)
    deploy(mm.owner, "large", "qwen3-vl-2b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2, "max_body_mb": 3},
           wait=True)
    e = FakeEngine("big", "secret-big")
    try:
        make_ready("small", [(mm.cpu, e.port, e.secret)])
        make_ready("large", [(mm.cpu, e.port, e.secret)])
        r = ask(mm.api, "small", image(big))
        assert r.status_code == 413 and err(r)["code"] == "request_too_large" and "at most 1 MiB" in err(r)["message"]
        r = ask(mm.api, "large", image(big))
        assert r.status_code == 200, r.text
        r = mm.api.post("/v1/embeddings", json={"model": "embed-none", "input": "x" * (2 * 1024**2)})
        assert r.status_code == 404
        assert proxy.body_ceiling() == 64 * 1024**2
        whisper, _ = recipes.builtin_recipes()["faster-whisper"]
        assert modalities.accepts(whisper, {}, "ctranslate2", {}).max_body == 1024**2
    finally:
        e.close()


# ------------------------------------------------------------------ fetching media URLs
def test_fy_mm_009_the_gateway_fetches_a_url_and_sends_data(mm):
    """FY-MM-009: an http image URL is fetched by the gateway (once) and the engine gets a data: URI,
    never the URL; an octet-stream PNG is recognised; an audio_url becomes base64 input_audio."""
    r = ask(mm.api, "vl", image(f"{mm.media.url}/img.png"), image(f"{mm.media.url}/octet"))
    assert r.status_code == 200, r.text
    parts = sent(mm.engines["vl"])
    assert parts[1]["image_url"]["url"] == PNG_URI and parts[2]["image_url"]["url"] == PNG_URI
    assert mm.media.url not in json.dumps(parts)
    assert sorted(mm.media.hits) == ["/img.png", "/octet"]          # fetched at the same time
    vlv = modalities.Accepts(limits=(("image", 4), ("audio", 4)), files=False, max_body=64 * 1024**2)
    assert vlv.kinds == ["image", "audio"]
    obj = {"messages": [{"role": "user", "content": [{"type": "audio_url", "audio_url": {"url": f"{mm.media.url}/clip.wav"}}]}]}
    m = gmedia.scan(obj)
    import asyncio
    asyncio.run(gmedia.fetch_all(m))
    assert obj["messages"][0]["content"][0] == {"type": "input_audio", "input_audio": {
        "data": base64.b64encode(b"RIFF....WAVE").decode(), "format": "wav"}}


def test_fy_mm_010_private_and_metadata_addresses_are_refused(mm, monkeypatch):
    """FY-MM-010: with MEDIA_ALLOW_PRIVATE_HOSTS off, 127.0.0.1 and a name that resolves to a private
    address are refused before connecting; 169.254.169.254 is refused even with it on, also as the
    target of a redirect; a public host that redirects to 127.0.0.1 is refused at the redirect."""
    r = ask(mm.api, "vl", image(f"{mm.media.url}/to-metadata"))
    assert r.status_code == 400 and err(r)["code"] == "media_url_refused" and "169.254.169.254" in err(r)["message"]
    assert ask(mm.api, "vl", image("http://169.254.169.254/latest/")).json()["error"]["code"] == "media_url_refused"
    monkeypatch.setenv("MEDIA_ALLOW_PRIVATE_HOSTS", "false")
    get_settings.cache_clear()
    before = len(mm.media.hits)
    r = ask(mm.api, "vl", image(f"{mm.media.url}/img.png"))
    assert r.status_code == 400 and err(r)["code"] == "media_url_refused"
    assert "MEDIA_ALLOW_PRIVATE_HOSTS" in err(r)["message"] and len(mm.media.hits) == before

    async def fake_resolver(host, port):
        return {"intranet.example": ["10.1.2.3"], "pub.example": ["93.184.216.34"]}[host]
    monkeypatch.setattr(egress, "resolver", fake_resolver)
    egress._cache.clear()
    r = ask(mm.api, "vl", image("https://intranet.example/a.png"))
    assert err(r)["code"] == "media_url_refused" and "10.1.2.3" in err(r)["message"]
    # pub.example is "public" and is served by the media server, which redirects to 127.0.0.1.
    original = gmedia._target

    async def target(url):
        if url.startswith("http://pub.example/"):
            return f"{mm.media.url}{url[len('http://pub.example'):]}", {}, {}
        return await original(url)
    monkeypatch.setattr(gmedia, "_target", target)
    r = ask(mm.api, "vl", image(f"http://pub.example/to/{mm.media.url}/img.png"))
    assert r.status_code == 400 and err(r)["code"] == "media_url_refused" and "127.0.0.1" in err(r)["message"]
    assert mm.media.hits[-1].startswith("/to/") and not mm.engines["vl"].posts()


def test_fy_mm_011_fetch_failures(mm, monkeypatch):
    """FY-MM-011: a 404, an HTML page for an image, a redirect loop (stopped after 3 redirects) and a file
    over MEDIA_FETCH_MAX_MB (declared, or found while reading) are refused with media_fetch_failed (400) or media_too_large (413); credentials in the URL too."""
    monkeypatch.setenv("MEDIA_FETCH_MAX_MB", "1")
    get_settings.cache_clear()
    for path, status, code in (("/missing", 400, "media_fetch_failed"), ("/page.html", 400, "media_fetch_failed"),
                               ("/loop", 400, "media_fetch_failed"), ("/big", 413, "media_too_large"),
                               ("/big-stream", 413, "media_too_large")):
        r = ask(mm.api, "vl", image(f"{mm.media.url}{path}"))
        assert (r.status_code, err(r)["code"]) == (status, code), (path, r.text)
    assert "text/html" in err(ask(mm.api, "vl", image(f"{mm.media.url}/page.html")))["message"]
    before = mm.media.hits.count("/loop")
    assert "more than 3 redirects" in err(ask(mm.api, "vl", image(f"{mm.media.url}/loop")))["message"]
    assert mm.media.hits.count("/loop") - before == 4          # the URL and 3 redirects, then it stops
    r = ask(mm.api, "vl", image(f"http://user:pw@127.0.0.1:{mm.media.port}/img.png"))
    assert err(r)["code"] == "media_url_refused"
    r = ask(mm.api, "vl", {"type": "video_url", "video_url": {"url": f"{mm.media.url}/img.png"}})
    assert err(r)["code"] == "media_not_supported"      # checked before anything is fetched
    assert not mm.engines["vl"].posts()


def test_fy_mm_012_egress_allowed_hosts_limits_media_urls(mm, monkeypatch):
    """FY-MM-012: EGRESS_ALLOWED_HOSTS applies to media URLs too."""
    monkeypatch.setenv("EGRESS_ALLOWED_HOSTS", "images.example.org")
    get_settings.cache_clear()
    r = ask(mm.api, "vl", image(f"{mm.media.url}/img.png"))
    assert err(r)["code"] == "media_url_refused" and "EGRESS_ALLOWED_HOSTS" in err(r)["message"]


def test_fy_mm_013_the_token_estimate_counts_items_not_base64(mm):
    """FY-MM-013: for the spill rules an image counts TOKENS_PER_ITEM tokens, not its base64 size."""
    big = "data:image/png;base64," + "A" * 300_000
    parsed = proxy.parse_body(json.dumps({"model": "vl", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "hi"}, image(big)]}], "max_tokens": 10}).encode(), "application/json", "json")
    proxy.scan_media(parsed, "chat/completions")
    assert proxy.prompt_estimate(parsed) < gmedia.TOKENS_PER_ITEM + 200


# ------------------------------------------------------------------ recipes and rendering
def test_fy_mm_014_vllm_is_hardened_and_old_deployments_render(mm):
    """FY-MM-014: vLLM gets --limit-mm-per-prompt from images and videos, --allowed-media-domains with a
    name that matches nothing, --enable-prompt-tokens-details and VLLM_MEDIA_URL_ALLOW_REDIRECTS=0; so
    does vllm-pooling (without the limits); a deployment stored without the new parameters renders the
    recipe's defaults; llama.cpp gets --mmproj only with a projector."""
    spec = replica_spec("vl")
    args = spec["runtime"]["args"]
    assert args[args.index("--limit-mm-per-prompt") + 1] == '{"image":4,"audio":4,"video":0}'
    assert args[args.index("--allowed-media-domains") + 1] == "forgeyard.invalid"
    assert "--enable-prompt-tokens-details" in args and spec["env"]["VLLM_MEDIA_URL_ALLOW_REDIRECTS"] == "0"
    g = replica_spec("vlg")["runtime"]["args"]
    assert g[g.index("--mmproj") + 1] == "${MODEL_PATH}/mmproj-Qwen3VL-2B-Instruct-F16.gguf"
    assert g[g.index("--image-max-tokens") + 1] == "1024"
    assert float(g[g.index("--video-fps") + 1]) == 1.0          # llama-server's own default (4) overran 4,096 tokens
    assert "--mmproj" not in replica_spec("txt")["runtime"]["args"]
    assert "--image-max-tokens" not in replica_spec("txt")["runtime"]["args"]
    pooling, _ = recipes.builtin_recipes()["vllm-pooling"]
    assert "--allowed-media-domains" in pooling.args and pooling.env["VLLM_MEDIA_URL_ALLOW_REDIRECTS"] == "0"
    # A deployment from before 1.3: its params lack images/videos/max_body_mb.
    from forgeyard.models import catalog, render
    db = new_session()
    try:
        d = db.query(Deployment).filter_by(name="vl").one()
        d.params = {k: v for k, v in d.params.items() if k not in ("images", "videos", "max_body_mb")}
        ctx = catalog.context_for(db, d)
        variant = ctx.recipe.variants[0]
        cards = [a for a in db.query(Accelerator).all() if a.index == 4 and a.vendor == "nvidia"][:1]
        out = render.render(ctx, variant, cards, "vl-0", gpu_context=400 * 1024**2)
        a = out["runtime"]["args"]
        assert a[a.index("--limit-mm-per-prompt") + 1] == '{"image":4,"audio":4,"video":0}'
    finally:
        db.close()


def test_fy_mm_015_recipe_fields_and_advanced_parameters(owner):
    """FY-MM-015: a recipe's media block and if_model are validated; advanced parameters are marked in
    /api/models' recipes; a recipe without a media block passes media on (unknown)."""
    with pytest.raises(recipes.RecipeError):
        recipes.parse_recipe("""name: x
title: X
engine: e
version: "1"
tasks: [chat]
formats: [gguf]
api: {chat: openai-chat}
estimate: {method: gguf}
media: {inputs: [smell]}
variants: [{name: c, when: {vendor: cpu}, runtime: process, command: /bin/x}]
""")
    with pytest.raises(recipes.RecipeError):
        recipes.parse_recipe("""name: x
title: X
engine: e
version: "1"
tasks: [chat]
formats: [gguf]
api: {chat: openai-chat}
estimate: {method: gguf}
optional_args: [{args: [--x]}]
variants: [{name: c, when: {vendor: cpu}, runtime: process, command: /bin/x}]
""")
    listed = {r["name"]: r for r in owner.get("/api/models").json()["recipes"]}
    assert listed["vllm"]["params"]["images"]["advanced"] is True
    assert listed["vllm"]["params"]["videos"]["default"] == 0 and listed["vllm"]["params"]["context"]["advanced"] is False
    assert listed["vllm"]["media"] == {"inputs": ["image", "audio", "video"], "files": False}
    assert listed["faster-whisper"]["media"] is None
    custom, _ = recipes.builtin_recipes()["faster-whisper"]
    a = modalities.accepts(custom, {}, "safetensors", {})
    assert a.kinds == ["image", "audio"] and not a.known and a.max_body == 32 * 1024**2


def test_fy_mm_016_a_custom_recipe_without_media_passes_images_on(mm):
    """FY-MM-016: a model added before 1.3 (no modalities: unknown) on a recipe without a media block
    gets images (the engine decides); a GGUF from before 1.3 has no projector, so no images."""
    yaml_text = recipes.builtin_recipes()["vllm"][1]
    lines = [ln for ln in yaml_text.splitlines() if not ln.startswith("media:")]
    custom = "\n".join(lines).replace("name: vllm", "name: my-vllm", 1)
    r = mm.owner.post("/api/recipes", json={"yaml": custom})
    assert r.status_code == 201, r.text
    meta = vl_meta()
    meta.pop("modalities")
    add_catalog("old-vl", meta, task="chat", fmt="safetensors", source="hf://acme/old-vl")
    deploy(mm.owner, "old", "old-vl", "my-vllm", params={"share": "vram", "context": 4096}, wait=True)
    e = FakeEngine("old", "secret-old")
    try:
        make_ready("old", [(mm.gpu, e.port, e.secret)])
        assert ask(mm.api, "old", image(PNG_URI)).status_code == 200
    finally:
        e.close()
    assert modalities.of_model({}, "gguf") == [] and modalities.of_model({}, "safetensors") is None


# ------------------------------------------------------------------ inspection, estimate, catalog
def test_fy_mm_017_modalities_from_configs_and_projectors():
    """FY-MM-017: vision_config / image tokens -> image; audio_config -> audio (an audio token with a
    null audio_config does not count); video tokens or video_preprocessor_config.json -> video; Omni
    models through thinker_config; a projector's clip.has_* keys."""
    assert modalities.from_config({"vision_config": {"x": 1}, "video_token_id": 5}, []) == ["image", "video"]
    assert modalities.from_config({"vision_config": {}, "image_token_index": 3}, ["video_preprocessor_config.json"]) \
        == ["image", "video"]
    assert modalities.from_config({"audio_config": None, "audio_token_id": 9, "vision_config": {"a": 1}}, []) == ["image"]
    assert modalities.from_config({"audio_token_id": 9}, []) == ["audio"]
    assert modalities.from_config({"thinker_config": {"audio_config": {"a": 1}, "vision_config": {"b": 1},
                                                      "video_token_index": 2}}, []) == ["image", "audio", "video"]
    assert modalities.from_config({"num_hidden_layers": 2}, ["config.json"]) == []
    assert modalities.from_mmproj({"clip.has_vision_encoder": True}) == ["image", "video"]
    assert modalities.from_mmproj({"clip.has_audio_encoder": True, "clip.has_vision_encoder": False}) == ["audio"]


def test_fy_mm_018_a_gguf_repository_with_projectors(owner, monkeypatch):
    """FY-MM-018: projector files are not model choices; the F16 one is used by default, read for what
    it adds and downloaded with the model; another one can be chosen, or none; the licence is read from
    the card. The catalog shows licence, gated, media and projector."""
    model_header = (FIX / "qwen3-1.7b-q4_k_m.gguf-header").read_bytes()
    proj = gguf_header({"general.architecture": "clip", "clip.has_vision_encoder": True,
                        "clip.projector_type": "qwen3vl_merger"})
    files = {"Qwen3VL-2B-Instruct-Q4_K_M.gguf": model_header, "Qwen3VL-2B-Instruct-F16.gguf": model_header,
             "mmproj-Qwen3VL-2B-Instruct-F16.gguf": proj, "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf": proj}
    repo = Repo("Qwen/Qwen3-VL-2B-Instruct-GGUF", "a" * 40, files)
    repo.info.update(cardData={"license": "apache-2.0"}, pipeline_tag="image-text-to-text")
    hf = FakeHF(repo)
    try:
        monkeypatch.setenv("HF_ENDPOINT", hf.url)
        monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
        get_settings.cache_clear()
        r = owner.post("/api/models/inspect", json={"source": "hf://Qwen/Qwen3-VL-2B-Instruct-GGUF"})
        assert r.status_code == 200, r.text
        meta = r.json()["meta"]
        assert [c["path"] for c in meta["gguf_files"]] == ["Qwen3VL-2B-Instruct-F16.gguf", "Qwen3VL-2B-Instruct-Q4_K_M.gguf"]
        assert meta["file"] == "Qwen3VL-2B-Instruct-Q4_K_M.gguf"
        assert meta["mmproj"]["path"] == "mmproj-Qwen3VL-2B-Instruct-F16.gguf"
        assert meta["modalities"] == ["image", "video"] and meta["license"] == "apache-2.0" and meta["gated"] is False
        est = r.json()["suggestion"]["estimate"]
        assert "Projector (mmproj)" in [p["label"] for p in est["parts"]]
        r = owner.post("/api/models", json={"name": "vl2", "source": "hf://Qwen/Qwen3-VL-2B-Instruct-GGUF",
                                             "mmproj": "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"})
        assert r.status_code == 201, r.text
        m = r.json()["model"]
        assert m["mmproj"]["path"] == "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf" and m["license"] == "apache-2.0"
        assert m["modalities"] == ["image", "video"]
        from forgeyard.models import catalog
        db = new_session()
        try:
            block = catalog.model_block(db.query(CatalogModel).filter_by(name="vl2").one())
        finally:
            db.close()
        assert [f["path"] for f in block["files"]] == ["Qwen3VL-2B-Instruct-Q4_K_M.gguf",
                                                       "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"]
        r = owner.post("/api/models", json={"name": "vl2-text", "source": "hf://Qwen/Qwen3-VL-2B-Instruct-GGUF",
                                             "mmproj": "none"})
        assert r.status_code == 201 and r.json()["model"]["mmproj"] is None and r.json()["model"]["modalities"] == []
        r = owner.post("/api/models/inspect", json={"source": "hf://Qwen/Qwen3-VL-2B-Instruct-GGUF",
                                                    "mmproj": "mmproj-nope.gguf"})
        assert r.status_code == 422
    finally:
        hf.close()


def test_fy_mm_019_a_safetensors_vision_model_is_recognised(owner, monkeypatch):
    """FY-MM-019: config.json with vision_config and video tokens, plus video_preprocessor_config.json:
    image and video; the licence comes from a license: tag when the card has none; the vision tower's
    patch and merge sizes give the pixels per image token."""
    config = json.loads((FIX / "qwen3-4b.config.json").read_text())
    config.update(vision_config={"depth": 24, "patch_size": 16, "spatial_merge_size": 2}, image_token_id=1,
                  video_token_id=2)
    header = {"model.embed_tokens.weight": {"dtype": "BF16", "shape": [16, 8], "data_offsets": [0, 256]}}
    files = {"config.json": json.dumps(config).encode(), "model.safetensors": safetensors_bytes(header, bytes(256)),
             "video_preprocessor_config.json": b"{}"}
    repo = Repo("acme/tiny-vl", "b" * 40, files)
    repo.info["tags"] = ["license:mit"]
    hf = FakeHF(repo)
    try:
        monkeypatch.setenv("HF_ENDPOINT", hf.url)
        monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "true")
        get_settings.cache_clear()
        r = owner.post("/api/models", json={"name": "tiny-vl", "source": "hf://acme/tiny-vl", "task": "chat"})
        assert r.status_code == 201, r.text
        m = r.json()["model"]
        assert m["modalities"] == ["image", "video"] and m["license"] == "mit" and m["mmproj"] is None
        db = new_session()
        try:   # 1.3: pixels per image token, for vLLM's image-token cap (FY-MM-025)
            assert db.query(CatalogModel).filter_by(name="tiny-vl").one().meta["vision_tile_px"] == 32
        finally:
            db.close()
    finally:
        hf.close()


def test_fy_mm_020_the_estimate_adds_the_vision_encoder():
    """FY-MM-020: a vision model on vLLM adds the recipe's vision term (and the video term once videos
    > 0), not with images 0, never for a text model; a GGUF with a projector adds its file and the
    compute buffers, on the CPU and on the GPU."""
    vllm, _ = recipes.builtin_recipes()["vllm"]
    llama, _ = recipes.builtin_recipes()["llama-cpp"]
    params = {k: p.default for k, p in vllm.params.items()} | {"gpus": 1}
    labels = lambda e: {p.label: p.bytes for p in e.parts}  # noqa: E731
    vl = estimate(vl_meta(), vllm, params, device="gpu", gpu_context=400 * 1024**2)
    assert labels(vl)["Vision encoder"] == vllm.estimate.vision_overhead and "Video" not in labels(vl)
    assert any("context tokens" in n for n in vl.notes)
    with_video = estimate(vl_meta(), vllm, params | {"videos": 1}, device="gpu", gpu_context=400 * 1024**2)
    assert with_video.per_gpu - vl.per_gpu == vllm.estimate.video_overhead
    off = estimate(vl_meta(), vllm, params | {"images": 0}, device="gpu", gpu_context=400 * 1024**2)
    assert vl.per_gpu - off.per_gpu == vllm.estimate.vision_overhead
    text_meta = vl_meta()
    text_meta["modalities"] = []
    assert "Vision encoder" not in labels(estimate(text_meta, vllm, params, device="gpu", gpu_context=0))
    g = gguf.parse((FIX / "qwen3-1.7b-q4_k_m.gguf-header")
                   .read_bytes()).summary()
    lp = {k: p.default for k, p in llama.params.items()}
    for device, gpus in (("cpu", 0), ("gpu", 1)):
        with_p = estimate(gguf_vl_meta(g), llama, lp | {"gpus": gpus}, device=device, gpu_context=0)
        plain = gguf_vl_meta(g)
        plain.pop("mmproj")
        plain["modalities"] = []
        without = estimate(plain, llama, lp | {"gpus": gpus}, device=device, gpu_context=0)
        assert labels(with_p)["Projector (mmproj)"] == 819394848
        assert with_p.amount - without.amount == 819394848 + llama.estimate.vision_overhead


def test_fy_mm_021_the_recommended_list_holds_allowed_licences_and_projectors():
    """FY-MM-021: every recommended entry declares a licence from the allowed list; projector files only
    on GGUF entries and named mmproj-*.gguf; vision entries say what they take; a licence outside the
    list is refused."""
    data = rec.load()
    assert all(e.license in rec.ALLOWED_LICENSES for e in data.models)
    vision = {e.name: e for e in data.models if e.inputs}
    assert {"qwen3-vl-2b", "qwen3-vl-4b", "qwen3-vl-8b", "qwen3-vl-2b-gguf", "qwen3-vl-8b-gguf"} <= set(vision)
    assert all(sources.is_mmproj(e.mmproj) for e in vision.values() if e.format == "gguf")
    raw = __import__("yaml").safe_load(rec.FILE.read_text())
    raw["models"][0]["license"] = "cc-by-nc-4.0"
    with pytest.raises(rec.RecommendedError, match="licence"):
        rec.check(raw)
    raw["models"][0]["license"] = "apache-2.0"
    raw["models"][0]["mmproj"] = "mmproj-x.gguf"
    with pytest.raises(rec.RecommendedError, match="projector"):
        rec.check(raw)


def test_fy_mm_022_projector_choices():
    """FY-MM-022: mmproj files are not model choices; F16, then BF16, then Q8_0 is the default
    projector; select_files downloads the chosen projector with the model."""
    files = [{"path": p, "size": 1} for p in ("m-Q4_K_M.gguf", "mmproj-m-Q8_0.gguf", "mmproj-m-BF16.gguf", "MMPROJ-x.gguf")]
    assert [c["path"] for c in sources.gguf_choices(files)] == ["m-Q4_K_M.gguf"]
    choices = sources.mmproj_choices(files)
    assert sources.default_mmproj(choices) == "mmproj-m-BF16.gguf"
    assert sources.default_mmproj([c for c in choices if "BF16" not in c["path"]]) == "mmproj-m-Q8_0.gguf"
    assert sources.default_mmproj([]) is None
    assert [f["path"] for f in sources.select_files(files, "gguf", "m-Q4_K_M.gguf", "mmproj-m-Q8_0.gguf")] == [
        "m-Q4_K_M.gguf", "mmproj-m-Q8_0.gguf"]
    assert [f["path"] for f in sources.select_files(files, "gguf", "m-Q4_K_M.gguf")] == ["m-Q4_K_M.gguf"]


# ------------------------------------------------------------------ usage columns (migration 0012)
def test_fy_mm_023_the_migration_adds_the_units_with_zero(owner):
    """FY-MM-023: usage rows from before 0012 come through the upgrade with every unit 0; down drops the
    columns without touching the rows; up again adds them back (on SQLite and PostgreSQL)."""
    def columns() -> set:
        with get_engine().connect() as conn:
            if conn.dialect.name == "sqlite":
                return {r[1] for r in conn.execute(text("PRAGMA table_info(usage_daily)"))}
            return {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns "
                                                    "WHERE table_name = 'usage_daily'"))}

    owner.post("/api/keys", json={"name": "k"})
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0011_budgets")
    assert not set(UNITS) & columns()
    with get_engine().begin() as conn:
        conn.execute(text("INSERT INTO usage_daily (day, source, key_id, user_id, username, model, target, requests, "
                          "errors, limited, prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens, spilled, "
                          "cost_micros, unpriced, cost_reported, cost_estimated, latency_ms_sum, latency_buckets) "
                          "VALUES ('2026-11-01', 'api', 1, 1, 'owner', 'old', 'local', 3, 0, 0, 10, 5, 0, 0, 0, 0, 0, "
                          "0, 0, 30, '[]')"))
    upgrade()
    assert set(UNITS) <= columns()
    with get_engine().connect() as conn:
        row = conn.execute(text(f"SELECT requests, {', '.join(UNITS)} FROM usage_daily WHERE model = 'old'")).one()
    assert tuple(row) == (3,) + (0,) * len(UNITS)
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0011_budgets")
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT requests FROM usage_daily WHERE model = 'old'")).scalar() == 3
        assert conn.execute(text("SELECT count(*) FROM api_keys")).scalar() == 1
    upgrade()
    assert set(UNITS) <= columns()


def test_fy_mm_024_the_recorder_sums_units():
    """FY-MM-024: units are summed per row and written; unknown names and negatives are ignored."""
    from forgeyard.gateway.usage import Recorder, summary
    upgrade()
    r = Recorder()
    for units in ({"media_items": 2}, {"media_items": 3, "characters": 7}, {"bogus": 9, "images": -4}, None):
        r.record(source="api", key_id=1, user_id=1, username="u", model="m", deployment_id=None, status=200,
                 latency_ms=5, units=units)
    r.flush()
    db = new_session()
    try:
        total = summary(db)["total"]
    finally:
        db.close()
    assert (total["media_items"], total["characters"], total["images"], total["requests"]) == (5, 7, 0, 4)



def test_fy_mm_025_vllm_scales_large_images_down_for_qwen_vl(mm):
    """FY-MM-025: for a vision tower whose tokens grow with the resolution (Qwen-VL: patch 16, merge 2,
    so 32x32 pixels a token) vLLM gets --mm-processor-kwargs max_pixels = image_tokens x 32 x 32, from the
    recipe's default (1,024) or the deployment's value; a model without that metadata, or a text model,
    gets none. Unscaled, a 5 MB photo was 16,244 tokens on a real A100 and refused by an 8,192 context."""
    from forgeyard.models import catalog, render
    args = replica_spec("vl")["runtime"]["args"]
    assert json.loads(args[args.index("--mm-processor-kwargs") + 1]) == {"max_pixels": 1024 * 32 * 32}
    assert "--mm-processor-kwargs" not in replica_spec("txt")["runtime"]["args"]
    assert modalities.vision_tile_px({"vision_config": {"patch_size": 16, "spatial_merge_size": 2}}) == 32
    assert modalities.vision_tile_px({"thinker_config": {"vision_config": {"patch_size": 14, "spatial_merge_size": 2}}}) == 28
    assert modalities.vision_tile_px({"vision_config": {"patch_size": 14}}) == 0          # Gemma-like: fixed tokens
    assert modalities.vision_tile_px({"vision_config": {"patch_size": "16", "spatial_merge_size": 2}}) == 0
    db = new_session()
    try:
        d = db.query(Deployment).filter_by(name="vl").one()
        d.params = {**d.params, "image_tokens": 256}
        ctx = catalog.context_for(db, d)
        cards = [a for a in db.query(Accelerator).all() if a.index == 4 and a.vendor == "nvidia"][:1]
        out = render.render(ctx, ctx.recipe.variants[0], cards, "vl-0", gpu_context=400 * 1024**2)
        a = out["runtime"]["args"]
        assert json.loads(a[a.index("--mm-processor-kwargs") + 1]) == {"max_pixels": 256 * 32 * 32}
        ctx.model.meta = {k: v for k, v in ctx.model.meta.items() if k != "vision_tile_px"}   # inspected before 1.3
        out = render.render(ctx, ctx.recipe.variants[0], cards, "vl-0", gpu_context=400 * 1024**2)
        assert "--mm-processor-kwargs" not in out["runtime"]["args"]
        db.rollback()
    finally:
        db.close()


def test_fy_mm_026_llama_cpp_counts_video_frames_once_video_is_on():
    """FY-MM-026: a GGUF vision model on llama.cpp adds the recipe's video term (512 MiB, measured on
    the CPU) only when the deployment takes video, on the CPU and on a GPU; a text-only GGUF never."""
    llama, _ = recipes.builtin_recipes()["llama-cpp"]
    assert llama.estimate.video_overhead == 512 * 1024**2
    labels = lambda e: {p.label: (p.bytes, p.detail) for p in e.parts}  # noqa: E731
    g = gguf.parse((FIX / "qwen3-1.7b-q4_k_m.gguf-header").read_bytes()).summary()
    lp = {k: p.default for k, p in llama.params.items()}
    assert lp["videos"] == 0
    for device, gpus in (("cpu", 0), ("gpu", 1)):
        off = estimate(gguf_vl_meta(g), llama, lp | {"gpus": gpus}, device=device, gpu_context=0)
        on = estimate(gguf_vl_meta(g), llama, lp | {"gpus": gpus, "videos": 1}, device=device, gpu_context=0)
        assert "Video" not in labels(off)
        assert labels(on)["Video"] == (512 * 1024**2, "buffers for a video's frames (recipe)")
        assert on.amount - off.amount == 512 * 1024**2
        text = gguf_vl_meta(g)
        text.pop("mmproj")
        text["modalities"] = []
        assert "Video" not in labels(estimate(text, llama, lp | {"gpus": gpus, "videos": 1}, device=device, gpu_context=0))
