"""Forwarding a gateway request to whoever serves its model: a deployment's replicas, or (1.2) a
cloud provider.

1. The request's `model` (a route's served name) picks the route. A route has targets in tiers:
   local deployments and provider models. Tier 0 is tried first.
2. Within a tier, a deployment's ready replicas are tried in the balancer's order (least in flight
   first). If connecting to a replica fails before anything was sent back, the next one is tried, and
   the failed one is tried last for a while. Provider targets of a tier are tried by weight; one
   that answers 429 or 5xx or cannot be reached cools down and the next is tried.
3. **Spilling** to the next tier happens only before the first byte reaches the client, and only
   when this tier is
   - *unavailable*: no ready replica, or no replica could be connected to;
   - *busy*: every ready replica already has as many requests in flight through this server as its
     deployment's `concurrency` (after waiting up to the route's `wait_ms` for a slot, in line, first
     come first served: forgeyard/gateway/waiting.py, 1.4.5);
   - *waking* (1.5.0, #37): its deployment was asleep because nobody used it; the request wakes it
     (forgeyard/gateway/waking.py) and, when nothing after the tier can take it either, waits for it to wake;
   - *refusing*: the replica answered 429 or 503;
   - *too small* (only when the route opts in): the prompt, estimated from its size, plus
     `max_tokens` does not fit the deployment's `context`.
   Never on other answers (a 400 would be the same anywhere), a timeout after the request was sent,
   the replica refusing the gateway's key, a client that went away, or once streaming has begun.
   **No request runs twice** (1.4.5): another replica (or provider) is tried only when the request
   provably never reached the first one (the connection could not be made, or no connection was free
   in the pool: RETRYABLE). A connection that breaks once the request was being sent (SENT_BROKE) may
   have left the engine working on it, so that answers 502 and is not tried again anywhere.
   **The first answer of a stream** (1.4.5): once a streamed request (`stream: true`, or speech with a
   `stream_format`) is sent to a replica, the replica has the deployment's `first_answer_timeout`
   (120 s by default) for the status line and the stream's first bytes. After that it is closed (the
   engine stops) and the client gets 504 `first_answer_timeout`; it is never sent elsewhere. Later
   pieces may take up to GATEWAY_READ_TIMEOUT each. A non-streamed answer is the whole answer, so it
   keeps GATEWAY_READ_TIMEOUT (600 s) as before; image requests their deployment's own `timeout`.
4. Which tiers a request may use: a route marked local only, a request with
   `X-Forgeyard-Local-Only: 1`, and a key whose `cloud` setting is off never reach a provider outside
   your network; with `fallback` only as a spill target (after a local tier); with `on` also cloud-only
   models. A model a key may not reach at all is "not found" for it.
5. The upstream request is built from the client's body only: `model` becomes the name the upstream
   expects, the replica's internal key or the provider key is added, and no other client header (its
   API key, cookies) is passed on. For a provider, the kind's quirks apply (forgeyard/gateway/quirks.py).
6. Streamed answers (SSE) are passed through as they arrive, never buffered. For a streamed chat or
   completion the gateway asks for a final usage chunk (`stream_options.include_usage`) to count
   tokens, and removes it again if the client did not ask for it.
7. If the client goes away, the upstream request is closed at once, so the engine stops generating.
8. The answer names who served it: `X-Forgeyard-Target: local:<deployment>#<replica>` or
   `provider:<provider>/<model>`, never a key; `X-Forgeyard-Spilled` says why it left tier 0, and
   `X-Forgeyard-Dropped-Params` (1.4.5) names the request parameters left out on the way to a provider.
   **Structured output is never lost silently** (1.4.5): a request with a JSON schema, a grammar or
   another guided-decoding parameter (quirks.STRUCTURED) skips every provider target that would drop or
   ignore it; with nothing else to serve it, it gets the local answer it would have had without the
   cloud (busy: sent to a replica anyway; unavailable: 503 saying why it was not spilled), and a route
   that only such providers serve answers 400 `structured_output_not_supported`.
9. Budgets (forgeyard/gateway/budgets.py): before a request goes to a provider outside your network,
   its estimated cost is reserved against the key's, the person's and the provider's monthly budget.
   A budget that cannot cover it skips that provider (the next target or tier, or waiting for a busy
   local tier, may still serve it) and the answer says so in `X-Forgeyard-Budget-Exhausted`; when
   nothing else can serve it, 402 `budget_exhausted` (or `budget_unpriced`). The reservation is
   settled with the real cost when the request finishes, and released when it fails.
10. Images, audio and video in chat (1.3, forgeyard/gateway/media.py): their parts are rebuilt, media
   URLs fetched by the gateway and inlined, and the route narrowed to the deployments that take
   what the request carries (`fit_route`). Such requests never go to a provider. Every deployment
   may also take larger bodies than GATEWAY_MAX_BODY_MB (its `max_body_mb`).
11. Text to speech (1.3, forgeyard/gateway/speech.py): /v1/audio/speech is checked and counted by the
   gateway (characters before, audio milliseconds after), voice cloning is refused, each deployment
   gets its own body (its default format, voice, language and frame cap), and the audio streams
   through as it is made. Local deployments only.
12. Images (1.3, forgeyard/gateway/imagegen.py): /v1/images/generations is checked against each
   deployment's caps (images per request, largest side, steps), sent with only the fields Forgeyard
   knows, queued in the gateway while every replica is busy (429 `queue_full` once the queue is full),
   answered as base64 only, and counted in images, kilopixels and busy milliseconds. Local only.
13. Videos (1.3, forgeyard/gateway/videogen.py and videojobs.py): /v1/videos is asynchronous: a job is
   checked against each deployment's caps, sent to one replica, which holds it, and its status and
   MP4 are relayed from that replica; it does not go through forward(). Local only.
14. Vision (1.4, forgeyard/models/vision.py): /v1/vision/detect, /segment, /depth and (1.4.4) /classify take one image
   (multipart with `model` and an `image` file, or JSON with `model` and `image` in base64) for a model
   of that task, passed to the vision server as it came (only `model` changed); its JSON answer is passed
   back unchanged and counted as one image, its kilopixels and the replica's busy milliseconds. Local
   only (no provider serves them). A zero-shot detector (1.4.4, #142) is told what to find with `labels` (a
   list of names, or a comma-separated string in a form); the vision server checks them (400 labels_required
   without them, labels_not_supported for a detector with fixed classes). So is a zero-shot image classifier (1.4.4,
   #141: CLIP, SigLIP) at /v1/vision/classify, which answers the labels it was given, scored.
15. Text classification (1.4.4, forgeyard/gateway/classify.py): /v1/classify takes `input` (a text or a list
   of texts) for a text classification model; the body is checked and rebuilt with the fields Forgeyard knows,
   sent to the engine's /classify (vLLM serves it without /v1), and the answer passed back, counted in prompt
   tokens. Local only.

The parts (1.4.5, #173): forgeyard/gateway/route.py (a route and its targets, steps 1 and 4), body.py (the
request body), fit.py (the targets that can take it), upstream.py (the client to the replicas, what may be
retried, disconnects), replicas.py and cloud.py (the two halves of a request's way through a tier),
streaming.py and answer.py (the answer). This module holds what ties them together: `Job` (one request through
the tiers, its usage counted once), `forward()` and `record()`, and re-exports their names, so every import of
forgeyard.gateway.proxy keeps working.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Union

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from forgeyard import metrics
from forgeyard.config import get_settings
from forgeyard.gateway import budgets, imagegen, pricing, quirks, speech, waiting
from forgeyard.gateway.answer import Answering
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.body import (CEILING_TTL, USAGE_ENDPOINTS, Parsed, _ceiling, body_ceiling, forget_ceiling,
                                    parse_body, parse_vision, read_body, streams, upstream_body)
from forgeyard.gateway.cloud import (DROPPED_HEADER, RETRY_AFTER_CAP, CloudSending, _SECRETISH, order_clouds,
                                     provider_error, retry_after, rng)
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.fit import (BYTES_PER_TOKEN, KIND_WORDS, MEDIA_ENDPOINTS, _takes, body_size, fit_classify,
                                   fit_embeddings, fit_images, fit_route, fit_speech, prompt_estimate, scan_media)
from forgeyard.gateway.keys import KeyCaller
from forgeyard.gateway.limits import Admission, Refusal, key_limiter
from forgeyard.gateway.replicas import LOCAL_BUSY_RETRY_AFTER, ReplicaSending
from forgeyard.gateway.route import (CLOUD_APIS, CLOUD_ENDPOINTS, ENDPOINTS, FIRST_ANSWER_DEFAULT, LOCAL_ONLY_HEADER,
                                     MAX_WAIT_MS, UPSTREAM_PATHS, CloudTarget, LocalGroup, Route, Tier, _int, _ready,
                                     _route_query, allowed_tiers, build_route, check_access, cloud_target,
                                     endpoint_for, load_route, local_group, model_names, not_ready_reason, serving,
                                     visible, wants_local_only)
from forgeyard.gateway.streaming import (AUDIO_HEADERS, AudioResponse, SpeechSSE, SSEFilter, StreamResponse,
                                         tokens_of)
from forgeyard.gateway.upstream import (CLIENT_CLOSED, NOT_TAKEN_HEADER, RETRYABLE, SENT_BROKE, ClientGone,
                                        EngineUnreachable, Failed, _clients, _empty, _until_disconnect, client,
                                        close_client, not_taken, unless_gone, upstream_retry_after)
from forgeyard.gateway.usage import recorder
from forgeyard.workloads.sleep import idle, route_deployments

__all__ = [
    # route.py
    "CLOUD_APIS", "CLOUD_ENDPOINTS", "ENDPOINTS", "FIRST_ANSWER_DEFAULT", "LOCAL_ONLY_HEADER", "MAX_WAIT_MS",
    "UPSTREAM_PATHS", "CloudTarget", "LocalGroup", "Route", "Tier", "_int", "_ready", "_route_query", "allowed_tiers",
    "build_route", "check_access", "cloud_target", "endpoint_for", "load_route", "local_group", "model_names",
    "not_ready_reason", "serving", "visible", "wants_local_only",
    # body.py
    "CEILING_TTL", "USAGE_ENDPOINTS", "Parsed", "_ceiling", "body_ceiling", "forget_ceiling", "parse_body",
    "parse_vision", "read_body", "streams", "upstream_body",
    # fit.py
    "BYTES_PER_TOKEN", "KIND_WORDS", "MEDIA_ENDPOINTS", "_takes", "body_size", "fit_classify", "fit_embeddings",
    "fit_images",
    "fit_route", "fit_speech", "prompt_estimate", "scan_media",
    # upstream.py
    "CLIENT_CLOSED", "NOT_TAKEN_HEADER", "RETRYABLE", "SENT_BROKE", "ClientGone", "EngineUnreachable", "Failed",
    "_clients", "_empty", "_until_disconnect", "client", "close_client", "not_taken", "unless_gone",
    "upstream_retry_after",
    # streaming.py
    "AUDIO_HEADERS", "AudioResponse", "SpeechSSE", "SSEFilter", "StreamResponse", "tokens_of",
    # cloud.py
    "DROPPED_HEADER", "RETRY_AFTER_CAP", "CloudSending", "_SECRETISH", "order_clouds", "provider_error",
    "retry_after", "rng",
    # replicas.py, answer.py
    "LOCAL_BUSY_RETRY_AFTER", "ReplicaSending", "Answering",
    # here
    "Job", "forward", "record",
]

log = logging.getLogger("forgeyard.gateway")
# A tier that failed for these reasons may still serve the request: it waits there after all when nothing after it
# could (busy: for a slot, 1.4.5; waking: for the model to wake, 1.5.0).
WAIT_LOCAL = ("busy", "waking")
balancer.listen(waiting.slot_freed)   # a freed slot wakes the first request waiting in line (1.4.5)


class Job(ReplicaSending, CloudSending, Answering):
    """One request on its way through the tiers. Usage is counted once, for whoever answered."""

    def __init__(self, request: Request, caller: KeyCaller, route: Route, endpoint: str, parsed: "Parsed",
                 started: float, admission: Optional[Admission]):
        self.request = request
        self.caller = caller
        self.route = route
        self.endpoint = endpoint
        self.parsed = parsed
        self.started = started
        self.admission = admission
        self.settled = False
        self.spilled: Optional[str] = None
        self.target = "local"                   # who the usage is counted for
        self.sent_to: Optional[CloudTarget] = None  # the provider it was last sent to
        self.deployment_id = route.deployment_id
        self.settings = get_settings()
        self.cost: Optional[pricing.Cost] = None  # once finished (budgets settle with it)
        self.hold: Optional[budgets.Hold] = None  # money reserved at the provider it was sent to
        self.budget_blocked: list[str] = []       # budgets that kept it from a provider
        self.budget_target: Optional[tuple] = None  # (target, provider) a budget kept it from, if nothing was sent
        self.speech_format: Optional[str] = None    # text to speech: the format the engine was asked for (1.3)
        self.speech_voice: Optional[str] = None     # the voice the gateway chose for it, if any
        self.speech_rate: Optional[int] = None      # a sample_rate the request asked for
        self.media_ms: Optional[int] = None         # the audio's length, once it has passed
        self.made: Optional[imagegen.Made] = None   # images: what the answer held (1.3)
        self.busy_ms: int = 0                       # images: the replica's time on it, times its GPUs
        self.image_draws: Optional[imagegen.Draws] = None
        self.seen: Optional[tuple[int, int]] = None   # vision: the size of the image the replica answered for (1.4)
        self.embedded: Optional[tuple[int, int]] = None   # embeddings: the images and kilopixels the engine embedded (1.5.4)
        self.busy_usage = False                     # the replica answering it counts its busy time (1.5.4)
        self.queue_key = ""
        self.sent_at = 0.0
        self._ticket: Optional[int] = None
        self.dropped: list[str] = []                # parameters left out for the provider it was sent to (1.4.5)
        self.first_answer: Optional[float] = None   # the replica's wait for a first answer, once sent (1.4.5)
        self.answer_by: Optional[float] = None      # by when (monotonic) its first answer must come
        self.answering: Optional[tuple] = None      # (group, replica) that is answering
        self.kept_from: list[str] = []              # providers skipped: they would lose the structured output
        self.first_byte_at: Optional[float] = None  # when the answer's first bytes came (monotonic; /metrics, 1.4.5)
        # 1.5.0: something was done for it (sent to a replica or provider, waited, woke the model), so it counts as
        # use of its deployments; a request the gateway refused at once (stopped, cannot wake, no target) does not.
        self.used = False

    # -------------------------------------------------------------- bookkeeping
    def finish(self, status: int, usage) -> None:
        if self.settled:
            return
        self.settled = True
        if self.used:
            idle.used(route_deployments(self.route))   # 1.5.0: its deployments were used until now (sleep when idle)
        u = quirks.read_usage(usage)
        if self.admission is not None:
            tts = self.parsed.tts
            key_limiter.settle(self.admission, tts.characters if tts is not None else u.prompt + u.completion)
        c = self.sent_to
        served = status < 400 or status == CLIENT_CLOSED
        self.cost = pricing.cost_of(u, c.prices if c else None, cloud=c is not None and c.external,
                                    served=served, reports_cached=quirks.get(c.kind).reports_cached if c else True)

        def count() -> None:
            record(self.caller, self.route.served_name, self.deployment_id, status, self.started, u.prompt,
                   u.completion, target=self.target, cached=u.cached, reasoning=u.reasoning,
                   spilled=self.spilled is not None, cost=self.cost, units=self.units(served), endpoint=self.endpoint,
                   first_byte=self.first_byte_at, spill_reason=self.spilled)

        hold, self.hold = self.hold, None
        if c is None or not c.external:
            budgets.ledger.release(hold)
            count()
            return
        # A cost that is unknown although the provider answered counts at its reservation.
        picos = self.cost.picos if self.cost.picos is not None else (hold.picos if hold and served else 0)
        budgets.ledger.settle(hold, key_id=self.caller.key_id if self.caller.source == "api" else 0,
                              user_id=self.caller.user_id, provider_id=c.provider_id, picos=max(0, picos), then=count,
                              estimated=self.cost.picos is None)

    def units(self, served: bool = True) -> Optional[dict]:
        """The request's 1.3 usage units: the media items it carried; for speech that was answered, the
        characters spoken and the audio's milliseconds."""
        m = self.parsed.media
        tts = self.parsed.tts
        if self.parsed.image is not None:
            if self.made is None:      # set only for an answer that held images
                return None
            return {"images": self.made.images, "kilopixels": self.made.kilopixels, "busy_ms": self.busy_ms}
        if self.parsed.vision is not None:
            if self.seen is None:      # set only for a 200 that says the image's size
                return None
            w, h = self.seen
            return {"images": 1, "kilopixels": round(w * h / 1000), "busy_ms": self.busy_ms}
        if tts is not None:
            if not served:
                return None
            return {"characters": tts.characters, **({"media_ms": self.media_ms} if self.media_ms else {})}
        out = {"media_items": m.items} if m is not None and m.items else {}
        if self.embedded is not None:      # 1.5.4: images embedded, as the vision endpoints count them
            out.update(images=self.embedded[0], kilopixels=self.embedded[1])
        if self.busy_usage and self.busy_ms:   # 1.5.4: the transformers server's time on it
            out["busy_ms"] = self.busy_ms
        return out or None

    def headers(self, target: str) -> dict:
        out = {"X-Forgeyard-Target": target}
        if self.speech_voice:
            out[speech.VOICE_HEADER] = self.speech_voice
        if self.spilled:
            out["X-Forgeyard-Spilled"] = self.spilled
        if self.budget_blocked:
            out[budgets.HEADER] = ",".join(self.budget_blocked)
        if self.dropped:
            out[DROPPED_HEADER] = ",".join(self.dropped)
        return out

    def keep_structure(self, tiers: list[Tier]) -> list[Tier]:
        """The tiers without the provider targets that would drop or ignore the request's structured
        output (1.4.5). Raises 400 when no target is left at all."""
        obj = self.parsed.obj
        if not quirks.structured(obj) or not any(t.clouds for t in tiers):
            return tiers
        out: list[Tier] = []
        for tier in tiers:
            clouds = []
            for c in tier.clouds:
                lost = quirks.structure_lost(c.kind, obj, self.endpoint, c.drop_params)
                if lost:
                    self.kept_from.append(f"{c.provider} would drop {', '.join(lost)}")
                    log.info("A request with structured output was not sent to %s (%s): it would drop %s.",
                             c.provider, c.upstream_model, ", ".join(lost))
                else:
                    clouds.append(c)
            if tier.locals or clouds:
                out.append(Tier(tier.index, list(tier.locals), clouds))
        if not out:
            raise GatewayError(400, "structured_output_not_supported",
                               f"{self.route.served_name!r} is served here only by providers that would not keep this "
                               f"request's structured output: {'; '.join(dict.fromkeys(self.kept_from))}. Leave the "
                               "parameter out, or ask an admin for a target that takes it.")
        return out

    def not_spilled(self) -> str:
        """What a local error adds when providers were skipped for the structured output."""
        if not self.kept_from:
            return ""
        return f" Not sent to the cloud: {'; '.join(dict.fromkeys(self.kept_from))} (structured output)."

    def gone(self) -> Response:
        self.finish(CLIENT_CLOSED, None)
        return Response(status_code=CLIENT_CLOSED)

    async def pause(self, seconds: float) -> None:
        await unless_gone(self.request.receive, asyncio.sleep(seconds))

    # -------------------------------------------------------------- the tiers
    async def run(self, tiers: list[Tier]) -> Response:
        failure: Optional[Failed] = None
        busy: Optional[Tier] = None
        for i, tier in enumerate(tiers):
            more = i < len(tiers) - 1
            result = await self.tier(tier, more)
            if isinstance(result, Response):
                return result
            if result.reason in WAIT_LOCAL and busy is None:
                busy = tier
            failure = result
        if busy is not None:
            # Nothing after the busy (or waking, 1.5.0) tier could take it either: it waits for a local slot, or for
            # the model to wake, after all.
            self.spilled = None
            result = await self.local(busy, can_move_on=False)
            if isinstance(result, Response):
                return result
            failure = result
        assert failure is not None
        if failure.answer is not None:
            self.finish(failure.answer.status_code, None)
            return failure.answer
        raise failure.error or GatewayError(502, "upstream_unreachable", "Nothing could answer.")

    async def tier(self, tier: Tier, more: bool) -> Union[Response, Failed]:
        failed: Optional[Failed] = None
        if tier.locals:
            result = await self.local(tier, can_move_on=more or bool(tier.clouds))
            if isinstance(result, Response):
                return result
            failed = result
            if more or tier.clouds:
                self.spilled = self.spilled or result.reason
        if tier.clouds:
            result = await self.cloud(tier, more)
            if isinstance(result, Response):
                return result
            if failed is None or failed.reason not in WAIT_LOCAL:
                failed = result
            elif result.error is not None:
                failed = Failed(failed.reason, error=result.error)
            if more and self.spilled is None:
                self.spilled = result.reason
        assert failed is not None
        return failed


async def forward(request: Request, *, caller: KeyCaller, route: Route, endpoint: str, parsed: Parsed,
                  started: float) -> Response:
    """Limits, a target, the upstream request and the answer. Records usage when it is over."""
    admission: Optional[Admission] = None
    if caller.source == "api":
        verdict = key_limiter.admit(caller.key_id, caller.rpm, caller.tpm, parsed.max_tokens)
        if isinstance(verdict, Refusal):
            what = "requests" if verdict.which == "rpm" else "tokens"
            record(caller, route.served_name, route.deployment_id, 429, started, limited=True, endpoint=endpoint)
            raise GatewayError(429, "rate_limit_exceeded",
                               f"This API key may use {verdict.limit} {what} per minute; try again in "
                               f"{verdict.retry_after} s.", retry_after=verdict.retry_after)
        admission = verdict
    job = Job(request, caller, route, endpoint, parsed, started, admission)
    # 1.5.0 (#37): use of every deployment the route serves, in flight while the gateway holds it (waiting in line
    # or in the image queue too; a stream being passed on is in flight on its replica, in the balancer).
    using = idle.begin(route_deployments(route))
    try:
        local_only = wants_local_only(request)
        tiers = allowed_tiers(route, caller.cloud, local_only, endpoint)
        if not tiers:
            if route.has_cloud and (local_only or route.local_only):
                raise GatewayError(400, "cloud_only_model", f"{route.served_name!r} is served only by cloud providers, "
                                                            "and this request may not leave your network.")
            raise GatewayError(503, "no_target", f"{route.served_name!r} has no target that can take this request "
                                                 "(every target is disabled, or none serves this endpoint).")
        clouds = [c for t in tiers for c in t.clouds if c.external]
        if clouds and (caller.budget_micros is not None or caller.user_budget_micros is not None
                       or any(c.budget_micros is not None for c in clouds)):
            try:
                await run_in_threadpool(budgets.ledger.ensure)  # this month's spend, off the event loop
            except Exception:
                log.exception("Reading this month's spend for budgets failed")
        tiers = job.keep_structure(tiers)
        return await job.run(tiers)
    except GatewayError as e:
        if job.budget_blocked and budgets.HEADER not in e.headers:
            e.headers[budgets.HEADER] = ",".join(job.budget_blocked)
        if job.dropped and job.sent_to is not None and DROPPED_HEADER not in e.headers:
            e.headers[DROPPED_HEADER] = ",".join(job.dropped)
        if job.kept_from and e.code != "structured_output_not_supported" and e.status >= 500:
            text = e.message.rstrip()
            e.message = text + ("" if text.endswith(".") else ".") + job.not_spilled()
        if e.status == 402 and job.budget_target is not None and job.sent_to is None:
            job.target, _ = job.budget_target  # counted under the provider, at no cost
            job.deployment_id = None
        job.finish(e.status, None)
        raise
    except BaseException:
        job.release()
        raise
    finally:
        idle.end(using, used=job.used)


def record(caller: KeyCaller, model: str, deployment_id: Optional[int], status: int, started: float,
           prompt: int = 0, completion: int = 0, limited: bool = False, *, target: str = "local", cached: int = 0,
           reasoning: int = 0, spilled: bool = False, cost: Optional[pricing.Cost] = None,
           units: Optional[dict] = None, endpoint: str = "", known: bool = True, first_byte: Optional[float] = None,
           spill_reason: Optional[str] = None) -> None:
    """Usage, and /metrics (1.4.5, #165). `known`: `model` is a route the gateway has (not only a name a client
    sent); `first_byte`: when the answer's first bytes came (monotonic)."""
    cost = cost or pricing.Cost(0, pricing.NONE)
    now = time.monotonic()
    metrics.request(endpoint=endpoint, model=model, known=known, status=status, source=caller.source, target=target,
                    seconds=now - started, first_byte=None if first_byte is None else first_byte - started,
                    prompt_tokens=prompt, completion_tokens=completion, spilled=spill_reason)
    recorder.record(source=caller.source, key_id=caller.key_id, user_id=caller.user_id, username=caller.username,
                    model=model, deployment_id=deployment_id, status=status,
                    latency_ms=(now - started) * 1000, prompt_tokens=prompt,
                    completion_tokens=completion, limited=limited, target=target, cached_tokens=cached,
                    reasoning_tokens=reasoning, spilled=spilled, cost_picos=cost.picos, cost_method=cost.method,
                    units=units)
