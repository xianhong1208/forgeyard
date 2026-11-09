# Forgeyard

One control plane for the GPUs (and CPUs) you own: NVIDIA, AMD ROCm and CPU-only machines
on one LAN or VPN, later rented cloud machines too. Agents report each machine's hardware;
you run services, tasks and dev environments on them, and deploy models from recipes behind
one OpenAI-compatible gateway.

> **Status: milestone M0 (0.1.0, unreleased).** The server, database, web UI and sign-in are in
> place. Machines and Deployments are empty pages until agents (M1) and workloads (M2) land.

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
| `forgeyard/api/` | `auth` (setup, sign-in, sessions, links), `users`, `audit_log` |
| `forgeyard/security/` | passwords, sessions, rate limits, host/CSRF middleware, secrets file |
| `forgeyard/migrations/` | Alembic |
| `web/` | React + TypeScript + Vite UI, built into `forgeyard/static` |
