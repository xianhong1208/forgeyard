# Changelog

All notable changes are recorded here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. Versions follow [Semantic Versioning](https://semver.org/); each milestone is one minor
version until 1.0.

## [Unreleased]

### Added — M2: generic workloads
- Workloads: **services** (run until stopped, health-checked, restarted with backoff) and **tasks**
  (run to the end; `retries` with doubling backoff; `max_duration` stops and fails a run), 1-16
  fixed replicas, as a **container** (Docker or Podman CLI) or a **process**. A small explicit spec:
  name, kind, runtime, env, resources (cpu, memory, gpus by count or by index, vendor), ports, health,
  restart, max_duration, retries, replicas, machine.
- Placement: the chosen machine, or the first online machine with the runtime, free allowed GPUs and
  the declared CPU and memory. GPUs are exclusive. What does not fit waits as *pending* with the
  reason and is placed when room appears (also by a background sweep).
- Per-machine **GPU allowlist** (`PUT /api/machines/{id}/gpu-allowlist`, admins; Machines page →
  Change GPU access). Cards outside it are never assigned and are marked *not for Forgeyard*; a card
  in use cannot be taken off; audit event `machine.gpu_allowlist_changed` with before and after.
- Isolation: workloads start from an empty environment (never the agent's); GPU visibility by UUID
  (`CUDA_VISIBLE_DEVICES`/`NVIDIA_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES` + `HIP_VISIBLE_DEVICES`)
  for processes, `--gpus "device=<uuid>"` / CDI / `--device /dev/kfd` + the assigned render node for
  containers; empty lists and `NVIDIA_VISIBLE_DEVICES=void` without GPUs. Containers run with
  `--security-opt no-new-privileges`, never `--privileged`, ports published on one address.
- A root agent runs process workloads only as `--workload-user` (refused without one). Processes get
  their own process group, a systemd scope with CPU/memory limits on systemd hosts, rlimits otherwise.
- Desired-state reconciliation: the server sends each agent the full list of its replicas (spec and
  generation) on connect and on change; the agent converges, adopts what it started after a restart
  (pid + start time + boot id; container labels), never starts anything twice, stops before it
  starts, and reports per-replica status (pending, starting, running, healthy, unhealthy, backoff,
  exited, failed, stopped).
- Output: kept per replica on the agent (`--log-max-mb`, 10 MB), live tail on the workload page
  (server asks the agent over the WebSocket), last 100 lines kept on the server when a replica ends.
- Agent protocol v1 extended compatibly: `features` in hello/welcome, and `desired`, `status`,
  `logs_request`, `logs`. M1 agents keep working without work; M2 agents on an M1 server only report
  hardware.
- Agent options `--workload-user`, `--work-dir`, `--port-range`, `--bind-address`,
  `--container-engine`, `--limits`, `--log-max-mb`, `--no-workloads`; `forgeyard agent runtimes`.
- Workloads page (list with replicas, machine, GPUs, addresses), New workload form (with the spec it
  sends), and a workload page with replicas, events, live output, stop / start / delete.
- API `/api/workloads` (list, create, get, stop, start, delete, replica logs). Admins and operators
  run workloads; users see them without environment values or output. Audit events
  `workload.created`, `workload.stopped`, `workload.started`, `workload.deleted`.
- `scripts/gpu_probe.py`: which NVIDIA GPU a workload sees, through the CUDA driver only.
- `docs/acceptance-m2.md`: the real-hardware checklist for a shared 8-GPU host restricted to one card.
- Migration `0003_workloads`.

### Changed — M2
- The *Deployments* placeholder became **Workloads** (`/deployments` redirects); model deployments
  come with M3.
- The AMD collector records the KFD `unique_id` (as `rocr_uuid`) and `drm_render_minor` per card.
- Revoking or removing a machine sends its replicas back to pending (placed elsewhere, or waiting).

### Added — M1: machines join
- Agent in the same package: `python -m forgeyard agent` (also `python main.py agent`). It joins
  with a one-time token, keeps a WebSocket to the server and reconnects by itself (exponential
  backoff with jitter, at most 15 s).
- Join tokens: created by admins on the Machines page, single use, valid 15 minutes, shown once
  and stored as a SHA-256 hash, with the exact command to run. Unused tokens can be cancelled.
- Per-machine credential issued at join, stored hashed on the server and in a 0600 file on the
  machine. Revoking or removing a machine kills the credential and closes its connection at once.
- Agent protocol v1 over `/agent/v1/stream` (JSON `hello`, `heartbeat`, `inventory`, `welcome`,
  `error`, with application close codes), and `POST /agent/v1/join`. Agents authenticate with a
  Bearer credential only. A browser cookie never counts, a request with an `Origin` header is
  refused, and the host check (421) now covers WebSocket handshakes too.
- Heartbeat every 10 s. A server-side sweep marks a machine offline after 30 s of silence and
  closes the dead connection; machines start offline after a server restart.
- Inventory collectors: NVIDIA via NVML (`--extra nvidia`), AMD via the `amdsmi` library, the
  `amd-smi` CLI or `rocm-smi` (gfx target as `arch`; unified-memory APUs such as gfx1151 detected
  from the gfx target or the KFD topology), and CPU and RAM from `/proc`. All read-only, all fail
  soft.
- `python -m forgeyard agent inventory [--json] [--raw]` shows what the agent detects without
  joining. `--simulate-inventory` (with `FORGEYARD_AGENT_SIMULATION=1`) replays a fixture. Fixtures
  ship for 8 × A100 80GB, 2 × RTX 4090 and a Strix Halo APU, and the server marks such machines
  as simulated.
- Machines page: fleet totals, and for each machine its status, last heartbeat, agent version,
  labels, and every card's vendor, model, arch, free / total memory, unified-memory badge,
  driver, utilisation and temperature. It refreshes every 5 s. Admins add, revoke and remove
  machines; everyone signed in can look.
- Audit events `machine.join_token_created`, `machine.join_token_cancelled`, `machine.joined`,
  `machine.join_refused`, `machine.revoked`, `machine.removed`.
- `docs/acceptance-m1.md`: real-hardware checklist (NVIDIA, AMD ROCm, CPU, several machines).
- Settings `AGENT_HEARTBEAT_SECONDS` (10) and `AGENT_OFFLINE_SECONDS` (30).

### Added — M0: skeleton and sign-in
- FastAPI server with SQLAlchemy 2 and Alembic (migrations run at startup); SQLite by default,
  PostgreSQL through `DATABASE_URL`.
- React + TypeScript + Vite web UI served by the server: setup, sign-in, request an account,
  set password, Machines and Deployments (empty until M1/M2), Users, My account and sessions,
  Audit log.
- First run: a one-time setup token printed in the log and kept in a 0600 secrets file, or an
  owner from `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
- Accounts with roles admin, operator and user; a protected owner; argon2id passwords with a
  minimum length and a common-password check; admin-created accounts set their own password
  through a one-time link; optional self-registration with admin approval.
- Server-side sessions: sign-out, password change, disabling an account and admin sign-out end
  them immediately; 2 h idle and 7-day absolute limits; per-session sign-out.
- Host check (421), CSRF protection on API writes, per-IP request budget and sign-in backoff that
  never locks an account for good and never signs anyone out.
- Audit log of sign-ins and account changes.
- CI: ruff and pytest on SQLite and PostgreSQL 17, frontend lint, typecheck, tests and build.
