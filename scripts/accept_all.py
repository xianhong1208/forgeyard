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
  machines   each given machine is online with the hardware it should have (its GPU allowlist is
             reported; the models group checks that a model is placed on an allowed card); a join command
             pins the CA
  models     deploys MODELS (below), waiting with progress: Qwen3-1.7B GGUF on the CPU machine(s);
             a chat, an embedding, a rerank, a text classification (1.4.4) and a speech model on the GPU
             machine, each skipped when
             its estimate says it does not fit; the estimate against the deployment page; Try it
  gateway    /v1 with the official SDK: the model list and key allowlists, chat, streaming with and
             without include_usage, completions, embeddings, rerank, text classification (/v1/classify:
             the right language for three texts, prompt tokens, a chat model refused), transcription, rpm (429), a
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
             what the image shows (--img-vision); and edits (1.5.5, scripts/accept_img.py --edit's checks) with
             FLUX.2 [klein] 4B on vLLM-Omni on the GPU machine and on stable-diffusion.cpp on the CPU machine (512 px):
             one and two input images, multipart and JSON (a data: URL, an http(s) URL the gateway fetches), the
             refusals (no image, a mask, a file_id, too many inputs, an input too wide) and their usage
  video      video generation (1.3, scripts/accept_video.py's checks): Wan2.1-T2V-1.3B on vLLM-Omni on the GPU
             machine: the SDK's videos.create, polling to completed, the MP4 fetched through the gateway and read
             (size, frames, length), the caps, image-to-video refused, another key's 404, the per-key job cap,
             deleting a job at work, usage (media_ms, busy time), expiry (--video-no-expiry skips its wait),
             the key guard, and a job failing when its deployment is stopped under it; start images (1.5.5,
             scripts/accept_video.py --image's checks): refused by Wan2.1 (text only), and Wan2.2-TI2V-5B on the GPU
             machine making a video from one uploaded with the SDK and one as a data: URL (an MP4 of the size and
             length asked), the refusals and the usage
  vision     vision models (1.4, scripts/accept_vision.py's checks): RT-DETRv2 R18, SAM 2.1 tiny and Depth
             Anything V2 Small with the built-in vision recipe, on the GPU machine (the CUDA build) and the
             detector also on the CPU machine: two cats found in the COCO photo (multipart and JSON), the mask
             of a square, a 16-bit depth map, the wrong endpoint, no image, no key, a key without the model,
             the replica's port behind its key
  transformers  the transformers engine (1.5.4, scripts/accept_transformers.py's checks): CLIP ViT-B/32, DINOv2 small, a
             DistilBERT sentiment classifier and bge-m3-zeroshot-v2.0-c on the CPU machine, CLIP and the zero-shot model
             on the GPU machine (the CUDA variant), with the recipe --transformers-recipe (default transformers): a text
             finds its picture, a picture its smaller copy, every vector of length 1, an image by address fetched by the
             gateway, the image cap and the media refusals, images staying local (with --cloud-provider), fixed labels,
             12 Traditional Chinese lines among 10 topics with a Chinese template, multi-label, the zero-shot refusals,
             Try it, the Playground's route, the replica's key guard, usage. Until the built-in recipe's image moves to
             1.5.4 its deployments are a SKIP saying vision_image_too_old
  relay      models through the server (1.4): on the relay machine (--relay-machine, default the CPU machine)
             with its fetch_via set to "server", Download to… a small GGUF (RELAY_MODEL, 26 MB): done, "via"
             server, the file and its size in the machine's model cache; then on a second machine
             (--relay-second-machine, default the second CPU machine, else the GPU machine) the same download
             comes from the first machine's cache ("via" machine, the source named). Each machine's fetch_via
             goes back to what it was and the model leaves both caches
  images     engine images (1.4): on a machine with a container engine (--engine-machine, default the GPU
             machine): its engine-images listing; a tiny image file built here (one 1-file layer, no Docker on
             this computer) uploaded and loaded (its id, no recipe match, audited, only counted in the listing,
             its removal refused as not an engine image); copied to a second engine machine
             (--second-engine-machine, default another given machine that can load images; same content as the
             source, audited); then removed through Remove engine image, with a short-lived custom recipe
             naming it so that Forgeyard may (no `docker rmi` of an image no recipe names)
  scale      sleep when idle, waking and automatic replicas (1.5, scripts/accept_scale.py's checks): a CPU model on
             the CPU machine that sleeps after --scale-idle-minutes (1): its replicas stop, its reservation is freed,
             /v1/models still lists it, its files stay; --scale-parallel (20) requests wake it once; Wake now and the
             Playground wake it; with --cloud-provider a key that may spill is served by the cloud while it wakes and
             a structured-output request waits; a short wake_wait gives 503 model_waking with Retry-After (a model
             that starts slower than --scale-wake-wait: the vLLM one on the GPU machine); with --stoppable-agent and
             its --agent-stop / --agent-start commands, cannot_wake while its pinned machine is offline; Stop is not
             woken; on the two CPU machines automatic replicas 1 to 2: a load adds one, a stream on it finishes while
             it is drained, none twice; sleep with a minimum of 0; the start times and the metrics
  console    with Playwright for Python: every main page as an admin and as a plain user, at 1440
             and 390 px: it loads, names itself in the tab, logs no error, does not scroll sideways

The gateway and cloud groups need the models group's deployments: it runs for them when it was not
chosen. A machine that is not given skips what would run on it, saying so.

Waiting has two limits (1.4.5): --timeout (40 minutes) for a deployment or workload to download and start, and
--place-timeout (5 minutes) for one the server cannot place at all (no machine has the room: "Cannot be placed:
GPU #4 has 1.8 GiB available, needs 3 GiB"), after which its checks are a SKIP naming that shortage. Before each
group that deploys on the GPU machine (tts, img, video, vision, transformers, scale: GPU_GROUPS) the run stops what earlier
groups left on a GPU and no later group needs, waits until it is stopped and the machine has reported its memory
again, and removes it; the models group's deployments are only stopped (the console shows one, the cleanup removes
them), and its speech-to-text one stays while the tts group may use it. So on a card shared with other people's
work each group has the run's share of it.

Everything the run creates is named accept-<run>-… (the run id is printed first) and removed at the
end, also after a failure or Ctrl-C: deployments, routes, API keys, the throwaway user,
the catalog entries it added (models it found in the catalog stay), the relay group's model in the
machines' caches and their fetch_via, the images group's image (accept-<run>-image:1, a few KB) and its
custom recipe. When a machine's agent cannot remove images (older than 1.4.2), that tiny image stays
there and the report says so. A provider's budget goes back
to what it was. Nothing it did not create is changed, except that one budget for a few seconds. Usage
rows and audit events of the run stay, like those of any request. Objects left by an earlier run
that was killed are listed at the start, never touched.

The official SDK (and, with --install-playwright, Playwright): when they cannot be imported, the
script makes its own virtual environment (--venv; with uv when it is installed, else python -m venv
and pip), installs them there and runs itself again with it. Nothing else is installed anywhere.

Adding a group (a 1.3 feature: multimodal, tts, images, video): write a function that
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
import io
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
import tarfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# One module, whether run as a script or imported: scripts/accept_scale.py (the scale group) imports accept_all.
sys.modules.setdefault("accept_all", sys.modules[__name__])

HERE = Path(__file__).resolve().parent
CLIP = HERE.parent / "forgeyard" / "models" / "assets" / "jfk-ask-not.mp3"
GIB = 1024**3
# How long a workload or deployment may wait for room on a machine before its checks are skipped (--place-timeout).
PLACE_TIMEOUT = 300
# Groups that deploy on the GPU machine's cards (1.4.5, #203). Before each, what earlier groups left on a GPU and
# no group still to run needs is stopped, and then removed (the models group's deployments only stopped: the
# console shows one), so that a card shared with other people's work has room for the next group's models. The
# models group's speech-to-text deployment stays while the tts group (--tts-asr) may still use it.
GPU_GROUPS = ("tts", "img", "video", "vision", "transformers", "scale")
FREE_WAIT = 120
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
    # 1.4.4 (#144): a text classifier (XLM-R, 20 languages, 512 tokens: no context given, so the model's own).
    {"key": "classify", "device": "gpu", "model": "accept-xlm-roberta-language-detection",
     "source": "hf://papluca/xlm-roberta-base-language-detection", "task": "classify", "recipe": "vllm-pooling",
     "params": {}},
]
# What the classify check sends, and the language each text is in (the model's labels).
CLASSIFY_TEXTS = [("今天天氣很好，我們去公園散步吧。", "zh"), ("Bonjour à tous, comment allez-vous aujourd'hui ?", "fr"),
                  ("The weather is lovely today, let's go for a walk.", "en")]
# The relay group's model: the smallest GGUF on Hugging Face that the catalog takes (26 MB), downloaded to
# one machine through the server and then from that machine's cache to another.
RELAY_MODEL = {"model": "accept-stories15m-gguf", "source": "hf://ggml-org/tiny-llamas", "file": "stories15M-q8_0.gguf",
               "task": "chat"}
# What the relay and images groups need of an agent (protocol features, forgeyard/machines/protocol.py).
RELAY_FEATURES = ("models", "prefetch", "relay", "cache_admin")
IMAGE_FEATURES = ("image_load", "image_save", "image_remove")
# The only text ever sent to a cloud provider, in the one request that reaches it.
CLOUD_PROMPT = "Count from 1 to 20, separated by spaces."
# Pages the console group opens: (path, the tab title it must have; None: the page's own name).
CONSOLE_PAGES = [
    ("/start", None), ("/machines", "Machines"),
    ("/serving", "Serving"), ("/serving/deploy", "Deploy a model"), ("/models", "Models"), ("/cloud", "Cloud"),
    ("/playground", "Playground"), ("/keys", "API keys"), ("/usage", "Usage"), ("/account", None),
    ("/users", "Users"), ("/audit", "Audit log"), ("/trust", "Trust this server"), ("/guide/en/how-to", "How-to"),
]
ADMIN_PAGES = ("/cloud", "/users", "/audit")
PUBLIC_PAGES = [("/login", "Sign in"), ("/trust", "Trust this server"), ("/guide/en/quick-start", "Quick start")]

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Fail(Exception):
    """Something the run needed did not work: the current group stops with a FAIL."""


class Unplaced(Fail):
    """A workload or deployment the server found no room for (no machine has the memory, a card or a port) for
    --place-timeout seconds: its checks are SKIPs naming the shortage, not a wait without end."""


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

    def items(self) -> list[dict]:
        return list(self._items)

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
    ap.add_argument("--gpu-machine", help="a machine with GPUs: the GPU models run there")
    ap.add_argument("--cpu-machine", help="a machine for the CPU models (may be the GPU machine)")
    ap.add_argument("--second-cpu-machine", help="another machine for the CPU model: balancing and failover")
    ap.add_argument("--relay-machine", help="the relay group's machine (default: --cpu-machine)")
    ap.add_argument("--relay-second-machine", help="the machine the relay group copies the model to from the first "
                    "one's cache (default: --second-cpu-machine, else --gpu-machine when it is another machine)")
    ap.add_argument("--engine-machine", help="the images group's machine with a container engine (default: "
                    "--gpu-machine)")
    ap.add_argument("--second-engine-machine", help="the machine the images group copies the image to (default: "
                    "another given machine whose agent loads images)")
    ap.add_argument("--cloud-provider", help="an existing provider: the cloud group runs only with it")
    ap.add_argument("--cloud-model", help="its model to use (default: its first chat model)")
    ap.add_argument("--only", help="run only these groups (comma-separated)")
    ap.add_argument("--skip", help="run every group but these")
    ap.add_argument("--timeout", type=float, default=2400, help="seconds a deployment may take to be ready")
    ap.add_argument("--place-timeout", type=float, default=PLACE_TIMEOUT, help="seconds a workload or deployment may "
                    "wait for room on a machine (the server places none of its replicas) before its checks are skipped, "
                    "naming the shortage (default 300)")
    ap.add_argument("--transformers-recipe", default="transformers", help="the transformers group's recipe: a custom one "
                    "naming a newer vision image before the built-in one's pin moves (default transformers)")
    ap.add_argument("--kill-local", action="store_true",
                    help="kill the engine of a CPU replica running on THIS computer to show failover")
    ap.add_argument("--tts-asr", default="auto", help="the tts group's intelligibility check: auto (a ready "
                    "speech-to-text deployment, else skipped), a served model, local:<faster-whisper model>, or none")
    ap.add_argument("--img-vision", default="auto", help="the img group's check of what an image shows: auto (a "
                    "ready chat model that reads images, else skipped), a served model, or none")
    ap.add_argument("--img-edit-url", default="http://images.cocodataset.org/val2017/000000039769.jpg",
                    help="the img group's edit by an http(s) image address the server fetches (default: a COCO photo); "
                    "none skips it")
    ap.add_argument("--video-steps", type=int, help="the video group's num_inference_steps (default: the model's "
                    "own); fewer is faster")
    ap.add_argument("--video-no-expiry", action="store_true", help="the video group skips its expiry check (a wait of "
                    "about two minutes)")
    ap.add_argument("--shots", help="save the console group's screenshots in this directory")
    ap.add_argument("--chrome", default="/opt/google/chrome/chrome", help="Chrome for the console group")
    ap.add_argument("--install-playwright", action="store_true",
                    help="install Playwright for Python into --venv when it is missing (for the console group)")
    ap.add_argument("--venv", default=str(Path.home() / ".cache" / "forgeyard-accept-all"),
                    help="where to put a virtual environment with the openai SDK if it is not installed")
    ap.add_argument("--out", help="also write the report as JSON here")
    sys.path.insert(0, str(HERE))
    import accept_scale

    accept_scale.add_args(ap)
    args = ap.parse_args(argv)
    accept_scale.check_args(ap, args)
    try:
        args.plan = plan_groups(names_list(args.only), names_list(args.skip), list(GROUPS),
                                {g.name: g.needs for g in GROUPS.values()})
    except ValueError as e:
        ap.error(str(e))
    if args.second_cpu_machine and not args.cpu_machine:
        ap.error("--second-cpu-machine needs --cpu-machine")
    if args.second_cpu_machine and args.second_cpu_machine in (args.cpu_machine, args.gpu_machine):
        ap.error("--second-cpu-machine must be another machine than --cpu-machine and --gpu-machine")
    for a, b in (("relay_machine", "relay_second_machine"), ("engine_machine", "second_engine_machine")):
        first = getattr(args, a) or (args.cpu_machine if a == "relay_machine" else args.gpu_machine)
        if getattr(args, b) and getattr(args, b) == first:
            ap.error(f"--{b.replace('_', '-')} must be another machine than the first one ({first})")
    if args.cloud_model and not args.cloud_provider:
        ap.error("--cloud-model needs --cloud-provider")
    if args.timeout <= 0:
        ap.error("--timeout must be positive")
    if args.place_timeout <= 0:
        ap.error("--place-timeout must be positive")
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
        self.named: dict[str, dict] = {}          # the relay and images groups' machines by flag (relay, engine…)
        self.deps: dict[str, dict] = {}           # model key -> ready deployment
        self.models_done = False
        self.cloud_calls = 0
        self.cloud_ok = False
        self.password = ""
        self._user: Optional[dict] = None

    # -- names and checks
    def name(self, what: str) -> str:
        return f"accept-{self.tag}-{what}"

    def check(self, name: str, ok, detail="", info: bool = False, skip: bool = False) -> bool:
        """Record a check. info=True / skip=True record it as SKIP (information, or not run): the per-feature
        scripts' exercise() functions call their check that way."""
        if skip or info:
            self.skip(name, ("information: " if info else "") + str(detail))
            return True
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

    def machine_by_id(self, mid: int) -> Optional[dict]:
        return next((x for x in self.admin.ok("GET", "/api/machines")["machines"] if x["id"] == mid), None)

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
    def wait(self, what: str, fn: Callable[[], tuple], timeout: float, every: float = 2.0) -> str:
        """Poll fn() -> (done, state text) or (done, state text, unplaced) until done; prints each new state.
        Raises Fail on timeout, and Unplaced once `unplaced` (the server's reason while a replica is on no machine:
        see unplaced()) has been given for --place-timeout seconds in a row: the shortage, not a wait for the whole
        timeout on a card other work holds."""
        deadline = time.monotonic() + timeout
        last, shape, said = None, None, 0.0
        since: Optional[float] = None
        limit = float(getattr(self.args, "place_timeout", PLACE_TIMEOUT))
        while True:
            got = fn()
            done, text = got[0], got[1]
            why = got[2] if len(got) > 2 and not done else None
            # A new state at once; the same state with new numbers (a download) every 30 s.
            now_shape = re.sub(r"[\d.]+", "#", text)
            if text != last and (now_shape != shape or done or time.monotonic() - said >= 30):
                log(f"  {what}: {text}")
                last, shape, said = text, now_shape, time.monotonic()
            if done:
                return text
            now = time.monotonic()
            since = (since if since is not None else now) if why else None
            if why and now - since >= limit:
                raise Unplaced(f"{what}: not placed within {limit:.0f} s, {why}")
            if now > deadline:
                raise Fail(f"{what}: not done within {timeout:.0f} s (last: {text})")
            time.sleep(every)

    def free_gpu(self, group: str, rest: list[str]) -> list[str]:
        """Before `group` (one of GPU_GROUPS; `rest`: the groups after it): stop what earlier groups deployed on a
        GPU and no group still to run needs, wait until its machine confirms, remove what was not the models
        group's, and wait for the machine's next report (its free memory). Returns what it let go."""
        keep = {"asr"} if "tts" in (group, *rest) and self.args.tts_asr != "none" else set()
        chosen = []
        for item in self.cleanup.items():
            if item["done"] or not item.get("gpu") or item.get("group") in (None, group, *rest) or item.get("stopped"):
                continue
            if item["group"] == "models":
                key = next((k for k, d in self.deps.items() if d["id"] == item.get("dep_id")), None)
                if key is None or key in keep:
                    continue
            chosen.append(item)
        if not chosen:
            return []
        log(f"freeing the GPU for {group}: " + ", ".join(i["label"] for i in chosen))
        for item in chosen:
            self.admin.request("POST", item["path"] + "/stop")
            item["stopped"] = True

        def stopped() -> tuple[bool, str]:
            left = []
            for item in chosen:
                r = self.admin.request("GET", item["path"])
                if r.status != 200:
                    continue
                w = (r.data or {}).get("deployment", {}).get("workload") or (r.data or {}).get("workload") or {}
                if any(x["state"] not in ("stopped", "exited", "failed") and not x.get("final")
                       for x in w.get("replica_list") or []):
                    left.append(item["label"])
            return not left, "still stopping: " + ", ".join(left) if left else "stopped"

        m = self.machine("gpu")
        seen = (self.machine_by_id(m["id"]) or {}).get("last_heartbeat_at") if m else None
        try:
            self.wait("stopping what the next group does not need", stopped, FREE_WAIT, every=2)
            if m:
                self.wait(f"{m['name']}'s next report of its free memory", lambda: (
                    (self.machine_by_id(m["id"]) or {}).get("last_heartbeat_at") != seen, "waiting"), FREE_WAIT)
        except Fail as e:
            log(f"  {e}: going on")
        for item in chosen:
            if item["group"] != "models":
                self.cleanup.now(item)
        freed = [i["label"] for i in chosen]
        self.report.notes.append(f"before {group}, freed the GPU of: {', '.join(freed)}")
        return freed

    def not_ready(self, name: str, e: Fail) -> None:
        """What a wait that ended without the object ready records: SKIP naming the shortage when the server found
        no room for it (Unplaced), else FAIL."""
        if isinstance(e, Unplaced):
            self.skip(name, str(e))
        else:
            self.check(name, False, str(e))

    # -- the groups
    def run_group(self, g: Group, why: str) -> None:
        self.current = g.name
        result = self.report.group(g.name, g.title, why)
        log(f"=== {g.name}: {g.title}" + (f" ({why})" if why else ""))
        t0 = time.monotonic()
        try:
            g.fn(self)
        except Unplaced as e:
            self.skip(f"{g.name}: the rest of the group", str(e))
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
    for role, name in (("relay", a.relay_machine), ("relay2", a.relay_second_machine), ("engine", a.engine_machine),
                       ("engine2", a.second_engine_machine)):
        if name:
            if name not in machines:
                raise Refused(f"no machine named {name!r} (have: {', '.join(sorted(machines)) or 'none'})")
            run.named[role] = machines[name]
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
                                    ("route", "/api/routes", "routes", "served_name"),
                                    ("user", "/api/users", "users", "username")):
        for x in run.admin.ok("GET", path).get(key) or []:
            if pat.match(str(x.get(field_, ""))):
                found.append(f"{kind} {x[field_]}")
    for k in run.admin.ok("GET", "/api/keys").get("keys") or []:
        if pat.match(k["name"]) and k["status"] == "active":
            found.append(f"API key {k['name']}")
    for r in run.admin.ok("GET", "/api/models").get("recipes") or []:
        if pat.match(str(r.get("name", ""))) and not r.get("builtin"):
            found.append(f"recipe {r['name']}")
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
        # (Until 1.5.3 an app asking for a card outside the allowlist checked here that it was refused; apps were
        # removed (#89): the models group checks that each model is placed on an allowed card.)
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


# ------------------------------------------------------------------ helpers
def delete(c: Client, path: str) -> None:
    """DELETE something the run made; gone already is fine."""
    r = c.request("DELETE", path)
    if r.status not in (200, 404):
        raise Fail(f"{r.status or r.error} {r.code()}")


def unplaced(w: dict) -> Optional[str]:
    """The server's reason while a replica of this workload (a deployment's: its "workload") is on no machine
    because none has room for it ("Cannot be placed: GPU #4 has 1.8 GiB available, needs 3 GiB"), else None."""
    why = [r["reason"] for r in w.get("replica_list") or [] if not r.get("final") and not r.get("machine")
           and str(r.get("reason") or "").startswith("Cannot be placed")]
    return "; ".join(dict.fromkeys(why)) or None


def dep_poll(run: Run, dep_id: int) -> tuple[bool, str, Optional[str]]:
    ready, text, d = deployment_state(run, dep_id)
    return ready, text, unplaced(d["workload"])


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
    item = run.cleanup.add(f"deployment {name}", _delete_deployment, c, d["id"])
    item.update(group=run.current, gpu=spec["device"] == "gpu", path=f"/api/deployments/{d['id']}", dep_id=d["id"])
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
                return ready, text, unplaced(d["workload"])

            run.wait(name, poll, run.args.timeout, every=5)
            d = holder["d"]
        except Fail as e:
            run.not_ready(f"{name}: ready on {got['where']}", e)
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
    if "classify" in deps:
        auth = {"Authorization": f"Bearer {k_all['key']}", "Content-Type": "application/json"}
        r = raw(run.args.server + "/v1/classify", "POST", json.dumps({"model": served["classify"],
                                                                       "input": [t for t, _ in CLASSIFY_TEXTS]}).encode(),
                auth, run.tls, 120)
        got = [d.get("label") for d in sorted((r.data or {}).get("data") or [], key=lambda d: d["index"])] if r.status == 200 else []
        tokens = ((r.data or {}).get("usage") or {}).get("prompt_tokens") if r.status == 200 else None
        run.check(f"{served['classify']}: /v1/classify names each text's language",
                  got == [lang for _, lang in CLASSIFY_TEXTS] and bool(tokens),
                  f"{r.status}: {got}, {tokens} prompt tokens")
        probs = ((r.data or {}).get("data") or [{}])[0].get("probs") or []
        run.check(f"{served['classify']}: every label's probability, adding up to 1", len(probs) == 20
                  and abs(sum(probs) - 1) < 0.01, f"{len(probs)} labels, sum {sum(probs):.3f}")
        chat_model = next((served[k] for k in ("chat", "cpu") if k in deps), None)
        if chat_model:
            r = raw(run.args.server + "/v1/classify", "POST", json.dumps({"model": chat_model, "input": "x"}).encode(),
                    auth, run.tls, 30)
            run.check("a chat model at /v1/classify: 400 wrong_endpoint, with structured outputs named",
                      r.status == 400 and "structured_outputs" in r.text, f"{r.status} {r.text[:80]}")
    else:
        run.skip("text classification", "no text classification deployment (needs --gpu-machine with room)")
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
    """Every replica's own port, called around the gateway the way the gateway reaches it (1.4.5, #163: TLS with the
    machine's binding name when its agent serves TLS, plain HTTP for an older one; scripts/replica_port.py): how the
    port is reached, then the key guard's refusals."""
    sys.path.insert(0, str(HERE))
    import replica_port

    probes = {"openai-chat": ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2}),
              "openai-embeddings": ("/v1/embeddings", {"input": "x"}),
              "rerank": ("/v1/rerank", {"query": "q", "documents": ["a"]}),
              "classify": ("/classify", {"input": "x"})}
    open_paths = (("/invocations", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2}),
                  ("/rerank", {"query": "q", "documents": ["a"]}), ("/pooling", {"input": "x"}),
                  ("/classify", {"input": "x"}))
    rp = replica_port.Replicas.of(run.admin, run.args.server, run.tls)
    unreachable = []
    for key, d in run.deps.items():
        api = (d.get("route") or {}).get("api") or ({"asr": "openai-audio", "embed": "openai-embeddings",
                                                      "rerank": "rerank", "classify": "classify"}.get(key, "openai-chat"))
        for e in d["endpoints"]:
            p = rp.endpoint(e)
            what = f"{d['name']} #{e['replica']} on {e['machine']}"
            if api == "openai-audio":
                body, ctype = multipart({"model": d["route"]["upstream_model"]}, "file", CLIP)
                r = rp.request(p, "POST", "/v1/audio/transcriptions", body, {"Content-Type": ctype}, 30)
            else:
                path, payload = probes[api]
                r = rp.request(p, "POST", path, json.dumps({"model": d["route"]["upstream_model"], **payload}).encode(),
                               {"Content-Type": "application/json"}, 30)
            # Only a port that cannot be reached at all is another network's; a closed connection or a refused
            # certificate is a failure.
            if r.status == 0 and r.failure == "unreachable":
                unreachable.append(f"{what} ({r.error})")
                continue
            replica_port.record(rp.transport_checks(p, what), run.check)
            run.check(f"{what}: a direct call without the internal key gets 401 from the key guard",
                      r.status == 401 and "replica_key_required" in r.text, f"{r.status} {r.text[:60]} ({p.url})")
            r = rp.request(p, "GET", "/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
            run.check(f"{what}: a client's API key does not open the replica's port", r.status == 401,
                      f"{r.status or r.error}")
            if d["recipe"].startswith("vllm"):
                bad = {}
                for path, payload in open_paths:
                    r = rp.request(p, "POST", path, json.dumps({"model": d["route"]["upstream_model"], **payload}).encode(),
                                   {"Content-Type": "application/json"}, 30)
                    if not (r.status == 401 and "replica_key_required" in r.text):
                        bad[path] = r.status or r.error
                run.check(f"{what}: vLLM's open paths (/invocations, /rerank, /pooling, /classify) are guarded", not bad,
                          f"not 401: {bad}" if bad else "401 on all four")
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
        run.wait(f"{d['name']} started again", lambda: dep_poll(run, d["id"]), run.args.timeout, every=3)
        listed = [x.id for x in sdk.models.list()]
        run.check(f"…and is listed again once started ({name})", name in listed, "")
        run.deps[key] = c.ok("GET", f"/api/deployments/{d['id']}")["deployment"]
    except Fail as e:
        run.not_ready(f"…and is listed again once started ({name})", e)
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
        time.sleep(12)  # alerts are written every 5 s, with the usage
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

    import openai

    sys.path.insert(0, str(HERE))
    import accept_m3
    httpx = accept_m3.sdk_httpx()
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
            return ready, text, unplaced(d["workload"])

        try:
            run.wait(d0["name"], poll, run.args.timeout, every=5)
        except Fail as e:
            run.not_ready(f"{d0['name']}: ready on {got['where']}", e)
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
# 1.5.5 (#143): edits, with scripts/accept_img.py's edit checks only (generations are the models' above): FLUX.2 [klein]
# 4B on vLLM-Omni, and on stable-diffusion.cpp on the CPU at 512 px (a functional check: minutes per image there).
IMG_EDIT_MODELS = (("gpu", "flux.2-klein-4b", 1024), ("cpu", "flux.2-klein-4b-sdcpp", 512))
EDIT_INPUTS = 2      # the edit deployments' max_input_images: two inputs are sent, then one more


@group("img", "image generation: /v1/images/generations with the official SDK, caps, the queue, usage")
def group_img(run: Run) -> None:
    import types

    import openai

    sys.path.insert(0, str(HERE))
    import accept_img
    import accept_m3
    httpx = accept_m3.sdk_httpx()

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
            return ready, text, unplaced(d["workload"])

        try:
            run.wait(d0["name"], poll, run.args.timeout, every=5)
        except Fail as e:
            run.not_ready(f"{d0['name']}: ready on {got['where']}", e)
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
    for role, name, side in IMG_EDIT_MODELS:
        img_edit(run, role, name, side, recipes, httpx, openai)


def img_edit(run: Run, role: str, name: str, side: int, recipes: dict, httpx, openai) -> None:
    """One edit model of IMG_EDIT_MODELS on the machine of `role`: deployed with EDIT_INPUTS input images at `side`,
    then scripts/accept_img.py's edit checks (1.5.5)."""
    import types

    import accept_img

    c = run.admin
    if run.machine(role) is None:
        run.skip(f"{name}: edits", f"no --{role}-machine given")
        return
    m = run.fresh_machine(role)
    spec = dict(accept_img.MODELS[name])
    recipe = spec["recipe"]
    if recipe not in recipes or "max_input_images" not in recipes[recipe]["params"]:
        run.skip(f"{name}: edits", f"the server's {recipe} recipe does not edit images (older than 1.5.5)")
        return
    if accept_img.ensure(c, name, spec):
        run.cleanup.add(f"catalog entry {name}", _remove_model, c, name)
    choices = recipes[recipe]["params"]["default_size"].get("choices") or []
    params = dict(spec["params"]) | {"gpus": 1 if role == "gpu" else 0, "max_images": 2, "max_side": side,
                                     "max_input_images": EDIT_INPUTS,
                                     "default_size": f"{side}x{side}" if f"{side}x{side}" in choices else "auto"}
    if role == "cpu" and "threads" in recipes[recipe]["params"]:
        params["threads"] = 8
    got = deploy(run, f"edit-{role}", {"model": name, "recipe": recipe, "params": params, "device": role}, m)
    if not got:
        return
    d0 = got["deployment"]

    def poll(dep_id=d0["id"]):
        ready, text, d = deployment_state(run, dep_id)
        return ready, text, unplaced(d["workload"])

    try:
        run.wait(d0["name"], poll, run.args.timeout, every=5)
    except Fail as e:
        run.not_ready(f"{d0['name']}: ready on {got['where']}", e)
        return
    run.check(f"{d0['name']}: ready on {got['where']}", True, f"after {fmt_seconds(time.monotonic() - got['t0'])}")
    d = c.ok("GET", f"/api/deployments/{d0['id']}")["deployment"]

    def check(label: str, ok, detail="", info: bool = False, skip: bool = False) -> None:
        if skip or info:
            run.skip(label, ("information: " if info else "") + str(detail))
        else:
            run.check(label, ok, detail)

    report = {"deployments": {d["name"]: {}}}
    args = types.SimpleNamespace(image_url=run.args.img_edit_url)
    accept_img.exercise_edit(c, d, d["name"], side, args, check, httpx, openai, report, None, key=run.new_key(f"edit-{role}"))
    run.report.info.setdefault("img", {})[d["name"]] = report["deployments"][d["name"]]


# ------------------------------------------------------------------ group: video (1.3)
# Video generation: Wan2.1-T2V-1.3B on the GPU machine with scripts/accept_video.py's checks (a job made,
# followed and fetched as a real MP4, the refusals, the per-key cap, deleting, usage, expiry), and last a
# job whose deployment is stopped under it.
VIDEO_MODELS = (("gpu", "wan2.1-t2v-1.3b"),)
# 1.5.5 (#143): start images. Wan2.1 above refuses them; Wan2.2-TI2V-5B takes one (scripts/accept_video.py's start-image
# checks only: its text-to-video checks are Wan2.1's). About 33 GiB: skipped when the card has not that much free.
VIDEO_START_MODELS = (("gpu", "wan2.2-ti2v-5b"),)


@group("video", "video generation: /v1/videos jobs with the official SDK, the MP4, caps, expiry, a lost replica")
def group_video(run: Run) -> None:
    import types

    import openai

    sys.path.insert(0, str(HERE))
    import accept_m3
    httpx = accept_m3.sdk_httpx()
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
            ready, text, d = deployment_state(run, dep_id)
            return ready, text, unplaced(d["workload"])

        try:
            run.wait(d0["name"], poll, run.args.timeout, every=5)
        except Fail as e:
            run.not_ready(f"{d0['name']}: ready on {got['where']}", e)
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
        accept_video.exercise_image(c, d, d["name"], spec, spec["size"], args, check, httpx, openai, report, None,
                                    key=run.new_key(f"video-image-{role}"))
        accept_video.restart_check(c, d, d["name"], spec["size"], args, check, httpx, openai)
        run.report.info.setdefault("video", {})[d["name"]] = report["deployments"][d["name"]]
    for role, name in VIDEO_START_MODELS:
        video_start(run, role, name, recipes, httpx, openai)


def video_start(run: Run, role: str, name: str, recipes: dict, httpx, openai) -> None:
    """A video model that takes a start image, on the machine of `role`: deployed at its own size for 5 s, then
    scripts/accept_video.py's start-image checks (1.5.5)."""
    import types

    import accept_video

    c = run.admin
    if run.machine(role) is None:
        run.skip(f"{name}: start images", f"no --{role}-machine given")
        return
    m = run.fresh_machine(role)
    spec = dict(accept_video.MODELS[name])
    if spec["recipe"] not in recipes or "max_input_side" not in recipes[spec["recipe"]]["params"]:
        run.skip(f"{name}: start images", f"the server's {spec['recipe']} recipe takes no start image (older than 1.5.5)")
        return
    if accept_video.ensure(c, name, spec):
        run.cleanup.add(f"catalog entry {name}", _remove_model, c, name)
    params = dict(spec["params"]) | {"gpus": 1, "max_seconds": 5, "seconds": 5, "keep_minutes": 2}
    got = deploy(run, f"video-start-{role}", {"model": name, "recipe": spec["recipe"], "params": params, "device": role}, m)
    if not got:
        return
    d0 = got["deployment"]

    def poll(dep_id=d0["id"]):
        ready, text, d = deployment_state(run, dep_id)
        return ready, text, unplaced(d["workload"])

    try:
        run.wait(d0["name"], poll, run.args.timeout, every=5)
    except Fail as e:
        run.not_ready(f"{d0['name']}: ready on {got['where']}", e)
        return
    run.check(f"{d0['name']}: ready on {got['where']}", True, f"after {fmt_seconds(time.monotonic() - got['t0'])}")
    d = c.ok("GET", f"/api/deployments/{d0['id']}")["deployment"]

    def check(label: str, ok, detail="", info: bool = False, skip: bool = False) -> None:
        if skip or info:
            run.skip(label, ("information: " if info else "") + str(detail))
        else:
            run.check(label, ok, detail)

    report = {"deployments": {d["name"]: {}}}
    args = types.SimpleNamespace(seconds=5, steps=run.args.video_steps, job_timeout=1800)
    accept_video.exercise_image(c, d, d["name"], spec, spec["size"], args, check, httpx, openai, report, None,
                                key=run.new_key(f"video-start-{role}"))
    run.report.info.setdefault("video", {})[d["name"]] = report["deployments"][d["name"]]


# ------------------------------------------------------------------ group: vision (1.4)
# Vision models deployed from Models (#86): the three recommended ones on the GPU machine, and the detector
# on the CPU machine, with scripts/accept_vision.py's checks.
VISION_RUNS = (("gpu", "rtdetr-v2-r18"), ("gpu", "sam2.1-hiera-tiny"), ("gpu", "depth-anything-v2-small"),
               ("cpu", "rtdetr-v2-r18"))


@group("vision", "vision models: /v1/vision/detect, segment and depth with the vision recipe, keys, usage")
def group_vision(run: Run) -> None:
    sys.path.insert(0, str(HERE))
    import accept_m3
    import accept_vision

    accept_m3.TLS = run.tls
    c = run.admin
    if "vision" not in {r["name"] for r in c.ok("GET", "/api/models")["recipes"]}:
        run.skip("vision models", "the server has no vision recipe (older than 1.4)")
        return
    image, filename = accept_vision.photo(None)
    key = run.new_key("vision")["key"]
    for role, name in VISION_RUNS:
        m = run.machine(role)
        if m is None:
            run.skip(f"{name}: vision on the {role}", f"no --{role}-machine given")
            continue
        m = run.fresh_machine(role)
        spec = accept_vision.MODELS[name]
        if accept_vision.ensure(c, name, spec):
            run.cleanup.add(f"catalog entry {name}", _remove_model, c, name)
        params = {"gpus": 1 if role == "gpu" else 0} | ({"threads": 8} if role == "cpu" else {})
        got = deploy(run, f"vis-{role}-{spec['task']}", {"model": name, "recipe": "vision", "params": params,
                                                          "device": role}, m)
        if not got:
            continue
        d0 = got["deployment"]

        def poll(dep_id=d0["id"]):
            ready, text, d = deployment_state(run, dep_id)
            return ready, text, unplaced(d["workload"])

        try:
            run.wait(d0["name"], poll, run.args.timeout, every=5)
        except Fail as e:
            run.not_ready(f"{d0['name']}: ready on {got['where']}", e)
            continue
        run.check(f"{d0['name']}: ready on {got['where']}", True, f"after {fmt_seconds(time.monotonic() - got['t0'])}")
        d = c.ok("GET", f"/api/deployments/{d0['id']}")["deployment"]
        report: dict = {}
        other = accept_vision.other_model(c, d["name"])
        narrow = run.new_key(f"vision-narrow-{role}-{spec['task']}", models=[other])["key"] if other else None
        accept_vision.exercise(run.args.server, d, d["name"], spec, run.check, key, narrow, image, filename, report, c)
        run.report.info.setdefault("vision", {})[d["name"]] = report.get(d["name"])
        _delete_deployment(c, d0["id"])


# ------------------------------------------------------------------ group: transformers
@group("transformers", "the transformers engine: image embeddings, fixed and zero-shot text classification")
def group_transformers(run: Run) -> None:
    sys.path.insert(0, str(HERE))
    import accept_m3
    import accept_transformers as at

    accept_m3.TLS = run.tls
    machines = {"cpu": run.fresh_machine("cpu"), "gpu": run.fresh_machine("gpu")}
    opts = argparse.Namespace(only=at.DEFAULT_ONLY, gpu_only=at.DEFAULT_GPU, transformers_recipe=run.args.transformers_recipe,
                              photo=None, image_url=at.COCO_CATS, cloud_provider=run.args.cloud_provider, zh_pass=11, threads=8,
                              timeout=run.args.timeout, place_timeout=run.args.place_timeout, keep=False)
    x = at.Ctx(run.admin, run.args.server, run.tls, run.check, opts, prefix=run.name("tf-"))
    try:
        at.run(x, machines)
    finally:
        at.undo(x)
    run.report.info["transformers"] = {"recipe": opts.transformers_recipe, "deployed": sorted(x.served), "too_old": x.too_old}


# ------------------------------------------------------------------ group: relay
def missing_features(m: dict, need) -> list[str]:
    have = set(m.get("features") or [])
    return [f for f in need if f not in have]


def pick_second(candidates: list[Optional[dict]], first: dict, want: Callable[[dict], bool] = lambda m: True) -> Optional[dict]:
    """The first of the given machines that is another machine than `first` and that `want` takes."""
    for m in candidates:
        if m is not None and m["id"] != first["id"] and want(m):
            return m
    return None


def cache_part(view: dict, repo: str, revision: Optional[str], file: Optional[str]) -> tuple[Optional[dict], Optional[dict]]:
    """In a machine's model cache (GET /api/machines/{id}/cache): (the entry of this Hugging Face revision, the part
    that is this GGUF file), each None when it is not there. Without a file the entry is the part."""
    for e in view.get("models") or []:
        if e.get("kind") != "hf" or e.get("repo") != repo or (revision and e.get("revision") != revision):
            continue
        if not file:
            return e, e
        return e, next((p for p in e.get("parts") or [] if p.get("file") == file), None)
    return None, None


def set_fetch_via(run: Run, m: dict, how: str) -> None:
    """Set a machine's fetch_via, and put it back at the end (registered before it changes)."""
    c = run.admin
    before = m.get("fetch_via") or "auto"
    if before != how:
        run.cleanup.add(f"fetch_via of {m['name']} back to {before}", c.ok, "PUT",
                        f"/api/machines/{m['id']}/fetch-via", {"fetch_via": before})
    got = c.ok("PUT", f"/api/machines/{m['id']}/fetch-via", {"fetch_via": how})["machine"]
    run.check(f"{m['name']}: fetch_via set to {how} (was {before})", got.get("fetch_via") == how, got.get("fetch_via"))


def _cancel_download(c: Client, download_id: int) -> None:
    r = c.request("POST", f"/api/downloads/{download_id}/cancel")
    if r.status not in (200, 404, 409):   # 409: it had finished already
        raise Fail(f"{r.status} {r.code()}")


def _remove_cached(c: Client, m: dict, repo: str, revision: Optional[str], file: Optional[str]) -> None:
    """Remove the model the run downloaded from the machine's cache (nothing to do when it is not there)."""
    view = c.ok("GET", f"/api/machines/{m['id']}/cache")
    entry, part = cache_part(view, repo, revision, file)
    if part is None:
        return
    r = c.request("POST", f"/api/machines/{m['id']}/cache/remove", {"key": part["key"]})
    if r.status not in (200, 404):
        raise Fail(f"{r.status} {r.code()}: {why(r)}")


def _remove_partials(run: Run, m: dict, label: str, wait: float = 25.0, every: float = 2.0) -> None:
    """After a download the run cancelled: what it left half-downloaded goes (Remove partial downloads). The agent
    reports its cache with its heartbeat, so the part file may show only after a while (up to `wait` seconds); the
    agent refuses while its download still winds down (cache_busy): asked again."""
    c = run.admin
    deadline = time.monotonic() + wait
    while True:
        if c.ok("GET", f"/api/machines/{m['id']}/cache").get("partial_files"):
            r = c.request("POST", f"/api/machines/{m['id']}/cache/remove-partial")
            if r.status == 200:
                return
            if r.code() != "cache_busy" or time.monotonic() > deadline:
                raise Fail(f"{label}: {r.status} {r.code()}: {why(r)}")
        elif time.monotonic() > deadline:
            return  # nothing was left
        time.sleep(every)


def _partials_after(run: Run, m: dict, row_id: int, before: int) -> None:
    """The cleanup step for a download's half-downloaded files: only when the machine had none before the run (Remove
    partial downloads takes them all, and another download's are not the run's to delete); a note otherwise."""
    rows = run.admin.ok("GET", f"/api/downloads?machine_id={m['id']}")["downloads"]
    row = next((d for d in rows if d["id"] == row_id), None)
    if row is not None and row["state"] == "done":
        return
    if before:
        if run.admin.ok("GET", f"/api/machines/{m['id']}/cache").get("partial_files"):
            run.report.notes.append(f"{m['name']} keeps the partial downloads it had before the run, and maybe the "
                                    f"one the run broke off: Remove partial downloads on its page")
        return
    _remove_partials(run, m, f"partial downloads on {m['name']}")


def relay_download(run: Run, m: dict, model: dict, spec: dict, label: str, want_via: str,
                   source: Optional[dict] = None) -> Optional[dict]:
    """Download to… `model` on machine `m` (its fetch_via already "server"), checked: started, done, "via" as
    wanted (and the source machine named), the file and its size in the machine's cache. Returns the finished row."""
    c = run.admin
    repo = spec["source"].removeprefix("hf://")
    t = next((x for x in c.ok("GET", f"/api/downloads/targets?model_id={model['id']}&engine=false")["machines"]
              if x["machine_id"] == m["id"]), None)
    run.check(f"{label}: the Download to… dialog says it comes through the server",
              t is not None and t["state"] == "ok" and t["via"] == "server"
              and (source is None or t.get("from_machine") == source["name"]),
              f"{t['state']}, via {t['via']}" + (f", from {t.get('from_machine')}" if source is not None else "")
              + (f" ({t['note']})" if t.get("note") else "") if t else "the machine is not offered")
    run.cleanup.add(f"{model['name']} in {m['name']}'s model cache", _remove_cached, c, m, repo, model.get("revision"),
                    spec.get("file"))
    partial_before = c.ok("GET", f"/api/machines/{m['id']}/cache").get("partial_files") or 0
    r = c.request("POST", "/api/downloads", {"model_id": model["id"], "machine_ids": [m["id"]], "engine": False})
    if r.status != 201:
        run.check(f"{label}: the download starts", False, f"{r.status} {r.code()}: {why(r)}")
        return None
    row = r.data["downloads"][0]
    run.cleanup.add(f"what download #{row['id']} left half-done on {m['name']}", _partials_after, run, m, row["id"],
                    partial_before)
    run.cleanup.add(f"download #{row['id']} on {m['name']}", _cancel_download, c, row["id"])
    seen = {"via": set(), "row": row}

    def poll():
        rows = c.ok("GET", f"/api/downloads?model_id={model['id']}&machine_id={m['id']}")["downloads"]
        x = next((d for d in rows if d["id"] == row["id"]), None)
        if x is None:
            raise Fail(f"download #{row['id']} is no longer listed")
        seen["row"] = x
        seen["via"].add(x["via"])
        dl = x.get("download") or {}
        text = x["state"] + (f" via {x['via']}" if x["via"] else "") + (
            f" {dl.get('done', 0) / 1024**2:.1f}/{dl.get('total', 0) / 1024**2:.1f} MiB" if dl else "") + (
            f" from {x['source']['name']}" if x.get("source") else "") + (f" ({x['reason']})" if x.get("reason") else "")
        if x["state"] in ("failed", "cancelled"):
            raise Fail(f"download #{row['id']}: {text}")
        return x["state"] == "done", text

    t0 = time.monotonic()
    try:
        run.wait(f"download #{row['id']} to {m['name']}", poll, min(run.args.timeout, 900), every=1)
    except Fail as e:
        run.check(f"{label}: the download finishes", False, str(e))
        return None
    x = seen["row"]
    src = (x.get("source") or {}).get("name")
    run.check(f"{label}: done, the model came via {want_via}" + (f" from {source['name']}" if source else ""),
              x["via"] == want_via and (source is None or src == source["name"]),
              f"via {x['via']}" + (f" from {src}" if src else "") + f" in {fmt_seconds(time.monotonic() - t0)}"
              + (f"; seen while downloading: {sorted(v for v in seen['via'] if v)}" if x["via"] != want_via else ""))
    holder = {}

    def cached():
        entry, part = cache_part(c.ok("GET", f"/api/machines/{m['id']}/cache"), repo, model.get("revision"),
                                 spec.get("file"))
        holder["e"], holder["p"] = entry, part
        return part is not None, "listed" if part is not None else "not in the cache report yet"

    try:
        run.wait(f"{m['name']}'s model cache", cached, 90, every=3)
    except Fail as e:
        run.check(f"{label}: the model is in {m['name']}'s cache", False, str(e))
        return x
    entry, part = holder["e"], holder["p"]
    size = model.get("size_bytes") or 0
    run.check(f"{label}: {m['name']}'s cache lists {spec.get('file') or repo} at its size",
              part["bytes"] == size and (entry.get("files") or 0) >= 1 and not entry.get("missing"),
              f"{part['bytes']:,} bytes (catalog {size:,}), {entry.get('files')} file(s) in {repo}@"
              f"{(entry.get('revision') or '')[:12]}")
    return x


@group("relay", "models through the server: Download to… with fetch_via server, and from another machine's cache")
def group_relay(run: Run) -> None:
    c = run.admin
    first = run.named.get("relay") or run.machine("cpu")
    if first is None:
        run.skip("relay through the server", "no --cpu-machine or --relay-machine given")
        return
    first = run.machine_by_id(first["id"])
    if first["status"] != "online":
        run.skip(f"{first['name']}: relay through the server", f"the machine is {first['status']}")
        return
    lack = missing_features(first, RELAY_FEATURES)
    if lack:
        run.skip(f"{first['name']}: relay through the server", f"its agent lacks {', '.join(lack)} (older than 1.4: "
                 "update it)")
        return
    spec = RELAY_MODEL
    ensure_model(run, spec)
    model = next(x for x in c.ok("GET", "/api/models")["models"] if x["name"] == spec["model"])
    repo = spec["source"].removeprefix("hf://")
    if cache_part(c.ok("GET", f"/api/machines/{first['id']}/cache"), repo, model.get("revision"), spec["file"])[1]:
        run.skip(f"{first['name']}: relay through the server", f"{spec['file']} is already in its model cache (an "
                 "earlier run?), so there is nothing to download: remove it on the machine's page and run again")
        return
    set_fetch_via(run, first, "server")
    if relay_download(run, first, model, spec, f"{first['name']} through the server", "server") is None:
        return
    # From the first machine's cache to a second machine, through the server.
    second = run.named.get("relay2") or pick_second([run.machine("cpu2"), run.machine("gpu")], first)
    name = "relay from another machine's cache"
    if second is None:
        run.skip(name, "no second machine: give --relay-second-machine (or --second-cpu-machine, or a --gpu-machine "
                       "that is another machine)")
        return
    second = run.machine_by_id(second["id"])
    if second["status"] != "online":
        run.skip(f"{second['name']}: {name}", f"the machine is {second['status']}")
        return
    if "relay_source" not in (first.get("features") or []):
        run.skip(f"{second['name']}: {name}", f"{first['name']}'s agent does not send cached files (no relay_source: "
                                              "older than 1.4.1)")
        return
    lack = missing_features(second, RELAY_FEATURES)
    if lack:
        run.skip(f"{second['name']}: {name}", f"its agent lacks {', '.join(lack)} (update it)")
        return
    if cache_part(c.ok("GET", f"/api/machines/{second['id']}/cache"), repo, model.get("revision"), spec["file"])[1]:
        run.skip(f"{second['name']}: {name}", f"{spec['file']} is already in its model cache: remove it there and "
                                              "run again")
        return
    set_fetch_via(run, second, "server")
    relay_download(run, second, model, spec, f"{second['name']} from {first['name']}'s cache", "machine", first)


# ------------------------------------------------------------------ group: images
def image_archive(ref: str, marker: str, arch: str = "amd64") -> tuple[bytes, str]:
    """A minimal `docker save` archive of one image tagged `ref`, built here (no Docker needed): one layer holding
    one small file that names the run (`marker`), its config, and manifest.json, as Docker and Podman load them.
    Returns (the archive, the image id the engine gives it: the sha256 of the config)."""
    def tar_of(files: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    layer = tar_of({"forgeyard-accept.txt": f"Forgeyard acceptance run {marker}: safe to delete.\n".encode()})
    diff = hashlib.sha256(layer).hexdigest()
    config = json.dumps({"architecture": arch, "os": "linux", "created": "1970-01-01T00:00:00Z",
                         "config": {"Labels": {"org.forgeyard.accept": marker}},
                         "rootfs": {"type": "layers", "diff_ids": [f"sha256:{diff}"]},
                         "history": [{"created": "1970-01-01T00:00:00Z", "created_by": "scripts/accept_all.py"}]},
                        sort_keys=True, separators=(",", ":")).encode()
    cid = hashlib.sha256(config).hexdigest()
    manifest = json.dumps([{"Config": f"{cid}.json", "RepoTags": [ref], "Layers": [f"{diff}/layer.tar"]}]).encode()
    return tar_of({f"{diff}/layer.tar": layer, f"{cid}.json": config, "manifest.json": manifest}), f"sha256:{cid}"


def engine_arch(m: dict) -> str:
    """The image architecture of a machine's CPU (amd64 unless it says arm64)."""
    cpu = next((x for x in m.get("accelerators") or [] if x.get("vendor") == "cpu"), {})
    arch = str(cpu.get("arch") or (m.get("platform") or {}).get("machine") or "").lower()
    return "arm64" if arch in ("aarch64", "arm64") else "amd64"


def cleanup_recipe(name: str, tags: list[str]) -> str:
    """A custom recipe whose only purpose is to name the uploaded image by its tags (a tag-only container image per
    tag), so that Remove engine image may remove it: Forgeyard removes only images a recipe names."""
    variants = "".join(f"  - name: tag{i}\n    when: {{vendor: cpu}}\n    runtime: container\n    image: {t}\n"
                       for i, t in enumerate(dict.fromkeys(tags)))
    return (f"name: {name}\ntitle: {name}\ndescription: >-\n  Made by scripts/accept_all.py for a few seconds, to remove the "
            "image it uploaded. Safe to delete.\nengine: accept\nversion: \"1\"\ntasks: [chat]\nformats: [gguf]\n"
            "api: {chat: openai-chat}\nport: 8080\nhealth: {path: /health}\nestimate: {method: files}\n"
            f"variants:\n{variants}")


def audited(run: Run, action: str, target: str, **match) -> Optional[dict]:
    """The newest audit event of this action on this machine whose detail has these values, or None."""
    events = run.admin.ok("GET", f"/api/audit?action={urllib.parse.quote(action)}&limit=50")["events"]
    return next((e for e in events if e["action"] == action and e["target"] == target
                 and all((e.get("detail") or {}).get(k) == v for k, v in match.items())), None)


def tagged(items: list[dict], ref: str) -> Optional[dict]:
    """The image among `items` (as an agent lists them) that carries the tag `ref` (Podman may add localhost/)."""
    for i in items or []:
        if any(t == ref or t.endswith("/" + ref) for t in i.get("tags") or []):
            return i
    return None


class UploadedImage:
    """The image the images group put on machines, and how it goes again: a short-lived custom recipe names it by
    its tag, so Remove engine image (the agent's `rmi`, never forced) may take it; then the recipe goes."""

    def __init__(self, run: Run, ref: str):
        self.run, self.ref = run, ref
        self.tags: list[str] = [ref]
        self.on: dict[int, dict] = {}            # machine id -> machine, where it was loaded
        self.ids: dict[int, str] = {}            # machine id -> the image's id there, as its engine says
        self.recipe = run.name("image-cleanup")
        self.recipe_item: Optional[dict] = None

    def loaded(self, m: dict, item: dict) -> None:
        self.on[m["id"]], self.ids[m["id"]] = m, item["id"]
        self.tags += [t for t in item.get("tags") or [] if t not in self.tags]
        self.run.cleanup.add(f"image {self.ref} on {m['name']}", self.remove, m)

    def has(self, m: dict) -> bool:
        r = self.run.admin.request("GET", f"/api/machines/{m['id']}/images")
        if r.status != 200:
            raise Fail(f"cannot list {m['name']}'s images: {r.status} {r.code()}")
        return any(i.get("id") == self.ids[m["id"]] for i in (r.data or {}).get("images") or [])

    def ensure_recipe(self) -> None:
        if self.recipe_item is not None and not self.recipe_item["done"]:
            return
        c = self.run.admin
        r = c.request("POST", "/api/recipes", {"yaml": cleanup_recipe(self.recipe, self.tags)})
        if r.status == 409 and r.code() == "name_taken":
            c.ok("PUT", f"/api/recipes/{self.recipe}", {"yaml": cleanup_recipe(self.recipe, self.tags)})
        elif r.status != 201:
            raise Fail(f"cannot add the recipe that names the image: {r.status} {r.code()}: {why(r)}")
        self.recipe_item = self.run.cleanup.add(f"recipe {self.recipe}", delete, c, f"/api/recipes/{self.recipe}")

    def drop_recipe(self) -> None:
        if self.recipe_item is not None:
            self.run.cleanup.now(self.recipe_item)

    def remove_answer(self, m: dict) -> Resp:
        return self.run.admin.request("POST", f"/api/machines/{m['id']}/engine-images/remove",
                                      {"image": self.ids[m["id"]]}, timeout=330)

    def remove(self, m: dict) -> None:
        """The cleanup step: gone already is fine; an agent that cannot remove images leaves it (said in a note)."""
        m = self.run.machine_by_id(m["id"]) or m
        if not self.has(m):
            return
        if "image_remove" not in (m.get("features") or []):
            note = (f"{self.ref} ({self.ids[m['id']][:19]}, a few KB) stays on {m['name']}: its agent cannot remove "
                    f"images (older than 1.4.2); `docker rmi {self.ref}` there removes it")
            self.run.report.notes.append(note)
            return
        self.ensure_recipe()
        r = self.remove_answer(m)
        self.drop_recipe()
        if r.status != 200:
            raise Fail(f"{r.status} {r.code()}: {why(r)}")


@group("images", "engine images: the listing, an image file uploaded, copied to another machine, removed")
def group_images(run: Run) -> None:
    c = run.admin
    first = run.named.get("engine") or run.machine("gpu")
    if first is None:
        run.skip("engine images", "no --gpu-machine or --engine-machine given")
        return
    first = run.machine_by_id(first["id"])
    name = first["name"]
    cinfo = (first.get("runtime_info") or {}).get("container") or {}
    if first["status"] != "online":
        run.skip(f"{name}: engine images", f"the machine is {first['status']}")
        return
    lack = missing_features(first, IMAGE_FEATURES)
    if lack:
        run.skip(f"{name}: engine images", f"its agent lacks {', '.join(lack)}: "
                 + (f"it has no container engine ({cinfo.get('reason') or 'not available'})" if not cinfo.get("ok")
                    else "older than 1.4.2, update it"))
        return
    view = c.ok("GET", f"/api/machines/{first['id']}/engine-images")
    run.check(f"{name}: the machine page lists its engine images", view["listed"] and view["note"] is None
              and all(i.get("matches") for i in view["images"]),
              f"{len(view['images'])} engine image(s), {view['others']} other(s)"
              + (f", {(view.get('free_bytes') or 0) / GIB:.0f} GiB free" if view.get("free_bytes") else "")
              + (f"; {view['note']}" if view.get("note") else ""))
    others = view["others"]
    # An image file uploaded to the machine (#102), built here.
    ref = f"{run.name('image')}:1"
    archive, image_id = image_archive(ref, run.tag, engine_arch(first))
    filename = f"{run.name('image')}.tar"
    r = c.request("POST", f"/api/machines/{first['id']}/images", archive,
                  headers={"Content-Type": "application/x-tar", "X-Filename": filename}, timeout=300)
    img = UploadedImage(run, ref)
    got = tagged((r.data or {}).get("images"), ref) if r.status == 200 else None
    if got is not None:
        img.loaded(first, got)
    run.check(f"{name}: an image file uploads and loads ({len(archive):,} bytes)", got is not None,
              f"{r.status} " + (f"{got['id'][:19]}, tags {got.get('tags')}, {got.get('architecture')}" if got
                                else f"{r.code()}: {why(r)}" if r.status != 200 else f"no image tagged {ref} in "
                                f"{[i.get('tags') for i in (r.data or {}).get('images') or []]}"))
    if got is None:
        return
    if got["id"] == image_id:
        run.check(f"{name}: it is the image built here (its id is the config's digest)", True, image_id[:19])
    else:
        run.skip(f"{name}: it is the image built here (its id is the config's digest)",
                 f"information: the engine calls it {got['id'][:19]}, the config is {image_id[:19]} (an engine that "
                 "keys images by their manifest, e.g. Docker's containerd store)")
    image_id = got["id"]
    run.check(f"{name}: no recipe names it (matches: none)", got.get("matches") == [], str(got.get("matches")))
    ev = audited(run, "node.image_uploaded", name, file=filename)
    run.check(f"{name}: the upload is in the audit log (node.image_uploaded)", ev is not None and ev["detail"].get("ok"),
              f"by {ev['actor']}, {ev['detail'].get('bytes')} bytes" if ev else "no event")
    view = c.ok("GET", f"/api/machines/{first['id']}/engine-images")
    run.check(f"{name}: an image no recipe names is only counted on the machine page",
              all(i["id"] != image_id for i in view["images"]) and view["others"] >= others + 1,
              f"{view['others']} other(s), {others} before")
    r = img.remove_answer(first)
    run.check(f"{name}: Remove engine image refuses it (not_an_engine_image)",
              r.status == 409 and r.code() == "not_an_engine_image", f"{r.status} {r.code()}")
    # Copied to another machine through the server (#117).
    candidates = [run.named.get("engine2"), run.machine("cpu2"), run.machine("cpu"), run.machine("gpu")]
    second = pick_second([run.machine_by_id(m["id"]) if m else None for m in candidates], first,
                         lambda m: m["status"] == "online" and "image_load" in (m.get("features") or []))
    if second is None:
        given = run.named.get("engine2")
        run.skip("copy the image to another machine", f"{given['name']} cannot take images (offline, or no container "
                 "engine)" if given else "no other given machine has a container engine (give --second-engine-machine)")
    else:
        r = c.request("POST", f"/api/machines/{second['id']}/images/copy", {"source_id": first["id"], "image": image_id},
                      timeout=600)
        copied = tagged((r.data or {}).get("images"), ref) if r.status == 200 else None
        if copied is not None:
            img.loaded(second, copied)
        run.check(f"{second['name']}: the image is copied from {name}, the same image",
                  copied is not None and copied.get("same_as_source") is True and (r.data or {}).get("bytes", 0) > 0,
                  f"{r.status} " + (f"{(r.data or {}).get('bytes'):,} bytes, same as source: {copied.get('same_as_source')}"
                                    if copied else f"{r.code()}: {why(r)}"))
        ev = audited(run, "node.image_copied", second["name"], image=image_id[:80])
        run.check(f"{second['name']}: the copy is in the audit log (node.image_copied)",
                  ev is not None and ev["detail"].get("ok") and ev["detail"].get("from_machine") == name,
                  f"from {ev['detail'].get('from_machine')}, {ev['detail'].get('bytes')} bytes" if ev else "no event")
    # Removed again: a custom recipe names it for a moment, so Forgeyard may remove it.
    img.ensure_recipe()
    for m in list(img.on.values()):
        m = run.machine_by_id(m["id"]) or m
        if "image_remove" not in (m.get("features") or []):
            run.skip(f"{m['name']}: Remove engine image", "its agent cannot remove images (older than 1.4.2): the "
                     "image stays there, see the notes")
            continue
        view = c.ok("GET", f"/api/machines/{m['id']}/engine-images")
        item = next((i for i in view["images"] if i["id"] == img.ids[m["id"]]), None)
        run.check(f"{m['name']}: named by a recipe, it is listed as an engine image that may go",
                  item is not None and item.get("remove_note") is None
                  and any(x.get("recipe") == img.recipe for x in item.get("matches") or []),
                  f"matches {[x.get('recipe') + ' (' + x.get('how', '') + ')' for x in (item or {}).get('matches') or []]}"
                  + (f", {item.get('remove_note')}" if item and item.get("remove_note") else ""))
        r = img.remove_answer(m)
        gone = not img.has(m)
        run.check(f"{m['name']}: Remove engine image removes it", r.status == 200 and gone,
                  f"{r.status} " + (f"freed {(r.data or {}).get('freed_bytes'):,} bytes" if r.status == 200
                                    else f"{r.code()}: {why(r)}") + ("" if gone else "; still listed"))
        ev = audited(run, "node.image_removed", m["name"], image=img.ids[m["id"]])
        run.check(f"{m['name']}: the removal is in the audit log (node.image_removed)", ev is not None
                  and ev["detail"].get("ok"), f"recipes {ev['detail'].get('recipes')}" if ev else "no event")
    img.drop_recipe()
    r = c.request("GET", f"/api/recipes/{img.recipe}")
    run.check("the recipe that named it is gone again", r.status == 404, f"{r.status}")


# ------------------------------------------------------------------ group: scale (1.5.0)
@group("scale", "sleep when idle, waking, spill while waking, Stop, automatic replicas with draining, start times")
def group_scale(run: Run) -> None:
    """scripts/accept_scale.py's checks (its docstring lists the steps and their options)."""
    sys.path.insert(0, str(HERE))
    import accept_scale

    accept_scale.exercise(run)


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
        pages.insert(6, (f"/serving/{dep['id']}", dep["name"]))
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
def run_groups(run: Run, plan: list[tuple[str, str]]) -> None:
    """The planned groups in order; before each of GPU_GROUPS, what earlier groups left on a GPU is let go."""
    names = [g for g, _ in plan]
    for i, (name, why) in enumerate(plan):
        if name in GPU_GROUPS:
            run.free_gpu(name, names[i + 1:])
        run.run_group(GROUPS[name], why)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    ensure_deps(args)
    return run_plan(args)


def run_plan(args: argparse.Namespace) -> int:
    """Sign in, run the planned groups, clean up, print the report (and write it with --out): the exit code.
    scripts/accept_scale.py runs its one group with it."""
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
        run_groups(run, args.plan)
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
