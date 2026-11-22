"""Answers that stream (forgeyard/gateway/proxy.py, steps 6 and 7): server-sent events passed through as they
complete (the usage chunk the gateway asked for removed again), a speech engine's audio measured on the way,
and the upstream closed the moment the client goes away."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Callable, Optional

import httpx
from starlette.responses import Response

from forgeyard.gateway import speech
from forgeyard.gateway.upstream import CLIENT_CLOSED, _until_disconnect

log = logging.getLogger("forgeyard.gateway")


# ------------------------------------------------------------------ usage in answers
def tokens_of(usage) -> tuple[int, int]:
    if not isinstance(usage, dict):
        return 0, 0

    def num(v) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0

    prompt, completion = num(usage.get("prompt_tokens")), num(usage.get("completion_tokens"))
    if not prompt and not completion:
        prompt = num(usage.get("total_tokens")) or num(usage.get("input_tokens"))
    return prompt, completion


class SSEFilter:
    """Passes server-sent events through as they complete, remembers the last `usage`, and drops
    the usage-only chunk (or the `usage` field) when the gateway asked for it on its own."""

    def __init__(self, strip_usage: bool):
        self.strip = strip_usage
        self.buf = b""
        self.usage: Optional[dict] = None
        self.events = 0

    def feed(self, chunk: bytes) -> bytes:
        self.buf += chunk
        out = []
        while True:
            cut = [(i, len(sep)) for sep in (b"\n\n", b"\r\n\r\n") if (i := self.buf.find(sep)) >= 0]
            if not cut:
                break
            i, n = min(cut)
            event, self.buf = self.buf[:i + n], self.buf[i + n:]
            out.append(self._event(event))
        return b"".join(out)

    def end(self) -> bytes:
        rest, self.buf = self.buf, b""
        return self._event(rest) if rest.strip() else rest

    def _event(self, event: bytes) -> bytes:
        self.events += 1
        if b'"usage"' not in event:
            return event
        data = b"".join(line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:"))
        try:
            obj = json.loads(data)
        except ValueError:
            return event
        if not isinstance(obj, dict) or not obj.get("usage"):
            return event
        self.usage = obj["usage"]
        if not self.strip:
            return event
        if not obj.get("choices"):
            return b""  # the usage-only chunk the client did not ask for
        obj.pop("usage")
        return b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"


class StreamResponse(Response):
    """Server-sent events from the replica (or provider) to the client, as they come. Stops the upstream request
    as soon as the client disconnects."""

    def __init__(self, upstream: httpx.Response, sse: SSEFilter, headers: dict, done: Callable[[int, Optional[dict]], None],
                 who: str = "replica", first: bytes = b"", chunks=None):
        # Not Response.__init__: that would declare Content-Length: 0 for the empty initial body.
        self.status_code = upstream.status_code
        self.background = None
        ctype = upstream.headers.get("content-type", "text/event-stream")
        self.raw_headers = [(b"content-type", ctype.encode("latin-1"))] + [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
        self.upstream = upstream
        self.sse = sse
        self.done = done
        self.who = who
        self.first = first                     # bytes already read (the first answer, 1.4.5)
        self.chunks = chunks                   # the rest, when some were read

    async def __call__(self, scope, receive, send):
        status = self.upstream.status_code
        await send({"type": "http.response.start", "status": status, "headers": self.raw_headers})

        async def pump():
            nonlocal status
            try:
                if self.first:
                    out = self.sse.feed(self.first)
                    if out:
                        await send({"type": "http.response.body", "body": out, "more_body": True})
                async for chunk in (self.chunks if self.chunks is not None else self.upstream.aiter_bytes()):
                    out = self.sse.feed(chunk)
                    if out:
                        await send({"type": "http.response.body", "body": out, "more_body": True})
                tail = self.sse.end()
            except httpx.HTTPError as e:
                status = 502
                log.warning("A streamed answer broke off: %s", type(e).__name__)
                source = "The replica's stream" if self.who == "replica" else f"The stream from {self.who}"
                err = {"error": {"message": f"{source} broke off ({type(e).__name__}).",
                                 "type": "upstream_error", "param": None, "code": "upstream_stream_broken"}}
                tail = b"data: " + json.dumps(err).encode() + b"\n\n"
            await send({"type": "http.response.body", "body": tail, "more_body": False})

        pumping = asyncio.ensure_future(pump())
        watching = asyncio.ensure_future(_until_disconnect(receive))
        try:
            done, _ = await asyncio.wait({pumping, watching}, return_when=asyncio.FIRST_COMPLETED)
            if pumping not in done:
                status = CLIENT_CLOSED
            else:
                pumping.result()
        except OSError:
            status = CLIENT_CLOSED  # the client's socket is gone (send failed)
        finally:
            for t in (pumping, watching):
                t.cancel()
            for t in (pumping, watching):
                with contextlib.suppress(BaseException):
                    await t
            await self.upstream.aclose()  # the engine sees the connection close and stops
            self.done(status, self.sse.usage)


class SpeechSSE(SSEFilter):
    """vLLM-Omni's `stream_format: sse` (OpenAI's speech.audio.* events): passed through as they come;
    the audio deltas are measured (pcm: their bytes over the sample rate) and the final event's
    token usage kept."""

    def __init__(self, rate: Optional[int] = None):
        super().__init__(False)
        self.bytes = 0
        self.rate = rate
        self.channels = 1
        self.fmt: Optional[str] = None

    def _event(self, event: bytes) -> bytes:
        if b"speech.audio.delta" in event:
            data = b"".join(line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:"))
            try:
                obj = json.loads(data)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                n, rate, channels, fmt = speech.sse_audio(obj)
                self.bytes += n
                self.rate = rate or self.rate
                self.channels = channels or self.channels
                self.fmt = fmt or self.fmt
            self.events += 1
            return event
        return super()._event(event)

    def ms(self) -> Optional[int]:
        if not self.bytes or (self.fmt not in (None, "pcm")):
            return None
        return int(self.bytes * 1000 / ((self.rate or 24000) * self.channels * 2))


AUDIO_HEADERS = ("x-audio-sample-rate", "x-audio-channels", "x-vllm-omni-input-tokens", "x-vllm-omni-output-tokens",
                 "x-vllm-omni-total-tokens")


class AudioResponse(Response):
    """A speech engine's audio, passed to the client chunk by chunk as it is made (1.3), measured on
    the way. Stops the upstream request as soon as the client disconnects."""

    def __init__(self, upstream: httpx.Response, headers: dict, meter: speech.Meter,
                 done: Callable[[int], None], who: str = "replica", first: bytes = b"", chunks=None):
        self.status_code = upstream.status_code
        self.background = None
        ctype = upstream.headers.get("content-type") or "application/octet-stream"
        extra = {k: v for k in AUDIO_HEADERS if (v := upstream.headers.get(k))}
        self.raw_headers = [(b"content-type", ctype.encode("latin-1"))] + [
            (k.lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in {**extra, **headers}.items()]
        self.upstream = upstream
        self.meter = meter
        self.done = done
        self.who = who
        self.first = first
        self.chunks = chunks

    async def __call__(self, scope, receive, send):
        status = self.upstream.status_code
        await send({"type": "http.response.start", "status": status, "headers": self.raw_headers})

        async def pump():
            nonlocal status
            try:
                if self.first:
                    self.meter.feed(self.first)
                    await send({"type": "http.response.body", "body": self.first, "more_body": True})
                async for chunk in (self.chunks if self.chunks is not None else self.upstream.aiter_bytes()):
                    if chunk:
                        self.meter.feed(chunk)
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
            except httpx.HTTPError as e:
                status = 502  # the headers are sent: the client sees an audio file that ends early
                log.warning("Streamed audio from %s broke off: %s", self.who, type(e).__name__)
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        pumping = asyncio.ensure_future(pump())
        watching = asyncio.ensure_future(_until_disconnect(receive))
        try:
            done, _ = await asyncio.wait({pumping, watching}, return_when=asyncio.FIRST_COMPLETED)
            if pumping not in done:
                status = CLIENT_CLOSED
            else:
                pumping.result()
        except OSError:
            status = CLIENT_CLOSED
        finally:
            for t in (pumping, watching):
                t.cancel()
            for t in (pumping, watching):
                with contextlib.suppress(BaseException):
                    await t
            await self.upstream.aclose()
            self.done(status)
