#!/usr/bin/env python3
"""1.3 acceptance on real machines: text to speech at /v1/audio/speech through the gateway, called with
the official OpenAI Python SDK (client.audio.speech.create and with_streaming_response).

Through Forgeyard's API only (an admin session), for each model of --only, on --machine:

  1. adds the model to the catalog unless it is there (pinned to the commit below; GGUF models with their
     projector), deploys it there as `accept-tts-<model>` and waits until it is ready. A GPU deployment
     goes to --machine's allowed GPU (or --gpu); the card's memory is read before and after, against the
     estimate;
  2. creates an API key and, with the SDK: speaks an English sentence with OpenAI's `alloy` (the gateway
     picks the model's own voice and says which), and a Traditional Chinese one for models that speak it;
     streams a few sentences (stream_format audio: the first bytes must arrive before the end); asks for
     every format the engine makes (each must decode: ffprobe, or Python's wave for wav and pcm) and
     shows the others refused; SSE where the engine has it; lists the voices;
  3. shows the refusals: a reference recording (ref_audio, speaker_ref_b64), a voice given as audio,
     uploading a voice, an input over the deployment's cap, and the replica's own port without its key;
  4. reads the usage: the characters of the answered requests and the audio milliseconds;
  5. intelligibility (--asr): the English and Chinese audio go back through speech to text (a
     speech-to-text deployment through the gateway, or faster-whisper on this computer with
     local:<model>) and the words are compared. Without one it is SKIPped.

then removes what it created (deployments, the catalog entries it added, keys) and prints a report (also
JSON with --out; --save-audio keeps the audio files). Everything is named accept-tts-*, and leftovers of an
earlier run are removed first, so it can be run again.

    FORGEYARD_PASSWORD=… python3 scripts/accept_tts.py --server https://<server>:7860 --cacert forgeyard-ca.pem \\
        --user <admin> --machine <machine> --only qwen3-tts-1.7b,kokoro --asr auto --out accept-tts.json

Models it knows (--only):
  qwen3-tts-0.6b, qwen3-tts-1.7b   Qwen3-TTS CustomVoice on vLLM-Omni (NVIDIA): Chinese and English
  kokoro                           Kokoro-82M on speaches (CPU or CUDA): English only
  chatterbox-turbo-gguf            Chatterbox Turbo GGUF on llama.cpp: English
  qwen3-tts-1.7b-gguf              Qwen3-TTS 1.7B Base GGUF on llama.cpp: Chinese and English, no built-in voices
GGUF models run with --recipe (default llama-cpp; a custom one, such as a Vulkan recipe with a local image,
must keep the llama-cpp recipe's `tts:` block) on --device (cpu or gpu).

With Forgeyard's own CA, --cacert is the CA file. The official SDK: as scripts/accept_m4.py, in its own
virtual environment (--venv) when `openai` is not installed.
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept_m3  # noqa: E402
from accept_m3 import GIB, Client, Fail, log, machine  # noqa: E402
from accept_m4 import ensure_sdk, raw_post, wait_all_ready  # noqa: E402

PREFIX = "accept-tts-"
EN = "The quick brown fox jumps over the lazy dog near the river bank."
ZH = "我們明天早上九點在台北車站見面，記得帶雨傘。"
STREAM_TEXT = ("Streaming starts before the whole text is spoken. This is the second sentence. "
               "And here is a third one, so there is enough to stream.")
# Simplified forms speech-to-text may write for the Traditional characters of ZH.
SIMPLIFIED = str.maketrans({"们": "們", "点": "點", "车": "車", "见": "見", "记": "記", "带": "帶", "伞": "傘",
                            "臺": "台"})
MODELS = {
    "qwen3-tts-0.6b": {"source": "hf://Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
                       "revision": "85e237c12c027371202489a0ec509ded67b5e4b5", "recipe": "vllm-omni-tts",
                       "device": "gpu", "zh": True, "params": {"share": "vram"}},
    "qwen3-tts-1.7b": {"source": "hf://Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                       "revision": "0c0e3051f131929182e2c023b9537f8b1c68adfe", "recipe": "vllm-omni-tts",
                       "device": "gpu", "zh": True, "params": {"share": "vram"}},
    "kokoro": {"source": "hf://speaches-ai/Kokoro-82M-v1.0-ONNX", "revision": "dc196c76d64fed9203906231372bcb98135815df",
               "recipe": "speaches-tts", "zh": False, "device": "cpu",
               # speaches sends each sentence as it is made, but Kokoro makes 8 s of speech in under 2 s on
               # a CPU, so every sentence can arrive at once: information only.
               "streams": None, "params": {}},
    "chatterbox-turbo-gguf": {"source": "hf://ggml-org/chatterbox-turbo-GGUF",
                              "revision": "66f0393558d0ac1e0337e23a4d15f26e8eca0e12", "file": "chatterbox-turbo-Q8_0.gguf",
                              "mmproj": "mmproj-chatterbox-turbo-F16.gguf", "recipe": "llama-cpp", "zh": False,
                              # It makes the whole utterance before any audio comes out (b12242, CPU):
                              # its streaming check is information only.
                              "streams": False, "params": {}},
    "qwen3-tts-1.7b-gguf": {"source": "hf://ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF",
                            "revision": "ca27d74bc954b73dadab5b71ca265d87fc861a7c",
                            "file": "Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M.gguf",
                            "mmproj": "mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf", "recipe": "llama-cpp", "zh": True,
                            # llama-server sends the audio in a few large pieces: on a fast machine one
                            # (measured: 5.32 s / 5.32 s), on a slower one several (5.5 s / 8.0 s).
                            "streams": None, "params": {}},
}
EXT = {"mp3": "mp3", "opus": "ogg", "aac": "aac", "flac": "flac", "wav": "wav", "pcm": "pcm"}


# ------------------------------------------------------------------ audio checks, here
def pcm_to_wav(pcm: bytes, rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm[: len(pcm) - len(pcm) % (2 * channels)])
    return buf.getvalue()


def rate_of(headers) -> int:
    h = {k.lower(): v for k, v in dict(headers).items()}
    if str(h.get("x-audio-sample-rate", "")).isdigit():
        return int(h["x-audio-sample-rate"])
    m = re.search(r"rate=(\d+)", h.get("content-type", ""))
    return int(m.group(1)) if m else 24000


def duration(data: bytes, fmt: str, headers=None) -> tuple[float | None, str]:
    """(seconds, how) of an audio answer, decoded by ffprobe when it is installed, else by Python's wave
    module for wav and pcm. (None, why) when it does not decode."""
    if fmt == "pcm":
        data, fmt = pcm_to_wav(data, rate_of(headers or {})), "wav"
    if shutil.which("ffprobe"):
        with tempfile.NamedTemporaryFile(suffix="." + EXT[fmt]) as f:
            f.write(data)
            f.flush()
            p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f.name],
                               capture_output=True, text=True, timeout=60)
            # A streamed WAV declares an unknown size: ffprobe reads to the end.
            dec = subprocess.run(["ffmpeg", "-v", "error", "-i", f.name, "-f", "null", "-"], capture_output=True,
                                 text=True, timeout=120) if shutil.which("ffmpeg") else None
        try:
            secs = float(p.stdout.strip())
        except ValueError:
            return None, f"ffprobe: {(p.stderr or p.stdout).strip()[:120]}"
        if dec is not None and dec.returncode != 0:
            return None, f"ffmpeg: {dec.stderr.strip()[:120]}"
        return secs, "ffprobe"
    if fmt == "wav":
        try:
            with wave.open(io.BytesIO(data)) as w:
                frames = w.getnframes()
                if frames >= 0x7FFFFFFF // max(1, w.getsampwidth()):  # a streamed WAV: count the bytes
                    frames = (len(data) - 44) // (w.getsampwidth() * w.getnchannels())
                return frames / w.getframerate(), "wave"
        except (wave.Error, EOFError, struct.error) as e:
            return None, f"wave: {e}"
    return None, "no ffprobe to decode it"


def words_score(said: str, heard: str, chinese: bool) -> float:
    """How much of what was said came back: Chinese by characters (Simplified mapped back), English by
    words."""
    if chinese:
        want = [c for c in said if "一" <= c <= "鿿"]
        got = set(heard.translate(SIMPLIFIED))
        return sum(1 for c in want if c in got) / max(1, len(want))
    norm = lambda t: re.findall(r"[a-z']+", t.lower())  # noqa: E731
    want, got = norm(said), set(norm(heard))
    return sum(1 for w in want if w in got) / max(1, len(want))


# ------------------------------------------------------------------ the run
def cleanup(c: Client, added: list[str]) -> None:
    for d in c.ok("GET", "/api/deployments")["deployments"]:
        if d["name"].startswith(PREFIX):
            status, _ = c.call("DELETE", f"/api/deployments/{d['id']}")
            log(f"cleanup: deployment {d['name']} -> {status}")
    for k in c.ok("GET", "/api/keys")["keys"]:
        if k["name"].startswith(PREFIX.rstrip("-")) and not k.get("revoked_at"):
            c.call("DELETE", f"/api/keys/{k['id']}")
    if added:
        deadline = time.time() + 120
        for m in c.ok("GET", "/api/models")["models"]:
            if m["name"] in added:
                while True:
                    status, _ = c.call("DELETE", f"/api/models/{m['id']}")
                    if status != 409 or time.time() > deadline:
                        break
                    time.sleep(3)
                log(f"cleanup: catalog entry {m['name']} -> {status}")


def ensure(c: Client, name: str, spec: dict) -> bool:
    have = {m["name"]: m for m in c.ok("GET", "/api/models")["models"]}
    if name in have:
        if have[name]["task"] != "tts":
            raise Fail(f"{name} is in the catalog as a {have[name]['task']} model: remove it first")
        return False
    body = {"name": name, "source": spec["source"], "task": "tts", "title": name, "revision": spec["revision"]}
    for k in ("file", "mmproj"):
        if spec.get(k):
            body[k] = spec[k]
    m = c.ok("POST", "/api/models", body, timeout=300)["model"]
    log(f"catalog: added {name} ({m['source']} @ {m['revision'][:12]}, {m['size_bytes'] / GIB:.2f} GiB, "
        f"licence {m.get('license')}, voices {len(m.get('voices') or [])})")
    return True


def samples_16k(wav_bytes: bytes):
    """A WAV as 16 kHz mono float samples, what faster-whisper takes (decoded here rather than by its
    own PyAV, whose versions disagree)."""
    import numpy as np

    if shutil.which("ffmpeg"):
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                           input=wav_bytes, capture_output=True, timeout=120, check=True)
        return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, data = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    x = data.astype(np.float32) / 32768.0
    t = np.arange(0, len(x) / rate, 1 / 16000)
    return np.interp(t, np.arange(len(x)) / rate, x).astype(np.float32)


class Asr:
    """Speech to text for the intelligibility check: a gateway deployment, faster-whisper here, or none."""

    def __init__(self, how: str, c: Client, sdk, report: dict):
        self.kind, self.model, self.local = None, None, None
        if how == "none":
            return
        if how.startswith("local:"):
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                log("--asr local: faster-whisper is not installed in this Python; intelligibility is skipped")
                return
            self.kind, self.model = "local", how[6:] or "small"
            self.local = WhisperModel(self.model, device="cpu", compute_type="int8")
        else:
            routes = c.ok("GET", "/api/routes")["routes"]
            asr = [r["served_name"] for r in routes if r.get("api") == "openai-audio" and r.get("endpoints")]
            pick = asr[0] if how == "auto" and asr else (how if how != "auto" else None)
            if pick:
                self.kind, self.model = "gateway", pick
        self.sdk = sdk
        report["asr"] = f"{self.kind}:{self.model}" if self.kind else None

    def text(self, wav_bytes: bytes, language: str) -> str:
        if self.kind == "local":
            segments, _ = self.local.transcribe(samples_16k(wav_bytes), language=language, beam_size=1)
            return "".join(s.text for s in segments).strip()
        out = self.sdk.audio.transcriptions.create(model=self.model, file=("speech.wav", wav_bytes, "audio/wav"),
                                                   language=language)
        return (out.text or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860")
    ap.add_argument("--cacert", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--machine", required=True, help="the machine to deploy on")
    ap.add_argument("--only", default="qwen3-tts-1.7b", help=f"comma-separated, of {', '.join(MODELS)}")
    ap.add_argument("--recipe", help="the recipe for GGUF models (default llama-cpp), e.g. a custom Vulkan one")
    ap.add_argument("--device", choices=("cpu", "gpu"), help="for GGUF and speaches models (default: gpu when the "
                                                             "machine has one)")
    ap.add_argument("--gpu", type=int, help="the GPU index (default: the machine's only allowed GPU)")
    ap.add_argument("--threads", type=int, default=8, help="CPU threads for a model on the CPU")
    ap.add_argument("--asr", default="auto", help="speech to text for the intelligibility check: auto (a ready "
                    "speech-to-text deployment, else skipped), a served model name, local:<faster-whisper model> "
                    "(on this computer, e.g. local:small), or none")
    ap.add_argument("--timeout", type=float, default=3600, help="seconds per deployment to become ready")
    ap.add_argument("--venv", default=str(Path.home() / ".cache" / "forgeyard-accept-m4"),
                    help="where to install the openai SDK if it is missing")
    ap.add_argument("--save-audio", help="keep every audio answer in this directory")
    ap.add_argument("--keep", action="store_true", help="leave the deployments running afterwards")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args()
    unknown = [n for n in args.only.split(",") if n not in MODELS]
    if unknown:
        raise Fail(f"unknown model(s) {unknown}; known: {', '.join(MODELS)}")
    ensure_sdk(Path(args.venv))
    accept_m3.use_cacert(args.cacert)
    import httpx
    import openai

    password = os.environ.get("FORGEYARD_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    c = Client(args.server)
    c.ok("POST", "/api/auth/login", {"username": args.user, "password": password})
    m = machine(c, args.machine)
    if m["status"] != "online" or "models" not in (m.get("features") or []):
        raise Fail(f"{m['name']} must be online and serve models; status {m['status']}, features {m.get('features')}")
    gpus = [a for a in m["accelerators"] if a["vendor"] != "cpu" and a.get("allowed", True)]
    report: dict = {"server": args.server, "machine": m["name"], "sdk": f"openai {openai.__version__}", "checks": [],
                    "deployments": {}}
    added: list[str] = []
    save = Path(args.save_audio) if args.save_audio else None
    if save:
        save.mkdir(parents=True, exist_ok=True)

    def check(name: str, ok: bool, detail="", info: bool = False, skip: bool = False) -> None:
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail, "info": info or skip, "skip": skip})
        tag = "SKIP" if skip else "INFO" if info else "PASS" if ok else "FAIL"
        log(f"{tag}  {name}" + (f"  ({detail})" if detail else ""))

    def card(index: int) -> dict:
        return next(a for a in machine(c, m["name"])["accelerators"] if a["index"] == index and a["vendor"] != "cpu")

    cleanup(c, [])
    recipes = {r["name"]: r for r in c.ok("GET", "/api/models")["recipes"]}
    try:
        for name in args.only.split(","):
            spec = dict(MODELS[name])
            recipe = (args.recipe or spec["recipe"]) if spec.get("file") else spec["recipe"]
            if recipe not in recipes:
                raise Fail(f"no recipe {recipe!r} on the server")
            fixed = spec.get("device")
            device = fixed or args.device or ("gpu" if gpus and "gpu" in recipes[recipe]["devices"] else "cpu")
            if ensure(c, name, spec):
                added.append(name)
            params = dict(spec["params"])
            params["gpus"] = 1 if device == "gpu" else 0
            if device == "cpu" and "threads" in recipes[recipe]["params"]:
                params["threads"] = args.threads
            dep_name = f"{PREFIX}{name}".replace(".", "-")[:48]
            body = {"name": dep_name, "model": name, "recipe": recipe, "params": params, "served_name": dep_name,
                    "replicas": 1, "machine_id": m["id"]}
            est = c.ok("POST", "/api/deployments/estimate", body)
            if not est["placement"]["fits"]:
                raise Fail(f"{dep_name} does not fit on {m['name']}: {est['placement'].get('summary')}")
            placed = est["placement"]["placed"][0]
            index = placed["gpus"][0] if placed["gpus"] else None
            if device == "gpu" and args.gpu is not None and index != args.gpu:
                raise Fail(f"{dep_name} would go to GPU {index}, not #{args.gpu}; stopping")
            before = accept_m3.settle_card(c, m["name"], card(index)["key"], 1) if index is not None else None
            dep = c.ok("POST", "/api/deployments", body)["deployment"]
            log(f"{dep_name}: deployed on {placed['machine']} {'GPU #' + str(index) if index is not None else 'CPU'} "
                f"({placed['variant']}); estimate {dep['estimate']['amount'] / GIB:.2f} GiB: "
                + ", ".join(f"{p['label']} {p['bytes'] / GIB:.2f}" for p in dep["estimate"]["parts"]))
            t0 = time.time()
            d = wait_all_ready(c, dep["id"], args.timeout)
            info = {"recipe": recipe, "device": device, "ready_seconds": int(time.time() - t0),
                    "estimate_gib": round(dep["estimate"]["amount"] / GIB, 2),
                    "estimate_parts": {p["label"]: round(p["bytes"] / GIB, 3) for p in dep["estimate"]["parts"]}}
            if index is not None:
                after = accept_m3.settle_card(c, m["name"], card(index)["key"])
                used = (after["vram_total"] - after["vram_free"]) - (before["vram_total"] - before["vram_free"])
                info["measured_gib"] = round(used / GIB, 2)
                check(f"{dep_name}: memory on GPU #{index} within the estimate", used <= dep["estimate"]["amount"],
                      f"measured {used / GIB:.2f} GiB (card used before/after; other work on the card adds noise), "
                      f"estimate {dep['estimate']['amount'] / GIB:.2f} GiB", info=used > dep["estimate"]["amount"])
            report["deployments"][dep_name] = info
            exercise(c, d, dep_name, spec, recipes[recipe], args, check, httpx, openai, report, save)
    finally:
        if args.keep:
            log("--keep: leaving the deployments running; run again without --keep to remove them")
        else:
            cleanup(c, added)
    failed = [x for x in report["checks"] if not x["ok"] and not x["info"]]
    report["passed"] = len([x for x in report["checks"] if x["ok"] and not x["info"]])
    report["failed"] = len(failed)
    report["skipped"] = len([x for x in report["checks"] if x.get("skip")])
    log(f"{report['passed']} passed, {len(failed)} failed, {report['skipped']} skipped")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


def usage_row(c: Client, model: str) -> dict:
    c.ok("GET", "/api/usage?everyone=true&group=model")    # writes what is counted in memory first
    time.sleep(1)
    rows = {r["id"]: r for r in c.ok("GET", "/api/usage?everyone=true&group=model")["rows"]}
    return rows.get(model, {})


def exercise(c: Client, d: dict, model: str, spec: dict, recipe: dict, args, check, httpx, openai, report: dict,
             save, key: dict | None = None) -> None:
    """Every check on one ready deployment `d` served as `model`. `key`: an API key to use (as POST
    /api/keys answers it); without one a key named accept-tts-key is created (removed by cleanup)."""
    k = key or c.ok("POST", "/api/keys", {"name": f"{PREFIX}key"})
    gw = k["gateway_url"]
    http = httpx.Client(verify=accept_m3.TLS, timeout=900) if accept_m3.TLS else httpx.Client(timeout=900)
    sdk = openai.OpenAI(base_url=gw, api_key=k["key"], max_retries=0, timeout=900, http_client=http)
    tts = recipe.get("tts") or {}
    formats = tts.get("formats") or ["wav"]
    before = usage_row(c, model)
    spoken = 0
    info = report["deployments"][model]

    def keep(label: str, data: bytes, fmt: str) -> None:
        if save:
            (save / f"{model}-{label}.{EXT[fmt]}").write_bytes(data)

    def speak(label: str, text: str, fmt: str, voice: str, **extra) -> tuple[bytes, dict] | None:
        nonlocal spoken
        t0 = time.time()
        try:
            raw = sdk.audio.speech.with_raw_response.create(model=model, voice=voice, input=text, response_format=fmt,
                                                            extra_body=extra or None)
        except openai.APIStatusError as e:
            check(f"{model}: {label}", False, f"{e.status_code}: {str(e)[:200]}")
            return None
        data, headers = raw.content, dict(raw.headers)
        spoken += len(text)
        secs, how = duration(data, fmt, headers)
        keep(label.split(" ")[0], data, fmt)
        voice_used = headers.get("x-forgeyard-voice")
        check(f"{model}: {label}", secs is not None and secs > 0.3,
              f"{time.time() - t0:.1f} s, {len(data) / 1024:.0f} KiB {headers.get('content-type')}, "
              f"{secs if secs is None else round(secs, 2)} s of audio ({how})"
              + (f", voice {voice_used}" if voice_used else ""))
        return data, headers

    # ---- voices
    r = http.get(f"{gw}/audio/voices", params={"model": model}, headers={"Authorization": f"Bearer {k['key']}"})
    voices = [v["id"] for v in r.json().get("voices", [])] if r.status_code == 200 else []
    check(f"{model}: GET /v1/audio/voices", r.status_code == 200,
          f"{r.status_code}: {len(voices)} voices {voices[:6]}{'…' if len(voices) > 6 else ''}"
          + (f", default {r.json().get('default')}" if r.status_code == 200 else ""), info=r.status_code == 200 and not voices)
    voice = (r.json().get("default") if r.status_code == 200 else None) or "alloy"

    # ---- English with OpenAI's alloy, Chinese, every format
    en = speak("English sentence with voice alloy (wav)", EN, "wav", "alloy")
    try:   # the SDK's plain call, as most programs use it
        out = sdk.audio.speech.create(model=model, voice=voice, input="Hello from Forgeyard.", response_format="wav")
        spoken += len("Hello from Forgeyard.")
        secs, how = duration(out.content, "wav")
        check(f"{model}: client.audio.speech.create", secs is not None and secs > 0.3,
              f"{len(out.content) / 1024:.0f} KiB, {secs if secs is None else round(secs, 2)} s ({how})")
    except openai.APIStatusError as e:
        check(f"{model}: client.audio.speech.create", False, f"{e.status_code}: {str(e)[:200]}")
    zh = speak("Traditional Chinese sentence (wav)", ZH, "wav", voice) if spec.get("zh") else None
    for fmt in ("mp3", "opus", "aac", "flac", "pcm"):
        if fmt in formats:
            speak(f"{fmt} answer decodes", "This is a format check.", fmt, voice)
        else:
            body = json.dumps({"model": model, "input": "Format check.", "voice": voice, "response_format": fmt})
            s, text = raw_post(f"{gw}/audio/speech", body.encode(), "application/json",
                               {"Authorization": f"Bearer {k['key']}"}, timeout=120)
            check(f"{model}: {fmt} (not made by this engine) is refused", s == 400 and "unsupported_response_format" in text,
                  f"{s} {text[:120]}")

    # ---- streaming: the first bytes before the end
    for fmt in ("pcm", "wav"):
        if "audio" not in (tts.get("stream_formats") or []) or fmt not in formats:
            continue
        t0, first, got = time.time(), None, b""
        try:
            with sdk.audio.speech.with_streaming_response.create(model=model, voice=voice, input=STREAM_TEXT,
                                                                 response_format=fmt,
                                                                 extra_body={"stream_format": "audio"}) as resp:
                headers = dict(resp.headers)
                for chunk in resp.iter_bytes():
                    if first is None and chunk:
                        first = time.time() - t0
                    got += chunk
            total = time.time() - t0
            spoken += len(STREAM_TEXT)
            secs, how = duration(got, fmt, headers)
            keep(f"stream-{fmt}", got, fmt)
            streamed = first is not None and secs and first < total - 0.25
            check(f"{model}: streamed {fmt} (stream_format audio): first audio before the end", streamed,
                  f"first bytes after {first:.2f} s, all after {total:.2f} s, {secs and round(secs, 2)} s of audio ({how})"
                  + ("" if streamed or spec.get("streams", True) is True else "; this engine may send the whole utterance at once"),
                  info=not streamed and spec.get("streams", True) is not True)
            info[f"stream_{fmt}_first_s"] = round(first or 0, 2)
        except openai.APIStatusError as e:
            check(f"{model}: streamed {fmt}", False, f"{e.status_code}: {str(e)[:200]}")
        break
    if "sse" in (tts.get("stream_formats") or []):
        t0 = time.time()
        with http.stream("POST", f"{gw}/audio/speech", headers={"Authorization": f"Bearer {k['key']}"},
                         json={"model": model, "voice": voice, "input": STREAM_TEXT, "response_format": "pcm",
                               "stream_format": "sse"}) as resp:
            types = [json.loads(line[5:]).get("type") for line in resp.iter_lines() if line.startswith("data:")]
        spoken += len(STREAM_TEXT) if resp.status_code == 200 else 0
        check(f"{model}: SSE (stream_format sse)", resp.status_code == 200 and "speech.audio.delta" in types
              and types[-1] == "speech.audio.done", f"{resp.status_code} in {time.time() - t0:.1f} s: "
                                                   f"{types.count('speech.audio.delta')} deltas, last {types[-1:] }")
    else:
        body = json.dumps({"model": model, "input": "SSE check.", "voice": voice, "stream_format": "sse"})
        s, text = raw_post(f"{gw}/audio/speech", body.encode(), "application/json", {"Authorization": f"Bearer {k['key']}"})
        check(f"{model}: SSE (not offered by this engine) is refused", s == 400 and "stream_format_not_supported" in text,
              f"{s} {text[:100]}")

    # ---- refusals
    def refused(label: str, body: dict, code: str, path: str = "audio/speech") -> None:
        s, text = raw_post(f"{gw}/{path}", json.dumps({"model": model, "voice": voice, **body}).encode(),
                           "application/json", {"Authorization": f"Bearer {k['key']}"}, timeout=120)
        check(f"{model}: {label}", s == 400 and f'"{code}"' in text, f"{s} {text[:120]}")

    refused("a reference recording (ref_audio) is refused",
            {"input": "Clone me.", "ref_audio": "https://example.com/me.wav", "ref_text": "hi"}, "voice_cloning_not_supported")
    refused("speaker_ref_b64 is refused", {"input": "Clone me.", "speaker_ref_b64": "UklGRg=="}, "voice_cloning_not_supported")
    refused("a voice given as audio is refused", {"input": "Clone me.", "voice": "data:audio/wav;base64,UklGRg=="},
            "voice_cloning_not_supported")
    refused("uploading a voice is refused", {"name": "me"}, "voice_cloning_not_supported", path="audio/voices")
    cap = int(d["params"].get("max_input_chars") or 0) or (int(d["params"].get("context") or 0) // 4 or 4096)
    refused(f"an input over the cap ({cap} characters) is refused", {"input": "x" * (cap + 1)}, "input_too_long")
    if not spec.get("zh") and recipe.get("tts", {}).get("cjk") is False:
        refused("Chinese to an English-only engine is refused", {"input": ZH}, "language_not_supported")

    # ---- the replica's own port without its internal key
    for e in d["endpoints"]:
        s, text = raw_post(f"{e['url']}/v1/audio/speech", b'{"input":"hi"}', "application/json", timeout=20)
        check(f"{model}: replica {e['replica']} on {e['machine']} without the key is refused", s in (401, 403),
              f"{s} {text[:80]}")

    # ---- usage
    after = usage_row(c, model)
    chars = after.get("characters", 0) - before.get("characters", 0)
    ms = after.get("media_ms", 0) - before.get("media_ms", 0)
    check(f"{model}: usage counts the characters spoken", chars == spoken, f"{chars} counted, {spoken} spoken")
    check(f"{model}: usage records the audio's length", ms > 0, f"{ms / 1000:.1f} s of audio")
    info.update(characters=chars, audio_seconds=round(ms / 1000, 1))

    # ---- intelligibility
    asr = Asr(args.asr, c, sdk, report)
    for label, sample, text, lang in (("English", en, EN, "en"), ("Chinese", zh, ZH, "zh")):
        if sample is None:
            continue
        if asr.kind is None:
            check(f"{model}: {label} audio is intelligible", True, "no speech-to-text model: --asr", skip=True)
            continue
        try:
            heard = asr.text(sample[0], lang)
        except Exception as e:  # noqa: BLE001 - report it and go on
            check(f"{model}: {label} audio is intelligible", False, f"speech to text failed: {e}"[:200])
            continue
        score = words_score(text, heard, lang == "zh")
        info[f"asr_{lang}"] = {"heard": heard, "score": round(score, 2)}
        check(f"{model}: {label} audio is intelligible ({asr.kind} {asr.model})", score >= 0.6,
              f"{score:.0%} of the {'characters' if lang == 'zh' else 'words'} came back: {heard[:90]!r}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        log(f"STOPPED: {e}")
        sys.exit(2)
