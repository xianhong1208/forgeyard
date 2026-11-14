"""Video jobs (1.3): /v1/videos through the gateway, with each job kept on the replica that makes it.

vLLM-Omni makes a video in the background and keeps the job (in its memory) and the file (on its own
disk) until asked to delete them. So a job belongs to one replica for its whole life:

- **Asked** (POST /v1/videos): checked (forgeyard/gateway/videogen.py) and admitted (the key's rpm; the
  deployment's `queue`: jobs waiting or being made on it; its `jobs_per_key`: of those, this key's, or
  this person's in the Playground; 429 with Retry-After from how long its videos took lately), then sent
  to the ready replica with the fewest jobs, which answers at once with its own job id. A replica that
  cannot be reached, or answers 429 or 503, is passed over for the next. The job is written to
  `video_jobs` with that replica (its uid and its generation and restarts then) and the engine's id;
  the client gets Forgeyard's own id.
- **Followed**: a sweeper asks the replica about every job at work every few seconds (and a client's GET
  of one asks too, at most every 2 s): queued, in progress (when it starts), completed (its length, from
  the engine), failed (the engine's error). The server can restart in between: the jobs are in its
  database, the replicas keep working, and the sweeper picks them up again.
- **Lost with its replica**: a job whose replica is gone (stopped, moved, deleted) or restarted (its
  restarts changed, or it answers that it does not know the job) is reported `failed` with that reason:
  the engine kept it in memory. A finished video whose replica went the same way answers 410 for its
  content. A job still at work after the deployment's `timeout` is cancelled on the replica and failed.
- **Expiry**: a finished (or failed) job's result can be fetched for the deployment's `keep_minutes`
  (`expires_at`); then it is deleted on the replica (the engine's own FILE_TTL sweep is a safety net
  behind that) and its content answers 410. The row goes a week after that.
- **Content** (GET /v1/videos/{id}/content): streamed from the replica as it comes (Range requests pass
  through, so a browser can seek), as video/mp4.
- **Deleting** (DELETE /v1/videos/{id}): the job is cancelled or its file deleted on the replica (best
  effort: a replica that is gone took it already), and the row goes.
- **Who sees what**: a key sees only the jobs it asked for; the Playground only the person's own. Any
  other id, another key's included, answers 404 the same way.
- **Usage**: one request per job, recorded when it ends: a finished video counts its length in
  `media_ms` and the replica's time on it (from when it was seen in progress, times its GPUs) in
  `busy_ms`; a failed one counts as an error and nothing else; one deleted while at work as 499 (not an
  error). A refused request (400, 404, 429) is counted when it is refused, as everywhere else. Polls,
  lists and downloads are not requests. No cloud.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import httpx
from sqlalchemy import delete as sql_delete, func, select, update
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from forgeyard.config import get_settings
from forgeyard.db import Replica, VideoJob, as_utc, new_session, utcnow
from forgeyard.gateway import imagegen, proxy, videogen
from forgeyard.gateway.balancer import Target, balancer
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.keys import KeyCaller
from forgeyard.gateway.limits import Refusal, key_limiter
from forgeyard.gateway.usage import recorder

log = logging.getLogger("forgeyard.gateway")

POLL_SECONDS = 3.0        # the sweeper's pass over the jobs at work
FRESH_SECONDS = 2.0       # a client's GET of a job at work asks its replica at most this often
ROW_DAYS = 7              # a row is kept this long after its result expired
SUBMIT_TIMEOUT = 60.0
STATUS_TIMEOUT = 10.0
DELETE_TIMEOUT = 15.0
LIST_DEFAULT, LIST_MAX = 20, 100
TOOK_DEFAULT = 180.0      # seconds a video takes, until one was made
GONE_STATES = ("stopping", "stopped", "exited", "failed")
STATUS_FOR = {"generation_failed": 500, "timed_out": 504, "replica_restarted": 503, "replica_gone": 503,
              "video_lost": 503}

_fresh: dict[int, float] = {}       # job id -> when its replica was last asked (monotonic)
_refreshing: set[int] = set()
_took: dict[int, float] = {}        # deployment id -> seconds its videos took lately (a moving average)
_locks: dict[int, tuple] = {}


def forget() -> None:
    _fresh.clear()
    _refreshing.clear()
    _took.clear()
    _locks.clear()


def _lock() -> asyncio.Lock:
    """One lock per event loop: admission (count, then insert) is one step."""
    loop = asyncio.get_running_loop()
    entry = _locks.get(id(loop))
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _locks[id(loop)] = entry
    return entry[1]


class Answer(Exception):
    """An engine's own answer to pass on (an error it gave for the request)."""

    def __init__(self, response: Response):
        super().__init__("engine answer")
        self.response = response


@contextlib.contextmanager
def _db():
    db = new_session()
    try:
        yield db
    finally:
        db.close()


def _owner(caller: KeyCaller) -> tuple:
    if caller.source == "api":
        return VideoJob.source == "api", VideoJob.key_id == caller.key_id
    return VideoJob.source == "console", VideoJob.user_id == caller.user_id


def _not_found(public_id: str) -> GatewayError:
    return GatewayError(404, "video_not_found", f"No video {public_id[:48]!r} for this key. GET /v1/videos lists "
                                                "yours.", param="video_id")


# ------------------------------------------------------------------ the replica that holds a job
@dataclass
class Where:
    url: Optional[str]
    secret: Optional[str]
    epoch: str
    gone: Optional[str] = None       # why it cannot hold the job any more: replica_gone | replica_restarted


def epoch_of(r: Replica) -> str:
    """What changes when a replica is placed again (generation) or its engine restarted (restarts)."""
    return f"{r.generation}:{r.restarts}"


def where(db, uid: str, epoch: str) -> Where:
    r = db.scalar(select(Replica).where(Replica.uid == uid).options(selectinload(Replica.node),
                                                                    selectinload(Replica.workload)))
    if r is None:
        return Where(None, None, "", "replica_gone")
    now = epoch_of(r)
    gone = None
    if r.workload.desired_state != "running" or r.final or r.state in GONE_STATES:
        gone = "replica_gone"
    elif now.split(":")[0] != epoch.split(":")[0]:
        gone = "replica_gone"          # placed again: a new replica under the same row
    elif now != epoch or r.state == "backoff":
        gone = "replica_restarted"
    port = next(iter(r.ports or []), None)
    url = None
    if port and port.get("host_port") and port.get("address"):
        host = port["address"]
        host = f"[{host}]" if ":" in host else host
        url = f"http://{host}:{port['host_port']}"
    return Where(url, r.secret, now, gone)


def _headers(secret: Optional[str], **more) -> dict:
    out = {"Accept-Encoding": "identity", "User-Agent": "forgeyard-gateway", **more}
    if secret:
        out["Authorization"] = f"Bearer {secret}"
    return out


def _timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(connect=get_settings().gateway_connect_timeout, read=read, write=60.0, pool=30.0)


# ------------------------------------------------------------------ usage
def note_took(deployment_id: Optional[int], seconds: float) -> None:
    if deployment_id is None:
        return
    old = _took.get(deployment_id)
    _took[deployment_id] = seconds if old is None else 0.7 * old + 0.3 * seconds


def retry_after(deployment_id: Optional[int], ahead: int, replicas: int) -> int:
    per = _took.get(deployment_id, TOOK_DEFAULT) if deployment_id is not None else TOOK_DEFAULT
    return max(5, min(3600, int(per * max(1, ahead) / max(1, replicas) + 0.999)))


def _count(db, job: VideoJob, status: int, units: Optional[dict] = None) -> Optional[dict]:
    """Mark the job counted (once, whoever gets there first) and return what to record."""
    done = db.execute(update(VideoJob).where(VideoJob.id == job.id, VideoJob.counted.is_(False))
                      .values(counted=True)).rowcount
    if not done:
        return None
    ended = as_utc(job.completed_at) or utcnow()
    return {"source": job.source, "key_id": job.key_id, "user_id": job.user_id, "username": job.username,
            "model": job.model, "deployment_id": job.deployment_id, "status": status,
            "latency_ms": max(0.0, (ended - as_utc(job.created_at)).total_seconds() * 1000), "units": units}


def _record(entry: Optional[dict]) -> None:
    if entry is not None:
        recorder.record(**entry)


# ------------------------------------------------------------------ asking for a video
def _route(name: str) -> Optional[proxy.Route]:
    with _db() as db:
        return proxy.load_route(db, name)


@dataclass
class Counts:
    deployment: dict
    mine: dict
    replica: dict


def _counts(deployments: list[int], caller: KeyCaller) -> Counts:
    with _db() as db:
        active = VideoJob.status.in_(videogen.ACTIVE)
        dep = dict(db.execute(select(VideoJob.deployment_id, func.count()).where(
            active, VideoJob.deployment_id.in_(deployments)).group_by(VideoJob.deployment_id)).all())
        mine = dict(db.execute(select(VideoJob.deployment_id, func.count()).where(
            active, VideoJob.deployment_id.in_(deployments), *_owner(caller)).group_by(VideoJob.deployment_id)).all())
        rep = dict(db.execute(select(VideoJob.replica_uid, func.count()).where(
            active, VideoJob.deployment_id.in_(deployments)).group_by(VideoJob.replica_uid)).all())
    return Counts(dep, mine, rep)


def _no_room(route: proxy.Route, groups: list[proxy.LocalGroup], why: dict, counts: Counts) -> GatewayError:
    name = route.served_name
    mine = [g for g in groups if why.get(g.deployment_id) == "too_many_jobs"]
    if mine:
        g = mine[0]
        n = counts.mine.get(g.deployment_id, 0)
        return GatewayError(429, "too_many_jobs", f"You have {n} video{'s' if n != 1 else ''} waiting or being made on "
                                                  f"{name!r} already (at most {g.films.per_key} at once). Try again when "
                                                  "one is done.",
                            retry_after=retry_after(g.deployment_id, 1, max(1, len(g.replicas))))
    full = [g for g in groups if why.get(g.deployment_id) == "queue_full"]
    if full:
        g = full[0]
        n = counts.deployment.get(g.deployment_id, 0)
        return GatewayError(429, "queue_full", f"{name!r} has {n} videos waiting or being made already (at most "
                                               f"{g.films.queue}). Try again later.",
                            retry_after=retry_after(g.deployment_id, n - g.films.queue + 1, max(1, len(g.replicas))))
    reason = "; ".join(g.reason for g in groups if g.reason)
    return GatewayError(503, "no_ready_replica", f"{name!r} has no ready replica. {reason}".strip())


def _insert(caller: KeyCaller, model: str, g: proxy.LocalGroup, t: Target, ask: videogen.Ask, upstream_id: str) -> VideoJob:
    f = g.films
    with _db() as db:
        r = db.scalar(select(Replica).where(Replica.uid == t.uid))
        now = utcnow()
        w, h = videogen.size_of(ask, f)
        secs = videogen.seconds_of(ask, f)
        job = VideoJob(public_id=videogen.new_id(), source=caller.source, key_id=caller.key_id or 0,
                       user_id=caller.user_id or 0, username=caller.username or "", model=model,
                       deployment_id=g.deployment_id, replica_uid=t.uid, replica_epoch=epoch_of(r) if r else "0:0",
                       upstream_id=upstream_id, status="queued", progress=0, size=f"{w}x{h}", seconds=secs,
                       frames=videogen.frames_of(secs, f), fps=f.fps, gpus=f.gpus, created_at=now,
                       deadline_at=now + timedelta(seconds=f.timeout), keep_s=f.keep, removed=False, counted=False)
        db.add(job)
        db.commit()
        return job


async def _send(g: proxy.LocalGroup, t: Target, ask: videogen.Ask):
    """Ask one replica for the video: its job id, or a failure to try the next replica with."""
    body, ctype = videogen.form(videogen.upstream_fields(ask, g.films, g.upstream_model))
    http = proxy.client()
    try:
        r = await http.post(f"{t.url}/v1/videos", content=body, timeout=_timeout(SUBMIT_TIMEOUT),
                            headers=_headers(t.secret, **{"Content-Type": ctype, "Accept": "application/json"}))
    except proxy.RETRYABLE as e:
        balancer.failed(t.uid)
        log.warning("Replica %s of %s did not take the video (%s); trying the next one.", t.ordinal, g.name,
                    type(e).__name__)
        return GatewayError(502, "upstream_unreachable", f"No replica of {g.name!r} could be reached "
                                                         f"({type(e).__name__}).")
    except httpx.TimeoutException:
        raise GatewayError(504, "upstream_timeout", f"The replica did not take the job within {SUBMIT_TIMEOUT:g} s.") \
            from None
    except httpx.HTTPError as e:
        raise GatewayError(502, "upstream_error", f"The request to the replica failed ({type(e).__name__}).") from None
    status = r.status_code
    if status in (401, 403):
        log.warning("Replica %s of %s refused the gateway's internal key (HTTP %s).", t.ordinal, g.name, status)
        raise GatewayError(502, "replica_refused_key", "The replica refused the gateway's internal key. Stop and start "
                                                       "the deployment to give it a new one.")
    if status in (429, 503):
        log.info("Replica %s of %s answered %s; trying the next one.", t.ordinal, g.name, status)
        return Answer(Response(content=imagegen.engine_error(status, r.content), status_code=status,
                               media_type="application/json", headers=proxy.upstream_retry_after(r)))
    if status >= 400:
        raise Answer(Response(content=imagegen.engine_error(status, r.content), status_code=status,
                              media_type="application/json"))
    uid = videogen.engine_id(r.content)
    if uid is None:
        raise GatewayError(502, "upstream_error", "The replica's answer holds no job id.")
    return uid


async def submit(request: Request, caller: KeyCaller) -> Response:
    """POST /v1/videos (and the Playground's): a job, or why not."""
    started = time.monotonic()
    model: Optional[str] = None
    deployment_id: Optional[int] = None
    limited = False
    try:
        body = await proxy.read_body(request)
        obj = videogen.read_body(body, request.headers.get("content-type", ""))
        raw = obj.get("model")
        if not isinstance(raw, str) or not raw.strip():
            raise GatewayError(400, "invalid_request", "The request needs a 'model'.", param="model")
        model = raw.strip()
        route = proxy.check_access(caller, await run_in_threadpool(_route, model), model, videogen.ENDPOINT)
        deployment_id = route.deployment_id
        ask = videogen.check(obj)
        groups = [g for tier in route.tiers for g in tier.locals if g.films is not None]
        if not groups:
            raise GatewayError(503, "no_target", f"{model!r} has no video deployment that can take this request.")
        why = {g.deployment_id: videogen.takes(g.films, ask) for g in groups}
        fits = [g for g in groups if why[g.deployment_id] is None]
        if not fits:
            code = min((c for c in why.values() if c), key=videogen.ORDER.index)
            raise videogen.refusal(route.served_name, code, ask, [g.films for g in groups])
        if caller.source == "api":
            verdict = key_limiter.admit(caller.key_id, caller.rpm, caller.tpm, None)
            if isinstance(verdict, Refusal):
                limited = True
                raise GatewayError(429, "rate_limit_exceeded", f"This API key may use {verdict.limit} requests per "
                                                               f"minute; try again in {verdict.retry_after} s.",
                                   retry_after=verdict.retry_after)
            key_limiter.settle(verdict, 0)
        job, target = await _place(caller, route, fits, ask)
    except Answer as a:
        proxy.record(caller, model or "", deployment_id, a.response.status_code, started)
        return a.response
    except GatewayError as e:
        if model is not None:
            proxy.record(caller, model, deployment_id, e.status, started, limited=limited)
        return e.response()
    return JSONResponse(videogen.video_object(job), headers={"X-Forgeyard-Target": target})


async def _place(caller: KeyCaller, route: proxy.Route, groups: list[proxy.LocalGroup],
                 ask: videogen.Ask) -> tuple[VideoJob, str]:
    """The job, on the replica that took it, and who that is (local:<deployment>#<replica>)."""
    async with _lock():
        counts = await run_in_threadpool(_counts, [g.deployment_id for g in groups], caller)
        why: dict = {}
        tries: list[tuple[proxy.LocalGroup, Target]] = []
        for g in groups:
            f = g.films
            if not g.replicas:
                why[g.deployment_id] = "unavailable"
            elif counts.mine.get(g.deployment_id, 0) >= f.per_key:
                why[g.deployment_id] = "too_many_jobs"
            elif counts.deployment.get(g.deployment_id, 0) >= f.queue:
                why[g.deployment_id] = "queue_full"
            else:
                order = balancer.order(f"{route.served_name}#video", list(g.replicas))
                order.sort(key=lambda t: counts.replica.get(t.uid, 0))  # fewest jobs first; ties keep the order
                tries += [(g, t) for t in order]
        if not tries:
            raise _no_room(route, groups, why, counts)
        last = None
        for g, t in tries:
            got = await _send(g, t, ask)
            if isinstance(got, str):
                job = await run_in_threadpool(_insert, caller, route.served_name, g, t, ask, got)
                return job, f"local:{g.name}#{t.ordinal}"
            last = got
        if isinstance(last, Answer):
            raise last
        raise last


# ------------------------------------------------------------------ following a job
@dataclass
class Snap:
    id: int
    status: str
    upstream_id: str
    deployment_id: Optional[int]
    deadline: object
    where: Where


def _snapshot(job_id: int) -> Optional[Snap]:
    with _db() as db:
        job = db.get(VideoJob, job_id)
        if job is None:
            return None
        return Snap(job.id, job.status, job.upstream_id, job.deployment_id, as_utc(job.deadline_at),
                    where(db, job.replica_uid, job.replica_epoch))


def _finish(job_id: int, status: str, *, code: Optional[str] = None, message: Optional[str] = None,
            seen: Optional[videogen.Seen] = None) -> None:
    """A job at work ends: completed (with what the engine says it made) or failed (code, message)."""
    entry = None
    with _db() as db:
        job = db.get(VideoJob, job_id)
        if job is None or job.status not in videogen.ACTIVE:
            return
        now = utcnow()
        job.status = status
        job.completed_at = now
        job.expires_at = now + timedelta(seconds=job.keep_s)
        if status == "completed":
            job.progress = 100
            job.duration_ms = (seen.duration_ms if seen and seen.duration_ms else
                               int(round(job.frames * 1000 / max(1, job.fps))))
            began = as_utc(job.started_at)
            busy = int((now - began).total_seconds() * 1000) if began else (seen.inference_ms if seen else None)
            busy = max(0, busy or 0) * max(1, job.gpus)
            note_took(job.deployment_id, (now - as_utc(job.created_at)).total_seconds())
            entry = _count(db, job, 200, {"media_ms": job.duration_ms, "busy_ms": busy})
        else:
            job.error_code = code or "generation_failed"
            job.error = (message or videogen.ERROR_MESSAGES.get(job.error_code) or "The video failed.")[:400]
            entry = _count(db, job, STATUS_FOR.get(job.error_code, 500))
        db.commit()
    _record(entry)


def _apply(job_id: int, seen: videogen.Seen) -> None:
    if seen.status == "completed":
        return _finish(job_id, "completed", seen=seen)
    if seen.status == "failed":
        return _finish(job_id, "failed", code="generation_failed",
                       message=f"The engine could not make the video: {seen.error or 'no reason given'}")
    with _db() as db:
        job = db.get(VideoJob, job_id)
        if job is None or job.status not in videogen.ACTIVE:
            return
        if seen.status == "in_progress" and job.started_at is None:
            job.started_at = utcnow()
        job.status = seen.status
        job.progress = seen.progress
        db.commit()


async def _abort(snap: Snap) -> None:
    w = snap.where
    if w.url is None or w.gone:
        return
    with contextlib.suppress(httpx.HTTPError):
        await proxy.client().delete(f"{w.url}/v1/videos/{snap.upstream_id}", headers=_headers(w.secret),
                                    timeout=_timeout(DELETE_TIMEOUT))


async def refresh(job_id: int) -> None:
    """Ask the replica about a job at work and record what it says (or that it is lost)."""
    if job_id in _refreshing:
        return
    _refreshing.add(job_id)
    try:
        snap = await run_in_threadpool(_snapshot, job_id)
        if snap is None or snap.status not in videogen.ACTIVE:
            return
        w = snap.where
        if w.gone:
            await run_in_threadpool(lambda: _finish(job_id, "failed", code=w.gone))
            return
        if utcnow() > snap.deadline:
            await _abort(snap)
            await run_in_threadpool(lambda: _finish(job_id, "failed", code="timed_out"))
            return
        if w.url is None:
            return
        try:
            r = await proxy.client().get(f"{w.url}/v1/videos/{snap.upstream_id}", headers=_headers(w.secret),
                                         timeout=_timeout(STATUS_TIMEOUT))
        except httpx.HTTPError as e:
            log.info("Video job %s: its replica did not answer (%s); asking again later.", job_id, type(e).__name__)
            return
        if r.status_code == 404:
            await run_in_threadpool(lambda: _finish(job_id, "failed", code="video_lost"))
            return
        if r.status_code in (401, 403):
            log.warning("Video job %s: its replica refused the gateway's internal key (HTTP %s).", job_id, r.status_code)
            return
        seen = videogen.read_job(r.status_code, r.content)
        if seen is not None:
            await run_in_threadpool(_apply, job_id, seen)
    finally:
        _refreshing.discard(job_id)
        _fresh[job_id] = time.monotonic()


# ------------------------------------------------------------------ what clients ask about their jobs
def _own(caller: KeyCaller, public_id: str) -> VideoJob:
    if not videogen.ID_RE.match(public_id or ""):
        raise _not_found(public_id or "")
    with _db() as db:
        job = db.scalar(select(VideoJob).where(VideoJob.public_id == public_id, *_owner(caller)))
        if job is None:
            raise _not_found(public_id)
        return job


async def retrieve(public_id: str, caller: KeyCaller) -> dict:
    job = await run_in_threadpool(_own, caller, public_id)
    if job.status in videogen.ACTIVE and time.monotonic() - _fresh.get(job.id, 0.0) >= FRESH_SECONDS:
        await refresh(job.id)
        job = await run_in_threadpool(_own, caller, public_id)
    return videogen.video_object(job)


def _list(caller: KeyCaller, limit: int, after: Optional[str], order: str) -> dict:
    with _db() as db:
        q = select(VideoJob).where(*_owner(caller))
        if after:
            cursor = db.scalar(select(VideoJob.id).where(VideoJob.public_id == after, *_owner(caller)))
            if cursor is None:
                raise GatewayError(400, "invalid_request", "after: not one of your videos.", param="after")
            q = q.where(VideoJob.id < cursor) if order == "desc" else q.where(VideoJob.id > cursor)
        q = q.order_by(VideoJob.id.desc() if order == "desc" else VideoJob.id.asc()).limit(limit + 1)
        rows = db.scalars(q).all()
    data = [videogen.video_object(j) for j in rows[:limit]]
    return {"object": "list", "data": data, "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None, "has_more": len(rows) > limit}


async def listing(caller: KeyCaller, limit: Optional[str], after: Optional[str], order: Optional[str]) -> dict:
    try:
        n = int(limit) if limit not in (None, "") else LIST_DEFAULT
    except ValueError:
        n = -1
    if not 1 <= n <= LIST_MAX:
        raise GatewayError(400, "invalid_request", f"limit: 1 to {LIST_MAX}.", param="limit")
    order = (order or "desc").lower()
    if order not in ("asc", "desc"):
        raise GatewayError(400, "invalid_request", "order: asc or desc.", param="order")
    return await run_in_threadpool(_list, caller, n, after or None, order)


def _forget_row(job_id: int, count_as: Optional[int]) -> None:
    entry = None
    with _db() as db:
        job = db.get(VideoJob, job_id)
        if job is None:
            return
        if count_as is not None:
            job.completed_at = utcnow()
            entry = _count(db, job, count_as)
        db.execute(sql_delete(VideoJob).where(VideoJob.id == job_id))
        db.commit()
    _record(entry)


async def remove(public_id: str, caller: KeyCaller) -> dict:
    """DELETE /v1/videos/{id}: cancelled or its file deleted on the replica, and gone here."""
    job = await run_in_threadpool(_own, caller, public_id)
    if not job.removed:
        snap = await run_in_threadpool(_snapshot, job.id)
        if snap is not None and snap.where.url and not snap.where.gone:
            for attempt in range(2):
                try:
                    r = await proxy.client().delete(f"{snap.where.url}/v1/videos/{job.upstream_id}",
                                                    headers=_headers(snap.where.secret), timeout=_timeout(DELETE_TIMEOUT))
                except httpx.HTTPError:
                    break
                if r.status_code != 409 or attempt:
                    break
                await asyncio.sleep(1.0)  # a cancel in progress, or a file being saved: once more
    await run_in_threadpool(_forget_row, job.id, 499 if job.status in videogen.ACTIVE else None)
    _fresh.pop(job.id, None)
    return {"id": job.public_id, "object": "video.deleted", "deleted": True}


def _lose(job_id: int, code: str) -> None:
    with _db() as db:
        db.execute(update(VideoJob).where(VideoJob.id == job_id).values(removed=True, error_code=code))
        db.commit()


GONE_CONTENT = {
    "video_expired": "This video expired: finished videos are kept for a while (the deployment's keep_minutes), then "
                     "deleted. Ask for it again.",
    "video_lost": "This video is gone: the replica that made it restarted or was stopped, and its files went with it. "
                  "Ask for it again.",
}


async def content(public_id: str, caller: KeyCaller, request: Request) -> Response:
    """GET /v1/videos/{id}/content: the MP4, streamed from the replica that made it."""
    variant = request.query_params.get("variant") or "video"
    if variant not in videogen.VARIANTS:
        raise GatewayError(400, "invalid_request", "variant: video (thumbnail and spritesheet are OpenAI's, not "
                                                   "made here).", param="variant")
    job = await run_in_threadpool(_own, caller, public_id)
    if variant != "video":
        raise GatewayError(400, "variant_not_supported", f"The engine makes the video only, no {variant}: ask for "
                                                         "variant=video.", param="variant")
    if job.status in videogen.ACTIVE:
        raise GatewayError(409, "video_not_ready", f"The video is not made yet (it is {job.status.replace('_', ' ')}). "
                                                   "Poll GET /v1/videos/{id} until it is completed.")
    if job.status == "failed":
        raise GatewayError(409, "video_failed", f"The video failed, so there is nothing to fetch: {job.error}")
    expired = job.expires_at is not None and as_utc(job.expires_at) <= utcnow()
    if job.removed or expired:
        code = "video_lost" if job.removed and job.error_code == "video_lost" else "video_expired"
        raise GatewayError(410, code, GONE_CONTENT[code])
    snap = await run_in_threadpool(_snapshot, job.id)
    w = snap.where if snap else Where(None, None, "", "replica_gone")
    if w.gone:
        await run_in_threadpool(_lose, job.id, "video_lost")
        raise GatewayError(410, "video_lost", GONE_CONTENT["video_lost"])
    if w.url is None:
        raise GatewayError(503, "no_ready_replica", "The replica that holds this video has no address right now. Try "
                                                    "again shortly.")
    headers = _headers(w.secret, Accept="video/mp4")
    for name in ("range", "if-range"):
        if request.headers.get(name):
            headers[name.title()] = request.headers[name]
    http = proxy.client()
    try:
        upstream = await http.send(http.build_request("GET", f"{w.url}/v1/videos/{job.upstream_id}/content",
                                                      headers=headers, timeout=_timeout(get_settings().gateway_read_timeout)),
                                   stream=True)
    except httpx.HTTPError as e:
        raise GatewayError(502, "upstream_unreachable", f"The replica that holds this video could not be reached "
                                                        f"({type(e).__name__}). Try again shortly.") from None
    if upstream.status_code not in (200, 206):
        data = b""
        with contextlib.suppress(httpx.HTTPError):
            data = await upstream.aread()
        await upstream.aclose()
        if upstream.status_code == 404:
            await run_in_threadpool(_lose, job.id, "video_lost")
            raise GatewayError(410, "video_lost", GONE_CONTENT["video_lost"])
        if upstream.status_code == 416:
            return Response(status_code=416, headers={k: v for k, v in upstream.headers.items()
                                                      if k.lower() == "content-range"})
        return Response(content=imagegen.engine_error(upstream.status_code, data), status_code=upstream.status_code,
                        media_type="application/json")
    out = {"Content-Disposition": f'inline; filename="{job.public_id}.mp4"', "Cache-Control": "private, no-store",
           "Accept-Ranges": "bytes"}
    for name in ("content-length", "content-range", "etag", "last-modified"):
        if upstream.headers.get(name):
            out[name.title()] = upstream.headers[name]

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, media_type="video/mp4", headers=out)


# ------------------------------------------------------------------ the sweeper
def _active_ids() -> list[int]:
    with _db() as db:
        return list(db.scalars(select(VideoJob.id).where(VideoJob.status.in_(videogen.ACTIVE))).all())


def _expired() -> list[int]:
    with _db() as db:
        return list(db.scalars(select(VideoJob.id).where(VideoJob.removed.is_(False), VideoJob.expires_at.is_not(None),
                                                         VideoJob.expires_at <= utcnow())).all())


def _purge() -> int:
    with _db() as db:
        n = db.execute(sql_delete(VideoJob).where(VideoJob.expires_at.is_not(None),
                                                  VideoJob.expires_at <= utcnow() - timedelta(days=ROW_DAYS))).rowcount
        db.commit()
        return n


async def expire(job_id: int) -> None:
    """A result past its time: deleted on the replica (when it still holds it), marked gone here."""
    snap = await run_in_threadpool(_snapshot, job_id)
    if snap is None:
        return
    if snap.where.url and not snap.where.gone:
        with contextlib.suppress(httpx.HTTPError):
            await proxy.client().delete(f"{snap.where.url}/v1/videos/{snap.upstream_id}",
                                        headers=_headers(snap.where.secret), timeout=_timeout(DELETE_TIMEOUT))

    def mark() -> None:
        with _db() as db:
            db.execute(update(VideoJob).where(VideoJob.id == job_id, VideoJob.removed.is_(False)).values(removed=True))
            db.commit()

    await run_in_threadpool(mark)


async def sweep() -> None:
    """One pass: every job at work asked about, every result past its time deleted, old rows removed."""
    ids = await run_in_threadpool(_active_ids)
    gate = asyncio.Semaphore(16)

    async def one(i: int) -> None:
        async with gate:
            await refresh(i)

    await asyncio.gather(*(one(i) for i in ids))
    for i in await run_in_threadpool(_expired):
        await expire(i)
    await run_in_threadpool(_purge)


async def run_forever(interval: float = POLL_SECONDS) -> None:
    while True:
        try:
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("The video job sweeper failed; trying again")
        await asyncio.sleep(interval)
