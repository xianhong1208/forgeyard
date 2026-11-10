#!/usr/bin/env python3
"""M3 acceptance on a real machine, through Forgeyard's public API only (an admin session).

Deploys the five acceptance models onto ONE machine whose GPU allowlist is exactly one card,
one after the other, and for each:

  1. records the estimate (and vLLM's computed --gpu-memory-utilization) before deploying;
  2. waits until it is downloaded, started and healthy;
  3. calls its own endpoint like a client would (chat with and without streaming, embeddings,
     rerank, a transcription of a bundled public-domain clip, CPU chat);
  4. reads the card's used memory from the agent's inventory before and after, so the actual
     VRAM of each deployment is the difference (other people's work on the card adds noise: it
     is reported), and picks vLLM's own memory lines out of the replica's output;

then removes everything it created and prints a report (also written as JSON with --out).

    python3 scripts/accept_m3.py --server https://127.0.0.1:7860 --cacert forgeyard-ca.pem \
        --user owner --machine <name>
    (the password is read from FORGEYARD_PASSWORD, or asked for; forgeyard-ca.pem is the server's
    CA, from its "Trust this server" page or data/tls/ca.crt on the server)

It refuses to run unless the machine is online, serves models, and Forgeyard may use exactly one
of its GPUs. Nothing outside that card (and the machine's CPU) is ever touched. Standard library
only, so it runs with any python3.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

GIB = 1024**3
CLIP = Path(__file__).resolve().parent.parent / "forgeyard" / "models" / "assets" / "jfk-ask-not.mp3"
RERANK_OVERRIDES = '{"architectures": ["Qwen3ForSequenceClassification"], "classifier_from_token": ["no", "yes"], ' \
                   '"is_original_qwen3_reranker": true}'

# name, source, revision/file, task, recipe, deployment params, engine args (catalog)
MODELS = [
    {"key": "chat", "model": "accept-qwen3-4b", "source": "hf://Qwen/Qwen3-4B", "task": "chat", "recipe": "vllm",
     "params": {"context": 8192, "concurrency": 4, "share": "vram"}},
    {"key": "embed", "model": "accept-qwen3-embedding-0.6b", "source": "hf://Qwen/Qwen3-Embedding-0.6B", "task": "embed",
     "recipe": "vllm-pooling", "params": {}},
    {"key": "rerank", "model": "accept-qwen3-reranker-0.6b", "source": "hf://Qwen/Qwen3-Reranker-0.6B", "task": "rerank",
     "recipe": "vllm-pooling", "params": {},
     "engine_args": {"vllm-pooling": ["--hf-overrides", RERANK_OVERRIDES, "--chat-template", "${MODEL_PATH}/chat_template.jinja"]}},
    {"key": "asr", "model": "accept-faster-whisper-small", "source": "hf://Systran/faster-whisper-small", "task": "asr",
     "recipe": "faster-whisper", "params": {}},
    {"key": "cpu", "model": "accept-qwen3-1.7b-gguf", "source": "hf://ggml-org/Qwen3-1.7B-GGUF",
     "file": "Qwen3-1.7B-Q4_K_M.gguf", "task": "chat", "recipe": "llama-cpp",
     "params": {"gpus": 0, "context": 4096, "concurrency": 2, "threads": 8}},
]
VLLM_LINES = re.compile(r"(Model loading took|Available KV cache memory|GPU KV cache size|Maximum concurrency|"
                        r"Free memory on device|CUDA graph|memory profiling|requested memory|non_kv_cache)", re.I)


class Fail(Exception):
    pass


# The server's CA (--cacert): every https call verifies against it. None: the system trust store.
TLS: ssl.SSLContext | None = None


def use_cacert(path: str | None) -> None:
    global TLS
    TLS = ssl.create_default_context(cafile=path) if path else None


class Client:
    def __init__(self, server: str):
        self.server = server.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  urllib.request.HTTPSHandler(context=TLS))

    def call(self, method: str, path: str, body=None, timeout: float = 120.0):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.server + path, data=data, method=method,
                                     headers={"Accept": "application/json", "X-Forgeyard": "1",
                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None

    def ok(self, method: str, path: str, body=None, timeout: float = 120.0):
        status, data = self.call(method, path, body, timeout)
        if status >= 300:
            detail = (data or {}).get("detail") if isinstance(data, dict) else data
            raise Fail(f"{method} {path} -> {status}: {detail}")
        return data


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def post_raw(url: str, body: bytes, ctype: str, timeout: float = 300.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=TLS) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def machine(c: Client, name: str) -> dict:
    for m in c.ok("GET", "/api/machines")["machines"]:
        if m["name"] == name:
            return m
    raise Fail(f"no machine named {name!r}")


def the_card(m: dict) -> dict:
    allowed = [a for a in m["accelerators"] if a["vendor"] != "cpu" and a.get("allowed")]
    if m.get("gpu_allowlist") is None or len(allowed) != 1:
        raise Fail(f"{m['name']}: Forgeyard may use {len(allowed)} GPUs there (allowlist "
                   f"{'not set' if m.get('gpu_allowlist') is None else m['gpu_allowlist']}); this script needs "
                   "exactly one. Set it on the Machines page (Change GPU access) first.")
    return allowed[0]


def card_now(c: Client, name: str, key: str) -> dict:
    return next(a for a in machine(c, name)["accelerators"] if a["key"] == key)


def settle_card(c: Client, name: str, key: str, seconds: int = 25) -> dict:
    """Wait for two heartbeats so the inventory shows the memory as it is now."""
    time.sleep(seconds)
    return card_now(c, name, key)


def ensure_model(c: Client, spec: dict) -> bool:
    """Add the model to the catalog unless it is there. True if this script added it."""
    names = {m["name"] for m in c.ok("GET", "/api/models")["models"]}
    if spec["model"] in names:
        return False
    body = {"name": spec["model"], "source": spec["source"], "task": spec["task"], "title": spec["model"]}
    if spec.get("file"):
        body["file"] = spec["file"]
    if spec.get("engine_args"):
        body["engine_args"] = spec["engine_args"]
    m = c.ok("POST", "/api/models", body, timeout=300)["model"]
    log(f"catalog: added {m['name']} ({m['source']} @ {m['revision'][:12]}, {m['size_bytes'] / GIB:.2f} GiB)")
    return True


def wait_ready(c: Client, dep_id: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        d = c.ok("GET", f"/api/deployments/{dep_id}")["deployment"]
        r = d["workload"]["replica_list"][0]
        text = f"{r['state']}: {r.get('reason') or ''}"
        if text != last:
            log(f"  {d['name']}: {text}")
            last = text
        if r["state"] == "healthy":
            return d
        if r["state"] in ("failed", "exited") and r.get("final"):
            raise Fail(f"{d['name']} ended: {r.get('reason')}")
        time.sleep(5)
    raise Fail(f"deployment {dep_id} not healthy within {timeout:.0f} s (last: {last})")


def exercise(key: str, base: str, model: str) -> dict:
    """Call a replica's own endpoint like a client would."""
    out: dict = {}
    if key in ("chat", "cpu"):
        body = {"model": model, "max_tokens": 48, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": "Name three primary colours, comma-separated."}]}
        t = time.time()
        s, raw = post_raw(base + "/v1/chat/completions", json.dumps(body).encode(), "application/json")
        ans = json.loads(raw)
        out["chat"] = {"status": s, "ms": int((time.time() - t) * 1000), "usage": ans.get("usage"),
                       "text": ans["choices"][0]["message"].get("content", "")[:200]}
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        t = time.time()
        s, raw = post_raw(base + "/v1/chat/completions", json.dumps(body).encode(), "application/json")
        chunks = [ln[6:] for ln in raw.decode().splitlines() if ln.startswith("data: ") and ln != "data: [DONE]"]
        text = "".join(((json.loads(ch).get("choices") or [{}])[0].get("delta") or {}).get("content") or "" for ch in chunks)
        out["stream"] = {"status": s, "ms": int((time.time() - t) * 1000), "chunks": len(chunks), "text": text[:200],
                         "done": raw.decode().rstrip().endswith("[DONE]")}
        if s != 200 or len(chunks) < 2 or not out["chat"]["text"]:
            raise Fail(f"{key}: chat failed: {out}")
    elif key == "embed":
        body = {"model": model, "input": ["Forgeyard runs models on your own machines.", "GPUs are fast."]}
        s, raw = post_raw(base + "/v1/embeddings", json.dumps(body).encode(), "application/json")
        ans = json.loads(raw)
        out = {"status": s, "count": len(ans.get("data") or []), "dims": len(ans["data"][0]["embedding"])}
        if s != 200 or out["count"] != 2:
            raise Fail(f"embed failed: {raw[:300]!r}")
    elif key == "rerank":
        docs = ["The Eiffel Tower was completed in 1889.", "Paris is the capital and largest city of France.",
                "Berlin is the capital of Germany."]
        body = {"model": model, "query": "What is the capital of France?", "documents": docs}
        s, raw = post_raw(base + "/v1/rerank", json.dumps(body).encode(), "application/json")
        ans = json.loads(raw)
        res = sorted(ans.get("results") or [], key=lambda r: -r["relevance_score"])
        out = {"status": s, "ranking": [(r["index"], round(r["relevance_score"], 4)) for r in res]}
        if s != 200 or not res or res[0]["index"] != 1:
            raise Fail(f"rerank failed (expected document 1 first): {raw[:300]!r}")
    elif key == "asr":
        boundary = uuid.uuid4().hex
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{CLIP.name}\"\r\n"
             "Content-Type: audio/mpeg\r\n\r\n").encode() + CLIP.read_bytes() + b"\r\n",
            f"--{boundary}--\r\n".encode()])
        t = time.time()
        s, raw = post_raw(base + "/v1/audio/transcriptions", body, f"multipart/form-data; boundary={boundary}")
        text = json.loads(raw).get("text", "") if s == 200 else raw.decode()[:300]
        out = {"status": s, "ms": int((time.time() - t) * 1000), "text": text}
        if s != 200 or "ask not" not in text.lower():
            raise Fail(f"transcription failed: {text}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860")
    ap.add_argument("--cacert", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--machine", required=True, help="the machine to deploy on (its allowlist must be one GPU)")
    ap.add_argument("--only", default="chat,embed,rerank,asr,cpu", help="which of chat,embed,rerank,asr,cpu")
    ap.add_argument("--timeout", type=float, default=2400, help="seconds per deployment to become healthy")
    ap.add_argument("--keep", action="store_true", help="leave the deployments running afterwards")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args()
    use_cacert(args.cacert)
    password = os.environ.get("FORGEYARD_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")

    c = Client(args.server)
    c.ok("POST", "/api/auth/login", {"username": args.user, "password": password})
    m = machine(c, args.machine)
    if m["status"] != "online" or "models" not in (m.get("features") or []):
        raise Fail(f"{m['name']} must be online and serve models (agent started with --model-cache); "
                   f"status {m['status']}, features {m.get('features')}")
    card = the_card(m)
    log(f"{m['name']}: Forgeyard may use only GPU #{card['index']} ({card['model']}), "
        f"{card['vram_free'] / GIB:.2f} GiB free of {card['vram_total'] / GIB:.2f} GiB")
    wanted = [s for s in MODELS if s["key"] in args.only.split(",")]
    added, created, report = [], [], {"machine": m["name"], "card": card["index"], "results": []}
    try:
        for spec in wanted:
            if ensure_model(c, spec):
                added.append(spec["model"])
        for spec in wanted:
            name = f"accept-{spec['key']}"
            body = {"name": name, "model": spec["model"], "recipe": spec["recipe"], "params": spec["params"],
                    "machine_id": m["id"], "served_name": name}
            est = c.ok("POST", "/api/deployments/estimate", body)
            placed = est["placement"]["placed"]
            if not est["placement"]["fits"]:
                raise Fail(f"{name} does not fit: {est['placement'].get('summary')}")
            p = placed[0]
            if spec["key"] != "cpu" and p["gpus"] != [card["index"]]:
                raise Fail(f"{name} would go to GPU {p['gpus']}, not #{card['index']}; stopping")
            amount = est["estimate"]["amount"]
            log(f"{name}: estimate {amount / GIB:.2f} GiB {'per GPU' if spec['key'] != 'cpu' else 'RAM'} "
                f"({p['variant']}{', gpu_memory_utilization ' + p['gpu_memory_utilization'] if p.get('gpu_memory_utilization') else ''})")
            before = settle_card(c, m["name"], card["key"], seconds=1)
            dep = c.ok("POST", "/api/deployments", body)["deployment"]
            created.append(dep["id"])
            t0 = time.time()
            d = wait_ready(c, dep["id"], args.timeout)
            ready_s = time.time() - t0
            url = d["endpoints"][0]["url"].rstrip("/")
            upstream = d["route"]["upstream_model"]
            calls = exercise(spec["key"], url, upstream)          # also loads the whisper model (lazy)
            after = settle_card(c, m["name"], card["key"])
            used = (after["vram_used"] or 0) - (before["vram_used"] or 0)
            logs = c.ok("GET", f"/api/workloads/{d['workload']['id']}/replicas/0/logs?lines=2000")
            lines = [ln.strip()[:240] for ln in logs.get("text", "").splitlines() if VLLM_LINES.search(ln)]
            row = {"deployment": name, "model": spec["source"] + (f" ({spec['file']})" if spec.get("file") else ""),
                   "recipe": spec["recipe"], "variant": p["variant"], "image": p.get("image"),
                   "estimate_gib": round(amount / GIB, 2),
                   "gpu_memory_utilization": p.get("gpu_memory_utilization"),
                   "card_used_before_gib": round((before["vram_used"] or 0) / GIB, 2),
                   "card_used_after_gib": round((after["vram_used"] or 0) / GIB, 2),
                   "actual_delta_gib": round(used / GIB, 2) if spec["key"] != "cpu" else None,
                   "ready_seconds": int(ready_s), "endpoint": url, "calls": calls, "engine_memory_lines": lines[-8:]}
            report["results"].append(row)
            if spec["key"] == "cpu":
                log(f"{name}: healthy after {ready_s:.0f} s; RAM is not in the inventory per process: measure "
                    "`ps -o rss` of llama-server on the host (see docs/acceptance-m3.md)")
            else:
                log(f"{name}: healthy after {ready_s:.0f} s; card used {row['card_used_before_gib']} -> "
                    f"{row['card_used_after_gib']} GiB: actual {row['actual_delta_gib']} GiB vs estimate "
                    f"{row['estimate_gib']} GiB")
            log(f"  calls: {json.dumps(calls)[:400]}")
        final = card_now(c, m["name"], card["key"])
        report["card_free_at_the_end_gib"] = round((final["vram_free"] or 0) / GIB, 2)
        report["ok"] = True
    except Fail as e:
        report["ok"] = False
        report["error"] = str(e)
        log(f"FAILED: {e}")
    finally:
        if not args.keep:
            for dep_id in reversed(created):
                c.call("DELETE", f"/api/deployments/{dep_id}")
            for name in added:
                mid = next((x["id"] for x in c.ok("GET", "/api/models")["models"] if x["name"] == name), None)
                if mid:
                    c.call("DELETE", f"/api/models/{mid}")
            log(f"removed {len(created)} deployments and {len(added)} catalog entries this script created")
    print("\n| deployment | variant | estimate | gpu_memory_utilization | actual (card delta) | ready after |")
    print("|---|---|---|---|---|---|")
    for r in report["results"]:
        actual = f"{r['actual_delta_gib']} GiB" if r["actual_delta_gib"] is not None else "(RAM: measure with ps)"
        print(f"| {r['deployment']} | {r['variant']} | {r['estimate_gib']} GiB | {r['gpu_memory_utilization'] or '-'} | "
              f"{actual} | {r['ready_seconds']} s |")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(2)
