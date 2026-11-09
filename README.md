# Forgeyard

One control plane for the GPUs (and CPUs) you own: NVIDIA, AMD ROCm and CPU-only machines
on one LAN or VPN, later rented cloud machines too. Agents report each machine's hardware;
you run services, tasks and dev environments on them, and deploy models from recipes behind
one OpenAI-compatible gateway.

> **Status: milestones M0 and M1 (0.1.0, unreleased).** The server, database, web UI and sign-in
> are in place, and machines join through the agent and report their hardware. Deployments stay
> empty until workloads land (M2).

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

## Accounts and sign-in

- **Roles:** `admin` manages users and sees the audit log; `operator` will run machines and
  deployments; `user` deploys their own work. The **owner** (the first account) is always an
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
| `forgeyard/api/` | `auth` (setup, sign-in, sessions, links), `users`, `audit_log`, `machines` |
| `forgeyard/machines/` | agent protocol, join and stream endpoints, live connections, offline detection |
| `forgeyard/agent/` | the agent: inventory collectors (NVML, AMD SMI, CPU), connection, state file |
| `forgeyard/security/` | passwords, sessions, rate limits, host/CSRF middleware, secrets file |
| `forgeyard/migrations/` | Alembic |
| `web/` | React + TypeScript + Vite UI, built into `forgeyard/static` |
