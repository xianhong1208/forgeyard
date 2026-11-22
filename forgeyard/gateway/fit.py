"""Which targets of a route can take a request (forgeyard/gateway/proxy.py, steps 10 to 12, 15): its media,
its size, a speech or image request's caps, a text classification body; and its size in tokens, for
spilling on a long prompt."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Optional

from forgeyard.config import get_settings
from forgeyard.gateway import classify, imagegen, media as gmedia, speech
from forgeyard.gateway.body import Parsed
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.route import LocalGroup, Route, Tier

BYTES_PER_TOKEN = 3     # a prompt's size in tokens, estimated on the high side (CJK is ~3 bytes a token)


def fit_classify(parsed: "Parsed") -> None:
    """Text classification (1.4.4): check the body; the engine gets only the fields Forgeyard knows."""
    if parsed.obj is not None:
        parsed.obj = classify.check(parsed.obj)


# ------------------------------------------------------------------ media and body size (1.3)
MEDIA_ENDPOINTS = ("chat/completions", "embeddings")   # bodies that can carry `messages` with media parts
KIND_WORDS = {"image": "images", "audio": "audio clips", "video": "videos"}


def scan_media(parsed: "Parsed", endpoint: str) -> Optional[gmedia.Media]:
    """Rebuild the request's media parts (400 for anything not accepted); None for text only."""
    if endpoint not in MEDIA_ENDPOINTS or parsed.obj is None:
        return None
    parsed.media = gmedia.scan(parsed.obj)
    return parsed.media


def body_size(parsed: "Parsed") -> int:
    """The body's size once media URLs were inlined."""
    if parsed.obj is None:
        return parsed.size
    return len(json.dumps(parsed.obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _takes(g: LocalGroup, m: Optional[gmedia.Media], size: int) -> bool:
    a = g.accepts
    if a is None:  # its recipe is gone: the global limit, no media checks
        return size <= get_settings().gateway_max_body_mb * 1024**2
    if size > a.max_body:
        return False
    if m is None:
        return True
    if m.files and not a.files:
        return False
    return all(n <= a.limit(kind) for kind, n in m.counts.items() if n)


def fit_route(route: Route, parsed: "Parsed", size: int) -> Route:
    """The route with only the targets that can take this request: deployments whose recipe and
    model take its media (and as many) and whose `max_body_mb` its size fits; providers only for
    requests without media, within GATEWAY_MAX_BODY_MB. Raises the most telling error when none can."""
    if not route.tiers:
        return route
    m = parsed.media
    limit = get_settings().gateway_max_body_mb * 1024**2
    tiers = []
    for tier in route.tiers:
        locals_ = [g for g in tier.locals if _takes(g, m, size)]
        clouds = [] if m is not None else [c for c in tier.clouds if size <= limit]
        if locals_ or clouds:
            tiers.append(Tier(tier.index, locals_, clouds))
    if tiers:
        return replace(route, tiers=tiers)
    name = route.served_name
    groups = [g for tier in route.tiers for g in tier.locals]
    if m is not None and not groups:
        raise GatewayError(400, "media_local_only", f"{name!r} is served by providers only, and requests with images, "
                                                    "audio, video or files are served by local deployments only.",
                           param="messages")
    if m is not None:
        known = [g.accepts for g in groups if g.accepts is not None]
        if m.files and not any(a.files for a in known):
            raise GatewayError(400, "file_input_not_supported", f"{name!r} does not take files (file parts). Send the "
                                                                "pages as images, or the text itself.", param="messages")
        for kind in gmedia.KINDS:
            n = m.counts[kind]
            if not n:
                continue
            most = max((a.limit(kind) for a in known), default=0)
            if most == 0:
                takes = sorted({k for a in known for k in a.kinds}, key=gmedia.KINDS.index)
                what = ", ".join(KIND_WORDS[k] for k in takes) if takes else "text only"
                raise GatewayError(400, "media_not_supported", f"{name!r} does not take {KIND_WORDS[kind]} (it takes "
                                                               f"{what}).", param="messages")
            if n > most:
                raise GatewayError(400, "too_many_media_items", f"{name!r} takes at most {most} {KIND_WORDS[kind]} per "
                                                                f"request; this one has {n}.", param="messages")
    most = max([g.accepts.max_body if g.accepts else limit for g in groups] + ([limit] if m is None else []))
    raise GatewayError(413, "request_too_large", f"The request is {size / 1024**2:.1f} MiB; {name!r} takes at most "
                                                 f"{most // 1024**2} MiB.")


def fit_speech(route: Route, parsed: "Parsed") -> Route:
    """Text to speech (1.3): check the body (voice cloning refused) and keep the deployments that can
    take it (input length, format, streaming, script). Raises the most telling error when none can."""
    if parsed.obj is None:
        return route
    ask = speech.check(parsed.obj)
    parsed.tts = ask
    parsed.max_tokens = ask.characters   # what the key's tpm holds for it: a character counts as a token
    text = parsed.obj.get("input") or ""
    groups = [g for tier in route.tiers for g in tier.locals]
    why = {g.deployment_id: speech.takes(g.speaks, ask, text) for g in groups}
    tiers = []
    for tier in route.tiers:
        locals_ = [g for g in tier.locals if why[g.deployment_id] is None]
        if locals_:
            tiers.append(Tier(tier.index, locals_, []))
    if tiers or not groups:
        return replace(route, tiers=tiers) if groups else route
    order = ("input_too_long", "unsupported_response_format", "stream_format_not_supported", "language_not_supported")
    code = min((c for c in why.values() if c), key=order.index)
    raise speech.refusal(route.served_name, code, ask, [g.speaks for g in groups if g.speaks is not None])


def fit_images(route: Route, parsed: "Parsed") -> Route:
    """Image generation (1.3): check the body and keep the deployments whose caps it fits (images per
    request, size, steps, prompt length, format). Raises the most telling error when none can."""
    if parsed.obj is None:
        return route
    ask = imagegen.check(parsed.obj)
    parsed.image = ask
    parsed.max_tokens = None             # no tokens: the key's rpm and its other limits apply
    groups = [g for tier in route.tiers for g in tier.locals]
    why = {g.deployment_id: imagegen.takes(g.draws, ask) for g in groups}
    tiers = []
    for tier in route.tiers:
        locals_ = [g for g in tier.locals if why[g.deployment_id] is None]
        if locals_:
            tiers.append(Tier(tier.index, locals_, []))
    if tiers or not groups:
        return replace(route, tiers=tiers) if groups else route
    code = min((c for c in why.values() if c), key=imagegen.ORDER.index)
    raise imagegen.refusal(route.served_name, code, ask, [g.draws for g in groups if g.draws is not None])


def prompt_estimate(parsed: "Parsed") -> int:
    """Tokens the request needs, estimated on the high side: the prompt's size plus max_tokens."""
    obj = parsed.obj or {}
    text = obj.get("messages") if "messages" in obj else obj.get("prompt", obj.get("input", ""))
    size = len(json.dumps(text, ensure_ascii=False).encode("utf-8")) if text is not None else 0
    m = parsed.media
    if m is not None:  # an image is so many tokens, not the size of its base64 text
        return max(0, size - m.data_bytes) // BYTES_PER_TOKEN + m.items * gmedia.TOKENS_PER_ITEM + (parsed.max_tokens or 0)
    return size // BYTES_PER_TOKEN + (parsed.max_tokens or 0)
