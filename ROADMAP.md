# Roadmap

[繁體中文](ROADMAP.zh-TW.md)

Where Forgeyard is going after 1.0. The order was agreed on 2026-11-10; each line names what
users get, not how it is built. Versions are minor releases unless a change breaks something.

## Shipped

**1.0.0 (2026-11-10), the MVP.** Milestones M0 to M4: sign-in and roles; machines join through
the agent; services and tasks as containers or processes with only the GPUs they were given;
models deployed from a catalog with engine recipes and memory estimates; one OpenAI-compatible
gateway at `/v1` with API keys, limits and usage. Each milestone was accepted on a real 8×A100
host plus a CPU machine. See [CHANGELOG.md](CHANGELOG.md).

## Shipped: 1.1.0 (2026-11-11), easier to start and safe on the network

Three pull requests, in this order:

1. ✅ **Getting started** (PR #9)
   - A checklist on the home page: add a machine, choose its GPUs, add a model, deploy it,
     create an API key, call `/v1`.
   - Every empty page says what to do next.
   - A list of recommended models: Qwen3, other open LLMs, Whisper, bge-m3 and bge-reranker.
   - Hugging Face search, showing an estimate and where each model would fit.
2. ✅ **Security** (PR #8 `replica-guard`, then PR `security`)
   - ✅ The agent guards every model replica, so a replica port cannot be used without the
     replica's key.
   - ✅ API key limits set by an admin are locked against the key's owner.
   - ✅ HTTPS out of the box from Forgeyard's own certificate authority:
     - ✅ the join command pins its fingerprint, and `agent trust` upgrades agents that joined
       over plain HTTP without a new join token;
     - ✅ each device trusts it once, from the *Trust this server* page;
     - ✅ your own certificate is optional;
     - ✅ examples are given for running behind nginx or Caddy.
   - ✅ Engine images are pinned by digest.
   - ✅ Stored secrets are encrypted.
3. ✅ **Install and guide** (PR `install-guide`)
   - ✅ The agent reads a config file, `/etc/forgeyard/agent.toml`, instead of a long command line;
     `forgeyard agent init` writes one from what it finds.
   - ✅ A one-line install served by your server sets the agent up as a system service (and upgrades
     or removes it).
   - ✅ An agent container image: one `docker run` per machine (built locally until the source is
     published with 1.2).
   - ✅ A user guide in English and Traditional Chinese: quick start, concepts, how-tos,
     troubleshooting, reference; also served in the app at `/guide`, linked from the pages.

1.1 is complete.

## Shipped with 1.2: publishing the source

Agreed on 2026-11-10. Forgeyard becomes public with 1.2. By then a newcomer can install it alone
(the agent image, the one-line install, the guide), it is on HTTPS by default, and it offers
something the alternatives do not (see below). One pull request before the 1.2 release prepares it:

1. **AMD ROCm accepted on real hardware.** The README claims AMD support, and it has only been
   checked against recorded data so far.
2. **A licence:** Apache-2.0.
3. **A full scan** of every commit, issue and pull request for secrets and anything specific to
   private infrastructure. Certificates and keys made at run time stay in the data directory and
   are never committed.
4. **Files for contributors:** `CONTRIBUTING.md`, `SECURITY.md` (how to report a vulnerability),
   issue templates.
5. **Repository settings:** public; secret scanning, push protection and Dependabot alerts on; a
   ruleset protecting `main` and `dev`.

Done with 1.2.0 (2026-11-11):

- ✅ 2, 3 and 4. The scan covered every blob and message on every branch, tag and pull request ref,
  plus every issue, pull request, comment, review, release and label: nothing private was found, and
  the GPU UUIDs in test fixtures are not those of any real machine used in testing. The detailed
  acceptance records of the test machines are kept out of the published repository.
- ✅ 1 in part, on a real Strix Halo (Radeon 8060S, gfx1151, ROCm 7.0.2):
  - the inventory, through the `amdsmi` library;
  - joining from another network, and the GPU allowlist;
  - a model deployed and called through `/v1` with llama.cpp's Vulkan build as a custom recipe, on
    a machine with no internet access.

  Still open: the ROCm images of the `vllm` and `llama-cpp` recipes on real hardware, and a
  discrete AMD card. The guide says so.
- ✅ 5. The repository is public, with secret scanning, push protection, Dependabot alerts and a
  ruleset protecting `main` and `dev`.

## Shipped: 1.2.0 (2026-11-11), cloud model APIs behind the same gateway

Researched on 2026-11-11 (LiteLLM, OpenRouter, Portkey, Envoy's AI gateway, Kong, GPUStack). "One
gateway for your GPUs and cloud APIs" alone is no longer new: **GPUStack 2.1 to 2.3 already has
cloud providers, weighted routes and fallback on status codes.** What Forgeyard 1.2 does that they
do not:

- **Spill when local is busy, not only when it is down.** A request goes to the cloud when every
  local replica is at its concurrency (or unavailable, or answers 429/503), only before the first
  byte, and waits for local again if the cloud cannot take it either.
- **Cost in US dollars, and budgets** per key, person and provider, reserved before a request is
  sent; an exhausted budget makes a route local-only. GPUStack meters tokens, not money.
- **Keys that cannot send data out unless allowed.** Every key is "never" by default (including
  every key from before 1.2), "only to spill" or "allowed"; cloud-only models do not exist for keys
  that may not use them; a route or a single request can be kept local.

Two pull requests (the second in two parts):

1. ✅ **Providers and tiered routing** (PR #16): OpenAI-compatible endpoints only (OpenAI,
   Anthropic, Gemini, Azure OpenAI, OpenRouter, any compatible server) with a quirks table, no LiteLLM;
   tiered routes; the spill rules; the per-key cloud setting; egress rules; usage per target.
2. **Cost and budgets** (PR B), in two parts:
   - ✅ B1, `cost` (PR #19): prices per provider model (entered by hand, or refreshed from
     OpenRouter's catalogue with a diff to accept), the cost of each request stored in micro-dollars,
     and **Usage → Cost**;
   - ✅ B2, `budgets` (PR #21): monthly budgets (UTC calendar month) per key, person and provider
     with reservation, alerts at 80% and 100%.

Later, if Claude users need it: an Anthropic `/v1/messages` passthrough, which keeps prompt caching
and thinking.

## In progress: 1.3, more kinds of models

Planned as one pull request per kind, each complete (recipe, estimate, gateway, usage, Playground,
docs, tests) and accepted on real hardware: ✅ means merged into `dev`.

1. ✅ **Multimodal chat** (#4): images in chat on vLLM and llama.cpp (projector files), video
   opt-in, the usage units for all of 1.3.
2. ✅ **llama.cpp b12242** (#9), with a built-in Vulkan variant for AMD APUs and the cards the ROCm
   image lacks.
3. ✅ **Text-to-speech** (#11, `/v1/audio/speech`): vLLM-Omni (Qwen3-TTS) on NVIDIA, llama.cpp
   TTS GGUFs on AMD and the CPU, speaches (Kokoro) for small English voices on CPU and CUDA; voices,
   streaming, characters and audio length in usage. Voice cloning stays off.
4. *In review*: **Image generation** (`/v1/images/generations`): vLLM-Omni (diffusers pipelines) on
   NVIDIA, stable-diffusion.cpp on AMD (Vulkan), NVIDIA (CUDA, a newer build after the first one
   crashed on an A100, #43) and the CPU; base64 only, caps and a queue per deployment, images,
   kilopixels and busy time in usage. Edits stay out.
5. *In review*: **Generic passthrough** (`/x/<name>/…`, #23): any service workload's HTTP API behind
   the gateway, through the agent's key guard (which strips the key), for keys whose new *Workloads*
   setting allows it (none by default, also for existing keys); APIs only (a forced CSP sandbox), no
   WebSockets or gRPC; bytes and busy time in usage. With ready-made workload templates on
   Forgeyard's own vision image: object detection (RT-DETRv2), segmentation (SAM 2.1), depth (Depth
   Anything V2 Small), Apache-2.0 only.
6. Video generation (async `/v1/videos`), the first to move to a later version if time is short.

Also in 1.3, alongside the model types:

- ✅ **Full regression run** on real machines, `scripts/accept_all.py` (#1), before every merge and
  release.
- ✅ **Setup health** (#3): checks for risky or useless settings, on Get started, in the top bar and
  on each page.
- ✅ **Container images on Docker Hub** (#2): the server and the agent, amd64 and arm64, published by
  a release tag.
- **Simpler settings** and **deployments that stay where they ran** moved to 1.3.1 (see *Next*).

The owner's decisions for 1.3 (2026-11-11):

- **Media URLs:** the gateway fetches `http(s)` media URLs itself through the 1.2 egress rules and
  sends data: URIs; engines never fetch (vLLM's hardening flags on). `file://` and other schemes are
  refused; private addresses only with `MEDIA_ALLOW_PRIVATE_HOSTS`. Caps per item, items per request
  and in total (the deployment's body limit).
- **PDF and other `file` parts:** no conversion by the gateway. Only recipes that say they take files
  get them; otherwise a clear 400.
- **Body limit per deployment** (`max_body_mb`), with a default that works.
- **Video in chat** is off by default, a per-deployment parameter; so is the number of images.
- **Usage columns** for all of 1.3 in one migration, one per unit, default 0.
- **No cloud for the new content types in 1.3:** requests with media are served locally only.
- **Text to speech:** three engines (vLLM-Omni Qwen3-TTS on NVIDIA, llama.cpp on AMD, speaches for
  English Kokoro and Piper voices, labelled English only). **Voice cloning is blocked in 1.3.**
- **Licences in the catalog**, and the recommended list holds only licences that allow commercial use.
- **Settings:** every new setting or parameter has a default that works untouched, sits in
  *Advanced* unless a normal user must decide it, and is derived from the model when it can be.

## Next

| Version | What you get |
|---|---|
| **1.3: more kinds of models** (in progress, above) | First-class support (a recipe, a memory estimate, an OpenAI-compatible API, keys, limits and usage) for: **multimodal chat**, i.e. images and video in chat on vLLM and llama.cpp (Qwen3-VL, Gemma and similar); **text-to-speech** (`/v1/audio/speech`); **image and video generation** (`/v1/images/generations`, async `/v1/videos`). Plus a **generic passthrough**: any workload's HTTP service behind the gateway at `/x/<name>/…`, guarded by API keys and limits and counted in usage, with templates for detection, segmentation and depth. That covers 3D, tabular and other tasks without a recipe each. |
| **1.3.1: change a deployment, see what it waits for** | **Edit a deployment** (GPU sharing, context, requests at once, the machine) and its replicas restart; a restarted deployment goes back to its previous machine first. **GPU sharing is pointed out:** chat models stay exclusive by default, but when a model needs half a card or less, the deploy page, a refusal ("fits if both share VRAM") and the setup advisor say that sharing would fit. **Progress for every download:** pulling an engine image shows how much has arrived, like a model download does; a pull fails only when it stops making progress, not after a fixed 30 minutes, and a cancelled pull says it was cancelled. A hint when a replica is never picked up by its machine. **Simpler settings:** one cloud-policy choice, one server-wide Hugging Face token, agent defaults that match the installer. |
| **1.3.2: dependency fix** | The web build's lint tools move off `minimatch` < 11.0.1. |
| **1.3.3: README** | The READMEs rewritten around what Forgeyard does, how to install it and a quick start; the developer layout moves to `docs/development.md`. |
| **1.4: easier to see** | **Charts:** usage and cost over time as line charts on **Usage**, split by key, model, provider or person, with the day, week and month view. **Machines and models, redesigned:** each machine shows what it is at a glance (an icon per kind: NVIDIA, AMD, APU, CPU), and its page lists the models, workloads and memory on it; models are grouped by kind (chat, multimodal, speech, images, embeddings, rerank) with an icon each; the other pages follow the same layout. **Download to…:** download a model to chosen machines before deploying it, with progress on the model's and each machine's page; each machine lists the models in its cache and their size, and can remove them. A machine that cannot reach the internet gets the model, and engine images, **relayed** by the server or by another machine that has them, checked by SHA-256, with progress too. **Clearer pages:** a model workload says which deployment created it and links to it; revoked keys leave the budget lists and alerts; the *Routes and cloud* and *Catalog and recipes* tabs are renamed after trying a few names and layouts. |
| **1.5: use GPUs only while needed** | **Scale to zero:** a deployment nobody calls stops and frees its GPU. The next request waits in a queue while it starts again. **Autoscaling:** replicas are added and removed by queue length or requests in flight. |
| **1.6: more kinds of work** | **Dev environments:** Jupyter or VS Code on a GPU machine, handed back when you are done. **OpenAI-compatible Batch API:** large jobs run at quiet times. |
| **1.7: teams** | Groups with quotas (GPU hours, tokens). Sign-in through SSO (OIDC, e.g. MCP Gate or Google) and two-factor authentication. |
| **1.8: running it for real** | Prometheus metrics and dashboards; alerts (machine offline, GPU errors and temperature, failing deployments, budgets at 80% and 100%, setup health errors) sent as **notifications to Discord, Slack, Telegram, LINE, email or any webhook**, chosen per event; scheduled backup and restore. |
| **1.9: fine-tuning** | A first-class **single-machine LoRA / QLoRA fine-tune**: pick a base model, upload a dataset, train with sensible defaults and a loss curve, then **deploy the adapter in one click** on vLLM (details below). |

Reinforcement learning, robotics and graph learning are not planned as model types: they are
training and research workloads, and run as plain tasks.

### 1.9: training

Fine-tuning already runs today as a one-off task in your own trainer container (Unsloth,
LLaMA-Factory, axolotl), with only the GPUs it was given. The direction under discussion is a
first-class **single-machine LoRA / QLoRA fine-tune** flow:

1. pick a base model from the catalog;
2. upload a dataset (checked on upload);
3. a recipe chooses the trainer and sensible defaults;
4. a training memory estimate places the job;
5. a loss curve and logs show progress;
6. the adapter is saved to the model cache and **deployed in one click** on vLLM with
   `--enable-lora`, so one card serves many fine-tuned variants.

Training holds whole GPUs for hours, so on shared machines it needs priorities against serving
(preemptible jobs, quiet-hours windows). That is why it comes after 1.5 (deployments that free
their GPUs) and 1.8 (alerts). The details are designed then; multi-machine distributed training is
much later.

### Larger platforms: Kubernetes and others (to be designed, after 1.9)

Forgeyard runs today on its own server with an agent per machine. For bigger installations:

- **The server on Kubernetes:** a Helm chart for the published server image, with PostgreSQL and
  the data volume, behind the cluster's ingress.
- **A cluster as a pool of machines:** instead of an agent on each machine, Forgeyard places replicas
  as pods on an existing Kubernetes cluster (GPUs through the NVIDIA or AMD device plugins), so a
  cluster that is already managed joins as it is, next to plain machines with agents.
- Other schedulers (Slurm, Nomad) are looked at in the same design.

How established projects do this (KServe, GPUStack, others) is researched before the design. The
version is not decided yet.

## Later, when there is a need

- **On-demand cloud GPUs.** A machine is rented when local capacity runs out and released when
  idle. The provider is chosen at that time.
- **Relay through the agent.** Machines behind NAT can serve without inbound ports.
- **Models larger than one machine.** vLLM with Ray across several machines.
- **Prefix-aware routing.** Requests that share a long prompt go to the replica that already has
  it cached.
- **Several servers.** For availability, with limits and counters shared between them.

## Small improvements, picked up along the way

- Download a model's shards in parallel, and support a Hugging Face mirror (on the test host,
  downloads ran at 3–4 MiB/s). Pre-downloading by hand (`hf download` into the agent's
  cache) works today: the agent reuses files it finds by their SHA-256.
- Per-process VRAM figures, instead of the change in whole-card usage.
- A `modelscope://` model source.
- Answer 404, as for an unknown model, when a key may not use a model, so a restricted key cannot
  tell which other models exist.
- Spread scoring for plain tasks (they still use first fit).
- Choose a recipe variant per deployment (today the first variant that matches a card wins; a
  custom recipe with only the wanted variant is the way around it).
- Vulkan on Intel and other GPUs: the agent's inventory reports NVIDIA and AMD cards only, so the
  `vulkan` variant of `llama-cpp` is for AMD.
- **Import an image** from a file, for machines that cannot reach a registry. ✅ (#10): an image carried over with `docker save` / `docker load` is checked against the
  ids recorded for the pinned image and used, so the built-in recipes work offline as they are, and
  each recipe lists the commands. Still to do: uploading an image file through the console to a
  machine.
- Tune each recipe's memory overhead from real runs (estimates were 7–39% above actual).

## Verification still to do

- **AMD ROCm on real hardware.** Done on a real Strix Halo (gfx1151, ROCm 7.0.2): the
  inventory through the `amdsmi` library, unified memory and the 96 GiB
  pool, joining, the GPU allowlist, and llama.cpp served through `/v1` with the built-in `vulkan` variant (b12242),
  its image loaded from a file (that machine cannot reach ghcr.io) and checked by its recorded ids.
  Still to confirm:
  - the ROCm images of the `vllm` and `llama-cpp` recipes (both list gfx1151 among their targets;
    `llama-cpp` sends APUs to Vulkan);
  - a discrete card;
  - `ROCR_VISIBLE_DEVICES` for a process workload.
- **The systemd scope for process limits.** It has not run on a host with systemd yet.
- **The one-line install on a systemd host, and the agent image on real GPUs.** Both ran in a
  container without systemd or Docker (the install with a stand-in `systemctl`, the image in CI on a
  runner without GPUs). On a real host, confirm the service (`KillMode=process`: workloads survive
  `systemctl restart forgeyard-agent`), `--gpus all` in the image, and AMD with ROCm mounted.
