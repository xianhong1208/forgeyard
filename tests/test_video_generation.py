"""Video generation (1.3): /v1/videos through the gateway as asynchronous jobs on vLLM-Omni, checked against
a fake engine (tests/video_helpers.py): the request checks (image-to-video refused), each deployment's caps
(length, size, steps, prompt; jobs per deployment and per key, 429 with Retry-After), only known fields
reaching the engine, the job kept on its replica (affinity), a replica that restarts or goes failing its
jobs, a job past its time, expiry, the content streamed (Range), deleting, the keys' isolation, usage
(media_ms, busy_ms, one request per job), the migration; the recipe, its rendering and estimate, the
inspector, the recommended list, a deployment's Try it and the Playground's routes (FY-VID-*)."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import inspect as sql_inspect, select, text, update

from forgeyard.db import Replica, VideoJob, Workload, get_engine, new_session, utcnow
from forgeyard.gateway import videogen, videojobs
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.usage import recorder
from forgeyard.migrate import alembic_config, upgrade
from forgeyard.models import inspect as inspector, recipes, recommended as rec
from forgeyard.models.estimate import GIB, estimate, frames_for, video_shape
from tests.gateway_helpers import api_client, make_ready, new_key
from tests.model_helpers import add_catalog
from tests.test_scheduler import MODELS, deploy
from tests.video_helpers import FakeVideos, tiny_mp4
from tests.workload_helpers import make_node

FEATURES = MODELS + ("replica_keys", "replica_guard", "engine_zip")
WAN_REV = "0fad780a534b6463e45facd96134c9f345acfa5b"


def wan_meta(**over) -> dict:
    """Wan-AI/Wan2.1-T2V-1.3B-Diffusers as the inspector reads it (every component stored in float32)."""
    meta = {"kind": "hf", "repo": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers", "ref": "main", "revision": WAN_REV,
            "format": "diffusers", "license": "apache-2.0", "modalities": [], "task_hint": "video",
            "diffusers": {"pipeline": "WanPipeline", "components": {
                "text_encoder": {"class": "UMT5EncoderModel", "params": 5_680_917_936, "bytes": 22_723_671_744,
                                 "dtype": "F32"},
                "transformer": {"class": "WanTransformer3DModel", "params": 1_419_017_662, "bytes": 5_676_070_648,
                                "dtype": "F32"},
                "vae": {"class": "AutoencoderKLWan", "params": 126_897_973, "bytes": 507_591_892, "dtype": "F32"}}},
            "video": {"size": [832, 480], "multiple": 16, "fps": 16},
            "files": [{"path": "model_index.json", "size": 400, "oid": "a" * 40}],
            "size_bytes": 28_930_000_000}
    meta.update(over)
    return meta


def jobs() -> list[VideoJob]:
    db = new_session()
    try:
        return list(db.scalars(select(VideoJob).order_by(VideoJob.id)).all())
    finally:
        db.close()


def job(public_id: str) -> VideoJob:
    return next(j for j in jobs() if j.public_id == public_id)


def set_job(public_id: str, **values) -> None:
    db = new_session()
    try:
        db.execute(update(VideoJob).where(VideoJob.public_id == public_id).values(**values))
        db.commit()
    finally:
        db.close()


def set_replica(uid: str, **values) -> None:
    db = new_session()
    try:
        db.execute(update(Replica).where(Replica.uid == uid).values(**values))
        db.commit()
    finally:
        db.close()


def sweep(c) -> None:
    """One pass of the sweeper, on the app's own event loop."""
    c.portal.call(videojobs.sweep)


def wait_status(c, api, vid: str, want: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        sweep(c)
        got = api.get(f"/v1/videos/{vid}").json()
        if got.get("status") == want or time.monotonic() > deadline:
            return got
        time.sleep(0.05)


def err(r) -> dict:
    body = r.json()
    assert set(body) == {"error"}, body
    return body["error"]


def usage_total(owner) -> dict:
    recorder.flush()
    return owner.get("/api/usage?everyone=true").json()["total"]


def make(api, **body):
    return api.post("/v1/videos", json={"model": "wan", "prompt": "A cat rides a skateboard down a hill.", **body})


@pytest.fixture()
def vid(owner):
    """A video deployment `wan` (vllm-omni-video, Wan2.1-T2V-1.3B) on the A100's card #4 with two replicas
    on fake engines (each video 0.15 s), and a key."""
    add_catalog("wan2.1-t2v-1.3b", wan_meta(), task="video", fmt="diffusers",
                source="hf://Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
    gpu = make_node("a100", allowlist=[4], features=FEATURES)
    engines = [FakeVideos("s-0", work=0.15), FakeVideos("s-1", work=0.15)]
    deploy(owner, "wan", "wan2.1-t2v-1.3b", "vllm-omni-video", replicas=2, wait=True)
    uids = make_ready("wan", [(gpu, e.port, e.secret) for e in engines])
    key = new_key(owner, "laptop")["key"]
    videojobs.forget()
    ns = SimpleNamespace(owner=owner, gpu=gpu, engines=engines, uids=uids, key=key, api=api_client(owner, key))
    yield ns
    for e in engines:
        e.close()
    videojobs.forget()


def engine_of(v, public_id: str) -> FakeVideos:
    uid = job(public_id).replica_uid
    return v.engines[v.uids.index(uid)]


# ------------------------------------------------------------------ the recipe
def test_fy_vid_001_the_recipe_serves_video_jobs():
    """FY-VID-001: vllm-omni-video serves the video task as openai-videos from diffusers pipelines on the
    vLLM-Omni image the other omni recipes use; every cap is a parameter with a working default (the
    length and default size up front, the rest under Advanced); the engine keeps its jobs' files in the
    container's own file system with its own sweep behind the gateway's expiry; recipes that mix video
    with another task, or serve it as another API, are refused."""
    b = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
    v = b["vllm-omni-video"]
    assert v.tasks == ["video"] and v.api == {"video": "openai-videos"} and v.formats == ["diffusers"]
    assert v.estimate.method == "diffusers" and v.estimate.cast_16bit and v.estimate.activation_per_ktoken > 0
    assert v.variants[0].image == b["vllm-omni-image"].variants[0].image and v.api_key_env == "VLLM_API_KEY"
    assert v.video.request == "vllm-omni" and v.video.frames_step == 4
    assert v.env["VLLM_OMNI_SERVER_STORAGE__PATH"].startswith("/tmp/")
    assert v.env["VLLM_OMNI_SERVER_STORAGE__FILE_TTL"] == "{keep_seconds}"
    up_front = {"default_size", "seconds", "max_seconds"}
    advanced = {"max_side", "fps", "steps", "max_steps", "queue", "jobs_per_key", "keep_minutes", "timeout",
                "max_prompt_chars", "offload"}
    assert set(v.params) == up_front | advanced
    assert all(not v.params[p].advanced for p in up_front) and all(v.params[p].advanced for p in advanced)
    assert v.params["keep_minutes"].default == 1440 and v.params["jobs_per_key"].default == 2
    assert "--allowed-media-domains" in v.args
    good = recipes.builtin_recipes()["vllm-omni-video"][1]
    for bad, why in [(good.replace("api: {video: openai-videos}", "api: {video: openai-images}"), "openai-videos"),
                     (good.replace("tasks: [video]", "tasks: [video, image]").replace(
                         "api: {video: openai-videos}", "api: {video: openai-videos, image: openai-images}"),
                      "only the video task"),
                     (good.replace("tasks: [video]", "tasks: [image]").replace("api: {video: openai-videos}",
                                                                               "api: {image: openai-images}"),
                      "video task"),
                     (good.replace("method: diffusers", "method: files"), "diffusers"),
                     (good.replace("request: vllm-omni", "request: comfy"), "request")]:
        with pytest.raises(recipes.RecipeError, match=why):
            recipes.parse_recipe(bad)


# ------------------------------------------------------------------ the request
@pytest.mark.parametrize("body,code,param", [
    ({}, "invalid_request", "prompt"),
    ({"prompt": "  "}, "invalid_request", "prompt"),
    ({"prompt": 7}, "invalid_request", "prompt"),
    ({"prompt": "a cat", "input_reference": {"file": "input_reference"}}, "input_reference_not_supported",
     "input_reference"),
    ({"prompt": "a cat", "input_reference": "data:image/png;base64,AAAA"}, "input_reference_not_supported",
     "input_reference"),
    ({"prompt": "a cat", "image_reference": {"image_url": "http://10.0.0.1/x.png"}}, "input_reference_not_supported",
     "image_reference"),
    ({"prompt": "a cat", "video_reference": '{"video_url": "https://x/y.mp4"}'}, "input_reference_not_supported",
     "video_reference"),
    ({"prompt": "a cat", "seconds": 0}, "invalid_request", "seconds"),
    ({"prompt": "a cat", "seconds": "4.5"}, "invalid_request", "seconds"),
    ({"prompt": "a cat", "seconds": True}, "invalid_request", "seconds"),
    ({"prompt": "a cat", "seconds": 61}, "invalid_request", "seconds"),
    ({"prompt": "a cat", "size": "832"}, "invalid_size", "size"),
    ({"prompt": "a cat", "size": "32x32"}, "invalid_size", "size"),
    ({"prompt": "a cat", "size": "4096x480"}, "invalid_size", "size"),
    ({"prompt": "a cat", "num_inference_steps": 0}, "invalid_request", "num_inference_steps"),
    ({"prompt": "a cat", "seed": True}, "invalid_request", "seed"),
    ({"prompt": "a cat", "guidance_scale": 500}, "invalid_request", "guidance_scale"),
    ({"prompt": "a cat", "negative_prompt": 3}, "invalid_request", "negative_prompt"),
])
def test_fy_vid_002_requests_are_checked(body, code, param):
    """FY-VID-002: a missing or empty prompt, any image-to-video or video-to-video reference (an upload or
    a URL), a bad length (OpenAI's seconds is a whole number, also as a string), size, steps, seed,
    guidance or negative prompt are 400 before anything is sent."""
    with pytest.raises(GatewayError) as e:
        videogen.check(body)
    assert e.value.status == 400 and e.value.code == code and e.value.param == param


def test_fy_vid_003_what_the_engine_is_sent(vid):
    """FY-VID-003: the engine gets multipart with only the fields Forgeyard knows: the prompt, its model
    name, the size (the deployment's default when none is asked), the frames (seconds x fps rounded to
    4k + 1: 5 s at 16 fps is 81) and fps, and the extensions asked; never lora, extra_params, frame
    interpolation paths or anything else a client sends. OpenAI's seconds as a string works."""
    f = videogen.Films()
    assert frames_for(5, 16, 4) == 81 and frames_for(4, 16, 4) == 65 and frames_for(5, 24, 4) == 121
    assert frames_for(1, 16, 1) == 16
    out = videogen.upstream_fields(videogen.check({"prompt": "a fox", "seconds": "4", "seed": 7, "guidance_scale": 5,
                                                   "num_inference_steps": 30, "negative_prompt": "blur"}), f, "wan")
    assert out == {"prompt": "a fox", "model": "wan", "size": "832x480", "num_frames": "65", "fps": "16",
                   "num_inference_steps": "30", "guidance_scale": "5.0", "seed": "7", "negative_prompt": "blur"}
    r = make(vid.api, seconds="4", size="480x832", lora='{"path": "/etc/passwd"}', extra_params='{"x": 1}',
             frame_interpolation_model_path="/root", enable_frame_interpolation=True, quality="high", user="u1")
    assert r.status_code == 200, r.text
    sent = [p for e in vid.engines for p in e.posts]
    assert len(sent) == 1 and "multipart/form-data" in sent[0]["content_type"]
    assert sent[0]["fields"] == {"prompt": "A cat rides a skateboard down a hill.", "model": "wan", "size": "480x832",
                                 "num_frames": "65", "fps": "16"}
    # multipart from the client (as OpenAI's SDK sends it) is read the same way.
    r = vid.api.post("/v1/videos", data={"model": "wan", "prompt": "Rain on a window.", "seconds": "5"})
    assert r.status_code == 200, r.text
    second = [p for e in vid.engines for p in e.posts][-1]["fields"]
    assert second["num_frames"] == "81" and second["size"] == "832x480"
    r = vid.api.post("/v1/videos", data={"model": "wan", "prompt": "Rain."},
                     files={"input_reference": ("x.png", b"\x89PNG", "image/png")})
    assert r.status_code == 400 and err(r)["code"] == "input_reference_not_supported"


@pytest.mark.parametrize("body,code,param", [
    ({"seconds": 6}, "video_too_long", "seconds"),
    ({"size": "1024x256"}, "video_too_large", "size"),
    ({"size": "832x832"}, "video_too_large", "size"),
    ({"size": "800x488"}, "invalid_size", "size"),
    ({"num_inference_steps": 51}, "too_many_steps", "num_inference_steps"),
    ({"prompt": "x" * 2001}, "prompt_too_long", "prompt"),
])
def test_fy_vid_004_each_deployment_s_caps(vid, body, code, param):
    """FY-VID-004: a request over the deployment's caps (the longest video, the largest side and no more
    pixels than the default size scaled to it, sizes a multiple of 16 for Wan2.1, the most steps, the longest
    prompt) is 400 saying what it takes, before anything is sent, and counted as an error."""
    r = make(vid.api, **body)
    assert r.status_code == 400, r.text
    e = err(r)
    assert e["code"] == code and e["param"] == param and "'wan'" in e["message"]
    assert not any(e.posts for e in vid.engines)
    assert usage_total(vid.owner)["errors"] == 1


# ------------------------------------------------------------------ the job
def test_fy_vid_005_a_job_from_asked_to_made(vid):
    """FY-VID-005: POST answers at once with a job in OpenAI's shape (Forgeyard's own opaque id, never the
    engine's; status queued; progress 0; seconds as a string; no prompt kept); polling sees it in progress,
    then completed with progress 100 and expires_at the deployment's keep_minutes after; the content is the
    MP4 from the replica, as video/mp4, inline with a file name, and Range requests answer 206."""
    r = make(vid.api)
    assert r.status_code == 200, r.text
    v = r.json()
    assert set(v) == {"id", "object", "model", "status", "progress", "created_at", "completed_at", "expires_at", "size",
                      "seconds", "quality", "remixed_from_video_id", "error"}
    assert r.headers["X-Forgeyard-Target"] in ("local:wan#0", "local:wan#1")
    assert v["object"] == "video" and v["model"] == "wan" and v["status"] == "queued" and v["progress"] == 0
    assert v["seconds"] == "5" and v["size"] == "832x480" and v["expires_at"] is None and v["error"] is None
    assert videogen.ID_RE.match(v["id"]) and "video_gen" not in json.dumps(v)
    row = job(v["id"])
    assert row.upstream_id.startswith("video_gen_") and row.frames == 81 and row.fps == 16
    eng = engine_of(vid, v["id"])
    eng.hang = True
    got = wait_status(vid.owner, vid.api, v["id"], "in_progress")
    assert got["status"] == "in_progress" and job(v["id"]).started_at is not None
    eng.hang = False
    got = wait_status(vid.owner, vid.api, v["id"], "completed")
    assert got["status"] == "completed" and got["progress"] == 100 and got["completed_at"]
    assert got["expires_at"] - got["completed_at"] in (86400, 86401)
    c = vid.api.get(f"/v1/videos/{v['id']}/content")
    assert c.status_code == 200 and c.headers["content-type"] == "video/mp4"
    assert c.headers["content-disposition"] == f'inline; filename="{v["id"]}.mp4"'
    assert c.content == eng.files[row.upstream_id] and c.content[4:8] == b"ftyp"
    part = vid.api.get(f"/v1/videos/{v['id']}/content?variant=video", headers={"Range": "bytes=0-7"})
    assert part.status_code == 206 and part.content == c.content[:8]
    assert part.headers["content-range"] == f"bytes 0-7/{len(c.content)}"
    listed = vid.api.get("/v1/videos").json()
    assert listed["object"] == "list" and [x["id"] for x in listed["data"]] == [v["id"]] and not listed["has_more"]


def test_fy_vid_006_a_job_stays_on_its_replica(vid):
    """FY-VID-006: a job goes to the ready replica with the fewest jobs, and everything about it (its
    status, its content, deleting it) is asked of that replica only, never another."""
    a, b = make(vid.api).json(), make(vid.api).json()
    ea, eb = engine_of(vid, a["id"]), engine_of(vid, b["id"])
    assert ea is not eb and len(ea.posts) == len(eb.posts) == 1
    for x in (a, b):
        wait_status(vid.owner, vid.api, x["id"], "completed")
    ua = job(a["id"]).upstream_id
    assert ua in ea.files and ua not in eb.files and ua not in eb.jobs
    assert vid.api.get(f"/v1/videos/{a['id']}/content").content == ea.files[ua]
    assert vid.api.delete(f"/v1/videos/{a['id']}").status_code == 200
    assert ea.deletes == [ua] and eb.deletes == []
    # The replica with fewer jobs at work gets the next one, whatever order the balancer gives.
    for e in vid.engines:
        e.hang = True
    busy = make(vid.api).json()
    first = engine_of(vid, busy["id"])
    order = [vid.uids[vid.engines.index(first)], vid.uids[1 - vid.engines.index(first)]]
    from forgeyard.gateway.balancer import balancer

    real = balancer.order
    try:
        balancer.order = lambda key, targets: sorted(targets, key=lambda t: order.index(t.uid))
        nxt = make(vid.api).json()
    finally:
        balancer.order = real
    assert engine_of(vid, nxt["id"]) is not first


def test_fy_vid_007_a_replica_that_restarts_or_goes_fails_its_jobs(vid):
    """FY-VID-007: the engine keeps its jobs in memory, so a job whose replica restarted (the engine no
    longer knows it, or the agent counted a restart) or is gone (stopped, placed again) reports failed
    with a reason that says so; a finished video whose replica restarted answers 410 for its content."""
    a = make(vid.api).json()
    eng = engine_of(vid, a["id"])
    eng.hang = True
    wait_status(vid.owner, vid.api, a["id"], "in_progress")
    eng.restart()
    got = wait_status(vid.owner, vid.api, a["id"], "failed")
    assert got["status"] == "failed" and got["error"]["code"] == "video_lost" and "Ask for the video again" in \
        got["error"]["message"]
    eng.hang = False
    b = make(vid.api).json()
    set_replica(job(b["id"]).replica_uid, restarts=3)          # the agent restarted the engine
    posts = sum(len(e.posts) for e in vid.engines)
    got = wait_status(vid.owner, vid.api, b["id"], "failed")
    assert got["error"]["code"] == "replica_restarted" and "restarted" in got["error"]["message"]
    assert sum(len(e.posts) for e in vid.engines) == posts
    set_replica(job(b["id"]).replica_uid, restarts=0)
    vid.api.delete(f"/v1/videos/{b['id']}")
    c = make(vid.api).json()
    set_replica(job(c["id"]).replica_uid, generation=9)        # restarted from the UI: placed again
    got = wait_status(vid.owner, vid.api, c["id"], "failed")
    assert got["error"]["code"] == "replica_gone"
    set_replica(job(c["id"]).replica_uid, generation=1)
    vid.api.delete(f"/v1/videos/{c['id']}")
    d = make(vid.api).json()
    wait_status(vid.owner, vid.api, d["id"], "completed")
    engine_of(vid, d["id"]).restart()
    r = vid.api.get(f"/v1/videos/{d['id']}/content")
    assert r.status_code == 410 and err(r)["code"] == "video_lost"
    assert vid.api.get(f"/v1/videos/{d['id']}").json()["status"] == "completed"
    r = vid.api.get(f"/v1/videos/{d['id']}/content")
    assert r.status_code == 410 and err(r)["code"] == "video_lost"
    # The agent counted a restart: the content is gone without asking the engine (whose file is still there).
    e = make(vid.api).json()
    wait_status(vid.owner, vid.api, e["id"], "completed")
    assert job(e["id"]).upstream_id in engine_of(vid, e["id"]).files
    set_replica(job(e["id"]).replica_uid, restarts=7)
    r = vid.api.get(f"/v1/videos/{e['id']}/content")
    assert r.status_code == 410 and err(r)["code"] == "video_lost" and job(e["id"]).removed


def test_fy_vid_008_a_stopped_deployment_fails_its_jobs(vid):
    """FY-VID-008: stopping the deployment takes its replicas, and their jobs report failed (replica_gone)."""
    a = make(vid.api).json()
    engine_of(vid, a["id"]).hang = True
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == "wan"))
        w.desired_state = "stopped"
        db.commit()
    finally:
        db.close()
    got = wait_status(vid.owner, vid.api, a["id"], "failed")
    assert got["error"]["code"] == "replica_gone" and "stopped" in got["error"]["message"]


def test_fy_vid_009_a_job_past_its_time_is_stopped(vid):
    """FY-VID-009: a job not done by the deployment's timeout is cancelled on its replica and failed."""
    a = make(vid.api).json()
    eng = engine_of(vid, a["id"])
    eng.hang = True
    set_job(a["id"], deadline_at=utcnow() - timedelta(seconds=1))
    got = wait_status(vid.owner, vid.api, a["id"], "failed")
    assert got["error"]["code"] == "timed_out" and job(a["id"]).upstream_id in eng.deletes


def test_fy_vid_010_results_expire(vid):
    """FY-VID-010: a result past expires_at is deleted on its replica by the sweeper and its content answers
    410; the job itself is still listed; a week later the row goes too."""
    a = make(vid.api).json()
    wait_status(vid.owner, vid.api, a["id"], "completed")
    eng, uid = engine_of(vid, a["id"]), job(a["id"]).upstream_id
    set_job(a["id"], expires_at=utcnow() - timedelta(seconds=1))
    r = vid.api.get(f"/v1/videos/{a['id']}/content")
    assert r.status_code == 410 and err(r)["code"] == "video_expired"
    sweep(vid.owner)
    assert uid in eng.deletes and uid not in eng.files and job(a["id"]).removed
    assert vid.api.get(f"/v1/videos/{a['id']}").json()["status"] == "completed"
    set_job(a["id"], expires_at=utcnow() - timedelta(days=8))
    sweep(vid.owner)
    assert jobs() == [] and vid.api.get(f"/v1/videos/{a['id']}").status_code == 404


def test_fy_vid_011_jobs_per_key_and_per_deployment(vid):
    """FY-VID-011: one key may have the deployment's jobs_per_key (2) waiting or being made at once; one more
    is 429 too_many_jobs with Retry-After, while another key still gets in; once the deployment holds its
    `queue`, everyone gets 429 queue_full. A finished job frees its place."""
    for e in vid.engines:
        e.hang = True
    a, b = make(vid.api), make(vid.api)
    assert a.status_code == b.status_code == 200
    r = make(vid.api)
    assert r.status_code == 429 and err(r)["code"] == "too_many_jobs" and int(r.headers["retry-after"]) >= 5
    other = api_client(vid.owner, new_key(vid.owner, "other")["key"])
    assert make(other).status_code == 200
    db = new_session()
    try:
        from forgeyard.db import Deployment

        d = db.scalar(select(Deployment).where(Deployment.name == "wan"))
        d.params = {**d.params, "queue": 3}
        db.commit()
    finally:
        db.close()
    third = api_client(vid.owner, new_key(vid.owner, "third")["key"])
    r = make(third)
    assert r.status_code == 429 and err(r)["code"] == "queue_full" and r.headers.get("retry-after")
    for e in vid.engines:
        e.hang = False
    wait_status(vid.owner, vid.api, a.json()["id"], "completed")
    wait_status(vid.owner, vid.api, b.json()["id"], "completed")
    assert make(vid.api).status_code == 200


def test_fy_vid_012_a_key_sees_only_its_own_jobs(vid):
    """FY-VID-012: another key's job id answers 404 the same way as an id that does not exist, for its status,
    content and delete; lists hold only the key's own; the Playground's jobs are the person's, not a key's,
    and the other way round; ids are 128 random bits."""
    a = make(vid.api).json()
    other = api_client(vid.owner, new_key(vid.owner, "other")["key"])
    missing = videogen.new_id()
    for path in (f"/v1/videos/{a['id']}", f"/v1/videos/{a['id']}/content"):
        r = other.get(path)
        assert r.status_code == 404 and err(r)["code"] == "video_not_found"
        miss = other.get(path.replace(a["id"], missing))
        assert err(miss)["message"].replace(missing, a["id"]) == err(r)["message"]
    assert other.delete(f"/v1/videos/{a['id']}").status_code == 404 and len(jobs()) == 1
    assert other.get("/v1/videos").json()["data"] == []
    assert other.get("/v1/videos/not-an-id").status_code == 404
    assert vid.owner.get(f"/api/playground/videos/{a['id']}").status_code == 404
    mine = vid.owner.post("/api/playground/videos", json={"model": "wan", "prompt": "Fog over a harbour."}).json()
    assert vid.api.get(f"/v1/videos/{mine['id']}").status_code == 404
    assert [x["id"] for x in vid.owner.get("/api/playground/videos").json()["data"]] == [mine["id"]]
    assert len({videogen.new_id() for _ in range(1000)}) == 1000 and len(videogen.new_id()) == 38


def test_fy_vid_013_deleting(vid):
    """FY-VID-013: DELETE cancels a job at work on its replica (and counts it as 499, not an error) or deletes
    a finished video's file there; the job is gone for its key afterwards."""
    a = make(vid.api).json()
    eng = engine_of(vid, a["id"])
    eng.hang = True
    uid = job(a["id"]).upstream_id
    r = vid.api.delete(f"/v1/videos/{a['id']}")
    assert r.status_code == 200 and r.json() == {"id": a["id"], "object": "video.deleted", "deleted": True}
    assert uid in eng.deletes and uid not in eng.jobs
    assert vid.api.get(f"/v1/videos/{a['id']}").status_code == 404
    eng.hang = False
    b = make(vid.api).json()
    wait_status(vid.owner, vid.api, b["id"], "completed")
    ub = job(b["id"]).upstream_id
    eb = engine_of(vid, b["id"])
    assert vid.api.delete(f"/v1/videos/{b['id']}").status_code == 200 and ub not in eb.files and jobs() == []
    t = usage_total(vid.owner)
    assert t["requests"] == 2 and t["errors"] == 0


def test_fy_vid_014_usage(vid):
    """FY-VID-014: one request per job, recorded when it ends: a finished video counts its length (the
    engine's: 81 frames at 16 fps is 5,062 ms) in media_ms and the replica's time on it in busy_ms; a failed
    one is an error and counts nothing else; a refused one is counted when refused; polls, lists and
    downloads are not requests."""
    a = make(vid.api).json()
    wait_status(vid.owner, vid.api, a["id"], "completed")
    for _ in range(3):
        vid.api.get(f"/v1/videos/{a['id']}")
        vid.api.get(f"/v1/videos/{a['id']}/content")
        vid.api.get("/v1/videos")
    t = usage_total(vid.owner)
    assert t["requests"] == 1 and t["errors"] == 0 and t["media_ms"] == 5062 and t["busy_ms"] > 0
    sweep(vid.owner)
    vid.api.get(f"/v1/videos/{a['id']}")
    assert usage_total(vid.owner)["requests"] == 1                      # counted once, whoever looked
    b = make(vid.api).json()
    engine_of(vid, b["id"]).fail_next = None
    eng = vid.engines[0] if engine_of(vid, b["id"]) is vid.engines[1] else vid.engines[1]
    eng.fail_next = "CUDA out of memory"
    c = make(vid.api).json()
    assert engine_of(vid, c["id"]) is eng
    got = wait_status(vid.owner, vid.api, c["id"], "failed")
    assert got["error"]["code"] == "generation_failed" and "CUDA out of memory" in got["error"]["message"]
    wait_status(vid.owner, vid.api, b["id"], "completed")
    make(vid.api, seconds=30)
    t = usage_total(vid.owner)
    assert t["requests"] == 4 and t["errors"] == 2 and t["media_ms"] == 2 * 5062
    rows = vid.owner.get("/api/usage?everyone=true&group=model").json()["rows"]
    assert [r["id"] for r in rows] == ["wan"] and rows[0]["busy_ms"] == t["busy_ms"]


def test_fy_vid_015_the_engine_s_own_answers(vid):
    """FY-VID-015: a replica that answers 503 (or cannot be reached) is passed over for the next; when none
    takes it, the engine's answer is passed on in OpenAI's error shape; a replica refusing the gateway's key
    is 502 replica_refused_key."""
    vid.engines[0].status = 503
    a = make(vid.api)
    assert a.status_code == 200 and engine_of(vid, a.json()["id"]) is vid.engines[1]
    vid.engines[1].status = 503
    r = make(vid.api)
    assert r.status_code == 503 and err(r)["code"] == "engine_error" and "503" in err(r)["message"]
    vid.engines[0].status = vid.engines[1].status = None
    vid.engines[0].close()
    for _ in range(3):
        assert engine_of(vid, make(vid.api, prompt="x").json()["id"]) is vid.engines[1]
        vid.api.delete(f"/v1/videos/{jobs()[-1].public_id}")
    vid.engines[1].secret = "something-else"
    r = make(vid.api)
    assert r.status_code == 502 and err(r)["code"] == "replica_refused_key"


def test_fy_vid_016_keys_limits_and_endpoints(vid):
    """FY-VID-016: the key's rpm counts the jobs asked for (polls are not requests); a key whose allowlist
    leaves the model out gets 403; a video model at another endpoint, or another model at /v1/videos, is
    400 wrong_endpoint; /v1/models lists the video model; remix and vLLM-Omni's sync route are not
    offered; thumbnails are not made; a video not made yet, or failed, has no content (409)."""
    limited = api_client(vid.owner, new_key(vid.owner, "slow", rpm=1)["key"])
    for e in vid.engines:
        e.hang = True
    a = make(limited)
    assert a.status_code == 200
    r = make(limited)
    assert r.status_code == 429 and err(r)["code"] == "rate_limit_exceeded"
    for _ in range(5):
        assert limited.get(f"/v1/videos/{a.json()['id']}").status_code == 200
    assert limited.get(f"/v1/videos/{a.json()['id']}/content").status_code == 409
    allow = new_key(vid.owner, "chat-only")
    db = new_session()
    try:
        from forgeyard.db import ApiKey

        db.execute(update(ApiKey).where(ApiKey.public_id == allow["api_key"]["id"]).values(models=["other"]))
        db.commit()
    finally:
        db.close()
    assert make(api_client(vid.owner, allow["key"])).status_code == 403
    r = vid.api.post("/v1/chat/completions", json={"model": "wan", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400 and err(r)["code"] == "wrong_endpoint" and "/v1/videos" in err(r)["message"]
    assert make(vid.api, model="nope").status_code == 404
    assert "wan" in [m["id"] for m in vid.api.get("/v1/models").json()["data"]]
    for path in ("/v1/videos/sync", f"/v1/videos/{a.json()['id']}/remix"):
        r = vid.api.post(path, json={"model": "wan", "prompt": "x"})
        assert r.status_code == 404 and err(r)["code"] == "endpoint_not_supported"
    for e in vid.engines:
        e.hang = False
    b = make(vid.api).json()
    wait_status(vid.owner, vid.api, b["id"], "completed")
    r = vid.api.get(f"/v1/videos/{b['id']}/content?variant=thumbnail")
    assert r.status_code == 400 and err(r)["code"] == "variant_not_supported"
    assert vid.api.get(f"/v1/videos/{b['id']}/content?variant=gif").status_code == 400
    assert vid.api.post("/v1/videos", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert vid.api.get("/v1/videos?limit=0").status_code == 400
    assert vid.api.get("/v1/videos?order=sideways").status_code == 400
    assert api_client(vid.owner).post("/v1/videos", json={"model": "wan", "prompt": "x"}).status_code == 401


def test_fy_vid_017_lists_page(vid):
    """FY-VID-017: GET /v1/videos lists a key's jobs newest first (or oldest with order=asc), `limit` at a
    time, `after` the last id seen, with has_more."""
    ids = []
    for i in range(3):
        ids.append(make(vid.api, prompt=f"clip {i}").json()["id"])
        if i < 2:
            wait_status(vid.owner, vid.api, ids[-1], "completed")
    page = vid.api.get("/v1/videos?limit=2").json()
    assert [x["id"] for x in page["data"]] == [ids[2], ids[1]] and page["has_more"] and page["last_id"] == ids[1]
    rest = vid.api.get(f"/v1/videos?limit=2&after={ids[1]}").json()
    assert [x["id"] for x in rest["data"]] == [ids[0]] and not rest["has_more"]
    assert [x["id"] for x in vid.api.get("/v1/videos?order=asc").json()["data"]] == ids
    assert vid.api.get(f"/v1/videos?after={videogen.new_id()}").status_code == 400


def test_fy_vid_018_jobs_outlive_a_server_restart(vid):
    """FY-VID-018: the jobs are in the database and the replicas keep working, so a server that restarted
    (nothing in memory) picks them up again and finishes them."""
    a = make(vid.api).json()
    videojobs.forget()
    got = wait_status(vid.owner, vid.api, a["id"], "completed")
    assert got["status"] == "completed" and usage_total(vid.owner)["requests"] == 1


def test_fy_vid_019_a_poll_asks_the_replica_at_most_every_two_seconds(vid, monkeypatch):
    """FY-VID-019: a client polling a job at work as fast as it likes reaches its replica at most every
    FRESH_SECONDS; the sweeper asks too."""
    for e in vid.engines:
        e.hang = True
    a = make(vid.api).json()
    asked = []
    real = videojobs.refresh

    async def counting(job_id):
        asked.append(job_id)
        await real(job_id)

    monkeypatch.setattr(videojobs, "refresh", counting)
    vid.api.get(f"/v1/videos/{a['id']}")
    for _ in range(10):
        vid.api.get(f"/v1/videos/{a['id']}")
    assert len(asked) == 1
    monkeypatch.setattr(videojobs, "FRESH_SECONDS", 0.0)
    vid.api.get(f"/v1/videos/{a['id']}")
    assert len(asked) == 2


# ------------------------------------------------------------------ the console
def test_fy_vid_020_try_it_and_the_playground(vid):
    """FY-VID-020: a deployment's Try it asks a replica's video API (with its key) for its jobs instead of
    making a video; the Playground reads the model's options and makes, follows, plays and deletes videos as
    the signed-in person (cookie and the console's CSRF header), counted as console."""
    dep = next(d for d in vid.owner.get("/api/deployments").json()["deployments"] if d["name"] == "wan")
    r = vid.owner.post(f"/api/deployments/{dep['id']}/try", json={})
    assert r.status_code == 200 and r.json()["ok"] and "video API answers" in r.json()["summary"]
    o = vid.owner.get("/api/playground/videos/options?model=wan").json()
    assert o == {"model": "wan", "default_size": "832x480", "max_side": 832, "max_pixels": 832 * 480, "multiple": 16,
                 "seconds": 5, "max_seconds": 5, "fps": 16, "max_steps": 50, "max_prompt_chars": 2000,
                 "keep_minutes": 1440}
    assert vid.owner.get("/api/playground/videos/options?model=nope").status_code == 404
    v = vid.owner.post("/api/playground/videos", json={"model": "wan", "prompt": "A paper boat.", "seconds": 3}).json()
    assert v["seconds"] == "3"
    got = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        sweep(vid.owner)
        got = vid.owner.get(f"/api/playground/videos/{v['id']}").json()
        if got["status"] == "completed":
            break
        time.sleep(0.05)
    assert got["status"] == "completed"
    c = vid.owner.get(f"/api/playground/videos/{v['id']}/content")
    assert c.status_code == 200 and c.content[4:8] == b"ftyp"
    bare = api_client(vid.owner)
    bare.cookies = vid.owner.cookies
    r = bare.post("/api/playground/videos", json={"model": "wan", "prompt": "x"})
    assert r.status_code == 403
    assert vid.owner.delete(f"/api/playground/videos/{v['id']}").json()["deleted"]
    recorder.flush()
    rows = vid.owner.get("/api/usage?everyone=true&group=key").json()["rows"]
    assert any(x["id"] == "console" and x["media_ms"] == 3062 for x in rows), rows


# ------------------------------------------------------------------ memory, rendering, catalog
def test_fy_vid_021_the_estimate_is_sized_for_the_longest_largest_video():
    """FY-VID-021: the estimate counts the pipeline's weights in 16 bits (as vLLM-Omni holds them), the
    activations for the longest video at the largest size (tokens = latent frames x (W/16) x (H/16): 32,760
    for 5 s of 832x480), one frame's VAE decode, the overhead and the CUDA context; longer or larger videos
    raise it; model offload keeps only the largest component on the card."""
    recipe = recipes.builtin_recipes()["vllm-omni-video"][0]
    params = {k: p.default for k, p in recipe.params.items()} | {"gpus": 1}
    est = estimate(wan_meta(), recipe, params, device="gpu", gpu_context=400 * 1024**2, task="video")
    parts = {p.label: p for p in est.parts}
    weights = sum(p.bytes for label, p in parts.items() if "(" in label)
    assert weights == 2 * (5_680_917_936 + 1_419_017_662 + 126_897_973)
    assert "32,760 tokens" in parts["Video activations"].detail and "81 frames" in parts["Video activations"].detail
    assert parts["Video activations"].bytes == int(recipe.estimate.activation_per_ktoken * 32760 / 1000)
    assert 18 * GIB < est.per_gpu < 26 * GIB
    longer = estimate(wan_meta(), recipe, params | {"max_seconds": 10}, device="gpu", gpu_context=400 * 1024**2)
    larger = estimate(wan_meta(), recipe, params | {"max_side": 1280}, device="gpu", gpu_context=400 * 1024**2)
    off = estimate(wan_meta(), recipe, params | {"offload": "model"}, device="gpu", gpu_context=400 * 1024**2)
    assert longer.per_gpu > est.per_gpu and larger.per_gpu > est.per_gpu and off.per_gpu < est.per_gpu - 2 * GIB
    vs = video_shape(wan_meta(), params, 4)
    assert (vs.width, vs.height, vs.max_w, vs.max_h, vs.frames, vs.latent_frames, vs.tokens) == \
        (832, 480, 832, 480, 81, 21, 32760)
    five = video_shape({"video": {"size": [1280, 704], "multiple": 32, "fps": 24}}, params, 4)
    assert (five.frames, five.latent_frames, five.tokens) == (121, 31, 31 * 40 * 22)
    small = video_shape(wan_meta(), params | {"max_side": 640, "default_size": "480x832"}, 4)
    assert (small.max_w, small.max_h) == (368, 640) and (small.width, small.height) == (368, 640)


def test_fy_vid_022_rendered_replica(vid):
    """FY-VID-022: the replica runs `vllm serve <model> --omni` with its served name, media URLs disallowed,
    its videos in the container's own /tmp (gone with the container) and the engine's own sweep set to the
    deployment's keep_minutes and an hour; the internal key is set per replica."""
    db = new_session()
    try:
        r = db.scalar(select(Replica).join(Workload).where(Workload.name == "wan"))
        spec = r.spec
    finally:
        db.close()
    assert spec["runtime"]["image"].startswith("vllm/vllm-omni:v0.32.0@sha256:")
    assert spec["runtime"]["args"][:2] == ["${MODEL_PATH}", "--omni"] and "wan" in spec["runtime"]["args"]
    assert spec["env"]["VLLM_OMNI_SERVER_STORAGE__FILE_TTL"] == str(1440 * 60 + 3600)
    assert spec["env"]["VLLM_OMNI_SERVER_STORAGE__PATH"] == "/tmp/forgeyard-videos"
    assert "VLLM_API_KEY" not in spec["env"]


def test_fy_vid_023_the_inspector_reads_video_pipelines(monkeypatch):
    """FY-VID-023: a diffusers repository whose model_index.json names WanPipeline is a video model: its
    task hint is video and it carries its own size, frame rate and size multiple (Wan2.1: 832x480 at 16
    fps, multiples of 16; Wan2.2-TI2V-5B, whose VAE packs 16 x 16 pixels: 1280x704 at 24 fps, multiples of
    32); image-to-video pipelines are not video models here."""
    assert inspector.is_video_pipeline("WanPipeline") and inspector.is_video_pipeline("HunyuanVideoPipeline")
    assert not inspector.is_video_pipeline("WanImageToVideoPipeline")
    assert not inspector.is_video_pipeline("ZImagePipeline") and not inspector.is_video_pipeline(None)
    configs = {"vae/config.json": {"_class_name": "AutoencoderKLWan", "z_dim": 16},
               "transformer/config.json": {"patch_size": [1, 2, 2]}}

    def fake_get(url, policy, headers=None, limit=None):
        return json.dumps(next(v for k, v in configs.items() if url.endswith(k))).encode()

    monkeypatch.setattr(inspector.fetch, "get_bytes", fake_get)
    assert inspector._video_native({}, lambda p: f"https://hf/{p}", None, None) == \
        {"size": [832, 480], "multiple": 16, "fps": 16}
    configs["vae/config.json"] = {"scale_factor_spatial": 16, "z_dim": 48}
    assert inspector._video_native({"expand_timesteps": True}, lambda p: f"https://hf/{p}", None, None) == \
        {"size": [1280, 704], "multiple": 32, "fps": 24}


def test_fy_vid_024_the_recommended_video_models():
    """FY-VID-024: the recommended list holds Wan2.1-T2V-1.3B pinned to the commit being downloaded for the
    acceptance, and Wan2.2-TI2V-5B, both Apache-2.0 and for vllm-omni-video, in their own group; a video entry
    must be pinned to a commit."""
    r = rec.recommended()
    video = {e.name: e for e in r.models if e.task == "video"}
    assert set(video) == {"wan2.1-t2v-1.3b", "wan2.2-ti2v-5b"}
    assert video["wan2.1-t2v-1.3b"].revision == WAN_REV
    assert all(e.license == "apache-2.0" and e.recipe == "vllm-omni-video" and e.group == "video" and
               e.format == "diffusers" for e in video.values())
    raw = rec.FILE.read_text()
    with pytest.raises(rec.RecommendedError, match="video entry is pinned"):
        import yaml

        rec.check(yaml.safe_load(raw.replace(f"revision: {WAN_REV}", "revision: main")))


def test_fy_vid_025_the_catalog_takes_video_models(owner):
    """FY-VID-025: a video model is listed with its task label, its own size and frame rate, and only the
    video recipe; a repository that is not a video pipeline cannot be added as one."""
    add_catalog("wan2.1-t2v-1.3b", wan_meta(), task="video", fmt="diffusers", source="hf://Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
    m = next(x for x in owner.get("/api/models").json()["models"] if x["name"] == "wan2.1-t2v-1.3b")
    assert m["task_label"] == "Video" and m["recipes"] == ["vllm-omni-video"]
    assert m["video"]["size"] == [832, 480] and m["video"]["fps"] == 16 and m["video"]["pipeline"] == "WanPipeline"


# ------------------------------------------------------------------ the migration
def test_fy_vid_026_the_migration_adds_only_the_jobs_table(owner):
    """FY-VID-026: 0014 adds video_jobs and nothing else: down to 0013 drops it and leaves every other table and
    its rows as they were; up again brings it back empty (on SQLite and PostgreSQL)."""
    owner.post("/api/keys", json={"name": "k"})

    def tables() -> set:
        with get_engine().connect() as conn:
            return set(sql_inspect(conn).get_table_names())

    def keys() -> int:
        with get_engine().connect() as conn:
            return conn.execute(text("SELECT count(*) FROM api_keys")).scalar()

    before, n = tables(), keys()
    assert "video_jobs" in before
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0013_passthrough")
    assert tables() == before - {"video_jobs"} and keys() == n
    upgrade()
    assert tables() == before and keys() == n
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM video_jobs")).scalar() == 0
        cols = {c["name"] for c in sql_inspect(conn).get_columns("video_jobs")}
    assert {"public_id", "key_id", "user_id", "deployment_id", "replica_uid", "replica_epoch", "upstream_id",
            "status", "size", "seconds", "created_at", "expires_at", "error"} <= cols
    assert tiny_mp4()[4:8] == b"ftyp"


def test_fy_vid_028_a_job_is_counted_once(vid):
    """FY-VID-028: whoever records a job's end first (the sweeper, a client's poll, a delete) counts it; any
    other attempt counts nothing."""
    a = make(vid.api).json()
    wait_status(vid.owner, vid.api, a["id"], "completed")
    set_job(a["id"], counted=False)
    db = new_session()
    try:
        row = db.scalar(select(VideoJob).where(VideoJob.public_id == a["id"]))
        first, second = videojobs._count(db, row, 200), videojobs._count(db, row, 200)
        db.commit()
    finally:
        db.close()
    assert first is not None and first["status"] == 200 and second is None


def test_fy_vid_027_engine_answers_and_mp4_files_are_read():
    """FY-VID-027: the engine's job record is read in its own shape (a failed job answers its error's status with
    the job as the body; anything else that is not a job is none); its job ids are checked before use; and
    scripts/accept_video.py reads an MP4's boxes (a real H.264 file made by ffmpeg, and the fake engine's),
    refusing what is not one."""
    import importlib.util
    from pathlib import Path

    seen = videogen.read_job(200, json.dumps({"id": "video_gen_1", "status": "in_progress", "progress": 140}).encode())
    assert seen.status == "in_progress" and seen.progress == 100 and seen.error is None
    done = videogen.read_job(200, json.dumps({"status": "completed", "duration_s": 5.0625, "inference_time_s": 70}).encode())
    assert done.duration_ms == 5062 and done.inference_ms == 70000
    failed = videogen.read_job(500, json.dumps({"status": "failed", "error": {"code": 500, "message": "OOM"}}).encode())
    assert failed.status == "failed" and failed.error == "OOM"
    for status, body in ((500, b'{"status": "queued"}'), (404, b'{"detail": "Video not found"}'), (200, b"<html>"),
                         (200, b'{"status": "done"}'), (200, b"[]")):
        assert videogen.read_job(status, body) is None
    assert videogen.engine_id(b'{"id": "video_gen_abc123"}') == "video_gen_abc123"
    for bad in (b'{"id": "../../etc"}', b'{"id": "a b"}', b'{"id": 7}', b'{}', b"nope", b'{"id": "' + b"x" * 200 + b'"}'):
        assert videogen.engine_id(bad) is None
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("accept_video_t", root / "scripts" / "accept_video.py")
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.path.insert(0, str(root / "scripts"))
    spec.loader.exec_module(mod)
    real = (root / "tests" / "fixtures" / "tiny-160x96.mp4").read_bytes()
    info = mod.mp4_info(real)
    assert (info["width"], info["height"], info["frames"], info["codec"]) == (160, 96, 17, "avc1")
    assert abs(info["duration_s"] - 17 / 16) < 0.01 and info["brand"] == "isom"
    fake = mod.mp4_info(tiny_mp4(832, 480, 81, 16))
    assert (fake["width"], fake["height"], fake["frames"], fake["fps"]) == (832, 480, 81, 16.0)
    for bad in (b"", b"\x00\x00\x00\x08moov", real[:300], b"GIF89a" + b"\x00" * 64, real[:32] + b"\x00" * 16):
        with pytest.raises(ValueError):
            mod.mp4_info(bad)
