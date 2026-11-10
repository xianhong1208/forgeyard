# Forgeyard

One control plane for the GPUs (and CPUs) you own: NVIDIA, AMD ROCm and CPU-only machines
on one LAN or VPN, later rented cloud machines too. Agents report each machine's hardware;
you run services, tasks and dev environments on them, and deploy models from recipes behind
one OpenAI-compatible gateway.

> **Status: milestones M0 to M4, the MVP (1.0.0).** The server, database, web UI and
> sign-in are in place; machines join through the agent and report their hardware; you run
> services and tasks on them, as containers or processes, with only the GPUs they were given; you
> deploy models from a catalog with engine recipes, placed by memory estimate; and every deployment
> answers behind one OpenAI-compatible gateway at `/v1`, with API keys, limits, usage statistics and
> a playground.

[繁體中文說明](README.zh-TW.md)

## Run it

Needs Python 3.13 with [uv](https://docs.astral.sh/uv/), and Node 22.12+ to build the UI.

```bash
cd web && npm ci && npm run build && cd ..   # builds the UI into forgeyard/static
uv sync
uv run python main.py                        # http://127.0.0.1:7860 — migrations run on boot
```

On the first start there is no account. The log prints a **one-time setup token**:

```
Forgeyard has no owner yet. Open http://127.0.0.1:7860/setup and enter this setup token:
    3x…
```

Open `/setup`, paste the token and create the **owner**. The token is also kept in
`data/secrets.json` (mode 0600) until setup is done, then deleted. Without it nobody can claim
a fresh server, so it is safe to start Forgeyard on a shared network before you have set it up.
To skip the page, set `ADMIN_USERNAME` and `ADMIN_PASSWORD` before the first start.

Settings come from the environment or a `.env` file; see [.env.example](.env.example).
`HOST=0.0.0.0` opens it to the LAN. PostgreSQL instead of SQLite:
`uv sync --extra postgres` and `DATABASE_URL=postgresql+psycopg://…`.

## Machines

Each machine runs the **Forgeyard agent** (same repository, `python -m forgeyard agent`). The
agent dials out to the server, so the server never needs to reach the machine.

1. An admin opens **Machines → Add machine** and gets a command with a **one-time join token**
   (single use, valid 15 minutes, shown once, stored as a hash).
2. On the machine, in a Forgeyard checkout (`uv sync`, plus `--extra nvidia` on NVIDIA machines):

   ```bash
   uv run --no-sync python -m forgeyard agent --server http://<server>:7860 --join-token fyj_…
   ```

   The agent trades the token for **this machine's own credential**, which it keeps in its
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
- **Transport:** plain `ws://` is fine on a LAN or VPN, which Forgeyard assumes. For `wss://`
  put the server behind a TLS reverse proxy or Tailscale and pass an `https://` URL. The host
  check applies to agents too, so an agent reaching the server by a name rather than an IP
  needs that name in `ALLOWED_HOSTS` (or as `PUBLIC_URL`). Agent endpoints never accept a
  browser's session cookie, and refuse any request carrying an `Origin` header.
- **Simulated inventory** (development and tests only): `--simulate-inventory nvidia-a100x8 |
  nvidia-rtx4090x2 | amd-gfx1151-apu | <file.json>` reports a fixture instead of real hardware.
  It works only with `FORGEYARD_AGENT_SIMULATION=1`, and the server marks such machines
  *simulated inventory*.

Real-hardware checks for this milestone: [docs/acceptance-m1.md](docs/acceptance-m1.md).

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
| `resources` | `cpu` (cores), `memory` (`4Gi`, `512Mi` or bytes), `gpus`: `{"count": n}` or `{"indices": [4]}` (on a chosen machine), optional `"vendor": "nvidia"/"amd"` |
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

Real-hardware checks for workloads: [docs/acceptance-m2.md](docs/acceptance-m2.md).

## Models

**Models → Deploy a model**: pick a model from the catalog and a recipe, set the parameters, and
the page shows how much memory one replica needs and where it would go, before anything is created
or downloaded. A deployment is one service workload (it also appears under Workloads) plus a
gateway route: programs reach it at `/v1` with its **model name** (see *Gateway and API keys*). Its
page shows that name, its usage, each replica and a small **Try it** that checks one replica directly.

- **Catalog** (admins): add `hf://<org>/<name>` (optionally a revision and, for GGUF repositories,
  the file), a GGUF URL from an allowed host, or `local://<dir>` under an allowed directory. The
  model is **inspected without downloading it**: the commit it pins to, the files and their size,
  `config.json`, the safetensors headers (parameter counts) or the GGUF header (read with HTTP range
  requests). A token for gated repositories is used for that only, never stored. Admins can add
  engine arguments per recipe to a model (Qwen3-Reranker needs vLLM's `--hf-overrides`).
- **Recipes** are YAML ([forgeyard/recipes](forgeyard/recipes)): tasks, formats, the protocol per
  task, parameters, the estimate method, a health check, and **variants** per hardware (vendor,
  NVIDIA compute capability or AMD gfx target, driver version), each a pinned container image or,
  for CPUs without Docker, a pinned release the agent downloads and checks against its SHA-256.
  Admins add custom recipes on the Models page. Built in:

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
  when they can (before binpack), so the gateway can balance and fail over between them.
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

Real-hardware checks: [docs/acceptance-m3.md](docs/acceptance-m3.md) and `scripts/accept_m3.py`.

## Gateway and API keys

Every deployment answers at **`http://<server>:7860/v1`**, OpenAI-compatible, on the console's own
port: `GET /v1/models`, `POST /v1/chat/completions` and `/v1/completions` (streaming passed through),
`/v1/embeddings`, `/v1/rerank` and `/v1/score` (vLLM's shapes), `/v1/audio/transcriptions` and
`/translations` (multipart). The `model` is the deployment's served name. Any OpenAI client works:

```python
from openai import OpenAI
client = OpenAI(base_url="http://<server>:7860/v1", api_key="fyk_…")
client.chat.completions.create(model="qwen3-4b", messages=[{"role": "user", "content": "Hi"}])
```

- **API keys** (page **API keys**): everyone creates their own; a key is `fyk_` + 43 random
  characters, **shown once** with a curl and a Python example; the server keeps its SHA-256 and a
  12-character prefix. Optional: an expiry, the models it may use (else every deployment, also later
  ones), requests and tokens per minute. Revoking, an expiry or a disabled account stop it on the next
  request. Admins see and revoke everyone's keys, change any key's limits, and set the **defaults**
  (for keys without their own limit) and **caps** (the most people may give their own keys). Creating,
  changing and revoking keys are in the audit log.
- **Only keys on `/v1`**: the console's cookie is never read there (a web page cannot use a signed-in
  browser against it), so the console's CSRF rule does not apply; the host check (421) does. No CORS.
- **Routing:** the ready replicas of the model, fewest requests in flight first, ties in turn. If
  connecting to one fails before anything was sent back, the next is tried and the failed one goes
  last until its agent reports it ready again (at most 15 s). A client that disconnects closes the
  request upstream, so the engine stops. Timeouts: 5 s to connect, 600 s for the headers or the next
  piece of a stream (`GATEWAY_CONNECT_TIMEOUT`, `GATEWAY_READ_TIMEOUT`).
- **Errors** in OpenAI's shape `{"error": {"message", "type", "param", "code"}}`: 401 key missing,
  wrong, expired or revoked; 403 model not allowed for the key; 404 unknown model; 400 wrong endpoint
  for the model; 429 over a limit, with `Retry-After`; 503 no ready replica, with each replica's state;
  502 replica unreachable or refusing its internal key; 504 no answer in time. An engine's own errors
  are passed on as they are.
- **Limits** per key, sliding windows over the last 60 s, counted in this server's memory (several
  servers sharing limits come later): requests per minute (refused requests do not count) and tokens
  per minute from the answers' `usage` (for a streamed chat the gateway asks the engine for its usage
  chunk and removes it unless the client asked for it); while a request runs, its `max_tokens` is held
  so parallel requests cannot all slip past.
- **Usage** (page **Usage**): requests, errors, limit refusals, prompt and completion tokens, latency
  p50/p95, per day, model, key, person or source (API key or playground). Everyone sees their own;
  admins everyone's. A deployment's page shows its last 7 days. Counting only, no billing.
- **Playground**: chat with streaming, embeddings, rerank and speech to text in the console, through
  the same routing, as the signed-in user (cookie and CSRF like the rest of the console). It creates
  no API key and counts as "console" in usage.
- **Replicas cannot be used around the gateway.** Every model replica gets an **internal key**, new
  with every placement. The agent passes it to the engine in the **environment** (vLLM
  `VLLM_API_KEY`, llama.cpp `LLAMA_API_KEY`, speaches `API_KEY`; a recipe names its variable with
  `api_key_env`), never on the command line, keeps it in memory only and sends it with health checks;
  it is never shown in the UI, the API, a spec or a log. Only the gateway and *Try it* send it. A call
  straight to a replica's port without it gets 401 (speaches: 403). **vLLM 0.32 checks the key only on
  `/v1`, `/v2`, `/inference` and `/cohere`**: its `/invocations`, `/rerank`, `/score`, `/pooling` and
  `/tokenize` stay open on the replica's port, so keep the replica port range (30000-32767) reachable
  from the server only (firewall) on a shared network. Replicas started by an agent from before M4
  have no key until they are placed again (stop and start the deployment after updating the agent).

Real-hardware checks: [docs/acceptance-m4.md](docs/acceptance-m4.md) and `scripts/accept_m4.py`.

## Accounts and sign-in

- **Roles:** `admin` manages users, machines and their GPU allowlists, the model catalog, recipes
  and sources, and sees the audit log; `operator` runs workloads and deploys models; `user` can
  look, use deployed models through the playground and their own API keys, and see their own usage. The **owner** (the first account) is always an
  admin and cannot be demoted, disabled or deleted, so nobody can lock administration out.
- **Passwords:** argon2id; at least 12 characters; common passwords (also with digits or
  symbols tacked on) and passwords containing the username are refused.
- **New users** are created by an admin, who gets a **one-time link** (72 h) to send them; the
  person sets their own password there. Admins can issue a reset link (24 h) the same way. The
  token sits in the URL fragment, so it never reaches server logs or a `Referer`.
- **Self-registration** is off. `REGISTRATION=approval` lets people request an account; it waits
  as *pending* until an admin approves it.
- **Sessions are server-side.** The cookie (`HttpOnly`, `SameSite=Strict`, `Secure` over https)
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
- **Audit log:** sign-ins, failed sign-ins and every account change, with who, what and from where.
- The UI goes to the sign-in page only on **401**, never on 403, 429 or a server error, and comes
  back to the page you were on afterwards.

## Develop

```bash
uv sync
uv run --no-sync python -m pytest -q          # backend tests (SQLite)
uv run --no-sync ruff check .
TEST_DATABASE_URL=postgresql+psycopg://user:pw@host/db uv run --no-sync python -m pytest -q
                                               # same suite on PostgreSQL — WIPES that database
cd web
npm run dev                                   # UI on :5173, API proxied to :7860
npm run lint && npm run typecheck && npm test && npm run build
```

Schema changes go through Alembic (`forgeyard/migrations`); startup runs `upgrade head`, and a
test fails when the models and migrations drift apart. Test IDs (`FY-…`) are unique across the
project.

## Layout

| Path | What |
|---|---|
| `forgeyard/app.py` | FastAPI app: guards, routes, the built UI |
| `forgeyard/cli.py` | `python -m forgeyard [serve]` and `python -m forgeyard agent …` |
| `forgeyard/api/` | `auth` (setup, sign-in, sessions, links), `users`, `audit_log`, `machines`, `workloads` |
| `forgeyard/machines/` | agent protocol, join and stream endpoints, live connections, offline detection |
| `forgeyard/workloads/` | the workload spec, placement and the GPU allowlist, desired state and status |
| `forgeyard/models/` | model sources, the SSRF-guarded fetcher, inspection (safetensors, GGUF), estimates, recipes, the scheduler, routes |
| `forgeyard/recipes/` | the built-in engine recipes (YAML) |
| `forgeyard/api/models.py`, `deployments.py` | catalog, recipes, sources; deployments, estimate, try it, routes |
| `forgeyard/gateway/` | the gateway: API keys, limits, the balancer, forwarding and streaming, usage |
| `forgeyard/api/gateway.py`, `keys.py` | `/v1/*` and the playground; API keys, key limits and usage |
| `scripts/accept_m4.py` | the M4 acceptance run on real machines, with the official OpenAI SDK |
| `forgeyard/agent/models.py` | the agent's model cache: downloads, verification, engines |
| `scripts/accept_m3.py` | the M3 acceptance run on a real machine, through the API |
| `forgeyard/agent/` | the agent: inventory collectors (NVML, AMD SMI, CPU), connection, state file |
| `forgeyard/agent/workloads/` | the agent's reconciler, process and container runtimes, logs, health checks |
| `scripts/gpu_probe.py` | checks which NVIDIA GPU a workload sees, through the CUDA driver only |
| `forgeyard/security/` | passwords, sessions, rate limits, host/CSRF middleware, secrets file |
| `forgeyard/migrations/` | Alembic |
| `web/` | React + TypeScript + Vite UI, built into `forgeyard/static` |
