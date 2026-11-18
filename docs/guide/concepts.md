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

Stopping or restarting the agent **leaves its models and apps running**: a restarted agent finds them and
takes them over. That is why the installed service uses `KillMode=process`.

The agent reads its settings from `/etc/forgeyard/agent.toml`, `FORGEYARD_*` environment variables
and flags ([all of them](reference.md#agent-settings)).

The **Machines** page shows one tile per machine, with a mark for its kind: **NVIDIA GPU**,
**AMD GPU**, **AMD APU** (an AMD GPU that shares the system memory, such as the Radeon 8060S) or
**CPU only**. Its cards are a row of slots filled from the bottom: what Forgeyard reserved, then
other use (other programs on the card, and apps that declare no memory), then free.
The tile also says how much memory is free, how many models and apps run there, and their
names. Above the tiles, a summary counts the machines of each kind and the offline ones: *1 NVIDIA
machine*, *2 CPU-only machines* (a machine, however many cards it has). Choose a machine for its own page: a **memory map** that splits each card by what holds it
(named, with what it reserved), what runs there, its GPU access and runtimes, and its agent. Admins
revoke or remove a machine there.

## GPU access

By default Forgeyard may use every GPU of a machine. On a machine you share with other people or
programs, an admin chooses on the machine's page (**Machines** → the machine) → **Change GPU access**
which cards Forgeyard may use. A
card outside the list is never given to anything Forgeyard runs.

- A GPU given to an exclusive replica belongs to it alone.
- A replica that shares a card (embedding models, rerankers, speech to text by default) is held to
  its own memory reservation, so several fit on one card.
- Memory is what the card reports **free** right now, so memory others use is respected, minus a
  safety margin (512 MiB).
- A process gets only its cards' IDs in `CUDA_VISIBLE_DEVICES` (NVIDIA) or `ROCR_VISIBLE_DEVICES`
  (AMD); a container gets only its cards passed in. A card in use cannot be taken off the list.

## GPU sharing

Every GPU deployment is **Exclusive** or **Share VRAM**:

- **Exclusive**, the default for chat recipes: each replica gets whole cards, and nothing else
  Forgeyard runs goes on them. The model has all of the card's compute.
- **Share VRAM**, the default for embeddings, rerankers and speech to text: a replica reserves its
  estimate, and other sharing replicas fit in the rest of the card (vLLM is told to use only that
  much). Replicas on one card share its compute, so under load each answers more slowly.

An exclusive replica and a sharing one never use the same card: both must share. Forgeyard points out
when sharing would help:

- the deploy and edit pages say so when an exclusive model needs half its card or less (*This model
  needs 14.38 GiB of an 80.00 GiB card; with Share VRAM the rest can hold other models*);
- a refusal says *fits if both share VRAM* and names the replica holding the card, when the memory is
  there and only exclusivity is in the way;
- **Setup health** notes (`deployment-could-share`) a running exclusive replica that reserves less than
  half of a card of 24 GiB or more.

Change it on a deployment with **Edit**: [Change a deployment](how-to.md#change-a-deployment).

## Apps (deprecated)

Before 1.4 the console could also run **apps**: programs of your own (a container or a process,
a service or a one-off task) on your machines, optionally exposed at `/x/<name>` with API keys.
Forgeyard is about running and managing models, so in 1.4 apps left the console, and **in 1.5 they
are removed**: their part of the API (`/api/workloads`), the templates and the gateway's `/x/`. Until
then they keep working through the API only, and say so: every answer carries a `Deprecation` header,
the server log warns (at most once an hour), and **Setup health** warns while any app exists. Details
are in the [reference](reference.md#apps-deprecated).

- To serve a model, deploy it under **Serving**. Object detection, segmentation and depth are
  [vision models](#vision-models) since 1.4.
- A model that no built-in engine runs gets a **custom engine** (**Models → Engines → Add a
  custom engine**): a recipe like the built-in ones, for its container image or program.

**Machines** still names an app that holds a card, so a card in use is never shown free. A model
deployment runs as a workload too (of runtime `model`): that is how models run, and it is not
deprecated.

## Models, engines and estimates

Two pages hold the models. **Serving** is what runs now: the deployments grouped by kind (chat,
multimodal chat, embeddings, rerank, speech to text, text to speech, images, video, vision), each
with the path it answers at under `/v1`, and one line per deployment: where it runs (machine and
cards), its memory and its state. **Deploy a model** is there too, and each deployment has its page
at `/serving/<id>`. A deployment with a [cloud fallback](#cloud-providers-and-tiers) shows its
provider, such as *→ Gemini*; the fallback itself is on the deployment's page.

**Models** is the library, what you can deploy, in three tabs: **Catalog** (the default), **Engines**
and, for admins, **Sources**. Cloud models and providers are on the **Cloud** page (admins). Links
from 1.3 (`/models/deployments/<id>`, `/models/deploy`, `/models?tab=routes`, `/models/routes/<id>`,
`/providers`) open the new places.

- The **catalog** lists the models you can deploy. An admin adds one from the **recommended**
  list, from a **Hugging Face search**, or from a source: `hf://org/name`, a GGUF URL, or
  `local://` a directory on the machines. Adding a model reads its details without downloading it.
  Each row has **Deploy** and **Download to…**.
- **What a model is for** comes from its Hugging Face task, checked against its `config.json`. Many
  rerankers are tagged *text classification*, and so are text classifiers; vLLM's rule tells them apart. A
  model with one output (`num_labels` 1: bge-reranker, the ms-marco cross-encoders) is a reranker; one with
  two labels or more (sentiment, topic, NLI) is a text classifier, which cannot be added yet (support comes
  with #144). Rerankers that are language models, such as Qwen3-Reranker, are not affected.
- An **engine** says how to run a kind of model: which program, which container image (pinned by
  digest) or release, which parameters, and how much memory it needs. The API and the YAML files
  call it a **recipe** (`/api/recipes`, `forgeyard/recipes/*.yaml`); an admin adds a custom one on the
  **Engines** tab. Built in: `vllm` (chat on
  NVIDIA and AMD), `vllm-pooling` (embeddings and rerank), `llama-cpp` (GGUF on NVIDIA, on AMD with ROCm or Vulkan, or on the CPU) and
  `faster-whisper` (speech to text).
- Each catalog model has **its own page** (1.4.3), opened from its name: `/models/<name>`. It shows
  what the model is (source and revision, licence, task, architecture), **which machines have it** in
  their model cache and **whether it fits** each one now with an engine's defaults, its deployments, the
  engines that run it, and its files with their sizes. Each engine has one too, `/models/engines/<name>`:
  its variants (the image or program per kind of hardware, and whether it is pinned by digest), **which
  machines have that image or program**, its parameters, and the deployments and models that use it.
  Model and engine names everywhere in the console link to these pages. An admin gives a model a
  **display name** there; its name stays, because deployments record it, so `deploy`, `deployments`,
  `routes`, `engines`, `catalog`, `sources`, `recommended`, `search` and `inspect` are not model names.
  ([Look at a model or an engine](how-to.md#look-at-a-model-or-an-engine).)
- **Sources** (admins) say where models may come from: the hosts allowed for GGUF URLs, the
  directories for `local://` models, and the server's Hugging Face token.
- The **estimate** is the memory one copy needs: the weights, the KV cache for the context and
  concurrency you choose, the engine's own overhead and the CUDA context, and, for a model that
  reads images, its vision encoder. The deploy page shows it,
  part by part, and where it fits, before anything is downloaded.
- The **model cache** on each machine (`/srv/forgeyard-models` by default, for an agent run as root)
  keeps the downloaded files, checked against their checksums, shared by every replica there.
  Containers get their model mounted read-only. The machine's page lists what is in it, with sizes,
  and removes what no deployment there uses ([Free disk space on a machine](how-to.md#free-disk-space-on-a-machine)).
  **Download to…** fills it ahead of a deployment, with the engine too
  ([Download a model before deploying it](how-to.md#download-a-model-before-deploying-it)). A machine
  that cannot reach Hugging Face or GitHub gets the files **through the server**, which passes them on
  and stores nothing; the agent checks them as it checks any download. Since 1.4.1 a model file comes
  from another machine that has it in its cache, still through the server, before the internet. A pinned
  engine image comes the
  same way, layer by layer, and an admin can upload any image file to a machine from its page, or copy an
  image from another machine's engine (1.4.1); the whole path is in
  [Run a machine without internet access](how-to.md#run-a-machine-without-internet-access). Whether a
  model is on a machine is always what its agent reports, so a model copied there by hand counts too.
- **Gated** Hugging Face models need a token of an account that accepted their licence. The server
  keeps **one Hugging Face token** (an admin sets it under **Models → Sources**, stored encrypted) for
  inspecting and downloading them; a deployment may bring its own, which wins for it.

## Deployments and the gateway

A **deployment** runs one model with one engine (recipe), as one or more replicas. Its replicas run as a
workload of runtime `model`, seen only on the deployment's page (not on **Apps**). Forgeyard:

1. places each replica where it fits. Among the machines that fit, in this order:
   - a machine that **could not reach** a source the replica would download from there (the model's
     host, such as huggingface.co, the engine archive's, such as github.com, or the image registry,
     such as ghcr.io) in the last hour comes last: it is used only when nothing else fits. A
     download counts as "could not reach" when the name did not resolve, the connection was refused
     or timed out, or the network was unreachable; an HTTP error, a refused token or a full disk does
     not. A machine whose agent can download **through the server** (1.4; its **Internet access for
     downloads** is **Auto** or **Always through the server**) gets the model, the engine archive and
     a pinned engine image that way instead, so it is not avoided for them
     ([Give an offline machine models through the server](how-to.md#give-an-offline-machine-models-through-the-server));
   - replicas of one deployment go on **different machines** when they can;
   - a machine that already has the **model in its cache** wins, then one that also has the engine
     (the archive a process engine runs, or the image a container engine runs);
   - then the tightest card that is big enough (whole cards stay free for big models);
2. places a replica again on another machine that fits when its download **could not reach its
   source** where it was (it waits in backoff there): the machine is remembered for an hour as one
   that cannot reach that host. A deployment pinned to a machine stays there and keeps trying. The
   replica says why, on its deployment's page: *cpu-01 could not reach huggingface.co; placed on
   cpu-02 instead*;
3. refuses a deployment that fits nowhere, and says, machine by machine, why and by how much;
4. downloads the model on that machine (and pulls the engine's image, if it is not there yet), starts
   the engine and waits until it is healthy. The deployment's page shows how far the download and the
   pull have got; deleting the deployment stops both at once.

Every ready deployment answers at **`https://<server>:7860/v1`**, the **gateway**, under its model
name, with the OpenAI API: `/v1/models`, `/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`, `/v1/rerank`, `/v1/score`, `/v1/audio/transcriptions` and `/translations`.
`/v1/models` lists the models your key may use; a stopped deployment leaves the list until it is
started again, unless its route can send the request to a cloud provider your key may use.

The gateway sends each request to the ready replica with the fewest requests in flight, and to the
next one if a replica does not answer. Replicas cannot be used around it: each has an internal key
that only the gateway knows, and the agent refuses every other request on the replica's port.

The **Playground** tries a model through the same gateway, as you, without an API key. One **Model**
picker lists the models that are ready, grouped by kind with the labels and icons of **Serving** (Chat,
Multimodal chat, Embeddings, Rerank, Speech to text, Text to speech, Images, Video, Vision), and picking
one opens its panel: chat (with streaming, and images for models that read them), embeddings, rerank,
speech to text, **Speak**, **Image**, **Video**, or the vision panel (a picture in; boxes, a mask or a
depth map out). The address keeps the model (`/playground?model=<name>`); 1.3's Vision tab address
(`/playground?tab=vision`) opens the first ready vision model, or the picker when there is none. With no
model ready, the page says so and points to **Models → Catalog** to deploy one. Its requests count as
`console` in Usage, and its cloud setting is **Allowed** for admins and, for everyone else, the most they
may give their own keys.

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

## Video

Video models answer `/v1/videos`, OpenAI's Videos API. A video takes minutes, so it is a **job**: the
request answers at once with the job (`queued`), you poll it until it is `completed` (or `failed`), then
fetch the MP4. One engine serves it: vLLM-Omni (recipe `vllm-omni-video`) on NVIDIA GPUs, with Wan
text-to-video pipelines (Wan2.1-T2V-1.3B, Wan2.2-TI2V-5B).

- **A job lives on one replica.** The gateway sends it to the ready replica with the fewest jobs, and
  asks that replica, and only that one, for its status and its file. The engine keeps its jobs in
  memory and its files on its own disk.
- **A replica that goes takes its jobs with it.** When the replica is stopped, moved or deleted, or its
  engine restarts, its jobs report `failed` with the reason (`replica_gone`, `replica_restarted`,
  `video_lost`): ask again. A finished video whose replica went answers 410 for its content.
- **Jobs survive a server restart.** They are in the server's database and the replicas keep working;
  the server picks them up again.
- **Results expire.** A finished video can be fetched for the deployment's *Keep results* (1,440
  minutes, a day); then it is deleted on the replica and its content answers 410. The engine also
  sweeps its own old files an hour after that, in case a deletion was missed.
- **Caps per deployment, each with a working default:** the longest video (5 s), the largest size (the
  model's own: 832x480 for Wan2.1, 1280x704 for Wan2.2), jobs waiting or being made (8), of those one
  key's (2), and the longest a job may take (an hour). The memory estimate is sized for the longest,
  largest video.
- **Text to video only** in 1.3: image-to-video references are refused.
- **The prompt is not kept**, and a key sees only its own jobs.
- Usage counts **one request per video**, its **length** and the **busy time** (GPU time).
- Videos stay on your own deployments; they never go to a cloud provider.

## Vision models

Vision models (1.4) look at a picture: **object detection** (boxes, labels and scores: RT-DETR, D-FINE,
DETR…), **segmentation** (the mask of what a point or a box shows: SAM 2 and 2.1) and **depth
estimation** (how near every pixel is: Depth Anything, DPT…). They are model kinds like the others: added
to the catalog from Hugging Face (three are recommended), deployed and listed under
**Serving → Vision**, tried in the **Playground** (under *Vision* in its model picker), called with an
API key.

- **One engine, Forgeyard's own.** The `vision` recipe runs Forgeyard's vision server (transformers on
  PyTorch) in the `forgeyard-vision` image, pinned by digest like every engine: the CUDA build on an
  NVIDIA GPU, one card shared by memory, or the CPU build on any machine (an AMD machine runs it on its
  CPU: there is no ROCm build yet). The model comes from the machine's model cache, like every other model.
- **Which models:** the catalog reads the model's `config.json`. An `…ForObjectDetection` architecture
  is detection, SAM 2 segmentation, an `…ForDepthEstimation` one depth; anything else is not a vision
  model. **Permissive licences only** (Apache-2.0, MIT, BSD): Depth Anything V2 Base and Large
  (CC-BY-NC-4.0) and Ultralytics' YOLO checkpoints (AGPL-3.0) cannot be added. Nor can zero-shot
  (open-vocabulary) detectors, which find what a text prompt names (Grounding DINO, MM-Grounding-DINO,
  OWL-ViT, OWLv2, OmDet-Turbo): their architectures end in `ForObjectDetection` too, but the vision server
  cannot load them yet (support comes with #142). One added before 1.4.3 stays in the catalog and cannot be
  deployed.
- **Their own API:** `/v1/vision/detect`, `/v1/vision/segment` and `/v1/vision/depth`, one image per
  request (multipart, or base64 in JSON) and a JSON answer; masks and depth maps are PNGs in base64.
  The OpenAI SDK has no such API: any HTTP client calls it.
- **One image at a time** per replica; the rest wait their turn. More throughput is more replicas.
- Usage counts **one image per request**, its kilopixels and the busy time. Pictures stay on your own
  deployments: they never go to a cloud provider, and nothing keeps them.

## API keys, limits and usage

- Programs call `/v1` with an **API key** (`fyk_…`). Everyone creates their own on **API keys**. A
  key is shown once; the server keeps only its hash.
- The list on **API keys** shows the keys that still work. Revoked and expired keys are folded away
  under one row, **Show N revoked keys**, in *Mine* and in *Everyone's*; **Usage** and the **Audit log**
  still name them. Its *Limits* column shows only the limits a key has ("3 requests/min",
  "10k tokens/min"), or *No limits*.
- A key can be limited to some models, have an expiry, and have **limits**: requests per minute and
  tokens per minute. Over a limit, the gateway answers 429 with `Retry-After`.
- Admins set **defaults** (for keys without their own limits) and **caps** (the most anyone may
  give their own key), and can set limits on anyone's key. A limit an admin set on someone else's
  key is **locked**: its owner cannot change it.
- Each key also has a **cloud** setting and may have a monthly **budget** (see below).
- **Usage** counts requests, errors, tokens and latency per day, model, key, person, source and
  local or cloud. Everyone sees their own; admins see everyone's. Its **Cost** tab shows what cloud
  requests cost (see below).
- The table under the chart lists the top 15 rows by the column it is sorted by (requests; cost on
  **Cost**; the latest days first when grouped by day). The rest are added up in one last row, *N
  others*, so the table still adds up to the totals; **Show all N** lists every row. Click a column's
  heading to sort by it, and again for the other way round. On a phone each row is one line, the
  name and the number it is sorted by; tap it for the other columns, and choose the order with
  **Sort by**. A long name is cut short: hover it, or open the row, for the whole name.
- **Over time**, on both tabs, draws one of those numbers as a line chart: per day (the last 30
  days), week (13 weeks, from Monday) or month (12 months), in total or one line per key, model,
  provider or person (admins, with **Everyone**). The five largest get a line each and the rest are
  added up as *Other*. Hover a point or use the arrow keys to read a period's values; **Show the
  numbers** lists them as a table. Days are UTC, and the latest period is still going on (dashed).

## Cloud providers and tiers

- A **provider** is a model API outside Forgeyard (OpenAI, Anthropic, Gemini, Azure OpenAI,
  OpenRouter, or an OpenAI-compatible server of yours). Admins add it with its key, which is stored
  encrypted and never shown again.
- Each model name at `/v1` is a **route** (in the API). A route has **targets** in **tiers**: local
  deployments and providers' models. A deployment's own route starts with one tier, the deployment.
  An admin can add a cloud model as a later tier: the deployment's **cloud fallback**, on its page.
  An admin can also make a **cloud model**, a name of its own for a provider's model, on
  **Cloud → Cloud models**.
- **Local first.** A request goes to the next tier only before its answer starts, and only when the
  local tier is unavailable, **busy** (every replica at its deployment's *Concurrent requests*), or
  answers 429 or 503. A prompt too long for the local context spills only if the route says so.
- Each API key's **cloud** setting decides whether its requests may leave your network: **Never**
  (the default, and every key from before 1.2), **Only to spill**, or **Allowed** (cloud-only models
  too). A route marked **Local only**, and a request with `X-Forgeyard-Local-Only: 1`, never leave
  either.
- The **cloud policy** (an admin's choice on **API keys**) is two values in one: what new keys get,
  and the most people may give their own keys. *No cloud*, *People may opt in to spill*, *Spill by
  default* and *Cloud allowed* name the usual pairs; *Custom* shows both.
- A provider is **outside your network** (it costs money, and only keys allowed to use the cloud
  reach it) or **inside** it (every key reaches it and no budget applies). A new provider gets the
  tick its kind and its address suggest; an admin confirms it.
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
| `admin` | everything: users, machines and their GPU access, the model catalog and engines, cloud providers and routes, the audit log, everyone's keys, limits and cloud settings |
| `operator` | run apps and deploy models; everything a user can |
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
