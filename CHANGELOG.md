# Changelog

All notable changes are recorded here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. Versions follow [Semantic Versioning](https://semver.org/); each milestone is one minor
version until 1.0.

## [Unreleased]

### Added
- **Key guard in front of every model replica.** vLLM 0.32's `VLLM_API_KEY` only covers `/v1`, `/v2`,
  `/inference` and `/cohere`; `/invocations`, `/rerank`, `/pooling`, `/score`, `/classify` and
  `/tokenize` answered anyone who could reach a replica's port. Now the engine listens on loopback
  only (a process binds `127.0.0.1`, a container is published on `127.0.0.1:<internal port>`), and the
  agent listens on the replica's published port with a small streaming reverse proxy that requires
  `Authorization: Bearer <internal key>` on every path and method (401 `replica_key_required`
  otherwise; constant-time comparison). SSE, chunked and multipart bodies stream both ways; a client
  disconnect closes the engine's connection; hop-by-hop headers are dropped; an engine that is down
  gets the connection closed unanswered, so the gateway fails over as before. Health checks go to the
  engine's loopback port; nothing is open on the guard. The same mechanism for every recipe (the
  engines' own key variables stay as defence in depth; a recipe without `api_key_env` is guarded too).
  An adopted replica's guard opens again once the server has sent its key; while the agent is down the
  replica is unreachable from outside. Replicas an older agent started are restarted behind a guard;
  a process engine found listening beyond loopback is stopped and failed. Agent feature
  `replica_guard`; with it the server gives every model replica a key (`env` null when the engine
  reads none). The deployment page shows "key guard" per replica. `h11` is listed as a dependency (it
  was installed already).
- **Limits an admin sets on someone else's key are locked**: the owner can no longer change that
  value (403 `limit_locked`), can still revoke the key and set the other limit; the API keys page shows
  a lock with the admin's name, disables the field for the owner and offers admins *Unlock* (clearing
  the value, back to the default). Locking and unlocking are audited (`api_key.changed` with `locked` /
  `unlocked`). Migration `0006_key_locks`.
- `scripts/accept_m4.py`: the vLLM paths (`/invocations`, `/rerank`, `/pooling`) on every replica
  port are PASS checks expecting the guard's 401 (they were INFO lines), and a client's API key on a
  replica port must get 401 too; model allowlists are checked for real (a key limited to one deployment
  lists and reaches only that one, 403 `model_not_allowed` for the others; a key with no list made
  before the deployments sees and reaches all of them).
- **Get started** checklist, the home page after sign-in until it is done or hidden, and always
  under the account menu: six steps for admins and operators (add a machine, choose its GPUs, add a
  model, deploy it, create an API key, call `/v1`), three for users (create a key, call `/v1`, see
  usage). Each step's state comes from real state: a machine online; every machine with GPUs has an
  allowlist or a saved "all of them" (read from the audit log); a catalog entry; a deployment with a
  ready replica; an active key of the person's own; a request with one of their keys through the
  gateway; for users, having opened Usage. Steps only admins can do say so to operators. A Python
  and curl example for this server's `/v1` uses the ready deployment (chat first) and the key from
  `FORGEYARD_API_KEY`. `GET/PUT /api/onboarding`, `POST /api/onboarding/seen`; hiding is stored per
  person in the new `users.preferences` (migration `0007_preferences`).
- **Recommended models** on Add a model, from `forgeyard/models/recommended.yaml` (27 entries in four
  groups: Qwen3, other open chat models, speech to text, embeddings and rerank; Gemma 4 E2B, E4B,
  12B, 26B-A4B and 31B need no token, and vLLM 0.32.0 has both of their architectures), checked at load
  (catalog names, `hf://` sources, groups, a built-in recipe that serves the task and format, a GGUF
  file for GGUF entries only, a CPU variant for CPU entries, engine arguments) and by
  `scripts/check_recommended.py` against Hugging Face. Gated entries (Gemma 3, Llama) are marked as
  needing a token and the licence. `GET /api/models/recommended` (admins).
- **Hugging Face search** through the server: `GET /api/models/search?q=&task=&format=&limit=`
  (admins), one request per Hugging Face pipeline tag of the task (embeddings: feature-extraction
  and sentence-similarity; rerank: text-ranking and text-classification), over the guarded fetcher to
  the configured `HF_ENDPOINT` only, 10 s timeout, results trimmed (no chat templates), merged and
  sorted by downloads (for rerank, names containing "rerank" first), cached 5 minutes, 30 searches per minute per person; 503 `hub_unreachable`
  when Hugging Face cannot be reached. A result on the recommended list is added the way the list
  says.
- **Inspecting a model suggests how to run it**: `POST /api/models/inspect` also returns the recipe
  (the one asked for if it fits, else the first that serves the model), the device (GPU when an
  online machine has an allowed GPU, else the CPU when the recipe has a CPU variant), the estimate
  with the recipe's defaults, and the scheduler's verdict per machine, so the page shows "fits on …"
  before the model is added. The deploy page takes `?recipe=` and `?device=` from "Deploy it".

- **HTTPS out of the box, from Forgeyard's own certificate authority.** On the first start the server
  makes a CA (EC P-256, 10 years, key 0600 in `data/tls`) and a server certificate signed by it
  (397 days), naming `PUBLIC_URL`'s host, `ALLOWED_HOSTS`, `TLS_EXTRA_NAMES`, the host name, the
  machine's own addresses and loopback. It is renewed 60 days before expiry, or when a name is
  missing; a running server checks twice a day and switches certificates without a restart. Your own
  certificate: `TLS_CERT_FILE` / `TLS_KEY_FILE` (reloaded when the files change), with `TLS_CA_FILE`
  for the CA agents pin. A missing CA key next to existing certificates stops the start instead of
  making a new CA. See [docs/https-and-trust.md](docs/https-and-trust.md).
- **Agents pin the CA.** The join command carries `--ca-fingerprint sha256:…`. The agent fetches the
  CA, refuses it unless the fingerprint matches, joins and connects over `https://` / `wss://`
  verified against that CA only (not the system store), and keeps it as `server-ca.pem` in its state
  directory. `--ca-file` pins a CA file instead. **`forgeyard agent trust --server https://…
  --ca-fingerprint …`** switches an agent that joined over plain HTTP, keeping its credential. An old
  agent that cannot connect logs that command.
- **Trust this server** (`/trust`, public): why the browser warns, in plain words; the CA's
  fingerprint and expiry; downloads as PEM and DER; steps for Windows, macOS, Linux (Chrome and the
  system store), Firefox, iOS and Android, with the device's own steps opened first; curl and Python.
  Admins also see the server certificate's names and expiry, and the trust command. Linked from the
  sign-in page, the account menu, and the Machines page when machines are offline.
  `GET /api/tls`, `/api/tls/ca.pem` and `/api/tls/ca.crt`.
- `/v1` examples on the API keys page and in *Get started* trust the CA when the server uses its
  own: `curl --cacert forgeyard-ca.pem`, and the OpenAI SDK with
  `http_client=httpx.Client(verify=ssl.create_default_context(cafile="forgeyard-ca.pem"))`.
- **Secrets encrypted at rest**: Hugging Face tokens, model replicas' internal keys and workload
  environment values are stored with AES-256-GCM, each bound to its column, under a key that is not
  in the database: `data/secret.key` (0600, made on the first start), `SECRET_KEY_FILE`, or
  `FORGEYARD_SECRET_KEY`. Migration `0008_encrypt_secrets` encrypts existing rows (SQLite and
  PostgreSQL); a plain-text value afterwards is an error. The database records the key's id, so a
  missing or wrong key stops the start with what to do. `python -m forgeyard secrets reset --yes`
  starts over after a lost key: Hugging Face tokens dropped, model replicas placed again with new
  keys, and lost environment values emptied, with an event on each workload.
- **Engine images pinned by digest**: every built-in recipe image is `repo:tag@sha256:…` (the
  multi-architecture index digest of the tag, checked for linux/amd64 and, where a variant claims
  aarch64, linux/arm64). The agent pulls and runs the digest; the UI shows the tag.
  `scripts/pin_images.py` checks the digests against the registries, and `--write` re-resolves
  them when an engine is bumped.

### Changed
- **HTTPS is the default.** Plain HTTP only with `TLS=off`, meant for running behind a proxy that
  terminates TLS (nginx and Caddy examples in the docs); while pages still arrive over plain HTTP,
  every admin sees a warning at the top of each page, and the startup log warns too. Session cookies
  are `Secure` under HTTPS (also behind a trusted proxy that says `X-Forwarded-Proto: https`).
  `Strict-Transport-Security` is sent only with your own certificate: with Forgeyard's CA the
  certificate warning must stay clickable until a device trusts the CA.
- An `http://` `PUBLIC_URL` kept from 1.0 is read as `https://` while TLS is on, with a warning, so
  join commands, set-password links and the gateway address keep working.
- New dependency: `cryptography` (CA, certificates, AES-GCM).
- `scripts/accept_m3.py` and `accept_m4.py` default to `https://127.0.0.1:7860` and take `--cacert`;
  the SDK gets the CA through its `http_client`.
- `npm run dev` proxies the API to `https://127.0.0.1:7860`.
- Spreading a deployment's replicas over machines is documented as the default (unchanged).
- Every empty page says what it is for and offers the next action: Machines (Add a machine, with
  how joining works), Workloads (New workload, or serve a model instead), Models (add a model first
  when the catalog is empty, else deploy one), the catalog, the deploy page with an empty catalog,
  API keys (Create a key), Usage (create a key, open the Playground) and the Playground (deploy a
  model, or who to ask). The Models page no longer says replicas answer only on their own ports.
- The Machines page's button reads "Add a machine", like the checklist.

## [1.0.0] — 2026-11-10

The MVP: milestones M0 to M4, each accepted on a real 8×A100 host (one shared card) plus a CPU machine.

### Added — M4: unified API (the MVP)
- **Gateway** at `/v1` on the server's own port, OpenAI-compatible: `GET /v1/models` (and
  `/v1/models/{model}`), `POST /v1/chat/completions`, `/v1/completions` (SSE streaming passed through
  as it arrives), `/v1/embeddings`, `/v1/rerank`, `/v1/score`, `/v1/audio/transcriptions` and
  `/translations` (multipart, the model field rewritten, the audio untouched). Only API keys open it,
  never the console cookie; no CSRF rule there, the host check applies; no CORS. Errors in OpenAI's
  shape: 401, 403 (model not allowed), 404 (unknown model), 400 (wrong endpoint), 429 with
  `Retry-After`, 503 (no ready replica, with each replica's state), 502, 504.
- **Routing**: least requests in flight per ready replica, ties in turn; a replica that cannot be
  connected to is skipped before anything reaches the client and tried last until its agent reports
  it ready again (at most 15 s); a client that disconnects closes the upstream request.
  `GATEWAY_CONNECT_TIMEOUT` (5 s), `GATEWAY_READ_TIMEOUT` (600 s), `GATEWAY_MAX_BODY_MB` (32).
- **API keys**: `fyk_` + 256 random bits, shown once (with curl and Python `openai` examples), stored
  as SHA-256 with a 12-character prefix; optional expiry and model allowlist; rpm and tpm per key, with
  admin-set defaults and caps (`GATEWAY_DEFAULT_RPM`/`_TPM`, `GATEWAY_MAX_RPM`/`_TPM` as initial
  values); revoke, expiry and a disabled owner take effect on the next request. Everyone manages
  their own keys; admins list and revoke all keys and change any key's limits. Audit events
  `api_key.created`, `api_key.changed`, `api_key.revoked`, `api_key_limits.changed`.
- **Limits**: sliding 60-second windows per key in the server's memory; tokens from the answers'
  `usage` (for streamed chats the gateway sets `stream_options.include_usage` and strips that chunk
  unless the client asked for it); `max_tokens` held while a request runs.
- **Usage statistics** per UTC day, source (API key or console), key, user and model: requests,
  errors, limit refusals, prompt and completion tokens, latency histogram (p50/p95); written every
  5 s (`USAGE_FLUSH_SECONDS`). `GET /api/usage` (own; admins everyone's), per deployment on its page.
- **Internal replica keys**: every model replica gets a random key per placement; the agent passes it
  to the engine through the environment only (`api_key_env` in recipes: `VLLM_API_KEY` for vLLM,
  `LLAMA_API_KEY` for llama.cpp, `API_KEY` for speaches), keeps it in memory only, and sends it with
  health checks (speaches guards `/health`); the gateway and *Try it* send it upstream. Never in the
  API, specs, events or logs. Agent feature `replica_keys` (protocol v1, compatible).
- **Playground** in the console (`/api/playground/…`, session + CSRF): chat with streaming,
  embeddings, rerank, speech to text; no API key is created; counted as "console".
- Pages **API keys**, **Usage**, **Playground**; a deployment's page shows its `/v1` model name and
  endpoint, its usage and each replica's requests and whether its engine requires the internal key.
- `scripts/accept_m4.py` (makes its own venv with the official `openai` SDK when needed) and
  `docs/acceptance-m4.md`.
- Migration `0005_gateway`. `httpx` is a runtime dependency; `openai` a dev dependency (tests).

### Changed — M4
- The request guard is a pure ASGI middleware (streamed answers pass straight through).
- Replicas of one deployment are spread over machines before binpacking.
- *Try it* sends the replica's internal key.

### Fixed — M4
- A process replica whose wrapper died left its engine running and holding the port, so every restart
  failed. Leftovers of the process group were only recognised by a marker in `/proc/<pid>/environ`,
  which a root agent in a container without `CAP_SYS_PTRACE` cannot read for another user's
  processes; the group itself is used then (unless its id was reused).

### Added — M3: model deployment
- **Catalog** of models (admins): `hf://<org>/<name>` pinned to the commit a revision resolves to, a
  GGUF file from an admin-allowed host, or `local://` under admin-allowed directories. Models are
  inspected without downloading the weights: file list and sizes, `config.json`, parameter counts
  from the safetensors headers, the GGUF metadata and exact tensor sizes by HTTP range reads. Tokens
  for gated repositories are used for the inspection only. Per-recipe engine arguments per model.
- **Recipes** as YAML (built-in and admin-added): tasks, formats, protocol per task, parameters,
  estimate method, health check with a start period, and variants per vendor / compute capability /
  gfx target / driver, each a pinned image or a pinned, SHA-256-checked release for a process.
  Built in: `vllm` (vLLM 0.32.0), `vllm-pooling` (embeddings and rerank on the same image),
  `llama-cpp` (b11977: CUDA, ROCm and CPU images, and a CPU process variant) and `faster-whisper`
  (speaches 0.8.3).
- **Estimates** before deploying: weights from parameters x bytes (FP8 aware) or GGUF tensor sizes,
  KV cache from layers x KV heads x head size x tokens, logits buffer, recipe overhead, CUDA context.
  A declared `vram` overrides it.
- **Scheduler** for model deployments: filter (variant for the hardware and driver, runtime,
  allowlist, memory), score (binpack, then a machine with the model already cached). Memory is the
  card's free memory as reported, minus reservations not yet visible there, minus a headroom
  (`VRAM_HEADROOM_MIB`, 512). Refused with every machine's reason and shortfall when nothing fits.
- **GPU sharing** (`share: vram`) enforced by reservation; vLLM's `--gpu-memory-utilization`
  computed from the reservation and the card's total. Replicas on one card start one at a time.
  Workloads can declare `gpus.vram` and `gpus.share` too; M2 workloads keep their behaviour.
- **Deployments**: a service workload (runtime `model`, rendered per replica for its machine and
  variant) plus a **gateway route** (served name, protocol, upstream model name) for M4;
  `GET /api/routes`. Each replica is reachable on its own port meanwhile; **Try it** sends one small
  request in the route's protocol (a bundled public-domain clip for speech).
- **Agent model cache** (`--model-cache DIR`): Hugging Face cache layout keyed by source and
  revision, shared by replicas, resumable downloads verified by SHA-256 or git blob id, progress
  reported, cancelled when unwanted, read-only files, the model's directory mounted read-only into
  containers (the only volume ever mounted); engines for process variants unpacked safely. Tokens
  stay in the agent's memory. `--model-local-dir`, `--allow-private-model-hosts`.
- **SSRF guard** for every model request on server and agent: https, every resolved address public
  (no private, loopback, link-local/metadata, CGNAT, reserved), connections pinned to the checked
  address, every redirect re-checked, credentials dropped when a redirect leaves the host.
- Models pages: deployments (download progress, endpoints), catalog and recipes (add a model with an
  inspection preview, custom recipes, model sources), deploy form with a live estimate and placement
  preview, deployment page (estimated vs live memory, endpoint, try it, output, events).
- API `/api/models` (+ `/inspect`), `/api/recipes`, `/api/model-sources`, `/api/deployments`
  (+ `/estimate`, `/stop`, `/start`, `/try`), `/api/routes`. Audit events `model.added`,
  `model.changed`, `model.removed`, `recipe.created`, `recipe.updated`, `recipe.deleted`,
  `model_sources.changed`, `deployment.created`, `deployment.stopped`, `deployment.started`,
  `deployment.deleted`.
- Agent protocol v1 extended compatibly: feature `models`; desired replicas carry a rendered spec,
  the model (with a token when needed) and an engine; status carries download progress and the model
  cache. M2 agents get specs without the new fields.
- `scripts/accept_m3.py` and `docs/acceptance-m3.md`: the real-hardware run.
- Migration `0004_models`.

### Changed — M3
- The `/deployments` path now opens Models.
- Health checks have `start_period`: failures before the first success do not count during it.
- The agent's reconciler only holds new starts back while replicas are being stopped (a slow start,
  such as an image pull or a download, no longer delays other replicas).
- A container whose image is not on the machine yet still starts when the docker CLI's credential
  helper (`credsStore` / `credHelpers`) fails under the agent. That failure stopped even public
  images, such as the llama.cpp one on the first real run. The agent now pulls once more with a
  copy of the CLI config that leaves out the helpers and keeps everything else, then starts the
  container. Other start failures are reported as before.

### Added — M2: generic workloads
- Workloads: **services** (run until stopped, health-checked, restarted with backoff) and **tasks**
  (run to the end; `retries` with doubling backoff; `max_duration` stops and fails a run), 1-16
  fixed replicas, as a **container** (Docker or Podman CLI) or a **process**. A small explicit spec:
  name, kind, runtime, env, resources (cpu, memory, gpus by count or by index, vendor), ports, health,
  restart, max_duration, retries, replicas, machine.
- Placement: the chosen machine, or the first online machine with the runtime, free allowed GPUs and
  the declared CPU and memory. GPUs are exclusive. What does not fit waits as *pending* with the
  reason and is placed when room appears (also by a background sweep).
- Per-machine **GPU allowlist** (`PUT /api/machines/{id}/gpu-allowlist`, admins; Machines page →
  Change GPU access). Cards outside it are never assigned and are marked *not for Forgeyard*; a card
  in use cannot be taken off; audit event `machine.gpu_allowlist_changed` with before and after.
- Isolation: workloads start from an empty environment (never the agent's); GPU visibility by UUID
  (`CUDA_VISIBLE_DEVICES`/`NVIDIA_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES` + `HIP_VISIBLE_DEVICES`)
  for processes, `--gpus "device=<uuid>"` / CDI / `--device /dev/kfd` + the assigned render node for
  containers; empty lists and `NVIDIA_VISIBLE_DEVICES=void` without GPUs. Containers run with
  `--security-opt no-new-privileges`, never `--privileged`, ports published on one address.
- A root agent runs process workloads only as `--workload-user` (refused without one). Processes get
  their own process group, a systemd scope with CPU/memory limits on systemd hosts, rlimits otherwise.
- Desired-state reconciliation: the server sends each agent the full list of its replicas (spec and
  generation) on connect and on change; the agent converges, adopts what it started after a restart
  (pid + start time + boot id; container labels), never starts anything twice, stops before it
  starts, and reports per-replica status (pending, starting, running, healthy, unhealthy, backoff,
  exited, failed, stopped).
- Output: kept per replica on the agent (`--log-max-mb`, 10 MB), live tail on the workload page
  (server asks the agent over the WebSocket), last 100 lines kept on the server when a replica ends.
- Agent protocol v1 extended compatibly: `features` in hello/welcome, and `desired`, `status`,
  `logs_request`, `logs`. M1 agents keep working without work; M2 agents on an M1 server only report
  hardware.
- Agent options `--workload-user`, `--work-dir`, `--port-range`, `--bind-address`,
  `--container-engine`, `--limits`, `--log-max-mb`, `--no-workloads`; `forgeyard agent runtimes`.
- Workloads page (list with replicas, machine, GPUs, addresses), New workload form (with the spec it
  sends), and a workload page with replicas, events, live output, stop / start / delete.
- API `/api/workloads` (list, create, get, stop, start, delete, replica logs). Admins and operators
  run workloads; users see them without environment values or output. Audit events
  `workload.created`, `workload.stopped`, `workload.started`, `workload.deleted`.
- `scripts/gpu_probe.py`: which NVIDIA GPU a workload sees, through the CUDA driver only.
- `docs/acceptance-m2.md`: the real-hardware checklist for a shared 8-GPU host restricted to one card.
- Migration `0003_workloads`.

### Changed — M2
- The *Deployments* placeholder became **Workloads** (`/deployments` redirects); model deployments
  come with M3.
- The AMD collector records the KFD `unique_id` (as `rocr_uuid`) and `drm_render_minor` per card.
- Revoking or removing a machine sends its replicas back to pending (placed elsewhere, or waiting).

### Added — M1: machines join
- Agent in the same package: `python -m forgeyard agent` (also `python main.py agent`). It joins
  with a one-time token, keeps a WebSocket to the server and reconnects by itself (exponential
  backoff with jitter, at most 15 s).
- Join tokens: created by admins on the Machines page, single use, valid 15 minutes, shown once
  and stored as a SHA-256 hash, with the exact command to run. Unused tokens can be cancelled.
- Per-machine credential issued at join, stored hashed on the server and in a 0600 file on the
  machine. Revoking or removing a machine kills the credential and closes its connection at once.
- Agent protocol v1 over `/agent/v1/stream` (JSON `hello`, `heartbeat`, `inventory`, `welcome`,
  `error`, with application close codes), and `POST /agent/v1/join`. Agents authenticate with a
  Bearer credential only. A browser cookie never counts, a request with an `Origin` header is
  refused, and the host check (421) now covers WebSocket handshakes too.
- Heartbeat every 10 s. A server-side sweep marks a machine offline after 30 s of silence and
  closes the dead connection; machines start offline after a server restart.
- Inventory collectors: NVIDIA via NVML (`--extra nvidia`), AMD via the `amdsmi` library, the
  `amd-smi` CLI or `rocm-smi` (gfx target as `arch`; unified-memory APUs such as gfx1151 detected
  from the gfx target or the KFD topology), and CPU and RAM from `/proc`. All read-only, all fail
  soft.
- `python -m forgeyard agent inventory [--json] [--raw]` shows what the agent detects without
  joining. `--simulate-inventory` (with `FORGEYARD_AGENT_SIMULATION=1`) replays a fixture. Fixtures
  ship for 8 × A100 80GB, 2 × RTX 4090 and a Strix Halo APU, and the server marks such machines
  as simulated.
- Machines page: fleet totals, and for each machine its status, last heartbeat, agent version,
  labels, and every card's vendor, model, arch, free / total memory, unified-memory badge,
  driver, utilisation and temperature. It refreshes every 5 s. Admins add, revoke and remove
  machines; everyone signed in can look.
- Audit events `machine.join_token_created`, `machine.join_token_cancelled`, `machine.joined`,
  `machine.join_refused`, `machine.revoked`, `machine.removed`.
- `docs/acceptance-m1.md`: real-hardware checklist (NVIDIA, AMD ROCm, CPU, several machines).
- Settings `AGENT_HEARTBEAT_SECONDS` (10) and `AGENT_OFFLINE_SECONDS` (30).

### Added — M0: skeleton and sign-in
- FastAPI server with SQLAlchemy 2 and Alembic (migrations run at startup); SQLite by default,
  PostgreSQL through `DATABASE_URL`.
- React + TypeScript + Vite web UI served by the server: setup, sign-in, request an account,
  set password, Machines and Deployments (empty until M1/M2), Users, My account and sessions,
  Audit log.
- First run: a one-time setup token printed in the log and kept in a 0600 secrets file, or an
  owner from `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
- Accounts with roles admin, operator and user; a protected owner; argon2id passwords with a
  minimum length and a common-password check; admin-created accounts set their own password
  through a one-time link; optional self-registration with admin approval.
- Server-side sessions: sign-out, password change, disabling an account and admin sign-out end
  them immediately; 2 h idle and 7-day absolute limits; per-session sign-out.
- Host check (421), CSRF protection on API writes, per-IP request budget and sign-in backoff that
  never locks an account for good and never signs anyone out.
- Audit log of sign-ins and account changes.
- CI: ruff and pytest on SQLite and PostgreSQL 17, frontend lint, typecheck, tests and build.
