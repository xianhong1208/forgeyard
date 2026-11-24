"""Embeddings with images at /v1/embeddings (1.5.4, #141): typed items beside OpenAI's plain strings, the Jina shape.

    request   {"model": "photos", "input": "a text" | ["a text", {"text": "a text"}, {"image": "<image>"}, …],
               "encoding_format": "float" | "base64", "dimensions": <the model's own>}
    answer    OpenAI's: {"object": "list", "data": [{"object": "embedding", "index", "embedding"}, …],
               "usage": {"prompt_tokens", "total_tokens", "images", "kilopixels", …}}, in the order of `input`

`<image>` is a data: URI (`data:image/png;base64,…`), bare base64, or an http(s) URL, which **the gateway fetches**
through the media rules of 1.3 (forgeyard/gateway/media.py: the egress guard, public addresses only unless
MEDIA_ALLOW_PRIVATE_HOSTS, MEDIA_FETCH_MAX_MB per image, every redirect checked) and inlines as a data: URI; the
engine never fetches anything. A plain OpenAI body (a string, strings, token ids) is passed on exactly as it came, so
OpenAI SDKs and every text client keep working; `{"text": …}` items are rewritten as plain strings, so a request of
texts only is an OpenAI one wherever it goes (a vLLM deployment, a provider).

Which deployments of the route can take it (`fit`), from what each deployment's model embeds (`Embeds`: the catalog's
meta.embed, forgeyard/models/encoders.py) and its recipe's `max_images`:

- images need a model that embeds images (CLIP, SigLIP, SigLIP 2, DINOv2 on Forgeyard's transformers server):
  400 `images_not_supported` otherwise, 400 `too_many_images` above the deployment's `max_images`;
- texts need a model that embeds texts: an image-only model (DINOv2) answers 400 `text_not_supported`;
- **a request with images never goes to a provider** (no provider takes typed items, and another model's vectors
  would not match an index made with this one): only local deployments, so it never spills to the cloud.

Usage: the engine's `prompt_tokens` as before; for a request with images also `images` and `kilopixels` (from the
engine's answer) and the replica's busy milliseconds.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field, replace
from typing import Optional

from forgeyard.gateway import media as gmedia
from forgeyard.gateway.errors import GatewayError

ENDPOINT = "embeddings"
MAX_INPUTS = 2048            # items per request (OpenAI's limit)
DEFAULT_MAX_IMAGES = 32      # the transformers recipe's default (max_images)
_B64_RE = re.compile(rb"^[A-Za-z0-9+/\r\n]+={0,2}$")


@dataclass
class Ask:
    """What an embeddings request carries: how many texts and images, the image URLs to fetch."""

    texts: int = 0
    images: int = 0
    typed: bool = False                                  # it had typed items ({"text"} or {"image"})
    fetch: list[tuple[int, str]] = field(default_factory=list)   # (index in input, URL)
    data_bytes: int = 0


@dataclass(frozen=True)
class Embeds:
    """What a deployment's model embeds (its catalog entry) and how many images per request it takes."""

    modalities: tuple = ("text",)
    max_images: int = 0

    @property
    def images(self) -> bool:
        return "image" in self.modalities

    @property
    def texts(self) -> bool:
        return "text" in self.modalities


def embeds(recipe, params: dict, meta: Optional[dict]) -> Embeds:
    """A deployment's Embeds: the transformers server embeds what its model takes (texts and images, or images
    only), with the deployment's max_images; any other engine (vLLM) texts only."""
    from forgeyard.models import encoders

    if not encoders.is_server(recipe, "embed"):
        return Embeds()
    mods = tuple(m for m in encoders.embed_of(meta or {}).get("modalities") or ["text"] if m in ("text", "image"))
    raw = (params or {}).get("max_images")
    spec = recipe.params.get("max_images") if getattr(recipe, "params", None) else None
    default = spec.default if spec is not None and isinstance(spec.default, int) else DEFAULT_MAX_IMAGES
    most = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else default
    return Embeds(modalities=mods or ("text",), max_images=most if "image" in mods else 0)


def _bad(message: str, code: str = "invalid_request") -> GatewayError:
    return GatewayError(400, code, message, param="input")


def _image(value, where: str, ask: Ask, index: int) -> str:
    """An image item's value, checked: a data: URI of an image, bare base64, or an http(s) URL to fetch."""
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{where}: an image item is {{\"image\": \"<a data: URI, base64 or an http(s) URL>\"}}.")
    v = value.strip()
    head = v[:16].lower()
    if head.startswith("data:"):
        try:
            gmedia.check_data_uri(v, "image")
        except GatewayError as e:
            raise _bad(f"{where}: {e.message}", e.code) from None
        ask.data_bytes += len(v)
        return v
    if head.startswith(("https://", "http://")):
        ask.fetch.append((index, v))
        return v
    if "://" in v[:24] or head.startswith(("file:", "ftp:")):
        scheme = v.split(":", 1)[0][:20]
        raise _bad(f"{where}: image URLs must be https:// or http:// (or send the image as base64 or a data: URI); "
                   f"{scheme}: is refused.", "media_url_refused")
    if not _B64_RE.match(v[:4096].encode("ascii", "replace")):
        raise _bad(f"{where}: the image is not base64, a data: URI or an http(s) URL.", "invalid_media_data")
    ask.data_bytes += len(v)
    return v


def check(obj: dict) -> Ask:
    """Read `input`, rebuild typed items in place (texts as plain strings, images checked), and say what it holds.
    A plain OpenAI body is left as it came. Raises a 400 GatewayError naming `input`."""
    inp = obj.get("input")
    ask = Ask()
    if inp is None and obj.get("messages") is not None:
        ask.texts = 1     # vLLM's chat form (its media parts are the media rules' own, forgeyard/gateway/media.py)
        return ask
    if isinstance(inp, str) or (isinstance(inp, list) and all(isinstance(x, (str, int)) or
                                                                  (isinstance(x, list) and all(isinstance(t, int) for t in x))
                                                                  for x in inp)):
        # OpenAI's shapes: a text, texts, token ids (one list or several). Passed on unchanged.
        ask.texts = 1 if isinstance(inp, str) or (inp and all(isinstance(x, int) for x in inp)) else len(inp)
        return ask
    items = [inp] if isinstance(inp, dict) else inp
    if not isinstance(items, list) or not items:
        raise _bad("'input' is a text, a list of texts, or a list of items: {\"text\": \"…\"} and "
                   "{\"image\": \"<a data: URI, base64 or an http(s) URL>\"}.")
    if len(items) > MAX_INPUTS:
        raise _bad(f"'input' has {len(items)} items; at most {MAX_INPUTS} per request.")
    if obj.get("messages") is not None:
        raise _bad("Send typed items in 'input' or chat 'messages', not both.")
    out: list = []
    ask.typed = True
    for i, item in enumerate(items):
        where = f"input[{i}]" if isinstance(inp, list) else "input"
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict) and set(item) == {"text"} and isinstance(item["text"], str):
            text = item["text"]
        elif isinstance(item, dict) and set(item) == {"image"}:
            out.append({"image": _image(item["image"], where, ask, i)})
            ask.images += 1
            continue
        else:
            raise _bad(f"{where}: an item is a text, {{\"text\": \"…\"}} or {{\"image\": \"<a data: URI, base64 or an "
                       "http(s) URL>\"} (one key each).")
        if not text.strip():
            raise _bad(f"{where} is an empty text.")
        out.append(text)
        ask.texts += 1
    obj["input"] = out
    return ask


async def fetch_all(obj: dict, ask: Ask) -> None:
    """Fetch every image URL of the request through the media rules and inline it as a data: URI. Raises a 400 or
    413 GatewayError (the media rules' own: media_url_refused, media_fetch_failed, media_too_large)."""
    import asyncio

    if not ask.fetch:
        return
    results = await asyncio.gather(*(gmedia.fetch_one(url, "image") for _, url in ask.fetch), return_exceptions=True)
    for (i, _), r in zip(ask.fetch, results):
        if isinstance(r, BaseException):
            if isinstance(r, GatewayError):
                r.param = "input"
            raise r
        mime, data = r
        uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        obj["input"][i] = {"image": uri}
        ask.data_bytes += len(uri)
    ask.fetch = []


def fit(route, ask: Ask):
    """The route with only the targets that can take this request (see the module's text). Raises the most telling
    400 when none can."""
    from forgeyard.gateway.route import Tier

    name = route.served_name
    groups = [g for tier in route.tiers for g in tier.locals]

    def known(g) -> Embeds:
        return g.embeds if g.embeds is not None else Embeds()

    def takes(g) -> bool:
        e = known(g)
        if ask.images and (not e.images or ask.images > e.max_images):
            return False
        return not ask.texts or e.texts

    tiers = []
    for tier in route.tiers:
        locals_ = [g for g in tier.locals if takes(g)]
        clouds = [] if ask.images else list(tier.clouds)   # images never leave for a provider
        if locals_ or clouds:
            tiers.append(Tier(tier.index, locals_, clouds))
    if tiers:
        return replace(route, tiers=tiers)
    if ask.images:
        takers = [known(g) for g in groups if known(g).images]
        if not takers:
            raise _bad(f"{name!r} embeds texts only; images need a model that embeds them (CLIP, SigLIP, SigLIP 2 or "
                       "DINOv2 on Forgeyard's transformers server). Requests with images are served by local "
                       "deployments only.", "images_not_supported")
        most = max(e.max_images for e in takers)
        if ask.images > most:
            raise _bad(f"{name!r} takes at most {most} images per request; this one has {ask.images}.",
                       "too_many_images")
    raise _bad(f"{name!r} embeds images only (an image-only model such as DINOv2): send {{\"image\": …}} items, or "
               "use a CLIP-like model (CLIP, SigLIP) for texts.", "text_not_supported")
