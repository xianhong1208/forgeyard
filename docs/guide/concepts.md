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

## Models, recipes and estimates

- The **catalog** lists the models you can deploy. An admin adds one from the **recommended**
  list, from a **Hugging Face search**, or from a source: `hf://org/name`, a GGUF URL, or
  `local://` a directory on the machines. Adding a model reads its details without downloading it.
- A **recipe** says how to run a kind of model: which engine, which container image (pinned by
  digest) or release, which parameters, and how much memory it needs. Built in: `vllm` (chat on
  NVIDIA and AMD), `vllm-pooling` (embeddings and rerank), `llama-cpp` (GGUF on GPU or CPU) and
  `faster-whisper` (speech to text).
- The **estimate** is the memory one copy needs: the weights, the KV cache for the context and
  concurrency you choose, the engine's own overhead and the CUDA context. The deploy page shows it,
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

The gateway sends each request to the ready replica with the fewest requests in flight, and to the
next one if a replica does not answer. Replicas cannot be used around it: each has an internal key
that only the gateway knows, and the agent refuses every other request on the replica's port.

## API keys, limits and usage

- Programs call `/v1` with an **API key** (`fyk_…`). Everyone creates their own on **API keys**. A
  key is shown once; the server keeps only its hash.
- A key can be limited to some models, have an expiry, and have **limits**: requests per minute and
  tokens per minute. Over a limit, the gateway answers 429 with `Retry-After`.
- Admins set **defaults** (for keys without their own limits) and **caps** (the most anyone may
  give their own key), and can set limits on anyone's key. A limit an admin set on someone else's
  key is **locked**: its owner cannot change it.
- **Usage** counts requests, errors, tokens and latency per day, model, key and person. Everyone
  sees their own; admins see everyone's. It counts, it does not bill.

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

## Roles

| Role | Can |
|---|---|
| `admin` | everything: users, machines and their GPU access, the model catalog and recipes, cloud providers and routes, the audit log, everyone's keys, limits and cloud settings |
| `operator` | run workloads and deploy models; everything a user can |
| `user` | look around, use deployed models in the Playground and with their own API keys, see their own usage |

The **owner** is the first account. It is always an admin and cannot be demoted, disabled or
deleted, so nobody can lock administration out. New people get a one-time link to set their own
password; self-registration is off unless `REGISTRATION=approval`.
