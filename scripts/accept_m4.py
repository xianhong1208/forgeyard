#!/usr/bin/env python3
"""M4 acceptance on real machines: the gateway, called with the official OpenAI Python SDK.

Through Forgeyard's API only (an admin session), it:

  1. deploys, on --machine's one allowed GPU, the chat model (Qwen3-4B, vLLM), the embedding and
     the rerank model (vLLM pooling) and faster-whisper (speaches); and the CPU GGUF (Qwen3-1.7B,
     llama.cpp) as TWO replicas, one on --machine's CPU and one on --second-machine;
  2. creates an API key and, with the official `openai` SDK against <server>/v1, calls chat,
     streamed chat, /v1/models, embeddings and a transcription; /v1/rerank with plain HTTP (it is not
     in the SDK);
  3. shows the balancing between the two CPU replicas (one at a time: they take turns; ten at once:
     both machines work), an rpm limit of 3 answering the 4th call with 429 and Retry-After, and a
     revoked key getting 401 on the very next call;
  4. calls every replica's own port without its internal key (must be refused), also on the paths
     vLLM itself leaves open (/invocations, /rerank, /pooling): the agent's key guard must answer 401
     there too;
  5. checks model allowlists: a key limited to one deployment lists and reaches only that one (403
     model_not_allowed for the others); a key made before any of the deployments, with no list,
     sees and reaches every one of them;
  6. with --kill-local, kills the engine of the CPU replica that runs on THIS computer (the second
     machine's agent) and shows that every request is still answered (failover);

then removes everything it created (deployments, catalog entries, keys) and prints a report (also
JSON with --out).

    FORGEYARD_PASSWORD=… python3 scripts/accept_m4.py --server https://127.0.0.1:7860 --cacert forgeyard-ca.pem \\
        --user <admin> --machine <GPU host> --second-machine <other machine> --out accept-m4.json [--kill-local]

With Forgeyard's own CA, --cacert is the CA file (from "Trust this server", or data/tls/ca.crt on
the server): the SDK gets it the documented way, http_client=httpx.Client(verify=<SSL context>).

The official SDK: if `openai` cannot be imported, the script makes its own virtual environment
(--venv, default ~/.cache/forgeyard-accept-m4; with uv when it is installed, else python -m venv and
pip), installs `openai` there and runs itself again with it. Nothing else is installed anywhere.

It refuses to run unless --machine is online, serves models and Forgeyard may use exactly one of its
GPUs, and --second-machine is another online machine that serves models.
"""

from __future__ import annotations

import argparse
import collections
import getpass
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept_m3  # noqa: E402
from accept_m3 import CLIP, GIB, MODELS as M3_MODELS, Client, Fail, ensure_model, log, machine, the_card  # noqa: E402

SDK_SPEC = "openai>=2,<4"
GPU_KEYS = ("chat", "embed", "rerank", "asr")


# ------------------------------------------------------------------ the SDK in its own venv
def ensure_sdk(venv: Path) -> None:
    try:
        import openai  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("FORGEYARD_ACCEPT_REEXEC"):
        raise Fail(f"the openai SDK is still missing in {venv}")
    py = venv / "bin" / "python"
    if not py.exists():
        log(f"the official openai SDK is not installed here: making a virtual environment in {venv}")
        venv.parent.mkdir(parents=True, exist_ok=True)
        if shutil.which("uv"):
            subprocess.run(["uv", "venv", "--quiet", str(venv)], check=True)
            subprocess.run(["uv", "pip", "install", "--quiet", "--python", str(py), SDK_SPEC], check=True)
        else:
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            subprocess.run([str(py), "-m", "pip", "install", "--quiet", SDK_SPEC], check=True)
    os.environ["FORGEYARD_ACCEPT_REEXEC"] = "1"
    os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


# ------------------------------------------------------------------ helpers
def raw_post(url: str, body: bytes, ctype: str, headers: dict | None = None, timeout: float = 60) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=accept_m3.TLS) as resp:
            return resp.status, resp.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(4000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def multipart(fields: dict[str, str], file_field: str, path: Path) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = [f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in fields.items()]
    parts.append((f"--{b}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{path.name}\"\r\n"
                  "Content-Type: audio/mpeg\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def wait_all_ready(c: Client, dep_id: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        d = c.ok("GET", f"/api/deployments/{dep_id}")["deployment"]
        reps = d["workload"]["replica_list"]
        text = "; ".join(f"#{r['ordinal']} {r['machine']['name'] if r.get('machine') else '-'} {r['state']}"
                         f"{': ' + r['reason'] if r.get('reason') else ''}" for r in reps)
        if text != last:
            log(f"  {d['name']}: {text}")
            last = text
        if reps and all(r["state"] == "healthy" for r in reps):
            return d
        for r in reps:
            if r.get("final") and r["state"] in ("failed", "exited"):
                raise Fail(f"{d['name']} replica {r['ordinal']} ended: {r.get('reason')}")
        time.sleep(5)
    raise Fail(f"deployment {dep_id} not ready within {timeout:.0f} s (last: {last})")


def replica_of(headers) -> int:
    return int(headers["x-forgeyard-replica"].split("#")[1])


def children(pid: int) -> list[int]:
    out = []
    for e in os.listdir("/proc"):
        if e.isdigit():
            try:
                stat = Path(f"/proc/{e}/stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(stat[1]) == pid:
                out.append(int(e))
    return out


def _call(client, model: str, no_think: dict):
    """One small request to `model` with `client`: 200, or "<status> <code>" when refused."""
    import openai

    key = model.removeprefix("accept-")
    try:
        if key == "embed":
            client.embeddings.create(model=model, input="x")
        elif key == "asr":
            with CLIP.open("rb") as fh:
                client.audio.transcriptions.create(model=model, file=fh)
        elif key == "rerank":  # not in the SDK
            status, text = raw_post(str(client.base_url).rstrip("/") + "/rerank",
                                    json.dumps({"model": model, "query": "q", "documents": ["a", "b"]}).encode(),
                                    "application/json", {"Authorization": f"Bearer {client.api_key}"})
            if status != 200:
                try:
                    return f"{status} {json.loads(text)['error']['code']}"
                except (ValueError, KeyError, TypeError):
                    return f"{status} {text[:60]}"
        else:  # chat; also any other deployment, which a key outside its list is refused before the endpoint
            client.chat.completions.create(model=model, max_tokens=2, extra_body=no_think,
                                           messages=[{"role": "user", "content": "hi"}])
        return 200
    except openai.APIStatusError as e:
        body = e.body if isinstance(e.body, dict) else {}
        code = body.get("code") or (body.get("error") or {}).get("code") if body else None
        return f"{e.status_code} {code or str(e.body)[:60]}"


# ------------------------------------------------------------------ the run
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860")
    ap.add_argument("--cacert", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--machine", required=True, help="the GPU machine (its allowlist must be exactly one GPU)")
    ap.add_argument("--second-machine", required=True, help="the other machine, for the second CPU replica")
    ap.add_argument("--only", default="chat,embed,rerank,asr,cpu", help="which of chat,embed,rerank,asr,cpu")
    ap.add_argument("--timeout", type=float, default=2400, help="seconds per deployment to become ready")
    ap.add_argument("--kill-local", action="store_true",
                    help="kill the engine of the CPU replica running on this computer to show failover")
    ap.add_argument("--venv", default=str(Path.home() / ".cache" / "forgeyard-accept-m4"),
                    help="where to put a virtual environment with the openai SDK if it is not installed")
    ap.add_argument("--keep", action="store_true", help="leave the deployments running afterwards")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args()
    ensure_sdk(Path(args.venv))
    accept_m3.use_cacert(args.cacert)
    import httpx
    import openai

    def sdk_client(key: str) -> "openai.OpenAI":
        """The official SDK; with Forgeyard's own CA, trusting it through its own httpx client."""
        http = httpx.Client(verify=accept_m3.TLS, timeout=300) if accept_m3.TLS else None
        return openai.OpenAI(base_url=gw, api_key=key, max_retries=0, timeout=300, http_client=http)

    password = os.environ.get("FORGEYARD_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    c = Client(args.server)
    c.ok("POST", "/api/auth/login", {"username": args.user, "password": password})
    m = machine(c, args.machine)
    m2 = machine(c, args.second_machine)
    for x in (m, m2):
        if x["status"] != "online" or "models" not in (x.get("features") or []):
            raise Fail(f"{x['name']} must be online and serve models; status {x['status']}, features {x.get('features')}")
    if m["id"] == m2["id"]:
        raise Fail("--second-machine must be another machine")
    for x in (m, m2):
        if "replica_keys" not in (x.get("features") or []):
            log(f"WARNING: {x['name']}'s agent does not give engines their internal key (an agent from before M4): "
                "update and restart it, or direct calls to its replicas will not be refused")
        elif "replica_guard" not in (x.get("features") or []):
            log(f"WARNING: {x['name']}'s agent has no key guard (an M4 agent): vLLM's /invocations, /rerank and "
                "/pooling stay open on its replica ports; update and restart it")
    card = the_card(m)
    log(f"{m['name']}: Forgeyard may use only GPU #{card['index']} ({card['model']}), "
        f"{card['vram_free'] / GIB:.2f} GiB free; second machine {m2['name']}")
    wanted = [s for s in M3_MODELS if s["key"] in args.only.split(",")]
    added, created, keys = [], [], []
    report: dict = {"server": args.server, "machine": m["name"], "second_machine": m2["name"], "card": card["index"],
                    "sdk": f"openai {openai.__version__}", "checks": [], "deployments": {}}

    def check(name: str, ok: bool, detail="", info: bool = False) -> None:
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail, "info": info})
        tag = "INFO" if info else ("PASS" if ok else "FAIL")
        log(f"{tag}  {name}" + (f"  ({detail})" if detail else ""))

    try:
        # A key with no model list, made before any deployment exists: it must cover them all.
        k_all = c.ok("POST", "/api/keys", {"name": "accept-m4-all"})
        keys.append(k_all["api_key"]["id"])
        for spec in wanted:
            if ensure_model(c, spec):
                added.append(spec["model"])
        deps: dict[str, dict] = {}
        for spec in wanted:
            name = f"accept-{spec['key']}"
            cpu = spec["key"] == "cpu"
            body = {"name": name, "model": spec["model"], "recipe": spec["recipe"], "params": spec["params"],
                    "served_name": name, "replicas": 2 if cpu else 1, "machine_id": None if cpu else m["id"]}
            est = c.ok("POST", "/api/deployments/estimate", body)["placement"]
            if not est["fits"]:
                raise Fail(f"{name} does not fit: {est.get('summary')}")
            if cpu:
                where = sorted(p["machine"] for p in est["placed"])
                if where != sorted([m["name"], m2["name"]]):
                    raise Fail(f"{name}: the two replicas would go to {where}, not {m['name']} and {m2['name']} "
                               "(other machines online?); stopping")
            elif est["placed"][0]["gpus"] != [card["index"]]:
                raise Fail(f"{name} would go to GPU {est['placed'][0]['gpus']}, not #{card['index']}; stopping")
            dep = c.ok("POST", "/api/deployments", body)["deployment"]
            created.append(dep["id"])
            deps[spec["key"]] = dep
            log(f"{name}: deployed ({', '.join(p['machine'] + '/' + p['variant'] for p in est['placed'])})")
        for key, dep in deps.items():
            t0 = time.time()
            d = wait_all_ready(c, dep["id"], args.timeout)
            deps[key] = d
            report["deployments"][d["name"]] = {"ready_seconds": int(time.time() - t0),
                                               "endpoints": [{k: e.get(k) for k in ("replica", "machine", "url", "protected",
                                                                                    "guarded")}
                                                             for e in d["endpoints"]]}
            log(f"{d['name']}: ready after {time.time() - t0:.0f} s; replicas protected: "
                f"{[e.get('protected') for e in d['endpoints']]}, guarded: {[e.get('guarded') for e in d['endpoints']]}")

        # ---- an API key and the official SDK
        k = c.ok("POST", "/api/keys", {"name": "accept-m4"})
        keys.append(k["api_key"]["id"])
        gw = k["gateway_url"]
        sdk = sdk_client(k["key"])
        names = sorted(x.id for x in sdk.models.list())
        check("GET /v1/models lists the deployments", all(f"accept-{s['key']}" in names for s in wanted), ", ".join(names))
        no_think = {"chat_template_kwargs": {"enable_thinking": False}}
        for key in ("chat", "cpu"):
            if key not in deps:
                continue
            model = f"accept-{key}"
            t0 = time.time()
            out = sdk.chat.completions.create(model=model, max_tokens=48, temperature=0, extra_body=no_think,
                                              messages=[{"role": "user", "content": "Name three primary colours."}])
            text = (out.choices[0].message.content or "").strip()
            check(f"{model}: chat (SDK)", bool(text), f"{time.time() - t0:.1f} s, {out.usage.prompt_tokens}+"
                  f"{out.usage.completion_tokens} tokens: {text[:100]!r}")
            t0, first, pieces, chunks = time.time(), None, [], 0
            for ch in sdk.chat.completions.create(model=model, max_tokens=64, temperature=0, stream=True,
                                                  extra_body=no_think,
                                                  messages=[{"role": "user", "content": "Count from 1 to 10."}]):
                chunks += 1
                if ch.choices and ch.choices[0].delta.content:
                    first = first or time.time()
                    pieces.append(ch.choices[0].delta.content)
            check(f"{model}: streamed chat (SDK)", chunks > 3 and pieces, f"{chunks} chunks, first token after "
                  f"{(first or t0) - t0:.2f} s: {''.join(pieces).strip()[:80]!r}")
        if "embed" in deps:
            e = sdk.embeddings.create(model="accept-embed", input=["Forgeyard runs models on your machines.", "GPUs."])
            check("accept-embed: embeddings (SDK)", len(e.data) == 2, f"{len(e.data[0].embedding)} dimensions, "
                  f"{e.usage.prompt_tokens} tokens")
        if "rerank" in deps:
            docs = ["The Eiffel Tower was completed in 1889.", "Paris is the capital and largest city of France.",
                    "Berlin is the capital of Germany."]
            s, text = raw_post(gw + "/rerank", json.dumps({"model": "accept-rerank", "query": "What is the capital of France?",
                                                           "documents": docs}).encode(), "application/json",
                               {"Authorization": f"Bearer {k['key']}"})
            ranked = sorted(json.loads(text).get("results") or [], key=lambda r: -r["relevance_score"]) if s == 200 else []
            check("accept-rerank: /v1/rerank (HTTP; not in the SDK)", ranked and ranked[0]["index"] == 1,
                  f"{s}: {[(r['index'], round(r['relevance_score'], 3)) for r in ranked]}")
        if "asr" in deps:
            with CLIP.open("rb") as fh:
                t0 = time.time()
                tr = sdk.audio.transcriptions.create(model="accept-asr", file=fh)
            check("accept-asr: transcription (SDK)", "ask not" in tr.text.lower(), f"{time.time() - t0:.1f} s: {tr.text.strip()!r}")

        # ---- balancing between the two CPU replicas
        if "cpu" in deps:
            machines = {e["replica"]: e["machine"] for e in deps["cpu"]["endpoints"]}
            seen = []
            for _ in range(8):
                raw = sdk.chat.completions.with_raw_response.create(model="accept-cpu", max_tokens=4, extra_body=no_think,
                                                                   messages=[{"role": "user", "content": "Hi"}])
                seen.append(replica_of(raw.headers))
            check("one request at a time: the two CPU replicas take turns",
                  all(a != b for a, b in zip(seen, seen[1:])), " ".join(machines[s] for s in seen))
            counts: collections.Counter = collections.Counter()
            lock = threading.Lock()

            def one():
                raw = sdk.chat.completions.with_raw_response.create(
                    model="accept-cpu", max_tokens=48, extra_body=no_think,
                    messages=[{"role": "user", "content": "Tell me one fact about the sea."}])
                with lock:
                    counts[machines[replica_of(raw.headers)]] += 1

            threads = [threading.Thread(target=one) for _ in range(10)]
            t0 = time.time()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            check("ten requests at once: both machines answer (least in flight)", len(counts) == 2,
                  f"{dict(counts)} in {time.time() - t0:.1f} s")

        # ---- model allowlists
        names_all = sorted(f"accept-{s['key']}" for s in wanted)
        every = sdk_client(k_all["key"])
        listed = sorted(x.id for x in every.models.list())
        check("a key with no list, made before the deployments, lists every one of them",
              all(n in listed for n in names_all), ", ".join(listed))
        allowed = "accept-cpu" if "cpu" in deps else names_all[0]
        others = [n for n in listed if n != allowed]  # every other deployment on the server, also ones not made here
        reach = {}
        for name in names_all:
            reach[name] = _call(every, name, no_think)
        check("a key with no list reaches every deployment", all(v == 200 for v in reach.values()), json.dumps(reach))
        k1 = c.ok("POST", "/api/keys", {"name": "accept-m4-one", "models": [allowed]})
        keys.append(k1["api_key"]["id"])
        one = sdk_client(k1["key"])
        listed = sorted(x.id for x in one.models.list())
        check(f"a key limited to {allowed}: GET /v1/models lists only it", listed == [allowed], ", ".join(listed))
        s1 = _call(one, allowed, no_think)
        check(f"a key limited to {allowed}: calling it works", s1 == 200, str(s1))
        if others:
            refused = {n: _call(one, n, no_think) for n in others}
            check(f"a key limited to {allowed}: every other deployment is 403 model_not_allowed",
                  all(v == "403 model_not_allowed" for v in refused.values()), json.dumps(refused))

        # ---- limits and revoking
        k3 = c.ok("POST", "/api/keys", {"name": "accept-m4-rpm3", "rpm": 3})
        keys.append(k3["api_key"]["id"])
        s3 = sdk_client(k3["key"])
        target = "accept-cpu" if "cpu" in deps else f"accept-{wanted[0]['key']}"
        codes, retry = [], None
        for _ in range(4):
            try:
                s3.chat.completions.create(model=target, max_tokens=2, messages=[{"role": "user", "content": "hi"}])
                codes.append(200)
            except openai.RateLimitError as e:
                codes.append(429)
                retry = e.response.headers.get("retry-after")
        check("rpm 3: the 4th call gets 429 with Retry-After", codes == [200, 200, 200, 429] and retry, f"{codes}, Retry-After {retry}")
        c.ok("DELETE", f"/api/keys/{k3['api_key']['id']}")
        try:
            s3.models.list()
            check("a revoked key gets 401 on the very next call", False, "it still worked")
        except openai.AuthenticationError as e:
            check("a revoked key gets 401 on the very next call", True, str((e.body or {}).get("code")))

        # ---- replicas cannot be used around the gateway
        probes = {"chat": ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2}),
                  "cpu": ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2}),
                  "embed": ("/v1/embeddings", {"input": "x"}),
                  "rerank": ("/v1/rerank", {"query": "q", "documents": ["a"]})}
        for key, d in deps.items():
            for e in d["endpoints"]:
                base = e["url"].rstrip("/")
                if key == "asr":
                    body, ctype = multipart({"model": d["route"]["upstream_model"]}, "file", CLIP)
                    s, text = raw_post(base + "/v1/audio/transcriptions", body, ctype)
                    # The guard answers 401; without it speaches itself answers 403 to a missing key.
                    ok = s == 401 if e.get("guarded") else s in (401, 403)
                else:
                    path, payload = probes[key]
                    s, text = raw_post(base + path, json.dumps({"model": d["route"]["upstream_model"], **payload}).encode(),
                                       "application/json")
                    ok = s == 401
                check(f"{d['name']} #{e['replica']} on {e['machine']}: direct call without the internal key is refused",
                      ok, f"{s} {text[:80]}")
        # The paths vLLM 0.32 itself leaves open: the agent's key guard must refuse them on every replica.
        open_paths = (("/invocations", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2}),
                      ("/rerank", {"query": "q", "documents": ["a"]}), ("/pooling", {"input": "x"}))
        for key, d in deps.items():
            for e in d["endpoints"]:
                base = e["url"].rstrip("/")
                for path, payload in open_paths:
                    s, text = raw_post(base + path, json.dumps({"model": d["route"]["upstream_model"], **payload}).encode(),
                                       "application/json")
                    check(f"{d['name']} #{e['replica']} on {e['machine']}: {path} without the key is refused by the guard",
                          s == 401 and "replica_key_required" in text, f"{s} {text[:80]}")
                s, text = raw_post(base + "/invocations", b"{}", "application/json", {"Authorization": f"Bearer {k['key']}"})
                check(f"{d['name']} #{e['replica']}: a client's API key does not open the replica's port", s == 401,
                      f"{s} {text[:60]}")

        # ---- failover
        if args.kill_local and "cpu" in deps:
            d = c.ok("GET", f"/api/deployments/{deps['cpu']['id']}")["deployment"]
            local = next((r for r in d["workload"]["replica_list"] if r.get("machine") and r["machine"]["name"] == m2["name"]), None)
            pid = local.get("pid") if local else None
            engine = children(pid) if pid and Path(f"/proc/{pid}").exists() else []
            if not engine:
                check("failover", False, f"the replica on {m2['name']} is not a process on this computer (pid {pid})")
            else:
                os.kill(engine[0], signal.SIGKILL)
                by_replica = {e["replica"]: e["machine"] for e in d["endpoints"]}
                t0, served, errors = time.time(), [], 0
                for _ in range(6):
                    try:
                        raw = sdk.chat.completions.with_raw_response.create(
                            model="accept-cpu", max_tokens=4, messages=[{"role": "user", "content": "Hi"}])
                        served.append(by_replica.get(replica_of(raw.headers)))
                    except openai.APIError:
                        errors += 1
                check(f"engine on {m2['name']} killed: every request is still answered", errors == 0,
                      f"{served} within {time.time() - t0:.1f} s")
                deadline, r = time.time() + 300, local
                while time.time() < deadline:
                    r = next(x for x in c.ok("GET", f"/api/deployments/{d['id']}")["deployment"]["workload"]["replica_list"]
                             if x["ordinal"] == local["ordinal"])
                    if r["state"] == "healthy" and r["pid"] != pid:
                        break
                    time.sleep(3)
                check(f"the agent on {m2['name']} restarts it", r["state"] == "healthy",
                      f"pid {pid} -> {r['pid']} after {time.time() - t0:.0f} s")
        usage = c.ok("GET", "/api/usage?days=1&group=model")
        report["usage_today"] = {row["name"]: {k2: row[k2] for k2 in ("requests", "errors", "limited", "prompt_tokens",
                                                                     "completion_tokens", "latency_p50_ms", "latency_p95_ms")}
                                 for row in usage["rows"]}
        report["ok"] = all(x["ok"] for x in report["checks"] if not x["info"])
    except Fail as e:
        report["ok"] = False
        report["error"] = str(e)
        log(f"FAILED: {e}")
    except openai.APIError as e:
        report["ok"] = False
        report["error"] = f"{type(e).__name__}: {e}"
        log(f"FAILED: {type(e).__name__}: {e}")
    finally:
        for key_id in keys:
            c.call("DELETE", f"/api/keys/{key_id}")
        if not args.keep:
            for dep_id in reversed(created):
                c.call("DELETE", f"/api/deployments/{dep_id}")
            for name in added:
                mid = next((x["id"] for x in c.ok("GET", "/api/models")["models"] if x["name"] == name), None)
                if mid:
                    c.call("DELETE", f"/api/models/{mid}")
        log(f"cleaned up: {len(keys)} keys revoked, {0 if args.keep else len(created)} deployments and "
            f"{0 if args.keep else len(added)} catalog entries removed")
    print()
    for x in report["checks"]:
        print(f"{'INFO' if x['info'] else ('PASS' if x['ok'] else 'FAIL')}  {x['check']}")
    passed = sum(1 for x in report["checks"] if x["ok"] and not x["info"])
    total = sum(1 for x in report["checks"] if not x["info"])
    print(f"\n{passed} of {total} checks passed" + ("" if report.get("ok") else f"; {report.get('error', 'see above')}"))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(2)
