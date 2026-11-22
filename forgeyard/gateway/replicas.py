"""Local replicas (forgeyard/gateway/proxy.py, steps 2 and 3): a tier's deployments tried in the balancer's order,
within their concurrency (in line for a slot, or an image request in the gateway's queue), moving on only when
a request provably never reached a replica, and a stream's wait for its first answer. `ReplicaSending` is the
local half of `proxy.Job`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Optional, Union

import httpx
from starlette.responses import Response

from forgeyard.gateway import imagegen, speech, waiting, waking
from forgeyard.gateway.balancer import Target, balancer
from forgeyard.gateway.body import streams, upstream_body
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.fit import prompt_estimate
from forgeyard.gateway.route import UPSTREAM_PATHS, LocalGroup, Tier
from forgeyard.gateway.upstream import (RETRYABLE, SENT_BROKE, ClientGone, EngineUnreachable, Failed, _empty, client,
                                        not_taken, unless_gone, upstream_retry_after)

log = logging.getLogger("forgeyard.gateway")

LOCAL_BUSY_RETRY_AFTER = 1   # seconds, on 503 local_busy: a slot of a text deployment frees up quickly (1.4.5)


class ReplicaSending:
    """`proxy.Job`'s local half: a tier's ready replicas tried in turn, and a stream's first answer awaited. Uses
    the job's request, route, parsed body, settings and bookkeeping (`gone`, `pause`, `headers`, `serve`)."""

    async def send_first(self, http: httpx.AsyncClient, req: httpx.Request, first: float) -> httpx.Response:
        """http.send, but the answer's status and headers must come within `first` seconds of the request
        being written to the replica (asyncio.TimeoutError otherwise; the request is then closed, so the
        engine stops). Connecting and writing keep httpx's own timeouts."""
        written = asyncio.Event()

        async def trace(name: str, info: dict) -> None:
            if name.endswith("send_request_body.complete") and not written.is_set():
                self.answer_by = time.monotonic() + first
                written.set()

        req.extensions = {**req.extensions, "trace": trace}
        task = asyncio.ensure_future(http.send(req, stream=True))
        waiting = asyncio.ensure_future(written.wait())
        try:
            # Until the request is written (bounded, in case a transport never says so).
            s = self.settings
            await asyncio.wait({task, waiting}, timeout=s.gateway_connect_timeout + 60.0 + first,
                               return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                if self.answer_by is None:
                    self.answer_by = time.monotonic()
                await asyncio.wait({task}, timeout=max(0.0, self.answer_by - time.monotonic()))
            if task.done():
                return task.result()
            raise asyncio.TimeoutError()
        finally:
            for t in (task, waiting):
                if not t.done():
                    t.cancel()
            for t in (task, waiting):
                with contextlib.suppress(BaseException):
                    await t

    def no_first_answer(self, g: "LocalGroup", pick: Target) -> GatewayError:
        log.warning("Replica %s of %s gave no answer within %g s of the request; closed, not tried elsewhere.",
                    pick.ordinal, g.name, self.first_answer or 0)
        return GatewayError(504, "first_answer_timeout",
                            f"Replica {pick.ordinal} of {g.name!r} took the request but did not start answering within "
                            f"{self.first_answer or 0:g} s (the deployment's first_answer_timeout). The request was "
                            "stopped and not sent to another replica, where it could have run twice.")

    async def first_chunk(self, response: httpx.Response, release: str) -> tuple[bytes, object]:
        """A stream's first bytes, within the replica's wait for a first answer (1.4.5): (them, the rest)."""
        chunks = response.aiter_bytes()
        if self.answer_by is None or self.sent_to is not None or self.answering is None:
            return b"", chunks
        g, pick = self.answering
        try:
            first = await unless_gone(self.request.receive, asyncio.wait_for(
                chunks.__anext__(), max(0.0, self.answer_by - time.monotonic())))
        except StopAsyncIteration:
            return b"", _empty()
        except ClientGone:
            await response.aclose()
            balancer.release(release)
            raise
        except asyncio.TimeoutError:
            await response.aclose()
            balancer.release(release)
            balancer.failed(pick.uid, reached=True)
            raise self.no_first_answer(g, pick) from None
        except httpx.HTTPError as e:
            await response.aclose()
            balancer.release(release)
            raise GatewayError(502, "upstream_error", f"The replica's stream broke off before its first byte "
                                                      f"({type(e).__name__}).") from None
        return first, chunks

    # -------------------------------------------------------------- local replicas
    async def local(self, tier: Tier, *, can_move_on: bool) -> Union[Response, Failed]:
        route, parsed = self.route, self.parsed
        pairs = [(g, t) for g in tier.locals for t in g.replicas]
        sleepy = waking.sleepy(tier.locals) if not pairs else []
        if sleepy:
            # 1.5.0 (#37): asleep because nobody used it, or being woken. This request wakes it (once for
            # everyone), then spills while it wakes when it may move on, or else waits for a ready replica.
            self.used = True
            by = waking.by_of(self.caller)
            await waking.ask(sleepy, by)
            if can_move_on:
                return Failed("waking", waking.waking_error(route.served_name, sleepy))
            try:
                fresh = await waking.until_awake(self.request.receive, f"{route.served_name}#{tier.index}",
                                                 route.served_name, tier.locals, sleepy, by)
            except ClientGone:
                return self.gone()
            tier = Tier(tier.index, fresh, list(tier.clouds))
            pairs = [(g, t) for g in tier.locals for t in g.replicas]
        if not pairs:
            reason = "; ".join(g.reason for g in tier.locals if g.reason)
            return Failed("unavailable", GatewayError(503, "no_ready_replica",
                                                      f"{route.served_name!r} has no ready replica. {reason}".strip()))
        if can_move_on and route.spill_on_long_prompt:
            need = prompt_estimate(parsed)
            fits = [(g, t) for g, t in pairs if not g.context or need <= g.context]
            if not fits:
                most = max(g.context or 0 for g, _ in pairs)
                return Failed("too_long", GatewayError(400, "context_length_exceeded",
                                                       f"The request needs about {need} tokens; the local deployments "
                                                       f"of {route.served_name!r} take at most {most}."))
            pairs = fits
        group_of = {t.uid: g for g, t in pairs}
        remaining = balancer.order(f"{route.served_name}#{tier.index}", [t for _, t in pairs])
        image = parsed.image is not None
        if image:
            # Image requests (1.3) always keep to each replica's concurrency (1 by default) and wait in
            # the gateway's queue, in order, for a free one.
            caps = {t.uid: group_of[t.uid].concurrency or 1 for t in remaining}
        else:
            caps = {t.uid: group_of[t.uid].concurrency for t in remaining} if can_move_on else {}
        self.queue_key = qkey = f"{route.served_name}#{tier.index}"
        q = imagegen.queue(qkey) if image else None
        # Text requests held to the concurrency wait in line for a slot (1.4.5); without caps nobody waits.
        line = waiting.line(qkey) if not image and any(caps.values()) else None
        self._ticket = None
        self._place = None
        try:
            return await self._local(tier, group_of, remaining, caps, q, can_move_on, line)
        finally:
            if q is not None and self._ticket is not None:
                q.leave(self._ticket)      # it gave up, failed or was sent: never left in the queue
                self._ticket = None
            if line is not None and self._place is not None:
                line.leave(self._place)    # the same for the line (a client that went away too)
                self._place = None

    async def _local(self, tier: Tier, group_of: dict, remaining: list, caps: dict, q: Optional[imagegen.Queue],
                     can_move_on: bool, line: Optional[waiting.Line] = None) -> Union[Response, Failed]:
        route, parsed = self.route, self.parsed
        image = q is not None
        waited_from = 0.0
        body_for: dict = {}
        speech_of = self._speech_of = {}
        last_exc: Optional[Exception] = None
        refused: Optional[Response] = None
        deadline: Optional[float] = None
        while remaining:
            # An image request tries for a slot only when nobody is waiting before it; so does a text
            # request held to the concurrency (1.4.5).
            may_try = not image or q.may_try(self._ticket)
            if line is not None:
                may_try = may_try and line.may_try(self._place)
            pick = next((t for t in remaining if balancer.try_acquire(t.uid, caps.get(t.uid))), None) if may_try else None
            if pick is None and image:
                if self._ticket is None:
                    limit = sum(g.draws.queue for g in tier.locals if g.draws is not None)
                    self._ticket = q.join(limit)
                    self.used = True    # 1.5.0: waiting in the queue is use (also when it is full: demand)
                    if self._ticket is None:
                        slots = sum(caps.get(t.uid) or 1 for t in remaining)
                        raise GatewayError(429, "queue_full",
                                           f"Every replica of {route.served_name!r} is busy and {len(q.waiting)} "
                                           f"request{'s are' if len(q.waiting) != 1 else ' is'} waiting already. "
                                           "Try again later.",
                                           retry_after=imagegen.retry_after(self.queue_key, len(q.waiting), slots))
                    waited_from = time.monotonic()
                most = max((g.draws.timeout for g in tier.locals if g.draws is not None), default=imagegen.DEFAULT_TIMEOUT)
                if time.monotonic() - waited_from > most:
                    raise GatewayError(503, "queue_timeout", f"The request waited {most:g} s for a free replica of "
                                                             f"{route.served_name!r}. Try again later.",
                                       retry_after=imagegen.retry_after(self.queue_key, len(q.waiting), 1))
                try:
                    await self.pause(0.05)
                except ClientGone:
                    return self.gone()
                continue
            if pick is not None and self._ticket is not None:
                q.leave(self._ticket)
                self._ticket = None
            if pick is not None and self._place is not None:
                line.leave(self._place)    # its turn: out of the line, the next one moves up
                self._place = None
            if pick is None:
                # Every replica left is at its deployment's concurrency, or others wait before it.
                if deadline is None:
                    deadline = time.monotonic() + route.wait_ms / 1000
                left = deadline - time.monotonic()
                if left > 0 and line is not None and self._place is None:
                    self._place = line.join()   # None: the line is full, so it does not wait
                    self.used = True            # 1.5.0: waiting for a slot is use
                    if self._place is not None:
                        continue    # in line: look once more, so a slot freed meanwhile is not missed
                if left > 0 and self._place is not None:
                    try:
                        await unless_gone(self.request.receive, line.wait(self._place, left))
                    except ClientGone:
                        return self.gone()
                    continue
                if refused is not None:
                    return Failed("refused", answer=refused)
                return Failed("busy", GatewayError(503, "local_busy", f"Every replica of {route.served_name!r} is busy.",
                                                   retry_after=LOCAL_BUSY_RETRY_AFTER))
            remaining.remove(pick)
            g = group_of[pick.uid]
            self.target, self.deployment_id, self.sent_to = "local", g.deployment_id, None
            self.dropped = []
            if parsed.image is not None:
                # Each image deployment gets its own body: its default size and steps, its request style.
                if g.deployment_id not in body_for:
                    obj = imagegen.body_for(parsed.image, g.draws, g.upstream_model)
                    body_for[g.deployment_id] = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                                                 .encode("utf-8"), False)
                body, strip = body_for[g.deployment_id]
                self.image_draws = g.draws
            elif parsed.tts is not None:
                # Each speech deployment gets its own body: its format, voice, language and frame cap.
                if g.deployment_id not in body_for:
                    body_for[g.deployment_id] = await self.speech_body(g, pick)
                body, strip = body_for[g.deployment_id]
                self.speech_format, self.speech_voice = speech_of[g.deployment_id]
            else:
                if g.upstream_model not in body_for:
                    body_for[g.upstream_model] = upstream_body(parsed, self.endpoint, g.upstream_model)
                body, strip = body_for[g.upstream_model]
            ctype = self.request.headers.get("content-type", "application/json") if parsed.parts is not None \
                else "application/json"
            headers = {"Content-Type": ctype, "Accept": self.request.headers.get("accept") or "*/*",
                       "Accept-Encoding": "identity", "User-Agent": "forgeyard-gateway"}
            if pick.secret:
                headers["Authorization"] = f"Bearer {pick.secret}"
            http = client()
            timeout = httpx.USE_CLIENT_DEFAULT
            if image and g.draws is not None:   # images take long: the deployment's own read timeout
                timeout = httpx.Timeout(connect=self.settings.gateway_connect_timeout, read=g.draws.timeout,
                                        write=60.0, pool=30.0)
            # A stream has the deployment's wait for a first answer, counted from the moment the request was
            # written to the replica. A non-streamed answer is the whole answer: GATEWAY_READ_TIMEOUT, as
            # before (Tony, #168); images keep their own timeout.
            first = g.first_answer if not image and streams(parsed) else None
            self.first_answer = first
            self.answer_by = None
            if first is not None and first > self.settings.gateway_read_timeout:
                # Each read may take as long as the first answer may (a long transcription, say).
                timeout = httpx.Timeout(connect=self.settings.gateway_connect_timeout, read=first, write=60.0,
                                        pool=30.0)
            try:
                path = UPSTREAM_PATHS.get(self.endpoint, f"/v1/{self.endpoint}")
                req = http.build_request("POST", f"{pick.url}{path}", content=body, headers=headers,
                                         timeout=timeout, extensions=pick.ext)
                self.sent_at = time.monotonic()
                self.used = True    # 1.5.0: sent to a replica, so the deployment was used (sleep when idle)
                sending = http.send(req, stream=True) if first is None else self.send_first(http, req, first)
                response = await unless_gone(self.request.receive, sending)
            except ClientGone:
                balancer.release(pick.uid)
                return self.gone()
            except asyncio.TimeoutError:
                balancer.release(pick.uid)
                balancer.failed(pick.uid, reached=True)
                raise self.no_first_answer(g, pick) from None
            except RETRYABLE as e:
                balancer.release(pick.uid)
                balancer.failed(pick.uid)
                last_exc = e
                log.warning("Replica %s of %s did not take the request (%s); trying the next one.",
                            pick.ordinal, g.name, type(e).__name__)
                continue
            except SENT_BROKE as e:
                balancer.release(pick.uid)
                balancer.failed(pick.uid, reached=True)
                log.warning("The connection to replica %s of %s broke after the request was sent (%s); not tried "
                            "on another replica.", pick.ordinal, g.name, type(e).__name__)
                raise GatewayError(502, "upstream_error", f"The connection to replica {pick.ordinal} of {g.name!r} broke "
                                                          f"after the request was sent ({type(e).__name__}). It was not "
                                                          "sent to another replica, since this one may have started on "
                                                          "it; retry if it is safe to.") from None
            except httpx.TimeoutException:
                balancer.release(pick.uid)
                limit = g.draws.timeout if image and g.draws is not None else self.settings.gateway_read_timeout
                raise GatewayError(504, "upstream_timeout", f"The replica did not answer within {limit:g} s.") from None
            except httpx.HTTPError as e:
                balancer.release(pick.uid)
                raise GatewayError(502, "upstream_error", f"The request to the replica failed ({type(e).__name__}).") from None
            out_headers = {"X-Forgeyard-Replica": f"{route.served_name}#{pick.ordinal}",
                           **self.headers(f"local:{g.name}#{pick.ordinal}")}
            if not_taken(response):
                # The replica's guard (1.4.5) says its engine never saw the request: the next replica may.
                await response.aclose()
                balancer.release(pick.uid)
                balancer.failed(pick.uid)
                last_exc = EngineUnreachable()
                log.warning("Replica %s of %s: its engine is not running (the guard answered); trying the next one.",
                            pick.ordinal, g.name)
                continue
            if response.status_code in (401, 403):
                await response.aclose()
                balancer.release(pick.uid)
                log.warning("Replica %s of %s refused the gateway's internal key (HTTP %s).", pick.ordinal, g.name,
                            response.status_code)
                raise GatewayError(502, "replica_refused_key", "The replica refused the gateway's internal key. Stop and "
                                                               "start the deployment to give it a new one.")
            if can_move_on and response.status_code in (429, 503):
                # The engine is overloaded: another replica, or the next tier, may take it.
                try:
                    data = await unless_gone(self.request.receive, response.aread())
                except ClientGone:
                    return self.gone()
                except httpx.HTTPError:
                    data = b""
                finally:
                    await response.aclose()
                    balancer.release(pick.uid)
                log.info("Replica %s of %s answered %s; moving on.", pick.ordinal, g.name, response.status_code)
                refused = Response(content=data, status_code=response.status_code,
                                   headers={**upstream_retry_after(response), **out_headers},
                                   media_type=response.headers.get("content-type") or "application/json")
                continue
            self.answering = (g, pick)
            return await self.serve(response, out_headers, strip, release=pick.uid, who="replica")
        if refused is not None:
            return Failed("refused", answer=refused)
        return Failed("unavailable", GatewayError(502, "upstream_unreachable",
                                                  f"No replica of {route.served_name!r} could be reached "
                                                  f"({type(last_exc).__name__ if last_exc else 'none tried'})."))

    async def speech_body(self, g: LocalGroup, pick: Target) -> tuple[bytes, bool]:
        """A speech request's body for this deployment; asks a replica for its voices (kept a while)
        only when the gateway has to choose one."""
        ask = self.parsed.tts
        obj = self.parsed.obj or {}
        names = None
        if g.speaks is not None and (ask.voice is None or ask.voice.lower() in speech.OPENAI_VOICES):
            found = await speech.voices_for(g.deployment_id, g.speaks, client(), pick.url, pick.secret,
                                            extensions=pick.ext)
            names = [v["id"] for v in found] if found is not None else None
        out, chosen = speech.body_for(obj, ask, g.speaks, names)
        out["model"] = g.upstream_model
        rate = obj.get("sample_rate")
        self.speech_rate = rate if isinstance(rate, int) and not isinstance(rate, bool) and rate > 0 else None
        self._speech_of[g.deployment_id] = (out.get("response_format") or ask.format, chosen)
        return json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), False
