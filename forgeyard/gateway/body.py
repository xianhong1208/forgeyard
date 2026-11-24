"""The request body (forgeyard/gateway/proxy.py): read within the largest limit any deployment takes,
parsed (JSON, multipart, or a vision request's image), and rebuilt for the engine with its model's name."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from starlette.requests import Request

from forgeyard.config import get_settings
from forgeyard.db import Deployment, new_session
from forgeyard.gateway import embeddings, imagegen, media as gmedia, multipart, speech
from forgeyard.gateway.errors import GatewayError

USAGE_ENDPOINTS = ("chat/completions", "completions")  # streamed ones can report usage at the end


def streams(parsed: "Parsed") -> bool:
    """Does the client ask for its answer as a stream (the first-answer wait applies only then)?"""
    if parsed.tts is not None:
        return parsed.tts.stream_format is not None
    return parsed.stream


# ------------------------------------------------------------------ the request body
@dataclass
class Parsed:
    model: str
    stream: bool = False
    max_tokens: Optional[int] = None
    obj: Optional[dict] = None                     # a JSON body
    parts: Optional[list] = None                   # a multipart body
    boundary: Optional[bytes] = None
    media: Optional[gmedia.Media] = None           # images, audio, video, files (1.3)
    tts: Optional[speech.Ask] = None               # a text-to-speech request, checked (1.3)
    image: Optional[imagegen.Ask] = None           # an image request, checked (1.3)
    vision: Optional[str] = None                   # a vision request: its task (1.4)
    embed: Optional[embeddings.Ask] = None         # an embeddings request: its texts and images (1.5.4)
    size: int = 0                                  # the body's size in bytes (after media were inlined)


# The largest body any deployment takes (its `max_body_mb`), read every few seconds.
CEILING_TTL = 5.0
_ceiling: dict = {"until": 0.0, "bytes": 0}


def forget_ceiling() -> None:
    _ceiling["until"] = 0.0


def body_ceiling() -> int:
    """The most the gateway reads of any request: GATEWAY_MAX_BODY_MB, or a deployment's larger
    `max_body_mb`. Which limit applies is known only once the body says its model (fit_route)."""
    now = time.monotonic()
    limit = get_settings().gateway_max_body_mb * 1024**2
    if now < _ceiling["until"]:
        return max(limit, _ceiling["bytes"])
    db = new_session()
    try:
        most = 0
        for params in db.scalars(select(Deployment.params)).all():
            mb = (params or {}).get("max_body_mb")
            if isinstance(mb, int) and not isinstance(mb, bool) and mb > 0:
                most = max(most, mb * 1024**2)
    finally:
        db.close()
    _ceiling.update(until=now + CEILING_TTL, bytes=most)
    return max(limit, most)


async def read_body(request: Request, limit: Optional[int] = None) -> bytes:
    limit = limit or get_settings().gateway_max_body_mb * 1024**2
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise GatewayError(413, "request_too_large", f"The request is larger than {limit // 1024**2} MiB.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise GatewayError(413, "request_too_large", f"The request is larger than {limit // 1024**2} MiB.")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_vision(body: bytes, content_type: str) -> Parsed:
    """A vision request (1.4): multipart with `model` and an `image` file, or a JSON object with `model`
    and `image` (base64 or a data: URI). The image itself is read by the vision server."""
    if "multipart/form-data" in (content_type or "").lower():
        parsed = parse_body(body, content_type, "multipart", what="the image")
        if not any(p.name == "image" and p.is_file for p in parsed.parts or []):
            raise GatewayError(400, "missing_image", "Send the image as a file field named 'image'.", param="image")
        return parsed
    parsed = parse_body(body, content_type, "json")
    image = (parsed.obj or {}).get("image")
    if not isinstance(image, str) or not image.strip():
        raise GatewayError(400, "missing_image", "Send multipart/form-data with an 'image' file, or JSON with 'image' "
                                                 "(base64 or a data: URI).", param="image")
    if parsed.stream:
        raise GatewayError(400, "streaming_not_supported", "Vision answers do not stream: leave 'stream' out.",
                           param="stream")
    return parsed


def parse_body(body: bytes, content_type: str, kind: str, *, what: str = "the audio") -> Parsed:
    if kind == "vision":
        return parse_vision(body, content_type)
    if kind == "multipart":
        if "multipart/form-data" not in (content_type or "").lower():
            raise GatewayError(400, "invalid_request", f"Send {what} as multipart/form-data.")
        try:
            boundary, parts = multipart.parse(body, content_type)
        except multipart.MultipartError as e:
            raise GatewayError(400, "invalid_request", f"The multipart body could not be read: {e}.") from None
        model = multipart.field(parts, "model")
        if not model:
            raise GatewayError(400, "invalid_request", "The form needs a 'model' field.", param="model")
        stream = (multipart.field(parts, "stream") or "").lower() in ("true", "1")
        return Parsed(model=model, stream=stream, parts=parts, boundary=boundary, size=len(body))
    try:
        obj = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        raise GatewayError(400, "invalid_json", "The request body is not valid JSON.") from None
    if not isinstance(obj, dict):
        raise GatewayError(400, "invalid_request", "The request body must be a JSON object.")
    model = obj.get("model")
    if not isinstance(model, str) or not model.strip():
        raise GatewayError(400, "invalid_request", "The request needs a 'model'.", param="model")
    max_tokens = obj.get("max_completion_tokens", obj.get("max_tokens"))
    max_tokens = max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0 else None
    return Parsed(model=model.strip(), stream=obj.get("stream") is True, max_tokens=max_tokens, obj=obj, size=len(body))


def upstream_body(parsed: Parsed, endpoint: str, upstream_model: str) -> tuple[bytes, bool]:
    """(the body for the engine, whether to strip a usage chunk the gateway asked for)."""
    if parsed.parts is not None:
        return multipart.build(parsed.boundary, parsed.parts, {"model": upstream_model}), False
    obj = dict(parsed.obj or {})
    obj["model"] = upstream_model
    strip = False
    if parsed.stream and endpoint in USAGE_ENDPOINTS:
        options = obj.get("stream_options")
        options = dict(options) if isinstance(options, dict) else {}
        if options.get("include_usage") is not True:
            options["include_usage"] = True
            obj["stream_options"] = options
            strip = True
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), strip
