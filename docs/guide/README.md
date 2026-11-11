# Forgeyard guide

Forgeyard runs work and models on the GPUs (and CPUs) you own, and serves the models behind one
OpenAI-compatible address. This guide takes you from nothing to your first API call, explains how
the pieces fit together, and answers the questions that come up later.

The guide is also inside Forgeyard itself: open **Guide** in the account menu, or `/guide` on your
server. It works without internet access. [繁體中文版](zh-TW/README.md)

## Where to start

| If you want to… | Read |
|---|---|
| Set Forgeyard up for the first time | [Quick start](quick-start.md): about 10 minutes, plus the model download |
| Understand machines, deployments, keys and roles | [Concepts](concepts.md) |
| Add an AMD or CPU-only machine, run the server or the agent in Docker, upgrade, back up | [How-to](how-to.md) |
| Add OpenAI, Anthropic, Gemini or Azure, and let a model spill to the cloud | [Cloud providers](cloud-providers.md) |
| Fix something that does not work | [Troubleshooting](troubleshooting.md) |
| Look up a setting or a command | [Reference](reference.md) |

## The pages

1. [Quick start](quick-start.md): install the server, trust its certificate, add a machine, choose
   its GPUs, add a recommended model, deploy it, create an API key, and call it with curl and the
   OpenAI SDK.
2. [Concepts](concepts.md): machines and agents, GPU access, workloads, models and recipes and
   estimates, deployments, the gateway and the Playground, API keys and limits and usage, cloud
   providers and tiers, cost and budgets, roles, sign-in and sessions.
3. [How-to](how-to.md): add an AMD machine or a CPU-only machine, run the agent in Docker, run a
   service or a task, add a cloud provider, track and limit cloud spend, restrict GPUs on a shared
   machine, add people, use your own certificate or a reverse proxy, run the server in Docker or as a service,
   upgrade, back up and restore, move a model cache, rotate keys, revoke a machine, uninstall the
   agent.
4. [Troubleshooting](troubleshooting.md): problems seen on real machines, with their symptom, cause
   and fix: certificates, offline agents, downloads, memory, every error from `/v1`, programs that
   do not trust the CA, missing models, spill and cost.
5. [Cloud providers](cloud-providers.md): add OpenAI, Anthropic, Gemini, Azure OpenAI, OpenRouter
   or a server of your own; route a model to the cloud; when a request spills; which keys may use the
   cloud; what each provider's endpoint cannot do; what leaves your server.
6. [Reference](reference.md): every server setting, every agent setting, the gateway's endpoints,
   headers and error codes, a workload's environment, the command line, files and ports.

## Conventions

- Commands are meant to be copied as they are. Replace only what is written like
  `192.168.1.20` (your server's address), `fyj_…` (a join token) or `fyk_…` (an API key).
- A block of commands holds only commands, so its **Copy** button copies something you can paste.
  What a command prints is shown in a separate block after it, marked *It prints*.
- The examples use `https://192.168.1.20:7860` for the server and `/srv/forgeyard-models` for the
  model cache. Use your own.
