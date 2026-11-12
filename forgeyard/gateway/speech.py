"""Text to speech at /v1/audio/speech (1.3): OpenAI's request shape in, the engine's audio out.

What the gateway does with a speech request, before and after the engine:

- **Checks** (400 before anything is sent): `input` is a non-empty string; `response_format` is one of
  OpenAI's (mp3, opus, aac, flac, wav, pcm) and one the deployment's engine makes (its recipe's
  `tts.formats`); `stream_format` (audio or sse) is one the engine streams; `speed` is 0.25 to 4; the
  input is no longer than the deployment's `max_input_chars` (default 4,096, OpenAI's own limit). An
  engine that cannot speak Chinese, Japanese or Korean (speaches deletes those characters) refuses
  such input instead of answering with silence.
- **Voice cloning is blocked** (1.3): a reference recording (`ref_audio`, `speaker_ref_b64`, …), a
  voice given as audio data or a URL, or Qwen3-TTS's `task_type: Base` get 400
  `voice_cloning_not_supported`; uploading voices (`POST /v1/audio/voices`) does too. Only an
  engine's built-in voices speak.
- **Filled in** per deployment, only when the request leaves them out: `response_format` (the
  engine's default, so the gateway knows what comes back), a voice (the recipe's default voice, or the
  first the engine lists) in place of a missing one or of an OpenAI voice name the model does not
  have (`alloy`, …; the answer says which in X-Forgeyard-Voice), the language from the text's script
  for engines that otherwise assume English (llama.cpp's `lang`), and a frame cap from the input's
  length for engines whose default would cut long text off (llama.cpp's `n_predict`).
- **Counted**: the input's characters (Unicode code points) before sending: they are the request's
  units in usage and its tokens for the key's tpm (a character counts as one token). After the
  answer: the audio's length in milliseconds (`media_ms`) where the format says it: wav and pcm from
  their header or the sample rate, flac from its STREAMINFO, mp3 and aac by counting frames, opus
  from the last Ogg page; SSE from the size of the audio deltas (pcm). Other answers count none.
- **No cloud**: speech is served by local deployments only (no provider spill in 1.3).
"""

from __future__ import annotations

import asyncio
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from forgeyard.gateway.errors import GatewayError

ENDPOINT = "audio/speech"
FORMATS = ("mp3", "opus", "aac", "flac", "wav", "pcm")
STREAM_FORMATS = ("audio", "sse")
DEFAULT_MAX_CHARS = 4096            # OpenAI's limit for one request
MIN_SPEED, MAX_SPEED = 0.25, 4.0
MAX_VOICE_NAME = 100
# OpenAI's built-in voices: clients (and the official SDK, where `voice` is required) send these; a
# model that has other voices speaks with its default one instead.
OPENAI_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse",
                 "marin", "cedar")
# Request fields that carry someone's voice to copy (vLLM-Omni, llama.cpp, speaches forks, CosyVoice,
# Xinference, F5 and similar servers). Any of them is refused, whatever its value.
CLONING_FIELDS = ("ref_audio", "ref_text", "ref_audio_url", "speaker_ref", "speaker_ref_b64", "speaker_audio",
                  "speaker_embedding", "speaker_wav", "x_vector_only_mode", "prompt_speech", "prompt_audio",
                  "prompt_wav", "prompt_text", "reference_audio", "reference_text", "reference_id", "audio_prompt",
                  "voice_sample", "voice_clone", "clone_voice", "audio_sample", "zero_shot_spk_id")
MIME = {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac", "flac": "audio/flac", "wav": "audio/wav",
        "pcm": "audio/pcm"}
VOICE_HEADER = "X-Forgeyard-Voice"
VOICES_TTL = 300.0                  # seconds an engine's voice list is kept
VOICES_TIMEOUT = 5.0
MAX_METER_BYTES = 64 * 1024**2      # compressed answers are kept this far to count their frames

_CJK = re.compile("[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿\U00020000-\U0002fa1f]")
_KANA = re.compile("[぀-ヿ]")
_HANGUL = re.compile("[가-힯]")


# ------------------------------------------------------------------ what a deployment speaks
@dataclass(frozen=True)
class Speaks:
    """A speech deployment's settings at the gateway: from its recipe's `tts` block and its parameters."""

    formats: tuple = ("wav",)
    default_format: str = "wav"
    stream_formats: tuple = ()
    max_chars: int = DEFAULT_MAX_CHARS
    voices_path: Optional[str] = None
    default_voice: Optional[str] = None
    language_field: Optional[str] = None
    frames_field: Optional[str] = None
    cjk: bool = True
    pcm_rate: int = 24000
    context: Optional[int] = None
    model_voices: tuple = ()        # the voices the catalog entry names (Qwen3-TTS config), if any


def speaks(recipe, params: dict, meta: Optional[dict] = None) -> Speaks:
    sp = recipe.speech
    ctx = params.get("context")
    ctx = ctx if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0 else None
    cap = params.get("max_input_chars")
    # 0 (or none): derived. Engines whose request holds the text and the audio codes in one context get a
    # quarter of it (the audio takes the rest); others OpenAI's 4,096.
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else \
        (max(1, ctx // 4) if ctx else DEFAULT_MAX_CHARS)
    voices = tuple(v for v in (meta or {}).get("voices") or [] if isinstance(v, str))
    return Speaks(formats=tuple(sp.formats), default_format=sp.default_format, stream_formats=tuple(sp.stream_formats),
                  max_chars=cap, voices_path=sp.voices, default_voice=sp.default_voice, language_field=sp.language_field,
                  frames_field=sp.frames_field, cjk=sp.cjk, pcm_rate=_int((meta or {}).get("sample_rate")) or sp.pcm_rate,
                  context=ctx, model_voices=voices)


# ------------------------------------------------------------------ the request
@dataclass
class Ask:
    """A checked speech request."""

    characters: int
    format: Optional[str]           # what the client asked for (None: the engine's default)
    stream_format: Optional[str]
    voice: Optional[str]            # the voice name asked for (an {"id": …} object is read as its id)
    language_given: bool
    frames_given: bool


def _refuse_cloning(what: str) -> GatewayError:
    return GatewayError(400, "voice_cloning_not_supported",
                        f"Voice cloning is not available ({what}). Use one of the model's own voices: "
                        "GET /v1/audio/voices?model=<model> lists them.", param="voice")


def _looks_like_audio(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith(("data:", "http://", "https://", "file:", "ftp:", "s3:")) or len(t) > MAX_VOICE_NAME


def check(obj: dict) -> Ask:
    """Validate a speech request's body (400 for what no engine should get). Mutates nothing."""
    text = obj.get("input")
    if not isinstance(text, str) or not text.strip():
        raise GatewayError(400, "invalid_request", "The request needs an 'input': the text to speak.", param="input")
    for name in CLONING_FIELDS:
        if obj.get(name) not in (None, "", False):
            raise _refuse_cloning(f"'{name}' gives a reference recording")
    task = obj.get("task_type")
    if isinstance(task, str) and task.strip().lower() == "base":
        raise _refuse_cloning("task_type 'Base' clones a reference recording")
    voice = obj.get("voice")
    name = None
    if isinstance(voice, dict):
        extra = set(voice) - {"id"}
        if extra or not isinstance(voice.get("id"), str):
            raise _refuse_cloning("a voice object carries more than a voice id")
        voice = voice["id"]
    if voice is not None:
        if not isinstance(voice, str):
            raise GatewayError(400, "invalid_request", "'voice' is a voice name.", param="voice")
        if _looks_like_audio(voice):
            raise _refuse_cloning("'voice' must name a built-in voice, not carry audio or a link")
        name = voice.strip() or None
    fmt = obj.get("response_format")
    if fmt is not None and fmt not in FORMATS:
        raise GatewayError(400, "unsupported_response_format",
                           f"response_format must be one of {', '.join(FORMATS)}.", param="response_format")
    sf = obj.get("stream_format")
    if sf is not None and sf not in STREAM_FORMATS:
        raise GatewayError(400, "invalid_request", "stream_format must be 'audio' or 'sse'.", param="stream_format")
    speed = obj.get("speed")
    if speed is not None and (isinstance(speed, bool) or not isinstance(speed, (int, float))
                              or not MIN_SPEED <= float(speed) <= MAX_SPEED):
        raise GatewayError(400, "invalid_request", f"speed must be a number from {MIN_SPEED:g} to {MAX_SPEED:g}.",
                           param="speed")
    if "stream" in obj and not isinstance(obj["stream"], bool):
        raise GatewayError(400, "invalid_request", "'stream' is true or false; use stream_format.", param="stream")
    return Ask(characters=len(text), format=fmt, stream_format=sf or ("audio" if obj.get("stream") is True else None),
               voice=name, language_given=obj.get("lang") is not None or obj.get("language") is not None,
               frames_given=obj.get("n_predict") is not None or obj.get("max_new_tokens") is not None)


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def language_of(text: str) -> Optional[str]:
    """The language code for an engine that would otherwise assume English, from the text's script:
    kana -> ja, Hangul -> ko, Han characters -> zh; None for anything else (the engine's default)."""
    if _KANA.search(text):
        return "ja"
    if _HANGUL.search(text):
        return "ko"
    if _CJK.search(text):
        return "zh"
    return None


def frames_for(characters: int, context: Optional[int]) -> int:
    """A frame cap for engines whose default is short (llama.cpp: 512 frames, about 41 s at 12.5 Hz):
    ten frames a character, at least 512, at most the deployment's context (the frames live there).
    Speech runs at about 3 to 15 characters a second and these models at 12.5 to 25 frames a second,
    so ten frames a character leaves room and still stops a model that never ends."""
    n = max(512, 10 * characters)
    return min(n, context) if context else min(n, 16384)


def takes(sp: Optional[Speaks], ask: Ask, text: str) -> Optional[str]:
    """Why this deployment cannot take the request (an error code), or None."""
    if sp is None:
        return None
    if ask.characters > sp.max_chars:
        return "input_too_long"
    if ask.format is not None and ask.format not in sp.formats:
        return "unsupported_response_format"
    if ask.stream_format is not None and ask.stream_format not in sp.stream_formats:
        return "stream_format_not_supported"
    if not sp.cjk and has_cjk(text):
        return "language_not_supported"
    return None


def refusal(name: str, code: str, ask: Ask, all_sp: list[Speaks]) -> GatewayError:
    """The error for a request no deployment of the route can take, saying what would work."""
    if code == "input_too_long":
        most = max(s.max_chars for s in all_sp)
        return GatewayError(400, "input_too_long", f"The input is {ask.characters} characters; {name!r} takes at most "
                                                   f"{most} per request. Split the text.", param="input")
    if code == "unsupported_response_format":
        fmts = sorted({f for s in all_sp for f in s.formats}, key=FORMATS.index)
        return GatewayError(400, "unsupported_response_format",
                            f"{name!r} makes {', '.join(fmts)}, not {ask.format}.", param="response_format")
    if code == "stream_format_not_supported":
        have = sorted({f for s in all_sp for f in s.stream_formats})
        what = f"streams only as {', '.join(have)}" if have else "does not stream"
        return GatewayError(400, "stream_format_not_supported", f"{name!r} {what}; leave stream_format out for the "
                                                                "whole file.", param="stream_format")
    return GatewayError(400, "language_not_supported",
                        f"{name!r} cannot speak Chinese, Japanese or Korean (its engine drops those characters). "
                        "Use a Qwen3-TTS deployment for them.", param="input")


# ------------------------------------------------------------------ the body for one deployment
def body_for(obj: dict, ask: Ask, sp: Optional[Speaks], voices: Optional[list[str]]) -> tuple[dict, Optional[str]]:
    """(the request body for an engine with these settings, the voice the gateway chose or None).
    `voices`: the names the engine lists (None: not known)."""
    out = {k: v for k, v in obj.items() if k != "stream"}
    if ask.stream_format is not None:
        out["stream_format"] = ask.stream_format
    if sp is None:
        return out, None
    if ask.format is None:
        out["response_format"] = sp.default_format
    if isinstance(obj.get("voice"), dict) and ask.voice:
        out["voice"] = ask.voice
    chosen = None
    want = ask.voice
    known = voices if voices is not None else (list(sp.model_voices) or None)
    if want is None or (want.lower() in OPENAI_VOICES and (known is None or want not in known)):
        if known:
            default = sp.default_voice if sp.default_voice in known else known[0]
        elif known is None:
            default = sp.default_voice
        else:
            default = None          # the model has no voices of its own to choose from
        if default is not None:
            out["voice"] = default
            chosen = default
        elif want is not None and known == []:
            out.pop("voice", None)  # an OpenAI name it does not know: let it speak with its own voice
            chosen = "default"
    if sp.language_field and not ask.language_given:
        lang = language_of(obj.get("input") or "")
        if lang:
            out[sp.language_field] = lang
    if sp.frames_field and not ask.frames_given:
        out[sp.frames_field] = frames_for(ask.characters, sp.context)
    return out, chosen


# ------------------------------------------------------------------ voices
_voices: dict[int, tuple[float, list[dict]]] = {}
_voice_locks: dict[int, asyncio.Lock] = {}


def forget_voices() -> None:
    _voices.clear()


def parse_voices(data) -> list[dict]:
    """An engine's voice list in one shape, [{"id", "name", "language"?, "gender"?}]: vLLM-Omni's
    {"voices": [names], "uploaded_voices": […]} (uploaded ones left out), speaches' {"voices": [{name,
    language, gender}]}, llama.cpp's /props {"voices": [names]}, or a plain list."""
    items = data.get("voices") if isinstance(data, dict) else data
    out, seen = [], set()
    for v in items if isinstance(items, list) else []:
        if isinstance(v, str):
            entry = {"id": v, "name": v}
        elif isinstance(v, dict):
            vid = v.get("id") or v.get("voice_id") or v.get("name")
            if not isinstance(vid, str):
                continue
            entry = {"id": vid, "name": str(v.get("name") or vid)}
            for k in ("language", "gender"):
                if isinstance(v.get(k), (str, list)):
                    entry[k] = v[k] if isinstance(v[k], str) else ", ".join(str(x) for x in v[k])[:64]
        else:
            continue
        entry = {k: (val[:MAX_VOICE_NAME] if isinstance(val, str) else val) for k, val in entry.items()}
        if entry["id"] not in seen:
            seen.add(entry["id"])
            out.append(entry)
        if len(out) >= 500:
            break
    return out


async def fetch_voices(http: httpx.AsyncClient, url: str, secret: Optional[str], path: str) -> list[dict]:
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "forgeyard-gateway"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    r = await http.get(f"{url}{path}", headers=headers, timeout=VOICES_TIMEOUT)
    if r.status_code != 200:
        raise GatewayError(502, "upstream_error", f"The replica answered {r.status_code} to its voice list.")
    try:
        return parse_voices(r.json())
    except ValueError:
        raise GatewayError(502, "upstream_error", "The replica's voice list is not JSON.") from None


async def voices_for(deployment_id: int, sp: Optional[Speaks], http: httpx.AsyncClient, url: str,
                     secret: Optional[str], *, fresh: bool = False) -> Optional[list[dict]]:
    """The deployment's voices, from one of its replicas and kept for VOICES_TTL; the catalog's list
    when the engine has no voice list; None when neither is known (or the replica did not answer)."""
    if sp is None:
        return None
    if not sp.voices_path:
        return [{"id": v, "name": v} for v in sp.model_voices] if sp.model_voices else None
    now = time.monotonic()
    hit = _voices.get(deployment_id)
    if hit and hit[0] > now and not fresh:
        return hit[1]
    lock = _voice_locks.setdefault(deployment_id, asyncio.Lock())
    async with lock:
        hit = _voices.get(deployment_id)
        if hit and hit[0] > now and not fresh:
            return hit[1]
        try:
            found = await fetch_voices(http, url, secret, sp.voices_path)
        except (httpx.HTTPError, GatewayError):
            return [{"id": v, "name": v} for v in sp.model_voices] if sp.model_voices else None
        _voices[deployment_id] = (now + VOICES_TTL, found)
        return found


# ------------------------------------------------------------------ the audio's length
def _rate_from(ctype: str) -> dict:
    """`audio/pcm;rate=24000;encoding=signed-int;bits=16` -> {"rate": 24000, "bits": 16, …}."""
    out = {}
    for part in (ctype or "").split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.lower() in ("rate", "bits", "channels") and v.strip().isdigit():
            out[k.lower()] = int(v.strip())
        elif k.lower() == "encoding":
            out["encoding"] = v.strip().lower()
    return out


@dataclass
class Meter:
    """Counts the milliseconds of audio in an answer as it passes, by its format."""

    fmt: str
    content_type: str = ""
    headers: dict = field(default_factory=dict)
    pcm_rate: int = 24000
    asked_rate: Optional[int] = None
    total: int = 0
    head: bytes = b""
    kept: bytearray = field(default_factory=bytearray)
    overflow: bool = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        if len(self.head) < 8192:
            self.head += chunk[:8192 - len(self.head)]
        if self.fmt in ("mp3", "aac", "opus", "flac") and not self.overflow:
            if len(self.kept) + len(chunk) > MAX_METER_BYTES:
                self.overflow = True
                self.kept = bytearray()
            else:
                self.kept += chunk

    def ms(self) -> Optional[int]:
        try:
            if self.fmt == "pcm":
                return self._pcm(self.total)
            if self.fmt == "wav":
                return wav_ms(self.head, self.total)
            if self.overflow:
                return None
            data = bytes(self.kept)
            if self.fmt == "mp3":
                return mp3_ms(data)
            if self.fmt == "flac":
                return flac_ms(data)
            if self.fmt == "opus":
                return ogg_opus_ms(data)
            if self.fmt == "aac":
                return adts_ms(data)
        except (ValueError, struct.error, IndexError, ZeroDivisionError):
            return None
        return None

    def _pcm(self, n: int) -> Optional[int]:
        info = _rate_from(self.content_type)
        h = {k.lower(): v for k, v in self.headers.items()}
        rate = _int(h.get("x-audio-sample-rate")) or info.get("rate") or self.asked_rate or self.pcm_rate
        channels = _int(h.get("x-audio-channels")) or info.get("channels") or 1
        bits = info.get("bits") or (32 if info.get("encoding") == "float" else 16)
        per_second = rate * channels * bits // 8
        return int(n * 1000 / per_second) if per_second else None


def _int(v) -> Optional[int]:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def wav_ms(head: bytes, total: int) -> Optional[int]:
    """From a RIFF/WAVE header: the bytes after the data chunk's header over the byte rate. A streamed
    WAV declares an unknown size (0 or 0xFFFFFFFF), so the bytes that came are counted, not the field."""
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos, rate = 12, None
    while pos + 8 <= len(head):
        cid, size = head[pos:pos + 4], struct.unpack("<I", head[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            channels, sr = struct.unpack("<HI", head[pos + 10:pos + 16])
            bits = struct.unpack("<H", head[pos + 22:pos + 24])[0]
            rate = sr * channels * bits // 8
        elif cid == b"data":
            if not rate:
                return None
            data = total - (pos + 8)
            if 0 < size < 0xFFFFFFFF and size <= data:
                data = size
            return int(max(0, data) * 1000 / rate)
        pos += 8 + size + (size & 1)
    return None


_MP3_BITRATES = {  # (version 1?, layer) -> kbps by index
    (True, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (False, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (True, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (False, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (True, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (False, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
}
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def mp3_ms(data: bytes) -> Optional[int]:
    """Counts MPEG audio frames (after an ID3v2 tag) and their samples. Answers made of several MP3
    files one after the other (speaches streams one per sentence) are counted whole."""
    pos, samples, rate_seen = 0, 0.0, None
    n = len(data)
    while pos + 4 <= n:
        if data[pos:pos + 3] == b"ID3" and pos + 10 <= n:
            size = (data[pos + 6] << 21) | (data[pos + 7] << 14) | (data[pos + 8] << 7) | data[pos + 9]
            pos += 10 + size
            continue
        b1, b2 = data[pos + 1], data[pos + 2]
        if data[pos] != 0xFF or (b1 & 0xE0) != 0xE0:
            pos += 1
            continue
        version = (b1 >> 3) & 3          # 3: MPEG1, 2: MPEG2, 0: MPEG2.5
        layer = 4 - ((b1 >> 1) & 3)      # 1, 2, 3
        br_i, sr_i, pad = (b2 >> 4) & 15, (b2 >> 2) & 3, (b2 >> 1) & 1
        if version == 1 or layer == 4 or br_i in (0, 15) or sr_i == 3:
            pos += 1
            continue
        v1 = version == 3
        kbps = _MP3_BITRATES[(v1, layer)][br_i]
        rate = _MP3_RATES[version][sr_i]
        if layer == 1:
            length, per = (12 * kbps * 1000 // rate + pad) * 4, 384
        else:
            per = 1152 if (layer == 2 or v1) else 576
            length = per // 8 * kbps * 1000 // rate + pad
        if length < 4:
            pos += 1
            continue
        samples += per / rate
        rate_seen = rate
        pos += length
    return round(samples * 1000) if rate_seen else None


def flac_ms(data: bytes) -> Optional[int]:
    if len(data) < 42 or data[:4] != b"fLaC":
        return None
    info = data[8:42]   # STREAMINFO, the first metadata block
    rate = (info[10] << 12) | (info[11] << 4) | (info[12] >> 4)
    total = ((info[13] & 0x0F) << 32) | struct.unpack(">I", info[14:18])[0]
    return int(total * 1000 / rate) if rate and total else None


def ogg_opus_ms(data: bytes) -> Optional[int]:
    """The last Ogg page's granule position (48 kHz samples for Opus) minus the pre-skip."""
    head = data.find(b"OpusHead")
    pre_skip = struct.unpack("<H", data[head + 10:head + 12])[0] if head >= 0 else 0
    last = data.rfind(b"OggS")
    while last >= 0:
        if last + 14 <= len(data):
            granule = struct.unpack("<q", data[last + 6:last + 14])[0]
            if granule > 0:
                return int(max(0, granule - pre_skip) * 1000 / 48000)
        last = data.rfind(b"OggS", 0, last)
    return None


_AAC_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)


def adts_ms(data: bytes) -> Optional[int]:
    pos, samples, n = 0, 0.0, len(data)
    seen = False
    while pos + 7 <= n:
        if data[pos] != 0xFF or (data[pos + 1] & 0xF6) != 0xF0:
            pos += 1
            continue
        sr_i = (data[pos + 2] >> 2) & 15
        length = ((data[pos + 3] & 3) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
        if sr_i >= len(_AAC_RATES) or length < 7:
            pos += 1
            continue
        blocks = (data[pos + 6] & 3) + 1
        samples += 1024 * blocks / _AAC_RATES[sr_i]
        seen = True
        pos += length
    return round(samples * 1000) if seen else None


def sse_audio(event: dict) -> tuple[int, Optional[int], Optional[int], Optional[str]]:
    """(bytes of audio, sample rate, channels, format) in a `speech.audio.delta` event."""
    if event.get("type") != "speech.audio.delta" or not isinstance(event.get("audio"), str):
        return 0, None, None, None
    b64 = event["audio"]
    size = len(b64) * 3 // 4 - b64[-2:].count("=") if b64 else 0
    return size, _int(event.get("sample_rate")), _int(event.get("channels")), event.get("response_format")
