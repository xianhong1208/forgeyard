#!/usr/bin/env python3
"""1.5.4 acceptance on real machines (#141, #144): image embeddings and text classification (fixed labels and zero-shot)
on Forgeyard's transformers server (the `transformers` recipe), through the gateway.

Through Forgeyard's API only (an admin session) and /v1 with an API key. For each model of --only, on the CPU machine
(--cpu-machine) and, with --gpu-machine, the GPU models (--gpu-only) on the GPU machine (the CUDA variant):

  1. adds the model to the catalog unless it is there (pinned to the commit below), estimates and deploys it there as
     `accept-tf-<where>-<model>` and waits until it is ready (bounded: --timeout to start, --place-timeout for room
     on the machine, then its checks are a SKIP naming why);
  2. an embedding model (/v1/embeddings, with a new API key): a CLIP-like model gets two texts and two generated
     pictures (a red square, a blue circle) and each text's closest picture must be its own; the COCO photo of two
     cats (downloaded here; --photo to use a file) is the closest of three pictures to "two cats lying on a blanket";
     the photo by its http(s) address (--image-url, default the COCO address), which the gateway fetches under the
     media rules, gives the vector of the photo sent inline; DINOv2 finds a picture's smaller copy closer than another
     picture, and refuses a text (400 text_not_supported). Every vector has length 1. The limits: more images than
     the deployment's max_images (400 too_many_images), a file:// address (400 media_url_refused), and the two kinds of
     bad image below. An answer with images comes from the local replica (X-Forgeyard-Target); with
     --cloud-provider (one with an embeddings model) the deployment's route gets that provider as a second tier and an
     image request with a key that may spill still stays local, and a cloud-only route refuses images (400
     images_not_supported), then both are put back; an image that is not base64 (400 invalid_media_data, the gateway),
     base64 that is not a picture (400 invalid_image, the engine);
  3. a text classifier (/v1/classify): a model with fixed labels (DistilBERT sentiment) labels a Chinese and an English
     review, and refuses labels (400 labels_not_supported); a zero-shot model chooses among 10 topics for 12 Traditional
     Chinese news lines with a Chinese hypothesis template (pass mark --zh-pass, default 11 of 12; the English default
     template is recorded too), the labels echoed in order; multi-label scores each label on its own; without labels
     400 labels_required; texts x labels over max_pairs 400 too_many_pairs;
  4. for every deployment: no key (401), a key whose allowlist lacks it (403), the replica's own port without its key
     (401 from the agent's key guard; over TLS with the machine's binding name when its agent serves TLS), Try it on
     its page, and the Playground's route as the signed-in admin;
  5. the usage of the run's key: requests, images and kilopixels for the embedding requests with images, prompt tokens
     and busy time per model;

then removes what it created (deployments, keys, routes it changed, the catalog entries it added) and prints a report
(JSON with --out). Everything is named accept-tf-*, and leftovers of an earlier run are removed first.

    FORGEYARD_PASSWORD=… python3 scripts/accept_transformers.py --server https://<server>:7860 --cacert forgeyard-ca.pem \\
        --user <admin> --cpu-machine <cpu machine> [--gpu-machine <gpu machine>] [--only …] [--out r.json]

On a recipe whose vision image is older than 1.5.4, deploying answers 422 vision_image_too_old: the script says so
and SKIPs every deployment with that reason. The built-in recipe pins a 1.5.4 image; to accept another image, make a
custom recipe (an engine copy) naming it and pass --transformers-recipe <its name>.

Models it knows (--only; the default runs the first four on the CPU machine, --gpu-only the CUDA ones):
  clip-vit-b-32-laion-embed, dinov2-small-embed, distilbert-sentiment, bge-m3-zeroshot-v2.0-c (defaults),
  siglip2-base-embed (multilingual CLIP-like), roberta-base-zeroshot-v2.0-c (English zero-shot)
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PREFIX = "accept-tf-"
RECIPE = "transformers"
COCO_CATS = "http://images.cocodataset.org/val2017/000000039769.jpg"   # CC BY 4.0: two cats on a pink blanket
MODELS = {
    "clip-vit-b-32-laion-embed": {"source": "hf://laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
                                  "revision": "1a25a446712ba5ee05982a381eed697ef9b435cf", "task": "embed", "kind": "clip"},
    "dinov2-small-embed": {"source": "hf://facebook/dinov2-small", "revision": "ed25f3a31f01632728cabb09d1542f84ab7b0056",
                           "task": "embed", "kind": "image-only"},
    "distilbert-sentiment": {"source": "hf://lxyuan/distilbert-base-multilingual-cased-sentiments-student",
                             "revision": "cf991100d706c13c0a080c097134c05b7f436c45", "task": "classify", "kind": "fixed"},
    "bge-m3-zeroshot-v2.0-c": {"source": "hf://MoritzLaurer/bge-m3-zeroshot-v2.0-c",
                               "revision": "705510dfe0f35d223a4bf6f5d2695ec5fd162341", "task": "classify", "kind": "zero-shot"},
    "siglip2-base-embed": {"source": "hf://google/siglip2-base-patch16-224", "revision": "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2",
                           "task": "embed", "kind": "clip"},
    "roberta-base-zeroshot-v2.0-c": {"source": "hf://MoritzLaurer/roberta-base-zeroshot-v2.0-c",
                                     "revision": "d825e740e0c59881cf0b0b1481ccf726b6d65341", "task": "classify",
                                     "kind": "zero-shot", "english": True},
}
DEFAULT_ONLY = "clip-vit-b-32-laion-embed,dinov2-small-embed,distilbert-sentiment,bge-m3-zeroshot-v2.0-c"
DEFAULT_GPU = "clip-vit-b-32-laion-embed,bge-m3-zeroshot-v2.0-c"
MAX_IMAGES = 4          # the deployments' max_images: 5 images in one request are refused
MAX_PAIRS = 128         # the zero-shot deployments' max_pairs: 12 x 10 fit, 13 x 10 do not

# A small Traditional Chinese news set (12 lines, 10 topics) and its template; 1.5.4's research: 12/12 with it.
ZH = [("台積電今天公布第三季財報，營收創下新高。", "財經"), ("中華隊在十二強棒球賽擊敗日本奪冠。", "運動"),
      ("立法院今天三讀通過新的預算案。", "政治"), ("這道紅燒牛肉麵要先把牛腱燉煮兩個小時。", "料理"),
      ("颱風明天下午可能登陸，氣象署發布海上警報。", "天氣"), ("新款筆電搭載最新處理器，續航力大幅提升。", "科技"),
      ("流感疫苗開打，醫師建議長者盡早接種。", "健康"), ("這次去京都住了三晚，清水寺的楓葉很美。", "旅遊"),
      ("央行宣布升息半碼，房貸族壓力增加。", "財經"), ("大谷翔平本季擊出五十支全壘打。", "運動"),
      ("教育部宣布明年起調整高中課綱。", "教育"), ("周杰倫演唱會門票五分鐘內售罄。", "音樂")]
ZH_LABELS = ["財經", "運動", "政治", "料理", "天氣", "科技", "健康", "旅遊", "教育", "音樂"]
ZH_TEMPLATE = "這段文字是關於{}。"
MULTI_TEXT, MULTI_LABELS = "我的發票金額開錯了，而且客服一直沒有回覆。", ["帳務", "客服", "稱讚"]
EN = [("The new laptop has a faster chip and a battery that lasts all day.", "technology"),
      ("The soup was cold and the waiter ignored us for twenty minutes.", "food")]
EN_LABELS = ["technology", "food", "sports"]
SENTIMENT = [("這家餐廳的服務很好，菜也好吃！", "positive"), ("The battery died after one day and support never answered.", "negative")]
SHAPES = [("a red square", "square"), ("a blue circle", "circle")]
CATS_TEXT = "two cats lying on a pink blanket"


class Fail(Exception):
    pass


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------------ pictures (no image library: plain PNG)
def _png(w: int, h: int, px: Callable[[int, int], bytes]) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"".join(px(x, y) for x in range(w)) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def shape_png(shape: str, side: int = 224) -> bytes:
    """A red square or a blue circle, filling half the picture, on white: the same drawing at any size."""
    c = (side - 1) / 2
    r = side * 0.3

    def px(x, y):
        if shape == "square" and abs(x - c) <= r and abs(y - c) <= r:
            return b"\xd8\x1e\x1e"
        if shape == "circle" and (x - c) ** 2 + (y - c) ** 2 <= r * r:
            return b"\x1e\x3c\xd8"
        return b"\xff\xff\xff"
    return _png(side, side, px)


def data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def photo(path: Optional[str]) -> tuple[Optional[bytes], str]:
    """The cats photo (or --photo), or (None, why) when it cannot be had (no internet here)."""
    if path:
        return Path(path).read_bytes(), Path(path).name
    try:
        with urllib.request.urlopen(COCO_CATS, timeout=30) as r:
            return r.read(), "coco-000000039769.jpg"
    except (urllib.error.URLError, OSError) as e:
        return None, f"the COCO photo could not be downloaded here ({e})"


# ------------------------------------------------------------------ arithmetic (tested: tests/test_accept_transformers.py)
def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def unit_lengths(vectors: list[list[float]], tolerance: float = 1e-3) -> bool:
    """Every vector has length 1 (the transformers server normalises them)."""
    return bool(vectors) and all(abs(math.sqrt(sum(x * x for x in v)) - 1) <= tolerance for v in vectors)


def nearest(query: list[float], candidates: list[list[float]]) -> tuple[int, list[float]]:
    """The index of the candidate most similar to `query`, and every cosine."""
    sims = [cosine(query, c) for c in candidates]
    return max(range(len(sims)), key=lambda i: sims[i]), sims


def topics_right(answer: dict, gold: list[str], labels: list[str]) -> int:
    """How many texts got their gold label: by the answer's label, else its highest probability in `labels`' order."""
    right = 0
    for d in answer.get("data") or []:
        probs = d.get("probs") or []
        label = d.get("label") or (labels[probs.index(max(probs))] if probs else None)
        i = d.get("index")
        if isinstance(i, int) and 0 <= i < len(gold) and label == gold[i]:
            right += 1
    return right


def image_too_old(status: int, data) -> Optional[str]:
    """The server's message when the recipe's vision image is older than 1.5.4 (422 vision_image_too_old), else None."""
    d = data.get("detail") if isinstance(data, dict) else None
    if status == 422 and isinstance(d, dict) and d.get("code") == "vision_image_too_old":
        return str(d.get("message") or "vision_image_too_old")
    return None


def code(data) -> Optional[str]:
    for k in ("error", "detail"):
        e = data.get(k) if isinstance(data, dict) else None
        if isinstance(e, dict) and e.get("code"):
            return str(e["code"])
    return None


def plan(only: list[str], gpu_only: list[str], cpu: Optional[dict], gpu: Optional[dict]) -> list[tuple[str, str]]:
    """(where, model) pairs to run, in order: every --only model on the CPU machine, then the --gpu-only ones on the GPU
    machine (each only when that machine is given)."""
    out = [("cpu", n) for n in only] if cpu else []
    out += [("gpu", n) for n in gpu_only] if gpu else []
    return out


def deployment_name(prefix: str, where: str, model: str) -> str:
    return f"{prefix}{where}-{model}".replace(".", "-")[:48].rstrip("-")


# ------------------------------------------------------------------ HTTP
def post_v1(server: str, path: str, body: dict, key: Optional[str], tls, timeout: float = 600) -> tuple[int, dict, dict, float]:
    """POST /v1/<path> with an API key: (status, JSON answer, headers in lower case, seconds)."""
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method="POST", headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls) as r:
            status, raw, h = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, raw, h = e.code, e.read(), e.headers
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": {"code": "unreachable", "message": str(e)}}, {}, time.monotonic() - t0
    try:
        out = json.loads(raw or b"null")
    except ValueError:
        out = {"raw": raw[:300].decode("utf-8", "replace")}
    return status, out if isinstance(out, dict) else {"raw": out}, {k.lower(): v for k, v in (h or {}).items()}, time.monotonic() - t0


def vectors(answer: dict) -> list[list[float]]:
    return [d.get("embedding") or [] for d in sorted(answer.get("data") or [], key=lambda d: d.get("index", 0))]


# ------------------------------------------------------------------ the run
class Ctx:
    """What one run needs: the signed-in admin client (accept_m3.Client or accept_all.Client: .call and .ok), the
    server, the TLS context for /v1 and the replicas, `check(label, ok, detail, info=, skip=)`, and the options."""

    def __init__(self, c, server: str, tls, check: Callable, args: argparse.Namespace, prefix: str = PREFIX):
        self.c, self.server, self.tls, self.check, self.args, self.prefix = c, server.rstrip("/"), tls, check, args, prefix
        self.added: list[str] = []
        self.undo: list[tuple[str, Callable[[], object]]] = []
        self.images_sent = 0
        self.served: dict[str, str] = {}      # deployment name -> model
        self.photo: Optional[bytes] = None
        self.photo_why = ""
        self.too_old: Optional[str] = None    # the recipe's image is older than 1.5.4: why (every deploy is skipped)

    def skip(self, label: str, why: str) -> None:
        self.check(label, True, why, skip=True)


def ensure(x: Ctx, name: str) -> bool:
    """The model in the catalog under its name (added pinned unless it is there). True when the run added it."""
    spec = MODELS[name]
    have = {m["name"]: m for m in x.c.ok("GET", "/api/models")["models"]}
    if name in have:
        if have[name]["task"] != spec["task"]:
            raise Fail(f"{name} is in the catalog as a {have[name]['task']} model: remove it first")
        return False
    m = x.c.ok("POST", "/api/models", {"name": name, "source": spec["source"], "revision": spec["revision"],
                                       "task": spec["task"]}, timeout=300)["model"]
    if m["task"] != spec["task"]:
        raise Fail(f"{name} was added as {m['task']}, not {spec['task']}")
    log(f"catalog: added {name} ({m['source']} @ {m['revision'][:12]}, recipes {m.get('recipes')})")
    return True


def new_key(x: Ctx, name: str, **body) -> str:
    """An API key the run removes at the end."""
    out = x.c.ok("POST", "/api/keys", {"name": name, **body})
    kid = (out.get("api_key") or {}).get("id")
    if kid is not None:
        x.undo.append((f"key {name}", lambda: x.c.call("DELETE", f"/api/keys/{kid}")))
    return out["key"]


def undo(x: Ctx) -> None:
    """Put back and remove what this run made, newest first, then the catalog entries it added."""
    for label, fn in reversed(x.undo):
        try:
            fn()
            log(f"cleanup: {label}")
        except Exception as e:   # noqa: BLE001 - every step is tried
            log(f"cleanup: {label} failed: {e}")
    x.undo.clear()
    remove_added(x)


def cleanup(x: Ctx) -> None:
    """The stand-alone script's sweep: also what an earlier run that was killed left (everything named accept-tf-*)."""
    undo(x)
    for d in x.c.ok("GET", "/api/deployments")["deployments"]:
        if d["name"].startswith(PREFIX):
            status, _ = x.c.call("DELETE", f"/api/deployments/{d['id']}")
            log(f"cleanup: deployment {d['name']} -> {status}")
    for k in x.c.ok("GET", "/api/keys")["keys"]:
        if k["name"].startswith(PREFIX.rstrip("-")) and not k.get("revoked_at"):
            x.c.call("DELETE", f"/api/keys/{k['id']}")
    for r in x.c.ok("GET", "/api/routes")["routes"]:
        if str(r.get("served_name", "")).startswith(PREFIX) and r.get("deployment_id") is None:
            x.c.call("DELETE", f"/api/routes/{r['id']}")


def remove_added(x: Ctx) -> None:
    if x.added:
        deadline = time.time() + 120
        for m in x.c.ok("GET", "/api/models")["models"]:
            if m["name"] in x.added:
                while True:
                    status, _ = x.c.call("DELETE", f"/api/models/{m['id']}")
                    if status != 409 or time.time() > deadline:
                        break
                    time.sleep(3)
                log(f"cleanup: catalog entry {m['name']} -> {status}")
        x.added.clear()


def wait_ready(x: Ctx, dep_id: int) -> tuple[Optional[dict], str]:
    """(the ready deployment, "") or (None, why): not placed within --place-timeout, not ready within --timeout, or a
    replica that ended."""
    t0 = time.monotonic()
    last = ""
    while True:
        d = x.c.ok("GET", f"/api/deployments/{dep_id}")["deployment"]
        reps = d["workload"]["replica_list"]
        text = "; ".join(f"#{r['ordinal']} {r['machine']['name'] if r.get('machine') else '-'} {r['state']}"
                         f"{': ' + str(r['reason'])[:120] if r.get('reason') else ''}" for r in reps)
        if text != last:
            log(f"  {d['name']}: {text}")
            last = text
        if reps and all(r["state"] == "healthy" for r in reps):
            return d, ""
        for r in reps:
            if r.get("final") and r["state"] in ("failed", "exited"):
                return None, f"replica {r['ordinal']} ended: {r.get('reason')}"
        waited = time.monotonic() - t0
        unplaced = any(not r.get("machine") for r in reps) or not reps
        if unplaced and waited > x.args.place_timeout:
            return None, f"not placed within {x.args.place_timeout:.0f} s: {last or 'no replica'}"
        if waited > x.args.timeout:
            return None, f"not ready within {x.args.timeout:.0f} s: {last}"
        time.sleep(5)


def deploy(x: Ctx, where: str, machine: dict, name: str) -> Optional[dict]:
    """Estimate, deploy and wait; None after a SKIP (no room, the recipe's image too old, not ready in time)."""
    spec = MODELS[name]
    dep_name = deployment_name(x.prefix, where, name)
    params: dict = {"gpus": 1 if where == "gpu" else 0}
    if where == "cpu":
        params["threads"] = x.args.threads
    if spec["task"] == "embed":
        params["max_images"] = MAX_IMAGES
    if spec["kind"] == "zero-shot":
        params["max_pairs"] = MAX_PAIRS
    body = {"name": dep_name, "model": name, "recipe": x.args.transformers_recipe, "params": params, "served_name": dep_name,
            "replicas": 1, "machine_id": machine["id"]}
    status, est = x.c.call("POST", "/api/deployments/estimate", body)
    if why := image_too_old(status, est):
        x.too_old = f"vision_image_too_old: {why}"
        x.skip(f"{dep_name}: deploy on {x.args.transformers_recipe}", x.too_old)
        return None
    if status != 200:
        x.check(f"{dep_name}: estimate on {x.args.transformers_recipe}", False, f"{status} {code(est)}: {est}")
        return None
    if not est["placement"]["fits"]:
        x.skip(f"{dep_name}: deploy", f"the estimate ({est['estimate']['amount'] / 1024**3:.2f} GiB) does not fit on "
                                      f"{machine['name']}: {est['placement'].get('summary')}")
        return None
    status, out = x.c.call("POST", "/api/deployments", body)
    if why := image_too_old(status, out):
        x.too_old = f"vision_image_too_old: {why}"
        x.skip(f"{dep_name}: deploy on {x.args.transformers_recipe}", x.too_old)
        return None
    if status != 201:
        x.check(f"{dep_name}: deploy", False, f"{status} {code(out)}: {out}")
        return None
    d0 = out["deployment"]
    x.undo.append((f"deployment {dep_name}", lambda i=d0["id"]: x.c.call("DELETE", f"/api/deployments/{i}")))
    placed = est["placement"]["placed"][0]
    log(f"{dep_name}: deployed on {placed['machine']} ({placed['variant']}), estimate {d0['estimate']['amount'] / 1024**3:.2f} GiB")
    t0 = time.monotonic()
    d, why = wait_ready(x, d0["id"])
    if d is None:
        x.skip(f"{dep_name}: ready on {machine['name']}", why) if why.startswith("not placed") else \
            x.check(f"{dep_name}: ready on {machine['name']}", False, why)
        return None
    x.check(f"{dep_name}: ready on {machine['name']} ({placed['variant']})", True, f"after {time.monotonic() - t0:.0f} s")
    x.served[dep_name] = name
    return d


def exercise_embed(x: Ctx, d: dict, name: str, key: str) -> None:
    spec = MODELS[name]
    dn = d["served_name"]
    red, blue = shape_png("square"), shape_png("circle")
    imgs = {"square": data_uri(red), "circle": data_uri(blue)}
    if spec["kind"] == "clip":
        texts = [t for t, _ in SHAPES]
        s, out, h, t = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": [*texts, {"image": imgs["square"]},
                                                                                    {"image": imgs["circle"]}]}, key, x.tls)
        v = vectors(out)
        ok = s == 200 and len(v) == 4
        x.images_sent += 2 if s == 200 else 0
        sims = [nearest(v[i], v[2:])[1] for i in range(2)] if ok else []
        x.check(f"{dn}: each shape's text is closest to its own picture", ok and all(nearest(v[i], v[2:])[0] == i for i in range(2)),
                f"{s} in {t:.2f} s: {[[round(c, 3) for c in row] for row in sims] or code(out)}")
        x.check(f"{dn}: every vector has length 1, {len(v[0]) if v else '?'} dimensions", ok and unit_lengths(v), f"{len(v)} vectors")
        x.check(f"{dn}: the answer comes from the local replica", s == 200 and (h.get("x-forgeyard-target") or "").startswith("local:"),
                h.get("x-forgeyard-target") or "no X-Forgeyard-Target")
        if x.photo is None:
            x.skip(f"{dn}: \"{CATS_TEXT}\" finds the cats photo", x.photo_why)
        else:
            cats = data_uri(x.photo, "image/jpeg")
            s, out, _, t = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": [CATS_TEXT, {"image": imgs["square"]}, {"image": cats},
                                                                                       {"image": imgs["circle"]}]}, key, x.tls)
            v = vectors(out)
            x.images_sent += 3 if s == 200 else 0
            best, sims = nearest(v[0], v[1:]) if s == 200 and len(v) == 4 else (-1, [])
            x.check(f"{dn}: \"{CATS_TEXT}\" finds the cats photo among three pictures", best == 1,
                    f"{s} in {t:.2f} s: {[round(c, 3) for c in sims] or code(out)}")
            url_check(x, dn, key, v[2] if best >= 0 else None)
        s, out, _, _ = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": ["a text"]}, key, x.tls)
        x.check(f"{dn}: a plain OpenAI body (texts only) still works", s == 200 and len(vectors(out)) == 1, f"{s} {code(out)}")
    else:
        small = data_uri(shape_png("square", 160))
        s, out, _, t = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": [{"image": imgs["square"]}, {"image": small},
                                                                                    {"image": imgs["circle"]}]}, key, x.tls)
        v = vectors(out)
        x.images_sent += 3 if s == 200 else 0
        best, sims = nearest(v[0], v[1:]) if s == 200 and len(v) == 3 else (-1, [])
        x.check(f"{dn}: a picture's smaller copy is closer than another picture", best == 0,
                f"{s} in {t:.2f} s: {[round(c, 3) for c in sims] or code(out)}")
        x.check(f"{dn}: every vector has length 1, {len(v[0]) if v else '?'} dimensions", s == 200 and unit_lengths(v), f"{len(v)} vectors")
        s, out, _, _ = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": ["a text"]}, key, x.tls)
        x.check(f"{dn}: a text: 400 text_not_supported", s == 400 and code(out) == "text_not_supported", f"{s} {code(out)}")
        if x.photo is not None:
            url_check(x, dn, key, None)
    # The limits.
    s, out, _, _ = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": [{"image": imgs["square"]}] * (MAX_IMAGES + 1)}, key, x.tls)
    x.check(f"{dn}: {MAX_IMAGES + 1} images (max_images {MAX_IMAGES}): 400 too_many_images", s == 400 and code(out) == "too_many_images",
            f"{s} {code(out)}")
    s, out, _, _ = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": [{"image": "file:///etc/hostname"}]}, key, x.tls)
    x.check(f"{dn}: a file:// address: 400 media_url_refused", s == 400 and code(out) == "media_url_refused", f"{s} {code(out)}")
    s, out, _, _ = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": [{"image": "not base64 at all!"}]}, key, x.tls)
    x.check(f"{dn}: an image that is not base64, a data: URI or an address: 400 invalid_media_data (the gateway)",
            s == 400 and code(out) == "invalid_media_data", f"{s} {code(out)}")
    s, out, _, _ = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": [{"image": "data:image/png;base64,bm90IGFuIGltYWdl"}]}, key, x.tls)
    x.check(f"{dn}: base64 bytes that are not a picture: 400 invalid_image (the engine)", s == 400 and code(out) == "invalid_image",
            f"{s} {code(out)}")


def url_check(x: Ctx, dn: str, key: str, inline: Optional[list[float]]) -> None:
    """The photo by its address, fetched by the gateway: answered, and (CLIP) the same vector as the photo sent inline."""
    url = x.args.image_url
    s, out, _, t = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": [{"image": url}]}, key, x.tls)
    if s == 400 and code(out) in ("media_url_refused", "media_fetch_failed"):
        x.skip(f"{dn}: the photo by its address, fetched by the gateway", f"{code(out)}: {(out.get('error') or {}).get('message')} "
               "(a server without internet, or MEDIA_ALLOW_PRIVATE_HOSTS off for a private address)")
        return
    v = vectors(out)
    x.images_sent += 1 if s == 200 else 0
    same = inline is not None and len(v) == 1 and cosine(inline, v[0]) > 0.999
    x.check(f"{dn}: the photo by its address, fetched by the gateway" + (" (the vector of the photo sent inline)" if inline else ""),
            s == 200 and len(v) == 1 and (inline is None or same),
            f"{s} in {t:.2f} s" + (f", cosine {cosine(inline, v[0]):.6f}" if inline is not None and len(v) == 1 else f" {code(out) or ''}"))


def exercise_classify(x: Ctx, d: dict, name: str, key: str) -> None:
    spec = MODELS[name]
    dn = d["served_name"]
    if spec["kind"] == "fixed":
        s, out, _, t = post_v1(x.server, "/v1/classify", {"model": dn, "input": [t for t, _ in SENTIMENT]}, key, x.tls)
        got = [dd.get("label") for dd in sorted(out.get("data") or [], key=lambda dd: dd.get("index", 0))]
        x.check(f"{dn}: a Chinese and an English review labelled", s == 200 and got == [g for _, g in SENTIMENT],
                f"{s} in {t:.2f} s: {got or code(out)}")
        s, out, _, _ = post_v1(x.server, "/v1/classify", {"model": dn, "input": "x", "labels": ["a", "b"]}, key, x.tls)
        x.check(f"{dn}: labels for a fixed-label model: 400 labels_not_supported", s == 400 and code(out) == "labels_not_supported",
                f"{s} {code(out)}")
        return
    if spec.get("english"):
        s, out, _, t = post_v1(x.server, "/v1/classify", {"model": dn, "input": [t for t, _ in EN], "labels": EN_LABELS}, key, x.tls)
        right = topics_right(out, [g for _, g in EN], EN_LABELS) if s == 200 else 0
        x.check(f"{dn}: two English texts among {', '.join(EN_LABELS)}", right == len(EN), f"{s} in {t:.2f} s: {right}/{len(EN)}")
    else:
        body = {"model": dn, "input": [t for t, _ in ZH], "labels": ZH_LABELS, "hypothesis_template": ZH_TEMPLATE}
        s, out, _, t = post_v1(x.server, "/v1/classify", body, key, x.tls)
        right = topics_right(out, [g for _, g in ZH], ZH_LABELS) if s == 200 else 0
        x.check(f"{dn}: 12 Traditional Chinese lines among 10 topics, a Chinese template: at least {x.args.zh_pass}/12",
                s == 200 and right >= x.args.zh_pass and out.get("labels") == ZH_LABELS,
                f"{s} in {t:.2f} s: {right}/12, labels echoed {out.get('labels') == ZH_LABELS}, usage {out.get('usage')}")
        body.pop("hypothesis_template")
        s, out, _, t = post_v1(x.server, "/v1/classify", body, key, x.tls)
        x.check(f"{dn}: the same with the English default template", s == 200,
                f"{topics_right(out, [g for _, g in ZH], ZH_LABELS)}/12 in {t:.2f} s", info=True)
    s, out, _, _ = post_v1(x.server, "/v1/classify", {"model": dn, "input": MULTI_TEXT, "labels": MULTI_LABELS, "multi_label": True}, key, x.tls)
    probs = ((out.get("data") or [{}])[0]).get("probs") or []
    x.check(f"{dn}: multi-label scores each label on its own", s == 200 and out.get("multi_label") is True and len(probs) == 3
            and all(0 <= p <= 1 for p in probs) and (spec.get("english") or probs[0] > 0.5),
            f"{s}: {dict(zip(MULTI_LABELS, [round(p, 3) for p in probs]))}")
    s, out, _, _ = post_v1(x.server, "/v1/classify", {"model": dn, "input": "x"}, key, x.tls)
    x.check(f"{dn}: no labels: 400 labels_required", s == 400 and code(out) == "labels_required", f"{s} {code(out)}")
    s, out, _, _ = post_v1(x.server, "/v1/classify", {"model": dn, "input": ["x"] * 13, "labels": ZH_LABELS}, key, x.tls)
    x.check(f"{dn}: 13 texts x 10 labels (max_pairs {MAX_PAIRS}): 400 too_many_pairs", s == 400 and code(out) == "too_many_pairs",
            f"{s} {code(out)}")


TRY = {"clip": {"input": "a red square"}, "image-only": {}, "fixed": {"input": "I love it"},
       "zero-shot": {"input": "台積電營收創新高", "labels": ["財經", "運動", "料理"]}}


def exercise_common(x: Ctx, d: dict, name: str, narrow: Optional[str]) -> None:
    import replica_port

    spec = MODELS[name]
    dn = d["served_name"]
    path = "/v1/embeddings" if spec["task"] == "embed" else "/v1/classify"
    body = {"model": dn, "input": "a text"} if spec["kind"] != "image-only" else {"model": dn, "input": [{"image": data_uri(shape_png("square", 64))}]}
    if spec["kind"] == "zero-shot":
        body["labels"] = ["a", "b"]
    s, out, _, _ = post_v1(x.server, path, body, None, x.tls)
    x.check(f"{dn}: no key: 401", s == 401, f"{s} {code(out)}")
    if narrow is None:
        x.skip(f"{dn}: a key without the model: 403", "no other model on this server to allow")
    else:
        s, out, _, _ = post_v1(x.server, path, body, narrow, x.tls)
        x.check(f"{dn}: a key without the model: 403", s == 403 and code(out) == "model_not_allowed", f"{s} {code(out)}")
    rp = replica_port.Replicas.of(x.c, x.server, x.tls)
    for e in d.get("endpoints") or []:
        p = rp.endpoint(e)
        what = f"{dn}: replica {e['replica']} on {e['machine']}"
        replica_port.record(rp.transport_checks(p, what), x.check)
        s, text = rp.post(p, "/v1/embeddings" if spec["task"] == "embed" else "/classify", json.dumps(body).encode(), "application/json", timeout=15)
        x.check(f"{what}: its own port without its key: 401 from the key guard", s == 401 and "replica_key_required" in text, f"{s} {text[:80]}")
    s, out = x.c.call("POST", f"/api/deployments/{d['id']}/try", TRY[spec["kind"]], timeout=300)
    x.check(f"{dn}: Try it", s == 200 and isinstance(out, dict) and out.get("ok"), f"{s}: {(out or {}).get('summary') if isinstance(out, dict) else out}")
    pg = dict(body)
    if spec["kind"] == "clip":
        pg["input"] = ["a red square", {"image": data_uri(shape_png("square", 64))}]
    s, out = x.c.call("POST", f"/api/playground/{path.rsplit('/', 1)[1]}", pg, timeout=300)
    x.check(f"{dn}: the Playground's route (signed in, no key)", s == 200 and isinstance(out, dict) and out.get("data"), f"{s} {code(out)}")


def exercise_cloud(x: Ctx, d: dict, key_on: str, provider: dict) -> None:
    """With a provider that has an embeddings model: a request with images stays local on a route that may spill, and a
    route of providers only refuses images. Both routes are put back."""
    pm = next((m for m in provider.get("models") or [] if m.get("api") == "openai-embeddings"), None)
    dn = d["served_name"]
    if pm is None:
        x.skip(f"{dn}: images never go to a cloud provider", f"{provider['name']} has no embeddings model")
        return
    route = next(r for r in x.c.ok("GET", "/api/routes")["routes"] if r["served_name"] == dn)
    local_t = {"tier": 0, "kind": "local", "deployment_id": d["id"]}
    x.c.ok("PUT", f"/api/routes/{route['id']}", {"targets": [local_t, {"tier": 1, "kind": "provider", "provider_model_id": pm["id"]}],
                                                  "spill_on_long_prompt": True})
    x.undo.append((f"route {dn} back to its deployment only", lambda: x.c.ok("PUT", f"/api/routes/{route['id']}", {"targets": [local_t]})))
    s, out, h, _ = post_v1(x.server, "/v1/embeddings", {"model": dn, "input": ["a red square", {"image": data_uri(shape_png("square"))}]},
                           key_on, x.tls)
    x.images_sent += 1 if s == 200 else 0
    x.check(f"{dn}: with {provider['name']} as a second tier, a request with images stays local",
            s == 200 and (h.get("x-forgeyard-target") or "").startswith("local:"), f"{s} {h.get('x-forgeyard-target') or code(out)}")
    name = f"{x.prefix}cloud-embed"
    r = x.c.ok("POST", "/api/routes", {"served_name": name, "api": "openai-embeddings",
                                      "targets": [{"tier": 0, "kind": "provider", "provider_model_id": pm["id"]}]})["route"]
    x.undo.append((f"route {name}", lambda: x.c.call("DELETE", f"/api/routes/{r['id']}")))
    s, out, _, _ = post_v1(x.server, "/v1/embeddings", {"model": name, "input": [{"image": data_uri(shape_png("circle"))}]}, key_on, x.tls)
    x.check(f"{name} (providers only): images: 400 images_not_supported", s == 400 and code(out) == "images_not_supported", f"{s} {code(out)}")


def usage(x: Ctx, key_name: str) -> None:
    x.c.ok("GET", "/api/usage?everyone=true&group=key")
    time.sleep(2)
    rows = x.c.ok("GET", "/api/usage?everyone=true&group=key")["rows"]
    row = next((r for r in rows if str(r.get("name")).startswith(f"{key_name} (")), {})
    if x.images_sent:
        x.check("usage of the run's key: the images sent in answered embedding requests, with kilopixels",
                row.get("images") == x.images_sent and (row.get("kilopixels") or 0) > 0,
                f"images {row.get('images')} (expected {x.images_sent}), kilopixels {row.get('kilopixels')}, requests {row.get('requests')}, "
                f"errors {row.get('errors')}")
    by_model = {r["id"]: r for r in x.c.ok("GET", "/api/usage?everyone=true&group=model")["rows"]}
    for dn, name in x.served.items():
        r = by_model.get(dn, {})
        need = "busy_ms" if MODELS[name]["kind"] == "image-only" else "prompt_tokens"
        x.check(f"usage of {dn}: requests, {need} and busy time", (r.get("requests") or 0) > 0 and (r.get(need) or 0) > 0
                and (r.get("busy_ms") or 0) > 0, f"requests {r.get('requests')}, prompt tokens {r.get('prompt_tokens')}, images "
                f"{r.get('images')}, busy {r.get('busy_ms')} ms")


def run(x: Ctx, machines: dict[str, Optional[dict]]) -> None:
    """Everything above, for each (where, model) of the plan. `machines`: {"cpu": machine or None, "gpu": …}."""
    names = [n for n in x.args.only.split(",") if n]
    gpu_names = [n for n in (x.args.gpu_only or "").split(",") if n]
    unknown = [n for n in names + gpu_names if n not in MODELS]
    if unknown:
        raise Fail(f"unknown model(s) {unknown}; known: {', '.join(MODELS)}")
    recipes = {r["name"]: r for r in x.c.ok("GET", "/api/models")["recipes"]}
    if x.args.transformers_recipe not in recipes:
        x.skip("transformers models", f"the server has no {x.args.transformers_recipe} recipe (older than 1.5.4, or no such custom recipe)")
        return
    x.photo, x.photo_why = photo(x.args.photo)
    todo = plan(names, gpu_names, machines.get("cpu"), machines.get("gpu"))
    if not todo:
        x.skip("transformers models", "no --cpu-machine or --gpu-machine given")
        return
    key_name = f"{x.prefix}{uuid.uuid4().hex[:6]}"
    key = new_key(x, key_name)
    key_on = None
    provider = None
    if x.args.cloud_provider:
        provider = next((p for p in x.c.ok("GET", "/api/providers")["providers"] if p["name"] == x.args.cloud_provider), None)
        if provider is None:
            raise Fail(f"no provider named {x.args.cloud_provider}")
        key_on = new_key(x, f"{key_name}-cloud", cloud="on")
    for where, name in todo:
        m = machines[where]
        if name not in x.added and ensure(x, name):
            x.added.append(name)
        if x.too_old:
            x.skip(f"{deployment_name(x.prefix, where, name)}: deploy on {x.args.transformers_recipe}", x.too_old)
            continue
        d = deploy(x, where, m, name)
        if d is None:
            continue
        others = [mm["name"] for mm in x.c.ok("GET", "/api/keys")["models"] if mm["name"] != d["served_name"]]
        narrow = new_key(x, f"{key_name}-narrow-{len(x.served)}", models=[others[0]]) if others else None
        if MODELS[name]["task"] == "embed":
            exercise_embed(x, d, name, key)
            if provider is not None and MODELS[name]["kind"] == "clip":
                exercise_cloud(x, d, key_on, provider)
        else:
            exercise_classify(x, d, name, key)
        exercise_common(x, d, name, narrow)
        if not x.args.keep:
            status, _ = x.c.call("DELETE", f"/api/deployments/{d['id']}")
            log(f"done with {d['name']}: deployment removed -> {status}")
    if x.served:
        usage(x, key_name)


def main() -> int:
    import accept_m3

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860")
    ap.add_argument("--cacert", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--cpu-machine", help="the machine for the CPU models")
    ap.add_argument("--gpu-machine", help="a machine with a GPU: --gpu-only's models on the CUDA variant")
    ap.add_argument("--only", default=DEFAULT_ONLY, help=f"models on the CPU machine, comma-separated, of {', '.join(MODELS)}")
    ap.add_argument("--gpu-only", default=DEFAULT_GPU, help="models on the GPU machine")
    ap.add_argument("--transformers-recipe", default=RECIPE, help="the recipe to deploy them with: a custom one names another "
                                                                   "vision image (1.5.4 or newer) or a process variant")
    ap.add_argument("--photo", help="the cats photo (default: COCO 000000039769, downloaded)")
    ap.add_argument("--image-url", default=COCO_CATS, help="an image address the server fetches (default: the COCO photo)")
    ap.add_argument("--cloud-provider", help="a provider with an embeddings model: images never go to it")
    ap.add_argument("--zh-pass", type=int, default=11, help="how many of the 12 Chinese lines the zero-shot model must get right")
    ap.add_argument("--threads", type=int, default=4, help="CPU threads for the models on the CPU")
    ap.add_argument("--timeout", type=float, default=2400, help="seconds a deployment may take to download and start")
    ap.add_argument("--place-timeout", type=float, default=300, help="seconds a deployment may wait for room on its machine")
    ap.add_argument("--keep", action="store_true", help="leave the deployments running afterwards")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args()
    if not args.cpu_machine and not args.gpu_machine:
        raise Fail("give --cpu-machine, --gpu-machine or both")
    accept_m3.use_cacert(args.cacert)
    password = os.environ.get("FORGEYARD_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    c = accept_m3.Client(args.server)
    c.ok("POST", "/api/auth/login", {"username": args.user, "password": password})
    report: dict = {"server": args.server, "recipe": args.transformers_recipe, "checks": []}

    def check(label: str, ok, detail="", info: bool = False, skip: bool = False) -> None:
        report["checks"].append({"check": label, "ok": bool(ok), "detail": str(detail), "info": info or skip, "skip": skip})
        log(f"{'SKIP' if skip else 'INFO' if info else 'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))

    x = Ctx(c, args.server, accept_m3.TLS, check, args)
    machines = {"cpu": accept_m3.machine(c, args.cpu_machine) if args.cpu_machine else None,
                "gpu": accept_m3.machine(c, args.gpu_machine) if args.gpu_machine else None}
    for role, m in machines.items():
        if m is not None and (m["status"] != "online" or "models" not in (m.get("features") or [])):
            raise Fail(f"{m['name']} must be online and serve models; status {m['status']}, features {m.get('features')}")
    cleanup(x)
    try:
        run(x, machines)
    finally:
        if args.keep:
            log("--keep: leaving the deployments running; run again without --keep to remove them")
            x.undo = [u for u in x.undo if not u[0].startswith("deployment ")]
            x.added.clear()
            undo(x)
        else:
            cleanup(x)
    failed = [r for r in report["checks"] if not r["ok"] and not r["info"]]
    report["passed"] = len([r for r in report["checks"] if r["ok"] and not r["info"]])
    report["skipped"] = len([r for r in report["checks"] if r["info"]])
    report["failed"] = len(failed)
    log(f"{report['passed']} passed, {report['failed']} failed, {report['skipped']} skipped or information")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        log(f"FAIL  {e}")
        sys.exit(1)
