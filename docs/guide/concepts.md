# Concepts

How Forgeyard fits together, in the order you meet it. Each section says what the thing is, what
it does for you, and what to keep in mind.

## Machines and agents

A **machine** is any computer Forgeyard can run work on: a GPU server, a workstation, a computer
with only a CPU. Each one runs the **agent**, a small program that:

- reports the machine's hardware: every GPU (model, memory, driver, load, temperature), the CPU and
  the RAM;
- keeps one connection to the server, which **the agent opens**: the server never needs to reach
  the machine, so a machine behind a firewall or NAT works as long as it can reach the server;
- starts, watches and stops what the server asks it to run there.

A machine **joins** once, with a **join token** from **Machines** → **Add a machine**: it works once,
for 15 minutes, and the server keeps only its hash. The agent trades it for the machine's own
**credential**, kept in its state directory (`/var/lib/forgeyard-agent`, mode 0600). Later starts
need no token.

The agent trusts the server only through its **certificate authority** (CA), pinned by the
fingerprint in the join command: a machine in between with another certificate is refused. See
[Trust the CA](quick-start.md#2-create-the-owner-and-trust-the-ca).

A machine shows **online** while its agent is connected, and **offline** when nothing has arrived
for 30 seconds (the agent sends a heartbeat every 10). The agent reconnects by itself. An admin can
**revoke** a machine (its credential stops working at once) or **remove** it.

Stopping or restarting the agent **leaves its workloads running**: a restarted agent finds them and
takes them over. That is why the installed service uses `KillMode=process`.

The agent reads its settings from `/etc/forgeyard/agent.toml`, `FORGEYARD_*` environment variables
and flags ([all of them](reference.md#agent-settings)).

## GPU access

By default Forgeyard may use every GPU of a machine. On a machine you share with other people or
programs, an admin chooses on **Machines** → **Change GPU access** which cards Forgeyard may use. A
card outside the list is never given to anything Forgeyard runs.

- A GPU given to an exclusive replica belongs to it alone.
- A replica that shares a card (embedding models, rerankers, speech to text by default) is held to
  its own memory reservation, so several fit on one card.
- Memory is what the card reports **free** right now, so memory others use is respected, minus a
  safety margin (512 MiB).
- A process gets only its cards' IDs in `CUDA_VISIBLE_DEVICES` (NVIDIA) or `ROCR_VISIBLE_DEVICES`
  (AMD); a container gets only its cards passed in. A card in use cannot be taken off the list.

## Workloads

A **workload** is something to run on your machines:

- a **service** runs until you stop it. It is health-checked and restarted when it exits;
- a **task** runs to the end, with retries and a time limit.

Each one has 1 to 16 **replicas**, each on one machine. A workload runs as a **container** (Docker
or Podman on the machine) or as a **process** (a program started by the agent).

Isolation, in short:

- A process runs as an unprivileged account (`forgeyard-run`), never as root, with an empty
  environment: only what the workload declares, plus its GPU and port variables.
- A container runs with the engine's defaults plus `no-new-privileges`, never `--privileged`.
- Limits: containers get `--cpus` and `--memory`; processes get a systemd scope on a systemd host,
  else rlimits.

Only admins and operators create workloads, because whoever can create one can run code on the
machines. Model deployments are workloads too; most people only deploy models.

## Workloads at /x/

A service workload can be **exposed** behind the gateway: programs then call its own HTTP API at
`https://<server>:7860/x/<name>/…` with an API key, as they call models at `/v1`. Use it for what is
not a chat, embedding, speech or image model: object detection, segmentation, depth, Triton or KServe
models, a FastAPI or BentoML service.

- **Behind the key guard.** An exposed workload runs behind the agent's key guard, like a model
  replica: the program listens on loopback, the guard holds the published port, and only the gateway
  gets through. The guard removes the gateway's internal key before the request reaches the program.
  Every port of the workload is guarded, not only the exposed one.
- **Keys opt in.** A key's **Workloads** setting says which exposed workloads it may call: *None*
  (the default, and every key from before 1.3), *Listed workloads* or *Every exposed workload*.
- **APIs, not web pages.** Answers are sandboxed (`Content-Security-Policy: sandbox`), cookies and
  CORS headers are removed, and redirects are rewritten under `/x/<name>`. A browser UI (Gradio,
  Jupyter) does not work at `/x/`. WebSockets and gRPC are not passed on in 1.3, and nothing spills
  to the cloud.
- **Counted.** Requests, errors, bytes in and out, busy time and latency go to **Usage** as the
  model `x:<name>`; the key's requests per minute apply.

Admins and operators expose a workload, when they create it or later on its page. See
[Expose a workload at /x/](how-to.md#expose-a-workload-at-x).

Three **templates** start the most common vision APIs this way in one step (object detection,
segmentation, depth), on Forgeyard's own image with permissively licensed models only. See
[Start a workload from a template](how-to.md#start-a-workload-from-a-template).

## Models, recipes and estimates

- The **catalog** lists the models you can deploy. An admin adds one from the **recommended**
  list, from a **Hugging Face search**, or from a source: `hf://org/name`, a GGUF URL, or
  `local://` a directory on the machines. Adding a model reads its details without downloading it.
- A **recipe** says how to run a kind of model: which engine, which container image (pinned by
  digest) or release, which parameters, and how much memory it needs. Built in: `vllm` (chat on
  NVIDIA and AMD), `vllm-pooling` (embeddings and rerank), `llama-cpp` (GGUF on NVIDIA, on AMD with ROCm or Vulkan, or on the CPU) and
  `faster-whisper` (speech to text).
- The **estimate** is the memory one copy needs: the weights, the KV cache for the context and
  concurrency you choose, the engine's own overhead and the CUDA context, and, for a model that
  reads images, its vision encoder. The deploy page shows it,
  part by part, and where it fits, before anything is downloaded.
- The **model cache** on each machine (`/srv/forgeyard-models` by default) keeps the downloaded
  files, checked against their checksums, shared by every replica there. Containers get their model
  mounted read-only.

## Deployments and the gateway

A **deployment** runs one model with one recipe, as one or more replicas. Forgeyard:

1. places each replica where it fits: the tightest card that is big enough (whole cards stay free
   for big models), on machines that already have the model cached when it can, and replicas of
   one deployment on **different machines** when it can;
2. refuses a deployment that fits nowhere, and says, machine by machine, why and by how much;
3. downloads the model on that machine, starts the engine and waits until it is healthy.

Every ready deployment answers at **`https://<server>:7860/v1`**, the **gateway**, under its model
name, with the OpenAI API: `/v1/models`, `/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`, `/v1/rerank`, `/v1/score`, `/v1/audio/transcriptions` and `/translations`.
`/v1/models` lists the models your key may use; a stopped deployment leaves the list until it is
started again, unless its route can send the request to a cloud provider your key may use.

The gateway sends each request to the ready replica with the fewest requests in flight, and to the
next one if a replica does not answer. Replicas cannot be used around it: each has an internal key
that only the gateway knows, and the agent refuses every other request on the replica's port.

The **Playground** tries a model through the same gateway, as you, without an API key: chat (with
streaming, and images for models that read them), embeddings, rerank and speech to text. Its requests count as `console` in Usage, and its
cloud setting is **Allowed** for admins and, for everyone else, the most they may give their own keys.

## Images, audio and video in chat

Models that read images (Qwen3-VL, Gemma 3 and 4, and similar) take them in
`/v1/chat/completions` as OpenAI's content parts. What a model takes is read when it is added to
the catalog: from its `config.json`, or, for a GGUF model, from its **projector file**
(`mmproj-*.gguf`), which llama.cpp needs to see images. The catalog shows it next to the licence.

- Send an image as a `data:` URI, or as an `https://` URL: the **gateway downloads it** and passes
  the data on. Engines never download anything themselves, and the gateway only fetches public
  addresses, so a request cannot make your servers reach into your own network.
- Each deployment takes at most a few images per request (4 by default), video only when it is
  turned on (it is off by default: it costs memory at startup), and requests up to a size (64 MB by
  default). These are in the deploy form's **Advanced** section; the defaults work.
- Requests with images, audio or video stay on your own deployments; they never go to a cloud
  provider.
- Usage counts the media items; image tokens are inside the prompt tokens.

## Text to speech

Speech models answer `/v1/audio/speech`, OpenAI's text-to-speech call, with the audio itself. Three
engines serve them:

| Engine (recipe) | Where | Models | Languages |
|---|---|---|---|
| vLLM-Omni (`vllm-omni-tts`) | NVIDIA GPUs | Qwen3-TTS CustomVoice: nine built-in voices, style instructions | Chinese (with Mandarin dialects), English and eight more |
| llama.cpp (`llama-cpp`) | AMD GPUs and APUs (Vulkan or ROCm), NVIDIA, the CPU | TTS GGUFs with their projector: Qwen3-TTS, Chatterbox | Qwen3-TTS: Chinese, English and more; Chatterbox: English |
| speaches (`speaches-tts`) | the CPU, NVIDIA | Kokoro-82M (51 voices), Piper voices | English (and other Latin-script languages) |

- A speech model has **voices** of its own (the Playground lists them). OpenAI's voice names such as
  `alloy` are replaced by the model's default voice, and the answer says which.
- **Voice cloning is not available**: no reference recordings, no uploaded voices. Only the voices a
  model was made with speak.
- Each deployment takes at most so many characters per request (*Longest input*, under
  **Advanced**). The default fits what the engine can speak in one go.
- Usage counts the **characters** spoken and the **length of the audio**. For a key's tokens per
  minute, a character counts as a token.
- Speech stays on your own deployments; it never goes to a cloud provider.

## Images

Image models answer `/v1/images/generations`, OpenAI's image call, with the images as base64. Two
engines serve them:

| Engine (recipe) | Where | Models |
|---|---|---|
| vLLM-Omni (`vllm-omni-image`) | NVIDIA GPUs | diffusers pipelines (a repository with `model_index.json`): Z-Image-Turbo, Stable Diffusion XL, FLUX.1-schnell, Qwen-Image |
| stable-diffusion.cpp (`sd-cpp`) | AMD GPUs and APUs (Vulkan), NVIDIA (CUDA), the CPU (a process, no Docker) | GGUF or single files: Stable Diffusion 1.5 / XL checkpoints, Z-Image and FLUX with their text encoder and VAE beside them in the same repository |

- **Base64 only.** Forgeyard keeps no copy of the images it makes, so it cannot hand out a URL:
  `response_format: url` is refused. Decode `data[].b64_json`.
- **Caps per deployment, each with a working default:** the most images per request (4), the largest
  side (the model's own: 512 for Stable Diffusion 1.5, 1024 for the others), the most steps (50). The
  memory estimate is sized for them, so raising a cap raises the estimate.
- **One at a time, then a queue.** An image takes seconds on a GPU and minutes on a CPU. A replica
  works on one request at a time (*Requests at once*); the others wait in the gateway, in order. When
  the queue is full, a request gets 429 with `Retry-After`.
- **Licences.** The catalog shows each model's licence and whether it is gated. The recommended list
  holds only Apache-2.0 and OpenRAIL models; FLUX.1-dev, SD-Turbo and SD3.5 are not on it (non-commercial
  or revenue-limited terms), though they can be added by hand. OpenRAIL models (SDXL, SD 1.5) may be used
  commercially, but their licence forbids some uses: their cards mark the licence and link to it, as they
  do for Gemma and Llama.
- **No prompt filtering.** Forgeyard checks a request's shape and caps, not what it asks for.
- Usage counts the **images**, their **kilopixels** and the **busy time** (GPU time on a GPU).
- Images stay on your own deployments; they never go to a cloud provider.

## API keys, limits and usage

- Programs call `/v1` with an **API key** (`fyk_…`). Everyone creates their own on **API keys**. A
  key is shown once; the server keeps only its hash.
- A key can be limited to some models, have an expiry, and have **limits**: requests per minute and
  tokens per minute. Over a limit, the gateway answers 429 with `Retry-After`.
- Admins set **defaults** (for keys without their own limits) and **caps** (the most anyone may
  give their own key), and can set limits on anyone's key. A limit an admin set on someone else's
  key is **locked**: its owner cannot change it.
- Each key also has a **cloud** setting and may have a monthly **budget** (see below).
- **Usage** counts requests, errors, tokens and latency per day, model, key, person, source and
  local or cloud. Everyone sees their own; admins see everyone's. Its **Cost** tab shows what cloud
  requests cost (see below).

## Cloud providers and tiers

- A **provider** is a model API outside Forgeyard (OpenAI, Anthropic, Gemini, Azure OpenAI,
  OpenRouter, or an OpenAI-compatible server of yours). Admins add it with its key, which is stored
  encrypted and never shown again.
- Each model name at `/v1` is a **route**. A route has **targets** in **tiers**: local deployments
  and providers' models. A deployment's own route starts with one tier, the deployment. An admin
  can add a cloud model as a second tier, or make a route that is only a cloud model.
- **Local first.** A request goes to the next tier only before its answer starts, and only when the
  local tier is unavailable, **busy** (every replica at its deployment's *Concurrent requests*), or
  answers 429 or 503. A prompt too long for the local context spills only if the route says so.
- Each API key's **cloud** setting decides whether its requests may leave your network: **Never**
  (the default, and every key from before 1.2), **Only to spill**, or **Allowed** (cloud-only models
  too). A route marked **Local only**, and a request with `X-Forgeyard-Local-Only: 1`, never leave
  either.
- Answers name who served them in `X-Forgeyard-Target`. Usage counts local and cloud apart.

[Cloud providers](cloud-providers.md) has the details for each provider.

## Cost and budgets

- **Cost.** Every request is priced once, when it finishes. Local replicas and providers inside your
  network cost 0. A cloud request costs what the provider reports (OpenRouter does), or else its
  tokens at the provider model's **prices** in US dollars per million tokens, which an admin enters
  or refreshes from OpenRouter's public list. A cloud request whose model has no price is counted as
  *without a price*, not as 0. **Usage** → **Cost** shows the cost per day, key, person, model or
  provider. It is Forgeyard's estimate of what the providers charge, not their invoice.
- **Budgets.** A budget is what cloud requests may cost per calendar month (UTC), in US dollars, set
  on an API key (by its owner, or by an admin, which locks it), on a person (by an admin) or on a
  provider (by an admin). Before a request goes to a cloud provider, its largest possible cost is
  **reserved** against every budget that applies; a budget that cannot cover it keeps the request
  from that provider. The request then stays local if it can, and fails with **402** only when
  nothing else can serve it.
- **Alerts.** At 80% and at 100% of a budget, Forgeyard writes one event to the **Audit log** and
  shows a warning on **API keys**, **Usage** → **Cost** and the provider's page.

[Cost and prices](cloud-providers.md#cost-and-prices) and [Budgets](cloud-providers.md#budgets)
have the details.

## Roles

| Role | Can |
|---|---|
| `admin` | everything: users, machines and their GPU access, the model catalog and recipes, cloud providers and routes, the audit log, everyone's keys, limits and cloud settings |
| `operator` | run workloads and deploy models; everything a user can |
| `user` | look around, use deployed models in the Playground and with their own API keys, see their own usage |

The **owner** is the first account. It is always an admin and cannot be demoted, disabled or
deleted, so nobody can lock administration out. New people get a one-time link to set their own
password; self-registration is off unless `REGISTRATION=approval`.

## Sign-in and sessions

- Passwords have at least 12 characters, and common passwords are refused. Repeated failed sign-ins
  make each further try wait (`LOGIN_FREE_FAILURES`, `LOGIN_IP_FAILURES`).
- A session ends after 2 hours without a request, and after 7 days in any case
  (`SESSION_IDLE_MINUTES`, `SESSION_MAX_DAYS`).
- On **My account**, you change your password (your other sessions are signed out) and see every
  browser signed in as you, each of which you can sign out. An admin can sign someone out everywhere,
  disable them, or give them a new set-password link on **Users**.
- The **Audit log** (admins) records sign-ins, failed sign-ins, changes to accounts, machines, models,
  keys and providers, and budget alerts.
