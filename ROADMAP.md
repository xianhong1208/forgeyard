# Roadmap

[繁體中文](ROADMAP.zh-TW.md)

Where Forgeyard is going after 1.0. The order was agreed on 2026-11-10 and revised on 2026-11-19 after an outside review; each line names what
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

## Shipped: 1.3.0 (2026-11-14), more kinds of models

One pull request (or a few) per kind, each complete (recipe, estimate, gateway, usage, Playground,
docs, tests). Each was accepted on real hardware.

1. ✅ **Multimodal chat** (#4): images in chat on vLLM and llama.cpp (projector files), video
   opt-in, the usage units for all of 1.3.
2. ✅ **llama.cpp b12242** (#9), with a built-in Vulkan variant for AMD APUs and the cards the ROCm
   image lacks.
3. ✅ **Text-to-speech** (#11, `/v1/audio/speech`): vLLM-Omni (Qwen3-TTS) on NVIDIA, llama.cpp
   TTS GGUFs on AMD and the CPU, speaches (Kokoro) for small English voices on CPU and CUDA; voices,
   streaming, characters and audio length in usage. Voice cloning stays off.
4. ✅ **Image generation** (#13, #44, `/v1/images/generations`): vLLM-Omni (diffusers pipelines) on
   NVIDIA, stable-diffusion.cpp on AMD (Vulkan), NVIDIA (CUDA, a newer build after the first one
   crashed on an A100, #43) and the CPU; base64 only, caps and a queue per deployment, images,
   kilopixels and busy time in usage. Edits stay out.
5. ✅ **Generic passthrough** (`/x/<name>/…`, #45): any service workload's HTTP API behind
   the gateway, through the agent's key guard (which strips the key), for keys whose new *Workloads*
   setting allows it (none by default, also for existing keys); APIs only (a forced CSP sandbox), no
   WebSockets or gRPC; bytes and busy time in usage. With ready-made workload templates (#46) on
   Forgeyard's own vision image: object detection (RT-DETRv2), segmentation (SAM 2.1), depth (Depth
   Anything V2 Small), Apache-2.0 only, and a **Vision** tab in the Playground to try them (#49): boxes
   with a score slider, a mask from a click or a box, a depth map beside the photo.
6. ✅ **Video generation** (async `/v1/videos`, #50): OpenAI's Videos API as jobs on
   vLLM-Omni (Wan2.1-T2V-1.3B, Wan2.2-TI2V-5B) on NVIDIA, each job kept on the replica that makes it
   (a job whose replica restarts or goes reports failed), results that expire, caps on length, size and
   jobs per deployment and per key, video length and GPU time in usage, and a **Video** panel in the
   Playground. Text to video only; image-to-video and video on AMD and the CPU (stable-diffusion.cpp)
   come later.

Also in 1.3.0, alongside the model types:

- ✅ **Full regression run** on real machines, `scripts/accept_all.py` (#1), before every merge and
  release.
- ✅ **Setup health** (#3): checks for risky or useless settings, on Get started, in the top bar and
  on each page.
- ✅ **Container images on Docker Hub** (#2, #12): the server and the agent, amd64 and arm64,
  published by a release tag.
- **Simpler settings** and **deployments that stay where they ran** moved to 1.3.1 (shipped, below).

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

## Shipped: 1.3.1 (2026-11-15), change a deployment, see what it waits for

1. ✅ **Change a deployment** (#55, for #26, #27 and #29): **Edit** a deployment's GPU sharing,
   context, requests at once or machine, and its replicas restart; a restarted replica goes back to
   the machine it ran on first. **GPU sharing is pointed out:** chat models stay exclusive by
   default, but when a model needs half a card or less, the deploy page, a refusal ("fits if both
   share VRAM") and Setup health say that sharing would fit. A replica that its machine never picks
   up says so, with the agent's version and what to do.
2. ✅ **Progress for every download** (#57, for #28): pulling an engine image shows how much has
   arrived, like a model download; a pull fails only when it stops making progress
   (`pull_stall_minutes`), not after a fixed 30 minutes; removing a deployment stops its pull or
   download at once, and the replica says it was cancelled.
3. ✅ **Simpler settings** (#58, for #30): one cloud-policy choice per key, one server-wide Hugging
   Face token, a provider's *Outside your network* ticked from its kind and address, agent defaults
   that match the installer, and the settings removed in 1.4 deprecated with a warning.
4. ✅ **llama.cpp 1.0.0** (#59, for #48), its first release with a semantic version, in every variant
   of the `llama-cpp` recipe, with an optional FP8 KV cache (half the KV memory; not on Vulkan).
   Accepted on the CPU, an NVIDIA A100 (CUDA) and AMD Strix Halo (Vulkan, offline).

## Shipped: 1.3.2 (2026-11-15), a dependency fix

1. ✅ **`minimatch` ≥ 11.0.1** (#63, for #31): the web build's lint tools no longer use `minimatch` < 11.0.1
   (eslint 10.15, and an npm override for typescript-eslint until it moves).

## Shipped: 1.3.3 (2026-11-15), README

1. ✅ **The READMEs rewritten** (#67, for #32): what Forgeyard does, how to install it and a quick start;
   the developer layout moved to `docs/development.md`.
2. ✅ **Python examples and console snippets for the current OpenAI SDK** (#67, #68): `DefaultHttpxClient`,
   since openai 3.51 no longer installs httpx.
3. ✅ **Tests fail fast instead of hanging CI** (#69).

## Shipped: 1.4.0 (2026-11-17), easier to see

1. ✅ **Usage and cost charts** (#80, for #33): a line chart on both Usage tabs, per day, week or month,
   in total or split by key, model, provider or person. Usage's tables list their top rows, sort by any
   column and keep one line per row on a phone (#84, for #82).
2. ✅ **Machines and models, redesigned** (#75, for #34): a tile per machine with a mark for its kind
   (NVIDIA, AMD GPU, AMD APU, CPU only), its cards and what runs there; a page per machine with a memory
   map of every card; running models grouped by kind, each with its icon and its `/v1` path.
3. ✅ **One thing in one place** (#85, #93, #101, for #81, #92 and #100): **Serving** for the running
   models; **Models** as the library, with Catalog, Engines (custom engines added, edited and deleted in
   the console) and Sources; providers and cloud models on **Cloud**; a deployment's cloud fallback on
   its own page; one model picker in the Playground. The *Routes and cloud* tab is gone.
4. ✅ **Vision models under Models** (#90, #91, for #86): object detection, segmentation and depth from
   Hugging Face (permissive licences only), run by a built-in `vision` engine and called at
   `/v1/vision/detect`, `/segment` and `/depth` with API keys and usage. Accepted on an NVIDIA A100 and
   on an AMD APU machine's CPU.
5. ✅ **Download to…, with relay for offline machines** (#83, #87, #96, #102, #107, for #35): each machine
   lists the models in its cache with their sizes and removes them; a model and its engine are
   downloaded to chosen machines before deploying, with progress; a machine that cannot reach the source
   gets model files, engine archives and pinned engine images **through the server**, which stores
   nothing (per machine: *Auto*, *Always through the server* or *Direct only*), checked by SHA-256 and
   image digests; an admin uploads an image file to a machine from its page.
6. ✅ **Placement knows what a machine can fetch** (#71, for #56): a machine that could not reach a
   replica's sources comes last, a replica stuck on such a download moves to one that can, and the
   deployment's page says why a replica is where it is.
7. ✅ **Clearer pages** (#71, #75, #106, for #36 and #105): a model's workload is seen only through its
   deployment; revoked keys leave the budget lists and alerts, and the API keys page folds them away.
8. ✅ **Apps leave the console** (#94, for #88): Forgeyard is about running models. The apps API, the
   templates and `/x/` are deprecated and say so; they are removed in 1.5 (#89).
9. ✅ **The settings deprecated in 1.3.1 are no longer read** (#79, for #78).

Also in 1.4.0: tests that wait for conditions instead of fixed times (#76, #77, #97, #103), a key guard
that closes in under a second (#98), and `agent init` writing a valid `agent.toml` whatever a tool's
message holds (#99). **Relay from another machine** that already has the files moved to 1.4.1 (#104).

## Shipped: 1.4.1 (2026-11-17), relay from another machine

1. ✅ **A model file from another machine** (#116, for #104): a machine that gets its files through the
   server gets a model file from another machine that already has it in its cache, still through the
   server (no machine opens a port), before the internet. The file is checked against the digest the
   server holds, and a machine whose copy does not match is skipped for an hour. Progress and
   **Download to…** name the machine that sends it. Accepted with model files sent from another machine
   at about 31 MiB/s through an SSH tunnel. Engine archives keep coming from the internet through the
   server.
2. ✅ **Copy an engine image from another machine** (#117, for #104): on a machine's page an admin copies
   an image from another machine's engine, through the server, which keeps nothing; never automatic.
   Accepted with an engine image copied from one machine to another, loaded with the same content id.
3. ✅ **The vision image pinned by digest, like every other engine** (#115, for #113): a loaded copy is
   checked by its ids, a machine without registry access gets it through the server, and a server
   upgrade alone no longer changes the image vision models run. A Docker Hub image named in full
   (`docker.io/<org>/<name>`) can now be relayed and pinned (`split_image`). Accepted with a machine
   without registry access fetching the pinned vision image through the server.

Also in 1.4.1: test IDs unique across the project (#114), and the server accepts up to 32 features in
an agent's hello. Files sent straight from machine to machine stay in *Later, when there is a need*.

## Shipped: 1.4.2 (2026-11-18), running 1.4 on real machines

1. ✅ **Update an agent with the exact command** (#135, for #121 and #122): an agent reports how it runs
   (its container, install.sh or a checkout), and its machine's page shows the command that brings it to
   the server's version, with the `docker save | ssh | docker load` step for a machine without internet.
   It never reports a token or a secret. An agent behind the server says so on the Machines tiles, on its
   page and in **Setup health**, and opens that command.
2. ✅ **Engine images on a machine's page** (#134, for #123 and #124): each image a recipe names, with its
   size and what uses it, removable by an admin when nothing on the machine uses it (never the agent's own
   image, never one a container uses). **Copy from another machine** lists engine images first.
3. ✅ **Placement counts free host ports** (#133, for #125): the agent reports its port range, and a machine
   without a free port is skipped like one without memory, with the reason in the plan, instead of a
   replica restarting in backoff there.
4. ✅ **A backup of the secret key from the console** (#132, for #127): an admin downloads the secret key
   and Forgeyard's own CA as one file encrypted with a passphrase of their own, and
   `python -m forgeyard secrets restore` puts them back.
5. ✅ **A wrong Docker Hub secret shows up before a release** (#137, for #128): a *Docker Hub login*
   workflow on every push to `dev` and `main` checks that the token may push the three images.
6. ✅ **A machine without internet access, in one place in the guide** (#138, for #129): joining it, its
   internet access, models, engine images, updating its agent and troubleshooting.

Also in 1.4.2: a relay whose source breaks off mid-file reads as one clear line in the server's log
(#136, for #130), and `scripts/accept_all.py` checks relaying, image upload and image copy (#139, for #126).

## Shipped: 1.4.3 (2026-11-18), a page per model and per engine

1. ✅ **A page per catalog model** (#151, for #140): `/models/<name>` shows the model's source, revision,
   licence, task and architecture, then the machines (each one's model cache entry, a download under way,
   whether a copy fits there now on a GPU or the CPU, and whether the engine's image or program is there),
   its deployments, the engines that can run it and its files, with **Deploy**, **Download to…** and
   **Remove**. An admin sets its display name; the name stays, since deployments record it, and nine names
   that are other pages under `/models` are reserved.
2. ✅ **A page per engine** (#151, for #140): `/models/engines/<name>` shows its variants (image or program,
   pinned by digest or by tag, what a machine needs, the offline commands), which machines have them, its
   parameters, and the deployments and catalog models that use it. The server keeps each machine's last image
   listing, so the page answers for machines that are offline too. Model and engine names link to these pages
   everywhere in the console.
3. ✅ **Models that would fail are refused when added** (#150, for #147): zero-shot (open-vocabulary)
   detectors are no longer taken as detection models, and sequence classifiers with more than one label are
   no longer filed as rerankers. Both come with 1.4.4.

## Next

| Version | What you get |
|---|---|
| **1.4.4: more kinds of models, part 1** | **Zero-shot object detection** (#142): `/v1/vision/detect` takes an optional `labels` list and finds the objects named in it, with the same answer, on Forgeyard's vision image (Grounding DINO, OWLv2, MM-Grounding-DINO). **Text classification** (#144): small classifier models on vLLM's pooling engine at `/v1/classify`, on a GPU. **Image classification** (#141): image classification and zero-shot image classification (CLIP and SigLIP 2) at `/v1/vision/classify` on the vision image. Permissive licences only. |
| **1.4.5: fixes from an outside review** | **Structured output is never silently lost:** a request with a JSON schema, grammar or guided-decoding parameter is not spilled to a cloud tier that would drop it, and any parameter dropped on the way to a cloud provider is named in an `X-Forgeyard-Dropped-Params` header. **Encrypted traffic to the machines:** the gateway talks to each replica over TLS, with a certificate the agent gets from Forgeyard's own CA, so prompts and answers no longer cross the network in plain HTTP. **Replicas move off a machine that stays offline:** after a grace period they are placed on another machine that fits. **Prometheus metrics** at `/metrics`. Smaller fixes: an agent that reports a vendor the server does not know is still accepted; `local_busy` answers carry `Retry-After`; cloud spend from answers without token counts survives a restart; the installer pins the uv it downloads by hash. **Behind the scenes:** the two largest files are split before more is built on them: the gateway's `proxy.py` (about 1,700 lines) into routing, spill, cloud and streaming parts, and the agent's `supervisor.py` (about 1,150 lines) into smaller pieces, with an explicit interface for container and process runtimes. Behaviour does not change; the existing tests and the full regression run must pass unchanged. |
| **1.5: use GPUs only while needed** | **Scale to zero:** a deployment nobody calls stops and frees its GPU. The next request waits in a queue while it starts again. **Autoscaling:** replicas are added and removed by queue length or requests in flight. **Engine updates:** a newer version of an engine is pointed out, and can be tried as a pinned copy. **Apps removed:** the apps API, the templates and `/x/`, deprecated in 1.4. **More kinds of models, part 2:** image embeddings (#141), image-to-image and image-to-video (#143), and text classification on the CPU with transformers, including zero-shot classification with NLI models (#144). |
| **1.6: teams and operations** | Groups with quotas (GPU hours, tokens). Sign-in through SSO (OIDC, e.g. MCP Gate or Google) and two-factor authentication. **A request log with metadata only** (time, key, model, where it was served, tokens; never the content), so you can tell which key sent what where. The audit log can be sent out (syslog, OpenTelemetry). Alerts (machine offline, GPU errors and temperature, failing deployments, budgets at 80% and 100%, setup health errors) sent as **notifications to Discord, Slack, Telegram, LINE, email or any webhook**, chosen per event; scheduled backup and restore. |
| **1.7: more kinds of work** | **Dev environments:** Jupyter or VS Code on a GPU machine, handed back when you are done. **OpenAI-compatible Batch API:** large jobs run at quiet times. |
| **1.8: fine-tuning** | A first-class **single-machine LoRA / QLoRA fine-tune**: pick a base model, upload a dataset, train with sensible defaults and a loss curve, then **deploy the adapter in one click** on vLLM (details below). |

Reinforcement learning, robotics and graph learning are not planned as model types: they are
training and research workloads, and run as plain tasks.

### 1.8: training

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
their GPUs) and 1.6 (alerts). The details are designed then; multi-machine distributed training is
much later.

## Later, when there is a need

- **On-demand cloud GPUs.** A machine is rented when local capacity runs out and released when
  idle. The provider is chosen at that time.
- **Relay through the agent.** Machines behind NAT can serve without inbound ports.
- **Files straight from machine to machine.** Model files sent from one agent to another directly, or
  directly when the machines can reach each other and through the server otherwise, instead of always
  through the server (1.4.1).
- **Models larger than one machine.** vLLM with Ray across several machines.
- **Prefix-aware routing.** Requests that share a long prompt go to the replica that already has
  it cached.
- **Several servers.** For availability, with limits and counters shared between them.

### Larger platforms: Kubernetes and others (when there is a need)

Forgeyard runs today on its own server with an agent per machine. For bigger installations:

- **The server on Kubernetes:** a Helm chart for the published server image, with PostgreSQL and
  the data volume, behind the cluster's ingress.
- **A cluster as a pool of machines:** instead of an agent on each machine, Forgeyard places replicas
  as pods on an existing Kubernetes cluster (GPUs through the NVIDIA or AMD device plugins), so a
  cluster that is already managed joins as it is, next to plain machines with agents.
- Other schedulers (Slurm, Nomad) are looked at in the same design.

How established projects do this (KServe, GPUStack, others) is researched before the design. The
version is not decided yet.

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
  each recipe lists the commands. Since 1.4 (#102) an admin can also upload an image file to a
  machine from its page.
- Tune each recipe's memory overhead from real runs (estimates were 7–39% above actual).

## Verification still to do

- **AMD ROCm on real hardware.** Done on a real Strix Halo (gfx1151, ROCm 7.0.2): the
  inventory through the `amdsmi` library, unified memory and the 96 GiB
  pool, joining, the GPU allowlist, and llama.cpp served through `/v1` with the built-in `vulkan` variant (b12242, then llama.cpp 1.0.0),
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
- **Wan2.2-TI2V-5B video** (`vllm-omni-video`, about 33 GiB) on an NVIDIA GPU. Wan2.1-T2V-1.3B passed on
  an A100: 25 checks, 832x480 for 5 s in 136 s, 20.75 GiB measured against an estimate of 21.62 GiB.
