"""Just enough multipart/form-data for the gateway: read the plain fields of a transcription
request (`model`, `stream`) and send it on with `model` replaced, byte for byte otherwise (the
audio file is never decoded or copied into a temporary file)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]{1,200})"|([^;\s]{1,200}))', re.I)
_NAME_RE = re.compile(rb'\bname="([^"]*)"', re.I)
_FILENAME_RE = re.compile(rb'\bfilename="', re.I)


class MultipartError(ValueError):
    pass


@dataclass
class Part:
    head: bytes          # the part's header lines, as received
    name: str
    is_file: bool
    content: bytes


def boundary_of(content_type: str) -> bytes:
    m = _BOUNDARY_RE.search(content_type or "")
    if not m:
        raise MultipartError("multipart/form-data without a boundary")
    return (m.group(1) or m.group(2)).encode("latin-1")


def parse(body: bytes, content_type: str) -> tuple[bytes, list[Part]]:
    boundary = boundary_of(content_type)
    delim = b"\r\n--" + boundary
    data = b"\r\n" + body
    chunks = data.split(delim)
    if len(chunks) < 3:
        raise MultipartError("the multipart body has no parts")
    parts: list[Part] = []
    closed = False
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            closed = True
            break
        if not chunk.startswith(b"\r\n"):
            raise MultipartError("a malformed multipart delimiter")
        head, sep, content = chunk[2:].partition(b"\r\n\r\n")
        if not sep:
            raise MultipartError("a multipart part without headers")
        disposition = next((line for line in head.split(b"\r\n") if line.lower().startswith(b"content-disposition:")), b"")
        m = _NAME_RE.search(disposition)
        if not m:
            raise MultipartError("a multipart part without a name")
        parts.append(Part(head=head, name=m.group(1).decode("utf-8", "replace"),
                          is_file=bool(_FILENAME_RE.search(disposition)), content=content))
    if not closed:
        raise MultipartError("the multipart body is not closed")
    return boundary, parts


def field(parts: list[Part], name: str) -> Optional[str]:
    for p in parts:
        if p.name == name and not p.is_file:
            return p.content.decode("utf-8", "replace").strip()
    return None


def build(boundary: bytes, parts: list[Part], replace: Optional[dict[str, str]] = None) -> bytes:
    replace = replace or {}
    out = []
    for p in parts:
        content = replace[p.name].encode("utf-8") if p.name in replace and not p.is_file else p.content
        out.append(b"--" + boundary + b"\r\n" + p.head + b"\r\n\r\n" + content + b"\r\n")
    out.append(b"--" + boundary + b"--\r\n")
    return b"".join(out)
