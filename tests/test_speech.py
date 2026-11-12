"""Text to speech (1.3): /v1/audio/speech through the gateway with three engines (vLLM-Omni Qwen3-TTS,
llama.cpp TTS GGUFs, speaches Kokoro), checked against fake engines: the request checks, voice cloning
refused, what each deployment gets filled in, the audio passed through as it is made (binary and SSE),
characters and audio milliseconds in usage, the input cap, keys, model allowlists, rpm and tpm, no cloud,
voices, the recipes, their rendering and estimate, the inspector and the recommended list (FY-TTS-*)."""

from __future__ import annotations

import base64
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Optional

import httpx
import pytest

from forgeyard.db import Accelerator, Deployment, new_session
from forgeyard.gateway import speech
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.usage import UNITS, recorder
from forgeyard.models import recipes, recommended as rec, sources
from forgeyard.models.estimate import CODEC_LABEL, estimate
from forgeyard.models.inspect import config_summary, voices_of
from tests.cloud_helpers import FakeCloud, add_provider, deployment_id, model_id, set_tiers
from tests.gateway_helpers import LiveServer, api_client, make_ready, new_key
from tests.model_helpers import FIX, add_catalog
from tests.test_scheduler import MODELS, deploy, replica_row
from tests.workload_helpers import make_node

KEYS = MODELS + ("replica_keys",)
GIB = 1024**3
RATE = 24000
PCM = b"".join(struct.pack("<h", (i * 37) % 2000 - 1000) for i in range(RATE // 2))   # 0.5 s of 16-bit mono


def wav(pcm: bytes = PCM, rate: int = RATE, streamed: bool = False) -> bytes:
    size = 0xFFFFFFFF if streamed else len(pcm)
    riff = 0xFFFFFFFF if streamed else 36 + len(pcm)
    return (b"RIFF" + struct.pack("<I", riff) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", size) + pcm)


def mp3_frames(n: int = 25) -> bytes:
    """MPEG-2 layer III frames at 24 kHz and 64 kbps: 192 bytes and 24 ms each."""
    head = bytes([0xFF, 0xF3, 0x84, 0xC4])  # MPEG2, layer III, no CRC; 64 kbps; 24 kHz; no padding
    return b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"\0" * 10 + (head + b"\0" * 188) * n


class FakeSpeech:
    """A speech engine at http://127.0.0.1:<port>: POST /v1/audio/speech answers 0.5 s of audio per
    chunk (`chunks` of them, `delay` apart) in the format asked; with stream_format sse the OpenAI events.
    GET `voices_path` lists `voices` (vLLM-Omni's or speaches' shape, or llama.cpp's /props). Every
    path needs the replica key."""

    def __init__(self, secret: str = "tts-secret", *, voices=("vivian", "ryan", "uncle_fu"),
                 voices_path: str = "/v1/audio/voices", shape: str = "omni", chunks: int = 2, delay: float = 0.0,
                 tokens: bool = True):
        self.secret, self.voices, self.voices_path, self.shape = secret, voices, voices_path, shape
        self.chunks, self.delay, self.tokens = chunks, delay, tokens
        self.status: Optional[int] = None
        self.seen: list[dict] = []
        self.voice_calls = 0
        self.disconnects = 0
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body: bytes, ctype: str, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self) -> bool:
                if engine.secret and self.headers.get("Authorization") != f"Bearer {engine.secret}":
                    self._send(401, b'{"error":"Unauthorized"}', "application/json")
                    return False
                return True

            def do_GET(self):
                if self.path == "/health":
                    return self._send(200, b"{}", "application/json")
                if not self._authorised():
                    return None
                if self.path == engine.voices_path:
                    engine.voice_calls += 1
                    if engine.shape == "speaches":
                        data = {"voices": [{"name": v, "language": "en-us", "gender": "female"} for v in engine.voices],
                                "object": "list"}
                    elif engine.shape == "props":
                        data = {"model_path": "x"} if not engine.voices else {"voices": list(engine.voices)}
                    else:
                        data = {"voices": list(engine.voices), "uploaded_voices": [{"name": "someone"}]}
                    return self._send(200, json.dumps(data).encode(), "application/json")
                self._send(404, b"{}", "application/json")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not self._authorised():
                    return None
                body = json.loads(raw or b"{}")
                engine.seen.append({"path": self.path, "body": body})
                if engine.status:
                    return self._send(engine.status, json.dumps({"error": {"message": "engine says no",
                                                                           "code": engine.status}}).encode(),
                                      "application/json")
                fmt, sf = body.get("response_format", "wav"), body.get("stream_format")
                tok = {"x-vllm-omni-input-tokens": "12", "x-vllm-omni-output-tokens": "34"} if engine.tokens else {}
                if sf == "sse":
                    return self._sse(fmt)
                if fmt == "pcm":
                    return self._stream([PCM] * engine.chunks, "audio/pcm;rate=24000;encoding=signed-int;bits=16",
                                        {"x-audio-sample-rate": str(RATE), "x-audio-channels": "1", **tok})
                if fmt == "mp3":
                    return self._send(200, mp3_frames(), "audio/mpeg", tok)
                if sf == "audio":
                    first = wav(PCM, streamed=True)
                    return self._stream([first] + [PCM] * (engine.chunks - 1), "audio/wav", tok)
                self._send(200, wav(PCM * engine.chunks), "audio/wav", tok)

            def _stream(self, parts: list[bytes], ctype: str, headers: dict):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    for i, p in enumerate(parts):
                        self.wfile.write(p)
                        self.wfile.flush()
                        if engine.delay and i < len(parts) - 1:
                            time.sleep(engine.delay)
                except (BrokenPipeError, ConnectionResetError):
                    engine.disconnects += 1
                self.close_connection = True

            def _sse(self, fmt: str):
                events = [{"type": "speech.audio.delta", "audio": base64.b64encode(PCM).decode(), "response_format": fmt,
                           "sample_rate": RATE, "channels": 1} for _ in range(engine.chunks)]
                events.append({"type": "speech.audio.done", "usage": {"input_tokens": 12, "output_tokens": 34,
                                                                       "total_tokens": 46}})
                self._stream([f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events],
                             "text/event-stream", {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def bodies(self) -> list[dict]:
        return [s["body"] for s in self.seen if s["path"] == "/v1/audio/speech"]


def qwen_tts_meta(**over) -> dict:
    config = json.loads((FIX / "qwen3-tts-0.6b-customvoice.config.json").read_text())
    meta = {"kind": "hf", "repo": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "ref": "main", "revision": "8" * 40,
            "format": "safetensors", "config": config_summary(config), "voices": voices_of(config), "modalities": [],
            "license": "apache-2.0",
            "weights": {"params": {"BF16": 905_000_000}, "bytes": 1_811_626_576, "linear_params": 800_000_000,
                        "other_params": 105_000_000},
            "files": [{"path": "model.safetensors", "size": 1_811_626_576, "sha256": "a" * 64},
                      {"path": "speech_tokenizer/model.safetensors", "size": 682_293_092, "sha256": "b" * 64}],
            "size_bytes": 2_493_919_668}
    meta.update(over)
    return meta


def tts_gguf_meta(seeded_g: dict) -> dict:
    return {"kind": "hf", "repo": "ggml-org/chatterbox-turbo-GGUF", "ref": "main", "revision": "6" * 40,
            "format": "gguf", "file": "chatterbox-turbo-Q8_0.gguf", "gguf": seeded_g, "modalities": [],
            "mmproj": {"path": "mmproj-chatterbox-turbo-F16.gguf", "size": 555_371_392, "projector": None},
            "files": [{"path": "chatterbox-turbo-Q8_0.gguf", "size": 481_791_712, "sha256": "c" * 64},
                      {"path": "mmproj-chatterbox-turbo-F16.gguf", "size": 555_371_392, "sha256": "d" * 64}],
            "size_bytes": 1_037_163_104, "license": "mit"}


def kokoro_meta() -> dict:
    return {"kind": "hf", "repo": "speaches-ai/Kokoro-82M-v1.0-ONNX", "ref": "main", "revision": "d" * 40,
            "format": "onnx", "weights_bytes": 353_746_785,
            "files": [{"path": "README.md", "size": 280, "oid": "e" * 40},
                      {"path": "model.onnx", "size": 325_532_387, "sha256": "f" * 64},
                      {"path": "voices.bin", "size": 28_214_398, "sha256": "1" * 64}], "size_bytes": 353_747_065}


@pytest.fixture()
def tts(owner):
    """Three speech deployments on fake engines: `qwen` (vllm-omni-tts on the A100's card #4, voices
    from GET /v1/audio/voices), `box` (llama-cpp, a TTS GGUF with its projector, on a CPU machine; voices
    from /props) and `kokoro` (speaches-tts on the CPU; English only); a chat deployment `chat`; a key."""
    from tests.test_scheduler import seed_catalog

    seed_catalog()
    db = new_session()
    try:
        from forgeyard.db import CatalogModel
        seeded_g = dict(db.query(CatalogModel).filter_by(name="qwen3-1.7b-gguf").one().meta["gguf"])
    finally:
        db.close()
    add_catalog("qwen3-tts-0.6b", qwen_tts_meta(), task="tts", fmt="safetensors",
                source="hf://Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
    add_catalog("chatterbox-turbo", tts_gguf_meta(seeded_g), task="tts", fmt="gguf",
                source="hf://ggml-org/chatterbox-turbo-GGUF")
    add_catalog("kokoro", kokoro_meta(), task="tts", fmt="onnx", source="hf://speaches-ai/Kokoro-82M-v1.0-ONNX")
    cpu = make_node("cpu-a", features=KEYS)
    gpu = make_node("a100", allowlist=[4], features=KEYS)
    engines = {"qwen": FakeSpeech("s-qwen"),
               "box": FakeSpeech("s-box", voices=("default",), voices_path="/props", shape="props", tokens=False),
               "kokoro": FakeSpeech("s-kokoro", voices=("af_heart", "am_adam"), shape="speaches", tokens=False)}
    deploy(owner, "qwen", "qwen3-tts-0.6b", "vllm-omni-tts", wait=True)
    deploy(owner, "box", "chatterbox-turbo", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    deploy(owner, "kokoro", "kokoro", "speaches-tts", params={"gpus": 0}, wait=True)
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    make_ready("qwen", [(gpu, engines["qwen"].port, engines["qwen"].secret)])
    for n in ("box", "kokoro"):
        make_ready(n, [(cpu, engines[n].port, engines[n].secret)])
    key = new_key(owner, "laptop")["key"]
    speech.forget_voices()
    ns = SimpleNamespace(owner=owner, cpu=cpu, gpu=gpu, engines=engines, key=key, api=api_client(owner, key))
    yield ns
    for e in engines.values():
        e.close()
    speech.forget_voices()


def say(c, model: str, text: str = "Hello there.", **extra):
    return c.post("/v1/audio/speech", json={"model": model, "input": text, **extra})


def err(r) -> dict:
    body = r.json()
    assert set(body) == {"error"}, body
    return body["error"]


def usage_total(owner) -> dict:
    recorder.flush()
    return owner.get("/api/usage?everyone=true").json()["total"]


# ------------------------------------------------------------------ recipes
def test_fy_tts_001_three_engines_serve_tts():
    """FY-TTS-001: vllm-omni-tts (NVIDIA, Qwen3-TTS), llama-cpp (chat and tts) and speaches-tts (onnx,
    English only) serve tts as openai-speech; the speech settings are checked when a recipe is read."""
    b = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
    omni, llama, kokoro = b["vllm-omni-tts"], b["llama-cpp"], b["speaches-tts"]
    assert omni.api == {"tts": "openai-speech"} and omni.tts.stream_formats == ["audio", "sse"]
    assert [v.name for v in omni.variants] == ["cuda"] and omni.variants[0].image.startswith("vllm/vllm-omni:v0.32.0@sha256:")
    assert omni.variants[0].entrypoint == ["vllm", "serve"] and omni.api_key_env == "VLLM_API_KEY"
    assert llama.tasks == ["chat", "tts"] and llama.api["tts"] == "openai-speech"
    assert llama.tts.voices == "/props" and llama.tts.language_field == "lang" and llama.tts.frames_field == "n_predict"
    assert set(llama.tts.formats) == {"wav", "pcm"} and llama.tts.stream_formats == ["audio"]
    assert kokoro.formats == ["onnx"] and kokoro.tts.cjk is False and kokoro.upstream_model == "repo"
    assert "opus" not in kokoro.tts.formats and kokoro.tts.default_format == "mp3" and kokoro.api_key_env == "API_KEY"
    # The same speaches images as faster-whisper: one image for speech both ways.
    whisper = b["faster-whisper"]
    assert [v.image for v in kokoro.variants] == [v.image for v in whisper.variants]
    good = (FIX.parent.parent.parent / "forgeyard" / "recipes" / "speaches-tts.yaml").read_text()
    for bad, why in [(good.replace("default_format: mp3", "default_format: opus"), "default_format"),
                     (good.replace("api: {tts: openai-speech}", "api: {tts: openai-audio}"), "openai-speech"),
                     (good.replace("voices: /v1/audio/voices", "voices: v1 voices"), "path"),
                     (good.replace("tasks: [tts]", "tasks: [asr]").replace("api: {tts: openai-speech}",
                                                                           "api: {asr: openai-audio}"), "tts")]:
        with pytest.raises(recipes.RecipeError, match=why):
            recipes.parse_recipe(bad)


# ------------------------------------------------------------------ the request
@pytest.mark.parametrize("body,code", [
    ({}, "invalid_request"),
    ({"input": "   "}, "invalid_request"),
    ({"input": 7}, "invalid_request"),
    ({"input": "hi", "ref_audio": "https://example.com/me.wav"}, "voice_cloning_not_supported"),
    ({"input": "hi", "ref_text": "what I said"}, "voice_cloning_not_supported"),
    ({"input": "hi", "speaker_ref_b64": "UklGRg=="}, "voice_cloning_not_supported"),
    ({"input": "hi", "speaker_embedding": [0.1, 0.2]}, "voice_cloning_not_supported"),
    ({"input": "hi", "task_type": "Base"}, "voice_cloning_not_supported"),
    ({"input": "hi", "voice": "data:audio/wav;base64,UklGRg=="}, "voice_cloning_not_supported"),
    ({"input": "hi", "voice": "https://example.com/me.wav"}, "voice_cloning_not_supported"),
    ({"input": "hi", "voice": "x" * 101}, "voice_cloning_not_supported"),
    ({"input": "hi", "voice": {"id": "vivian", "audio": "UklGRg=="}}, "voice_cloning_not_supported"),
    ({"input": "hi", "voice": 3}, "invalid_request"),
    ({"input": "hi", "response_format": "ogg"}, "unsupported_response_format"),
    ({"input": "hi", "stream_format": "chunks"}, "invalid_request"),
    ({"input": "hi", "speed": 5}, "invalid_request"),
    ({"input": "hi", "speed": True}, "invalid_request"),
    ({"input": "hi", "stream": "yes"}, "invalid_request"),
])
def test_fy_tts_002_requests_are_checked_and_cloning_refused(body, code):
    """FY-TTS-002: an empty or missing input, any field that carries a reference recording, a voice
    given as audio data, a link or an object with more than its id, Qwen3-TTS's Base task, and values
    outside OpenAI's shape are 400 before anything is sent."""
    with pytest.raises(GatewayError) as e:
        speech.check(body)
    assert e.value.status == 400 and e.value.code == code


def test_fy_tts_003_what_each_deployment_is_given():
    """FY-TTS-003: the engine's default format when none is asked; an OpenAI voice the model lacks (or
    none) becomes the recipe's default voice, else the first the engine lists, and a model without voices
    gets none; a voice it has is kept; `{"id": …}` becomes the name; llama.cpp's lang from the script and
    n_predict from the length (at most the context); `stream: true` means stream_format audio."""
    b = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
    omni = speech.speaks(b["vllm-omni-tts"], {"context": 4096}, {"voices": ["serena", "vivian"]})
    llama = speech.speaks(b["llama-cpp"], {"context": 4096})
    kokoro = speech.speaks(b["speaches-tts"], {})
    ask = speech.check({"input": "Hello", "voice": "alloy"})
    out, chosen = speech.body_for({"input": "Hello", "voice": "alloy"}, ask, omni, None)
    assert out["voice"] == "vivian" and chosen == "vivian" and out["response_format"] == "wav"
    out, chosen = speech.body_for({"input": "Hello", "voice": "alloy"}, ask, kokoro, ["am_adam", "bf_emma"])
    assert out["voice"] == "am_adam" and chosen == "am_adam" and out["response_format"] == "mp3"
    out, chosen = speech.body_for({"input": "Hello", "voice": "alloy"}, ask, llama, [])
    assert "voice" not in out and chosen == "default"
    ask = speech.check({"input": "Hello", "voice": "ryan", "response_format": "pcm"})
    out, chosen = speech.body_for({"input": "Hello", "voice": "ryan", "response_format": "pcm"}, ask, omni, ["ryan"])
    assert out["voice"] == "ryan" and chosen is None and out["response_format"] == "pcm"
    ask = speech.check({"input": "Hello", "voice": {"id": "default"}, "stream": True})
    out, _ = speech.body_for({"input": "Hello", "voice": {"id": "default"}, "stream": True}, ask, llama, ["default"])
    assert out["voice"] == "default" and out["stream_format"] == "audio" and "stream" not in out
    text = "今天天氣很好，我們去公園散步吧。"
    out, _ = speech.body_for({"input": text}, speech.check({"input": text}), llama, [])
    assert out["lang"] == "zh" and out["n_predict"] == 512
    assert speech.language_of("こんにちは") == "ja" and speech.language_of("안녕하세요") == "ko"
    assert speech.language_of("Hello") is None
    out, _ = speech.body_for({"input": text * 20, "lang": "en", "n_predict": 99},
                             speech.check({"input": text * 20, "lang": "en", "n_predict": 99}), llama, [])
    assert out["lang"] == "en" and out["n_predict"] == 99      # the client's own values win
    assert speech.frames_for(300, 4096) == 3000 and speech.frames_for(1000, 4096) == 4096
    assert speech.frames_for(10, None) == 512 and speech.frames_for(10_000, None) == 16384
    out, _ = speech.body_for({"input": "Hi"}, speech.check({"input": "Hi"}), omni, None)
    assert "lang" not in out and "n_predict" not in out and "language" not in out


def test_fy_tts_004_the_audio_is_measured_in_every_format_it_can_be():
    """FY-TTS-004: wav from its header (also a streamed one of unknown size), pcm from the rate the
    answer states (header or Content-Type, else the asked or the recipe's rate), mp3 and aac by their
    frames, flac from STREAMINFO, opus from the last Ogg page; anything else is not guessed."""
    def ms(fmt, data, ctype="", headers=None, **kw):
        m = speech.Meter(fmt, ctype, headers or {}, **kw)
        for i in range(0, len(data), 1000):
            m.feed(data[i:i + 1000])
        return m.ms()

    assert ms("wav", wav()) == 500 and ms("wav", wav(streamed=True) + PCM) == 1000
    assert ms("wav", b"not a wav") is None
    assert ms("pcm", PCM * 2, headers={"x-audio-sample-rate": "24000"}) == 1000
    assert ms("pcm", PCM, "audio/pcm;rate=12000;encoding=signed-int;bits=16") == 1000
    assert ms("pcm", PCM * 2, "audio/pcm;rate=24000;encoding=float;bits=32") == 500
    assert ms("pcm", PCM, "audio/pcm", pcm_rate=22050) == int(len(PCM) * 1000 / 44100)
    assert ms("pcm", PCM, "audio/pcm", asked_rate=48000) == 250
    assert ms("mp3", mp3_frames(25)) == 600 and ms("mp3", mp3_frames(5) + mp3_frames(5)) == 240
    # rate 24000 = 0x05DC0: bytes 10-12 hold its 20 bits; 48,000 samples.
    streaminfo = bytes(10) + bytes([0x05, 0xDC, 0x00, 0x00]) + struct.pack(">I", 48000) + bytes(16)
    assert ms("flac", b"fLaC" + b"\x00\x00\x00\x22" + streaminfo) == 2000
    page = lambda granule: b"OggS" + b"\x00\x00" + struct.pack("<q", granule) + bytes(20)  # noqa: E731
    head = b"OpusHead" + b"\x01\x01" + struct.pack("<H", 312) + bytes(10)
    assert ms("opus", page(0) + head + page(0) + page(48312)) == 1000
    adts = bytes([0xFF, 0xF1, (6 << 2) | 0x40, 0x80, (100 >> 3) & 0xFF, ((100 & 7) << 5) | 0x1F, 0xFC]) + bytes(93)
    assert ms("aac", adts * 24) == 1024
    assert ms("mp3", b"garbage" * 10) is None and ms("flac", b"nope") is None


# ------------------------------------------------------------------ through the gateway
def test_fy_tts_005_audio_passes_through_and_is_counted(tts):
    """FY-TTS-005: the official request shape gets the engine's audio back unchanged; the engine is sent
    the upstream model, its default format and a voice for OpenAI's `alloy` (named in X-Forgeyard-Voice),
    never `stream`; usage counts the characters and the milliseconds, and the tokens vLLM-Omni reports."""
    r = say(tts.api, "qwen", "Hello there, this is a test.", voice="alloy")
    assert r.status_code == 200, r.text
    assert r.content == wav(PCM * 2) and r.headers["content-type"] == "audio/wav"
    assert r.headers["x-forgeyard-voice"] == "vivian" and r.headers["x-forgeyard-target"] == "local:qwen#0"
    assert r.headers["x-vllm-omni-input-tokens"] == "12"
    [body] = tts.engines["qwen"].bodies()
    assert body == {"model": "qwen", "input": "Hello there, this is a test.", "voice": "vivian",
                    "response_format": "wav"}
    total = usage_total(tts.owner)
    assert total["characters"] == 28 and total["media_ms"] == 1000 and total["requests"] == 1
    assert total["prompt_tokens"] == 12 and total["completion_tokens"] == 34
    assert all(total[u] == 0 for u in UNITS if u not in ("characters", "media_ms"))
    # A voice the model has is sent as it is, without the header.
    r = say(tts.api, "qwen", "Hi", voice="ryan", response_format="pcm")
    assert r.status_code == 200 and "x-forgeyard-voice" not in r.headers and r.content == PCM * 2
    assert usage_total(tts.owner)["media_ms"] == 2000
    # The voices are asked once and kept.
    assert tts.engines["qwen"].voice_calls == 1


def test_fy_tts_006_each_engine_gets_its_own_body(tts):
    """FY-TTS-006: llama.cpp gets `lang` from Chinese text and an `n_predict` cap and no OpenAI voice
    (its model has `default`: that one); speaches gets mp3 by default, its upstream model is the
    repository, and Kokoro's af_heart for OpenAI voices."""
    text = "今天天氣很好"
    r = say(tts.api, "box", text, voice="nova")
    assert r.status_code == 200 and r.headers["x-forgeyard-voice"] == "default"
    [b] = tts.engines["box"].bodies()
    assert b["lang"] == "zh" and b["n_predict"] == 512 and b["voice"] == "default" and b["response_format"] == "wav"
    r = say(tts.api, "kokoro", "Good morning.", voice="alloy")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    [b] = tts.engines["kokoro"].bodies()
    assert b["model"] == "speaches-ai/Kokoro-82M-v1.0-ONNX" and b["voice"] == "af_heart" and b["response_format"] == "mp3"
    assert usage_total(tts.owner)["media_ms"] == 600 + int(len(PCM) * 2 * 1000 / (RATE * 2))


def test_fy_tts_007_streaming_reaches_the_client_as_it_is_made(tts):
    """FY-TTS-007: with stream_format audio the first audio arrives while the engine is still speaking,
    and the whole answer is the engine's; a client that goes away mid-stream closes the engine's request
    and is counted as 499 with no characters."""
    live = LiveServer()
    try:
        tts.engines["qwen"].chunks, tts.engines["qwen"].delay = 4, 0.4
        h = {"Authorization": f"Bearer {tts.key}"}
        t0 = time.monotonic()
        first, got = None, b""
        with httpx.stream("POST", f"{live.url}/v1/audio/speech", headers=h, timeout=20,
                          json={"model": "qwen", "input": "Streaming test.", "voice": "ryan", "response_format": "pcm",
                                "stream_format": "audio"}) as r:
            assert r.status_code == 200
            for chunk in r.iter_bytes():
                if first is None:
                    first = time.monotonic() - t0
                got += chunk
        total = time.monotonic() - t0
        assert got == PCM * 4 and first < 0.8 < 1.1 < total, (first, total)
        assert tts.engines["qwen"].bodies()[-1]["stream_format"] == "audio"
        tts.engines["qwen"].chunks, tts.engines["qwen"].delay = 30, 0.2
        with httpx.stream("POST", f"{live.url}/v1/audio/speech", headers=h, timeout=20,
                          json={"model": "qwen", "input": "Cut me off.", "voice": "ryan", "response_format": "pcm",
                                "stream_format": "audio"}) as r:
            next(r.iter_bytes())
        deadline = time.monotonic() + 10
        while tts.engines["qwen"].disconnects == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert tts.engines["qwen"].disconnects == 1
        recorder.flush()
        rows = tts.owner.get("/api/usage?everyone=true&group=model").json()["rows"]
        q = next(x for x in rows if x["id"] == "qwen")
        assert q["requests"] == 2 and q["characters"] == len("Streaming test.") + len("Cut me off.")
    finally:
        live.close()


def test_fy_tts_008_sse_passes_through_with_its_usage(tts):
    """FY-TTS-008: stream_format sse (vLLM-Omni) passes OpenAI's speech.audio.* events through; the
    audio milliseconds come from the deltas' size and the tokens from speech.audio.done."""
    r = say(tts.api, "qwen", "Events.", voice="ryan", response_format="pcm", stream_format="sse")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    assert [e["type"] for e in events] == ["speech.audio.delta"] * 2 + ["speech.audio.done"]
    total = usage_total(tts.owner)
    assert total["media_ms"] == 1000 and total["characters"] == 7 and total["completion_tokens"] == 34


def test_fy_tts_009_what_an_engine_cannot_do_is_refused_first(tts):
    """FY-TTS-009: mp3 or SSE from llama.cpp, opus from speaches and Chinese to speaches are 400 with
    what the model can do, and nothing reaches an engine; the engine's own 400 is passed on."""
    r = say(tts.api, "box", response_format="mp3")
    assert err(r)["code"] == "unsupported_response_format" and "wav, pcm" in err(r)["message"]
    r = say(tts.api, "box", stream_format="sse")
    assert r.status_code == 400 and err(r)["code"] == "stream_format_not_supported" and "audio" in err(r)["message"]
    assert err(say(tts.api, "kokoro", response_format="opus"))["code"] == "unsupported_response_format"
    r = say(tts.api, "kokoro", "你好，世界")
    assert err(r)["code"] == "language_not_supported" and "Qwen3-TTS" in err(r)["message"]
    assert not any(e.seen for e in tts.engines.values())
    tts.engines["qwen"].status = 400
    r = say(tts.api, "qwen", voice="ryan")
    assert r.status_code == 400 and r.json()["error"]["message"] == "engine says no"
    assert usage_total(tts.owner)["characters"] == 0


def test_fy_tts_010_the_input_cap_is_per_deployment(tts):
    """FY-TTS-010: max_input_chars caps the input (400 input_too_long); 0 means a quarter of the context
    (1,024 at 4,096) for engines that hold the text and the audio in one context, OpenAI's 4,096 for
    speaches; a deployment's own value wins."""
    assert err(say(tts.api, "qwen", "x" * 1025))["code"] == "input_too_long"
    assert say(tts.api, "qwen", "x" * 1024, voice="ryan").status_code == 200
    assert say(tts.api, "kokoro", "x" * 4096).status_code == 200
    r = say(tts.api, "kokoro", "x" * 4097)
    assert r.status_code == 400 and err(r)["code"] == "input_too_long" and "at most 4096" in err(r)["message"]
    dep = tts.owner.get("/api/deployments").json()["deployments"]
    kid = next(d["id"] for d in dep if d["name"] == "kokoro")
    db = new_session()
    try:
        d = db.get(Deployment, kid)
        d.params = {**d.params, "max_input_chars": 10}
        db.commit()
    finally:
        db.close()
    assert err(say(tts.api, "kokoro", "x" * 11))["code"] == "input_too_long"
    assert say(tts.api, "kokoro", "x" * 10).status_code == 200


def test_fy_tts_011_voice_cloning_never_reaches_an_engine(tts):
    """FY-TTS-011: reference audio in the body, and uploading or deleting voices, are 400
    voice_cloning_not_supported for every model; nothing is sent and nothing counted."""
    for model in ("qwen", "box", "kokoro"):
        r = say(tts.api, model, ref_audio="data:audio/wav;base64,UklGRg==", ref_text="hi")
        assert r.status_code == 400 and err(r)["code"] == "voice_cloning_not_supported"
        assert err(say(tts.api, model, speaker_ref_b64="UklGRg=="))["code"] == "voice_cloning_not_supported"
    r = tts.api.post("/v1/audio/voices", files={"audio_sample": ("me.wav", wav())}, data={"name": "me", "consent": "x"})
    assert r.status_code == 400 and err(r)["code"] == "voice_cloning_not_supported"
    assert err(tts.api.delete("/v1/audio/voices/someone"))["code"] == "voice_cloning_not_supported"
    assert api_client(tts.owner).post("/v1/audio/voices").status_code == 401
    assert not any(e.seen for e in tts.engines.values())
    assert usage_total(tts.owner)["characters"] == 0


def test_fy_tts_012_voices_are_listed_in_one_shape(tts):
    """FY-TTS-012: GET /v1/audio/voices?model= asks a replica (vLLM-Omni without the uploaded ones,
    speaches' objects, llama.cpp's /props), kept a while; the key's model allowlist applies; a chat
    model is the wrong endpoint; the playground lists them too."""
    r = tts.api.get("/v1/audio/voices", params={"model": "qwen"})
    assert r.status_code == 200, r.text
    assert r.json() == {"object": "list", "model": "qwen", "default": "vivian",
                        "voices": [{"id": v, "name": v} for v in ("vivian", "ryan", "uncle_fu")],
                        "formats": ["wav", "pcm", "flac", "mp3", "opus"], "default_format": "wav",
                        "stream_formats": ["audio", "sse"], "max_input_chars": 1024}
    tts.api.get("/v1/audio/voices", params={"model": "qwen"})
    assert tts.engines["qwen"].voice_calls == 1
    k = tts.api.get("/v1/audio/voices", params={"model": "kokoro"}).json()
    assert k["voices"][0] == {"id": "af_heart", "name": "af_heart", "language": "en-us", "gender": "female"}
    assert k["default"] == "af_heart"
    assert tts.api.get("/v1/audio/voices", params={"model": "box"}).json()["voices"] == [{"id": "default", "name": "default"}]
    assert err(tts.api.get("/v1/audio/voices"))["code"] == "invalid_request"
    assert err(tts.api.get("/v1/audio/voices", params={"model": "chat"}))["code"] == "wrong_endpoint"
    assert err(tts.api.get("/v1/audio/voices", params={"model": "nope"}))["code"] == "model_not_found"
    narrow = new_key(tts.owner, "narrow", models=["box"])["key"]
    r = api_client(tts.owner, narrow).get("/v1/audio/voices", params={"model": "qwen"})
    assert r.status_code == 403 and err(r)["code"] == "model_not_allowed"
    assert api_client(tts.owner).get("/v1/audio/voices", params={"model": "qwen"}).status_code == 401
    r = tts.owner.get("/api/playground/audio/voices", params={"model": "kokoro"})
    assert r.status_code == 200 and r.json()["default"] == "af_heart"
    r = tts.owner.post("/api/playground/audio/speech", json={"model": "kokoro", "input": "From the console."},
                       headers={"X-Forgeyard": "1"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    recorder.flush()
    by = {g["id"]: g for g in tts.owner.get("/api/usage?everyone=true&group=source").json()["rows"]}
    assert by["console"]["characters"] == len("From the console.")


def test_fy_tts_013_keys_allowlists_and_limits_apply(tts):
    """FY-TTS-013: no key is 401; a key without the model is 403; rpm refuses the next request; tpm counts
    one token per character, held before sending (15 + 15 > 20 is refused)."""
    assert api_client(tts.owner).post("/v1/audio/speech", json={"model": "qwen", "input": "hi"}).status_code == 401
    narrow = new_key(tts.owner, "narrow", models=["box"])["key"]
    assert err(say(api_client(tts.owner, narrow), "qwen"))["code"] == "model_not_allowed"
    rpm = new_key(tts.owner, "rpm", rpm=1)["key"]
    assert say(api_client(tts.owner, rpm), "kokoro").status_code == 200
    r = say(api_client(tts.owner, rpm), "kokoro")
    assert r.status_code == 429 and err(r)["code"] == "rate_limit_exceeded" and "requests" in err(r)["message"]
    tpm = new_key(tts.owner, "tpm", tpm=20)["key"]
    assert say(api_client(tts.owner, tpm), "kokoro", "x" * 15).status_code == 200
    r = say(api_client(tts.owner, tpm), "kokoro", "y" * 15)
    assert r.status_code == 429 and "tokens" in err(r)["message"]
    assert say(api_client(tts.owner, tpm), "kokoro", "z" * 5).status_code == 200


def test_fy_tts_014_speech_never_goes_to_a_provider(tts, monkeypatch):
    """FY-TTS-014: a speech route takes no provider target (422 wrong_api), and a key whose cloud setting
    is on still gets 503 no_ready_replica, never a provider, when no replica is ready."""
    from forgeyard.config import get_settings
    monkeypatch.setenv("PROVIDER_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    cloud = FakeCloud("stub")
    try:
        p = add_provider(tts.owner, cloud, name="cloudy")
        set_tiers(tts.owner, "kokoro", [[("local", deployment_id(tts.owner, "kokoro"))],
                                        [("provider", model_id(p, "gpt-mini"))]], status=422)
        on = new_key(tts.owner, "on", cloud="on")["key"]
        from tests.gateway_helpers import set_state
        set_state("kokoro", 0, "starting")
        r = say(api_client(tts.owner, on), "kokoro")
        assert r.status_code == 503 and err(r)["code"] == "no_ready_replica"
        assert not cloud.seen
    finally:
        cloud.close()


def test_fy_tts_015_rendering_and_estimates(tts):
    """FY-TTS-015: vLLM-Omni runs `vllm serve <model> --omni` with both stages' memory share from the
    reservation (the decoder's part as its own fraction, the talker the rest; together within it), and its
    estimate is the talker's (talker_config) plus the audio decoder; llama.cpp gets the TTS projector;
    speaches the cache and its key variable."""
    spec = replica_row("qwen").spec
    rt = spec["runtime"]
    assert rt["command"] == ["vllm", "serve"] and rt["image"].startswith("vllm/vllm-omni:v0.32.0@sha256:")
    args = rt["args"]
    assert args[0] == "${MODEL_PATH}" and args[1] == "--omni" and args[args.index("--served-model-name") + 1] == "qwen"
    stages = json.loads(args[args.index("--stage-overrides") + 1])
    assert stages["0"]["max_model_len"] == 4096 and stages["0"]["max_num_seqs"] == 4 and stages["1"]["max_num_seqs"] == 4
    reserved = spec["resources"]["gpus"]["vram"]
    db = new_session()
    try:
        card = next(a for a in db.query(Accelerator).all() if a.index == 4 and a.vendor == "nvidia")
        total = card.vram_total
        est = db.query(Deployment).filter_by(name="qwen").one().estimate
    finally:
        db.close()
    parts = {p["label"]: p["bytes"] for p in est["parts"]}
    assert parts[CODEC_LABEL] == 4608 * 1024**2 and parts["Weights"] == 1_811_626_576
    assert parts["KV cache"] == 2 * 28 * 8 * 128 * 2 * 4096 * 4
    main, codec = stages["0"]["gpu_memory_utilization"], stages["1"]["gpu_memory_utilization"]
    from forgeyard.models import scheduler
    ctx = scheduler.gpu_context()
    assert codec == int((4608 * 1024**2 - ctx) / total * 1000) / 1000
    assert main == int((reserved - 4608 * 1024**2 - ctx) / total * 1000) / 1000
    assert 0 < codec < main and (main + codec) * total <= reserved
    assert 9 * GIB < reserved < 11 * GIB
    assert spec["env"]["VLLM_MEDIA_URL_ALLOW_REDIRECTS"] == "0" and "--allowed-media-domains" in args
    box = replica_row("box").spec["runtime"]["args"]
    assert box[box.index("--mmproj") + 1] == "${MODEL_PATH}/mmproj-chatterbox-turbo-F16.gguf"
    # llama.cpp keeps an audio pipeline per slot for a speech model, not for a chat model.
    db = new_session()
    try:
        parts = {d.name: {p["label"]: p["bytes"] for p in d.estimate["parts"]} for d in db.query(Deployment).all()}
    finally:
        db.close()
    assert parts["box"]["Speech pipeline"] == 2 * 2 * GIB and "Speech pipeline" not in parts["chat"]
    k = replica_row("kokoro").spec
    assert k["env"]["HF_HUB_CACHE"] == "${MODEL_HUB}" and k["env"]["WHISPER__TTL"] == "-1"
    assert k["runtime"]["image"].startswith("ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:")
    # The CPU estimate of Kokoro: its files plus the speaches process.
    kokoro, _ = recipes.builtin_recipes()["speaches-tts"]
    e = estimate(kokoro_meta(), kokoro, {"gpus": 0}, device="cpu", gpu_context=0)
    assert e.ram == 353_746_785 + GIB


def test_fy_tts_016_the_inspector_reads_speech_models(monkeypatch):
    """FY-TTS-016: Qwen3-TTS's talker_config gives the layer counts (Omni chat models keep theirs) and its
    built-in voices, also through the inspector against a repository; Hugging Face's text-to-speech is the
    tts task; model.onnx is the onnx format, whose whole repository (README included: speaches reads it)
    is downloaded, and a Piper voice's sample rate is read from its config.json."""
    from forgeyard.models import fetch, inspect as inspector
    from tests.model_helpers import FakeHF, Repo, safetensors_bytes

    raw = (FIX / "qwen3-tts-0.6b-customvoice.config.json").read_bytes()
    weights = safetensors_bytes({"talker.w": {"dtype": "BF16", "shape": [4, 4], "data_offsets": [0, 32]}}, b"\0" * 32)
    qwen = Repo("Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "8" * 40, {"config.json": raw, "model.safetensors": weights,
                                                                   "speech_tokenizer/model.safetensors": weights})
    qwen.info.update(pipeline_tag="text-to-speech", library_name=None)
    piper = Repo("speaches-ai/piper-en_US-amy-medium", "9" * 40,
                 {"README.md": b"---\nlibrary_name: piper\n---\n", "model.onnx": b"o" * 64,
                  "config.json": json.dumps({"audio": {"sample_rate": 22050}}).encode()})
    piper.info.update(pipeline_tag="text-to-speech", library_name="piper")
    hf = FakeHF(qwen, piper)
    try:
        policy = fetch.Policy(allow_private=True)
        q = inspector.inspect_hf("Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", None, None, endpoint=hf.url, policy=policy)
        assert q["task_hint"] == "tts" and q["config"]["layers"] == 28 and q["voices"][1] == "vivian"
        assert {f["path"] for f in q["files"]} >= {"model.safetensors", "speech_tokenizer/model.safetensors"}
        p = inspector.inspect_hf("speaches-ai/piper-en_US-amy-medium", None, None, endpoint=hf.url, policy=policy)
        assert p["format"] == "onnx" and p["sample_rate"] == 22050 and p["weights_bytes"] == 64
        assert "README.md" in {f["path"] for f in p["files"]}
    finally:
        hf.close()
    config = json.loads((FIX / "qwen3-tts-0.6b-customvoice.config.json").read_text())
    c = config_summary(config)
    assert (c["layers"], c["kv_heads"], c["head_dim"], c["max_context"]) == (28, 8, 128, 32768)
    assert voices_of(config)[:3] == ["serena", "vivian", "uncle_fu"] and len(voices_of(config)) == 9
    omni = {"thinker_config": {"text_config": {"num_hidden_layers": 36, "num_attention_heads": 16,
                                               "num_key_value_heads": 2, "hidden_size": 2048}},
            "talker_config": {"num_hidden_layers": 24}}
    assert config_summary(omni)["layers"] == 0          # read from the root as before 1.3, not the talker
    assert sources.task_hint("text-to-speech", []) == "tts"
    files = ["README.md", "model.onnx", "voices.bin", ".gitattributes"]
    assert sources.detect_format(files, "onnx") == "onnx"
    picked = sources.select_files([{"path": p, "size": 1} for p in files], "onnx")
    assert [f["path"] for f in picked] == ["README.md", "model.onnx", "voices.bin"]
    assert sources.detect_format(["model.safetensors", "onnx/model.onnx"]) == "safetensors"


def test_fy_tts_017_the_recommended_speech_models():
    """FY-TTS-017: the recommended list has a Qwen3-TTS for vLLM-Omni, Kokoro for speaches and a TTS GGUF
    for llama.cpp, each pinned to a commit with a permissive licence; an unpinned tts entry is refused."""
    data = rec.load()
    tts_entries = [e for e in data.models if e.task == "tts"]
    by_recipe = {e.recipe for e in tts_entries}
    assert {"vllm-omni-tts", "speaches-tts", "llama-cpp"} <= by_recipe
    assert all(sources.REVISION_RE.match(e.revision or "") and e.license in rec.ALLOWED_LICENSES for e in tts_entries)
    gguf = [e for e in tts_entries if e.format == "gguf"]
    assert gguf and all(e.mmproj for e in gguf)
    raw = json.loads(json.dumps({"groups": [g.model_dump() for g in data.groups],
                                 "models": [e.model_dump() for e in data.models]}))
    first = next(m for m in raw["models"] if m["task"] == "tts")
    first["revision"] = "main"
    with pytest.raises(rec.RecommendedError, match="pinned to a commit"):
        rec.check(raw)


def test_fy_tts_018_try_it_speaks_on_a_replica(tts):
    """FY-TTS-018: a deployment's Try it sends a short WAV request with the recipe's default voice
    straight to a replica and answers with the audio's length, not the audio."""
    dep = tts.owner.get("/api/deployments").json()["deployments"]
    kid = next(d["id"] for d in dep if d["name"] == "kokoro")
    r = tts.owner.post(f"/api/deployments/{kid}/try", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["summary"].startswith("1.0 s of audio (WAV")
    assert out["request"]["voice"] == "af_heart" and out["request"]["response_format"] == "wav"
    assert out["answer"]["riff"] is True
