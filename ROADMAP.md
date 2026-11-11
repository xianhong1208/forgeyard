# Roadmap

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

Where it stands (PR #22):

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
- 5 is done when the repository is made public.

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
2. *In review* (#9): llama.cpp bump to a current build (b12242), with a built-in Vulkan variant for AMD
   APUs and the cards the ROCm image lacks.
3. Text-to-speech (`/v1/audio/speech`): vLLM-Omni (Qwen3-TTS) on NVIDIA, llama.cpp on AMD, speaches
   for small English voices on CPU and CUDA. Voice cloning stays off.
4. Image generation (`/v1/images/generations`).
5. Generic passthrough (`/x/<name>/…`), with ready-made workload templates: object detection,
   segmentation, depth (permissively licensed images only).
6. Video generation (async `/v1/videos`), the first to move to a later version if time is short.

Also in 1.3, alongside the model types:

- ✅ **Full regression run** on real machines, `scripts/accept_all.py` (#1), before every merge and
  release.
- ✅ **Setup health** (#3): checks for risky or useless settings, on Get started, in the top bar and
  on each page.
- ✅ **Container images on Docker Hub** (#2): the server and the agent, amd64 and arm64, published by
  a release tag.
- **Simpler settings** (the owner's decisions on the settings audit): a provider's *outside your
  network* from its address; one Hugging Face token for the server; one cloud policy for keys;
  agent defaults like the installer's; deprecations of developer-only settings.
- **Deployments stay where they ran:** a restarted deployment goes back to its machine when it still
  fits, a deployment can be moved or pinned in the console, and a replica no agent has picked up says
  so (an agent too old for the server).

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
- **Licences in the catalog**, and the recommended list holds only licences that allow commercial use.
- **Settings:** every new setting or parameter has a default that works untouched, sits in
  *Advanced* unless a normal user must decide it, and is derived from the model when it can be.

## Next

| Version | What you get |
|---|---|
| **1.3: more kinds of models** (in progress, above) | First-class support (a recipe, a memory estimate, an OpenAI-compatible API, keys, limits and usage) for: **multimodal chat**, i.e. images and video in chat on vLLM and llama.cpp (Qwen3-VL, Gemma and similar); **text-to-speech** (`/v1/audio/speech`); **image and video generation** (`/v1/images/generations`, async `/v1/videos`). Plus a **generic passthrough**: any workload's HTTP service behind the gateway at `/x/<name>/…`, guarded by API keys and limits and counted in usage, with templates for detection, segmentation and depth. That covers 3D, tabular and other tasks without a recipe each. |
| **1.4: easier to see** | **Charts:** usage and cost over time as line charts on **Usage**, split by key, model, provider or person, with the day, week and month view. **Machines and models, redesigned:** each machine shows what it is at a glance (an icon per kind: NVIDIA, AMD, APU, CPU), and its page lists the models, workloads and memory on it; models are grouped by kind (chat, multimodal, speech, images, embeddings, rerank) with an icon each; the other pages follow the same layout. |
| **1.5: use GPUs only while needed** | **Scale to zero:** a deployment nobody calls stops and frees its GPU. The next request waits in a queue while it starts again. **Autoscaling:** replicas are added and removed by queue length or requests in flight. |
| **1.6: more kinds of work** | **Dev environments:** Jupyter or VS Code on a GPU machine, handed back when you are done. **OpenAI-compatible Batch API:** large jobs run at quiet times. |
| **1.7: teams** | Groups with quotas (GPU hours, tokens). Sign-in through SSO (OIDC, e.g. MCP Gate or Google) and two-factor authentication. |
| **1.8: running it for real** | Prometheus metrics and dashboards; alerts (machine offline, GPU errors and temperature, failing deployments, budgets at 80% and 100%, setup health errors) sent as **notifications to Discord, Slack, Telegram, LINE, email or any webhook**, chosen per event; scheduled backup and restore. |

Reinforcement learning, robotics and graph learning are not planned as model types: they are
training and research workloads, and run as plain tasks.

### Training: to be designed

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
(preemptible jobs, quiet-hours windows). Multi-machine distributed training is much later. The
version and the details are not decided yet.

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
  downloads ran at 3–4 MiB/s).
- Per-process VRAM figures, instead of the change in whole-card usage.
- A `modelscope://` model source.
- Answer 404, as for an unknown model, when a key may not use a model, so a restricted key cannot
  tell which other models exist.
- Spread scoring for plain tasks (they still use first fit).
- Choose a recipe variant per deployment (today the first variant that matches a card wins; a
  custom recipe with only the wanted variant is the way around it).
- Vulkan on Intel and other GPUs: the agent's inventory reports NVIDIA and AMD cards only, so the
  `vulkan` variant of `llama-cpp` is for AMD.
- **Import an image** from a file, for machines that cannot reach a registry (seen on a machine
  whose DNS blocked ghcr.io and Hugging Face). `docker load` on Docker's classic image store keeps an
  image's tag but not its registry digest, so a recipe image pinned as `tag@sha256:…` is not found
  there and Docker tries to pull it; the agent could check a loaded tag against the pinned image's
  config digest instead.
- Tune each recipe's memory overhead from real runs (estimates were 7–39% above actual).

## Verification still to do

- **AMD ROCm on real hardware.** Done on a real Strix Halo (gfx1151, ROCm 7.0.2): the
  inventory through the `amdsmi` library, unified memory and the 96 GiB
  pool, joining, the GPU allowlist, and a llama.cpp Vulkan deployment served through `/v1`. Still to
  confirm:
  - the built-in `vulkan` variant of `llama-cpp` (b12242) on the Strix Halo;
  - the ROCm images of the `vllm` and `llama-cpp` recipes (both list gfx1151 among their targets;
    `llama-cpp` sends APUs to Vulkan);
  - a discrete card;
  - `ROCR_VISIBLE_DEVICES` for a process workload.
- **The systemd scope for process limits.** It has not run on a host with systemd yet.
- **The one-line install on a systemd host, and the agent image on real GPUs.** Both ran in a
  container without systemd or Docker (the install with a stand-in `systemctl`, the image in CI on a
  runner without GPUs). On a real host, confirm the service (`KillMode=process`: workloads survive
  `systemctl restart forgeyard-agent`), `--gpus all` in the image, and AMD with ROCm mounted.
