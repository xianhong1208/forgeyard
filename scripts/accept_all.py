#!/usr/bin/env python3
"""Full regression acceptance: one command that checks, on real machines, every feature Forgeyard has
shipped so far (1.0 to 1.2), so that a pull request or a release can show the old features still work.

It works through the server's API only (an admin session), calls /v1 with the official `openai` SDK,
and drives the web console with Playwright when it is installed. Every check prints PASS, FAIL or SKIP
(with the reason). The exit code is 0 when nothing failed, 1 when a check failed, 2 when the run
could not start (a wrong password, an unknown machine, …).

    FORGEYARD_PASSWORD=… python3 scripts/accept_all.py --server https://192.168.1.20:7860 \\
        --ca forgeyard-ca.pem --user admin \\
        --gpu-machine gpu-01 --cpu-machine cpu-01 --second-cpu-machine cpu-02 \\
        --cloud-provider gemini --out accept-all.json --shots accept-shots

Groups, in the order they run (`--only` and `--skip` take comma-separated names):

  server     HTTPS with the CA, /api/health, security headers, a foreign Host (421), the CSRF rule,
             sign-in back-off on a throwaway account (never the admin), /guide, /trust, /install.sh
  machines   each given machine is online with the hardware it should have; its GPU allowlist is
             enforced; a join command pins the CA
  workloads  on each given machine: a process service (healthy, output, stop, start), tasks (ok,
             retries, max_duration), a container task when it has a container engine; GPU isolation
             on the GPU machine (scripts/gpu_probe.py, also in a container on NVIDIA)
  models     deploys MODELS (below), waiting with progress: Qwen3-1.7B GGUF on the CPU machine(s);
             a chat, an embedding, a rerank and a speech model on the GPU machine, each skipped when
             its estimate says it does not fit; the estimate against the deployment page; Try it
  gateway    /v1 with the official SDK: the model list and key allowlists, chat, streaming with and
             without include_usage, completions, embeddings, rerank, transcription, rpm (429), a
             revoked key, locked limits, the replicas' key guard, balancing and failover over two
             CPU machines, the Playground, usage rows, a stopped deployment leaving the list
  cloud      only with --cloud-provider: spilling a prompt too long for the local model, keys that
             may not spill, local only (route and header), the cloud maximum, cost, budgets (key,
             person, provider) and their 80% / 100% alerts. Exactly one request reaches the
             provider (CLOUD_PROMPT); everything changed on it is put back
  tts        text to speech (1.3, scripts/accept_tts.py's checks): Qwen3-TTS 0.6B on vLLM-Omni on the GPU
             machine and Chatterbox Turbo GGUF on llama.cpp on the CPU machine: the SDK's speech calls,
             every format, streaming, voices, voice cloning refused, the input cap, usage (characters and
             audio length), the key guard and, with a speech-to-text model, intelligibility (--tts-asr)
  img        image generation (1.3, scripts/accept_img.py's checks): Z-Image-Turbo on vLLM-Omni on the GPU
             machine and Stable Diffusion 1.5 GGUF on stable-diffusion.cpp on the CPU machine: the SDK's
             images.generate, images that decode at the size asked, n, the caps, url refused, the queue,
             usage (images, kilopixels, busy time), the key guard and, with a chat model that reads images,
             what the image shows (--img-vision)
  video      video generation (1.3, scripts/accept_video.py's checks): Wan2.1-T2V-1.3B on vLLM-Omni on the GPU
             machine: the SDK's videos.create, polling to completed, the MP4 fetched through the gateway and read
             (size, frames, length), the caps, image-to-video refused, another key's 404, the per-key job cap,
             deleting a job at work, usage (media_ms, busy time), expiry (--video-no-expiry skips its wait),
             the key guard, and a job failing when its deployment is stopped under it
  passthrough  workloads at /x/<name> (1.3, scripts/accept_x.py's checks): a small HTTP app exposed as a
             process on the CPU machine and in a container on the GPU machine: keys (none, listed), the path
             as sent and refused paths, credentials and forwarded headers replaced, the forced sandbox,
             redirects, streaming, a client that hangs up, the body limit, rpm, concurrency, the read
             timeout, the replica's port behind the key guard, usage (bytes, busy time), turning it off; with
             --x-templates, the ready-made templates on the GPU machine (an image in, boxes, masks, depth out)
  console    with Playwright for Python: every main page as an admin and as a plain user, at 1440
             and 390 px: it loads, names itself in the tab, logs no error, does not scroll sideways

The gateway and cloud groups need the models group's deployments: it runs for them when it was not
chosen. A machine that is not given skips what would run on it, saying so.

Everything the run creates is named accept-<run>-… (the run id is printed first) and removed at the
end, also after a failure or Ctrl-C: deployments, workloads, routes, API keys, the throwaway user,
and the catalog entries it added (models it found in the catalog stay). A provider's budget goes back
to what it was. Nothing it did not create is changed, except that one budget for a few seconds. Usage
rows and audit events of the run stay, like those of any request. Objects left by an earlier run
that was killed are listed at the start, never touched.

The official SDK (and, with --install-playwright, Playwright): when they cannot be imported, the
script makes its own virtual environment (--venv; with uv when it is installed, else python -m venv
and pip), installs them there and runs itself again with it. Nothing else is installed anywhere.

Adding a group (a 1.3 feature: multimodal, tts, images, passthrough, video): write a function that
takes the Run, calls run.check(name, ok, detail) / run.skip(name, reason) for each check, registers
everything it creates with run.cleanup.add(label, fn, …), and decorate it with
@group("<name>", "<title>", needs=("models",)). Groups run in the order they are defined. Useful:
run.admin (the admin's Client), run.deps (the models group's ready deployments by key), run.sdk(key),
run.new_key(what, **body), run.name(what), run.machine(role), run.user() (the throwaway account).
"""

from __future__ import annotations

import argparse
import base64
import collections
import getpass
import hashlib
import http.cookiejar
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import ssl
import string
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
CLIP = HERE.parent / "forgeyard" / "models" / "assets" / "jfk-ask-not.mp3"
GPU_PROBE = HERE / "gpu_probe.py"
GIB = 1024**3
SDK_SPEC = "openai>=2,<4"
PLAYWRIGHT_SPEC = "playwright>=1.48,<2"
REEXEC = "FORGEYARD_ACCEPT_ALL_REEXEC"
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
RERANK_OVERRIDES = ('{"architectures": ["Qwen3ForSequenceClassification"], "classifier_from_token": ["no", "yes"], '
                    '"is_original_qwen3_reranker": true}')

# ------------------------------------------------------------------ what the run deploys and runs
# The models group deploys these. `device` says which machine: cpu (--cpu-machine, and a second copy on
# --second-cpu-machine) or gpu (--gpu-machine). Catalog names are those of accept_m3.py / accept_m4.py,
# so a catalog entry (and a machine's model cache) from those runs is reused; the run removes only the
# entries it added itself.
MODELS = [
    {"key": "cpu", "device": "cpu", "model": "accept-qwen3-1.7b-gguf", "source": "hf://ggml-org/Qwen3-1.7B-GGUF",
     "file": "Qwen3-1.7B-Q4_K_M.gguf", "task": "chat", "recipe": "llama-cpp",
     "params": {"gpus": 0, "context": 4096, "concurrency": 2, "threads": 8}},
    {"key": "chat", "device": "gpu", "model": "accept-qwen3-4b", "source": "hf://Qwen/Qwen3-4B", "task": "chat",
     "recipe": "vllm", "params": {"context": 8192, "concurrency": 4, "share": "vram"}},
    {"key": "embed", "device": "gpu", "model": "accept-qwen3-embedding-0.6b", "source": "hf://Qwen/Qwen3-Embedding-0.6B",
     "task": "embed", "recipe": "vllm-pooling", "params": {}},
    {"key": "rerank", "device": "gpu", "model": "accept-qwen3-reranker-0.6b", "source": "hf://Qwen/Qwen3-Reranker-0.6B",
     "task": "rerank", "recipe": "vllm-pooling", "params": {},
     "engine_args": {"vllm-pooling": ["--hf-overrides", RERANK_OVERRIDES, "--chat-template",
                                      "${MODEL_PATH}/chat_template.jinja"]}},
    {"key": "asr", "device": "gpu", "model": "accept-faster-whisper-small", "source": "hf://Systran/faster-whisper-small",
     "task": "asr", "recipe": "faster-whisper", "params": {}},
]
# Images for container workloads (pulled by the machine's container engine when missing).
CONTAINER_IMAGE = "busybox:1.37"
CUDA_IMAGE = "nvidia/cuda:12.4.1-base-ubuntu22.04"
# The only text ever sent to a cloud provider, in the one request that reaches it.
CLOUD_PROMPT = "Count from 1 to 20, separated by spaces."
# Pages the console group opens: (path, the tab title it must have; None: the page's own name).
CONSOLE_PAGES = [
    ("/start", None), ("/machines", "Machines"), ("/workloads", "Workloads"), ("/workloads/new", "New workload"),
    ("/models", "Models"), ("/models/deploy", "Deploy a model"), ("/providers", "Cloud providers"),
    ("/playground", "Playground"), ("/keys", "API keys"), ("/usage", "Usage"), ("/account", None),
    ("/users", "Users"), ("/audit", "Audit log"), ("/trust", "Trust this server"), ("/guide/en/how-to", "How-to"),
]
ADMIN_PAGES = ("/providers", "/users", "/audit")
PUBLIC_PAGES = [("/login", "Sign in"), ("/trust", "Trust this server"), ("/guide/en/quick-start", "Quick start")]

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Fail(Exception):
    """Something the run needed did not work: the current group stops with a FAIL."""


class Refused(Exception):
    """The run cannot start (exit code 2)."""


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------------ the report
@dataclass
class Check:
    group: str
    name: str
    status: str
    detail: str = ""


@dataclass
class GroupResult:
    name: str
    title: str
    why: str = ""            # "needed by gateway", …
    seconds: float = 0.0


class Report:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.started = clock()
        self.finished: Optional[float] = None
        self.checks: list[Check] = []
        self.groups: dict[str, GroupResult] = {}
        self.notes: list[str] = []
        self.cleanup: list[dict] = []
        self.info: dict = {}

    def group(self, name: str, title: str, why: str = "") -> GroupResult:
        self.groups[name] = GroupResult(name, title, why)
        return self.groups[name]

    def add(self, group: str, name: str, status: str, detail: str = "") -> Check:
        if status not in (PASS, FAIL, SKIP):
            raise ValueError(f"status must be PASS, FAIL or SKIP, not {status!r}")
        c = Check(group, name, status, " ".join(str(detail).split())[:600])
        self.checks.append(c)
        return c

    def totals(self, group: Optional[str] = None) -> dict:
        n = collections.Counter(c.status for c in self.checks if group is None or c.group == group)
        return {PASS: n[PASS], FAIL: n[FAIL], SKIP: n[SKIP]}

    @property
    def ok(self) -> bool:
        return not any(c.status == FAIL for c in self.checks) and not any(not x["ok"] for x in self.cleanup)

    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def finish(self) -> None:
        self.finished = self.clock()

    @property
    def seconds(self) -> float:
        return (self.finished if self.finished is not None else self.clock()) - self.started

    def render(self) -> str:
        out = []
        width = max([len(c.name) for c in self.checks] + [20])
        width = min(width, 78)
        for g in self.groups.values():
            t = self.totals(g.name)
            head = f"== {g.name}: {g.title}" + (f" ({g.why})" if g.why else "")
            out.append(f"{head}  [{t[PASS]} pass, {t[FAIL]} fail, {t[SKIP]} skip; {fmt_seconds(g.seconds)}]")
            for c in (c for c in self.checks if c.group == g.name):
                name = c.name if len(c.name) <= width else c.name[: width - 1] + "…"
                out.append(f"  {c.status}  {name.ljust(width)}  {c.detail[:160]}".rstrip())
            out.append("")
        skipped = [c for c in self.checks if c.status == SKIP]
        if skipped:
            out.append("Skipped, and why:")
            out += [f"  {c.group}: {c.name}: {c.detail}" for c in skipped]
            out.append("")
        failed = [c for c in self.checks if c.status == FAIL]
        if failed:
            out.append("Failed:")
            out += [f"  {c.group}: {c.name}: {c.detail}" for c in failed]
            out.append("")
        bad = [x for x in self.cleanup if not x["ok"]]
        if self.cleanup:
            out.append(f"Cleanup: {len(self.cleanup) - len(bad)} of {len(self.cleanup)} removed or restored"
                       + ("" if not bad else "; NOT removed: " + "; ".join(f"{x['label']} ({x['error']})" for x in bad)))
        for n in self.notes:
            out.append(f"Note: {n}")
        t = self.totals()
        out.append(f"Total: {t[PASS]} pass, {t[FAIL]} fail, {t[SKIP]} skip in {fmt_seconds(self.seconds)}: "
                   + ("OK" if self.ok else "FAILED"))
        return "\n".join(out)

    def to_json(self) -> dict:
        return {"ok": self.ok, "seconds": round(self.seconds, 1), "totals": self.totals(), "info": self.info,
                "groups": [{"name": g.name, "title": g.title, "why": g.why, "seconds": round(g.seconds, 1),
                            "totals": self.totals(g.name),
                            "checks": [{"check": c.name, "status": c.status, "detail": c.detail}
                                       for c in self.checks if c.group == g.name]} for g in self.groups.values()],
                "cleanup": self.cleanup, "notes": self.notes}


def fmt_seconds(s: float) -> str:
    s = int(round(s))
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


# ------------------------------------------------------------------ the cleanup registry
class Cleanup:
    """What the run created, removed in reverse order at the end, whatever happened. Each undo runs
    once; one that fails is recorded and the others still run."""

    def __init__(self, say: Callable[[str], None] = log):
        self.say = say
        self._items: list[dict] = []
        self.results: list[dict] = []

    def add(self, label: str, fn: Callable, *args, **kwargs) -> dict:
        item = {"label": label, "fn": fn, "args": args, "kwargs": kwargs, "done": False}
        self._items.append(item)
        return item

    def now(self, item: dict) -> bool:
        """Undo one item at once (a deployment stopped mid-run, a budget restored early)."""
        if item["done"]:
            return True
        return self._undo(item)

    def _undo(self, item: dict) -> bool:
        item["done"] = True
        try:
            item["fn"](*item["args"], **item["kwargs"])
            self.results.append({"label": item["label"], "ok": True, "error": None})
            return True
        except BaseException as e:  # noqa: BLE001 - a cleanup step must never stop the others
            self.results.append({"label": item["label"], "ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            self.say(f"cleanup: could not undo {item['label']}: {e}")
            return False

    @property
    def pending(self) -> int:
        return sum(1 for i in self._items if not i["done"])

    def run(self) -> list[dict]:
        for item in reversed(self._items):
            if not item["done"]:
                self._undo(item)
        return self.results


# ------------------------------------------------------------------ arguments and groups
@dataclass
class Group:
    name: str
    title: str
    fn: Callable
    needs: tuple = ()


GROUPS: dict[str, Group] = {}


def group(name: str, title: str, needs: tuple = ()):
    def register(fn):
        GROUPS[name] = Group(name, title, fn, tuple(needs))
        return fn
    return register


def names_list(text: Optional[str]) -> list[str]:
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def plan_groups(only: list[str], skip: list[str], known: list[str], needs: dict[str, tuple]) -> list[tuple[str, str]]:
    """The groups to run, in order, each with why it runs ("" when chosen, "needed by …" otherwise)."""
    unknown = [g for g in only + skip if g not in known]
    if unknown:
        raise ValueError(f"unknown group {unknown[0]!r}: choose from {', '.join(known)}")
    if only and skip:
        raise ValueError("use --only or --skip, not both")
    chosen = [g for g in known if (g in only if only else g not in skip)]
    if not chosen:
        raise ValueError("no group left to run")
    why: dict[str, str] = {g: "" for g in chosen}
    for g in chosen:
        for dep in needs.get(g, ()):
            if dep not in why:
                why[dep] = f"needed by {g}"
    return [(g, why[g]) for g in known if g in why]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860", help="the server's address")
    ap.add_argument("--ca", "--cacert", dest="ca", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner", help="an admin account (password from FORGEYARD_PASSWORD, or asked)")
    ap.add_argument("--gpu-machine", help="a machine with GPUs: GPU workloads and GPU models run there")
    ap.add_argument("--cpu-machine", help="a machine for CPU workloads and the CPU model (may be the GPU machine)")
    ap.add_argument("--second-cpu-machine", help="another machine for the CPU model: balancing and failover")
    ap.add_argument("--cloud-provider", help="an existing provider: the cloud group runs only with it")
    ap.add_argument("--cloud-model", help="its model to use (default: its first chat model)")
    ap.add_argument("--only", help="run only these groups (comma-separated)")
    ap.add_argument("--skip", help="run every group but these")
    ap.add_argument("--timeout", type=float, default=2400, help="seconds a deployment may take to be ready")
    ap.add_argument("--kill-local", action="store_true",
                    help="kill the engine of a CPU replica running on THIS computer to show failover")
    ap.add_argument("--tts-asr", default="auto", help="the tts group's intelligibility check: auto (a ready "
                    "speech-to-text deployment, else skipped), a served model, local:<faster-whisper model>, or none")
    ap.add_argument("--img-vision", default="auto", help="the img group's check of what an image shows: auto (a "
                    "ready chat model that reads images, else skipped), a served model, or none")
    ap.add_argument("--video-steps", type=int, help="the video group's num_inference_steps (default: the model's "
                    "own); fewer is faster")
    ap.add_argument("--video-no-expiry", action="store_true", help="the video group skips its expiry check (a wait of "
                    "about two minutes)")
    ap.add_argument("--x-templates", default="", help="the passthrough group also deploys these workload templates "
                    "(object-detection,segmentation,depth or all) on the GPU machine's NVIDIA card (a multi-GB image "
                    "pull the first time)")
    ap.add_argument("--shots", help="save the console group's screenshots in this directory")
    ap.add_argument("--chrome", default="/opt/google/chrome/chrome", help="Chrome for the console group")
    ap.add_argument("--install-playwright", action="store_true",
                    help="install Playwright for Python into --venv when it is missing (for the console group)")
    ap.add_argument("--venv", default=str(Path.home() / ".cache" / "forgeyard-accept-all"),
                    help="where to put a virtual environment with the openai SDK if it is not installed")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args(argv)
    try:
        args.plan = plan_groups(names_list(args.only), names_list(args.skip), list(GROUPS),
                                {g.name: g.needs for g in GROUPS.values()})
    except ValueError as e:
        ap.error(str(e))
    if args.second_cpu_machine and not args.cpu_machine:
        ap.error("--second-cpu-machine needs --cpu-machine")
    if args.second_cpu_machine and args.second_cpu_machine in (args.cpu_machine, args.gpu_machine):
        ap.error("--second-cpu-machine must be another machine than --cpu-machine and --gpu-machine")
    if args.cloud_model and not args.cloud_provider:
        ap.error("--cloud-model needs --cloud-provider")
    if args.timeout <= 0:
        ap.error("--timeout must be positive")
    args.server = args.server.rstrip("/")
    return args


def planned(args, name: str) -> bool:
    return any(g == name for g, _ in args.plan)


# ------------------------------------------------------------------ small pure helpers
def ca_fingerprint(pem: str) -> str:
    """sha256:<hex> of the first certificate in a PEM, as Forgeyard prints it."""
    body = pem.split("-----BEGIN CERTIFICATE-----", 1)[1].split("-----END CERTIFICATE-----", 1)[0]
    return "sha256:" + hashlib.sha256(base64.b64decode("".join(body.split()))).hexdigest()


def choose_budget(spent_micros: int) -> Optional[int]:
    """A budget (micro-USD) that `spent_micros` (rounded half up from the real spend) is at least 80% of
    but below, so the next request alerts at 80% and is then refused (100%). None: too small to fit."""
    if spent_micros < 1:
        return None
    budget = (5 * (2 * spent_micros - 1)) // 8          # floor(1.25 * (spent - 0.5))
    return budget if budget >= spent_micros + 1 else None


def provider_test_budget(spent_micros: int, reserved_micros: int) -> int:
    """A provider budget that this month's spend stays below 80% of, so setting it alerts nobody."""
    used = spent_micros + reserved_micros
    return (5 * used) // 4 + 2


def tokens_for(micros: float, price_out: Optional[int], least: int = 64, most: int = 10_000_000) -> Optional[int]:
    """max_tokens whose estimated output cost is at least twice `micros` (price in micro-USD per
    million tokens). None when it would have to be larger than `most`."""
    if not price_out:
        return least
    n = max(least, int(2 * max(micros, 1) * 1_000_000 / price_out) + 1)
    return n if n <= most else None


def utc(ts: str):
    """A server timestamp (naive ones are UTC) as an aware datetime."""
    from datetime import datetime, timezone

    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def why(r: "Resp") -> str:
    """The message of an error answer (either shape), or its start."""
    d = r.data if isinstance(r.data, dict) else {}
    for k in ("detail", "error"):
        if isinstance(d.get(k), dict) and d[k].get("message"):
            return str(d[k]["message"])[:200]
    return r.text[:200]


def target_of(headers) -> str:
    return (headers.get("x-forgeyard-target") or headers.get("X-Forgeyard-Target") or "") if headers else ""


def multipart(fields: dict, file_field: str, path: Path, ctype: str = "audio/mpeg") -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = [f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in fields.items()]
    parts.append((f"--{b}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{path.name}\"\r\n"
                  f"Content-Type: {ctype}\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def probe_source() -> str:
    """scripts/gpu_probe.py as a `python3 -c` program (compressed: one argument holds at most 4096
    characters), so it runs on a machine without a copy of this repository."""
    blob = base64.b64encode(zlib.compress(GPU_PROBE.read_bytes(), 9)).decode()
    return f"import base64,zlib;exec(zlib.decompress(base64.b64decode('{blob}')))"


# ------------------------------------------------------------------ HTTP
@dataclass
class Resp:
    status: int
    headers: dict
    body: bytes
    error: Optional[str] = None      # no answer at all: why

    @property
    def data(self):
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace") if self.body else (self.error or "")

    def code(self) -> str:
        """The error code of an answer: Forgeyard's {"detail": {"code"}} or the gateway's {"error": {"code"}}."""
        d = self.data
        if isinstance(d, dict):
            for k in ("detail", "error"):
                if isinstance(d.get(k), dict) and d[k].get("code"):
                    return str(d[k]["code"])
        return ""


def raw(url: str, method: str = "GET", body: Optional[bytes] = None, headers: Optional[dict] = None,
        tls: Optional[ssl.SSLContext] = None, timeout: float = 60, opener=None) -> Resp:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        fn = opener.open if opener is not None else (lambda r, timeout: urllib.request.urlopen(r, timeout=timeout, context=tls))
        with fn(req, timeout=timeout) as r:
            return Resp(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Resp(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read())
    except (urllib.error.URLError, OSError) as e:
        return Resp(0, {}, b"", f"{type(getattr(e, 'reason', e)).__name__}: {getattr(e, 'reason', e)}")


class Client:
    """A signed-in session on the server's API (cookie + the X-Forgeyard header on writes)."""

    def __init__(self, server: str, tls: Optional[ssl.SSLContext]):
        self.server = server
        self.tls = tls
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  urllib.request.HTTPSHandler(context=tls))
        self.username = ""

    def request(self, method: str, path: str, body=None, headers: Optional[dict] = None, *,
                csrf: bool = True, timeout: float = 120, wait_on_429: bool = True) -> Resp:
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        h = {"Accept": "application/json"}
        if csrf:
            h["X-Forgeyard"] = "1"
        if data is not None and not isinstance(body, bytes):
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        for attempt in range(4):
            r = raw(self.server + path, method, data, h, timeout=timeout, opener=self.opener)
            # The API's per-address budget (RATE_LIMIT_PER_MINUTE): wait, never fail a check on it.
            if wait_on_429 and r.status == 429 and r.code() == "rate_limited" and attempt < 3:
                wait = min(65.0, float(r.headers.get("retry-after") or 5))
                log(f"  the server asks to slow down ({path}); waiting {wait:.0f} s")
                time.sleep(wait)
                continue
            return r
        return r

    def call(self, method: str, path: str, body=None, **kw) -> tuple[int, object]:
        r = self.request(method, path, body, **kw)
        return r.status, r.data

    def ok(self, method: str, path: str, body=None, **kw):
        r = self.request(method, path, body, **kw)
        if not 200 <= r.status < 300:
            d = r.data
            detail = (d.get("detail") if isinstance(d, dict) else None) or r.text[:200]
            raise Fail(f"{method} {path} -> {r.status or r.error}: {detail}")
        return r.data

    def login(self, username: str, password: str) -> None:
        r = self.request("POST", "/api/auth/login", {"username": username, "password": password})
        if r.status != 200:
            raise Refused(f"cannot sign in as {username}: {r.status or r.error} {r.code() or r.text[:120]}")
        self.username = username

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)


# ------------------------------------------------------------------ the run
class Run:
    def __init__(self, args: argparse.Namespace, tls: Optional[ssl.SSLContext], report: Optional[Report] = None):
        self.args = args
        self.tls = tls
        self.report = report or Report()
        self.cleanup = Cleanup()
        self.tag = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
        self.admin = Client(args.server, tls)
        self.current = "setup"
        self.version = ""
        self.machines: dict[str, dict] = {}       # role (gpu, cpu, cpu2) -> machine as /api/machines lists it
        self.deps: dict[str, dict] = {}           # model key -> ready deployment
        self.models_done = False
        self.cloud_calls = 0
        self.cloud_ok = False
        self.password = ""
        self._user: Optional[dict] = None

    # -- names and checks
    def name(self, what: str) -> str:
        return f"accept-{self.tag}-{what}"

    def check(self, name: str, ok, detail="") -> bool:
        c = self.report.add(self.current, name, PASS if ok else FAIL, detail)
        log(f"{c.status}  {name}" + (f"  ({c.detail[:200]})" if c.detail else ""))
        return bool(ok)

    def skip(self, name: str, reason: str) -> None:
        c = self.report.add(self.current, name, SKIP, reason)
        log(f"SKIP  {name}  ({c.detail[:200]})")

    def machine(self, role: str) -> Optional[dict]:
        return self.machines.get(role)

    def fresh_machine(self, role: str) -> Optional[dict]:
        m = self.machines.get(role)
        if m is None:
            return None
        for x in self.admin.ok("GET", "/api/machines")["machines"]:
            if x["id"] == m["id"]:
                self.machines[role] = x
                return x
        return None

    # -- keys, the SDK, the throwaway user
    def new_key(self, what: str, client: Optional[Client] = None, **body) -> dict:
        c = client or self.admin
        out = c.ok("POST", "/api/keys", {"name": self.name(what), **body})
        self.cleanup.add(f"API key {out['api_key']['name']}", self._revoke_key, c, out["api_key"]["id"])
        return out

    def _revoke_key(self, c: Client, key_id: str) -> None:
        r = c.request("DELETE", f"/api/keys/{key_id}")
        if r.status not in (200, 404):
            raise Fail(f"revoking {key_id}: {r.status} {r.code()}")

    def sdk(self, key: str, headers: Optional[dict] = None):
        import openai

        # The SDK's own client class: httpx in openai 1.x/2.x, httpx2 in later 3.x releases.
        http = openai.DefaultHttpxClient(verify=self.tls, timeout=300) if self.tls else None
        return openai.OpenAI(base_url=self.args.server + "/v1", api_key=key, max_retries=0, timeout=300,
                             http_client=http, default_headers=headers)

    def user(self) -> dict:
        """The run's throwaway account (role user), signed in: {"id", "name", "password", "client"}."""
        if self._user is not None:
            return self._user
        name = self.name("user")
        created = self.admin.ok("POST", "/api/users", {"username": name, "role": "user"})
        uid = created["user"]["id"]
        self.cleanup.add(f"user {name}", self._delete_user, uid)
        token = created["set_password_url"].split("#token=", 1)[1]
        password = "Fy" + secrets.token_urlsafe(18) + "-9"
        anon = Client(self.args.server, self.tls)
        anon.ok("POST", "/api/auth/password-link", {"token": token, "password": password})
        client = Client(self.args.server, self.tls)
        client.login(name, password)
        self._user = {"id": uid, "name": name, "password": password, "client": client}
        return self._user

    def _delete_user(self, uid: int) -> None:
        if self._user and self._user.get("client"):
            for k in (self._user["client"].ok("GET", "/api/keys").get("keys") or []):
                if k["status"] == "active":
                    self._user["client"].request("DELETE", f"/api/keys/{k['id']}")
        r = self.admin.request("DELETE", f"/api/users/{uid}")
        if r.status not in (200, 404):
            raise Fail(f"{r.status} {r.code()}")

    # -- waiting
    def wait(self, what: str, fn: Callable[[], tuple[bool, str]], timeout: float, every: float = 2.0) -> str:
        """Poll fn() -> (done, state text) until done; prints each new state. Raises Fail on timeout."""
        deadline = time.monotonic() + timeout
        last, shape, said = None, None, 0.0
        while True:
            done, text = fn()
            # A new state at once; the same state with new numbers (a download) every 30 s.
            now_shape = re.sub(r"[\d.]+", "#", text)
            if text != last and (now_shape != shape or done or time.monotonic() - said >= 30):
                log(f"  {what}: {text}")
                last, shape, said = text, now_shape, time.monotonic()
            if done:
                return text
            if time.monotonic() > deadline:
                raise Fail(f"{what}: not done within {timeout:.0f} s (last: {text})")
            time.sleep(every)

    # -- the groups
    def run_group(self, g: Group, why: str) -> None:
        self.current = g.name
        result = self.report.group(g.name, g.title, why)
        log(f"=== {g.name}: {g.title}" + (f" ({why})" if why else ""))
        t0 = time.monotonic()
        try:
            g.fn(self)
        except Fail as e:
            self.check(f"{g.name}: the group stopped", False, str(e))
        except KeyboardInterrupt:
            self.check(f"{g.name}: interrupted", False, "Ctrl-C or SIGTERM")
            raise
        except Exception as e:  # noqa: BLE001 - one broken group must not stop the run or the cleanup
            frames = traceback.extract_tb(e.__traceback__)
            tb = next((f for f in reversed(frames) if f.filename == __file__), frames[-1])
            self.check(f"{g.name}: the group stopped", False,
                       f"{type(e).__name__}: {e} (line {tb.lineno} in {tb.name})")
        finally:
            result.seconds = time.monotonic() - t0


# ------------------------------------------------------------------ setup
def start(run: Run, password: str) -> None:
    a = run.args
    r = raw(a.server + "/api/health", tls=run.tls, timeout=20)
    if r.status == 0:
        hint = " (pass the server's CA with --ca)" if "CERTIFICATE" in (r.error or "").upper() else ""
        raise Refused(f"cannot reach {a.server}: {r.error}{hint}")
    run.version = (r.data or {}).get("version", "")
    run.admin.login(a.user, password)
    me = run.admin.ok("GET", "/api/auth/me")["user"]
    if me.get("role") != "admin":
        raise Refused(f"{a.user} is not an admin")
    machines = {m["name"]: m for m in run.admin.ok("GET", "/api/machines")["machines"]}
    for role, name in (("gpu", a.gpu_machine), ("cpu", a.cpu_machine), ("cpu2", a.second_cpu_machine)):
        if name:
            if name not in machines:
                raise Refused(f"no machine named {name!r} (have: {', '.join(sorted(machines)) or 'none'})")
            run.machines[role] = machines[name]
    if a.cloud_provider:
        names = [p["name"] for p in run.admin.ok("GET", "/api/providers")["providers"]]
        if a.cloud_provider not in names:
            raise Refused(f"no provider named {a.cloud_provider!r} (have: {', '.join(names) or 'none'})")
    run.report.info.update({"server": a.server, "version": run.version, "run": run.tag, "user": a.user,
                            "machines": {k: v["name"] for k, v in run.machines.items()},
                            "cloud_provider": a.cloud_provider, "started": time.strftime("%Y-%m-%d %H:%M:%S")})
    leftovers(run)


def leftovers(run: Run) -> None:
    """Objects an earlier run left (it was killed before its cleanup): listed, never touched."""
    pat = re.compile(r"^accept-[a-z0-9]{4}-")
    found = []
    for kind, path, key, field_ in (("deployment", "/api/deployments", "deployments", "name"),
                                    ("workload", "/api/workloads", "workloads", "name"),
                                    ("route", "/api/routes", "routes", "served_name"),
                                    ("user", "/api/users", "users", "username")):
        for x in run.admin.ok("GET", path).get(key) or []:
            if pat.match(str(x.get(field_, ""))) and not (kind == "workload" and x.get("runtime") == "model"):
                found.append(f"{kind} {x[field_]}")
    for k in run.admin.ok("GET", "/api/keys").get("keys") or []:
        if pat.match(k["name"]) and k["status"] == "active":
            found.append(f"API key {k['name']}")
    if found:
        note = ("named like this script's, from an earlier run that was killed (or one still running), not touched: "
                + ", ".join(found))
        run.report.notes.append(note)
        log("NOTE: " + note)


# ------------------------------------------------------------------ group: server
@group("server", "HTTPS, health, request guards, sign-in back-off, guide, trust")
def group_server(run: Run) -> None:
    a, c = run.args, run.admin
    url = a.server
    r = raw(url + "/api/health", tls=run.tls, timeout=20)
    run.check("HTTPS: /api/health over a verified connection", r.status == 200 and url.startswith("https://"),
              f"{r.status} version {run.version}" + ("" if url.startswith("https://") else "; the server is not on https://"))
    tls_info = c.ok("GET", "/api/tls")
    mode = tls_info.get("mode")
    if a.ca:
        mine = ca_fingerprint(Path(a.ca).read_text())
        served = raw(url + "/api/tls/ca.pem", tls=run.tls)
        theirs = ca_fingerprint(served.text) if served.status == 200 and "BEGIN CERTIFICATE" in served.text else None
        run.check("the CA given with --ca is the one the server serves (/api/tls/ca.pem)",
                  theirs == mine and (tls_info.get("ca") or {}).get("fingerprint") == mine, f"{mine[:23]}…")
    else:
        run.skip("the CA given with --ca is the one the server serves", "no --ca: the system's trust store was used")
    h = r.headers
    want = {"x-content-type-options": "nosniff", "referrer-policy": "same-origin", "x-frame-options": "DENY",
            "cache-control": "no-store"}
    missing = {k: h.get(k) for k, v in want.items() if h.get(k) != v}
    hsts = "strict-transport-security" in h
    run.check("security headers (nosniff, same-origin referrer, no frames, no-store on /api)", not missing,
              f"wrong: {missing}" if missing else "all four")
    run.check("Strict-Transport-Security only with your own certificate", hsts == (mode == "own"),
              f"mode {mode}, HSTS {'sent' if hsts else 'not sent'}")
    for path, shape in (("/api/health", "detail"), ("/v1/models", "error")):
        r = raw(url + path, headers={"Host": "accept-foreign.example"}, tls=run.tls)
        run.check(f"a foreign Host gets 421 ({path})", r.status == 421 and r.code() == "host_not_allowed"
                  and shape in (r.data or {}), f"{r.status} {r.code()}")
    r = c.request("POST", "/api/keys", {"name": run.name("csrf")}, csrf=False)
    run.check("an API write without X-Forgeyard is refused (403 csrf)", r.status == 403 and r.code() == "csrf",
              f"{r.status} {r.code()}")
    if r.status == 201:
        run.cleanup.add("API key from the CSRF check", run._revoke_key, c, (r.data or {})["api_key"]["id"])
    r = c.request("POST", "/api/keys", {"name": run.name("csrf2")}, headers={"Origin": "https://accept-foreign.example"})
    run.check("an API write from another origin is refused (403 csrf)", r.status == 403 and r.code() == "csrf",
              f"{r.status} {r.code()}")
    if r.status == 201:
        run.cleanup.add("API key from the origin check", run._revoke_key, c, (r.data or {})["api_key"]["id"])
    r = raw(url + "/api/machines", tls=run.tls)
    run.check("the API without a session answers 401", r.status == 401, f"{r.status} {r.code()}")
    r = raw(url + "/v1/models", headers={"Cookie": c.cookie_header()}, tls=run.tls)
    run.check("/v1 does not take the console's session cookie (401)", r.status == 401, f"{r.status} {r.code()}")

    # Sign-in back-off on the run's own account, never on a real one.
    u = run.user()
    run.check("a throwaway user is created, sets a password by link and signs in", True, u["name"])
    r = u["client"].request("GET", "/api/users")
    run.check("a plain user cannot open an admin API (403)", r.status == 403, f"{r.status} {r.code()}")
    anon = Client(url, run.tls)
    codes, wait = [], None
    for _ in range(8):
        r = anon.request("POST", "/api/auth/login", {"username": u["name"], "password": "wrong-" + secrets.token_hex(4)},
                         wait_on_429=False)
        codes.append(r.status)
        if r.status == 429:
            wait = r.headers.get("retry-after")
            break
    run.check("wrong passwords: 401 each, then 429 with Retry-After (the account's back-off)",
              codes[-1] == 429 and wait and all(x == 401 for x in codes[:-1]) and len(codes) >= 3,
              f"{codes}, Retry-After {wait}")
    still = c.request("GET", "/api/auth/me")
    run.check("the admin's own session is not touched by it", still.status == 200, f"{still.status}")
    if wait:
        time.sleep(min(float(wait), 30) + 0.5)
        r = anon.request("POST", "/api/auth/login", {"username": u["name"], "password": u["password"]}, wait_on_429=False)
        run.check("after the wait the right password works (never locked for good)", r.status == 200, f"{r.status} {r.code()}")
    else:
        run.skip("after the wait the right password works", "no back-off was reached")

    g = raw(url + "/api/guide", tls=run.tls)
    langs = [x.get("code") for x in (g.data or {}).get("languages", [])] if g.status == 200 else []
    page = raw(url + "/api/guide/en/how-to", tls=run.tls)
    ui = raw(url + "/guide", tls=run.tls)
    run.check("/guide serves (both languages, without signing in)", g.status == 200 and {"en", "zh-TW"} <= set(langs)
              and page.status == 200 and ui.status == 200 and b"<div" in ui.body,
              f"/api/guide {g.status} {langs}, how-to {page.status}, /guide {ui.status}")
    t = raw(url + "/trust", tls=run.tls)
    pem = raw(url + "/api/tls/ca.pem", tls=run.tls)
    der = raw(url + "/api/tls/ca.crt", tls=run.tls)
    if mode == "forgeyard-ca" or (tls_info.get("ca") or {}).get("fingerprint"):
        run.check("/trust serves, with the CA as PEM and DER", t.status == 200 and pem.status == 200 and
                  "BEGIN CERTIFICATE" in pem.text and der.status == 200 and der.body[:1] == b"\x30",
                  f"/trust {t.status}, ca.pem {pem.status}, ca.crt {der.status}")
    else:
        run.check("/trust serves (no CA to offer with this certificate)", t.status == 200, f"/trust {t.status}, mode {mode}")
    i = raw(url + "/install.sh", tls=run.tls)
    run.check("/install.sh is served over HTTPS", i.status == 200 and i.body.startswith(b"#!/bin/sh"), f"{i.status}")
    ob = c.request("GET", "/api/onboarding")
    steps = (ob.data or {}).get("steps") or []
    run.check("the Get started checklist answers", ob.status == 200 and len(steps) >= 3,
              f"{(ob.data or {}).get('done')} of {len(steps)} steps done")


# ------------------------------------------------------------------ group: machines
@group("machines", "the given machines, their hardware and GPU allowlist")
def group_machines(run: Run) -> None:
    c = run.admin
    listing = c.ok("GET", "/api/machines")
    offline_after = listing.get("offline_after") or 30
    if not run.machines:
        run.skip("machines", "no --gpu-machine, --cpu-machine or --second-cpu-machine given")
    seen_ids: set = set()
    for role, m in list(run.machines.items()):
        if m["id"] in seen_ids:
            continue
        seen_ids.add(m["id"])
        roles = {r for r, x in run.machines.items() if x["id"] == m["id"]}
        listing = c.ok("GET", "/api/machines")
        m = run.fresh_machine(role)
        name = m["name"]
        seen = m.get("last_heartbeat_at")
        age = (utc(listing.get("server_time")) - utc(seen)).total_seconds() if seen and listing.get("server_time") else None
        run.check(f"{name}: online, heartbeat in the last {offline_after} s",
                  m["status"] == "online" and m.get("connected") and age is not None and age < offline_after + 5,
                  f"{m['status']}, last heartbeat {age:.0f} s ago, agent {m.get('agent_version')}"
                  if age is not None else f"{m['status']}, never heard from")
        if m.get("agent_version") and run.version and m["agent_version"] != run.version:
            run.report.notes.append(f"{name}'s agent is {m['agent_version']}, the server {run.version}")
        feats = set(m.get("features") or [])
        need = {"workloads", "models", "replica_keys", "replica_guard"}
        run.check(f"{name}: the agent runs workloads and models behind the key guard", need <= feats,
                  f"missing {sorted(need - feats)}" if need - feats else ", ".join(sorted(feats)))
        gpus = [x for x in m["accelerators"] if x["vendor"] != "cpu"]
        cpu = [x for x in m["accelerators"] if x["vendor"] == "cpu"]
        if "gpu" in roles:
            vendors = sorted({x["vendor"] for x in gpus})
            run.check(f"{name}: the inventory shows its GPUs", bool(gpus) and all(x.get("vram_total") for x in gpus),
                      f"{len(gpus)} × " + ", ".join(sorted({x['model'] or '?' for x in gpus})) + f" ({'/'.join(vendors)}), "
                      + ", ".join(f"#{x['index']} {x['arch'] or ''}" for x in gpus[:8]))
        if roles - {"gpu"}:
            run.check(f"{name}: the inventory shows its CPU and memory", bool(cpu) and bool(cpu[0].get("vram_total")),
                      f"{cpu[0]['model']}, {cpu[0]['vram_total'] / GIB:.1f} GiB" if cpu else "no cpu entry")
        allowed = [x for x in gpus if x.get("allowed")]
        outside = [x for x in gpus if not x.get("allowed")]
        if gpus and outside:
            x = outside[0]
            body = {"name": run.name("refused"), "kind": "task", "machine_id": m["id"],
                    "runtime": {"type": "process", "command": "true"}, "resources": {"gpus": {"indices": [x["index"]]}}}
            r = c.request("POST", "/api/workloads", body)
            if r.status == 201:
                wid = (r.data or {})["workload"]["id"]
                c.request("DELETE", f"/api/workloads/{wid}")
            run.check(f"{name}: a workload asking for GPU #{x['index']} (not allowed) is refused",
                      r.status == 422 and r.code() == "cannot_place", f"{r.status} {((r.data or {}).get('detail') or {}).get('message', '')}")
        elif gpus:
            run.skip(f"{name}: the GPU allowlist is enforced", "every GPU there may be used (no allowlist): nothing to refuse")
        else:
            body = {"name": run.name("refused"), "kind": "task", "machine_id": m["id"],
                    "runtime": {"type": "process", "command": "true"}, "resources": {"gpus": {"indices": [0]}}}
            r = c.request("POST", "/api/workloads", body)
            if r.status == 201:
                c.request("DELETE", f"/api/workloads/{(r.data or {})['workload']['id']}")
            run.check(f"{name}: a workload asking for a GPU on a machine without one is refused",
                      r.status == 422, f"{r.status} {((r.data or {}).get('detail') or {}).get('message', '')}")
        if "gpu" in roles:
            run.report.info.setdefault("gpu_allowlist", {})[name] = (
                "all" if m.get("gpu_allowlist") is None else [f"#{x['index']}" for x in allowed])
    # A join command pins the CA by its fingerprint; the token is cancelled at once.
    t = c.ok("POST", "/api/machines/join-tokens")
    item = run.cleanup.add("join token", delete, c, f"/api/machines/join-tokens/{t['id']}")
    fp = t.get("ca_fingerprint")
    tls_info = c.ok("GET", "/api/tls")
    want = (tls_info.get("ca") or {}).get("fingerprint")
    if want:
        run.check("a join command pins the CA (--ca-fingerprint)", fp == want and f"--ca-fingerprint {want}" in (t.get("command") or ""),
                  f"{(fp or 'none')[:23]}…")
    else:
        run.skip("a join command pins the CA", "the server has a publicly trusted certificate: nothing to pin")
    r = c.request("DELETE", f"/api/machines/join-tokens/{t['id']}")
    item["done"] = True
    run.check("an unused join token can be cancelled", r.status == 200, f"{r.status}")


# ------------------------------------------------------------------ group: workloads
def workload(run: Run, body: dict) -> dict:
    c = run.admin
    w = c.ok("POST", "/api/workloads", body)["workload"]
    run.cleanup.add(f"workload {w['name']}", lambda: _delete_workload(c, w["id"]))
    return w


def _delete_workload(c: Client, wid: int) -> None:
    delete(c, f"/api/workloads/{wid}")


def delete(c: Client, path: str) -> None:
    """DELETE something the run made; gone already is fine."""
    r = c.request("DELETE", path)
    if r.status not in (200, 404):
        raise Fail(f"{r.status or r.error} {r.code()}")


def get_workload(run: Run, wid: int) -> dict:
    return run.admin.ok("GET", f"/api/workloads/{wid}")["workload"]


def replicas_text(w: dict) -> str:
    return "; ".join(f"#{r['ordinal']} {r['state']}" + (f" ({r['reason']})" if r.get("reason") else "")
                     for r in w["replica_list"]) or "no replicas"


def wait_status(run: Run, wid: int, statuses: tuple, timeout: float) -> dict:
    holder = {}

    def poll():
        w = get_workload(run, wid)
        holder["w"] = w
        return w["status"] in statuses, f"{w['status']}: {replicas_text(w)}"

    run.wait(f"workload #{wid}", poll, timeout)
    return holder["w"]


def output_of(run: Run, wid: int, lines: int = 400) -> str:
    r = run.admin.request("GET", f"/api/workloads/{wid}/replicas/0/logs?lines={lines}")
    return (r.data or {}).get("text", "") if r.status == 200 else ""


def task(run: Run, m: dict, what: str, command: str, args: list, **spec) -> tuple[dict, str]:
    body = {"name": run.name(what), "kind": "task", "machine_id": m["id"],
            "runtime": {"type": "process", "command": command, "args": args}, **spec}
    w = workload(run, body)
    w = wait_status(run, w["id"], ("succeeded", "failed"), spec.get("max_duration", 0) + 120)
    time.sleep(1)  # the agent sends the last lines with the final state
    return w, output_of(run, w["id"])


@group("workloads", "services and tasks on the given machines, GPU isolation")
def group_workloads(run: Run) -> None:
    roles = [r for r in ("cpu", "gpu") if run.machine(r)]
    if not roles:
        run.skip("workloads", "no --cpu-machine or --gpu-machine given")
        return
    if len(roles) == 2 and run.machine("cpu")["id"] == run.machine("gpu")["id"]:
        roles = ["gpu"]  # one machine in both roles: once
    for role in roles:
        m = run.fresh_machine(role)
        if m["status"] != "online":
            run.skip(f"{m['name']}: workloads", f"the machine is {m['status']}")
            continue
        workloads_on(run, role, m)
    if run.machine("gpu") and run.machine("gpu")["status"] == "online":
        gpu_isolation(run, run.machine("gpu"))
    else:
        run.skip("GPU isolation", "no --gpu-machine given (or it is offline)")


def workloads_on(run: Run, role: str, m: dict) -> None:
    c, name, t = run.admin, m["name"], role[0]
    info = m.get("runtime_info") or {}
    if not (info.get("process") or {}).get("ok"):
        run.skip(f"{name}: process workloads", f"no process runtime: {(info.get('process') or {}).get('reason')}")
    else:
        # A service: python3's http.server on the assigned port (loopback only), health-checked over HTTP.
        body = {"name": run.name(f"svc-{t}"), "kind": "service", "machine_id": m["id"],
                "runtime": {"type": "process", "command": "python3",
                            "args": ["-m", "http.server", "--bind", "127.0.0.1", "${PORT}"]},
                "ports": [{"name": "http"}], "health": {"type": "http", "path": "/", "interval": 2},
                "restart": "always"}
        w = workload(run, body)
        t0 = time.monotonic()
        try:
            w = wait_status(run, w["id"], ("running", "failed"), 120)
            rep = w["replica_list"][0]
            run.check(f"{name}: a process service starts and becomes healthy", w["status"] == "running" and rep["state"] == "healthy",
                      f"{rep['state']} after {time.monotonic() - t0:.0f} s, pid {rep.get('pid')}, port "
                      f"{(rep.get('ports') or [{}])[0].get('host_port')}")
            time.sleep(3)
            out = output_of(run, w["id"])
            run.check(f"{name}: its output is readable (the health checks' requests)", "GET / HTTP" in out,
                      f"{len(out.splitlines())} lines" if out else "no output")
            c.ok("POST", f"/api/workloads/{w['id']}/stop")
            w2 = wait_status(run, w["id"], ("stopped",), 60)
            run.check(f"{name}: Stop stops it", all(r["state"] in ("stopped", "exited") for r in w2["replica_list"]),
                      replicas_text(w2))
            c.ok("POST", f"/api/workloads/{w['id']}/start")
            w3 = wait_status(run, w["id"], ("running", "failed"), 120)
            run.check(f"{name}: Start runs it again (a new generation)", w3["status"] == "running"
                      and w3["generation"] == w2["generation"] + 1, f"generation {w3['generation']}, {replicas_text(w3)}")
        except Fail as e:
            run.check(f"{name}: the process service", False, str(e))
        # Tasks: one that succeeds (and shows its environment), one that retries, one killed at max_duration.
        w, out = task(run, m, f"ok-{t}", "sh", ["-c", "echo accept-hello; echo user=$(id -un); env | sort"])
        env = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in out.splitlines() if "=" in ln and not ln.startswith("user=")}
        leaked = sorted(k for k in env if k.startswith(("SUDO_", "SSH_", "FORGEYARD_PASSWORD", "OPENAI_", "HF_TOKEN")))
        user = next((ln[5:] for ln in out.splitlines() if ln.startswith("user=")), "?")
        run.check(f"{name}: a task runs to the end (succeeded)", w["status"] == "succeeded" and "accept-hello" in out,
                  f"{w['status']}, exit {w['replica_list'][0].get('exit_code')}")
        run.check(f"{name}: it runs as the unprivileged workload account, from an empty environment",
                  user not in ("root", "?") and not leaked and env.get("NVIDIA_VISIBLE_DEVICES") == "void",
                  f"user {user}, {len(env)} variables, NVIDIA_VISIBLE_DEVICES={env.get('NVIDIA_VISIBLE_DEVICES')}"
                  + (f", leaked {leaked}" if leaked else ""))
        w, out = task(run, m, f"retry-{t}", "sh", ["-c", "echo accept-try; exit 3"], retries=2, retry_backoff=1)
        rep = w["replica_list"][0]
        run.check(f"{name}: a failing task is retried, then fails (attempt 3 of 3)",
                  w["status"] == "failed" and rep.get("attempt") == 3 and out.count("accept-try") == 3,
                  f"{w['status']}: {rep.get('reason')}; {out.count('accept-try')} tries in the output")
        w, out = task(run, m, f"max-{t}", "sleep", ["600"], max_duration=5)
        rep = w["replica_list"][0]
        run.check(f"{name}: a task over max_duration is stopped and fails", w["status"] == "failed"
                  and "max_duration" in (rep.get("reason") or ""), f"{w['status']}: {rep.get('reason')}")
    cinfo = info.get("container") or {}
    if not cinfo.get("ok"):
        run.skip(f"{name}: a container workload", f"no container engine there ({cinfo.get('reason') or 'not available'})")
        return
    body = {"name": run.name(f"ctr-{t}"), "kind": "task", "machine_id": m["id"],
            "runtime": {"type": "container", "image": CONTAINER_IMAGE, "command": ["sh", "-c"],
                        "args": ["echo accept-container; ls /dev | grep -ci nvidia || true"]}}
    w = workload(run, body)
    try:
        w = wait_status(run, w["id"], ("succeeded", "failed"), 600)
        time.sleep(1)
        out = output_of(run, w["id"])
        run.check(f"{name}: a container task runs ({CONTAINER_IMAGE})", w["status"] == "succeeded" and "accept-container" in out,
                  f"{w['status']}: {replicas_text(w)}")
    except Fail as e:
        run.check(f"{name}: a container task runs ({CONTAINER_IMAGE})", False, str(e))


def gpu_isolation(run: Run, m: dict) -> None:
    m = run.fresh_machine("gpu")
    name = m["name"]
    gpus = [x for x in m["accelerators"] if x["vendor"] != "cpu" and x.get("allowed")]
    if not gpus:
        run.skip(f"{name}: GPU isolation", "no GPU there may be used by Forgeyard")
        return
    vendor = gpus[0]["vendor"]
    by_key = {x["key"]: x for x in m["accelerators"]}
    gpu_spec = {"gpus": {"count": 1, "vendor": vendor, "share": "vram", "vram": "1Gi"}}
    if vendor == "nvidia":
        w, out = task(run, m, "gpu-probe", "python3", ["-c", probe_source(), "--expect", "1", "--gib", "0.25", "--hold", "2"],
                      resources=gpu_spec)
        card = by_key.get(((w["replica_list"][0].get("gpus") or [{}])[0]).get("key"), {})
        seen = [ln for ln in out.splitlines() if ln.startswith("device ")]
        run.check(f"{name}: a process sees only its card (gpu_probe.py: one device, the assigned one)",
                  w["status"] == "succeeded" and "visible devices: 1" in out and len(seen) == 1
                  and card.get("uuid", "?") in seen[0] and card.get("allowed"),
                  f"{w['status']}, given #{card.get('index')}, {seen[0][:60] if seen else out[-160:]}")
    else:
        w, out = task(run, m, "gpu-env", "sh", ["-c", "echo ROCR=$ROCR_VISIBLE_DEVICES; echo HIP=$HIP_VISIBLE_DEVICES"],
                      resources=gpu_spec)
        card = by_key.get(((w["replica_list"][0].get("gpus") or [{}])[0]).get("key"), {})
        rocr = next((ln[5:] for ln in out.splitlines() if ln.startswith("ROCR=")), "")
        run.check(f"{name}: a process is given only its card (ROCR_VISIBLE_DEVICES)",
                  w["status"] == "succeeded" and rocr and "," not in rocr and card.get("allowed"),
                  f"{w['status']}, given #{card.get('index')}, ROCR_VISIBLE_DEVICES={rocr}")
    cinfo = (m.get("runtime_info") or {}).get("container") or {}
    if vendor != "nvidia":
        run.skip(f"{name}: a container sees only its card", "checked on NVIDIA only (no small AMD image to probe with)")
    elif not cinfo.get("ok") or not cinfo.get("nvidia"):
        run.skip(f"{name}: a container sees only its card", "its container engine cannot pass NVIDIA GPUs (or there is none)")
    else:
        body = {"name": run.name("gpu-ctr"), "kind": "task", "machine_id": m["id"],
                "runtime": {"type": "container", "image": CUDA_IMAGE, "command": ["nvidia-smi"], "args": ["-L"]},
                "resources": gpu_spec}
        w = workload(run, body)
        try:
            w = wait_status(run, w["id"], ("succeeded", "failed"), 900)
            time.sleep(1)
            out = output_of(run, w["id"])
            card = by_key.get(((w["replica_list"][0].get("gpus") or [{}])[0]).get("key"), {})
            lines = [ln for ln in out.splitlines() if ln.startswith("GPU ")]
            run.check(f"{name}: a container sees only its card (nvidia-smi -L)", w["status"] == "succeeded"
                      and len(lines) == 1 and card.get("uuid", "?") in lines[0],
                      f"{w['status']}, given #{card.get('index')}, {len(lines)} GPU line(s)")
        except Fail as e:
            run.check(f"{name}: a container sees only its card (nvidia-smi -L)", False, str(e))


# ------------------------------------------------------------------ group: models
def ensure_model(run: Run, spec: dict) -> None:
    c = run.admin
    if spec["model"] in {x["name"] for x in c.ok("GET", "/api/models")["models"]}:
        return
    body = {"name": spec["model"], "source": spec["source"], "task": spec["task"], "title": spec["model"]}
    if spec.get("file"):
        body["file"] = spec["file"]
    if spec.get("engine_args"):
        body["engine_args"] = spec["engine_args"]
    m = c.ok("POST", "/api/models", body, timeout=300)["model"]
    log(f"catalog: added {m['name']} ({m['source']}, {m['size_bytes'] / GIB:.2f} GiB)")
    run.cleanup.add(f"catalog entry {m['name']}", _remove_model, c, m["name"])


def _remove_model(c: Client, name: str) -> None:
    mid = next((x["id"] for x in c.ok("GET", "/api/models")["models"] if x["name"] == name), None)
    if mid is not None:
        r = c.request("DELETE", f"/api/models/{mid}")
        if r.status not in (200, 404):
            raise Fail(f"{r.status} {r.code()}")


def deploy(run: Run, key: str, spec: dict, m: dict) -> Optional[dict]:
    """Estimate, then deploy `spec` on machine `m` as accept-<run>-<key>; None (with a SKIP or FAIL) when
    it does not fit or is refused."""
    c = run.admin
    name = run.name(key)
    body = {"name": name, "model": spec["model"], "recipe": spec["recipe"], "params": spec["params"],
            "served_name": name, "replicas": 1, "machine_id": m["id"]}
    est = c.ok("POST", "/api/deployments/estimate", body)
    plan = est["placement"]
    amount = est["estimate"]["amount"]
    if not plan["fits"]:
        run.skip(f"{name} ({spec['model']}): deploy", f"the estimate ({amount / GIB:.2f} GiB) does not fit on {m['name']}: "
                 f"{plan.get('summary')}")
        return None
    p = plan["placed"][0]
    where = f"{p['machine']}" + (f" GPU {p['gpus']}" if p.get("gpus") else " CPU")
    if spec["device"] == "gpu":
        allowed = {x["index"] for x in m["accelerators"] if x["vendor"] != "cpu" and x.get("allowed")}
        if not p.get("gpus") or not set(p["gpus"]) <= allowed:
            run.check(f"{name}: the estimate places it on an allowed GPU", False, where)
            return None
    r = c.request("POST", "/api/deployments", body)
    if r.status != 201:
        run.check(f"{name} ({spec['model']}): deploy", False, f"{r.status} {r.code()}: {(r.data or {}).get('detail')}")
        return None
    d = r.data["deployment"]
    run.cleanup.add(f"deployment {name}", _delete_deployment, c, d["id"])
    log(f"{name}: deployed on {where}, estimate {amount / GIB:.2f} GiB ({p.get('variant')})")
    return {"spec": spec, "deployment": d, "estimate": est, "where": where, "t0": time.monotonic()}


def _delete_deployment(c: Client, dep_id: int) -> None:
    r = c.request("DELETE", f"/api/deployments/{dep_id}")
    if r.status not in (200, 404):
        raise Fail(f"{r.status} {r.code()}")


def deployment_state(run: Run, dep_id: int) -> tuple[bool, str, dict]:
    d = run.admin.ok("GET", f"/api/deployments/{dep_id}")["deployment"]
    parts = []
    for r in d["workload"]["replica_list"]:
        dl = r.get("download") or {}
        text = f"#{r['ordinal']} {r['machine']['name'] if r.get('machine') else '-'} {r['state']}"
        if dl:
            what = "engine" if dl.get("phase") == "engine" else "model"
            text += (f" downloading the {what} {dl.get('done', 0) / GIB:.2f}/{dl['total'] / GIB:.2f} GiB" if dl.get("total")
                     else f" downloading the {what} {dl.get('done', 0) / GIB:.2f} GiB")
        elif r.get("reason"):
            text += f" ({r['reason'][:120]})"
        parts.append(text)
        if r.get("final") and r["state"] in ("failed", "exited"):
            raise Fail(f"{d['name']} replica {r['ordinal']} ended: {r.get('reason')}")
    ready = bool(d["workload"]["replica_list"]) and all(r["state"] == "healthy" for r in d["workload"]["replica_list"])
    return ready, "; ".join(parts), d


@group("models", "deploying the models, the estimate, Try it")
def group_models(run: Run) -> None:
    c = run.admin
    rec = c.request("GET", "/api/models/recommended")
    run.check("the recommended models list loads", rec.status == 200 and len((rec.data or {}).get("models") or []) >= 10,
              f"{len((rec.data or {}).get('models') or [])} models")
    s = c.request("GET", "/api/models/search?q=qwen3&task=chat&limit=5")
    if s.status == 503 and s.code() == "hub_unreachable":
        run.skip("Hugging Face search through the server", "the server cannot reach Hugging Face")
    else:
        run.check("Hugging Face search through the server", s.status == 200 and bool((s.data or {}).get("results")),
                  f"{s.status}, {len((s.data or {}).get('results') or [])} results")
    if run.machine("cpu") and run.machine("cpu2"):
        spec = MODELS[0]
        ensure_model(run, spec)
        body = {"name": run.name("spread"), "model": spec["model"], "recipe": spec["recipe"], "params": spec["params"],
                "replicas": 2}
        est = c.ok("POST", "/api/deployments/estimate", body)["placement"]
        where = [p["machine"] for p in est.get("placed") or []]
        run.check("two replicas of one deployment are spread over two machines (estimate)",
                  est["fits"] and len(set(where)) == 2, ", ".join(where) or est.get("summary"))
    started: dict[str, dict] = {}
    for spec in MODELS:
        roles = ["cpu", "cpu2"] if spec["device"] == "cpu" else ["gpu"]
        for role in roles:
            m = run.machine(role)
            key = spec["key"] + ("2" if role == "cpu2" else "")
            if m is None:
                run.skip(f"{spec['key']} ({spec['model']})" if role != "cpu2" else f"{key}: a second copy for balancing",
                         f"no --{'gpu-machine' if role == 'gpu' else 'cpu-machine' if role == 'cpu' else 'second-cpu-machine'}"
                         " given")
                continue
            m = run.fresh_machine(role)
            if m["status"] != "online":
                run.skip(f"{key} on {m['name']}", f"the machine is {m['status']}")
                continue
            ensure_model(run, spec)
            got = deploy(run, key, spec, m)
            if got:
                started[key] = got
    for key, got in started.items():
        d0 = got["deployment"]
        name = d0["name"]
        try:
            holder = {}

            def poll(dep_id=d0["id"]):
                ready, text, d = deployment_state(run, dep_id)
                holder["d"] = d
                return ready, text

            run.wait(name, poll, run.args.timeout, every=5)
            d = holder["d"]
        except Fail as e:
            run.check(f"{name}: ready on {got['where']}", False, str(e))
            continue
        secs = time.monotonic() - got["t0"]
        run.check(f"{name}: ready on {got['where']}", True, f"after {fmt_seconds(secs)}")
        detail = c.ok("GET", f"/api/deployments/{d['id']}")["deployment"]
        est = got["estimate"]["estimate"]["amount"]
        shown = (detail.get("estimate") or {}).get("amount")
        reserved = [x.get("reserved") for x in detail.get("cards") or []]
        dev = got["spec"]["device"]
        ok = shown == est and (dev != "gpu" or (reserved and all(x == est for x in reserved)))
        live = ", ".join(f"{(x.get('used') or 0) / GIB:.1f} GiB used of {(x.get('total') or 0) / GIB:.0f}"
                         for x in detail.get("cards") or [])
        run.check(f"{name}: the deployment page shows the estimate ({est / GIB:.2f} GiB)", ok,
                  f"page {(shown or 0) / GIB:.2f} GiB, reserved {[round((x or 0) / GIB, 2) for x in reserved]}; live: {live}")
        t = c.request("POST", f"/api/deployments/{d['id']}/try", {}, timeout=200)
        run.check(f"{name}: Try it answers", t.status == 200 and (t.data or {}).get("ok"),
                  f"{(t.data or {}).get('ms')} ms: {str((t.data or {}).get('summary'))[:100]}")
        run.deps[key] = detail
    run.models_done = True
    run.report.info["deployments"] = {k: v["name"] for k, v in run.deps.items()}


# ------------------------------------------------------------------ group: gateway
def chat_once(sdk, model: str, max_tokens: int = 4, text: str = "Hi") -> tuple[object, str]:
    raw_ = sdk.chat.completions.with_raw_response.create(model=model, max_tokens=max_tokens, extra_body=NO_THINK,
                                                         messages=[{"role": "user", "content": text}])
    return raw_.parse(), target_of(raw_.headers)


def gw_status(fn) -> str:
    """'200' or '<status> <code>' of one SDK call."""
    import openai

    try:
        fn()
        return "200"
    except openai.APIStatusError as e:
        body = e.body if isinstance(e.body, dict) else {}
        code = body.get("code") or ((body.get("error") or {}).get("code") if isinstance(body.get("error"), dict) else None)
        return f"{e.status_code} {code or ''}".strip()


def engine_pids(pid: int, depth: int = 3) -> list[int]:
    """`pid` and its descendants on this computer."""
    out, level = [pid], [pid]
    for _ in range(depth):
        nxt = []
        for e in os.listdir("/proc"):
            if e.isdigit():
                try:
                    stat = Path(f"/proc/{e}/stat").read_text().rsplit(")", 1)[1].split()
                except OSError:
                    continue
                if int(stat[1]) in level:
                    nxt.append(int(e))
        out += nxt
        level = nxt
    return out


def local_engine(pid: Optional[int], replica_uid: str, program: str = "llama-server") -> Optional[int]:
    """The engine process of a replica, if it runs on THIS computer: a process under `pid` whose
    command line is `program` and whose environment (when readable) names the replica. Never a
    process that merely has the same pid on this computer."""
    if not pid or not Path(f"/proc/{pid}").exists():
        return None
    for p in engine_pids(pid):
        try:
            cmd = Path(f"/proc/{p}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not cmd or program.encode() not in Path(cmd[0].decode(errors="replace")).name.encode():
            continue
        try:
            env = Path(f"/proc/{p}/environ").read_bytes()
            if replica_uid and f"FORGEYARD_REPLICA={replica_uid}".encode() not in env:
                continue
        except OSError:
            pass
        return p
    return None


@group("gateway", "/v1 with the official SDK, keys, limits, key guard, balancing, Playground, usage",
       needs=("models",))
def group_gateway(run: Run) -> None:
    import openai

    c, deps = run.admin, run.deps
    if not deps:
        run.skip("gateway", "no deployment is ready (see the models group)")
        return
    served = {k: d["served_name"] for k, d in deps.items()}
    k_all = run.new_key("gw")
    sdk = run.sdk(k_all["key"])
    listed = sorted(x.id for x in sdk.models.list())
    run.check("GET /v1/models lists every ready deployment", all(n in listed for n in served.values()),
              ", ".join(n for n in listed if n.startswith(run.name(""))))
    chats = [k for k in ("chat", "cpu") if k in deps]
    for key in chats:
        model = served[key]
        t0 = time.time()
        out, target = chat_once(sdk, model, 48, "Name three primary colours.")
        text = (out.choices[0].message.content or "").strip()
        run.check(f"{model}: chat", bool(text) and target.startswith("local:"),
                  f"{time.time() - t0:.1f} s, {out.usage.prompt_tokens}+{out.usage.completion_tokens} tokens, {target}: {text[:60]!r}")
        chunks, usage_chunks, pieces = 0, 0, []
        for ch in sdk.chat.completions.create(model=model, max_tokens=32, stream=True, extra_body=NO_THINK,
                                              messages=[{"role": "user", "content": "Count from 1 to 10."}]):
            chunks += 1
            usage_chunks += 1 if getattr(ch, "usage", None) else 0
            if ch.choices and ch.choices[0].delta.content:
                pieces.append(ch.choices[0].delta.content)
        run.check(f"{model}: streamed chat, no usage chunk unless asked", chunks > 3 and pieces and usage_chunks == 0,
                  f"{chunks} chunks, {usage_chunks} with usage: {''.join(pieces).strip()[:50]!r}")
        chunks, usage = 0, None
        for ch in sdk.chat.completions.create(model=model, max_tokens=32, stream=True, extra_body=NO_THINK,
                                              stream_options={"include_usage": True},
                                              messages=[{"role": "user", "content": "Count from 1 to 10."}]):
            chunks += 1
            if getattr(ch, "usage", None):
                usage = ch.usage
        run.check(f"{model}: streamed chat with include_usage ends with the usage", usage is not None and usage.completion_tokens > 0,
                  f"{chunks} chunks, usage {usage.prompt_tokens}+{usage.completion_tokens}" if usage else f"{chunks} chunks, no usage")
        comp = sdk.completions.create(model=model, prompt="The capital of France is", max_tokens=8)
        run.check(f"{model}: /v1/completions", bool(comp.choices and comp.choices[0].text), repr(comp.choices[0].text[:40]))
    if "embed" in deps:
        e = sdk.embeddings.create(model=served["embed"], input=["Forgeyard runs models on your machines.", "GPUs."])
        run.check(f"{served['embed']}: embeddings", len(e.data) == 2 and len(e.data[0].embedding) > 8,
                  f"{len(e.data[0].embedding)} dimensions")
    else:
        run.skip("embeddings", "no embedding deployment (needs --gpu-machine with room)")
    if "rerank" in deps:
        docs = ["The Eiffel Tower was completed in 1889.", "Paris is the capital and largest city of France.",
                "Berlin is the capital of Germany."]
        r = raw(run.args.server + "/v1/rerank", "POST", json.dumps({"model": served["rerank"], "documents": docs,
                                                                     "query": "What is the capital of France?"}).encode(),
                {"Authorization": f"Bearer {k_all['key']}", "Content-Type": "application/json"}, run.tls, 120)
        ranked = sorted((r.data or {}).get("results") or [], key=lambda x: -x["relevance_score"]) if r.status == 200 else []
        run.check(f"{served['rerank']}: /v1/rerank puts the right document first", ranked and ranked[0]["index"] == 1,
                  f"{r.status}: {[(x['index'], round(x['relevance_score'], 3)) for x in ranked]}")
    else:
        run.skip("rerank", "no rerank deployment (needs --gpu-machine with room)")
    if "asr" in deps:
        with CLIP.open("rb") as fh:
            tr = sdk.audio.transcriptions.create(model=served["asr"], file=fh)
        run.check(f"{served['asr']}: transcription", "ask not" in tr.text.lower(), repr(tr.text.strip()[:80]))
    else:
        run.skip("transcription", "no speech-to-text deployment (needs --gpu-machine with room)")

    # Key allowlists, limits, revoking.
    if not chats:
        run.skip("key allowlists, rpm, revoking, the Playground", "they call a chat model, and none is ready")
        guard_checks(run, k_all["key"])
        stopped_leaves_list(run, sdk)
        return
    first = served[chats[0]]
    k1 = run.new_key("one", models=[first])
    one = run.sdk(k1["key"])
    only = sorted(x.id for x in one.models.list())
    run.check(f"a key limited to {first} lists only it", only == [first], ", ".join(only))
    run.check(f"a key limited to {first} reaches it", gw_status(lambda: chat_once(one, first)) == "200")
    others = [n for n in served.values() if n != first]
    if others:
        got = {n: gw_status(lambda n=n: one.chat.completions.create(model=n, max_tokens=2, messages=[{"role": "user", "content": "x"}]))
               for n in others}
        run.check("…and gets 403 model_not_allowed for the others", all(v == "403 model_not_allowed" for v in got.values()),
                  json.dumps(got))
    else:
        run.skip("a limited key is refused other models", "only one deployment is ready")
    k3 = run.new_key("rpm3", rpm=3)
    s3 = run.sdk(k3["key"])
    codes, retry = [], None
    for _ in range(4):
        try:
            s3.chat.completions.create(model=first, max_tokens=2, extra_body=NO_THINK, messages=[{"role": "user", "content": "hi"}])
            codes.append(200)
        except openai.RateLimitError as e:
            codes.append(429)
            retry = e.response.headers.get("retry-after")
    run.check("rpm 3: the 4th call gets 429 with Retry-After", codes == [200, 200, 200, 429] and retry, f"{codes}, Retry-After {retry}")
    c.ok("DELETE", f"/api/keys/{k3['api_key']['id']}")
    run.check("a revoked key gets 401 on the very next call", gw_status(lambda: s3.models.list()).startswith("401"),
              gw_status(lambda: s3.models.list()))
    exp = run.new_key("expires", expires_in_days=1)
    run.check("a key with an expiry works until then", exp["api_key"].get("expires_at") and
              gw_status(lambda: run.sdk(exp["key"]).models.list()) == "200", f"expires {exp['api_key'].get('expires_at')}")
    run.skip("an expired key gets 401", "the API's shortest expiry is one day; covered by the test suite (FY-GW-007)")
    u = run.user()
    uk = run.new_key("mine", client=u["client"])
    c.ok("PATCH", f"/api/keys/{uk['api_key']['id']}", {"rpm": 1000})
    r = u["client"].request("PATCH", f"/api/keys/{uk['api_key']['id']}", {"rpm": 999})
    run.check("an admin's limit on someone's key is locked for its owner (403 limit_locked)",
              r.status == 403 and r.code() == "limit_locked", f"{r.status} {r.code()}")
    r = u["client"].request("DELETE", f"/api/keys/{uk['api_key']['id']}")
    run.check("…who can still revoke the key", r.status == 200 and (r.data or {}).get("api_key", {}).get("status") == "revoked",
              f"{r.status}")

    # The replicas cannot be used around the gateway.
    guard_checks(run, k_all["key"])
    balancing(run, sdk)

    # The Playground, as the signed-in person (no API key).
    pmodel = first
    for who, client in (("an admin", c), ("a plain user", u["client"])):
        r = client.request("POST", "/api/playground/chat/completions",
                           {"model": pmodel, "max_tokens": 16, "messages": [{"role": "user", "content": "Say hello."}], **NO_THINK})
        text = (((r.data or {}).get("choices") or [{}])[0].get("message") or {}).get("content") if r.status == 200 else None
        run.check(f"the Playground answers {who} (no API key)", r.status == 200 and text is not None,
                  f"{r.status} {target_of(r.headers)}: {str(text)[:40]!r}")

    # Usage rows.
    time.sleep(1)
    rows = {x["name"]: x for x in c.ok("GET", "/api/usage?days=1&group=model&everyone=true")["rows"]}
    missing = [n for n in served.values() if not rows.get(n, {}).get("requests")]
    run.check("usage: a row per model with requests and tokens", not missing,
              ", ".join(f"{n} {rows[n]['requests']} req" for n in served.values() if n in rows) + (f"; missing {missing}" if missing else ""))
    by_key = c.ok("GET", "/api/usage?days=1&group=key&everyone=true")["rows"]
    ours = [x for x in by_key if x["name"].startswith(run.name(""))]
    limited = sum(x.get("limited") or 0 for x in ours)
    run.check("usage: rows per key, with the refused (limited) request", len(ours) >= 3 and limited >= 1,
              f"{len(ours)} keys, {limited} limited")
    src = {x["id"]: x for x in c.ok("GET", "/api/usage?days=1&group=source&everyone=true")["rows"]}
    run.check("usage: the Playground counts as console", (src.get("console") or {}).get("requests", 0) >= 2,
              ", ".join(f"{v['name']} {v['requests']}" for v in src.values()))

    stopped_leaves_list(run, sdk)


def guard_checks(run: Run, api_key: str) -> None:
    probes = {"openai-chat": ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2}),
              "openai-embeddings": ("/v1/embeddings", {"input": "x"}),
              "rerank": ("/v1/rerank", {"query": "q", "documents": ["a"]})}
    open_paths = (("/invocations", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2}),
                  ("/rerank", {"query": "q", "documents": ["a"]}), ("/pooling", {"input": "x"}))
    unreachable = []
    for key, d in run.deps.items():
        api = (d.get("route") or {}).get("api") or ({"asr": "openai-audio", "embed": "openai-embeddings",
                                                      "rerank": "rerank"}.get(key, "openai-chat"))
        for e in d["endpoints"]:
            base = e["url"].rstrip("/")
            what = f"{d['name']} #{e['replica']} on {e['machine']}"
            if api == "openai-audio":
                body, ctype = multipart({"model": d["route"]["upstream_model"]}, "file", CLIP)
                r = raw(base + "/v1/audio/transcriptions", "POST", body, {"Content-Type": ctype}, None, 30)
            else:
                path, payload = probes[api]
                r = raw(base + path, "POST", json.dumps({"model": d["route"]["upstream_model"], **payload}).encode(),
                        {"Content-Type": "application/json"}, None, 30)
            if r.status == 0:
                unreachable.append(f"{what} ({r.error})")
                continue
            run.check(f"{what}: a direct call without the internal key gets 401 from the key guard",
                      r.status == 401 and "replica_key_required" in r.text, f"{r.status} {r.text[:60]}")
            r = raw(base + "/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
            run.check(f"{what}: a client's API key does not open the replica's port", r.status == 401, f"{r.status}")
            if d["recipe"].startswith("vllm"):
                bad = {}
                for path, payload in open_paths:
                    r = raw(base + path, "POST", json.dumps({"model": d["route"]["upstream_model"], **payload}).encode(),
                            {"Content-Type": "application/json"}, None, 30)
                    if not (r.status == 401 and "replica_key_required" in r.text):
                        bad[path] = r.status
                run.check(f"{what}: vLLM's open paths (/invocations, /rerank, /pooling) are guarded", not bad,
                          f"not 401: {bad}" if bad else "401 on all three")
    if unreachable:
        run.skip("direct calls to some replica ports", "not reachable from this computer (another network): "
                 + "; ".join(unreachable))


def balancing(run: Run, sdk) -> None:
    """Balancing and failover between the CPU model on two machines: a route of its own with both
    deployments in one tier."""
    import openai

    if not ("cpu" in run.deps and "cpu2" in run.deps):
        run.skip("balancing between two CPU machines", "needs --cpu-machine and --second-cpu-machine with the CPU model ready")
        run.skip("failover when an engine dies", "needs two CPU machines")
        return
    c = run.admin
    a, b = run.deps["cpu"], run.deps["cpu2"]
    name = run.name("cpu-pair")
    route = c.ok("POST", "/api/routes", {"served_name": name, "api": "openai-chat", "targets": [
        {"tier": 0, "kind": "local", "deployment_id": a["id"]}, {"tier": 0, "kind": "local", "deployment_id": b["id"]}]})["route"]
    run.cleanup.add(f"route {name}", delete, c, f"/api/routes/{route['id']}")
    machine_of = {a["name"]: a["endpoints"][0]["machine"], b["name"]: b["endpoints"][0]["machine"]}
    seen = []
    for _ in range(8):
        _, target = chat_once(sdk, name)
        seen.append(machine_of.get(target.removeprefix("local:").split("#")[0], target))
    run.check("one request at a time: the two CPU machines take turns", len(set(seen)) == 2
              and all(x != y for x, y in zip(seen, seen[1:])), " ".join(seen))
    counts: collections.Counter = collections.Counter()
    lock = threading.Lock()
    errors = []

    def one():
        try:
            _, target = chat_once(sdk, name, 48, "Tell me one fact about the sea.")
            with lock:
                counts[machine_of.get(target.removeprefix("local:").split("#")[0], target)] += 1
        except openai.APIError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=one) for _ in range(10)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    run.check("ten requests at once: both machines answer", len(counts) == 2 and not errors,
              f"{dict(counts)} in {time.time() - t0:.1f} s" + (f", errors {errors[:2]}" if errors else ""))
    if not run.args.kill_local:
        run.skip("failover when an engine dies", "only with --kill-local, run on one of the two CPU machines")
        return
    victim = None
    for d in (b, a):
        rep = c.ok("GET", f"/api/deployments/{d['id']}")["deployment"]["workload"]["replica_list"][0]
        pid = local_engine(rep.get("pid"), rep.get("id"))
        if pid:
            victim = (d, rep, pid)
            break
    if victim is None:
        run.skip("failover when an engine dies", "neither CPU replica's engine runs on this computer")
        return
    d, rep, pid = victim
    os.kill(pid, signal.SIGKILL)
    served_by, errs = [], 0
    t0 = time.time()
    for _ in range(6):
        try:
            _, target = chat_once(sdk, name)
            served_by.append(machine_of.get(target.removeprefix("local:").split("#")[0], target))
        except openai.APIError:
            errs += 1
    run.check(f"engine on {rep['machine']['name']} killed: every request is still answered", errs == 0,
              f"{served_by} within {time.time() - t0:.1f} s")
    holder = {}

    def back():
        r2 = c.ok("GET", f"/api/deployments/{d['id']}")["deployment"]["workload"]["replica_list"][0]
        holder["r"] = r2
        moved = r2.get("restarts", 0) > rep.get("restarts", 0) or r2.get("pid") != rep.get("pid")
        return r2["state"] == "healthy" and moved, f"{r2['state']}, restarts {r2.get('restarts')}, pid {r2.get('pid')}"

    try:
        run.wait(f"{d['name']} after the kill", back, 300, every=2)
        run.check(f"the agent on {rep['machine']['name']} restarts the engine", True,
                  f"after {time.time() - t0:.0f} s, restarts {holder['r'].get('restarts')}")
    except Fail as e:
        run.check(f"the agent on {rep['machine']['name']} restarts the engine", False, str(e))


def stopped_leaves_list(run: Run, sdk) -> None:
    """Stop one deployment (the quickest to start again): it leaves /v1/models, answers 503, comes back."""
    import openai

    c = run.admin
    key = next((k for k in ("cpu2", "asr", "embed", "rerank", "cpu", "chat") if k in run.deps), None)
    d = run.deps[key]
    name = d["served_name"]
    c.ok("POST", f"/api/deployments/{d['id']}/stop")
    try:
        run.wait(f"{d['name']} stopping", lambda: (all(r["state"] in ("stopped", "exited", "failed") for r in
                                                       c.ok("GET", f"/api/deployments/{d['id']}")["deployment"]["workload"]
                                                       ["replica_list"]), "stopping"), 120)
        listed = [x.id for x in sdk.models.list()]
        one = gw_status(lambda: sdk.models.retrieve(name))
        call = "-"
        try:
            if key in ("cpu", "cpu2", "chat"):
                sdk.chat.completions.create(model=name, max_tokens=2, messages=[{"role": "user", "content": "x"}])
                call = "200"
            elif key == "embed":
                sdk.embeddings.create(model=name, input="x")
                call = "200"
        except openai.APIStatusError as e:
            call = str(e.status_code)
        run.check(f"a stopped deployment leaves /v1/models ({name})", name not in listed and one.startswith("404")
                  and call in ("503", "-"), f"listed: {name in listed}, GET /v1/models/{name}: {one}, a call: {call}")
    finally:
        c.ok("POST", f"/api/deployments/{d['id']}/start")
    try:
        run.wait(f"{d['name']} started again", lambda: deployment_state(run, d["id"])[:2], run.args.timeout, every=3)
        listed = [x.id for x in sdk.models.list()]
        run.check(f"…and is listed again once started ({name})", name in listed, "")
        run.deps[key] = c.ok("GET", f"/api/deployments/{d['id']}")["deployment"]
    except Fail as e:
        run.check(f"…and is listed again once started ({name})", False, str(e))
        run.deps.pop(key, None)


# ------------------------------------------------------------------ group: cloud
def route_of(run: Run, served_name: str) -> dict:
    return next(r for r in run.admin.ok("GET", "/api/routes")["routes"] if r["served_name"] == served_name)


def post_chat(run: Run, key: str, model: str, max_tokens: int, text: str, headers: Optional[dict] = None) -> Resp:
    body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": text}], **NO_THINK}
    return raw(run.args.server + "/v1/chat/completions", "POST", json.dumps(body).encode(),
               {"Authorization": f"Bearer {key}", "Content-Type": "application/json", **(headers or {})}, run.tls, 300)


def audit_budget_events(run: Run, scope: str, name: str) -> list[dict]:
    events = run.admin.ok("GET", "/api/audit?action=budget.&limit=200")["events"]
    return [e for e in events if (e.get("detail") or {}).get("scope") == scope and (e.get("detail") or {}).get("name") == name]


@group("cloud", "spilling, cloud settings, cost and budgets", needs=("models",))
def group_cloud(run: Run) -> None:
    a, c = run.args, run.admin
    if not a.cloud_provider:
        run.skip("cloud", "no --cloud-provider given")
        return
    local_key = next((k for k in ("cpu", "chat") if k in run.deps), None)
    if local_key is None:
        run.skip("cloud", "no local chat deployment is ready to spill from (see the models group)")
        return
    p = next(x for x in c.ok("GET", "/api/providers")["providers"] if x["name"] == a.cloud_provider)
    models = [m for m in p["models"] if m["api"] == "openai-chat"]
    pm = next((m for m in models if m["upstream_model"] == a.cloud_model), None) if a.cloud_model else (models[0] if models else None)
    if pm is None:
        run.check(f"{p['name']}: has the chat model to use", False, f"chat models: {[m['upstream_model'] for m in models]}")
        return
    priced = pm.get("price_input") is not None and pm.get("price_output") is not None
    run.report.info["cloud_model"] = f"{p['name']}/{pm['upstream_model']}" + ("" if priced else " (no price)")
    t = c.request("POST", f"/api/providers/{p['id']}/test", timeout=60)
    td = t.data if isinstance(t.data, dict) else {}
    run.check(f"{p['name']}: Test connection (GET /models, no tokens)", t.status == 200 and td.get("ok"),
              f"{td.get('status')} in {td.get('ms')} ms, {td.get('models')} models" if t.status == 200 and td.get("ok")
              else f"{t.status} {t.code()}: {why(t)}")
    if not p.get("enabled"):
        run.check(f"{p['name']} is enabled", False, "enable it on its page first")
        return
    dep = run.deps[local_key]
    context = int((dep.get("params") or {}).get("context") or 4096)
    long_tokens = context + 1000
    route = route_of(run, dep["served_name"])
    local_t = {"tier": 0, "kind": "local", "deployment_id": dep["id"]}
    cloud_t = {"tier": 1, "kind": "provider", "provider_model_id": pm["id"]}

    def set_route(**flags):
        c.ok("PUT", f"/api/routes/{route['id']}", {"targets": [local_t, cloud_t], "spill_on_long_prompt": True, **flags})

    set_route()
    run.cleanup.add(f"route {dep['served_name']} back to its own deployment only",
                    lambda: c.ok("PUT", f"/api/routes/{route['id']}", {"targets": [local_t]}))
    model = dep["served_name"]
    k_on = run.new_key("cloud-on", cloud="on")
    k_off = run.new_key("cloud-off", cloud="off")
    k_fb = run.new_key("cloud-fb", cloud="fallback")
    # No request leaves for the provider in these: they must be served locally.
    r = post_chat(run, k_off["key"], model, long_tokens, "Reply with one word: ok.")
    run.check("a key set to Never does not spill, even when the prompt is too long", target_of(r.headers).startswith("local:"),
              f"{r.status}, {target_of(r.headers) or r.code()}")
    r = post_chat(run, k_on["key"], model, long_tokens, "Reply with one word: ok.", {"X-Forgeyard-Local-Only": "1"})
    run.check("X-Forgeyard-Local-Only: 1 keeps a request local", target_of(r.headers).startswith("local:")
              and not r.headers.get("x-forgeyard-spilled"), f"{r.status}, {target_of(r.headers) or r.code()}")
    set_route(local_only=True)
    r = post_chat(run, k_on["key"], model, long_tokens, "Reply with one word: ok.")
    run.check("a route marked local only does not spill", target_of(r.headers).startswith("local:"),
              f"{r.status}, {target_of(r.headers) or r.code()}")
    set_route()
    cloud_name = run.name("cloud-only")
    cr = c.ok("POST", "/api/routes", {"served_name": cloud_name, "api": "openai-chat",
                                     "targets": [{"tier": 0, "kind": "provider", "provider_model_id": pm["id"]}]})["route"]
    run.cleanup.add(f"route {cloud_name}", delete, c, f"/api/routes/{cr['id']}")
    fb_list = [x.id for x in run.sdk(k_fb["key"]).models.list()]
    on_list = [x.id for x in run.sdk(k_on["key"]).models.list()]
    r = post_chat(run, k_fb["key"], cloud_name, 8, "x")
    run.check("a cloud-only model: listed for Allowed, unlisted and 404 for Only to spill", cloud_name in on_list
              and cloud_name not in fb_list and r.status == 404, f"Allowed lists it: {cloud_name in on_list}, "
              f"Only to spill: listed {cloud_name in fb_list}, call {r.status} {r.code()}")

    # The one request that reaches the provider: too long for the local model, so it spills.
    r = post_chat(run, k_on["key"], model, long_tokens, CLOUD_PROMPT)
    run.cloud_calls += 1
    tgt = target_of(r.headers)
    run.check("a prompt too long for the local model spills to the cloud (X-Forgeyard-Spilled: too_long)",
              r.status == 200 and tgt.startswith("provider:") and r.headers.get("x-forgeyard-spilled") == "too_long",
              f"{r.status}, {tgt}, spilled {r.headers.get('x-forgeyard-spilled')}: "
              + (f"{str(((r.data or {}).get('choices') or [{}])[0].get('message', {}).get('content'))[:40]!r}"
                 if r.status == 200 else f"{r.code()}: {why(r)}"))
    run.cloud_ok = r.status == 200
    time.sleep(1)
    rows = [x for x in c.ok("GET", "/api/usage?days=1&group=key&everyone=true")["rows"] if x["name"].startswith(k_on["api_key"]["name"] + " ")]
    row = rows[0] if rows else {}
    if not run.cloud_ok:
        run.skip("its cost is recorded", "the request did not reach the provider (see above)")
    elif priced:
        run.check("its cost is recorded (priced model)", (row.get("cost_micros") or 0) > 0,
                  f"${(row.get('cost_micros') or 0) / 1e6:.6f}, {row.get('spilled')} spilled")
    else:
        run.check("it is counted as without a price (no price on the model)", (row.get("unpriced") or 0) >= 1,
                  f"{row.get('unpriced')} without a price")

    cloud_max(run)
    budgets(run, p, pm, priced, k_on, cloud_name)
    run.report.info["cloud_calls"] = run.cloud_calls


def cloud_max(run: Run) -> None:
    c, u = run.admin, run.user()
    order = ["off", "fallback", "on"]
    settings = c.ok("GET", "/api/keys")["cloud"]
    cap = settings["max"]
    k = run.new_key("mine-cloud", client=u["client"], cloud=cap)
    run.check(f"a person may give their own key the cloud maximum ({cap})", k["api_key"]["effective_cloud"] == cap,
              f"effective {k['api_key']['effective_cloud']}")
    if cap != "on":
        above = order[order.index(cap) + 1]
        r = u["client"].request("POST", "/api/keys", {"name": run.name("over-cap"), "cloud": above})
        if r.status == 201:
            run.cleanup.add("API key over the cap", run._revoke_key, u["client"], (r.data or {})["api_key"]["id"])
        run.check(f"…and not more ({above}: 422 over_cap)", r.status == 422 and r.code() == "over_cap", f"{r.status} {r.code()}")
    else:
        run.skip("a person may not give their own key more than the maximum",
                 "the server's maximum is Allowed, the highest; changing it would affect everyone")
    c.ok("PATCH", f"/api/keys/{k['api_key']['id']}", {"cloud": "on"})
    r = u["client"].request("PATCH", f"/api/keys/{k['api_key']['id']}", {"cloud": "off"})
    run.check("an admin's cloud setting on someone's key is locked for its owner", r.status == 403 and r.code() == "limit_locked",
              f"{r.status} {r.code()}")


def budgets(run: Run, p: dict, pm: dict, priced: bool, k_on: dict, cloud_name: str) -> None:
    c, u = run.admin, run.user()
    if not p.get("external"):
        run.skip("budgets", f"{p['name']} is inside your network: requests to it cost nothing and have no budget")
        return
    price_out = pm.get("price_output") if priced else None
    blocked_code = "budget_exhausted" if priced else "budget_unpriced"

    # A key budget too small for the estimate: 402, nothing sent.
    small = run.new_key("tiny-budget", cloud="on", budget_micros=1)
    r = post_chat(run, small["key"], cloud_name, tokens_for(1, price_out) or 64, "x")
    spent = next((k for k in c.ok("GET", "/api/budgets")["keys"] if k["id"] == small["api_key"]["id"]), {})
    run.check(f"a key budget too small for the request: 402 {blocked_code}, nothing sent",
              r.status == 402 and r.code() == blocked_code and r.headers.get("x-forgeyard-budget-exhausted") == "key"
              and not spent.get("spent_micros"), f"{r.status} {r.code()}, header {r.headers.get('x-forgeyard-budget-exhausted')}, "
              f"spent {spent.get('spent_micros')}")

    # 80% and 100% alerts, once: the key that made the one cloud request gets a budget just above its spend.
    mine = next((k for k in c.ok("GET", "/api/budgets")["keys"] if k["id"] == k_on["api_key"]["id"]), {})
    spent_micros = mine.get("spent_micros") or 0
    budget = choose_budget(spent_micros) if priced else None
    if budget is None:
        run.skip("budget alerts at 80% and 100%, once each", "the one cloud request " + (
            "failed, so nothing was spent" if not run.cloud_ok else
            f"cost {spent_micros} micro-USD, too little to place a budget between 80% and 100% of it" if priced
            else "has no price, so nothing was spent"))
    else:
        c.ok("PATCH", f"/api/keys/{k_on['api_key']['id']}", {"budget_micros": budget})
        need = tokens_for(budget - spent_micros, price_out) or 10_000_000
        r1 = post_chat(run, k_on["key"], cloud_name, need, "x")
        events = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            events = audit_budget_events(run, "key", k_on["api_key"]["name"])
            if {e["detail"].get("threshold") for e in events} >= {80, 100}:
                break
            time.sleep(2)
        r2 = post_chat(run, k_on["key"], cloud_name, need, "x")
        time.sleep(12)  # alerts are written every few seconds (USAGE_FLUSH_SECONDS)
        again = audit_budget_events(run, "key", k_on["api_key"]["name"])
        th = collections.Counter(e["detail"].get("threshold") for e in again)
        run.check("a key past 80% of its budget: the next request is refused (402) and alerts 80% and 100% once each",
                  r1.status == 402 and r2.status == 402 and th[80] == 1 and th[100] == 1
                  and {e["action"] for e in again} == {"budget.warning", "budget.exhausted"},
                  f"spent {spent_micros} of {budget} micro-USD; answers {r1.status}, {r2.status}; alerts {dict(th)}")

    # A person's budget (the throwaway user).
    c.ok("PUT", f"/api/budgets/people/{u['id']}", {"budget_micros": 1})
    run.cleanup.add("the throwaway user's budget", lambda: c.request("PUT", f"/api/budgets/people/{u['id']}", {"budget_micros": None}))
    uk = run.new_key("user-cloud", client=u["client"])
    c.ok("PATCH", f"/api/keys/{uk['api_key']['id']}", {"cloud": "on"})
    r = post_chat(run, uk["key"], cloud_name, tokens_for(1, price_out) or 64, "x")
    run.check("a person's budget refuses their request (402, X-Forgeyard-Budget-Exhausted: person)",
              r.status == 402 and r.code() == blocked_code and r.headers.get("x-forgeyard-budget-exhausted") == "person",
              f"{r.status} {r.code()}, header {r.headers.get('x-forgeyard-budget-exhausted')}")

    # The provider's budget: set just above this month's spend (so nobody is alerted), for one request.
    every = c.ok("GET", "/api/budgets?everyone=true")
    prov = next((x for x in every["providers"] if x["id"] == p["id"]), None)
    me = next((x for x in every["people"] if x["username"] == run.admin.username), {})
    if prov is None:
        run.skip("a provider's budget", f"{p['name']} is not listed with the budgets")
        return
    old = prov.get("budget_micros")
    test_budget = provider_test_budget(prov.get("spent_micros") or 0, prov.get("reserved_micros") or 0)
    room = test_budget - (prov.get("spent_micros") or 0) - (prov.get("reserved_micros") or 0)
    need = tokens_for(room + 1, price_out)
    if need is None:
        run.skip("a provider's budget", f"{p['name']} spent ${prov.get('spent_micros', 0) / 1e6:.2f} this month: a refused "
                 "request would need an absurd max_tokens")
        return
    if me.get("budget_micros") is not None:
        run.skip("a provider's budget", f"you ({run.admin.username}) have a budget of your own, which would be checked first")
        return
    kp = run.new_key("prov-budget", cloud="on")
    restore = run.cleanup.add(f"{p['name']}'s budget back to {old}",
                              lambda: c.ok("PUT", f"/api/budgets/providers/{p['id']}", {"budget_micros": old}))
    c.ok("PUT", f"/api/budgets/providers/{p['id']}", {"budget_micros": test_budget})
    try:
        r = post_chat(run, kp["key"], cloud_name, need, "x")
    finally:
        back = run.cleanup.now(restore)
    now_budget = next((x for x in c.ok("GET", "/api/budgets?everyone=true")["providers"] if x["id"] == p["id"]), {})
    run.check(f"a provider's budget refuses a request it cannot cover (402, provider:{p['name']})",
              r.status == 402 and r.code() == blocked_code and r.headers.get("x-forgeyard-budget-exhausted") == f"provider:{p['name']}",
              f"{r.status} {r.code()}, header {r.headers.get('x-forgeyard-budget-exhausted')}")
    run.check(f"{p['name']}'s budget is back to what it was ({old})", back and now_budget.get("budget_micros") == old,
              f"now {now_budget.get('budget_micros')}")


# ------------------------------------------------------------------ group: console
# ------------------------------------------------------------------ group: tts (1.3)
# Text to speech: one model per machine role, the ones scripts/accept_tts.py knows; that script's checks
# run on each (the SDK's speech calls, every format, streaming, voices, refusals, usage, the key guard and,
# with a speech-to-text model on the server or --tts-asr local:<model>, intelligibility).
TTS_MODELS = (("gpu", "qwen3-tts-0.6b"), ("cpu", "chatterbox-turbo-gguf"))


@group("tts", "text to speech: /v1/audio/speech with the official SDK, voices, formats, refusals, usage")
def group_tts(run: Run) -> None:
    import types

    import httpx
    import openai

    sys.path.insert(0, str(HERE))
    import accept_m3
    import accept_tts

    accept_m3.TLS = run.tls
    c = run.admin
    recipes = {r["name"]: r for r in c.ok("GET", "/api/models")["recipes"]}
    for role, name in TTS_MODELS:
        m = run.machine(role)
        if m is None:
            run.skip(f"{name}: text to speech", f"no --{role}-machine given")
            continue
        m = run.fresh_machine(role)
        spec = dict(accept_tts.MODELS[name])
        recipe = spec["recipe"]
        if recipe not in recipes:
            run.skip(f"{name}: text to speech", f"the server has no recipe {recipe}")
            continue
        if accept_tts.ensure(c, name, spec):
            run.cleanup.add(f"catalog entry {name}", _remove_model, c, name)
        params = dict(spec["params"]) | {"gpus": 1 if role == "gpu" else 0}
        if role == "cpu" and "threads" in recipes[recipe]["params"]:
            params["threads"] = 8
        got = deploy(run, f"tts-{role}", {"model": name, "recipe": recipe, "params": params, "device": role}, m)
        if not got:
            continue
        d0 = got["deployment"]
        holder: dict = {}

        def poll(dep_id=d0["id"]):
            ready, text, d = deployment_state(run, dep_id)
            holder["d"] = d
            return ready, text

        try:
            run.wait(d0["name"], poll, run.args.timeout, every=5)
        except Fail as e:
            run.check(f"{d0['name']}: ready on {got['where']}", False, str(e))
            continue
        run.check(f"{d0['name']}: ready on {got['where']}", True, f"after {fmt_seconds(time.monotonic() - got['t0'])}")
        d = c.ok("GET", f"/api/deployments/{d0['id']}")["deployment"]

        def check(label: str, ok, detail="", info: bool = False, skip: bool = False) -> None:
            if skip or info:
                run.skip(label, ("information: " if info else "") + str(detail))
            else:
                run.check(label, ok, detail)

        report = {"deployments": {d["name"]: {}}}
        args = types.SimpleNamespace(asr=run.args.tts_asr)
        accept_tts.exercise(c, d, d["name"], spec, recipes[recipe], args, check, httpx, openai, report, None,
                            key=run.new_key(f"tts-{role}"))
        run.report.info.setdefault("tts", {})[d["name"]] = report["deployments"][d["name"]]


# ------------------------------------------------------------------ group: img (1.3)
# Image generation: one model per machine role, the ones scripts/accept_img.py knows; that script's checks
# run on each (the SDK's images.generate at the model's size and two at once, the caps, url refused, the
# queue, usage, the key guard and, with a chat model that reads images, what the image shows).
IMG_MODELS = (("gpu", "z-image-turbo"), ("cpu", "sd-1.5-gguf"))


@group("img", "image generation: /v1/images/generations with the official SDK, caps, the queue, usage")
def group_img(run: Run) -> None:
    import types

    import httpx
    import openai

    sys.path.insert(0, str(HERE))
    import accept_img
    import accept_m3

    accept_m3.TLS = run.tls
    c = run.admin
    recipes = {r["name"]: r for r in c.ok("GET", "/api/models")["recipes"]}
    for role, name in IMG_MODELS:
        m = run.machine(role)
        if m is None:
            run.skip(f"{name}: images", f"no --{role}-machine given")
            continue
        m = run.fresh_machine(role)
        spec = dict(accept_img.MODELS[name])
        recipe = spec["recipe"]
        if recipe not in recipes:
            run.skip(f"{name}: images", f"the server has no recipe {recipe}")
            continue
        if accept_img.ensure(c, name, spec):
            run.cleanup.add(f"catalog entry {name}", _remove_model, c, name)
        side = spec["size"]
        params = dict(spec["params"]) | {"gpus": 1 if role == "gpu" else 0, "max_images": 2, "max_side": side}
        if role == "cpu" and "threads" in recipes[recipe]["params"]:
            params["threads"] = 8
        got = deploy(run, f"img-{role}", {"model": name, "recipe": recipe, "params": params, "device": role}, m)
        if not got:
            continue
        d0 = got["deployment"]
        holder: dict = {}

        def poll(dep_id=d0["id"]):
            ready, text, d = deployment_state(run, dep_id)
            holder["d"] = d
            return ready, text

        try:
            run.wait(d0["name"], poll, run.args.timeout, every=5)
        except Fail as e:
            run.check(f"{d0['name']}: ready on {got['where']}", False, str(e))
            continue
        run.check(f"{d0['name']}: ready on {got['where']}", True, f"after {fmt_seconds(time.monotonic() - got['t0'])}")
        d = c.ok("GET", f"/api/deployments/{d0['id']}")["deployment"]

        def check(label: str, ok, detail="", info: bool = False, skip: bool = False) -> None:
            if skip or info:
                run.skip(label, ("information: " if info else "") + str(detail))
            else:
                run.check(label, ok, detail)

        report = {"deployments": {d["name"]: {}}}
        args = types.SimpleNamespace(vision=run.args.img_vision)
        accept_img.exercise(c, d, d["name"], spec, side, args, check, httpx, openai, report, None,
                            key=run.new_key(f"img-{role}"))
        run.report.info.setdefault("img", {})[d["name"]] = report["deployments"][d["name"]]


# ------------------------------------------------------------------ group: video (1.3)
# Video generation: Wan2.1-T2V-1.3B on the GPU machine with scripts/accept_video.py's checks (a job made,
# followed and fetched as a real MP4, the refusals, the per-key cap, deleting, usage, expiry), and last a
# job whose deployment is stopped under it.
VIDEO_MODELS = (("gpu", "wan2.1-t2v-1.3b"),)


@group("video", "video generation: /v1/videos jobs with the official SDK, the MP4, caps, expiry, a lost replica")
def group_video(run: Run) -> None:
    import types

    import httpx
    import openai

    sys.path.insert(0, str(HERE))
    import accept_m3
    import accept_video

    accept_m3.TLS = run.tls
    c = run.admin
    recipes = {r["name"]: r for r in c.ok("GET", "/api/models")["recipes"]}
    for role, name in VIDEO_MODELS:
        m = run.machine(role)
        if m is None:
            run.skip(f"{name}: video", f"no --{role}-machine given")
            continue
        m = run.fresh_machine(role)
        spec = dict(accept_video.MODELS[name])
        if spec["recipe"] not in recipes:
            run.skip(f"{name}: video", f"the server has no recipe {spec['recipe']}")
            continue
        if accept_video.ensure(c, name, spec):
            run.cleanup.add(f"catalog entry {name}", _remove_model, c, name)
        params = dict(spec["params"]) | {"gpus": 1, "max_seconds": 5, "seconds": 5, "keep_minutes": 2}
        got = deploy(run, f"video-{role}", {"model": name, "recipe": spec["recipe"], "params": params, "device": role}, m)
        if not got:
            continue
        d0 = got["deployment"]

        def poll(dep_id=d0["id"]):
            ready, text, _ = deployment_state(run, dep_id)
            return ready, text

        try:
            run.wait(d0["name"], poll, run.args.timeout, every=5)
        except Fail as e:
            run.check(f"{d0['name']}: ready on {got['where']}", False, str(e))
            continue
        run.check(f"{d0['name']}: ready on {got['where']}", True, f"after {fmt_seconds(time.monotonic() - got['t0'])}")
        d = c.ok("GET", f"/api/deployments/{d0['id']}")["deployment"]

        def check(label: str, ok, detail="", info: bool = False, skip: bool = False) -> None:
            if skip or info:
                run.skip(label, ("information: " if info else "") + str(detail))
            else:
                run.check(label, ok, detail)

        report = {"deployments": {d["name"]: {}}}
        args = types.SimpleNamespace(seconds=5, steps=run.args.video_steps, no_expiry=run.args.video_no_expiry,
                                     job_timeout=1800)
        accept_video.exercise(c, d, d["name"], spec, spec["size"], args, check, httpx, openai, report, None,
                              key=run.new_key(f"video-{role}"))
        accept_video.restart_check(c, d, d["name"], spec["size"], args, check, httpx, openai)
        run.report.info.setdefault("video", {})[d["name"]] = report["deployments"][d["name"]]


# ------------------------------------------------------------------ group: passthrough (1.3)
# Workloads at /x/<name>: scripts/accept_x.py's app and checks, as a process on the CPU machine and in a
# container on the GPU machine (when it has a container engine).
X_ROLES = (("cpu", "process"), ("gpu", "container"))


@group("passthrough", "workloads at /x/<name>: keys, paths, headers, streaming, limits, the key guard, usage")
def group_passthrough(run: Run) -> None:
    sys.path.insert(0, str(HERE))
    import accept_m3
    import accept_x

    accept_m3.TLS = run.tls
    c = run.admin
    for role, kind in X_ROLES:
        m = run.machine(role)
        if m is None:
            run.skip(f"/x/ {kind} workload", f"no --{role}-machine given")
            continue
        m = run.fresh_machine(role)
        if "workload_guard" not in (m.get("features") or []):
            run.skip(f"{m['name']}: /x/ {kind} workload", "its agent does not list workload_guard (older than 1.3)")
            continue
        if not ((m.get("runtime_info") or {}).get(kind) or {}).get("ok"):
            run.skip(f"{m['name']}: /x/ {kind} workload", f"no {kind} runtime there")
            continue
        w = workload(run, accept_x.spec_for(kind, run.name(f"x-{kind}"), m))
        t0 = time.monotonic()
        try:
            w = wait_status(run, w["id"], ("running", "failed"), run.args.timeout)
        except Fail as e:
            run.check(f"{w['name']}: ready on {m['name']}", False, str(e))
            continue
        if not run.check(f"{w['name']}: ready on {m['name']}", w["status"] == "running",
                         f"{replicas_text(w)} after {fmt_seconds(time.monotonic() - t0)}"):
            continue

        def new_key(what: str, body: dict, kind=kind) -> str:
            return run.new_key(f"x-{kind}-{what}", **body)["key"]

        out = accept_x.exercise(c, run.args.server, w, run.check, new_key)
        run.report.info.setdefault("passthrough", {})[w["name"]] = out.get("usage")
    wanted = names_list(run.args.x_templates)
    if not wanted:
        run.skip("workload templates", "not asked for (--x-templates)")
        return
    m = run.fresh_machine("gpu")
    if m is None or not ((m.get("runtime_info") or {}).get("container") or {}).get("ok"):
        run.skip("workload templates", "no --gpu-machine with a container engine")
        return
    ts = {t["id"]: t for t in c.ok("GET", "/api/workloads/templates")["templates"]}
    wanted = list(ts) if wanted == ["all"] else wanted
    image, image_name = accept_x.photo(None)
    key = run.new_key("x-templates", workloads="all")["key"]

    def tcheck(label: str, ok, detail="", info: bool = False, skip: bool = False) -> None:
        if skip or info:
            run.skip(label, ("information: " if info else "") + str(detail))
        else:
            run.check(label, ok, detail)

    for tid in wanted:
        if tid not in ts:
            run.check(f"template {tid}", False, f"the server has no such template ({', '.join(ts)})")
            continue
        w = workload(run, dict(ts[tid]["specs"]["cuda"], name=run.name(f"x-{tid}"), machine_id=m["id"]))
        try:
            w = wait_status(run, w["id"], ("running", "failed"), run.args.timeout)
        except Fail as e:
            run.check(f"{w['name']}: ready", False, str(e))
            continue
        if run.check(f"{w['name']}: ready on {m['name']}", w["status"] == "running", replicas_text(w)):
            accept_x.exercise_template(c, run.args.server, w, ts[tid], tcheck, key, True, image, image_name)


@group("console", "the web console in a browser, as an admin and as a plain user")
def group_console(run: Run) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        run.skip("console", "Playwright for Python is not installed (run with --install-playwright to put it in --venv)")
        return
    a = run.args
    shots = Path(a.shots) if a.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    dep = next(iter(run.deps.values()), None)
    pages = list(CONSOLE_PAGES)
    if dep:
        pages.insert(6, (f"/models/deployments/{dep['id']}", dep["name"]))
    u = run.user()
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=a.chrome if a.chrome and Path(a.chrome).exists() else None,
                                         args=["--no-sandbox"])
        except Exception as e:  # noqa: BLE001 - no browser here: skip, say why
            run.skip("console", f"no browser could be started ({str(e).splitlines()[0][:120]}); give one with --chrome")
            return
        try:
            for who, username, password in (("admin", a.user, run.password), ("user", u["name"], u["password"]),
                                            ("anonymous", None, None)):
                ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
                try:
                    if username:
                        resp = ctx.request.post(a.server + "/api/auth/login", data={"username": username, "password": password},
                                                headers={"X-Forgeyard": "1"})
                        if resp.status != 200:
                            run.check(f"console: sign in as {who}", False, f"{resp.status}")
                            continue
                    todo = PUBLIC_PAGES if who == "anonymous" else pages
                    for path, title in todo:
                        console_page(run, ctx, who, path, title, shots)
                finally:
                    ctx.close()
        finally:
            browser.close()


def console_page(run: Run, ctx, who: str, path: str, title: Optional[str], shots: Optional[Path]) -> None:
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text[:160]) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"page error: {str(e)[:160]}"))
    page.on("response", lambda r: errors.append(f"{r.status} {r.url[len(run.args.server):][:80]}") if r.status >= 500 else None)
    want = title
    if who == "user" and path in ADMIN_PAGES:
        want = "Admins only"
    if path == "/account" and who != "anonymous":
        want = run.args.user if who == "admin" else run.user()["name"]
    try:
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(run.args.server + path, wait_until="networkidle", timeout=45000)
        try:
            page.wait_for_function("document.title.endsWith(' · Forgeyard')", timeout=10000)
        except Exception:  # noqa: BLE001 - reported below as the wrong title
            pass
        got = page.title()
        wide = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        slug = (path.strip("/").replace("/", "-") or "home")
        if shots:
            page.screenshot(path=str(shots / f"{who}-1440-{slug}.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(400)
        narrow = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        if shots:
            page.screenshot(path=str(shots / f"{who}-390-{slug}.png"), full_page=True)
        title_ok = got.endswith(" · Forgeyard") and (want is None or got == f"{want} · Forgeyard")
        scroll = [w for w, (sw, cw) in (("1440", wide), ("390", narrow)) if sw > cw + 1]
        run.check(f"{who} {path}: tab title, no errors, no sideways scroll", title_ok and not errors and not scroll,
                  f"tab {got!r}" + ("" if title_ok else f" (should be {want!r})") + (f"; errors: {errors[:3]}" if errors else "")
                  + (f"; scrolls sideways at {', '.join(scroll)} px" if scroll else "; fits 1440 and 390 px"))
    except Exception as e:  # noqa: BLE001 - one page must not stop the others
        run.check(f"{who} {path}: loads", False, f"{type(e).__name__}: {str(e).splitlines()[0][:160]}")
    finally:
        page.close()


# ------------------------------------------------------------------ the SDK in its own venv
def ensure_deps(args) -> None:
    want = {}
    if planned(args, "gateway") or planned(args, "cloud"):
        want["openai"] = SDK_SPEC
    if args.install_playwright and planned(args, "console"):
        want["playwright"] = PLAYWRIGHT_SPEC
    missing = [m for m in want if importlib.util.find_spec(m) is None]
    if not missing:
        return
    venv = Path(args.venv)
    if os.environ.get(REEXEC):
        raise Refused(f"still missing in {venv}: {', '.join(missing)}")
    py = venv / "bin" / "python"
    log(f"{', '.join(missing)} not installed here: putting {', '.join(want.values())} in a virtual environment in {venv}")
    venv.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("uv"):
        if not py.exists():
            subprocess.run(["uv", "venv", "--quiet", str(venv)], check=True)
        subprocess.run(["uv", "pip", "install", "--quiet", "--python", str(py), *want.values()], check=True)
    else:
        if not py.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "--quiet", *want.values()], check=True)
    os.environ[REEXEC] = "1"
    os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


# ------------------------------------------------------------------ main
def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    ensure_deps(args)
    tls = ssl.create_default_context(cafile=args.ca) if args.ca else None
    password = os.environ.get("FORGEYARD_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    run = Run(args, tls)
    run.password = password

    def stop(_sig, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    log(f"run {run.tag}: everything it creates is named {run.name('…')}")
    try:
        start(run, password)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except Fail as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    log(f"server {args.server} {run.version}; machines: "
        + (", ".join(f"{k} {v['name']}" for k, v in run.machines.items()) or "none given")
        + f"; groups: {', '.join(g for g, _ in args.plan)}")
    try:
        for name, why in args.plan:
            run.run_group(GROUPS[name], why)
    except KeyboardInterrupt:
        run.report.notes.append("interrupted (Ctrl-C or SIGTERM): the remaining groups did not run")
        log("interrupted: cleaning up")
    finally:
        log(f"cleaning up {run.cleanup.pending} things this run created")
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # a second Ctrl-C must not stop the cleanup
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        run.report.cleanup = run.cleanup.run()
        signal.signal(signal.SIGINT, signal.default_int_handler)
        run.report.finish()
    run.report.info["cloud_calls"] = run.cloud_calls
    print()
    print(run.report.render())
    if args.out:
        Path(args.out).write_text(json.dumps(run.report.to_json(), indent=2))
    return run.report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
