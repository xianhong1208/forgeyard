# Forgeyard

One control plane for the GPUs (and CPUs) you own: NVIDIA, AMD ROCm and CPU-only machines on one
LAN or VPN. An agent on each machine reports its hardware; you run services and tasks on them, and
deploy models from recipes behind one OpenAI-compatible gateway at `/v1`, with API keys, limits
and usage statistics.

[繁體中文說明](README.zh-TW.md)

> **Status: 1.1.1.** 1.0.0 was the MVP (milestones M0 to M4, each accepted on a real 8×A100 host and a CPU
> machine). 1.1 adds a first-run checklist and recommended models, HTTPS out of the box, a
> one-line agent install, an agent container image and this guide. Coming in 1.2 (on `dev`): cloud
> providers behind the same gateway, local first: a model spills to OpenAI, Anthropic, Gemini, Azure
> or OpenRouter when its replicas are busy or down, and only for keys allowed to send data out, with
> the cost of each cloud request counted in US dollars (prices entered by hand or refreshed from
> OpenRouter after review). See
> [CHANGELOG.md](CHANGELOG.md) and [ROADMAP.md](ROADMAP.md).

## The guide

Everything you need to set it up and use it is in the **[guide](docs/guide/README.md)**
([繁體中文](docs/guide/zh-TW/README.md)), also served by Forgeyard itself at `/guide`:

| | |
|---|---|
| [Quick start](docs/guide/quick-start.md) | from nothing to your first `/v1` call in about ten minutes |
| [Concepts](docs/guide/concepts.md) | machines and agents, GPU access, workloads, models, deployments and the gateway, keys, roles |
| [How-to](docs/guide/how-to.md) | AMD and CPU-only machines, the agent in Docker, shared GPUs, your own certificate, upgrades, backups |
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

A machine: **Machines → Add a machine** gives a one-line install that checks the server's CA
against its fingerprint, then installs the agent of the server's own version as a systemd service:

```bash
curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem && \
  sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \
  echo "3f9a…  forgeyard-ca.der" | sha256sum -c && \
  curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | \
  sudo sh -s -- --join fyj_… --ca-fingerprint sha256:3f9a…
```

Or one `docker run` of the agent image (`docker build -f Dockerfile.agent -t forgeyard-agent:<version> .`),
or a checkout with `/etc/forgeyard/agent.toml`. Then deploy a model on **Models** and call it at
`https://192.168.1.20:7860/v1` with an API key and any OpenAI client.

**Upgrading from 1.0:** `uv sync`, restart the server, then run the one-time `agent trust`
command on every machine that joined over plain HTTP; see
[docs/https-and-trust.md](docs/https-and-trust.md#upgrading-from-10-agents-that-joined-over-plain-http).

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
```

Schema changes go through Alembic (`forgeyard/migrations`); startup runs `upgrade head`, and a
test fails when the models and migrations drift apart. Test IDs (`FY-…`) are unique across the
project.

## Layout

| Path | What |
|---|---|
| `forgeyard/app.py` | FastAPI app: guards, routes, the built UI |
| `forgeyard/cli.py` | `python -m forgeyard [serve]` and `python -m forgeyard agent …` |
| `forgeyard/agent/config.py`, `setup.py` | the agent's settings (`agent.toml`, `FORGEYARD_*`, flags) and `agent init` |
| `forgeyard/install/` | the agent's wheel built from the running code, `install.sh`, the commands on the Machines page |
| `forgeyard/guide.py`, `docs/guide/` | the user guide (en, zh-TW), served at `/guide` |
| `Dockerfile.agent`, `scripts/agent_image_smoke.sh` | the agent's container image and its CI smoke test |
| `forgeyard/api/` | `auth` (setup, sign-in, sessions, links), `users`, `audit_log`, `machines`, `workloads` |
| `forgeyard/machines/` | agent protocol, join and stream endpoints, live connections, offline detection |
| `forgeyard/workloads/` | the workload spec, placement and the GPU allowlist, desired state and status |
| `forgeyard/models/` | model sources, the SSRF-guarded fetcher, inspection (safetensors, GGUF), estimates, recipes, the scheduler, routes |
| `forgeyard/recipes/` | the built-in engine recipes (YAML) |
| `forgeyard/api/models.py`, `deployments.py` | catalog, recipes, sources; deployments, estimate, try it, routes |
| `forgeyard/gateway/` | the gateway: API keys, limits, the balancer, forwarding and streaming, usage |
| `forgeyard/api/gateway.py`, `keys.py` | `/v1/*` and the playground; API keys, key limits and usage |
| `scripts/accept_m4.py` | the M4 acceptance run on real machines, with the official OpenAI SDK |
| `forgeyard/agent/models.py` | the agent's model cache: downloads, verification, engines |
| `scripts/accept_m3.py` | the M3 acceptance run on a real machine, through the API |
| `forgeyard/agent/` | the agent: inventory collectors (NVML, AMD SMI, CPU), connection, state file |
| `forgeyard/agent/workloads/` | the agent's reconciler, process and container runtimes, logs, health checks |
| `scripts/gpu_probe.py` | checks which NVIDIA GPU a workload sees, through the CUDA driver only |
| `forgeyard/security/` | passwords, sessions, rate limits, host/CSRF middleware, secrets file |
| `forgeyard/migrations/` | Alembic |
| `web/` | React + TypeScript + Vite UI, built into `forgeyard/static` |
