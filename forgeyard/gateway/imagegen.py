"""Image generation at /v1/images/generations (1.3): OpenAI's request shape in, base64 images out.

What the gateway does with an image request, before and after the engine:

- **Checks** (400 before anything is sent): `prompt` is a non-empty string; `n` a whole number from 1;
  `size` is `WIDTHxHEIGHT` or `auto`; `output_format` png, jpeg or webp; `num_inference_steps`,
  `guidance_scale`, `seed`, `negative_prompt` of the right types. **`response_format: url` is refused**
  (`response_format_not_supported`): Forgeyard keeps no copy of the images it makes, so there is no
  URL to give; leave it out or ask for `b64_json`. Streaming (`stream: true`) is refused too. OpenAI's
  `quality`, `style`, `background`, `moderation` and `user` are accepted and have no effect.
- **Per deployment** (each with a working default, from its parameters): the most images per request
  (`max_images`), the largest side (`max_side`, 0: the model's own) and a size its model takes (a
  multiple of 16 for diffusion transformers, 64 for Stable Diffusion's UNet), the most steps
  (`max_steps`), the longest prompt (`max_prompt_chars`): a request over a cap gets 400 saying what
  the deployment takes. A size left out (or `auto`) is the deployment's default size, always sent, so
  what is made and counted is known.
- **Only known fields reach the engine**: prompt, n, size, the output format, and the extras above
  (vLLM-Omni as fields; stable-diffusion.cpp inside the prompt as `<sd_cpp_extra_args>{…}`, which
  clients may not send themselves: it could ask for any size, hires passes or LoRA files). Anything
  else a client sends (vLLM-Omni's `lora` with a server path, its `extra_params`) is left out.
- **Queued**: a deployment works on `concurrency` requests per replica (1 by default); the others wait
  in the gateway in order, at most `queue` of them; one more gets 429 `queue_full` with Retry-After
  (from how long its requests took lately). A waiting request that the client gives up is dropped.
- **Counted** once answered: the images and their kilopixels (from each image's own header: PNG, JPEG
  or WebP; else the size asked) and the replica's busy milliseconds (the time it worked on the
  request, times the GPUs it holds). The answer keeps OpenAI's shape: `created`, `data[].b64_json`,
  plus `output_format` and `size`; engine extras (the prompt and settings stable-diffusion.cpp
  embeds as `generation_info`, vLLM-Omni's stage timings) are not passed on.
- **No prompt filtering**: Forgeyard checks the request's shape, not what it asks for. A deployment
  that needs a safety filter needs one in front of it.
- **No cloud**: images are made by local deployments only (no provider spill in 1.3).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import struct
import time
from dataclasses import dataclass
from typing import Optional

from forgeyard.gateway.errors import GatewayError

ENDPOINT = "images/generations"
FORMATS = ("png", "jpeg", "webp")
MIN_SIDE = 64
MAX_SIDE = 8192
DEFAULT_MAX_PROMPT = 4000
DEFAULT_TIMEOUT = 600.0
DEFAULT_QUEUE = 8
EXTRA_ARGS_TAG = "<sd_cpp_extra_args>"
_SIZE_RE = re.compile(r"^\s*(\d{1,5})\s*[xX×]\s*(\d{1,5})\s*$")
IGNORED = ("quality", "style", "background", "moderation", "user", "partial_images")


# ------------------------------------------------------------------ what a deployment makes
@dataclass(frozen=True)
class Draws:
    """An image deployment's settings at the gateway: from its recipe's `image` block, its parameters
    and its model."""

    request: str = "fields"          # fields (vLLM-Omni) | sd-cpp
    formats: tuple = ("png",)
    max_n: int = 1
    max_side: int = 1024
    width: int = 1024
    height: int = 1024
    multiple: int = 16
    steps: int = 0                   # sent when a request gives none (0: the engine's default)
    max_steps: int = 50
    max_prompt: int = DEFAULT_MAX_PROMPT
    concurrency: int = 1
    queue: int = DEFAULT_QUEUE
    timeout: float = DEFAULT_TIMEOUT
    gpus: int = 0


def _int(v, default: int, low: int = 0) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= low else default


def draws(recipe, params: dict, meta: Optional[dict] = None) -> Draws:
    from forgeyard.models.estimate import image_caps
    from forgeyard.models.render import default_size

    meta = meta or {}
    img = recipe.imagegen
    side, most = image_caps(meta, params)
    w, h = default_size(meta, params)
    multiple = (meta.get("image") or {}).get("multiple") if isinstance(meta.get("image"), dict) else None
    return Draws(request=img.request, formats=tuple(img.output_formats), max_n=max(1, min(img.max_n, most)),
                 max_side=side, width=w, height=h, multiple=_int(multiple, 16, 1), steps=_int(params.get("steps"), 0),
                 max_steps=_int(params.get("max_steps"), 50, 1),
                 max_prompt=_int(params.get("max_prompt_chars"), DEFAULT_MAX_PROMPT, 1),
                 concurrency=_int(params.get("concurrency"), 1, 1), queue=_int(params.get("queue"), DEFAULT_QUEUE),
                 timeout=float(_int(params.get("timeout"), int(DEFAULT_TIMEOUT), 1)),
                 gpus=_int(params.get("gpus"), 0))


# ------------------------------------------------------------------ the request
@dataclass
class Ask:
    """A checked image request."""

    prompt: str
    n: int = 1
    size: Optional[tuple] = None     # (width, height); None: the deployment's default
    output_format: Optional[str] = None
    compression: Optional[int] = None
    steps: Optional[int] = None
    guidance: Optional[float] = None
    true_cfg: Optional[float] = None
    seed: Optional[int] = None
    negative: Optional[str] = None


def _bad(message: str, param: str, code: str = "invalid_request") -> GatewayError:
    return GatewayError(400, code, message, param=param)


def _number(obj: dict, name: str, low: float, high: float) -> Optional[float]:
    v = obj.get(name)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= float(v) <= high:
        raise _bad(f"{name} must be a number from {low:g} to {high:g}.", name)
    return float(v)


def check(obj: dict) -> Ask:
    """Validate an image request's body (400 for what no deployment should get). Mutates nothing."""
    prompt = obj.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _bad("The request needs a 'prompt': the text that describes the image.", "prompt")
    neg = obj.get("negative_prompt")
    if neg is not None and not isinstance(neg, str):
        raise _bad("negative_prompt is text.", "negative_prompt")
    for name, text in (("prompt", prompt), ("negative_prompt", neg or "")):
        if EXTRA_ARGS_TAG[:-1] in text.lower():
            raise _bad("Engine settings inside the prompt (<sd_cpp_extra_args>) are not accepted. Use the request's "
                       "fields: size, n, num_inference_steps, guidance_scale, seed, negative_prompt.", name)
    fmt = obj.get("response_format")
    if fmt == "url":
        raise GatewayError(400, "response_format_not_supported",
                           "Forgeyard returns images as base64 only (response_format b64_json): it keeps no copy of "
                           "the images it makes, so there is no URL to give. Leave response_format out or set it to "
                           "b64_json, and decode data[].b64_json.", param="response_format")
    if fmt not in (None, "b64_json"):
        raise _bad("response_format must be b64_json (images come back as base64).", "response_format")
    if obj.get("stream") not in (None, False):
        raise GatewayError(400, "streaming_not_supported", "Image generation does not stream here: leave 'stream' "
                                                           "out; the answer comes when the images are made.",
                           param="stream")
    n = obj.get("n", 1)
    if n is None:
        n = 1
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise _bad("n must be a whole number of at least 1.", "n")
    size = obj.get("size")
    wh = None
    if size not in (None, "", "auto"):
        m = _SIZE_RE.match(size) if isinstance(size, str) else None
        if not m:
            raise _bad("size is WIDTHxHEIGHT in pixels (for example 1024x1024), or auto.", "size", "invalid_size")
        wh = (int(m.group(1)), int(m.group(2)))
        if min(wh) < MIN_SIDE or max(wh) > MAX_SIDE:
            raise _bad(f"Each side of size is {MIN_SIDE} to {MAX_SIDE} pixels.", "size", "invalid_size")
    out = obj.get("output_format")
    if out == "jpg":
        out = "jpeg"
    if out is not None and out not in FORMATS:
        raise _bad("output_format must be png, jpeg or webp.", "output_format", "unsupported_output_format")
    comp = obj.get("output_compression")
    if comp is not None and (isinstance(comp, bool) or not isinstance(comp, int) or not 0 <= comp <= 100):
        raise _bad("output_compression is a whole number from 0 to 100.", "output_compression")
    steps = obj.get("num_inference_steps")
    if steps is not None and (isinstance(steps, bool) or not isinstance(steps, int) or steps < 1):
        raise _bad("num_inference_steps must be a whole number of at least 1.", "num_inference_steps")
    seed = obj.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not -(2**63) <= seed < 2**63):
        raise _bad("seed must be a whole number.", "seed")
    return Ask(prompt=prompt, n=n, size=wh, output_format=out, compression=comp, steps=steps,
               guidance=_number(obj, "guidance_scale", 0, 100), true_cfg=_number(obj, "true_cfg_scale", 0, 100),
               seed=seed, negative=neg if neg else None)


def takes(d: Optional[Draws], ask: Ask) -> Optional[str]:
    """Why this deployment cannot take the request (an error code), or None."""
    if d is None:
        return None
    if len(ask.prompt) > d.max_prompt or len(ask.negative or "") > d.max_prompt:
        return "prompt_too_long"
    if ask.n > d.max_n:
        return "too_many_images"
    if ask.size is not None:
        if max(ask.size) > d.max_side:
            return "image_too_large"
        if ask.size[0] % d.multiple or ask.size[1] % d.multiple:
            return "invalid_size"
    if ask.output_format is not None and ask.output_format not in d.formats:
        return "unsupported_output_format"
    if ask.steps is not None and ask.steps > d.max_steps:
        return "too_many_steps"
    return None


ORDER = ("prompt_too_long", "too_many_images", "image_too_large", "invalid_size", "unsupported_output_format",
         "too_many_steps")


def refusal(name: str, code: str, ask: Ask, all_d: list[Draws]) -> GatewayError:
    """The error for a request no deployment of the route can take, saying what would work."""
    if code == "prompt_too_long":
        most = max(d.max_prompt for d in all_d)
        return _bad(f"The prompt is {max(len(ask.prompt), len(ask.negative or ''))} characters; {name!r} takes at most "
                    f"{most}.", "prompt", code)
    if code == "too_many_images":
        most = max(d.max_n for d in all_d)
        return _bad(f"{name!r} makes at most {most} image{'s' if most > 1 else ''} per request; this one asks for "
                    f"{ask.n}.", "n", code)
    if code == "image_too_large":
        most = max(d.max_side for d in all_d)
        return _bad(f"{name!r} makes images up to {most}x{most}; this one asks for {ask.size[0]}x{ask.size[1]}.",
                    "size", code)
    if code == "invalid_size":
        m = min(d.multiple for d in all_d)
        return _bad(f"{name!r} takes sizes whose sides are multiples of {m} (for example "
                    f"{max(m, (ask.size[0] // m) * m)}x{max(m, (ask.size[1] // m) * m)}).", "size", code)
    if code == "unsupported_output_format":
        fmts = sorted({f for d in all_d for f in d.formats}, key=FORMATS.index)
        return _bad(f"{name!r} makes {', '.join(fmts)}, not {ask.output_format}.", "output_format", code)
    most = max(d.max_steps for d in all_d)
    return _bad(f"{name!r} takes at most {most} steps (num_inference_steps); this one asks for {ask.steps}.",
                "num_inference_steps", code)


# ------------------------------------------------------------------ the body for one deployment
def size_of(ask: Ask, d: Optional[Draws]) -> tuple[int, int]:
    if ask.size is not None:
        return ask.size
    return (d.width, d.height) if d is not None else (1024, 1024)


def body_for(ask: Ask, d: Optional[Draws], upstream: str) -> dict:
    """The request body an engine with these settings gets: only fields Forgeyard knows."""
    w, h = size_of(ask, d)
    steps = ask.steps if ask.steps is not None else (d.steps if d is not None and d.steps > 0 else None)
    out: dict = {"model": upstream, "n": ask.n, "size": f"{w}x{h}"}
    fmt = ask.output_format or "png"
    out["output_format"] = fmt
    if ask.compression is not None and fmt != "png":
        out["output_compression"] = ask.compression
    if d is not None and d.request == "sd-cpp":
        extra: dict = {}
        if ask.negative:
            extra["negative_prompt"] = ask.negative
        if ask.seed is not None:
            extra["seed"] = ask.seed
        sample: dict = {}
        if steps is not None:
            sample["sample_steps"] = steps
        if ask.guidance is not None:
            sample["guidance"] = {"txt_cfg": ask.guidance}
        if sample:
            extra["sample_params"] = sample
        prompt = ask.prompt
        if extra:
            # One line, and no '<' or '>' of its own: the engine finds the block with a regular expression.
            blob = json.dumps(extra, ensure_ascii=False, separators=(",", ":"))
            blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("\n", "\\n").replace("\r", "\\r")
            prompt = f"{prompt} {EXTRA_ARGS_TAG}{blob}</sd_cpp_extra_args>"
        out["prompt"] = prompt
        return out
    out["prompt"] = ask.prompt
    out["response_format"] = "b64_json"
    for name, value in (("negative_prompt", ask.negative), ("num_inference_steps", steps),
                        ("guidance_scale", ask.guidance), ("true_cfg_scale", ask.true_cfg), ("seed", ask.seed)):
        if value is not None:
            out[name] = value
    return out


# ------------------------------------------------------------------ the answer
def image_dims(data: bytes) -> Optional[tuple[int, int, str]]:
    """(width, height, format) from the start of a PNG, JPEG or WebP file; None for anything else."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h, "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        kind = data[12:16]
        if kind == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"), "webp"
        if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF, "webp"
        if kind == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, "webp"
        return None
    if data[:2] == b"\xff\xd8":
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                pos += 1 if marker == 0xFF else 2
                continue
            length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
                return w, h, "jpeg"
            pos += 2 + length
    return None


HEAD_B64 = 256 * 1024   # base64 characters decoded to find an image's size (a JPEG's EXIF can come first)


def b64_dims(b64: str) -> Optional[tuple[int, int, str]]:
    head = b64[:HEAD_B64]
    head = head[:len(head) - len(head) % 4]
    try:
        return image_dims(base64.b64decode(head, validate=False))
    except (binascii.Error, ValueError):
        return None


@dataclass
class Made:
    """What an answer held: the images and their kilopixels."""

    images: int = 0
    kilopixels: int = 0
    size: Optional[str] = None
    fmt: Optional[str] = None


def answer(data: bytes, ask: Ask, d: Optional[Draws]) -> tuple[Optional[bytes], Made]:
    """The answer for the client (OpenAI's shape, the engine's extras left out) and what it holds;
    (None, nothing) when the engine's answer is not one."""
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None, Made()
    items = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return None, Made()
    want = size_of(ask, d)
    made = Made()
    out_items = []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("b64_json"), str) or not it["b64_json"]:
            continue
        dims = b64_dims(it["b64_json"])
        w, h = (dims[0], dims[1]) if dims else want
        made.images += 1
        made.kilopixels += round(w * h / 1000)
        made.size = made.size or f"{w}x{h}"
        made.fmt = made.fmt or (dims[2] if dims else None)
        entry = {"b64_json": it["b64_json"]}
        if isinstance(it.get("revised_prompt"), str):
            entry["revised_prompt"] = it["revised_prompt"]
        out_items.append(entry)
    created = obj.get("created")
    out = {"created": created if isinstance(created, int) and not isinstance(created, bool) else int(time.time()),
           "data": out_items, "output_format": made.fmt or ask.output_format or "png",
           "size": made.size or f"{want[0]}x{want[1]}"}
    return json.dumps(out, separators=(",", ":")).encode("utf-8"), made


def engine_error(status: int, data: bytes) -> bytes:
    """An engine's error in OpenAI's shape: sd-server answers {"error": "prompt required"}, vLLM-Omni
    FastAPI's {"detail": …}; one already in OpenAI's shape passes as it is."""
    try:
        obj = json.loads(data or b"null")
    except (ValueError, UnicodeDecodeError):
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict) and obj["error"].get("message"):
        return data
    message = None
    if isinstance(obj, dict):
        for k in ("error", "detail", "message"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                message = v
                break
            if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("msg"):
                message = "; ".join(str(x.get("msg")) for x in v if isinstance(x, dict))[:500]
                break
        if isinstance(obj.get("message"), str) and message and obj.get("error") == "server_error":
            message = obj["message"]
    message = (message or (data[:300].decode("utf-8", "replace") if data else "") or f"HTTP {status}")[:500]
    kind = "invalid_request_error" if 400 <= status < 500 else "upstream_error"
    return json.dumps({"error": {"message": f"The engine answered {status}: {message}", "type": kind, "param": None,
                                 "code": "engine_error"}}).encode("utf-8")


# ------------------------------------------------------------------ the queue
class Queue:
    """Requests waiting for a slot on one route's tier, first come first served."""

    def __init__(self):
        self.waiting: list[int] = []
        self.next = 0

    def join(self, limit: int) -> Optional[int]:
        if len(self.waiting) >= limit:
            return None
        self.next += 1
        self.waiting.append(self.next)
        return self.next

    def first(self, ticket: int) -> bool:
        return bool(self.waiting) and self.waiting[0] == ticket

    def leave(self, ticket: int) -> None:
        if ticket in self.waiting:
            self.waiting.remove(ticket)


_queues: dict[str, Queue] = {}
_busy: dict[str, float] = {}         # route#tier -> seconds a request took lately (a moving average)
BUSY_DEFAULT = 10.0


def queue(key: str) -> Queue:
    return _queues.setdefault(key, Queue())


def note_busy(key: str, seconds: float) -> None:
    old = _busy.get(key)
    _busy[key] = seconds if old is None else 0.7 * old + 0.3 * seconds


def retry_after(key: str, ahead: int, slots: int) -> int:
    """Seconds until a place is likely free: how long requests took lately, for those ahead of it."""
    per = _busy.get(key, BUSY_DEFAULT)
    return max(1, min(600, int(per * (ahead + 1) / max(1, slots) + 0.999)))


def forget() -> None:
    _queues.clear()
    _busy.clear()
