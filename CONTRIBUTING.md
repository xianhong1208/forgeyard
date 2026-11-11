# Contributing

Thank you for helping. Bug reports with the output that shows the problem are as welcome as code.

## Reporting

- **A bug:** open an issue with the *Bug report* form. For anything about a machine's GPUs, attach
  `forgeyard agent inventory --json --raw` from that machine (it holds no secrets).
- **A security problem:** never in an issue; see [SECURITY.md](SECURITY.md).
- **An idea:** open an issue with the *Feature request* form first, so we can agree on the
  behaviour before you write it. [ROADMAP.md](ROADMAP.md) says what is planned.

## Setting up

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), Node 22.12+ for the UI, and Docker
only to build the agent image.

```bash
uv sync
cd web && npm ci && npm run build && cd ..
uv run python main.py          # https://127.0.0.1:7860, prints a one-time setup token
```

A machine without GPUs can still act as one with made-up hardware, for development only:
`FORGEYARD_AGENT_SIMULATION=1 uv run python -m forgeyard agent --simulate-inventory nvidia-a100x8 …`
(fixtures in `forgeyard/agent/fixtures/`; see *Simulated inventory* in [docs/design.md](docs/design.md)).

## Making a change

1. Branch off `dev`; open the pull request into `dev`. `main` only moves at a release.
2. Every behaviour change comes with tests. Test IDs (`FY-<AREA>-NNN`, in each test's name and
   docstring) are unique across the project: take the next free number of the area.
3. Schema changes go through a new Alembic migration in `forgeyard/migrations`; a test fails when
   the models and the migrations drift apart. Existing data must survive the upgrade.
4. User-facing changes update the docs in the same pull request:
   - the guide in **both** languages (`docs/guide/` and `docs/guide/zh-TW/`);
   - `README.md` and `README.zh-TW.md` when the overview changes;
   - `CHANGELOG.md` under *Unreleased*.
5. UI changes: check them at desktop width and at 390 px (no horizontal scroll, no console
   errors).
6. Run the same checks as CI before you push:

   ```bash
   uv run --no-sync ruff check .
   uv run --no-sync python -m pytest -q
   cd web && npm run lint && npm run typecheck && npm test && npm run build
   shellcheck -s sh forgeyard/install/install.sh
   ```

   `TEST_DATABASE_URL=postgresql+psycopg://…` runs the backend suite on PostgreSQL too. It
   **wipes that database**: use a throwaway one.

### New settings

Settings pile up; each one is something people have to understand. Before adding one, prefer, in
order: deriving the value (from the model, the hardware or another setting), a fixed constant (tests
patch it), an advanced setting, and only then an essential one. Every new setting must:

- **have a default that works** for one server and one machine on a LAN. Anything that sends data
  out, spends money, widens access or opens a port defaults to off;
- **be documented**: `forgeyard/config.py`, `.env.example` and the guide's reference in both
  languages (a test fails when they drift; the agent's settings have the same test);
- **be advanced unless it is essential**: in the lower part of `.env.example`, outside the reference's
  *Common settings*, and under a collapsed *Advanced* in a form;
- **say its unit in its name** (`_SECONDS`, `_MIB`), use an existing area prefix (`GATEWAY_`,
  `PROVIDER_`, `EGRESS_`, `MODEL_`, `TLS_`, `AGENT_`, `BUDGET_`), and keep 0 meaning zero, never
  "unlimited";
- **come with a setup advisor check** (`forgeyard/advisor.py` or `forgeyard/settings_check.py`) when
  getting it wrong is risky or silently does nothing, with a test.

A value the server cannot use stops the start; a setting ignored because of another one is a
warning, never silent. Runtime policy (limits, cloud, sources, budgets) lives in the database and
the web UI, not in the environment.

Commit messages say what changed and why, in plain English. Keep pull requests to one topic.

## Licence

Forgeyard is licensed under the [Apache License 2.0](LICENSE). By contributing you agree that your
contribution is licensed the same way (section 5 of the licence).
