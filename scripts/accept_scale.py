#!/usr/bin/env python3
"""1.5.0 acceptance on real machines (#37): sleep when idle, waking (by a request, by Start, by the Playground),
spill while waking, cannot wake, Stop, automatic replicas with draining, and the start times and metrics behind them.

Through the server's API only (an admin session) and /v1 with plain HTTP calls, on the given machines. Every check
prints PASS, FAIL or SKIP (with the reason); the exit code is 0 when nothing failed, 1 when a check failed, 2 when
the run could not start. It is also the `scale` group of scripts/accept_all.py (same options, same report).

    FORGEYARD_PASSWORD=… python3 scripts/accept_scale.py --server https://<server>:7860 --ca forgeyard-ca.pem \\
        --user <admin> --cpu-machine cpu-a --second-cpu-machine cpu-b [--gpu-machine gpu-01] \\
        [--cloud-provider <provider>] [--stoppable-agent cpu-a --agent-stop '…' --agent-start '…'] --out r.json

The CPU model is Qwen3-1.7B GGUF on llama.cpp (the catalog entry accept_all.py and accept_m4.py use), so it runs on
any machine; --gpu-machine adds Qwen3-0.6B on vLLM there. The steps, numbered as in the report:

  1  sleep: a deployment on --cpu-machine (pinned there) with sleep on and --scale-idle-minutes (1) goes asleep
     after that time; its replicas stop (their port no longer answers), the scheduler's reservation on the machine
     is freed (GET /metrics against what the machine's other replicas hold), /v1/models still lists it without
     waking it, and the model stays in the machine's cache
  2  wake by request: --scale-parallel (20) requests at once wake it exactly once (one "Woken by a request" event,
     forgeyard_deployment_wakes_total{by="request"} +1, the generation +1), and every one is answered 200
  3  wake by Start (Wake now: event "Woken by <you>", by="person" +1) and by the Playground (a request from the
     signed-in admin: "Woken by a request from the Playground"), each answering afterwards
  4  spill while waking (only with --cloud-provider): the provider as tier 1 of the deployment's route; a key that
     may spill (fallback) is answered by the provider at once with X-Forgeyard-Spilled: waking (the one request that
     reaches the provider); a structured-output request (guided_json, which no provider keeps) with the same key
     waits and is answered locally; once ready the key is served locally again
  5  wake_wait short: --scale-wake-wait (10 s) on a model whose last start took longer: the request gets 503
     model_waking after about that time with a Retry-After of at least 5 s and at most its last start; the wake
     carries on and the model answers. A model that starts faster than that cannot show it (llama.cpp on a CPU
     starts in seconds): SKIP naming its start time; the vLLM model (--gpu-machine) runs it
  6  cannot wake (opt-in: only with --stoppable-agent NAME, --agent-stop CMD and --agent-start CMD, a machine this
     run may take offline; never a machine you were not told you may stop): while asleep, pinned to that machine,
     its agent is stopped; a request gets 503 cannot_wake at once naming the machine; once the agent is back the
     deployment starts and answers. Without the three options: SKIP, saying so
  7  Stop is not woken: Stop on the asleep deployment; a request gets 503 no_ready_replica (stopped) at once,
     nothing wakes, /v1/models no longer lists it
  8  automatic replicas (needs --cpu-machine and --second-cpu-machine, and room for two replicas, else SKIP): a
     deployment, not pinned, Automatic 1 to 2 with Concurrent requests 2 and a --scale-down-minutes (2) window;
     after its 2-minute pause, --scale-clients (4) clients add a second replica within about a minute, on the other
     machine; every request answered 200 and none twice (each replica's served count equals the requests the run
     sent it, and every answer's id is different). Then a long stream on the newest replica: the drain begins while
     it runs, short requests meanwhile go to the other replica, the stream ends complete, the replica is removed
     only after its last byte, and nothing is added back for a minute
  9  automatic replicas with sleep and a minimum of 0: sleep turned on (idle --scale-idle-minutes): it goes asleep
     with 1 replica and a request wakes it with 1
  10 start times and metrics: each deployment's last_start (seconds, from the agent or measured by the server);
     /metrics has forgeyard_replica_start_seconds, forgeyard_deployment_wakes_total,
     forgeyard_gateway_wake_wait_seconds, forgeyard_deployment_replicas, forgeyard_deployment_load,
     forgeyard_autoscale_total (up and down) and, after step 4, forgeyard_gateway_spills_total{reason="waking"}

Parts 1-5 and 7 (the sleepy deployment), 8-9 (automatic replicas) and the vLLM model run side by side, so the whole
run takes about 15 minutes when the machines already have the models; step 6 runs after them, step 7 last.

/metrics answers only a direct request from the server's own host unless the server has METRICS_TOKEN: run the
script there, or pass --metrics-token (FORGEYARD_METRICS_TOKEN); otherwise the metric checks are SKIPs saying so.
Replica ports are called the way the gateway reaches them (scripts/replica_port.py: TLS with the machine's binding
name when its agent serves TLS); a port this computer cannot reach at all is a SKIP.

Everything it creates is named accept-<run>-scale-… (deployments, their routes, API keys, the catalog entries it had
to add) and removed at the end, also after a failure or Ctrl-C; a stopped agent is started again. Waits are bounded:
a deployment the server cannot place is skipped after --place-timeout seconds with the server's reason (#203).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

sys.modules.setdefault("accept_scale", sys.modules[__name__])   # one module, run as a script or imported
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept_all as A  # noqa: E402

# The CPU model (accept_all.py's, so its catalog entry and the machines' caches are shared) and the vLLM one.
CPU_SPEC = dict(A.MODELS[0])
GPU_SPEC = {"key": "gpu", "device": "gpu", "model": "accept-qwen3-0.6b", "source": "hf://Qwen/Qwen3-0.6B",
            "task": "chat", "recipe": "vllm", "params": {"context": 4096, "concurrency": 4, "share": "vram"}}

IDLE_DEFAULT, PARALLEL_DEFAULT, WAKE_WAIT_DEFAULT, DOWN_DEFAULT, CLIENTS_DEFAULT, THREADS_DEFAULT = 1, 20, 10, 2, 4, 4
WAKE_WAIT_MIN, WAKE_WAIT_MAX = 10, 1800        # the server's bounds for wake_wait (forgeyard/workloads/sleep.py)
PAUSE_AFTER_CHANGE = 120                       # no scaling within 2 minutes of a start, wake or change
SWEEP = 30                                     # the idle sweeper's period
UP_WITHIN = 75                                 # "a second replica within about a minute" of the load beginning
QUICK = 5.0                                    # an answer that must come "at once" comes within this
MiB = 1024 * 1024
STRUCTURED = {"guided_json": {"type": "object"}}   # structured output no provider keeps: such a request waits locally
AS_PARAMS = {"gpus": 0, "context": 8192, "concurrency": 2}   # 8,192 per request: a stream of minutes fits


# ------------------------------------------------------------------ options
def add_args(ap: argparse.ArgumentParser) -> None:
    """The scale group's options (accept_all.py adds them to its own)."""
    g = ap.add_argument_group("scale (1.5.0: sleep, wake, automatic replicas)")
    g.add_argument("--scale-idle-minutes", type=int, default=IDLE_DEFAULT,
                   help=f"the idle time of the deployments it lets sleep (default {IDLE_DEFAULT})")
    g.add_argument("--scale-parallel", type=int, default=PARALLEL_DEFAULT,
                   help=f"requests sent at once to an asleep model: one wake (default {PARALLEL_DEFAULT})")
    g.add_argument("--scale-wake-wait", type=int, default=WAKE_WAIT_DEFAULT,
                   help=f"the short wake_wait of step 5, seconds (default {WAKE_WAIT_DEFAULT}, the least the server takes)")
    g.add_argument("--scale-down-minutes", type=int, default=DOWN_DEFAULT,
                   help=f"the automatic deployment's scale-down window (default {DOWN_DEFAULT}; the server's own is 10)")
    g.add_argument("--scale-clients", type=int, default=CLIENTS_DEFAULT,
                   help=f"clients sending one request after another to make the load (default {CLIENTS_DEFAULT})")
    g.add_argument("--scale-threads", type=int, default=THREADS_DEFAULT,
                   help=f"CPU threads of each llama.cpp replica (default {THREADS_DEFAULT})")
    g.add_argument("--stoppable-agent", help="step 6: a machine whose agent this run may stop and start again")
    g.add_argument("--agent-stop", help="step 6: a shell command that stops that agent (run on this computer)")
    g.add_argument("--agent-start", help="step 6: a shell command that starts it again, in the background")
    g.add_argument("--metrics-token", default=os.environ.get("FORGEYARD_METRICS_TOKEN", ""),
                   help="the server's METRICS_TOKEN, for GET /metrics from another host (or FORGEYARD_METRICS_TOKEN)")


def check_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not 1 <= args.scale_idle_minutes <= 1440:
        ap.error("--scale-idle-minutes must be 1 to 1440")
    if not 2 <= args.scale_parallel <= 200:
        ap.error("--scale-parallel must be 2 to 200")
    if not WAKE_WAIT_MIN <= args.scale_wake_wait <= WAKE_WAIT_MAX:
        ap.error(f"--scale-wake-wait must be {WAKE_WAIT_MIN} to {WAKE_WAIT_MAX} seconds")
    if not 1 <= args.scale_down_minutes <= 120:
        ap.error("--scale-down-minutes must be 1 to 120")
    if not 2 <= args.scale_clients <= 32:
        ap.error("--scale-clients must be 2 to 32")
    if not 1 <= args.scale_threads <= 256:
        ap.error("--scale-threads must be 1 to 256")
    given = [bool(args.stoppable_agent), bool(args.agent_stop), bool(args.agent_start)]
    if any(given) and not all(given):
        ap.error("--stoppable-agent, --agent-stop and --agent-start go together (step 6 stops that agent and starts it)")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860", help="the server's address")
    ap.add_argument("--ca", "--cacert", dest="ca", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner", help="an admin account (password from FORGEYARD_PASSWORD, or asked)")
    ap.add_argument("--cpu-machine", help="the machine of the sleepy CPU deployment (steps 1-7)")
    ap.add_argument("--second-cpu-machine", help="another machine for the CPU model: automatic replicas (steps 8-9)")
    ap.add_argument("--gpu-machine", help="a machine with a GPU: the same on vLLM (steps 1, 2, 5, 10)")
    ap.add_argument("--cloud-provider", help="an existing provider: spill while waking (step 4)")
    ap.add_argument("--cloud-model", help="its chat model to use (default: its first)")
    ap.add_argument("--timeout", type=float, default=2400, help="seconds a deployment may take to be ready")
    ap.add_argument("--place-timeout", type=float, default=A.PLACE_TIMEOUT, help="seconds a deployment may wait for "
                    "room on a machine before its checks are skipped, naming the shortage (default 300)")
    ap.add_argument("--out", help="also write the report as JSON here")
    add_args(ap)
    args = ap.parse_args(argv)
    check_args(ap, args)
    if args.second_cpu_machine and not args.cpu_machine:
        ap.error("--second-cpu-machine needs --cpu-machine")
    if args.second_cpu_machine and args.second_cpu_machine == args.cpu_machine:
        ap.error("--second-cpu-machine must be another machine than --cpu-machine")
    if args.cloud_model and not args.cloud_provider:
        ap.error("--cloud-model needs --cloud-provider")
    if args.timeout <= 0 or args.place_timeout <= 0:
        ap.error("--timeout and --place-timeout must be positive")
    args.server = args.server.rstrip("/")
    # What accept_all's Run and start() read, for the parts of it this script does not have.
    for name in ("relay_machine", "relay_second_machine", "engine_machine", "second_engine_machine"):
        setattr(args, name, None)
    args.tts_asr = "none"
    args.plan = [("scale", "")]
    return args


# ------------------------------------------------------------------ pure helpers (tests/test_accept_scale.py)
_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\d+)?$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"\s*,?\s*')


def parse_metrics(text: str) -> list[tuple[str, dict, float]]:
    """The samples of a Prometheus text exposition: (name, labels, value). Comments and broken lines are left out."""
    out = []
    for line in (text or "").splitlines():
        m = _SAMPLE.match(line.strip())   # a comment (# HELP, # TYPE) or a blank line does not match
        if not m:
            continue
        labels = {}
        for k, v in _LABEL.findall(m.group(2) or ""):
            labels[k] = v.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        out.append((m.group(1), labels, value))
    return out


def metric(samples: Optional[list], name: str, **labels) -> Optional[float]:
    """The sum of `name`'s samples whose labels include these, or None when there is none (or no samples at all)."""
    got = [v for n, ls, v in samples or () if n == name and all(ls.get(k) == str(want) for k, want in labels.items())]
    return sum(got) if got else None


def replica_of(header: Optional[str]) -> Optional[int]:
    """The ordinal in X-Forgeyard-Replica ("<served name>#<ordinal>")."""
    m = re.search(r"#(\d+)$", header or "")
    return int(m.group(1)) if m else None


def sse_tally(lines) -> dict:
    """What a streamed chat answer held: chunks with data, the finish reason, whether [DONE] came, the text's end."""
    chunks, finish, done, text = 0, None, False, []
    for raw in lines:
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)).strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            continue
        try:
            j = json.loads(data)
        except ValueError:
            continue
        chunks += 1
        for ch in j.get("choices") or []:
            finish = ch.get("finish_reason") or finish
            text.append((ch.get("delta") or {}).get("content") or "")
    return {"chunks": chunks, "finish": finish, "done": done, "tail": "".join(text)[-40:]}


def tally(calls: list) -> dict:
    """Load results: how many, per status, per replica ordinal (answered ones), and answer ids seen more than once."""
    status: dict = {}
    per: dict = {}
    ids: dict = {}
    for c in calls:
        status[c.status] = status.get(c.status, 0) + 1
        if c.status == 200:
            per[c.replica] = per.get(c.replica, 0) + 1
            if c.id:
                ids[c.id] = ids.get(c.id, 0) + 1
    return {"sent": len(calls), "status": status, "per_replica": per, "repeated": sorted(i for i, n in ids.items() if n > 1)}


def served_delta(before: list[dict], after: list[dict]) -> dict:
    """Per replica ordinal, what the gateway sent it between two snapshots of a deployment's endpoints (each replica
    counts from 0 when it is new)."""
    was = {e.get("replica"): int(e.get("served") or 0) for e in before or []}
    return {e.get("replica"): int(e.get("served") or 0) - was.get(e.get("replica"), 0) for e in after or []}


def new_events(before: list[dict], after: list[dict], prefix: str) -> list[dict]:
    """Events in `after` (a deployment's events) that were not in `before` and whose message starts with `prefix`."""
    seen = {(e.get("at"), e.get("message")) for e in before or []}
    return [e for e in after or [] if (e.get("at"), e.get("message")) not in seen
            and str(e.get("message") or "").startswith(prefix)]


def held(workloads: list[dict], machine_id: int, gpu: Optional[int] = None, only: Optional[int] = None,
         but: Optional[int] = None) -> int:
    """Bytes the scheduler counts as held on a machine's RAM (gpu None: replicas without a GPU) or on one of its GPUs,
    from /api/workloads: replicas placed there, not ended, not confirmed stopped (forgeyard/workloads/placement.py
    holding()). `only` / `but`: one workload's replicas, or all but its."""
    total = 0
    for w in workloads or []:
        if (only is not None and w.get("id") != only) or (but is not None and w.get("id") == but):
            continue
        for r in w.get("replica_list") or []:
            m = r.get("machine") or {}
            if m.get("id") != machine_id or r.get("final") or r.get("state") == "stopped":
                continue
            cards = [g.get("index") for g in r.get("gpus") or []]
            if (gpu is None and not cards) or (gpu is not None and gpu in cards):
                total += int(r.get("vram") or 0)
    return total


def freed(before: tuple[int, int], after: tuple[int, int], mine: int, slack: int = MiB) -> bool:
    """Whether the machine's reserved bytes (/metrics) went down by `mine`, once what its other replicas took or let
    go meanwhile (from the API) is accounted: before/after are (metric, others)."""
    (m0, o0), (m1, o1) = before, after
    return mine > 0 and abs((m0 - m1) - (o0 - o1) - mine) <= slack


def retry_after_ok(value: Optional[str], last_start: Optional[float]) -> tuple[bool, str]:
    """Retry-After of a 503 model_waking: whole seconds, at least 5, at most the last start (+1 for rounding) when
    one is known (else the server's 30 s default)."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return False, f"Retry-After {value!r} is not a whole number of seconds"
    top = math.ceil(last_start) + 1 if last_start else 30
    return 5 <= n <= max(5, top), f"Retry-After {n} s (last start {last_start if last_start else 'unknown'} s)"


def stream_tokens(speed: Optional[float], seconds: float, context: int) -> int:
    """max_tokens of a stream meant to last `seconds` at `speed` tokens/s, within the context (less 300 for the
    prompt), at least 256."""
    want = int((speed or 10.0) * seconds)
    return max(256, min(want, context - 300))


def pause_left(updated_at: Optional[str], server_time: Optional[str], pause: float = PAUSE_AFTER_CHANGE) -> float:
    """Seconds until the autoscaler's pause after a start, wake or change is over (0 when it is)."""
    if not updated_at or not server_time:
        return 0.0
    gone = (A.utc(server_time) - A.utc(updated_at)).total_seconds()
    return max(0.0, pause - gone)


def plan_parts(args, machines: dict) -> dict[str, Optional[str]]:
    """Each part of the run and why it is skipped (None: it runs). `machines`: role -> machine."""
    out: dict[str, Optional[str]] = {}
    out["sleep"] = None if machines.get("cpu") else "no --cpu-machine given"
    out["spill"] = (out["sleep"] or (None if args.cloud_provider else
                    "no --cloud-provider given (spill while waking needs a provider as the route's tier 1)"))
    out["autoscale"] = (None if machines.get("cpu") and machines.get("cpu2") else
                        "needs two machines that fit the CPU model: --cpu-machine and --second-cpu-machine")
    out["vllm"] = None if machines.get("gpu") else "no --gpu-machine given (the vLLM variant)"
    out["cannot_wake"] = out["sleep"] or (None if args.stoppable_agent else
                                          "opt-in: give --stoppable-agent NAME with --agent-stop and --agent-start, a "
                                          "machine this run may take offline (it never stops a machine it was not told "
                                          "it may)")
    out["stop"] = out["sleep"]
    return out


def waking_skip(last_start: Optional[float], wake_wait: int) -> Optional[str]:
    """Why step 5 cannot run on a model, or None: its wake must take clearly longer than the short wake_wait."""
    if not last_start:
        return "its start time is not known (no last_start)"
    if last_start < wake_wait + 5:
        return (f"it starts in {last_start:.1f} s, faster than the shortest wait for a wake ({wake_wait} s), so a "
                "request never waits that long; the vLLM model (--gpu-machine) shows it")
    return None


# ------------------------------------------------------------------ HTTP
@dataclass
class Call:
    status: int
    seconds: float
    replica: Optional[int] = None
    target: str = ""
    spilled: str = ""
    retry_after: Optional[str] = None
    code: str = ""
    message: str = ""
    id: Optional[str] = None
    tokens: int = 0
    text: str = ""

    @classmethod
    def of(cls, r, seconds: float) -> "Call":
        d = r.data if isinstance(r.data, dict) else {}
        h = r.headers or {}
        content = ""
        if r.status == 200:
            try:
                content = str(((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            except (AttributeError, IndexError):
                content = ""
        return cls(r.status, round(seconds, 2), replica_of(h.get("x-forgeyard-replica")), A.target_of(h),
                   h.get("x-forgeyard-spilled") or "", h.get("retry-after"), r.code(),
                   A.why(r) if r.status != 200 else "", d.get("id"),
                   int((d.get("usage") or {}).get("completion_tokens") or 0), content[:60])

    def brief(self) -> str:
        if self.status == 200:
            return f"200 in {self.seconds} s from {self.target or '?'}" + (f", spilled {self.spilled}" if self.spilled else "")
        return f"{self.status} {self.code} in {self.seconds} s: {self.message[:140]}"


class Metrics:
    """GET /metrics (direct from the server's host, or with METRICS_TOKEN)."""

    def __init__(self, server: str, tls, token: str = ""):
        self.server, self.tls, self.token = server, tls, token
        self.why: Optional[str] = None

    def read(self) -> Optional[list]:
        h = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        r = A.raw(self.server + "/metrics", headers=h, tls=self.tls, timeout=30)
        if r.status == 200:
            self.why = None
            return parse_metrics(r.text)
        self.why = (f"GET /metrics answered {r.status or r.error} {r.code()}: run this script on the server's host, or "
                    "pass --metrics-token")
        return None


class Scale:
    """One run of the scale checks on an accept_all Run."""

    def __init__(self, run: "A.Run"):
        self.run, self.c, self.a = run, run.admin, run.args
        self.metrics = Metrics(self.a.server, run.tls, getattr(self.a, "metrics_token", "") or "")
        self.stop = threading.Event()
        self.sleepy: Optional[dict] = None      # the sleepy CPU deployment, once ready
        self.local_key: Optional[str] = None    # an API key without the cloud
        self.spilled = False
        self.done: dict[str, dict] = {}         # deployment name -> what step 10 reads (recipe, served name)
        self.cpu_start: Optional[float] = None
        self._catalog = threading.Lock()        # the parts add the same catalog entry: once

    # -- recording
    def check(self, name, ok, detail="") -> bool:
        return self.run.check(name, ok, detail)

    def skip(self, name, why) -> None:
        self.run.skip(name, why)

    # -- the API
    def dep(self, dep_id: int) -> dict:
        """{"deployment", "events", "server_time"}."""
        return self.c.ok("GET", f"/api/deployments/{dep_id}")

    def chat(self, key: str, model: str, max_tokens: int = 8, text: str = "Say hi.", extra: Optional[dict] = None,
             timeout: float = 600) -> Call:
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                "messages": [{"role": "user", "content": text}], **A.NO_THINK, **(extra or {})}
        t0 = time.monotonic()
        r = A.raw(self.a.server + "/v1/chat/completions", "POST", json.dumps(body).encode(),
                  {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, self.run.tls, timeout)
        return Call.of(r, time.monotonic() - t0)

    def models_listed(self, key: str) -> list[str]:
        r = A.raw(self.a.server + "/v1/models", headers={"Authorization": f"Bearer {key}"}, tls=self.run.tls, timeout=30)
        return [m.get("id") for m in ((r.data or {}).get("data") or [])] if r.status == 200 else []

    def ensure_model(self, spec: dict) -> None:
        with self._catalog:
            A.ensure_model(self.run, spec)

    def deploy(self, key: str, spec: dict, params: dict, machine: Optional[dict]) -> Optional[dict]:
        """Deploy `spec` as accept-<run>-<key> (pinned to `machine` when given) and wait until it is ready: its
        detail, or None with a SKIP (no room) or FAIL."""
        run, c = self.run, self.c
        self.ensure_model(spec)
        name = run.name(key)
        body = {"name": name, "model": spec["model"], "recipe": spec["recipe"], "params": params, "served_name": name}
        if machine is not None:
            body["machine_id"] = machine["id"]
        if not params.get("autoscale"):
            body["replicas"] = 1
        est = c.ok("POST", "/api/deployments/estimate", body)
        plan = est["placement"]
        if not plan["fits"]:
            self.skip(f"{name}: deploy", f"it does not fit now: {plan.get('summary')}")
            return None
        r = c.request("POST", "/api/deployments", body)
        if r.status != 201:
            self.check(f"{name}: deploy", False, f"{r.status} {r.code()}: {A.why(r)}")
            return None
        d0 = r.data["deployment"]
        item = run.cleanup.add(f"deployment {name}", A._delete_deployment, c, d0["id"])
        item.update(group=run.current, gpu=spec["device"] == "gpu", path=f"/api/deployments/{d0['id']}", dep_id=d0["id"])
        t0 = time.monotonic()
        try:
            run.wait(name, lambda: A.dep_poll(run, d0["id"]), self.a.timeout, every=3)
        except A.Fail as e:
            run.not_ready(f"{name}: ready", e)
            return None
        d = self.dep(d0["id"])["deployment"]
        where = ", ".join(f"#{x['ordinal']} {x['machine']['name']}" for x in d["workload"]["replica_list"] if x.get("machine"))
        self.check(f"{name}: deployed and ready ({where})", True, f"after {A.fmt_seconds(time.monotonic() - t0)}")
        d["_cleanup"] = item
        return d

    def last_start(self, d: dict, step: str = "10") -> Optional[float]:
        """Step 10: the deployment's last_start after a start (polled a few seconds: the ready event comes with it)."""
        got = None
        for _ in range(10):
            got = self.dep(d["id"])["deployment"].get("last_start")
            if got and got.get("seconds"):
                break
            time.sleep(1)
        ok = bool(got and (got.get("seconds") or 0) > 0 and got.get("source") in ("agent", "server"))
        self.check(f"{step}: {d['name']}: its start time is recorded (last_start)", ok,
                   f"{got.get('seconds')} s, from the {got.get('source')}, phases {got.get('phases')}" if got else "none")
        return float(got["seconds"]) if ok else None

    def wait_asleep(self, d: dict, label: str) -> bool:
        """Until the deployment is asleep with every replica stopped: at most its idle time and two sweeps more (from
        now, so from its last use at the latest). A FAIL naming what held it, and False, otherwise."""
        idle = int((d.get("params") or {}).get("idle_minutes") or self.a.scale_idle_minutes)
        holder: dict = {}

        def poll():
            x = self.dep(d["id"])["deployment"]
            holder["d"] = x
            w = x["workload"]
            reps = ", ".join(f"#{r['ordinal']} {r['state']}" for r in w["replica_list"])
            done = w["desired_state"] == "asleep" and w["status"] == "asleep" and all(
                r["state"] == "stopped" for r in w["replica_list"])
            s = x.get("sleep") or {}
            text = f"{w['status']} ({reps})" + (f", held by {s.get('held_by')}" if s.get("held_by") else "")
            return done, text

        try:
            self.run.wait(f"{d['name']} going to sleep", poll, idle * 60 + 2 * SWEEP + 60, every=3)
            return True
        except A.Fail as e:
            s = (holder.get("d") or {}).get("sleep") or {}
            self.check(f"{label}: {d['name']} goes asleep after {idle} min without a request", False,
                       f"{e}; sleeps at {s.get('sleeps_at')}, held by {s.get('held_by')}")
            return False

    def wait_ready(self, d: dict, what: str, timeout: Optional[float] = None) -> bool:
        try:
            self.run.wait(f"{d['name']} {what}", lambda: A.dep_poll(self.run, d["id"]), timeout or self.a.timeout, every=2)
            return True
        except A.Fail as e:
            self.run.not_ready(f"{d['name']}: {what}", e)
            return False

    def wakes(self, name: str, by: str) -> Optional[float]:
        return metric(self.metrics.read(), "forgeyard_deployment_wakes_total", deployment=name, by=by)

    def reservation(self, m: dict, gpu: Optional[int], wid: int) -> Optional[tuple[int, int, int]]:
        """(the machine's reserved bytes on /metrics, what its other replicas hold, what this workload holds), read
        until two readings of the metric agree; None when /metrics cannot be read."""
        name = "forgeyard_machine_memory_reserved_bytes" if gpu is None else "forgeyard_gpu_memory_reserved_bytes"
        labels = {"machine": m["name"]} if gpu is None else {"machine": m["name"], "gpu": gpu}
        for _ in range(5):
            s0 = self.metrics.read()
            ws = self.c.ok("GET", "/api/workloads")["workloads"]
            s1 = self.metrics.read()
            if s0 is None or s1 is None:
                return None
            a, b = metric(s0, name, **labels), metric(s1, name, **labels)
            if a is not None and a == b:
                return int(a), held(ws, m["id"], gpu, but=wid), held(ws, m["id"], gpu, only=wid)
            time.sleep(2)
        return None

    def agent_cmd(self, cmd: str) -> tuple[bool, str]:
        # Output goes to a file, not a pipe: an agent started in the background by a shell that keeps the pipe
        # open (`cd … && cmd &`) would otherwise hold this call until the timeout although the command is done.
        with tempfile.TemporaryFile(mode="w+") as out:
            try:
                p = subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                   text=True, timeout=120)
            except subprocess.TimeoutExpired:
                return False, "did not return within 120 s (start the agent in the background)"
            out.seek(0)
            text = out.read().strip()[-200:]
        return p.returncode == 0, f"exit {p.returncode}" + (f": {text}" if text else "")

    def machine_status(self, mid: int) -> str:
        return (self.run.machine_by_id(mid) or {}).get("status", "gone")


# ------------------------------------------------------------------ the parts
def sleep_and_wake(sc: Scale, m: dict, d: dict, key: str, label: str, gpu: Optional[int]) -> bool:
    """Steps 1 and 2 on one deployment: it sleeps (replicas stopped, port closed, reservation freed, still listed,
    files kept), then parallel requests wake it once. False when it did not get that far."""
    run, a = sc.run, sc.a
    import replica_port

    served = d["served_name"]
    wid = d["workload"]["id"]
    rp = replica_port.Replicas.of(sc.c, a.server, run.tls)
    ports = [rp.endpoint(e) for e in d.get("endpoints") or []]
    before_port = [rp.request(p, "GET", "/v1/models", timeout=10) for p in ports]
    before = sc.reservation(m, gpu, wid)
    if not sc.wait_asleep(d, f"1 {label}"):
        return False
    x = sc.dep(d["id"])["deployment"]
    reps = x["workload"]["replica_list"]
    sc.check(f"1 {label}: asleep after {x['sleep']['idle_minutes']} min without a request; its replicas are stopped "
             "and keep their machine", all(r["state"] == "stopped" and (r.get("machine") or {}).get("id") == m["id"]
                                          for r in reps) and x["sleep"]["asleep"],
             "; ".join(f"#{r['ordinal']} {r['state']} on {(r.get('machine') or {}).get('name')}" for r in reps))
    since = x["sleep"].get("asleep_since")
    sc.check(f"1 {label}: the API says since when it is asleep (sleep.asleep_since)", bool(since), f"since {since}")
    ev = [e["message"] for e in sc.dep(d["id"])["events"] if str(e["message"]).startswith("Asleep")]
    sc.check(f"1 {label}: the event says it went to sleep and what it frees", bool(ev), (ev or ["no Asleep event"])[0])
    # The port: it answered (the key guard's 401) while awake; asleep, nothing listens there.
    if not ports or all(b.status == 0 and b.failure == "unreachable" for b in before_port):
        sc.skip(f"1 {label}: its replica port no longer answers",
                "the replica port is not reachable from this computer (another network)" if ports else "no endpoint")
    else:
        after_port = [rp.request(p, "GET", "/v1/models", timeout=10) for p in ports]
        sc.check(f"1 {label}: its replica port no longer answers (engine and key guard stopped)",
                 all(y.status == 0 for y in after_port),
                 "; ".join(f"{p.url}: awake {b.status or b.failure}, asleep {y.status or y.failure}"
                           for p, b, y in zip(ports, before_port, after_port)))
    after = sc.reservation(m, gpu, wid)
    where = f"{m['name']} " + ("RAM" if gpu is None else f"GPU #{gpu}")
    if before is None or after is None:
        sc.skip(f"1 {label}: its reservation on {where} is freed", sc.metrics.why or "the metric did not settle")
    else:
        sc.check(f"1 {label}: its reservation on {where} is freed (/metrics)",
                 after[2] == 0 and freed(before[:2], after[:2], before[2]),
                 f"reserved {before[0] / A.GIB:.2f} -> {after[0] / A.GIB:.2f} GiB; it held {before[2] / A.GIB:.2f} GiB, "
                 f"now {after[2] / A.GIB:.2f}; the other replicas there {before[1] / A.GIB:.2f} -> {after[1] / A.GIB:.2f} GiB")
    listed = served in sc.models_listed(key)
    time.sleep(2)
    still = sc.dep(d["id"])["deployment"]["workload"]["desired_state"]
    sc.check(f"1 {label}: /v1/models still lists it, and listing does not wake it", listed and still == "asleep",
             f"listed {listed}, then {still}")
    view = sc.c.request("GET", f"/api/machines/{m['id']}/cache")
    model = x["model"]
    repo = str(model.get("source") or "").removeprefix("hf://")
    if view.status != 200 or not (view.data or {}).get("models"):
        sc.skip(f"1 {label}: the model stays in {m['name']}'s cache", f"no cache listing ({view.status} {view.code()}): "
                "the agent reports none")
    else:
        entry, part = A.cache_part(view.data, repo, model.get("revision"), model.get("file"))
        sc.check(f"1 {label}: the model stays in {m['name']}'s cache", part is not None and not (entry or {}).get("missing"),
                 f"{repo}@{str(model.get('revision') or '')[:12]} {model.get('file') or ''}: "
                 + (f"{part.get('bytes', 0) / A.GIB:.2f} GiB" if part else "not listed"))

    # 2: many requests at once, one wake.
    n = a.scale_parallel
    gen = x["workload"]["generation"]
    wakes0 = x["sleep"].get("wakes")
    events0 = sc.dep(d["id"])["events"]
    w0 = sc.wakes(d["name"], "request")
    out: list[Optional[Call]] = [None] * n

    def one(i: int) -> None:
        out[i] = sc.chat(key, served, text=f"Say hi to number {i}.")

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(n)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    seen: Optional[dict] = None   # sleep.waking while the requests wait (the console's "Waking 0:42")
    while seen is None and any(t.is_alive() for t in threads) and time.monotonic() - t0 < 60:
        seen = sc.dep(d["id"])["deployment"]["sleep"].get("waking")
        time.sleep(0.3)
    for t in threads:
        t.join()
    took = time.monotonic() - t0
    calls = [c for c in out if c is not None]
    if seen is None:
        sc.skip(f"2 {label}: while it wakes the API shows it (sleep.waking: by, waiting, expected_seconds)",
                "the wake was over before the API was read")
    else:
        sc.check(f"2 {label}: while it wakes the API shows it (sleep.waking: by, waiting, expected_seconds)",
                 str(seen.get("by") or "").startswith("a request") and (seen.get("waiting") or 0) >= 1
                 and (seen.get("expected_seconds") or 0) > 0, f"{seen}")
    after_d = sc.dep(d["id"])
    woke = new_events(events0, after_d["events"], "Woken by a request")
    statuses = sorted({c.status for c in calls})
    sc.check(f"2 {label}: {n} requests at once to the asleep model are all answered", len(calls) == n and statuses == [200],
             f"statuses {statuses} in {took:.1f} s (slowest {max((c.seconds for c in calls), default=0)} s)"
             + "".join(f"; {c.brief()}" for c in calls if c.status != 200)[:300])
    sc.check(f"2 {label}: they woke it exactly once (one event, generation +1)",
             len(woke) == 1 and after_d["deployment"]["workload"]["generation"] == gen + 1,
             f"{len(woke)} event(s): {[e['message'] for e in woke][:2]}; generation {gen} -> "
             f"{after_d['deployment']['workload']['generation']}")
    wakes1 = after_d["deployment"]["sleep"].get("wakes")
    sc.check(f"2 {label}: the API counts the wake (sleep.wakes in the last day +1)",
             isinstance(wakes0, int) and wakes1 == wakes0 + 1, f"{wakes0} -> {wakes1}")
    w1 = sc.wakes(d["name"], "request")
    if w1 is None:
        sc.skip(f"2 {label}: forgeyard_deployment_wakes_total{{by=\"request\"}} +1",
                sc.metrics.why or "the deployment has no wakes sample")
    else:
        sc.check(f"2 {label}: forgeyard_deployment_wakes_total{{by=\"request\"}} +1", w1 - (w0 or 0) == 1,
                 f"{w0 or 0:g} -> {w1:g}")
    return True


def model_waking(sc: Scale, d: dict, key: str, label: str, last: Optional[float]) -> None:
    """Step 5 on an asleep deployment: a short wake_wait gives 503 model_waking with Retry-After; the wake carries
    on and the model answers."""
    ww = sc.a.scale_wake_wait
    why = waking_skip(last, ww)
    name = f"5 {label}: with wake_wait {ww} s a request to the waking model gets 503 model_waking and Retry-After"
    if why:
        sc.skip(name, why)
        return
    if not sc.wait_asleep(d, f"5 {label}"):
        return
    r = sc.c.request("PATCH", f"/api/deployments/{d['id']}", {"params": {"wake_wait": ww}})
    shown = (((r.data or {}).get("deployment") or {}).get("sleep") or {}).get("wake_wait") if r.status == 200 else None
    shown = shown if shown is not None else (sc.dep(d["id"])["deployment"]["sleep"].get("wake_wait") if r.status == 200 else None)
    sc.check(f"5 {label}: wake_wait {ww} s is saved (sleep.wake_wait)", r.status == 200 and shown == ww,
             f"{r.status} {r.code()} sleep.wake_wait {shown}, restart {(r.data or {}).get('restart')}")
    if r.status != 200:
        return
    call = sc.chat(key, d["served_name"], timeout=ww + 120)
    ok_ra, ra = retry_after_ok(call.retry_after, last)
    sc.check(name, call.status == 503 and call.code == "model_waking" and ok_ra and ww - 1 <= call.seconds <= ww + 20,
             f"{ra}; {call.brief()}")
    sc.c.request("PATCH", f"/api/deployments/{d['id']}", {"params": {"wake_wait": None}})
    if sc.wait_ready(d, "ready after the 503 (the wake carries on)"):
        c2 = sc.chat(key, d["served_name"])
        sc.check(f"5 {label}: the wake carried on: it is ready and answers", c2.status == 200, c2.brief())


def part_sleep(sc: Scale) -> None:
    """Steps 1-5 on the CPU model (6 and 7 come after the other parts)."""
    run, a = sc.run, sc.a
    m = run.fresh_machine("cpu")
    if m["status"] != "online":
        sc.skip("1-7: the sleepy deployment", f"{m['name']} is {m['status']}")
        return
    params = dict(CPU_SPEC["params"], threads=a.scale_threads, sleep_when_idle=True, idle_minutes=a.scale_idle_minutes)
    d = sc.deploy("scale-sleepy", CPU_SPEC, params, m)
    if d is None:
        return
    sc.local_key = run.new_key("scale-local")["key"]
    key, served = sc.local_key, d["served_name"]
    warm = sc.chat(key, served)
    sc.check(f"{d['name']}: answers once awake", warm.status == 200, warm.brief())
    sc.cpu_start = sc.last_start(d)
    sc.done[d["name"]] = {"recipe": CPU_SPEC["recipe"], "served": served}
    if not sleep_and_wake(sc, m, d, key, "CPU", None):
        return
    sc.sleepy = d

    # 3: Wake now (Start) and the Playground.
    for how in ("start", "playground"):
        if not sc.wait_asleep(d, f"3 ({how})"):
            return
        events0 = sc.dep(d["id"])["events"]
        by = "person" if how == "start" else "request"
        w0 = sc.wakes(d["name"], by)
        if how == "start":
            r = sc.c.request("POST", f"/api/deployments/{d['id']}/start")
            prefix = f"Woken by {sc.c.username} "
            sc.check("3: Start on the asleep deployment (Wake now) is accepted", r.status == 200,
                     f"{r.status} {r.code()}")
            ready = sc.wait_ready(d, "ready after Wake now")
            call = sc.chat(key, served) if ready else None
            sc.check("3: after Wake now it answers", bool(call and call.status == 200), call.brief() if call else "not ready")
        else:
            body = {"model": served, "max_tokens": 8, "messages": [{"role": "user", "content": "Say hi."}], **A.NO_THINK}
            t0 = time.monotonic()
            r = sc.c.request("POST", "/api/playground/chat/completions", body, timeout=600)
            prefix = f"Woken by a request from the Playground ({sc.c.username})"
            sc.check("3: a Playground request to the asleep model wakes it and is answered", r.status == 200,
                     f"{r.status} {r.code()} in {time.monotonic() - t0:.1f} s")
        woke = new_events(events0, sc.dep(d["id"])["events"], prefix)
        sc.check(f"3: the event says who woke it ({prefix.strip()}…)", len(woke) == 1,
                 "; ".join(e["message"] for e in woke) or "no such event")
        w1 = sc.wakes(d["name"], by)
        if w1 is None:
            sc.skip(f"3: forgeyard_deployment_wakes_total{{by=\"{by}\"}} +1 ({how})", sc.metrics.why or "no sample")
        else:
            sc.check(f"3: forgeyard_deployment_wakes_total{{by=\"{by}\"}} +1 ({how})", w1 - (w0 or 0) == 1,
                     f"{w0 or 0:g} -> {w1:g}")
        if how == "start":
            audit = A.audited(run, "workload.started", d["workload"]["name"], woken=True)
            sc.check("3: Wake now is audited (workload.started, woken: true)", audit is not None,
                     f"{audit.get('at')} by {audit.get('actor')}" if audit else "no such audit event")

    # 4: spill while waking.
    part_spill(sc, d)
    # 5: a short wake_wait (only when its start is slower than that).
    model_waking(sc, d, key, "CPU", sc.cpu_start)


def part_spill(sc: Scale, d: dict) -> None:
    run, a, c = sc.run, sc.a, sc.c
    label = "4: spill while waking"
    if not a.cloud_provider:
        sc.skip(label, plan_parts(a, run.machines)["spill"])
        return
    p = next((x for x in c.ok("GET", "/api/providers")["providers"] if x["name"] == a.cloud_provider), None)
    models = [x for x in (p or {}).get("models") or [] if x["api"] == "openai-chat"]
    pm = (next((x for x in models if x["upstream_model"] == a.cloud_model), None) if a.cloud_model
          else (models[0] if models else None))
    if p is None or pm is None or not p.get("enabled"):
        sc.check(f"{label}: {a.cloud_provider} is enabled and has the chat model", False,
                 f"enabled {bool(p and p.get('enabled'))}, chat models {[x['upstream_model'] for x in models]}")
        return
    route = A.route_of(run, d["served_name"])
    local_t = {"tier": 0, "kind": "local", "deployment_id": d["id"]}
    item = run.cleanup.add(f"route {d['served_name']} back to its own deployment only",
                           lambda: c.ok("PUT", f"/api/routes/{route['id']}", {"targets": [local_t]}))
    c.ok("PUT", f"/api/routes/{route['id']}", {"targets": [local_t, {"tier": 1, "kind": "provider",
                                                                    "provider_model_id": pm["id"]}]})
    fb = run.new_key("scale-fallback", cloud="fallback")["key"]
    if not sc.wait_asleep(d, label):
        run.cleanup.now(item)
        return
    served = d["served_name"]
    # The one request that reaches the provider.
    call = sc.chat(fb, served, text=A.CLOUD_PROMPT, max_tokens=64)
    run.cloud_calls += 1
    sc.spilled = call.status == 200 and call.spilled == "waking"
    sc.check(f"{label}: a key that may spill is answered by {p['name']} at once (X-Forgeyard-Spilled: waking)",
             sc.spilled and call.target.startswith("provider:") and call.seconds < 60, call.brief())
    s = sc.chat(fb, served, extra=STRUCTURED)
    sc.check(f"{label}: structured output (guided_json) with the same key waits and is answered locally",
             s.status == 200 and s.target.startswith("local:") and not s.spilled, s.brief())
    if sc.wait_ready(d, "ready after the spill"):
        back = sc.chat(fb, served)
        sc.check(f"{label}: once ready, the same key is served locally", back.status == 200 and back.target.startswith("local:")
                 and not back.spilled, back.brief())
    run.cleanup.now(item)


def part_vllm(sc: Scale) -> None:
    """Steps 1, 2, 5 and 10 on vLLM on the GPU machine."""
    run, a = sc.run, sc.a
    m = run.fresh_machine("gpu")
    if m["status"] != "online":
        sc.skip("vLLM: sleep and wake", f"{m['name']} is {m['status']}")
        return
    params = dict(GPU_SPEC["params"], sleep_when_idle=True, idle_minutes=a.scale_idle_minutes)
    d = sc.deploy("scale-vllm", GPU_SPEC, params, m)
    if d is None:
        return
    key = run.new_key("scale-vllm")["key"]
    warm = sc.chat(key, d["served_name"])
    sc.check(f"{d['name']}: answers once awake", warm.status == 200, warm.brief())
    last = sc.last_start(d)
    sc.done[d["name"]] = {"recipe": GPU_SPEC["recipe"], "served": d["served_name"]}
    gpus = [g.get("index") for r in d["workload"]["replica_list"] for g in r.get("gpus") or []]
    if sleep_and_wake(sc, m, d, key, "vLLM", gpus[0] if gpus else None):
        model_waking(sc, d, key, "vLLM", last)
    run.cleanup.now(d["_cleanup"])


def part_autoscale(sc: Scale) -> None:
    """Steps 8 and 9 on a deployment with automatic replicas (1 to 2) on the two CPU machines."""
    run, a, c = sc.run, sc.a, sc.c
    m1, m2 = run.fresh_machine("cpu"), run.fresh_machine("cpu2")
    off = [x["name"] for x in (m1, m2) if x["status"] != "online"]
    if off:
        sc.skip("8-9: automatic replicas", f"{', '.join(off)} offline")
        return
    base = dict(AS_PARAMS, threads=a.scale_threads)
    sc.ensure_model(CPU_SPEC)
    est = c.ok("POST", "/api/deployments/estimate", {"name": run.name("scale-auto"), "model": CPU_SPEC["model"],
                                                     "recipe": CPU_SPEC["recipe"], "params": base, "replicas": 2})["placement"]
    if not est["fits"]:
        sc.skip("8-9: automatic replicas", f"two replicas of the CPU model do not fit now: {est.get('summary')}")
        return
    where = [p["machine"] for p in est.get("placed") or []]
    if len(set(where)) < 2:
        sc.skip("8-9: automatic replicas", f"two replicas would not go on two machines: {', '.join(where)}")
        return
    down = a.scale_down_minutes
    params = dict(base, autoscale=True, min_replicas=1, max_replicas=2, scale_down_minutes=down)
    d = sc.deploy("scale-auto", CPU_SPEC, params, None)
    if d is None:
        return
    key, served = run.new_key("scale-auto")["key"], d["served_name"]
    sc.done[d["name"]] = {"recipe": CPU_SPEC["recipe"], "served": served, "autoscale": True}
    probe = sc.chat(key, served, max_tokens=64, text="Count from 1 to 100.", extra={"ignore_eos": True})
    speed = probe.tokens / probe.seconds if probe.status == 200 and probe.seconds > 0 else None
    sc.check(f"8: {d['name']} answers", probe.status == 200, f"{probe.brief()}, {speed or 0:.1f} tokens/s")
    x = sc.dep(d["id"])
    left = pause_left(x["deployment"]["workload"]["updated_at"], x["server_time"])
    if left > 0:
        A.log(f"  {d['name']}: waiting {left:.0f} s (no scaling within 2 minutes of a start or change)")
        time.sleep(left + 3)

    # The load: clients one request after another until the second replica is ready, and 10 s more.
    stop = threading.Event()
    calls: list[Call] = []
    lock = threading.Lock()
    before = sc.dep(d["id"])["deployment"]["endpoints"]

    def client(i: int) -> None:
        while not stop.is_set() and not sc.stop.is_set():
            r = sc.chat(key, served, max_tokens=32, text=f"Client {i}: name three colours.")
            with lock:
                calls.append(r)

    events0 = sc.dep(d["id"])["events"]
    threads = [threading.Thread(target=client, args=(i,), daemon=True) for i in range(a.scale_clients)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    up_at = ready_at = None
    room = None
    snap: Optional[dict] = None
    try:
        while True:
            x = sc.dep(d["id"])
            dd = x["deployment"]
            if up_at is None:
                if new_events(events0, x["events"], "Scaled up to 2"):
                    up_at = time.monotonic() - t0
                    A.log(f"  {d['name']}: scaled up {up_at:.1f} s after the load began")
                elif new_events(events0, x["events"], "Wants 2 replicas"):
                    room = new_events(events0, x["events"], "Wants 2 replicas")[0]["message"]
                    break
                elif time.monotonic() - t0 > max(UP_WITHIN, 120):
                    break
            if up_at is not None and ready_at is None and dd["autoscale"]["ready"] == 2:
                ready_at = time.monotonic() - t0
                snap = dd
                A.log(f"  {d['name']}: replica 2 ready {ready_at:.1f} s after the load began")
            if ready_at is not None and time.monotonic() - t0 > ready_at + 10:
                break
            if up_at is not None and ready_at is None and time.monotonic() - t0 > up_at + a.timeout:
                break
            time.sleep(1)
    finally:
        stop.set()
        for t in threads:
            t.join()
    if room:
        sc.skip("8: a load adds a second replica", f"the server has no room for it: {room}")
        run.cleanup.now(d["_cleanup"])
        return
    sc.check(f"8: a load of {a.scale_clients} clients adds a second replica within about a minute",
             up_at is not None and up_at <= UP_WITHIN, f"after {up_at:.1f} s" if up_at is not None else "not added in 2 min")
    if ready_at is None:
        sc.check("8: the second replica becomes ready", False, "not ready")
        run.cleanup.now(d["_cleanup"])
        return
    reps = {r["ordinal"]: (r.get("machine") or {}).get("name") for r in snap["workload"]["replica_list"]}
    sc.check("8: the second replica is ready, on the other machine", len(set(reps.values())) == 2,
             f"ready {ready_at - up_at:.1f} s after it was added; " + ", ".join(f"#{k} {v}" for k, v in sorted(reps.items())))
    s = metric(sc.metrics.read(), "forgeyard_deployment_replicas", deployment=d["name"], kind="ready")
    load = metric(sc.metrics.read(), "forgeyard_deployment_load", deployment=d["name"])
    if s is None and sc.metrics.why:
        sc.skip("10: /metrics has its replicas and its load", sc.metrics.why)
    else:
        sc.check("10: /metrics has its replicas and its load", s == 2 and load is not None, f"ready {s}, load {load}")
    t = tally(calls)
    after = sc.dep(d["id"])["deployment"]["endpoints"]
    got = served_delta(before, after)
    sc.check(f"8: every request during the load is answered ({t['sent']})", t["status"] == {200: t["sent"]},
             f"statuses {t['status']}; per replica {t['per_replica']}")
    sc.check("8: no request ran twice: each replica's served count equals the requests sent to it, every id different",
             got == t["per_replica"] and not t["repeated"], f"gateway {got}, answers {t['per_replica']}, repeated ids "
             f"{t['repeated'][:3]}")
    drain_checks(sc, d, key, speed)
    sleep_with_autoscale(sc, d, key)
    run.cleanup.now(d["_cleanup"])


def drain_checks(sc: Scale, d: dict, key: str, speed: Optional[float]) -> None:
    """Step 8's drain: a long stream on the newest replica while the scale-down drains it."""
    run, a = sc.run, sc.a
    served = d["served_name"]
    window = a.scale_down_minutes * 60
    tokens = stream_tokens(speed, window + 60, AS_PARAMS["context"])
    victim = max(r["ordinal"] for r in sc.dep(d["id"])["deployment"]["workload"]["replica_list"])
    prompt = "Count from 1 to 5000, one number per line, nothing else."
    result: dict = {}
    full = 0   # streams that went to the replica to remove: two at most (each lasts the window and a minute)
    for attempt in range(6):
        if sc.dep(d["id"])["deployment"]["autoscale"].get("draining"):
            result["early"] = True   # it takes no new request now: no stream can go to it
            break
        # Line the turn up: once a short request went to another replica, the next idle one goes to the victim.
        for _ in range(6):
            r = sc.chat(key, served, max_tokens=2)
            if r.replica is not None and r.replica != victim:
                break
        out: dict = {"status": None}

        def stream() -> None:
            body = {"model": served, "max_tokens": tokens, "temperature": 0, "stream": True, "ignore_eos": True,
                    "messages": [{"role": "user", "content": prompt}], **A.NO_THINK}
            req = urllib.request.Request(a.server + "/v1/chat/completions", json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            out["start"] = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=900, context=run.tls) as resp:
                    out["status"] = resp.status
                    out["replica"] = replica_of(resp.headers.get("X-Forgeyard-Replica"))
                    if out["replica"] != victim:
                        return   # the other replica took it: hang up (the gateway cancels it) and line up again
                    out.update(sse_tally(resp))
            except urllib.error.HTTPError as e:
                out["status"] = e.code
            except OSError as e:
                out["status"], out["error"] = 0, str(e)
            finally:
                out["end"] = time.monotonic()
                out["end_wall"] = time.time()

        th = threading.Thread(target=stream, daemon=True)
        th.start()
        drain_seen = None
        during: list[Call] = []
        while th.is_alive():
            x = sc.dep(d["id"])["deployment"]
            dr = x["autoscale"].get("draining")
            if dr and drain_seen is None:
                drain_seen = time.monotonic()
                result["draining"] = dr
                A.log(f"  {d['name']}: draining replica {dr['replica']} ({dr['in_flight']} in flight)")
            if drain_seen is not None and len(during) < 3 and time.monotonic() - drain_seen > 5 * (len(during) + 1):
                during.append(sc.chat(key, served, max_tokens=4))
            time.sleep(2)
        th.join()
        A.log(f"  stream {attempt + 1}: {out.get('status')} from #{out.get('replica')}, {out.get('chunks')} chunks, "
              f"{(out.get('end', 0) - out.get('start', 0)):.0f} s, drain seen {drain_seen is not None}")
        if out.get("replica") != victim:
            continue   # (it hung up at once)
        full += 1
        if drain_seen is not None:
            result.update(out=out, during=during, seen=drain_seen)
            break
        if sc.dep(d["id"])["deployment"]["autoscale"].get("draining"):
            result.update(out=out, during=during, seen=None, between=True)
            break
        if full >= 2:
            break
    if result.get("early"):
        sc.skip("8: the drain begins while a long stream runs on the replica it removes",
                "it began before a stream could go to that replica (nothing in flight on it): run again")
        return
    if "out" not in result:
        sc.check("8: the drain begins while a long stream runs on the replica it removes", False,
                 f"no drain during the streams of {tokens} tokens on #{victim} (scale-down window {window} s)")
        return
    out = result["out"]
    if result.get("between"):
        sc.skip("8: the drain begins while a long stream runs on the replica it removes",
                "it began between two streams (nothing in flight on it): run again")
        return
    dr = result["draining"]
    sc.check("8: the drain begins while a long stream runs on the replica it removes", dr["replica"] == victim,
             f"replica {dr['replica']}, {dr['in_flight']} in flight, {result['seen'] - out['start']:.0f} s into the stream")
    during = result["during"]
    sc.check("8: requests during the drain go to the other replica and are answered",
             bool(during) and all(c.status == 200 and c.replica != victim for c in during),
             "; ".join(f"{c.status} from #{c.replica}" for c in during) or "none sent (the stream ended first)")
    sc.check("8: the stream on the drained replica finishes complete", out.get("status") == 200 and out.get("done")
             and out.get("finish") == "length", f"{out.get('chunks')} chunks, finish {out.get('finish')}, "
             f"[DONE] {out.get('done')}, {out['end'] - out['start']:.0f} s")
    removed = None
    limit = time.monotonic() + 120
    while time.monotonic() < limit and removed is None:
        x = sc.dep(d["id"])
        ev = [e for e in x["events"] if str(e["message"]).startswith(f"Removed replica {victim}")]
        if ev:
            skew = A.utc(x["server_time"]).timestamp() - time.time()
            removed = (ev[0], A.utc(ev[0]["at"]).timestamp() - skew - out["end_wall"])
            break
        time.sleep(2)
    sc.check("8: the replica is removed only after the stream's last byte",
             removed is not None and removed[1] >= -2,
             f"{removed[0]['message']} ({removed[1]:+.1f} s after the last byte)" if removed else "no Removed event in 2 min")
    t0 = time.monotonic()
    n = 0
    while time.monotonic() - t0 < 60:
        n = len(sc.dep(d["id"])["deployment"]["workload"]["replica_list"])
        if n != 1:
            break
        time.sleep(5)
    sc.check("8: back to 1 replica, and none added back for a minute", n == 1, f"{n} replica(s)")


def sleep_with_autoscale(sc: Scale, d: dict, key: str) -> None:
    """Step 9: sleep on with a minimum of 0: asleep with 1 replica, woken with 1."""
    r = sc.c.request("PATCH", f"/api/deployments/{d['id']}", {"params": {
        "sleep_when_idle": True, "idle_minutes": sc.a.scale_idle_minutes, "min_replicas": 0}})
    sc.check("9: sleep when idle and a minimum of 0 are saved on the automatic deployment (nothing restarts)",
             r.status == 200 and (r.data or {}).get("restart") in ("none", None), f"{r.status} {r.code()} "
             f"restart {(r.data or {}).get('restart')}")
    if r.status != 200:
        return
    d = dict(d, params=dict(d["params"], idle_minutes=sc.a.scale_idle_minutes))
    if not sc.wait_asleep(d, "9"):
        return
    x = sc.dep(d["id"])["deployment"]
    sc.check("9: it goes asleep with 1 replica (the minimum it wakes with)", len(x["workload"]["replica_list"]) == 1,
             f"{len(x['workload']['replica_list'])} replica row(s), autoscale {x['autoscale'].get('replicas')}")
    call = sc.chat(key, d["served_name"])
    x = sc.dep(d["id"])["deployment"]
    sc.check("9: a request wakes it with 1 replica and is answered",
             call.status == 200 and len(x["workload"]["replica_list"]) == 1, f"{call.brief()}; "
             f"{len(x['workload']['replica_list'])} replica(s)")


def part_cannot_wake(sc: Scale) -> None:
    """Step 6 (opt-in): the pinned machine offline while asleep."""
    run, a, c = sc.run, sc.a, sc.c
    name = "6: cannot wake: a request to a model pinned to an offline machine gets 503 cannot_wake at once"
    why = plan_parts(a, run.machines)["cannot_wake"]
    if why or sc.sleepy is None:
        sc.skip(name, why or "the sleepy deployment did not get that far")
        return
    m = next((x for x in c.ok("GET", "/api/machines")["machines"] if x["name"] == a.stoppable_agent), None)
    if m is None or m["status"] != "online":
        sc.check(f"6: {a.stoppable_agent} is a machine that is online", False, m["status"] if m else "no such machine")
        return
    d = sc.sleepy
    if not sc.wait_asleep(d, "6"):
        return
    if (sc.dep(d["id"])["deployment"].get("pinned") or {}).get("id") != m["id"]:
        r = c.request("PATCH", f"/api/deployments/{d['id']}", {"machine_id": m["id"], "wait": True})
        sc.check(f"6: pinned to {m['name']} while asleep (it moves there when it wakes)", r.status == 200,
                 f"{r.status} {r.code()} restart {(r.data or {}).get('restart')}")
    ok, how = sc.agent_cmd(a.agent_stop)
    sc.check(f"6: --agent-stop stops {m['name']}'s agent", ok, how)

    def back() -> None:
        if sc.machine_status(m["id"]) != "online":
            done, text = sc.agent_cmd(a.agent_start)
            if not done:
                raise A.Fail(f"--agent-start: {text}")

    item = run.cleanup.add(f"{m['name']}'s agent started again", back)
    try:
        run.wait(f"{m['name']} offline", lambda: (sc.machine_status(m["id"]) == "offline", sc.machine_status(m["id"])), 120)
    except A.Fail as e:
        sc.check(f"6: {m['name']} goes offline", False, str(e))
        run.cleanup.now(item)
        return
    call = sc.chat(sc.local_key, d["served_name"], timeout=60)
    sc.check(name, call.status == 503 and call.code == "cannot_wake" and call.seconds <= QUICK
             and m["name"] in call.message and "offline" in call.message, call.brief())
    run.cleanup.now(item)
    try:
        run.wait(f"{m['name']} online again", lambda: (sc.machine_status(m["id"]) == "online", sc.machine_status(m["id"])), 180)
    except A.Fail as e:
        sc.check(f"6: {m['name']} is online again", False, str(e))
        return
    if sc.wait_ready(d, "ready once its machine is back"):
        c2 = sc.chat(sc.local_key, d["served_name"])
        sc.check("6: once its machine is back, the woken deployment starts and answers", c2.status == 200, c2.brief())


def part_stop(sc: Scale) -> None:
    """Step 7: Stop on an asleep deployment is not woken by a request."""
    name = "7: Stop is not woken: a request to the stopped deployment gets 503 no_ready_replica"
    why = plan_parts(sc.a, sc.run.machines)["stop"]
    if why or sc.sleepy is None:
        sc.skip(name, why or "the sleepy deployment did not get that far")
        return
    d = sc.sleepy
    if not sc.wait_asleep(d, "7"):
        return
    events0 = sc.dep(d["id"])["events"]
    w0 = (sc.wakes(d["name"], "request"), sc.wakes(d["name"], "person"))
    r = sc.c.request("POST", f"/api/deployments/{d['id']}/stop")
    sc.check("7: Stop on the asleep deployment", r.status == 200, f"{r.status} {r.code()}")
    call = sc.chat(sc.local_key, d["served_name"], timeout=60)
    sc.check(name, call.status == 503 and call.code == "no_ready_replica" and call.seconds <= QUICK, call.brief())
    time.sleep(3)
    x = sc.dep(d["id"])
    woke = new_events(events0, x["events"], "Woken")
    w1 = (sc.wakes(d["name"], "request"), sc.wakes(d["name"], "person"))
    sc.check("7: nothing woke it (still stopped, no Woken event, the wake counters unchanged)",
             x["deployment"]["workload"]["desired_state"] == "stopped" and not woke and w0 == w1,
             f"{x['deployment']['workload']['desired_state']}, {len(woke)} Woken event(s), wakes {w0} -> {w1}")
    listed = d["served_name"] in sc.models_listed(sc.local_key)
    sc.check("7: /v1/models no longer lists it", not listed, f"listed {listed}")


def part_metrics(sc: Scale) -> None:
    """Step 10: the counters and histograms the run should have moved."""
    s = sc.metrics.read()
    if s is None:
        sc.skip("10: /metrics has the start, wake and autoscaling metrics", sc.metrics.why or "not read")
        return
    for name, info in sc.done.items():
        n = metric(s, "forgeyard_replica_start_seconds_count", recipe=info["recipe"], model=info["served"])
        sc.check(f"10: forgeyard_replica_start_seconds counts {name}'s starts", (n or 0) >= 1, f"count {n}")
    if sc.sleepy is not None:
        d = sc.sleepy
        req = metric(s, "forgeyard_deployment_wakes_total", deployment=d["name"], by="request")
        per = metric(s, "forgeyard_deployment_wakes_total", deployment=d["name"], by="person")
        sc.check(f"10: forgeyard_deployment_wakes_total for {d['name']} (by request, by person)",
                 (req or 0) >= 2 and (per or 0) >= 1, f"request {req}, person {per}")
        n = metric(s, "forgeyard_gateway_wake_wait_seconds_count", model=d["served_name"])
        sc.check(f"10: forgeyard_gateway_wake_wait_seconds counts the requests that waited for {d['name']}",
                 (n or 0) >= 1, f"count {n}")
        if sc.spilled:
            n = metric(s, "forgeyard_gateway_spills_total", model=d["served_name"], reason="waking")
            sc.check("10: forgeyard_gateway_spills_total{reason=\"waking\"}", (n or 0) >= 1, f"count {n}")
    for name, info in sc.done.items():
        if info.get("autoscale"):
            up = metric(s, "forgeyard_autoscale_total", deployment=name, direction="up")
            down = metric(s, "forgeyard_autoscale_total", deployment=name, direction="down")
            sc.check(f"10: forgeyard_autoscale_total for {name} (up, down)", (up or 0) >= 1 and (down or 0) >= 1,
                     f"up {up}, down {down}")


def _guarded(sc: Scale, label: str, fn: Callable, *args) -> None:
    """One part, as accept_all's run_group runs a group: a shortage is a SKIP, anything else that stops it a FAIL."""
    try:
        fn(sc, *args)
    except A.Unplaced as e:
        sc.skip(f"{label}: the rest of it", str(e))
    except A.Fail as e:
        sc.check(f"{label}: stopped", False, str(e))
    except Exception as e:  # noqa: BLE001 - one part must not stop the others or the cleanup
        frames = traceback.extract_tb(e.__traceback__)
        tb = next((f for f in reversed(frames) if f.filename == __file__), frames[-1])
        sc.check(f"{label}: stopped", False, f"{type(e).__name__}: {e} (line {tb.lineno} in {tb.name})")


def exercise(run: "A.Run") -> None:
    """The scale checks (accept_all's `scale` group). The sleepy deployment, the automatic one and the vLLM one run
    side by side; cannot-wake and Stop come after them, the metrics last."""
    sc = Scale(run)
    plan = plan_parts(run.args, run.machines)
    parts = [("sleep", "1-5 (CPU)", part_sleep), ("autoscale", "8-9", part_autoscale), ("vllm", "vLLM", part_vllm)]
    threads = []
    for key, label, fn in parts:
        if plan[key]:
            sc.skip(f"{label}: {'sleep and wake' if key != 'autoscale' else 'automatic replicas'}", plan[key])
            continue
        t = threading.Thread(target=_guarded, args=(sc, label, fn), name=f"scale-{key}", daemon=True)
        t.start()
        threads.append(t)
    try:
        for t in threads:
            while t.is_alive():
                t.join(1)
    except KeyboardInterrupt:
        sc.stop.set()
        raise
    _guarded(sc, "6", part_cannot_wake)
    _guarded(sc, "7", part_stop)
    _guarded(sc, "10", part_metrics)


def main(argv: Optional[list[str]] = None) -> int:
    return A.run_plan(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
