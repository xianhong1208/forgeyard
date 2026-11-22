"""Provider targets (forgeyard/gateway/proxy.py, steps 2, 5, 8 and 9): their order by weight and cooldown, a
provider's error in Forgeyard's shape, the budget reservation before a request leaves your network, and
sending it with the kind's quirks. `CloudSending` is the provider half of `proxy.Job`."""

from __future__ import annotations

import email.utils
import json
import logging
import random
import re
import time
from typing import Optional, Union

import httpx
from starlette.responses import Response

from forgeyard.gateway import budgets, egress, quirks
from forgeyard.gateway.balancer import PROVIDER_COOLDOWN, PROVIDER_COOLDOWN_MAX, balancer
from forgeyard.gateway.body import USAGE_ENDPOINTS
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.route import CloudTarget, Tier
from forgeyard.gateway.upstream import RETRYABLE, SENT_BROKE, ClientGone, Failed, unless_gone

log = logging.getLogger("forgeyard.gateway")

DROPPED_HEADER = "X-Forgeyard-Dropped-Params"   # 1.4.5: what a provider was sent without
RETRY_AFTER_CAP = 2.0   # with no other target left, a provider's Retry-After up to this is waited for once

rng = random.Random()
_SECRETISH = re.compile(r"\b(sk|rk|pk|key|sess)-[A-Za-z0-9_\-*.]{6,}|\bAIza[0-9A-Za-z_\-]{10,}", re.IGNORECASE)


def order_clouds(targets: list[CloudTarget]) -> list[CloudTarget]:
    """By weight (a weighted shuffle), the ones cooling down last, the longest cooldown last of all."""
    keyed = [(balancer.cooldown_left(t.key), -(rng.random() ** (1.0 / max(1, t.weight))), i, t)
             for i, t in enumerate(targets)]
    return [t for *_, t in sorted(keyed, key=lambda k: (k[0] > 0, k[0], k[1], k[2]))]


def retry_after(headers: httpx.Headers) -> Optional[float]:
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000)
        except ValueError:
            pass
    value = (headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def provider_error(c: CloudTarget, status: int, data: bytes, ra: Optional[float] = None) -> GatewayError:
    """A provider's error answer in Forgeyard's shape. Its message is kept (it says what was wrong
    with the request) without anything that looks like a key; 401/403 are never passed on as such,
    since a client would blame its own Forgeyard key."""
    message, code, param = "", None, None
    try:
        obj = json.loads(data or b"null")
        if isinstance(obj, list) and obj:
            obj = obj[0]
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict):
            message, code, param = str(err.get("message") or ""), err.get("code") or err.get("type"), err.get("param")
        elif isinstance(err, str):
            message = err
        elif isinstance(obj, dict) and obj.get("message"):
            message = str(obj["message"])
    except (ValueError, UnicodeDecodeError):
        message = data[:300].decode("utf-8", "replace") if data else ""
    if c.api_key:
        message = message.replace(c.api_key, "[redacted]")
    message = _SECRETISH.sub("[redacted]", " ".join(message.split()))[:500] or f"HTTP {status}"
    code = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(code))[:64] if code not in (None, "") else None
    param = str(param)[:64] if isinstance(param, str) else None
    if status == 429:
        return GatewayError(429, "provider_rate_limited", f"{c.provider} is rate limiting this model: {message}",
                            retry_after=max(1, int(ra + 0.999)) if ra else None)
    if status >= 500:
        return GatewayError(502, "provider_error", f"{c.provider} answered {status}: {message}")
    return GatewayError(status, code or "provider_error", f"{c.provider}: {message}", param=param)



class CloudSending:
    """`proxy.Job`'s provider half: the budget reservation, and the request sent to each provider target of a
    tier in turn. Uses the job's request, caller, parsed body, settings and bookkeeping (`gone`, `pause`,
    `headers`, `serve`)."""

    def release(self) -> None:
        hold, self.hold = self.hold, None
        budgets.ledger.release(hold)

    def reserve(self, c: CloudTarget) -> Optional[Failed]:
        """Reserve the request's estimated cost against the budgets that apply at `c`, or why not."""
        self.release()
        if not c.external:
            return None
        limits = budgets.limits_for(self.caller, c.provider_id, c.provider, c.budget_micros)
        if not limits:
            return None
        need = budgets.estimate(self.parsed.obj, self.parsed.max_tokens, self.endpoint, c.prices,
                                self.settings.budget_default_max_tokens)
        try:
            verdict = budgets.ledger.reserve(limits, need, model=f"{c.provider}/{c.upstream_model}")
        except Exception:
            log.exception("The budgets could not be read; %s is not used for this request.", c.provider)
            return Failed("unavailable", GatewayError(503, "budget_unavailable", "This month's spend could not be read, "
                                                                                 "so no cloud request is sent under a "
                                                                                 "budget. Try again shortly."))
        if isinstance(verdict, budgets.Blocked):
            if verdict.limit.token not in self.budget_blocked:
                self.budget_blocked.append(verdict.limit.token)
            log.info("A %s budget (%s) kept a request from %s.", verdict.limit.scope, verdict.limit.name, c.provider)
            if self.sent_to is None and self.target == "local":
                self.budget_target = (c.key, c)
            return Failed("budget", verdict.error())
        self.hold = verdict
        return None

    # -------------------------------------------------------------- providers
    async def cloud(self, tier: Tier, more: bool) -> Union[Response, Failed]:
        failures: list[tuple[CloudTarget, Failed]] = []
        for c in order_clouds(tier.clouds):
            result = await self.send_cloud(c)
            if isinstance(result, Response):
                return result
            failures.append((c, result))
        cap = RETRY_AFTER_CAP
        waits = [(f.retry_after, i, c) for i, (c, f) in enumerate(failures) if f.retry_after is not None
                 and f.retry_after <= cap]
        if waits and not more:
            # Nothing else is left: wait the short Retry-After once.
            delay, _, c = min(waits, key=lambda w: (w[0], w[1]))
            try:
                await self.pause(delay)
            except ClientGone:
                return self.gone()
            result = await self.send_cloud(c, again=True)
            if isinstance(result, Response):
                return result
            failures.append((c, result))
        # A provider's own failure says more than a budget that kept the request from another one.
        return next((f for _, f in reversed(failures) if f.reason != "budget"), failures[-1][1])

    async def send_cloud(self, c: CloudTarget, again: bool = False) -> Union[Response, Failed]:
        blocked = self.reserve(c)
        if blocked is not None:
            return blocked
        result = await self._send_cloud(c, again)
        if isinstance(result, Failed):
            self.release()
        return result

    async def _send_cloud(self, c: CloudTarget, again: bool = False) -> Union[Response, Failed]:
        parsed, endpoint = self.parsed, self.endpoint
        self.target, self.deployment_id, self.sent_to = c.key, None, c
        kind = quirks.get(c.kind)
        obj = dict(parsed.obj or {})
        obj["model"] = c.upstream_model
        strip = False
        if parsed.stream and endpoint in USAGE_ENDPOINTS and kind.stream_usage:
            options = obj.get("stream_options")
            options = dict(options) if isinstance(options, dict) else {}
            if options.get("include_usage") is not True:
                options["include_usage"] = True
                obj["stream_options"] = options
                strip = True
        obj, self.dropped = quirks.outgoing(c.kind, obj, endpoint, c.drop_params)
        if self.dropped:
            log.info("Left out for %s (%s): %s.", c.provider, c.upstream_model, ", ".join(self.dropped))
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cooldown = PROVIDER_COOLDOWN
        try:
            url, host_headers, extensions = await egress.prepare(c.base_url, endpoint, external=c.external,
                                                                 allow_http=c.allow_http)
        except egress.EgressError as e:
            log.warning("Provider %s: the request was not sent (%s).", c.provider, e)
            balancer.cool_down(c.key, cooldown)
            return Failed("unavailable", GatewayError(502, "provider_blocked", f"{c.provider}: {e}."))
        headers = {k: str(v) for k, v in (c.headers or {}).items()}
        headers.update({"Content-Type": "application/json", "Accept": self.request.headers.get("accept") or "*/*",
                        "Accept-Encoding": "identity", "User-Agent": "forgeyard-gateway"})
        headers.update(host_headers)
        headers.update(quirks.auth_headers(c.kind, c.api_key))
        http = egress.client()
        balancer.acquire(c.key)
        try:
            req = http.build_request("POST", url, content=body, headers=headers, extensions=extensions,
                                     timeout=egress.timeout_for(c.timeout_s) or httpx.USE_CLIENT_DEFAULT)
            response = await unless_gone(self.request.receive, http.send(req, stream=True))
        except ClientGone:
            balancer.release(c.key)
            return self.gone()
        except RETRYABLE as e:
            balancer.release(c.key)
            balancer.cool_down(c.key, cooldown)
            log.warning("Provider %s (%s) could not be reached (%s); trying the next target.", c.provider,
                        c.upstream_model, type(e).__name__)
            return Failed("unavailable", GatewayError(502, "provider_unreachable",
                                                      f"{c.provider} could not be reached ({type(e).__name__})."))
        except httpx.TimeoutException:
            balancer.release(c.key)
            raise GatewayError(504, "provider_timeout", f"{c.provider} did not answer in time.") from None
        except SENT_BROKE as e:
            # It may have reached the provider (and be billed there): never sent to another target.
            balancer.release(c.key)
            balancer.cool_down(c.key, cooldown)
            log.warning("The connection to provider %s (%s) broke after the request was sent (%s); not tried on "
                        "another target.", c.provider, c.upstream_model, type(e).__name__)
            raise GatewayError(502, "provider_error", f"The connection to {c.provider} broke after the request was sent "
                                                      f"({type(e).__name__}). It was not sent to another target, since "
                                                      "it may have been served; retry if it is safe to.") from None
        except httpx.HTTPError as e:
            balancer.release(c.key)
            balancer.cool_down(c.key, cooldown)
            return Failed("unavailable", GatewayError(502, "provider_error",
                                                      f"The request to {c.provider} failed ({type(e).__name__})."))
        status = response.status_code
        rid = response.headers.get("x-request-id") or response.headers.get("request-id") or "-"
        if 300 <= status < 400:
            await response.aclose()
            balancer.release(c.key)
            balancer.cool_down(c.key, cooldown)
            log.warning("Provider %s answered a redirect (%s); not followed.", c.provider, status)
            return Failed("unavailable", GatewayError(502, "provider_redirect",
                                                      f"{c.provider} answered with a redirect ({status}); Forgeyard does "
                                                      "not follow redirects to providers. Check its base URL."))
        if status in (401, 403, 407):
            await response.aclose()
            balancer.release(c.key)
            log.warning("Provider %s refused Forgeyard's key for it (HTTP %s, request id %s).", c.provider, status, rid)
            raise GatewayError(502, "provider_auth_failed", f"The provider {c.provider} refused Forgeyard's key for it "
                                                            f"(HTTP {status}). An admin needs to check the provider.")
        if status >= 400:
            try:
                data = await unless_gone(self.request.receive, response.aread())
            except ClientGone:
                return self.gone()
            except httpx.HTTPError:
                data = b""
            finally:
                await response.aclose()
                balancer.release(c.key)
            ra = retry_after(response.headers)
            err = provider_error(c, status, data[:65536], ra)
            if status == 429 or status >= 500:
                balancer.cool_down(c.key, max(cooldown, min(ra or 0.0, PROVIDER_COOLDOWN_MAX)))
                log.warning("Provider %s (%s) answered %s (request id %s)%s.", c.provider, c.upstream_model, status, rid,
                            "" if again else "; trying the next target")
                return Failed("refused" if status in (429, 503) else "unavailable", err, retry_after=ra, status=status)
            raise err
        return await self.serve(response, self.headers(c.label), strip, release=c.key, who=c.provider)
