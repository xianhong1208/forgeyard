"""Images, audio and video in chat requests (1.3): the content parts the gateway accepts, how it
rewrites them, and how it fetches media URLs itself.

Engines must never fetch a URL from a request: vLLM 0.32 and llama-server b11977 both download any
http(s) URL they find in a media part, with no private-address filter (vLLM also follows redirects),
from inside the machine's network. So the gateway

- walks `messages[].content[]` and **rebuilds every media part** in one canonical shape, keeping only
  the fields it checked: `image_url` (also from `input_image`), `video_url` (also from
  `input_video`), and `input_audio` with base64 `data` (also from `audio_url`). Text parts pass
  as they are; any other part type is refused (400 `invalid_content_part`), and so are `*_embeds`
  parts;
- passes `data:` URIs on, after checking their media type matches the part (an image part carries
  `data:image/…;base64,`);
- **fetches `http(s)` URLs itself** (`fetch_all`) through the egress rules (forgeyard/gateway/egress.py:
  EGRESS_PROXY, EGRESS_CA_FILE, EGRESS_ALLOWED_HOSTS): public addresses only unless
  MEDIA_ALLOW_PRIVATE_HOSTS (link-local and metadata addresses never), every redirect checked again
  (at most MAX_REDIRECTS), MEDIA_FETCH_MAX_MB per item, FETCH_TIMEOUT seconds per item, and the answer's
  content type must be the part's kind. The result replaces the URL as a data: URI;
- refuses every other scheme (`file://`, `ftp://`, raw base64 in a URL field): 400
  `media_url_refused`;
- counts the items per kind, so the route can be narrowed to deployments that take them
  (forgeyard/gateway/proxy.py: `fit_media`) and usage can count them;
- passes `file` parts (PDF) on only to deployments whose recipe says it takes them, and only as a
  `data:` URI; the gateway never converts a file.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from forgeyard.config import get_settings
from forgeyard.gateway import egress
from forgeyard.gateway.errors import GatewayError
from forgeyard.models.fetch import host_matches

KINDS = ("image", "audio", "video")
TEXT_PARTS = ("text", "input_text", "refusal")
MAX_REDIRECTS = 3
FETCH_TIMEOUT = 20.0          # seconds for one item, redirects included
CONNECT_TIMEOUT = 5.0
# What an image, an audio clip or a video adds to a prompt's token estimate (spill rules), instead of
# the size of its base64 text: about a 1280 x 1280 image on Qwen-VL.
TOKENS_PER_ITEM = 1600
_DATA_RE = re.compile(r"^data:([a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*)((?:;[a-z0-9-]+=[^;,]*)*);base64,", re.I)
# Audio formats llama-server and vLLM read from input_audio.format, by media type.
AUDIO_FORMATS = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav",
                 "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/flac": "flac", "audio/x-flac": "flac",
                 "audio/ogg": "ogg", "audio/webm": "webm", "audio/mp4": "m4a", "audio/aac": "aac"}
# Image magic numbers, for servers that answer application/octet-stream.
_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"),
                (b"RIFF", "image/webp"))


@dataclass
class Fetch:
    """A media URL to fetch and the canonical part to put its data into."""

    part: dict
    kind: str
    url: str


@dataclass
class Media:
    """What a request carries besides text."""

    counts: dict = field(default_factory=lambda: {k: 0 for k in KINDS})
    files: int = 0
    fetch: list[Fetch] = field(default_factory=list)
    data_bytes: int = 0       # size of the data: URIs (inlined or sent), for the token estimate

    @property
    def items(self) -> int:
        return sum(self.counts.values()) + self.files

    @property
    def kinds(self) -> set[str]:
        return {k for k, n in self.counts.items() if n}


def _bad(message: str, code: str = "invalid_content_part") -> GatewayError:
    return GatewayError(400, code, message, param="messages")


def _url_of(value, key: str) -> str:
    """`{"url": …}` or a bare string (some clients send `"image_url": "https://…"`)."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict) and isinstance(value.get(key), str):
        return value[key].strip()
    raise _bad("A media part needs its URL (a data: URI or an http(s) URL).")


def check_data_uri(uri: str, kind: str) -> str:
    """The media type of a base64 data: URI of this kind, or 400."""
    m = _DATA_RE.match(uri[:300])
    if not m:
        raise _bad(f"The {kind} is not a base64 data: URI (data:{kind}/…;base64,…).", "invalid_media_data")
    mime = m.group(1).lower()
    if not (mime.startswith(f"{kind}/") or (kind == "image" and mime == "application/octet-stream")):
        raise _bad(f"An {kind} part carries {mime} data.", "invalid_media_data")
    return mime


def _classify(url: str, kind: str) -> str:
    """'data' or 'fetch', or 400 for any other scheme."""
    head = url[:16].lower()
    if head.startswith("data:"):
        check_data_uri(url, kind)
        return "data"
    if head.startswith(("https://", "http://")):
        return "fetch"
    scheme = url.split(":", 1)[0][:20] if ":" in url[:24] else ""
    what = f"{scheme}:// URLs" if scheme else "this kind of value"
    raise _bad(f"Media URLs must be https://, http:// or data: URIs; {what} are refused.", "media_url_refused")


def scan(obj: Optional[dict]) -> Optional[Media]:
    """Rebuild the media parts of a chat request in place and say what it carries. None: no part
    other than text (a plain text request is left exactly as it came)."""
    messages = (obj or {}).get("messages")
    if not isinstance(messages, list):
        return None
    media = Media()
    found = False
    for msg in messages:
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        parts = []
        for part in msg["content"]:
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                raise _bad("Each content part is an object with a 'type'.")
            ptype = part["type"]
            if ptype in TEXT_PARTS:
                parts.append(part)
                continue
            found = True
            parts.append(_media_part(part, ptype, media))
        msg["content"] = parts
    return media if found else None


def _media_part(part: dict, ptype: str, media: Media) -> dict:
    if ptype in ("image_url", "input_image"):
        source = part.get("image_url")
        url = _url_of(source, "url")
        out: dict = {"url": url}
        detail = source.get("detail") if isinstance(source, dict) else part.get("detail")
        if detail in ("auto", "low", "high"):
            out["detail"] = detail
        new = {"type": "image_url", "image_url": out}
        _place(new, new["image_url"], "url", url, "image", media)
        return new
    if ptype in ("video_url", "input_video"):
        source = part.get(ptype)
        if isinstance(source, dict) and "url" not in source and isinstance(source.get("data"), str):
            url = source["data"].strip()          # llama-server's input_video {"data": …}
        else:
            url = _url_of(source, "url")
        new = {"type": "video_url", "video_url": {"url": url}}
        _place(new, new["video_url"], "url", url, "video", media)
        return new
    if ptype == "audio_url":
        url = _url_of(part.get("audio_url"), "url")
        new = {"type": "input_audio", "input_audio": {"data": url, "format": "wav"}}
        _place(new, new["input_audio"], "data", url, "audio", media)
        return new
    if ptype == "input_audio":
        source = part.get("input_audio")
        if not isinstance(source, dict) or not isinstance(source.get("data"), str):
            raise _bad("input_audio needs base64 'data'.")
        data = source["data"].strip()
        fmt = source.get("format") if isinstance(source.get("format"), str) else "wav"
        if data[:5].lower() == "data:":
            mime = check_data_uri(data, "audio")
            fmt = AUDIO_FORMATS.get(mime, fmt)
            data = data.split(",", 1)[1]
        elif "://" in data[:16] or data[:5].lower() in ("http:", "https", "file:"):
            raise _bad("input_audio takes base64 data, not a URL (send an audio_url part for a URL).",
                       "media_url_refused")
        media.counts["audio"] += 1
        media.data_bytes += len(data)
        return {"type": "input_audio", "input_audio": {"data": data, "format": fmt[:16]}}
    if ptype == "file":
        source = part.get("file")
        data = source.get("file_data") if isinstance(source, dict) else None
        if not isinstance(data, str) or not data[:5].lower() == "data:" or not _DATA_RE.match(data[:300]):
            raise _bad("A file part needs its content as a base64 data: URI in file.file_data "
                       "(file ids are not supported).", "invalid_media_data")
        media.files += 1
        media.data_bytes += len(data)
        out = {"file_data": data}
        if isinstance(source.get("filename"), str):
            out["filename"] = source["filename"][:255]
        return {"type": "file", "file": out}
    if ptype.endswith("_embeds"):
        raise _bad(f"{ptype} parts (precomputed embeddings) are not accepted.")
    raise _bad(f"Content parts of type {ptype[:40]!r} are not supported. Use text, image_url, "
               "input_audio, audio_url or video_url.")


def _place(part: dict, holder: dict, key: str, url: str, kind: str, media: Media) -> None:
    media.counts[kind] += 1
    if _classify(url, kind) == "fetch":
        media.fetch.append(Fetch(part=holder, kind=kind, url=url))
        holder["_key"] = key
    else:
        media.data_bytes += len(url)
        if kind == "audio" and key == "data":     # audio_url with a data: URI
            mime = check_data_uri(url, "audio")
            holder["format"] = AUDIO_FORMATS.get(mime, "wav")
            holder["data"] = url.split(",", 1)[1]


# ------------------------------------------------------------------ fetching
async def fetch_all(media: Media) -> None:
    """Fetch every media URL of the request and inline it. Raises GatewayError (400 or 413)."""
    if not media.fetch:
        return
    results = await asyncio.gather(*(fetch_one(f.url, f.kind) for f in media.fetch), return_exceptions=True)
    for f, r in zip(media.fetch, results):
        if isinstance(r, BaseException):
            raise r
        mime, data = r
        key = f.part.pop("_key", "url")
        encoded = base64.b64encode(data).decode("ascii")
        if f.kind == "audio":
            f.part["data"] = encoded
            f.part["format"] = AUDIO_FORMATS.get(mime, "wav")
        else:
            f.part[key] = f"data:{mime};base64,{encoded}"
        media.data_bytes += len(encoded)
    media.fetch = []


def _refused(url: str, why: str) -> GatewayError:
    return GatewayError(400, "media_url_refused", f"The media URL {_shown(url)} is refused: {why}.", param="messages")


def _failed(url: str, why: str) -> GatewayError:
    return GatewayError(400, "media_fetch_failed", f"The media URL {_shown(url)} could not be fetched: {why}.",
                        param="messages")


def _shown(url: str) -> str:
    """The URL without its query (which may hold a signature), shortened."""
    p = urlsplit(url)
    host = p.hostname or ""
    try:
        port = f":{p.port}" if p.port else ""
    except ValueError:
        port = ""
    return f"{p.scheme}://{host}{port}{p.path[:80]}" if host else url[:40]


async def _target(url: str) -> tuple[str, dict, dict]:
    """(the URL to request, extra headers, extensions) after checking the URL and its addresses."""
    s = get_settings()
    p = urlsplit(url)
    scheme = p.scheme.lower()
    if scheme not in ("http", "https"):
        raise _refused(url, "only http and https are followed")
    if p.username or p.password:
        raise _refused(url, "credentials in the URL are not allowed")
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        raise _refused(url, "it has no host")
    allowed = s.egress_allowed_host_list
    if allowed and not host_matches(host, allowed):
        raise _refused(url, f"{host} is not in EGRESS_ALLOWED_HOSTS")
    try:
        port = p.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise _refused(url, "its port is not valid") from None
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    is_ip = _is_ip(host)
    if s.egress_proxy.strip() and not is_ip:
        return url.split("#", 1)[0], {}, {}     # the proxy resolves names (EGRESS_ALLOWED_HOSTS limits them)
    try:
        addresses = await egress.resolve(host, port)
    except egress.EgressError as e:
        raise _failed(url, str(e)) from None
    for addr in addresses:
        problem = egress.address_problem(addr, private_ok=s.media_allow_private_hosts,
                                         private_why="MEDIA_ALLOW_PRIVATE_HOSTS is off")
        if problem:
            raise _refused(url, f"{host} is at a refused address ({problem})" if addr != host else problem)
    if s.egress_proxy.strip():
        return url.split("#", 1)[0], {}, {}
    ip = ipaddress.ip_address(addresses[0].split("%", 1)[0])
    netloc = f"[{ip}]" if ip.version == 6 else str(ip)
    shown_host = f"[{host}]" if ":" in host else host
    host_header = shown_host if port == (443 if scheme == "https" else 80) else f"{shown_host}:{port}"
    return f"{scheme}://{netloc}:{port}{path}", {"Host": host_header}, ({"sni_hostname": host} if scheme == "https" else {})


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


async def fetch_one(url: str, kind: str) -> tuple[str, bytes]:
    """(media type, bytes) of a media URL, following at most MAX_REDIRECTS redirects, each checked."""
    try:
        return await asyncio.wait_for(_fetch(url, kind), FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        raise _failed(url, f"no complete answer within {FETCH_TIMEOUT:g} s") from None


async def _fetch(url: str, kind: str) -> tuple[str, bytes]:
    cap = get_settings().media_fetch_max_mb * 1024**2
    http = egress.client()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        target, headers, extensions = await _target(current)
        headers.update({"Accept": f"{kind}/*", "Accept-Encoding": "identity", "User-Agent": "forgeyard-gateway"})
        try:
            req = http.build_request("GET", target, headers=headers, extensions=extensions,
                                     timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=FETCH_TIMEOUT, write=10.0,
                                                           pool=10.0))
            response = await http.send(req, stream=True)
        except httpx.HTTPError as e:
            raise _failed(current, f"the connection failed ({type(e).__name__})") from None
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise _failed(current, f"it redirects ({response.status_code}) without a Location")
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                raise _failed(current, f"the server answered {response.status_code}")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > cap:
                raise _too_large(current, cap)
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > cap:
                    raise _too_large(current, cap)
            mime = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            return _media_type(current, kind, mime, bytes(data)), bytes(data)
        except httpx.HTTPError as e:
            raise _failed(current, f"the answer broke off ({type(e).__name__})") from None
        finally:
            await response.aclose()
    raise _failed(url, f"more than {MAX_REDIRECTS} redirects")


def _too_large(url: str, cap: int) -> GatewayError:
    return GatewayError(413, "media_too_large", f"The media at {_shown(url)} is larger than {cap // 1024**2} MiB "
                                                "(MEDIA_FETCH_MAX_MB).", param="messages")


def _media_type(url: str, kind: str, mime: str, data: bytes) -> str:
    if mime.startswith(f"{kind}/"):
        return mime
    if kind == "image" and mime in ("", "application/octet-stream", "binary/octet-stream"):
        for magic, sniffed in _IMAGE_MAGIC:
            if data.startswith(magic) and (sniffed != "image/webp" or data[8:12] == b"WEBP"):
                return sniffed
    raise _failed(url, f"its content type is {mime or 'missing'}, not {kind}/…")

