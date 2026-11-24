"""Video generation at /v1/videos (1.3): OpenAI's Videos API, the request shape and the job object.

Videos take minutes, so they are jobs (forgeyard/gateway/videojobs.py does the work): POST /v1/videos
answers at once with a job (`queued`), the client polls GET /v1/videos/{id} until it is `completed`
or `failed`, and fetches the MP4 from GET /v1/videos/{id}/content. This module is what is checked
before anything is sent, what the engine is asked, and what a job looks like to the client.

- **Checks** (400 before anything is sent): `prompt` is a non-empty string; `seconds` a whole number of
  seconds (OpenAI sends it as a string: "4" and 4 are the same); `size` is `WIDTHxHEIGHT`; the
  extensions `num_inference_steps`, `guidance_scale`, `seed` and `negative_prompt` of the right types.
  OpenAI's `quality` and `user` are accepted and change nothing.
- **A start image** (1.5.5, #143: image to video) is OpenAI's `input_reference`: a file in a multipart request, or
  in JSON `{"image_url": …}`, a base64 data: URL or an http(s) URL that **the gateway fetches** under the media rules
  (forgeyard/gateway/media.py), only once the key may use the model; a `file_id` is refused. It gets the checks of an
  edit's input images (forgeyard/gateway/imageedit.py): at most MEDIA_FETCH_MAX_MB, a PNG, JPEG or WebP by its first
  bytes, its size read from its header (never decoded) and within the deployment's `max_input_side`. What the model
  does comes first, once the key may use it and before the image is checked or its URL fetched: a model that needs
  one (`meta.video.image_input` required) refuses a request without it (`input_reference_required`); one that takes
  none refuses a request with it (`input_reference_not_supported`), fetching nothing. vLLM-Omni's `image_reference`,
  `video_reference`, `audio_reference` and `input_references` stay refused: the engine would fetch those URLs itself
  (following redirects) or read videos and sound.
- **Per deployment** (each with a working default, from its parameters): the longest video
  (`max_seconds`), the largest side (`max_side`, 0: the model's own) and no more pixels than the
  default size scaled to it, sizes a multiple of what the model takes (16 for Wan2.1, 32 for Wan2.2-5B),
  the most steps and the longest prompt. A request over a cap gets 400 saying what the deployment takes.
  A size or length left out is the deployment's default, always sent, so what is made and counted is
  known.
- **Frames**: seconds x the deployment's frames per second, rounded to the frame counts the model makes
  (Wan: 4k + 1, so 5 s at 16 fps is 81 frames, 5.06 s).
- **Only known fields reach the engine**: prompt, model, size, num_frames, fps, and the extensions
  above, and the checked start image as the file `input_reference` (never a URL). Anything else a client sends
  (vLLM-Omni's `lora`, `extra_params`, frame interpolation with a model path, other references, other files) is left
  out. The size is always sent: the engine would otherwise make the start image's size.
- **The job object** keeps OpenAI's shape: `id` (Forgeyard's own, `video_` and 32 random hex digits, never
  the engine's), `object: "video"`, `model`, `status` (queued, in_progress, completed, failed),
  `progress` (vLLM-Omni reports 0 until it is done, then 100), `created_at`, `completed_at`,
  `expires_at`, `size`, `seconds` (a string, as OpenAI's), `error` ({code, message} once failed). The
  prompt is not kept.
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Optional

from forgeyard.gateway import imageedit, multipart
from forgeyard.gateway.errors import GatewayError

ENDPOINT = "videos"
STATUSES = ("queued", "in_progress", "completed", "failed")
ACTIVE = ("queued", "in_progress")
MIN_SIDE = 64
MAX_SIDE = 2048
MAX_SECONDS = 60
DEFAULT_MAX_PROMPT = 2000
ID_RE = re.compile(r"^video_[0-9a-f]{32}$")
_SIZE_RE = re.compile(r"^\s*(\d{1,5})\s*[xX×]\s*(\d{1,5})\s*$")
START = "input_reference"   # OpenAI's start image (1.5.5)
REFERENCES = ("image_reference", "video_reference", "audio_reference", "input_references")   # never taken
DEFAULT_INPUT_SIDE = 2048
VARIANTS = ("video", "thumbnail", "spritesheet")


def new_id() -> str:
    """An opaque job id: 128 random bits, so another key cannot guess one."""
    return "video_" + secrets.token_hex(16)


# ------------------------------------------------------------------ what a deployment makes
@dataclass(frozen=True)
class Films:
    """A video deployment's settings at the gateway: from its recipe's `video` block, its parameters and
    its model (forgeyard/models/estimate.py video_shape)."""

    width: int = 832
    height: int = 480
    multiple: int = 16
    max_side: int = 832
    max_pixels: int = 832 * 480
    seconds: int = 5
    max_seconds: int = 5
    fps: int = 16
    step: int = 4
    steps: int = 0                   # sent when a request gives none (0: the engine's default)
    max_steps: int = 50
    max_prompt: int = DEFAULT_MAX_PROMPT
    queue: int = 8                   # jobs waiting or being made on the deployment, at most
    per_key: int = 2                 # of those, one key's (or one person's in the Playground)
    keep: int = 86400                # seconds a finished video can be fetched
    timeout: int = 3600              # seconds a job may take from when it was asked
    gpus: int = 1
    image_input: str = "none"        # 1.5.5: none | optional | required (forgeyard/models/videoinput.py)
    max_input_side: int = DEFAULT_INPUT_SIDE   # the widest or tallest start image it takes


def _int(v, default: int, low: int = 0) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= low else default


def films(recipe, params: dict, meta: Optional[dict] = None) -> Films:
    from forgeyard.models import videoinput
    from forgeyard.models.estimate import video_shape

    vs = video_shape(meta or {}, params, recipe.videogen.frames_step)
    return Films(width=vs.width, height=vs.height, multiple=vs.multiple, max_side=vs.max_side,
                 max_pixels=vs.max_pixels, seconds=vs.seconds, max_seconds=vs.max_seconds, fps=vs.fps, step=vs.step,
                 steps=_int(params.get("steps"), 0), max_steps=_int(params.get("max_steps"), 50, 1),
                 max_prompt=_int(params.get("max_prompt_chars"), DEFAULT_MAX_PROMPT, 1),
                 queue=_int(params.get("queue"), 8, 1), per_key=_int(params.get("jobs_per_key"), 2, 1),
                 keep=_int(params.get("keep_minutes"), 1440, 1) * 60, timeout=_int(params.get("timeout"), 3600, 1),
                 gpus=max(1, _int(params.get("gpus"), 1)), image_input=videoinput.of(meta),
                 max_input_side=_int(params.get("max_input_side"), DEFAULT_INPUT_SIDE, 1))


def start_image(fs: list[Films]) -> tuple[str, int]:
    """(none | optional | required, the largest side) of a start image over a route's deployments (1.5.5): required
    only when every one needs it, optional when any takes it, and the largest side any of those takes (0: none does)."""
    takers = [f for f in fs if f.image_input != "none"]
    kind = "required" if fs and all(f.image_input == "required" for f in fs) else "optional" if takers else "none"
    return kind, max((f.max_input_side for f in takers), default=0)


def frames_of(seconds: int, f: Films) -> int:
    from forgeyard.models.estimate import frames_for

    return frames_for(seconds, f.fps, f.step)


# ------------------------------------------------------------------ the request
@dataclass
class Ask:
    """A checked video request."""

    prompt: str
    seconds: Optional[int] = None        # None: the deployment's default
    size: Optional[tuple] = None         # (width, height); None: the deployment's default
    steps: Optional[int] = None
    guidance: Optional[float] = None
    seed: Optional[int] = None
    negative: Optional[str] = None
    image: Optional[imageedit.Input] = None   # the start image (1.5.5): its bytes, or a URL still to fetch

    @property
    def inputs(self) -> imageedit.Inputs:
        return imageedit.Inputs([self.image] if self.image is not None else [])


def _bad(message: str, param: str, code: str = "invalid_request") -> GatewayError:
    return GatewayError(400, code, message, param=param)


def read_body(body: bytes, content_type: str) -> dict:
    """The request's fields from JSON, multipart/form-data (OpenAI's SDK sends multipart) or a URL-encoded form
    (curl -d): a dict of name -> value, where a file part's value is the marker {"file": name} (1.5.5: with its
    bytes as "data" for the start image, `input_reference`; more than one of those is refused)."""
    ctype = (content_type or "").lower()
    if "multipart/form-data" in ctype:
        try:
            _, parts = multipart.parse(body, content_type)
        except multipart.MultipartError as e:
            raise GatewayError(400, "invalid_request", f"The multipart body could not be read: {e}.") from None
        out: dict = {}
        for p in parts:
            if p.is_file and p.name == START:
                if START in out:
                    raise _bad("A video takes one start image: send one input_reference file.", START,
                               "too_many_input_images")
                out[START] = {"file": START, "data": p.content}
            elif p.is_file:
                out[p.name] = {"file": p.name}
            elif p.name not in out:
                out[p.name] = p.content.decode("utf-8", "replace").strip()
        return out
    if "application/x-www-form-urlencoded" in ctype:
        from urllib.parse import parse_qsl

        try:
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=64)
        except (UnicodeDecodeError, ValueError):
            raise GatewayError(400, "invalid_request", "The form could not be read.") from None
        out = {}
        for k, v in pairs:
            out.setdefault(k, v.strip())
        return out
    try:
        obj = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        raise GatewayError(400, "invalid_json", "The request body is not valid JSON (or send multipart/form-data).") \
            from None
    if not isinstance(obj, dict):
        raise GatewayError(400, "invalid_request", "The request body must be a JSON object.")
    return obj


def _whole(obj: dict, name: str, low: int, high: Optional[int] = None) -> Optional[int]:
    v = obj.get(name)
    if v is None or v == "":
        return None
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d{1,20}\s*", v):
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or v < low or (high is not None and v > high):
        rng = f"from {low} to {high}" if high is not None else f"of at least {low}"
        raise _bad(f"{name} must be a whole number {rng}.", name)
    return v


def _number(obj: dict, name: str, low: float, high: float) -> Optional[float]:
    v = obj.get(name)
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            v = None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= float(v) <= high:
        raise _bad(f"{name} must be a number from {low:g} to {high:g}.", name)
    return float(v)


def _start(value) -> Optional[imageedit.Input]:
    """The start image of a request (1.5.5): a multipart file, or OpenAI's JSON {"image_url": …} (a data: URL or an
    http(s) URL, fetched later, once the key may use the model)."""
    if value in (None, "", {}):
        return None
    if isinstance(value, dict) and "data" in value and value.get("file") == START:
        return imageedit.Input(data=value["data"])
    if isinstance(value, dict):
        return imageedit.read_item(value, START)
    raise _bad("input_reference is the start image: a file in a multipart/form-data request, or in JSON an object "
               "{\"image_url\": \"data:image/png;base64,…\" or an https URL}.", START)


def check(obj: dict) -> Ask:
    """Validate a video request's fields (400 for what no deployment should get). Mutates nothing."""
    for name in REFERENCES:
        if obj.get(name) not in (None, "", [], {}):
            raise GatewayError(400, "input_reference_not_supported",
                               f"{name} is not taken: the engine would fetch or read it itself. Send a start image as "
                               "input_reference (a file, or {\"image_url\": …}); video and sound references are not "
                               "taken.", param=name)
    image = _start(obj.get(START))
    prompt = obj.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _bad("The request needs a 'prompt': the text that describes the video.", "prompt")
    neg = obj.get("negative_prompt")
    if neg is not None and not isinstance(neg, str):
        raise _bad("negative_prompt is text.", "negative_prompt")
    seconds = _whole(obj, "seconds", 1, MAX_SECONDS)
    size = obj.get("size")
    wh = None
    if size not in (None, "", "auto"):
        m = _SIZE_RE.match(size) if isinstance(size, str) else None
        if not m:
            raise _bad("size is WIDTHxHEIGHT in pixels (for example 832x480).", "size", "invalid_size")
        wh = (int(m.group(1)), int(m.group(2)))
        if min(wh) < MIN_SIDE or max(wh) > MAX_SIDE:
            raise _bad(f"Each side of size is {MIN_SIDE} to {MAX_SIDE} pixels.", "size", "invalid_size")
    steps = _whole(obj, "num_inference_steps", 1)
    seed = _whole(obj, "seed", -(2**63), 2**63 - 1)
    return Ask(prompt=prompt, seconds=seconds, size=wh, steps=steps, guidance=_number(obj, "guidance_scale", 0, 20),
               seed=seed, negative=neg if neg else None, image=image)


def offered(name: str, ask: Ask, all_f: list[Films]) -> None:
    """What the model does with a start image comes first (1.5.5), before the image is checked or its URL fetched: a
    start image to a model none of whose deployments takes one is 400 `input_reference_not_supported`. Called once the
    key's access is checked. (A request without one to a model that needs one has nothing to check or fetch: `takes`
    answers `input_reference_required`; a route that mixes kinds is sorted out per deployment there too.)"""
    if ask.image is not None and all(f.image_input == "none" for f in all_f):
        raise refusal(name, "input_reference_not_supported", ask, all_f)


def takes(f: Optional[Films], ask: Ask) -> Optional[str]:
    """Why this deployment cannot take the request (an error code), or None."""
    if f is None:
        return None
    if ask.image is None and f.image_input == "required":
        return "input_reference_required"
    if ask.image is not None and f.image_input == "none":
        return "input_reference_not_supported"
    if ask.image is not None and max(ask.image.width, ask.image.height) > f.max_input_side:
        return "input_image_too_large"
    if len(ask.prompt) > f.max_prompt or len(ask.negative or "") > f.max_prompt:
        return "prompt_too_long"
    if ask.seconds is not None and ask.seconds > f.max_seconds:
        return "video_too_long"
    if ask.size is not None:
        if max(ask.size) > f.max_side or ask.size[0] * ask.size[1] > f.max_pixels:
            return "video_too_large"
        if ask.size[0] % f.multiple or ask.size[1] % f.multiple:
            return "invalid_size"
    if ask.steps is not None and ask.steps > f.max_steps:
        return "too_many_steps"
    return None


# A deployment that takes the start image says more about what to change than one that takes none, so the image's
# codes come last.
ORDER = ("prompt_too_long", "video_too_long", "video_too_large", "invalid_size", "too_many_steps",
         "input_image_too_large", "input_reference_required", "input_reference_not_supported")


def refusal(name: str, code: str, ask: Ask, all_f: list[Films]) -> GatewayError:
    """The error for a request no deployment of the route can take, saying what would work."""
    if code == "input_reference_not_supported":
        return _bad(f"{name!r} makes videos from text only: leave input_reference out, or use a model that takes a "
                    "start image (Wan2.2-TI2V-5B).", START, code)
    if code == "input_reference_required":
        return _bad(f"{name!r} makes videos from a start image: send it as input_reference (a file, or "
                    "{\"image_url\": \"data:image/png;base64,…\" or an https URL}).", START, code)
    if code == "input_image_too_large":
        most = max(f.max_input_side for f in all_f if f.image_input != "none")
        return _bad(f"{name!r} takes start images up to {most} pixels on a side; this one is "
                    f"{ask.image.width}x{ask.image.height}. Scale it down first.", START, code)
    if code == "prompt_too_long":
        most = max(f.max_prompt for f in all_f)
        return _bad(f"The prompt is {max(len(ask.prompt), len(ask.negative or ''))} characters; {name!r} takes at most "
                    f"{most}.", "prompt", code)
    if code == "video_too_long":
        most = max(f.max_seconds for f in all_f)
        return _bad(f"{name!r} makes videos of up to {most} s; this one asks for {ask.seconds} s.", "seconds", code)
    if code == "video_too_large":
        f = max(all_f, key=lambda x: x.max_pixels)
        return _bad(f"{name!r} makes videos of up to {f.max_side} pixels on a side and {f.max_pixels:,} pixels a frame "
                    f"(for example {f.width}x{f.height}); this one asks for {ask.size[0]}x{ask.size[1]}.", "size", code)
    if code == "invalid_size":
        m = min(f.multiple for f in all_f)
        return _bad(f"{name!r} takes sizes whose sides are multiples of {m} (for example "
                    f"{max(m, (ask.size[0] // m) * m)}x{max(m, (ask.size[1] // m) * m)}).", "size", code)
    most = max(f.max_steps for f in all_f)
    return _bad(f"{name!r} takes at most {most} steps (num_inference_steps); this one asks for {ask.steps}.",
                "num_inference_steps", code)


# ------------------------------------------------------------------ what the engine is asked
def size_of(ask: Ask, f: Films) -> tuple[int, int]:
    return ask.size if ask.size is not None else (f.width, f.height)


def seconds_of(ask: Ask, f: Films) -> int:
    return ask.seconds if ask.seconds is not None else f.seconds


def upstream_fields(ask: Ask, f: Films, upstream: str) -> dict:
    """The fields vLLM-Omni's POST /v1/videos gets: only those Forgeyard knows."""
    w, h = size_of(ask, f)
    steps = ask.steps if ask.steps is not None else (f.steps if f.steps > 0 else None)
    out = {"prompt": ask.prompt, "model": upstream, "size": f"{w}x{h}", "num_frames": str(frames_of(seconds_of(ask, f), f)),
           "fps": str(f.fps)}
    for name, value in (("num_inference_steps", steps), ("guidance_scale", ask.guidance), ("seed", ask.seed),
                        ("negative_prompt", ask.negative)):
        if value is not None:
            out[name] = str(value)
    return out


def form(fields: dict, image: Optional[imageedit.Input] = None) -> tuple[bytes, str]:
    """multipart/form-data of plain text fields and, for image to video (1.5.5), the checked start image as the file
    `input_reference`: (body, content type)."""
    boundary = "forgeyard-" + uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode() +
                   str(value).encode("utf-8") + b"\r\n")
    if image is not None:
        ext = "jpg" if image.fmt == "jpeg" else image.fmt
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{START}\"; filename=\"start.{ext}\"\r\n"
                   f"Content-Type: {imageedit.INPUT_TYPES[image.fmt]}\r\n\r\n".encode() + image.data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# ------------------------------------------------------------------ the engine's answers
@dataclass
class Seen:
    """What the engine said about a job."""

    status: str                      # queued | in_progress | completed | failed
    progress: int = 0
    duration_ms: Optional[int] = None
    inference_ms: Optional[int] = None
    error: Optional[str] = None


def read_job(status: int, data: bytes) -> Optional[Seen]:
    """vLLM-Omni's job record (GET /v1/videos/{id}; a failed job answers its error's status with the job as
    the body), or None when the answer is not one."""
    try:
        obj = json.loads(data or b"null")
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("status") not in STATUSES:
        return None
    if status >= 400 and obj["status"] != "failed":
        return None
    progress = obj.get("progress")
    progress = max(0, min(100, int(progress))) if isinstance(progress, (int, float)) and not isinstance(progress, bool) else 0
    dur = obj.get("duration_s")
    inf = obj.get("inference_time_s")
    err = obj.get("error")
    message = None
    if isinstance(err, dict) and err.get("message"):
        message = str(err["message"])[:300]
    elif obj["status"] == "failed":
        message = "The engine reports the job failed."
    return Seen(status=obj["status"], progress=progress,
                duration_ms=int(dur * 1000) if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0 else None,
                inference_ms=int(inf * 1000) if isinstance(inf, (int, float)) and not isinstance(inf, bool) and inf > 0 else None,
                error=message)


def engine_id(data: bytes) -> Optional[str]:
    """The id of the job the engine made (POST /v1/videos), or None."""
    try:
        obj = json.loads(data or b"null")
    except (ValueError, UnicodeDecodeError):
        return None
    jid = obj.get("id") if isinstance(obj, dict) else None
    return jid if isinstance(jid, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", jid) else None


# ------------------------------------------------------------------ what clients see
def _ts(dt) -> Optional[int]:
    from forgeyard.db import as_utc

    return int(as_utc(dt).timestamp()) if dt is not None else None


ERROR_MESSAGES = {
    "replica_restarted": "The replica making this video restarted, and its jobs went with it (the engine keeps them "
                         "in memory). Ask for the video again.",
    "replica_gone": "The replica making this video is gone (its deployment was stopped, restarted or moved), and its "
                    "jobs went with it. Ask for the video again.",
    "video_lost": "The replica making this video no longer knows the job (its engine restarted). Ask for the video "
                  "again.",
    "timed_out": "The video was not done within the deployment's time for a job (its timeout), so it was stopped. Ask "
                 "for a shorter or smaller video, or ask an admin to raise the deployment's timeout.",
}


def video_object(job) -> dict:
    """A job (forgeyard.db.VideoJob) as OpenAI's Video object."""
    out = {"id": job.public_id, "object": "video", "model": job.model, "status": job.status,
           "progress": 100 if job.status == "completed" else int(job.progress or 0),
           "created_at": _ts(job.created_at), "completed_at": _ts(job.completed_at), "expires_at": _ts(job.expires_at),
           "size": job.size, "seconds": str(job.seconds), "quality": "standard", "remixed_from_video_id": None,
           "error": None}
    if job.status == "failed":
        out["error"] = {"code": job.error_code or "generation_failed", "message": job.error or "The video failed."}
    return out
