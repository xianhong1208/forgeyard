# Roadmap

Where Forgeyard is going after 1.0. The order was agreed on 2026-11-10; each line names what
users get, not how it is built. Versions are minor releases unless a change breaks something.

## Shipped

**1.0.0 (2026-11-10), the MVP.** Milestones M0 to M4: sign-in and roles; machines join through
the agent; services and tasks as containers or processes with only the GPUs they were given;
models deployed from a catalog with engine recipes and memory estimates; one OpenAI-compatible
gateway at `/v1` with API keys, limits and usage. Each milestone was accepted on a real 8×A100
host (Forgeyard limited to one shared card) plus a CPU machine. See [CHANGELOG.md](CHANGELOG.md).

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

## Publishing the source: with 1.2

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

## Next: 1.2, cloud model APIs behind the same gateway

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

Two pull requests:

1. **Providers and tiered routing** (PR A, `providers`, in review): OpenAI-compatible endpoints only (OpenAI,
   Anthropic, Gemini, Azure OpenAI, OpenRouter, any compatible server) with a quirks table, no LiteLLM;
   tiered routes; the spill rules; the per-key cloud setting; egress rules; usage per target.
2. **Cost and budgets** (PR B): prices per provider model (entered by hand, or refreshed from
   OpenRouter's catalogue with a diff to accept), the cost of each request stored in micro-dollars,
   monthly budgets (UTC calendar month) per key, person and provider with reservation, alerts at 80%
   and 100%.

Later, if Claude users need it: an Anthropic `/v1/messages` passthrough, which keeps prompt caching
and thinking.

## Then

| Version | What you get |
|---|---|
| **1.3: use GPUs only while needed** | **Scale to zero:** a deployment nobody calls stops and frees its GPU. The next request waits in a queue while it starts again. **Autoscaling:** replicas are added and removed by queue length or requests in flight. |
| **1.4: more kinds of work** | **Dev environments:** Jupyter or VS Code on a GPU machine, handed back when you are done. **OpenAI-compatible Batch API:** large jobs run at quiet times. |
| **1.5: teams** | Groups with quotas (GPU hours, tokens). Sign-in through SSO (OIDC, e.g. MCP Gate or Google) and two-factor authentication. |
| **1.6: running it for real** | Prometheus metrics and dashboards; alerts (machine offline, GPU errors and temperature, failing deployments); scheduled backup and restore. |

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
- Tune each recipe's memory overhead from real runs (estimates were 7–39% above actual).

## Verification still to do

- **AMD ROCm on real hardware.** The inventory has run on a real Strix Halo (gfx1151, ROCm 7.0.2;
  see docs/acceptance-m1.md): the `gfx` target, unified memory and the 96 GiB pool are right. Still to
  confirm there:
  - the `amdsmi` library loaded from the mounted ROCm, and a discrete card;
  - vLLM's ROCm image;
  - `ROCR_VISIBLE_DEVICES`.
- **The systemd scope for process limits.** It has not run on a host with systemd yet.
- **The one-line install on a systemd host, and the agent image on real GPUs.** Both ran in a
  container without systemd or Docker (the install with a stand-in `systemctl`, the image in CI on a
  runner without GPUs). On a real host, confirm the service (`KillMode=process`: workloads survive
  `systemctl restart forgeyard-agent`), `--gpus all` in the image, and AMD with ROCm mounted.
