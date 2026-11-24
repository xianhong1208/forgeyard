"""Image to video (1.5.5, #143): /v1/videos with a start image (OpenAI's `input_reference`) through the gateway to fake
vLLM-Omni video engines: what a video model does with a start image (catalog meta.video.image_input, from its pipeline
class), both request shapes (a multipart file; JSON {"image_url"}, data: URLs, http(s) URLs fetched by the gateway),
the checks an edit's input images get (bytes, type, header size), models that need, take or refuse a start image, the
engine's request (the checked file as input_reference, an explicit size, never a URL), usage, the estimate and the
Playground's options (FY-I2V-*)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forgeyard.config import get_settings
from forgeyard.gateway import videogen
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.usage import recorder
from forgeyard.models import inspect as inspector, recipes, videoinput
from forgeyard.models.estimate import GIB, estimate
from tests.gateway_helpers import api_client, make_ready, new_key
from tests.model_helpers import add_catalog
from tests.test_image_edits import claims, data_url, jpeg_header
from tests.test_image_generation import png
from tests.test_multimodal import PNG, MediaServer
from tests.test_scheduler import deploy
from tests.test_video_generation import FEATURES, wait_status, wan_meta
from tests.video_helpers import FakeVideos
from tests.workload_helpers import make_node

MIB = 1024**2
TI2V_REV = "b8fff7315c768468a5333511427288870b2e9635"


def ti2v_meta(**video) -> dict:
    """Wan-AI/Wan2.2-TI2V-5B-Diffusers at b8fff73 as the inspector reads it: WanPipeline with expand_timesteps, its
    component sizes from the repository's safetensors indexes (text encoder bf16, transformer and VAE float32)."""
    return {"kind": "hf", "repo": "Wan-AI/Wan2.2-TI2V-5B-Diffusers", "ref": "main", "revision": TI2V_REV,
            "format": "diffusers", "license": "apache-2.0", "modalities": [], "task_hint": "video",
            "diffusers": {"pipeline": "WanPipeline", "components": {
                "text_encoder": {"class": "UMT5EncoderModel", "params": 5_680_910_336, "bytes": 11_361_820_672,
                                 "dtype": "BF16"},
                "transformer": {"class": "WanTransformer3DModel", "params": 4_999_787_712, "bytes": 19_999_150_848,
                                "dtype": "F32"},
                "vae": {"class": "AutoencoderKLWan", "params": 704_694_452, "bytes": 2_818_777_808, "dtype": "F32"}}},
            "video": {"size": [1280, 704], "multiple": 32, "fps": 24, **video},
            "files": [{"path": "model_index.json", "size": 499, "oid": "b" * 40}], "size_bytes": 34_179_749_328}


def i2v_meta() -> dict:
    """An image-to-video pipeline (WanImageToVideoPipeline), small enough to share the test card: needs a start image."""
    meta = wan_meta(repo="Wan-AI/Wan2.1-I2V-test-Diffusers")
    meta["diffusers"] = dict(meta["diffusers"], pipeline="WanImageToVideoPipeline")
    return meta


def err(r) -> dict:
    body = r.json()
    assert set(body) == {"error"}, body
    return body["error"]


def usage_by_model(owner) -> dict:
    recorder.flush()
    return {x["id"]: x for x in owner.get("/api/usage?group=model").json()["rows"]}


def posts(v, name: str) -> list[dict]:
    return v.engines[name].posts


def start(api, model: str, image: bytes, ctype: str = "image/png", **fields):
    """A multipart request as OpenAI's SDK sends videos.create(input_reference=…)."""
    return api.post("/v1/videos", data={"model": model, "prompt": "The cat walks to the window.", **fields},
                    files={"input_reference": ("start.png", image, ctype)})


@pytest.fixture()
def iv(owner, monkeypatch):
    """Three video deployments on the A100's card #4, each on its own fake engine: `ti2v` (Wan2.2-TI2V-5B: a start
    image is optional), `i2v` (an image-to-video pipeline: one is required), `t2v` (Wan2.1-T2V-1.3B: text only); a key;
    a media server on 127.0.0.1 (MEDIA_ALLOW_PRIVATE_HOSTS on)."""
    monkeypatch.setenv("MEDIA_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    add_catalog("wan2.2-ti2v-5b", ti2v_meta(image_input="optional"), task="video", fmt="diffusers",
                source="hf://Wan-AI/Wan2.2-TI2V-5B-Diffusers")
    add_catalog("wan-i2v", i2v_meta(), task="video", fmt="diffusers", source="hf://Wan-AI/Wan2.1-I2V-test-Diffusers")
    add_catalog("wan2.1-t2v-1.3b", wan_meta(), task="video", fmt="diffusers",
                source="hf://Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
    gpu = make_node("a100", allowlist=[4], features=FEATURES)
    engines = {}
    for name, model in (("ti2v", "wan2.2-ti2v-5b"), ("i2v", "wan-i2v"), ("t2v", "wan2.1-t2v-1.3b")):
        engines[name] = FakeVideos(f"{name}-secret", work=0.1)
        deploy(owner, name, model, "vllm-omni-video", params={"share": "vram"}, wait=True)
        make_ready(name, [(gpu, engines[name].port, engines[name].secret)])
    key = new_key(owner, "laptop")["key"]
    media = MediaServer()
    yield SimpleNamespace(owner=owner, engines=engines, key=key, api=api_client(owner, key), media=media)
    for e in engines.values():
        e.close()
    media.close()
    get_settings.cache_clear()


# ------------------------------------------------------------------ what a video model does with a start image
def test_fy_i2v_001_what_a_video_model_does_with_a_start_image(monkeypatch):
    """FY-I2V-001: the pipeline class says it: WanPipeline with expand_timesteps (Wan2.2-TI2V-5B) takes an optional
    start image, the image-to-video pipelines vLLM-Omni 0.32.0 runs need one, other text-to-video pipelines take none;
    an entry saying it wins; an entry from before 1.5.5 is read from its class, a WanPipeline with sizes in multiples of
    32 being TI2V-5B. The inspector writes it into meta.video and takes those image-to-video pipelines as video models
    (others, and video-to-video, are still not)."""
    assert videoinput.from_pipeline("WanPipeline", expand_timesteps=True) == "optional"
    assert videoinput.from_pipeline("WanPipeline") == "none"
    for p in ("WanImageToVideoPipeline", "SanaImageToVideoPipeline", "HunyuanVideo15ImageToVideoPipeline"):
        assert videoinput.from_pipeline(p) == "required" and inspector.is_video_pipeline(p)
    assert videoinput.from_pipeline("HunyuanVideoPipeline") == "none" and videoinput.from_pipeline(None) == "none"
    for p in ("CogVideoXImageToVideoPipeline", "LTXImageToVideoPipeline", "WanVideoToVideoPipeline"):
        assert not inspector.is_video_pipeline(p)
    # From the catalog entry: its own key first, else its class (and the TI2V-5B shape for a WanPipeline).
    assert videoinput.of(ti2v_meta(image_input="optional")) == "optional"
    assert videoinput.of(ti2v_meta()) == "optional"                 # 1.3 entry of TI2V-5B: multiples of 32
    assert videoinput.of(ti2v_meta(image_input="none")) == "none"   # the entry says it
    assert videoinput.of(wan_meta()) == "none"                      # Wan2.1-T2V: multiples of 16
    assert videoinput.of(i2v_meta()) == "required"
    assert videoinput.of({}) == "none" and videoinput.of(None) == "none"
    assert videoinput.of(ti2v_meta(image_input="sometimes")) == "optional"   # not one of the kinds: read from the class
    # The inspector, on a repository whose model_index.json names an image-to-video pipeline.
    configs = {"vae/config.json": {"_class_name": "AutoencoderKLWan", "z_dim": 16},
               "transformer/config.json": {"patch_size": [1, 2, 2]}}

    def fake_get(url, policy, headers=None, limit=None):
        return json.dumps(next(v for k, v in configs.items() if url.endswith(k))).encode()

    monkeypatch.setattr(inspector.fetch, "get_bytes", fake_get)
    native = inspector._video_native({"_class_name": "WanImageToVideoPipeline"}, lambda p: f"https://hf/{p}", None,
                                     None)
    assert native["image_input"] == "required" and native["multiple"] == 16


def test_fy_i2v_002_the_catalog_says_it(owner):
    """FY-I2V-002: GET /api/models lists each video model's image_input (none, optional, required), including an
    entry from before 1.5.5 that has no key."""
    add_catalog("wan2.2-ti2v-5b", ti2v_meta(), task="video", fmt="diffusers", source="hf://Wan-AI/Wan2.2-TI2V-5B-Diffusers")
    add_catalog("wan-i2v", i2v_meta(), task="video", fmt="diffusers", source="hf://Wan-AI/Wan2.1-I2V-test-Diffusers")
    add_catalog("wan2.1-t2v-1.3b", wan_meta(), task="video", fmt="diffusers", source="hf://Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
    models = {m["name"]: m for m in owner.get("/api/models").json()["models"]}
    assert models["wan2.2-ti2v-5b"]["video"]["image_input"] == "optional"
    assert models["wan-i2v"]["video"]["image_input"] == "required"
    assert models["wan2.1-t2v-1.3b"]["video"]["image_input"] == "none"


# ------------------------------------------------------------------ the request
def test_fy_i2v_003_a_start_image_reaches_the_engine_as_a_checked_file(iv):
    """FY-I2V-003: a multipart request with input_reference (as OpenAI's SDK sends it) reaches the engine as a new
    multipart body: the fields Forgeyard builds, always with an explicit size (the deployment's default, 1280x704, when
    none is asked), and the image byte for byte as the file input_reference with its type; never a URL, another
    reference, a LoRA or another file the client sent. A request without a start image to the same model is plain text
    to video (no file). The job completes."""
    a = png(640, 352, 40)
    r = start(iv.api, "ti2v", a, image_reference='{"image_url": "http://169.254.169.254/"}')
    assert r.status_code == 400 and err(r)["code"] == "input_reference_not_supported"   # vLLM-Omni's URL field
    r = iv.api.post("/v1/videos", data={"model": "ti2v", "prompt": "The cat walks.", "lora": '{"path": "/etc/shadow"}',
                                        "extra_params": '{"x": 1}'},
                    files=[("input_reference", ("s.png", a, "image/png")), ("source_video", ("v.mp4", b"x" * 64, "video/mp4"))])
    assert r.status_code == 200, r.text
    sent = posts(iv, "ti2v")[-1]
    assert sent["fields"] == {"prompt": "The cat walks.", "model": "ti2v", "size": "1280x704", "num_frames": "121",
                              "fps": "24", "input_reference": {"file": True}}
    assert [(n, d) for n, _, d in sent["files"]] == [("input_reference", a)]
    assert "Content-Type: image/png" in sent["files"][0][1]
    assert b"/etc/shadow" not in sent["raw"] and b"169.254" not in sent["raw"] and b"x" * 64 not in sent["raw"]
    assert wait_status(iv.owner, iv.api, r.json()["id"], "completed")["status"] == "completed"
    r = iv.api.post("/v1/videos", json={"model": "ti2v", "prompt": "Rain.", "size": "704x1280"})
    assert r.status_code == 200, r.text
    sent = posts(iv, "ti2v")[-1]
    assert sent["files"] == [] and sent["fields"]["size"] == "704x1280"
    # A JPEG start image keeps its type.
    j = jpeg_header(320, 240)
    assert start(iv.api, "ti2v", j, "image/jpeg").status_code == 200
    name, head, data = posts(iv, "ti2v")[-1]["files"][0]
    assert data == j and "Content-Type: image/jpeg" in head and 'filename="start.jpg"' in head


def test_fy_i2v_004_json_start_images_and_urls_fetched_by_the_gateway(iv, monkeypatch):
    """FY-I2V-004: OpenAI's JSON form: input_reference {"image_url": …} as a data: URL, or an http(s) URL the gateway
    fetches under the media rules and sends as a file (the engine never gets the URL); a file_id is 400
    file_id_not_supported, another scheme media_url_refused, a bad data: URL invalid_image, a string or a list 400; a
    key that may not use the model makes the gateway fetch nothing (403); with private hosts off the URL is refused
    before it is fetched. None of these reaches the engine."""
    a = png(320, 192, 50)
    r = iv.api.post("/v1/videos", json={"model": "ti2v", "prompt": "x", "input_reference": {"image_url": data_url(a)}})
    assert r.status_code == 200, r.text
    assert [d for _, _, d in posts(iv, "ti2v")[-1]["files"]] == [a]
    url = f"{iv.media.url}/img.png"
    r = iv.api.post("/v1/videos", json={"model": "ti2v", "prompt": "x", "input_reference": {"image_url": {"url": url}}})
    assert r.status_code == 200, r.text
    sent = posts(iv, "ti2v")[-1]
    assert [d for _, _, d in sent["files"]] == [PNG] and iv.media.hits == ["/img.png"] and url.encode() not in sent["raw"]
    before = len(posts(iv, "ti2v"))
    for ref, code in [({"file_id": "file-abc"}, "file_id_not_supported"),
                      ({"image_url": "file:///etc/passwd"}, "media_url_refused"),
                      ({"image_url": "data:image/png;base64,@@@"}, "invalid_image"),
                      ({"image_url": ""}, "invalid_request"),
                      ("https://example.com/a.png", "invalid_request"),
                      ([{"image_url": data_url(a)}], "invalid_request")]:
        r = iv.api.post("/v1/videos", json={"model": "ti2v", "prompt": "x", "input_reference": ref})
        assert r.status_code == 400 and err(r)["code"] == code and err(r)["param"] == "input_reference", (ref, r.text)
    narrow = api_client(iv.owner, new_key(iv.owner, "narrow", models=["t2v"])["key"])
    hits = len(iv.media.hits)
    r = narrow.post("/v1/videos", json={"model": "ti2v", "prompt": "x", "input_reference": {"image_url": url}})
    assert r.status_code == 403 and err(r)["code"] == "model_not_allowed" and len(iv.media.hits) == hits
    monkeypatch.setenv("MEDIA_ALLOW_PRIVATE_HOSTS", "false")
    get_settings.cache_clear()
    r = iv.api.post("/v1/videos", json={"model": "ti2v", "prompt": "x", "input_reference": {"image_url": url}})
    assert r.status_code == 400 and err(r)["code"] == "media_url_refused" and len(iv.media.hits) == hits
    assert err(r)["param"] == "input_reference"
    assert len(posts(iv, "ti2v")) == before


def test_fy_i2v_005_the_start_image_checks(iv, monkeypatch):
    """FY-I2V-005: the start image gets an edit's input checks before any engine sees it: a HEIC photo or a GIF is
    unsupported_image_format, a PNG with a cut header or no pixels invalid_image, one over MEDIA_FETCH_MAX_MB 413
    media_too_large, a 33-byte PNG claiming 60000x60000 (never decoded) input_image_too_large (the deployment's
    max_input_side, 2048 by default; a deployment with a larger one takes it), two start images too_many_input_images."""
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
    gif = b"GIF89a" + b"\x00" * 64
    cut = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    cases = [(start(iv.api, "ti2v", heic, "image/heic"), 400, "unsupported_image_format"),
             (start(iv.api, "ti2v", gif, "image/gif"), 400, "unsupported_image_format"),
             (start(iv.api, "ti2v", cut), 400, "invalid_image"),
             (start(iv.api, "ti2v", claims(0, 10)), 400, "invalid_image"),
             (start(iv.api, "ti2v", claims(60000, 60000)), 400, "input_image_too_large"),
             (start(iv.api, "ti2v", claims(2049, 64)), 400, "input_image_too_large"),
             (iv.api.post("/v1/videos", data={"model": "ti2v", "prompt": "x"},
                          files=[("input_reference", ("a.png", PNG, "image/png")),
                                 ("input_reference", ("b.png", PNG, "image/png"))]), 400, "too_many_input_images")]
    monkeypatch.setenv("MEDIA_FETCH_MAX_MB", "1")
    get_settings.cache_clear()
    cases.append((start(iv.api, "ti2v", PNG + b"\x00" * (MIB + 1)), 413, "media_too_large"))
    for r, status, code in cases:
        assert r.status_code == status and err(r)["code"] == code and err(r)["param"] == "input_reference", r.text
    assert "HEIC" in err(cases[0][0])["message"] and "2048" in err(cases[4][0])["message"]
    assert posts(iv, "ti2v") == []
    # Exactly the largest side is taken; a deployment that allows more takes more.
    monkeypatch.delenv("MEDIA_FETCH_MAX_MB")
    get_settings.cache_clear()
    assert start(iv.api, "ti2v", claims(2048, 64)).status_code == 200
    from forgeyard.gateway import imageedit
    recipe = recipes.builtin_recipes()["vllm-omni-video"][0]
    ask = videogen.check({"prompt": "x", "input_reference": {"file": "input_reference", "data": claims(4000, 64)}})
    imageedit.check(ask.inputs, videogen.START)
    assert videogen.takes(videogen.films(recipe, {}, ti2v_meta()), ask) == "input_image_too_large"
    assert videogen.takes(videogen.films(recipe, {"max_input_side": 4096}, ti2v_meta()), ask) is None


def test_fy_i2v_006_models_that_need_take_or_refuse_a_start_image(iv):
    """FY-I2V-006: an image-to-video model refuses a request without a start image (400 input_reference_required) and
    takes one with it; a text-to-video model refuses a start image (400 input_reference_not_supported); neither reaches
    the engine when refused. Each error names input_reference."""
    r = iv.api.post("/v1/videos", json={"model": "i2v", "prompt": "The cat walks."})
    assert r.status_code == 400 and err(r)["code"] == "input_reference_required" and err(r)["param"] == "input_reference"
    assert "'i2v'" in err(r)["message"] and posts(iv, "i2v") == []
    r = start(iv.api, "i2v", png(320, 192))
    assert r.status_code == 200, r.text
    assert [n for n, _, _ in posts(iv, "i2v")[-1]["files"]] == ["input_reference"]
    r = start(iv.api, "t2v", png(320, 192))
    assert r.status_code == 400 and err(r)["code"] == "input_reference_not_supported"
    assert err(r)["param"] == "input_reference" and posts(iv, "t2v") == []
    assert iv.api.post("/v1/videos", json={"model": "t2v", "prompt": "Rain."}).status_code == 200


def test_fy_i2v_007_which_refusal_a_route_gives():
    """FY-I2V-007: when no deployment of a route takes a request, the error comes from what the one that takes start
    images would need (a shorter prompt, a smaller image) before "takes none": the image's own codes come last."""
    ask = videogen.check({"prompt": "x" * 3000, "input_reference": {"file": "input_reference", "data": PNG}})
    from forgeyard.gateway import imageedit
    imageedit.check(ask.inputs, videogen.START)
    text_only, ti2v = videogen.Films(), videogen.Films(image_input="optional")
    why = [videogen.takes(text_only, ask), videogen.takes(ti2v, ask)]
    assert why == ["input_reference_not_supported", "prompt_too_long"]
    assert min(why, key=videogen.ORDER.index) == "prompt_too_long"
    small = videogen.Films(image_input="optional", max_input_side=0)
    short = videogen.check({"prompt": "x", "input_reference": {"file": "input_reference", "data": PNG}})
    imageedit.check(short.inputs, videogen.START)
    why = [videogen.takes(text_only, short), videogen.takes(small, short)]
    code = min(why, key=videogen.ORDER.index)
    assert code == "input_image_too_large"
    e = videogen.refusal("m", code, short, [text_only, small])
    assert isinstance(e, GatewayError) and e.code == "input_image_too_large" and e.param == "input_reference"
    assert videogen.takes(videogen.Films(image_input="required"), videogen.check({"prompt": "x"})) == \
        "input_reference_required"


# ------------------------------------------------------------------ usage, the estimate, the Playground
def test_fy_i2v_008_usage_counts_the_start_image(iv):
    """FY-I2V-008: a job with a start image counts one request (when it ends), one media item and the image's bytes in
    bytes_in (when it is accepted), and the video's media_ms; a text-only job counts no media item; a refused request
    counts no input."""
    a = png(320, 192, 70)
    r = start(iv.api, "ti2v", a)
    assert r.status_code == 200
    wait_status(iv.owner, iv.api, r.json()["id"], "completed")
    r = iv.api.post("/v1/videos", json={"model": "t2v", "prompt": "Rain."})
    wait_status(iv.owner, iv.api, r.json()["id"], "completed")
    assert start(iv.api, "t2v", a).status_code == 400
    rows = usage_by_model(iv.owner)
    t = rows["ti2v"]
    assert (t["requests"], t["errors"], t["media_items"], t["bytes_in"]) == (1, 0, 1, len(a)) and t["media_ms"] > 0
    w = rows["t2v"]
    assert (w["requests"], w["errors"], w["media_items"], w["bytes_in"]) == (2, 1, 0, 0)


def test_fy_i2v_009_the_estimate(iv):
    """FY-I2V-009: Wan2.2-TI2V-5B at its own size (1280x704, 5 s at 24 fps) is estimated at about 33 GiB on the card,
    the same with or without image_input in its entry (a start image adds no tokens; its encode runs before the
    denoiser); the estimate says it is not yet measured with a start image; a text-only model's estimate does not
    mention it."""
    r = recipes.builtin_recipes()["vllm-omni-video"][0]
    params = {k: p.default for k, p in r.params.items()} | {"gpus": 1}
    with_key = estimate(ti2v_meta(image_input="optional"), r, params, device="gpu", gpu_context=512 * MIB)
    old = estimate(ti2v_meta(), r, params, device="gpu", gpu_context=512 * MIB)
    assert with_key.per_gpu == old.per_gpu and 32 * GIB < with_key.per_gpu < 34 * GIB
    assert any("Not yet measured with a start image" in n for n in with_key.notes)
    t2v = estimate(wan_meta(), r, params, device="gpu", gpu_context=512 * MIB)
    assert not any("start image" in n for n in t2v.notes)
    # The deployment's own estimate, as stored.
    dep = next(d for d in iv.owner.get("/api/deployments").json()["deployments"] if d["name"] == "ti2v")
    assert 32 * GIB < dep["estimate"]["per_gpu"] < 34 * GIB


def test_fy_i2v_010_the_playground_options(iv):
    """FY-I2V-010: the Playground's video options say whether a start image is optional, required or refused, and its
    largest side; over a route's deployments: required only when every one needs it, optional when any takes it."""
    def opts(model):
        return iv.owner.get(f"/api/playground/videos/options?model={model}").json()

    assert (opts("ti2v")["image_input"], opts("ti2v")["max_input_side"]) == ("optional", 2048)
    assert opts("i2v")["image_input"] == "required"
    assert (opts("t2v")["image_input"], opts("t2v")["max_input_side"]) == ("none", 0)
    F = videogen.Films
    assert videogen.start_image([F(image_input="required"), F(image_input="required", max_input_side=4096)]) == \
        ("required", 4096)
    assert videogen.start_image([F(image_input="required"), F(image_input="none")]) == ("optional", 2048)
    assert videogen.start_image([F(image_input="optional", max_input_side=1024), F()]) == ("optional", 1024)
    assert videogen.start_image([F(), F()]) == ("none", 0)


def test_fy_i2v_011_nothing_of_the_start_image_is_kept(iv, caplog):
    """FY-I2V-011: the start image is not logged or stored: no byte of it in the log or in the job's row."""
    import logging

    from tests.test_video_generation import job

    marker = b"FORGEYARD-START-IMAGE-MARKER"
    a = png(320, 192, 80) + marker   # bytes after the PNG's end are still sent, byte for byte
    with caplog.at_level(logging.DEBUG):
        r = start(iv.api, "ti2v", a)
        assert r.status_code == 200
        wait_status(iv.owner, iv.api, r.json()["id"], "completed")
    assert marker.decode() not in caplog.text
    row = job(r.json()["id"])
    assert all(marker.decode() not in str(getattr(row, c.name)) for c in row.__table__.columns)


def test_fy_i2v_012_the_openai_sdk_sends_a_start_image(iv):
    """FY-I2V-012: the official OpenAI SDK's videos.create(input_reference=<file>) is answered with its Video type; the
    engine gets the file as sent; a model that needs a start image answers the SDK's BadRequestError without one."""
    openai = pytest.importorskip("openai")
    import warnings

    c = openai.OpenAI(base_url="http://localhost/v1", api_key=iv.key, http_client=iv.api, max_retries=0)
    a = png(320, 192, 90)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)   # the SDK warns that OpenAI's own video API shut down
        v = c.videos.create(model="ti2v", prompt="The cat walks.", input_reference=("s.png", a, "image/png"))
        assert v.status == "queued" and v.model == "ti2v" and v.size == "1280x704"
        assert [d for _, _, d in posts(iv, "ti2v")[-1]["files"]] == [a]
        with pytest.raises(openai.BadRequestError) as e:
            c.videos.create(model="i2v", prompt="The cat walks.")
    assert e.value.code == "input_reference_required"


def test_fy_i2v_013_what_the_model_does_comes_before_the_image(iv):
    """FY-I2V-013: once the key may use the model, what the model does with a start image is decided before the image
    is checked or its URL fetched: a text-to-video model answers input_reference_not_supported to a start image URL
    without fetching it, and to a HEIC file without reading it as an image; an image-to-video model answers
    input_reference_required to a request without one. A model that takes start images fetches the URL. No refused
    request reaches an engine."""
    url = f"{iv.media.url}/img.png"
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
    for r, code in [(iv.api.post("/v1/videos", json={"model": "t2v", "prompt": "x", "input_reference": {"image_url": url}}),
                     "input_reference_not_supported"),
                    (start(iv.api, "t2v", heic, "image/heic"), "input_reference_not_supported"),
                    (start(iv.api, "t2v", claims(60000, 60000)), "input_reference_not_supported"),
                    (iv.api.post("/v1/videos", json={"model": "i2v", "prompt": "x"}), "input_reference_required"),
                    (iv.api.post("/v1/videos", data={"model": "i2v", "prompt": "x"}), "input_reference_required")]:
        assert r.status_code == 400 and err(r)["code"] == code and err(r)["param"] == "input_reference", r.text
    assert iv.media.hits == [] and posts(iv, "t2v") == [] and posts(iv, "i2v") == []
    r = iv.api.post("/v1/videos", json={"model": "i2v", "prompt": "x", "input_reference": {"image_url": url}})
    assert r.status_code == 200, r.text
    assert iv.media.hits == ["/img.png"] and [d for _, _, d in posts(iv, "i2v")[-1]["files"]] == [PNG]
    # A route whose deployments differ: a start image goes on while any of them takes one.
    image = videogen.check({"prompt": "x", "input_reference": {"image_url": url}})
    videogen.offered("m", image, [videogen.Films(), videogen.Films(image_input="optional")])
    with pytest.raises(GatewayError) as e:
        videogen.offered("m", image, [videogen.Films(), videogen.Films()])
    assert e.value.code == "input_reference_not_supported"
