"""Image edits at /v1/images/edits (1.5.5, #143): OpenAI's request in both its shapes, input images checked by the
gateway, base64 images out. Everything after the checks is image generation's (forgeyard/gateway/imagegen.py): the
same caps, queue, timeout, answer and usage.

- **Two request shapes**, as OpenAI's API takes them:
  - multipart/form-data (what the OpenAI SDKs send for `images.edit`): `model`, `prompt`, the images as files named
    `image` or `image[]`, and the plain fields of a generation (`n`, `size`, `output_format`, …);
  - JSON: `images` is a list of `{"image_url": …}`, each a base64 `data:image/…` URL or an http(s) URL, which **the
    gateway fetches** under the media rules (forgeyard/gateway/media.py: egress settings, public addresses only unless
    MEDIA_ALLOW_PRIVATE_HOSTS, every redirect checked, MEDIA_FETCH_MAX_MB), only once the key may use the model. A
    `file_id` is refused (`file_id_not_supported`: Forgeyard keeps no files).
- **No masks** (`mask` as a file or a JSON field: 400 `mask_not_supported`): edits follow the prompt. OpenAI's masks
  mark the area to change with transparent pixels, the engines with white ones, and the recommended models edit from
  the instruction alone.
- **What the model does comes first**: once the key may use the model, a model none of whose deployments edits is
  400 `edits_not_supported`, whatever the request carries (no image is needed to learn that, and no URL is fetched
  for it). Refusals about the request's shape alone (a mask, a `file_id`, a URL of another scheme, a data: URL that is
  not base64, a file under another name) come while it is read, before the model is looked up.
- **Input checks** then, before any deployment is chosen: at least one image (`missing_image`), at most MAX_INPUTS
  (`too_many_input_images`), each at most MEDIA_FETCH_MAX_MB (413 `media_too_large`), a PNG, JPEG or WebP by its
  first bytes (`unsupported_image_format`: HEIC, GIF and the rest; neither engine reads HEIC) whose header gives its
  size (`invalid_image`). The size is read from the header only: no image is decoded here, so a small file that
  claims a huge size costs nothing and is refused by the deployment's cap.
- **Per deployment**: only deployments whose model edits (catalog `meta.image.edit`) and whose recipe serves edits
  take the request; at most its `Input images`
  (`too_many_input_images`), each no wider or taller than its `Largest input` (`input_image_too_large`); then the
  caps of a generation (images per request, size, steps, prompt) and the formats it makes for edits.
- **Always an explicit size**: both engines would make the first input's size when none is sent, so a 12-megapixel
  phone photo would set the memory of the request. A request without a size gets the first input's shape at the
  deployment's default number of pixels, within its largest side, its sides a multiple of what the model takes.
- **The engine's request is rebuilt** from the checked fields and the checked image bytes, sent as `image[]` files:
  never a URL (vLLM-Omni fetches `url` and string `mask_image` / `reference_image` fields itself, with no address
  filter), never a mask, LoRA path or anything else the client sent.
- **Counted** like a generation (the images made, their kilopixels, busy milliseconds), plus the input images
  (`media_items`) and their bytes (`bytes_in`). Nothing of the request is kept: no input image is logged or stored.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Optional

from forgeyard.config import get_settings
from forgeyard.gateway import imagegen, media as gmedia, multipart
from forgeyard.gateway.errors import GatewayError
from forgeyard.models.editing import MAX_INPUTS

ENDPOINT = "images/edits"
INPUT_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
FILE_FIELDS = ("image", "image[]")
INTS = ("n", "output_compression", "num_inference_steps", "seed")
FLOATS = ("guidance_scale", "true_cfg_scale")
_DATA_RE = re.compile(r"^data:(image/[a-z0-9.+-]+)(?:;[a-z0-9-]+=[^;,]*)*;base64,", re.I)
_INT_RE = re.compile(r"^\s*-?\d{1,20}\s*$")


# ------------------------------------------------------------------ the input images
@dataclass
class Input:
    """One input image: its bytes (once fetched) and what its header says."""

    data: bytes = b""
    url: Optional[str] = None         # an http(s) URL the gateway fetches (JSON requests)
    width: int = 0
    height: int = 0
    fmt: str = ""


@dataclass
class Inputs:
    """The input images of an edit request, in order (the first sets the shape of what is made)."""

    items: list[Input] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(len(i.data) for i in self.items)


def _bad(message: str, param: str, code: str = "invalid_request") -> GatewayError:
    return GatewayError(400, code, message, param=param)


def _mask() -> GatewayError:
    return _bad("Masks are not taken: describe the change in the prompt and send the whole image. (Inpainting with a "
                "mask is not offered yet.)", "mask", "mask_not_supported")


def _missing() -> GatewayError:
    return _bad("The request needs the image to edit: a file named 'image' (or 'image[]' for several) in a "
                "multipart/form-data request, or JSON 'images': [{\"image_url\": \"data:image/png;base64,…\" or an "
                "https URL}].", "image", "missing_image")


def _typed(fields: dict) -> dict:
    """A form's fields with the numbers of a generation request read as numbers (a form sends text); a field that is
    not one is left as text, for the checks to refuse."""
    out = dict(fields)
    for name in INTS:
        if isinstance(out.get(name), str) and _INT_RE.match(out[name]):
            out[name] = int(out[name])
    for name in FLOATS:
        if isinstance(out.get(name), str) and out[name].strip():
            try:
                out[name] = float(out[name])
            except ValueError:
                pass
    if isinstance(out.get("stream"), str):
        out["stream"] = out["stream"].strip().lower() in ("true", "1")
    for name in ("size", "output_format", "response_format"):
        if out.get(name) == "":
            out[name] = None
    return out


def read_form(body: bytes, content_type: str) -> tuple[dict, Inputs]:
    """(the plain fields, the input images) of a multipart edit request."""
    try:
        _, parts = multipart.parse(body, content_type)
    except multipart.MultipartError as e:
        raise GatewayError(400, "invalid_request", f"The multipart body could not be read: {e}.") from None
    fields: dict = {}
    inputs = Inputs()
    for p in parts:
        if p.name == "mask" and (p.is_file or p.content.strip()):
            raise _mask()
        if p.is_file:
            if p.name not in FILE_FIELDS:
                raise _bad(f"The file field {p.name[:40]!r} is not taken: send the images as 'image' or 'image[]'.",
                           p.name[:40])
            inputs.items.append(Input(data=p.content))
        elif p.name not in fields:
            fields[p.name] = p.content.decode("utf-8", "replace").strip()
    return _typed(fields), inputs


def read_json(obj: dict) -> Inputs:
    """The input images of a JSON edit request (OpenAI's `images`: a list of {image_url} or {file_id})."""
    if obj.get("mask") not in (None, "", {}):
        raise _mask()
    images = obj.get("images")
    if images is None:
        return Inputs()     # none: refused once the model is known (`count`), as an edit to a model that edits none is
    if not isinstance(images, list):
        raise _bad("images is a list of objects with an image_url.", "images")
    inputs = Inputs()
    for item in images[:MAX_INPUTS + 1]:
        if not isinstance(item, dict):
            raise _bad("Each entry of images is an object with an image_url.", "images")
        if item.get("file_id") not in (None, ""):
            raise _bad("file_id is not taken: Forgeyard keeps no uploaded files. Send the image itself as image_url "
                       "(a base64 data: URL or an https URL), or as a multipart file.", "images", "file_id_not_supported")
        url = item.get("image_url")
        if isinstance(url, dict):
            url = url.get("url")
        if not isinstance(url, str) or not url.strip():
            raise _bad("Each entry of images needs an image_url: a base64 data: URL or an http(s) URL.", "images")
        url = url.strip()
        head = url[:16].lower()
        if head.startswith("data:"):
            m = _DATA_RE.match(url[:300])
            if not m:
                raise _bad("An image_url data: URL is base64 data of an image (data:image/png;base64,…).", "images",
                           "invalid_image")
            try:
                data = base64.b64decode(url[m.end():], validate=True)
            except (binascii.Error, ValueError):
                raise _bad("An image_url data: URL does not hold valid base64.", "images", "invalid_image") from None
            inputs.items.append(Input(data=data))
        elif head.startswith(("https://", "http://")):
            inputs.items.append(Input(url=url))
        else:
            raise _bad("image_url is a base64 data: URL or an http(s) URL; other kinds are refused.", "images",
                       "media_url_refused")
    return inputs


def offered(route) -> None:
    """400 `edits_not_supported` unless some deployment of the route edits: what the model does comes first, before
    anything about the images (whether there are any, fetching URLs). Called once the key's access is checked."""
    if not any(g.draws is not None and g.draws.edit > 0 for tier in route.tiers for g in tier.locals):
        raise _bad(f"{route.served_name!r} makes images from text and edits none: send the prompt alone to "
                   "/v1/images/generations, or edit with a model that edits images (FLUX.2 [klein], Qwen-Image-Edit).",
                   "model", "edits_not_supported")


def count(inputs: Inputs) -> None:
    """At least one input, at most MAX_INPUTS (after `offered`, before anything is fetched)."""
    if not inputs.items:
        raise _missing()
    if len(inputs.items) > MAX_INPUTS:
        raise _bad(f"An edit takes at most {MAX_INPUTS} input images.", "image", "too_many_input_images")


async def fetch_all(inputs: Inputs) -> None:
    """Fetch the http(s) inputs of a JSON request through the media rules (forgeyard/gateway/media.py)."""
    todo = [i for i in inputs.items if i.url is not None]
    if not todo:
        return
    results = await asyncio.gather(*(gmedia.fetch_one(i.url, "image") for i in todo), return_exceptions=True)
    for item, r in zip(todo, results):
        if isinstance(r, BaseException):
            if isinstance(r, GatewayError):
                r.param = "images"
            raise r
        item.data, item.url = r[1], None


def check(inputs: Inputs) -> None:
    """Each input's bytes, type (by its first bytes) and size (from its header, never decoded)."""
    cap = get_settings().media_fetch_max_mb * 1024**2
    for n, item in enumerate(inputs.items, 1):
        if len(item.data) > cap:
            raise GatewayError(413, "media_too_large", f"Input image {n} is larger than {cap // 1024**2} MiB "
                                                       "(MEDIA_FETCH_MAX_MB).", param="image")
        dims = imagegen.image_dims(item.data)
        if dims is None:
            if _known(item.data):
                raise _bad(f"Input image {n} could not be read: its header does not say its size.", "image",
                           "invalid_image")
            what = "a HEIC (iPhone) photo" if item.data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc") \
                else "not a PNG, JPEG or WebP image"
            raise _bad(f"Input image {n} is {what}. Send PNG, JPEG or WebP (convert HEIC photos first).", "image",
                       "unsupported_image_format")
        item.width, item.height, item.fmt = dims
        if item.width < 1 or item.height < 1:
            raise _bad(f"Input image {n} says it has no pixels.", "image", "invalid_image")


def _known(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


# ------------------------------------------------------------------ which deployments take it
def takes(d: Optional[imagegen.Draws], ask: imagegen.Ask, inputs: Inputs) -> Optional[str]:
    """Why this deployment cannot take the edit (an error code), or None. One that edits nothing (`edit` 0, or its
    recipe is gone) takes fewer inputs than any edit has."""
    if d is None or len(inputs.items) > d.edit:
        return "too_many_input_images"
    if any(max(i.width, i.height) > d.max_input_side for i in inputs.items):
        return "input_image_too_large"
    return imagegen.caps(d, ask, d.edit_formats)


ORDER = ("prompt_too_long", "too_many_images", "too_many_input_images", "input_image_too_large", "image_too_large",
         "invalid_size", "unsupported_output_format", "too_many_steps")


def refusal(name: str, code: str, ask: imagegen.Ask, inputs: Inputs, all_d: list[imagegen.Draws]) -> GatewayError:
    """The error for an edit no deployment of the route can take, saying what would work. Some deployment edits
    (`offered` came first), so the most telling code is one of an editing deployment's caps."""
    editing = [d for d in all_d if d.edit > 0]
    if code == "too_many_input_images":
        most = max(d.edit for d in editing)
        return _bad(f"{name!r} takes at most {most} input image{'s' if most > 1 else ''} per edit; this one has "
                    f"{len(inputs.items)}.", "image", code)
    if code == "input_image_too_large":
        most = max(d.max_input_side for d in editing)
        big = max(inputs.items, key=lambda i: max(i.width, i.height))
        return _bad(f"{name!r} takes input images up to {most} pixels on a side; one is {big.width}x{big.height}. "
                    "Scale it down first.", "image", code)
    return imagegen.refusal(name, code, ask, editing, [d.edit_formats for d in editing])


# ------------------------------------------------------------------ what the engine is sent
def size_for(ask: imagegen.Ask, d: Optional[imagegen.Draws], first: Input) -> tuple[int, int]:
    """The size to make: the one asked, else the first input's shape at the deployment's default number of pixels,
    within its largest side, each side a multiple of what its model takes."""
    if ask.size is not None:
        return ask.size
    if d is None:
        return imagegen.size_of(ask, d)
    m = max(1, d.multiple)
    scale = math.sqrt(d.width * d.height / (first.width * first.height))
    w, h = first.width * scale, first.height * scale
    if max(w, h) > d.max_side:
        k = d.max_side / max(w, h)
        w, h = w * k, h * k
    floor = max(m, -(-imagegen.MIN_SIDE // m) * m)
    return max(floor, round(w) // m * m), max(floor, round(h) // m * m)


def body_for(ask: imagegen.Ask, inputs: Inputs, d: Optional[imagegen.Draws], upstream: str) -> tuple[bytes, str]:
    """(the multipart body, its content type) an engine with these settings gets: the fields image generation would
    send at the size chosen for the edit, and the checked input images as `image[]` files."""
    fields = imagegen.body_for(replace(ask, size=size_for(ask, d, inputs.items[0])), d, upstream)
    boundary = "forgeyard-" + uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode() +
                   str(value).encode("utf-8") + b"\r\n")
    for n, item in enumerate(inputs.items, 1):
        ext = "jpg" if item.fmt == "jpeg" else item.fmt
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image[]\"; filename=\"image-{n}.{ext}\"\r\n"
                   f"Content-Type: {INPUT_TYPES[item.fmt]}\r\n\r\n".encode() + item.data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"
