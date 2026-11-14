"""A fake vLLM-Omni video engine (its /v1/videos at v0.32.0) and MP4 files made here, for the video tests
and the real run through the gateway (`python -m tests.video_helpers --port … --secret …`).

The engine behaves as vLLM-Omni's does: POST /v1/videos takes multipart/form-data only and answers the
job at once (queued); the jobs are made one at a time (the others wait, queued), each `work` seconds;
GET /v1/videos/{id} answers the job (a failed one with its error's HTTP status and the job as the body);
progress stays 0 until it is done; GET …/content is the MP4 (Range requests answered with 206); DELETE
aborts a job at work or deletes the file; every /v1 path needs the replica key. `restart()` forgets every
job, as a restarted engine does (they live in its memory).
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional


# ------------------------------------------------------------------ MP4 files
def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _full(kind: bytes, version: int, payload: bytes) -> bytes:
    return _box(kind, bytes([version, 0, 0, 0]) + payload)


def tiny_mp4(width: int = 832, height: int = 480, frames: int = 81, fps: int = 16, data: bytes = b"\x00" * 64) -> bytes:
    """An ISO base media file with the boxes a video player and scripts/accept_video.py read: ftyp (isom),
    moov with mvhd (timescale = fps, duration = frames), one video trak (tkhd with the size, mdhd, hdlr
    'vide', stbl with stts: `frames` samples) and mdat. Not a decodable picture: the structure is real."""
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = _full(b"mvhd", 0, struct.pack(">IIII", 0, 0, fps, frames) + struct.pack(">IH", 0x10000, 0x100) + b"\x00" * 10
                 + matrix + b"\x00" * 24 + struct.pack(">I", 2))
    tkhd = _full(b"tkhd", 0, struct.pack(">IIIII", 0, 0, 1, 0, frames) + b"\x00" * 8 + struct.pack(">HHHH", 0, 0, 0, 0)
                 + matrix + struct.pack(">II", width << 16, height << 16))
    mdhd = _full(b"mdhd", 0, struct.pack(">IIII", 0, 0, fps, frames) + struct.pack(">HH", 0x55C4, 0))
    hdlr = _full(b"hdlr", 0, struct.pack(">I", 0) + b"vide" + b"\x00" * 12 + b"VideoHandler\x00")
    stts = _full(b"stts", 0, struct.pack(">III", 1, frames, 1))
    stbl = _box(b"stbl", stts)
    minf = _box(b"minf", _full(b"vmhd", 0, b"\x00" * 8) + stbl)
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    moov = _box(b"moov", mvhd + _box(b"trak", tkhd + mdia))
    return ftyp + moov + _box(b"mdat", data)


# ------------------------------------------------------------------ the engine
class FakeVideos:
    """http://127.0.0.1:<port>: vLLM-Omni's video jobs. `work` seconds per job, one at a time; `mp4` the file
    each finished job gets (None: tiny_mp4 of the size and frames asked); `fail_next` makes the next job
    fail with that message; `status` answers every POST with that HTTP status instead."""

    def __init__(self, secret: Optional[str] = "vid-secret", *, work: float = 0.2, mp4: Optional[bytes] = None,
                 port: int = 0, host: str = "127.0.0.1", model: str = "wan", clips: Optional[dict] = None):
        self.secret, self.work, self.mp4, self.model = secret, work, mp4, model
        self.clips = clips or {}             # "WxH" -> a real MP4 for jobs of that size (the real run)
        self.jobs: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.posts: list[dict] = []
        self.deletes: list[str] = []
        self.status: Optional[int] = None
        self.fail_next: Optional[str] = None
        self.hang = False                    # jobs never finish (until deleted)
        self.lock = threading.Lock()
        self._gen = 0
        self._busy = threading.Lock()        # one job at a time, as the diffusion scheduler
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status: int, body: bytes, ctype: str = "application/json", headers: Optional[dict] = None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, status: int, obj) -> None:
                self._send(status, json.dumps(obj).encode())

            def _authorised(self) -> bool:
                if engine.secret and self.headers.get("Authorization") != f"Bearer {engine.secret}":
                    self._json(401, {"error": "Unauthorized"})
                    return False
                return True

            def do_GET(self):
                if self.path == "/health":
                    return self._json(200, {})
                if not self._authorised():
                    return
                m = re.fullmatch(r"/v1/videos/([A-Za-z0-9_]+)(/content)?(\?.*)?", self.path)
                if self.path.split("?")[0] == "/v1/videos":
                    with engine.lock:
                        data = sorted(engine.jobs.values(), key=lambda j: -j["created_at"])
                    return self._json(200, {"object": "list", "data": data, "has_more": False,
                                            "first_id": None, "last_id": None})
                if not m:
                    return self._json(404, {"detail": "Not Found"})
                jid = m.group(1)
                with engine.lock:
                    job = dict(engine.jobs[jid]) if jid in engine.jobs else None
                if job is None:
                    return self._json(404, {"detail": "Video not found"})
                if not m.group(2):
                    if job["status"] == "failed":
                        return self._json(500, job)
                    return self._json(200, job)
                if job["status"] == "failed":
                    return self._json(422, {"detail": "Video generation failed. Check job status for error details."})
                data = engine.files.get(jid)
                if data is None:
                    return self._json(404, {"detail": "Generation is still in-progress"})
                rng = self.headers.get("Range")
                m2 = re.fullmatch(r"bytes=(\d+)-(\d*)", rng or "")
                if m2:
                    a = int(m2.group(1))
                    b = int(m2.group(2)) if m2.group(2) else len(data) - 1
                    if a >= len(data):
                        return self._send(416, b"", headers={"Content-Range": f"bytes */{len(data)}"})
                    b = min(b, len(data) - 1)
                    return self._send(206, data[a:b + 1], "video/mp4",
                                      {"Content-Range": f"bytes {a}-{b}/{len(data)}", "Accept-Ranges": "bytes"})
                self._send(200, data, "video/mp4", {"Accept-Ranges": "bytes",
                                                    "Content-Disposition": f'attachment; filename="{jid}.mp4"'})

            def do_DELETE(self):
                if not self._authorised():
                    return
                m = re.fullmatch(r"/v1/videos/([A-Za-z0-9_]+)", self.path)
                jid = m.group(1) if m else ""
                with engine.lock:
                    known = engine.jobs.pop(jid, None)
                    engine.files.pop(jid, None)
                if known is None:
                    return self._json(404, {"detail": "Video not found"})
                engine.deletes.append(jid)
                self._json(200, {"id": jid, "deleted": True, "object": "video.deleted"})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not self._authorised():
                    return
                if self.path != "/v1/videos":
                    return self._json(404, {"detail": "Not Found"})
                ctype = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ctype:
                    return self._json(422, {"detail": [{"msg": "Field required", "loc": ["body", "prompt"]}]})
                fields = parse_form(raw, ctype)
                engine.posts.append({"fields": fields, "content_type": ctype})
                if engine.status:
                    return self._json(engine.status, {"detail": "engine says no"})
                if not fields.get("prompt"):
                    return self._json(422, {"detail": [{"msg": "Field required", "loc": ["body", "prompt"]}]})
                jid = f"video_gen_{uuid.uuid4().hex}"
                w, _, h = (fields.get("size") or "832x480").partition("x")
                frames = int(fields.get("num_frames") or 1)
                fps = float(fields.get("fps") or 24)
                job = {"id": jid, "object": "video", "model": fields.get("model") or engine.model, "status": "queued",
                       "progress": 0, "created_at": int(time.time()), "completed_at": None, "size": fields.get("size"),
                       "seconds": "4", "quality": "default", "error": None, "media_type": "video/mp4",
                       "expires_at": None, "file_name": None, "fps": fps, "num_frames": frames,
                       "duration_s": None, "prompt": fields.get("prompt"), "inference_time_s": None}
                fail = engine.fail_next
                engine.fail_next = None
                with engine.lock:
                    engine.jobs[jid] = job
                    gen = engine._gen
                threading.Thread(target=engine._make, args=(jid, int(w), int(h), frames, fps, fail, gen),
                                 daemon=True).start()
                self._json(200, job)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _make(self, jid: str, w: int, h: int, frames: int, fps: float, fail: Optional[str], gen: int) -> None:
        with self._busy:
            with self.lock:
                if jid not in self.jobs or self._gen != gen:
                    return
                self.jobs[jid]["status"] = "in_progress"
            started = time.monotonic()
            end = started + self.work
            while time.monotonic() < end or self.hang:
                time.sleep(0.02)
                with self.lock:
                    if jid not in self.jobs or self._gen != gen:
                        return
            with self.lock:
                if jid not in self.jobs or self._gen != gen:
                    return
                job = self.jobs[jid]
                job["completed_at"] = int(time.time())
                job["inference_time_s"] = time.monotonic() - started
                if fail:
                    job["status"] = "failed"
                    job["error"] = {"code": 500, "message": fail}
                    return
                clip = self.clips.get(f"{w}x{h}")
                self.files[jid] = clip if clip is not None else self.mp4 if self.mp4 is not None else \
                    tiny_mp4(w, h, frames, int(fps))
                job.update(status="completed", progress=100, file_name=f"{jid}.mp4", duration_s=frames / fps)

    def restart(self) -> None:
        """Forget every job and file, as a restarted engine does."""
        with self.lock:
            self._gen += 1
            self.jobs.clear()
            self.files.clear()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def parse_form(raw: bytes, ctype: str) -> dict:
    from forgeyard.gateway import multipart

    _, parts = multipart.parse(raw, ctype)
    return {p.name: (p.content.decode("utf-8", "replace") if not p.is_file else {"file": True}) for p in parts}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A fake vLLM-Omni video engine (its /v1/videos at v0.32.0).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--secret", default=None, help="the replica key it requires (default: $VLLM_API_KEY, as vLLM "
                    "reads it; none when that is unset)")
    ap.add_argument("--work", type=float, default=8.0, help="seconds each video takes")
    ap.add_argument("--mp4", default=None, help="an MP4 file every finished job gets (default: a tiny one)")
    ap.add_argument("--clips", default=None, help="a directory of clip-<W>x<H>.mp4 files: a job of that size gets it")
    a = ap.parse_args()
    import os

    clips = {p.stem.removeprefix("clip-"): p.read_bytes() for p in Path(a.clips).glob("clip-*.mp4")} if a.clips else {}
    e = FakeVideos(a.secret or os.environ.get("VLLM_API_KEY") or None, work=a.work, clips=clips,
                   mp4=Path(a.mp4).read_bytes() if a.mp4 else None, port=a.port, host=a.host)
    print(f"fake vLLM-Omni videos on {a.host}:{e.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        e.close()
