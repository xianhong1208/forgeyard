# Forgeyard

One control plane for the GPUs (and CPUs) you own: NVIDIA, AMD ROCm and CPU-only machines
on one LAN or VPN, later rented cloud machines too. Agents report each machine's hardware;
you run services, tasks and dev environments on them, and deploy models from recipes behind
one OpenAI-compatible gateway.

> **Status: milestones M0, M1 and M2 (0.1.0, unreleased).** The server, database, web UI and
> sign-in are in place; machines join through the agent and report their hardware; and you run
> services and tasks on them, as containers or processes, with only the GPUs they were given.
> Model deployments come with M3.

[繁體中文說明](README.zh-TW.md)

## Run it

Needs Python 3.13 with [uv](https://docs.astral.sh/uv/), and Node 22.12+ to build the UI.

```bash
cd web && npm ci && npm run build && cd ..   # builds the UI into forgeyard/static
uv sync
uv run python main.py                        # http://127.0.0.1:7860 — migrations run on boot
```

On the first start there is no account. The log prints a **one-time setup token**:

```
Forgeyard has no owner yet. Open http://127.0.0.1:7860/setup and enter this setup token:
    3x…
```

Open `/setup`, paste the token and create the **owner**. The token is also kept in
`data/secrets.json` (mode 0600) until setup is done, then deleted. Without it nobody can claim
a fresh server, so it is safe to start Forgeyard on a shared network before you have set it up.
To skip the page, set `ADMIN_USERNAME` and `ADMIN_PASSWORD` before the first start.

Settings come from the environment or a `.env` file; see [.env.example](.env.example).
`HOST=0.0.0.0` opens it to the LAN. PostgreSQL instead of SQLite:
`uv sync --extra postgres` and `DATABASE_URL=postgresql+psycopg://…`.

## Machines

Each machine runs the **Forgeyard agent** (same repository, `python -m forgeyard agent`). The
agent dials out to the server, so the server never needs to reach the machine.

1. An admin opens **Machines → Add machine** and gets a command with a **one-time join token**
   (single use, valid 15 minutes, shown once, stored as a hash).
2. On the machine, in a Forgeyard checkout (`uv sync`, plus `--extra nvidia` on NVIDIA machines):

   ```bash
   uv run --no-sync python -m forgeyard agent --server http://<server>:7860 --join-token fyj_…
   ```

   The agent trades the token for **this machine's own credential**, which it keeps in its
   state directory (`/var/lib/forgeyard-agent` as root, else `~/.local/state/forgeyard-agent`;
   `--state-dir`) with mode 0600. The server keeps only its SHA-256. Later starts need no token:
   `python -m forgeyard agent`.
3. The agent opens a WebSocket to `/agent/v1/stream` with `Authorization: Bearer <credential>`,
   sends its full inventory, then a heartbeat every 10 s with the values that change (free and
   used memory, utilisation, temperature). It sends the full inventory again when the hardware
   changes.

- **Inventory:** NVIDIA through NVML (`nvidia-ml-py`): model, UUID, compute capability as `arch`
  (`8.0`), memory, driver. AMD through the `amdsmi` library that ships with ROCm, else the
  `amd-smi` CLI, else `rocm-smi`: `arch` is the gfx target (`gfx1100`, `gfx1151`). APUs such as
  Strix Halo are flagged **unified memory**, and their size is the larger of the VRAM carve-out
  and GTT. The CPU and system RAM always appear as one row. A collector that finds nothing
  reports why and never stops the agent. All of these are read-only: no GPU memory is
  allocated, and running processes are not touched.
- **`python -m forgeyard agent inventory [--json] [--raw]`** prints what the agent detects,
  without joining.
- **Online / offline:** the server marks a machine offline when nothing has arrived for 30 s.
  It checks this itself every 2 s, so a pulled cable or a frozen agent is caught even while
  the TCP connection still looks open. The agent reconnects by itself, with exponential
  backoff and jitter capped at 15 s.
- **Revoke and remove** (admins): the credential stops working and an open connection is
  closed at once (the agent exits). Everyone signed in can see the Machines page; only admins
  add, revoke and remove machines. Join tokens, joins, refused joins, revokes and removals are
  in the audit log.
- **Transport:** plain `ws://` is fine on a LAN or VPN, which Forgeyard assumes. For `wss://`
  put the server behind a TLS reverse proxy or Tailscale and pass an `https://` URL. The host
  check applies to agents too, so an agent reaching the server by a name rather than an IP
  needs that name in `ALLOWED_HOSTS` (or as `PUBLIC_URL`). Agent endpoints never accept a
  browser's session cookie, and refuse any request carrying an `Origin` header.
- **Simulated inventory** (development and tests only): `--simulate-inventory nvidia-a100x8 |
  nvidia-rtx4090x2 | amd-gfx1151-apu | <file.json>` reports a fixture instead of real hardware.
  It works only with `FORGEYARD_AGENT_SIMULATION=1`, and the server marks such machines
  *simulated inventory*.

Real-hardware checks for this milestone: [docs/acceptance-m1.md](docs/acceptance-m1.md).

## Workloads

A **workload** is something to run on your machines: a **service** (runs until stopped, is
health-checked and restarted) or a **task** (runs to the end, with retries and a time limit).
Each one has 1 to 16 **replicas**; each replica runs on one machine. Open **Workloads → New
workload**, or `POST /api/workloads` with the spec:

```json
{"name": "web", "kind": "service",
 "runtime": {"type": "container", "image": "python:3.13-slim", "args": ["python", "-m", "http.server", "8000"]},
 "ports": [{"name": "http", "port": 8000}], "health": {"type": "http", "path": "/"},
 "resources": {"cpu": 1, "memory": "512Mi", "gpus": {"count": 0}}, "machine_id": null}
```

| Field | What |
|---|---|
| `runtime` | `{"type": "container", "image", "command"?, "args"?}` (Docker or Podman; `command` replaces the entrypoint) or `{"type": "process", "command", "args"?, "workdir"?}` |
| `env` | explicit variables only (see *Isolation*) |
| `resources` | `cpu` (cores), `memory` (`4Gi`, `512Mi` or bytes), `gpus`: `{"count": n}` or `{"indices": [4]}` (on a chosen machine), optional `"vendor": "nvidia"/"amd"` |
| `ports` | services: `[{"name", "port"}]`. The agent picks the **host port** from its range (30000-32767 by default) and the page shows the address. A process must listen on it: `$PORT`, or `${PORT}` / `${PORT_<NAME>}` in its arguments |
| `health` | services: `{"type": "http" or "tcp", "port", "path", "interval", "timeout", "failure_threshold", "initial_delay"}`. Shown as healthy / unhealthy; it does not restart anything |
| `restart` | services: `always` (default), `on-failure`, `never`. Restarts back off 1, 2, 4… s, at most 60 s |
| `max_duration`, `retries`, `retry_backoff` | tasks: a run longer than `max_duration` seconds is stopped and fails (no retry); a non-zero exit is retried `retries` times, `retry_backoff` seconds apart, doubling, at most 5 min |
| `replicas`, `machine_id` | how many copies; a machine, or `null` for the first online machine with room |

**Placement (M2):** the machine you chose, or the first online machine (by name) that has the
runtime, enough free GPUs on its allowlist, and the CPU and memory the spec declares (counted
against the replicas already there). GPUs are **exclusive**: one card, one replica. Something that
does not fit waits as *pending* with the reason, and is placed as soon as room appears. The real
scheduler (scoring, VRAM estimates, binpacking) is M3.

**Who:** admins and operators create, stop, start and delete workloads and read their output.
Everyone signed in sees the list, the status and the events, but not environment values or output
(either can hold secrets). Creating, stopping, starting and deleting are in the audit log.

**GPU allowlist per machine:** an admin chooses on the Machines page (**Change GPU access**) which
cards Forgeyard may assign on each machine; by default all. A card outside the list is never
assigned, whatever a spec asks; the page marks such cards *not for Forgeyard*. A card in use cannot
be taken off the list (stop its workload first), and every change is audited with before and after.

### Isolation

- **Empty environment.** A process gets `PATH`, `HOME` (its own empty directory), `LANG`,
  `USER`/`LOGNAME`, the declared `env`, its GPU and port variables and `FORGEYARD_REPLICA`,
  `FORGEYARD_WORKLOAD`, `FORGEYARD_REPLICA_INDEX`. A container gets the declared `env`, its GPU
  variables and identity; its image supplies the rest. Nothing of the agent's environment or the
  host's credentials ever reaches a workload. GPU variables and `FORGEYARD_*`/`PORT*` cannot be
  declared in a spec.
- **GPUs, process runtime:** `CUDA_VISIBLE_DEVICES` and `NVIDIA_VISIBLE_DEVICES` hold the
  assigned cards' **UUIDs** (CUDA numbers cards fastest-first, nvidia-smi by PCI bus, so an index
  can name a different card; a UUID cannot), with `CUDA_DEVICE_ORDER=PCI_BUS_ID`. AMD:
  `ROCR_VISIBLE_DEVICES` holds the ROCr UUIDs (`GPU-<KFD unique_id>`, the index when there is none)
  and `HIP_VISIBLE_DEVICES` the renumbered `0..n-1`. Without GPUs every list is empty
  (`NVIDIA_VISIBLE_DEVICES=void`), and on AMD the process also gets none of the device groups, so
  the kernel keeps it off `/dev/kfd`. This is cooperative for NVIDIA (`/dev/nvidia*` are usually
  world-readable); containers are enforced.
- **GPUs, container runtime:** `--gpus "device=<uuid>"` (NVIDIA Container Toolkit) or CDI
  `--device nvidia.com/gpu=<uuid>`; AMD: `--device /dev/kfd` plus only the assigned
  `/dev/dri/renderD*`. `NVIDIA_VISIBLE_DEVICES` is always set (`void` without GPUs), because CUDA
  images declare `all` and a host whose default Docker runtime is `nvidia` would otherwise expose
  every card.
- **Never root by default.** When the agent runs as root, process workloads run as
  `--workload-user` (an unprivileged account, e.g. `nobody` or a dedicated user); without it,
  process workloads are refused and the Machines page says why. An agent that is not root runs
  processes as its own user. Containers run with the engine's defaults plus
  `--security-opt no-new-privileges`, never `--privileged`.
- **Limits.** Processes run in their own process group. On a systemd host with a root agent they get
  a transient scope with `MemoryMax` and `CPUQuota`; otherwise only rlimits: no core dumps, and an
  address-space cap for memory on workloads without GPUs (GPU runtimes reserve huge address
  ranges). CPU is then not capped, only counted for placement. Containers get `--cpus` and
  `--memory`.
- **Ports.** Container ports are published on one address: the agent's own address towards the
  server (`--bind-address` to change it).
- Anyone who can create a workload can run code on the machines. That is why only admins and
  operators can.

### Desired state and the agent

The server keeps, per machine, the full list of replicas that should run there (spec and
generation) and sends it to the agent on connect and on every change (`desired`). The agent
compares it with what actually runs, stops what is not wanted, starts what is missing, applies the
restart, retry and time-limit rules and reports every replica's state (`status`). It never gets
commands such as "start X", so a lost message, a reconnect or an agent restart just converges
again.

- **Adoption:** the agent remembers each replica in `<state dir>/replicas/` (pid, its kernel start
  time and the boot id; container name). A restarted agent finds its processes (the same pids;
  never a stranger that reused a pid) and its containers (labels `forgeyard.agent`,
  `forgeyard.replica`, `forgeyard.generation`), takes them over and starts nothing twice. Until the
  server's first list arrives it only watches. **Stopping the agent leaves workloads running**;
  stop them from the UI.
- **Output:** a process writes straight to `<state dir>/logs/<replica>/output.log`, kept to the last
  `--log-max-mb` MB (10); containers use the engine's log driver with the same budget. The detail
  page shows a live tail: the browser asks the server every 2 s and the server asks the agent over
  its WebSocket. When a replica ends, its last 100 lines are kept on the server for the page.
- **Protocol:** still version 1, extended compatibly. The agent lists `features: ["workloads"]` in
  `hello`, the server answers with its own in `welcome`, and `desired`, `status`,
  `logs_request` / `logs` only flow when both have it. An M1 agent keeps working (it is just
  never given work); an M2 agent on an M1 server only reports hardware.
- **Agent options:** `--workload-user`, `--work-dir` (one directory per process replica; default
  next to the state directory, never inside it), `--port-range` (30000-32767),
  `--bind-address`, `--container-engine auto|docker|podman|none|<path>`,
  `--limits auto|systemd|rlimit`, `--log-max-mb`, `--no-workloads`. `python -m forgeyard agent
  runtimes [--workload-user …]` prints what it can run here without joining.

Real-hardware checks for workloads: [docs/acceptance-m2.md](docs/acceptance-m2.md).

## Accounts and sign-in

- **Roles:** `admin` manages users, machines and their GPU allowlists and sees the audit log;
  `operator` runs workloads; `user` can look (and gets API keys in M4). The **owner** (the first account) is always an
  admin and cannot be demoted, disabled or deleted, so nobody can lock administration out.
- **Passwords:** argon2id; at least 12 characters; common passwords (also with digits or
  symbols tacked on) and passwords containing the username are refused.
- **New users** are created by an admin, who gets a **one-time link** (72 h) to send them; the
  person sets their own password there. Admins can issue a reset link (24 h) the same way. The
  token sits in the URL fragment, so it never reaches server logs or a `Referer`.
- **Self-registration** is off. `REGISTRATION=approval` lets people request an account; it waits
  as *pending* until an admin approves it.
- **Sessions are server-side.** The cookie (`HttpOnly`, `SameSite=Strict`, `Secure` over https)
  holds a random token; the database keeps only its SHA-256. Signing out, changing a password,
  disabling an account or an admin's *Sign out* end the session on the very next request — a
  copied cookie stops working. Sessions end after 2 h idle and 7 days at most, checked on the
  server. *My account* lists your sessions and signs any of them out.
- **Rate limits:** after 5 failed sign-ins on an account each further try waits (2 s, doubling,
  at most 5 min); an IP with 30 failures in 15 min waits too. Nothing is ever locked for good, and
  a limit never signs anyone out. `X-Forwarded-For` counts only from `TRUSTED_PROXIES`.
- **Request guards:** a request for a host name that is not ours gets **421** (DNS rebinding);
  writes to `/api` need the `X-Forgeyard: 1` header and, when the browser sends one, a
  same-origin `Origin` / `Referer`.
- **Audit log:** sign-ins, failed sign-ins and every account change, with who, what and from where.
- The UI goes to the sign-in page only on **401**, never on 403, 429 or a server error, and comes
  back to the page you were on afterwards.

## Develop

```bash
uv sync
uv run --no-sync python -m pytest -q          # backend tests (SQLite)
uv run --no-sync ruff check .
TEST_DATABASE_URL=postgresql+psycopg://user:pw@host/db uv run --no-sync python -m pytest -q
                                               # same suite on PostgreSQL — WIPES that database
cd web
npm run dev                                   # UI on :5173, API proxied to :7860
npm run lint && npm run typecheck && npm test && npm run build
```

Schema changes go through Alembic (`forgeyard/migrations`); startup runs `upgrade head`, and a
test fails when the models and migrations drift apart. Test IDs (`FY-…`) are unique across the
project.

## Layout

| Path | What |
|---|---|
| `forgeyard/app.py` | FastAPI app: guards, routes, the built UI |
| `forgeyard/cli.py` | `python -m forgeyard [serve]` and `python -m forgeyard agent …` |
| `forgeyard/api/` | `auth` (setup, sign-in, sessions, links), `users`, `audit_log`, `machines`, `workloads` |
| `forgeyard/machines/` | agent protocol, join and stream endpoints, live connections, offline detection |
| `forgeyard/workloads/` | the workload spec, placement and the GPU allowlist, desired state and status |
| `forgeyard/agent/` | the agent: inventory collectors (NVML, AMD SMI, CPU), connection, state file |
| `forgeyard/agent/workloads/` | the agent's reconciler, process and container runtimes, logs, health checks |
| `scripts/gpu_probe.py` | checks which NVIDIA GPU a workload sees, through the CUDA driver only |
| `forgeyard/security/` | passwords, sessions, rate limits, host/CSRF middleware, secrets file |
| `forgeyard/migrations/` | Alembic |
| `web/` | React + TypeScript + Vite UI, built into `forgeyard/static` |
