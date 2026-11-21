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
only to build the container images.

```bash
uv sync
cd web && npm ci && npm run build && cd ..
uv run python main.py          # https://127.0.0.1:7860, prints a one-time setup token
```

A machine without GPUs can still act as one with made-up hardware, for development only:
`FORGEYARD_AGENT_SIMULATION=1 uv run python -m forgeyard agent --simulate-inventory nvidia-a100x8 …`
(fixtures in `forgeyard/agent/fixtures/`; see *Simulated inventory* in [docs/design.md](docs/design.md)).

Every development command (tests on SQLite and PostgreSQL, the UI's dev server, the container
images) and a map of the code are in [docs/development.md](docs/development.md).

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
   shellcheck scripts/agent_image_smoke.sh scripts/server_image_smoke.sh scripts/check_dco.sh
   ```

   CI also builds both container images and runs their smoke tests (`agent-image`, `server-image`)
   and builds them for linux/arm64 (`images-arm64`).

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
Every commit is signed off (`git commit -s`, see [Licence](#licence)); the `DCO` check fails on a pull
request with a commit that is not.

## Releasing

Maintainers only. On a release branch off `dev`: bump the version in `pyproject.toml`,
`forgeyard/__init__.py` and `web/package.json` (then `uv lock` and `npm install --package-lock-only`),
date the `CHANGELOG.md` section, and open the release pull request into `dev`. After it merges,
`main` follows `dev`, and the annotated tag `vX.Y.Z` goes on that commit.

**Before tagging, check the Docker Hub secrets.** Every push to `dev` and `main` runs
`.github/workflows/dockerhub-login.yml` (*Docker Hub login*): with the secrets set it logs in to Docker Hub,
asks Docker Hub whether the token may push to the three published repositories
(`scripts/dockerhub_check.py`), pushes nothing and logs out; without them it ends green with the warning
*Docker Hub secrets not set: a release would not push images*. The latest run on `main` (or on `dev`, just
before the release pull request) must be green without that warning:

```bash
gh run list --workflow dockerhub-login.yml --branch main --limit 1
```

When it is red (*unauthorized: incorrect username or password*, *Docker Hub refused the secrets* or *Docker
Hub token cannot push*), re-enter both secrets under *Settings → Secrets and variables → Actions*:
`DOCKERHUB_USERNAME` is the Docker Hub account name (not the email address), `DOCKERHUB_TOKEN` a Docker Hub
personal access token with *Read & Write*. Then run it again by hand (*Actions → Docker Hub login → Run
workflow*) and tag only once it is green. It is not a required check: it does not run on pull requests,
which get no secrets.

Pushing the tag runs `.github/workflows/publish-images.yml`, which publishes both images on Docker
Hub (`docker.io/tonyhuang1101/forgeyard` and `docker.io/tonyhuang1101/forgeyard-agent`) for linux/amd64
and linux/arm64, tagged `X.Y.Z`, `X.Y` and `latest` (a pre-release only `X.Y.Z`), with provenance
and an SBOM. It then pulls the amd64 images back by digest, smoke-tests them and lists the digests
in the run's summary. It needs:

- the namespace in `forgeyard/install/images.py` (`DOCKERHUB_NAMESPACE`), the one place it is
  written: the Machines page names the agent image from it too;
- the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token with
  *Read & Write*).

Without them, or run by hand (*Actions → Publish images → Run workflow*) on a branch, it builds and
smoke-tests both images, pushes nothing and ends green with a notice saying why. Run by hand on a
tag, it publishes that tag again. The tag must be `v` plus the version in `forgeyard/__init__.py`.

The same run publishes the vision image (`docker.io/tonyhuang1101/forgeyard-vision`, `X.Y.Z` and
`X.Y.Z-cuda`). The `vision` recipe pins it by digest, so a release does not change what vision models
run by itself: when the release changed `images/vision`, a follow-up pull request into `dev` moves the
pin to the new version with `scripts/pin_images.py --write` ([docs/development.md](docs/development.md#pinned-engine-images)).

## Licence

Forgeyard is licensed under the [Apache License 2.0](LICENSE). By contributing you agree that your
contribution is licensed the same way (section 5 of the licence).

Contributions are accepted under the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO 1.1): by adding a `Signed-off-by:` line to a commit you certify that you wrote it, or otherwise
have the right to submit it under this licence. `git commit -s` adds the line with your configured
name and email; the email must be the commit's author email. Merge commits and commits by bots are
not checked, and neither are the release pull requests (`dev` → `main` and the `main` → `dev`
back-merge): every commit reached `dev` through its own checked pull request. To sign off commits already on your branch: `git rebase --signoff dev`, then push with
`--force-with-lease`.
