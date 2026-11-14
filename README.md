# Forgeyard

One control plane for the GPUs (and CPUs) you own: NVIDIA, AMD ROCm and CPU-only machines on one
LAN or VPN. An agent on each machine reports its hardware; you run services and tasks on them, and
deploy models from recipes behind one OpenAI-compatible gateway at `/v1`, with API keys, limits
and usage statistics. Vision models read images in chat, fetched by the gateway, never by the engine. Speech models
turn text into speech at `/v1/audio/speech` (Qwen3-TTS in Chinese and English, small English voices on a CPU). Image
models make images at `/v1/images/generations` (vLLM-Omni on NVIDIA, stable-diffusion.cpp on AMD and CPUs). Any other
service (detection, segmentation, a Triton or FastAPI model) can be exposed at `/x/<name>/…` behind the same keys, limits
and usage. Cloud model APIs can sit behind the same gateway, local first, with cost
tracking and monthly budgets.

[繁體中文說明](README.zh-TW.md)

> **Status: 1.2.0.** 1.0.0 was the MVP (milestones M0 to M4, each accepted on real machines). 1.1
> added a first-run checklist and recommended models, HTTPS out of the box, a one-line agent install,
> an agent container image and this guide. 1.2 puts cloud providers behind the same gateway, local
> first: a model spills to OpenAI, Anthropic, Gemini, Azure or OpenRouter when its replicas are busy
> or down, only for keys allowed to send data out; each cloud request's cost is counted in US dollars,
> and monthly budgets per key, person and provider are reserved before a request is sent. 1.3 (in
> progress) adds more kinds of models; first, images in chat on vLLM and llama.cpp (Qwen3-VL and
> similar), with the catalog showing each model's licence, then llama.cpp b12242 with a built-in
> Vulkan variant for AMD APUs, then text to speech with three engines (vLLM-Omni Qwen3-TTS, llama.cpp
> TTS GGUFs, speaches Kokoro), then image generation (vLLM-Omni, stable-diffusion.cpp), then the generic
> passthrough: any service workload's HTTP API behind the gateway at `/x/<name>`. See [CHANGELOG.md](CHANGELOG.md) and
> [ROADMAP.md](ROADMAP.md).

## The guide

Everything you need to set it up and use it is in the **[guide](docs/guide/README.md)**
([繁體中文](docs/guide/zh-TW/README.md)), also served by Forgeyard itself at `/guide`:

| | |
|---|---|
| [Quick start](docs/guide/quick-start.md) | from nothing to your first `/v1` call in about ten minutes |
| [Concepts](docs/guide/concepts.md) | machines and agents, GPU access, workloads, models, deployments and the gateway, keys, roles |
| [How-to](docs/guide/how-to.md) | AMD and CPU-only machines, the server and the agent in Docker, shared GPUs, your own certificate, upgrades, backups |
| [Cloud providers](docs/guide/cloud-providers.md) | OpenAI, Anthropic, Gemini, Azure, OpenRouter or your own server behind `/v1`; when a request spills; which keys may use the cloud |
| [Troubleshooting](docs/guide/troubleshooting.md) | problems seen on real machines, with their fix |
| [Reference](docs/guide/reference.md) | every server and agent setting, the command line, files and ports |

How each part works, in detail: [docs/design.md](docs/design.md). HTTPS, trust and the encryption of
stored secrets: [docs/https-and-trust.md](docs/https-and-trust.md).

## In short

The server (Python 3.13 with [uv](https://docs.astral.sh/uv/); Node 22.12+ to build the UI):

```bash
cd web && npm ci && npm run build && cd ..
uv sync
HOST=0.0.0.0 PUBLIC_URL=https://192.168.1.20:7860 uv run python main.py
```

It speaks HTTPS from its own certificate authority, prints a one-time setup token for `/setup`,
and keeps everything in `data/` (back up the database, `data/secret.key` and `data/tls/`).
Admins see what needs attention in the setup under **Get started → Setup health**: misspelled
settings, keys that may spend on the cloud without a budget, shared GPUs nobody chose, outdated
agents and more, each with a link to the fix.

Or with Docker, two commands and nothing to build (the images are on Docker Hub for linux/amd64
and linux/arm64):

```bash
sudo docker run -d --name forgeyard --restart always --hostname forgeyard -p 7860:7860 \
  -v forgeyard-data:/data -e PUBLIC_URL=https://192.168.1.20:7860 docker.io/tonyhuang1101/forgeyard:1.2.0
sudo docker logs forgeyard      # the one-time setup token
```

The image keeps everything in the `forgeyard-data` volume; backups, upgrades by tag and pinning a
digest: [Run the server in Docker](docs/guide/how-to.md#run-the-server-in-docker).

A machine: **Machines → Add a machine** gives a one-line install that checks the server's CA
against its fingerprint, then installs the agent of the server's own version as a systemd service:

```bash
curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem && \
  sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \
  echo "3f9a…  forgeyard-ca.der" | sha256sum -c && \
  curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | \
  sudo sh -s -- --join fyj_… --ca-fingerprint sha256:3f9a…
```

Or one `docker run` of the agent image, `docker.io/tonyhuang1101/forgeyard-agent` at the server's
version (the **Docker** tab writes the command), or a checkout with `/etc/forgeyard/agent.toml`. Then deploy a model on **Models** and call it at
`https://192.168.1.20:7860/v1` with an API key and any OpenAI client.

**Upgrading from 1.0:** `uv sync`, restart the server, then run the one-time `agent trust`
command on every machine that joined over plain HTTP; see
[docs/https-and-trust.md](docs/https-and-trust.md#upgrading-from-10-agents-that-joined-over-plain-http).

## Tested on

Every release is checked on real machines, not only in CI. So far:

| Hardware | Software | What ran |
|---|---|---|
| NVIDIA A100 80GB PCIe (an 8-GPU host, with a GPU allowlist) | driver 580, CUDA 13.0, Docker | joining and the GPU allowlist; services and tasks as processes and containers, each seeing only its card; vLLM 0.32 (Qwen3-4B chat, Qwen3 embedding and reranker, images and video in chat with Qwen3-VL-2B, audio in chat with Gemma 4 12B), speaches 0.8.3 (faster-whisper), vLLM-Omni (Qwen3-TTS text to speech, Stable Diffusion XL image generation); the gateway with the official OpenAI SDK; the agent from its container image |
| AMD Ryzen AI MAX+ 395 with Radeon 8060S (Strix Halo, gfx1151 APU, 96 GiB carve-out) | Ubuntu 24.04, ROCm 7.0.2, amd-smi 26.0.2 | the inventory through the `amdsmi` library (unified memory), joining, and Qwen3-1.7B GGUF on llama.cpp's Vulkan build served through `/v1`, on a machine that could reach neither Hugging Face nor `ghcr.io`; on the same build, images, audio and video in chat (Qwen3-VL-2B and Qwen2.5-Omni-3B GGUF) and text to speech (Qwen3-TTS 1.7B and Chatterbox Turbo GGUF); image generation with stable-diffusion.cpp's Vulkan build (Z-Image-Turbo GGUF), its image loaded from a file |
| x86-64 without a GPU | Linux | llama.cpp (Qwen3-1.7B GGUF) on two agents at once: balancing, failover when one engine dies; images, audio and video in chat with llama.cpp (Qwen3-VL-2B and Qwen2.5-Omni-3B GGUF); text to speech with llama.cpp b12242 (Qwen3-TTS 1.7B GGUF in Chinese and English, Chatterbox Turbo) and speaches (Kokoro 82M), the speech checked by faster-whisper; image generation with stable-diffusion.cpp as a process (Stable Diffusion 1.5 and Z-Image-Turbo GGUF) |
| Google Gemini (`gemini-2.5-flash`) | its OpenAI-compatible endpoint | spilling when a request is too long for the local model, streaming, cost (thinking tokens included) and budgets |

Not checked on real hardware yet: the ROCm images of the `vllm` and `llama-cpp` recipes, discrete AMD
cards, and the one-line install on a host
with systemd. Image generation on NVIDIA is checked with vLLM-Omni (Stable Diffusion XL and Z-Image-Turbo on the A100);
stable-diffusion.cpp offers no CUDA image there ([#43](https://github.com/xianhong1208/forgeyard/issues/43)). Reports from other machines are welcome (see
[CONTRIBUTING.md](CONTRIBUTING.md)).

### Models tested

| Model | Engine (recipe) | Hardware |
|---|---|---|
| **Chat** | | |
| Qwen3-4B | vLLM 0.32 (`vllm`) | NVIDIA A100 |
| Qwen3-1.7B GGUF | llama.cpp (`llama-cpp`) | CPU; AMD Strix Halo (Vulkan) |
| **Multimodal chat** | | |
| Qwen3-VL-2B Instruct: image, video | vLLM (`vllm`) | NVIDIA A100 |
| Gemma 4 12B: audio | vLLM (`vllm`) | NVIDIA A100 |
| Qwen3-VL-2B GGUF: image, video | llama.cpp (`llama-cpp`) | CPU; AMD Strix Halo (Vulkan) |
| Qwen2.5-Omni-3B GGUF: image, audio, video | llama.cpp (`llama-cpp`) | CPU; AMD Strix Halo (Vulkan) |
| **Embeddings and rerank** | | |
| Qwen3-Embedding-0.6B, Qwen3-Reranker-0.6B | vLLM pooling (`vllm-pooling`) | NVIDIA A100 |
| **Speech to text** | | |
| faster-whisper small | speaches 0.8.3 (`faster-whisper`) | NVIDIA A100 |
| **Text to speech** | | |
| Qwen3-TTS 12Hz 0.6B and 1.7B CustomVoice | vLLM-Omni (`vllm-omni-tts`) | NVIDIA A100 |
| Qwen3-TTS 12Hz 1.7B GGUF, Chatterbox Turbo GGUF | llama.cpp (`llama-cpp`) | CPU; AMD Strix Halo (Vulkan) |
| Kokoro 82M | speaches (`speaches-tts`) | CPU |
| **Image generation** | | |
| Stable Diffusion XL base 1.0, Z-Image-Turbo | vLLM-Omni (`vllm-omni-image`) | NVIDIA A100 |
| Stable Diffusion 1.5 GGUF | stable-diffusion.cpp (`sd-cpp`, CPU process) | CPU |
| Z-Image-Turbo GGUF | stable-diffusion.cpp (`sd-cpp`) | CPU (process); AMD Strix Halo (Vulkan) |
| **Cloud** | | |
| Google `gemini-2.5-flash` | its OpenAI-compatible endpoint (a provider) | — |

## Develop

```bash
uv sync
uv run --no-sync python -m pytest -q          # backend tests (SQLite)
uv run --no-sync ruff check .
TEST_DATABASE_URL=postgresql+psycopg://user:pw@host/db uv run --no-sync python -m pytest -q
                                               # same suite on PostgreSQL — WIPES that database
cd web
npm run dev                                   # UI on :5173, API proxied to https://127.0.0.1:7860
npm run lint && npm run typecheck && npm test && npm run build
cd ..
shellcheck -s sh forgeyard/install/install.sh  # the install script stays POSIX sh
docker build -f Dockerfile.agent -t forgeyard-agent:dev . && scripts/agent_image_smoke.sh forgeyard-agent:dev
docker build -f Dockerfile.server -t forgeyard:dev . && scripts/server_image_smoke.sh forgeyard:dev
```

Schema changes go through Alembic (`forgeyard/migrations`); startup runs `upgrade head`, and a
test fails when the models and migrations drift apart. Test IDs (`FY-…`) are unique across the
project.

## Layout

| Path | What |
|---|---|
| `forgeyard/app.py` | FastAPI app: guards, routes, background jobs (usage, alerts, certificate renewal), the built UI |
| `forgeyard/cli.py` | `python -m forgeyard [serve]`, `secrets reset` and `forgeyard agent …` |
| `forgeyard/config.py` | every server setting (environment and `.env`) |
| `forgeyard/db.py`, `forgeyard/migrations/` | the database models; Alembic migrations, run on start |
| `forgeyard/api/` | the HTTP API: `auth` (setup, sign-in, sessions, links), `users`, `audit_log`, `machines`, `workloads`, `onboarding` (Get started), `tls` (the CA download), `install` (`/install.sh` and the wheel), `guide` |
| `forgeyard/api/models.py`, `deployments.py` | catalog, recipes, sources; deployments, estimate, try it |
| `forgeyard/api/gateway.py`, `keys.py` | `/v1/*` and the playground; API keys, their limits, cloud setting and budget, and usage |
| `forgeyard/api/providers.py` | cloud providers and their models: add, test, import, prices by hand, clone |
| `forgeyard/api/routing.py` | routes and their tiers of targets |
| `forgeyard/api/prices.py` | the price refresh from OpenRouter: preview and apply |
| `forgeyard/api/budgets.py` | monthly budgets of people and providers, and this month's spend |
| `forgeyard/gateway/proxy.py` | forwarding a request through a route's tiers: replicas, providers, spill, streaming |
| `forgeyard/gateway/keys.py`, `limits.py`, `balancer.py` | API key checks and cloud settings; requests and tokens per minute; replica order and provider cooldowns |
| `forgeyard/gateway/quirks.py` | what differs per provider kind: how the key is sent, parameters dropped or renamed, how usage is read back |
| `forgeyard/gateway/egress.py` | the HTTP client for providers: TLS, no redirects, the address checks, `EGRESS_*` |
| `forgeyard/gateway/pricing.py` | a request's cost from its usage and prices; OpenRouter's list and price matching |
| `forgeyard/gateway/budgets.py` | the budget ledger: reserve, settle, release, the 80% and 100% alerts, 402 |
| `forgeyard/gateway/media.py` | images, audio and video in chat: parts rebuilt, media URLs fetched under the egress rules |
| `forgeyard/gateway/speech.py` | text to speech: request checks, voice cloning refused, what each engine is given, the audio's length |
| `forgeyard/gateway/imagegen.py` | image generation: request checks, per-deployment caps, what each engine is given, the queue, the answer and its size |
| `forgeyard/gateway/passthrough.py` | the generic passthrough at `/x/<name>`: paths refused, headers dropped and forced, the answer as raw bytes, limits, usage |
| `forgeyard/gateway/usage.py`, `errors.py` | usage counted in memory and written to the database; errors in OpenAI's shape |
| `forgeyard/machines/` | agent protocol, join and stream endpoints, live connections, offline detection |
| `forgeyard/workloads/` | the workload spec, placement and the GPU allowlist, desired state and status |
| `forgeyard/models/` | model sources, the SSRF-guarded fetcher, inspection (safetensors, GGUF, what a model takes besides text, licence), estimates, recipes, the scheduler, routes, the recommended list |
| `forgeyard/recipes/` | the built-in engine recipes (YAML), images pinned by digest |
| `forgeyard/security/` | passwords, sessions, rate limits, host/CSRF middleware, HTTPS and the CA, secrets at rest, `secrets reset` |
| `forgeyard/agent/` | the agent: inventory collectors (NVML, AMD SMI, CPU), connection, state, trust (`agent trust`) |
| `forgeyard/agent/config.py`, `setup.py` | the agent's settings (`agent.toml`, `FORGEYARD_*`, flags) and `agent init` |
| `forgeyard/agent/models.py` | the agent's model cache: downloads, verification, engines |
| `forgeyard/agent/workloads/` | the agent's reconciler, process and container runtimes, the replicas' key guard, logs, health checks |
| `forgeyard/install/` | the agent's wheel built from the running code, `install.sh`, the commands on the Machines page, the published images' names (`images.py`), the server image's health check |
| `forgeyard/guide.py`, `docs/guide/` | the user guide (en, zh-TW), served at `/guide` |
| `docs/design.md`, `docs/https-and-trust.md` | how each part works; HTTPS, trust and secrets |
| `Dockerfile.agent`, `scripts/agent_image_smoke.sh` | the agent's container image and its CI smoke test |
| `Dockerfile.server`, `scripts/server_image_smoke.sh` | the server's container image and its CI smoke test |
| `.github/workflows/publish-images.yml`, `scripts/publish_plan.py` | a release tag publishes both images on Docker Hub |
| `scripts/accept_all.py` | the full regression run: every shipped feature checked on your own server and machines ([how](docs/guide/how-to.md#check-a-server-end-to-end)) |
| `scripts/accept_m3.py`, `accept_m4.py`, `accept_mm.py` | acceptance runs on real machines, through the API and the official OpenAI SDK |
| `scripts/gpu_probe.py` | checks which NVIDIA GPU a workload sees, through the CUDA driver only |
| `scripts/accept_tts.py` | the real-hardware acceptance of text to speech (also the `tts` group of `accept_all.py`) |
| `scripts/accept_img.py` | the real-hardware acceptance of image generation (also the `img` group of `accept_all.py`) |
| `scripts/accept_x.py` | the real-hardware acceptance of the passthrough at `/x/` (also the `passthrough` group of `accept_all.py`) |
| `scripts/check_recommended.py`, `pin_images.py` | check the recommended models against Hugging Face; pin the recipes' images by digest |
| `web/` | React + TypeScript + Vite UI, built into `forgeyard/static` |

## Contributing and security

Bug reports, ideas and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Report a
vulnerability privately, as [SECURITY.md](SECURITY.md) explains; it also says what Forgeyard assumes
about the network and the people using it.

## Licence

[Apache License 2.0](LICENSE).
