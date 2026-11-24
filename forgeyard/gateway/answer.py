"""The answer (forgeyard/gateway/proxy.py, steps 6, 11, 12 and 14): passed to the client as it came, streamed when
it streams, an image engine's rewritten in OpenAI's shape, a vision server's checked, and the request's usage
counted once it is over. `Answering` is that part of `proxy.Job`."""

from __future__ import annotations

import json
import time

import httpx
from starlette.responses import Response

from forgeyard.gateway import imagegen, speech
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.route import _int
from forgeyard.gateway.streaming import AudioResponse, SpeechSSE, SSEFilter, StreamResponse
from forgeyard.gateway.upstream import ClientGone, unless_gone, upstream_retry_after


class Answering:
    """`proxy.Job`'s answer: what came back from a replica or provider, passed on, and the job finished with
    its usage (`finish`)."""

    # -------------------------------------------------------------- the answer
    def audio(self, response: httpx.Response, out_headers: dict, release: str, who: str, first: bytes = b"",
              chunks=None) -> Response:
        """A speech engine's 200: streamed through, its length measured, usage counted at the end."""
        fmt = self.speech_format or "wav"
        meter = speech.Meter(fmt, response.headers.get("content-type", ""), dict(response.headers),
                             pcm_rate=next((g.speaks.pcm_rate for t in self.route.tiers for g in t.locals
                                            if g.deployment_id == self.deployment_id and g.speaks), 24000),
                             asked_rate=self.speech_rate)
        tokens = {"input_tokens": _int(int(response.headers.get("x-vllm-omni-input-tokens") or 0)),
                  "output_tokens": _int(int(response.headers.get("x-vllm-omni-output-tokens") or 0))} \
            if (response.headers.get("x-vllm-omni-input-tokens") or "").isdigit() and \
            (response.headers.get("x-vllm-omni-output-tokens") or "0").isdigit() else None

        def done(status: int) -> None:
            balancer.release(release)
            self.media_ms = meter.ms()
            self.finish(status, tokens)

        return AudioResponse(response, {**out_headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                             meter, done, who=who, first=first, chunks=chunks)

    async def images(self, response: httpx.Response, out_headers: dict, release: str, who: str) -> Response:
        """An image engine's answer (1.3): read whole, rewritten in OpenAI's shape, counted."""
        try:
            data = await unless_gone(self.request.receive, response.aread())
        except ClientGone:
            return self.gone()
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", f"The images from {who} did not come in time.") from None
        except httpx.HTTPError as e:
            raise GatewayError(502, "upstream_error", f"The answer from {who} broke off ({type(e).__name__}).") from None
        finally:
            await response.aclose()
            balancer.release(release)
        busy = max(0.0, time.monotonic() - self.sent_at)
        imagegen.note_busy(self.queue_key, busy)
        d = self.image_draws
        status = response.status_code
        if status == 200:
            out, made = imagegen.answer(data, self.parsed.image, d)
            if out is None or not made.images:
                raise GatewayError(502, "upstream_error", f"The answer from {who} holds no images.")
            self.made = made
            self.busy_ms = int(busy * 1000) * max(1, d.gpus if d is not None else 1)
            self.finish(200, None)
            return Response(content=out, status_code=200, headers=out_headers, media_type="application/json")
        self.finish(status, None)
        return Response(content=imagegen.engine_error(status, data), status_code=status,
                        headers={**upstream_retry_after(response), **out_headers}, media_type="application/json")

    async def looked(self, response: httpx.Response, out_headers: dict, release: str, who: str) -> Response:
        """A vision server's answer (1.4): read whole and passed on as it is; a 200 is counted as one image,
        its kilopixels (the width and height the answer says) and the replica's busy time."""
        try:
            data = await unless_gone(self.request.receive, response.aread())
        except ClientGone:
            return self.gone()
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", f"The answer from {who} did not come in time.") from None
        except httpx.HTTPError as e:
            raise GatewayError(502, "upstream_error", f"The answer from {who} broke off ({type(e).__name__}).") from None
        finally:
            await response.aclose()
            balancer.release(release)
        busy = max(0.0, time.monotonic() - self.sent_at)
        status = response.status_code
        if status == 200:
            try:
                obj = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                obj = None
            w, h = (obj.get("width"), obj.get("height")) if isinstance(obj, dict) else (None, None)
            if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (w, h)):
                raise GatewayError(502, "upstream_error", f"The answer from {who} is not a vision answer.")
            self.seen = (w, h)
            self.busy_ms = int(busy * 1000)
        self.finish(status, None)
        return Response(content=data, status_code=status, headers={**upstream_retry_after(response), **out_headers},
                        media_type="application/json")

    def counted(self, usage) -> None:
        """1.5.4: what an answer of the transformers server adds to usage: the images it embedded and their
        kilopixels (from its `usage`), and the replica's busy time."""
        if self.busy_usage:
            self.busy_ms = int(max(0.0, time.monotonic() - self.sent_at) * 1000)
        e = self.parsed.embed
        if e is not None and e.images and isinstance(usage, dict):
            images, kpx = usage.get("images"), usage.get("kilopixels")
            ok = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (images, kpx))
            self.embedded = (images, kpx) if ok else (e.images, 0)

    async def serve(self, response: httpx.Response, out_headers: dict, strip: bool, *, release: str,
                    who: str) -> Response:
        self.first_byte_at = time.monotonic()  # the status line and headers are here (a stream: its first chunk, below)
        if self.parsed.image is not None:
            return await self.images(response, out_headers, release, who)
        if self.parsed.vision is not None:
            return await self.looked(response, out_headers, release, who)
        is_sse = "text/event-stream" in response.headers.get("content-type", "")
        streamed = response.status_code == 200 and (is_sse or self.parsed.tts is not None)
        first, chunks = b"", None
        if streamed:
            try:
                first, chunks = await self.first_chunk(response, release)
            except ClientGone:
                return self.gone()
            self.first_byte_at = time.monotonic()
        if is_sse and response.status_code == 200:
            def done(status: int, usage) -> None:
                balancer.release(release)
                if self.parsed.tts is not None:
                    self.media_ms = sse.ms()
                self.finish(status, usage)

            out_headers = {**out_headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
            sse = SpeechSSE(self.speech_rate) if self.parsed.tts is not None else SSEFilter(strip)
            return StreamResponse(response, sse, out_headers, done, who=who, first=first, chunks=chunks)
        if self.parsed.tts is not None and response.status_code == 200:
            return self.audio(response, out_headers, release, who, first, chunks)
        try:
            data = await unless_gone(self.request.receive, response.aread())
        except ClientGone:
            return self.gone()
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", f"The answer from {who} did not finish in time.") from None
        except httpx.HTTPError as e:
            raise GatewayError(502, "upstream_error", f"The answer from {who} broke off ({type(e).__name__}).") from None
        finally:
            await response.aclose()
            balancer.release(release)
        usage = None
        if response.status_code == 200 and "json" in response.headers.get("content-type", ""):
            try:
                answer = json.loads(data)
                usage = answer.get("usage") if isinstance(answer, dict) else None
            except ValueError:
                pass
        if response.status_code == 200:
            self.counted(usage)
        self.finish(response.status_code, usage)
        return Response(content=data, status_code=response.status_code,
                        headers={**upstream_retry_after(response), **out_headers},
                        media_type=response.headers.get("content-type") or "application/json")
