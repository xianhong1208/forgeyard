# How Forgeyard works, in detail

The design and behaviour of each part, for people who run Forgeyard seriously or work on it. To
set it up and use it, read the [guide](guide/README.md) first. [繁體中文](design.zh-TW.md)

## Machines

Each machine runs the **Forgeyard agent** (same repository, `python -m forgeyard agent`). The
agent dials out to the server, so the server never needs to reach the machine.

1. An admin opens **Machines → Add a machine** and gets a **one-time join token** (single use,
   valid 15 minutes, shown once, stored as a hash), with three ways to use it: the one-line install
   (`install.sh` served by the server: the agent of the server's version, `/etc/forgeyard/agent.toml`,
   a systemd service with `KillMode=process`), the agent's Docker image (`Dockerfile.agent`), or a
   Forgeyard checkout with a config file. See the [guide](guide/quick-start.md#3-add-a-machine).
2. However it starts, the agent joins the same way, e.g. from a checkout:

   ```bash
   uv run --no-sync python -m forgeyard agent --server https://<server>:7860 --join-token fyj_… \
       --ca-fingerprint sha256:…
   ```

   The agent first fetches the server's CA and refuses it unless it matches the fingerprint; from
   then on it trusts **that CA only** (not the system's list), over `https://` and `wss://`, so a
   machine in the middle is refused. It then trades the token for **this machine's own credential**, which it keeps in its
   state directory (`/var/lib/forgeyard-agent` as root, else `~/.local/state/forgeyard-agent`;
   `--state-dir`) with mode 0600. The server keeps only its SHA-256. Later starts need no token:
   `python -m forgeyard agent`.
3. The agent opens a WebSocket to `/agent/v1/stream` with `Authorization: Bearer <credential>`,
   sends its full inventory, then a heartbeat every 10 s with the values that change (free and
   used memory, utilisation, temperature). It sends the full inventory again when the hardware
   changes.

- **Inventory:** NVIDIA through NVML (`nvidia-ml-py`): model, UUID, compute capability as `arch`
  (`8.0`), memory, driver. AMD through the `amdsmi` library that ships with ROCm, else the
  `amd-smi` CLI, else `rocm-smi`: `arch` is the gfx target (`gfx1100`, `gfx1151`). APUs such as
  Strix Halo are flagged **unified memory**, and their size is the larger of the VRAM carve-out
  and GTT. The CPU and system RAM always appear as one row. A collector that finds nothing
  reports why and never stops the agent. All of these are read-only: no GPU memory is
  allocated, and running processes are not touched.
- **`python -m forgeyard agent inventory [--json] [--raw]`** prints what the agent detects,
  without joining.
- **Online / offline:** the server marks a machine offline when nothing has arrived for 30 s.
  It checks this itself every 2 s, so a pulled cable or a frozen agent is caught even while
  the TCP connection still looks open. The agent reconnects by itself, with exponential
  backoff and jitter capped at 15 s.
- **Revoke and remove** (admins): the credential stops working and an open connection is
  closed at once (the agent exits). Everyone signed in can see the Machines page; only admins
  add, revoke and remove machines. Join tokens, joins, refused joins, revokes and removals are
  in the audit log.
- **Transport:** `wss://`, verified against the pinned CA (kept as `server-ca.pem` in the state
  directory). An agent that joined over plain HTTP (1.0) switches once with
  `python -m forgeyard agent trust --server https://<server>:7860 --ca-fingerprint sha256:…`, keeping
  its credential; until then it stays offline and its log says so. `--ca-file` pins a CA file of
  your own instead (a company CA, a TLS proxy). The host check applies to agents too, so an agent reaching the server by a name rather than an IP
  needs that name in `ALLOWED_HOSTS` (or as `PUBLIC_URL`). Agent endpoints never accept a
  browser's session cookie, and refuse any request carrying an `Origin` header.
- **Simulated inventory** (development and tests only): `--simulate-inventory nvidia-a100x8 |
  nvidia-rtx4090x2 | amd-gfx1151-apu | <file.json>` reports a fixture instead of real hardware.
  It works only with `FORGEYARD_AGENT_SIMULATION=1`, and the server marks such machines
  *simulated inventory*.

## Workloads

A **workload** is something to run on your machines: a **service** (runs until stopped, is
health-checked and restarted) or a **task** (runs to the end, with retries and a time limit).
Each one has 1 to 16 **replicas**; each replica runs on one machine. Open **Workloads → New
workload**, or `POST /api/workloads` with the spec:

```json
{"name": "web", "kind": "service",
 "runtime": {"type": "container", "image": "python:3.13-slim", "args": ["python", "-m", "http.server", "8000"]},
 "ports": [{"name": "http", "port": 8000}], "health": {"type": "http", "path": "/"},
 "resources": {"cpu": 1, "memory": "512Mi", "gpus": {"count": 0}}, "machine_id": null}
```

| Field | What |
|---|---|
| `runtime` | `{"type": "container", "image", "command"?, "args"?}` (Docker or Podman; `command` replaces the entrypoint) or `{"type": "process", "command", "args"?, "workdir"?}` |
| `env` | explicit variables only (see *Isolation*) |
| `resources` | `cpu` (cores), `memory` (`4Gi`, `512Mi` or bytes), `gpus`: `{"count": n}` or `{"indices": [2]}` (on a chosen machine), optional `"vendor": "nvidia"/"amd"` |
| `ports` | services: `[{"name", "port"}]`. The agent picks the **host port** from its range (30000-32767 by default) and the page shows the address. A process must listen on it: `$PORT`, or `${PORT}` / `${PORT_<NAME>}` in its arguments |
| `health` | services: `{"type": "http" or "tcp", "port", "path", "interval", "timeout", "failure_threshold", "initial_delay", "start_period"}`. Shown as healthy / unhealthy; it does not restart anything. Failures within `start_period` (before the first success) do not count |
| `restart` | services: `always` (default), `on-failure`, `never`. Restarts back off 1, 2, 4… s, at most 60 s |
| `max_duration`, `retries`, `retry_backoff` | tasks: a run longer than `max_duration` seconds is stopped and fails (no retry); a non-zero exit is retried `retries` times, `retry_backoff` seconds apart, doubling, at most 5 min |
| `replicas`, `machine_id` | how many copies; a machine, or `null` for the first online machine with room |

**Placement (M2):** the machine you chose, or the first online machine (by name) that has the
runtime, enough free GPUs on its allowlist, and the CPU and memory the spec declares (counted
against the replicas already there). GPUs are **exclusive**: one card, one replica. Something that
does not fit waits as *pending* with the reason, and is placed as soon as room appears. A workload
may also declare the memory it needs per card (`gpus.vram`) and share cards with others that do
(`gpus.share: vram`); model deployments use the M3 scheduler (see *Models*).

**Who:** admins and operators create, stop, start and delete workloads and read their output.
Everyone signed in sees the list, the status and the events, but not environment values or output
(either can hold secrets). Creating, stopping, starting and deleting are in the audit log.

**GPU allowlist per machine:** an admin chooses on the Machines page (**Change GPU access**) which
cards Forgeyard may assign on each machine; by default all. A card outside the list is never
assigned, whatever a spec asks; the page marks such cards *not for Forgeyard*. A card in use cannot
be taken off the list (stop its workload first), and every change is audited with before and after.

### Isolation

- **Empty environment.** A process gets `PATH`, `HOME` (its own empty directory), `LANG`,
  `USER`/`LOGNAME`, the declared `env`, its GPU and port variables and `FORGEYARD_REPLICA`,
  `FORGEYARD_WORKLOAD`, `FORGEYARD_REPLICA_INDEX`. A container gets the declared `env`, its GPU
  variables and identity; its image supplies the rest. Nothing of the agent's environment or the
  host's credentials ever reaches a workload. GPU variables and `FORGEYARD_*`/`PORT*` cannot be
  declared in a spec.
- **GPUs, process runtime:** `CUDA_VISIBLE_DEVICES` and `NVIDIA_VISIBLE_DEVICES` hold the
  assigned cards' **UUIDs** (CUDA numbers cards fastest-first, nvidia-smi by PCI bus, so an index
  can name a different card; a UUID cannot), with `CUDA_DEVICE_ORDER=PCI_BUS_ID`. AMD:
  `ROCR_VISIBLE_DEVICES` holds the ROCr UUIDs (`GPU-<KFD unique_id>`, the index when there is none)
  and `HIP_VISIBLE_DEVICES` the renumbered `0..n-1`. Without GPUs every list is empty
  (`NVIDIA_VISIBLE_DEVICES=void`), and on AMD the process also gets none of the device groups, so
  the kernel keeps it off `/dev/kfd`. This is cooperative for NVIDIA (`/dev/nvidia*` are usually
  world-readable); containers are enforced.
- **GPUs, container runtime:** `--gpus "device=<uuid>"` (NVIDIA Container Toolkit) or CDI
  `--device nvidia.com/gpu=<uuid>`; AMD: `--device /dev/kfd` plus only the assigned
  `/dev/dri/renderD*`. `NVIDIA_VISIBLE_DEVICES` is always set (`void` without GPUs), because CUDA
  images declare `all` and a host whose default Docker runtime is `nvidia` would otherwise expose
  every card.
- **Never root by default.** When the agent runs as root, process workloads run as
  `--workload-user` (an unprivileged account, e.g. `nobody` or a dedicated user); without it,
  process workloads are refused and the Machines page says why. An agent that is not root runs
  processes as its own user. Containers run with the engine's defaults plus
  `--security-opt no-new-privileges`, never `--privileged`.
- **Limits.** Processes run in their own process group. On a systemd host with a root agent they get
  a transient scope with `MemoryMax` and `CPUQuota`; otherwise only rlimits: no core dumps, and an
  address-space cap for memory on workloads without GPUs (GPU runtimes reserve huge address
  ranges). CPU is then not capped, only counted for placement. Containers get `--cpus` and
  `--memory`.
- **Ports.** Container ports are published on one address: the agent's own address towards the
  server (`--bind-address` to change it).
- Anyone who can create a workload can run code on the machines. That is why only admins and
  operators can.

### Desired state and the agent

The server keeps, per machine, the full list of replicas that should run there (spec and
generation) and sends it to the agent on connect and on every change (`desired`). The agent
compares it with what actually runs, stops what is not wanted, starts what is missing, applies the
restart, retry and time-limit rules and reports every replica's state (`status`). It never gets
commands such as "start X", so a lost message, a reconnect or an agent restart just converges
again.

- **Adoption:** the agent remembers each replica in `<state dir>/replicas/` (pid, its kernel start
  time and the boot id; container name). A restarted agent finds its processes (the same pids;
  never a stranger that reused a pid) and its containers (labels `forgeyard.agent`,
  `forgeyard.replica`, `forgeyard.generation`), takes them over and starts nothing twice. Until the
  server's first list arrives it only watches. **Stopping the agent leaves workloads running**;
  stop them from the UI.
- **Output:** a process writes straight to `<state dir>/logs/<replica>/output.log`, kept to the last
  `--log-max-mb` MB (10); containers use the engine's log driver with the same budget. The detail
  page shows a live tail: the browser asks the server every 2 s and the server asks the agent over
  its WebSocket. When a replica ends, its last 100 lines are kept on the server for the page.
- **Protocol:** still version 1, extended compatibly. The agent lists `features: ["workloads"]` in
  `hello`, the server answers with its own in `welcome`, and `desired`, `status`,
  `logs_request` / `logs` only flow when both have it. An M1 agent keeps working (it is just
  never given work); an M2 agent on an M1 server only reports hardware.
- **Agent options:** `--workload-user`, `--work-dir` (one directory per process replica; default
  next to the state directory, never inside it), `--port-range` (30000-32767),
  `--bind-address`, `--container-engine auto|docker|podman|none|<path>`,
  `--limits auto|systemd|rlimit`, `--log-max-mb`, `--no-workloads`; for models `--model-cache DIR`,
  `--model-local-dir DIR` (repeatable) and `--allow-private-model-hosts`. `python -m forgeyard agent
  runtimes [--workload-user …]` prints what it can run here without joining.

## Models

**Models → Deploy a model**: pick a model from the catalog and a recipe, set the parameters, and
the page shows how much memory one replica needs and where it would go, before anything is created
or downloaded. A deployment is one service workload (it also appears under Workloads) plus a
gateway route: programs reach it at `/v1` with its **model name** (see *Gateway and API keys*). Its
page shows that name, its usage, each replica and a small **Try it** that checks one replica directly.

- **Adding a model** (admins), three ways on **Models → Catalog and recipes → Add a model**:
  - **Recommended**: a curated list ([forgeyard/models/recommended.yaml](../forgeyard/models/recommended.yaml),
    easy to edit) of models known to work with the built-in recipes, grouped: Qwen3 (chat 0.6B to
    32B, two GGUF builds for the CPU, Embedding and Reranker 0.6B / 4B), other open chat models
    (gpt-oss-20b, Gemma 4 E2B / E4B / 12B / 26B-A4B / 31B, Gemma 3 4B / 12B / 27B, Llama 3.2 3B and
    3.1 8B), speech to text (Whisper small and
    large-v3 turbo for faster-whisper) and BGE-M3 with its reranker. Each says its task, the
    recipe it is meant for, about how much it downloads and how much memory one copy needs. Gemma 3
    and Llama are **gated**: they need a Hugging Face token whose account accepted the licence
    (Gemma 4 is not).
    BGE-M3 is added at `refs/pr/130`, Hugging Face's own safetensors conversion (its main branch
    has PyTorch weights only). `python scripts/check_recommended.py` checks every entry against
    Hugging Face with Forgeyard's own inspector and estimator.
  - **Search Hugging Face**: the server asks `<HF_ENDPOINT>/api/models` (the browser never talks to
    Hugging Face), with filters for the task and the format (safetensors, GGUF, CTranslate2), most
    downloaded first (a rerank search also covers text classification, where many rerankers are
    filed, and lists names containing "rerank" first), and shows downloads, likes, last update, gated,
    and the size when Hugging Face reports it. Answers are cached for 5 minutes; 30 searches per minute per admin. Without internet
    the page says so and the recommended list (and adding by source) still work.
  - **Enter a source**, as below.

  Choosing a model inspects it (next point) and shows the recipe it would run with, what one copy
  reserves with that recipe's defaults (GPU or CPU, switchable when the recipe has both), and the
  machines it **fits on right now**, or why it fits nowhere, before anything is added.
- **Catalog** (admins): add `hf://<org>/<name>` (optionally a revision and, for GGUF repositories,
  the file), a GGUF URL from an allowed host, or `local://<dir>` under an allowed directory. The
  model is **inspected without downloading it**: the commit it pins to, the files and their size,
  `config.json`, the safetensors headers (parameter counts) or the GGUF header (read with HTTP range
  requests). A token for gated repositories is used for that only, never stored. Admins can add
  engine arguments per recipe to a model (Qwen3-Reranker needs vLLM's `--hf-overrides`).
- **Recipes** are YAML ([forgeyard/recipes](../forgeyard/recipes)): tasks, formats, the protocol per
  task, parameters, the estimate method, a health check, and **variants** per hardware (vendor,
  NVIDIA compute capability or AMD gfx target, driver version), each a pinned container image or,
  for CPUs without Docker, a pinned release the agent downloads and checks against its SHA-256.
  Built-in images are pinned **by digest** as `repo:tag@sha256:…`: the agent pulls and runs the
  digest, so a tag moved on the registry cannot change what runs. The UI shows the tag. To bump
  an engine, change the tag, run `uv run --no-sync python scripts/pin_images.py --write` (it resolves
  the multi-architecture digest from Docker Hub or GHCR and checks the platforms each variant
  claims), and review the diff; without `--write` it checks that every digest still matches its
  tag. A test fails if a built-in image has no digest. Admins add custom recipes on the Models page
  (a digest is recommended there too). Built in:

  | Recipe | Serves | Runs | Pinned |
  |---|---|---|---|
  | `vllm` | chat (safetensors) | NVIDIA, AMD | `vllm/vllm-openai:v0.32.0` (CUDA 13.0, driver ≥ 580), `-cu129` (≥ 575), `vllm/vllm-openai-rocm:v0.32.0` |
  | `vllm-pooling` | embeddings, rerank | NVIDIA, AMD, shares a GPU by default | the same images, `--runner pooling` |
  | `llama-cpp` | chat (GGUF) | NVIDIA, AMD, CPU (container or process) | `ghcr.io/ggml-org/llama.cpp:server-*-b11977`, release `b11977` for the process |
  | `faster-whisper` | speech to text | NVIDIA, CPU, shares a GPU by default | `ghcr.io/speaches-ai/speaches:0.8.3-cuda` / `-cuda-12.4.1` / `-cpu` |

- **Estimate** (per replica, per GPU): safetensors = parameters × bytes (FP8 keeps embeddings in 16
  bits) + KV cache (2 × layers × KV heads × head size × context × concurrent sequences × bytes) +
  the recipe's engine overhead + the CUDA context (0.4 GiB, measured on an A100); GGUF = the exact
  tensor sizes of the offloaded layers + their KV cache + the logits buffer + overhead; whisper =
  the model files + overhead. A declared `vram` replaces it (required for `local://` models).
- **Scheduling**: filter (a recipe variant for the hardware and driver, the runtime, the GPU
  allowlist, enough memory), then score: **binpack** (the tightest card that fits; whole cards stay
  free for big models), and on a tie the machine that already **has the model cached**. Memory is
  what the agent reports **free** on the card (it may be shared with work outside Forgeyard), minus
  Forgeyard's reservations that are not in that number yet, minus `VRAM_HEADROOM_MIB` (512). Never
  the card's total. When nothing fits the deployment is refused and every machine says why and by
  how much (`wait: true` queues it instead). Replicas of one deployment go to **different machines**
  when they can (before binpack), so the gateway can balance and fail over between them. This
  spreading is the default and always on; replicas share a machine only when no other machine fits.
- **Sharing a GPU** (`share: vram`): several replicas on one card, each held to its own
  reservation; a shared card is never shared with an exclusive replica. vLLM's
  `--gpu-memory-utilization` is a fraction of the card's **total** memory and vLLM refuses to start
  unless that much is **free**, so a fixed 0.9 never starts on a shared card: Forgeyard sets it to
  (reservation − CUDA context) / total, rounded down. Replicas on one card start one at a time (the
  next once the previous is healthy), so two engines never size themselves against the same free
  memory.
- **Model cache** on each machine (`agent --model-cache DIR`): keyed by source and revision, laid out
  like the Hugging Face cache, shared by every replica there, downloaded by the agent (resumable,
  every file checked against its SHA-256 or git blob id, progress on the page), mounted
  **read-only** into containers (the model's own directory, nothing else); files are read-only for
  processes too. Downloads go through the same guard as the server: https and public addresses
  only, every redirect checked (`--allow-private-model-hosts` for a LAN mirror).
- **Who:** admins manage the catalog, recipes and model sources; admins and operators deploy;
  everyone signed in sees deployments and can try them.

Real-hardware check: `scripts/accept_m3.py`.

## Gateway and API keys

Every deployment answers at **`https://<server>:7860/v1`**, OpenAI-compatible, on the console's own
port: `GET /v1/models`, `POST /v1/chat/completions` and `/v1/completions` (streaming passed through),
`/v1/embeddings`, `/v1/rerank` and `/v1/score` (vLLM's shapes), `/v1/audio/transcriptions` and
`/translations` (multipart). The `model` is the deployment's served name. Any OpenAI client works:

```python
import ssl

import httpx
from openai import OpenAI

ca = ssl.create_default_context(cafile="forgeyard-ca.pem")   # the server's CA, from "Trust this server"
client = OpenAI(base_url="https://<server>:7860/v1", api_key="fyk_…", http_client=httpx.Client(verify=ca))
client.chat.completions.create(model="qwen3-4b", messages=[{"role": "user", "content": "Hi"}])
```

With your own publicly trusted certificate, leave out `ca` and `http_client`. `curl --cacert
forgeyard-ca.pem …` and `SSL_CERT_FILE=forgeyard-ca.pem` work as well. The examples on the API keys
page contain these lines whenever the server uses its own CA.

- **API keys** (page **API keys**): everyone creates their own; a key is `fyk_` + 43 random
  characters, **shown once** with a curl and a Python example; the server keeps its SHA-256 and a
  12-character prefix. Optional: an expiry, the models it may use (else every deployment, also later
  ones), requests and tokens per minute, its **cloud** setting and a monthly **budget** (see *Cloud
  providers and tiered routes*). Revoking, an expiry or a disabled account stop it on the next
  request. Admins see and revoke everyone's keys, change any key's limits, cloud setting and budget,
  and set the **defaults**
  (for keys without their own limit) and **caps** (the most people may give their own keys). Creating,
  changing and revoking keys are in the audit log.
- **Limits an admin sets are locked.** When an admin sets the requests or tokens per minute, the
  cloud setting or the monthly budget of someone else's key, its owner can no longer change that
  value (403 `limit_locked`); the key shows a
  lock with the admin's name. The owner can still revoke the key and set a limit no admin set. An
  admin changes a locked value at will and **unlocks** it by clearing it (back to the default, the
  owner's to set again). Locking and unlocking are in the audit log (`api_key.changed`, `locked` /
  `unlocked`). An admin's own keys are never locked.
- **Only keys on `/v1`**: the console's cookie is never read there (a web page cannot use a signed-in
  browser against it), so the console's CSRF rule does not apply; the host check (421) does. No CORS.
- **Routing:** the ready replicas of the model, fewest requests in flight first, ties in turn. If
  connecting to one fails before anything was sent back, the next is tried and the failed one goes
  last until its agent reports it ready again (at most 15 s). A client that disconnects closes the
  request upstream, so the engine stops. Timeouts: 5 s to connect, 600 s for the headers or the next
  piece of a stream (`GATEWAY_CONNECT_TIMEOUT`, `GATEWAY_READ_TIMEOUT`).
- **`/v1/models`** lists a route only when the key may see it (it has a target inside your network,
  or the key's cloud setting is `on`), the key's model list includes it, and it can serve: some tier
  has a deployment that is meant to run (starting and restarting count) or an enabled provider inside
  your network, or an enabled cloud target the key may reach: an `on` key on a route that is not local
  only, and a `fallback` key only when a local tier comes before the cloud tier (the same tiers a call
  may use). So a stopped model leaves the list; `GET /v1/models/{id}` says 404 "…is stopped", and a call
  to it anyway answers 503 `no_ready_replica` "The deployment is stopped."
- **Errors** in OpenAI's shape `{"error": {"message", "type", "param", "code"}}`, `type` following the
  status: 400 bad body or wrong endpoint for the model; 401 key missing, wrong, expired or revoked, or
  its account not active; 402 a budget (below); 403 model not allowed for the key; 404 unknown model or
  path; 413 body over `GATEWAY_MAX_BODY_MB`; 421 foreign host name; 429 over a limit, with
  `Retry-After`; 502 replica or provider unreachable, refusing a key, redirecting or failing; 503 no
  ready replica (with each replica's state), no usable target, or this month's spend unreadable; 504
  no answer in time. Every `code` is listed in docs/guide/reference.md, and a test keeps that list
  complete. An engine's own error answers are passed on with their status and body (not its headers).
  A stream that breaks off after it started ends with an `upstream_stream_broken` error event.
- **Limits** per key, sliding windows over the last 60 s, counted in this server's memory (several
  servers sharing limits come later): requests per minute (refused requests do not count) and tokens
  per minute from the answers' `usage` (for a streamed chat the gateway asks the engine for its usage
  chunk and removes it unless the client asked for it); while a request runs, its `max_tokens` is held
  so parallel requests cannot all slip past.
- **Usage** (page **Usage**): requests, errors, limit refusals, prompt, completion, cached and
  reasoning tokens, spilled requests, latency p50/p95, per day, model, key, person, source (API key or
  playground) or local and cloud; its **Cost** tab adds the cost per day, key, person, model or
  provider (see *Cost*). Counted in memory and written every `USAGE_FLUSH_SECONDS` (5 s), on shutdown
  and before a read, one row per UTC day, source, key, person, model and target. A client that went
  away (499) is not an error. Everyone sees their own; admins everyone's. A deployment's page shows
  its last 7 days.
- **Playground**: chat with streaming, embeddings, rerank and speech to text in the console, through
  the same routing, as the signed-in user (cookie and CSRF like the rest of the console). It creates
  no API key and counts as "console" in usage. Its cloud setting is `on` for admins and the server's
  most-allowed for everyone else; the person's budget applies, there is no key budget.
- **Replicas cannot be used around the gateway.** Every model replica gets an **internal key**, new
  with every placement. Only the gateway and *Try it* send it; it is never shown in the UI, the API,
  a spec or a log. The agent keeps it in memory only and enforces it with its **key guard**:
  - The **engine listens on loopback only**, on a port only the agent knows: a process binds
    `127.0.0.1` (the recipe's `{host}`), a container is published on `127.0.0.1:<internal port>`.
  - The agent itself listens on the replica's **published** address and port (the one the server and
    the gateway know) and passes a request on only with `Authorization: Bearer <internal key>`, on
    **every path and method** (key compared in constant time); everything else gets 401
    `replica_key_required` and never reaches the engine. This closes what the engines leave open:
    vLLM 0.32 checks its own key only on `/v1`, `/v2`, `/inference` and `/cohere`, so its
    `/invocations`, `/rerank`, `/score`, `/pooling`, `/classify` and `/tokenize` would answer anyone.
  - Bodies stream both ways as they come (SSE, chunked and multipart uploads, large answers); a client
    that disconnects closes the engine's connection; hop-by-hop headers are dropped. If the engine is
    down the guard closes the connection unanswered, so the gateway tries the next replica.
  - The guard has nothing open, not even `/health`: the agent checks health on the engine's loopback
    port. A process engine found listening beyond loopback (a recipe that ignores `{host}`) is
    stopped and failed.
  - The engines still get the key in their **environment** too (vLLM `VLLM_API_KEY`, llama.cpp
    `LLAMA_API_KEY`, speaches `API_KEY`; a recipe names its variable with `api_key_env`), never on the
    command line: defence in depth. A recipe without `api_key_env` is guarded the same way.
  - The guard lives in the agent. **While the agent is down, its replicas are unreachable from
    outside** (the engines keep running); a restarted agent adopts them and opens their guards again
    as soon as the server has sent their keys. Anyone who can run code on the machine itself (Forgeyard
    workloads there included) can reach the loopback port; there the engine's own key still guards
    its `/v1` routes.
  - Replicas started by an older agent are restarted behind a guard once the new agent has their key
    (same published port); those from before M4 have no key until they are placed again (stop and
    start the deployment). Plain workloads (not models) are not guarded.

Real-hardware check: `scripts/accept_m4.py`.

## Cloud providers and tiered routes (1.2)

The gateway can also send requests to model APIs outside Forgeyard, local first. Decided on
2026-11-11 after a study of LiteLLM, OpenRouter, Portkey, Envoy's AI gateway, Kong and GPUStack.

- **OpenAI-compatible upstreams only.** Forgeyard speaks the OpenAI shape to each provider's own
  compatibility endpoint (OpenAI, Anthropic `…/v1/`, Gemini `…/v1beta/openai/`, Azure OpenAI's v1 API,
  OpenRouter, any OpenAI-compatible server) and passes answers back unchanged. A small **quirks
  table** per kind (`forgeyard/gateway/quirks.py`) covers what differs: how the key is sent (Bearer,
  Azure's `api-key`, Anthropic's `x-api-key` and version), engine-only parameters dropped
  (`chat_template_kwargs`, `top_k`, `guided_*`, llama.cpp samplers…), `max_tokens` renamed for OpenAI
  and Azure, Gemini's `models/` prefix, and how `usage` is read back (cached and reasoning tokens;
  Anthropic's are always empty). No native translators: what each endpoint loses is documented
  (docs/guide/cloud-providers.md) instead of hidden. **No LiteLLM**, as a library or a sidecar: it
  was compromised on PyPI twice in 2026 (1.82.7/1.82.8 in March through a compromised CI tool; 1.107.0
  and its Docker images in October, which read a proxy's credential tables), and its key and budget
  system would duplicate Forgeyard's.
- **Providers** (`providers`, `provider_models`; page **Providers**, admins only). A provider is a
  kind, a base URL, an encrypted key, up to 16 extra headers (not secret; the names that carry keys,
  `Host` and `Cookie` are refused), an optional answer timeout (1–3600 s), whether it is **outside
  your network** (the kind's default: every kind but *OpenAI-compatible server*) and, only then not,
  whether plain http is allowed. Its models each serve `openai-chat` or `openai-embeddings`, may drop
  more parameters (`drop_params`), and carry prices. **Test connection** asks `GET {base}/models` with
  the key; **Import from /models** lists what to add. A provider whose models are in a route cannot
  be deleted (409). **Clone** copies it with its models, prices and budget, without its key and disabled.

  | Kind | Key sent as | Differences |
  |---|---|---|
  | OpenAI | `Authorization: Bearer` | `max_tokens` sent as `max_completion_tokens` |
  | Anthropic | Bearer, `x-api-key`, `anthropic-version: 2023-06-01` | no embeddings; usage has no cached tokens (`reports_cached` false); `response_format: {"type": "text"}` dropped |
  | Google Gemini | Bearer | `models/` removed from listed ids |
  | Azure OpenAI | `api-key` | your own base URL (the v1 API); `max_tokens` renamed |
  | OpenRouter | Bearer | keeps `top_k`, `min_p`, `repetition_penalty`; reports each request's cost |
  | OpenAI-compatible server | Bearer, key optional | inside your network by default; drops nothing |

  Every kind but the last drops engine-only parameters (`chat_template_kwargs`, `guided_*`,
  llama.cpp's samplers, `top_k`…) on the way out.
- **Usage read back** (`quirks.read_usage`): `prompt_tokens`, `completion_tokens`, cached
  (`prompt_tokens_details.cached_tokens`), cache writes (OpenRouter's `cache_write_tokens`), reasoning
  (`completion_tokens_details.reasoning_tokens`), a reported `usage.cost`, and Anthropic's native names
  (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, the prompt
  then being all three inputs). **Gemini's** compatibility endpoint leaves thinking tokens out of
  `completion_tokens` and counts them only in `total_tokens`: when `total_tokens` is more than prompt
  plus completion, the difference is added to the completion and to the reasoning tokens, so it is
  priced as output, as Google bills it. Cached tokens are capped at the prompt, reasoning at the
  completion. Rate limits count prompt plus completion from the same reading.
- **Routes and targets.** A route is a served name with targets in tiers (`route_targets`): local
  deployments and provider models, each with an on/off switch; provider targets also have a weight
  (1–100). A deployment creates its own route (tier 0: itself), cannot lose that target, and deletes
  the route with itself; admins add tiers or create routes of their own (cloud only, or several
  deployments behind one name). At most 8 tiers and 32 targets; a served name is unique across
  routes; provider targets only on chat or embedding routes, with a matching model. Everyone signed in
  reads routes (with whether each is usable, and its kind: local, mixed or cloud); admins change them
  (`route.created`, `route.changed`, `route.deleted` in the audit log). Migration 0009 turned every
  existing route into one tier-0 target with the same upstream model name, so they behave exactly as
  before.
- **Spill rules, only before the first byte.** The next tier is used when this one is *unavailable*
  (no ready replica, every connection failed), *busy* (every ready replica has as many requests in
  flight through this server as its deployment's `concurrency`; the slot is taken atomically, so two
  requests cannot both get the last one; `wait_ms` waits for one first) or *refusing* (429 or 503).
  *Too long* (estimated prompt plus `max_tokens` over the deployment's `context`) only when the route
  opts in. Never on another 4xx, a timeout once the request was sent, a replica refusing the internal
  key, a client that went away, or mid-stream. The concurrency cap, `wait_ms` (0–10,000 ms, polled
  every 25 ms) and the too-long check apply only when something can follow, in this tier or a later
  one; a deployment without `concurrency` is never busy, one without `context` always fits. The prompt
  is estimated as its UTF-8 size (messages, prompt or input) ÷ 3, plus `max_completion_tokens` or
  `max_tokens`. If local was busy and every later tier failed, the request goes to the least-loaded
  local replica after all, past its `concurrency` (the engine queues it), and `X-Forgeyard-Spilled` is
  dropped. A local 429 or 503 that nothing else could replace is passed on.
- **The cloud tier** tries its targets by weight (a weighted shuffle); one that answers 429 or 5xx,
  cannot be reached, redirects or is refused by the egress rules cools down for
  `PROVIDER_COOLDOWN_SECONDS` (30 s, or its `Retry-After` / `retry-after-ms`, up to 5 min) and is
  tried last, not skipped. When nothing else is left (the last tier), a `Retry-After` up to
  `PROVIDER_RETRY_AFTER_CAP` (2 s) is waited for once. Provider answers become: 429 → 429
  `provider_rate_limited` (with its `Retry-After`); 5xx → 502 `provider_error`; 401, 403 or 407 → 502
  `provider_auth_failed` at once, without trying another target (never a 401 the client would blame
  on its own key); a redirect → 502 `provider_redirect`, never followed; any other 4xx keeps its status
  and the provider's `code`, with the provider's name before its message and anything that looks like
  a key removed. A timeout after sending is 504 `provider_timeout`, with no fallback.
- **Who may send data out.** Each API key has `cloud` = `off` (every key from before 1.2, and the
  default) / `fallback` (only as a spill target: a cloud target is used only in a tier after one that
  has a deployment or a provider inside your network, so not in the same tier as the local one) /
  `on` (cloud-only models too). A cloud-only model is 404 and unlisted for `off` and `fallback` keys.
  Admins set the default and the most a user may give their own key (`GATEWAY_CLOUD_DEFAULT` and
  `GATEWAY_CLOUD_MAX` at first, then stored; the default may not be above the most; changing them
  changes no existing key; audit `api_key_cloud.changed`). An admin's value on someone else's key is
  locked, like limits; a user who clears their key's value gets the default. A provider marked as
  inside your network (a vLLM on your VPN) is not cloud. A route marked `local_only` and a request with
  `X-Forgeyard-Local-Only: 1` (or `true`, `yes`, `on`) never use cloud targets. Only chat,
  completions and embeddings go to providers. Answers carry `X-Forgeyard-Target`, after a spill
  `X-Forgeyard-Spilled` (`unavailable`, `busy`, `refused`, `too_long` or `budget`), and
  `X-Forgeyard-Budget-Exhausted` when a budget kept the request from a provider.
- **Egress.** A separate HTTP client for providers: TLS verified (plus `EGRESS_CA_FILE`), redirects
  never followed, never a proxy from the environment (`EGRESS_PROXY` instead), `EGRESS_ALLOWED_HOSTS`.
  https only; cloud providers must resolve to public addresses; private addresses for providers inside
  your network, plain http only when an admin allows it for one (and never to a public address);
  link-local, metadata (169.254.169.254, fd00:ec2::254, 100.100.100.200), multicast, reserved and
  unspecified addresses always refused; CGNAT (100.64.0.0/10) counts as private. A base URL with
  credentials, a query or a fragment is refused. The name is resolved (cached 30 s) and checked before
  every request, and the connection goes to the checked address (TLS still verifies the name). With
  `EGRESS_PROXY`, the proxy resolves names, so only IP addresses are checked and
  `EGRESS_ALLOWED_HOSTS` is what limits names. Timeouts: `PROVIDER_CONNECT_TIMEOUT` (10 s),
  `PROVIDER_READ_TIMEOUT` (300 s, or the provider's own).
- **Provider keys** are `EncryptedText("providers.api_key")`: write-only (the API shows the last four
  characters), never logged, audited or returned, never copied by **Clone** (the copy starts without a
  key, disabled), and dropped by `secrets reset`.
- **Usage** rows carry the target (`local` or `pm:<provider model id>`), cached and reasoning tokens,
  how many requests spilled, and the cost, so cost can be shown per provider and model.
- **Cost** (`forgeyard/gateway/pricing.py`), in US dollars only. A request is priced once, when it
  finishes; changing a price later never rewrites what was counted.
  - Local replicas and providers inside your network cost 0.
  - A cost the upstream reports (`usage.cost`, OpenRouter) is used as it is.
  - Otherwise: (prompt − cached − cache writes) × input + cached × cached input + cache writes × cache
    write + completion × output, with prices in micro-USD per million tokens. Reasoning tokens are
    inside the completion, billed once at the output price.
  - No input or output price for the tokens used: the cost is unknown (NULL, counted as *unpriced*),
    not 0. No cached or cache-write price: the input price, and the cost is marked *estimated*; so is
    every request with a prompt to a kind that does not report cached tokens (Anthropic).
  - No tokens at all: 0 when the upstream refused or failed (providers do not bill those); unpriced
    when it answered (a stream closed before the usage arrived).
  - Costs are summed in pico-USD and written as whole micro-USD per usage row, the rounding remainder
    carried, so many tiny requests add up exactly.
- **Prices** live on provider models: input, output, cached input and cache write, each optional,
  with where they came from (`manual` or `openrouter`), when, and the OpenRouter id they match. Only
  models of providers outside your network are priced. **Refresh prices from OpenRouter** (admins,
  `forgeyard/api/prices.py`) reads OpenRouter's public model list (`OPENROUTER_MODELS_URL`, no key,
  through the egress rules) and returns a preview: each model, its match and old and new prices. The
  match is `confirmed` (the stored id), `exact` (an OpenRouter provider's own id), `suggested` (the
  kind's OpenRouter vendor plus the name as is, without a date suffix, or with version dashes as dots;
  or the name itself when it has a `/`), `not_listed` or `none`; an admin may type an id to compare.
  **Apply** reads the list again and writes only the ticked rows whose prices are still those shown
  (else skipped as `changed`), overwrites a manual price only when asked, and records
  `provider.prices_refreshed`. OpenRouter's per-token strings are converted exactly; negative
  ("variable") or unreadable prices are unknown. No background sync and no bundled price list.
- **Budgets** (`forgeyard/gateway/budgets.py`, `forgeyard/api/budgets.py`): what cloud requests may
  cost per UTC calendar month, in micro-USD (up to 10^15), on an **API key** (its owner, or an admin,
  which locks it), a **person** (admins, `user.budget_changed`) or a **provider** (admins,
  `provider.changed`); null removes one. They apply only to providers outside your network.
  - **Ledger.** One in-memory ledger in the server process, under a lock: this month's spent and
    reserved amounts per key, person and provider, in pico-USD. It is seeded once a month, lazily,
    from `usage_daily.cost_micros` (after writing what the usage recorder still holds; a provider's
    spend through its provider models), and read off the event loop before routing when any budget
    applies. If it cannot be read, no cloud request is sent under a budget (503
    `budget_unavailable`).
  - **Reserve.** Before a request is sent to a provider outside your network, its largest cost is
    estimated: the prompt as ceil(UTF-8 size of messages, prompt, input, tools and functions ÷ 3),
    plus (`max_completion_tokens` or `max_tokens`, else `BUDGET_DEFAULT_MAX_TOKENS` = 4096) × `n` (2–128)
    completion tokens (none for embeddings), at the model's prices. It is reserved against every budget
    that applies at once; if spent + reserved + this request is more than one of them, or the model has
    no price (also for OpenRouter, which reports cost only afterwards), that target is skipped, and the
    next target, tier or a busy local replica may still serve it.
  - **Settle and release.** When the request finishes, its reservation is replaced by its real cost,
    added to the key, the person and the provider (under the same lock as counting its usage, so the
    seeding sees it exactly once). A failed or refused request releases its reservation. An answered
    request whose cost is unknown counts at its reservation, in memory only, until the server restarts.
  - **402.** When nothing else can serve: `402` (type `insufficient_quota`), code `budget_exhausted` or
    `budget_unpriced`, with an extra `budget` object (scope, name, budget, spent, reserved and request
    in micro-USD, month and `resets_at`) and `X-Forgeyard-Budget-Exhausted` (`key`, `person`,
    `provider:<name>`); that header also comes on a successful answer from another target. 402 because
    OpenAI's SDKs retry 429 but not 402, and retrying cannot help before the month ends.
  - **Alerts.** At 80% and 100% of a budget, once per scope, month and threshold: the `budget_alerts`
    table (unique, so also once across restarts) and an audit event `budget.warning` or
    `budget.exhausted` (actor `forgeyard`). Checked when a request is reserved and settled, so a budget
    lowered below the month's spend alerts on its next request; a refusal while spend and reservations
    are past 80% counts as 100%. Written with the usage, every `USAGE_FLUSH_SECONDS`, and on shutdown.
  - `GET /api/budgets` (admins: `?everyone=true`) gives the month, `resets_at` and, per key, person
    and provider, the budget, spent, reserved, the share used and a level (`ok`, `warning` from 80%,
    `exhausted` from 100%; a budget of 0 is exhausted). The API keys page, **Usage → Cost** (*Budgets
    this month*) and a provider's page show them.
- **Not in 1.2:** native Anthropic or Gemini translators and a `/v1/messages` passthrough; the
  Responses API, batch, files, images; cloud rerank, score or audio; fallback mid-stream; cost-,
  latency- or semantic routing between clouds; sticky routing; Entra ID, Vertex or Bedrock auth.

## Accounts and sign-in

- **Roles:** `admin` manages users, machines and their GPU allowlists, the model catalog, recipes
  and sources, cloud providers, routes and prices, the cloud setting for keys, and people's and
  providers' budgets, and sees the audit log; `operator` runs workloads and deploys models; `user` can
  look, use deployed models through the playground and their own API keys, and see their own usage. The **owner** (the first account) is always an
  admin and cannot be demoted, disabled or deleted, so nobody can lock administration out.
- **Passwords:** argon2id; at least 12 characters; common passwords (also with digits or
  symbols tacked on) and passwords containing the username are refused.
- **New users** are created by an admin, who gets a **one-time link** (72 h) to send them; the
  person sets their own password there. Admins can issue a reset link (24 h) the same way. The
  token sits in the URL fragment, so it never reaches server logs or a `Referer`.
- **Self-registration** is off. `REGISTRATION=approval` lets people request an account; it waits
  as *pending* until an admin approves it.
- **Sessions are server-side.** The cookie (`HttpOnly`, `SameSite=Strict`, `Secure` over HTTPS)
  holds a random token; the database keeps only its SHA-256. Signing out, changing a password,
  disabling an account or an admin's *Sign out* end the session on the very next request — a
  copied cookie stops working. Sessions end after 2 h idle and 7 days at most, checked on the
  server. *My account* lists your sessions and signs any of them out.
- **Rate limits:** after 5 failed sign-ins on an account each further try waits (2 s, doubling,
  at most 5 min); an IP with 30 failures in 15 min waits too. Nothing is ever locked for good, and
  a limit never signs anyone out. `X-Forwarded-For` counts only from `TRUSTED_PROXIES`.
- **Request guards:** a request for a host name that is not ours gets **421** (DNS rebinding);
  writes to `/api` need the `X-Forgeyard: 1` header and, when the browser sends one, a
  same-origin `Origin` / `Referer`.
- **Audit log:** sign-ins, failed sign-ins, every account change, and changes to machines, models,
  recipes, deployments, workloads, keys, providers, routes and prices, plus budget alerts, with who,
  what and from where.
- **Secrets at rest:** Hugging Face tokens, replicas' internal keys, workload environment values and
  cloud providers' API keys are stored encrypted (AES-256-GCM) with a server key that is not in the database
  (`data/secret.key`, mode 0600, or `SECRET_KEY_FILE` / `FORGEYARD_SECRET_KEY`). The server refuses
  to start with a missing or wrong key, instead of making a new one over the old secrets. If the
  key is lost, `python -m forgeyard secrets reset --yes` starts over: the tokens and provider keys are
  entered again, and the replicas restart with new keys. Passwords, sessions, API keys and join tokens are
  hashed, not encrypted.
- The UI goes to the sign-in page only on **401**, never on 403, 429 or a server error, and comes
  back to the page you were on afterwards.
